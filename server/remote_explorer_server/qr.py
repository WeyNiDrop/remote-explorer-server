from __future__ import annotations

import binascii
import struct
import zlib


class QrError(ValueError):
    pass


_M_BLOCKS: dict[int, tuple[int, list[int]]] = {
    1: (10, [16]),
    2: (16, [28]),
    3: (26, [44]),
    4: (18, [32, 32]),
    5: (24, [43, 43]),
    6: (16, [27, 27, 27, 27]),
}

_ALIGNMENT_POSITIONS: dict[int, list[int]] = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
}


def make_qr_png(text: str, *, scale: int = 8, border: int = 4) -> bytes:
    matrix = make_qr_matrix(text)
    return matrix_to_png(matrix, scale=scale, border=border)


def make_qr_matrix(text: str) -> list[list[bool]]:
    data = text.encode("utf-8")
    version = _choose_version(len(data))
    ecc_len, block_sizes = _M_BLOCKS[version]
    data_codewords = sum(block_sizes)
    payload = _make_data_codewords(data, version, data_codewords)
    blocks: list[list[int]] = []
    offset = 0
    for block_size in block_sizes:
        block = payload[offset : offset + block_size]
        offset += block_size
        blocks.append(block)
    ecc_blocks = [_reed_solomon_remainder(block, ecc_len) for block in blocks]
    codewords = _interleave_blocks(blocks, ecc_blocks)
    return _draw_codewords(version, codewords)


def matrix_to_png(matrix: list[list[bool]], *, scale: int = 8, border: int = 4) -> bytes:
    if scale <= 0:
        raise QrError("QR scale must be positive")
    if border < 0:
        raise QrError("QR border cannot be negative")

    modules = len(matrix)
    size = (modules + border * 2) * scale
    rows = []
    for y in range(size):
        module_y = y // scale - border
        row = bytearray()
        for x in range(size):
            module_x = x // scale - border
            dark = 0 <= module_x < modules and 0 <= module_y < modules and matrix[module_y][module_x]
            row.append(0 if dark else 255)
        rows.append(b"\x00" + bytes(row))

    raw = b"".join(rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack("!IIBBBBB", size, size, 8, 0, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


def _choose_version(byte_count: int) -> int:
    for version, (_ecc, blocks) in _M_BLOCKS.items():
        # Versions 1-6 use an 8-bit byte-mode character count.
        capacity = sum(blocks) - 2
        if byte_count <= capacity:
            return version
    raise QrError("QR payload is too long for the built-in encoder")


def _make_data_codewords(data: bytes, version: int, data_codewords: int) -> list[int]:
    bits: list[int] = []
    _append_bits(bits, 0b0100, 4)
    _append_bits(bits, len(data), 8 if version <= 9 else 16)
    for byte in data:
        _append_bits(bits, byte, 8)

    max_bits = data_codewords * 8
    terminator = min(4, max_bits - len(bits))
    _append_bits(bits, 0, terminator)
    while len(bits) % 8:
        bits.append(0)

    codewords = [_bits_to_int(bits[index : index + 8]) for index in range(0, len(bits), 8)]
    pads = (0xEC, 0x11)
    index = 0
    while len(codewords) < data_codewords:
        codewords.append(pads[index % 2])
        index += 1
    return codewords


def _append_bits(bits: list[int], value: int, count: int) -> None:
    for shift in range(count - 1, -1, -1):
        bits.append((value >> shift) & 1)


def _bits_to_int(bits: list[int]) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | bit
    return value


def _interleave_blocks(blocks: list[list[int]], ecc_blocks: list[list[int]]) -> list[int]:
    result: list[int] = []
    for index in range(max(len(block) for block in blocks)):
        for block in blocks:
            if index < len(block):
                result.append(block[index])
    for index in range(max(len(block) for block in ecc_blocks)):
        for block in ecc_blocks:
            if index < len(block):
                result.append(block[index])
    return result


_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_value = 1
for _index in range(255):
    _GF_EXP[_index] = _value
    _GF_LOG[_value] = _index
    _value <<= 1
    if _value & 0x100:
        _value ^= 0x11D
for _index in range(255, 512):
    _GF_EXP[_index] = _GF_EXP[_index - 255]


def _gf_mul(left: int, right: int) -> int:
    if left == 0 or right == 0:
        return 0
    return _GF_EXP[_GF_LOG[left] + _GF_LOG[right]]


def _reed_solomon_generator(degree: int) -> list[int]:
    poly = [1]
    for index in range(degree):
        next_poly = [0] * (len(poly) + 1)
        for coefficient_index, coefficient in enumerate(poly):
            next_poly[coefficient_index] ^= coefficient
            next_poly[coefficient_index + 1] ^= _gf_mul(coefficient, _GF_EXP[index])
        poly = next_poly
    return poly


def _reed_solomon_remainder(data: list[int], degree: int) -> list[int]:
    generator = _reed_solomon_generator(degree)
    remainder = [0] * degree
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + [0]
        for index, coefficient in enumerate(generator[1:]):
            remainder[index] ^= _gf_mul(coefficient, factor)
    return remainder


def _draw_codewords(version: int, codewords: list[int]) -> list[list[bool]]:
    size = 21 + (version - 1) * 4
    base = [[-1 for _ in range(size)] for _ in range(size)]
    function = [[False for _ in range(size)] for _ in range(size)]

    _draw_function_patterns(base, function, version)
    data_bits: list[int] = []
    for codeword in codewords:
        _append_bits(data_bits, codeword, 8)

    best_matrix: list[list[int]] | None = None
    best_penalty: int | None = None
    for mask in range(8):
        matrix = [row[:] for row in base]
        _place_data_bits(matrix, function, data_bits, mask)
        _draw_format_bits(matrix, function, mask)
        penalty = _penalty(matrix)
        if best_penalty is None or penalty < best_penalty:
            best_penalty = penalty
            best_matrix = matrix

    if best_matrix is None:
        raise QrError("Could not render QR matrix")
    return [[cell == 1 for cell in row] for row in best_matrix]


def _draw_function_patterns(matrix: list[list[int]], function: list[list[bool]], version: int) -> None:
    size = len(matrix)
    _draw_finder(matrix, function, 0, 0)
    _draw_finder(matrix, function, size - 7, 0)
    _draw_finder(matrix, function, 0, size - 7)

    for index in range(8, size - 8):
        value = 1 if index % 2 == 0 else 0
        _set_function(matrix, function, 6, index, value)
        _set_function(matrix, function, index, 6, value)

    _reserve_format_areas(matrix, function)

    positions = _ALIGNMENT_POSITIONS[version]
    for cy in positions:
        for cx in positions:
            if matrix[cy][cx] >= 0:
                continue
            _draw_alignment(matrix, function, cx, cy)

    _set_function(matrix, function, 8, 4 * version + 9, 1)


def _draw_finder(matrix: list[list[int]], function: list[list[bool]], left: int, top: int) -> None:
    for y in range(-1, 8):
        for x in range(-1, 8):
            xx = left + x
            yy = top + y
            if not (0 <= xx < len(matrix) and 0 <= yy < len(matrix)):
                continue
            dark = 0 <= x <= 6 and 0 <= y <= 6 and (x in {0, 6} or y in {0, 6} or (2 <= x <= 4 and 2 <= y <= 4))
            _set_function(matrix, function, xx, yy, 1 if dark else 0)


def _draw_alignment(matrix: list[list[int]], function: list[list[bool]], cx: int, cy: int) -> None:
    for y in range(-2, 3):
        for x in range(-2, 3):
            dark = max(abs(x), abs(y)) != 1
            _set_function(matrix, function, cx + x, cy + y, 1 if dark else 0)


def _reserve_format_areas(matrix: list[list[int]], function: list[list[bool]]) -> None:
    size = len(matrix)
    for index in range(6):
        _set_function(matrix, function, 8, index, 0)
    _set_function(matrix, function, 8, 7, 0)
    _set_function(matrix, function, 8, 8, 0)
    _set_function(matrix, function, 7, 8, 0)
    for index in range(9, 15):
        _set_function(matrix, function, 14 - index, 8, 0)
    for index in range(8):
        _set_function(matrix, function, size - 1 - index, 8, 0)
    for index in range(8, 15):
        _set_function(matrix, function, 8, size - 15 + index, 0)


def _set_function(matrix: list[list[int]], function: list[list[bool]], x: int, y: int, value: int) -> None:
    matrix[y][x] = value
    function[y][x] = True


def _place_data_bits(matrix: list[list[int]], function: list[list[bool]], bits: list[int], mask: int) -> None:
    size = len(matrix)
    bit_index = 0
    upward = True
    x = size - 1
    while x > 0:
        if x == 6:
            x -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for y in rows:
            for dx in (0, 1):
                xx = x - dx
                if function[y][xx]:
                    continue
                bit = bits[bit_index] if bit_index < len(bits) else 0
                if _mask_bit(mask, xx, y):
                    bit ^= 1
                matrix[y][xx] = bit
                bit_index += 1
        upward = not upward
        x -= 2


def _mask_bit(mask: int, x: int, y: int) -> bool:
    if mask == 0:
        return (x + y) % 2 == 0
    if mask == 1:
        return y % 2 == 0
    if mask == 2:
        return x % 3 == 0
    if mask == 3:
        return (x + y) % 3 == 0
    if mask == 4:
        return (y // 2 + x // 3) % 2 == 0
    if mask == 5:
        return (x * y) % 2 + (x * y) % 3 == 0
    if mask == 6:
        return ((x * y) % 2 + (x * y) % 3) % 2 == 0
    if mask == 7:
        return ((x + y) % 2 + (x * y) % 3) % 2 == 0
    raise QrError(f"Invalid mask: {mask}")


def _draw_format_bits(matrix: list[list[int]], function: list[list[bool]], mask: int) -> None:
    del function
    size = len(matrix)
    bits = _format_bits(mask)
    for index in range(6):
        matrix[index][8] = _bit(bits, index)
    matrix[7][8] = _bit(bits, 6)
    matrix[8][8] = _bit(bits, 7)
    matrix[8][7] = _bit(bits, 8)
    for index in range(9, 15):
        matrix[8][14 - index] = _bit(bits, index)
    for index in range(8):
        matrix[8][size - 1 - index] = _bit(bits, index)
    for index in range(8, 15):
        matrix[size - 15 + index][8] = _bit(bits, index)
    matrix[size - 8][8] = 1


def _format_bits(mask: int) -> int:
    data = mask  # Error correction level M uses format bits 00.
    value = data << 10
    generator = 0x537
    for shift in range(14, 9, -1):
        if (value >> shift) & 1:
            value ^= generator << (shift - 10)
    return ((data << 10) | value) ^ 0x5412


def _bit(value: int, index: int) -> int:
    return (value >> index) & 1


def _penalty(matrix: list[list[int]]) -> int:
    size = len(matrix)
    total = 0
    for rows in (matrix, _columns(matrix)):
        for row in rows:
            run_color = row[0]
            run_len = 1
            for cell in row[1:]:
                if cell == run_color:
                    run_len += 1
                else:
                    if run_len >= 5:
                        total += 3 + run_len - 5
                    run_color = cell
                    run_len = 1
            if run_len >= 5:
                total += 3 + run_len - 5

    for y in range(size - 1):
        for x in range(size - 1):
            cell = matrix[y][x]
            if matrix[y][x + 1] == cell and matrix[y + 1][x] == cell and matrix[y + 1][x + 1] == cell:
                total += 3

    finder_a = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    finder_b = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    for rows in (matrix, _columns(matrix)):
        for row in rows:
            for index in range(len(row) - 10):
                segment = row[index : index + 11]
                if segment == finder_a or segment == finder_b:
                    total += 40

    dark = sum(1 for row in matrix for cell in row if cell)
    percent = dark * 100 // (size * size)
    total += abs(percent - 50) // 5 * 10
    return total


def _columns(matrix: list[list[int]]) -> list[list[int]]:
    return [[matrix[y][x] for y in range(len(matrix))] for x in range(len(matrix))]


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", binascii.crc32(kind + data) & 0xFFFFFFFF)
