# KCP Management Capacity Decision Plan

## Milestone 1: Management Decision Model [x]

- [x] Add request-based 30-day planning-safe capacity trend calculation using retained snapshots.
- [x] Add `Capacity Available`, `Expansion Planning Required`, `Expansion Required`, and `Decision Needs Review` management decisions.
- [x] Keep scheduling-capacity confidence separate from trend confidence and Metrics API observed-usage availability.
- [x] Preserve readiness, schedulability, node pressure, reserve, ResourceQuota, and LimitRange evidence.

**Validation:** Unit tests cover reserve exhaustion, stale data, node pressure, Metrics API absence, two-snapshot preliminary trends, and no-expansion trends.

## Milestone 2: Executive Dashboard and Navigation [x]

- [x] Rebuild the active-cluster Overview as the executive Dashboard: decision, capacity flow, full CPU/memory values, 30-day trend, data quality, top actions, and deployment-fit entry point.
- [x] Change primary navigation to Dashboard, Clusters, and Reports.
- [x] Group Findings, Nodes, Namespaces, Workloads, and Docs under Operations; move Settings and Account out of primary navigation.
- [x] Show each cluster's isolated decision, safe capacity, trend conclusion, report freshness, and connection state on Clusters.

**Validation:** Browser tests confirm desktop and mobile Dashboard users can read the decision, raw figures, data quality, source evidence, and trend without visiting a technical page.

## Milestone 3: Deployment Approval and Expansion Gap [x]

- [x] Present the deployment fit check as a plain-language approval action on Dashboard.
- [x] Show maximum safe replicas and CPU/memory shortfall for capacity failures.
- [x] Separate capacity expansion recommendations from ResourceQuota, LimitRange, stale-data, and node-pressure follow-up actions.
- [x] Keep the resource-only scheduler limitation beside every result.

**Validation:** Test fitting demand, per-Pod and aggregate capacity gaps, namespace quota/LimitRange blocks, omitted namespace, stale reports, and pressured nodes.

## Milestone 4: Operations, Reports, and Official Evidence [x]

- [x] Provide Reports for historical snapshots, trend evidence, and JSON/Markdown/HTML exports.
- [x] Retain technical diagnosis in Operations and preserve existing direct URLs.
- [x] Link decisions, capacity formulas, policy blockers, and actions to embedded Kubernetes v1.36 documentation.
- [x] Label the KCP reserve as local planning policy in Dashboard, exports, and Settings.

**Validation:** Test citations, report exports, active-cluster isolation, offline rendering, and direct legacy route compatibility.

## Milestone 5: Management Acceptance [x]

- [x] Run the authenticated management workflow against the local runtime and representative read-only cluster data.
- [x] Validate Dashboard, Clusters, Reports, Operations, fit check, Metrics API absence, trend confidence, and zero Kubernetes write actions.
- [x] Run the full automated suite and browser-check desktop and mobile layouts.

**Validation:** All `PROMPT.md` done-when conditions pass. Update `STATUS.md` with validation evidence and any live-cluster limitation.
