# Remote Explorer Unity Client

These scripts implement the first client slice:

- discover Python servers over UDP
- save password and auto-connect settings
- connect/authenticate
- open and close remote webpages
- run basic browser navigation commands

The UI is generated automatically at runtime. Add the scripts to a Unity project
and press Play in an empty scene.

Entry point:

- `RemoteExplorerBootstrap` creates `RemoteExplorerApp`.

Important files:

- `Scripts/RemoteExplorerClient.cs`: UDP protocol/auth/control client.
- `Scripts/RemoteExplorerApp.cs`: runtime UI and app flow.
- `Scripts/RemoteExplorerProtocol.cs`: JSON, signing, and protocol helpers.

## Mobile permissions

The current client only needs network access for LAN discovery, UDP control/stream
traffic, and WebRTC playback. It does not use camera, microphone, location,
photos, contacts, or storage permissions.

Android uses the custom main manifest in `Assets/Plugins/Android` to declare:

- internet access
- network state access

iOS build postprocessing adds the local-network usage description used by LAN
discovery, control, and streaming. The client no longer requests the multicast
networking entitlement because discovery is passive.
