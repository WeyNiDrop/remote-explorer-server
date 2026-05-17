# Unity Client Plan

## Goal

Build a Unity client that can run on desktop, Android, and iOS, discover the
LAN browser server, connect with an optional password, control remote webpages,
show the current browser content as a stream, and eventually map video-player
controls to a TV-style remote.

## Stage 1: Discovery, Auth, and Webpage Control

Status: implemented in this slice.

Features:

- UDP broadcast discovery.
- Server list with host, port, name, auth mode, and server id.
- Password input.
- Password and last server saved with Unity `PlayerPrefs`.
- Optional auto-connect on app launch.
- Password challenge-response auth compatible with the Python server.
- Signed UDP commands for password sessions.
- Remote commands:
  - open URL
  - close current page
  - back
  - forward
  - reload
  - status

Notes:

- Password storage currently uses `PlayerPrefs`. This is acceptable for the
  first LAN prototype, but a production mobile client should use Android
  Keystore / iOS Keychain.
- The UI is generated at runtime so an empty Unity scene can run it.

## Stage 2: Browser Content Streaming

Status: implemented as an MVP.

Server side:

- Screenshot/streaming producer from the embedded browser.
- Default stream size: 640x360.
- User-selectable sizes up to 1920x1080.
- Stream FPS defaults to 30 and is configurable from 20-60.
- Stream start/stop/config UDP commands.
- JPEG frames over UDP chunks.
- Optional WebRTC/H.264 offer/answer commands are available on the server when
  the Python `webrtc` extra dependencies are installed.

Client side:

- Preview surface.
- Connected sessions switch into a remote-control page where the stream takes
  the available space and command controls stay compact.
- Frame reassembly.
- Tap on preview maps to remote browser coordinates and sends pointer/mouse
  events to the page element under that point.
- Resolution selector:
  - 360p default
  - 540p
  - 720p
  - 1080p
- FPS slider:
  - 30fps default
  - 20-60fps range
  - hot-applies through `stream_config` while the UDP stream is running
- Unity WebRTC/H.264 playback is implemented behind the
  `REMOTE_EXPLORER_HAS_WEBRTC` package version define. It is enabled when
  `com.unity.webrtc@3.0.0-pre.8` resolves. In the WebRTC branch, negotiation
  failure is reported directly instead of falling back to UDP/JPEG.

Known MVP limits:

- UDP frame chunks are best-effort. Lost chunks drop a frame.
- 1080p can generate many datagrams per frame; use 360p/540p first on weak Wi-Fi.
- If JPEG/UDP is not smooth enough, use the WebRTC branch while keeping the
  same control protocol.

## Stage 3: Player Capture and TV Controls

Planned after streaming is usable.

Server side:

- Detect media elements and known-player controls.
- Expose media status:
  - playing
  - duration
  - current time
  - volume
  - fullscreen state
  - available next/previous actions
- Add media commands:
  - play/pause
  - next
  - previous
  - seek
  - volume
  - fullscreen toggle

Client side:

- Add TV-style buttons:
  - play/pause
  - next
  - previous
  - volume
  - progress slider
  - fullscreen/exit fullscreen
- Poll or subscribe to media status so the UI reflects the page state.

## Recommended Test Order

1. Launch the Python server without password and verify discovery.
2. Connect and run status/open/close/back/forward/reload from Unity.
3. Relaunch the server with a password and verify auth.
4. Enable auto-connect, restart Unity, and verify it reconnects.
5. Test Android and iOS LAN discovery after platform permission setup.
