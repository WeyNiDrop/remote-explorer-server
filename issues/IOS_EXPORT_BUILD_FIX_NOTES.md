# iOS 导出工程构建修复记录

## 背景

当前 iOS 工程由 Unity/Tuanjie 导出。Xcode 构建时最初出现以下错误：

- `Unity/IUnityGraphics.h file not found`
- `Sandbox: chmod(...) deny(1) file-write-mode .../il2cpp`
- `Sandbox: chmod(...) deny(1) file-write-mode .../bee_backend`

修复后，`GameAssembly`、`TuanjieFramework` 和顶层 `Tuanjie-iPhone` Debug `iphoneos` 目标均可构建通过。

## 导出工程中的变更

1. WebRTC iOS 插件头文件路径兼容

   文件：`Libraries/com.unity.webrtc/Runtime/Plugins/iOS/RegisterPlugin.mm`

   WebRTC 插件原本固定包含：

   ```cpp
   #include "Unity/IUnityGraphics.h"
   ```

   但当前导出工程中的实际头文件位于：

   ```text
   Classes/Tuanjie/IUnityGraphics.h
   ```

   因此改为优先使用 Unity 路径，缺失时回退到 Tuanjie 路径。

2. 关闭 Xcode User Script Sandboxing

   文件：`Tuanjie-iPhone.xcodeproj/project.pbxproj`

   将相关配置中的 `ENABLE_USER_SCRIPT_SANDBOXING` 从 `YES` 改为 `NO`。

   原因是 `GameAssembly` 的 IL2CPP Run Script 会对导出的 `il2cpp` 和 `bee_backend` 执行 `chmod +x`。脚本沙盒开启时，Xcode 阻止该权限变更，导致 IL2CPP 构建阶段失败。

3. 关闭 TuanjieFramework Module Verifier

   文件：`Tuanjie-iPhone.xcodeproj/project.pbxproj`

   将 `TuanjieFramework` 各构建配置中的 `ENABLE_MODULE_VERIFIER` 从 `YES` 改为 `NO`。

   原因是前两个错误修复后，Xcode 的模块校验继续检查导出 framework 的公开头布局，并因 umbrella header、quoted include 和 `RedefinePlatforms.h` 的包含约束失败。该失败来自导出头文件结构与 Xcode module verifier 的要求不匹配，不是业务代码编译失败。

## 本仓库中的源头修复

已在 `client/Assets/RemoteExplorer/Editor/RemoteExplorerIosPostprocess.cs` 中把上述导出后手动修复固化为 iOS postprocess：

- 每次 iOS 导出后自动修补 WebRTC `RegisterPlugin.mm`，在 `Unity/IUnityGraphics.h` 缺失时回退到 `Tuanjie/IUnityGraphics.h`。
- 自动将导出 Xcode 工程中的 `ENABLE_USER_SCRIPT_SANDBOXING` 改为 `NO`，覆盖 `GameAssembly`、`TuanjieFramework` 和顶层 app target 中已生成的配置项。
- 自动将 `ENABLE_MODULE_VERIFIER` 改为 `NO`，避免 Tuanjie 导出 framework 的公开头布局被 Xcode module verifier 拦截。
- 自动为导出的 `AppIcon.appiconset` 补齐 App Store 需要的 `1024x1024` marketing icon。

## 建议在 Unity/Tuanjie 源头核查

- Unity WebRTC 插件在 Tuanjie 导出环境下是否应生成 `Tuanjie/IUnityGraphics.h` 引用，或同时提供 `Unity` 兼容头目录。
- iOS 导出模板是否应关闭 `ENABLE_USER_SCRIPT_SANDBOXING`，或在导出前就为 IL2CPP 工具保留可执行权限，避免构建脚本内再执行 `chmod`。
- iOS framework 导出模板是否应默认关闭 `ENABLE_MODULE_VERIFIER`，或调整公开头和 umbrella header 结构以满足 Xcode 模块校验。

## 当前仍存在的非阻塞警告

- `GameAssembly` 的 Run Script 未声明 outputs，因此每次构建都会执行。
