from __future__ import annotations

import struct
import sys
from typing import Any


STREAM_MAGIC = b"REXPSTR1"
STREAM_HEADER_FORMAT = "!8sIHHHHHH"
STREAM_HEADER_SIZE = struct.calcsize(STREAM_HEADER_FORMAT)
STREAM_CHUNK_BYTES = 1000
MAX_STREAM_CHUNKS_PER_FRAME = 48 if sys.platform == "darwin" else 64
STREAM_CHUNK_PACE_BATCH = 4 if sys.platform == "darwin" else 8
STREAM_CHUNK_PACE_SECONDS = 0.003 if sys.platform == "darwin" else 0.0015
DEFAULT_STREAM_FPS = 30
DEFAULT_MACOS_UDP_STREAM_FPS = 15
MIN_STREAM_FPS = 20
MAX_STREAM_FPS = 60
DEFAULT_JPEG_QUALITY = 55
MIN_JPEG_QUALITY = 30
MAX_STREAM_HEIGHT = 1080
MAX_STREAM_WIDTH = 1920

RESOLUTIONS: dict[str, tuple[int, int]] = {
    "360p": (640, 360),
    "540p": (960, 540),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
}


def resolve_size(payload: dict[str, Any]) -> tuple[int, int, str]:
    resolution = str(payload.get("resolution") or "360p").lower()
    if resolution in RESOLUTIONS:
        width, height = RESOLUTIONS[resolution]
        return width, height, resolution

    width = clamp_int(payload.get("width"), 1, MAX_STREAM_WIDTH, 640)
    height = clamp_int(payload.get("height"), 1, MAX_STREAM_HEIGHT, 360)
    return width, height, f"{height}p"


def clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def clean_host(host: str) -> str:
    host = host.strip().strip("[]")
    if host.startswith("::ffff:"):
        return host.removeprefix("::ffff:")
    return host
