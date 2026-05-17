import unittest

from remote_explorer_server.protocol import (
    ProtocolError,
    canonical_json,
    decode_datagram,
    encode_message,
    normalize_url,
)


class ProtocolTests(unittest.TestCase):
    def test_encode_decode_roundtrip(self) -> None:
        message = {"type": "status", "request_id": "abc", "payload": {"b": 2, "a": 1}}

        decoded = decode_datagram(encode_message(message))

        self.assertEqual(decoded["v"], 1)
        self.assertEqual(decoded["type"], "status")
        self.assertEqual(decoded["payload"], {"a": 1, "b": 2})

    def test_canonical_json_is_stable(self) -> None:
        self.assertEqual(canonical_json({"b": 2, "a": 1}), '{"a":1,"b":2}')

    def test_decode_rejects_bad_version(self) -> None:
        with self.assertRaises(ProtocolError):
            decode_datagram(b'{"v":999,"type":"x"}')

    def test_normalize_url_adds_scheme(self) -> None:
        self.assertEqual(normalize_url("example.com"), "https://example.com")
        self.assertEqual(normalize_url("about:blank"), "about:blank")


if __name__ == "__main__":
    unittest.main()
