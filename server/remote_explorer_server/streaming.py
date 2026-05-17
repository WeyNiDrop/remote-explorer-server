from __future__ import annotations

import math
import socket
import struct
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
DEFAULT_STREAM_FPS = 30
MIN_STREAM_FPS = 20
MAX_STREAM_FPS = 60
DEFAULT_JPEG_QUALITY = 55
MAX_STREAM_HEIGHT = 1080
MAX_STREAM_WIDTH = 1920

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
        self.timer.start()
        self._send_frame()
        return self.status()

    def stop(self) -> dict[str, Any]:
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
            return

        if not self.config or not self.target_host or not self.target_address:
            return

        pixmap = self.view.grab()
        if pixmap.isNull():
            return

        source_width = max(1, self.view.width())
        source_height = max(1, self.view.height())
        scaled = pixmap.scaled(
            QSize(self.config.width, self.config.height),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        if scaled.isNull():
            return

        self.frame_id = (self.frame_id + 1) & 0xFFFFFFFF
        image = scaled.toImage().copy()
        config = self.config
        target_address = self.target_address
        frame_width = max(1, scaled.width())
        frame_height = max(1, scaled.height())
        frame_id = self.frame_id
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
        except Exception:
            pass
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
        jpeg = _encode_jpeg(image, config.quality)
        if not jpeg:
            return

        chunk_count = int(math.ceil(len(jpeg) / STREAM_CHUNK_BYTES))
        if chunk_count <= 0 or chunk_count > 65535:
            return

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
            self.socket.sendto(header + chunk, (target_address, config.port))


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
