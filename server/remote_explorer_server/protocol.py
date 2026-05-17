from __future__ import annotations

import json
import uuid
from typing import Any

PROTOCOL_VERSION = 1
MAX_RECOMMENDED_DATAGRAM_BYTES = 1200


class ProtocolError(ValueError):
    """Raised when a datagram is not a valid Remote Explorer message."""


def make_request_id() -> str:
    return uuid.uuid4().hex


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def encode_message(message: dict[str, Any]) -> bytes:
    if "v" not in message:
        message = {"v": PROTOCOL_VERSION, **message}
    return canonical_json(message).encode("utf-8")


def decode_datagram(data: bytes) -> dict[str, Any]:
    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("Datagram is not valid UTF-8 JSON") from exc

    if not isinstance(message, dict):
        raise ProtocolError("Datagram JSON root must be an object")

    if message.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(f"Unsupported protocol version: {message.get('v')!r}")

    message_type = message.get("type")
    if not isinstance(message_type, str) or not message_type:
        raise ProtocolError("Message type is required")

    return message


def result_message(request_id: str | None, result: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "type": "result",
        "request_id": request_id,
        "ok": True,
        "result": result or {},
    }


def error_message(
    request_id: str | None,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {
        "v": PROTOCOL_VERSION,
        "type": "error",
        "request_id": request_id,
        "ok": False,
        "error": error,
    }


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ProtocolError("URL cannot be empty")
    if "://" not in url and not url.startswith(("about:", "file:")):
        return f"https://{url}"
    return url
