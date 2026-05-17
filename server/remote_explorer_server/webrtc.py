from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from concurrent.futures import Future
from typing import Any

from PySide6.QtCore import QObject, QSize, Qt, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtWebEngineWidgets import QWebEngineView

from .streaming import (
    DEFAULT_STREAM_FPS,
    MAX_STREAM_FPS,
    MAX_STREAM_HEIGHT,
    MAX_STREAM_WIDTH,
    MIN_STREAM_FPS,
    RESOLUTIONS,
    _clamp_int,
)

WEBRTC_LOG_INTERVAL_SECONDS = 5.0
LOGGER = logging.getLogger("remote_explorer.webrtc")

try:
    import av
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import VideoStreamTrack
    from aiortc.rtcrtpsender import RTCRtpSender
except Exception as exc:  # pragma: no cover - exercised only when optional deps are present.
    WEBRTC_IMPORT_ERROR: Exception | None = exc
    av = None  # type: ignore[assignment]
    RTCPeerConnection = None  # type: ignore[assignment]
    RTCSessionDescription = None  # type: ignore[assignment]
    VideoStreamTrack = object  # type: ignore[assignment,misc]
    RTCRtpSender = None  # type: ignore[assignment]
else:
    WEBRTC_IMPORT_ERROR = None


class BrowserWebRtcService(QObject):
    stop_capture_requested = Signal()
    # 跨线程抓图请求必须回到 Qt 主线程执行。 / Cross-thread capture requests must run on the Qt main thread.
    capture_requested = Signal(object)

    def __init__(self, view: QWebEngineView, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.view = view
        self.stop_capture_requested.connect(self._stop_capture_if_idle)
        self.capture_requested.connect(self._capture_requested)
        self.capture_width = 640
        self.capture_height = 360
        self.capture_fps = DEFAULT_STREAM_FPS
        self.latest_frame: tuple[int, int, int, bytes] | None = None
        self.frame_lock = threading.Lock()
        self.peer_lock = threading.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.loop_thread: threading.Thread | None = None
        self.loop_ready = threading.Event()
        self.peer_connections: dict[str, Any] = {}
        self.stats_lock = threading.Lock()
        self._reset_stats()

    def offer(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.offer_async(payload).result(timeout=10)

    def offer_async(self, payload: dict[str, Any]) -> Future[dict[str, Any]]:
        self._ensure_available()
        sdp = str(payload.get("sdp") or "")
        offer_type = str(payload.get("type") or "offer")
        if not sdp:
            raise ValueError("WebRTC offer SDP is required")

        width, height, resolution = _resolve_webrtc_size(payload)
        fps = _clamp_int(payload.get("fps"), MIN_STREAM_FPS, MAX_STREAM_FPS, DEFAULT_STREAM_FPS)
        LOGGER.info(
            "WebRTC offer received resolution=%s size=%sx%s fps=%s sdp_bytes=%s",
            resolution,
            width,
            height,
            fps,
            len(sdp),
        )
        self._start_capture(width, height, fps)
        try:
            loop = self._ensure_loop()
            return asyncio.run_coroutine_threadsafe(
                self._create_answer(
                    sdp=sdp,
                    offer_type=offer_type,
                    resolution=resolution,
                    fps=fps,
                    width=width,
                    height=height,
                ),
                loop,
            )
        except Exception:
            self.stop_capture_requested.emit()
            raise

    def stop(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.stop_async(payload).result(timeout=5)

    def stop_async(self, payload: dict[str, Any] | None = None) -> Future[dict[str, Any]]:
        payload = payload or {}
        peer_id = str(payload.get("peer_id") or "")
        LOGGER.info("WebRTC stop requested peer_id=%s", peer_id or "all")
        loop = self._ensure_loop()

        async def stop_and_report() -> dict[str, Any]:
            await self._close_peers(peer_id)
            return self.status()

        return asyncio.run_coroutine_threadsafe(stop_and_report(), loop)

    def status(self) -> dict[str, Any]:
        return {
            "available": WEBRTC_IMPORT_ERROR is None,
            "error": str(WEBRTC_IMPORT_ERROR) if WEBRTC_IMPORT_ERROR else "",
            "transport": "webrtc",
            "codec": "h264",
            "peers": self._peer_count(),
            "fps": self.capture_fps,
            "width": self.capture_width,
            "height": self.capture_height,
            "min_fps": MIN_STREAM_FPS,
            "max_fps": MAX_STREAM_FPS,
        }

    def has_peers(self) -> bool:
        return self._peer_count() > 0

    def frame_snapshot(self) -> tuple[int, int, int, bytes] | None:
        with self.frame_lock:
            return self.latest_frame

    def _ensure_available(self) -> None:
        if WEBRTC_IMPORT_ERROR is not None:
            raise RuntimeError(
                "WebRTC/H.264 streaming requires optional server dependencies: "
                "aiortc and PyAV with an H.264 encoder. "
                f"Import error: {WEBRTC_IMPORT_ERROR}"
            )

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self.loop and self.loop.is_running():
            return self.loop

        self.loop_ready.clear()
        self.loop_thread = threading.Thread(target=self._run_loop, name="RemoteExplorerWebRTC", daemon=True)
        self.loop_thread.start()
        if not self.loop_ready.wait(timeout=5) or self.loop is None:
            raise RuntimeError("Could not start WebRTC event loop")
        return self.loop

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self.loop_ready.set()
        LOGGER.info("WebRTC event loop started")
        loop.run_forever()

    def _start_capture(self, width: int, height: int, fps: int) -> None:
        self.capture_width = width
        self.capture_height = height
        self.capture_fps = fps
        self._reset_stats()
        LOGGER.info("WebRTC capture start size=%sx%s fps=%s", width, height, fps)
        self._capture_frame()

    def _capture_frame(self) -> None:
        self._capture_frame_data()

    def _capture_frame_data(self) -> tuple[int, int, int, bytes] | None:
        capture_started_at = time.perf_counter()
        pixmap = self.view.grab()
        if pixmap.isNull():
            self._record_capture(empty=True, capture_ms=0.0, width=0, height=0)
            return None

        scaled = pixmap.scaled(
            QSize(self.capture_width, self.capture_height),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        if scaled.isNull():
            self._record_capture(empty=True, capture_ms=0.0, width=0, height=0)
            return None

        image = scaled.toImage().convertToFormat(QImage.Format.Format_RGB888)
        width = max(1, image.width())
        height = max(1, image.height())
        stride = max(width * 3, image.bytesPerLine())
        data = bytes(image.constBits())
        frame_data = (width, height, stride, data)
        with self.frame_lock:
            self.latest_frame = frame_data
        capture_ms = (time.perf_counter() - capture_started_at) * 1000.0
        self._record_capture(empty=False, capture_ms=capture_ms, width=width, height=height)
        return frame_data

    async def capture_frame_async(self, timeout: float = 0.2) -> tuple[int, int, int, bytes] | None:
        # aiortc 的 RTP 发送线程拉帧时，同步请求 Qt 线程抓一张最新画面。 / When aiortc pulls a frame, ask Qt for a fresh capture.
        request = _CaptureRequest()
        self.capture_requested.emit(request)
        await asyncio.to_thread(request.ready.wait, timeout)
        return request.frame_data

    @Slot(object)
    def _capture_requested(self, request: object) -> None:
        if not isinstance(request, _CaptureRequest):
            return

        request.frame_data = self._capture_frame_data()
        request.ready.set()

    async def _create_answer(
        self,
        *,
        sdp: str,
        offer_type: str,
        resolution: str,
        fps: int,
        width: int,
        height: int,
    ) -> dict[str, Any]:
        peer_id = uuid.uuid4().hex
        pc = RTCPeerConnection()  # type: ignore[misc]
        with self.peer_lock:
            self.peer_connections[peer_id] = pc
        track = BrowserVideoTrack(self, fps)
        LOGGER.info("WebRTC peer create peer_id=%s resolution=%s size=%sx%s fps=%s", peer_id, resolution, width, height, fps)

        try:
            @pc.on("connectionstatechange")
            async def on_connectionstatechange() -> None:
                LOGGER.info("WebRTC peer state peer_id=%s state=%s", peer_id, pc.connectionState)
                if pc.connectionState in {"failed", "closed", "disconnected"}:
                    await self._close_peer(peer_id)

            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=offer_type))  # type: ignore[misc]
            sender = pc.addTrack(track)
            _prefer_h264(pc, sender)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            await _wait_for_ice_gathering(pc)
            LOGGER.info(
                "WebRTC answer ready peer_id=%s ice=%s type=%s sdp_bytes=%s",
                peer_id,
                pc.iceGatheringState,
                pc.localDescription.type,
                len(pc.localDescription.sdp),
            )

            return {
                "transport": "webrtc",
                "codec": "h264",
                "peer_id": peer_id,
                "type": pc.localDescription.type,
                "sdp": pc.localDescription.sdp,
                "resolution": resolution,
                "width": width,
                "height": height,
                "fps": fps,
            }
        except Exception:
            await self._close_peer(peer_id)
            raise

    async def _close_peers(self, peer_id: str = "") -> None:
        if peer_id:
            await self._close_peer(peer_id)
            return

        with self.peer_lock:
            peer_ids = list(self.peer_connections.keys())

        for current_peer_id in peer_ids:
            await self._close_peer(current_peer_id)

    async def _close_peer(self, peer_id: str) -> None:
        with self.peer_lock:
            pc = self.peer_connections.pop(peer_id, None)
        if pc:
            LOGGER.info("WebRTC peer close peer_id=%s", peer_id)
            await pc.close()
        self._request_capture_stop_if_idle()

    def _peer_count(self) -> int:
        with self.peer_lock:
            return len(self.peer_connections)

    def _request_capture_stop_if_idle(self) -> None:
        if self._peer_count() == 0:
            self.stop_capture_requested.emit()

    @Slot()
    def _stop_capture_if_idle(self) -> None:
        if self._peer_count() == 0:
            self._log_stats("capture_stop", force=True)
            LOGGER.info("WebRTC capture stop: no active peers")

    def _reset_stats(self) -> None:
        now = time.monotonic()
        with self.stats_lock:
            self.stats_since = now
            self.next_stats_log_at = now + WEBRTC_LOG_INTERVAL_SECONDS
            self.captures = 0
            self.empty_captures = 0
            self.track_frames = 0
            self.empty_track_frames = 0
            self.capture_ms_total = 0.0
            self.capture_ms_max = 0.0
            self.last_capture_size = "none"

    def _record_capture(self, *, empty: bool, capture_ms: float, width: int, height: int) -> None:
        with self.stats_lock:
            if empty:
                self.empty_captures += 1
            else:
                self.captures += 1
                self.capture_ms_total += capture_ms
                self.capture_ms_max = max(self.capture_ms_max, capture_ms)
                self.last_capture_size = f"{width}x{height}"
        self._log_stats("interval")

    def record_track_frame(self, *, empty: bool) -> None:
        with self.stats_lock:
            self.track_frames += 1
            if empty:
                self.empty_track_frames += 1
        self._log_stats("interval")

    def _log_stats(self, reason: str, force: bool = False) -> None:
        now = time.monotonic()
        with self.stats_lock:
            if not force and now < self.next_stats_log_at:
                return

            elapsed = max(0.001, now - self.stats_since)
            captures = self.captures
            empty_captures = self.empty_captures
            track_frames = self.track_frames
            empty_track_frames = self.empty_track_frames
            capture_avg = self.capture_ms_total / captures if captures else 0.0
            capture_max = self.capture_ms_max
            last_capture_size = self.last_capture_size
            peers = self._peer_count()

            self.stats_since = now
            self.next_stats_log_at = now + WEBRTC_LOG_INTERVAL_SECONDS
            self.captures = 0
            self.empty_captures = 0
            self.track_frames = 0
            self.empty_track_frames = 0
            self.capture_ms_total = 0.0
            self.capture_ms_max = 0.0

        LOGGER.info(
            "WebRTC stats reason=%s elapsed=%.1fs peers=%s captures=%s capture_fps=%.1f "
            "empty_captures=%s track_frames=%s track_fps=%.1f empty_track_frames=%s "
            "capture_ms=%.1f/%.1f last_capture=%s",
            reason,
            elapsed,
            peers,
            captures,
            captures / elapsed,
            empty_captures,
            track_frames,
            track_frames / elapsed,
            empty_track_frames,
            capture_avg,
            capture_max,
            last_capture_size,
        )


class BrowserVideoTrack(VideoStreamTrack):  # type: ignore[misc]
    kind = "video"

    def __init__(self, service: BrowserWebRtcService, fps: int) -> None:
        super().__init__()
        self.service = service
        self.fps = max(MIN_STREAM_FPS, min(MAX_STREAM_FPS, fps))

    async def recv(self) -> Any:
        # 用 aiortc 的时间戳节奏驱动编码，同时每帧主动抓新画面。 / Let aiortc pace timestamps and actively capture each frame.
        pts, time_base = await self.next_timestamp()
        frame_data = await self.service.capture_frame_async(timeout=max(0.1, 2 / self.fps))
        if frame_data is None:
            frame_data = self.service.frame_snapshot()
            if frame_data is None:
                width, height, stride, data = 2, 2, 6, bytes(12)
                self.service.record_track_frame(empty=True)
            else:
                width, height, stride, data = frame_data
                self.service.record_track_frame(empty=False)
        else:
            width, height, stride, data = frame_data
            self.service.record_track_frame(empty=False)

        frame = av.VideoFrame(width, height, "rgb24")  # type: ignore[union-attr]
        plane = frame.planes[0]
        row_bytes = width * 3
        if stride == row_bytes and plane.line_size == row_bytes:
            plane.update(data[: row_bytes * height])
        else:
            output = bytearray(plane.line_size * height)
            for row in range(height):
                src_start = row * stride
                dst_start = row * plane.line_size
                output[dst_start : dst_start + row_bytes] = data[src_start : src_start + row_bytes]
            plane.update(output)
        frame.pts = pts
        frame.time_base = time_base
        return frame


class _CaptureRequest:
    # Qt signal 传递的小对象，用 Event 把抓图结果交回 aiortc 线程。 / Signal payload that returns capture data to the aiortc thread.
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.frame_data: tuple[int, int, int, bytes] | None = None


def _prefer_h264(pc: Any, sender: Any) -> None:
    if RTCRtpSender is None:
        return

    capabilities = RTCRtpSender.getCapabilities("video")
    h264_codecs = [codec for codec in capabilities.codecs if codec.mimeType.lower() == "video/h264"]
    if not h264_codecs:
        return

    for transceiver in pc.getTransceivers():
        if transceiver.sender == sender:
            rtx_codecs = [codec for codec in capabilities.codecs if codec.mimeType.lower() == "video/rtx"]
            transceiver.setCodecPreferences(h264_codecs + rtx_codecs)
            return


async def _wait_for_ice_gathering(pc: Any) -> None:
    if pc.iceGatheringState == "complete":
        return

    complete = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def on_icegatheringstatechange() -> None:
        if pc.iceGatheringState == "complete":
            complete.set()

    try:
        await asyncio.wait_for(complete.wait(), timeout=2)
    except TimeoutError:
        pass


def _resolve_webrtc_size(payload: dict[str, Any]) -> tuple[int, int, str]:
    resolution = str(payload.get("resolution") or "360p").lower()
    if resolution in RESOLUTIONS:
        width, height = RESOLUTIONS[resolution]
        return width, height, resolution

    width = _clamp_int(payload.get("width"), 1, MAX_STREAM_WIDTH, 640)
    height = _clamp_int(payload.get("height"), 1, MAX_STREAM_HEIGHT, 360)
    return width, height, f"{height}p"
