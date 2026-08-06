# Lab110 KCP Validation Plan

## Milestone 1: Cluster Access Readiness [x]

- [x] Confirm `lab110` is reachable at `192.168.0.110:6443`.
- [x] Confirm the `kcp-reader` service account can read the APIs required by KCP: `/version`, nodes, namespaces, pods, quotas, LimitRanges, events, ReplicaSets, HPAs, and Metrics API resources when available.
- [x] Generate a dedicated kubeconfig with the API IP and CA data required for TLS verification.
- [x] Record any unavailable API as a known partial-data condition.

**Exit criteria:** KCP's kubeconfig can authenticate to `lab110` without using administrator credentials.

## Milestone 2: KCP Cluster Onboarding [x]

- [x] Add `lab110` in KCP using the read-only kubeconfig upload, paste, or mounted-file flow.
- [x] Select `lab110` as the active cluster.
- [x] Run **Test connection** and verify the connection log records a redacted successful result.

**Exit criteria:** The Cluster page shows `lab110`, its Kubernetes version, and a successful test result.

## Milestone 3: Snapshot and Data Quality [x]

- [x] Run **Take snapshot** for `lab110`.
- [x] Verify node, namespace, pod/workload, requests, limits, ownership, quota, LimitRange, HPA, warning-event, and Metrics API collection results.
- [x] Verify that a missing optional API produces a visible warning rather than failed or fabricated usage data.

**Exit criteria:** A persisted `lab110` snapshot is available in Overview and History, with all partial-data limitations visible.

## Milestone 4: Capacity Recommendations [ ]

- [ ] Review node allocatable headroom, requested capacity, limits, QoS, quota/LimitRange coverage, pressure signals, HPA coverage, and workload usage where Metrics API data exists.
- [ ] Review the Allocation and Findings pages for actionable CPU and memory allocation recommendations.
- [ ] Verify every finding cites the matching embedded Kubernetes v1.36 official guidance section.

**Exit criteria:** KCP provides evidence-based, source-cited recommendations without modifying `lab110`.

## Milestone 5: Operator Acceptance [ ]

- [ ] Export the `lab110` report as JSON, Markdown, and HTML.
- [ ] Verify that switching active clusters keeps `lab110` reports and findings isolated from other configured clusters.
- [ ] Confirm the connection log retains test/snapshot outcomes and hides sensitive kubeconfig or API error details.

**Exit criteria:** An operator can connect, collect, review, export, and repeat the read-only capacity assessment for `lab110` from KCP.
