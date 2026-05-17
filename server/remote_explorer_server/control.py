from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QHostAddress, QUdpSocket

from .config import ServerConfig
from .protocol import decode_datagram, encode_message, error_message, result_message
from .security import AuthError, AuthManager

BrowserResponder = Callable[[dict[str, Any]], None]


class ControlService(QObject):
    def __init__(
        self,
        config: ServerConfig,
        auth_manager: AuthManager,
        command_handler: Callable[[str, dict[str, Any], BrowserResponder], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.auth_manager = auth_manager
        self.command_handler = command_handler
        self.socket = QUdpSocket(self)
        flags = QUdpSocket.ShareAddress | QUdpSocket.ReuseAddressHint
        if not self.socket.bind(QHostAddress.AnyIPv4, config.control_port, flags):
            raise RuntimeError(f"Could not bind UDP control port {config.control_port}")
        self.socket.readyRead.connect(self._read_pending)

    def _read_pending(self) -> None:
        while self.socket.hasPendingDatagrams():
            data, host, port = self.socket.readDatagram(self.socket.pendingDatagramSize())
            request_id = None
            try:
                message = decode_datagram(bytes(data))
                request_id = message.get("request_id")
                self._handle_message(message, host, port)
            except AuthError as exc:
                self._send(host, port, error_message(request_id, exc.code, exc.message))
            except Exception as exc:
                self._send(host, port, error_message(request_id, "bad_request", str(exc)))

    def _handle_message(self, message: dict[str, Any], host: QHostAddress, port: int) -> None:
        message_type = message.get("type")
        request_id = message.get("request_id")

        if message_type == "auth_hello":
            body = self.auth_manager.start_auth(message.get("client") or {})
            self._send(host, port, {"v": 1, "request_id": request_id, **body})
            return

        if message_type == "auth_response":
            body = self.auth_manager.complete_auth(message)
            self._send(host, port, {"v": 1, "request_id": request_id, **body})
            return

        if message_type != "command":
            self._send(
                host,
                port,
                error_message(request_id, "unknown_message", f"Unknown message type: {message_type}"),
            )
            return

        self.auth_manager.verify_command(message)
        command = message.get("command")
        if not isinstance(command, str) or not command:
            self._send(host, port, error_message(request_id, "bad_command", "Command name is required"))
            return

        payload = message.get("payload") or {}
        if not isinstance(payload, dict):
            self._send(host, port, error_message(request_id, "bad_payload", "Payload must be an object"))
            return
        payload = dict(payload)
        payload["_source_host"] = host.toString()
        payload["_source_port"] = int(port)

        def respond(result: dict[str, Any]) -> None:
            if result.get("ok") is False:
                error = result.get("error") or {}
                self._send(
                    host,
                    port,
                    error_message(
                        request_id,
                        str(error.get("code") or "command_failed"),
                        str(error.get("message") or "Command failed"),
                    ),
                )
                return
            self._send(host, port, result_message(request_id, result.get("result") or result))

        self.command_handler(command, payload, respond)

    def _send(self, host: QHostAddress, port: int, message: dict[str, Any]) -> None:
        self.socket.writeDatagram(encode_message(message), host, port)
