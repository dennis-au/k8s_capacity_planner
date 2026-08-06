#!/bin/sh
set -eu

: "${KCP_KUBECONFIG_FILE:?KCP_KUBECONFIG_FILE is required}"
: "${KCP_DB_PATH:?KCP_DB_PATH is required}"

[ -r "$KCP_KUBECONFIG_FILE" ] || { echo "KCP_KUBECONFIG_FILE is not readable" >&2; exit 1; }

set -- gunicorn --workers 1 --threads 8 --worker-class gthread --timeout 60 --bind "${KCP_BIND:-0.0.0.0:8443}" --access-logfile - --error-logfile -

if [ "${KCP_INSECURE_HTTP:-0}" = "1" ]; then
  echo "WARNING: KCP_INSECURE_HTTP=1 is for development only" >&2
  exec "$@" kcp.wsgi:app
fi

: "${KCP_TLS_CERT_FILE:?KCP_TLS_CERT_FILE is required unless KCP_INSECURE_HTTP=1}"
: "${KCP_TLS_KEY_FILE:?KCP_TLS_KEY_FILE is required unless KCP_INSECURE_HTTP=1}"
[ -r "$KCP_TLS_CERT_FILE" ] || { echo "KCP_TLS_CERT_FILE is not readable" >&2; exit 1; }
[ -r "$KCP_TLS_KEY_FILE" ] || { echo "KCP_TLS_KEY_FILE is not readable" >&2; exit 1; }

exec "$@" --certfile "$KCP_TLS_CERT_FILE" --keyfile "$KCP_TLS_KEY_FILE" kcp.wsgi:app
