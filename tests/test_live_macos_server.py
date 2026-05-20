import json
import os
import socket
import struct
import time
import unittest
from dataclasses import dataclass
from typing import Any

from remote_explorer_server.config import DEFAULT_CONTROL_PORT
from remote_explorer_server.protocol import decode_datagram, encode_message, make_request_id
from remote_explorer_server.security import (
    derive_password_key,
    hmac_hex,
    proof_message,
    session_message,
    sign_message,
)
from remote_explorer_server.streaming_protocol import (
    STREAM_CHUNK_BYTES,
    STREAM_HEADER_FORMAT,
    STREAM_HEADER_SIZE,
    STREAM_MAGIC,
)


LIVE_MAC_ENV = "REMOTE_EXPLORER_LIVE_MAC"
DEFAULT_LIVE_HOST = "192.168.10.31"
DEFAULT_LIVE_PASSWORD = "123456"
DEFAULT_LIVE_SECONDS = 8.0


@unittest.skipUnless(os.environ.get(LIVE_MAC_ENV) == "1", f"set {LIVE_MAC_ENV}=1 to run live macOS server tests")
class LiveMacServerStreamTests(unittest.TestCase):
    def test_control_and_udp_stream_from_live_macos_server(self) -> None:
        host = os.environ.get("REMOTE_EXPLORER_LIVE_HOST", DEFAULT_LIVE_HOST)
        port = int(os.environ.get("REMOTE_EXPLORER_LIVE_PORT", str(DEFAULT_CONTROL_PORT)))
        password = os.environ.get("REMOTE_EXPLORER_LIVE_PASSWORD", DEFAULT_LIVE_PASSWORD)
        seconds = float(os.environ.get("REMOTE_EXPLORER_LIVE_STREAM_SECONDS", str(DEFAULT_LIVE_SECONDS)))
        resolution = os.environ.get("REMOTE_EXPLORER_LIVE_STREAM_RESOLUTION", "360p")
        fps = int(os.environ.get("REMOTE_EXPLORER_LIVE_STREAM_FPS", "15"))
        quality = int(os.environ.get("REMOTE_EXPLORER_LIVE_STREAM_QUALITY", "40"))

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
            control.settimeout(3.0)
            control.connect((host, port))
            local_control = control.getsockname()
            session_id, session_key = authenticate(control, password)

            status = send_command(control, session_id, session_key, "status", {})
            self.assertTrue(status.get("ok"), self._json(status))

            navigate = send_command(
                control,
                session_id,
                session_key,
                "navigate",
                {"url": "https://example.com"},
                timeout=5.0,
            )
            self.assertTrue(navigate.get("ok"), self._json(navigate))

            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stream:
                stream.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
                stream.bind(("", 0))
                stream.settimeout(0.25)
                stream_port = int(stream.getsockname()[1])

                start = send_command(
                    control,
                    session_id,
                    session_key,
                    "stream_start",
                    {
                        "port": stream_port,
                        "resolution": resolution,
                        "fps": fps,
                        "quality": quality,
                    },
                    timeout=5.0,
                )
                self.assertTrue(start.get("ok"), self._json(start))
                source_port = int((start.get("result") or {}).get("source_port") or 0)
                if source_port:
                    punch_stream_source(stream, host, source_port)

                stream_status = send_command(control, session_id, session_key, "stream_status", {}, timeout=3.0)
                capture = capture_stream(stream, seconds)

                try:
                    send_command(control, session_id, session_key, "stream_stop", {}, timeout=3.0)
                except Exception:
                    pass

        summary = {
            "server": f"{host}:{port}",
            "local_control": f"{local_control[0]}:{local_control[1]}",
            "local_stream_port": stream_port,
            "requested": {"resolution": resolution, "fps": fps, "quality": quality},
            "stream_start": start.get("result", start),
            "stream_status": stream_status.get("result", stream_status),
            "capture": capture.summary(),
        }
        if os.environ.get("REMOTE_EXPLORER_LIVE_VERBOSE") == "1":
            print("\nLive macOS stream diagnostic:\n" + self._json(summary))
        self.assertGreater(capture.packet_count, 0, "No UDP stream packets received:\n" + self._json(summary))
        self.assertGreater(capture.completed_frames, 0, "UDP packets arrived but no complete JPEG frame was reassembled:\n" + self._json(summary))
        self.assertEqual(capture.invalid_packets, 0, "Invalid stream packets received:\n" + self._json(summary))

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def authenticate(sock: socket.socket, password: str) -> tuple[str, bytes | None]:
    client_id = socket.gethostname() or "python-live-mac-test"
    hello = {
        "v": 1,
        "type": "auth_hello",
        "request_id": make_request_id(),
        "client": {"id": client_id, "name": "Python Live Mac Stream Test"},
    }
    response = request(sock, hello)
    if response.get("type") == "auth_ok":
        return response["session"]["id"], None
    if response.get("type") != "auth_challenge":
        raise RuntimeError(f"Unexpected auth response: {response}")

    challenge = response["challenge"]
    client_nonce = make_request_id()
    key = derive_password_key(password, challenge["salt"], int(challenge["iterations"]))
    proof = hmac_hex(key, proof_message(client_id, challenge["server_nonce"], client_nonce))
    session_key_hex = hmac_hex(key, session_message(client_id, challenge["server_nonce"], client_nonce))
    auth_response = {
        "v": 1,
        "type": "auth_response",
        "request_id": make_request_id(),
        "client_id": client_id,
        "server_nonce": challenge["server_nonce"],
        "client_nonce": client_nonce,
        "proof": proof,
    }
    auth_ok = request(sock, auth_response)
    if auth_ok.get("type") != "auth_ok":
        raise RuntimeError(f"Authentication failed: {auth_ok}")
    return auth_ok["session"]["id"], bytes.fromhex(session_key_hex)


def send_command(
    sock: socket.socket,
    session_id: str,
    session_key: bytes | None,
    command: str,
    payload: dict[str, Any],
    timeout: float = 3.0,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "v": 1,
        "type": "command",
        "request_id": make_request_id(),
        "command": command,
        "payload": payload,
        "auth": {"session_id": session_id},
    }
    if session_key is not None:
        message["auth"]["counter"] = int(time.monotonic_ns())
        message["auth"]["signature"] = sign_message(message, session_key)
    return request(sock, message, timeout)


def request(sock: socket.socket, message: dict[str, Any], timeout: float = 3.0) -> dict[str, Any]:
    previous_timeout = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        sock.send(encode_message(message))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = sock.recv(65535)
            response = decode_datagram(data)
            if response.get("request_id") == message.get("request_id"):
                if response.get("type") == "error":
                    raise RuntimeError(json.dumps(response, ensure_ascii=False))
                return response
    finally:
        sock.settimeout(previous_timeout)
    raise TimeoutError(f"Timed out waiting for {message.get('type')} response")


def capture_stream(sock: socket.socket, seconds: float) -> "StreamCapture":
    capture = StreamCapture()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            packet, address = sock.recvfrom(65535)
        except socket.timeout:
            continue
        capture.accept(packet, address)
    return capture


def punch_stream_source(sock: socket.socket, host: str, port: int) -> None:
    for _ in range(3):
        sock.sendto(b"REXPPING", (host, port))
        time.sleep(0.02)


@dataclass
class PartialFrame:
    frame_id: int
    chunk_count: int
    width: int
    height: int
    source_width: int
    source_height: int
    chunks: list[bytes | None]
    first_seen: float
    last_seen: float

    @property
    def received(self) -> int:
        return sum(1 for chunk in self.chunks if chunk is not None)

    @property
    def missing(self) -> int:
        return self.chunk_count - self.received


class StreamCapture:
    max_chunks = 512

    def __init__(self) -> None:
        self.packet_count = 0
        self.byte_count = 0
        self.invalid_packets = 0
        self.invalid_chunks = 0
        self.completed_frames = 0
        self.completed_frame_ids: list[int] = []
        self.sources: dict[str, int] = {}
        self.partials: dict[int, PartialFrame] = {}
        self.max_observed_chunk_count = 0

    def accept(self, packet: bytes, address: tuple[str, int]) -> None:
        self.packet_count += 1
        self.byte_count += len(packet)
        self.sources[f"{address[0]}:{address[1]}"] = self.sources.get(f"{address[0]}:{address[1]}", 0) + 1
        if len(packet) <= STREAM_HEADER_SIZE:
            self.invalid_packets += 1
            return

        magic, frame_id, chunk_index, chunk_count, width, height, source_width, source_height = struct.unpack(
            STREAM_HEADER_FORMAT,
            packet[:STREAM_HEADER_SIZE],
        )
        if magic != STREAM_MAGIC:
            self.invalid_packets += 1
            return
        if chunk_count == 0 or chunk_count > self.max_chunks or chunk_index >= chunk_count:
            self.invalid_chunks += 1
            return

        self.max_observed_chunk_count = max(self.max_observed_chunk_count, chunk_count)
        now = time.monotonic()
        partial = self.partials.get(frame_id)
        if partial is None:
            partial = PartialFrame(
                frame_id=frame_id,
                chunk_count=chunk_count,
                width=width,
                height=height,
                source_width=source_width,
                source_height=source_height,
                chunks=[None] * chunk_count,
                first_seen=now,
                last_seen=now,
            )
            self.partials[frame_id] = partial

        if partial.chunk_count != chunk_count or partial.width != width or partial.height != height:
            self.invalid_chunks += 1
            self.partials.pop(frame_id, None)
            return

        partial.last_seen = now
        if partial.chunks[chunk_index] is None:
            partial.chunks[chunk_index] = packet[STREAM_HEADER_SIZE:]

        if partial.missing == 0:
            self.completed_frames += 1
            self.completed_frame_ids.append(frame_id)
            self.partials.pop(frame_id, None)

    def summary(self) -> dict[str, Any]:
        partials = sorted(self.partials.values(), key=lambda item: item.frame_id)
        newest = partials[-8:]
        return {
            "packets": self.packet_count,
            "bytes": self.byte_count,
            "completed_frames": self.completed_frames,
            "completed_frame_ids": self.completed_frame_ids[-10:],
            "invalid_packets": self.invalid_packets,
            "invalid_chunks": self.invalid_chunks,
            "sources": self.sources,
            "partial_frames": len(self.partials),
            "max_observed_chunk_count": self.max_observed_chunk_count,
            "newest_partials": [
                {
                    "frame_id": partial.frame_id,
                    "size": f"{partial.width}x{partial.height}",
                    "source_size": f"{partial.source_width}x{partial.source_height}",
                    "chunks": partial.chunk_count,
                    "received": partial.received,
                    "missing": partial.missing,
                }
                for partial in newest
            ],
        }


if __name__ == "__main__":
    unittest.main()
