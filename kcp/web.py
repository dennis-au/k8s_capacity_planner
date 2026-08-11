from __future__ import annotations

import json
import logging
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, abort, flash, jsonify, redirect, render_template_string, request, session, url_for
from markupsafe import Markup

from kcp.allocation import build_allocation_plan, build_management_decision
from kcp.config import RuntimeConfig
from kcp.docs import DocumentRegistry, render_document
from kcp.kubeconfig_files import MAX_KUBECONFIG_BYTES, KubeconfigFiles
from kcp.kubernetes import KubernetesCollector, inspect_kubeconfig, inspect_kubeconfig_text
from kcp.service import CollectionService
from kcp.store import Store


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReportQuality:
    state: str
    message: str
    warnings: list[str]


def create_app(
    config: RuntimeConfig,
    store: Store | None = None,
    collector_factory: Callable[[dict[str, Any]], KubernetesCollector] | None = None,
    start_scheduler: bool = True,
) -> Flask:
    store = store or Store(config.db_path)
    store.migrate()
    kubeconfig_files = KubeconfigFiles(config.db_path.parent / "kubeconfigs")
    docs = DocumentRegistry(config.docs_dir)
    collector_factory = collector_factory or _collector_factory(store)
    service = CollectionService(config, store, docs, collector_factory)
    throttle = _LoginThrottle()

    app = Flask(__name__, static_folder="static")
    app.config.update(
        SECRET_KEY=config.session_secret,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=not config.insecure_http,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
        MAX_CONTENT_LENGTH=MAX_KUBECONFIG_BYTES,
    )
    app.extensions["kcp_store"] = store
    app.extensions["kcp_docs"] = docs
    app.extensions["kcp_kubeconfig_files"] = kubeconfig_files
    app.extensions["kcp_service"] = service

    @app.before_request
    def require_authentication() -> Response | None:
        if request.endpoint in {"login", "healthz", "static"}:
            return None
        if session.get("username") != config.admin_username:
            return redirect(url_for("login", next=request.full_path if request.method == "GET" else ""))
        return None

    @app.after_request
    def set_security_headers(response: Response) -> Response:
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        if not config.insecure_http:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.get("/healthz")
    def healthz() -> Response:
        return jsonify(status="ok", snapshotAvailable=store.latest_snapshot() is not None)

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Response | str:
        if request.method == "POST":
            _validate_csrf()
            if not throttle.permit(request.remote_addr or "unknown"):
                flash("Too many login attempts. Try again later.", "error")
                return redirect(url_for("login"))
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if store.verify_admin(username, password):
                throttle.reset(request.remote_addr or "unknown")
                session.clear()
                session["username"] = username
                session["csrf_token"] = secrets.token_urlsafe(32)
                session.permanent = True
                next_url = request.args.get("next", "")
                return redirect(next_url if next_url.startswith("/") and not next_url.startswith("//") else url_for("overview"))
            throttle.record_failure(request.remote_addr or "unknown")
            flash("Invalid credentials.", "error")
        return _render("Sign in", _LOGIN_TEMPLATE, authenticated=False)

    @app.post("/logout")
    def logout() -> Response:
        _validate_csrf()
        session.clear()
        return redirect(url_for("login"))

    @app.route("/account", methods=["GET", "POST"])
    def account() -> Response | str:
        if request.method == "POST":
            _validate_csrf()
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            if new_password != confirm_password:
                flash("New password confirmation does not match.", "error")
            else:
                try:
                    store.reset_admin_password(config.admin_username, new_password)
                except ValueError as exc:
                    flash(str(exc), "error")
                else:
                    flash("Password updated.", "success")
                    return redirect(url_for("account"))
        return _render("Account", _ACCOUNT_TEMPLATE, username=config.admin_username)

    @app.route("/settings", methods=["GET", "POST"])
    def settings() -> Response | str:
        runtime_settings = service.runtime_settings()
        if request.method == "POST":
            _validate_csrf()
            try:
                service.update_runtime_settings(
                    request.form.get("schedule_enabled") == "1",
                    int(request.form.get("snapshot_interval_minutes", "")),
                    int(request.form.get("retention_days", "")),
                    int(request.form.get("planning_reserve_percent", runtime_settings["planning_reserve_percent"])),
                )
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                flash("Runtime settings updated.", "success")
                return redirect(url_for("settings"))
        return _render("Settings", _SETTINGS_TEMPLATE, settings=runtime_settings)

    @app.get("/")
    def overview() -> str:
        active_cluster = _active_cluster(store)
        record = store.latest_snapshot(active_cluster["id"]) if active_cluster else None
        runtime_settings = service.runtime_settings()
        plan = _allocation_plan(
            store,
            docs,
            record,
            active_cluster["id"] if active_cluster else None,
            int(runtime_settings["planning_reserve_percent"]),
            include_history=True,
        )
        return _render(
            "Dashboard",
            _DASHBOARD_PAGE_TEMPLATE,
            record=record,
            plan=plan,
            capacity_charts=_capacity_charts(plan),
            trend_charts=_trend_charts(plan),
            namespace_resources=_namespace_resources(record),
            connection=active_cluster,
        )

    @app.get("/cluster")
    def legacy_cluster() -> Response:
        return redirect(url_for("clusters"))

    @app.get("/clusters")
    def clusters() -> str:
        runtime_settings = service.runtime_settings()
        clusters = []
        for cluster in store.list_clusters():
            record = store.latest_snapshot(cluster["id"])
            plan = _allocation_plan(
                store,
                docs,
                record,
                cluster["id"],
                int(runtime_settings["planning_reserve_percent"]),
                include_history=True,
            )
            quality = _report_quality(record, int(runtime_settings["snapshot_interval_minutes"]))
            clusters.append(
                cluster
                | {
                    "capacity_status": plan.capacity_status if plan else None,
                    "decision": _management_decision(plan, quality),
                    "planning_safe": plan.total_planning_safe if plan else None,
                    "trend": plan.trend if plan else None,
                    "report_quality": quality,
                }
            )
        return _render("Clusters", _MANAGEMENT_CLUSTERS_TEMPLATE, clusters=clusters)

    @app.route("/clusters/new", methods=["GET", "POST"])
    def new_cluster() -> Response | str:
        form = _empty_cluster_form()
        if request.method == "POST":
            _validate_csrf()
            form = _cluster_form_values()
            imported_file: str | None = None
            try:
                normalized, imported_file = _validate_cluster_form(form, kubeconfig_files)
                cluster = store.create_cluster(**normalized)
            except ValueError as exc:
                if imported_file:
                    kubeconfig_files.remove(imported_file)
                flash(str(exc), "error")
                return _render("Add cluster", _CLUSTER_FORM_TEMPLATE, cluster=None, form=form, logs=[])
            session["active_cluster_id"] = cluster["id"]
            flash("Cluster connection saved.", "success")
            return redirect(url_for("edit_cluster", cluster_id=cluster["id"]))
        return _render("Add cluster", _CLUSTER_FORM_TEMPLATE, cluster=None, form=form, logs=[])

    @app.route("/clusters/<int:cluster_id>", methods=["GET", "POST"])
    def edit_cluster(cluster_id: int) -> Response | str:
        cluster = store.get_cluster(cluster_id)
        if cluster is None:
            abort(404)
        previous_kubeconfig_file = str(cluster["kubeconfig_file"])
        form = _cluster_form(cluster)
        if request.method == "POST":
            _validate_csrf()
            form = _cluster_form_values()
            imported_file: str | None = None
            try:
                normalized, imported_file = _validate_cluster_form(
                    form, kubeconfig_files, existing_file=previous_kubeconfig_file
                )
                cluster = store.update_cluster(cluster_id, **normalized)
            except ValueError as exc:
                if imported_file:
                    kubeconfig_files.remove(imported_file)
                flash(str(exc), "error")
                return _render(
                    "Edit cluster",
                    _CLUSTER_FORM_TEMPLATE,
                    cluster=cluster,
                    form=form,
                    logs=store.list_cluster_logs(cluster_id),
                )
            if imported_file or normalized["kubeconfig_file"] != previous_kubeconfig_file:
                kubeconfig_files.remove(previous_kubeconfig_file)
            flash("Cluster connection saved.", "success")
            return redirect(url_for("edit_cluster", cluster_id=cluster_id))
        return _render(
            "Edit cluster",
            _CLUSTER_FORM_TEMPLATE,
            cluster=cluster,
            form=form,
            logs=store.list_cluster_logs(cluster_id),
        )

    @app.post("/clusters/<int:cluster_id>/test")
    def test_cluster_connection(cluster_id: int) -> Response:
        _validate_csrf()
        cluster = store.get_cluster(cluster_id)
        if cluster is None:
            abort(404)
        if cluster["legacy_connection"]:
            flash("Update this legacy cluster with a kubeconfig before testing the connection.", "warning")
            return redirect(url_for("edit_cluster", cluster_id=cluster_id))
        try:
            version = service.test_connection(cluster_id)
        except Exception:
            flash("Connection test failed. Check the service log and cluster connection settings.", "error")
        else:
            if version is None:
                flash("Another cluster operation is already running.", "warning")
            else:
                flash(f"Connection verified: {version}.", "success")
        return redirect(url_for("edit_cluster", cluster_id=cluster_id))

    @app.post("/clusters/<int:cluster_id>/snapshot")
    def take_cluster_snapshot(cluster_id: int) -> Response:
        _validate_csrf()
        cluster = store.get_cluster(cluster_id)
        if cluster is None:
            abort(404)
        if cluster["legacy_connection"]:
            flash("Update this legacy cluster with a kubeconfig before collecting a report.", "warning")
            return redirect(url_for("clusters"))
        try:
            snapshot_id = service.collect_now(cluster_id)
        except Exception:
            flash("Snapshot collection failed. Check the service log and cluster connection settings.", "error")
        else:
            if snapshot_id is None:
                flash("Another cluster operation is already running.", "warning")
            else:
                flash(f"Snapshot {snapshot_id} collected.", "success")
        return redirect(url_for("clusters"))

    @app.route("/clusters/<int:cluster_id>/remove", methods=["GET", "POST"])
    def remove_cluster(cluster_id: int) -> Response | str:
        cluster = store.get_cluster(cluster_id)
        if cluster is None:
            abort(404)
        if request.method == "POST":
            _validate_csrf()
            if not store.delete_cluster(cluster_id):
                abort(404)
            kubeconfig_files.remove(str(cluster["kubeconfig_file"]))
            if session.get("active_cluster_id") == cluster_id:
                session.pop("active_cluster_id", None)
            flash("Cluster connection and stored reports removed.", "success")
            return redirect(url_for("clusters"))
        return _render("Remove cluster", _CLUSTER_REMOVE_TEMPLATE, cluster=cluster)

    @app.post("/clusters/activate")
    def activate_cluster() -> Response:
        _validate_csrf()
        cluster_id = _snapshot_id(request.form.get("cluster_id", ""))
        if store.get_cluster(cluster_id) is None:
            abort(404)
        session["active_cluster_id"] = cluster_id
        return redirect(_safe_local_path(request.form.get("next", "")) or url_for("overview"))

    @app.post("/collect")
    def collect() -> Response:
        _validate_csrf()
        active_cluster = _active_cluster(store)
        if active_cluster is None:
            flash("Add a Kubernetes cluster before collecting a report.", "warning")
            return redirect(url_for("clusters"))
        if active_cluster["legacy_connection"]:
            flash("Update this legacy cluster with a kubeconfig before collecting a report.", "warning")
            return redirect(url_for("edit_cluster", cluster_id=active_cluster["id"]))
        try:
            snapshot_id = service.collect_now(active_cluster["id"])
        except Exception:
            flash("Collection failed. Check the service log and cluster connection settings.", "error")
            return redirect(url_for("overview"))
        if snapshot_id is None:
            flash("A collection is already running.", "warning")
        else:
            flash(f"Snapshot {snapshot_id} collected.", "success")
        return redirect(url_for("overview"))

    @app.get("/findings")
    def findings() -> str:
        active_cluster = _active_cluster(store)
        record = store.latest_snapshot(active_cluster["id"]) if active_cluster else None
        severity = request.args.get("severity", "")
        findings = (record or {"payload": {}})["payload"].get("findings", [])
        if severity in {"critical", "warning", "info"}:
            findings = [finding for finding in findings if finding["severity"] == severity]
        return _render("Findings", _FINDINGS_TEMPLATE, record=record, findings=findings, severity=severity)

    @app.get("/allocation")
    def allocation() -> str:
        active_cluster = _active_cluster(store)
        record = store.latest_snapshot(active_cluster["id"]) if active_cluster else None
        runtime_settings = service.runtime_settings()
        plan = _allocation_plan(
            store,
            docs,
            record,
            active_cluster["id"] if active_cluster else None,
            int(runtime_settings["planning_reserve_percent"]),
            include_history=True,
        )
        quality = _report_quality(record, int(runtime_settings["snapshot_interval_minutes"]))
        return _render(
            "Allocation",
            _ALLOCATION_TEMPLATE,
            record=record,
            plan=plan,
            quality=quality,
        )

    @app.get("/nodes")
    def nodes() -> str:
        active_cluster = _active_cluster(store)
        record = store.latest_snapshot(active_cluster["id"]) if active_cluster else None
        nodes = _snapshot_section(record, "nodes")
        return _render("Nodes", _NODES_TEMPLATE, record=record, nodes=nodes)

    @app.get("/namespaces")
    def namespaces() -> str:
        active_cluster = _active_cluster(store)
        record = store.latest_snapshot(active_cluster["id"]) if active_cluster else None
        namespaces = _snapshot_section(record, "namespaces")
        return _render("Namespaces", _NAMESPACES_TEMPLATE, record=record, namespaces=namespaces)

    @app.get("/workloads")
    def workloads() -> str:
        active_cluster = _active_cluster(store)
        record = store.latest_snapshot(active_cluster["id"]) if active_cluster else None
        workloads = _snapshot_section(record, "workloads")
        return _render("Workloads", _WORKLOADS_TEMPLATE, record=record, workloads=workloads)

    @app.get("/history")
    @app.get("/reports")
    def reports() -> str:
        active_cluster = _active_cluster(store)
        snapshots = store.list_snapshots(active_cluster["id"]) if active_cluster else []
        runtime_settings = service.runtime_settings()
        record = snapshots[0] if snapshots else None
        plan = _allocation_plan(
            store,
            docs,
            record,
            active_cluster["id"] if active_cluster else None,
            int(runtime_settings["planning_reserve_percent"]),
            include_history=True,
        )
        return _render("Reports", _REPORTS_TEMPLATE, snapshots=snapshots, plan=plan)

    @app.get("/docs")
    def documentation() -> str:
        query = request.args.get("q", "")
        return _render("Documentation", _DOCS_TEMPLATE, documents=docs.search(query), query=query)

    @app.get("/docs/<document_id>")
    def document_detail(document_id: str) -> str:
        try:
            document = docs.get(document_id)
        except KeyError:
            abort(404)
        return _render(
            "Documentation",
            _DOCUMENT_TEMPLATE,
            document=document,
            article=render_document(document.content, document.title),
            kubernetes_version=docs.kubernetes_version,
        )

    @app.get("/exports/<snapshot_ref>.<format_name>")
    def export(snapshot_ref: str, format_name: str) -> Response:
        active_cluster = _active_cluster(store)
        if active_cluster is None:
            abort(404)
        record = (
            store.latest_snapshot(active_cluster["id"])
            if snapshot_ref == "latest"
            else store.get_snapshot_record(_snapshot_id(snapshot_ref))
        )
        if record is None or record["cluster_id"] != active_cluster["id"]:
            abort(404)
        runtime_settings = service.runtime_settings()
        plan = _allocation_plan(
            store,
            docs,
            record,
            active_cluster["id"],
            int(runtime_settings["planning_reserve_percent"]),
        )
        quality = _report_quality(record, int(runtime_settings["snapshot_interval_minutes"]))
        if format_name == "json":
            return Response(json.dumps(_export_payload(record, plan, quality), separators=(",", ":")), mimetype="application/json")
        if format_name == "md":
            return Response(_markdown_export(record, plan, quality), mimetype="text/markdown")
        if format_name == "html":
            return Response(
                _html_export(record, plan, quality),
                mimetype="text/html",
                headers={
                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
                },
            )
        abort(404)

    def _render(title: str, body: str, authenticated: bool = True, **context: object) -> str:
        active_cluster = _active_cluster(store) if authenticated else None
        template_context = {
            "title": title,
            "authenticated": authenticated,
            "csrf_token": _csrf_token(),
            "clusters": store.list_clusters() if authenticated else [],
            "active_cluster": active_cluster,
            "current_path": request.path,
            "show_refresh": authenticated
            and active_cluster is not None
            and not active_cluster["legacy_connection"]
            and title
            not in {"Documentation", "Clusters", "Add cluster", "Edit cluster", "Remove cluster", "Account", "Settings", "Reports"},
            "format_cpu": _format_cpu,
            "format_cpu_raw": _format_cpu_raw,
            "format_bytes": _format_bytes,
            "format_bytes_raw": _format_bytes_raw,
            "format_percent": _format_percent,
            **context,
        }
        content = Markup(render_template_string(body, **template_context))
        return render_template_string(
            _BASE_TEMPLATE,
            title=title,
            authenticated=authenticated,
            csrf_token=template_context["csrf_token"],
            clusters=template_context["clusters"],
            active_cluster=template_context["active_cluster"],
            current_path=template_context["current_path"],
            show_refresh=template_context["show_refresh"],
            content=content,
        )

    if start_scheduler:
        service.start()
    return app


def _collector_factory(store: Store) -> Callable[[dict[str, Any]], KubernetesCollector]:
    def build(cluster: dict[str, Any]) -> KubernetesCollector:
        if cluster["legacy_connection"]:
            raise ValueError("Cluster uses a legacy token/CA connection and must be updated with a kubeconfig.")
        return KubernetesCollector.from_kubeconfig(
            str(cluster["kubeconfig_file"]),
            str(cluster["kube_context"]),
            str(cluster["api_ip"]) or None,
            bool(cluster["disable_proxy"]),
        )

    return build


def _active_cluster(store: Store) -> dict[str, Any] | None:
    cluster_id = session.get("active_cluster_id")
    if isinstance(cluster_id, int):
        cluster = store.get_cluster(cluster_id)
        if cluster is not None:
            return cluster
    cluster = store.first_cluster()
    if cluster is not None:
        session["active_cluster_id"] = cluster["id"]
    return cluster


def _allocation_plan(
    store: Store,
    docs: DocumentRegistry,
    record: dict[str, Any] | None,
    cluster_id: int | None,
    planning_reserve_percent: int,
    include_history: bool = False,
) -> Any | None:
    if record is None or cluster_id is None:
        return None
    history = store.list_snapshots(cluster_id, limit=2_160) if include_history else []
    return build_allocation_plan(
        record["payload"]["snapshot"],
        [snapshot["payload"]["snapshot"] for snapshot in history],
        docs,
        planning_reserve_percent=planning_reserve_percent,
    )


def _management_decision(plan: Any | None, quality: ReportQuality | None) -> Any | None:
    if plan is None:
        return None
    return build_management_decision(
        plan,
        quality.state if quality else "Unknown",
        quality.warnings if quality else ["No report quality is available."],
    )


def _cluster_form_values() -> dict[str, str | bool]:
    return {
        "name": request.form.get("name", "").strip(),
        "kubeconfig_source": request.form.get("kubeconfig_source", "path").strip(),
        "kubeconfig_file": request.form.get("kubeconfig_file", "").strip(),
        "kubeconfig_text": request.form.get("kubeconfig_text", ""),
        "kube_context": request.form.get("kube_context", "").strip(),
        "api_ip": request.form.get("api_ip", "").strip(),
        "disable_proxy": request.form.get("disable_proxy") == "1",
    }


def _validate_cluster_form(
    form: dict[str, str | bool], kubeconfig_files: KubeconfigFiles, existing_file: str | None = None
) -> tuple[dict[str, str | bool], str | None]:
    source = str(form["kubeconfig_source"])
    imported_file: str | None = None
    if source == "existing":
        if not existing_file:
            raise ValueError("Choose a kubeconfig source.")
        kubeconfig_file = _readable_file(existing_file, "Existing kubeconfig")
        details = inspect_kubeconfig(
            kubeconfig_file, str(form["kube_context"]) or None, str(form["api_ip"]) or None
        )
    elif source == "path":
        kubeconfig_file = _readable_file(str(form["kubeconfig_file"]), "Kubeconfig")
        details = inspect_kubeconfig(
            kubeconfig_file, str(form["kube_context"]) or None, str(form["api_ip"]) or None
        )
    elif source == "paste":
        details = inspect_kubeconfig_text(
            str(form["kubeconfig_text"]), str(form["kube_context"]) or None, str(form["api_ip"]) or None
        )
        imported_file = kubeconfig_files.save_text(str(form["kubeconfig_text"]))
        kubeconfig_file = imported_file
    elif source == "upload":
        contents = _uploaded_kubeconfig_text()
        details = inspect_kubeconfig_text(contents, form["kube_context"] or None, form["api_ip"] or None)
        imported_file = kubeconfig_files.save_text(contents)
        kubeconfig_file = imported_file
    else:
        raise ValueError("Choose a kubeconfig source.")
    normalized = {
        "name": str(form["name"]),
        "kubeconfig_file": kubeconfig_file,
        "kube_context": details.context,
        "endpoint": details.endpoint,
        "api_ip": str(form["api_ip"]),
        "disable_proxy": bool(form["disable_proxy"]),
    }
    return normalized, imported_file


def _uploaded_kubeconfig_text() -> str:
    uploaded = request.files.get("kubeconfig_upload")
    if uploaded is None or not uploaded.filename:
        raise ValueError("Choose a kubeconfig file to upload.")
    try:
        return uploaded.read().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("Kubeconfig upload must be a UTF-8 YAML file.") from exc


def _readable_file(value: str, label: str) -> str:
    path = Path(value)
    if not value or not path.is_file():
        raise ValueError(f"{label} file must point to a readable mounted file.")
    try:
        if not path.read_text(encoding="utf-8").strip():
            raise ValueError(f"{label} file cannot be empty.")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{label} file must point to a readable mounted file.") from exc
    return str(path)


def _empty_cluster_form() -> dict[str, str | bool]:
    return {
        "name": "",
        "kubeconfig_source": "upload",
        "kubeconfig_file": "",
        "kubeconfig_text": "",
        "kube_context": "",
        "api_ip": "",
        "disable_proxy": False,
    }


def _cluster_form(cluster: dict[str, Any]) -> dict[str, str | bool]:
    return {
        "name": str(cluster["name"]),
        "kubeconfig_source": "existing",
        "kubeconfig_file": str(cluster["kubeconfig_file"]),
        "kubeconfig_text": "",
        "kube_context": str(cluster["kube_context"]),
        "api_ip": str(cluster["api_ip"]),
        "disable_proxy": bool(cluster["disable_proxy"]),
    }


def _csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _validate_csrf() -> None:
    expected = session.get("csrf_token")
    supplied = request.form.get("csrf_token", "")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        abort(400)


def _snapshot_id(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        abort(404)


def _safe_local_path(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") else ""


def _snapshot_section(record: dict | None, section: str) -> list[dict]:
    if record is None:
        return []
    return record["payload"]["snapshot"].get(section, [])


def _summary(record: dict | None) -> dict[str, int]:
    if record is None:
        return {"nodes": 0, "namespaces": 0, "workloads": 0, "critical": 0, "warning": 0}
    snapshot = record["payload"]["snapshot"]
    findings = record["payload"].get("findings", [])
    return {
        "nodes": len(snapshot.get("nodes", [])),
        "namespaces": len(snapshot.get("namespaces", [])),
        "workloads": len(snapshot.get("workloads", [])),
        "critical": sum(finding["severity"] == "critical" for finding in findings),
        "warning": sum(finding["severity"] == "warning" for finding in findings),
    }


def _namespace_resources(record: dict | None) -> list[dict[str, Any]]:
    if record is None:
        return []
    snapshot = record["payload"]["snapshot"]
    metrics_available = snapshot.get("metrics_available") is True
    rows: dict[str, dict[str, Any]] = {}

    def row_for(name: str) -> dict[str, Any]:
        return rows.setdefault(
            name,
            {
                "name": name,
                "requests": {"cpu_millicores": 0, "memory_bytes": 0},
                "limits": {"cpu_millicores": 0, "memory_bytes": 0},
                "usage": {"cpu_millicores": 0, "memory_bytes": 0},
                "usage_complete": metrics_available,
            },
        )

    for namespace in snapshot.get("namespaces", []):
        row_for(str(namespace.get("name", "default")))
    for workload in snapshot.get("workloads", []):
        row = row_for(str(workload.get("namespace", "default")))
        for key in ("requests", "limits"):
            values = workload.get(key) or {}
            row[key]["cpu_millicores"] += int(values.get("cpu_millicores", 0))
            row[key]["memory_bytes"] += int(values.get("memory_bytes", 0))
        usage = workload.get("usage")
        if usage is None:
            row["usage_complete"] = False
            continue
        row["usage"]["cpu_millicores"] += int(usage.get("cpu_millicores", 0))
        row["usage"]["memory_bytes"] += int(usage.get("memory_bytes", 0))

    return [
        {
            "name": row["name"],
            "requests": row["requests"],
            "limits": row["limits"],
            "usage": row["usage"] if row["usage_complete"] else None,
        }
        for _, row in sorted(rows.items())
    ]


def _report_quality(record: dict[str, Any] | None, snapshot_interval_minutes: int) -> ReportQuality | None:
    if record is None:
        return None
    snapshot = record["payload"]["snapshot"]
    warnings = [str(warning) for warning in snapshot.get("warnings", [])]
    try:
        collected_at = datetime.fromisoformat(str(record["collected_at"]))
        if collected_at.tzinfo is None:
            raise ValueError
        stale = datetime.now(UTC) - collected_at.astimezone(UTC) > timedelta(minutes=snapshot_interval_minutes * 2)
    except ValueError:
        stale = True
    if stale:
        return ReportQuality(
            "Stale",
            "Report stale: the latest snapshot is older than two configured collection intervals.",
            warnings,
        )
    return ReportQuality("Current", "Report is within two configured collection intervals.", warnings)


def _format_cpu(value: int) -> str:
    return f"{value / 1000:g} cores" if value >= 1000 and value % 1000 == 0 else f"{value}m"


def _format_cpu_raw(value: int) -> str:
    return f"{value:,}m"


def _format_bytes(value: int) -> str:
    for suffix, scale in (("Gi", 1024**3), ("Mi", 1024**2), ("Ki", 1024)):
        if value >= scale:
            return f"{value / scale:.1f}{suffix}"
    return f"{value}B"


def _format_bytes_raw(value: int) -> str:
    return f"{value:,} B"


def _format_percent(value: float) -> str:
    return f"{value:.0%}"


def _capacity_charts(plan: Any) -> list[dict[str, Any]]:
    if plan is None or plan.total_node_capacity is None or plan.total_not_allocatable is None:
        return []

    resources = (
        (
            "CPU",
            plan.total_node_capacity.cpu_millicores,
            plan.total_not_allocatable.cpu_millicores,
            plan.total_requested.cpu_millicores,
            plan.total_remaining.cpu_millicores,
            _format_cpu,
            _format_cpu_raw,
        ),
        (
            "Memory",
            plan.total_node_capacity.memory_bytes,
            plan.total_not_allocatable.memory_bytes,
            plan.total_requested.memory_bytes,
            plan.total_remaining.memory_bytes,
            _format_bytes,
            _format_bytes_raw,
        ),
    )
    charts = []
    for resource, total, not_allocatable, requested, raw_remaining, format_value, format_raw in resources:
        if total <= 0:
            continue
        not_allocatable = min(total, max(0, not_allocatable))
        allocatable = total - not_allocatable
        requested = min(allocatable, max(0, requested))
        remaining = allocatable - requested
        raw_remaining = min(remaining, max(0, raw_remaining))
        values = (
            ("Not allocatable to Pods", "not-allocatable", not_allocatable),
            ("Scheduled requests", "requested", requested),
            ("Raw remaining", "remaining", raw_remaining),
        )
        position = 0.0
        segments = []
        for label, class_name, value in values:
            share = value / total * 100
            segments.append(
                {
                    "label": label,
                    "class_name": class_name,
                    "value": value,
                    "display": format_value(value),
                    "raw": format_raw(value),
                    "share_label": f"{share:.0f}%",
                    "x": round(position, 4),
                    "width": round(share, 4),
                }
            )
            position += share
        charts.append(
            {
                "resource": resource,
                "total_display": format_value(total),
                "total_raw": format_raw(total),
                "remaining_display": format_value(raw_remaining),
                "remaining_raw": format_raw(raw_remaining),
                "segments": segments,
            }
        )
    return charts


def _trend_charts(plan: Any) -> list[dict[str, Any]]:
    if plan is None or len(plan.trend.points) < 2:
        return []

    def sparkline(values: list[int]) -> str:
        minimum = min(values)
        maximum = max(values)
        span = maximum - minimum
        last_index = len(values) - 1
        return " ".join(
            f"{index / last_index * 100:.2f},{50 if span == 0 else 94 - (value - minimum) / span * 88:.2f}"
            for index, value in enumerate(values)
        )

    resources = (
        ("CPU", "Planning-safe CPU", "cpu_millicores", _format_cpu, _format_cpu_raw),
        ("Memory", "Planning-safe memory", "memory_bytes", _format_bytes, _format_bytes_raw),
    )
    charts = []
    for resource, label, attribute, format_value, format_raw in resources:
        values = [getattr(point.planning_safe, attribute) for point in plan.trend.points]
        charts.append(
            {
                "resource": resource,
                "label": label,
                "points": sparkline(values),
                "first_display": format_value(values[0]),
                "latest_display": format_value(values[-1]),
                "latest_raw": format_raw(values[-1]),
            }
        )
    return charts


def _export_payload(record: dict[str, Any], plan: Any, quality: ReportQuality | None) -> dict[str, Any]:
    payload = dict(record["payload"])
    if plan is None:
        return payload
    decision = _management_decision(plan, quality)
    payload["management_capacity"] = {
        "state": plan.capacity_status.state,
        "confidence": plan.capacity_status.confidence,
        "summary": plan.capacity_status.summary,
        "blockers": plan.capacity_status.blockers,
        "planning_reserve_percent": plan.planning_reserve_percent,
        "decision": {
            "state": decision.state if decision else "Decision Needs Review",
            "summary": decision.summary if decision else "No management decision is available.",
            "reasons": decision.reasons if decision else [],
            "scheduling_confidence": decision.scheduling_confidence if decision else "Unknown",
            "observed_usage": decision.observed_usage if decision else "Observed usage unavailable",
        },
        "technical_capacity_status": {
            "state": plan.capacity_status.state,
            "confidence": plan.capacity_status.confidence,
            "summary": plan.capacity_status.summary,
            "blockers": plan.capacity_status.blockers,
        },
        "capacity_flow": {
            "total_node_capacity": _resource_export(plan.total_node_capacity),
            "not_allocatable_to_pods": _resource_export(plan.total_not_allocatable),
            "node_allocatable": _resource_export(plan.total_allocatable),
            "scheduled_requests": _resource_export(plan.total_requested),
            "raw_remaining": _resource_export(plan.total_remaining),
            "planning_safe_capacity": _resource_export(plan.total_planning_safe),
            "planning_reserve_percent": plan.planning_reserve_percent,
            "planning_reserve_label": "Capacity Planner policy, not a Kubernetes-mandated threshold",
        },
        "raw_remaining": {
            "cpu_millicores": plan.total_remaining.cpu_millicores,
            "memory_bytes": plan.total_remaining.memory_bytes,
        },
        "planning_safe": {
            "cpu_millicores": plan.total_planning_safe.cpu_millicores,
            "memory_bytes": plan.total_planning_safe.memory_bytes,
        },
        "source": plan.capacity_source,
        "report_quality": {
            "state": quality.state if quality else "Unknown",
            "message": quality.message if quality else "No report quality is available.",
            "warnings": quality.warnings if quality else [],
        },
    }
    return payload


def _markdown_export(record: dict[str, Any], plan: Any, quality: ReportQuality | None) -> str:
    payload = record["payload"]
    snapshot = payload["snapshot"]
    lines = [
        "# Kubernetes Capacity Report",
        "",
        f"Cluster: {record['cluster_name'] or 'Unknown'}",
        f"Collected: {record['collected_at']}",
        f"Cluster version: {snapshot['cluster_version']}",
        "",
    ]
    if plan is not None:
        decision = _management_decision(plan, quality)
        lines.extend(
            [
                "## Management Capacity",
                "",
                f"Management decision: {decision.state if decision else 'Decision Needs Review'}",
                f"Decision evidence: {decision.summary if decision else 'No management decision is available.'}",
                f"Scheduling capacity: {decision.scheduling_confidence if decision else 'Unknown'}",
                f"Observed usage: {decision.observed_usage if decision else 'Observed usage unavailable'}",
                f"Technical capacity status: {plan.capacity_status.state} ({plan.capacity_status.confidence})",
                "",
                "### Capacity Flow",
                "",
                f"Total Node Capacity: {_resource_text(plan.total_node_capacity)}",
                f"Not allocatable to Pods: {_resource_text(plan.total_not_allocatable)}",
                f"Node Allocatable: {_resource_text(plan.total_allocatable)}",
                f"Scheduled Requests: {_resource_text(plan.total_requested)}",
                f"Raw Remaining: {_resource_text(plan.total_remaining)}",
                f"Planning-safe capacity: {_resource_text(plan.total_planning_safe)}",
                f"Planning reserve: {plan.planning_reserve_percent}% (Capacity Planner policy, not a Kubernetes-mandated threshold)",
                f"Local source: /docs/{plan.capacity_source['document_id']} ({plan.capacity_source['section']})",
                "",
            ]
        )
    if quality is not None:
        lines.extend(["## Report Quality", "", f"State: {quality.state}", quality.message])
        lines.extend(f"- {warning}" for warning in quality.warnings)
        lines.append("")
    lines.extend(
        [
        "## Findings",
        ]
    )
    for finding in payload.get("findings", []):
        lines.append(f"- [{finding['severity'].upper()}] {finding['resource']}: {finding['title']}")
        lines.append(f"  {finding['evidence']}")
        lines.append(f"  Local source: /docs/{finding['source']['document_id']} ({finding['source']['section']})")
    return "\n".join(lines) + "\n"


def _resource_export(resources: Any | None) -> dict[str, int] | None:
    if resources is None:
        return None
    return {
        "cpu_millicores": resources.cpu_millicores,
        "memory_bytes": resources.memory_bytes,
    }


def _resource_text(resources: Any | None) -> str:
    if resources is None:
        return "Not recorded"
    return (
        f"{_format_cpu(resources.cpu_millicores)} CPU ({_format_cpu_raw(resources.cpu_millicores)}) / "
        f"{_format_bytes(resources.memory_bytes)} memory ({_format_bytes_raw(resources.memory_bytes)})"
    )


def _html_export(record: dict[str, Any], plan: Any, quality: ReportQuality | None) -> str:
    return render_template_string(
        _HTML_EXPORT_TEMPLATE,
        record=record,
        plan=plan,
        quality=quality,
        capacity_charts=_capacity_charts(plan),
        namespace_resources=_namespace_resources(record),
        format_cpu=_format_cpu,
        format_cpu_raw=_format_cpu_raw,
        format_bytes=_format_bytes,
        format_bytes_raw=_format_bytes_raw,
    )


_HTML_EXPORT_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ record.cluster_name or 'Kubernetes cluster' }} snapshot | K8S Capacity Planner</title>
<style>
  :root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #172033; background: #eef2f6; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #eef2f6; }
  .snapshot-dashboard { width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 40px 0 56px; }
  .snapshot-header { display: flex; align-items: end; justify-content: space-between; gap: 24px; margin-bottom: 24px; padding-bottom: 24px; border-bottom: 1px solid #cad3df; }
  .eyebrow { margin: 0 0 8px; color: #526174; font-size: 12px; font-weight: 700; letter-spacing: 0; text-transform: uppercase; }
  h1, h2, h3, p { margin-top: 0; }
  h1 { margin-bottom: 8px; font-size: 32px; line-height: 1.15; }
  h2 { margin-bottom: 6px; font-size: 21px; line-height: 1.25; }
  h3 { margin: 0; font-size: 18px; }
  .snapshot-header > div > p:last-child, .card-header p:last-child { margin-bottom: 0; color: #526174; line-height: 1.5; }
  .snapshot-meta { display: grid; grid-template-columns: max-content minmax(180px, 1fr); gap: 8px 20px; min-width: 320px; margin: 0; font-size: 13px; }
  .snapshot-meta dt { color: #526174; }
  .snapshot-meta dd { margin: 0; font-weight: 700; overflow-wrap: anywhere; }
  .dashboard-card { margin-top: 20px; border: 1px solid #cad3df; border-radius: 8px; background: #ffffff; overflow: hidden; }
  .card-header { padding: 20px 22px 16px; border-bottom: 1px solid #dce3eb; }
  .capacity-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .resource-card { min-width: 0; padding: 20px 22px 22px; }
  .resource-card + .resource-card { border-left: 1px solid #dce3eb; }
  .resource-summary { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; margin: 18px 0 12px; }
  .resource-summary strong { font-size: 24px; }
  .resource-summary span { color: #526174; font-size: 13px; text-align: right; }
  .capacity-bar { display: flex; height: 14px; overflow: hidden; border-radius: 4px; background: #e8edf2; }
  .capacity-segment { display: block; min-width: 0; }
  .capacity-segment.not-allocatable { background: #7c8798; }
  .capacity-segment.requested { background: #d9952b; }
  .capacity-segment.remaining { background: #31836b; }
  .capacity-list { display: grid; gap: 10px; margin: 18px 0 0; }
  .capacity-list > div { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: baseline; }
  .capacity-list dt { color: #526174; }
  .capacity-list dd { margin: 0; text-align: right; }
  .capacity-list strong { display: block; }
  .capacity-list small { color: #526174; font-variant-numeric: tabular-nums; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; min-width: 680px; }
  th, td { padding: 14px 22px; border-bottom: 1px solid #dce3eb; text-align: left; vertical-align: top; }
  th { color: #526174; font-size: 12px; text-transform: uppercase; }
  tbody tr:last-child td { border-bottom: 0; }
  .resource-pair { display: block; line-height: 1.5; white-space: nowrap; }
  .snapshot-details { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .snapshot-detail { padding: 18px 22px; }
  .snapshot-detail + .snapshot-detail { border-left: 1px solid #dce3eb; }
  .snapshot-detail span { display: block; margin-bottom: 6px; color: #526174; font-size: 12px; font-weight: 700; text-transform: uppercase; }
  .snapshot-detail strong { display: block; font-size: 16px; }
  .snapshot-detail p { margin: 6px 0 0; color: #526174; font-size: 13px; line-height: 1.45; }
  .notice { margin: 16px 22px 22px; padding: 12px 14px; border-left: 3px solid #c47a18; background: #fff6e5; color: #593900; line-height: 1.45; }
  @media (max-width: 760px) {
    .snapshot-dashboard { width: min(100% - 24px, 1180px); padding-top: 24px; }
    .snapshot-header { align-items: start; flex-direction: column; }
    .snapshot-meta { min-width: 0; width: 100%; }
    .capacity-grid, .snapshot-details { grid-template-columns: 1fr; }
    .resource-card + .resource-card, .snapshot-detail + .snapshot-detail { border-top: 1px solid #dce3eb; border-left: 0; }
    .card-header, .resource-card, .snapshot-detail { padding-right: 18px; padding-left: 18px; }
    th, td { padding-right: 18px; padding-left: 18px; }
  }
</style>
</head>
<body>
<main class="snapshot-dashboard">
  <header class="snapshot-header">
    <div><p class="eyebrow">K8S Capacity Planner</p><h1>Snapshot dashboard</h1><p>Capacity and namespace resource data captured at this stored snapshot time.</p></div>
    <dl class="snapshot-meta"><dt>Cluster</dt><dd>{{ record.cluster_name or 'Unknown' }}</dd><dt>Snapshot time</dt><dd>{{ record.collected_at }}</dd><dt>Kubernetes</dt><dd>{{ record.cluster_version }}</dd></dl>
  </header>

  <section class="dashboard-card">
    <header class="card-header"><p class="eyebrow">Capacity flow</p><h2>From total resources to raw remaining capacity</h2><p>Total node capacity, Kubernetes allocatable capacity, declared Pod requests, and raw remaining capacity from this snapshot.</p></header>
    {% if capacity_charts %}<div class="capacity-grid">{% for chart in capacity_charts %}
      <article class="resource-card"><h3>{{ chart.resource }}</h3><div class="resource-summary"><strong>{{ chart.total_display }}</strong><span>{{ chart.total_raw }} total node capacity</span></div><div class="capacity-bar" role="img" aria-label="{{ chart.resource }} capacity composition">{% for segment in chart.segments %}{% if segment.width > 0 %}<span class="capacity-segment {{ segment.class_name }}" style="width: {{ segment.width }}%" title="{{ segment.label }}: {{ segment.display }}"></span>{% endif %}{% endfor %}</div><dl class="capacity-list">{% for segment in chart.segments %}<div><dt>{{ segment.label }}</dt><dd><strong>{{ segment.display }}</strong><small>{{ segment.raw }}</small></dd></div>{% endfor %}</dl></article>
    {% endfor %}</div>{% else %}<p class="notice">Node capacity was not recorded in this snapshot.</p>{% endif %}
  </section>

  <section class="dashboard-card">
    <header class="card-header"><p class="eyebrow">Resource allocation</p><h2>Namespace resources</h2><p>Declared requests, limits, and observed CPU and memory usage from this snapshot.</p></header>
    <div class="table-wrap"><table><thead><tr><th>Namespace</th><th>Requests</th><th>Limits</th><th>Actual used</th></tr></thead><tbody>{% for namespace in namespace_resources %}<tr><td>{{ namespace.name }}</td><td><span class="resource-pair">CPU {{ format_cpu(namespace.requests.cpu_millicores) }}</span><span class="resource-pair">Memory {{ format_bytes(namespace.requests.memory_bytes) }}</span></td><td><span class="resource-pair">CPU {{ format_cpu(namespace.limits.cpu_millicores) }}</span><span class="resource-pair">Memory {{ format_bytes(namespace.limits.memory_bytes) }}</span></td><td>{% if namespace.usage is not none %}<span class="resource-pair">CPU {{ format_cpu(namespace.usage.cpu_millicores) }}</span><span class="resource-pair">Memory {{ format_bytes(namespace.usage.memory_bytes) }}</span>{% else %}Unavailable{% endif %}</td></tr>{% else %}<tr><td colspan="4">No namespace data was recorded in this snapshot.</td></tr>{% endfor %}</tbody></table></div>
  </section>

  <section class="dashboard-card">
    <div class="snapshot-details"><article class="snapshot-detail"><span>Metrics API</span><strong>{{ 'Available' if record.payload.snapshot.metrics_available else 'Unavailable' }}</strong><p>Actual usage is shown only where Metrics API data was collected.</p></article><article class="snapshot-detail"><span>Data quality</span><strong>{{ quality.state if quality else 'Unknown' }}</strong><p>{{ quality.message if quality else 'No report quality is available.' }}</p></article><article class="snapshot-detail"><span>Planning reserve</span><strong>{{ plan.planning_reserve_percent if plan else 0 }}%</strong><p>Applied when calculating planning-safe capacity; it does not change the raw remaining values above.</p></article></div>
    {% if quality and quality.warnings %}<div class="notice">{% for warning in quality.warnings %}{{ warning }}{% if not loop.last %}<br>{% endif %}{% endfor %}</div>{% endif %}
  </section>
</main>
</body>
</html>"""


class _LoginThrottle:
    def __init__(self) -> None:
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def permit(self, address: str) -> bool:
        now = time.monotonic()
        attempts = self._attempts[address]
        while attempts and now - attempts[0] > 900:
            attempts.popleft()
        return len(attempts) < 5

    def record_failure(self, address: str) -> None:
        self._attempts[address].append(time.monotonic())

    def reset(self, address: str) -> None:
        self._attempts.pop(address, None)


_BASE_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} | K8S Capacity Planner</title><link rel="stylesheet" href="{{ url_for('static', filename='app.css') }}"></head><body>
{% if authenticated %}<div class="app-shell">
<aside class="topbar sidebar"><a class="brand" href="{{ url_for('overview') }}"><img class="brand-mark" src="{{ url_for('static', filename='k8s-capacity-planner-logo.svg') }}" width="34" height="34" alt=""><span>K8S Capacity Planner</span><small>Read-only cluster reporting</small></a>
<nav aria-label="Primary"><a class="{% if current_path == '/' %}active{% endif %}" href="{{ url_for('overview') }}">Dashboard</a><a class="{% if current_path.startswith('/clusters') %}active{% endif %}" href="{{ url_for('clusters') }}">Clusters</a><a class="{% if current_path == '/reports' or current_path == '/history' %}active{% endif %}" href="{{ url_for('reports') }}">Reports</a></nav>
<details class="nav-menu operation-menu" {% if current_path in ['/allocation', '/findings', '/nodes', '/namespaces', '/workloads', '/docs'] or current_path.startswith('/docs/') %}open{% endif %}><summary>Operations</summary><div><a class="{% if current_path == '/allocation' %}active{% endif %}" href="{{ url_for('allocation') }}">Capacity planning</a><a class="{% if current_path == '/findings' %}active{% endif %}" href="{{ url_for('findings') }}">Findings</a><a class="{% if current_path == '/nodes' %}active{% endif %}" href="{{ url_for('nodes') }}">Nodes</a><a class="{% if current_path == '/namespaces' %}active{% endif %}" href="{{ url_for('namespaces') }}">Namespaces</a><a class="{% if current_path == '/workloads' %}active{% endif %}" href="{{ url_for('workloads') }}">Workloads</a><a class="{% if current_path.startswith('/docs') %}active{% endif %}" href="{{ url_for('documentation') }}">Local docs</a></div></details>
<details class="nav-menu manage-menu" {% if current_path in ['/settings', '/account'] %}open{% endif %}><summary>Manage</summary><div><a class="{% if current_path == '/settings' %}active{% endif %}" href="{{ url_for('settings') }}">Settings</a><a class="{% if current_path == '/account' %}active{% endif %}" href="{{ url_for('account') }}">Account</a></div></details>
</aside><section class="workspace"><header class="workspace-header"><div class="workspace-context"><span>Active cluster</span><strong>{{ active_cluster.name if active_cluster else 'Not configured' }}</strong></div><div class="workspace-actions">{% if clusters %}<form class="cluster-switcher" method="post" action="{{ url_for('activate_cluster') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="next" value="{{ current_path }}"><label for="active-cluster">Active cluster</label><select id="active-cluster" name="cluster_id">{% for cluster in clusters %}<option value="{{ cluster.id }}" {% if active_cluster and cluster.id == active_cluster.id %}selected{% endif %}>{{ cluster.name }}</option>{% endfor %}</select><button class="quiet" type="submit">Use</button></form>{% endif %}<form method="post" action="{{ url_for('logout') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="quiet" type="submit">Sign out</button></form></div></header>
{% endif %}
<main class="page"><div class="page-heading"><div><p class="eyebrow">{{ active_cluster.name if active_cluster else 'Dark-site operation' }}</p><h1>{{ title }}</h1></div>{% if show_refresh %}<form method="post" action="{{ url_for('collect') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button type="submit">Refresh</button></form>{% endif %}</div>
{% with messages = get_flashed_messages(with_categories=true) %}{% for category, message in messages %}<p class="notice {{ category }}">{{ message }}</p>{% endfor %}{% endwith %}
{{ content }}
"""

_LOGIN_TEMPLATE = """<section class="login-panel"><p class="eyebrow">Dark-site operation</p><h1>K8S Capacity Planner</h1><p>Sign in to review the configured Kubernetes endpoint.</p><form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label>Administrator<input name="username" autocomplete="username" required></label><label>Password<input name="password" type="password" autocomplete="current-password" required></label><button type="submit">Sign in</button></form></section>"""

_ACCOUNT_TEMPLATE = """<div class="account-layout"><section class="account-summary"><p class="eyebrow">Local administrator</p><h2>Administrator account</h2><dl><dt>Username</dt><dd>{{ username }}</dd><dt>Authentication</dt><dd>Local password</dd></dl></section><section class="account-form"><p class="eyebrow">Password</p><h2>Change password</h2><form method="post" autocomplete="off"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label for="new-password">New password<input id="new-password" name="new_password" type="password" autocomplete="new-password" maxlength="1024" required></label><label for="confirm-password">Confirm new password<input id="confirm-password" name="confirm_password" type="password" autocomplete="new-password" maxlength="1024" required></label><button type="submit">Update password</button></form></section></div>"""

_SETTINGS_TEMPLATE = """<div class="settings-layout"><section class="settings-summary"><p class="eyebrow">Runtime policy</p><h2>Collection settings</h2><dl><dt>Automatic snapshots</dt><dd>{{ 'Enabled' if settings.schedule_enabled else 'Paused' }}</dd><dt>Snapshot interval</dt><dd>Every {{ settings.snapshot_interval_minutes }} minutes</dd><dt>Report retention</dt><dd>{{ settings.retention_days }} days</dd><dt>Planning reserve</dt><dd>{{ settings.planning_reserve_percent }}% of each eligible node</dd></dl></section><section class="settings-form"><p class="eyebrow">Scheduling</p><h2>Update settings</h2><form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label class="checkbox-label" for="schedule-enabled"><input id="schedule-enabled" name="schedule_enabled" type="checkbox" value="1" {% if settings.schedule_enabled %}checked{% endif %}>Automatic snapshots</label><label for="snapshot-interval">Snapshot interval (minutes)<input id="snapshot-interval" name="snapshot_interval_minutes" type="number" min="15" max="1440" step="1" value="{{ settings.snapshot_interval_minutes }}" required></label><label for="retention-days">Report retention (days)<input id="retention-days" name="retention_days" type="number" min="1" max="3650" step="1" value="{{ settings.retention_days }}" required></label><label for="planning-reserve">Planning reserve (%)<input id="planning-reserve" name="planning_reserve_percent" type="number" min="0" max="50" step="1" value="{{ settings.planning_reserve_percent }}" required></label><p class="form-hint">Capacity Planner policy, not a Kubernetes-mandated threshold.</p><button type="submit">Save settings</button></form></section></div>"""

_OVERVIEW_TEMPLATE = """{% if not record %}<section class="empty"><div class="empty-copy">{% if connection %}<p class="eyebrow">First report</p><h2>No snapshots collected</h2><p>Confirm the active cluster connection, then collect the first read-only snapshot.</p><a class="button-link" href="{{ url_for('clusters') }}">Review clusters</a>{% else %}<p class="eyebrow">First cluster</p><h2>Add a cluster</h2><p>Mount a read-only kubeconfig in the container, then add its path and context before collecting a report.</p><a class="button-link" href="{{ url_for('new_cluster') }}">Add cluster</a>{% endif %}</div><dl class="empty-details"><div><dt>Active cluster</dt><dd>{{ connection.name if connection else 'Not configured' }}</dd></div><div><dt>API endpoint</dt><dd>{{ connection.endpoint if connection else 'Unavailable' }}</dd></div><div><dt>Collection mode</dt><dd>Read-only</dd></div></dl></section>{% else %}
<div class="overview-dashboard">
  <section class="dashboard-decision">
    <div><p class="eyebrow">Capacity decision</p><div class="capacity-state"><span class="capacity-badge {{ plan.capacity_status.state|lower }}">{{ plan.capacity_status.state }}</span><div><h2>{{ plan.capacity_status.summary }}</h2><p>Based on Kubernetes Node Allocatable and scheduled Pod requests. This is a read-only planning decision, not a scheduler guarantee.</p></div></div></div>
    <div class="dashboard-decision-meta"><dl><dt>Eligible nodes</dt><dd>{{ plan.eligible_node_count }}</dd><dt>Data confidence</dt><dd>{{ plan.capacity_status.confidence }}</dd><dt>Metrics API</dt><dd>{{ 'Available' if record.payload.snapshot.metrics_available else 'Unavailable' }}</dd></dl><a class="quiet-link" href="{{ url_for('document_detail', document_id=plan.capacity_source.document_id) }}">{{ plan.capacity_source.document_title }}</a></div>
  </section>
  <section class="capacity-chart-panel" aria-labelledby="capacity-composition-title">
    <header class="capacity-chart-heading"><div><p class="eyebrow">Management view</p><h2 id="capacity-composition-title">Capacity composition</h2><p>Each bar starts with total node capacity and shows how much remains safe for additional deployments.</p></div><span class="capacity-chart-snapshot">Latest snapshot</span></header>
    {% if capacity_charts %}<div class="capacity-chart-grid">{% for chart in capacity_charts %}
      <figure class="capacity-chart" aria-labelledby="capacity-chart-{{ chart.resource|lower }}">
        <figcaption><div><span id="capacity-chart-{{ chart.resource|lower }}">{{ chart.resource }}</span><strong>{{ chart.total_display }}</strong><small>{{ chart.total_raw }} total node capacity</small></div><div class="capacity-chart-outcome"><span>Safe for deployment</span><strong>{{ chart.safe_display }}</strong><small>{{ chart.safe_raw }}</small></div></figcaption>
        <svg class="capacity-chart-bar" viewBox="0 0 100 14" preserveAspectRatio="none" role="img" aria-label="{{ chart.resource }} capacity composition"><title>{{ chart.resource }} capacity composition from total node capacity to planning-safe capacity</title>{% for segment in chart.segments %}{% if segment.width > 0 %}<rect class="capacity-chart-segment {{ segment.class_name }}" x="{{ segment.x }}" y="0" width="{{ segment.width }}" height="14"><title>{{ segment.label }}: {{ segment.display }} ({{ segment.raw }}, {{ segment.share_label }} of total)</title></rect>{% endif %}{% endfor %}</svg>
        <ul class="capacity-chart-labels">{% for segment in chart.segments %}<li><span class="capacity-chart-swatch {{ segment.class_name }}" aria-hidden="true"></span><div><span>{{ segment.label }}</span><strong>{{ segment.display }}</strong><small>{{ segment.raw }}; {{ segment.share_label }} of total</small></div></li>{% endfor %}</ul>
      </figure>
    {% endfor %}</div>{% else %}<p class="capacity-chart-empty">Take a new snapshot to collect Node Capacity and render the management graph.</p>{% endif %}
    <p class="capacity-chart-note"><strong>Held or unavailable</strong> includes the configured Capacity Planner reserve and remaining capacity on nodes that are not currently eligible for new Pods. This is a resource-only planning view, not a scheduler guarantee.</p>
  </section>
  <section class="dashboard-resource-grid" aria-label="Resource capacity">
    <article class="resource-dashboard-card"><header><p class="eyebrow">Cluster resource chain</p><h2>CPU</h2><p>Total node capacity is reported before resources become allocatable to Pods.</p></header><dl class="resource-figures">{% if plan.total_node_capacity %}<div><dt>Total node capacity</dt><dd><strong>{{ format_cpu(plan.total_node_capacity.cpu_millicores) }}</strong><small>{{ format_cpu_raw(plan.total_node_capacity.cpu_millicores) }}</small></dd></div><div><dt>Not allocatable to Pods</dt><dd><strong>{{ format_cpu(plan.total_not_allocatable.cpu_millicores) }}</strong><small>{{ format_cpu_raw(plan.total_not_allocatable.cpu_millicores) }}</small></dd></div>{% else %}<div><dt>Total node capacity</dt><dd><strong>Not recorded</strong><small>Take a new snapshot to collect Node Capacity.</small></dd></div><div><dt>Not allocatable to Pods</dt><dd><strong>Not recorded</strong><small>Available after the next snapshot.</small></dd></div>{% endif %}<div><dt>Node Allocatable</dt><dd><strong>{{ format_cpu(plan.total_allocatable.cpu_millicores) }}</strong><small>{{ format_cpu_raw(plan.total_allocatable.cpu_millicores) }}</small></dd></div><div><dt>Scheduled requests</dt><dd><strong>{{ format_cpu(plan.total_requested.cpu_millicores) }}</strong><small>{{ format_cpu_raw(plan.total_requested.cpu_millicores) }}</small></dd></div><div><dt>Raw remaining CPU</dt><dd><strong>{{ format_cpu(plan.total_remaining.cpu_millicores) }}</strong><small>{{ format_cpu_raw(plan.total_remaining.cpu_millicores) }}; allocatable minus requests</small></dd></div><div><dt>Planning-safe CPU</dt><dd><strong>{{ format_cpu(plan.total_planning_safe.cpu_millicores) }}</strong><small>{{ format_cpu_raw(plan.total_planning_safe.cpu_millicores) }}; after {{ plan.planning_reserve_percent }}% reserve</small></dd></div></dl></article>
    <article class="resource-dashboard-card"><header><p class="eyebrow">Cluster resource chain</p><h2>Memory</h2><p>Total node capacity is reported before resources become allocatable to Pods.</p></header><dl class="resource-figures">{% if plan.total_node_capacity %}<div><dt>Total node capacity</dt><dd><strong>{{ format_bytes(plan.total_node_capacity.memory_bytes) }}</strong><small>{{ format_bytes_raw(plan.total_node_capacity.memory_bytes) }}</small></dd></div><div><dt>Not allocatable to Pods</dt><dd><strong>{{ format_bytes(plan.total_not_allocatable.memory_bytes) }}</strong><small>{{ format_bytes_raw(plan.total_not_allocatable.memory_bytes) }}</small></dd></div>{% else %}<div><dt>Total node capacity</dt><dd><strong>Not recorded</strong><small>Take a new snapshot to collect Node Capacity.</small></dd></div><div><dt>Not allocatable to Pods</dt><dd><strong>Not recorded</strong><small>Available after the next snapshot.</small></dd></div>{% endif %}<div><dt>Node Allocatable</dt><dd><strong>{{ format_bytes(plan.total_allocatable.memory_bytes) }}</strong><small>{{ format_bytes_raw(plan.total_allocatable.memory_bytes) }}</small></dd></div><div><dt>Scheduled requests</dt><dd><strong>{{ format_bytes(plan.total_requested.memory_bytes) }}</strong><small>{{ format_bytes_raw(plan.total_requested.memory_bytes) }}</small></dd></div><div><dt>Raw remaining memory</dt><dd><strong>{{ format_bytes(plan.total_remaining.memory_bytes) }}</strong><small>{{ format_bytes_raw(plan.total_remaining.memory_bytes) }}; allocatable minus requests</small></dd></div><div><dt>Planning-safe memory</dt><dd><strong>{{ format_bytes(plan.total_planning_safe.memory_bytes) }}</strong><small>{{ format_bytes_raw(plan.total_planning_safe.memory_bytes) }}; after {{ plan.planning_reserve_percent }}% reserve</small></dd></div></dl></article>
  </section>
  <section class="dashboard-context-grid"><article><p class="eyebrow">Current usage</p><h2>Observed resource usage</h2>{% if record.payload.snapshot.metrics_available %}<dl><dt>Observed CPU</dt><dd>{{ format_cpu(plan.total_observed_usage.cpu_millicores) }} <span>{{ format_cpu_raw(plan.total_observed_usage.cpu_millicores) }}</span></dd><dt>Observed memory</dt><dd>{{ format_bytes(plan.total_observed_usage.memory_bytes) }} <span>{{ format_bytes_raw(plan.total_observed_usage.memory_bytes) }}</span></dd></dl>{% else %}<p>Observed usage is unavailable because Metrics API data was not collected. Capacity remains request-based.</p>{% endif %}</article><article><p class="eyebrow">Planning policy</p><h2>Capacity Planner reserve</h2><p>{{ plan.planning_reserve_percent }}% of each eligible node's allocatable CPU and memory is held back before the Capacity Planner reports planning-safe capacity. This is a Capacity Planner policy, not a Kubernetes-mandated threshold.</p></article><article><p class="eyebrow">Latest snapshot</p><h2>Report quality</h2><dl><dt>Collected</dt><dd>{{ record.collected_at }}</dd><dt>Kubernetes</dt><dd>{{ record.cluster_version }}</dd><dt>Workloads</dt><dd>{{ summary.workloads }}</dd></dl>{% if connection and service.last_error_for(connection.id) %}<p class="notice error">{{ service.last_error_for(connection.id) }}</p>{% endif %}</article></section>
</div>
<section><div class="section-heading"><div><h2>What needs attention</h2><p>Priority blockers and policy issues that affect additional workload demand.</p></div><a href="{{ url_for('findings') }}">All findings</a></div>{% if plan.capacity_status.blockers %}<ul class="capacity-blockers">{% for blocker in plan.capacity_status.blockers %}<li>{{ blocker }}</li>{% endfor %}</ul>{% endif %}<table><thead><tr><th>Severity</th><th>Resource</th><th>Finding</th><th>Recommended action</th><th>Local source</th></tr></thead><tbody>{% for finding in priority_findings %}<tr><td><span class="badge {{ finding.severity }}">{{ finding.severity }}</span></td><td>{{ finding.resource }}</td><td>{{ finding.title }}</td><td>{{ finding.recommendation }}</td><td><a href="{{ url_for('document_detail', document_id=finding.source.document_id) }}">{{ finding.source.document_title }}</a></td></tr>{% else %}<tr><td colspan="5">No critical or warning findings. Continue to review workload requests before adding demand.</td></tr>{% endfor %}</tbody></table></section>{% endif %}"""

_REPORT_QUALITY_TEMPLATE = """
{% if record and quality %}
{% if quality.state == 'Stale' %}<p class="notice warning"><strong>{{ quality.message }}</strong> Take a new read-only snapshot before making a deployment decision.</p>{% endif %}
{% if quality.warnings %}<section class="collection-limitations"><div><p class="eyebrow">Data quality</p><h2>Collection limitations</h2><p>{{ quality.message }}</p></div><ul>{% for warning in quality.warnings %}<li>{{ warning }}</li>{% endfor %}</ul></section>{% endif %}
{% endif %}
"""

_DASHBOARD_TEMPLATE = """
{% if not record %}
  <section class="empty"><div class="empty-copy">{% if connection %}<p class="eyebrow">First report</p><h2>No snapshots collected</h2><p>Confirm the active cluster connection, then collect the first read-only snapshot.</p><a class="button-link" href="{{ url_for('clusters') }}">Review clusters</a>{% else %}<p class="eyebrow">First cluster</p><h2>Add a cluster</h2><p>Add a read-only kubeconfig to begin capacity planning.</p><a class="button-link" href="{{ url_for('new_cluster') }}">Add cluster</a>{% endif %}</div><dl class="empty-details"><div><dt>Active cluster</dt><dd>{{ connection.name if connection else 'Not configured' }}</dd></div><div><dt>API endpoint</dt><dd>{{ connection.endpoint if connection else 'Unavailable' }}</dd></div><div><dt>Collection mode</dt><dd>Read-only</dd></div></dl></section>
{% else %}
  <div class="overview-dashboard">
    <section class="capacity-chart-panel" aria-labelledby="capacity-composition-title">
      <header class="capacity-chart-heading"><div><p class="eyebrow">Capacity flow</p><h2 id="capacity-composition-title">From total resources to raw remaining capacity</h2><p>Total node capacity, Kubernetes allocatable capacity, declared Pod requests, and raw remaining capacity from the latest snapshot.</p></div><span class="capacity-chart-snapshot">Latest snapshot</span></header>
      {% if capacity_charts %}<div class="capacity-chart-grid">{% for chart in capacity_charts %}<figure class="capacity-chart" aria-labelledby="capacity-chart-{{ chart.resource|lower }}"><figcaption><div><span id="capacity-chart-{{ chart.resource|lower }}">{{ chart.resource }}</span><strong>{{ chart.total_display }}</strong><small>{{ chart.total_raw }} total node capacity</small></div><div class="capacity-chart-outcome"><span>Raw remaining</span><strong>{{ chart.remaining_display }}</strong><small>{{ chart.remaining_raw }}</small></div></figcaption><svg class="capacity-chart-bar" viewBox="0 0 100 14" preserveAspectRatio="none" role="img" aria-label="{{ chart.resource }} capacity composition"><title>{{ chart.resource }} capacity composition</title>{% for segment in chart.segments %}{% if segment.width > 0 %}<rect class="capacity-chart-segment {{ segment.class_name }}" x="{{ segment.x }}" y="0" width="{{ segment.width }}" height="14"><title>{{ segment.label }}: {{ segment.display }} ({{ segment.raw }})</title></rect>{% endif %}{% endfor %}</svg><ul class="capacity-chart-labels">{% for segment in chart.segments %}<li><span class="capacity-chart-swatch {{ segment.class_name }}" aria-hidden="true"></span><div><span>{{ segment.label }}</span><strong>{{ segment.display }}</strong><small>{{ segment.raw }}; {{ segment.share_label }} of total</small></div></li>{% endfor %}</ul></figure>{% endfor %}</div>{% else %}<p class="capacity-chart-empty">Take a new snapshot to collect Node Capacity and render the capacity flow.</p>{% endif %}
      <p class="capacity-chart-note"><strong>Node Allocatable</strong> is the capacity Kubernetes makes available to Pods after node reservations. <strong>Raw remaining</strong> is Node Allocatable minus scheduled Pod requests. <a href="{{ url_for('document_detail', document_id=plan.capacity_source.document_id) }}">Read the local Kubernetes guidance.</a></p>
    </section>

    <section class="trend-panel" aria-labelledby="capacity-trend-title">
      <header><div><p class="eyebrow">Capacity trend</p><h2 id="capacity-trend-title">Planning-safe capacity</h2><p>Planning-safe CPU and memory across the retained 30-day snapshot window.</p></div><dl><dt>Samples</dt><dd>{{ plan.trend.sample_count }} snapshot{{ '' if plan.trend.sample_count == 1 else 's' }}</dd><dt>State</dt><dd>{{ plan.trend.state }}</dd></dl></header>
      {% if trend_charts %}<div class="trend-chart-grid">{% for chart in trend_charts %}<figure class="trend-chart" aria-labelledby="trend-chart-{{ chart.resource|lower }}"><figcaption><span id="trend-chart-{{ chart.resource|lower }}">{{ chart.label }}</span><strong>{{ chart.latest_display }}</strong><small>{{ chart.latest_raw }}</small></figcaption><svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="{{ chart.label }} trend"><line class="trend-baseline" x1="0" y1="94" x2="100" y2="94"></line><polyline class="trend-line" points="{{ chart.points }}"></polyline></svg><div><span>{{ chart.first_display }}</span><span>{{ chart.latest_display }}</span></div></figure>{% endfor %}</div>{% else %}<p class="trend-empty">{{ plan.trend.summary }}</p>{% endif %}
    </section>

    <section class="capacity-chart-panel namespace-resource-panel" aria-labelledby="namespace-resources-title">
      <header class="capacity-chart-heading"><div><p class="eyebrow">Resource allocation</p><h2 id="namespace-resources-title">Namespace resources</h2><p>Declared requests, limits, and observed CPU and memory usage from the latest snapshot.</p></div></header>
      <table><thead><tr><th>Namespace</th><th>Requests</th><th>Limits</th><th>Actual used</th></tr></thead><tbody>{% for namespace in namespace_resources %}<tr><td>{{ namespace.name }}</td><td><span class="resource-pair">CPU {{ format_cpu(namespace.requests.cpu_millicores) }}</span><span class="resource-pair">Memory {{ format_bytes(namespace.requests.memory_bytes) }}</span></td><td><span class="resource-pair">CPU {{ format_cpu(namespace.limits.cpu_millicores) }}</span><span class="resource-pair">Memory {{ format_bytes(namespace.limits.memory_bytes) }}</span></td><td>{% if namespace.usage is not none %}<span class="resource-pair">CPU {{ format_cpu(namespace.usage.cpu_millicores) }}</span><span class="resource-pair">Memory {{ format_bytes(namespace.usage.memory_bytes) }}</span>{% else %}Unavailable{% endif %}</td></tr>{% else %}<tr><td colspan="4">No namespace data available.</td></tr>{% endfor %}</tbody></table>
    </section>
  </div>
{% endif %}
"""

_DASHBOARD_PAGE_TEMPLATE = _DASHBOARD_TEMPLATE

_OVERVIEW_PAGE_TEMPLATE = _REPORT_QUALITY_TEMPLATE + _OVERVIEW_TEMPLATE

_FINDINGS_TEMPLATE = """{% if not record %}<section class="empty"><h2>No snapshots collected</h2></section>{% else %}<div class="filters"><a href="{{ url_for('findings') }}">All</a><a href="{{ url_for('findings', severity='critical') }}">Critical</a><a href="{{ url_for('findings', severity='warning') }}">Warnings</a><a href="{{ url_for('findings', severity='info') }}">Info</a></div><table><thead><tr><th>Severity</th><th>Resource</th><th>Evidence</th><th>Recommended action</th><th>Guidance</th></tr></thead><tbody>{% for finding in findings %}<tr><td><span class="badge {{ finding.severity }}">{{ finding.severity }}</span></td><td>{{ finding.resource }}</td><td>{{ finding.evidence }}</td><td>{{ finding.recommendation }}</td><td><a href="{{ url_for('document_detail', document_id=finding.source.document_id) }}">{{ finding.source.section }}</a></td></tr>{% else %}<tr><td colspan="5">No findings match this filter.</td></tr>{% endfor %}</tbody></table>{% endif %}"""

_ALLOCATION_TEMPLATE = """{% if not record %}<section class="empty"><div class="empty-copy"><p class="eyebrow">Allocation guidance</p><h2>No snapshots collected</h2><p>Collect a read-only snapshot before reviewing request-based capacity and workload recommendations.</p></div></section>{% else %}<section class="allocation-intro"><div><p class="eyebrow">Allocation guidance</p><h2>Request-based scheduling capacity</h2><p>Kubernetes schedules Pods against declared requests and Node Allocatable. Available capacity is an aggregate; confirm that a single node can fit each planned Pod.</p></div><a class="quiet-link" href="{{ url_for('document_detail', document_id=plan.capacity_source.document_id) }}">{{ plan.capacity_source.document_title }}</a></section><section class="metrics allocation-metrics"><div><span>Allocatable CPU</span><strong>{{ format_cpu(plan.total_allocatable.cpu_millicores) }}</strong></div><div><span>Requested CPU</span><strong>{{ format_cpu(plan.total_requested.cpu_millicores) }}</strong></div><div><span>Available CPU</span><strong>{{ format_cpu(plan.total_remaining.cpu_millicores) }}</strong></div><div><span>Allocatable memory</span><strong>{{ format_bytes(plan.total_allocatable.memory_bytes) }}</strong></div><div><span>Requested memory</span><strong>{{ format_bytes(plan.total_requested.memory_bytes) }}</strong></div><div><span>Available memory</span><strong>{{ format_bytes(plan.total_remaining.memory_bytes) }}</strong></div></section><section><div class="section-heading"><div><h2>Node fit</h2><p>Remaining capacity after declared Pod requests; resource pressure makes a node unsuitable for additional demand.</p></div></div><table><thead><tr><th>Node</th><th>Allocatable CPU</th><th>Requested CPU</th><th>Available CPU</th><th>Allocatable memory</th><th>Requested memory</th><th>Available memory</th><th>State</th></tr></thead><tbody>{% for node in plan.nodes %}<tr><td>{{ node.name }}</td><td>{{ format_cpu(node.allocatable.cpu_millicores) }}</td><td>{{ format_cpu(node.requested.cpu_millicores) }}</td><td>{{ format_cpu(node.remaining.cpu_millicores) }}</td><td>{{ format_bytes(node.allocatable.memory_bytes) }}</td><td>{{ format_bytes(node.requested.memory_bytes) }}</td><td>{{ format_bytes(node.remaining.memory_bytes) }}</td><td>{% if node.has_pressure %}<span class="badge warning">Pressure</span>{% else %}<span class="badge info">Available</span>{% endif %}</td></tr>{% else %}<tr><td colspan="8">No node data available.</td></tr>{% endfor %}</tbody></table></section><section class="allocation-recommendations"><div class="section-heading"><div><h2>Workload request recommendations</h2><p>Observed floors use the highest retained workload usage from {{ plan.metric_snapshot_count }} Metrics API snapshot{{ '' if plan.metric_snapshot_count == 1 else 's' }}. They are evidence for review, not automatic changes.</p></div></div><table><thead><tr><th>Workload</th><th>Current total request</th><th>Observed peak</th><th>Suggested floor</th><th>Recommendation</th><th>Guidance</th></tr></thead><tbody>{% for recommendation in plan.recommendations %}<tr><td>{{ recommendation.identity }}</td><td>{{ format_cpu(recommendation.current_request.cpu_millicores) }} / {{ format_bytes(recommendation.current_request.memory_bytes) }}</td><td>{% if recommendation.observed_peak %}{{ format_cpu(recommendation.observed_peak.cpu_millicores) }} / {{ format_bytes(recommendation.observed_peak.memory_bytes) }}<br><span class="cell-note">{{ recommendation.sample_count }} snapshot{{ '' if recommendation.sample_count == 1 else 's' }}</span>{% else %}No usable observation{% endif %}</td><td>{% if recommendation.suggested_request %}{{ format_cpu(recommendation.suggested_request.cpu_millicores) }} / {{ format_bytes(recommendation.suggested_request.memory_bytes) }}{% else %}No increase suggested{% endif %}</td><td><span class="badge {{ recommendation.severity }}">{{ recommendation.status|replace('-', ' ') }}</span><p class="recommendation-copy">{{ recommendation.recommendation }}</p></td><td><a href="{{ url_for('document_detail', document_id=recommendation.source.document_id) }}">{{ recommendation.source.document_title }}: {{ recommendation.source.section }}</a></td></tr>{% else %}<tr><td colspan="6">No workloads available for allocation guidance.</td></tr>{% endfor %}</tbody></table></section>{% endif %}"""

_DEPLOYMENT_FIT_TEMPLATE = """
{% if record %}
{% if quality and quality.state == 'Stale' %}<p class="notice warning"><strong>{{ quality.message }}</strong> Take a new snapshot before relying on this fit result.</p>{% endif %}
{% if quality and quality.warnings %}<p class="notice warning">Collection limitations: {{ quality.warnings|join('; ') }}</p>{% endif %}
<section class="deployment-fit">
  <div class="section-heading">
    <div>
      <p class="eyebrow">Capacity planning</p>
      <h2>New deployment fit</h2>
      <p>Evaluate replicas against planning-safe CPU and memory from the latest snapshot.</p>
    </div>
    <span class="cell-note">No Kubernetes changes are made.</span>
  </div>
  <form class="fit-form" method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label for="fit-replicas">Replicas<input id="fit-replicas" name="replicas" type="number" min="1" step="1" value="{{ fit_form.replicas }}" required></label>
    <label for="fit-cpu">CPU request per Pod<input id="fit-cpu" name="cpu_request" value="{{ fit_form.cpu_request }}" placeholder="250m" required></label>
    <label for="fit-memory">Memory request per Pod<input id="fit-memory" name="memory_request" value="{{ fit_form.memory_request }}" placeholder="512Mi" required></label>
    <label for="fit-namespace">Target namespace<select id="fit-namespace" name="namespace"><option value="">No namespace policy check</option>{% for namespace in namespaces %}<option value="{{ namespace.name }}" {% if fit_form.namespace == namespace.name %}selected{% endif %}>{{ namespace.name }}</option>{% endfor %}</select></label>
    <button type="submit">Evaluate fit</button>
  </form>
  {% if fit %}
  <div class="fit-result {{ fit.status|lower }}">
    <div><span class="capacity-badge {{ fit.status|lower }}">{{ fit.status }}</span><h3>{{ fit.summary }}</h3></div>
    <dl><dt>Maximum safe replicas</dt><dd>{{ fit.maximum_safe_replicas }}</dd><dt>Requested replicas</dt><dd>{{ fit_form.replicas }}</dd></dl>
    {% if fit.issues %}<ul>{% for issue in fit.issues %}<li>{{ issue.message }} <a href="{{ url_for('document_detail', document_id=issue.source.document_id) }}">{{ issue.source.document_title }}: {{ issue.source.section }}</a></li>{% endfor %}</ul>{% endif %}
  </div>
  {% endif %}
  <p class="fit-limitations"><strong>Resource-only estimate.</strong> Node selectors, affinity, taints, topology, storage, extended resources, init-container behavior, and future HPA scale-out are not evaluated.</p>
</section>
{% endif %}
"""

_ALLOCATION_PAGE_TEMPLATE = _DEPLOYMENT_FIT_TEMPLATE + _ALLOCATION_TEMPLATE

_NODES_TEMPLATE = """<table><thead><tr><th>Node</th><th>Allocatable CPU</th><th>Requested CPU</th><th>Allocatable memory</th><th>Requested memory</th><th>Pressure</th></tr></thead><tbody>{% for node in nodes %}<tr><td>{{ node.name }}</td><td>{{ format_cpu(node.allocatable.cpu_millicores) }}</td><td>{{ format_cpu(node.requested.cpu_millicores) }}</td><td>{{ format_bytes(node.allocatable.memory_bytes) }}</td><td>{{ format_bytes(node.requested.memory_bytes) }}</td><td>{{ node.conditions|join(', ') or 'None' }}</td></tr>{% else %}<tr><td colspan="6">No node data available.</td></tr>{% endfor %}</tbody></table>"""

_NAMESPACES_TEMPLATE = """<table><thead><tr><th>Namespace</th><th>LimitRange</th><th>Quota usage</th></tr></thead><tbody>{% for namespace in namespaces %}<tr><td>{{ namespace.name }}</td><td>{{ 'Present' if namespace.has_limit_range else 'Missing' }}</td><td>{% for name, quota in namespace.quotas.items() %}{{ name }}: {{ quota.used }}/{{ quota.hard }}{% if not loop.last %}; {% endif %}{% else %}No quota{% endfor %}</td></tr>{% else %}<tr><td colspan="3">No namespace data available.</td></tr>{% endfor %}</tbody></table>"""

_WORKLOADS_TEMPLATE = """<table><thead><tr><th>Workload</th><th>Replicas</th><th>QoS</th><th>Requests</th><th>Limits</th><th>HPA</th></tr></thead><tbody>{% for workload in workloads %}<tr><td>{{ workload.namespace }}/{{ workload.kind }}/{{ workload.name }}</td><td>{{ workload.replicas }}</td><td>{{ workload.qos }}</td><td>{{ format_cpu(workload.requests.cpu_millicores) }} / {{ format_bytes(workload.requests.memory_bytes) }}</td><td>{{ format_cpu(workload.limits.cpu_millicores) }} / {{ format_bytes(workload.limits.memory_bytes) }}</td><td>{{ 'Present' if workload.has_hpa else 'None' }}</td></tr>{% else %}<tr><td colspan="6">No workload data available.</td></tr>{% endfor %}</tbody></table>"""

_HISTORY_TEMPLATE = """<table><thead><tr><th>ID</th><th>Collected</th><th>Kubernetes</th><th>Exports</th></tr></thead><tbody>{% for snapshot in snapshots %}<tr><td>{{ snapshot.id }}</td><td>{{ snapshot.collected_at }}</td><td>{{ snapshot.cluster_version }}</td><td><a href="{{ url_for('export', snapshot_ref=snapshot.id, format_name='json') }}">JSON</a> <a href="{{ url_for('export', snapshot_ref=snapshot.id, format_name='md') }}">Markdown</a> <a href="{{ url_for('export', snapshot_ref=snapshot.id, format_name='html') }}">HTML</a></td></tr>{% else %}<tr><td colspan="4">No stored snapshots.</td></tr>{% endfor %}</tbody></table>"""

_REPORTS_TEMPLATE = """<section class="reports-intro"><div><p class="eyebrow">Historical evidence</p><h2>Stored capacity reports</h2><p>Reports remain isolated to the active cluster and can be exported for management review.</p></div>{% if plan %}<dl><dt>30-day trend evidence</dt><dd>{{ plan.trend.confidence }}: {{ plan.trend.sample_count }} snapshots across {{ plan.trend.span_days }} days.</dd><dt>Current trend</dt><dd>{{ plan.trend.state }}</dd></dl>{% endif %}</section>""" + _HISTORY_TEMPLATE

_CLUSTERS_TEMPLATE = """<section><div class="section-heading"><div><h2>Configured clusters</h2><p>Management capacity is calculated from each cluster's own latest snapshot. Reports and exports remain isolated.</p></div><a class="button-link" href="{{ url_for('new_cluster') }}">Add cluster</a></div><table class="cluster-table"><thead><tr><th>Cluster</th><th>Context</th><th>API endpoint</th><th>Last snapshot</th><th>Management capacity</th><th>Active</th><th>Actions</th></tr></thead><tbody>{% for cluster in clusters %}<tr><td><a href="{{ url_for('edit_cluster', cluster_id=cluster.id) }}">{{ cluster.name }}</a></td><td>{{ cluster.kube_context if not cluster.legacy_connection else 'Update required' }}</td><td>{{ cluster.endpoint or 'Unavailable' }}</td><td>{{ cluster.last_collected_at or 'Not collected' }}</td><td>{% if cluster.capacity_status %}<span class="capacity-badge compact {{ cluster.capacity_status.state|lower }}">{{ cluster.capacity_status.state }}</span><span class="cell-note">{{ cluster.capacity_status.confidence }}</span>{% else %}<span class="badge warning">No report</span>{% endif %}</td><td>{% if active_cluster and cluster.id == active_cluster.id %}<span class="badge info">Active</span>{% else %}<form class="inline-form" method="post" action="{{ url_for('activate_cluster') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="cluster_id" value="{{ cluster.id }}"><input type="hidden" name="next" value="{{ url_for('overview') }}"><button class="quiet" type="submit">Use</button></form>{% endif %}</td><td><div class="table-actions">{% if not cluster.legacy_connection %}<form method="post" action="{{ url_for('take_cluster_snapshot', cluster_id=cluster.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="quiet table-action-button" type="submit">Take snapshot</button></form>{% endif %}<a class="quiet-link" href="{{ url_for('edit_cluster', cluster_id=cluster.id) }}">Edit</a><a class="quiet-link danger-link" href="{{ url_for('remove_cluster', cluster_id=cluster.id) }}">Remove</a></div></td></tr>{% else %}<tr><td colspan="7">No clusters configured.</td></tr>{% endfor %}</tbody></table></section>"""

_MANAGEMENT_CLUSTERS_TEMPLATE = """<section><div class="section-heading"><div><p class="eyebrow">Cluster portfolio</p><h2>Configured clusters</h2><p>Each management decision is calculated from its own latest snapshot. Reports remain isolated.</p></div><a class="button-link" href="{{ url_for('new_cluster') }}">Add cluster</a></div><table class="cluster-table"><thead><tr><th>Cluster</th><th>Last snapshot</th><th>Management decision</th><th>Safe CPU / memory</th><th>30-day trend</th><th>Connection</th><th>Actions</th></tr></thead><tbody>{% for cluster in clusters %}<tr><td><a href="{{ url_for('edit_cluster', cluster_id=cluster.id) }}">{{ cluster.name }}</a><span class="cell-note">{{ cluster.kube_context if not cluster.legacy_connection else 'Update required' }}</span></td><td>{% if cluster.report_quality %}{{ cluster.last_collected_at }}<br><span class="cell-note">{{ cluster.report_quality.state }}</span>{% else %}Not collected{% endif %}</td><td>{% if cluster.decision %}<span class="capacity-badge compact {{ cluster.decision.state|lower|replace(' ', '-') }}">{{ cluster.decision.state }}</span><p class="table-summary">{{ cluster.decision.summary }}</p>{% else %}<span class="badge warning">No report</span>{% endif %}</td><td>{% if cluster.planning_safe %}{{ format_cpu(cluster.planning_safe.cpu_millicores) }}<br><span class="cell-note">{{ format_bytes(cluster.planning_safe.memory_bytes) }}</span>{% else %}Unavailable{% endif %}</td><td>{% if cluster.trend %}<strong>{{ cluster.trend.state }}</strong><br><span class="cell-note">{{ cluster.trend.confidence }}</span>{% else %}Unavailable{% endif %}</td><td>{% if active_cluster and cluster.id == active_cluster.id %}<span class="badge info">Active</span>{% else %}<form class="inline-form" method="post" action="{{ url_for('activate_cluster') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="cluster_id" value="{{ cluster.id }}"><input type="hidden" name="next" value="{{ url_for('overview') }}"><button class="quiet" type="submit">Use</button></form>{% endif %}</td><td><div class="table-actions">{% if not cluster.legacy_connection %}<form method="post" action="{{ url_for('take_cluster_snapshot', cluster_id=cluster.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="quiet table-action-button" type="submit">Take snapshot</button></form>{% endif %}<a class="quiet-link" href="{{ url_for('edit_cluster', cluster_id=cluster.id) }}">Edit</a><a class="quiet-link danger-link" href="{{ url_for('remove_cluster', cluster_id=cluster.id) }}">Remove</a></div></td></tr>{% else %}<tr><td colspan="7">No clusters configured.</td></tr>{% endfor %}</tbody></table></section>"""

_CLUSTER_FORM_TEMPLATE = """
<div class="cluster-layout">
  {% if cluster %}
  <section class="cluster-summary">
    <p class="eyebrow">Cluster connection</p>
    <h2>{{ cluster.name }}</h2>
    <dl>
      <dt>API endpoint</dt><dd>{{ cluster.endpoint or 'Unavailable until updated' }}</dd>
      <dt>Kubeconfig context</dt><dd>{{ cluster.kube_context or 'Update required' }}</dd>
      <dt>API IP</dt><dd>{{ cluster.api_ip or 'Kubeconfig server' }}</dd>
    </dl>
    {% if cluster.legacy_connection %}<p class="notice warning">Update this connection with a kubeconfig before the next collection.</p>{% endif %}
    <p><a href="{{ url_for('clusters') }}">Back to clusters</a></p>
  </section>
  {% else %}
  <section class="cluster-summary">
    <p class="eyebrow">New connection</p>
    <h2>Add Kubernetes cluster</h2>
  </section>
  {% endif %}
  <section class="connection-form">
    <p class="eyebrow">Read-only connection</p>
    <h2>{{ 'Update cluster connection' if cluster else 'Connection details' }}</h2>
    <form method="post" enctype="multipart/form-data" autocomplete="off">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <label for="cluster-name">Cluster name<input id="cluster-name" name="name" value="{{ form.name }}" maxlength="64" required></label>
      <fieldset class="kubeconfig-source">
        <legend>Kubeconfig source</legend>
        {% if cluster %}<label><input type="radio" name="kubeconfig_source" value="existing" {% if form.kubeconfig_source == 'existing' %}checked{% endif %}>Use current configuration</label>{% endif %}
        <label><input type="radio" name="kubeconfig_source" value="upload" {% if form.kubeconfig_source == 'upload' %}checked{% endif %}>Upload file</label>
        <label><input type="radio" name="kubeconfig_source" value="paste" {% if form.kubeconfig_source == 'paste' %}checked{% endif %}>Paste configuration</label>
        <label><input type="radio" name="kubeconfig_source" value="path" {% if form.kubeconfig_source == 'path' %}checked{% endif %}>Use mounted file</label>
      </fieldset>
      <div class="kubeconfig-inputs">
        <label for="kubeconfig-upload">Kubeconfig file<input id="kubeconfig-upload" name="kubeconfig_upload" type="file" accept=".yaml,.yml,text/yaml,application/x-yaml,text/plain"></label>
        <label for="kubeconfig-text" class="kubeconfig-paste">Paste kubeconfig<textarea id="kubeconfig-text" name="kubeconfig_text" spellcheck="false" placeholder="apiVersion: v1"></textarea></label>
        <label for="kubeconfig-file">Mounted kubeconfig file<input id="kubeconfig-file" name="kubeconfig_file" value="{{ form.kubeconfig_file }}" placeholder="/run/kcp/clusters/production.kubeconfig"></label>
      </div>
      <label for="kube-context">Kubeconfig context<input id="kube-context" name="kube_context" value="{{ form.kube_context }}" placeholder="Current context"></label>
      <label for="api-ip">Kubernetes API IP<input id="api-ip" name="api_ip" value="{{ form.api_ip }}" inputmode="decimal" placeholder="10.20.30.40"></label>
      <label><input type="checkbox" name="disable_proxy" value="1" {% if form.disable_proxy %}checked{% endif %}>Do not use HTTP(S) proxy for this cluster</label>
      <button type="submit">{{ 'Save cluster' if cluster else 'Add cluster' }}</button>
    </form>
  </section>
</div>
{% if cluster %}
<section class="cluster-operations">
  <div class="section-heading">
    <div><p class="eyebrow">Cluster operations</p><h2>Connection testing</h2></div>
  </div>
  {% if cluster.legacy_connection %}
  <p class="notice warning">Save a kubeconfig-based connection before running cluster operations.</p>
  {% else %}
  <div class="operation-actions">
    <form method="post" action="{{ url_for('test_cluster_connection', cluster_id=cluster.id) }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <button class="quiet" type="submit">Test connection</button>
    </form>
  </div>
  {% endif %}
  <div class="connection-log">
    <div class="section-heading"><h2>Connection log</h2></div>
    <table>
      <thead><tr><th>Timestamp</th><th>Action</th><th>Status</th><th>Details</th></tr></thead>
      <tbody>{% for log in logs %}<tr><td>{{ log.created_at }}</td><td>{{ 'Connection test' if log.action == 'connection-test' else 'Snapshot' }}</td><td><span class="badge {{ 'info' if log.status == 'success' else 'critical' }}">{{ log.status }}</span></td><td>{{ log.message }}</td></tr>{% else %}<tr><td colspan="4">No cluster operations recorded.</td></tr>{% endfor %}</tbody>
    </table>
  </div>
</section>
{% endif %}
"""

_CLUSTER_REMOVE_TEMPLATE = """<section class="remove-panel"><p class="eyebrow">Cluster connection</p><h2>Remove {{ cluster.name }}?</h2><p>Removing this cluster also removes its stored capacity reports. This cannot be undone.</p><dl><dt>API endpoint</dt><dd>{{ cluster.endpoint }}</dd><dt>Kubeconfig context</dt><dd>{{ cluster.kube_context }}</dd></dl><form class="remove-actions" method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><a class="quiet-link" href="{{ url_for('clusters') }}">Cancel</a><button class="danger" type="submit">Remove cluster</button></form></section>"""

_DOCS_TEMPLATE = """<form class="search" method="get"><label>Search bundled Kubernetes guidance<input name="q" value="{{ query }}"></label><button type="submit">Search</button></form><section class="doc-list">{% for document in documents %}<a href="{{ url_for('document_detail', document_id=document.id) }}"><strong>{{ document.title }}</strong><span>{{ document.id }}</span></a>{% else %}<p>No local documentation matched.</p>{% endfor %}</section>"""

_DOCUMENT_TEMPLATE = """<section class="doc-reader-header"><a class="back-link" href="{{ url_for('documentation') }}">Back to local docs</a><p class="eyebrow">Offline Kubernetes reference</p><h2>{{ document.title }}</h2><p>Bundled for dark-site use. This article is local; its source details remain available below.</p><details class="doc-provenance"><summary>Source details</summary><dl><dt>Kubernetes baseline</dt><dd>{{ kubernetes_version }}</dd><dt>Source revision</dt><dd><code>{{ document.source_revision }}</code></dd><dt>Canonical source</dt><dd>{{ document.canonical_url }}</dd></dl></details></section><section class="doc-reading-layout">{% if article.headings %}<aside class="doc-toc" aria-label="On this page"><p>On this page</p><ol>{% for heading in article.headings %}<li><a href="#{{ heading.anchor }}">{{ heading.title }}</a></li>{% endfor %}</ol></aside>{% endif %}<article class="doc-article">{{ article.content }}</article></section>"""

_BASE_TEMPLATE += "</main>{% if authenticated %}</section></div>{% endif %}</body></html>"
