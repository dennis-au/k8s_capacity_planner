from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeConfig:
    kubeconfig_file: Path
    kube_context: str | None
    kube_api_ip: str | None
    db_path: Path
    docs_dir: Path
    refresh_seconds: int
    retention_days: int
    admin_username: str
    insecure_http: bool
    session_secret: str


def load_runtime_config(insecure_http: bool = False) -> RuntimeConfig:
    kubeconfig_file = _required_path("KCP_KUBECONFIG_FILE")
    kube_context = os.getenv("KCP_KUBE_CONTEXT", "").strip() or None
    kube_api_ip = os.getenv("KCP_KUBE_API_IP", "").strip() or None
    db_path = Path(_required("KCP_DB_PATH"))
    docs_dir = Path(__file__).parent / "assets" / "k8s-docs"
    return RuntimeConfig(
        kubeconfig_file=kubeconfig_file,
        kube_context=kube_context,
        kube_api_ip=kube_api_ip,
        db_path=db_path,
        docs_dir=docs_dir,
        refresh_seconds=_duration_seconds(os.getenv("KCP_REFRESH_INTERVAL", "1h")),
        retention_days=_positive_int(os.getenv("KCP_RETENTION_DAYS", "90"), "KCP_RETENTION_DAYS"),
        admin_username=os.getenv("KCP_ADMIN_USERNAME", "admin"),
        insecure_http=insecure_http or os.getenv("KCP_INSECURE_HTTP") == "1",
        session_secret=_session_secret(db_path),
    )


def read_secret_file(path: Path) -> str:
    value = path.read_text(encoding="utf-8").rstrip("\r\n")
    if not value:
        raise ValueError(f"secret file is empty: {path}")
    return value


def load_tls_files() -> tuple[str, str]:
    certificate = _required_path("KCP_TLS_CERT_FILE")
    key = _required_path("KCP_TLS_KEY_FILE")
    return str(certificate), str(key)


def _session_secret(db_path: Path) -> str:
    secret_path = Path(os.getenv("KCP_SESSION_SECRET_FILE", str(db_path.with_suffix(".session.key"))))
    if secret_path.exists():
        return read_secret_file(secret_path)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(48)
    descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(secret)
    return secret


def _duration_seconds(value: str) -> int:
    value = value.strip().lower()
    if value.endswith("h") and value[:-1].isdigit():
        return _positive_int(value[:-1], "KCP_REFRESH_INTERVAL") * 3600
    if value.endswith("m") and value[:-1].isdigit():
        return _positive_int(value[:-1], "KCP_REFRESH_INTERVAL") * 60
    if value.isdigit():
        return _positive_int(value, "KCP_REFRESH_INTERVAL")
    raise ValueError("KCP_REFRESH_INTERVAL must be seconds, minutes (e.g. 15m), or hours (e.g. 1h)")


def _positive_int(value: str, name: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if result <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return result


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _required_path(name: str) -> Path:
    path = Path(_required(name))
    if not path.is_file():
        raise ValueError(f"{name} must point to a readable file")
    return path
