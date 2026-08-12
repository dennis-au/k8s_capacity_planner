from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from kcp.config import RuntimeConfig
from kcp.models import ClusterSnapshot, NamespaceSummary, NodeSummary, ResourceValues, WorkloadSummary
from kcp.store import Store
from kcp.web import _namespace_resource_total, _namespace_resources, create_app


class _Collector:
    def test_connection(self) -> str:
        return "v1.36.1"

    def collect(self) -> ClusterSnapshot:
        return ClusterSnapshot(
            cluster_version="v1.36.1",
            metrics_available=True,
            nodes=[
                NodeSummary(
                    name="worker-a",
                    capacity=ResourceValues(cpu_millicores=2500, memory_bytes=3 * 1024**3),
                    allocatable=ResourceValues(cpu_millicores=2000, memory_bytes=2 * 1024**3),
                    requested=ResourceValues(cpu_millicores=500, memory_bytes=512 * 1024**2),
                    limits=ResourceValues(cpu_millicores=1000, memory_bytes=1024 * 1024**2),
                    usage=ResourceValues(cpu_millicores=300, memory_bytes=256 * 1024**2),
                ),
                NodeSummary(
                    name="control-plane-a",
                    capacity=ResourceValues(cpu_millicores=1000, memory_bytes=1024**3),
                    allocatable=ResourceValues(cpu_millicores=800, memory_bytes=768 * 1024**2),
                    requested=ResourceValues(cpu_millicores=600, memory_bytes=512 * 1024**2),
                    limits=ResourceValues(cpu_millicores=700, memory_bytes=768 * 1024**2),
                    usage=ResourceValues(cpu_millicores=500, memory_bytes=384 * 1024**2),
                    control_plane=True,
                )
            ],
            namespaces=[
                NamespaceSummary(name="payments", has_limit_range=True),
                NamespaceSummary(name="logging", has_limit_range=False),
            ],
            workloads=[
                WorkloadSummary(
                    namespace="payments",
                    kind="Deployment",
                    name="api",
                    replicas=2,
                    requests=ResourceValues(cpu_millicores=500, memory_bytes=512 * 1024**2),
                    limits=ResourceValues(cpu_millicores=1000, memory_bytes=1024 * 1024**2),
                    usage=ResourceValues(cpu_millicores=300, memory_bytes=256 * 1024**2),
                    qos="Burstable",
                    missing_requests=False,
                    has_hpa=True,
                    desired_replicas=2,
                    deployment_strategy="RollingUpdate",
                    rolling_update_max_surge="50%",
                    template_requests=ResourceValues(cpu_millicores=250, memory_bytes=256 * 1024**2),
                ),
                WorkloadSummary(
                    namespace="logging",
                    kind="Deployment",
                    name="collector",
                    replicas=1,
                    requests=ResourceValues(cpu_millicores=100, memory_bytes=64 * 1024**2),
                    limits=ResourceValues(cpu_millicores=200, memory_bytes=128 * 1024**2),
                    usage=ResourceValues(cpu_millicores=50, memory_bytes=32 * 1024**2),
                    qos="Burstable",
                    missing_requests=False,
                    has_hpa=False,
                    desired_replicas=1,
                    deployment_strategy="RollingUpdate",
                    rolling_update_max_surge="25%",
                    template_requests=ResourceValues(cpu_millicores=100, memory_bytes=64 * 1024**2),
                ),
            ],
        )


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config = RuntimeConfig(
            db_path=root / "kcp.sqlite3",
            docs_dir=Path("kcp/assets/k8s-docs"),
            refresh_seconds=3600,
            retention_days=90,
            admin_username="admin",
            insecure_http=True,
            session_secret="test-session-secret" * 4,
        )
        self.store = Store(self.config.db_path)
        self.store.migrate()
        self.store.bootstrap_admin("admin", "correct horse battery staple")
        self.app = create_app(self.config, store=self.store, collector_factory=lambda _: _Collector())
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_login_collect_export_and_local_docs(self) -> None:
        self.assertEqual(self.client.get("/").status_code, 302)
        login = self.client.get("/login")
        csrf = _csrf(login.text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": csrf},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Add a cluster", response.text)

        kubeconfig = _write_kubeconfig(Path(self.temp_dir.name), "configured", "https://kubernetes.darksite.local:6443")
        self.store.create_cluster("Production", str(kubeconfig), "configured", "https://kubernetes.darksite.local:6443")
        csrf = _csrf(response.text)
        refreshed = self.client.post("/collect", data={"csrf_token": csrf}, follow_redirects=True)
        self.assertIn("Snapshot 1 collected", refreshed.text)
        self.assertNotIn("Current capacity decision", refreshed.text)
        self.assertIn("Remaining after reserve", refreshed.text)

        nodes = self.client.get("/nodes")
        self.assertIn("worker-a", nodes.text)
        namespaces = self.client.get("/namespaces")
        self.assertIn("payments", namespaces.text)

        docs = self.client.get("/docs?q=Resource")
        self.assertIn("Resource Management", docs.text)
        self.assertNotIn("https://kubernetes.io/docs", docs.text.split("<pre", 1)[0])

        export = self.client.get("/exports/latest.json")
        self.assertEqual(export.status_code, 200)
        self.assertIn('"cluster_version":"v1.36.1"', export.text)

    def test_local_documentation_uses_readable_article_layout(self) -> None:
        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )

        document = self.client.get("/docs/limit-range")

        self.assertEqual(document.status_code, 200)
        self.assertIn("Offline Kubernetes reference", document.text)
        self.assertIn('class="doc-reading-layout"', document.text)
        self.assertIn("On this page", document.text)
        self.assertIn('class="doc-article"', document.text)
        self.assertNotIn('class="doc-content"', document.text)

    def test_local_documentation_renders_mermaid_without_exposing_source(self) -> None:
        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )

        document = self.client.get("/docs/horizontal-pod-autoscale")

        self.assertEqual(document.status_code, 200)
        self.assertIn('class="doc-diagram"', document.text)
        self.assertIn("HorizontalPodAutoscaler controls Scale.", document.text)
        self.assertNotIn("graph BT hpa", document.text)

    def test_dashboard_explains_management_capacity_and_cluster_status(self) -> None:
        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )
        kubeconfig = _write_kubeconfig(Path(self.temp_dir.name), "configured", "https://kubernetes.darksite.local:6443")
        self.store.create_cluster("Production", str(kubeconfig), "configured", "https://kubernetes.darksite.local:6443")

        overview = self.client.get("/")
        collected = self.client.post("/collect", data={"csrf_token": _csrf(overview.text)}, follow_redirects=True)
        self.assertIn("Dashboard", collected.text)
        self.assertNotIn("Current capacity decision", collected.text)
        self.assertNotIn("Capacity Available", collected.text)
        self.assertIn("Remaining after reserve", collected.text)
        self.assertIn('class="overview-dashboard"', collected.text)
        self.assertIn("Planning Reserve setting", collected.text)
        self.assertIn('class="capacity-chart-panel"', collected.text)
        self.assertIn("From worker allocatable capacity to remaining capacity", collected.text)
        self.assertIn("Scheduled requests", collected.text)
        self.assertIn("Remaining after reserve", collected.text)
        self.assertIn('aria-label="CPU capacity composition after reserve"', collected.text)
        self.assertIn('aria-label="Memory capacity composition after reserve"', collected.text)
        self.assertNotIn("2,500m total worker-node capacity", collected.text)
        self.assertNotIn("3,500m", collected.text)
        self.assertIn("1,600m total worker allocatable after 20% reserve", collected.text)
        self.assertIn("Planning Reserve setting", collected.text)
        self.assertIn('href="/settings"', collected.text)
        self.assertIn("namespace-resource-panel", collected.text)
        self.assertIn("Namespace resources", collected.text)
        self.assertIn("Actual used", collected.text)
        self.assertIn("All namespaces", collected.text)
        self.assertIn("<tfoot>", collected.text)
        self.assertIn("payments", collected.text)
        self.assertIn("logging", collected.text)
        self.assertNotIn("Concurrent rollout capacity", collected.text)
        self.assertNotIn("One Deployment rollout per namespace", collected.text)
        self.assertNotIn("Conservative envelope", collected.text)
        self.assertNotIn("30-day request trend", collected.text)
        self.assertNotIn("Full resource figures", collected.text)
        self.assertNotIn("Observed usage", collected.text)
        self.assertNotIn("KCP planning reserve", collected.text)
        self.assertNotIn("Snapshot quality", collected.text)

        clusters = self.client.get("/clusters")
        self.assertIn("Management decision", clusters.text)
        self.assertIn("Capacity Available", clusters.text)
        self.assertIn("Trend unavailable", clusters.text)

        reports = self.client.get("/reports")
        self.assertEqual(reports.status_code, 200)
        self.assertIn("Reports", reports.text)
        self.assertIn("30-day trend evidence", reports.text)

    def test_dashboard_loads_snapshot_history_for_capacity_trend(self) -> None:
        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )
        kubeconfig = _write_kubeconfig(Path(self.temp_dir.name), "configured", "https://kubernetes.darksite.local:6443")
        self.store.create_cluster("Production", str(kubeconfig), "configured", "https://kubernetes.darksite.local:6443")
        overview = self.client.get("/")
        self.client.post("/collect", data={"csrf_token": _csrf(overview.text)}, follow_redirects=True)

        with patch.object(self.store, "list_snapshots", wraps=self.store.list_snapshots) as snapshots:
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        snapshots.assert_called_once_with(1, limit=2_160)

    def test_dashboard_renders_factual_resource_trend(self) -> None:
        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )
        kubeconfig = _write_kubeconfig(Path(self.temp_dir.name), "configured", "https://kubernetes.darksite.local:6443")
        cluster = self.store.create_cluster("Production", str(kubeconfig), "configured", "https://kubernetes.darksite.local:6443")
        overview = self.client.get("/")
        self.client.post("/collect", data={"csrf_token": _csrf(overview.text)}, follow_redirects=True)
        current = self.store.latest_snapshot(cluster["id"])
        self.assertIsNotNone(current)
        assert current is not None
        earlier_payload = json.loads(json.dumps(current["payload"]))
        earlier_at = datetime.fromisoformat(current["collected_at"]) - timedelta(days=1)
        earlier_payload["snapshot"]["collected_at"] = earlier_at.isoformat()
        earlier_payload["snapshot"]["nodes"][0]["requested"] = {
            "cpu_millicores": 100,
            "memory_bytes": 128 * 1024**2,
        }
        earlier_payload["snapshot"]["nodes"][0]["limits"] = {
            "cpu_millicores": 800,
            "memory_bytes": 768 * 1024**2,
        }
        earlier_payload["snapshot"]["nodes"][0]["usage"] = {
            "cpu_millicores": 200,
            "memory_bytes": 256 * 1024**2,
        }
        self.store.save_snapshot(
            earlier_at,
            current["cluster_version"],
            earlier_payload,
            cluster_id=cluster["id"],
        )

        dashboard = self.client.get("/")

        self.assertIn("Resource trend", dashboard.text)
        self.assertIn("Actual use, requests, limits, and worker allocatable capacity", dashboard.text)
        self.assertIn("Worker allocatable after reserve", dashboard.text)
        self.assertIn("Requested", dashboard.text)
        self.assertIn("Limits", dashboard.text)
        self.assertIn("Actual used", dashboard.text)
        self.assertIn('class="trend-panel"', dashboard.text)
        self.assertIn('class="trend-capacity-area"', dashboard.text)
        self.assertIn('class="trend-request-line"', dashboard.text)
        self.assertIn('class="trend-limit-line"', dashboard.text)
        self.assertIn('class="trend-usage-line"', dashboard.text)
        self.assertIn("2 snapshots", dashboard.text)
        self.assertIn("Planning Reserve setting", dashboard.text)

    def test_first_login_has_no_cluster_until_a_kubeconfig_is_added(self) -> None:
        login = self.client.get("/login")
        overview = self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
            follow_redirects=True,
        )

        self.assertIn("Add a cluster", overview.text)
        self.assertIn("Not configured", overview.text)
        self.assertEqual(self.store.list_clusters(), [])

        clusters = self.client.get("/clusters")
        self.assertIn("No clusters configured.", clusters.text)

    def test_health_check_does_not_require_login(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertIn('"status":"ok"', response.text)

    def test_account_page_changes_password_and_keeps_current_session_active(self) -> None:
        self.assertEqual(self.client.get("/account").status_code, 302)

        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )
        account = self.client.get("/account")
        self.assertEqual(account.status_code, 200)
        self.assertIn("Administrator account", account.text)
        self.assertIn("admin", account.text)
        self.assertNotIn('minlength="12"', account.text)

        changed = self.client.post(
            "/account",
            data={
                "csrf_token": _csrf(account.text),
                "new_password": "x",
                "confirm_password": "x",
            },
            follow_redirects=True,
        )
        self.assertIn("Password updated.", changed.text)
        self.assertEqual(self.client.get("/clusters").status_code, 200)

        old_password_client = self.app.test_client()
        old_login = old_password_client.get("/login")
        old_password = old_password_client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(old_login.text)},
            follow_redirects=True,
        )
        self.assertIn("Invalid credentials.", old_password.text)

        new_password_client = self.app.test_client()
        new_login = new_password_client.get("/login")
        new_password = new_password_client.post(
            "/login",
            data={"username": "admin", "password": "x", "csrf_token": _csrf(new_login.text)},
            follow_redirects=True,
        )
        self.assertIn("Dashboard", new_password.text)

    def test_namespace_resources_aggregate_workloads_and_mark_incomplete_usage_unavailable(self) -> None:
        record = {
            "payload": {
                "snapshot": {
                    "metrics_available": True,
                    "namespaces": [{"name": "empty"}, {"name": "payments"}, {"name": "logging"}],
                    "workloads": [
                        {
                            "namespace": "payments",
                            "requests": {"cpu_millicores": 500, "memory_bytes": 512},
                            "limits": {"cpu_millicores": 1000, "memory_bytes": 1024},
                            "usage": {"cpu_millicores": 300, "memory_bytes": 256},
                        },
                        {
                            "namespace": "payments",
                            "requests": {"cpu_millicores": 25, "memory_bytes": 16},
                            "limits": {"cpu_millicores": 50, "memory_bytes": 32},
                            "usage": {"cpu_millicores": 20, "memory_bytes": 8},
                        },
                        {
                            "namespace": "logging",
                            "requests": {"cpu_millicores": 100, "memory_bytes": 64},
                            "limits": {"cpu_millicores": 200, "memory_bytes": 128},
                            "usage": None,
                        },
                    ],
                }
            }
        }

        rows = _namespace_resources(record)

        self.assertEqual([row["name"] for row in rows], ["empty", "logging", "payments"])
        self.assertEqual(rows[0]["requests"], {"cpu_millicores": 0, "memory_bytes": 0})
        self.assertEqual(rows[0]["limits"], {"cpu_millicores": 0, "memory_bytes": 0})
        self.assertEqual(rows[0]["usage"], {"cpu_millicores": 0, "memory_bytes": 0})
        self.assertEqual(rows[1]["usage"], None)
        self.assertEqual(rows[2]["requests"], {"cpu_millicores": 525, "memory_bytes": 528})
        self.assertEqual(rows[2]["limits"], {"cpu_millicores": 1050, "memory_bytes": 1056})
        self.assertEqual(rows[2]["usage"], {"cpu_millicores": 320, "memory_bytes": 264})

        total = _namespace_resource_total(rows)
        self.assertEqual(total["requests"], {"cpu_millicores": 625, "memory_bytes": 592})
        self.assertEqual(total["limits"], {"cpu_millicores": 1250, "memory_bytes": 1184})
        self.assertIsNone(total["usage"])

        record["payload"]["snapshot"]["metrics_available"] = False
        self.assertEqual(_namespace_resources(record)[0]["usage"], None)

    def test_dashboard_marks_namespace_usage_unavailable_when_a_workload_metric_is_missing(self) -> None:
        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )
        kubeconfig = _write_kubeconfig(Path(self.temp_dir.name), "configured", "https://kubernetes.darksite.local:6443")
        cluster = self.store.create_cluster("Production", str(kubeconfig), "configured", "https://kubernetes.darksite.local:6443")
        self.store.save_snapshot(
            datetime.now(UTC),
            "v1.36.1",
            {
                "snapshot": {
                    "metrics_available": True,
                    "nodes": [],
                    "namespaces": [{"name": "payments", "has_limit_range": True}],
                    "workloads": [
                        {
                            "namespace": "payments",
                            "kind": "Deployment",
                            "name": "api",
                            "replicas": 1,
                            "requests": {"cpu_millicores": 100, "memory_bytes": 64},
                            "limits": {"cpu_millicores": 200, "memory_bytes": 128},
                            "usage": None,
                        }
                    ],
                },
                "findings": [],
            },
            cluster_id=cluster["id"],
        )

        dashboard = self.client.get("/")

        self.assertIn("Namespace resources", dashboard.text)
        self.assertIn("Unavailable", dashboard.text)

    def test_account_page_rejects_mismatched_passwords_and_invalid_csrf(self) -> None:
        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )
        account = self.client.get("/account")

        mismatch = self.client.post(
            "/account",
            data={
                "csrf_token": _csrf(account.text),
                "new_password": "a newer correct password",
                "confirm_password": "a different valid password",
            },
            follow_redirects=True,
        )
        self.assertIn("New password confirmation does not match.", mismatch.text)
        self.assertTrue(self.store.verify_admin("admin", "correct horse battery staple"))

        invalid_csrf = self.client.post(
            "/account",
            data={
                "csrf_token": "invalid",
                "new_password": "a newer correct password",
                "confirm_password": "a newer correct password",
            },
        )
        self.assertEqual(invalid_csrf.status_code, 400)
        self.assertTrue(self.store.verify_admin("admin", "correct horse battery staple"))

    def test_settings_page_updates_runtime_collection_policy(self) -> None:
        self.assertEqual(self.client.get("/settings").status_code, 302)

        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )
        settings = self.client.get("/settings")
        self.assertIn("Collection settings", settings.text)
        self.assertIn('value="60"', settings.text)
        self.assertIn('value="90"', settings.text)
        self.assertIn('value="20"', settings.text)

        updated = self.client.post(
            "/settings",
            data={
                "csrf_token": _csrf(settings.text),
                "snapshot_interval_minutes": "30",
                "retention_days": "180",
                "planning_reserve_percent": "25",
            },
            follow_redirects=True,
        )
        self.assertIn("Runtime settings updated.", updated.text)
        self.assertIn("Paused", updated.text)
        self.assertIn('value="30"', updated.text)
        self.assertIn('value="180"', updated.text)
        self.assertIn('value="25"', updated.text)
        self.assertEqual(
            self.app.extensions["kcp_service"].runtime_settings(),
            {
                "schedule_enabled": False,
                "snapshot_interval_minutes": 30,
                "retention_days": 180,
                "planning_reserve_percent": 25,
            },
        )

        invalid = self.client.post(
            "/settings",
            data={
                "csrf_token": _csrf(updated.text),
                "schedule_enabled": "1",
                "snapshot_interval_minutes": "1",
                "retention_days": "180",
                "planning_reserve_percent": "25",
            },
            follow_redirects=True,
        )
        self.assertIn("Snapshot interval must be between 15 and 1440 minutes", invalid.text)
        self.assertIn("Paused", invalid.text)

        invalid_csrf = self.client.post(
            "/settings",
            data={
                "csrf_token": "invalid",
                "snapshot_interval_minutes": "30",
                "retention_days": "180",
                "planning_reserve_percent": "25",
            },
        )
        self.assertEqual(invalid_csrf.status_code, 400)

    def test_dashboard_uses_the_configured_planning_reserve_for_worker_allocatable_capacity(self) -> None:
        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )
        kubeconfig = _write_kubeconfig(Path(self.temp_dir.name), "configured", "https://kubernetes.darksite.local:6443")
        self.store.create_cluster("Production", str(kubeconfig), "configured", "https://kubernetes.darksite.local:6443")
        overview = self.client.get("/")
        self.client.post("/collect", data={"csrf_token": _csrf(overview.text)}, follow_redirects=True)

        settings = self.client.get("/settings")
        self.client.post(
            "/settings",
            data={
                "csrf_token": _csrf(settings.text),
                "snapshot_interval_minutes": "60",
                "retention_days": "90",
                "planning_reserve_percent": "25",
            },
            follow_redirects=True,
        )

        dashboard = self.client.get("/")

        self.assertIn("1,500m total worker allocatable after 25% reserve", dashboard.text)
        self.assertIn("current 25% Planning Reserve setting", dashboard.text)

    def test_cluster_connection_can_be_saved_from_the_dashboard(self) -> None:
        kubeconfig = _write_kubeconfig(
            Path(self.temp_dir.name), "prod-west-readonly", "https://kubernetes.prod.example:6443", "prod-west.kubeconfig"
        )

        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )

        cluster = self.client.get("/clusters")
        self.assertEqual(cluster.status_code, 200)
        self.assertIn("No clusters configured.", cluster.text)

        new_cluster = self.client.get("/clusters/new")
        self.assertIn("Upload file", new_cluster.text)
        self.assertIn("Paste configuration", new_cluster.text)

        saved = self.client.post(
            "/clusters/new",
            data={
                "csrf_token": _csrf(new_cluster.text),
                "name": "Production West",
                "kubeconfig_source": "path",
                "kubeconfig_file": str(kubeconfig),
                "kube_context": "prod-west-readonly",
                "api_ip": "10.20.30.40",
                "disable_proxy": "1",
            },
            follow_redirects=True,
        )

        self.assertIn("Cluster connection saved.", saved.text)
        self.assertEqual(len(self.store.list_clusters()), 1)
        production_west = self.store.first_cluster()
        self.assertEqual(production_west["endpoint"], "https://10.20.30.40:6443")
        self.assertEqual(production_west["kube_context"], "prod-west-readonly")
        self.assertEqual(production_west["api_ip"], "10.20.30.40")
        self.assertTrue(production_west["disable_proxy"])
        self.assertIn("Do not use HTTP(S) proxy for this cluster", saved.text)
        self.assertRegex(saved.text, r'name="disable_proxy" value="1"\s+checked')
        self.assertNotIn(b"read-only-token", self.store.db_path.read_bytes())

    def test_cluster_connection_can_be_saved_from_pasted_kubeconfig(self) -> None:
        kubeconfig = _write_kubeconfig(
            Path(self.temp_dir.name), "prod-paste", "https://paste.example:6443", "paste.kubeconfig"
        )
        contents = kubeconfig.read_text(encoding="utf-8")
        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )
        new_cluster = self.client.get("/clusters/new")

        saved = self.client.post(
            "/clusters/new",
            data={
                "csrf_token": _csrf(new_cluster.text),
                "name": "Pasted cluster",
                "kubeconfig_source": "paste",
                "kubeconfig_text": contents,
                "kube_context": "prod-paste",
            },
            follow_redirects=True,
        )

        cluster = self.store.first_cluster()
        self.assertIsNotNone(cluster)
        stored_file = Path(cluster["kubeconfig_file"])
        self.assertEqual(stored_file.parent, Path(self.temp_dir.name) / "kubeconfigs")
        self.assertEqual(stored_file.read_text(encoding="utf-8"), contents)
        self.assertNotIn("read-only-token", saved.text)
        self.assertNotIn(b"read-only-token", self.store.db_path.read_bytes())

        removal = self.client.get(f"/clusters/{cluster['id']}/remove")
        self.client.post(
            f"/clusters/{cluster['id']}/remove",
            data={"csrf_token": _csrf(removal.text)},
            follow_redirects=True,
        )
        self.assertFalse(stored_file.exists())

    def test_cluster_connection_can_be_saved_from_uploaded_kubeconfig(self) -> None:
        kubeconfig = _write_kubeconfig(
            Path(self.temp_dir.name), "prod-upload", "https://upload.example:6443", "upload.kubeconfig"
        )
        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )
        new_cluster = self.client.get("/clusters/new")

        saved = self.client.post(
            "/clusters/new",
            data={
                "csrf_token": _csrf(new_cluster.text),
                "name": "Uploaded cluster",
                "kubeconfig_source": "upload",
                "kubeconfig_upload": (io.BytesIO(kubeconfig.read_bytes()), "production.yaml"),
                "kube_context": "prod-upload",
            },
            follow_redirects=True,
        )

        self.assertIn("Cluster connection saved.", saved.text)
        cluster = self.store.first_cluster()
        self.assertIsNotNone(cluster)
        self.assertTrue(Path(cluster["kubeconfig_file"]).is_file())
        self.assertEqual(cluster["endpoint"], "https://upload.example:6443")
        self.assertNotIn(b"read-only-token", self.store.db_path.read_bytes())

    def test_cluster_connection_rejects_missing_and_unsafe_kubeconfig_files(self) -> None:
        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )
        cluster = self.client.get("/clusters/new")

        invalid = self.client.post(
            "/clusters/new",
            data={
                "csrf_token": _csrf(cluster.text),
                "name": "Invalid",
                "kubeconfig_source": "path",
                "kubeconfig_file": "/does/not/exist",
                "kube_context": "missing",
                "api_ip": "not-an-ip",
            },
            follow_redirects=True,
        )

        self.assertIn("Kubeconfig file must point to a readable mounted file.", invalid.text)

        unsafe_kubeconfig = _write_kubeconfig(
            Path(self.temp_dir.name), "unsafe", "https://unsafe.darksite.local:6443", "unsafe.kubeconfig", user="exec:\n      command: credential-helper"
        )
        unsafe = self.client.post(
            "/clusters/new",
            data={
                "csrf_token": _csrf(invalid.text),
                "name": "Unsafe",
                "kubeconfig_source": "path",
                "kubeconfig_file": str(unsafe_kubeconfig),
                "kube_context": "unsafe",
            },
            follow_redirects=True,
        )
        self.assertIn("exec or auth-provider", unsafe.text)

        pasted = self.client.post(
            "/clusters/new",
            data={
                "csrf_token": _csrf(unsafe.text),
                "name": "Unsafe pasted",
                "kubeconfig_source": "paste",
                "kubeconfig_text": unsafe_kubeconfig.read_text(encoding="utf-8"),
                "kube_context": "unsafe",
            },
            follow_redirects=True,
        )
        self.assertIn("exec or auth-provider", pasted.text)
        self.assertNotIn("credential-helper", pasted.text)
        self.assertFalse((Path(self.temp_dir.name) / "kubeconfigs").exists())

    def test_cluster_can_be_edited_and_removed_from_the_dashboard(self) -> None:
        kubeconfig = _write_kubeconfig(
            Path(self.temp_dir.name),
            "west-readonly",
            "https://west.example:6443",
            "west.kubeconfig",
        )
        west = self.store.create_cluster(
            "Production West",
            str(kubeconfig),
            "west-readonly",
            "https://west.example:6443",
        )

        login = self.client.get("/login")
        overview = self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
            follow_redirects=True,
        )
        selected = self.client.post(
            "/clusters/activate",
            data={"csrf_token": _csrf(overview.text), "cluster_id": west["id"], "next": "/clusters"},
            follow_redirects=True,
        )
        self.assertIn(f'href="/clusters/{west["id"]}"', selected.text)
        self.assertIn(f'href="/clusters/{west["id"]}/remove"', selected.text)
        self.store.save_snapshot(
            datetime.now(UTC),
            "v1.36.0",
            {"cluster": "west"},
            cluster_id=west["id"],
        )

        edit = self.client.get(f"/clusters/{west['id']}")
        updated = self.client.post(
            f"/clusters/{west['id']}",
            data={
                "csrf_token": _csrf(edit.text),
                "name": "Production West Updated",
                "kubeconfig_source": "path",
                "kubeconfig_file": str(kubeconfig),
                "kube_context": "west-readonly",
                "api_ip": "",
            },
            follow_redirects=True,
        )
        self.assertIn("Cluster connection saved.", updated.text)
        self.assertEqual(self.store.get_cluster(west["id"])["name"], "Production West Updated")

        confirmation = self.client.get(f"/clusters/{west['id']}/remove")
        self.assertIn("Remove Production West Updated?", confirmation.text)
        removed = self.client.post(
            f"/clusters/{west['id']}/remove",
            data={"csrf_token": _csrf(confirmation.text)},
            follow_redirects=True,
        )

        self.assertIn("Cluster connection and stored reports removed.", removed.text)
        self.assertIsNone(self.store.get_cluster(west["id"]))
        self.assertEqual(self.store.list_snapshots(west["id"]), [])
        self.assertIn("No clusters configured.", removed.text)

    def test_cluster_configuration_tests_connection_collects_snapshot_and_shows_log(self) -> None:
        kubeconfig = _write_kubeconfig(
            Path(self.temp_dir.name),
            "operations-readonly",
            "https://operations.example:6443",
            "operations.kubeconfig",
        )
        cluster = self.store.create_cluster(
            "Operations",
            str(kubeconfig),
            "operations-readonly",
            "https://operations.example:6443",
        )
        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )

        edit = self.client.get(f"/clusters/{cluster['id']}")
        self.assertIn("Test connection", edit.text)
        self.assertNotIn("Take snapshot", edit.text)
        self.assertIn("Connection log", edit.text)
        self.assertIn("No cluster operations recorded.", edit.text)

        tested = self.client.post(
            f"/clusters/{cluster['id']}/test",
            data={"csrf_token": _csrf(edit.text)},
            follow_redirects=True,
        )
        self.assertIn("Connection verified: v1.36.1.", tested.text)
        self.assertIn("Connected to Kubernetes v1.36.1.", tested.text)
        self.assertEqual(self.store.list_cluster_logs(cluster["id"])[0]["action"], "connection-test")

        clusters = self.client.get("/clusters")
        self.assertIn(f'action="/clusters/{cluster["id"]}/snapshot"', clusters.text)
        self.assertIn("Take snapshot", clusters.text)
        snapped = self.client.post(
            f"/clusters/{cluster['id']}/snapshot",
            data={"csrf_token": _csrf(clusters.text)},
            follow_redirects=True,
        )
        self.assertIn("Snapshot 1 collected.", snapped.text)
        self.assertIn("Configured clusters", snapped.text)
        self.assertIsNotNone(self.store.latest_snapshot(cluster["id"]))
        logs = self.store.list_cluster_logs(cluster["id"])
        self.assertEqual([log["action"] for log in logs], ["snapshot", "connection-test"])

        invalid_csrf = self.client.post(f"/clusters/{cluster['id']}/test", data={"csrf_token": "invalid"})
        self.assertEqual(invalid_csrf.status_code, 400)
        invalid_snapshot_csrf = self.client.post(
            f"/clusters/{cluster['id']}/snapshot", data={"csrf_token": "invalid"}
        )
        self.assertEqual(invalid_snapshot_csrf.status_code, 400)
        missing = self.client.post(
            "/clusters/9999/snapshot", data={"csrf_token": _csrf(snapped.text)}
        )
        self.assertEqual(missing.status_code, 404)

    def test_cluster_snapshot_action_reports_failure_and_overlap_on_clusters_page(self) -> None:
        kubeconfig = _write_kubeconfig(
            Path(self.temp_dir.name),
            "operations-readonly",
            "https://operations.example:6443",
            "operations.kubeconfig",
        )
        cluster = self.store.create_cluster(
            "Operations",
            str(kubeconfig),
            "operations-readonly",
            "https://operations.example:6443",
        )
        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )
        clusters = self.client.get("/clusters")
        service = self.app.extensions["kcp_service"]

        with patch.object(service, "collect_now", side_effect=RuntimeError):
            failed = self.client.post(
                f"/clusters/{cluster['id']}/snapshot",
                data={"csrf_token": _csrf(clusters.text)},
                follow_redirects=True,
            )
        self.assertIn("Snapshot collection failed.", failed.text)
        self.assertIn("Configured clusters", failed.text)

        with patch.object(service, "collect_now", return_value=None):
            overlapping = self.client.post(
                f"/clusters/{cluster['id']}/snapshot",
                data={"csrf_token": _csrf(failed.text)},
                follow_redirects=True,
            )
        self.assertIn("Another cluster operation is already running.", overlapping.text)
        self.assertIn("Configured clusters", overlapping.text)

    def test_legacy_cluster_does_not_offer_or_run_snapshot_action(self) -> None:
        now = datetime.now(UTC).isoformat()
        with self.store._connection() as connection:
            result = connection.execute(
                """
                INSERT INTO clusters(
                    name, endpoint, kubeconfig_file, kube_context, api_ip,
                    legacy_token_file, legacy_ca_file, created_at, updated_at
                ) VALUES (?, ?, NULL, NULL, NULL, ?, ?, ?, ?)
                """,
                ("Legacy", "https://legacy.example:6443", "/run/legacy.token", "/run/legacy.ca", now, now),
            )
            cluster_id = int(result.lastrowid)
        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )

        clusters = self.client.get("/clusters")
        self.assertNotIn(f'action="/clusters/{cluster_id}/snapshot"', clusters.text)
        blocked = self.client.post(
            f"/clusters/{cluster_id}/snapshot",
            data={"csrf_token": _csrf(clusters.text)},
            follow_redirects=True,
        )

        self.assertIn("Update this legacy cluster with a kubeconfig", blocked.text)
        self.assertIn("Configured clusters", blocked.text)
        self.assertIsNone(self.store.latest_snapshot(cluster_id))

    def test_default_collector_uses_the_saved_cluster_connection(self) -> None:
        kubeconfig = _write_kubeconfig(
            Path(self.temp_dir.name), "prod-west-readonly", "https://kubernetes.prod.example:6443", "saved.kubeconfig"
        )
        cluster = self.store.create_cluster(
            "Production West",
            str(kubeconfig),
            "prod-west-readonly",
            "https://kubernetes.prod.example:6443",
        )

        with patch("kcp.web.KubernetesCollector.from_kubeconfig") as from_kubeconfig:
            app = create_app(self.config, store=self.store, start_scheduler=False)
            app.extensions["kcp_service"].collector_factory(cluster)

        from_kubeconfig.assert_called_once_with(str(kubeconfig), "prod-west-readonly", None, False)

    def test_active_cluster_scopes_manual_collection_and_history(self) -> None:
        west = self.store.create_cluster(
            "Production West",
            "/run/kcp/west.kubeconfig",
            "west-readonly",
            "https://west.example:6443",
        )

        login = self.client.get("/login")
        overview = self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
            follow_redirects=True,
        )
        selected = self.client.post(
            "/clusters/activate",
            data={"csrf_token": _csrf(overview.text), "cluster_id": west["id"], "next": "/"},
            follow_redirects=True,
        )
        self.assertIn("Production West", selected.text)

        collected = self.client.post("/collect", data={"csrf_token": _csrf(selected.text)}, follow_redirects=True)

        self.assertIn("Snapshot 1 collected", collected.text)
        self.assertIsNotNone(self.store.latest_snapshot(west["id"]))

        history = self.client.get("/history")
        self.assertIn("v1.36.1", history.text)
        export = self.client.get("/exports/latest.md")
        self.assertIn("Cluster: Production West", export.text)

    def test_switching_active_clusters_keeps_reports_findings_and_exports_isolated(self) -> None:
        east = self.store.create_cluster("East", "/run/east.kubeconfig", "east", "https://east.example")
        west = self.store.create_cluster("West", "/run/west.kubeconfig", "west", "https://west.example")
        east_payload = _report_payload("East finding")
        west_payload = _report_payload("West finding")
        self.store.save_snapshot(datetime.now(UTC), "v1.36.0", east_payload, cluster_id=east["id"])
        self.store.save_snapshot(datetime.now(UTC), "v1.36.1", west_payload, cluster_id=west["id"])

        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )

        east_page = self.client.get("/clusters")
        east_selected = self.client.post(
            "/clusters/activate",
            data={"csrf_token": _csrf(east_page.text), "cluster_id": east["id"], "next": "/history"},
            follow_redirects=True,
        )
        self.assertIn("v1.36.0", east_selected.text)
        self.assertNotIn("v1.36.1", east_selected.text)
        self.assertIn("East finding evidence.", self.client.get("/findings").text)
        self.assertNotIn("West finding evidence.", self.client.get("/findings").text)
        self.assertIn("Cluster: East", self.client.get("/exports/latest.md").text)

        west_page = self.client.get("/clusters")
        west_selected = self.client.post(
            "/clusters/activate",
            data={"csrf_token": _csrf(west_page.text), "cluster_id": west["id"], "next": "/history"},
            follow_redirects=True,
        )
        self.assertIn("v1.36.1", west_selected.text)
        self.assertNotIn("v1.36.0", west_selected.text)
        self.assertIn("West finding evidence.", self.client.get("/findings").text)
        self.assertNotIn("East finding evidence.", self.client.get("/findings").text)
        self.assertIn("Cluster: West", self.client.get("/exports/latest.md").text)

    def test_allocation_view_uses_persisted_cluster_snapshot(self) -> None:
        kubeconfig = _write_kubeconfig(Path(self.temp_dir.name), "configured", "https://kubernetes.darksite.local:6443")
        self.store.create_cluster("Production", str(kubeconfig), "configured", "https://kubernetes.darksite.local:6443")
        login = self.client.get("/login")
        overview = self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
            follow_redirects=True,
        )
        self.client.post("/collect", data={"csrf_token": _csrf(overview.text)}, follow_redirects=True)

        allocation = self.client.get("/allocation")

        self.assertEqual(allocation.status_code, 200)
        self.assertIn("Request-based scheduling capacity", allocation.text)
        self.assertIn("payments/Deployment/api", allocation.text)
        self.assertIn("Resource Management for Pods and Containers", allocation.text)

    def test_dashboard_shows_facts_without_deployment_approval(self) -> None:
        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )
        kubeconfig = _write_kubeconfig(Path(self.temp_dir.name), "configured", "https://kubernetes.darksite.local:6443")
        self.store.create_cluster("Production", str(kubeconfig), "configured", "https://kubernetes.darksite.local:6443")
        overview = self.client.get("/")
        self.client.post("/collect", data={"csrf_token": _csrf(overview.text)}, follow_redirects=True)

        dashboard = self.client.get("/")
        self.assertIn("From worker allocatable capacity to remaining capacity", dashboard.text)
        self.assertIn("Remaining after reserve", dashboard.text)
        self.assertNotIn("Next action", dashboard.text)
        self.assertNotIn("Management follow-up", dashboard.text)
        self.assertNotIn("Items that need attention", dashboard.text)
        self.assertNotIn("Recommended action", dashboard.text)
        self.assertNotIn("Current capacity decision", dashboard.text)
        self.assertNotIn("Deployment approval", dashboard.text)
        self.assertNotIn("Can this deployment be approved?", dashboard.text)
        self.assertNotIn("Resource-only planning estimate", dashboard.text)
        self.assertNotIn("30-day request trend", dashboard.text)
        self.assertNotIn("Full resource figures", dashboard.text)
        self.assertNotIn("Observed usage", dashboard.text)
        self.assertNotIn("KCP planning reserve", dashboard.text)
        self.assertNotIn("Snapshot quality", dashboard.text)
        self.assertEqual(len(self.store.list_snapshots()), 1)

        self.assertEqual(self.client.post("/").status_code, 405)

        allocation = self.client.get("/allocation")
        self.assertIn("Request-based scheduling capacity", allocation.text)
        self.assertNotIn("Can this deployment be approved?", allocation.text)

    def test_stale_reports_show_data_quality_and_export_capacity_provenance(self) -> None:
        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )
        kubeconfig = _write_kubeconfig(Path(self.temp_dir.name), "configured", "https://kubernetes.darksite.local:6443")
        cluster = self.store.create_cluster("Production", str(kubeconfig), "configured", "https://kubernetes.darksite.local:6443")
        payload = _report_payload("Stale report")
        payload["snapshot"]["warnings"] = ["Metrics API unavailable: ApiException"]
        self.store.save_snapshot(datetime.now(UTC) - timedelta(hours=3), "v1.36.0", payload, cluster_id=cluster["id"])

        overview = self.client.get("/")
        self.assertIn("Capacity flow", overview.text)
        self.assertIn("Take a new snapshot to collect worker-node capacity", overview.text)
        self.assertNotIn("Report stale", overview.text)
        self.assertNotIn("Collection limitations", overview.text)
        self.assertNotIn("Metrics API unavailable", overview.text)

        export_json = self.client.get("/exports/latest.json")
        export_markdown = self.client.get("/exports/latest.md")
        self.assertIn('"management_capacity"', export_json.text)
        self.assertIn("Planning-safe capacity", export_markdown.text)
        self.assertIn("/docs/node-allocatable", export_markdown.text)

    def test_reports_exports_and_legacy_history_share_management_evidence(self) -> None:
        login = self.client.get("/login")
        self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple", "csrf_token": _csrf(login.text)},
        )
        kubeconfig = _write_kubeconfig(Path(self.temp_dir.name), "configured", "https://kubernetes.darksite.local:6443")
        cluster = self.store.create_cluster("Production", str(kubeconfig), "configured", "https://kubernetes.darksite.local:6443")
        overview = self.client.get("/")
        self.client.post("/collect", data={"csrf_token": _csrf(overview.text)}, follow_redirects=True)
        historical_record = self.store.latest_snapshot(cluster["id"])
        self.assertIsNotNone(historical_record)
        assert historical_record is not None
        newer_payload = json.loads(json.dumps(historical_record["payload"]))
        newer_payload["snapshot"]["cluster_version"] = "v9.99.0"
        newer_payload["snapshot"]["namespaces"].append({"name": "future", "has_limit_range": False})
        self.store.save_snapshot(
            datetime.now(UTC) + timedelta(minutes=1),
            "v9.99.0",
            newer_payload,
            cluster_id=cluster["id"],
        )

        reports = self.client.get("/reports")
        history = self.client.get("/history")
        export_json = self.client.get("/exports/latest.json")
        export_markdown = self.client.get("/exports/latest.md")
        export_html = self.client.get(f"/exports/{historical_record['id']}.html")
        settings = self.client.get("/settings")
        allocation = self.client.get("/allocation")

        self.assertEqual(reports.status_code, 200)
        self.assertEqual(history.status_code, 200)
        self.assertIn("Stored capacity reports", reports.text)
        self.assertIn("Stored capacity reports", history.text)
        self.assertIn("Request-based scheduling capacity", allocation.text)
        self.assertNotIn("Can this deployment be approved?", allocation.text)
        self.assertIn("Capacity Planner policy", settings.text)

        management = json.loads(export_json.text)["management_capacity"]
        self.assertEqual(management["decision"]["state"], "Capacity Available")
        self.assertEqual(management["capacity_flow"]["total_node_capacity"]["cpu_millicores"], 3_500)
        self.assertEqual(management["capacity_flow"]["scheduled_requests"]["cpu_millicores"], 1_100)
        self.assertEqual(management["source"]["document_id"], "node-allocatable")
        self.assertIn("Capacity Planner policy", export_markdown.text)
        self.assertIn("Management decision: Capacity Available", export_markdown.text)
        self.assertIn("Total Node Capacity", export_markdown.text)
        self.assertIn("/docs/node-allocatable", export_markdown.text)
        self.assertIn("Snapshot dashboard", export_html.text)
        self.assertIn("Snapshot time", export_html.text)
        self.assertIn(historical_record["collected_at"], export_html.text)
        self.assertIn("Capacity flow", export_html.text)
        self.assertIn("Namespace resources", export_html.text)
        self.assertIn("All namespaces", export_html.text)
        self.assertIn("payments", export_html.text)
        self.assertIn("v1.36.1", export_html.text)
        self.assertIn('class="dashboard-card"', export_html.text)
        self.assertIn("style-src 'unsafe-inline'", export_html.headers["Content-Security-Policy"])
        self.assertNotIn("v9.99.0", export_html.text)
        self.assertNotIn("future", export_html.text)
        self.assertNotIn("<pre>", export_html.text)


def _csrf(text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', text)
    if not match:
        raise AssertionError("CSRF token missing")
    return match.group(1)


def _report_payload(title: str) -> dict:
    return {
        "snapshot": {
            "cluster_version": "v1.36.0",
            "metrics_available": False,
            "nodes": [],
            "namespaces": [],
            "workloads": [],
            "warnings": [],
        },
        "findings": [
            {
                "severity": "info",
                "resource": "cluster",
                "title": title,
                "evidence": f"{title} evidence.",
                "recommendation": "Review the active cluster report.",
                "source": {
                    "document_id": "resource-management",
                    "document_title": "Resource Management",
                    "section": "Overview",
                },
            }
        ],
    }


def _write_kubeconfig(root: Path, context: str, endpoint: str, filename: str = "kubeconfig", user: str = "token: read-only-token") -> Path:
    kubeconfig = root / filename
    kubeconfig.write_text(
        f"""apiVersion: v1
kind: Config
clusters:
- name: configured
  cluster:
    server: {endpoint}
contexts:
- name: {context}
  context:
    cluster: configured
    user: kcp-reader
current-context: {context}
users:
- name: kcp-reader
  user:
    {user}
""",
        encoding="utf-8",
    )
    return kubeconfig
