from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .config import ServerConfig, save_server_settings
from .local_domain import normalize_local_domain

CHROME_DOWNLOAD_URL = "https://www.google.com/chrome/"
H5_QR_DISPLAY_SIZE = 232

STYLE = """
QWidget#serverConsole,
QDialog#serverConsoleDialog {
    background: #07111f;
    color: #e7f3ff;
    font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
}
QWidget#windowShell {
    background: #07111f;
    border: 1px solid #213957;
}
QFrame#titleBar {
    background: transparent;
}
QWidget QLabel,
QDialog QLabel {
    color: #e7f3ff;
}
QLabel[muted="true"] {
    color: #8ba4bd;
}
QLabel[pill="true"] {
    background: #152b46;
    border: 1px solid #213957;
    border-radius: 15px;
    color: #28e59d;
    padding: 6px 13px;
    font-weight: 700;
}
QLabel[pill="warn"] {
    background: #2a1730;
    border: 1px solid #ff5c7a;
    border-radius: 15px;
    color: #ff8aa0;
    padding: 6px 13px;
    font-weight: 700;
}
QFrame[card="true"] {
    background: #0d1b2e;
    border: 1px solid #213957;
    border-radius: 18px;
}
QFrame[inner="true"] {
    background: #07111f;
    border: 1px solid #213957;
    border-radius: 14px;
}
QLineEdit,
QTextEdit {
    background: #07111f;
    border: 1px solid #213957;
    border-radius: 10px;
    color: #e7f3ff;
    selection-background-color: #4d8dff;
    padding: 9px 12px;
}
QTextEdit {
    font-family: "Consolas", "Menlo", monospace;
    font-size: 12px;
    line-height: 1.45;
}
QPushButton {
    background: #152b46;
    border: 1px solid #213957;
    border-radius: 12px;
    color: #e7f3ff;
    font-weight: 700;
    min-height: 36px;
    padding: 0 16px;
}
QPushButton:hover {
    background: #1a3454;
}
QPushButton[primary="true"] {
    background: #4d8dff;
    border: 1px solid #4d8dff;
    color: #ffffff;
}
QPushButton[primary="true"]:hover {
    background: #6aa1ff;
}
QPushButton[windowControl="true"] {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 10px;
    min-width: 34px;
    max-width: 34px;
    min-height: 32px;
    max-height: 32px;
    padding: 0;
    font-size: 16px;
}
QPushButton[windowControl="true"]:hover {
    background: #152b46;
    border: 1px solid #213957;
}
QPushButton[windowClose="true"]:hover {
    background: #ff5c7a;
    border: 1px solid #ff5c7a;
    color: #ffffff;
}
"""


class FramelessTitleBar(QFrame):
    def __init__(self, parent: QWidget | None = None, *, allow_maximize: bool = True, height: int = 56) -> None:
        super().__init__(parent)
        self.allow_maximize = allow_maximize
        self.setObjectName("titleBar")
        self.setFixedHeight(height)
        self._drag_offset: QPoint | None = None
        self._system_move_active = False

    def add_drag_handle(self, widget: QWidget) -> None:
        widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(watched, QWidget) and watched is not self:
            if event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
                return self._handle_mouse_press(event)
            if event.type() == QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
                return self._handle_mouse_move(event)
            if event.type() == QEvent.Type.MouseButtonRelease and isinstance(event, QMouseEvent):
                return self._handle_mouse_release(event)
            if event.type() == QEvent.Type.MouseButtonDblClick and isinstance(event, QMouseEvent):
                return self._handle_mouse_double_click(event)
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._handle_mouse_press(event):
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._handle_mouse_move(event):
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._handle_mouse_release(event):
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if self._handle_mouse_double_click(event):
            return
        super().mouseDoubleClickEvent(event)

    def _handle_mouse_press(self, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        window = self.window()
        self._drag_offset = event.globalPosition().toPoint() - window.frameGeometry().topLeft()
        self._system_move_active = False
        if not window.isMaximized():
            window_handle = window.windowHandle()
            if window_handle is not None:
                self._system_move_active = window_handle.startSystemMove()
        event.accept()
        return True

    def _handle_mouse_move(self, event: QMouseEvent) -> bool:
        if not (event.buttons() & Qt.MouseButton.LeftButton) or self._drag_offset is None:
            return False
        window = self.window()
        if not window.isMaximized() and not self._system_move_active:
            window.move(event.globalPosition().toPoint() - self._drag_offset)
        event.accept()
        return True

    def _handle_mouse_release(self, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton and self._drag_offset is None:
            return False
        self._drag_offset = None
        self._system_move_active = False
        event.accept()
        return True

    def _handle_mouse_double_click(self, event: QMouseEvent) -> bool:
        if self.allow_maximize and event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            if window.isMaximized():
                window.showNormal()
            else:
                window.showMaximized()
            event.accept()
            return True
        return False


class ServerControlPanel(QWidget):
    def __init__(
        self,
        config: ServerConfig,
        log_path: Path | None,
        chrome_executable: str | None,
        parent: QWidget | None = None,
        *,
        web_url: str | None = None,
        web_urls: list[str] | None = None,
        web_qr_png: bytes | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.log_path = log_path
        self.chrome_executable = chrome_executable
        self.web_url = web_url
        self.web_urls = web_urls or ([web_url] if web_url else [])
        self.web_qr_png = web_qr_png
        self.setObjectName("serverConsole")
        self.setStyleSheet(STYLE)
        self.setMinimumSize(960, 740)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        shell = QWidget(self)
        shell.setObjectName("windowShell")
        root.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(20)

        layout.addWidget(self._build_title_bar())

        content = QHBoxLayout()
        content.setSpacing(22)
        content.addWidget(self._build_status_card(), 0)
        content.addWidget(self._build_log_card(), 1)
        layout.addLayout(content, 1)

        self.refresh_logs()
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh_logs)
        self.timer.start()

    def _build_title_bar(self) -> QWidget:
        bar = FramelessTitleBar(self)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        mark = QLabel("RC")
        mark.setFixedSize(44, 44)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setStyleSheet(
            "background:#09243e;border:1px solid #2de2e6;border-radius:12px;"
            "color:#e7f3ff;font-size:14px;font-weight:800;"
        )
        bar.add_drag_handle(mark)
        layout.addWidget(mark)

        title = QVBoxLayout()
        title.setSpacing(0)
        title_label = self._text("RCViewer", 22, bold=True)
        subtitle_label = self._text("服务端控制台", 12, muted=True)
        bar.add_drag_handle(title_label)
        bar.add_drag_handle(subtitle_label)
        title.addWidget(title_label)
        title.addWidget(subtitle_label)
        layout.addLayout(title)
        layout.addStretch(1)

        settings = QPushButton("设置")
        settings.setProperty("primary", True)
        settings.clicked.connect(self.open_settings)
        layout.addWidget(settings, 0, Qt.AlignmentFlag.AlignVCenter)

        minimize = self._window_button("−")
        minimize.clicked.connect(lambda: self.window().showMinimized())
        maximize = self._window_button("□")
        maximize.clicked.connect(self.toggle_maximized)
        close = self._window_button("×")
        close.setProperty("windowClose", True)
        close.clicked.connect(lambda: self.window().close())
        layout.addWidget(minimize)
        layout.addWidget(maximize)
        layout.addWidget(close)
        return bar

    def _build_status_card(self) -> QFrame:
        card = self._card(width=330)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 22, 24, 24)
        layout.setSpacing(18)
        layout.addWidget(self._text("运行状态", 20, bold=True))

        self.status_label = QLabel("运行中")
        self.status_label.setProperty("pill", True)
        layout.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignLeft)

        info = self._inner()
        info_layout = QGridLayout(info)
        info_layout.setContentsMargins(18, 18, 18, 18)
        info_layout.setHorizontalSpacing(16)
        info_layout.setVerticalSpacing(14)
        self.port_value = self._text(str(self.config.control_port), 15, bold=True)
        self.auth_value = self._text("已设置密码" if self.config.password else "无密码", 15, bold=True)
        self.discovery_value = self._text(str(self.config.discovery_port), 15, bold=True)
        info_layout.addWidget(self._text("监听端口", 12, muted=True), 0, 0)
        info_layout.addWidget(self.port_value, 0, 1)
        info_layout.addWidget(self._text("访问密码", 12, muted=True), 1, 0)
        info_layout.addWidget(self.auth_value, 1, 1)
        info_layout.addWidget(self._text("发现端口", 12, muted=True), 2, 0)
        info_layout.addWidget(self.discovery_value, 2, 1)
        layout.addWidget(info)

        client = self._inner()
        client_layout = QVBoxLayout(client)
        client_layout.setContentsMargins(18, 18, 18, 18)
        client_layout.setSpacing(10)
        client_layout.addWidget(self._text("当前连接客户端", 12, muted=True))
        self.client_label = self._text("暂无客户端连接", 17, bold=True, wrap=True)
        self.client_note = self._text("连接后会显示设备名、IP 与端口", 12, muted=True, wrap=True)
        client_layout.addWidget(self.client_label)
        client_layout.addWidget(self.client_note)
        client_layout.addStretch(1)
        layout.addWidget(client, 1)
        return card

    def _build_h5_card(self) -> QFrame:
        card = self._inner()
        card.setMinimumHeight(H5_QR_DISPLAY_SIZE + 36)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(18)

        qr = QLabel()
        qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        qr.setFixedSize(H5_QR_DISPLAY_SIZE, H5_QR_DISPLAY_SIZE)
        qr.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        qr.setStyleSheet("background: transparent; border: none; padding: 0;")
        pixmap = QPixmap()
        if self.web_qr_png and pixmap.loadFromData(self.web_qr_png, "PNG"):
            if pixmap.width() > H5_QR_DISPLAY_SIZE or pixmap.height() > H5_QR_DISPLAY_SIZE:
                pixmap = pixmap.scaled(
                    H5_QR_DISPLAY_SIZE,
                    H5_QR_DISPLAY_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.FastTransformation,
                )
            qr.setPixmap(pixmap)
        else:
            qr.setText("QR unavailable")
        layout.addWidget(qr, 0, Qt.AlignmentFlag.AlignVCenter)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(9)
        text_layout.addWidget(self._text("Server H5 Client", 16, bold=True))
        text_layout.addWidget(self._text("Scan QR or open address", 12, muted=True, wrap=True))

        url_label = self._text(self.web_url or "", 15, bold=True, wrap=True)
        url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        url_label.setMinimumHeight(44)
        url_label.setMaximumHeight(72)
        text_layout.addWidget(url_label)
        text_layout.addStretch(1)
        layout.addLayout(text_layout, 1)
        return card

    def _build_log_card(self) -> QFrame:
        card = self._card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 22, 24, 24)
        layout.setSpacing(14)

        if self.web_url:
            layout.addWidget(self._build_h5_card(), 0)

        header = QHBoxLayout()
        header.setSpacing(12)
        header.addWidget(self._text("日志记录", 20, bold=True))
        header.addStretch(1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh_logs)
        clear = QPushButton("清空")
        clear.clicked.connect(self.clear_logs)
        header.addWidget(refresh)
        header.addWidget(clear)
        layout.addLayout(header)

        self.log_preview = QTextEdit()
        self.log_preview.setReadOnly(True)
        self.log_preview.setMinimumHeight(300)
        layout.addWidget(self.log_preview, 1)
        return card

    def update_client(self, name: str, host: str, port: int) -> None:
        self.client_label.setText(f"{name}\n{host}:{port}")
        self.client_note.setText("客户端已连接")

    def set_startup_warning(self, message: str) -> None:
        self.status_label.setText("浏览器不可用")
        self.status_label.setProperty("pill", "warn")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.client_label.setText("Chromium 启动失败")
        self.client_note.setText(message)

    def open_settings(self) -> None:
        dialog = ServerSettingsDialog(self.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.config = dialog.config
            self.port_value.setText(str(self.config.control_port))
            self.auth_value.setText("已设置密码" if self.config.password else "无密码")
            self.discovery_value.setText(str(self.config.discovery_port))
            self.status_label.setText("设定已保存，重启后生效")

    def refresh_logs(self) -> None:
        if self.log_path is None or not self.log_path.exists():
            self.log_preview.setPlainText("日志文件尚未创建。")
            return
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-120:]
        except OSError as exc:
            self.log_preview.setPlainText(str(exc))
            return
        self.log_preview.setPlainText("\n".join(lines))
        self.log_preview.verticalScrollBar().setValue(self.log_preview.verticalScrollBar().maximum())

    def clear_logs(self) -> None:
        if self.log_path is None:
            self.status_label.setText("日志文件尚未创建")
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text("", encoding="utf-8")
        self.log_preview.clear()
        self.status_label.setText("日志已清空")

    def toggle_maximized(self) -> None:
        window = self.window()
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()

    @staticmethod
    def _card(width: int | None = None) -> QFrame:
        frame = QFrame()
        frame.setProperty("card", True)
        frame.setSizePolicy(
            QSizePolicy.Policy.Fixed if width else QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        if width is not None:
            frame.setFixedWidth(width)
        return frame

    @staticmethod
    def _inner() -> QFrame:
        frame = QFrame()
        frame.setProperty("inner", True)
        return frame

    @staticmethod
    def _window_button(label: str) -> QPushButton:
        button = QPushButton(label)
        button.setProperty("windowControl", True)
        return button

    @staticmethod
    def _dialog_header(title: str, close_action: Callable[[], None], parent: QWidget) -> QWidget:
        bar = FramelessTitleBar(parent, allow_maximize=False, height=42)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        title_label = ServerControlPanel._text(title, 20, bold=True)
        bar.add_drag_handle(title_label)
        layout.addWidget(title_label)
        layout.addStretch(1)
        close = ServerControlPanel._window_button("×")
        close.setProperty("windowClose", True)
        close.clicked.connect(close_action)
        layout.addWidget(close)
        return bar

    @staticmethod
    def _text(value: str, size: int, bold: bool = False, muted: bool = False, wrap: bool = False) -> QLabel:
        label = QLabel(value)
        label.setWordWrap(wrap)
        label.setProperty("muted", muted)
        weight = 700 if bold else 400
        color = "#8ba4bd" if muted else "#e7f3ff"
        label.setStyleSheet(f"color: {color}; font-size: {size}px; font-weight: {weight};")
        return label


class ServerSettingsDialog(QDialog):
    def __init__(self, config: ServerConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.setObjectName("serverConsoleDialog")
        self.setStyleSheet(STYLE)
        self.setWindowTitle("服务端设置")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.setFixedSize(460, 378)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 18, 26, 24)
        layout.setSpacing(16)
        layout.addWidget(ServerControlPanel._dialog_header("服务端设置", self.reject, self))
        layout.addWidget(ServerControlPanel._text("保存后下次启动自动加载；端口和密码变更需要重启程序生效。", 13, muted=True, wrap=True))

        fields = QGridLayout()
        fields.setHorizontalSpacing(16)
        fields.setVerticalSpacing(10)
        fields.addWidget(ServerControlPanel._text("连接端口", 13, muted=True), 0, 0)
        fields.addWidget(ServerControlPanel._text("连接密码", 13, muted=True), 0, 1)
        self.port_input = QLineEdit(str(config.control_port))
        self.password_input = QLineEdit(config.password or "")
        self.password_input.setPlaceholderText("可留空")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.port_input.setFixedHeight(40)
        self.password_input.setFixedHeight(40)
        fields.addWidget(self.port_input, 1, 0)
        fields.addWidget(self.password_input, 1, 1)
        fields.addWidget(ServerControlPanel._text("H5 .local 域名", 13, muted=True), 2, 0, 1, 2)
        self.local_domain_input = QLineEdit(config.local_domain or "")
        self.local_domain_input.setPlaceholderText("example.local")
        self.local_domain_input.setFixedHeight(40)
        fields.addWidget(self.local_domain_input, 3, 0, 1, 2)
        layout.addLayout(fields)

        self.error_label = ServerControlPanel._text("", 12, muted=True)
        layout.addWidget(self.error_label)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存")
        save.setProperty("primary", True)
        save.clicked.connect(self.save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def save(self) -> None:
        try:
            port = int(self.port_input.text().strip())
        except ValueError:
            self.error_label.setText("端口必须是数字")
            return
        if port <= 0 or port > 65535:
            self.error_label.setText("端口范围必须是 1-65535")
            return
        local_domain = normalize_local_domain(self.local_domain_input.text())
        if not local_domain:
            self.error_label.setText("H5 .local 域名不能为空")
            return

        self.config = ServerConfig(
            name=self.config.name,
            discovery_port=port,
            control_port=port,
            password=self.password_input.text(),
            data_dir=self.config.data_dir,
            start_url=self.config.start_url,
            allow_evaluate_js=self.config.allow_evaluate_js,
            browser_engine=self.config.browser_engine,
            browser_executable=self.config.browser_executable,
            web_port=self.config.web_port,
            local_domain=local_domain,
        )
        save_server_settings(self.config)
        self.accept()


ServerControlDock = ServerControlPanel


class ChromeInstallDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("未检测到支持的 Chrome")
        self.setObjectName("serverConsoleDialog")
        self.setStyleSheet(STYLE)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.setFixedSize(430, 250)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 18, 26, 24)
        layout.setSpacing(15)
        layout.addWidget(ServerControlPanel._dialog_header("未检测到支持的 Chrome", self.close, self))
        layout.addWidget(
            ServerControlPanel._text(
                "RCViewer 服务端需要 Chrome / Edge / Chromium 支持远程浏览器能力。请安装完成后重启本程序。",
                13,
                muted=True,
                wrap=True,
            )
        )
        layout.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)
        later = QPushButton("稍后")
        later.clicked.connect(self.close)
        download = QPushButton("打开下载页面")
        download.setProperty("primary", True)
        download.clicked.connect(self.open_download)
        row.addWidget(later)
        row.addWidget(download)
        layout.addLayout(row)

    def open_download(self) -> None:
        QDesktopServices.openUrl(QUrl(CHROME_DOWNLOAD_URL))
        self.close()
