from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QSize, Qt, QTimer
from PySide6.QtNetwork import QHostAddress, QUdpSocket
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
        self.socket = QUdpSocket(self)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._send_frame)
        self.config: StreamConfig | None = None
        self.target_host: QHostAddress | None = None
        self.frame_id = 0

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
        self.timer.setInterval(max(1, round(1000 / fps)))
        self.timer.start()
        self._send_frame()
        return self.status()

    def stop(self) -> dict[str, Any]:
        self.timer.stop()
        self.config = None
        self.target_host = None
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
        if not self.config or not self.target_host:
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

        jpeg = _encode_jpeg(scaled, self.config.quality)
        if not jpeg:
            return

        self.frame_id = (self.frame_id + 1) & 0xFFFFFFFF
        chunk_count = int(math.ceil(len(jpeg) / STREAM_CHUNK_BYTES))
        if chunk_count <= 0 or chunk_count > 65535:
            return

        frame_width = max(1, scaled.width())
        frame_height = max(1, scaled.height())

        for chunk_index in range(chunk_count):
            start = chunk_index * STREAM_CHUNK_BYTES
            chunk = jpeg[start : start + STREAM_CHUNK_BYTES]
            header = struct.pack(
                STREAM_HEADER_FORMAT,
                STREAM_MAGIC,
                self.frame_id,
                chunk_index,
                chunk_count,
                frame_width,
                frame_height,
                source_width,
                source_height,
            )
            self.socket.writeDatagram(header + chunk, self.target_host, self.config.port)


def _encode_jpeg(pixmap: Any, quality: int) -> bytes:
    byte_array = QByteArray()
    buffer = QBuffer(byte_array)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        if not pixmap.toImage().save(buffer, "JPG", quality):
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
