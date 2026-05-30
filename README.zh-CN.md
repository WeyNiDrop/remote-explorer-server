# Remote Explorer

语言：[English](README.md) | [简体中文](README.zh-CN.md)

Remote Explorer 是一个面向局域网的远程浏览器控制工具。

当前版本提供 Python/PySide6 桌面服务端，内置 Chromium 浏览器，支持 UDP 发现、可选密码认证、UDP 控制协议，以及作为补充方案的 H5 轻量客户端。Android 和 iOS 原生客户端可以使用文档化协议接入；仓库中也包含一个 Python 开发客户端用于调试。

## 当前范围

- 跨平台桌面服务端，支持 Windows 和 macOS。
- 内置浏览器，支持持久化 cookies、缓存和 profile 数据。
- 服务端在局域网内广播 UDP 发现信息。
- UDP 控制通道，支持导航、点击/触摸、文本输入、滚动、后退、前进、刷新和 selector 操作。
- 服务端内置 H5 客户端，可通过二维码配对并在浏览器中控制。
- 可选密码认证，使用 challenge-response 握手。
- Python CLI 客户端用于本地和局域网开发调试。

## 快速开始

macOS/Linux 终端：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[server,dev]"
python -m remote_explorer_server --password 123456
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[server,dev]"
python -m remote_explorer_server --password 123456
```

当系统已安装 Chrome、Edge 或 Chromium 时，服务端会使用 Chromium 系浏览器引擎，以提高主流 HTML5 视频网站的兼容性。也可以手动指定浏览器可执行文件：

macOS/Linux 终端：

```bash
python -m remote_explorer_server --browser-engine chromium --browser-executable "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

Windows PowerShell：

```powershell
python -m remote_explorer_server --browser-engine chromium --browser-executable "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

在另一个终端或 PowerShell 窗口中：

```bash
python tools/dev_client.py discover
python tools/dev_client.py navigate --host 127.0.0.1 --password 123456 https://example.com
python tools/dev_client.py click --host 127.0.0.1 --password 123456 200 300
python tools/dev_client.py text --host 127.0.0.1 --password 123456 "hello"
```

## H5 客户端

桌面服务端会同时启动一个基于 TCP 的轻量 H5 客户端。服务端控制面板会展示二维码和访问地址；用户扫描二维码即可在同一局域网内打开网页客户端。

H5 客户端是补充方案，不替代原生 UDP/Unity 客户端路径。服务端启动时会尝试根据机器名注册本地 mDNS 域名，域名会转换为小写 ASCII 并移除空格，例如 `livingroompc.local`。如果本地域名注册不可用，二维码会回退到局域网 IP 地址。

默认情况下，H5 服务使用与 UDP 控制服务相同的数字端口。TCP 和 UDP 可以共用同一个端口号。如需指定不同的 TCP 端口：

```bash
python -m remote_explorer_server --password 123456 --web-port 8080
```

## Unity 客户端

Unity 客户端位于 `client/Assets/RemoteExplorer`。

已实现的客户端能力：

- UDP 服务端发现。
- 密码输入和已保存的自动连接设置。
- 密码认证和签名命令。
- 远程打开/关闭页面、后退、前进、刷新和状态命令。
- UDP JPEG 浏览器预览串流，支持 360p/540p/720p/1080p。
- 串流 FPS 默认 30，可在客户端中配置为 20-60。
- 串流诊断信息每 5 秒记录一次。服务端日志进入服务端控制台；Unity 客户端诊断日志写入 `Application.persistentDataPath` 下的 `remote-explorer-stream.log`，Unity Console 会在启动时打印该路径。
- 点击预览画面可点击远程网页；在预览画面上滑动可滚动页面。
- 点击网页输入框会打开 Unity 侧输入栏，并把文本写回远程焦点元素。
- 客户端可以自动检测当前页面播放器，并提供播放/暂停、全屏、音量、快进/快退、下一项和上一项等电视遥控器式控制。
- 视频全屏由服务端浏览器处理；全屏期间活动客户端预览串流保持运行，因此退出全屏后仍可继续远程控制。

客户端 UI 由 `RemoteExplorerBootstrap` 在运行时创建，因此可以直接使用空 Unity 场景。

推荐首次测试流程：

1. 启动 Python 服务端：

   macOS/Linux 终端：

   ```bash
   PYTHONPATH=server python -m remote_explorer_server --password 123456
   ```

   Windows PowerShell：

   ```powershell
   $env:PYTHONPATH='server'
   python -m remote_explorer_server --password 123456
   ```

2. 打开包含 `client/Assets/RemoteExplorer` 的 Unity 项目。
3. 点击 Play。
4. 点击 `Discover`，选择服务端，输入密码，然后点击 `Connect`。
5. 输入网址，并使用 `Open` / `Close Page`。

不使用密码时：

```bash
python -m remote_explorer_server
python tools/dev_client.py navigate --host 127.0.0.1 https://example.com
```

## 端口

- 发现 UDP 端口：`45454`
- 控制 UDP 端口：`45455`

两个端口都可以通过命令行参数修改。

## 打包

服务端打包由 `.github/workflows/package-server.yml` 执行。

- Windows 上传 `remote-explorer-server.exe`。
- macOS 发布包含 `Remote Explorer Server.app` 的 zip，双击后会像普通 macOS App 一样打开，而不是作为终端命令运行。
- macOS 构建拆分为 Apple Silicon 的 `macos-arm64` 和 Intel Mac 的 `macos-x86_64`。Intel 构建设置 `MACOSX_DEPLOYMENT_TARGET=11.0`。
- `remote-explorer-server-macos-catalina-x86_64` 是单独的 legacy Intel 包，用于 macOS 10.15。它在受支持的 Intel macOS runner 上构建，设置 `MACOSX_DEPLOYMENT_TARGET=10.15`，使用 Python 3.11 和 `requirements-macos-catalina.txt`，因此旧版 Qt/PySide 依赖不会影响常规 macOS、Windows 或 Linux 包。
- Linux 上传 `remote-explorer-server` 可执行文件。

在当前默认 PySide6 依赖下，常规 macOS App 的目标最低版本是 macOS 11。如需 macOS 10.15 支持，请使用 Catalina artifact。

## 文档

- [项目计划](docs/PROJECT_PLAN.md)
- [UDP 协议](docs/PROTOCOL.md)

## 注意事项

UDP 流量仅面向局域网，且未加密。密码模式可以避免在网络中直接发送密码，但数据包和 session token 仍然可能被同一网络中的设备看到。请把首个版本视为可信局域网工具。
