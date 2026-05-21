import math
import socket
import struct
import sys
import threading
import time
import unittest
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

from remote_explorer_server import streaming_protocol

try:
    from PySide6.QtGui import QImage

    from remote_explorer_server import chromium_backend
    from remote_explorer_server.chromium_backend import (
        ChromiumBrowserService,
        ChromiumStreamConfig,
        ChromiumStreamService,
    )
except ImportError:  # pragma: no cover - server UI dependencies are optional in CI test jobs.
    QImage = None
    chromium_backend = None
    ChromiumBrowserService = None
    ChromiumStreamConfig = None
    ChromiumStreamService = None


class StreamingProtocolPacketTests(unittest.TestCase):
    def test_stream_packet_header_layout_matches_unity_client(self) -> None:
        header = struct.pack(
            streaming_protocol.STREAM_HEADER_FORMAT,
            streaming_protocol.STREAM_MAGIC,
            42,
            1,
            3,
            640,
            360,
            1280,
            720,
        )

        self.assertEqual(streaming_protocol.STREAM_HEADER_SIZE, 24)
        self.assertEqual(len(header), streaming_protocol.STREAM_HEADER_SIZE)
        self.assertEqual(
            struct.unpack(streaming_protocol.STREAM_HEADER_FORMAT, header),
            (streaming_protocol.STREAM_MAGIC, 42, 1, 3, 640, 360, 1280, 720),
        )
        self.assertLessEqual(
            streaming_protocol.STREAM_HEADER_SIZE + streaming_protocol.STREAM_CHUNK_BYTES,
            1200,
        )

    def test_stream_host_accepts_ipv4_mapped_qhostaddress(self) -> None:
        self.assertEqual(streaming_protocol.clean_host("[::ffff:192.168.1.25]"), "192.168.1.25")
        self.assertEqual(streaming_protocol.clean_host("::ffff:10.0.0.7"), "10.0.0.7")

    @unittest.skipUnless(sys.platform == "darwin", "macOS UDP pacing defaults are only active on macOS")
    def test_macos_udp_stream_defaults_are_low_burst(self) -> None:
        self.assertEqual(streaming_protocol.MAX_STREAM_CHUNKS_PER_FRAME, 48)
        self.assertEqual(streaming_protocol.STREAM_CHUNK_PACE_BATCH, 4)
        self.assertEqual(streaming_protocol.STREAM_CHUNK_PACE_SECONDS, 0.003)
        self.assertEqual(streaming_protocol.DEFAULT_MACOS_UDP_STREAM_FPS, 15)


class FakeTimer:
    def __init__(self) -> None:
        self.interval = 0
        self.active = False

    def setInterval(self, interval: int) -> None:
        self.interval = interval

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def isActive(self) -> bool:
        return self.active


class FakeChromiumBrowser:
    def __init__(self, image: Any | None = None) -> None:
        self.image = image
        self.minimum_viewports: list[tuple[int, int]] = []
        self.stream_viewports: list[tuple[int, int]] = []
        self.cleared_stream_viewports = 0
        self.noted_capture_sizes: list[tuple[int, int]] = []

    def ensure_minimum_viewport(self, width: int, height: int) -> None:
        self.minimum_viewports.append((width, height))

    def set_stream_viewport(self, width: int, height: int) -> None:
        self.stream_viewports.append((width, height))

    def clear_stream_viewport(self) -> None:
        self.cleared_stream_viewports += 1

    def capture_jpeg(self, quality: int) -> bytes:
        return chromium_backend._encode_jpeg(self.image, quality)  # type: ignore[union-attr]

    def note_capture_size(self, width: int, height: int) -> None:
        self.noted_capture_sizes.append((width, height))


class FakeSocket:
    def __init__(self, source_port: int = 0) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.source_port = source_port

    def sendto(self, packet: bytes, address: tuple[str, int]) -> int:
        self.sent.append((packet, address))
        return len(packet)

    def getsockname(self) -> tuple[str, int]:
        return ("0.0.0.0", self.source_port)


class FakeCdpConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.metrics = {
            "innerWidth": 1280,
            "innerHeight": 720,
            "outerWidth": 1280,
            "outerHeight": 800,
        }

    def call(self, method: str, payload: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        del timeout
        self.calls.append((method, payload or {}))
        if method == "Runtime.evaluate":
            return {"result": {"value": dict(self.metrics)}}
        return {}

    def close(self) -> None:
        self.calls.append(("close", {}))


@dataclass
class ReceivedStreamFrame:
    frame_id: int
    width: int
    height: int
    source_width: int
    source_height: int
    jpeg: bytes


class DatagramStreamReceiver:
    # Keep this in sync with RemoteExplorerClient.MaxStreamChunks.
    max_chunks = 512

    def __init__(self) -> None:
        self.frames: list[ReceivedStreamFrame] = []
        self.partials: dict[int, _PartialFrame] = {}
        self.invalid_packets = 0
        self.invalid_chunks = 0

    def accept(self, packet: bytes) -> ReceivedStreamFrame | None:
        if len(packet) <= streaming_protocol.STREAM_HEADER_SIZE:
            self.invalid_packets += 1
            return None

        header = packet[: streaming_protocol.STREAM_HEADER_SIZE]
        payload = packet[streaming_protocol.STREAM_HEADER_SIZE :]
        magic, frame_id, chunk_index, chunk_count, width, height, source_width, source_height = struct.unpack(
            streaming_protocol.STREAM_HEADER_FORMAT,
            header,
        )
        if magic != streaming_protocol.STREAM_MAGIC:
            self.invalid_packets += 1
            return None
        if chunk_count == 0 or chunk_count > self.max_chunks or chunk_index >= chunk_count:
            self.invalid_chunks += 1
            return None

        partial = self.partials.get(frame_id)
        if partial is None:
            partial = _PartialFrame(
                frame_id=frame_id,
                width=width,
                height=height,
                source_width=source_width,
                source_height=source_height,
                chunks=[None] * chunk_count,
            )
            self.partials[frame_id] = partial
        elif not partial.matches(width, height, source_width, source_height, chunk_count):
            self.invalid_chunks += 1
            self.partials.pop(frame_id, None)
            return None

        if partial.chunks[chunk_index] is None:
            partial.chunks[chunk_index] = payload

        if not partial.complete:
            return None

        frame = ReceivedStreamFrame(
            frame_id=frame_id,
            width=partial.width,
            height=partial.height,
            source_width=partial.source_width,
            source_height=partial.source_height,
            jpeg=b"".join(chunk for chunk in partial.chunks if chunk is not None),
        )
        self.partials.pop(frame_id, None)
        self.frames.append(frame)
        return frame

    def missing_chunks(self, frame_id: int) -> list[int]:
        partial = self.partials.get(frame_id)
        if partial is None:
            return []
        return [index for index, chunk in enumerate(partial.chunks) if chunk is None]


@dataclass
class _PartialFrame:
    frame_id: int
    width: int
    height: int
    source_width: int
    source_height: int
    chunks: list[bytes | None]

    @property
    def complete(self) -> bool:
        return all(chunk is not None for chunk in self.chunks)

    def matches(
        self,
        width: int,
        height: int,
        source_width: int,
        source_height: int,
        chunk_count: int,
    ) -> bool:
        return (
            self.width == width
            and self.height == height
            and self.source_width == source_width
            and self.source_height == source_height
            and len(self.chunks) == chunk_count
        )


@unittest.skipIf(ChromiumStreamService is None, "PySide6 is not installed")
class MacChromiumStreamTests(unittest.TestCase):
    def test_stream_start_uses_control_source_host_and_reports_source_port(self) -> None:
        service = ChromiumStreamService.__new__(ChromiumStreamService)
        service.browser = FakeChromiumBrowser()
        service.timer = FakeTimer()
        service.config = None
        service.target_host = None
        service.target_address = None
        service.socket = FakeSocket(source_port=37123)
        service._send_frame = lambda: None

        status = ChromiumStreamService.start(
            service,
            {
                "_source_host": "::ffff:192.168.1.25",
                "host": "203.0.113.10",
                "port": 49152,
                "resolution": "720p",
                "fps": 60,
                "quality": 99,
            },
        )

        expected_fps = (
            streaming_protocol.DEFAULT_MACOS_UDP_STREAM_FPS
            if sys.platform == "darwin"
            else streaming_protocol.MAX_STREAM_FPS
        )
        self.assertEqual(status["host"], "192.168.1.25")
        self.assertEqual(status["port"], 49152)
        self.assertEqual(status["source_port"], 37123)
        self.assertEqual(status["resolution"], "720p")
        self.assertEqual(status["width"], 1280)
        self.assertEqual(status["height"], 720)
        self.assertEqual(status["fps"], expected_fps)
        self.assertEqual(status["quality"], 90)
        self.assertEqual(service.timer.interval, round(1000 / expected_fps))
        self.assertEqual(service.browser.stream_viewports, [(1280, 720)])
        self.assertEqual(service.browser.minimum_viewports, [])

    def test_stream_stop_clears_chromium_stream_viewport_override(self) -> None:
        service = ChromiumStreamService.__new__(ChromiumStreamService)
        service.browser = FakeChromiumBrowser()
        service.timer = FakeTimer()
        service.config = ChromiumStreamConfig("127.0.0.1", 54321, 640, 360, 30, 55, "360p")
        service.target_host = None
        service.target_address = None

        status = ChromiumStreamService.stop(service)

        self.assertFalse(status["streaming"])
        self.assertEqual(service.browser.cleared_stream_viewports, 1)

    def test_stream_capture_interval_idles_without_client_activity(self) -> None:
        service = ChromiumStreamService.__new__(ChromiumStreamService)
        service.timer = FakeTimer()
        service.config = ChromiumStreamConfig("127.0.0.1", 54321, 640, 360, 30, 55, "360p")
        service.last_client_activity_at = 10.0
        service.using_idle_capture_interval = False

        idle_at = 10.0 + chromium_backend.STREAM_CLIENT_ACTIVITY_TIMEOUT_SECONDS
        with patch.object(chromium_backend.time, "monotonic", return_value=idle_at):
            ChromiumStreamService._apply_idle_capture_interval_if_needed(service)

        self.assertEqual(service.timer.interval, chromium_backend.IDLE_CAPTURE_INTERVAL_MS)
        self.assertTrue(service.using_idle_capture_interval)

        with patch.object(chromium_backend.time, "monotonic", return_value=100.0):
            ChromiumStreamService.mark_client_activity(service)

        self.assertEqual(service.timer.interval, round(1000 / service.config.fps))
        self.assertFalse(service.using_idle_capture_interval)

    def test_chromium_stream_target_does_not_resize_or_emulate_visible_window(self) -> None:
        browser = ChromiumBrowserService.__new__(ChromiumBrowserService)
        browser.connection = FakeCdpConnection()
        browser.viewport_width = 5120
        browser.viewport_height = 1554
        browser._stream_viewport = None

        with patch.object(chromium_backend.sys, "platform", "darwin"):
            ChromiumBrowserService.set_stream_viewport(browser, 960, 540)

        self.assertEqual(browser.viewport_width, 1280)
        self.assertEqual(browser.viewport_height, 720)
        self.assertEqual(browser._stream_viewport, (960, 540))
        methods = [method for method, _payload in browser.connection.calls]
        self.assertIn("Runtime.evaluate", methods)
        self.assertNotIn("Emulation.setDeviceMetricsOverride", methods)
        self.assertNotIn("Browser.setWindowBounds", methods)

    def test_chromium_note_capture_size_keeps_css_viewport_for_click_mapping(self) -> None:
        browser = ChromiumBrowserService.__new__(ChromiumBrowserService)
        browser.viewport_width = 1280
        browser.viewport_height = 720
        browser._stream_viewport = (960, 540)

        with patch.object(chromium_backend.sys, "platform", "darwin"):
            ChromiumBrowserService.note_capture_size(browser, 5120, 1554)

        self.assertEqual((browser.viewport_width, browser.viewport_height), (1280, 720))

    def test_chromium_stream_capture_uses_css_viewport_clip_without_emulation(self) -> None:
        browser = ChromiumBrowserService.__new__(ChromiumBrowserService)
        browser.connection = FakeCdpConnection()
        browser.viewport_width = 1280
        browser.viewport_height = 720
        browser._stream_viewport = (960, 540)

        with patch.object(chromium_backend.sys, "platform", "darwin"):
            ChromiumBrowserService._capture_jpeg_result(browser, 55)

        self.assertEqual(browser.connection.calls[-1][0], "Page.captureScreenshot")
        self.assertFalse(browser.connection.calls[-1][1]["fromSurface"])
        self.assertEqual(
            browser.connection.calls[-1][1]["clip"],
            {"x": 0, "y": 0, "width": 1280, "height": 720, "scale": 1},
        )
        self.assertNotIn("Emulation.setDeviceMetricsOverride", [method for method, _payload in browser.connection.calls])

    def test_chromium_stream_capture_uses_visible_window_on_windows(self) -> None:
        browser = ChromiumBrowserService.__new__(ChromiumBrowserService)
        browser.connection = FakeCdpConnection()
        browser.viewport_width = 1280
        browser.viewport_height = 720
        browser._stream_viewport = (960, 540)

        with patch.object(chromium_backend.sys, "platform", "win32"):
            ChromiumBrowserService._capture_jpeg_result(browser, 55)

        self.assertEqual(browser.connection.calls[-1][0], "Page.captureScreenshot")
        self.assertTrue(browser.connection.calls[-1][1]["fromSurface"])
        self.assertNotIn("clip", browser.connection.calls[-1][1])

    def test_chromium_reconnect_restores_active_stream_viewport(self) -> None:
        browser = ChromiumBrowserService.__new__(ChromiumBrowserService)
        old_connection = FakeCdpConnection()
        new_connection = FakeCdpConnection()
        browser.connection = old_connection
        browser.viewport_width = 960
        browser.viewport_height = 540
        browser._stream_viewport = (960, 540)
        browser._connect = lambda: new_connection
        configured = []
        browser._configure_page = lambda: configured.append(True)

        with patch.object(chromium_backend.sys, "platform", "darwin"):
            ChromiumBrowserService._reconnect_page(browser)

        self.assertEqual(configured, [True])
        self.assertEqual(old_connection.calls[-1], ("close", {}))
        self.assertIs(browser.connection, new_connection)
        self.assertEqual(new_connection.calls[-1][0], "Runtime.evaluate")
        self.assertEqual(browser._stream_viewport, (960, 540))
        self.assertEqual((browser.viewport_width, browser.viewport_height), (1280, 720))

    def test_capture_and_send_emits_reassemblable_udp_jpeg_frame(self) -> None:
        source_image = _pattern_image(640, 360)
        service = ChromiumStreamService.__new__(ChromiumStreamService)
        service.browser = FakeChromiumBrowser(source_image)
        service.socket = FakeSocket()
        service.stats_lock = threading.Lock()
        service.frames_sent = 0
        service.last_stats_at = time.monotonic()
        config = ChromiumStreamConfig("127.0.0.1", 54321, 640, 360, 30, 55, "360p")

        ChromiumStreamService._capture_and_send(service, config, "127.0.0.1", 42)

        sent = service.socket.sent
        self.assertGreater(len(sent), 0)
        self.assertLessEqual(len(sent), streaming_protocol.MAX_STREAM_CHUNKS_PER_FRAME)
        chunks: list[bytes] = []
        for expected_index, (packet, address) in enumerate(sent):
            self.assertEqual(address, ("127.0.0.1", 54321))
            header = packet[: streaming_protocol.STREAM_HEADER_SIZE]
            payload = packet[streaming_protocol.STREAM_HEADER_SIZE :]
            magic, frame_id, chunk_index, chunk_count, width, height, source_width, source_height = struct.unpack(
                streaming_protocol.STREAM_HEADER_FORMAT,
                header,
            )
            self.assertEqual(magic, streaming_protocol.STREAM_MAGIC)
            self.assertEqual(frame_id, 42)
            self.assertEqual(chunk_index, expected_index)
            self.assertEqual(chunk_count, len(sent))
            self.assertLessEqual(width, config.width)
            self.assertLessEqual(height, config.height)
            self.assertEqual((source_width, source_height), (640, 360))
            self.assertGreater(len(payload), 0)
            self.assertLessEqual(len(payload), streaming_protocol.STREAM_CHUNK_BYTES)
            chunks.append(payload)

        decoded = QImage()
        self.assertTrue(decoded.loadFromData(b"".join(chunks), "JPG"))
        self.assertEqual(service.browser.noted_capture_sizes, [(640, 360)])
        self.assertEqual(service.frames_sent, 1)

    def test_receiver_reassembles_server_packets_out_of_order(self) -> None:
        service = _stream_service_with_fake_socket(_pattern_image(640, 360))
        config = ChromiumStreamConfig("127.0.0.1", 54321, 640, 360, 30, 55, "360p")
        ChromiumStreamService._capture_and_send(service, config, "127.0.0.1", 77)
        packets = [packet for packet, _address in service.socket.sent]
        self.assertGreater(len(packets), 1)

        receiver = DatagramStreamReceiver()
        completed = None
        for packet in reversed(packets):
            completed = receiver.accept(packet) or completed

        self.assertIsNotNone(completed)
        self.assertEqual(completed.frame_id, 77)
        self.assertEqual((completed.source_width, completed.source_height), (640, 360))
        decoded = QImage()
        self.assertTrue(decoded.loadFromData(completed.jpeg, "JPG"))
        self.assertEqual(receiver.invalid_packets, 0)
        self.assertEqual(receiver.invalid_chunks, 0)

    def test_receiver_does_not_complete_frame_when_one_udp_chunk_is_missing(self) -> None:
        service = _stream_service_with_fake_socket(_pattern_image(640, 360))
        config = ChromiumStreamConfig("127.0.0.1", 54321, 640, 360, 30, 55, "360p")
        ChromiumStreamService._capture_and_send(service, config, "127.0.0.1", 88)
        packets = [packet for packet, _address in service.socket.sent]
        self.assertGreater(len(packets), 2)
        missing_index = len(packets) // 2

        receiver = DatagramStreamReceiver()
        for index, packet in enumerate(packets):
            if index != missing_index:
                self.assertIsNone(receiver.accept(packet))

        self.assertEqual(receiver.frames, [])
        self.assertEqual(receiver.missing_chunks(88), [missing_index])

    def test_local_udp_receiver_reassembles_server_packets_when_socket_bind_is_available(self) -> None:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        stream_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            try:
                udp.bind(("127.0.0.1", 0))
            except PermissionError as exc:
                raise unittest.SkipTest(f"local UDP bind is blocked by this environment: {exc}") from exc
            udp.settimeout(0.5)

            service = ChromiumStreamService.__new__(ChromiumStreamService)
            service.browser = FakeChromiumBrowser(_pattern_image(640, 360))
            service.socket = stream_socket
            service.stats_lock = threading.Lock()
            service.frames_sent = 0
            service.last_stats_at = time.monotonic()
            host, port = udp.getsockname()
            config = ChromiumStreamConfig(host, port, 640, 360, 30, 55, "360p")

            ChromiumStreamService._capture_and_send(service, config, host, 99)
            receiver = DatagramStreamReceiver()
            deadline = time.monotonic() + 2.0
            completed = None
            while time.monotonic() < deadline and completed is None:
                try:
                    packet, _address = udp.recvfrom(2048)
                except TimeoutError:
                    continue
                completed = receiver.accept(packet)

            self.assertIsNotNone(completed)
            self.assertEqual(completed.frame_id, 99)
            decoded = QImage()
            self.assertTrue(decoded.loadFromData(completed.jpeg, "JPG"))
        finally:
            udp.close()
            stream_socket.close()

    @unittest.skipUnless(sys.platform == "darwin", "this diagnostic check targets the macOS chunk budget")
    @unittest.expectedFailure
    def test_macos_encoder_keeps_540p_frame_under_advertised_chunk_budget(self) -> None:
        target_bytes = streaming_protocol.STREAM_CHUNK_BYTES * streaming_protocol.MAX_STREAM_CHUNKS_PER_FRAME

        def encode_until_small_enough(image: QImage, quality: int) -> bytes:
            del quality
            if image.width() > 500:
                return b"x" * (target_bytes + 1)
            return b"x" * target_bytes

        with patch.object(chromium_backend, "_encode_jpeg", side_effect=encode_until_small_enough):
            frame_image, jpeg = chromium_backend._encode_jpeg_for_udp(_blank_image(960, 540), 55)

        chunk_count = math.ceil(len(jpeg) / streaming_protocol.STREAM_CHUNK_BYTES)
        self.assertLessEqual(
            chunk_count,
            streaming_protocol.MAX_STREAM_CHUNKS_PER_FRAME,
            f"encoded {frame_image.width()}x{frame_image.height()} into {chunk_count} chunks",
        )


def _stream_service_with_fake_socket(image: Any) -> Any:
    service = ChromiumStreamService.__new__(ChromiumStreamService)
    service.browser = FakeChromiumBrowser(image)
    service.socket = FakeSocket()
    service.stats_lock = threading.Lock()
    service.frames_sent = 0
    service.last_stats_at = time.monotonic()
    return service


def _blank_image(width: int, height: int) -> Any:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0xFF202020)
    return image


def _pattern_image(width: int, height: int) -> Any:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    for y in range(height):
        for x in range(width):
            image.setPixel(
                x,
                y,
                0xFF000000
                | (((x * 37 + y * 17) & 0xFF) << 16)
                | (((x * 13 + y * 29) & 0xFF) << 8)
                | ((x * 71 + y * 7) & 0xFF),
            )
    return image
