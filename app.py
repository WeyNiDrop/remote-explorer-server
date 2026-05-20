from __future__ import annotations

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
SERVER_DIR = ROOT_DIR / "server"

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from remote_explorer_server.main import main


if __name__ == "__main__":
    raise SystemExit(main())
