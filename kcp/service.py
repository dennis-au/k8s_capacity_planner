from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from kcp.analysis import analyze_snapshot
from kcp.config import RuntimeConfig
from kcp.docs import DocumentRegistry
from kcp.kubernetes import KubernetesCollector
from kcp.store import Store


LOGGER = logging.getLogger(__name__)


class CollectionService:
    def __init__(
        self,
        config: RuntimeConfig,
        store: Store,
        docs: DocumentRegistry,
        collector_factory: Callable[[dict[str, Any]], KubernetesCollector],
    ) -> None:
        self.config = config
        self.store = store
        self.docs = docs
        self.collector_factory = collector_factory
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._schedule_changed = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_errors: dict[int, str] = {}

    def last_error_for(self, cluster_id: int) -> str | None:
        return self._last_errors.get(cluster_id)

    def runtime_settings(self) -> dict[str, int | bool]:
        return self.store.get_runtime_settings(self.config.refresh_seconds, self.config.retention_days)

    def update_runtime_settings(self, schedule_enabled: bool, snapshot_interval_minutes: int, retention_days: int) -> None:
        self.store.update_runtime_settings(schedule_enabled, snapshot_interval_minutes, retention_days)
        self._schedule_changed.set()

    def collect_now(self, cluster_id: int) -> int | None:
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return self._collect_cluster(cluster_id)
        finally:
            self._lock.release()

    def test_connection(self, cluster_id: int) -> str | None:
        if not self._lock.acquire(blocking=False):
            return None
        try:
            cluster = self.store.get_cluster(cluster_id)
            if cluster is None:
                raise ValueError("Cluster is not configured")
            try:
                version = self.collector_factory(cluster).test_connection()
            except Exception as exc:
                self._last_errors[cluster_id] = f"Connection test failed: {type(exc).__name__}"
                self._record_log(cluster_id, "connection-test", "error", "Connection test failed.")
                LOGGER.warning("Kubernetes connection test failed for cluster %s: %s", cluster_id, type(exc).__name__)
                raise
            self._last_errors.pop(cluster_id, None)
            self._record_log(cluster_id, "connection-test", "success", f"Connected to Kubernetes {version}.")
            return version
        finally:
            self._lock.release()

    def collect_all(self) -> list[int] | None:
        if not self._lock.acquire(blocking=False):
            return None
        try:
            snapshot_ids: list[int] = []
            for cluster in self.store.list_clusters():
                try:
                    snapshot_ids.append(self._collect_cluster(cluster["id"]))
                except Exception:
                    continue
            return snapshot_ids
        finally:
            self._lock.release()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="kcp-collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._schedule_changed.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            settings = self.runtime_settings()
            if not settings["schedule_enabled"]:
                self._schedule_changed.wait()
                self._schedule_changed.clear()
                continue
            changed = self._schedule_changed.wait(int(settings["snapshot_interval_minutes"]) * 60)
            self._schedule_changed.clear()
            if self._stop.is_set() or changed:
                continue
            self.collect_all()

    def _collect_cluster(self, cluster_id: int) -> int:
        cluster = self.store.get_cluster(cluster_id)
        if cluster is None:
            raise ValueError("Cluster is not configured")
        try:
            snapshot = self.collector_factory(cluster).collect()
            findings = analyze_snapshot(snapshot, self.docs)
            payload = {
                "snapshot": snapshot.to_dict(),
                "findings": [finding.to_dict() for finding in findings],
            }
            snapshot_id = self.store.save_snapshot(
                snapshot.collected_at,
                snapshot.cluster_version,
                payload,
                cluster_id=cluster_id,
            )
            self.store.prune_snapshots(datetime.now(UTC) - timedelta(days=int(self.runtime_settings()["retention_days"])))
            self._last_errors.pop(cluster_id, None)
            self._record_log(cluster_id, "snapshot", "success", f"Snapshot {snapshot_id} collected.")
            return snapshot_id
        except Exception as exc:
            self._last_errors[cluster_id] = f"Collection failed: {type(exc).__name__}"
            self._record_log(cluster_id, "snapshot", "error", "Snapshot collection failed.")
            LOGGER.warning("Kubernetes collection failed for cluster %s: %s", cluster_id, type(exc).__name__)
            raise

    def _record_log(self, cluster_id: int, action: str, status: str, message: str) -> None:
        try:
            self.store.add_cluster_log(cluster_id, action, status, message)
            self.store.prune_cluster_logs(datetime.now(UTC) - timedelta(days=int(self.runtime_settings()["retention_days"])))
        except Exception as exc:
            LOGGER.warning("Cluster log update failed for cluster %s: %s", cluster_id, type(exc).__name__)
