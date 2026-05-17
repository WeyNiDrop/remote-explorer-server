from __future__ import annotations

import math
import logging
import socket
import struct
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QSize, Qt, QTimer
from PySide6.QtNetwork import QHostAddress
from PySide6.QtGui import QImage
from PySide6.QtWebEngineWidgets import QWebEngineView

STREAM_MAGIC = b"REXPSTR1"
STREAM_HEADER_FORMAT = "!8sIHHHHHH"
STREAM_HEADER_SIZE = struct.calcsize(STREAM_HEADER_FORMAT)
STREAM_CHUNK_BYTES = 1000
# 大帧分片发送削峰，避免 UDP 突发压垮客户端缓冲。 / Pace large frame chunks to avoid UDP bursts.
STREAM_CHUNK_PACE_BATCH = 16
STREAM_CHUNK_PACE_SECONDS = 0.001
DEFAULT_STREAM_FPS = 30
MIN_STREAM_FPS = 20
MAX_STREAM_FPS = 60
DEFAULT_JPEG_QUALITY = 55
MAX_STREAM_HEIGHT = 1080
MAX_STREAM_WIDTH = 1920
STREAM_LOG_INTERVAL_SECONDS = 5.0

LOGGER = logging.getLogger("remote_explorer.streaming")

RESOLUTIONS: dict[str, tuple[int, int]] = {
    "360p": (640, 360),
    "540p": (960, 540),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
}


@dataclass
class StreamConfig:
    host: str
    port: int
    width: int = 640
    height: int = 360
    fps: int = DEFAULT_STREAM_FPS
    quality: int = DEFAULT_JPEG_QUALITY
    resolution: str = "360p"


class BrowserStreamService(QObject):
    def __init__(self, view: QWebEngineView, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.view = view
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._send_frame)
        self.config: StreamConfig | None = None
        self.target_host: QHostAddress | None = None
        self.target_address: str | None = None
        self.frame_id = 0
        self.pending_frame: Future[None] | None = None
        self.encoder = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RemoteExplorerJpegStream")
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.stats_lock = threading.Lock()
        self._reset_stats()

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        host = _clean_host(str(payload.get("_source_host") or payload.get("host") or ""))
        port = int(payload.get("port") or payload.get("stream_port") or 0)
        if not host:
            raise ValueError("Stream host is required")
        if port <= 0 or port > 65535:
            raise ValueError("Valid stream port is required")

        width, height, resolution = _resolve_size(payload)
        fps = _clamp_int(payload.get("fps"), MIN_STREAM_FPS, MAX_STREAM_FPS, DEFAULT_STREAM_FPS)
        quality = _clamp_int(payload.get("quality"), 30, 90, DEFAULT_JPEG_QUALITY)

        self.config = StreamConfig(
            host=host,
            port=port,
            width=width,
            height=height,
            fps=fps,
            quality=quality,
            resolution=resolution,
        )
        self.target_host = QHostAddress(host)
        self.target_address = host
        self.timer.setInterval(max(1, round(1000 / fps)))
        self._reset_stats()
        LOGGER.info(
            "UDP/JPEG stream start target=%s:%s resolution=%s size=%sx%s fps=%s quality=%s",
            host,
            port,
            resolution,
            width,
            height,
            fps,
            quality,
        )
        self.timer.start()
        self._send_frame()
        return self.status()

    def stop(self) -> dict[str, Any]:
        if self.config or self.timer.isActive():
            self._log_stats("stop", force=True)
            LOGGER.info("UDP/JPEG stream stop")
        self.timer.stop()
        self.config = None
        self.target_host = None
        self.target_address = None
        return self.status()

    def configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.config:
            return self.start(payload)

        merged = {
            "_source_host": self.config.host,
            "port": self.config.port,
            "width": self.config.width,
            "height": self.config.height,
            "resolution": self.config.resolution,
            "fps": self.config.fps,
            "quality": self.config.quality,
            **payload,
        }
        return self.start(merged)

    def status(self) -> dict[str, Any]:
        if not self.config:
            return {
                "streaming": False,
                "transport": "udp",
                "codec": "jpeg",
                "default_fps": DEFAULT_STREAM_FPS,
                "min_fps": MIN_STREAM_FPS,
                "max_fps": MAX_STREAM_FPS,
                "chunk_bytes": STREAM_CHUNK_BYTES,
                "magic": STREAM_MAGIC.decode("ascii"),
            }

        return {
            "streaming": self.timer.isActive(),
            "host": self.config.host,
            "port": self.config.port,
            "width": self.config.width,
            "height": self.config.height,
            "resolution": self.config.resolution,
            "fps": self.config.fps,
            "quality": self.config.quality,
            "transport": "udp",
            "codec": "jpeg",
            "min_fps": MIN_STREAM_FPS,
            "max_fps": MAX_STREAM_FPS,
            "chunk_bytes": STREAM_CHUNK_BYTES,
            "magic": STREAM_MAGIC.decode("ascii"),
        }

    def _send_frame(self) -> None:
        self._clear_completed_frame()
        if self.pending_frame is not None:
            # 编码线程还在处理上一帧时丢弃本 tick，保持低延迟。 / Drop this tick while the encoder is busy to keep latency low.
            self._record_skip("encoder_busy")
            return

        if not self.config or not self.target_host or not self.target_address:
            return

        capture_started_at = time.perf_counter()
        pixmap = self.view.grab()
        if pixmap.isNull():
            self._record_skip("null_grab")
            return

        source_width = max(1, self.view.width())
        source_height = max(1, self.view.height())
        scaled = pixmap.scaled(
            QSize(self.config.width, self.config.height),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        if scaled.isNull():
            self._record_skip("null_scaled")
            return

        capture_ms = (time.perf_counter() - capture_started_at) * 1000.0
        self.frame_id = (self.frame_id + 1) & 0xFFFFFFFF
        image = scaled.toImage().copy()
        config = self.config
        target_address = self.target_address
        frame_width = max(1, scaled.width())
        frame_height = max(1, scaled.height())
        frame_id = self.frame_id
        self._record_frame_queued(capture_ms)
        # JPEG 编码和 UDP 发送放到单线程池，避免阻塞 Qt UI 线程。 / Encode/send on one worker so the Qt UI thread stays responsive.
        self.pending_frame = self.encoder.submit(
            self._encode_and_send_frame,
            image,
            config,
            target_address,
            source_width,
            source_height,
            frame_width,
            frame_height,
            frame_id,
        )

    def _clear_completed_frame(self) -> None:
        if self.pending_frame is None or not self.pending_frame.done():
            return

        try:
            self.pending_frame.result()
        except Exception as exc:
            LOGGER.warning("UDP/JPEG frame worker failed: %s", exc)
        finally:
            self.pending_frame = None

    def _encode_and_send_frame(
        self,
        image: QImage,
        config: StreamConfig,
        target_address: str,
        source_width: int,
        source_height: int,
        frame_width: int,
        frame_height: int,
        frame_id: int,
    ) -> None:
        encode_started_at = time.perf_counter()
        jpeg = _encode_jpeg(image, config.quality)
        encode_ms = (time.perf_counter() - encode_started_at) * 1000.0
        if not jpeg:
            self._record_skip("encode_failed")
            return

        chunk_count = int(math.ceil(len(jpeg) / STREAM_CHUNK_BYTES))
        if chunk_count <= 0 or chunk_count > 65535:
            self._record_skip("invalid_chunk_count")
            return

        send_started_at = time.perf_counter()
        bytes_sent = 0
        for chunk_index in range(chunk_count):
            start = chunk_index * STREAM_CHUNK_BYTES
            chunk = jpeg[start : start + STREAM_CHUNK_BYTES]
            header = struct.pack(
                STREAM_HEADER_FORMAT,
                STREAM_MAGIC,
                frame_id,
                chunk_index,
                chunk_count,
                frame_width,
                frame_height,
                source_width,
                source_height,
            )
            packet = header + chunk
            self.socket.sendto(packet, (target_address, config.port))
            bytes_sent += len(packet)
            if chunk_index and chunk_index % STREAM_CHUNK_PACE_BATCH == 0:
                # 微小间隔让系统/网络栈有机会排空发送队列。 / A tiny pause lets the OS/network stack drain the burst.
                time.sleep(STREAM_CHUNK_PACE_SECONDS)
        send_ms = (time.perf_counter() - send_started_at) * 1000.0
        self._record_frame_sent(
            frame_id=frame_id,
            frame_width=frame_width,
            frame_height=frame_height,
            jpeg_bytes=len(jpeg),
            packet_bytes=bytes_sent,
            chunks=chunk_count,
            encode_ms=encode_ms,
            send_ms=send_ms,
        )

    def _reset_stats(self) -> None:
        now = time.monotonic()
        with self.stats_lock:
            self.stats_since = now
            self.next_stats_log_at = now + STREAM_LOG_INTERVAL_SECONDS
            self.frames_queued = 0
            self.frames_sent = 0
            self.bytes_sent = 0
            self.chunks_sent = 0
            self.capture_ms_total = 0.0
            self.capture_ms_max = 0.0
            self.encode_ms_total = 0.0
            self.encode_ms_max = 0.0
            self.send_ms_total = 0.0
            self.send_ms_max = 0.0
            self.skipped_busy = 0
            self.skipped_null_grab = 0
            self.skipped_null_scaled = 0
            self.skipped_encode_failed = 0
            self.skipped_invalid_chunk_count = 0
            self.last_frame_id = 0
            self.last_frame_size = ""

    def _record_frame_queued(self, capture_ms: float) -> None:
        with self.stats_lock:
            self.frames_queued += 1
            self.capture_ms_total += capture_ms
            self.capture_ms_max = max(self.capture_ms_max, capture_ms)
        self._log_stats("interval")

    def _record_frame_sent(
        self,
        *,
        frame_id: int,
        frame_width: int,
        frame_height: int,
        jpeg_bytes: int,
        packet_bytes: int,
        chunks: int,
        encode_ms: float,
        send_ms: float,
    ) -> None:
        with self.stats_lock:
            self.frames_sent += 1
            self.bytes_sent += packet_bytes
            self.chunks_sent += chunks
            self.encode_ms_total += encode_ms
            self.encode_ms_max = max(self.encode_ms_max, encode_ms)
            self.send_ms_total += send_ms
            self.send_ms_max = max(self.send_ms_max, send_ms)
            self.last_frame_id = frame_id
            self.last_frame_size = f"{frame_width}x{frame_height}/{jpeg_bytes}B/{chunks} chunks"
        self._log_stats("interval")

    def _record_skip(self, reason: str) -> None:
        with self.stats_lock:
            if reason == "encoder_busy":
                self.skipped_busy += 1
            elif reason == "null_grab":
                self.skipped_null_grab += 1
            elif reason == "null_scaled":
                self.skipped_null_scaled += 1
            elif reason == "encode_failed":
                self.skipped_encode_failed += 1
            elif reason == "invalid_chunk_count":
                self.skipped_invalid_chunk_count += 1
        self._log_stats("interval")

    def _log_stats(self, reason: str, force: bool = False) -> None:
        now = time.monotonic()
        with self.stats_lock:
            if not force and now < self.next_stats_log_at:
                return

            elapsed = max(0.001, now - self.stats_since)
            queued = self.frames_queued
            sent = self.frames_sent
            chunks = self.chunks_sent
            bytes_sent = self.bytes_sent
            capture_avg = self.capture_ms_total / queued if queued else 0.0
            encode_avg = self.encode_ms_total / sent if sent else 0.0
            send_avg = self.send_ms_total / sent if sent else 0.0
            capture_max = self.capture_ms_max
            encode_max = self.encode_ms_max
            send_max = self.send_ms_max
            skipped_busy = self.skipped_busy
            skipped_null_grab = self.skipped_null_grab
            skipped_null_scaled = self.skipped_null_scaled
            skipped_encode_failed = self.skipped_encode_failed
            skipped_invalid_chunk_count = self.skipped_invalid_chunk_count
            last_frame_id = self.last_frame_id
            last_frame_size = self.last_frame_size or "none"

            self.stats_since = now
            self.next_stats_log_at = now + STREAM_LOG_INTERVAL_SECONDS
            self.frames_queued = 0
            self.frames_sent = 0
            self.bytes_sent = 0
            self.chunks_sent = 0
            self.capture_ms_total = 0.0
            self.capture_ms_max = 0.0
            self.encode_ms_total = 0.0
            self.encode_ms_max = 0.0
            self.send_ms_total = 0.0
            self.send_ms_max = 0.0
            self.skipped_busy = 0
            self.skipped_null_grab = 0
            self.skipped_null_scaled = 0
            self.skipped_encode_failed = 0
            self.skipped_invalid_chunk_count = 0

        LOGGER.info(
            "UDP/JPEG stats reason=%s elapsed=%.1fs queued=%s sent=%s fps=%.1f bytes=%s chunks=%s "
            "capture_ms=%.1f/%.1f encode_ms=%.1f/%.1f send_ms=%.1f/%.1f "
            "skip_busy=%s skip_null_grab=%s skip_null_scaled=%s skip_encode_failed=%s "
            "skip_bad_chunks=%s last_frame=%s %s",
            reason,
            elapsed,
            queued,
            sent,
            sent / elapsed,
            bytes_sent,
            chunks,
            capture_avg,
            capture_max,
            encode_avg,
            encode_max,
            send_avg,
            send_max,
            skipped_busy,
            skipped_null_grab,
            skipped_null_scaled,
            skipped_encode_failed,
            skipped_invalid_chunk_count,
            last_frame_id,
            last_frame_size,
        )


def _encode_jpeg(image: QImage, quality: int) -> bytes:
    byte_array = QByteArray()
    buffer = QBuffer(byte_array)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        if not image.save(buffer, "JPG", quality):
            return b""
        return bytes(byte_array)
    finally:
        buffer.close()


def _resolve_size(payload: dict[str, Any]) -> tuple[int, int, str]:
    resolution = str(payload.get("resolution") or "360p").lower()
    if resolution in RESOLUTIONS:
        width, height = RESOLUTIONS[resolution]
        return width, height, resolution

    width = _clamp_int(payload.get("width"), 1, MAX_STREAM_WIDTH, 640)
    height = _clamp_int(payload.get("height"), 1, MAX_STREAM_HEIGHT, 360)
    return width, height, f"{height}p"


def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _clean_host(host: str) -> str:
    host = host.strip().strip("[]")
    if host.startswith("::ffff:"):
        return host.removeprefix("::ffff:")
    return host
