from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kcp.__main__ import main
from kcp.store import Store


class CliTests(unittest.TestCase):
    def test_reset_password_does_not_require_cluster_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "kcp.sqlite3"
            password_file = root / "new-password"
            password_file.write_text("a newer correct password", encoding="utf-8")
            store = Store(database)
            store.migrate()
            store.bootstrap_admin("admin", "correct horse battery staple")

            with patch.dict(os.environ, {"KCP_DB_PATH": str(database)}, clear=True), patch.object(
                sys, "argv", ["kcp", "admin", "reset-password", "--password-file", str(password_file)]
            ):
                main()

            self.assertTrue(store.verify_admin("admin", "a newer correct password"))
