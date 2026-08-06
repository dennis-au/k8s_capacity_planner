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

## Milestone 3: Complete

- Snapshot `2` was collected from the real `lab110` API and records Kubernetes `v1.36.3+k3s1`.
- The snapshot contains 1 node, 5 namespaces, 3 workload owners, requests, limits, QoS, node conditions, quota/LimitRange coverage, HPA coverage, and warning events.
- This test cluster currently has no ResourceQuota, LimitRange, or HPA resources; KCP records those as absent state rather than missing collection data.
- The optional Metrics API is unavailable and is shown as **Unavailable** in Overview with a snapshot warning; usage values are not fabricated.
- Overview, Nodes, Namespaces, Workloads, and History render the `lab110` snapshot successfully.

## Milestone 4: Complete

- All 13 persisted findings include resource evidence, a recommendation, and an embedded Kubernetes source reference.
- Allocation displays Node Allocatable/request headroom and 3 workload recommendations, each with its Kubernetes source citation.
- With zero Metrics API snapshots, allocation recommendations correctly request usable metrics before proposing numeric request changes.
- Findings and Allocation link to local documentation; the resource-metrics document displays Kubernetes baseline `v1.36` and its source revision.
- `lab110` reports Kubernetes `v1.36.3+k3s1`, which matches the embedded `v1.36` minor baseline. The automated version-compatibility test covers the warning behavior for other minors.

## Current Work

Milestone 5: validate exports, active-cluster isolation, and redacted cluster-operation history.
