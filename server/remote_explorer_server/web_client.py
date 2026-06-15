from __future__ import annotations

import json
import logging
import socket
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QSize, Qt, Signal, Slot
from PySide6.QtGui import QImage

from .config import ServerConfig
from .local_domain import MdnsHostResponder, local_domain_for_machine, normalize_local_domain
from .protocol import PROTOCOL_VERSION, error_message, result_message
from .qr import make_qr_png
from .security import AuthError, AuthManager

LOGGER = logging.getLogger("remote_explorer.web_client")
MAX_POST_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 20.0
SNAPSHOT_MAX_WIDTH = 1280
SNAPSHOT_DEFAULT_WIDTH = 960
SNAPSHOT_DEFAULT_QUALITY = 55


@dataclass
class _JsonRequest:
    message: dict[str, Any]
    client_host: str
    client_port: int
    event: threading.Event
    result: dict[str, Any] | None = None


@dataclass
class _SnapshotRequest:
    session_id: str
    width: int
    quality: int
    event: threading.Event
    result: bytes | None = None
    error: dict[str, Any] | None = None


@dataclass
class _WebLoginRequest:
    client: dict[str, Any]
    password: str | None
    client_host: str
    client_port: int
    event: threading.Event
    result: dict[str, Any] | None = None


class WebClientService(QObject):
    json_request_ready = Signal(object)
    snapshot_request_ready = Signal(object)
    web_login_request_ready = Signal(object)
    client_changed = Signal(str, str, int)

    def __init__(
        self,
        config: ServerConfig,
        auth_manager: AuthManager,
        command_handler: Any,
        browser_window: Any,
        *,
        server_id: str,
        capabilities: list[str] | None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.auth_manager = auth_manager
        self.command_handler = command_handler
        self.browser_window = browser_window
        self.server_id = server_id
        self.capabilities = capabilities or []
        self.bound_port = 0
        self.urls: list[str] = []
        self.local_domain = ""
        self.local_domain_registered = False
        self._mdns: MdnsHostResponder | None = None
        self._httpd: _RemoteExplorerHttpServer | None = None
        self._thread: threading.Thread | None = None
        self._snapshot_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RemoteExplorerH5Snapshot")
        self._snapshot_future: Future[None] | None = None
        self._stopping = False
        self.json_request_ready.connect(self._process_json_request)
        self.snapshot_request_ready.connect(self._process_snapshot_request)
        self.web_login_request_ready.connect(self._process_web_login_request)

    @property
    def client_url(self) -> str:
        return self.urls[0] if self.urls else f"http://127.0.0.1:{self.bound_port}/"

    def start(self) -> None:
        self._stopping = False
        port = self.config.effective_web_port
        self._httpd = _RemoteExplorerHttpServer(("", port), _WebClientHandler, self)
        self.bound_port = int(self._httpd.server_port)
        addresses = _local_ipv4_addresses()
        self.local_domain = normalize_local_domain(self.config.local_domain) or local_domain_for_machine(socket.gethostname(), self.config.name)
        self._mdns = MdnsHostResponder(self.local_domain, addresses, address_provider=_local_ipv4_addresses)
        self.local_domain_registered = self._mdns.start()
        self._refresh_urls()
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="RemoteExplorerH5",
            daemon=True,
        )
        self._thread.start()
        LOGGER.info("H5 client server listening on TCP %s url=%s", self.bound_port, self.client_url)

    def stop(self) -> None:
        self._stopping = True
        httpd = self._httpd
        if httpd is not None:
            self._httpd = None
            httpd.shutdown()
            httpd.server_close()
            thread = self._thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
        mdns = self._mdns
        self._mdns = None
        self.local_domain_registered = False
        if mdns is not None:
            mdns.stop()
        self._snapshot_executor.shutdown(wait=False, cancel_futures=True)

    def qr_png(self, *, scale: int = 7, border: int = 4) -> bytes:
        self._refresh_urls()
        return make_qr_png(self.client_url, scale=scale, border=border)

    def info(self) -> dict[str, Any]:
        self._refresh_urls()
        return {
            "server": self._server_payload(),
            "web": {
                "url": self.client_url,
                "urls": self.urls,
                "port": self.bound_port,
                "local_domain": self.local_domain,
                "local_domain_registered": self.local_domain_registered,
                "snapshot": {
                    "default_width": SNAPSHOT_DEFAULT_WIDTH,
                    "max_width": SNAPSHOT_MAX_WIDTH,
                    "quality": SNAPSHOT_DEFAULT_QUALITY,
                },
            },
        }

    def handle_json_message(self, message: dict[str, Any], client_host: str, client_port: int) -> dict[str, Any]:
        request = _JsonRequest(
            message=message,
            client_host=client_host,
            client_port=client_port,
            event=threading.Event(),
        )
        self.json_request_ready.emit(request)
        if not request.event.wait(REQUEST_TIMEOUT_SECONDS):
            return error_message(_request_id(message), "timeout", "Command timed out")
        return request.result or error_message(_request_id(message), "empty_response", "Command returned no response")

    def capture_snapshot(self, session_id: str, width: int, quality: int) -> tuple[bytes | None, dict[str, Any] | None]:
        request = _SnapshotRequest(
            session_id=session_id,
            width=width,
            quality=quality,
            event=threading.Event(),
        )
        self.snapshot_request_ready.emit(request)
        if not request.event.wait(REQUEST_TIMEOUT_SECONDS):
            return None, {"code": "timeout", "message": "Snapshot timed out"}
        return request.result, request.error

    def handle_web_login(
        self,
        client: dict[str, Any],
        password: str | None,
        client_host: str,
        client_port: int,
    ) -> dict[str, Any]:
        request = _WebLoginRequest(
            client=client,
            password=password,
            client_host=client_host,
            client_port=client_port,
            event=threading.Event(),
        )
        self.web_login_request_ready.emit(request)
        if not request.event.wait(REQUEST_TIMEOUT_SECONDS):
            return {"ok": False, "error": {"code": "timeout", "message": "Login timed out"}}
        return request.result or {"ok": False, "error": {"code": "empty_response", "message": "Login returned no response"}}

    def _refresh_urls(self) -> None:
        if self._mdns is not None:
            self._mdns.refresh_addresses()
            addresses = self._mdns.current_addresses()
        else:
            addresses = _local_ipv4_addresses()
        self.urls = local_client_urls(
            self.bound_port,
            domain=self.local_domain if self.local_domain_registered else None,
            addresses=addresses,
        )

    @Slot(object)
    def _process_json_request(self, request: _JsonRequest) -> None:
        try:
            self._handle_json_request(request)
        except AuthError as exc:
            request.result = error_message(_request_id(request.message), exc.code, exc.message)
            request.event.set()
        except Exception as exc:
            request.result = error_message(_request_id(request.message), "bad_request", str(exc))
            request.event.set()

    @Slot(object)
    def _process_snapshot_request(self, request: _SnapshotRequest) -> None:
        try:
            if not self.auth_manager.touch_session(request.session_id):
                request.error = {"code": "invalid_session", "message": "Session is invalid or expired"}
                request.event.set()
                return
            if self._stopping:
                request.error = {"code": "server_stopping", "message": "Server is stopping"}
                request.event.set()
                return

            browser = getattr(self.browser_window, "browser", None)
            if browser is not None and hasattr(browser, "capture_jpeg"):
                if self._snapshot_future is not None and not self._snapshot_future.done():
                    request.error = {"code": "snapshot_busy", "message": "A snapshot is already in progress"}
                    request.event.set()
                    return
                self._snapshot_future = self._snapshot_executor.submit(self._capture_snapshot_worker, request)
            else:
                request.result = self._capture_jpeg(request.width, request.quality)
                request.event.set()
        except Exception as exc:
            request.error = {"code": "snapshot_failed", "message": str(exc)}
            request.event.set()

    def _capture_snapshot_worker(self, request: _SnapshotRequest) -> None:
        try:
            request.result = self._capture_jpeg(request.width, request.quality)
        except Exception as exc:
            request.error = {"code": "snapshot_failed", "message": str(exc)}
        finally:
            request.event.set()

    @Slot(object)
    def _process_web_login_request(self, request: _WebLoginRequest) -> None:
        try:
            session = self.auth_manager.start_web_session(request.client, request.password)
            name = str(request.client.get("name") or request.client.get("id") or "H5 Client")
            self.client_changed.emit(name, request.client_host, request.client_port)
            request.result = {"ok": True, "session": session}
        except AuthError as exc:
            request.result = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:
            request.result = {"ok": False, "error": {"code": "login_failed", "message": str(exc)}}
        finally:
            request.event.set()

    def _handle_json_request(self, request: _JsonRequest) -> None:
        message = request.message
        message_type = message.get("type")
        request_id = _request_id(message)

        if message.get("v") != PROTOCOL_VERSION:
            request.result = error_message(request_id, "bad_protocol", f"Unsupported protocol version: {message.get('v')!r}")
            request.event.set()
            return

        if message_type == "discover":
            request.result = {
                "v": PROTOCOL_VERSION,
                "type": "offer",
                "request_id": request_id,
                "server": self._server_payload(),
            }
            request.event.set()
            return

        if message_type == "auth_hello":
            client = message.get("client") or {}
            if isinstance(client, dict):
                self.client_changed.emit(str(client.get("name") or client.get("id") or "H5 Client"), request.client_host, request.client_port)
            body = self.auth_manager.start_auth(client if isinstance(client, dict) else {})
            request.result = {"v": PROTOCOL_VERSION, "request_id": request_id, **body}
            request.event.set()
            return

        if message_type == "auth_response":
            body = self.auth_manager.complete_auth(message)
            request.result = {"v": PROTOCOL_VERSION, "request_id": request_id, **body}
            request.event.set()
            return

        if message_type != "command":
            request.result = error_message(request_id, "unknown_message", f"Unknown message type: {message_type}")
            request.event.set()
            return

        self.auth_manager.verify_command(message)
        command = message.get("command")
        if not isinstance(command, str) or not command:
            request.result = error_message(request_id, "bad_command", "Command name is required")
            request.event.set()
            return

        payload = message.get("payload") or {}
        if not isinstance(payload, dict):
            request.result = error_message(request_id, "bad_payload", "Payload must be an object")
            request.event.set()
            return
        payload = dict(payload)
        payload["_source_host"] = request.client_host
        payload["_source_port"] = request.client_port
        self.client_changed.emit("H5 Client", request.client_host, request.client_port)

        def respond(result: dict[str, Any]) -> None:
            if result.get("ok") is False:
                error = result.get("error") or {}
                request.result = error_message(
                    request_id,
                    str(error.get("code") or "command_failed"),
                    str(error.get("message") or "Command failed"),
                )
            else:
                request.result = result_message(request_id, result.get("result") or result)
            request.event.set()

        try:
            self.command_handler(command, payload, respond)
        except Exception as exc:
            request.result = error_message(request_id, "command_failed", str(exc))
            request.event.set()

    def _server_payload(self) -> dict[str, Any]:
        return {
            "id": self.server_id,
            "name": self.config.name,
            "control_port": self.config.control_port,
            "web_port": self.bound_port,
            "web_url": self.client_url,
            "local_domain": self.local_domain,
            "local_domain_registered": self.local_domain_registered,
            "auth": self.auth_manager.auth_mode,
            "capabilities": self.capabilities,
        }

    def _capture_jpeg(self, width: int, quality: int) -> bytes:
        width = _clamp(width, 1, SNAPSHOT_MAX_WIDTH, SNAPSHOT_DEFAULT_WIDTH)
        quality = _clamp(quality, 35, 90, SNAPSHOT_DEFAULT_QUALITY)

        browser = getattr(self.browser_window, "browser", None)
        if browser is not None and hasattr(browser, "capture_jpeg"):
            data = browser.capture_jpeg(quality)
            if width <= 0:
                return data
            image = QImage()
            if not image.loadFromData(data, "JPG"):
                return data
            if image.width() > width:
                image = image.scaledToWidth(width, Qt.TransformationMode.FastTransformation)
            return _encode_jpeg(image, quality)

        view = getattr(self.browser_window, "view", None)
        if view is None:
            raise RuntimeError("No browser preview source is available")
        pixmap = view.grab()
        if pixmap.isNull():
            raise RuntimeError("Browser preview is empty")
        image = pixmap.toImage().copy()
        if image.width() > width:
            image = image.scaled(QSize(width, max(1, round(image.height() * width / image.width()))), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
        return _encode_jpeg(image, quality)


class _RemoteExplorerHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], service: WebClientService) -> None:
        super().__init__(server_address, handler_class)
        self.service = service


class _WebClientHandler(BaseHTTPRequestHandler):
    server_version = "RemoteExplorerH5/0.1"

    @property
    def service(self) -> WebClientService:
        return self.server.service  # type: ignore[attr-defined,return-value]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"", "/", "/client"}:
            self._send_bytes(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8", cache=False)
            return
        if parsed.path == "/api/info":
            self._send_json(self.service.info())
            return
        if parsed.path == "/api/qr.png":
            scale = _query_int(parsed.query, "scale", 7)
            self._send_bytes(self.service.qr_png(scale=_clamp(scale, 3, 16, 7)), "image/png", cache=False)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        try:
            self._do_POST()
        except Exception as exc:
            LOGGER.debug("H5 POST failed: %s", exc)
            self._send_json(
                {"ok": False, "error": {"code": "bad_request", "message": str(exc)}},
                status=HTTPStatus.BAD_REQUEST,
            )

    def _do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/web-login":
            body = self._read_json_body()
            client = body.get("client") or {}
            if not isinstance(client, dict):
                client = {}
            password = body.get("password")
            result = self.service.handle_web_login(
                client,
                str(password) if password is not None else None,
                self.client_address[0],
                int(self.client_address[1]),
            )
            status = HTTPStatus.OK if result.get("ok") is not False else HTTPStatus.UNAUTHORIZED
            self._send_json(result, status=status)
            return
        if parsed.path == "/api/message":
            body = self._read_json_body()
            result = self.service.handle_json_message(body, self.client_address[0], int(self.client_address[1]))
            status = HTTPStatus.OK if result.get("ok", True) is not False else HTTPStatus.BAD_REQUEST
            self._send_json(result, status=status)
            return
        if parsed.path == "/api/snapshot.jpg":
            body = self._read_json_body()
            session_id = str(body.get("session_id") or "")
            width = _clamp(_coerce_int(body.get("width"), SNAPSHOT_DEFAULT_WIDTH), 1, SNAPSHOT_MAX_WIDTH, SNAPSHOT_DEFAULT_WIDTH)
            quality = _clamp(_coerce_int(body.get("quality"), SNAPSHOT_DEFAULT_QUALITY), 35, 90, SNAPSHOT_DEFAULT_QUALITY)
            data, error = self.service.capture_snapshot(session_id, width, quality)
            if error is not None:
                status = HTTPStatus.UNAUTHORIZED if error.get("code") == "invalid_session" else HTTPStatus.SERVICE_UNAVAILABLE
                self._send_json({"ok": False, "error": error}, status=status)
                return
            self._send_bytes(data or b"", "image/jpeg", cache=False)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", self._allowed_origin())
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.debug("H5 HTTP %s - %s", self.address_string(), format % args)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        if length > MAX_POST_BYTES:
            raise ValueError("Request body is too large")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _send_json(self, value: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(data, "application/json; charset=utf-8", status=status, cache=False)

    def _send_bytes(
        self,
        data: bytes,
        content_type: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        cache: bool = True,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", self._allowed_origin())
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _allowed_origin(self) -> str:
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if origin and host and origin in {f"http://{host}", f"https://{host}"}:
            return origin
        return "*"


def _encode_jpeg(image: QImage, quality: int) -> bytes:
    byte_array = QByteArray()
    buffer = QBuffer(byte_array)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        return b""
    try:
        if not image.save(buffer, "JPG", quality):
            return b""
        return bytes(byte_array)
    finally:
        buffer.close()


def local_client_urls(port: int, *, domain: str | None = None, addresses: list[str] | None = None) -> list[str]:
    resolved_addresses = addresses if addresses is not None else _local_ipv4_addresses()
    urls: list[str] = []
    if domain:
        urls.append(f"http://{domain}:{port}/")
    if not resolved_addresses:
        resolved_addresses = ["127.0.0.1"]
    urls.extend(f"http://{address}:{port}/" for address in resolved_addresses)
    return urls


def _local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
            address = item[4][0]
            if _is_usable_lan_address(address):
                addresses.add(address)
    except OSError:
        pass

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            address = sock.getsockname()[0]
            if _is_usable_lan_address(address):
                addresses.add(address)
        finally:
            sock.close()
    except OSError:
        pass

    return sorted(addresses, key=_address_sort_key)


def _is_usable_lan_address(address: str) -> bool:
    if address.startswith("127.") or address.startswith("169.254."):
        return False
    parts = address.split(".")
    return len(parts) == 4 and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)


def _address_sort_key(address: str) -> tuple[int, str]:
    private = address.startswith("10.") or address.startswith("192.168.") or _is_172_private(address)
    return (0 if private else 1, address)


def _is_172_private(address: str) -> bool:
    parts = address.split(".")
    return len(parts) == 4 and parts[0] == "172" and parts[1].isdigit() and 16 <= int(parts[1]) <= 31


def _query_int(query: str, key: str, default: int) -> int:
    values = parse_qs(query).get(key)
    if not values:
        return default
    return _coerce_int(values[0], default)


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: int, minimum: int, maximum: int, default: int) -> int:
    if value < minimum or value > maximum:
        return default
    return value


def _request_id(message: dict[str, Any]) -> str | None:
    value = message.get("request_id")
    return value if isinstance(value, str) else None


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>RCViewer H5</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #171b22;
      --muted: #697381;
      --line: #d9dee7;
      --accent: #087f5b;
      --accent-dark: #06694b;
      --danger: #c24141;
      --shadow: 0 10px 30px rgba(20, 29, 45, .10);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    button, input, select {
      font: inherit;
    }
    button {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      min-height: 40px;
      border-radius: 8px;
      padding: 0 12px;
      font-weight: 700;
    }
    button.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }
    button.primary:active { background: var(--accent-dark); }
    button.danger { color: var(--danger); }
    button.icon {
      min-width: 40px;
      padding: 0;
    }
    input {
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0 12px;
      color: var(--ink);
      background: #fff;
    }
    select {
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0 10px;
      color: var(--ink);
      background: #fff;
    }
    .app {
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
    }
    header {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 12px max(14px, env(safe-area-inset-left)) 10px max(14px, env(safe-area-inset-left));
      background: rgba(246, 247, 249, .92);
      backdrop-filter: blur(14px);
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 0;
      z-index: 5;
    }
    .brand {
      font-size: 18px;
      font-weight: 800;
      white-space: nowrap;
    }
    .status {
      margin-left: auto;
      min-width: 0;
      color: var(--muted);
      text-align: right;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .lang-toggle {
      min-height: 34px;
      padding: 0 10px;
    }
    main {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 320px;
      gap: 12px;
      padding: 12px;
      min-height: 0;
    }
    .viewport {
      position: relative;
      min-height: 300px;
      background: #111;
      border: 1px solid #1f2329;
      border-radius: 8px;
      overflow: hidden;
      box-shadow: var(--shadow);
      touch-action: none;
    }
    .viewport img {
      width: 100%;
      height: 100%;
      display: block;
      object-fit: contain;
      background: #111;
      user-select: none;
      -webkit-user-drag: none;
    }
    .overlay {
      position: absolute;
      inset: auto 12px 12px 12px;
      padding: 8px 10px;
      border-radius: 8px;
      background: rgba(255, 255, 255, .92);
      color: var(--muted);
      pointer-events: none;
      box-shadow: 0 8px 18px rgba(0, 0, 0, .18);
    }
    .controls {
      min-height: 0;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      box-shadow: var(--shadow);
    }
    .row {
      display: flex;
      gap: 8px;
      align-items: center;
    }
    .row + .row { margin-top: 8px; }
    .grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }
    .grid.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      margin: 0 0 8px;
    }
    .panel-head {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 8px;
    }
    .panel-head .label {
      margin: 0;
    }
    .panel-head button {
      margin-left: auto;
      min-height: 32px;
      padding: 0 10px;
    }
    .bookmark-list {
      display: grid;
      gap: 6px;
      max-height: 172px;
      overflow: auto;
    }
    .site-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .site-grid button {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .bookmark {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 36px;
      gap: 6px;
      align-items: center;
    }
    .bookmark button:first-child {
      min-width: 0;
      text-align: left;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .empty {
      color: var(--muted);
      font-size: 12px;
      padding: 4px 0;
    }
    .auth {
      position: fixed;
      inset: 0;
      z-index: 10;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 18px;
      background: rgba(246, 247, 249, .92);
    }
    .auth.show { display: flex; }
    .auth-card {
      width: min(420px, 100%);
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      box-shadow: var(--shadow);
    }
    .error {
      color: var(--danger);
      min-height: 20px;
      margin-top: 8px;
    }
    .input-modal {
      position: fixed;
      inset: 0;
      z-index: 9;
      display: none;
      align-items: flex-end;
      justify-content: center;
      padding: 16px max(16px, env(safe-area-inset-right)) max(16px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left));
      background: rgba(23, 27, 34, .36);
    }
    .input-modal.show { display: flex; }
    .input-card {
      width: min(480px, 100%);
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      box-shadow: var(--shadow);
    }
    .input-card .actions {
      justify-content: flex-end;
      margin-top: 10px;
    }
    @media (max-width: 860px) {
      main {
        grid-template-columns: 1fr;
        grid-template-rows: minmax(280px, 54vh) auto;
      }
      .controls { padding-bottom: max(12px, env(safe-area-inset-bottom)); }
    }
  </style>
</head>
<body>
  <div class="app">
    <header>
      <div class="brand">RCViewer H5</div>
      <div class="status" id="status">Starting</div>
      <button class="lang-toggle" id="langButton" type="button">中文</button>
    </header>
    <main>
      <section class="viewport" id="viewport">
        <img id="frame" alt="">
        <div class="overlay" id="overlay">Connecting</div>
      </section>
      <section class="controls">
        <div class="panel">
          <div class="row">
            <input id="urlInput" inputmode="url" autocomplete="off" placeholder="https://example.com" data-i18n-placeholder="urlPlaceholder">
            <button class="primary" id="goButton" data-i18n="go">Go</button>
          </div>
          <div class="row">
            <button id="backButton" data-i18n="back">Back</button>
            <button id="forwardButton" data-i18n="forward">Forward</button>
            <button id="reloadButton" data-i18n="reload">Reload</button>
          </div>
        </div>
        <div class="panel">
          <p class="label" data-i18n="commonSites">Common Sites</p>
          <div class="site-grid" id="commonSiteList"></div>
        </div>
        <div class="panel">
          <div class="panel-head">
            <p class="label" data-i18n="bookmarks">Bookmarks</p>
            <button id="addBookmarkButton" data-i18n="add">Add</button>
          </div>
          <div class="bookmark-list" id="bookmarkList"></div>
        </div>
        <div class="panel">
          <div class="panel-head">
            <p class="label" data-i18n="stream">Stream</p>
            <button id="streamToggleButton" data-i18n="stopStream">Stop</button>
          </div>
          <div class="grid three">
            <select id="streamResolution">
              <option value="640">360p</option>
              <option value="960">540p</option>
              <option value="1280">720p</option>
            </select>
            <select id="streamQuality">
              <option value="45">Q45</option>
              <option value="55">Q55</option>
              <option value="70">Q70</option>
            </select>
            <select id="streamRate">
              <option value="500">2 fps</option>
              <option value="250">4 fps</option>
              <option value="125">8 fps</option>
              <option value="100">10 fps</option>
              <option value="67">15 fps</option>
              <option value="50">20 fps</option>
              <option value="33">30 fps</option>
            </select>
          </div>
        </div>
        <div class="panel">
          <p class="label" data-i18n="keys">Keys</p>
          <div class="row">
            <button id="backspaceButton" data-i18n="backspace">Backspace</button>
            <button id="escapeButton" data-i18n="esc">Esc</button>
          </div>
        </div>
        <div class="panel">
          <p class="label" data-i18n="scroll">Scroll</p>
          <div class="grid two">
            <button id="scrollUpButton" data-i18n="up">Up</button>
            <button id="scrollDownButton" data-i18n="down">Down</button>
          </div>
        </div>
        <div class="panel">
          <p class="label" data-i18n="media">Media</p>
          <div class="grid">
            <button id="playButton" data-i18n="play">Play</button>
            <button id="muteButton" data-i18n="mute">Mute</button>
            <button id="fullButton" data-i18n="full">Full</button>
            <button id="seekBackButton">-10s</button>
            <button id="seekForwardButton">+10s</button>
            <button id="exitFullButton" data-i18n="exit">Exit</button>
          </div>
        </div>
      </section>
    </main>
  </div>

  <div class="auth" id="authPanel">
    <div class="auth-card">
      <div class="brand">RCViewer H5</div>
      <div class="row" style="margin-top:12px">
        <input id="passwordInput" type="password" autocomplete="current-password" placeholder="Password" data-i18n-placeholder="passwordPlaceholder">
        <button class="primary" id="unlockButton" data-i18n="unlock">Unlock</button>
      </div>
      <div class="error" id="authError"></div>
    </div>
  </div>

  <div class="input-modal" id="inputModal" aria-hidden="true">
    <div class="input-card" role="dialog" aria-modal="true" aria-labelledby="inputDialogTitle">
      <p class="label" id="inputDialogTitle" data-i18n="remoteInputTitle">Remote Input</p>
      <input id="textInput" autocomplete="off" placeholder="Text" data-i18n-placeholder="textPlaceholder">
      <div class="row actions">
        <button id="inputCancelButton" data-i18n="cancel">Cancel</button>
        <button class="primary" id="sendTextButton" data-i18n="send">Send</button>
      </div>
    </div>
  </div>

  <script>
    const $ = id => document.getElementById(id);
    const I18N = {
      en: {
        go: "Go",
        back: "Back",
        forward: "Forward",
        reload: "Reload",
        commonSites: "Common Sites",
        bookmarks: "Bookmarks",
        add: "Add",
        input: "Input",
        keys: "Keys",
        remoteInputTitle: "Remote Input",
        cancel: "Cancel",
        send: "Send",
        enter: "Enter",
        backspace: "Backspace",
        esc: "Esc",
        stream: "Stream",
        startStream: "Start",
        stopStream: "Stop",
        streamOn: "Stream on",
        streamOff: "Stream off",
        remoteInputReady: "Remote input captured",
        scroll: "Scroll",
        up: "Up",
        down: "Down",
        media: "Media",
        play: "Play",
        pause: "Pause",
        mute: "Mute",
        unmute: "Unmute",
        full: "Full",
        exit: "Exit",
        unlock: "Unlock",
        urlPlaceholder: "https://example.com",
        textPlaceholder: "Text",
        passwordPlaceholder: "Password",
        noBookmarks: "No bookmarks",
        bookmarkAdded: "Bookmark added",
        connected: "Connected",
        liveSnapshot: "Live snapshot",
        passwordRequired: "Password required",
        offline: "Offline",
        couldNotConnect: "Could not connect",
        snapshotFailed: "Snapshot failed",
        sessionExpired: "Session expired, reconnecting",
        statusFailed: "Status failed",
        navigateFailed: "Navigate failed",
        clickSent: "Click sent",
        clickFailed: "Click failed",
        remove: "Remove"
      },
      zh: {
        go: "打开",
        back: "后退",
        forward: "前进",
        reload: "刷新",
        commonSites: "常用网址",
        bookmarks: "收藏夹",
        add: "加入",
        input: "输入",
        keys: "按键",
        remoteInputTitle: "远端输入",
        cancel: "取消",
        send: "发送",
        enter: "回车",
        backspace: "退格",
        esc: "退出焦点",
        stream: "串流",
        startStream: "开启",
        stopStream: "关闭",
        streamOn: "串流已开启",
        streamOff: "串流已关闭",
        remoteInputReady: "已捕获远端输入框",
        scroll: "滚动",
        up: "向上",
        down: "向下",
        media: "媒体",
        play: "播放",
        pause: "暂停",
        mute: "静音",
        unmute: "解除静音",
        full: "全屏",
        exit: "退出",
        unlock: "解锁",
        urlPlaceholder: "输入网址",
        textPlaceholder: "输入文本",
        passwordPlaceholder: "密码",
        noBookmarks: "暂无收藏",
        bookmarkAdded: "已加入收藏",
        connected: "已连接",
        liveSnapshot: "实时预览",
        passwordRequired: "需要密码",
        offline: "离线",
        couldNotConnect: "无法连接",
        snapshotFailed: "预览失败",
        sessionExpired: "会话已过期，正在重新连接",
        statusFailed: "状态获取失败",
        navigateFailed: "打开失败",
        clickSent: "点击已发送",
        clickFailed: "点击失败",
        remove: "删除"
      }
    };
    const COMMON_SITES = [
      { name: { en: "YouTube", zh: "YouTube" }, url: "https://www.youtube.com" },
      { name: { en: "Bilibili", zh: "哔哩哔哩" }, url: "https://www.bilibili.com" },
      { name: { en: "Douyin", zh: "抖音" }, url: "https://www.douyin.com" },
      { name: { en: "TikTok", zh: "TikTok" }, url: "https://www.tiktok.com" },
      { name: { en: "Mango TV", zh: "芒果TV" }, url: "https://www.mgtv.com" },
      { name: { en: "iQIYI", zh: "爱奇艺" }, url: "https://www.iqiyi.com" }
    ];
    const state = {
      info: null,
      clientId: localStorage.getItem("remoteExplorerH5ClientId") || makeClientId(),
      sessionId: "",
      sessionAuth: "",
      authRetrying: false,
      snapshotUrl: "",
      language: loadLanguage(),
      bookmarks: loadBookmarks(),
      lastRemoteUrl: "",
      urlDirty: false,
      remoteInputActive: false,
      mediaStatus: null,
      streamEnabled: loadBoolean("remoteExplorerH5StreamEnabled", true),
      streamWidth: loadNumber("remoteExplorerH5StreamWidth", 960),
      streamQuality: loadNumber("remoteExplorerH5StreamQuality", 55),
      streamIntervalMs: loadNumber("remoteExplorerH5StreamInterval", 250),
      snapshotTimer: null,
      snapshotInFlight: false,
      snapshotAbortController: null,
      commandChain: Promise.resolve(),
      lastWheelAt: 0,
      touchStart: null
    };
    localStorage.setItem("remoteExplorerH5ClientId", state.clientId);

    function setStatus(text) { $("status").textContent = text; }
    function setOverlay(text) { $("overlay").textContent = text; }
    function requestId(prefix) { return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`; }
    function loadBoolean(key, fallback) {
      const value = localStorage.getItem(key);
      if (value === "true") return true;
      if (value === "false") return false;
      return fallback;
    }
    function loadNumber(key, fallback) {
      const value = Number(localStorage.getItem(key));
      return Number.isFinite(value) && value > 0 ? value : fallback;
    }
    function loadLanguage() {
      const saved = localStorage.getItem("remoteExplorerH5Language");
      if (saved === "en" || saved === "zh") return saved;
      return (navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en";
    }
    function tr(key) {
      return (I18N[state.language] && I18N[state.language][key]) || I18N.en[key] || key;
    }
    function applyLanguage() {
      document.documentElement.lang = state.language === "zh" ? "zh-CN" : "en";
      document.querySelectorAll("[data-i18n]").forEach(el => {
        el.textContent = tr(el.getAttribute("data-i18n"));
      });
      document.querySelectorAll("[data-i18n-placeholder]").forEach(el => {
        el.setAttribute("placeholder", tr(el.getAttribute("data-i18n-placeholder")));
      });
      $("langButton").textContent = state.language === "zh" ? "EN" : "中文";
      renderCommonSites();
      renderBookmarks();
      updateMediaButtons();
      updateStreamControls();
    }
    function toggleLanguage() {
      state.language = state.language === "zh" ? "en" : "zh";
      localStorage.setItem("remoteExplorerH5Language", state.language);
      applyLanguage();
    }
    function loadBookmarks() {
      try {
        const value = JSON.parse(localStorage.getItem("remoteExplorerH5Bookmarks") || "[]");
        return Array.isArray(value) ? value.filter(item => item && typeof item.url === "string") : [];
      } catch (_) {
        return [];
      }
    }
    function saveBookmarks() {
      localStorage.setItem("remoteExplorerH5Bookmarks", JSON.stringify(state.bookmarks.slice(0, 40)));
    }
    function bookmarkTitle(url) {
      try {
        const parsed = new URL(url);
        return parsed.hostname || url;
      } catch (_) {
        return url;
      }
    }
    function renderBookmarks() {
      const list = $("bookmarkList");
      list.innerHTML = "";
      if (!state.bookmarks.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = tr("noBookmarks");
        list.appendChild(empty);
        return;
      }
      state.bookmarks.forEach((bookmark, index) => {
        const row = document.createElement("div");
        row.className = "bookmark";
        const open = document.createElement("button");
        open.textContent = bookmark.title || bookmarkTitle(bookmark.url);
        open.title = bookmark.url;
        open.onclick = () => navigateTo(bookmark.url);
        const remove = document.createElement("button");
        remove.className = "icon danger";
        remove.textContent = "X";
        remove.title = tr("remove");
        remove.onclick = () => {
          state.bookmarks.splice(index, 1);
          saveBookmarks();
          renderBookmarks();
        };
        row.appendChild(open);
        row.appendChild(remove);
        list.appendChild(row);
      });
    }
    function renderCommonSites() {
      const list = $("commonSiteList");
      list.innerHTML = "";
      COMMON_SITES.forEach(site => {
        const button = document.createElement("button");
        button.textContent = site.name[state.language] || site.name.en;
        button.title = site.url;
        button.onclick = () => navigateTo(site.url);
        list.appendChild(button);
      });
    }
    function addBookmark() {
      const url = state.lastRemoteUrl || $("urlInput").value.trim();
      if (!url) return;
      state.bookmarks = state.bookmarks.filter(item => item.url !== url);
      state.bookmarks.unshift({ url, title: bookmarkTitle(url) });
      saveBookmarks();
      renderBookmarks();
      setOverlay(tr("bookmarkAdded"));
    }
    function updateUrlInput(url, { force = false } = {}) {
      state.lastRemoteUrl = url || state.lastRemoteUrl;
      const input = $("urlInput");
      if (!url || (!force && (document.activeElement === input || state.urlDirty))) {
        return;
      }
      input.value = url;
      state.urlDirty = false;
    }
    async function navigateTo(url) {
      const target = String(url || "").trim();
      if (!target) return;
      try {
        await enqueueCommand("navigate", { url: target });
        updateUrlInput(target, { force: true });
        await refreshStatus({ forceUrl: true });
      } catch (error) {
        setOverlay(error.message || tr("navigateFailed"));
      }
    }
    function handleRemoteClick(result) {
      const click = result && result.js && typeof result.js === "object" ? { ...result.js, ...result } : result;
      if (!click || !click.editable) {
        state.remoteInputActive = false;
        closeInputModal();
        return;
      }
      state.remoteInputActive = true;
      openInputModal(typeof click.input_value === "string" ? click.input_value : "");
      setOverlay(tr("remoteInputReady"));
    }
    function openInputModal(value = "") {
      const input = $("textInput");
      input.value = value;
      $("inputModal").classList.add("show");
      $("inputModal").setAttribute("aria-hidden", "false");
      window.setTimeout(() => {
        input.focus();
        try {
          input.setSelectionRange(input.value.length, input.value.length);
        } catch (_) {}
      }, 0);
    }
    function closeInputModal({ clear = false } = {}) {
      $("inputModal").classList.remove("show");
      $("inputModal").setAttribute("aria-hidden", "true");
      if (clear) $("textInput").value = "";
    }
    async function sendInput() {
      const input = $("textInput");
      const text = input.value;
      try {
        if (state.remoteInputActive) {
          await enqueueCommand("set_input", { text });
        } else if (text) {
          await enqueueCommand("text", { text });
        }
        await enqueueCommand("key", { key: "Enter" });
        input.value = "";
        state.remoteInputActive = false;
        closeInputModal();
      } catch (error) {
        setOverlay(error.message || tr("statusFailed"));
      }
    }
    function updateMediaButtons(status = state.mediaStatus) {
      const paused = !status || status.media_paused !== false;
      const muted = !!(status && (status.media_muted || Number(status.media_volume || 0) <= 0));
      $("playButton").textContent = paused ? tr("play") : tr("pause");
      $("muteButton").textContent = muted ? tr("unmute") : tr("mute");
    }
    async function refreshMediaStatus() {
      if (!state.sessionId) return;
      try {
        const result = await enqueueCommand("media_status", {});
        state.mediaStatus = result;
        updateMediaButtons(result);
      } catch (_) {}
    }
    async function controlMedia(action, amount = 0) {
      const payload = { action };
      if (amount) payload.amount = amount;
      try {
        const result = await enqueueCommand("media_control", payload);
        state.mediaStatus = result;
        updateMediaButtons(result);
      } catch (error) {
        setOverlay(error.message || tr("statusFailed"));
      }
    }
    function updateStreamControls() {
      $("streamResolution").value = String(state.streamWidth);
      $("streamQuality").value = String(state.streamQuality);
      $("streamRate").value = String(state.streamIntervalMs);
      $("streamToggleButton").textContent = state.streamEnabled ? tr("stopStream") : tr("startStream");
    }
    function saveStreamSettings() {
      localStorage.setItem("remoteExplorerH5StreamEnabled", String(state.streamEnabled));
      localStorage.setItem("remoteExplorerH5StreamWidth", String(state.streamWidth));
      localStorage.setItem("remoteExplorerH5StreamQuality", String(state.streamQuality));
      localStorage.setItem("remoteExplorerH5StreamInterval", String(state.streamIntervalMs));
    }
    function applyStreamSettings() {
      state.streamWidth = Number($("streamResolution").value) || 960;
      state.streamQuality = Number($("streamQuality").value) || 55;
      state.streamIntervalMs = Number($("streamRate").value) || 250;
      saveStreamSettings();
      updateStreamControls();
      if (state.streamEnabled && state.sessionId) {
        window.clearTimeout(state.snapshotTimer);
        refreshSnapshot();
      }
    }
    function setStreamEnabled(enabled) {
      state.streamEnabled = enabled;
      saveStreamSettings();
      updateStreamControls();
      window.clearTimeout(state.snapshotTimer);
      if (enabled) {
        setOverlay(tr("streamOn"));
        if (state.sessionId) refreshSnapshot();
      } else {
        setOverlay(tr("streamOff"));
      }
    }
    function scheduleSnapshot(delay) {
      window.clearTimeout(state.snapshotTimer);
      if (!state.streamEnabled || !state.sessionId || document.hidden) return;
      state.snapshotTimer = window.setTimeout(refreshSnapshot, delay);
    }
    function makeClientId() {
      if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return window.crypto.randomUUID();
      }
      const random = new Uint8Array(16);
      if (window.crypto && typeof window.crypto.getRandomValues === "function") {
        window.crypto.getRandomValues(random);
      } else {
        for (let i = 0; i < random.length; i += 1) random[i] = Math.floor(Math.random() * 256);
      }
      return `h5-${Array.from(random).map(b => b.toString(16).padStart(2, "0")).join("")}`;
    }
    async function postJson(url, body) {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      const data = await response.json();
      if (!response.ok || data.ok === false) {
        const error = data.error || {};
        const failure = new Error(error.message || response.statusText);
        failure.code = error.code || "request_failed";
        throw failure;
      }
      return data;
    }
    async function postMessage(message) {
      return postJson("/api/message", message);
    }
    async function login(password = "") {
      return postJson("/api/web-login", {
        client: { id: state.clientId, name: "H5 Client" },
        password
      });
    }
    async function loginOrPrompt() {
      if (state.authRetrying) return;
      state.authRetrying = true;
      try {
        const result = await login("");
        completeAuth(result.session);
      } catch (error) {
        if (error.code === "password_required") {
          $("authError").textContent = "";
          $("authPanel").classList.add("show");
          setStatus(tr("passwordRequired"));
          $("passwordInput").focus();
          return;
        }
        throw error;
      } finally {
        state.authRetrying = false;
      }
    }
    async function unlock() {
      $("authError").textContent = "";
      const password = $("passwordInput").value;
      try {
        const result = await login(password);
        completeAuth(result.session);
      } catch (error) {
        $("authError").textContent = error.message || String(error);
      }
    }
    function completeAuth(session) {
      state.sessionId = session.id;
      state.sessionAuth = session.auth;
      state.authRetrying = false;
      $("authPanel").classList.remove("show");
      $("passwordInput").value = "";
      setStatus(tr("connected"));
      setOverlay(tr("liveSnapshot"));
      refreshStatus();
      updateStreamControls();
      if (state.streamEnabled) {
        refreshSnapshot();
      } else {
        setOverlay(tr("streamOff"));
      }
      refreshMediaStatus();
    }
    function enqueueCommand(command, payload = {}) {
      const job = state.commandChain.then(() => callCommand(command, payload));
      state.commandChain = job.catch(() => {});
      return job;
    }
    async function callCommand(command, payload = {}) {
      if (!state.sessionId) throw new Error("Not authenticated");
      const message = {
        v: 1,
        type: "command",
        request_id: requestId("cmd"),
        command,
        payload,
        auth: { session_id: state.sessionId }
      };
      const response = await postMessage(message);
      if (response.type === "error") {
        const failure = protocolError(response);
        if (failure.code === "invalid_session") handleSessionExpired();
        throw failure;
      }
      return response.result || {};
    }
    function protocolError(response) {
      const error = response.error || {};
      const failure = new Error(error.message || tr("statusFailed"));
      failure.code = error.code || "command_failed";
      return failure;
    }
    function handleSessionExpired() {
      if (!state.sessionId && $("authPanel").classList.contains("show")) return;
      state.sessionId = "";
      state.sessionAuth = "";
      state.snapshotInFlight = false;
      window.clearTimeout(state.snapshotTimer);
      setOverlay(tr("sessionExpired"));
      loginOrPrompt().catch(error => setStatus(error.message || tr("couldNotConnect")));
    }
    async function refreshSnapshot() {
      if (!state.sessionId) return;
      if (!state.streamEnabled) {
        setOverlay(tr("streamOff"));
        return;
      }
      if (document.hidden) return;
      if (state.snapshotInFlight) {
        scheduleSnapshot(state.streamIntervalMs);
        return;
      }
      state.snapshotInFlight = true;
      const abortController = new AbortController();
      state.snapshotAbortController = abortController;
      try {
        const response = await fetch("/api/snapshot.jpg", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          signal: abortController.signal,
          body: JSON.stringify({
            session_id: state.sessionId,
            width: state.streamWidth,
            quality: state.streamQuality
          })
        });
        if (!response.ok) {
          let failure = new Error(response.statusText);
          try {
            const data = await response.json();
            const error = data.error || {};
            failure = new Error(error.message || response.statusText);
            failure.code = error.code || "snapshot_failed";
          } catch (_) {}
          if (failure.code === "invalid_session") handleSessionExpired();
          throw failure;
        }
        const blob = await response.blob();
        const nextUrl = URL.createObjectURL(blob);
        const frame = $("frame");
        const oldUrl = state.snapshotUrl;
        frame.onload = () => {
          if (oldUrl) URL.revokeObjectURL(oldUrl);
          setOverlay(tr("liveSnapshot"));
        };
        state.snapshotUrl = nextUrl;
        frame.src = nextUrl;
      } catch (error) {
        if (error.name !== "AbortError" && error.code !== "invalid_session") {
          setOverlay(error.message || tr("snapshotFailed"));
        }
      } finally {
        if (state.snapshotAbortController === abortController) {
          state.snapshotAbortController = null;
        }
        state.snapshotInFlight = false;
        scheduleSnapshot(state.streamIntervalMs);
      }
    }
    async function refreshStatus(options = {}) {
      if (!state.sessionId) return;
      try {
        const result = await enqueueCommand("status", {});
        if (result.url) updateUrlInput(result.url, { force: !!options.forceUrl });
        setStatus(result.title || result.url || tr("connected"));
      } catch (error) {
        setStatus(error.message || tr("statusFailed"));
      }
    }
    function framePoint(clientX, clientY) {
      const frame = $("frame");
      const rect = frame.getBoundingClientRect();
      const naturalWidth = frame.naturalWidth || rect.width || 1;
      const naturalHeight = frame.naturalHeight || rect.height || 1;
      const scale = Math.min(rect.width / naturalWidth, rect.height / naturalHeight);
      const renderedWidth = naturalWidth * scale;
      const renderedHeight = naturalHeight * scale;
      const offsetX = (rect.width - renderedWidth) / 2;
      const offsetY = (rect.height - renderedHeight) / 2;
      const localX = Math.max(0, Math.min(renderedWidth, clientX - rect.left - offsetX));
      const localY = Math.max(0, Math.min(renderedHeight, clientY - rect.top - offsetY));
      return {
        x: Math.max(0, Math.min(naturalWidth - 1, localX * naturalWidth / Math.max(1, renderedWidth))),
        y: Math.max(0, Math.min(naturalHeight - 1, localY * naturalHeight / Math.max(1, renderedHeight))),
        source_width: naturalWidth,
        source_height: naturalHeight
      };
    }
    async function clickFrame(clientX, clientY) {
      try {
        const result = await enqueueCommand("click", framePoint(clientX, clientY));
        handleRemoteClick(result);
        if (!result || !result.editable) {
          setOverlay(tr("clickSent"));
        }
      } catch (error) {
        setOverlay(error.message || tr("clickFailed"));
      }
    }
    function bindControls() {
      $("unlockButton").onclick = unlock;
      $("langButton").onclick = toggleLanguage;
      $("passwordInput").addEventListener("keydown", event => { if (event.key === "Enter") unlock(); });
      $("goButton").onclick = () => navigateTo($("urlInput").value);
      $("urlInput").addEventListener("input", () => { state.urlDirty = true; });
      $("urlInput").addEventListener("blur", () => {
        if ($("urlInput").value.trim() === state.lastRemoteUrl) state.urlDirty = false;
      });
      $("urlInput").addEventListener("keydown", event => { if (event.key === "Enter") $("goButton").click(); });
      $("addBookmarkButton").onclick = addBookmark;
      $("streamToggleButton").onclick = () => setStreamEnabled(!state.streamEnabled);
      $("streamResolution").onchange = applyStreamSettings;
      $("streamQuality").onchange = applyStreamSettings;
      $("streamRate").onchange = applyStreamSettings;
      $("backButton").onclick = () => enqueueCommand("back", {}).then(() => refreshStatus({ forceUrl: true }));
      $("forwardButton").onclick = () => enqueueCommand("forward", {}).then(() => refreshStatus({ forceUrl: true }));
      $("reloadButton").onclick = () => enqueueCommand("reload", {}).then(() => refreshStatus({ forceUrl: true }));
      $("sendTextButton").onclick = sendInput;
      $("inputCancelButton").onclick = () => {
        state.remoteInputActive = false;
        closeInputModal({ clear: true });
      };
      $("inputModal").addEventListener("click", event => {
        if (event.target === $("inputModal")) {
          state.remoteInputActive = false;
          closeInputModal({ clear: true });
        }
      });
      $("textInput").addEventListener("keydown", event => {
        if (event.key === "Enter" && !event.isComposing) {
          event.preventDefault();
          sendInput();
        }
        if (event.key === "Escape") {
          event.preventDefault();
          state.remoteInputActive = false;
          closeInputModal({ clear: true });
        }
      });
      $("backspaceButton").onclick = () => enqueueCommand("key", { key: "Backspace" });
      $("escapeButton").onclick = () => enqueueCommand("key", { key: "Escape" });
      $("scrollUpButton").onclick = () => enqueueCommand("scroll", { dx: 0, dy: -520 });
      $("scrollDownButton").onclick = () => enqueueCommand("scroll", { dx: 0, dy: 520 });
      $("playButton").onclick = () => controlMedia("play_pause");
      $("muteButton").onclick = () => controlMedia("mute");
      $("fullButton").onclick = () => controlMedia("fullscreen");
      $("exitFullButton").onclick = () => controlMedia("exit_fullscreen");
      $("seekBackButton").onclick = () => controlMedia("seek_back", 10);
      $("seekForwardButton").onclick = () => controlMedia("seek_forward", 10);

      const viewport = $("viewport");
      viewport.addEventListener("click", event => clickFrame(event.clientX, event.clientY));
      viewport.addEventListener("wheel", event => {
        event.preventDefault();
        const now = performance.now();
        if (now - state.lastWheelAt < 180) return;
        state.lastWheelAt = now;
        enqueueCommand("scroll", { dx: event.deltaX, dy: event.deltaY });
      }, { passive: false });
      viewport.addEventListener("touchstart", event => {
        const touch = event.changedTouches[0];
        state.touchStart = { x: touch.clientX, y: touch.clientY, at: performance.now() };
      }, { passive: true });
      viewport.addEventListener("touchend", event => {
        const touch = event.changedTouches[0];
        const start = state.touchStart;
        if (!start) return;
        const dx = touch.clientX - start.x;
        const dy = touch.clientY - start.y;
        if (Math.hypot(dx, dy) < 10 && performance.now() - start.at < 500) {
          clickFrame(touch.clientX, touch.clientY);
        } else {
          enqueueCommand("scroll", { dx: -dx * 2, dy: -dy * 2 });
        }
      }, { passive: true });
      document.addEventListener("visibilitychange", () => {
        window.clearTimeout(state.snapshotTimer);
        if (document.hidden) {
          if (state.snapshotAbortController) state.snapshotAbortController.abort();
          return;
        }
        if (state.streamEnabled && state.sessionId && !state.snapshotInFlight) refreshSnapshot();
        refreshStatus();
        refreshMediaStatus();
      });
      window.addEventListener("pagehide", () => {
        window.clearTimeout(state.snapshotTimer);
        if (state.snapshotAbortController) state.snapshotAbortController.abort();
      });
    }
    async function boot() {
      bindControls();
      applyLanguage();
      try {
        const info = await fetch("/api/info").then(response => response.json());
        state.info = info;
        setStatus(info.server.name || "RCViewer");
        await loginOrPrompt();
      } catch (error) {
        setStatus(tr("offline"));
        setOverlay(error.message || tr("couldNotConnect"));
      }
      setInterval(() => { if (!document.hidden) refreshStatus(); }, 2500);
      setInterval(() => { if (!document.hidden) refreshMediaStatus(); }, 3000);
    }
    boot();
  </script>
</body>
</html>
"""
