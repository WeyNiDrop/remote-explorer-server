from __future__ import annotations

import argparse
import atexit
import logging
import logging.handlers
import os
import queue
import socket
import sys
from pathlib import Path

from .config import (
    DEFAULT_CONTROL_PORT,
    DEFAULT_DISCOVERY_PORT,
    ServerConfig,
    config_with_local_domain,
    default_data_dir,
    default_server_name,
    load_server_settings,
    load_or_create_server_id,
    save_server_local_domain,
)


def configure_logging(log_dir: Path | None = None) -> tuple[logging.handlers.QueueListener, Path | None]:
    # 使用队列日志避免串流线程被控制台 I/O 卡住。 / Queue logging keeps stream threads off console I/O.
    log_queue: queue.Queue[logging.LogRecord] = queue.Queue()
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handlers: list[logging.Handler] = [console]
    log_path: Path | None = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "server.log"
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handlers.append(file_handler)
    listener = logging.handlers.QueueListener(log_queue, *handlers)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(logging.handlers.QueueHandler(log_queue))
    listener.start()
    atexit.register(listener.stop)
    return listener, log_path


def build_parser() -> argparse.ArgumentParser:
    saved = load_server_settings()
    saved_web_port = saved.get("web_port")
    default_web_port = int(saved_web_port) if saved_web_port else None
    parser = argparse.ArgumentParser(description="Remote Explorer desktop server")
    parser.add_argument("--name", default=str(saved.get("name") or default_server_name()), help="Server name shown to clients")
    parser.add_argument(
        "--local-domain",
        default=str(saved.get("local_domain") or ""),
        help="Stable .local hostname for the H5 client. Generated once and saved unless changed.",
    )
    parser.add_argument("--discovery-port", type=int, default=int(saved.get("discovery_port") or DEFAULT_DISCOVERY_PORT))
    parser.add_argument("--control-port", type=int, default=int(saved.get("control_port") or DEFAULT_CONTROL_PORT))
    parser.add_argument(
        "--web-port",
        type=int,
        default=default_web_port,
        help="TCP port for the supplemental H5 client. Defaults to the control port.",
    )
    parser.add_argument("--password", default=os.environ.get("REMOTE_EXPLORER_PASSWORD", str(saved.get("password") or "")))
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--start-url", default=str(saved.get("start_url") or "about:blank"))
    parser.add_argument(
        "--browser-engine",
        choices=["auto", "chromium"],
        default=os.environ.get("REMOTE_EXPLORER_BROWSER_ENGINE", str(saved.get("browser_engine") or "auto")),
        help="Browser backend. auto uses system Chromium, Chrome, or Edge.",
    )
    parser.add_argument(
        "--browser-executable",
        default=os.environ.get("REMOTE_EXPLORER_BROWSER_EXECUTABLE", str(saved.get("browser_executable") or "")) or None,
        help="Path to Chrome, Edge, or Chromium for --browser-engine chromium/auto.",
    )
    parser.add_argument(
        "--allow-evaluate-js",
        action="store_true",
        help="Enable the dangerous evaluate_js command for development only",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_args = sys.argv[1:] if argv is None else argv
    local_domain_arg_provided = any(arg == "--local-domain" or arg.startswith("--local-domain=") for arg in raw_args)
    args = build_parser().parse_args(argv)
    config = ServerConfig(
        name=args.name,
        discovery_port=args.discovery_port,
        control_port=args.control_port,
        password=args.password,
        data_dir=args.data_dir,
        start_url=args.start_url,
        allow_evaluate_js=args.allow_evaluate_js,
        browser_engine=args.browser_engine,
        browser_executable=args.browser_executable,
        web_port=args.web_port,
        local_domain=args.local_domain or None,
    )
    before_local_domain = config.local_domain
    config = config_with_local_domain(config, socket.gethostname(), default_server_name())
    if config.local_domain and (not before_local_domain or local_domain_arg_provided or config.local_domain != before_local_domain):
        save_server_local_domain(config.data_dir, config.local_domain)
    log_listener, log_path = configure_logging(config.data_dir / "logs")

    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QMainWindow

        from .chromium_backend import ChromiumBrowserWindow, ChromiumUnavailable, find_chromium_executable
        from .control import ControlService
        from .discovery import DiscoveryService
        from .server_ui import ChromeInstallDialog, ServerControlPanel
        from .security import AuthManager
        from .web_client import WebClientService
    except ImportError as exc:
        print(
            "PySide6 is required to run the server. Install with:\n"
            '  pip install -e ".[server]"\n'
            f"Original error: {exc}",
            file=sys.stderr,
        )
        return 2

    app = QApplication(sys.argv[:1])
    server_id = load_or_create_server_id(config)
    chrome_executable = find_chromium_executable(config.browser_executable)
    browser_error = ""
    try:
        window = create_browser_window(config, ChromiumBrowserWindow, ChromiumUnavailable)
    except ChromiumUnavailable as exc:
        browser_error = str(exc)
        logging.getLogger("remote_explorer.main").exception("Chromium browser engine failed to start")
        window = create_browser_unavailable_window(config.name, browser_error, QMainWindow)
    auth_manager = AuthManager(config.password)
    capabilities = getattr(window.controller, "capabilities", None)
    discovery_on_control_port = config.discovery_port == config.control_port
    discovery = None if discovery_on_control_port else DiscoveryService(
        config,
        server_id,
        window,
        capabilities=capabilities,
    )
    control = ControlService(
        config,
        auth_manager,
        window.controller.handle,
        window,
        server_id=server_id,
        capabilities=capabilities,
        handle_discovery=discovery_on_control_port,
    )
    web_client = None
    try:
        web_client = WebClientService(
            config,
            auth_manager,
            window.controller.handle,
            window,
            server_id=server_id,
            capabilities=capabilities,
            parent=window,
        )
        web_client.start()
    except Exception as exc:
        logging.getLogger("remote_explorer.main").exception("H5 client server failed to start: %s", exc)

    window.setWindowTitle("RCViewer Server")
    window.setWindowFlags(window.windowFlags() | Qt.WindowType.FramelessWindowHint)
    window.setMinimumSize(1120, 800)
    window.statusBar().showMessage(
        f"Discovery UDP {config.discovery_port} | Control UDP {config.control_port} | "
        f"H5 TCP {(web_client.bound_port if web_client else config.effective_web_port)} | "
        f"Auth {'password' if config.password else 'none'}"
    )
    window.statusBar().hide()

    # Keep service objects alive for the lifetime of the Qt app.
    window.discovery_service = discovery
    window.control_service = control
    window.web_client_service = web_client
    window.log_listener = log_listener
    web_qr_png = None
    if web_client is not None:
        try:
            web_qr_png = web_client.qr_png(scale=5)
        except Exception as exc:
            logging.getLogger("remote_explorer.main").warning("Could not render H5 QR code: %s", exc)
    window.server_control_panel = ServerControlPanel(
        config,
        log_path,
        chrome_executable,
        window,
        web_url=web_client.client_url if web_client is not None else None,
        web_urls=web_client.urls if web_client is not None else None,
        web_qr_png=web_qr_png,
    )
    window.setCentralWidget(window.server_control_panel)
    if browser_error:
        window.server_control_panel.set_startup_warning(browser_error)
    control.client_changed.connect(window.server_control_panel.update_client)
    if web_client is not None:
        web_client.client_changed.connect(window.server_control_panel.update_client)
        app.aboutToQuit.connect(web_client.stop)
    shutdown_browser = getattr(window, "shutdown", None)
    if callable(shutdown_browser):
        app.aboutToQuit.connect(shutdown_browser)
    window.show()

    if not chrome_executable:
        window.chrome_install_dialog = ChromeInstallDialog(window)
        window.chrome_install_dialog.show()

    return app.exec()


def create_browser_window(
    config: ServerConfig,
    chromium_window_type: type,
    chromium_unavailable_type: type[Exception],
):
    if config.browser_engine in {"auto", "chromium"}:
        try:
            return chromium_window_type(config)
        except chromium_unavailable_type as exc:
            logging.getLogger("remote_explorer.main").error("Chromium browser engine unavailable: %s", exc)
            raise
    raise chromium_unavailable_type(f"Unsupported browser engine: {config.browser_engine}")


class BrowserUnavailableController:
    capabilities = ["status"]

    def __init__(self, message: str) -> None:
        self.message = message

    def handle(self, command: str, payload: dict[str, object], respond: object) -> None:
        del payload
        result = {
            "ok": False,
            "error": {
                "code": "browser_unavailable",
                "message": self.message,
            },
        }
        if command == "status":
            result = {
                "ok": True,
                "result": {
                    "engine": "chromium",
                    "available": False,
                    "error": self.message,
                },
            }
        respond(result)  # type: ignore[operator]


def create_browser_unavailable_window(name: str, message: str, window_type: type):
    window = window_type()
    window.setWindowTitle(f"{name} - Browser unavailable")
    window.controller = BrowserUnavailableController(message)
    return window


if __name__ == "__main__":
    raise SystemExit(main())
