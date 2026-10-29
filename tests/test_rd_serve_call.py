import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from remote_dispatch.rd_serve import ServeFailed, ServeUnavailable, serve_call


class ServeCallTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "s.sock"

    def serve_once(self, reply):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        server.listen(1)
        self.addCleanup(server.close)

        def run():
            connection, _ = server.accept()
            with connection:
                connection.settimeout(0.3)
                try:
                    while connection.recv(65536):
                        pass
                except socket.timeout:
                    pass
                connection.sendall(reply)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)

    def test_missing_socket_is_unavailable_and_absent(self):
        with self.assertRaises(ServeUnavailable) as caught:
            serve_call("GET", "/v1/x", socket_path=self.path)
        self.assertTrue(caught.exception.absent)

    def test_refused_socket_is_unavailable_and_absent(self):
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.path))
        stale.close()
        with self.assertRaises(ServeUnavailable) as caught:
            serve_call("GET", "/v1/x", socket_path=self.path)
        self.assertTrue(caught.exception.absent)

    def test_json_reply_returns_status_and_body(self):
        self.serve_once(b'HTTP/1.0 201 Created\r\nContent-Type: application/json\r\n\r\n{"ok": true}')
        self.assertEqual(serve_call("POST", "/v1/x", {"a": 1}, socket_path=self.path), (201, {"ok": True}))

    def test_disconnect_after_send_is_failed(self):
        self.serve_once(b"")
        with self.assertRaises(ServeFailed):
            serve_call("POST", "/v1/x", {"a": 1}, socket_path=self.path)

    def test_non_json_reply_is_failed(self):
        self.serve_once(b"HTTP/1.0 200 OK\r\n\r\nnot json")
        with self.assertRaises(ServeFailed):
            serve_call("GET", "/v1/x", socket_path=self.path)


if __name__ == "__main__":
    unittest.main()
