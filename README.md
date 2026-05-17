# Remote Explorer

Remote Explorer is a LAN-first remote browser controller.

The initial version provides a Python/PySide6 desktop server with an embedded
Chromium browser, UDP discovery, optional password authentication, and a UDP
control protocol. Mobile clients for Android and iOS can use the documented
protocol; a Python development client is included for testing the first slice.

## Current Scope

- Cross-platform desktop server for Windows and macOS.
- Embedded browser with persistent cookies/cache/profile data.
- UDP broadcast discovery on the LAN.
- UDP control channel for navigation, click/tap, text input, scroll, back,
  forward, reload, and selector-based actions.
- Optional password authentication using a challenge-response handshake.
- Python CLI client for local/LAN development.

## Quick Start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[server,dev]"
python -m remote_explorer_server --password 123456
```

For server-side WebRTC/H.264 support, install the optional WebRTC extra:

```powershell
pip install -e ".[server,webrtc,dev]"
```

In another terminal:

```powershell
python tools/dev_client.py discover
python tools/dev_client.py navigate --host 127.0.0.1 --password 123456 https://example.com
python tools/dev_client.py click --host 127.0.0.1 --password 123456 200 300
python tools/dev_client.py text --host 127.0.0.1 --password 123456 "hello"
```

## Unity Client

The Unity client slice lives under `client/Assets/RemoteExplorer`.

Implemented client features:

- UDP server discovery.
- Password entry and saved auto-connect settings.
- Password authentication and signed commands.
- Remote open/close page, back, forward, reload, and status commands.
- UDP JPEG browser preview stream with 360p/540p/720p/1080p options.
- Stream FPS defaults to 30 and can be configured from 20-60 in the client.
- Server-side WebRTC/H.264 offer/answer support is available through the
  optional Python `webrtc` extra; Unity playback requires adding Unity WebRTC to
  the client project, for example `com.unity.webrtc@3.0.0-pre.8`.
- Tap preview to click the remote webpage.

The client UI is created at runtime by `RemoteExplorerBootstrap`, so an empty
Unity scene can be used directly.

Recommended first test:

1. Start the Python server:

   ```powershell
   $env:PYTHONPATH='server'
   python -m remote_explorer_server --password 123456
   ```

2. Open the Unity project that contains `client/Assets/RemoteExplorer`.
3. Press Play.
4. Click `Discover`, select the server, enter the password, then click
   `Connect`.
5. Enter a URL and use `Open` / `Close Page`.

Without a password:

```powershell
python -m remote_explorer_server
python tools/dev_client.py navigate --host 127.0.0.1 https://example.com
```

## Ports

- Discovery UDP port: `45454`
- Control UDP port: `45455`

Both can be changed with command-line flags.

## Documentation

- [Project plan](docs/PROJECT_PLAN.md)
- [UDP protocol](docs/PROTOCOL.md)

## Notes

UDP traffic is LAN-scoped and not encrypted. Password mode avoids sending the
password itself over the network, but packets and session tokens are still
visible to devices on the same network. Treat the first release as a trusted-LAN
tool.
