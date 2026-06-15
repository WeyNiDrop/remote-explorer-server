from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
from typing import Any


class CdpError(RuntimeError):
    pass


def http_json(url: str, timeout: float = 5.0, method: str = "GET") -> Any:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class CdpConnection:
    def __init__(self, websocket_url: str) -> None:
        self.websocket = _WebSocket(websocket_url)
        self.next_id = 0
        self.lock = threading.Lock()
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()
        self.websocket.close()

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 10.0) -> Any:
        timeout = max(0.001, float(timeout))
        deadline = time.monotonic() + timeout
        if not self.lock.acquire(timeout=timeout):
            raise CdpError(f"{method} timed out waiting for the CDP connection")
        try:
            if self.closed.is_set():
                raise CdpError("CDP connection is closed")
            self.next_id += 1
            message_id = self.next_id
            try:
                self.websocket.send_json({"id": message_id, "method": method, "params": params or {}})
            except OSError as exc:
                raise CdpError(f"{method} could not be sent: {exc}") from exc
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CdpError(f"{method} timed out after {timeout:.1f}s")
                try:
                    message = self.websocket.recv_json(remaining)
                except (TimeoutError, socket.timeout) as exc:
                    raise CdpError(f"{method} timed out after {timeout:.1f}s") from exc
                except OSError as exc:
                    raise CdpError(f"{method} connection failed: {exc}") from exc
                if message.get("id") != message_id:
                    continue
                if "error" in message:
                    error = message["error"]
                    raise CdpError(f"{method} failed: {error}")
                return message.get("result") or {}
        finally:
            self.lock.release()


class _WebSocket:
    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise CdpError(f"Unsupported WebSocket URL: {url}")
        if parsed.scheme == "wss":
            raise CdpError("wss CDP endpoints are not supported")

        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.socket = socket.create_connection((self.host, self.port), timeout=5.0)
        self._handshake()

    def close(self) -> None:
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.socket.close()
        except OSError:
            pass

    def send_json(self, payload: dict[str, Any]) -> None:
        self._send_text(json.dumps(payload, separators=(",", ":")))

    def recv_json(self, timeout: float) -> dict[str, Any]:
        text = self._recv_text(timeout)
        return json.loads(text)

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.socket.sendall(request.encode("ascii"))
        response = self._recv_http_response()
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise CdpError(f"WebSocket handshake failed: {response[:200]!r}")

        expected = base64.b64encode(hashlib.sha1((key + self.GUID).encode("ascii")).digest()).decode("ascii")
        if expected.lower().encode("ascii") not in response.lower():
            raise CdpError("WebSocket handshake accept key mismatch")

    def _recv_http_response(self) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.socket.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    def _send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))

        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(bytes(header) + mask + masked)

    def _recv_text(self, timeout: float) -> str:
        self.socket.settimeout(timeout)
        fragments: list[bytes] = []
        receiving_text = False
        while True:
            final, opcode, payload = self._recv_frame()
            if opcode == 0x8:
                raise CdpError("WebSocket closed")
            if opcode == 0x9:
                self._send_pong(payload)
                continue
            if opcode == 0x1:
                fragments = [payload]
                receiving_text = True
                if final:
                    return b"".join(fragments).decode("utf-8")
                continue
            if opcode == 0x0 and receiving_text:
                fragments.append(payload)
                if final:
                    return b"".join(fragments).decode("utf-8")

    def _send_pong(self, payload: bytes) -> None:
        header = bytearray([0x8A])
        length = len(payload)
        header.append(0x80 | length)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(bytes(header) + mask + masked)

    def _recv_frame(self) -> tuple[bool, int, bytes]:
        first_two = self._recv_exact(2)
        first, second = first_two
        final = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]

        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return final, opcode, payload

    def _recv_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.socket.recv(size - len(data))
            if not chunk:
                raise CdpError("WebSocket connection closed")
            data.extend(chunk)
        return bytes(data)
