from __future__ import annotations

import json
import logging
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QLineEdit, QMainWindow, QToolBar
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
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

        self.view = QWebEngineView(self)
        self.page = QWebEnginePage(self.profile, self)
        self.view.setPage(self.page)
        self.setCentralWidget(self.view)

        self.address_bar = QLineEdit(self)
        self.address_bar.returnPressed.connect(self._navigate_from_bar)
        self._build_toolbar()

        self.stream_service = BrowserStreamService(self.view, self)
        self.webrtc_service = BrowserWebRtcService(self.view, self)
        self.controller = BrowserController(
            self.view,
            self.stream_service,
            self.webrtc_service,
            config.allow_evaluate_js,
        )
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

    def _navigate_from_bar(self) -> None:
        self.view.setUrl(QUrl(normalize_url(self.address_bar.text())))


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
        x = float(payload.get("x", 0))
        y = float(payload.get("y", 0))
        script = f"""
(() => {{
  const sourceWidth = Number({json.dumps(payload.get("source_width", 0))}) || 0;
  const sourceHeight = Number({json.dumps(payload.get("source_height", 0))}) || 0;
  const rawX = Number({json.dumps(x)}) || 0;
  const rawY = Number({json.dumps(y)}) || 0;
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
  const base = {{
    bubbles: true,
    cancelable: true,
    composed: true,
    view: window,
    clientX: x,
    clientY: y,
    screenX: window.screenX + x,
    screenY: window.screenY + y,
    button: 0
  }};
  const pointerBase = {{
    ...base,
    pointerId: 1,
    pointerType: "touch",
    isPrimary: true,
    width: 1,
    height: 1,
    pressure: 0.5
  }};
  const dispatchMouse = (name, extra = {{}}) => target.dispatchEvent(new MouseEvent(name, {{ ...base, ...extra }}));
  const dispatchPointer = (name, extra = {{}}) => {{
    try {{
      return target.dispatchEvent(new PointerEvent(name, {{ ...pointerBase, ...extra }}));
    }} catch (_) {{
      return true;
    }}
  }};

  dispatchPointer("pointerover", {{ buttons: 0, pressure: 0 }});
  dispatchMouse("mouseover", {{ buttons: 0 }});
  dispatchPointer("pointermove", {{ buttons: 0, pressure: 0 }});
  dispatchMouse("mousemove", {{ buttons: 0 }});
  dispatchPointer("pointerdown", {{ buttons: 1 }});
  dispatchMouse("mousedown", {{ buttons: 1 }});
  if (typeof target.focus === "function") {{
    try {{ target.focus({{ preventScroll: true }}); }} catch (_) {{ target.focus(); }}
  }}
  dispatchPointer("pointerup", {{ buttons: 0, pressure: 0 }});
  dispatchMouse("mouseup", {{ buttons: 0 }});
  const clickAccepted = dispatchMouse("click", {{ buttons: 0, detail: 1 }});
  try {{
    if (clickAccepted && typeof target.click === "function") target.click();
  }} catch (_) {{}}
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
    text: (target.innerText || target.value || el.innerText || el.value || "").slice(0, 120)
  }};
}})();
"""
        self._run_js(script, respond)

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
  const el = document.querySelector(selector);
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
  return {{ set: true, tag: el.tagName }};
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

    def _run_js(self, script: str, respond: BrowserResponder) -> None:
        self.view.page().runJavaScript(script, lambda value: respond(_ok({"js": value})))

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
