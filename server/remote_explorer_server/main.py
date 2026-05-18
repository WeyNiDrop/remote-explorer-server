from __future__ import annotations

import argparse
import atexit
import logging
import logging.handlers
import os
import queue
import sys
from pathlib import Path

from .config import (
    DEFAULT_CONTROL_PORT,
    DEFAULT_DISCOVERY_PORT,
    ServerConfig,
    default_data_dir,
    default_server_name,
    load_or_create_server_id,
)


def configure_logging() -> logging.handlers.QueueListener:
    # 使用队列日志避免串流线程被控制台 I/O 卡住。 / Queue logging keeps stream threads off console I/O.
    log_queue: queue.Queue[logging.LogRecord] = queue.Queue()
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    listener = logging.handlers.QueueListener(log_queue, console)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(logging.handlers.QueueHandler(log_queue))
    listener.start()
    atexit.register(listener.stop)
    return listener


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Remote Explorer desktop server")
    parser.add_argument("--name", default=default_server_name(), help="Server name shown to clients")
    parser.add_argument("--discovery-port", type=int, default=DEFAULT_DISCOVERY_PORT)
    parser.add_argument("--control-port", type=int, default=DEFAULT_CONTROL_PORT)
    parser.add_argument("--password", default=os.environ.get("REMOTE_EXPLORER_PASSWORD"))
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--start-url", default="about:blank")
    parser.add_argument(
        "--browser-engine",
        choices=["auto", "chromium", "qt"],
        default=os.environ.get("REMOTE_EXPLORER_BROWSER_ENGINE", "auto"),
        help="Browser backend. auto prefers system Chromium/Chrome/Edge and falls back to Qt WebEngine.",
    )
    parser.add_argument(
        "--browser-executable",
        default=os.environ.get("REMOTE_EXPLORER_BROWSER_EXECUTABLE"),
        help="Path to Chrome, Edge, or Chromium for --browser-engine chromium/auto.",
    )
    parser.add_argument(
        "--allow-evaluate-js",
        action="store_true",
        help="Enable the dangerous evaluate_js command for development only",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_listener = configure_logging()
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
    )

    try:
        from PySide6.QtWidgets import QApplication

        from .browser import BrowserWindow
        from .chromium_backend import ChromiumBrowserWindow, ChromiumUnavailable
        from .control import ControlService
        from .discovery import DiscoveryService
        from .security import AuthManager
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
    window = create_browser_window(config, BrowserWindow, ChromiumBrowserWindow, ChromiumUnavailable)
    auth_manager = AuthManager(config.password)
    discovery = DiscoveryService(
        config,
        server_id,
        window,
        capabilities=getattr(window.controller, "capabilities", None),
    )
    control = ControlService(config, auth_manager, window.controller.handle, window)

    window.statusBar().showMessage(
        f"Discovery UDP {config.discovery_port} | Control UDP {config.control_port} | "
        f"Auth {'password' if config.password else 'none'}"
    )
    window.show()

    # Keep service objects alive for the lifetime of the Qt app.
    window.discovery_service = discovery
    window.control_service = control
    window.log_listener = log_listener

    return app.exec()


def create_browser_window(
    config: ServerConfig,
    qt_window_type: type,
    chromium_window_type: type,
    chromium_unavailable_type: type[Exception],
):
    if config.browser_engine in {"auto", "chromium"}:
        try:
            return chromium_window_type(config)
        except chromium_unavailable_type as exc:
            if config.browser_engine == "chromium":
                raise
            logging.getLogger("remote_explorer.main").warning(
                "Chromium browser engine unavailable, falling back to Qt WebEngine: %s",
                exc,
            )
    return qt_window_type(config)


if __name__ == "__main__":
    raise SystemExit(main())
