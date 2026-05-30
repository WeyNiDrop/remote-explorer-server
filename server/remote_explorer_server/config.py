from __future__ import annotations

import os
import platform
import json
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DISCOVERY_PORT = 45454
DEFAULT_CONTROL_PORT = 45454


@dataclass(frozen=True)
class ServerConfig:
    name: str
    discovery_port: int
    control_port: int
    password: str | None
    data_dir: Path
    start_url: str
    allow_evaluate_js: bool = False
    browser_engine: str = "auto"
    browser_executable: str | None = None
    web_port: int | None = None

    @property
    def server_id_path(self) -> Path:
        return self.data_dir / "server_id"

    @property
    def effective_web_port(self) -> int:
        return int(self.web_port or self.control_port)


def default_server_name() -> str:
    node_name = platform.node().strip()
    return node_name or "Remote Explorer"


def default_data_dir() -> Path:
    app_data = os.environ.get("APPDATA")
    if app_data:
        return Path(app_data) / "RemoteExplorer"
    return Path.home() / ".remote-explorer"


def server_settings_path(data_dir: Path | None = None) -> Path:
    return (data_dir or default_data_dir()) / "server_settings.json"


def load_server_settings(data_dir: Path | None = None) -> dict[str, object]:
    path = server_settings_path(data_dir)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_server_settings(config: ServerConfig) -> Path:
    path = server_settings_path(config.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": config.name,
        "discovery_port": config.discovery_port,
        "control_port": config.control_port,
        "password": config.password or "",
        "start_url": config.start_url,
        "browser_engine": config.browser_engine,
        "browser_executable": config.browser_executable or "",
        "web_port": config.web_port or 0,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_or_create_server_id(config: ServerConfig) -> str:
    import uuid

    config.data_dir.mkdir(parents=True, exist_ok=True)
    if config.server_id_path.exists():
        value = config.server_id_path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = uuid.uuid4().hex
    config.server_id_path.write_text(value, encoding="utf-8")
    return value
