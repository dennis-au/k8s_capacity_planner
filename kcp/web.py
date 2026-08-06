from __future__ import annotations

import json
import logging
import secrets
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, abort, flash, jsonify, redirect, render_template_string, request, session, url_for
from markupsafe import Markup

from kcp.allocation import build_allocation_plan
from kcp.config import RuntimeConfig
from kcp.docs import DocumentRegistry
from kcp.kubeconfig_files import MAX_KUBECONFIG_BYTES, KubeconfigFiles
from kcp.kubernetes import KubernetesCollector, inspect_kubeconfig, inspect_kubeconfig_text
from kcp.service import CollectionService
from kcp.store import Store


LOGGER = logging.getLogger(__name__)


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
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
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

    @app.get("/")
    def overview() -> str:
        active_cluster = _active_cluster(store)
        record = store.latest_snapshot(active_cluster["id"]) if active_cluster else None
        return _render(
            "Overview",
            _OVERVIEW_TEMPLATE,
            record=record,
            summary=_summary(record),
            service=service,
            connection=active_cluster,
        )

    @app.get("/cluster")
    def legacy_cluster() -> Response:
        return redirect(url_for("clusters"))

    @app.get("/clusters")
    def clusters() -> str:
        return _render("Clusters", _CLUSTERS_TEMPLATE, clusters=store.list_clusters())

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
                return _render("Add cluster", _CLUSTER_FORM_TEMPLATE, cluster=None, form=form)
            session["active_cluster_id"] = cluster["id"]
            flash("Cluster connection saved.", "success")
            return redirect(url_for("edit_cluster", cluster_id=cluster["id"]))
        return _render("Add cluster", _CLUSTER_FORM_TEMPLATE, cluster=None, form=form)

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
                return _render("Edit cluster", _CLUSTER_FORM_TEMPLATE, cluster=cluster, form=form)
            if imported_file or normalized["kubeconfig_file"] != previous_kubeconfig_file:
                kubeconfig_files.remove(previous_kubeconfig_file)
            flash("Cluster connection saved.", "success")
            return redirect(url_for("edit_cluster", cluster_id=cluster_id))
        return _render("Edit cluster", _CLUSTER_FORM_TEMPLATE, cluster=cluster, form=form)

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
        history = store.list_snapshots(active_cluster["id"], limit=2_160) if active_cluster else []
        plan = (
            build_allocation_plan(
                record["payload"]["snapshot"],
                [snapshot["payload"]["snapshot"] for snapshot in history],
                docs,
            )
            if record
            else None
        )
        return _render("Allocation", _ALLOCATION_TEMPLATE, record=record, plan=plan)

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
    def history() -> str:
        active_cluster = _active_cluster(store)
        snapshots = store.list_snapshots(active_cluster["id"]) if active_cluster else []
        return _render("History", _HISTORY_TEMPLATE, snapshots=snapshots)

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
        return _render("Documentation", _DOCUMENT_TEMPLATE, document=document)

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
        if format_name == "json":
            return Response(json.dumps(record["payload"], separators=(",", ":")), mimetype="application/json")
        if format_name == "md":
            return Response(_markdown_export(record), mimetype="text/markdown")
        if format_name == "html":
            return Response(_html_export(record), mimetype="text/html")
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
            and title not in {"Documentation", "Clusters", "Add cluster", "Edit cluster", "Remove cluster", "Account"},
            "format_cpu": _format_cpu,
            "format_bytes": _format_bytes,
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


def _cluster_form_values() -> dict[str, str]:
    return {
        "name": request.form.get("name", "").strip(),
        "kubeconfig_source": request.form.get("kubeconfig_source", "path").strip(),
        "kubeconfig_file": request.form.get("kubeconfig_file", "").strip(),
        "kubeconfig_text": request.form.get("kubeconfig_text", ""),
        "kube_context": request.form.get("kube_context", "").strip(),
        "api_ip": request.form.get("api_ip", "").strip(),
    }


def _validate_cluster_form(
    form: dict[str, str], kubeconfig_files: KubeconfigFiles, existing_file: str | None = None
) -> tuple[dict[str, str], str | None]:
    source = form["kubeconfig_source"]
    imported_file: str | None = None
    if source == "existing":
        if not existing_file:
            raise ValueError("Choose a kubeconfig source.")
        kubeconfig_file = _readable_file(existing_file, "Existing kubeconfig")
        details = inspect_kubeconfig(kubeconfig_file, form["kube_context"] or None, form["api_ip"] or None)
    elif source == "path":
        kubeconfig_file = _readable_file(form["kubeconfig_file"], "Kubeconfig")
        details = inspect_kubeconfig(kubeconfig_file, form["kube_context"] or None, form["api_ip"] or None)
    elif source == "paste":
        details = inspect_kubeconfig_text(form["kubeconfig_text"], form["kube_context"] or None, form["api_ip"] or None)
        imported_file = kubeconfig_files.save_text(form["kubeconfig_text"])
        kubeconfig_file = imported_file
    elif source == "upload":
        contents = _uploaded_kubeconfig_text()
        details = inspect_kubeconfig_text(contents, form["kube_context"] or None, form["api_ip"] or None)
        imported_file = kubeconfig_files.save_text(contents)
        kubeconfig_file = imported_file
    else:
        raise ValueError("Choose a kubeconfig source.")
    normalized = {
        "name": form["name"],
        "kubeconfig_file": kubeconfig_file,
        "kube_context": details.context,
        "endpoint": details.endpoint,
        "api_ip": form["api_ip"],
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


def _empty_cluster_form() -> dict[str, str]:
    return {
        "name": "",
        "kubeconfig_source": "upload",
        "kubeconfig_file": "",
        "kubeconfig_text": "",
        "kube_context": "",
        "api_ip": "",
    }


def _cluster_form(cluster: dict[str, Any]) -> dict[str, str]:
    return {
        "name": str(cluster["name"]),
        "kubeconfig_source": "existing",
        "kubeconfig_file": str(cluster["kubeconfig_file"]),
        "kubeconfig_text": "",
        "kube_context": str(cluster["kube_context"]),
        "api_ip": str(cluster["api_ip"]),
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


def _format_cpu(value: int) -> str:
    return f"{value / 1000:g} cores" if value >= 1000 and value % 1000 == 0 else f"{value}m"


def _format_bytes(value: int) -> str:
    for suffix, scale in (("Gi", 1024**3), ("Mi", 1024**2), ("Ki", 1024)):
        if value >= scale:
            return f"{value / scale:.1f}{suffix}"
    return f"{value}B"


def _format_percent(value: float) -> str:
    return f"{value:.0%}"


def _markdown_export(record: dict) -> str:
    payload = record["payload"]
    snapshot = payload["snapshot"]
    lines = [
        "# Kubernetes Capacity Report",
        "",
        f"Cluster: {record['cluster_name'] or 'Unknown'}",
        f"Collected: {record['collected_at']}",
        f"Cluster version: {snapshot['cluster_version']}",
        "",
        "## Findings",
    ]
    for finding in payload.get("findings", []):
        lines.append(f"- [{finding['severity'].upper()}] {finding['resource']}: {finding['title']}")
        lines.append(f"  {finding['evidence']}")
        lines.append(f"  Local source: /docs/{finding['source']['document_id']} ({finding['source']['section']})")
    return "\n".join(lines) + "\n"


def _html_export(record: dict) -> str:
    markdown = _markdown_export(record)
    escaped = markdown.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<!doctype html><html><head><meta charset='utf-8'><title>KCP Report</title></head><body><pre>{escaped}</pre></body></html>"


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
<title>{{ title }} | KCP</title><link rel="stylesheet" href="{{ url_for('static', filename='app.css') }}"></head><body>
{% if authenticated %}
<header class="topbar"><a class="brand" href="{{ url_for('overview') }}">KCP <span>Capacity Planner</span></a>
<nav aria-label="Primary"><a href="{{ url_for('overview') }}">Overview</a><a href="{{ url_for('allocation') }}">Allocation</a><a href="{{ url_for('clusters') }}">Clusters</a><a href="{{ url_for('findings') }}">Findings</a><a href="{{ url_for('nodes') }}">Nodes</a><a href="{{ url_for('namespaces') }}">Namespaces</a><a href="{{ url_for('workloads') }}">Workloads</a><a href="{{ url_for('history') }}">History</a><a href="{{ url_for('documentation') }}">Docs</a><a href="{{ url_for('account') }}">Account</a></nav>
{% if clusters %}<form class="cluster-switcher" method="post" action="{{ url_for('activate_cluster') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="next" value="{{ current_path }}"><label for="active-cluster">Active cluster</label><select id="active-cluster" name="cluster_id">{% for cluster in clusters %}<option value="{{ cluster.id }}" {% if active_cluster and cluster.id == active_cluster.id %}selected{% endif %}>{{ cluster.name }}</option>{% endfor %}</select><button class="quiet" type="submit">Use</button></form>{% endif %}
<form method="post" action="{{ url_for('logout') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="quiet" type="submit">Sign out</button></form></header>
{% endif %}
<main class="page"><div class="page-heading"><div><p class="eyebrow">{{ active_cluster.name if active_cluster else 'Dark-site operation' }}</p><h1>{{ title }}</h1></div>{% if show_refresh %}<form method="post" action="{{ url_for('collect') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button type="submit">Refresh</button></form>{% endif %}</div>
{% with messages = get_flashed_messages(with_categories=true) %}{% for category, message in messages %}<p class="notice {{ category }}">{{ message }}</p>{% endfor %}{% endwith %}
{{ content }}
"""

_LOGIN_TEMPLATE = """<section class="login-panel"><p class="eyebrow">Dark-site operation</p><h1>Capacity Planner</h1><p>Sign in to review the configured Kubernetes endpoint.</p><form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label>Administrator<input name="username" autocomplete="username" required></label><label>Password<input name="password" type="password" autocomplete="current-password" required></label><button type="submit">Sign in</button></form></section>"""

_ACCOUNT_TEMPLATE = """<div class="account-layout"><section class="account-summary"><p class="eyebrow">Local administrator</p><h2>Administrator account</h2><dl><dt>Username</dt><dd>{{ username }}</dd><dt>Authentication</dt><dd>Local password</dd></dl></section><section class="account-form"><p class="eyebrow">Password</p><h2>Change password</h2><form method="post" autocomplete="off"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label for="new-password">New password<input id="new-password" name="new_password" type="password" autocomplete="new-password" minlength="12" maxlength="1024" required></label><label for="confirm-password">Confirm new password<input id="confirm-password" name="confirm_password" type="password" autocomplete="new-password" minlength="12" maxlength="1024" required></label><button type="submit">Update password</button></form></section></div>"""

_OVERVIEW_TEMPLATE = """{% if not record %}<section class="empty"><div class="empty-copy">{% if connection %}<p class="eyebrow">First report</p><h2>No snapshots collected</h2><p>Confirm the active cluster connection, then collect the first read-only snapshot.</p><a class="button-link" href="{{ url_for('clusters') }}">Review clusters</a>{% else %}<p class="eyebrow">First cluster</p><h2>Add a cluster</h2><p>Mount a read-only kubeconfig in the container, then add its path and context before collecting a report.</p><a class="button-link" href="{{ url_for('new_cluster') }}">Add cluster</a>{% endif %}</div><dl class="empty-details"><div><dt>Active cluster</dt><dd>{{ connection.name if connection else 'Not configured' }}</dd></div><div><dt>API endpoint</dt><dd>{{ connection.endpoint if connection else 'Unavailable' }}</dd></div><div><dt>Collection mode</dt><dd>Read-only</dd></div></dl></section>{% else %}
<section class="metrics"><div><span>Nodes</span><strong>{{ summary.nodes }}</strong></div><div><span>Namespaces</span><strong>{{ summary.namespaces }}</strong></div><div><span>Workloads</span><strong>{{ summary.workloads }}</strong></div><div class="critical"><span>Critical</span><strong>{{ summary.critical }}</strong></div><div class="warning"><span>Warnings</span><strong>{{ summary.warning }}</strong></div></section>
<section class="split"><div><h2>Latest snapshot</h2><dl><dt>Collected</dt><dd>{{ record.collected_at }}</dd><dt>Kubernetes</dt><dd>{{ record.cluster_version }}</dd><dt>Metrics API</dt><dd>{{ 'Available' if record.payload.snapshot.metrics_available else 'Unavailable' }}</dd></dl></div><div><h2>Next review</h2><p>Scheduled collection runs every configured interval. Manual refresh never overlaps an active collection.</p>{% if connection and service.last_error_for(connection.id) %}<p class="notice error">{{ service.last_error_for(connection.id) }}</p>{% endif %}</div></section>
<section><div class="section-heading"><h2>Priority findings</h2><a href="{{ url_for('findings') }}">All findings</a></div><table><thead><tr><th>Severity</th><th>Resource</th><th>Finding</th><th>Local source</th></tr></thead><tbody>{% for finding in record.payload.findings[:8] %}<tr><td><span class="badge {{ finding.severity }}">{{ finding.severity }}</span></td><td>{{ finding.resource }}</td><td>{{ finding.title }}</td><td><a href="{{ url_for('document_detail', document_id=finding.source.document_id) }}">{{ finding.source.document_title }}</a></td></tr>{% else %}<tr><td colspan="4">No findings.</td></tr>{% endfor %}</tbody></table></section>{% endif %}"""

_FINDINGS_TEMPLATE = """{% if not record %}<section class="empty"><h2>No snapshots collected</h2></section>{% else %}<div class="filters"><a href="{{ url_for('findings') }}">All</a><a href="{{ url_for('findings', severity='critical') }}">Critical</a><a href="{{ url_for('findings', severity='warning') }}">Warnings</a><a href="{{ url_for('findings', severity='info') }}">Info</a></div><table><thead><tr><th>Severity</th><th>Resource</th><th>Evidence</th><th>Recommended action</th><th>Guidance</th></tr></thead><tbody>{% for finding in findings %}<tr><td><span class="badge {{ finding.severity }}">{{ finding.severity }}</span></td><td>{{ finding.resource }}</td><td>{{ finding.evidence }}</td><td>{{ finding.recommendation }}</td><td><a href="{{ url_for('document_detail', document_id=finding.source.document_id) }}">{{ finding.source.section }}</a></td></tr>{% else %}<tr><td colspan="5">No findings match this filter.</td></tr>{% endfor %}</tbody></table>{% endif %}"""

_ALLOCATION_TEMPLATE = """{% if not record %}<section class="empty"><div class="empty-copy"><p class="eyebrow">Allocation guidance</p><h2>No snapshots collected</h2><p>Collect a read-only snapshot before reviewing request-based capacity and workload recommendations.</p></div></section>{% else %}<section class="allocation-intro"><div><p class="eyebrow">Allocation guidance</p><h2>Request-based scheduling capacity</h2><p>Kubernetes schedules Pods against declared requests and Node Allocatable. Available capacity is an aggregate; confirm that a single node can fit each planned Pod.</p></div><a class="quiet-link" href="{{ url_for('document_detail', document_id=plan.capacity_source.document_id) }}">{{ plan.capacity_source.document_title }}</a></section><section class="metrics allocation-metrics"><div><span>Allocatable CPU</span><strong>{{ format_cpu(plan.total_allocatable.cpu_millicores) }}</strong></div><div><span>Requested CPU</span><strong>{{ format_cpu(plan.total_requested.cpu_millicores) }}</strong></div><div><span>Available CPU</span><strong>{{ format_cpu(plan.total_remaining.cpu_millicores) }}</strong></div><div><span>Allocatable memory</span><strong>{{ format_bytes(plan.total_allocatable.memory_bytes) }}</strong></div><div><span>Requested memory</span><strong>{{ format_bytes(plan.total_requested.memory_bytes) }}</strong></div><div><span>Available memory</span><strong>{{ format_bytes(plan.total_remaining.memory_bytes) }}</strong></div></section><section><div class="section-heading"><div><h2>Node fit</h2><p>Remaining capacity after declared Pod requests; resource pressure makes a node unsuitable for additional demand.</p></div></div><table><thead><tr><th>Node</th><th>Allocatable CPU</th><th>Requested CPU</th><th>Available CPU</th><th>Allocatable memory</th><th>Requested memory</th><th>Available memory</th><th>State</th></tr></thead><tbody>{% for node in plan.nodes %}<tr><td>{{ node.name }}</td><td>{{ format_cpu(node.allocatable.cpu_millicores) }}</td><td>{{ format_cpu(node.requested.cpu_millicores) }}</td><td>{{ format_cpu(node.remaining.cpu_millicores) }}</td><td>{{ format_bytes(node.allocatable.memory_bytes) }}</td><td>{{ format_bytes(node.requested.memory_bytes) }}</td><td>{{ format_bytes(node.remaining.memory_bytes) }}</td><td>{% if node.has_pressure %}<span class="badge warning">Pressure</span>{% else %}<span class="badge info">Available</span>{% endif %}</td></tr>{% else %}<tr><td colspan="8">No node data available.</td></tr>{% endfor %}</tbody></table></section><section class="allocation-recommendations"><div class="section-heading"><div><h2>Workload request recommendations</h2><p>Observed floors use the highest retained workload usage from {{ plan.metric_snapshot_count }} Metrics API snapshot{{ '' if plan.metric_snapshot_count == 1 else 's' }}. They are evidence for review, not automatic changes.</p></div></div><table><thead><tr><th>Workload</th><th>Current total request</th><th>Observed peak</th><th>Suggested floor</th><th>Recommendation</th><th>Guidance</th></tr></thead><tbody>{% for recommendation in plan.recommendations %}<tr><td>{{ recommendation.identity }}</td><td>{{ format_cpu(recommendation.current_request.cpu_millicores) }} / {{ format_bytes(recommendation.current_request.memory_bytes) }}</td><td>{% if recommendation.observed_peak %}{{ format_cpu(recommendation.observed_peak.cpu_millicores) }} / {{ format_bytes(recommendation.observed_peak.memory_bytes) }}<br><span class="cell-note">{{ recommendation.sample_count }} snapshot{{ '' if recommendation.sample_count == 1 else 's' }}</span>{% else %}No usable observation{% endif %}</td><td>{% if recommendation.suggested_request %}{{ format_cpu(recommendation.suggested_request.cpu_millicores) }} / {{ format_bytes(recommendation.suggested_request.memory_bytes) }}{% else %}No increase suggested{% endif %}</td><td><span class="badge {{ recommendation.severity }}">{{ recommendation.status|replace('-', ' ') }}</span><p class="recommendation-copy">{{ recommendation.recommendation }}</p></td><td><a href="{{ url_for('document_detail', document_id=recommendation.source.document_id) }}">{{ recommendation.source.document_title }}: {{ recommendation.source.section }}</a></td></tr>{% else %}<tr><td colspan="6">No workloads available for allocation guidance.</td></tr>{% endfor %}</tbody></table></section>{% endif %}"""

_NODES_TEMPLATE = """<table><thead><tr><th>Node</th><th>Allocatable CPU</th><th>Requested CPU</th><th>Allocatable memory</th><th>Requested memory</th><th>Pressure</th></tr></thead><tbody>{% for node in nodes %}<tr><td>{{ node.name }}</td><td>{{ format_cpu(node.allocatable.cpu_millicores) }}</td><td>{{ format_cpu(node.requested.cpu_millicores) }}</td><td>{{ format_bytes(node.allocatable.memory_bytes) }}</td><td>{{ format_bytes(node.requested.memory_bytes) }}</td><td>{{ node.conditions|join(', ') or 'None' }}</td></tr>{% else %}<tr><td colspan="6">No node data available.</td></tr>{% endfor %}</tbody></table>"""

_NAMESPACES_TEMPLATE = """<table><thead><tr><th>Namespace</th><th>LimitRange</th><th>Quota usage</th></tr></thead><tbody>{% for namespace in namespaces %}<tr><td>{{ namespace.name }}</td><td>{{ 'Present' if namespace.has_limit_range else 'Missing' }}</td><td>{% for name, quota in namespace.quotas.items() %}{{ name }}: {{ quota.used }}/{{ quota.hard }}{% if not loop.last %}; {% endif %}{% else %}No quota{% endfor %}</td></tr>{% else %}<tr><td colspan="3">No namespace data available.</td></tr>{% endfor %}</tbody></table>"""

_WORKLOADS_TEMPLATE = """<table><thead><tr><th>Workload</th><th>Replicas</th><th>QoS</th><th>Requests</th><th>Limits</th><th>HPA</th></tr></thead><tbody>{% for workload in workloads %}<tr><td>{{ workload.namespace }}/{{ workload.kind }}/{{ workload.name }}</td><td>{{ workload.replicas }}</td><td>{{ workload.qos }}</td><td>{{ format_cpu(workload.requests.cpu_millicores) }} / {{ format_bytes(workload.requests.memory_bytes) }}</td><td>{{ format_cpu(workload.limits.cpu_millicores) }} / {{ format_bytes(workload.limits.memory_bytes) }}</td><td>{{ 'Present' if workload.has_hpa else 'None' }}</td></tr>{% else %}<tr><td colspan="6">No workload data available.</td></tr>{% endfor %}</tbody></table>"""

_HISTORY_TEMPLATE = """<table><thead><tr><th>ID</th><th>Collected</th><th>Kubernetes</th><th>Exports</th></tr></thead><tbody>{% for snapshot in snapshots %}<tr><td>{{ snapshot.id }}</td><td>{{ snapshot.collected_at }}</td><td>{{ snapshot.cluster_version }}</td><td><a href="{{ url_for('export', snapshot_ref=snapshot.id, format_name='json') }}">JSON</a> <a href="{{ url_for('export', snapshot_ref=snapshot.id, format_name='md') }}">Markdown</a> <a href="{{ url_for('export', snapshot_ref=snapshot.id, format_name='html') }}">HTML</a></td></tr>{% else %}<tr><td colspan="4">No stored snapshots.</td></tr>{% endfor %}</tbody></table>"""

_CLUSTERS_TEMPLATE = """<section><div class="section-heading"><div><h2>Configured clusters</h2><p>Snapshots, findings, and exports remain isolated for each connection.</p></div><a class="button-link" href="{{ url_for('new_cluster') }}">Add cluster</a></div><table class="cluster-table"><thead><tr><th>Cluster</th><th>Context</th><th>API endpoint</th><th>Last snapshot</th><th>Status</th><th>Actions</th></tr></thead><tbody>{% for cluster in clusters %}<tr><td><a href="{{ url_for('edit_cluster', cluster_id=cluster.id) }}">{{ cluster.name }}</a></td><td>{{ cluster.kube_context if not cluster.legacy_connection else 'Update required' }}</td><td>{{ cluster.endpoint or 'Unavailable' }}</td><td>{{ cluster.last_collected_at or 'Not collected' }}</td><td>{% if active_cluster and cluster.id == active_cluster.id %}<span class="badge info">Active</span>{% else %}<form class="inline-form" method="post" action="{{ url_for('activate_cluster') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="cluster_id" value="{{ cluster.id }}"><input type="hidden" name="next" value="{{ url_for('overview') }}"><button class="quiet" type="submit">Use</button></form>{% endif %}</td><td><div class="table-actions"><a class="quiet-link" href="{{ url_for('edit_cluster', cluster_id=cluster.id) }}">Edit</a><a class="quiet-link danger-link" href="{{ url_for('remove_cluster', cluster_id=cluster.id) }}">Remove</a></div></td></tr>{% else %}<tr><td colspan="6">No clusters configured.</td></tr>{% endfor %}</tbody></table></section>"""

_CLUSTER_FORM_TEMPLATE = """<div class="cluster-layout">{% if cluster %}<section class="cluster-summary"><p class="eyebrow">Cluster connection</p><h2>{{ cluster.name }}</h2><dl><dt>API endpoint</dt><dd>{{ cluster.endpoint or 'Unavailable until updated' }}</dd><dt>Kubeconfig context</dt><dd>{{ cluster.kube_context or 'Update required' }}</dd><dt>API IP</dt><dd>{{ cluster.api_ip or 'Kubeconfig server' }}</dd></dl>{% if cluster.legacy_connection %}<p class="notice warning">Update this connection with a kubeconfig before the next collection.</p>{% endif %}<p><a href="{{ url_for('clusters') }}">Back to clusters</a></p></section>{% else %}<section class="cluster-summary"><p class="eyebrow">New connection</p><h2>Add Kubernetes cluster</h2></section>{% endif %}<section class="connection-form"><p class="eyebrow">Read-only connection</p><h2>{{ 'Update cluster connection' if cluster else 'Connection details' }}</h2><form method="post" enctype="multipart/form-data" autocomplete="off"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label for="cluster-name">Cluster name<input id="cluster-name" name="name" value="{{ form.name }}" maxlength="64" required></label><fieldset class="kubeconfig-source"><legend>Kubeconfig source</legend>{% if cluster %}<label><input type="radio" name="kubeconfig_source" value="existing" {% if form.kubeconfig_source == 'existing' %}checked{% endif %}>Use current configuration</label>{% endif %}<label><input type="radio" name="kubeconfig_source" value="upload" {% if form.kubeconfig_source == 'upload' %}checked{% endif %}>Upload file</label><label><input type="radio" name="kubeconfig_source" value="paste" {% if form.kubeconfig_source == 'paste' %}checked{% endif %}>Paste configuration</label><label><input type="radio" name="kubeconfig_source" value="path" {% if form.kubeconfig_source == 'path' %}checked{% endif %}>Use mounted file</label></fieldset><div class="kubeconfig-inputs"><label for="kubeconfig-upload">Kubeconfig file<input id="kubeconfig-upload" name="kubeconfig_upload" type="file" accept=".yaml,.yml,text/yaml,application/x-yaml,text/plain"></label><label for="kubeconfig-text" class="kubeconfig-paste">Paste kubeconfig<textarea id="kubeconfig-text" name="kubeconfig_text" spellcheck="false" placeholder="apiVersion: v1"></textarea></label><label for="kubeconfig-file">Mounted kubeconfig file<input id="kubeconfig-file" name="kubeconfig_file" value="{{ form.kubeconfig_file }}" placeholder="/run/kcp/clusters/production.kubeconfig"></label></div><label for="kube-context">Kubeconfig context<input id="kube-context" name="kube_context" value="{{ form.kube_context }}" placeholder="Current context"></label><label for="api-ip">Kubernetes API IP<input id="api-ip" name="api_ip" value="{{ form.api_ip }}" inputmode="decimal" placeholder="10.20.30.40"></label><button type="submit">{{ 'Save cluster' if cluster else 'Add cluster' }}</button></form></section></div>"""

_CLUSTER_REMOVE_TEMPLATE = """<section class="remove-panel"><p class="eyebrow">Cluster connection</p><h2>Remove {{ cluster.name }}?</h2><p>Removing this cluster also removes its stored capacity reports. This cannot be undone.</p><dl><dt>API endpoint</dt><dd>{{ cluster.endpoint }}</dd><dt>Kubeconfig context</dt><dd>{{ cluster.kube_context }}</dd></dl><form class="remove-actions" method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><a class="quiet-link" href="{{ url_for('clusters') }}">Cancel</a><button class="danger" type="submit">Remove cluster</button></form></section>"""

_DOCS_TEMPLATE = """<form class="search" method="get"><label>Search bundled Kubernetes guidance<input name="q" value="{{ query }}"></label><button type="submit">Search</button></form><section class="doc-list">{% for document in documents %}<a href="{{ url_for('document_detail', document_id=document.id) }}"><strong>{{ document.title }}</strong><span>{{ document.id }}</span></a>{% else %}<p>No local documentation matched.</p>{% endfor %}</section>"""

_DOCUMENT_TEMPLATE = """<section class="doc-meta"><a href="{{ url_for('documentation') }}">Back to local docs</a><dl><dt>Kubernetes baseline</dt><dd>v1.36</dd><dt>Source revision</dt><dd><code>{{ document.source_revision }}</code></dd><dt>Canonical source</dt><dd>{{ document.canonical_url }}</dd></dl></section><pre class="doc-content">{{ document.content }}</pre>"""

_BASE_TEMPLATE += "</main></body></html>"
