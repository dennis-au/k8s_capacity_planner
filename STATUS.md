+# Lab110 KCP Validation Status

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

## Milestone 5: Complete

- `lab110` report exports succeed as JSON, Markdown, and HTML; each reflects Kubernetes `v1.36.3+k3s1` and the active `lab110` cluster.
- Dashboard coverage includes a passing active-cluster isolation test for History, Findings, and latest export routing.
- The cluster edit page shows the successful real connection test and snapshots while excluding kubeconfig tokens, CA data, raw stack traces, and raw API exception details.
- Two real `lab110` snapshots have been collected, demonstrating repeatable read-only collection.

## Assessment Complete

All `PROMPT.md` done-when conditions are satisfied for the `lab110` validation. The current KCP runtime at `http://127.0.0.1:5056` uses the real Kubernetes collector; the earlier fake demo runtime has been stopped.

## Management Capacity Plan

### Milestone 1: Complete

- Snapshots now record each node's Ready and schedulable state in addition to pressure conditions.
- Runtime Settings stores a global planning reserve with a 20% default and a validated 0-50% range.
- Allocation calculates raw and planning-safe per-node and cluster capacity after excluding unready, unschedulable, and pressured nodes.
- KCP now derives Ready, Constrained, and Blocked capacity states. Missing Metrics API data remains request-based planning with reduced confidence, not zero usage.
- Validation: `.venv/bin/python -m unittest tests.test_allocation tests.test_store tests.test_config_service tests.test_web tests.test_kubernetes` passed (46 tests).

### Milestone 2: Complete

- The active-cluster Overview now leads with a Ready, Constrained, or Blocked capacity decision; it shows raw and planning-safe CPU/memory, eligible nodes, collection time, and Metrics API confidence.
- Observed CPU and memory are shown only when the Metrics API supplied node usage. Otherwise Overview explicitly retains request-based planning with reduced confidence.
- Clusters now calculates and displays each connection's latest isolated management capacity state.
- Validation: authenticated dashboard test and `.venv/bin/python -m unittest discover -s tests -v` passed (58 tests).

### Milestone 3: Complete

- Allocation includes a read-only new-deployment fit check for replicas, per-Pod CPU/memory requests, and an optional target namespace.
- The fit result uses per-node planning-safe capacity, reports maximum safe replicas, and blocks known ResourceQuota and Pod LimitRange violations.
- Container-level LimitRange policies are reported as manifest-review constraints rather than incorrectly treated as aggregate Pod limits.
- The page identifies the resource-only estimate limitations and links each blocker to embedded Kubernetes guidance.
- Validation: deployment-fit, Kubernetes normalization, browser-route, and full-suite tests passed (58 tests).

### Milestone 4: Complete

- Capacity decisions cite the embedded Node Allocatable guidance; deployment-fit blockers retain their ResourceQuota or LimitRange citation; workload findings keep their existing local Kubernetes v1.36 citations.
- The reserve is visible as KCP planning policy in Settings, Overview, Allocation, and exports.
- Reports older than two configured collection intervals are marked stale. Snapshot warnings, including missing Metrics API data, appear as data-quality limitations rather than fabricated usage.
- JSON and Markdown exports include management capacity state, raw and planning-safe CPU/memory, reserve, report quality, and local source provenance.
- Validation: `.venv/bin/python -m unittest discover -s tests -v` passed (61 tests after final review regressions).

### Milestone 5: Complete

- A fresh read-only `lab110` Snapshot 3 was collected on August 6, 2026. The cluster reported Kubernetes `v1.36.3+k3s1`.
- Overview reports `Ready` with 1 eligible node, raw remaining capacity of `1800m` CPU and `7.4Gi` memory, and planning-safe capacity of `1400m` CPU and `5.9Gi` memory using the 20% reserve.
- `metrics.k8s.io` remains unavailable (`ServiceException`). KCP explicitly shows request-based confidence and collection limitations; it does not fabricate observed usage.
- The authenticated browser fit check evaluated 2 replicas at `250m` CPU and `256Mi` memory per Pod as `Fits`, with 5 maximum safe replicas under the current resource-only estimate.
- Actual lab110 Markdown and JSON export routes returned `200` and included planning-safe capacity, the local Node Allocatable source, and management capacity metadata. Automated tests cover active-cluster isolation and zero Kubernetes write actions.
- The updated dashboard now runs at `http://127.0.0.1:5056` using the existing local SQLite database and session secret.
- Final validation: `.venv/bin/python -m unittest discover -s tests -v` passed (61 tests); the live Overview rendered without browser console errors.


## Management Capacity Decision Dashboard Redesign

Last updated: August 7, 2026

### Milestone 1: Complete

- Implemented the management decision model: request-based planning-safe capacity trend, the four management states, and separate scheduling, trend, and observed-usage confidence.
- Validation: `.venv/bin/python -m unittest tests.test_allocation -v` passed (8 tests).

### Milestone 2: Complete

- Dashboard now leads with an executive capacity decision, full Total Node Capacity-to-Planning-safe Capacity flow, raw CPU/memory values, 30-day trend evidence, data quality, local Kubernetes source, and next action.
- Primary navigation is Dashboard, Clusters, and Reports. Operations retains technical evidence; Manage holds Settings and Account. Clusters presents isolated management summaries.
- Validation: `.venv/bin/python -m unittest tests.test_allocation tests.test_web -v` passed (32 tests). An isolated desktop and 390px mobile browser check confirmed readable decision evidence, raw figures, data-quality limitations, no horizontal overflow, and no console errors.

### Milestone 3: Complete

- Dashboard now provides the CSRF-protected `Can this deployment be approved?` flow; Operations Capacity planning retains technical evidence without a duplicate approval form.
- The result shows total requested CPU/memory, maximum safe replicas, capacity shortfall, and single-Pod shortfall where applicable. ResourceQuota and LimitRange blockers are clearly separated from capacity gaps; stale data and node pressure require review instead of an expansion recommendation.
- Validation: `.venv/bin/python -m unittest tests.test_allocation tests.test_web -v` passed (35 tests). An isolated browser submission rendered a stale-data `Review required` result with total requests, follow-up action, local Kubernetes citation, and no console errors.

### Milestone 4: Complete

- Reports, legacy `/history`, and JSON/Markdown/HTML exports now carry the same management decision as Dashboard plus the full Total Node Capacity-to-Planning-safe Capacity evidence and raw values.
- Exports preserve the existing technical status while adding the management decision, local Node Allocatable citation, and an explicit `KCP planning policy` reserve label. Operations direct routes and bundled local documentation remain available offline.
- Validation: `.venv/bin/python -m unittest tests.test_web tests.test_docs -v` passed (29 tests), including active-cluster isolation, legacy history, local-doc rendering, export provenance, and no remote documentation links.

### Milestone 5: Complete

- Authenticated acceptance used an isolated local runtime with representative read-only cluster data. Dashboard displayed the stale-snapshot decision, Metrics API absence, preliminary trend, local evidence, and deployment approval flow; Clusters, Reports, and Operations displayed their expected isolated views.
- Desktop and 390px mobile browser checks found no horizontal overflow and no browser console errors. The deployment approval form remains on Dashboard while Operations Capacity planning remains technical evidence only.
- Validation: `.venv/bin/python -m unittest discover -s tests -v` passed (77 tests). The collector test now asserts all collection client calls are `list_*`; source inspection found no Kubernetes write methods in `kcp/kubernetes.py`.
- Live-cluster limitation: this management-redesign acceptance did not access `lab110` or any production Kubernetes endpoint.
