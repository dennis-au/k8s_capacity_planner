from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError


_USERNAME = re.compile(r"^[a-zA-Z0-9_.-]{3,64}$")
_CLUSTER_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 ._-]{1,63}$")
_PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
_CLUSTER_FIELDS = "id, name, endpoint, kubeconfig_file, kube_context, api_ip, legacy_token_file, legacy_ca_file"
_SETTINGS_SCHEDULE_ENABLED = "schedule_enabled"
_SETTINGS_INTERVAL_MINUTES = "snapshot_interval_minutes"
_SETTINGS_RETENTION_DAYS = "retention_days"
_CLUSTERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS clusters (
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


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY,
                    collected_at TEXT NOT NULL,
                    cluster_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS snapshots_collected_at_idx
                    ON snapshots(collected_at DESC);
                """
            )
            self._ensure_clusters_schema(connection)
            self._migrate_legacy_cluster_connection(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cluster_logs (
                    id INTEGER PRIMARY KEY,
                    cluster_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    FOREIGN KEY(cluster_id) REFERENCES clusters(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS cluster_logs_cluster_created_at_idx
                    ON cluster_logs(cluster_id, created_at DESC);
                """
            )
            if "cluster_id" not in self._table_columns(connection, "snapshots"):
                connection.execute("ALTER TABLE snapshots ADD COLUMN cluster_id INTEGER")
            first_cluster = connection.execute("SELECT id FROM clusters ORDER BY id LIMIT 1").fetchone()
            if first_cluster is not None:
                connection.execute("UPDATE snapshots SET cluster_id = ? WHERE cluster_id IS NULL", (first_cluster["id"],))
            connection.execute(
                "CREATE INDEX IF NOT EXISTS snapshots_cluster_collected_at_idx "
                "ON snapshots(cluster_id, collected_at DESC)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS clusters_name_idx ON clusters(name COLLATE NOCASE)"
            )
            if connection.execute("SELECT 1 FROM clusters LIMIT 1").fetchone() is not None:
                connection.execute(
                    "INSERT OR IGNORE INTO app_state(key, value) VALUES ('clusters_initialized', '1')"
                )

    def bootstrap_admin(self, username: str, password: str) -> bool:
        self._validate_credentials(username, password)
        now = _iso_now()
        password_hash = _PASSWORD_HASHER.hash(password)
        with self._connection() as connection:
            exists = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
            if exists:
                return False
            connection.execute(
                "INSERT INTO users(username, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (username, password_hash, now, now),
            )
        return True

    def verify_admin(self, username: str, password: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
        if row is None:
            return False
        try:
            valid = _PASSWORD_HASHER.verify(row["password_hash"], password)
        except (InvalidHashError, VerifyMismatchError):
            return False
        if valid and _PASSWORD_HASHER.check_needs_rehash(row["password_hash"]):
            self._update_password_hash(username, _PASSWORD_HASHER.hash(password))
        return valid

    def reset_admin_password(self, username: str, password: str) -> None:
        self._validate_credentials(username, password)
        password_hash = _PASSWORD_HASHER.hash(password)
        with self._connection() as connection:
            result = connection.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ?",
                (password_hash, _iso_now(), username),
            )
            if result.rowcount != 1:
                raise ValueError(f"administrator {username!r} does not exist")

    def get_runtime_settings(self, default_refresh_seconds: int, default_retention_days: int) -> dict[str, int | bool]:
        default_interval_minutes = max(15, default_refresh_seconds // 60)
        self._validate_runtime_settings(default_interval_minutes, default_retention_days)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT key, value FROM app_state WHERE key IN (?, ?, ?)",
                (_SETTINGS_SCHEDULE_ENABLED, _SETTINGS_INTERVAL_MINUTES, _SETTINGS_RETENTION_DAYS),
            ).fetchall()
        values = {row["key"]: row["value"] for row in rows}
        try:
            interval_minutes = int(values.get(_SETTINGS_INTERVAL_MINUTES, default_interval_minutes))
            retention_days = int(values.get(_SETTINGS_RETENTION_DAYS, default_retention_days))
        except ValueError:
            interval_minutes = default_interval_minutes
            retention_days = default_retention_days
        self._validate_runtime_settings(interval_minutes, retention_days)
        return {
            "schedule_enabled": values.get(_SETTINGS_SCHEDULE_ENABLED, "1") == "1",
            "snapshot_interval_minutes": interval_minutes,
            "retention_days": retention_days,
        }

    def update_runtime_settings(self, schedule_enabled: bool, snapshot_interval_minutes: int, retention_days: int) -> None:
        self._validate_runtime_settings(snapshot_interval_minutes, retention_days)
        values = (
            (_SETTINGS_SCHEDULE_ENABLED, "1" if schedule_enabled else "0"),
            (_SETTINGS_INTERVAL_MINUTES, str(snapshot_interval_minutes)),
            (_SETTINGS_RETENTION_DAYS, str(retention_days)),
        )
        with self._connection() as connection:
            connection.executemany(
                "INSERT INTO app_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                values,
            )

    def bootstrap_cluster(
        self, name: str, kubeconfig_file: str, kube_context: str, endpoint: str, api_ip: str | None = None
    ) -> dict[str, Any] | None:
        self._validate_cluster_connection(name, kubeconfig_file, kube_context, endpoint)
        now = _iso_now()
        with self._connection() as connection:
            initialized = connection.execute(
                "SELECT 1 FROM app_state WHERE key = 'clusters_initialized'"
            ).fetchone()
            if initialized is not None:
                return None
            row = connection.execute(f"SELECT {_CLUSTER_FIELDS} FROM clusters LIMIT 1").fetchone()
            if row is not None:
                return self._cluster_record(row)
            result = connection.execute(
                """
                INSERT INTO clusters(name, kubeconfig_file, kube_context, endpoint, api_ip, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (name, kubeconfig_file, kube_context, endpoint.rstrip("/"), api_ip, now, now),
            )
            cluster_id = int(result.lastrowid)
            connection.execute("INSERT INTO app_state(key, value) VALUES ('clusters_initialized', '1')")
            connection.execute("UPDATE snapshots SET cluster_id = ? WHERE cluster_id IS NULL", (cluster_id,))
            row = connection.execute(
                f"SELECT {_CLUSTER_FIELDS} FROM clusters WHERE id = ?", (cluster_id,)
            ).fetchone()
            return self._cluster_record(row)

    def create_cluster(
        self, name: str, kubeconfig_file: str, kube_context: str, endpoint: str, api_ip: str | None = None
    ) -> dict[str, Any]:
        self._validate_cluster_connection(name, kubeconfig_file, kube_context, endpoint)
        now = _iso_now()
        try:
            with self._connection() as connection:
                result = connection.execute(
                    """
                    INSERT INTO clusters(name, kubeconfig_file, kube_context, endpoint, api_ip, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (name, kubeconfig_file, kube_context, endpoint.rstrip("/"), api_ip, now, now),
                )
                row = connection.execute(
                    f"SELECT {_CLUSTER_FIELDS} FROM clusters WHERE id = ?", (result.lastrowid,)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ValueError("A cluster already uses that name.") from exc
        return self._cluster_record(row)

    def update_cluster(
        self, cluster_id: int, name: str, kubeconfig_file: str, kube_context: str, endpoint: str, api_ip: str | None = None
    ) -> dict[str, Any]:
        self._validate_cluster_connection(name, kubeconfig_file, kube_context, endpoint)
        try:
            with self._connection() as connection:
                result = connection.execute(
                    """
                    UPDATE clusters
                    SET name = ?, kubeconfig_file = ?, kube_context = ?, endpoint = ?, api_ip = ?,
                        legacy_token_file = NULL, legacy_ca_file = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (name, kubeconfig_file, kube_context, endpoint.rstrip("/"), api_ip, _iso_now(), cluster_id),
                )
                if result.rowcount != 1:
                    raise ValueError("Cluster not found.")
                row = connection.execute(
                    f"SELECT {_CLUSTER_FIELDS} FROM clusters WHERE id = ?", (cluster_id,)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ValueError("A cluster already uses that name.") from exc
        return self._cluster_record(row)

    def get_cluster(self, cluster_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {_CLUSTER_FIELDS} FROM clusters WHERE id = ?", (cluster_id,)
            ).fetchone()
        return self._cluster_record(row) if row else None

    def first_cluster(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {_CLUSTER_FIELDS} FROM clusters ORDER BY id LIMIT 1"
            ).fetchone()
        return self._cluster_record(row) if row else None

    def list_clusters(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT clusters.id, clusters.name, clusters.endpoint, clusters.kubeconfig_file, clusters.kube_context,
                       clusters.api_ip, clusters.legacy_token_file, clusters.legacy_ca_file,
                       MAX(snapshots.collected_at) AS last_collected_at
                FROM clusters
                LEFT JOIN snapshots ON snapshots.cluster_id = clusters.id
                GROUP BY clusters.id
                ORDER BY clusters.name COLLATE NOCASE, clusters.id
                """
            ).fetchall()
        return [self._cluster_record(row) | {"last_collected_at": row["last_collected_at"]} for row in rows]

    def delete_cluster(self, cluster_id: int) -> bool:
        with self._connection() as connection:
            connection.execute("DELETE FROM snapshots WHERE cluster_id = ?", (cluster_id,))
            connection.execute("DELETE FROM cluster_logs WHERE cluster_id = ?", (cluster_id,))
            result = connection.execute("DELETE FROM clusters WHERE id = ?", (cluster_id,))
        return result.rowcount == 1

    def add_cluster_log(self, cluster_id: int, action: str, status: str, message: str) -> None:
        if action not in {"connection-test", "snapshot"}:
            raise ValueError("invalid cluster log action")
        if status not in {"success", "error"}:
            raise ValueError("invalid cluster log status")
        if not message or len(message) > 512:
            raise ValueError("cluster log message must be 1-512 characters")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO cluster_logs(cluster_id, created_at, action, status, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (cluster_id, _iso_now(), action, status, message),
            )

    def list_cluster_logs(self, cluster_id: int, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("cluster log limit must be between 1 and 500")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT created_at, action, status, message
                FROM cluster_logs
                WHERE cluster_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (cluster_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def prune_cluster_logs(self, cutoff: datetime) -> int:
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        with self._connection() as connection:
            result = connection.execute("DELETE FROM cluster_logs WHERE created_at < ?", (cutoff.isoformat(),))
            return result.rowcount

    def save_snapshot(
        self, collected_at: datetime, cluster_version: str, payload: dict[str, Any], cluster_id: int | None = None
    ) -> int:
        if collected_at.tzinfo is None:
            raise ValueError("collected_at must be timezone-aware")
        if not cluster_version:
            raise ValueError("cluster_version must not be empty")
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        with self._connection() as connection:
            result = connection.execute(
                "INSERT INTO snapshots(collected_at, cluster_version, payload_json, cluster_id) VALUES (?, ?, ?, ?)",
                (collected_at.isoformat(), cluster_version, payload_json, cluster_id),
            )
            return int(result.lastrowid)

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        record = self.get_snapshot_record(snapshot_id)
        return record["payload"] if record else None

    def get_snapshot_record(self, snapshot_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT snapshots.id, snapshots.collected_at, snapshots.cluster_version, snapshots.payload_json,
                       snapshots.cluster_id, clusters.name AS cluster_name
                FROM snapshots
                LEFT JOIN clusters ON clusters.id = snapshots.cluster_id
                WHERE snapshots.id = ?
                """,
                (snapshot_id,),
            ).fetchone()
        return self._snapshot_record(row) if row else None

    def latest_snapshot(self, cluster_id: int | None = None) -> dict[str, Any] | None:
        with self._connection() as connection:
            if cluster_id is None:
                row = connection.execute(
                    """
                    SELECT snapshots.id, snapshots.collected_at, snapshots.cluster_version, snapshots.payload_json,
                           snapshots.cluster_id, clusters.name AS cluster_name
                    FROM snapshots
                    LEFT JOIN clusters ON clusters.id = snapshots.cluster_id
                    ORDER BY snapshots.collected_at DESC LIMIT 1
                    """
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT snapshots.id, snapshots.collected_at, snapshots.cluster_version, snapshots.payload_json,
                           snapshots.cluster_id, clusters.name AS cluster_name
                    FROM snapshots
                    LEFT JOIN clusters ON clusters.id = snapshots.cluster_id
                    WHERE snapshots.cluster_id = ? ORDER BY snapshots.collected_at DESC LIMIT 1
                    """,
                    (cluster_id,),
                ).fetchone()
        return self._snapshot_record(row) if row else None

    def list_snapshots(self, cluster_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if cluster_id is None:
                rows = connection.execute(
                    """
                    SELECT snapshots.id, snapshots.collected_at, snapshots.cluster_version, snapshots.payload_json,
                           snapshots.cluster_id, clusters.name AS cluster_name
                    FROM snapshots
                    LEFT JOIN clusters ON clusters.id = snapshots.cluster_id
                    ORDER BY snapshots.collected_at DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT snapshots.id, snapshots.collected_at, snapshots.cluster_version, snapshots.payload_json,
                           snapshots.cluster_id, clusters.name AS cluster_name
                    FROM snapshots
                    LEFT JOIN clusters ON clusters.id = snapshots.cluster_id
                    WHERE snapshots.cluster_id = ? ORDER BY snapshots.collected_at DESC LIMIT ?
                    """,
                    (cluster_id, limit),
                ).fetchall()
        return [self._snapshot_record(row) for row in rows]

    def has_users(self) -> bool:
        with self._connection() as connection:
            return connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def prune_snapshots(self, cutoff: datetime) -> int:
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        with self._connection() as connection:
            result = connection.execute(
                "DELETE FROM snapshots WHERE collected_at < ?", (cutoff.isoformat(),)
            )
            return result.rowcount

    @staticmethod
    def _validate_runtime_settings(snapshot_interval_minutes: int, retention_days: int) -> None:
        if not 15 <= snapshot_interval_minutes <= 1_440:
            raise ValueError("Snapshot interval must be between 15 and 1440 minutes")
        if not 1 <= retention_days <= 3_650:
            raise ValueError("Report retention must be between 1 and 3650 days")

    def _update_password_hash(self, username: str, password_hash: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ?",
                (password_hash, _iso_now(), username),
            )

    @staticmethod
    def _validate_credentials(username: str, password: str) -> None:
        if not _USERNAME.fullmatch(username):
            raise ValueError("username must be 3-64 characters: letters, digits, '.', '_', or '-'")
        if not 12 <= len(password) <= 1024:
            raise ValueError("password must be between 12 and 1024 characters")

    @staticmethod
    def _validate_cluster_connection(name: str, kubeconfig_file: str, kube_context: str, endpoint: str) -> None:
        if not _CLUSTER_NAME.fullmatch(name):
            raise ValueError("cluster name must be 2-64 characters: letters, digits, spaces, '.', '_', or '-'")
        if not endpoint or not endpoint.startswith("https://"):
            raise ValueError("cluster endpoint must use https")
        if not kubeconfig_file or not kube_context:
            raise ValueError("kubeconfig file and context are required")

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _cluster_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "endpoint": str(row["endpoint"] or ""),
            "kubeconfig_file": str(row["kubeconfig_file"] or ""),
            "kube_context": str(row["kube_context"] or ""),
            "api_ip": str(row["api_ip"] or ""),
            "legacy_connection": row["kubeconfig_file"] is None,
        }

    def _ensure_clusters_schema(self, connection: sqlite3.Connection) -> None:
        columns = self._table_columns(connection, "clusters")
        if not columns:
            connection.executescript(_CLUSTERS_SCHEMA)
            return
        expected = {"kubeconfig_file", "kube_context", "api_ip", "legacy_token_file", "legacy_ca_file"}
        if expected.issubset(columns):
            return
        connection.execute("DROP INDEX IF EXISTS clusters_name_idx")
        connection.execute("ALTER TABLE clusters RENAME TO clusters_legacy")
        connection.executescript(_CLUSTERS_SCHEMA)
        endpoint = "endpoint" if "endpoint" in columns else "NULL"
        token_file = "token_file" if "token_file" in columns else "NULL"
        ca_file = "ca_file" if "ca_file" in columns else "NULL"
        connection.execute(
            f"""
            INSERT INTO clusters(
                id, name, endpoint, kubeconfig_file, kube_context, api_ip,
                legacy_token_file, legacy_ca_file, created_at, updated_at
            )
            SELECT id, name, {endpoint}, NULL, NULL, NULL, {token_file}, {ca_file}, created_at, updated_at
            FROM clusters_legacy
            """
        )
        connection.execute("DROP TABLE clusters_legacy")

    def _migrate_legacy_cluster_connection(self, connection: sqlite3.Connection) -> None:
        legacy_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cluster_connection'"
        ).fetchone()
        if legacy_exists is None:
            return
        row = connection.execute(
            "SELECT id, name, endpoint, token_file, ca_file, created_at, updated_at FROM cluster_connection WHERE id = 1"
        ).fetchone()
        if row is not None:
            connection.execute(
                """
                INSERT OR IGNORE INTO clusters(
                    id, name, endpoint, legacy_token_file, legacy_ca_file, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["name"],
                    row["endpoint"],
                    row["token_file"],
                    row["ca_file"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )
        connection.execute("DROP TABLE cluster_connection")

    @staticmethod
    def _snapshot_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "collected_at": row["collected_at"],
            "cluster_version": row["cluster_version"],
            "cluster_id": int(row["cluster_id"]) if row["cluster_id"] is not None else None,
            "cluster_name": row["cluster_name"] if "cluster_name" in row.keys() else None,
            "payload": json.loads(row["payload_json"]),
        }


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat()
