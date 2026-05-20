from __future__ import annotations

import binascii
import struct
import zlib
from pathlib import Path


ICO_SIZES = (16, 32, 48, 64, 128, 256)
ICNS_SIZES = (
    ("ic04", 16),
    ("ic05", 32),
    ("ic07", 128),
    ("ic08", 256),
    ("ic09", 512),
    ("ic10", 1024),
)


def _inside_rounded_rect(x: float, y: float, left: float, top: float, right: float, bottom: float, radius: float) -> bool:
    if x < left or x > right or y < top or y > bottom:
        return False
    cx = min(max(x, left + radius), right - radius)
    cy = min(max(y, top + radius), bottom - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2


def _blend(dst: tuple[int, int, int, int], src: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    sr, sg, sb, sa = src
    dr, dg, db, da = dst
    alpha = sa / 255.0
    inv = 1.0 - alpha
    out_a = int(sa + da * inv)
    if out_a == 0:
        return (0, 0, 0, 0)
    return (
        int(sr * alpha + dr * inv),
        int(sg * alpha + dg * inv),
        int(sb * alpha + db * inv),
        out_a,
    )


def _pixel(size: int, x: int, y: int) -> tuple[int, int, int, int]:
    samples: list[tuple[float, float]] = [
        (x + 0.25, y + 0.25),
        (x + 0.75, y + 0.25),
        (x + 0.25, y + 0.75),
        (x + 0.75, y + 0.75),
    ]
    rgba = (0, 0, 0, 0)
    for sx, sy in samples:
        nx = sx / size
        ny = sy / size
        sample = (0, 0, 0, 0)

        if _inside_rounded_rect(nx, ny, 0.08, 0.08, 0.92, 0.92, 0.18):
            t = (nx * 0.45) + (ny * 0.55)
            sample = (int(26 + 20 * t), int(92 + 70 * t), int(168 + 45 * t), 255)

        if _inside_rounded_rect(nx, ny, 0.20, 0.24, 0.80, 0.66, 0.055):
            sample = _blend(sample, (238, 248, 255, 238))

        if _inside_rounded_rect(nx, ny, 0.245, 0.295, 0.755, 0.595, 0.028):
            sample = _blend(sample, (28, 45, 78, 255))

        if 0.38 <= nx <= 0.62 and 0.66 <= ny <= 0.72:
            sample = _blend(sample, (238, 248, 255, 236))

        if 0.31 <= nx <= 0.69 and 0.72 <= ny <= 0.78 and abs(ny - 0.75) <= 0.022:
            sample = _blend(sample, (238, 248, 255, 236))

        if (nx - 0.63) ** 2 + (ny - 0.43) ** 2 <= 0.055**2:
            sample = _blend(sample, (79, 214, 145, 255))

        if 0.33 <= nx <= 0.55 and abs((ny - 0.46) - 0.18 * (nx - 0.33)) <= 0.022:
            sample = _blend(sample, (79, 214, 145, 230))

        rgba = _blend(rgba, (sample[0], sample[1], sample[2], sample[3] // 4))
    return rgba


def _dib(size: int) -> bytes:
    pixels = bytearray()
    for y in reversed(range(size)):
        for x in range(size):
            r, g, b, a = _pixel(size, x, y)
            pixels.extend((b, g, r, a))

    mask_stride = ((size + 31) // 32) * 4
    mask = b"\0" * (mask_stride * size)
    header = struct.pack(
        "<IIIHHIIIIII",
        40,
        size,
        size * 2,
        1,
        32,
        0,
        len(pixels),
        0,
        0,
        0,
        0,
    )
    return header + bytes(pixels) + mask


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = binascii.crc32(chunk_type)
    crc = binascii.crc32(data, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def _png(size: int) -> bytes:
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            rows.extend(_pixel(size, x, y))
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + _png_chunk(b"IEND", b"")
    )


def write_ico(path: Path) -> None:
    images = [_dib(size) for size in ICO_SIZES]
    offset = 6 + 16 * len(images)
    entries = bytearray()
    for size, image in zip(ICO_SIZES, images):
        entries.extend(
            struct.pack(
                "<BBBBHHII",
                0 if size == 256 else size,
                0 if size == 256 else size,
                0,
                0,
                1,
                32,
                len(image),
                offset,
            )
        )
        offset += len(image)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + bytes(entries) + b"".join(images))


def write_icns(path: Path) -> None:
    chunks = bytearray()
    for icon_type, size in ICNS_SIZES:
        image = _png(size)
        chunks.extend(icon_type.encode("ascii"))
        chunks.extend(struct.pack(">I", len(image) + 8))
        chunks.extend(image)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"icns" + struct.pack(">I", len(chunks) + 8) + bytes(chunks))


if __name__ == "__main__":
    assets_dir = Path(__file__).resolve().parents[1] / "assets"
    write_ico(assets_dir / "remote-explorer.ico")
    write_icns(assets_dir / "remote-explorer.icns")
