# Lab110 KCP Validation Status

Last updated: August 6, 2026

## Milestone 1: Complete

- `lab110` is reachable from the KCP host at `https://192.168.0.110:6443`; unauthenticated requests correctly receive `401`.
- The `lab110` node is `Ready` on Kubernetes `v1.36.3+k3s1`.
- `kcp-reader` has the required read-only access for `/version`, nodes, namespaces, pods, ResourceQuotas, LimitRanges, events, ReplicaSets, and HPAs.
- KCP's collector authenticated with the dedicated reader kubeconfig and returned `v1.36.3+k3s1`.
- `metrics.k8s.io` is currently unavailable. This is a known partial-data condition; it must remain visible in KCP reports and must not be interpreted as zero usage.

## Milestone 2: Complete

- `lab110` was added through KCP's authenticated kubeconfig upload flow and retains the `kcp-lab110` context.
- A fresh KCP session selects `lab110` as the active cluster.
- The real KCP runtime verified the connection against `lab110` and recorded `Connected to Kubernetes v1.36.3+k3s1.`
- The connection log contains only the action, status, timestamp, and redacted result message; it does not expose kubeconfig or token contents.

## Current Work

Milestone 3: collect a real snapshot and validate data coverage and partial-data behavior.
