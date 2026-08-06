from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from kcp.kubeconfig_files import KubeconfigFiles


class KubeconfigFilesTests(unittest.TestCase):
    def test_saves_private_files_and_removes_only_managed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "kubeconfigs"
            files = KubeconfigFiles(root)
            stored = Path(files.save_text("apiVersion: v1\nkind: Config\n"))
            unrelated = root / "operator.yaml"
            unrelated.write_text("keep", encoding="utf-8")

            self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
            files.remove(str(unrelated))
            self.assertTrue(unrelated.exists())

            files.remove(str(stored))
            self.assertFalse(stored.exists())
