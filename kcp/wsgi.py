from __future__ import annotations

import os
from pathlib import Path

from kcp.config import load_runtime_config, read_secret_file
from kcp.store import Store
from kcp.web import create_app


config = load_runtime_config()
store = Store(config.db_path)
store.migrate()
if not store.has_users():
    password_file = Path(os.environ.get("KCP_ADMIN_PASSWORD_FILE", ""))
    if not password_file.is_file():
        raise RuntimeError("KCP_ADMIN_PASSWORD_FILE must point to a readable file on first start")
    store.bootstrap_admin(config.admin_username, read_secret_file(password_file))
app = create_app(config, store=store)
