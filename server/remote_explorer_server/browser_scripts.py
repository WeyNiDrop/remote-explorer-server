from __future__ import annotations


CHROME_COMPAT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.7778.168 Safari/537.36"
)
CHROME_COMPAT_ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7"

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
      "璇煶",
      "瑾為煶",
      "楹﹀厠椋?,
      "楹ュ厠棰?,
      "鎼滅储",
      "鎼滃皨",
      "褰曢煶",
      "閷勯煶"
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
    } else if (action === "exit_fullscreen") {
      controlled = remoteExplorerExitFullscreen();
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
