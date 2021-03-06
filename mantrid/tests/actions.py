import os
import errno
import socket
import time
import eventlet
import unittest
httplib2 = eventlet.import_patched("httplib2")
from eventlet.timeout import Timeout
from ..loadbalancer import Balancer
from ..actions import Empty, Static, Unknown, NoHosts, Redirect, Proxy, Spin


class MockBalancer(object):
    "Fake Balancer class for testing."

    def __init__(self, fixed_action=None):
        self.fixed_action = None
        self.static_dir = "/tmp/"

    def resolve_host(self, host):
        return self.fixed_action


class MockSocket(object):
    "Fake Socket class that remembers what was sent. Doesn't implement sendfile."

    def __init__(self):
        self.data = ""

    def send(self, data):
        self.data += data
        return len(data)

    def sendall(self, data):
        self.data += data

    def close(self):
        pass


class MockErrorSocket(object):
    "Fake Socket class that raises a specific error message on use."

    def __init__(self, error_code):
        self.error_code = error_code

    def _error(self, *args, **kwargs):
        raise socket.error(self.error_code, os.strerror(self.error_code))
    sendall = _error


class ActionTests(unittest.TestCase):
    "Tests the various actions"

    def test_empty(self):
        "Tests the Empty action"
        action = Empty(MockBalancer(), "zomg-lol.com", "zomg-lol.com", code=500)
        sock = MockSocket()
        action.handle(sock, "", "/", {})
        self.assertEqual(
            "HTTP/1.0 500 Internal Server Error\r\nConnection: close\r\nContent-length: 0\r\n\r\n",
            sock.data,
        )

    def test_handle(self):
        "Tests the Static action"
        action = Static(MockBalancer(), "kittens.net", "kittens.net", type="timeout")
        sock = MockSocket()
        action.handle(sock, "", "/", {})
        self.assertEqual(
            open(os.path.join(os.path.dirname(__file__), "..", "static", "timeout.http")).read(),
            sock.data,
        )

    def test_unknown(self):
        "Tests the Unknown action"
        action = Unknown(MockBalancer(), "firefly.org", "firefly.org")
        sock = MockSocket()
        action.handle(sock, "", "/", {})
        self.assertEqual(
            open(os.path.join(os.path.dirname(__file__), "..", "static", "unknown.http")).read(),
            sock.data,
        )

    def test_nohosts(self):
        "Tests the NoHosts action"
        action = NoHosts(MockBalancer(), "thevoid.local", "thevoid.local")
        sock = MockSocket()
        action.handle(sock, "", "/", {})
        self.assertEqual(
            open(os.path.join(os.path.dirname(__file__), "..", "static", "no-hosts.http")).read(),
            sock.data,
        )

    def test_redirect(self):
        "Tests the Redirect action"
        action = Redirect(MockBalancer(), "lions.net", "lions.net", redirect_to="http://tigers.net")
        # Test with root path
        sock = MockSocket()
        action.handle(sock, "", "/", {})
        self.assertEqual(
            "HTTP/1.0 302 Found\r\nLocation: http://tigers.net/\r\n\r\n",
            sock.data,
        )
        # Test with non-root path
        sock = MockSocket()
        action.handle(sock, "", "/bears/", {})
        self.assertEqual(
            "HTTP/1.0 302 Found\r\nLocation: http://tigers.net/bears/\r\n\r\n",
            sock.data,
        )
        # Test with https
        action = Redirect(MockBalancer(), "oh-my.com", "oh-my.com", redirect_to="https://meme-overload.com")
        sock = MockSocket()
        action.handle(sock, "", "/bears2/", {})
        self.assertEqual(
            "HTTP/1.0 302 Found\r\nLocation: https://meme-overload.com/bears2/\r\n\r\n",
            sock.data,
        )
        # Test with same-protocol
        action = Redirect(MockBalancer(), "example.com", "example.com", redirect_to="example.net")
        sock = MockSocket()
        action.handle(sock, "", "/test/", {})
        self.assertEqual(
            "HTTP/1.0 302 Found\r\nLocation: http://example.net/test/\r\n\r\n",
            sock.data,
        )
        sock = MockSocket()
        action.handle(sock, "", "/test/", {"X-Forwarded-Protocol": "SSL"})
        self.assertEqual(
            "HTTP/1.0 302 Found\r\nLocation: https://example.net/test/\r\n\r\n",
            sock.data,
        )

    def test_proxy(self):
        "Tests the Proxy action"
        # Check failure with no backends
        self.assertRaises(
            AssertionError,
            lambda: Proxy(MockBalancer(), "khaaaaaaaaaaaaan.xxx", "khaaaaaaaaaaaaan.xxx", backends=[]),
        )
        # TODO: launch local server, proxy to that

    def test_spin(self):
        "Tests the Spin action"
        # Set the balancer up to return a Spin
        balancer = MockBalancer()
        action = Spin(balancer, "aeracode.org", "aeracode.org", timeout=2, check_interval=1)
        balancer.fixed_action = action
        # Ensure it times out
        sock = MockSocket()
        try:
            with Timeout(2.2):
                start = time.time()
                action.handle(sock, "", "/", {})
                duration = time.time() - start
        except Timeout:
            self.fail("Spin lasted for too long")
        self.assert_(
            duration >= 1,
            "Spin did not last for long enough"
        )
        self.assertEqual(
            open(os.path.join(os.path.dirname(__file__), "..", "static", "timeout.http")).read(),
            sock.data,
        )
        # Now, ensure it picks up a change
        sock = MockSocket()
        try:
            with Timeout(2):
                def host_changer():
                    eventlet.sleep(0.7)
                    balancer.fixed_action = Empty(balancer, "aeracode.org", "aeracode.org", code=402)
                eventlet.spawn(host_changer)
                action.handle(sock, "", "/", {})
        except Timeout:
            self.fail("Spin lasted for too long")
        self.assertEqual(
            "HTTP/1.0 402 Payment Required\r\nConnection: close\r\nContent-length: 0\r\n\r\n",
            sock.data,
        )

    def test_socket_errors(self):
        for action in [
            Empty(MockBalancer(), "", "", code=500),
            Unknown(MockBalancer(), "", ""),
            Redirect(MockBalancer(), "", "", redirect_to="http://pypy.org/"),
        ]:
            sock = MockErrorSocket(errno.EPIPE)
            # Doesn't error
            action.handle(sock, "", "/", {})
            sock = MockErrorSocket(errno.EBADF)
            with self.assertRaises(socket.error) as cm:
                action.handle(sock, "", "/", {})
            self.assertEqual(cm.exception.errno, errno.EBADF)


class LiveActionTests(unittest.TestCase):
    """
    Tests that the client/API work correctly.
    """

    next_port = 30300

    def setUp(self):
        self.__class__.next_port += 3
        self.balancer = Balancer(
            [(("0.0.0.0", self.next_port), socket.AF_INET)],
            [(("0.0.0.0", self.next_port + 1), socket.AF_INET)],
            [(("0.0.0.0", self.next_port + 2), socket.AF_INET)],
            "/tmp/mantrid-test-state-2",
        )
        self.balancer_thread = eventlet.spawn(self.balancer.run)
        eventlet.sleep(0.1)
        self.balancer.hosts = {
            "test-host.com": ["static", {"type": "test"}, True],
        }
    
    def tearDown(self):
        self.balancer.running = False
        self.balancer_thread.kill()
        eventlet.sleep(0.1)

    def test_unknown(self):
        # Send a HTTP request to the balancer, ensure the response
        # is the same as the "unknown" template
        h = httplib2.Http()
        resp, content = h.request(
            "http://127.0.0.1:%i" % self.next_port,
            "GET",
        )
        self.assertEqual(
            '503',
            resp['status'],
        )
        expected_content = open(os.path.join(os.path.dirname(__file__), "..", "static", "unknown.http")).read()
        expected_content = expected_content[expected_content.index("\r\n\r\n") + 4:]
        self.assertEqual(
            expected_content,
            content,
        )
