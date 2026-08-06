from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from kcp.config import load_runtime_config, load_tls_files, read_secret_file
from kcp.docs_sync import build_bundle
from kcp.store import Store
from kcp.web import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="kcp", description="Dark-site Kubernetes capacity planner")
    subcommands = parser.add_subparsers(dest="command", required=True)
    serve = subcommands.add_parser("serve", help="Run the dashboard")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8443)
    serve.add_argument("--insecure-http", action="store_true", help="Development use only")
    admin = subcommands.add_parser("admin", help="Administrative actions")
    admin_subcommands = admin.add_subparsers(dest="admin_command", required=True)
    reset = admin_subcommands.add_parser("reset-password", help="Reset the local administrator password")
    reset.add_argument("--password-file", type=Path, required=True)
    sync = subcommands.add_parser("docs-sync", help="Connected-build documentation importer")
    sync.add_argument("--catalog", type=Path, required=True)
    sync.add_argument("--output", type=Path, required=True)
    sync.add_argument("--source-revision", required=True)
    args = parser.parse_args()

    if args.command == "docs-sync":
        build_bundle(args.catalog, args.output, args.source_revision)
        return
    if args.command == "admin":
        db_value = os.environ.get("KCP_DB_PATH", "").strip()
        if not db_value:
            raise ValueError("KCP_DB_PATH is required")
        db_path = Path(db_value)
        store = Store(db_path)
        store.migrate()
        store.reset_admin_password(os.environ.get("KCP_ADMIN_USERNAME", "admin"), read_secret_file(args.password_file))
        return
    config = load_runtime_config(insecure_http=getattr(args, "insecure_http", False))
    store = Store(config.db_path)
    store.migrate()
    password_file = Path(os.environ.get("KCP_ADMIN_PASSWORD_FILE", ""))
    if not store.has_users():
        if not password_file.is_file():
            raise ValueError("KCP_ADMIN_PASSWORD_FILE must point to a readable file on first start")
        store.bootstrap_admin(config.admin_username, read_secret_file(password_file))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = create_app(config, store=store)
    ssl_context = None if config.insecure_http else load_tls_files()
    app.run(host=args.host, port=args.port, ssl_context=ssl_context)


if __name__ == "__main__":
    main()
