from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SERVER_SRC = ROOT / "server"
if str(SERVER_SRC) not in sys.path:
    sys.path.insert(0, str(SERVER_SRC))

from remote_explorer_server.config import DEFAULT_CONTROL_PORT, DEFAULT_DISCOVERY_PORT
from remote_explorer_server.protocol import decode_datagram, encode_message, make_request_id
from remote_explorer_server.security import (
    derive_password_key,
    hmac_hex,
    proof_message,
    session_message,
    sign_message,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Remote Explorer development UDP client")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover", help="Discover servers on the LAN")
    discover.add_argument("--port", type=int, default=DEFAULT_DISCOVERY_PORT)
    discover.add_argument("--timeout", type=float, default=3.0)

    for name in ["navigate", "click", "click-selector", "text", "set-input", "scroll", "key", "status", "back", "forward", "reload"]:
        command_parser = subparsers.add_parser(name)
        add_connection_args(command_parser)

    subparsers.choices["navigate"].add_argument("url")
    subparsers.choices["click"].add_argument("x", type=float)
    subparsers.choices["click"].add_argument("y", type=float)
    subparsers.choices["click-selector"].add_argument("selector")
    subparsers.choices["text"].add_argument("text")
    subparsers.choices["set-input"].add_argument("selector")
    subparsers.choices["set-input"].add_argument("text")
    subparsers.choices["set-input"].add_argument("--submit", action="store_true")
    subparsers.choices["scroll"].add_argument("dy", type=float)
    subparsers.choices["scroll"].add_argument("--dx", type=float, default=0)
    subparsers.choices["key"].add_argument("key", choices=["Enter", "Escape", "Backspace"])

    evaluate = subparsers.add_parser("evaluate-js", help="Run JS when server enables --allow-evaluate-js")
    add_connection_args(evaluate)
    evaluate.add_argument("script")

    return parser


def add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", required=True, help="Server host or IP address")
    parser.add_argument("--port", type=int, default=DEFAULT_CONTROL_PORT)
    parser.add_argument("--password")
    parser.add_argument("--timeout", type=float, default=3.0)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "discover":
        return discover(args)

    action, payload = command_payload(args)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(args.timeout)
        session_id, session_key = authenticate(sock, args.host, args.port, args.password)
        response = send_command(sock, args.host, args.port, session_id, session_key, action, payload)
        print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0


def discover(args: argparse.Namespace) -> int:
    deadline = time.time() + args.timeout
    seen: set[tuple[str, int, str]] = set()

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", args.port))
        sock.settimeout(0.3)
        while time.time() < deadline:
            try:
                data, address = sock.recvfrom(65535)
            except (socket.timeout, ConnectionResetError):
                continue
            try:
                decoded = decode_datagram(data)
            except Exception:
                continue
            if decoded.get("type") != "offer":
                continue
            server = decoded.get("server") or {}
            key = (address[0], int(server.get("control_port", 0)), str(server.get("id", "")))
            if key in seen:
                continue
            seen.add(key)
            print(json.dumps({"address": address[0], "server": server}, ensure_ascii=False, indent=2))

    if not seen:
        print("No servers discovered")
        return 1
    return 0


def authenticate(
    sock: socket.socket,
    host: str,
    port: int,
    password: str | None,
) -> tuple[str, bytes | None]:
    client_id = socket.gethostname() or "python-dev-client"
    hello = {
        "v": 1,
        "type": "auth_hello",
        "request_id": make_request_id(),
        "client": {
            "id": client_id,
            "name": "Python Dev Client",
        },
    }
    response = request(sock, host, port, hello)
    if response.get("type") == "auth_ok":
        return response["session"]["id"], None

    if response.get("type") != "auth_challenge":
        raise RuntimeError(f"Unexpected auth response: {response}")
    if not password:
        raise RuntimeError("Server requires a password. Pass --password.")

    challenge = response["challenge"]
    client_nonce = make_request_id()
    key = derive_password_key(password, challenge["salt"], int(challenge["iterations"]))
    proof = hmac_hex(key, proof_message(client_id, challenge["server_nonce"], client_nonce))
    session_key = bytes.fromhex(hmac_hex(key, session_message(client_id, challenge["server_nonce"], client_nonce)))
    auth_response = {
        "v": 1,
        "type": "auth_response",
        "request_id": make_request_id(),
        "client_id": client_id,
        "server_nonce": challenge["server_nonce"],
        "client_nonce": client_nonce,
        "proof": proof,
    }
    auth_ok = request(sock, host, port, auth_response)
    if auth_ok.get("type") != "auth_ok":
        raise RuntimeError(f"Authentication failed: {auth_ok}")
    return auth_ok["session"]["id"], session_key


def send_command(
    sock: socket.socket,
    host: str,
    port: int,
    session_id: str,
    session_key: bytes | None,
    action: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "v": 1,
        "type": "command",
        "request_id": make_request_id(),
        "command": action,
        "payload": payload,
        "auth": {
            "session_id": session_id,
        },
    }
    if session_key is not None:
        message["auth"]["counter"] = 1
        message["auth"]["signature"] = sign_message(message, session_key)
    return request(sock, host, port, message)


def request(sock: socket.socket, host: str, port: int, message: dict[str, Any]) -> dict[str, Any]:
    sock.sendto(encode_message(message), (host, port))
    while True:
        data, _ = sock.recvfrom(65535)
        response = decode_datagram(data)
        if response.get("request_id") == message.get("request_id"):
            if response.get("type") == "error":
                raise RuntimeError(json.dumps(response, ensure_ascii=False))
            return response


def command_payload(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    if args.command == "navigate":
        return "navigate", {"url": args.url}
    if args.command == "click":
        return "click", {"x": args.x, "y": args.y}
    if args.command == "click-selector":
        return "click_selector", {"selector": args.selector}
    if args.command == "text":
        return "text", {"text": args.text}
    if args.command == "set-input":
        return "set_input", {"selector": args.selector, "text": args.text, "submit": args.submit}
    if args.command == "scroll":
        return "scroll", {"dx": args.dx, "dy": args.dy}
    if args.command == "key":
        return "key", {"key": args.key}
    if args.command == "evaluate-js":
        return "evaluate_js", {"script": args.script}
    if args.command in {"status", "back", "forward", "reload"}:
        return args.command, {}
    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
