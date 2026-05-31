from __future__ import annotations

import unittest
from pathlib import Path

from remote_explorer_server.config import ServerConfig
from remote_explorer_server.local_domain import local_domain_for_machine, sanitize_domain_label
from remote_explorer_server.qr import make_qr_matrix, make_qr_png
from remote_explorer_server.security import AuthError, AuthManager
from remote_explorer_server.web_client import INDEX_HTML, local_client_urls


class H5WebClientTests(unittest.TestCase):
    def test_qr_png_is_generated_without_optional_dependencies(self) -> None:
        data = make_qr_png("http://192.168.1.10:45454/", scale=2)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(b"IHDR", data)
        self.assertIn(b"IDAT", data)

    def test_qr_matrix_scales_to_common_lan_urls(self) -> None:
        matrix = make_qr_matrix("http://192.168.100.200:45454/")
        self.assertGreaterEqual(len(matrix), 21)
        self.assertEqual(len(matrix), len(matrix[0]))
        self.assertTrue(matrix[0][0])

    def test_qr_matrix_decodes_with_standard_module_placement(self) -> None:
        url = "http://laptop-4s4cgo0p.local:45454/"
        self.assertEqual(_decode_qr_byte_payload(make_qr_matrix(url)), url)

    def test_h5_client_uses_popup_input_and_30_fps_option(self) -> None:
        self.assertIn('id="inputModal"', INDEX_HTML)
        self.assertIn('<option value="33">30 fps</option>', INDEX_HTML)
        self.assertNotIn("scrollIntoView", INDEX_HTML)

    def test_auth_manager_can_touch_existing_web_snapshot_session(self) -> None:
        auth = AuthManager(None)
        result = auth.start_auth({"id": "h5"})
        session_id = result["session"]["id"]

        self.assertTrue(auth.touch_session(session_id))
        self.assertFalse(auth.touch_session("missing"))

    def test_h5_web_session_uses_server_side_password_check(self) -> None:
        auth = AuthManager("secret")
        with self.assertRaises(AuthError):
            auth.start_web_session({"id": "h5"}, "")

        session = auth.start_web_session({"id": "h5"}, "secret")
        verified = auth.verify_command(
            {
                "v": 1,
                "type": "command",
                "request_id": "cmd-1",
                "command": "status",
                "payload": {},
                "auth": {"session_id": session["id"]},
            }
        )
        self.assertEqual(verified.client_id, "h5")

    def test_web_port_defaults_to_control_port(self) -> None:
        config = ServerConfig(
            name="test",
            discovery_port=45454,
            control_port=45455,
            password=None,
            data_dir=Path("."),
            start_url="about:blank",
        )
        self.assertEqual(config.effective_web_port, 45455)

    def test_local_domain_uses_ascii_machine_name_without_spaces(self) -> None:
        self.assertEqual(sanitize_domain_label("Living Room PC"), "livingroompc")
        self.assertEqual(sanitize_domain_label("客厅 PC_01"), "pc-01")
        self.assertEqual(local_domain_for_machine("Living Room PC"), "livingroompc.local")

    def test_local_client_urls_prefer_registered_domain_with_ip_fallbacks(self) -> None:
        self.assertEqual(
            local_client_urls(45454, domain="livingroompc.local", addresses=["192.168.1.8"]),
            [
                "http://livingroompc.local:45454/",
                "http://192.168.1.8:45454/",
            ],
        )


def _decode_qr_byte_payload(matrix: list[list[bool]]) -> str:
    size = len(matrix)
    mask = _standard_format_mask(matrix)
    function = _standard_function_modules(size)
    bits: list[int] = []
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
                bit = int(matrix[y][xx])
                if _standard_mask_bit(mask, xx, y):
                    bit ^= 1
                bits.append(bit)
        upward = not upward
        x -= 2

    cursor = 0

    def take(count: int) -> int:
        nonlocal cursor
        value = 0
        for bit in bits[cursor : cursor + count]:
            value = (value << 1) | bit
        cursor += count
        return value

    mode = take(4)
    if mode != 0b0100:
        raise AssertionError(f"Expected byte-mode QR payload, got mode {mode}")
    length = take(8)
    return bytes(take(8) for _ in range(length)).decode("utf-8")


def _standard_function_modules(size: int) -> list[list[bool]]:
    version = (size - 17) // 4
    function = [[False for _ in range(size)] for _ in range(size)]

    def mark(x: int, y: int) -> None:
        if 0 <= x < size and 0 <= y < size:
            function[y][x] = True

    for left, top in ((0, 0), (size - 7, 0), (0, size - 7)):
        for y in range(-1, 8):
            for x in range(-1, 8):
                mark(left + x, top + y)

    for index in range(8, size - 8):
        mark(6, index)
        mark(index, 6)

    for index in range(6):
        mark(8, index)
    for point in ((8, 7), (8, 8), (7, 8)):
        mark(*point)
    for index in range(9, 15):
        mark(14 - index, 8)
    for index in range(8):
        mark(size - 1 - index, 8)
    for index in range(8, 15):
        mark(8, size - 15 + index)
    mark(8, size - 8)

    alignment_positions = {
        1: [],
        2: [6, 18],
        3: [6, 22],
        4: [6, 26],
        5: [6, 30],
        6: [6, 34],
    }[version]
    for cy in alignment_positions:
        for cx in alignment_positions:
            if function[cy][cx]:
                continue
            for y in range(cy - 2, cy + 3):
                for x in range(cx - 2, cx + 3):
                    mark(x, y)
    return function


def _standard_format_mask(matrix: list[list[bool]]) -> int:
    positions = (
        [(8, index) for index in range(6)]
        + [(8, 7), (8, 8), (7, 8)]
        + [(14 - index, 8) for index in range(9, 15)]
    )
    actual = sum((1 << index) for index, (x, y) in enumerate(positions) if matrix[y][x])
    matches = [
        (sum(int(bit) for bit in bin(actual ^ _standard_format_bits(mask))[2:]), mask)
        for mask in range(8)
    ]
    distance, mask = min(matches)
    if distance > 3:
        raise AssertionError("QR format bits are not recoverable")
    return mask


def _standard_format_bits(mask: int) -> int:
    value = mask << 10
    generator = 0x537
    for shift in range(14, 9, -1):
        if (value >> shift) & 1:
            value ^= generator << (shift - 10)
    return ((mask << 10) | value) ^ 0x5412


def _standard_mask_bit(mask: int, x: int, y: int) -> bool:
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
    raise AssertionError(f"Unexpected mask {mask}")
