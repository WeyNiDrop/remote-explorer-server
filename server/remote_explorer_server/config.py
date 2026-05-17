from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DISCOVERY_PORT = 45454
DEFAULT_CONTROL_PORT = 45455


@dataclass(frozen=True)
class ServerConfig:
    name: str
    discovery_port: int
    control_port: int
    password: str | None
    data_dir: Path
    start_url: str
    allow_evaluate_js: bool = False

    @property
    def server_id_path(self) -> Path:
        return self.data_dir / "server_id"


def default_server_name() -> str:
    node_name = platform.node().strip()
    return node_name or "Remote Explorer"


def default_data_dir() -> Path:
    app_data = os.environ.get("APPDATA")
    if app_data:
        return Path(app_data) / "RemoteExplorer"
    return Path.home() / ".remote-explorer"


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
