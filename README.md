# Kubernetes Capacity Planner

KCP is a dark-site Kubernetes capacity dashboard. It runs outside the target cluster, lets an authenticated administrator add mounted kubeconfig files, stores reports in local SQLite, and bundles Kubernetes v1.36 guidance into the application image.

At runtime KCP does not fetch public documentation, load CDN assets, send telemetry, or make browser-side API calls. Its only network destination is the configured in-site Kubernetes API endpoint.

## Included Analysis

- Node allocatable capacity versus requested resources.
- Container CPU/memory requests, limits, QoS, and near-limit usage when Metrics API is available.
- Node pressure, ResourceQuota pressure, LimitRange coverage, HPA presence, and warning events.
- Local source citations for every rule, backed by the embedded Kubernetes v1.36 guidance bundle.

## Connected Build

The repository already contains a generated documentation bundle under `kcp/assets/k8s-docs`. Refresh it only from a connected build machine:

```sh
python -m kcp docs-sync \
  --catalog docs/catalog.yaml \
  --output kcp/assets/k8s-docs \
  --source-revision <immutable-kubernetes-website-commit>
```

Build the OCI image:

```sh
docker build -t kcp:0.1.4 .
```

## Cluster Access

Apply the read-only service account and binding. Change the namespace in the manifest if needed.

```sh
kubectl apply -f deploy/kcp-reader-rbac.yaml
kubectl -n kcp create token kcp-reader --duration=24h > kcp-reader.token
```

Create a kubeconfig that references the read-only identity and cluster CA, then mount the kubeconfig and any credential or CA files it references as read-only. The dashboard never creates or changes Kubernetes objects and does not request access to Secrets.

## Dark-site Run

For a Docker-only dark site, download the architecture-specific Docker archive from the `v0.1.4` release, verify its checksum, and load it directly:

```sh
# x86_64 host
sha256sum -c kcp-0.1.4-linux-amd64.docker.tar.sha256
docker load --input kcp-0.1.4-linux-amd64.docker.tar

# ARM64 host
sha256sum -c kcp-0.1.4-linux-arm64.docker.tar.sha256
docker load --input kcp-0.1.4-linux-arm64.docker.tar
```

Prepare these files on the dashboard host:

- TLS certificate and key for the dashboard endpoint.
- Bootstrap administrator password file with at least 12 characters.
- A writable data directory for the SQLite database and session key.

The `/srv/kcp/clusters` mount is optional. After signing in, add a cluster from the **Clusters** screen by uploading a kubeconfig, pasting its YAML, or entering the path to a mounted kubeconfig such as `/run/kcp/clusters/production.kubeconfig`.

```sh
docker run --detach --name kcp --restart unless-stopped \
  --publish 8443:8443 \
  --read-only --tmpfs /tmp \
  --volume /srv/kcp/data:/var/lib/kcp \
  --volume /srv/kcp/clusters:/run/kcp/clusters:ro \
  --volume /srv/kcp/tls.crt:/run/kcp/tls.crt:ro \
  --volume /srv/kcp/tls.key:/run/kcp/tls.key:ro \
  --volume /srv/kcp/admin-password:/run/kcp/admin-password:ro \
  --env KCP_DB_PATH=/var/lib/kcp/kcp.sqlite3 \
  --env KCP_REFRESH_INTERVAL=1h \
  --env KCP_RETENTION_DAYS=90 \
  --env KCP_TLS_CERT_FILE=/run/kcp/tls.crt \
  --env KCP_TLS_KEY_FILE=/run/kcp/tls.key \
  --env KCP_ADMIN_USERNAME=admin \
  --env KCP_ADMIN_PASSWORD_FILE=/run/kcp/admin-password \
  kcp:0.1.4
```

The password file is only used to create the first administrator. Reset it deliberately:

```sh
docker run --rm \
  --volume /srv/kcp/data:/var/lib/kcp \
  --volume /srv/kcp/new-admin-password:/run/kcp/new-admin-password:ro \
  --env KCP_DB_PATH=/var/lib/kcp/kcp.sqlite3 \
  --env KCP_ADMIN_USERNAME=admin \
  --entrypoint python kcp:0.1.4 \
  -m kcp admin reset-password --password-file /run/kcp/new-admin-password
```

Open `https://<dashboard-host>:8443` and use `https://<dashboard-host>:8443/healthz` for a non-authenticated health check.

## Cluster Connections

KCP supports multiple Kubernetes API endpoints per dashboard deployment. The **Clusters** screen lets the local administrator add, select, and update read-only connections after signing in. Each cluster keeps separate snapshots, findings, history, and exports. Scheduled collection visits all configured clusters sequentially, while manual refresh collects only the selected cluster.

For a mounted kubeconfig, mount the kubeconfig and any files it references into the container, then enter its mounted path and context in the dashboard. Uploaded and pasted kubeconfigs should include the credentials and CA data they need. An optional **Kubernetes API IP** field connects directly to a literal IPv4 or IPv6 address when cluster DNS is unavailable. KCP preserves TLS verification: set `tls-server-name` in the kubeconfig when the API certificate uses a DNS name.

KCP stores connection metadata in SQLite. Uploaded and pasted kubeconfigs are stored as local files in `/srv/kcp/data/kubeconfigs`; their contents are not written to SQLite. Mounted kubeconfigs remain external to KCP. It accepts static token, token-file, and client-certificate credentials, and rejects `exec`, `auth-provider`, proxy, and insecure-TLS kubeconfig settings. Existing token/CA connections remain visible with their historical reports, but must be updated with a kubeconfig before further collection.

## Backup and Restore

Stop the container before backup or restore so SQLite's main database and WAL files remain consistent. Copy the complete `/srv/kcp/data` directory, including `kcp.sqlite3`, `kcp.sqlite3-wal`, `kcp.sqlite3-shm`, `kcp.session.key`, and `kubeconfigs/`. Restore by stopping KCP, replacing the complete directory with the backup, ensuring Docker can write to it, then restarting the container.

KCP automatically removes snapshots older than 90 days. Export JSON, Markdown, or HTML from the History screen before they expire.

## Dark-site Verification

After image import, start KCP with `--network none` and use the local documentation screen to confirm bundled guidance works without egress. A network-disabled instance cannot collect from Kubernetes, which is expected; use a normal in-site network only for scheduled or manual cluster collection.

## Development

Create a virtual environment, install the pinned dependencies, and run the tests:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

For local UI work only, `python -m kcp serve --insecure-http` permits HTTP. Production deployments require TLS certificate and key files.
