# UDP Protocol

## Transport

- Encoding: UTF-8 JSON.
- Max recommended datagram size: 1200 bytes.
- Protocol version field: `v: 1`.
- Default discovery port: `45454`.
- Default control port: `45455`.

All requests should include:

```json
{
  "v": 1,
  "type": "command",
  "request_id": "unique-client-id"
}
```

## Discovery

Clients broadcast:

```json
{
  "v": 1,
  "type": "discover",
  "request_id": "abc",
  "client": {
    "id": "phone-1",
    "name": "Alice iPhone"
  }
}
```

Servers reply directly to the sender and may also broadcast periodic announces:

```json
{
  "v": 1,
  "type": "offer",
  "request_id": "abc",
  "server": {
    "id": "server-id",
    "name": "Living Room PC",
    "control_port": 45455,
    "auth": "password",
    "capabilities": ["navigate", "click", "text", "scroll", "status"]
  }
}
```

When no password is configured, `auth` is `none`.

## Authentication

### Start

Client sends to the control port:

```json
{
  "v": 1,
  "type": "auth_hello",
  "request_id": "auth-1",
  "client": {
    "id": "phone-1",
    "name": "Alice iPhone"
  }
}
```

If the server has no password, it responds with an active session:

```json
{
  "v": 1,
  "type": "auth_ok",
  "request_id": "auth-1",
  "session": {
    "id": "session-id",
    "auth": "none"
  }
}
```

If password mode is enabled:

```json
{
  "v": 1,
  "type": "auth_challenge",
  "request_id": "auth-1",
  "challenge": {
    "server_nonce": "hex",
    "salt": "hex",
    "iterations": 100000,
    "algorithm": "PBKDF2-HMAC-SHA256"
  }
}
```

Client derives:

```text
password_key = PBKDF2-HMAC-SHA256(password, salt, iterations)
proof = HMAC-SHA256(password_key, "proof:{client_id}:{server_nonce}:{client_nonce}")
session_key = HMAC-SHA256(password_key, "session:{client_id}:{server_nonce}:{client_nonce}")
```

Then sends:

```json
{
  "v": 1,
  "type": "auth_response",
  "request_id": "auth-2",
  "client_id": "phone-1",
  "server_nonce": "hex",
  "client_nonce": "hex",
  "proof": "hex"
}
```

Server replies:

```json
{
  "v": 1,
  "type": "auth_ok",
  "request_id": "auth-2",
  "session": {
    "id": "session-id",
    "auth": "hmac"
  }
}
```

## Signed Commands

Password sessions sign every command.

The command includes `auth.session_id`, `auth.counter`, and
`auth.signature`. The signature is computed over the canonical JSON message
after removing only `auth.signature`.

```json
{
  "v": 1,
  "type": "command",
  "request_id": "cmd-1",
  "command": "navigate",
  "payload": {
    "url": "https://example.com"
  },
  "auth": {
    "session_id": "session-id",
    "counter": 1,
    "signature": "hex"
  }
}
```

No-password sessions include only `auth.session_id`.

## Commands

### `navigate`

```json
{ "url": "https://example.com" }
```

### `click`

Coordinates are CSS viewport pixels.

```json
{ "x": 200, "y": 300, "source_width": 1280, "source_height": 720 }
```

`source_width` and `source_height` are optional. When present, the server maps
the received point from the streamed source viewport into the current browser
CSS viewport before dispatching click events.

### `click_selector`

```json
{ "selector": "button.play" }
```

### `text`

Types into the focused element.

```json
{ "text": "hello" }
```

### `set_input`

```json
{ "selector": "input[name=q]", "text": "search term", "submit": false }
```

### `scroll`

```json
{ "dx": 0, "dy": 600 }
```

### `key`

Initial supported keys: `Enter`, `Escape`, `Backspace`.

```json
{ "key": "Enter" }
```

### `close_page`

The current single-page browser view navigates to `about:blank`.

```json
{}
```

### `back`, `forward`, `reload`, `status`

Payload may be omitted or `{}`.

### `stream_start`

Starts a UDP JPEG frame stream to the requesting client. The server uses the
source IP of the control packet and the UDP `port` supplied by the client.

```json
{
  "port": 49152,
  "resolution": "360p",
  "fps": 30,
  "quality": 55
}
```

Supported `resolution` values:

- `360p`: 640x360, default
- `540p`: 960x540
- `720p`: 1280x720
- `1080p`: 1920x1080

`fps` is clamped to the supported 20-60 range. Default is 30.

The server may preserve the browser aspect ratio, so individual frames include
their actual image size.

### `stream_stop`

```json
{}
```

### `stream_config`

Updates stream resolution, FPS, or JPEG quality. If no stream is active, this
starts one.

```json
{
  "port": 49152,
  "resolution": "720p",
  "fps": 30,
  "quality": 60
}
```

### `stream_status`

```json
{}
```

### `webrtc_offer`

Negotiates a WebRTC/H.264 stream over the existing authenticated UDP control
channel. The Unity client creates a recv-only WebRTC offer and sends:

```json
{
  "type": "offer",
  "sdp": "v=0...",
  "resolution": "720p",
  "fps": 30
}
```

The server answers:

```json
{
  "transport": "webrtc",
  "codec": "h264",
  "peer_id": "hex",
  "type": "answer",
  "sdp": "v=0...",
  "resolution": "720p",
  "width": 1280,
  "height": 720,
  "fps": 30
}
```

Server WebRTC support is optional and requires the Python `webrtc` extra
dependencies. If they are missing, this command returns a normal command error
and the WebRTC branch reports the failure directly.

### `webrtc_stop`

```json
{ "peer_id": "hex" }
```

An empty `peer_id` closes all active WebRTC peers.

### `webrtc_status`

```json
{}
```

## Frame Stream Packet

Frame data is sent as binary UDP datagrams.

Header layout, big-endian:

```text
0   8 bytes   magic: "REXPSTR1"
8   uint32    frame id
12  uint16    chunk index
14  uint16    chunk count
16  uint16    JPEG image width
18  uint16    JPEG image height
20  uint16    browser source viewport width
22  uint16    browser source viewport height
24  bytes     JPEG chunk payload
```

Clients reassemble chunks with the same frame id. Incomplete frames should be
dropped rather than blocking newer frames.

## Command Result

```json
{
  "v": 1,
  "type": "result",
  "request_id": "cmd-1",
  "ok": true,
  "result": {
    "url": "https://example.com",
    "title": "Example Domain"
  }
}
```

Errors:

```json
{
  "v": 1,
  "type": "error",
  "request_id": "cmd-1",
  "ok": false,
  "error": {
    "code": "unknown_command",
    "message": "Unknown command: foo"
  }
}
```
