# KCP Management Capacity Decision Dashboard

## Goal

Kubernetes Capacity Planner (KCP) helps management make a fast, evidence-based capacity decision for the active Kubernetes cluster:

1. Does the cluster require expansion now?
2. Should expansion be planned within the next 30 days?
3. How much CPU and memory remains safe to allocate?
4. Can a proposed deployment be approved?
5. If not, is the blocker cluster capacity, namespace policy, or insufficient data?

KCP is a read-only, dark-site dashboard. It uses Kubernetes Node Allocatable and scheduled Pod requests as the source of truth for scheduling-capacity decisions. `metrics.k8s.io` usage is supporting context only. Every Kubernetes recommendation cites the bundled Kubernetes v1.36 documentation.

## Management Decision Model

- **Total Node Capacity** is the combined node CPU and memory before Kubernetes reservations.
- **Node Allocatable** is the CPU and memory Kubernetes makes available to Pods after node-level reservations and enforcement.
- **Scheduled Requests** is the combined CPU and memory requested by scheduled active Pods.
- **Raw remaining capacity** is Node Allocatable minus Scheduled Requests.
- **Planning-safe capacity** is calculated per eligible node after subtracting the configured KCP planning reserve. The default reserve is 20% of allocatable CPU and memory.
- An eligible node is Ready, schedulable, and free of MemoryPressure, DiskPressure, and PIDPressure.
- The reserve is KCP planning policy. It is configurable and is not a Kubernetes-mandated threshold.

Dashboard decisions use these states:

- **Capacity Available**: the latest request-based data is current, eligible nodes retain planning-safe CPU and memory, and the request trend does not reach the reserve in 30 days.
- **Expansion Planning Required**: the cluster remains safe today, but the 30-day request-based trend reaches the planning reserve.
- **Expansion Required**: raw remaining CPU or memory is exhausted, or no planning-safe CPU or memory remains after the reserve.
- **Decision Needs Review**: the report is stale or incomplete, no node is eligible, or a node reports resource pressure. KCP must not claim an expansion decision is reliable in these conditions.

## Information Architecture

- Primary navigation is **Dashboard**, **Clusters**, and **Reports**.
- Dashboard is the active-cluster executive view. It shows the expansion decision, total-to-safe CPU and memory flow, current safe capacity, 30-day request trend, data quality, top actions, and the deployment fit check.
- Clusters shows an isolated management summary for every configured cluster: decision, safe capacity, trend conclusion, report freshness, and connection state.
- Reports provides historical snapshots and JSON, Markdown, and HTML exports.
- Operations contains Findings, Nodes, Namespaces, Workloads, and bundled local documentation for technical follow-up.
- Settings and Account remain authenticated but move out of the primary navigation.

## Trend and Data Quality

- The 30-day trend uses retained snapshots of scheduled CPU and memory requests and planning-safe capacity. It does not use observed usage as a replacement for Kubernetes scheduling capacity.
- With at least two snapshots, KCP estimates the direction and earliest CPU or memory reserve crossing. Fewer than seven snapshots or fewer than seven days of coverage is labelled **Preliminary trend**.
- With fewer than two usable timestamps, KCP shows **Trend unavailable** rather than fabricating a forecast.
- Metrics API absence displays **Observed usage unavailable**. It does not display zero usage and does not invalidate an otherwise complete request-based capacity decision.
- Stale reports and non-Metrics collection limitations lower scheduling-capacity confidence and produce **Decision Needs Review**.

## Deployment Approval

Dashboard provides **Can this deployment fit?** for replicas, per-Pod CPU request, memory request, and an optional target namespace.

- The result states whether the known request-based capacity can approve the demand, the maximum safe replicas, and any CPU or memory shortfall.
- When a namespace is selected, ResourceQuota and LimitRange blockers are shown separately from cluster-capacity blockers.
- A quota or LimitRange blocker must recommend the relevant policy review, not node expansion.
- Every result states that it is a resource-only planning estimate, not a Kubernetes scheduler guarantee.

## Non-goals

- Do not create, update, scale, restart, evict, or delete Kubernetes resources.
- Do not request or store Kubernetes Secrets, pod environment values, command arguments, or raw kubeconfig contents in SQLite.
- Do not claim a resource-only fit estimate or 30-day projection guarantees scheduler placement.
- Do not evaluate affinity, anti-affinity, taints, tolerations, topology spread, storage, extended resources, init-container placement, or future HPA scale-out in v1.
- Do not make cloud-cost estimates, choose a node instance type, or automatically remediate cluster or namespace policy.
- Do not require public internet access or remote documentation at runtime.
- Do not add multi-user roles or Kubernetes write access.

## Done when

- A management user can open Dashboard and understand the current expansion decision, evidence, current safe CPU and memory, and 30-day trend within three minutes.
- Dashboard shows complete human-readable and raw CPU/memory values from Total Node Capacity through Planning-safe Capacity.
- Dashboard separates scheduling-capacity confidence, trend confidence, and observed-usage availability.
- Dashboard can evaluate a proposed deployment and separates expansion gaps from ResourceQuota or LimitRange policy blockers.
- Clusters shows isolated management summaries without mixing cluster snapshots or reports.
- Reports retains history and exports, while Operations retains technical evidence and local Kubernetes v1.36 documentation.
- Every capacity decision, fit blocker, and recommendation links to the appropriate embedded Kubernetes source and distinguishes KCP policy from Kubernetes guidance.
- The application remains read-only against Kubernetes and fully functional without public network access.
