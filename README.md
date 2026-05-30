# Remote Explorer

Language: [English](README.md) | [简体中文](README.zh-CN.md)

Remote Explorer is a LAN-first remote browser controller.

The initial version provides a Python/PySide6 desktop server with an embedded
Chromium browser, UDP discovery, optional password authentication, and a UDP
control protocol. Mobile clients for Android and iOS can use the documented
protocol; a Python development client is included for testing the first slice.

## Current Scope

- Cross-platform desktop server for Windows and macOS.
- Embedded browser with persistent cookies/cache/profile data.
- Server-announced UDP discovery on the LAN.
- UDP control channel for navigation, click/tap, text input, scroll, back,
  forward, reload, and selector-based actions.
- Supplemental H5 client served by the desktop server for QR-code pairing.
- Optional password authentication using a challenge-response handshake.
- Python CLI client for local/LAN development.

## Quick Start

macOS/Linux Terminal:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[server,dev]"
python -m remote_explorer_server --password 123456
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[server,dev]"
python -m remote_explorer_server --password 123456
```

The server uses a Chromium-family browser engine when Chrome, Edge, or Chromium
is installed. This improves compatibility with mainstream HTML5 video sites. To
point to a browser executable:

macOS/Linux Terminal:

```bash
python -m remote_explorer_server --browser-engine chromium --browser-executable "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

Windows PowerShell:

```powershell
python -m remote_explorer_server --browser-engine chromium --browser-executable "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

In another Terminal or PowerShell window:

```bash
python tools/dev_client.py discover
python tools/dev_client.py navigate --host 127.0.0.1 --password 123456 https://example.com
python tools/dev_client.py click --host 127.0.0.1 --password 123456 200 300
python tools/dev_client.py text --host 127.0.0.1 --password 123456 "hello"
```

## H5 Client

The desktop server also starts a lightweight H5 client over TCP. The control
panel shows a QR code and URL; scanning it opens the web client on the same LAN.
This is supplemental and does not replace the native UDP/Unity client path.
At startup the server tries to register a local mDNS hostname from the machine
name, using lowercase ASCII and removing spaces, for example
`livingroompc.local`. If local-domain registration is unavailable, the QR code
falls back to the LAN IP URL.

By default the H5 service uses the same numeric port as the UDP control service
(TCP and UDP can share a port). To choose a different TCP port:

```bash
python -m remote_explorer_server --password 123456 --web-port 8080
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
- Stream diagnostics are logged every 5 seconds while active. Server logs go to
  the server console through an asynchronous logger. Unity client diagnostics
  are written to `remote-explorer-stream.log` under `Application.persistentDataPath`;
  the Unity Console prints that path once at startup.
- Tap preview to click the remote webpage. Swipe the preview to scroll the page.
- Tapping a webpage input opens the Unity-side input field and writes the text
  back to the focused remote element.
- The client can auto-detect the current page player and expose TV-remote
  controls for play/pause, fullscreen, volume, seeking, next, and previous.
- Video fullscreen is handled by the server browser while the active client
  preview stream keeps running, so remote control can continue after exiting
  fullscreen.

The client UI is created at runtime by `RemoteExplorerBootstrap`, so an empty
Unity scene can be used directly.

Recommended first test:

1. Start the Python server:

   macOS/Linux Terminal:

   ```bash
   PYTHONPATH=server python -m remote_explorer_server --password 123456
   ```

   Windows PowerShell:

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

```bash
python -m remote_explorer_server
python tools/dev_client.py navigate --host 127.0.0.1 https://example.com
```

## Ports

- Discovery UDP port: `45454`
- Control UDP port: `45455`

Both can be changed with command-line flags.

## Packaging

Server packages are built by `.github/workflows/package-server.yml`.

- Windows uploads `remote-explorer-server.exe`.
- macOS publishes one zip containing `Remote Explorer Server.app`, so
  double-clicking opens a normal macOS app bundle instead of a terminal-style
  command executable.
- macOS builds are split into `macos-arm64` for Apple Silicon and
  `macos-x86_64` for Intel Macs. The Intel build sets
  `MACOSX_DEPLOYMENT_TARGET=11.0`.
- `remote-explorer-server-macos-catalina-x86_64` is a separate legacy Intel
  package for macOS 10.15. It builds on the supported Intel macOS runner with
  `MACOSX_DEPLOYMENT_TARGET=10.15`, Python 3.11, and
  `requirements-macos-catalina.txt` so older Qt/PySide dependencies do not
  affect the normal macOS, Windows, or Linux packages.
- Linux uploads the `remote-explorer-server` executable.

With the current default PySide6 dependency, macOS 11 is the intended minimum
for normal packaged macOS apps. Use the Catalina artifact only when macOS 10.15
support is required.

## Documentation

- [Project plan](docs/PROJECT_PLAN.md)
- [UDP protocol](docs/PROTOCOL.md)

## Notes

UDP traffic is LAN-scoped and not encrypted. Password mode avoids sending the
password itself over the network, but packets and session tokens are still
visible to devices on the same network. Treat the first release as a trusted-LAN
tool.
