from __future__ import annotations

import json
import logging
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt, QTimer, QUrl
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QLineEdit, QMainWindow, QToolBar
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

from .config import ServerConfig
from .protocol import normalize_url
from .streaming import BrowserStreamService
from .webrtc import BrowserWebRtcService

BrowserResponder = Callable[[dict[str, Any]], None]

REMOTE_INPUT_HELPERS = r"""
  const remoteExplorerEditableSelector = [
    "textarea",
    "input:not([type='button']):not([type='submit']):not([type='reset']):not([type='checkbox']):not([type='radio']):not([type='file'])",
    "[contenteditable='true']",
    "[contenteditable='plaintext-only']"
  ].join(",");
  const remoteExplorerIsEditable = candidate => {
    if (!candidate) return false;
    if (candidate.isContentEditable) return true;
    if (!candidate.matches || !candidate.matches(remoteExplorerEditableSelector)) return false;
    if (candidate.disabled || candidate.readOnly) return false;
    return true;
  };
  const remoteExplorerEditableFor = candidate => {
    if (remoteExplorerIsEditable(candidate)) return candidate;
    if (candidate && candidate.tagName === "LABEL" && candidate.control && remoteExplorerIsEditable(candidate.control)) {
      return candidate.control;
    }
    const closest = candidate && candidate.closest ? candidate.closest(remoteExplorerEditableSelector) : null;
    return remoteExplorerIsEditable(closest) ? closest : null;
  };
  const remoteExplorerActiveEditable = () =>
    remoteExplorerIsEditable(document.activeElement) ? document.activeElement : null;
  const remoteExplorerStoredEditable = () => {
    const stored = window.__remoteExplorerFocusedInput || null;
    return remoteExplorerIsEditable(stored) && document.contains(stored) ? stored : null;
  };
  const remoteExplorerFocusEditable = el => {
    if (!remoteExplorerIsEditable(el)) return null;
    window.__remoteExplorerFocusedInput = el;
    if (typeof el.focus === "function") {
      try {
        el.focus({ preventScroll: true });
      } catch (_) {
        el.focus();
      }
    }
    return el;
  };
  const remoteExplorerReadEditableValue = el => {
    if (!el) return "";
    return el.isContentEditable ? (el.innerText || el.textContent || "") : (el.value || "");
  };
  const remoteExplorerSetNativeValue = (el, text) => {
    const proto =
      window.HTMLTextAreaElement && el instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : window.HTMLInputElement && el instanceof HTMLInputElement
          ? HTMLInputElement.prototype
          : null;
    const descriptor = proto ? Object.getOwnPropertyDescriptor(proto, "value") : null;
    if (descriptor && descriptor.set) {
      descriptor.set.call(el, text);
    } else {
      el.value = text;
    }
  };
  const remoteExplorerDispatchInput = (el, inputType, data = null) => {
    try {
      el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType, data }));
    } catch (_) {
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
  };
  const remoteExplorerSetEditableText = (el, text) => {
    if (el.isContentEditable) {
      el.textContent = text;
      remoteExplorerDispatchInput(el, "insertText", text);
      return true;
    }
    if ("value" in el) {
      remoteExplorerSetNativeValue(el, text);
      try {
        el.setSelectionRange(text.length, text.length);
      } catch (_) {}
      remoteExplorerDispatchInput(el, "insertText", text);
      return true;
    }
    return false;
  };
  const remoteExplorerInsertEditableText = (el, text) => {
    if (el.isContentEditable) {
      document.execCommand("insertText", false, text);
      return true;
    }
    if ("value" in el) {
      const value = String(el.value || "");
      const start = typeof el.selectionStart === "number" ? el.selectionStart : value.length;
      const end = typeof el.selectionEnd === "number" ? el.selectionEnd : value.length;
      const next = value.slice(0, start) + text + value.slice(end);
      const caret = start + text.length;
      remoteExplorerSetNativeValue(el, next);
      try {
        el.setSelectionRange(caret, caret);
      } catch (_) {}
      remoteExplorerDispatchInput(el, "insertText", text);
      return true;
    }
    return false;
  };
  const remoteExplorerBackspaceEditable = el => {
    if (el.isContentEditable) {
      document.execCommand("delete", false);
      return true;
    }
    if ("value" in el) {
      const value = String(el.value || "");
      const start = typeof el.selectionStart === "number" ? el.selectionStart : value.length;
      const end = typeof el.selectionEnd === "number" ? el.selectionEnd : value.length;
      if (start === 0 && end === 0) return true;
      const deleteStart = start === end ? Math.max(0, start - 1) : start;
      const next = value.slice(0, deleteStart) + value.slice(end);
      remoteExplorerSetNativeValue(el, next);
      try {
        el.setSelectionRange(deleteStart, deleteStart);
      } catch (_) {}
      remoteExplorerDispatchInput(el, "deleteContentBackward");
      return true;
    }
    return false;
  };
"""

MEDIA_CONTROL_HELPERS = r"""
  const remoteExplorerVisibleScore = el => {
    if (!el || !el.getBoundingClientRect) return 0;
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return 0;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity || 1) <= 0) return 0;
    const visibleWidth = Math.max(0, Math.min(rect.right, window.innerWidth) - Math.max(rect.left, 0));
    const visibleHeight = Math.max(0, Math.min(rect.bottom, window.innerHeight) - Math.max(rect.top, 0));
    return visibleWidth * visibleHeight;
  };
  const remoteExplorerMediaStatus = media => {
    if (!media) {
      return { media_found: false, media_reason: "no_media" };
    }
    return {
      media_found: true,
      media_tag: media.tagName,
      media_paused: !!media.paused,
      media_muted: !!media.muted,
      media_volume: Number(media.volume || 0),
      media_current_time: Number(media.currentTime || 0),
      media_duration: Number.isFinite(media.duration) ? Number(media.duration) : 0,
      media_fullscreen: !!(
        document.fullscreenElement ||
        document.webkitFullscreenElement ||
        document.mozFullScreenElement ||
        document.msFullscreenElement
      ),
      media_title: document.title || ""
    };
  };
  const remoteExplorerFindMedia = () => {
    const stored = window.__remoteExplorerMedia || null;
    if (stored && document.contains(stored)) return stored;
    const candidates = Array.from(document.querySelectorAll("video,audio"))
      .map(media => {
        const visible = remoteExplorerVisibleScore(media);
        const playing = media.paused ? 0 : 1000000000;
        const active = media.currentTime > 0 ? 10000000 : 0;
        const hasSource = media.currentSrc || media.src ? 100000 : 0;
        return { media, score: playing + active + hasSource + visible };
      })
      .filter(item => item.score > 0)
      .sort((a, b) => b.score - a.score);
    const media = candidates.length ? candidates[0].media : null;
    if (media) window.__remoteExplorerMedia = media;
    return media;
  };
  const remoteExplorerPlayerRoot = media => {
    let node = media;
    let fallback = null;
    for (let i = 0; node && i < 8; i += 1, node = node.parentElement) {
      const marker = `${node.id || ""} ${node.className || ""} ${node.getAttribute("role") || ""}`.toLowerCase();
      if (marker.includes("player") || marker.includes("control")) {
        return node;
      }
      if (!fallback && (marker.includes("video") || marker.includes("media"))) fallback = node;
    }
    return fallback || media.parentElement || media;
  };
  const remoteExplorerPlayerScopes = media => {
    const scopes = [];
    let node = media;
    for (let i = 0; node && i < 10; i += 1, node = node.parentElement) {
      const marker = `${node.id || ""} ${node.className || ""} ${node.getAttribute("role") || ""}`.toLowerCase();
      if (
        marker.includes("player") ||
        marker.includes("control") ||
        marker.includes("video") ||
        marker.includes("media")
      ) {
        scopes.push(node);
      }
    }
    scopes.push(media.parentElement || media);
    return Array.from(new Set(scopes)).filter(Boolean);
  };
  const remoteExplorerClickableText = el =>
    [
      el.getAttribute("aria-label") || "",
      el.getAttribute("title") || "",
      el.getAttribute("data-title") || "",
      el.getAttribute("data-tooltip") || "",
      el.id || "",
      el.className || "",
      el.textContent || ""
    ].join(" ").toLowerCase();
  const remoteExplorerBlockedMediaButton = el => {
    const text = remoteExplorerClickableText(el);
    if ([
      "voice",
      "microphone",
      "mic",
      "speech",
      "dictation",
      "search",
      "record",
      "recording",
      "permission",
      "语音",
      "語音",
      "麦克风",
      "麥克風",
      "搜索",
      "搜尋",
      "录音",
      "錄音"
    ].some(keyword => text.includes(keyword))) {
      return true;
    }
    return !!(el.closest && el.closest("form,[role='search'],[type='search']"));
  };
  const remoteExplorerKeywordMatches = (text, keyword) => {
    if (keyword === "next" || keyword === "prev") {
      return new RegExp(`(^|[^a-z])${keyword}([^a-z]|$)`).test(text);
    }
    return text.includes(keyword);
  };
  const remoteExplorerFindButton = (media, keywordGroups) => {
    const selector = "button,a,[role='button'],[aria-label],[title],[onclick]";
    const scopes = remoteExplorerPlayerScopes(media);
    const seen = new Set();
    for (const scope of scopes) {
      const buttons = Array.from(scope.querySelectorAll ? scope.querySelectorAll(selector) : []);
      for (const button of buttons) {
        if (seen.has(button)) continue;
        seen.add(button);
        if (remoteExplorerVisibleScore(button) <= 0) continue;
        if (remoteExplorerBlockedMediaButton(button)) continue;
        const text = remoteExplorerClickableText(button);
        if (keywordGroups.some(group => group.every(keyword => remoteExplorerKeywordMatches(text, keyword)))) {
          return button;
        }
      }
    }
    return null;
  };
  const remoteExplorerClickButton = button => {
    if (!button) return false;
    button.dispatchEvent(new MouseEvent("mouseover", { bubbles: true, cancelable: true }));
    button.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, button: 0 }));
    button.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, button: 0 }));
    button.click();
    return true;
  };
  const remoteExplorerRequestFullscreen = el => {
    const target = remoteExplorerPlayerRoot(el) || el;
    const requestTarget =
      target.requestFullscreen ||
      target.webkitRequestFullscreen ||
      target.mozRequestFullScreen ||
      target.msRequestFullscreen
        ? target
        : el;
    const request =
      requestTarget.requestFullscreen ||
      requestTarget.webkitRequestFullscreen ||
      requestTarget.mozRequestFullScreen ||
      requestTarget.msRequestFullscreen;
    if (!request) return false;
    request.call(requestTarget);
    return true;
  };
  const remoteExplorerExitFullscreen = () => {
    const exit =
      document.exitFullscreen ||
      document.webkitExitFullscreen ||
      document.mozCancelFullScreen ||
      document.msExitFullscreen;
    if (!exit) return false;
    exit.call(document);
    return true;
  };
  const remoteExplorerRunMediaAction = (media, action, amount) => {
    window.__remoteExplorerMedia = media;
    try {
      if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
      if (!media.hasAttribute("tabindex")) media.setAttribute("tabindex", "-1");
      if (media.focus) media.focus({ preventScroll: true });
    } catch (_) {}
    let controlled = true;
    let reason = "";
    if (action === "play_pause") {
      if (media.paused || media.ended) {
        const playResult = media.play();
        if (playResult && typeof playResult.catch === "function") playResult.catch(() => {});
      } else {
        media.pause();
      }
    } else if (action === "fullscreen") {
      controlled = remoteExplorerClickButton(remoteExplorerFindButton(media, [["fullscreen"], ["full", "screen"]]));
      reason = controlled ? "" : "keyboard_shortcut_required";
    } else if (action === "volume_up" || action === "volume_down") {
      const step = amount > 0 ? amount : 0.1;
      const delta = action === "volume_up" ? step : -step;
      media.volume = Math.max(0, Math.min(1, Number(media.volume || 0) + delta));
      if (action === "volume_up" && media.volume > 0) media.muted = false;
    } else if (action === "mute") {
      media.muted = !media.muted;
    } else if (action === "seek_forward" || action === "seek_back") {
      const step = amount > 0 ? amount : 10;
      const delta = action === "seek_forward" ? step : -step;
      const duration = Number.isFinite(media.duration) ? media.duration : Number.MAX_SAFE_INTEGER;
      media.currentTime = Math.max(0, Math.min(duration, Number(media.currentTime || 0) + delta));
    } else if (action === "next") {
      controlled = remoteExplorerClickButton(remoteExplorerFindButton(media, [["next"]]));
      if (!controlled) reason = "next_control_not_found";
    } else if (action === "previous") {
      controlled = remoteExplorerClickButton(remoteExplorerFindButton(media, [["previous"], ["prev"]]));
      if (!controlled) reason = "previous_control_not_found";
    } else {
      controlled = false;
      reason = "unsupported_media_action";
    }
    return {
      ...remoteExplorerMediaStatus(media),
      controlled,
      media_action: action,
      media_reason: reason
    };
  };
"""
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
            "media_status": self._media_status,
            "media_control": self._media_control,
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
{REMOTE_INPUT_HELPERS}
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

  const editable = remoteExplorerFocusEditable(
    remoteExplorerEditableFor(target) ||
    remoteExplorerEditableFor(el) ||
    remoteExplorerActiveEditable()
  );
  const inputType = editable && editable.getAttribute ? (editable.getAttribute("type") || "") : "";
  const inputValue = editable ? remoteExplorerReadEditableValue(editable) : "";
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

    def _send_native_key(
        self,
        key: Qt.Key,
        text: str = "",
        modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
    ) -> bool:
        target = self.view.focusProxy() or self.view
        if target is None:
            return False

        self.view.setFocus(Qt.FocusReason.OtherFocusReason)
        events = (
            QKeyEvent(QEvent.Type.KeyPress, key, modifiers, text),
            QKeyEvent(QEvent.Type.KeyRelease, key, modifiers, text),
        )
        for event in events:
            QCoreApplication.sendEvent(target, event)
        return True

    def _send_media_shortcut(self, action: str) -> bool:
        shortcuts: dict[str, tuple[Qt.Key, str, Qt.KeyboardModifier]] = {
            "fullscreen": (Qt.Key.Key_F, "f", Qt.KeyboardModifier.NoModifier),
            "next": (Qt.Key.Key_N, "N", Qt.KeyboardModifier.ShiftModifier),
            "previous": (Qt.Key.Key_P, "P", Qt.KeyboardModifier.ShiftModifier),
        }
        shortcut = shortcuts.get(action)
        if not shortcut:
            return False

        key, text, modifiers = shortcut
        return self._send_native_key(key, text, modifiers)

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
{REMOTE_INPUT_HELPERS}
  const el = remoteExplorerFocusEditable(remoteExplorerActiveEditable() || remoteExplorerStoredEditable());
  if (!el) return {{ inserted: false, reason: "no_active_element" }};
  if (!remoteExplorerInsertEditableText(el, text)) {{
    return {{ inserted: false, reason: "active_element_not_editable", tag: el.tagName }};
  }}
  el.dispatchEvent(new Event("change", {{ bubbles: true }}));
  return {{ inserted: true, mode: el.isContentEditable ? "contenteditable" : "input", tag: el.tagName }};
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
{REMOTE_INPUT_HELPERS}
  let el = null;
  if (selector) {{
    try {{
      el = document.querySelector(selector);
    }} catch (error) {{
      return {{ set: false, reason: "invalid_selector", selector, error: String(error && error.message || error) }};
    }}
  }} else {{
    el = remoteExplorerActiveEditable() || remoteExplorerStoredEditable();
  }}
  el = remoteExplorerFocusEditable(remoteExplorerEditableFor(el) || el);
  if (!el) return {{ set: false, reason: "not_found", selector }};
  if (!remoteExplorerSetEditableText(el, text)) {{
    return {{ set: false, reason: "element_not_editable", tag: el.tagName }};
  }}
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
{REMOTE_INPUT_HELPERS}
  const editable = remoteExplorerFocusEditable(remoteExplorerActiveEditable() || remoteExplorerStoredEditable());
  const el = editable || document.activeElement || document.body;
  const opts = {{ key, bubbles: true, cancelable: true }};
  el.dispatchEvent(new KeyboardEvent("keydown", opts));
  if (key === "Enter" && el.form && typeof el.form.requestSubmit === "function") el.form.requestSubmit();
  if (key === "Backspace" && editable) {{
    remoteExplorerBackspaceEditable(editable);
    editable.dispatchEvent(new Event("change", {{ bubbles: true }}));
  }}
  if (key === "Escape" && editable && typeof editable.blur === "function") {{
    editable.blur();
  }}
  el.dispatchEvent(new KeyboardEvent("keyup", opts));
  return {{ sent: true, key }};
}})();
"""
        self._run_js(script, respond)

    def _media_status(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        script = (
            "(() => {\n"
            + MEDIA_CONTROL_HELPERS
            + """
  const media = remoteExplorerFindMedia();
  return remoteExplorerMediaStatus(media);
})();
"""
        )
        self._run_js(script, respond)

    def _media_control(self, payload: dict[str, Any], respond: BrowserResponder) -> None:
        action = str(payload.get("action") or "")
        if action not in {
            "play_pause",
            "fullscreen",
            "volume_up",
            "volume_down",
            "mute",
            "seek_forward",
            "seek_back",
            "next",
            "previous",
        }:
            respond(_error("unsupported_media_action", f"Unsupported media action: {action}"))
            return

        amount = float(payload.get("amount", 0) or 0)
        script = (
            "(() => {\n"
            + MEDIA_CONTROL_HELPERS
            + f"""
  const action = {json.dumps(action)};
  const amount = Number({json.dumps(amount)}) || 0;
  const media = remoteExplorerFindMedia();
  if (!media) return {{ media_found: false, controlled: false, media_action: action, media_reason: "no_media" }};
  return remoteExplorerRunMediaAction(media, action, amount);
}})();
"""
        )

        def apply_shortcut_fallback(value: Any) -> Any:
            if not isinstance(value, dict):
                return value

            needs_shortcut = action == "fullscreen" or (
                action in {"next", "previous"} and not bool(value.get("controlled"))
            )
            if not needs_shortcut:
                return value

            if self._send_media_shortcut(action):
                value["controlled"] = True
                value["media_reason"] = "keyboard_shortcut"
                value["media_action"] = action
            return value

        self._run_js(script, respond, apply_shortcut_fallback)

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
