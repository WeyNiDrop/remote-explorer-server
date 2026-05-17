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
