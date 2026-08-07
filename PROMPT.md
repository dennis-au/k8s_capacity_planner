# KCP Management Capacity Planning

## Goal

Kubernetes Capacity Planner (KCP) helps management review the active Kubernetes cluster and answer two operational questions quickly:

1. What CPU and memory capacity is currently in use and safely available?
2. Can a proposed deployment fit without exceeding the cluster's known scheduling or namespace policy constraints?

KCP remains a read-only, dark-site dashboard. It reports request-based scheduling capacity from Node Allocatable and scheduled Pod requests. When `metrics.k8s.io` is available, KCP also presents observed resource usage and request recommendations. Every Kubernetes guidance recommendation cites the bundled Kubernetes v1.36 documentation.

## Capacity Definitions

- **Raw remaining capacity** is Node Allocatable minus scheduled Pod CPU and memory requests.
- **Planning-safe capacity** is calculated per eligible node after reserving the configured planning reserve. The default reserve is 20% of each node's allocatable CPU and memory.
- An eligible node is Ready, schedulable, and free of MemoryPressure, DiskPressure, and PIDPressure.
- **Ready** means the cluster has positive planning-safe CPU and memory capacity, at least one eligible node, and no active node pressure.
- **Constrained** means the cluster is not blocked but one resource is at or beyond the planning reserve, or a selected namespace policy needs review.
- **Blocked** means active node pressure, no eligible node, exhausted raw request-based CPU or memory capacity, or a proposed deployment cannot fit a known node, ResourceQuota, or LimitRange constraint.
- Missing Metrics API data lowers the confidence of observed-usage guidance. It does not turn usage into zero or prevent request-based capacity planning.

## Non-goals

- Do not create, update, scale, restart, evict, or delete Kubernetes resources.
- Do not request or store Kubernetes Secrets, pod environment values, command arguments, or raw kubeconfig contents in SQLite.
- Do not claim that a resource-only fit estimate guarantees Kubernetes scheduler placement.
- Do not evaluate affinity, anti-affinity, taints, tolerations, topology spread, storage, extended resources, init-container behavior, or future HPA scale-out in v1.
- Do not make cloud cost estimates, historical Prometheus right-sizing claims, or automatic remediation recommendations.
- Do not require internet access at runtime. Kubernetes documentation and rule sources remain embedded in the application.
- Do not add multi-user roles or Kubernetes write access.

## Done when

- The active-cluster Overview states whether the cluster is Ready, Constrained, or Blocked for additional request-based workload demand.
- Overview shows raw remaining and planning-safe CPU and memory capacity, the configured reserve, latest snapshot time, data confidence, and the main blockers or next actions.
- Allocation provides a new-deployment fit check for replicas and per-Pod CPU and memory requests, with an optional target namespace.
- Fit results show whether the known resource constraints can accommodate the request, the maximum request-based safe replicas, and any node, quota, or LimitRange blocker.
- Metrics API absence remains explicit and only affects usage confidence; requests-based capacity and fit results remain available.
- Capacity findings, fit blockers, and allocation recommendations explain their evidence and link to the appropriate embedded Kubernetes v1.36 document section.
- Clusters page shows the latest management capacity state for each configured cluster without mixing its reports or snapshots with another cluster.
- The application remains read-only against Kubernetes and works without public network access.
