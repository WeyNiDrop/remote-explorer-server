from __future__ import annotations

import json
import logging
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt, QTimer, QUrl
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QLineEdit, QMainWindow, QToolBar
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

from .config import ServerConfig
from .protocol import normalize_url
from .streaming import BrowserStreamService
from .webrtc import BrowserWebRtcService

BrowserResponder = Callable[[dict[str, Any]], None]
LOGGER = logging.getLogger("remote_explorer.browser")


class BrowserWindow(QMainWindow):
    def __init__(self, config: ServerConfig) -> None:
        super().__init__()
        self.config = config
        self.setWindowTitle(config.name)
        self.resize(1280, 800)

        profile_dir = config.data_dir / "profile"
        cache_dir = config.data_dir / "cache"
        profile_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)

        self.profile = QWebEngineProfile("remote-explorer", self)
        self.profile.setPersistentStoragePath(str(profile_dir))
        self.profile.setCachePath(str(cache_dir))
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        self.profile.settings().setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True)

        self.view = QWebEngineView(self)
        self.page = QWebEnginePage(self.profile, self)
        self.page.settings().setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True)
        self.view.setPage(self.page)
        self.setCentralWidget(self.view)

        self.address_bar = QLineEdit(self)
        self.address_bar.returnPressed.connect(self._navigate_from_bar)
        self.toolbar: QToolBar | None = None
        self._build_toolbar()

        self.stream_service = BrowserStreamService(self.view, self)
        self.webrtc_service = BrowserWebRtcService(self.view, self)
        self.controller = BrowserController(
            self.view,
            self.stream_service,
            self.webrtc_service,
            config.allow_evaluate_js,
        )
        self.page.fullScreenRequested.connect(self._handle_fullscreen_request)
        self.view.urlChanged.connect(lambda url: self.address_bar.setText(url.toString()))
        self.view.titleChanged.connect(lambda title: self.setWindowTitle(f"{title} - {config.name}"))

        self.view.setUrl(QUrl(normalize_url(config.start_url)))

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Browser", self)
        toolbar.setMovable(False)
        toolbar.addAction("Back", self.view.back)
        toolbar.addAction("Forward", self.view.forward)
        toolbar.addAction("Reload", self.view.reload)
        toolbar.addWidget(self.address_bar)
        self.addToolBar(toolbar)
        self.toolbar = toolbar

    def _navigate_from_bar(self) -> None:
        self.view.setUrl(QUrl(normalize_url(self.address_bar.text())))

    def _handle_fullscreen_request(self, request: Any) -> None:
        toggle_on = bool(request.toggleOn())
        if toggle_on:
            LOGGER.info("Browser fullscreen requested")
            request.accept()
            if self.toolbar is not None:
                self.toolbar.hide()
            self.showFullScreen()
            return

        request.accept()
        self.showNormal()
        if self.toolbar is not None:
            self.toolbar.show()


class BrowserController:
    def __init__(
        self,
        view: QWebEngineView,
        stream_service: BrowserStreamService,
        webrtc_service: BrowserWebRtcService,
        allow_evaluate_js: bool = False,
    ) -> None:
        self.view = view
        self.stream_service = stream_service
        self.webrtc_service = webrtc_service
        self.allow_evaluate_js = allow_evaluate_js

    def handle(self, command: str, payload: dict[str, Any], respond: BrowserResponder) -> None:
        handlers = {
            "navigate": self._navigate,
            "click": self._click,
            "click_selector": self._click_selector,
            "text": self._text,
            "set_input": self._set_input,
            "scroll": self._scroll,
            "key": self._key,
            "close_page": self._close_page,
            "back": self._back,
            "forward": self._forward,
            "reload": self._reload,
            "status": self._status,
            "stream_start": self._stream_start,
            "stream_stop": self._stream_stop,
            "stream_config": self._stream_config,
            "stream_status": self._stream_status,
            "webrtc_offer": self._webrtc_offer,
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

    def _navigate(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        url = normalize_url(str(payload.get("url") or ""))
        self.view.setUrl(QUrl(url))
        respond(_ok({"accepted": True, "url": url}))

    def _click(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        raw_x = float(payload.get("x", 0))
        raw_y = float(payload.get("y", 0))
        source_width = float(payload.get("source_width", 0) or 0)
        source_height = float(payload.get("source_height", 0) or 0)
        view_x, view_y = self._map_stream_point_to_view(raw_x, raw_y, source_width, source_height)
        native_click = self._send_native_click(view_x, view_y)
        script = f"""
(() => {{
  const sourceWidth = Number({json.dumps(payload.get("source_width", 0))}) || 0;
  const sourceHeight = Number({json.dumps(payload.get("source_height", 0))}) || 0;
  const rawX = Number({json.dumps(raw_x)}) || 0;
  const rawY = Number({json.dumps(raw_y)}) || 0;
  const requestedX = sourceWidth > 1 ? rawX * window.innerWidth / sourceWidth : rawX;
  const requestedY = sourceHeight > 1 ? rawY * window.innerHeight / sourceHeight : rawY;
  const x = Math.max(0, Math.min(window.innerWidth - 1, requestedX));
  const y = Math.max(0, Math.min(window.innerHeight - 1, requestedY));
  const el = document.elementFromPoint(x, y);
  if (!el) return {{ clicked: false, reason: "no_element", x, y }};

  const selector = [
    "a[href]",
    "button",
    "input",
    "textarea",
    "select",
    "label",
    "summary",
    "video",
    "[role='button']",
    "[onclick]",
    "[tabindex]"
  ].join(",");
  const target = el.closest ? (el.closest(selector) || el) : el;
  const editableSelector = [
    "textarea",
    "input:not([type='button']):not([type='submit']):not([type='reset']):not([type='checkbox']):not([type='radio']):not([type='file'])",
    "[contenteditable='true']",
    "[contenteditable='plaintext-only']"
  ].join(",");
  const isEditable = candidate => {{
    if (!candidate) return false;
    if (candidate.isContentEditable) return true;
    if (!candidate.matches || !candidate.matches(editableSelector)) return false;
    if (candidate.disabled || candidate.readOnly) return false;
    return true;
  }};
  const activeEditable = () => isEditable(document.activeElement) ? document.activeElement : null;
  const editableFor = candidate => {{
    if (isEditable(candidate)) return candidate;
    if (candidate && candidate.tagName === "LABEL" && candidate.control && isEditable(candidate.control)) {{
      return candidate.control;
    }}
    const closest = candidate && candidate.closest ? candidate.closest(editableSelector) : null;
    return isEditable(closest) ? closest : null;
  }};
  const videoAtPoint = () => {{
    const direct = el.tagName === "VIDEO" ? el : (el.closest ? el.closest("video") : null);
    if (direct) return direct;
    const videos = Array.from(document.querySelectorAll("video"))
      .filter(video => {{
        const rect = video.getBoundingClientRect();
        return rect.width > 1 && rect.height > 1 &&
          x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
      }})
      .sort((a, b) => {{
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        return (br.width * br.height) - (ar.width * ar.height);
      }});
    return videos[0] || null;
  }};
  const video = videoAtPoint();
  const fullscreenElement =
    document.fullscreenElement ||
    document.webkitFullscreenElement ||
    document.mozFullScreenElement ||
    document.msFullscreenElement;

  const editable = activeEditable() || editableFor(target) || editableFor(el);
  const inputType = editable && editable.getAttribute ? (editable.getAttribute("type") || "") : "";
  const inputValue = editable
    ? (editable.isContentEditable ? (editable.innerText || editable.textContent || "") : (editable.value || ""))
    : "";
  return {{
    clicked: true,
    x,
    y,
    requestedX,
    requestedY,
    sourceWidth,
    sourceHeight,
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
    media_error: ""
  }};
}})();
"""
        def annotate(value: Any) -> Any:
            if isinstance(value, dict):
                value["native_click"] = native_click
                value["view_x"] = view_x
                value["view_y"] = view_y
            return value

        QTimer.singleShot(80, lambda: self._run_js(script, respond, annotate))

    def _map_stream_point_to_view(
        self,
        raw_x: float,
        raw_y: float,
        source_width: float,
        source_height: float,
    ) -> tuple[float, float]:
        view_width = max(1, self.view.width())
        view_height = max(1, self.view.height())
        x = raw_x * view_width / source_width if source_width > 1 else raw_x
        y = raw_y * view_height / source_height if source_height > 1 else raw_y
        return (
            max(0.0, min(view_width - 1.0, x)),
            max(0.0, min(view_height - 1.0, y)),
        )

    def _send_native_click(self, x: float, y: float) -> bool:
        target = self.view.focusProxy() or self.view
        if target is None:
            return False

        self.view.setFocus(Qt.FocusReason.OtherFocusReason)
        view_point = QPoint(round(x), round(y))
        global_point = self.view.mapToGlobal(view_point)
        if target is self.view:
            target_point = view_point
        else:
            target_point = target.mapFromGlobal(global_point)

        local_pos = QPointF(target_point)
        global_pos = QPointF(global_point)
        events = (
            QMouseEvent(
                QEvent.Type.MouseMove,
                local_pos,
                global_pos,
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            ),
            QMouseEvent(
                QEvent.Type.MouseButtonPress,
                local_pos,
                global_pos,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            ),
            QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                local_pos,
                global_pos,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )

        for event in events:
            QCoreApplication.sendEvent(target, event)
        return True

    def _click_selector(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        selector = str(payload.get("selector") or "")
        script = f"""
(() => {{
  const selector = {json.dumps(selector)};
  const el = document.querySelector(selector);
  if (!el) return {{ clicked: false, reason: "not_found", selector }};
  el.scrollIntoView({{ block: "center", inline: "center" }});
  if (typeof el.focus === "function") el.focus();
  el.click();
  return {{
    clicked: true,
    tag: el.tagName,
    id: el.id || "",
    text: (el.innerText || el.value || "").slice(0, 120)
  }};
}})();
"""
        self._run_js(script, respond)

    def _text(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        text = str(payload.get("text") or "")
        script = f"""
(() => {{
  const text = {json.dumps(text)};
  const el = document.activeElement;
  if (!el) return {{ inserted: false, reason: "no_active_element" }};
  if (el.isContentEditable) {{
    document.execCommand("insertText", false, text);
    return {{ inserted: true, mode: "contenteditable" }};
  }}
  if ("value" in el) {{
    const start = el.selectionStart ?? el.value.length;
    const end = el.selectionEnd ?? el.value.length;
    el.setRangeText(text, start, end, "end");
    el.dispatchEvent(new InputEvent("input", {{ bubbles: true, inputType: "insertText", data: text }}));
    el.dispatchEvent(new Event("change", {{ bubbles: true }}));
    return {{ inserted: true, mode: "input" }};
  }}
  return {{ inserted: false, reason: "active_element_not_editable", tag: el.tagName }};
}})();
"""
        self._run_js(script, respond)

    def _set_input(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        selector = str(payload.get("selector") or "")
        text = str(payload.get("text") or "")
        submit = bool(payload.get("submit", False))
        script = f"""
(() => {{
  const selector = {json.dumps(selector)};
  const text = {json.dumps(text)};
  const submit = {json.dumps(submit)};
  let el = null;
  if (selector) {{
    try {{
      el = document.querySelector(selector);
    }} catch (error) {{
      return {{ set: false, reason: "invalid_selector", selector, error: String(error && error.message || error) }};
    }}
  }} else {{
    el = document.activeElement;
  }}
  if (!el) return {{ set: false, reason: "not_found", selector }};
  if (typeof el.focus === "function") el.focus();
  if (el.isContentEditable) {{
    el.textContent = text;
  }} else if ("value" in el) {{
    el.value = text;
  }} else {{
    return {{ set: false, reason: "element_not_editable", tag: el.tagName }};
  }}
  el.dispatchEvent(new InputEvent("input", {{ bubbles: true, inputType: "insertText", data: text }}));
  el.dispatchEvent(new Event("change", {{ bubbles: true }}));
  if (submit && el.form && typeof el.form.requestSubmit === "function") el.form.requestSubmit();
  return {{ set: true, tag: el.tagName, active: !selector }};
}})();
"""
        self._run_js(script, respond)

    def _scroll(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        dx = float(payload.get("dx", 0))
        dy = float(payload.get("dy", 0))
        script = f"""
(() => {{
  window.scrollBy({{ left: {json.dumps(dx)}, top: {json.dumps(dy)}, behavior: "smooth" }});
  return {{ x: window.scrollX, y: window.scrollY }};
}})();
"""
        self._run_js(script, respond)

    def _key(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        key = str(payload.get("key") or "")
        if key not in {"Enter", "Escape", "Backspace"}:
            respond(_error("unsupported_key", f"Unsupported key: {key}"))
            return
        script = f"""
(() => {{
  const key = {json.dumps(key)};
  const el = document.activeElement || document.body;
  const opts = {{ key, bubbles: true, cancelable: true }};
  el.dispatchEvent(new KeyboardEvent("keydown", opts));
  if (key === "Enter" && el.form && typeof el.form.requestSubmit === "function") el.form.requestSubmit();
  if (key === "Backspace" && "value" in el) {{
    const start = el.selectionStart ?? el.value.length;
    const end = el.selectionEnd ?? el.value.length;
    if (start !== end) el.setRangeText("", start, end, "end");
    else if (start > 0) el.setRangeText("", start - 1, start, "end");
    el.dispatchEvent(new InputEvent("input", {{ bubbles: true, inputType: "deleteContentBackward" }}));
  }}
  el.dispatchEvent(new KeyboardEvent("keyup", opts));
  return {{ sent: true, key }};
}})();
"""
        self._run_js(script, respond)

    def _back(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        self.view.back()
        respond(_ok({"accepted": True}))

    def _close_page(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        self.view.setUrl(QUrl("about:blank"))
        respond(_ok({"accepted": True, "url": "about:blank"}))

    def _forward(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        self.view.forward()
        respond(_ok({"accepted": True}))

    def _reload(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        self.view.reload()
        respond(_ok({"accepted": True}))

    def _status(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        history = self.view.history()
        respond(
            _ok(
                {
                    "url": self.view.url().toString(),
                    "title": self.view.title(),
                    "can_go_back": history.canGoBack(),
                    "can_go_forward": history.canGoForward(),
                }
            )
        )

    def _stream_start(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        self._stop_webrtc_if_active()
        respond(_ok(self.stream_service.start(payload)))

    def _stream_stop(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        respond(_ok(self.stream_service.stop()))

    def _stream_config(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        self._stop_webrtc_if_active()
        respond(_ok(self.stream_service.configure(payload)))

    def _stream_status(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        respond(_ok(self.stream_service.status()))

    def _webrtc_offer(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        if self.stream_service.status().get("streaming"):
            LOGGER.info("Stopping UDP/JPEG stream before accepting WebRTC offer")
        self.stream_service.stop()
        self._respond_future(self.webrtc_service.offer_async(payload), respond)

    def _webrtc_stop(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        self._respond_future(self.webrtc_service.stop_async(payload), respond)

    def _webrtc_status(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        respond(_ok(self.webrtc_service.status()))

    def _evaluate_js(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        if not self.allow_evaluate_js:
            respond(_error("forbidden", "evaluate_js is disabled"))
            return
        script = str(payload.get("script") or "")
        self._run_js(script, respond)

    def _run_js(
        self,
        script: str,
        respond: BrowserResponder,
        after: Callable[[Any], Any] | None = None,
    ) -> None:
        def done(value: Any) -> None:
            try:
                if after is not None:
                    value = after(value)
                respond(_ok(_normalize_js_result(value)))
            except Exception as exc:
                respond(_error("command_failed", str(exc)))

        self.view.page().runJavaScript(script, done)

    @staticmethod
    def _respond_future(future: Future[dict[str, Any]], respond: BrowserResponder) -> None:
        def done(completed: Future[dict[str, Any]]) -> None:
            if completed.cancelled():
                respond(_error("command_cancelled", "Command was cancelled"))
                return

            try:
                respond(_ok(completed.result()))
            except Exception as exc:
                respond(_error("command_failed", str(exc)))

        future.add_done_callback(done)

    def _stop_webrtc_if_active(self) -> None:
        if not self.webrtc_service.has_peers():
            return

        LOGGER.info("Stopping WebRTC peers before starting UDP/JPEG stream")
        self.webrtc_service.stop({})


def _ok(result: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "result": result}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _normalize_js_result(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {"js": value, **value}
    return {"js": value}
