# Build Helpers

The editor script adds these menu items:

- `Remote Explorer/Ensure Build Scene`
- `Remote Explorer/Build Android Development`
- `Remote Explorer/Build iOS Development`

Batch mode examples:

```powershell
Unity.exe -batchmode -quit -projectPath "E:\workspace\remote-explorer\client" -executeMethod RemoteExplorer.Editor.RemoteExplorerBuild.BuildAndroidDevelopment
```

For Tuanjie Engine, use the same arguments with the Tuanjie editor executable.
The C# runtime API used by the client remains `UnityEngine` / `UnityEditor`.
