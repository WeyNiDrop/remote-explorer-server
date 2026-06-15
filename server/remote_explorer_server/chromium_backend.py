from __future__ import annotations

import base64
import json
import logging
import math
import os
import signal
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QSize, Qt, QTimer
from PySide6.QtGui import QImage
from PySide6.QtNetwork import QHostAddress
from PySide6.QtWidgets import QLabel, QLineEdit, QMainWindow, QToolBar, QVBoxLayout, QWidget

from .browser_scripts import CHROME_COMPAT_ACCEPT_LANGUAGE, CHROME_COMPAT_USER_AGENT, MEDIA_CONTROL_HELPERS, REMOTE_INPUT_HELPERS
from .cdp import CdpConnection, CdpError, http_json
from .config import ServerConfig
from .protocol import normalize_url
from .streaming_protocol import (
    DEFAULT_JPEG_QUALITY,
    DEFAULT_MACOS_UDP_STREAM_FPS,
    MAX_STREAM_CHUNKS_PER_FRAME,
    DEFAULT_STREAM_FPS,
    MAX_STREAM_FPS,
    MIN_JPEG_QUALITY,
    MIN_STREAM_FPS,
    STREAM_CHUNK_BYTES,
    STREAM_CHUNK_PACE_BATCH,
    STREAM_CHUNK_PACE_SECONDS,
    STREAM_HEADER_FORMAT,
    STREAM_MAGIC,
    clamp_int,
    clean_host,
    resolve_size,
)

LOGGER = logging.getLogger("remote_explorer.chromium")
IDLE_CAPTURE_INTERVAL_MS = 1000
STREAM_CLIENT_ACTIVITY_TIMEOUT_SECONDS = 25.0
STREAM_CAPTURE_SUSPEND_TIMEOUT_SECONDS = 5 * 60.0
PROFILE_LOCK_NAMES = ("SingletonCookie", "SingletonLock", "SingletonSocket")


class ChromiumUnavailable(RuntimeError):
    pass


class ChromiumBrowserWindow(QMainWindow):
    def __init__(self, config: ServerConfig) -> None:
        super().__init__()
        self.config = config
        self.setWindowTitle(f"{config.name} - Chromium")
        self.resize(720, 220)

        self.browser = ChromiumBrowserService(config)
        self.stream_service = ChromiumStreamService(self.browser, self)
        self.controller = ChromiumBrowserController(self.browser, self.stream_service, config.allow_evaluate_js)
        self._shutdown_started = False

        self.address_bar = QLineEdit(self)
        self.address_bar.setText(normalize_url(config.start_url))
        self.address_bar.returnPressed.connect(self._navigate_from_bar)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Chromium Browser", self)
        toolbar.setMovable(False)
        toolbar.addAction("Back", lambda: self.browser.back())
        toolbar.addAction("Forward", lambda: self.browser.forward())
        toolbar.addAction("Reload", lambda: self.browser.reload())
        toolbar.addWidget(self.address_bar)
        self.addToolBar(toolbar)

    def _build_body(self) -> None:
        widget = QWidget(self)
        layout = QVBoxLayout(widget)
        label = QLabel(
            "Remote Explorer is using an external Chromium-family browser engine.\n"
            "Use the Unity client preview to view and control the page.",
            widget,
        )
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        self.setCentralWidget(widget)

    def _navigate_from_bar(self) -> None:
        url = normalize_url(self.address_bar.text())
        self.browser.navigate(url)
        self.address_bar.setText(url)

    def shutdown(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self.stream_service.close()
        self.browser.close()

    def closeEvent(self, event: Any) -> None:
        self.shutdown()
        super().closeEvent(event)


class ChromiumBrowserService:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.executable = find_chromium_executable(config.browser_executable)
        if not self.executable:
            raise ChromiumUnavailable(
                "Could not find Chrome, Edge, or Chromium. Pass --browser-executable PATH."
            )

        self.profile_dir = config.data_dir / "chromium-profile"
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        remaining_profile_processes = _cleanup_stale_chromium_profile(self.profile_dir)
        if remaining_profile_processes:
            raise ChromiumUnavailable(
                "Could not stop the previous Chromium process using the Remote Explorer profile: "
                + ", ".join(str(pid) for pid in remaining_profile_processes)
            )
        self.port = _free_port()
        self.browser_log_file = None
        self.viewport_width = 1280
        self.viewport_height = 720
        self._stream_viewport: tuple[int, int] | None = None
        self._fullscreen_topmost = False
        self._closed = False
        self._reconnect_lock = threading.Lock()
        self.process_group_id: int | None = None
        try:
            self.process = self._launch_browser(config.start_url)
            if sys.platform != "win32":
                self.process_group_id = self.process.pid
            self.connection = self._connect()
            self._configure_page()
            self.navigate(normalize_url(config.start_url))
        except Exception as exc:
            if hasattr(self, "process"):
                self.close()
            elif self.browser_log_file is not None:
                try:
                    self.browser_log_file.close()
                except Exception:
                    pass
                self.browser_log_file = None
            if isinstance(exc, ChromiumUnavailable):
                raise
            raise ChromiumUnavailable(f"Chromium startup failed: {exc}") from exc

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self._set_fullscreen_topmost(False)
        connection = getattr(self, "connection", None)
        process = getattr(self, "process", None)
        if connection is not None and process is not None and process.poll() is None:
            try:
                connection.call("Browser.close", timeout=2.0)
            except Exception as exc:
                LOGGER.debug("Could not close Chromium through CDP: %s", exc)
            else:
                try:
                    process.wait(timeout=4.0)
                except subprocess.TimeoutExpired:
                    LOGGER.debug("Chromium did not exit after CDP close; terminating")
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if process is not None:
            _terminate_owned_process(process, getattr(self, "process_group_id", None))
        profile_dir = getattr(self, "profile_dir", None)
        if isinstance(profile_dir, Path):
            remaining = _cleanup_stale_chromium_profile(profile_dir)
            if remaining:
                LOGGER.warning("Chromium processes still use the Remote Explorer profile after shutdown: %s", remaining)
        if self.browser_log_file is not None:
            try:
                self.browser_log_file.close()
            except Exception:
                pass
            self.browser_log_file = None

    def navigate(self, url: str) -> dict[str, Any]:
        target = normalize_url(url)
        self.connection.call("Page.navigate", {"url": target})
        return {"accepted": True, "url": target}

    def back(self) -> dict[str, Any]:
        self.evaluate("history.back(); true")
        return {"accepted": True}

    def forward(self) -> dict[str, Any]:
        self.evaluate("history.forward(); true")
        return {"accepted": True}

    def reload(self) -> dict[str, Any]:
        self.connection.call("Page.reload", {"ignoreCache": False})
        return {"accepted": True}

    def status(self) -> dict[str, Any]:
        return self.evaluate(
            """
(() => ({
  url: location.href,
  title: document.title,
  can_go_back: history.length > 1,
  can_go_forward: false
}))()
"""
        )

    def set_viewport(self, width: int, height: int) -> None:
        self.set_stream_viewport(width, height)

    def set_stream_viewport(self, width: int, height: int) -> None:
        viewport_width = max(1, int(width))
        viewport_height = max(1, int(height))
        self._stream_viewport = (viewport_width, viewport_height)
        if sys.platform != "darwin":
            self.ensure_minimum_viewport(viewport_width, viewport_height)
            LOGGER.info("Chromium stream target set size=%sx%s using visible window capture", viewport_width, viewport_height)
            return

        try:
            current_width, current_height = self._refresh_viewport_size()
        except Exception as exc:
            LOGGER.debug("Could not refresh Chromium viewport for streaming: %s", exc)
            current_width, current_height = self.viewport_width, self.viewport_height
        LOGGER.info(
            "Chromium stream target set size=%sx%s viewport=%sx%s",
            viewport_width,
            viewport_height,
            current_width,
            current_height,
        )

    def clear_stream_viewport(self) -> None:
        if self._stream_viewport is None:
            return
        self._stream_viewport = None
        if sys.platform != "darwin":
            return

        try:
            self._refresh_viewport_size()
        except Exception as exc:
            LOGGER.debug("Could not refresh Chromium viewport after stream stop: %s", exc)

    def ensure_minimum_viewport(self, width: int, height: int) -> None:
        minimum_width = max(1, int(width))
        minimum_height = max(1, int(height))
        current_width, current_height = self._refresh_viewport_size()
        if current_width >= minimum_width and current_height >= minimum_height:
            return

        try:
            window = self.connection.call("Browser.getWindowForTarget")
            window_id = window.get("windowId")
            bounds = window.get("bounds") if isinstance(window.get("bounds"), dict) else {}
            if not window_id or bounds.get("windowState") == "fullscreen":
                return

            metrics = self._window_metrics()
            chrome_width = max(0, int(metrics.get("outerWidth", current_width)) - current_width)
            chrome_height = max(0, int(metrics.get("outerHeight", current_height)) - current_height)
            target_width = minimum_width + chrome_width
            target_height = minimum_height + chrome_height
            new_bounds: dict[str, Any] = {
                "windowState": "normal",
                "width": target_width,
                "height": target_height,
            }
            if isinstance(bounds.get("left"), int):
                new_bounds["left"] = bounds["left"]
            if isinstance(bounds.get("top"), int):
                new_bounds["top"] = bounds["top"]
            self.connection.call("Browser.setWindowBounds", {"windowId": window_id, "bounds": new_bounds})
            self._refresh_viewport_size()
        except Exception as exc:
            LOGGER.debug("Could not resize Chromium window: %s", exc)

    def note_capture_size(self, width: int, height: int) -> None:
        if self._stream_viewport is not None and sys.platform == "darwin":
            return
        self.viewport_width = max(1, int(width))
        self.viewport_height = max(1, int(height))

    def capture_jpeg(self, quality: int) -> bytes:
        connection = self.connection
        try:
            result = self._capture_jpeg_result(quality)
        except CdpError as exc:
            if getattr(self, "_closed", False) or not _is_recoverable_capture_error(exc):
                raise
            LOGGER.warning("Chromium capture connection failed; reconnecting CDP page: %s", exc)
            with self._reconnect_lock:
                if getattr(self, "_closed", False):
                    raise
                if self.connection is connection:
                    self._reconnect_page()
            result = self._capture_jpeg_result(quality)
        encoded = result.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise CdpError("captureScreenshot returned no data")
        return base64.b64decode(encoded)

    def _capture_jpeg_result(self, quality: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "format": "jpeg",
            "quality": clamp_int(quality, MIN_JPEG_QUALITY, 90, DEFAULT_JPEG_QUALITY),
            "fromSurface": sys.platform != "darwin",
            "captureBeyondViewport": False,
        }
        if self._stream_viewport is not None and sys.platform == "darwin":
            payload["clip"] = {
                "x": 0,
                "y": 0,
                "width": max(1, int(self.viewport_width)),
                "height": max(1, int(self.viewport_height)),
                "scale": 1,
            }
        return self.connection.call(
            "Page.captureScreenshot",
            payload,
            timeout=10.0,
        )

    def _reconnect_page(self) -> None:
        if getattr(self, "_closed", False):
            raise CdpError("Chromium browser is closing")
        stream_viewport = self._stream_viewport
        try:
            self.connection.close()
        except Exception:
            pass
        self.connection = self._connect()
        self._configure_page()
        if stream_viewport is not None:
            self.set_stream_viewport(*stream_viewport)

    def evaluate(self, script: str) -> Any:
        result = self.connection.call(
            "Runtime.evaluate",
            {
                "expression": script,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            },
        )
        if result.get("exceptionDetails"):
            raise CdpError(f"JavaScript failed: {result['exceptionDetails']}")
        remote = result.get("result") or {}
        return remote.get("value")

    def request_media_fullscreen(self) -> Any:
        return self.evaluate(
            "(() => {\n"
            + MEDIA_CONTROL_HELPERS
            + """
  return new Promise(resolve => {
    const media = remoteExplorerFindMedia();
    if (!media) {
      resolve({
        media_found: false,
        controlled: false,
        media_action: "fullscreen",
        media_reason: "no_media"
      });
      return;
    }

    const fullscreenElement = () =>
      document.fullscreenElement ||
      document.webkitFullscreenElement ||
      document.mozFullScreenElement ||
      document.msFullscreenElement;
    const status = (controlled, reason) => ({
      ...remoteExplorerMediaStatus(media),
      controlled,
      media_action: "fullscreen",
      media_reason: reason
    });

    if (fullscreenElement()) {
      resolve(status(true, "already_fullscreen"));
      return;
    }

    const target = remoteExplorerPlayerRoot(media) || media;
    const requestTarget =
      target.requestFullscreen ||
      target.webkitRequestFullscreen ||
      target.mozRequestFullScreen ||
      target.msRequestFullscreen
        ? target
        : media;
    const request =
      requestTarget.requestFullscreen ||
      requestTarget.webkitRequestFullscreen ||
      requestTarget.mozRequestFullScreen ||
      requestTarget.msRequestFullscreen;
    if (!request) {
      resolve(status(false, "keyboard_shortcut_required"));
      return;
    }

    let completed = false;
    let timer = 0;
    const cleanup = () => {
      window.clearTimeout(timer);
      document.removeEventListener("fullscreenchange", onChange, true);
      document.removeEventListener("webkitfullscreenchange", onChange, true);
      document.removeEventListener("mozfullscreenchange", onChange, true);
      document.removeEventListener("MSFullscreenChange", onChange, true);
    };
    const finish = (controlled, reason) => {
      if (completed) return;
      completed = true;
      cleanup();
      resolve(status(controlled, reason));
    };
    const check = reason => {
      const active = !!fullscreenElement();
      finish(active, active ? "dom_fullscreen" : reason);
    };
    const onChange = () => {
      if (fullscreenElement()) finish(true, "dom_fullscreen");
    };

    document.addEventListener("fullscreenchange", onChange, true);
    document.addEventListener("webkitfullscreenchange", onChange, true);
    document.addEventListener("mozfullscreenchange", onChange, true);
    document.addEventListener("MSFullscreenChange", onChange, true);
    timer = window.setTimeout(() => check("dom_fullscreen_timeout"), 1000);

    try {
      const result = request.call(requestTarget);
      if (result && typeof result.then === "function") {
        result
          .then(() => window.setTimeout(() => check("dom_fullscreen"), 0))
          .catch(() => finish(false, "dom_fullscreen_rejected"));
      }
    } catch (_) {
      finish(false, "dom_fullscreen_exception");
    }
  });
})()
"""
        )

    def click(self, x: float, y: float, source_width: float = 0, source_height: float = 0) -> dict[str, Any]:
        view_x = x * self.viewport_width / source_width if source_width and source_width > 1 else x
        view_y = y * self.viewport_height / source_height if source_height and source_height > 1 else y
        view_x = max(0.0, min(float(self.viewport_width - 1), view_x))
        view_y = max(0.0, min(float(self.viewport_height - 1), view_y))
        for event_type, button, buttons in (
            ("mouseMoved", "none", 0),
            ("mousePressed", "left", 1),
            ("mouseReleased", "left", 0),
        ):
            payload: dict[str, Any] = {"type": event_type, "x": view_x, "y": view_y, "button": button, "buttons": buttons}
            if event_type != "mouseMoved":
                payload["clickCount"] = 1
            self.connection.call("Input.dispatchMouseEvent", payload)

        script = f"""
(() => {{
  const x = Math.max(0, Math.min(window.innerWidth - 1, Number({view_x}) || 0));
  const y = Math.max(0, Math.min(window.innerHeight - 1, Number({view_y}) || 0));
  const el = document.elementFromPoint(x, y);
  if (!el) return {{ clicked: false, reason: "no_element", x, y }};
{REMOTE_INPUT_HELPERS}
  const selector = [
    "a[href]", "button", "input", "textarea", "select", "label", "summary",
    "video", "[role='button']", "[onclick]", "[tabindex]"
  ].join(",");
  const target = el.closest ? (el.closest(selector) || el) : el;
  const editable = remoteExplorerFocusEditable(
    remoteExplorerEditableFor(target) ||
    remoteExplorerEditableFor(el) ||
    remoteExplorerActiveEditable()
  );
  const fullscreenElement =
    document.fullscreenElement ||
    document.webkitFullscreenElement ||
    document.mozFullScreenElement ||
    document.msFullscreenElement;
  const inputType = editable && editable.getAttribute ? (editable.getAttribute("type") || "") : "";
  const inputValue = editable ? remoteExplorerReadEditableValue(editable) : "";
  return {{
    clicked: true,
    x,
    y,
    sourceWidth: {json_number(source_width)},
    sourceHeight: {json_number(source_height)},
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    tag: el.tagName,
    targetTag: target.tagName,
    id: target.id || el.id || "",
    text: (target.innerText || target.value || el.innerText || el.value || "").slice(0, 120),
    editable: !!editable,
    input_tag: editable ? editable.tagName : "",
    input_type: inputType,
    input_value: inputType.toLowerCase() === "password" ? "" : inputValue,
    media_fullscreen_requested: !!fullscreenElement,
    native_click: true,
    view_x: x,
    view_y: y
  }};
}})()
"""
        value = self.evaluate(script)
        self.schedule_fullscreen_topmost_refresh()
        return value if isinstance(value, dict) else {"js": value}

    def send_key_shortcut(self, action: str) -> bool:
        shortcuts = {
            "fullscreen": ("f", "KeyF", 70, 3, 0),
            "exit_fullscreen": ("Escape", "Escape", 27, 53, 0),
            "next": ("N", "KeyN", 78, 45, 8),
            "previous": ("P", "KeyP", 80, 35, 8),
        }
        shortcut = shortcuts.get(action)
        if not shortcut:
            return False
        key, code, vk, mac_vk, modifiers = shortcut
        for event_type in ("keyDown", "keyUp"):
            payload: dict[str, Any] = {
                "type": event_type,
                "key": key,
                "code": code,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": mac_vk if sys.platform == "darwin" else vk,
                "modifiers": modifiers,
            }
            if len(key) == 1 and event_type == "keyDown":
                payload["text"] = key
                payload["unmodifiedText"] = key.lower()
            self.connection.call(
                "Input.dispatchKeyEvent",
                payload,
            )
        if action in {"fullscreen", "exit_fullscreen"}:
            self.schedule_fullscreen_topmost_refresh()
        return True

    def send_special_key(self, key: str) -> bool:
        keys = {
            "Enter": ("Enter", "Enter", 13),
            "Escape": ("Escape", "Escape", 27),
            "Backspace": ("Backspace", "Backspace", 8),
        }
        spec = keys.get(key)
        if not spec:
            return False
        key_value, code, vk = spec
        for event_type in ("keyDown", "keyUp"):
            self.connection.call(
                "Input.dispatchKeyEvent",
                {
                    "type": event_type,
                    "key": key_value,
                    "code": code,
                    "windowsVirtualKeyCode": vk,
                    "nativeVirtualKeyCode": vk,
                },
            )
        if key == "Escape":
            self.schedule_fullscreen_topmost_refresh()
        return True

    def schedule_fullscreen_topmost_refresh(self) -> None:
        if sys.platform != "win32":
            return
        for delay_ms in (120, 420, 900):
            QTimer.singleShot(delay_ms, self.refresh_fullscreen_topmost)

    def refresh_fullscreen_topmost(self) -> None:
        fullscreen = self._is_chromium_window_fullscreen()
        if fullscreen is None:
            return
        self._set_fullscreen_topmost(fullscreen)

    def _is_chromium_window_fullscreen(self) -> bool | None:
        try:
            window = self.connection.call("Browser.getWindowForTarget")
            bounds = window.get("bounds") if isinstance(window.get("bounds"), dict) else {}
            return bounds.get("windowState") == "fullscreen"
        except Exception as exc:
            LOGGER.debug("Could not read Chromium window fullscreen state: %s", exc)
            return None

    def _set_fullscreen_topmost(self, enabled: bool) -> None:
        if sys.platform != "win32":
            return
        if not enabled and not self._fullscreen_topmost:
            return
        try:
            changed = _set_process_windows_topmost(self.process.pid, enabled)
        except Exception as exc:
            LOGGER.debug("Could not update Chromium topmost state: %s", exc)
            return
        if changed:
            self._fullscreen_topmost = enabled

    def _launch_browser(self, start_url: str) -> subprocess.Popen[Any]:
        args = [
            self.executable,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--disable-popup-blocking",
            "--autoplay-policy=no-user-gesture-required",
            "--window-size=1280,800",
            normalize_url(start_url),
        ]
        LOGGER.info("Starting Chromium browser engine: %s", self.executable)
        log_path = self.config.data_dir / "logs" / "chromium.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.browser_log_file = log_path.open("ab", buffering=0)
        self.browser_log_file.write(f"\n--- Chromium start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode("utf-8"))
        return subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=self.browser_log_file,
            stderr=subprocess.STDOUT,
            start_new_session=sys.platform != "win32",
        )

    def _connect(self) -> CdpConnection:
        deadline = time.time() + 12.0
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                targets = http_json(f"http://127.0.0.1:{self.port}/json/list", timeout=1.0)
                page = next((target for target in targets if target.get("type") == "page"), None)
                if page and page.get("webSocketDebuggerUrl"):
                    return CdpConnection(page["webSocketDebuggerUrl"])
            except Exception as exc:
                last_error = exc
            time.sleep(0.15)
        exit_code = self.process.poll()
        detail = f"Chromium CDP endpoint did not become ready: {last_error}"
        if exit_code is not None:
            detail += f" (Chromium exited with code {exit_code})"
        raise ChromiumUnavailable(detail)

    def _configure_page(self) -> None:
        for method in ("Page.enable", "Runtime.enable", "Network.enable"):
            self.connection.call(method)
        try:
            self.connection.call("Emulation.clearDeviceMetricsOverride")
        except CdpError:
            pass
        self.connection.call(
            "Network.setUserAgentOverride",
            {
                "userAgent": CHROME_COMPAT_USER_AGENT,
                "acceptLanguage": CHROME_COMPAT_ACCEPT_LANGUAGE,
                "platform": "Windows",
            },
        )
        self.connection.call(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
(() => {
  const originalOpen = window.open;
  window.open = function(url) {
    if (url) window.location.href = String(url);
    return window;
  };
  document.addEventListener("click", event => {
    const link = event.target && event.target.closest ? event.target.closest("a[target='_blank']") : null;
    if (link && link.href) {
      event.preventDefault();
      event.stopPropagation();
      window.location.href = link.href;
    }
  }, true);
})();
"""
            },
        )
        self.ensure_minimum_viewport(self.viewport_width, self.viewport_height)

    def _refresh_viewport_size(self) -> tuple[int, int]:
        metrics = self._window_metrics()
        self.viewport_width = max(1, int(metrics.get("innerWidth") or self.viewport_width))
        self.viewport_height = max(1, int(metrics.get("innerHeight") or self.viewport_height))
        return self.viewport_width, self.viewport_height

    def _window_metrics(self) -> dict[str, Any]:
        value = self.evaluate(
            """
(() => ({
  innerWidth: window.innerWidth,
  innerHeight: window.innerHeight,
  outerWidth: window.outerWidth,
  outerHeight: window.outerHeight,
  screenWidth: window.screen ? window.screen.width : 0,
  screenHeight: window.screen ? window.screen.height : 0
}))()
"""
        )
        return value if isinstance(value, dict) else {}


class ChromiumStreamService(QObject):
    def __init__(self, browser: ChromiumBrowserService, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.browser = browser
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._send_frame)
        self.config: ChromiumStreamConfig | None = None
        self.target_host: QHostAddress | None = None
        self.target_address: str | None = None
        self.frame_id = 0
        self.pending_frame: Future[None] | None = None
        self.encoder = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RemoteExplorerChromiumStream")
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("", 0))
        self.stats_lock = threading.Lock()
        self.frames_sent = 0
        self.last_stats_at = time.monotonic()
        self.last_client_activity_at = 0.0
        self.using_idle_capture_interval = False
        self.capture_suspended = False
        self._closed = False

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        if getattr(self, "_closed", False):
            raise RuntimeError("Chromium stream service is closed")
        host = clean_host(str(payload.get("_source_host") or payload.get("host") or ""))
        port = int(payload.get("port") or payload.get("stream_port") or 0)
        if not host:
            raise ValueError("Stream host is required")
        if port <= 0 or port > 65535:
            raise ValueError("Valid stream port is required")
        width, height, resolution = resolve_size(payload)
        requested_fps = clamp_int(payload.get("fps"), MIN_STREAM_FPS, MAX_STREAM_FPS, DEFAULT_STREAM_FPS)
        fps = min(requested_fps, DEFAULT_MACOS_UDP_STREAM_FPS) if sys.platform == "darwin" else requested_fps
        quality = clamp_int(payload.get("quality"), 30, 90, DEFAULT_JPEG_QUALITY)
        self.config = ChromiumStreamConfig(host, port, width, height, fps, quality, resolution)
        self.target_host = QHostAddress(host)
        self.target_address = host
        self.browser.set_stream_viewport(width, height)
        self.mark_client_activity()
        LOGGER.info(
            "Chromium UDP stream start target=%s:%s source_port=%s resolution=%s size=%sx%s fps=%s requested_fps=%s quality=%s chunk_bytes=%s max_chunks=%s pace=%s/%ss",
            host,
            port,
            _socket_source_port(getattr(self, "socket", None)),
            resolution,
            width,
            height,
            fps,
            requested_fps,
            quality,
            STREAM_CHUNK_BYTES,
            MAX_STREAM_CHUNKS_PER_FRAME,
            STREAM_CHUNK_PACE_BATCH,
            STREAM_CHUNK_PACE_SECONDS,
        )
        self.timer.start()
        self._send_frame()
        return self.status()

    def stop(self) -> dict[str, Any]:
        self.timer.stop()
        self.config = None
        self.target_host = None
        self.target_address = None
        self.last_client_activity_at = 0.0
        self.using_idle_capture_interval = False
        self.capture_suspended = False
        self.browser.clear_stream_viewport()
        return self.status()

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self.timer.stop()
        self.config = None
        self.target_host = None
        self.target_address = None
        self.last_client_activity_at = 0.0
        self.using_idle_capture_interval = False
        self.capture_suspended = False
        try:
            self.socket.close()
        except OSError:
            pass
        self.encoder.shutdown(wait=False, cancel_futures=True)

    def configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.config:
            return self.start(payload)
        merged = {
            "_source_host": self.config.host,
            "port": self.config.port,
            "source_port": _socket_source_port(getattr(self, "socket", None)),
            "width": self.config.width,
            "height": self.config.height,
            "resolution": self.config.resolution,
            "fps": self.config.fps,
            "quality": self.config.quality,
            **payload,
        }
        return self.start(merged)

    def status(self) -> dict[str, Any]:
        if not self.config:
            return {
                "streaming": False,
                "transport": "udp",
                "codec": "jpeg",
                "engine": "chromium",
                "default_fps": DEFAULT_STREAM_FPS,
                "min_fps": MIN_STREAM_FPS,
                "max_fps": MAX_STREAM_FPS,
                "chunk_bytes": STREAM_CHUNK_BYTES,
                "magic": STREAM_MAGIC.decode("ascii"),
            }
        return {
            "streaming": self.timer.isActive(),
            "host": self.config.host,
            "port": self.config.port,
            "source_port": _socket_source_port(getattr(self, "socket", None)),
            "width": self.config.width,
            "height": self.config.height,
            "resolution": self.config.resolution,
            "fps": self.config.fps,
            "quality": self.config.quality,
            "transport": "udp",
            "codec": "jpeg",
            "engine": "chromium",
            "min_fps": MIN_STREAM_FPS,
            "max_fps": MAX_STREAM_FPS,
            "chunk_bytes": STREAM_CHUNK_BYTES,
            "magic": STREAM_MAGIC.decode("ascii"),
        }

    def mark_client_activity(self) -> None:
        if getattr(self, "_closed", False):
            return
        self.last_client_activity_at = time.monotonic()
        if not self.config:
            return

        self.timer.setInterval(self._configured_capture_interval_ms())
        self.using_idle_capture_interval = False
        if getattr(self, "capture_suspended", False) or not self.timer.isActive():
            self.timer.start()
        self.capture_suspended = False

    def _send_frame(self) -> None:
        self._apply_idle_capture_interval_if_needed()
        if self.pending_frame is not None and not self.pending_frame.done():
            return
        if self.pending_frame is not None:
            try:
                self.pending_frame.result()
            except Exception as exc:
                LOGGER.warning("Chromium stream frame failed: %s", exc)
            self.pending_frame = None
        if not self.config or not self.target_address:
            return
        self.frame_id = (self.frame_id + 1) & 0xFFFFFFFF
        self.pending_frame = self.encoder.submit(self._capture_and_send, self.config, self.target_address, self.frame_id)

    def _apply_idle_capture_interval_if_needed(self) -> None:
        if not self.config or getattr(self, "capture_suspended", False):
            return

        idle_for = time.monotonic() - self.last_client_activity_at
        if idle_for >= STREAM_CAPTURE_SUSPEND_TIMEOUT_SECONDS:
            self.timer.stop()
            self.capture_suspended = True
            LOGGER.info(
                "Chromium UDP stream suspended: no client activity for %.1fs",
                idle_for,
            )
            return
        if idle_for < STREAM_CLIENT_ACTIVITY_TIMEOUT_SECONDS:
            return

        if not self.using_idle_capture_interval:
            self.timer.setInterval(IDLE_CAPTURE_INTERVAL_MS)
            self.using_idle_capture_interval = True
            LOGGER.info(
                "Chromium UDP stream idle: no client activity for %.1fs; capture interval=%sms",
                idle_for,
                IDLE_CAPTURE_INTERVAL_MS,
            )

    def _configured_capture_interval_ms(self) -> int:
        fps = self.config.fps if self.config else DEFAULT_STREAM_FPS
        return max(1, round(1000 / fps))

    def _capture_and_send(self, config: "ChromiumStreamConfig", target_address: str, frame_id: int) -> None:
        source_jpeg = self.browser.capture_jpeg(config.quality)
        source_image = QImage()
        if not source_image.loadFromData(source_jpeg, "JPG"):
            return

        source_width = max(1, source_image.width())
        source_height = max(1, source_image.height())
        self.browser.note_capture_size(source_width, source_height)

        frame_image = source_image.scaled(
            QSize(config.width, config.height),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        if frame_image.isNull():
            return

        frame_image, jpeg = _encode_jpeg_for_udp(frame_image, config.quality)
        if not jpeg:
            return
        frame_width = max(1, frame_image.width())
        frame_height = max(1, frame_image.height())

        chunk_count = int(math.ceil(len(jpeg) / STREAM_CHUNK_BYTES))
        if chunk_count <= 0 or chunk_count > 65535:
            return
        bytes_sent = 0
        for chunk_index in range(chunk_count):
            start = chunk_index * STREAM_CHUNK_BYTES
            chunk = jpeg[start : start + STREAM_CHUNK_BYTES]
            header = struct.pack(
                STREAM_HEADER_FORMAT,
                STREAM_MAGIC,
                frame_id,
                chunk_index,
                chunk_count,
                frame_width,
                frame_height,
                source_width,
                source_height,
            )
            packet = header + chunk
            self.socket.sendto(packet, (target_address, config.port))
            bytes_sent += len(packet)
            if chunk_index and chunk_index % STREAM_CHUNK_PACE_BATCH == 0:
                time.sleep(STREAM_CHUNK_PACE_SECONDS)
        with self.stats_lock:
            self.frames_sent += 1
            now = time.monotonic()
            if now - self.last_stats_at >= 5.0:
                LOGGER.info(
                    "Chromium UDP stream sent frames=%s target=%s:%s last=%sx%s source=%sx%s/%sB/%s chunks",
                    self.frames_sent,
                    target_address,
                    config.port,
                    frame_width,
                    frame_height,
                    source_width,
                    source_height,
                    len(jpeg),
                    chunk_count,
                )
                self.frames_sent = 0
                self.last_stats_at = now


class ChromiumStreamConfig:
    def __init__(self, host: str, port: int, width: int, height: int, fps: int, quality: int, resolution: str) -> None:
        self.host = host
        self.port = port
        self.width = width
        self.height = height
        self.fps = fps
        self.quality = quality
        self.resolution = resolution


def _socket_source_port(sock: socket.socket | None) -> int:
    if sock is None:
        return 0
    try:
        return int(sock.getsockname()[1])
    except OSError:
        return 0


class ChromiumBrowserController:
    capabilities = [
        "navigate",
        "click",
        "click_selector",
        "text",
        "set_input",
        "scroll",
        "key",
        "media_status",
        "media_control",
        "close_page",
        "back",
        "forward",
        "reload",
        "status",
        "clear_cache",
        "stream_start",
        "stream_stop",
        "stream_config",
        "stream_status",
    ]

    def __init__(self, browser: ChromiumBrowserService, stream_service: ChromiumStreamService, allow_evaluate_js: bool) -> None:
        self.browser = browser
        self.stream_service = stream_service
        self.allow_evaluate_js = allow_evaluate_js

    def handle(self, command: str, payload: dict[str, Any], respond: Any) -> None:
        self.stream_service.mark_client_activity()
        handlers = {
            "navigate": self._navigate,
            "click": self._click,
            "click_selector": self._click_selector,
            "text": self._text,
            "set_input": self._set_input,
            "scroll": self._scroll,
            "key": self._key,
            "media_status": self._media_status,
            "media_control": self._media_control,
            "close_page": self._close_page,
            "back": self._back,
            "forward": self._forward,
            "reload": self._reload,
            "status": self._status,
            "clear_cache": self._clear_cache,
            "stream_start": self._stream_start,
            "stream_stop": self._stream_stop,
            "stream_config": self._stream_config,
            "stream_status": self._stream_status,
            "webrtc_offer": self._webrtc_unavailable,
            "webrtc_stop": self._webrtc_stop,
            "webrtc_status": self._webrtc_status,
            "evaluate_js": self._evaluate_js,
        }
        handler = handlers.get(command)
        if not handler:
            respond(_error("unknown_command", f"Unknown command: {command}"))
            return
        try:
            handler(payload, respond)
        except Exception as exc:
            respond(_error("command_failed", str(exc)))

    def _navigate(self, payload: dict[str, Any], respond: Any) -> None:
        respond(_ok(self.browser.navigate(str(payload.get("url") or ""))))

    def _click(self, payload: dict[str, Any], respond: Any) -> None:
        respond(
            _ok(
                self.browser.click(
                    float(payload.get("x", 0)),
                    float(payload.get("y", 0)),
                    float(payload.get("source_width", 0) or 0),
                    float(payload.get("source_height", 0) or 0),
                )
            )
        )

    def _click_selector(self, payload: dict[str, Any], respond: Any) -> None:
        selector = str(payload.get("selector") or "")
        script = f"""
(() => {{
  const el = document.querySelector({json_string(selector)});
  if (!el) return {{ clicked: false, reason: "not_found", selector: {json_string(selector)} }};
  el.scrollIntoView({{ block: "center", inline: "center" }});
  if (el.focus) el.focus();
  el.click();
  return {{ clicked: true, tag: el.tagName, id: el.id || "", text: (el.innerText || el.value || "").slice(0, 120) }};
}})()
"""
        respond(_ok(_normalize_js_result(self.browser.evaluate(script))))

    def _text(self, payload: dict[str, Any], respond: Any) -> None:
        text = str(payload.get("text") or "")
        script = f"""
(() => {{
{REMOTE_INPUT_HELPERS}
  const el = remoteExplorerFocusEditable(remoteExplorerActiveEditable() || remoteExplorerStoredEditable());
  if (!el) return {{ inserted: false, reason: "no_active_element" }};
  if (!remoteExplorerInsertEditableText(el, {json_string(text)})) {{
    return {{ inserted: false, reason: "active_element_not_editable", tag: el.tagName }};
  }}
  el.dispatchEvent(new Event("change", {{ bubbles: true }}));
  return {{ inserted: true, mode: el.isContentEditable ? "contenteditable" : "input", tag: el.tagName }};
}})()
"""
        respond(_ok(_normalize_js_result(self.browser.evaluate(script))))

    def _set_input(self, payload: dict[str, Any], respond: Any) -> None:
        selector = str(payload.get("selector") or "")
        text = str(payload.get("text") or "")
        submit = bool(payload.get("submit", False))
        script = f"""
(() => {{
{REMOTE_INPUT_HELPERS}
  const selector = {json_string(selector)};
  const text = {json_string(text)};
  const submit = {json.dumps(submit)};
  let el = selector ? document.querySelector(selector) : (remoteExplorerActiveEditable() || remoteExplorerStoredEditable());
  el = remoteExplorerFocusEditable(remoteExplorerEditableFor(el) || el);
  if (!el) return {{ set: false, reason: "not_found", selector }};
  if (!remoteExplorerSetEditableText(el, text)) return {{ set: false, reason: "element_not_editable", tag: el.tagName }};
  el.dispatchEvent(new Event("change", {{ bubbles: true }}));
  if (submit && el.form && el.form.requestSubmit) el.form.requestSubmit();
  return {{ set: true, tag: el.tagName, active: !selector }};
}})()
"""
        respond(_ok(_normalize_js_result(self.browser.evaluate(script))))

    def _scroll(self, payload: dict[str, Any], respond: Any) -> None:
        dx = float(payload.get("dx", 0))
        dy = float(payload.get("dy", 0))
        value = self.browser.evaluate(
            f"(() => {{ window.scrollBy({{ left: {json_number(dx)}, top: {json_number(dy)}, behavior: 'smooth' }}); return {{ x: window.scrollX, y: window.scrollY }}; }})()"
        )
        respond(_ok(_normalize_js_result(value)))

    def _key(self, payload: dict[str, Any], respond: Any) -> None:
        key = str(payload.get("key") or "")
        if key not in {"Enter", "Escape", "Backspace"}:
            respond(_error("unsupported_key", f"Unsupported key: {key}"))
            return
        self.browser.send_special_key(key)
        respond(_ok({"sent": True, "key": key}))

    def _media_status(self, payload: dict[str, Any], respond: Any) -> None:
        value = self.browser.evaluate("(() => {\n" + MEDIA_CONTROL_HELPERS + "\nconst media = remoteExplorerFindMedia(); return remoteExplorerMediaStatus(media);\n})()")
        respond(_ok(_normalize_js_result(value)))

    def _media_control(self, payload: dict[str, Any], respond: Any) -> None:
        action = str(payload.get("action") or "")
        amount = float(payload.get("amount", 0) or 0)
        if action not in {"play_pause", "fullscreen", "exit_fullscreen", "volume_up", "volume_down", "mute", "seek_forward", "seek_back", "next", "previous"}:
            respond(_error("unsupported_media_action", f"Unsupported media action: {action}"))
            return
        value = self.browser.evaluate(
            "(() => {\n"
            + MEDIA_CONTROL_HELPERS
            + f"""
const action = {json_string(action)};
const amount = Number({json_number(amount)}) || 0;
const media = remoteExplorerFindMedia();
if (!media && action === "exit_fullscreen") {{
  return {{
    media_found: false,
    controlled: remoteExplorerExitFullscreen(),
    media_action: action,
    media_reason: "no_media"
  }};
}}
if (!media) return {{ media_found: false, controlled: false, media_action: action, media_reason: "no_media" }};
return remoteExplorerRunMediaAction(media, action, amount);
}})()
"""
        )
        if sys.platform == "darwin" and action == "fullscreen" and isinstance(value, dict) and not value.get("media_fullscreen"):
            request_value = self.browser.request_media_fullscreen()
            if isinstance(request_value, dict):
                value = request_value

        if isinstance(value, dict):
            if sys.platform == "darwin" and action in {"fullscreen", "exit_fullscreen"}:
                needs_shortcut = not bool(value.get("controlled"))
            else:
                needs_shortcut = action in {"fullscreen", "exit_fullscreen"} or (
                    action in {"next", "previous"} and not value.get("controlled")
                )
        else:
            needs_shortcut = False

        if needs_shortcut:
            if self.browser.send_key_shortcut(action):
                value["controlled"] = True
                value["media_reason"] = "keyboard_shortcut"
                value["media_action"] = action
        if action in {"fullscreen", "exit_fullscreen"}:
            self.browser.schedule_fullscreen_topmost_refresh()
        respond(_ok(_normalize_js_result(value)))

    def _close_page(self, payload: dict[str, Any], respond: Any) -> None:
        respond(_ok(self.browser.navigate("about:blank")))

    def _back(self, payload: dict[str, Any], respond: Any) -> None:
        respond(_ok(self.browser.back()))

    def _forward(self, payload: dict[str, Any], respond: Any) -> None:
        respond(_ok(self.browser.forward()))

    def _reload(self, payload: dict[str, Any], respond: Any) -> None:
        respond(_ok(self.browser.reload()))

    def _status(self, payload: dict[str, Any], respond: Any) -> None:
        respond(_ok(self.browser.status()))

    def _clear_cache(self, payload: dict[str, Any], respond: Any) -> None:
        clear_cookies = bool(payload.get("clear_cookies", False))
        self.browser.connection.call("Network.clearBrowserCache")
        if clear_cookies:
            self.browser.connection.call("Network.clearBrowserCookies")
        respond(_ok({"cache_cleared": True, "cookies_cleared": clear_cookies}))

    def _stream_start(self, payload: dict[str, Any], respond: Any) -> None:
        respond(_ok(self.stream_service.start(payload)))

    def _stream_stop(self, payload: dict[str, Any], respond: Any) -> None:
        respond(_ok(self.stream_service.stop()))

    def _stream_config(self, payload: dict[str, Any], respond: Any) -> None:
        respond(_ok(self.stream_service.configure(payload)))

    def _stream_status(self, payload: dict[str, Any], respond: Any) -> None:
        respond(_ok(self.stream_service.status()))

    def _webrtc_unavailable(self, payload: dict[str, Any], respond: Any) -> None:
        respond(_error("webrtc_unavailable", "WebRTC streaming is not available with the Chromium backend yet"))

    def _webrtc_stop(self, payload: dict[str, Any], respond: Any) -> None:
        respond(_ok({"stopped": True, "available": False}))

    def _webrtc_status(self, payload: dict[str, Any], respond: Any) -> None:
        respond(_ok({"available": False, "engine": "chromium"}))

    def _evaluate_js(self, payload: dict[str, Any], respond: Any) -> None:
        if not self.allow_evaluate_js:
            respond(_error("forbidden", "evaluate_js is disabled"))
            return
        respond(_ok(_normalize_js_result(self.browser.evaluate(str(payload.get("script") or "")))))


def _is_recoverable_capture_error(exc: CdpError) -> bool:
    message = str(exc).lower()
    if "timed out waiting for the cdp connection" in message:
        return False
    return any(
        marker in message
        for marker in (
            "not attached to an active page",
            "captureScreenshot timed out".lower(),
            "connection failed",
            "websocket closed",
            "connection is closed",
        )
    )


def _cleanup_stale_chromium_profile(profile_dir: Path) -> list[int]:
    if sys.platform not in {"darwin", "linux"}:
        return []

    profile_path = str(profile_dir.resolve())
    scan_succeeded, stale = _scan_profile_processes(profile_path)
    if not scan_succeeded:
        LOGGER.warning("Could not inspect existing Chromium processes for profile %s", profile_path)
        return []
    if stale:
        LOGGER.warning(
            "Stopping stale Chromium processes for Remote Explorer profile %s: %s",
            profile_path,
            ", ".join(str(pid) for pid in stale),
        )
        for pid in stale:
            _signal_process(pid, signal.SIGTERM)

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            scan_succeeded, remaining = _scan_profile_processes(profile_path)
            if not scan_succeeded:
                return stale
            if not remaining:
                break
            time.sleep(0.1)

        scan_succeeded, remaining = _scan_profile_processes(profile_path)
        if not scan_succeeded:
            return stale
        if remaining:
            for pid in remaining:
                _signal_process(pid, signal.SIGKILL)

            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                scan_succeeded, remaining = _scan_profile_processes(profile_path)
                if not scan_succeeded:
                    return stale
                if not remaining:
                    break
                time.sleep(0.1)
    else:
        remaining = []

    if not remaining:
        for name in PROFILE_LOCK_NAMES:
            try:
                (profile_dir / name).unlink(missing_ok=True)
            except OSError as exc:
                LOGGER.debug("Could not remove stale Chromium profile lock %s: %s", name, exc)
    return remaining


def _find_profile_processes(profile_path: str) -> list[int]:
    return _scan_profile_processes(profile_path)[1]


def _scan_profile_processes(profile_path: str) -> tuple[bool, list[int]]:
    try:
        result = subprocess.run(
            ["ps", "-axww", "-o", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False, []
    if result.returncode != 0:
        return False, []

    marker = f"--user-data-dir={profile_path}"
    process_ids: list[int] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or marker not in parts[1]:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid != os.getpid():
            process_ids.append(pid)
    return True, process_ids


def _signal_process(pid: int, signal_number: int) -> None:
    try:
        os.kill(pid, signal_number)
    except (OSError, ProcessLookupError):
        pass


def _terminate_owned_process(process: subprocess.Popen[Any], process_group_id: int | None) -> None:
    if sys.platform != "win32" and process_group_id:
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and _process_group_exists(process_group_id):
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
        if _process_group_exists(process_group_id):
            LOGGER.debug("Chromium process group did not exit after terminate; killing")
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        return

    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        LOGGER.debug("Chromium did not exit after terminate; killing")
        process.kill()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def find_chromium_executable(explicit: str | None = None) -> str | None:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    candidates.extend(
        [
            "chrome",
            "msedge",
            "chromium",
            "chromium-browser",
            "google-chrome",
            "google-chrome-stable",
        ]
    )
    if sys.platform == "win32":
        roots = [
            Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
            Path.home() / "AppData/Local/Microsoft/Edge/Application/msedge.exe",
            Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
            Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        ]
        candidates.extend(str(path) for path in roots)
    elif sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            ]
        )

    for candidate in candidates:
        resolved = shutil.which(candidate) if not Path(candidate).is_file() else candidate
        if resolved:
            return str(resolved)
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _set_process_windows_topmost(process_id: int, enabled: bool) -> int:
    if sys.platform != "win32":
        return 0

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_windows_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.EnumWindows.argtypes = [enum_windows_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL

    GW_OWNER = 4
    HWND_TOPMOST = wintypes.HWND(-1)
    HWND_NOTOPMOST = wintypes.HWND(-2)
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
    insert_after = HWND_TOPMOST if enabled else HWND_NOTOPMOST
    handles: list[wintypes.HWND] = []

    @enum_windows_proc
    def collect_window(hwnd: wintypes.HWND, lparam: wintypes.LPARAM) -> int:
        del lparam
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if owner_pid.value == process_id and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, GW_OWNER):
            handles.append(hwnd)
        return 1

    if not user32.EnumWindows(collect_window, 0):
        last_error = ctypes.get_last_error()
        if last_error:
            raise ctypes.WinError(last_error)

    changed = 0
    for hwnd in handles:
        if user32.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, flags):
            changed += 1
    return changed


def json_string(value: str) -> str:
    import json

    return json.dumps(value)


def json_number(value: float) -> str:
    if not math.isfinite(float(value)):
        return "0"
    return repr(float(value))


def _encode_jpeg(image: QImage, quality: int) -> bytes:
    byte_array = QByteArray()
    buffer = QBuffer(byte_array)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        if not image.save(buffer, "JPG", clamp_int(quality, MIN_JPEG_QUALITY, 90, DEFAULT_JPEG_QUALITY)):
            return b""
        return bytes(byte_array)
    finally:
        buffer.close()


def _encode_jpeg_for_udp(image: QImage, quality: int) -> tuple[QImage, bytes]:
    target_bytes = STREAM_CHUNK_BYTES * MAX_STREAM_CHUNKS_PER_FRAME
    current = image
    for scale_attempt in range(4):
        for candidate_quality in (quality, 48, 42, 36, MIN_JPEG_QUALITY):
            jpeg = _encode_jpeg(current, candidate_quality)
            if not jpeg:
                continue
            if len(jpeg) <= target_bytes or (scale_attempt == 3 and candidate_quality == MIN_JPEG_QUALITY):
                return current, jpeg

        next_width = max(320, int(current.width() * 0.85))
        next_height = max(180, int(current.height() * 0.85))
        if next_width >= current.width() and next_height >= current.height():
            break
        scaled = current.scaled(
            QSize(next_width, next_height),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        if scaled.isNull():
            break
        current = scaled

    return current, _encode_jpeg(current, MIN_JPEG_QUALITY)


def _normalize_js_result(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {"js": value, **value}
    return {"js": value}


def _ok(result: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "result": result}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}
