# KCP Lab110 Capacity Review

## Goal

Use Kubernetes Capacity Planner (KCP) to connect to the single-node Kubernetes cluster on `lab110` (`192.168.0.110`) through a dedicated read-only kubeconfig. KCP must collect a point-in-time view of cluster capacity and workload resource allocation, then present practical CPU and memory recommendations grounded in the bundled Kubernetes v1.36 official documentation.

The review must cover node allocatable capacity, workload requests and limits, observed usage when `metrics.k8s.io` is available, Pod QoS, node pressure, ResourceQuota, LimitRange, HPA coverage, and warning events. Every recommendation must show its evidence and link to the relevant embedded Kubernetes guidance.

## Non-goals

- Do not create, update, scale, restart, evict, or delete Kubernetes resources.
- Do not request or store Kubernetes Secrets, pod environment values, command arguments, or raw kubeconfig contents in SQLite.
- Do not make cost estimates, cloud-sizing recommendations, or historical Prometheus right-sizing claims.
- Do not require internet access at KCP runtime; the Kubernetes documentation and rule sources remain embedded in the application.
- Do not treat unavailable Metrics API data as zero usage. Show a partial-data warning instead.
- Do not add multi-user roles, write access, or automatic remediation.

## Done when

- KCP has a saved `lab110` connection using the `kcp-reader` read-only kubeconfig and can pass **Test connection**.
- A **Take snapshot** action completes and creates a report for `lab110` without changing any Kubernetes resource.
- The overview, nodes, namespaces, workloads, findings, allocation, history, and exports display data scoped only to `lab110` when it is the active cluster.
- The report clearly identifies Metrics API availability and any permission or collection gaps.
- Findings and allocation recommendations identify the affected resource, explain the observed request/limit/headroom evidence, and cite the embedded Kubernetes v1.36 document section used as guidance.
- KCP displays the source/baseline provenance and warns when a connected cluster is outside the embedded Kubernetes v1.36 compatibility baseline.
- The cluster operation log records the connection test and snapshot result without exposing credentials or raw API errors.
