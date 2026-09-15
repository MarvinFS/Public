"""Minimal Chrome DevTools Protocol client, for reading the sign-in page.

The session token has to come from the live page rather than from the browser
profile on disk. Chromium buffers its localStorage writes, so the file only
catches up long after the user has visibly finished signing in, which measured
about a minute and looked like the button had done nothing.

The browser is started with ``--remote-debugging-port=0``, which picks a free
port and writes it to ``DevToolsActivePort`` in the profile. That port is
loopback-only and lives no longer than the sign-in window.

Chrome DevTools speaks RFC6455, and this is the only thing ClaudeBar needs
from it, so the client is hand-rolled rather than pulled in as a dependency.
"""

import base64
import json
import os
import socket
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

CONNECT_TIMEOUT = 3.0


def debug_port(profile: Path) -> Optional[int]:
    """The port the browser chose, from the file it writes into the profile."""
    try:
        first = (profile / "DevToolsActivePort").read_text(encoding="utf-8")
        return int(first.splitlines()[0].strip())
    except (OSError, ValueError, IndexError):
        return None


def _http_json(url: str, timeout: float = CONNECT_TIMEOUT):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def page_targets(port: int) -> list:
    """Open page targets, newest first, with their WebSocket URLs."""
    targets = _http_json(f"http://127.0.0.1:{port}/json/list")
    if not isinstance(targets, list):
        return []
    return [t for t in targets
            if isinstance(t, dict) and t.get("type") == "page"
            and t.get("webSocketDebuggerUrl")]


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        part = sock.recv(count - len(data))
        if not part:
            raise OSError("connection closed")
        data += part
    return data


def _mask_frame(payload: bytes) -> bytes:
    """A client frame, which RFC6455 requires to be masked."""
    mask = os.urandom(4)
    header = bytearray([0x81])            # FIN + text
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < (1 << 16):
        header.append(0x80 | 126)
        header += length.to_bytes(2, "big")
    else:
        header.append(0x80 | 127)
        header += length.to_bytes(8, "big")
    header += mask
    body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + body


def _read_frame(sock: socket.socket) -> Optional[tuple]:
    """One frame as (opcode, payload), or None when the peer closed."""
    first, second = _recv_exact(sock, 2)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(_recv_exact(sock, 2), "big")
    elif length == 127:
        length = int.from_bytes(_recv_exact(sock, 8), "big")
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if opcode == 0x8:
        return None
    return opcode, payload


def evaluate(ws_url: str, expression: str, timeout: float = CONNECT_TIMEOUT) -> Optional[str]:
    """Evaluate `expression` in a page and return its string value, or None."""
    parsed = urlparse(ws_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/"

    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode()

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(handshake)

            header = b""
            while b"\r\n\r\n" not in header:
                header += _recv_exact(sock, 1)
            if b" 101 " not in header.split(b"\r\n")[0]:
                return None

            request = json.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {"expression": expression, "returnByValue": True},
            }).encode()
            sock.sendall(_mask_frame(request))

            for _ in range(10):                    # skip any ping or event frames
                frame = _read_frame(sock)
                if frame is None:
                    return None
                opcode, payload = frame
                if opcode not in (0x1, 0x2):
                    continue
                message = json.loads(payload.decode("utf-8"))
                result = ((message.get("result") or {}).get("result")) or {}
                value = result.get("value")
                return value if isinstance(value, str) and value else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return None


def unwrap(raw: str) -> Optional[str]:
    """localStorage holds {"value":"...","__version":"0"}, so unwrap it."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        inner = data.get("value") if isinstance(data, dict) else None
        return inner.strip() if isinstance(inner, str) and inner.strip() else None
    return text


def token_from_page(profile: Path) -> Optional[str]:
    """The session token from the live sign-in page, or None while it is absent."""
    port = debug_port(profile)
    if not port:
        return None
    for target in page_targets(port):
        raw = evaluate(target["webSocketDebuggerUrl"],
                       "localStorage.getItem('userToken')")
        token = unwrap(raw) if raw else None
        if token:
            return token
    return None
