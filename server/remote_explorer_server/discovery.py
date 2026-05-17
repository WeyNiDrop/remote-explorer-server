from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QHostAddress, QUdpSocket

from .config import ServerConfig
from .protocol import decode_datagram, encode_message

CAPABILITIES = [
    "navigate",
    "click",
    "click_selector",
    "text",
    "set_input",
    "scroll",
    "key",
    "close_page",
    "back",
    "forward",
    "reload",
    "status",
    "stream_start",
    "stream_stop",
    "stream_config",
    "stream_status",
    "webrtc_offer",
    "webrtc_stop",
    "webrtc_status",
]


class DiscoveryService(QObject):
    def __init__(self, config: ServerConfig, server_id: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.server_id = server_id
        self.socket = QUdpSocket(self)
        flags = QUdpSocket.ShareAddress | QUdpSocket.ReuseAddressHint
        if not self.socket.bind(QHostAddress.AnyIPv4, config.discovery_port, flags):
            raise RuntimeError(f"Could not bind UDP discovery port {config.discovery_port}")

        self.socket.readyRead.connect(self._read_pending)

        self.announce_timer = QTimer(self)
        self.announce_timer.setInterval(2500)
        self.announce_timer.timeout.connect(self.broadcast_offer)
        self.announce_timer.start()

        QTimer.singleShot(250, self.broadcast_offer)

    def broadcast_offer(self) -> None:
        self._send_offer(QHostAddress.Broadcast, self.config.discovery_port, None)

    def _read_pending(self) -> None:
        while self.socket.hasPendingDatagrams():
            data, host, port = self.socket.readDatagram(self.socket.pendingDatagramSize())
            try:
                message = decode_datagram(bytes(data))
            except Exception:
                continue
            if message.get("type") != "discover":
                continue
            self._send_offer(host, port, message.get("request_id"))

    def _send_offer(self, host: QHostAddress, port: int, request_id: Any) -> None:
        message = {
            "v": 1,
            "type": "offer",
            "request_id": request_id,
            "server": {
                "id": self.server_id,
                "name": self.config.name,
                "control_port": self.config.control_port,
                "auth": "password" if self.config.password else "none",
                "capabilities": CAPABILITIES,
            },
        }
        self.socket.writeDatagram(encode_message(message), host, port)
