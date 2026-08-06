from __future__ import annotations

import io
import re
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from kcp.config import RuntimeConfig
from kcp.models import ClusterSnapshot, NamespaceSummary, NodeSummary, ResourceValues, WorkloadSummary
from kcp.store import Store
from kcp.web import create_app


class _Collector:
    def collect(self) -> ClusterSnapshot:
        return ClusterSnapshot(
            cluster_version="v1.36.1",
            metrics_available=True,
            nodes=[
                NodeSummary(
                    name="worker-a",
                    allocatable=ResourceValues(cpu_millicores=2000, memory_bytes=2 * 1024**3),
                    requested=ResourceValues(cpu_millicores=500, memory_bytes=512 * 1024**2),
                    limits=ResourceValues(cpu_millicores=1000, memory_bytes=1024 * 1024**2),
                )
            ],
            namespaces=[NamespaceSummary(name="payments", has_limit_range=True)],
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
                )
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
        self.assertIn("payments/Deployment/api", refreshed.text)

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

        changed = self.client.post(
            "/account",
            data={
                "csrf_token": _csrf(account.text),
                "new_password": "a newer correct password",
                "confirm_password": "a newer correct password",
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
            data={"username": "admin", "password": "a newer correct password", "csrf_token": _csrf(new_login.text)},
            follow_redirects=True,
        )
        self.assertIn("Overview", new_password.text)

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
            },
            follow_redirects=True,
        )

        self.assertIn("Cluster connection saved.", saved.text)
        self.assertEqual(len(self.store.list_clusters()), 1)
        production_west = self.store.first_cluster()
        self.assertEqual(production_west["endpoint"], "https://10.20.30.40:6443")
        self.assertEqual(production_west["kube_context"], "prod-west-readonly")
        self.assertEqual(production_west["api_ip"], "10.20.30.40")
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

        from_kubeconfig.assert_called_once_with(str(kubeconfig), "prod-west-readonly", None)

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


def _csrf(text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', text)
    if not match:
        raise AssertionError("CSRF token missing")
    return match.group(1)


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
