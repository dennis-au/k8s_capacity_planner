from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from kcp.config import RuntimeConfig, load_runtime_config
from kcp.docs import DocumentRegistry
from kcp.models import ClusterSnapshot
from kcp.service import CollectionService
from kcp.store import Store


class ConfigAndServiceTests(unittest.TestCase):
    def test_load_runtime_config_does_not_require_cluster_credentials_and_creates_session_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            environment = {
                "KCP_DB_PATH": str(root / "kcp.sqlite3"),
                "KCP_REFRESH_INTERVAL": "1h",
                "KCP_RETENTION_DAYS": "90",
            }
            with patch.dict(os.environ, environment, clear=True):
                config = load_runtime_config()

            self.assertEqual(config.refresh_seconds, 3600)
            self.assertEqual(config.retention_days, 90)
            self.assertEqual(config.db_path, root / "kcp.sqlite3")
            self.assertTrue((root / "kcp.session.key").is_file())

    def test_collection_persists_snapshot_and_prunes_expired_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "kcp.sqlite3")
            store.migrate()
            cluster = store.create_cluster(
                "Production East",
                str(root / "east.kubeconfig"),
                "east-readonly",
                "https://east.example:6443",
            )
            store.save_snapshot(
                datetime.now(UTC) - timedelta(days=91),
                "v1.36.0",
                {"expired": True},
                cluster_id=cluster["id"],
            )
            config = RuntimeConfig(
                db_path=root / "kcp.sqlite3",
                docs_dir=Path("kcp/assets/k8s-docs"),
                refresh_seconds=3600,
                retention_days=90,
                admin_username="admin",
                insecure_http=True,
                session_secret="test" * 16,
            )
            service = CollectionService(config, store, DocumentRegistry(config.docs_dir), lambda _: _SnapshotCollector())

            snapshot_id = service.collect_now(cluster["id"])

            self.assertIsNotNone(snapshot_id)
            self.assertEqual(len(store.list_snapshots(cluster["id"])), 1)
            self.assertEqual(store.latest_snapshot(cluster["id"])["cluster_version"], "v1.36.0")
            self.assertEqual(store.list_cluster_logs(cluster["id"])[0]["action"], "snapshot")
            self.assertEqual(store.list_cluster_logs(cluster["id"])[0]["status"], "success")

    def test_connection_test_records_a_redacted_successful_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "kcp.sqlite3")
            store.migrate()
            cluster = store.create_cluster("East", "/run/east.kubeconfig", "east", "https://east.example")
            config = RuntimeConfig(
                db_path=root / "kcp.sqlite3",
                docs_dir=Path("kcp/assets/k8s-docs"),
                refresh_seconds=3600,
                retention_days=90,
                admin_username="admin",
                insecure_http=True,
                session_secret="test" * 16,
            )
            service = CollectionService(config, store, DocumentRegistry(config.docs_dir), lambda _: _SnapshotCollector())

            self.assertEqual(service.test_connection(cluster["id"]), "v1.36.0")

            log = store.list_cluster_logs(cluster["id"])[0]
            self.assertEqual(log["action"], "connection-test")
            self.assertEqual(log["status"], "success")
            self.assertEqual(log["message"], "Connected to Kubernetes v1.36.0.")

    def test_connection_test_records_a_redacted_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "kcp.sqlite3")
            store.migrate()
            cluster = store.create_cluster("East", "/run/east.kubeconfig", "east", "https://east.example")
            config = RuntimeConfig(
                db_path=root / "kcp.sqlite3",
                docs_dir=Path("kcp/assets/k8s-docs"),
                refresh_seconds=3600,
                retention_days=90,
                admin_username="admin",
                insecure_http=True,
                session_secret="test" * 16,
            )
            service = CollectionService(config, store, DocumentRegistry(config.docs_dir), lambda _: _FailingCollector())

            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                service.test_connection(cluster["id"])

            self.assertEqual(service.last_error_for(cluster["id"]), "Connection test failed: RuntimeError")
            log = store.list_cluster_logs(cluster["id"])[0]
            self.assertEqual(log["action"], "connection-test")
            self.assertEqual(log["status"], "error")
            self.assertEqual(log["message"], "Connection test failed.")

    def test_collect_all_persists_a_snapshot_for_each_cluster(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "kcp.sqlite3")
            store.migrate()
            east = store.create_cluster("East", "/run/east.kubeconfig", "east", "https://east.example")
            west = store.create_cluster("West", "/run/west.kubeconfig", "west", "https://west.example")
            config = RuntimeConfig(
                db_path=root / "kcp.sqlite3",
                docs_dir=Path("kcp/assets/k8s-docs"),
                refresh_seconds=3600,
                retention_days=90,
                admin_username="admin",
                insecure_http=True,
                session_secret="test" * 16,
            )
            service = CollectionService(config, store, DocumentRegistry(config.docs_dir), lambda _: _SnapshotCollector())

            self.assertEqual(service.collect_all(), [1, 2])
            self.assertIsNotNone(store.latest_snapshot(east["id"]))
            self.assertIsNotNone(store.latest_snapshot(west["id"]))

    def test_collect_all_keeps_errors_scoped_to_the_failed_cluster(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "kcp.sqlite3")
            store.migrate()
            east = store.create_cluster("East", "/run/east.kubeconfig", "east", "https://east.example")
            west = store.create_cluster("West", "/run/west.kubeconfig", "west", "https://west.example")
            config = RuntimeConfig(
                db_path=root / "kcp.sqlite3",
                docs_dir=Path("kcp/assets/k8s-docs"),
                refresh_seconds=3600,
                retention_days=90,
                admin_username="admin",
                insecure_http=True,
                session_secret="test" * 16,
            )

            def collector(cluster: dict) -> _SnapshotCollector:
                if cluster["id"] == east["id"]:
                    raise RuntimeError("unavailable")
                return _SnapshotCollector()

            service = CollectionService(config, store, DocumentRegistry(config.docs_dir), collector)

            self.assertEqual(service.collect_all(), [1])
            self.assertEqual(service.last_error_for(east["id"]), "Collection failed: RuntimeError")
            self.assertIsNone(service.last_error_for(west["id"]))


class _SnapshotCollector:
    def collect(self) -> ClusterSnapshot:
        return ClusterSnapshot(cluster_version="v1.36.0", metrics_available=True, nodes=[], namespaces=[], workloads=[])

    def test_connection(self) -> str:
        return "v1.36.0"


class _FailingCollector:
    def test_connection(self) -> str:
        raise RuntimeError("unavailable")


def _write_kubeconfig(root: Path) -> Path:
    kubeconfig = root / "kubeconfig"
    kubeconfig.write_text(
        """apiVersion: v1
kind: Config
clusters:
- name: darksite
  cluster:
    server: https://cluster.darksite.local:6443
contexts:
- name: darksite-readonly
  context:
    cluster: darksite
    user: kcp-reader
current-context: darksite-readonly
users:
- name: kcp-reader
  user:
    token: read-only-token
""",
        encoding="utf-8",
    )
    return kubeconfig
