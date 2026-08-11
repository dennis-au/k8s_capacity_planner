from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

from kcp.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "kcp.sqlite3")
        self.store.migrate()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_bootstrap_and_reset_admin_password(self) -> None:
        self.assertTrue(self.store.bootstrap_admin("admin", "correct horse battery staple"))
        self.assertFalse(self.store.bootstrap_admin("other", "another long password"))
        self.assertTrue(self.store.verify_admin("admin", "correct horse battery staple"))
        self.assertFalse(self.store.verify_admin("admin", "wrong password"))

        self.store.reset_admin_password("admin", "a newer correct password")

        self.assertFalse(self.store.verify_admin("admin", "correct horse battery staple"))
        self.assertTrue(self.store.verify_admin("admin", "a newer correct password"))

    def test_admin_password_accepts_any_nonempty_value_up_to_1024_characters(self) -> None:
        self.assertTrue(self.store.bootstrap_admin("admin", "x"))
        self.assertTrue(self.store.verify_admin("admin", "x"))

        maximum_length_password = "x" * 1024
        self.store.reset_admin_password("admin", maximum_length_password)
        self.assertTrue(self.store.verify_admin("admin", maximum_length_password))

        with self.assertRaisesRegex(ValueError, "password must be between 1 and 1024 characters"):
            self.store.reset_admin_password("admin", "")
        with self.assertRaisesRegex(ValueError, "password must be between 1 and 1024 characters"):
            self.store.reset_admin_password("admin", "x" * 1025)

    def test_runtime_settings_use_defaults_and_persist_valid_updates(self) -> None:
        self.assertEqual(
            self.store.get_runtime_settings(default_refresh_seconds=3600, default_retention_days=90),
            {
                "schedule_enabled": True,
                "snapshot_interval_minutes": 60,
                "retention_days": 90,
                "planning_reserve_percent": 20,
            },
        )

        self.store.update_runtime_settings(
            False, snapshot_interval_minutes=30, retention_days=180, planning_reserve_percent=25
        )

        self.assertEqual(
            self.store.get_runtime_settings(default_refresh_seconds=3600, default_retention_days=90),
            {
                "schedule_enabled": False,
                "snapshot_interval_minutes": 30,
                "retention_days": 180,
                "planning_reserve_percent": 25,
            },
        )
        with self.assertRaisesRegex(ValueError, "Snapshot interval"):
            self.store.update_runtime_settings(True, snapshot_interval_minutes=14, retention_days=90)
        with self.assertRaisesRegex(ValueError, "Report retention"):
            self.store.update_runtime_settings(True, snapshot_interval_minutes=60, retention_days=0)
        with self.assertRaisesRegex(ValueError, "Planning reserve"):
            self.store.update_runtime_settings(
                True, snapshot_interval_minutes=60, retention_days=90, planning_reserve_percent=51
            )

    def test_prune_snapshots_removes_only_expired_rows(self) -> None:
        now = datetime.now(UTC)
        expired = self.store.save_snapshot(
            collected_at=now - timedelta(days=91),
            cluster_version="v1.36.0",
            payload={"summary": {"nodes": 1}},
        )
        recent = self.store.save_snapshot(
            collected_at=now - timedelta(days=1),
            cluster_version="v1.36.0",
            payload={"summary": {"nodes": 2}},
        )

        self.assertEqual(self.store.prune_snapshots(now - timedelta(days=90)), 1)
        self.assertIsNone(self.store.get_snapshot(expired))
        self.assertEqual(self.store.get_snapshot(recent)["summary"]["nodes"], 2)

    def test_cluster_metadata_records_kubeconfig_without_secret_contents(self) -> None:
        connection = self.store.create_cluster(
            name="Production West",
            kubeconfig_file="/run/kcp/prod-west.kubeconfig",
            kube_context="prod-west-readonly",
            endpoint="https://kubernetes.prod.example:6443/",
            api_ip="10.20.30.40",
            disable_proxy=True,
        )

        self.assertEqual(connection["name"], "Production West")
        self.assertEqual(connection["endpoint"], "https://kubernetes.prod.example:6443")
        self.assertEqual(connection["kubeconfig_file"], "/run/kcp/prod-west.kubeconfig")
        self.assertEqual(connection["kube_context"], "prod-west-readonly")
        self.assertEqual(connection["api_ip"], "10.20.30.40")
        self.assertTrue(connection["disable_proxy"])
        self.assertNotIn(b"read-only-token", self.store.db_path.read_bytes())

    def test_migration_adds_proxy_setting_to_existing_clusters(self) -> None:
        db_path = Path(self.temp_dir.name) / "previous-release.sqlite3"
        connection = sqlite3.connect(db_path)
        try:
            connection.executescript(
                """
                CREATE TABLE clusters (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    endpoint TEXT,
                    kubeconfig_file TEXT,
                    kube_context TEXT,
                    api_ip TEXT,
                    legacy_token_file TEXT,
                    legacy_ca_file TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO clusters(
                    name, endpoint, kubeconfig_file, kube_context, api_ip, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Existing cluster",
                    "https://kubernetes.example:6443",
                    "/run/kcp/existing.kubeconfig",
                    "existing",
                    None,
                    "2026-08-10T00:00:00+00:00",
                    "2026-08-10T00:00:00+00:00",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        store = Store(db_path)
        store.migrate()

        self.assertFalse(store.get_cluster(1)["disable_proxy"])

    def test_multiple_clusters_keep_snapshots_separate(self) -> None:
        east = self.store.create_cluster(
            "Production East",
            "/run/kcp/east.kubeconfig",
            "east-readonly",
            "https://east.example:6443",
        )
        west = self.store.create_cluster(
            "Production West",
            "/run/kcp/west.kubeconfig",
            "west-readonly",
            "https://west.example:6443",
        )
        self.store.save_snapshot(
            datetime.now(UTC) - timedelta(minutes=2),
            "v1.36.0",
            {"cluster": "east"},
            cluster_id=east["id"],
        )
        self.store.save_snapshot(
            datetime.now(UTC),
            "v1.36.0",
            {"cluster": "west"},
            cluster_id=west["id"],
        )

        self.assertEqual(self.store.latest_snapshot(east["id"])["payload"]["cluster"], "east")
        self.assertEqual(self.store.latest_snapshot(west["id"])["payload"]["cluster"], "west")
        self.assertEqual(len(self.store.list_snapshots(east["id"])), 1)
        self.assertEqual(len(self.store.list_snapshots(west["id"])), 1)

    def test_delete_cluster_removes_only_its_snapshots(self) -> None:
        east = self.store.create_cluster(
            "Production East",
            "/run/kcp/east.kubeconfig",
            "east-readonly",
            "https://east.example:6443",
        )
        west = self.store.create_cluster(
            "Production West",
            "/run/kcp/west.kubeconfig",
            "west-readonly",
            "https://west.example:6443",
        )
        self.store.save_snapshot(datetime.now(UTC), "v1.36.0", {"cluster": "east"}, cluster_id=east["id"])
        self.store.save_snapshot(datetime.now(UTC), "v1.36.0", {"cluster": "west"}, cluster_id=west["id"])

        self.assertTrue(self.store.delete_cluster(east["id"]))
        self.assertIsNone(self.store.get_cluster(east["id"]))
        self.assertEqual(self.store.list_snapshots(east["id"]), [])
        self.assertEqual(self.store.latest_snapshot(west["id"])["payload"]["cluster"], "west")
        self.assertFalse(self.store.delete_cluster(east["id"]))

    def test_cluster_logs_are_retained_per_cluster_and_removed_with_the_cluster(self) -> None:
        east = self.store.create_cluster(
            "Production East",
            "/run/kcp/east.kubeconfig",
            "east-readonly",
            "https://east.example:6443",
        )
        west = self.store.create_cluster(
            "Production West",
            "/run/kcp/west.kubeconfig",
            "west-readonly",
            "https://west.example:6443",
        )
        self.store.add_cluster_log(east["id"], "connection-test", "success", "Connected to Kubernetes v1.36.0.")
        self.store.add_cluster_log(west["id"], "snapshot", "error", "Snapshot collection failed.")

        east_logs = self.store.list_cluster_logs(east["id"])
        self.assertEqual(len(east_logs), 1)
        self.assertEqual(east_logs[0]["action"], "connection-test")
        self.assertEqual(self.store.list_cluster_logs(west["id"])[0]["action"], "snapshot")

        self.assertTrue(self.store.delete_cluster(east["id"]))
        self.assertEqual(self.store.list_cluster_logs(east["id"]), [])
        self.assertEqual(len(self.store.list_cluster_logs(west["id"])), 1)

    def test_bootstrap_cluster_does_not_restore_a_removed_final_cluster(self) -> None:
        cluster = self.store.bootstrap_cluster(
            "Configured cluster",
            "/run/kcp/configured.kubeconfig",
            "configured",
            "https://configured.example:6443",
        )
        self.assertIsNotNone(cluster)

        self.assertTrue(self.store.delete_cluster(cluster["id"]))

        self.assertIsNone(
            self.store.bootstrap_cluster(
                "Configured cluster",
                "/run/kcp/configured.kubeconfig",
                "configured",
                "https://configured.example:6443",
            )
        )

    def test_cluster_name_must_be_unique_but_endpoint_can_be_shared(self) -> None:
        self.store.create_cluster(
            "Production East",
            "/run/kcp/east.kubeconfig",
            "east-readonly",
            "https://shared.example:6443",
        )

        with self.assertRaisesRegex(ValueError, "already uses"):
            self.store.create_cluster(
                "production east",
                "/run/kcp/other.kubeconfig",
                "other-readonly",
                "https://shared.example:6443",
            )

        other = self.store.create_cluster(
            "Production Other",
            "/run/kcp/other.kubeconfig",
            "other-readonly",
            "https://shared.example:6443",
        )
        self.assertEqual(other["endpoint"], "https://shared.example:6443")

    def test_migration_converts_legacy_connection_and_snapshot(self) -> None:
        now = datetime.now(UTC).isoformat()
        connection = sqlite3.connect(self.store.db_path)
        try:
            connection.executescript(
                """
                DROP TABLE snapshots;
                DROP TABLE IF EXISTS cluster_connection;
                CREATE TABLE snapshots (
                    id INTEGER PRIMARY KEY,
                    collected_at TEXT NOT NULL,
                    cluster_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE cluster_connection (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    name TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    token_file TEXT NOT NULL,
                    ca_file TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO cluster_connection VALUES (1, ?, ?, ?, ?, ?, ?)",
                ("Legacy cluster", "https://legacy.example:6443", "/run/token", "/run/ca.pem", now, now),
            )
            connection.execute(
                "INSERT INTO snapshots(collected_at, cluster_version, payload_json) VALUES (?, ?, ?)",
                (now, "v1.36.0", '{"legacy":true}'),
            )
            connection.commit()
        finally:
            connection.close()

        self.store.migrate()

        cluster = self.store.list_clusters()[0]
        snapshot = self.store.latest_snapshot(cluster["id"])
        self.assertEqual(cluster["name"], "Legacy cluster")
        self.assertTrue(cluster["legacy_connection"])
        self.assertEqual(snapshot["payload"], {"legacy": True})

    def test_migration_retains_existing_multi_cluster_snapshots_as_legacy_connections(self) -> None:
        now = datetime.now(UTC).isoformat()
        connection = sqlite3.connect(self.store.db_path)
        try:
            connection.executescript(
                """
                DROP TABLE clusters;
                CREATE TABLE clusters (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    endpoint TEXT NOT NULL UNIQUE,
                    token_file TEXT NOT NULL,
                    ca_file TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO clusters VALUES (1, ?, ?, ?, ?, ?, ?)",
                ("Legacy multi-cluster", "https://legacy.example:6443", "/run/token", "/run/ca.pem", now, now),
            )
            connection.execute(
                "INSERT INTO snapshots(collected_at, cluster_version, payload_json, cluster_id) VALUES (?, ?, ?, 1)",
                (now, "v1.36.0", '{"legacy":true}'),
            )
            connection.commit()
        finally:
            connection.close()

        self.store.migrate()

        cluster = self.store.get_cluster(1)
        self.assertIsNotNone(cluster)
        self.assertTrue(cluster["legacy_connection"])
        self.assertEqual(cluster["endpoint"], "https://legacy.example:6443")
        self.assertEqual(self.store.latest_snapshot(1)["payload"], {"legacy": True})
