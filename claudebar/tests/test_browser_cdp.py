"""The DevTools client used to read the session token from the live page.

Reading it from the browser profile on disk waits on Chromium flushing its
storage, which measured about a minute; asking the page returns it at once.
The WebSocket framing is hand-rolled, so it is tested rather than trusted.
"""

import json
import socket

import browser_cdp as cdp

TOKEN = "QOdEKnsm8ByIxG4aU62U3+tpiWPyAOw4cMGY5w0xR8jOyzlJzbixXigIUavD8+L+"


class TestUnwrap:
    """localStorage stores an object, so the obvious read returns JSON."""

    def test_a_bare_token_passes_through(self):
        assert cdp.unwrap(TOKEN) == TOKEN

    def test_the_stored_object_is_unwrapped(self):
        assert cdp.unwrap('{"value":"abc123","__version":"0"}') == "abc123"

    def test_surrounding_whitespace_is_trimmed(self):
        assert cdp.unwrap(f"  {TOKEN}  ") == TOKEN

    def test_nothing_returns_none(self):
        assert cdp.unwrap("") is None
        assert cdp.unwrap("   ") is None

    def test_an_object_without_a_value_returns_none(self):
        """Better to report nothing than to return a JSON blob as a token."""
        assert cdp.unwrap('{"__version":"0"}') is None

    def test_malformed_json_returns_none(self):
        assert cdp.unwrap('{"value": broken') is None


class TestDebugPort:
    def test_reads_the_first_line_of_the_port_file(self, tmp_path):
        (tmp_path / "DevToolsActivePort").write_text("55305\n/browser/path\n", encoding="utf-8")
        assert cdp.debug_port(tmp_path) == 55305

    def test_a_missing_file_is_none(self, tmp_path):
        assert cdp.debug_port(tmp_path) is None

    def test_garbage_is_none(self, tmp_path):
        (tmp_path / "DevToolsActivePort").write_text("not a port\n", encoding="utf-8")
        assert cdp.debug_port(tmp_path) is None

    def test_an_empty_file_is_none(self, tmp_path):
        (tmp_path / "DevToolsActivePort").write_text("", encoding="utf-8")
        assert cdp.debug_port(tmp_path) is None


class TestFraming:
    """RFC6455: client frames must be masked, and lengths switch encoding at
    126 and 65536 bytes."""

    def test_a_small_frame_is_masked_text(self):
        frame = cdp._mask_frame(b"hi")
        assert frame[0] == 0x81            # FIN + text opcode
        assert frame[1] & 0x80             # mask bit set, as a client must
        assert frame[1] & 0x7F == 2        # inline length

    def test_the_payload_is_recoverable(self):
        payload = b"hello websocket"
        frame = cdp._mask_frame(payload)
        mask = frame[2:6]
        body = frame[6:]
        assert bytes(b ^ mask[i % 4] for i, b in enumerate(body)) == payload

    def test_medium_lengths_use_two_bytes(self):
        frame = cdp._mask_frame(b"x" * 200)
        assert frame[1] & 0x7F == 126
        assert int.from_bytes(frame[2:4], "big") == 200

    def test_large_lengths_use_eight_bytes(self):
        frame = cdp._mask_frame(b"x" * 70000)
        assert frame[1] & 0x7F == 127
        assert int.from_bytes(frame[2:10], "big") == 70000

    def test_a_server_frame_is_parsed(self):
        """Server frames are unmasked, which is the mirror of what we send."""
        a, b = socket.socketpair()
        try:
            payload = json.dumps({"id": 1, "result": {"result": {"value": TOKEN}}}).encode()
            b.sendall(bytes([0x81, len(payload)]) + payload)
            opcode, body = cdp._read_frame(a)
            assert opcode == 0x1
            assert json.loads(body.decode())["result"]["result"]["value"] == TOKEN
        finally:
            a.close()
            b.close()

    def test_a_masked_server_frame_is_unmasked(self):
        a, b = socket.socketpair()
        try:
            payload = b"masked"
            mask = b"\x01\x02\x03\x04"
            body = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            b.sendall(bytes([0x81, 0x80 | len(payload)]) + mask + body)
            opcode, out = cdp._read_frame(a)
            assert out == payload
        finally:
            a.close()
            b.close()

    def test_a_medium_server_frame_is_parsed(self):
        a, b = socket.socketpair()
        try:
            payload = b"y" * 300
            b.sendall(bytes([0x81, 126]) + len(payload).to_bytes(2, "big") + payload)
            opcode, out = cdp._read_frame(a)
            assert out == payload
        finally:
            a.close()
            b.close()

    def test_a_close_frame_ends_the_stream(self):
        a, b = socket.socketpair()
        try:
            b.sendall(bytes([0x88, 0x00]))
            assert cdp._read_frame(a) is None
        finally:
            a.close()
            b.close()


class TestAgainstARealServer:
    """One end-to-end pass over the handshake and a Runtime.evaluate exchange,
    against a throwaway socket, so the client is exercised as the browser
    would exercise it."""

    def test_evaluate_returns_the_value(self):
        import threading

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        seen = {}

        def serve():
            conn, _ = server.accept()
            with conn:
                request = b""
                while b"\r\n\r\n" not in request:
                    request += conn.recv(1)
                seen["request"] = request.decode("utf-8", "replace")
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                             b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
                opcode, payload = cdp._read_frame(conn)
                seen["sent"] = json.loads(payload.decode("utf-8"))
                reply = json.dumps({"id": 1, "result": {"result": {"value": TOKEN}}}).encode()
                conn.sendall(bytes([0x81, len(reply)]) + reply)
            server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        value = cdp.evaluate(f"ws://127.0.0.1:{port}/devtools/page/x",
                             "localStorage.getItem('userToken')")
        thread.join(timeout=5)

        assert value == TOKEN
        assert "Sec-WebSocket-Key" in seen["request"]
        assert seen["sent"]["method"] == "Runtime.evaluate"
        assert seen["sent"]["params"]["returnByValue"] is True

    def test_a_refused_connection_returns_none(self):
        """No browser, no port: the caller falls back rather than raising."""
        assert cdp.evaluate("ws://127.0.0.1:1/devtools/page/x", "1") is None
