# KCP Management Capacity Plan

## Milestone 1: Capacity Decision Model [x]

- [x] Capture Ready and schedulable node state in snapshots.
- [x] Add a global planning reserve setting with a default of 20%.
- [x] Calculate raw and planning-safe capacity per eligible node and cluster.
- [x] Derive Ready, Constrained, and Blocked from node eligibility, pressure, raw capacity, and reserve thresholds.

**Validation:** Unit-test reserve math, pressure, unschedulable and unready nodes, state boundaries, and Metrics API absence.

## Milestone 2: Management Overview [x]

- [x] Replace the technical Overview summary with a management capacity decision, raw and planning-safe CPU/memory, freshness, confidence, blockers, and next actions.
- [x] Show the latest management capacity state for each configured cluster on Clusters without mixing report data.

**Validation:** Browser-test that a signed-in user can read the active-cluster decision, evidence source, confidence, and capacity values on one page.

## Milestone 3: New Deployment Fit Check [x]

- [x] Add an Allocation form for replicas and per-Pod CPU/memory requests, with an optional target namespace.
- [x] Calculate request-based fit and maximum safe replicas against per-node planning-safe capacity.
- [x] When a namespace is selected, check collected ResourceQuota and LimitRange constraints.
- [x] State the resource-only estimate limitations beside the result.

**Validation:** Test fitting and non-fitting demand, single-node and multi-node limits, quota and LimitRange blockers, and an omitted namespace.

## Milestone 4: Official Guidance and Data Quality [x]

- [x] Link capacity state, fit blockers, and allocation guidance to embedded Kubernetes v1.36 documentation.
- [x] Label the reserve as KCP planning policy, not a Kubernetes-mandated threshold.
- [x] Show incomplete collection, Metrics API absence, and stale reports without fabricating usage data.

**Validation:** Test source citations, missing-data confidence, version compatibility, and report exports.

## Milestone 5: Lab110 Acceptance [x]

- [x] Collect a fresh read-only snapshot from `lab110`.
- [x] Verify request-based capacity and low usage-confidence behavior with Metrics API unavailable.
- [x] Verify Overview, fit check, exports, active-cluster isolation, and no Kubernetes write operations.

**Validation:** Run the automated suite and complete the authenticated browser workflow against the local KCP runtime.
