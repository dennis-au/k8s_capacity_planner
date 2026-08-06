# Kubernetes Capacity Planner Todo

## Phase 1: Offline Foundation

- [x] Task 1: Bootstrap the application, CLI, README, and test command.
- [x] Task 2: Import and package the curated Kubernetes documentation bundle.
- [x] Task 3: Define normalized snapshot model and resource math.
- [ ] Task 4: Add offline fixture loader and fixture-based analysis.

## Checkpoint A

- [ ] `go test ./...` passes.
- [ ] Imported Kubernetes guidance, manifest, attribution, and local citations work with egress blocked.
- [ ] `kcp analyze --from-file ...` produces a basic report.
- [ ] Capacity, allocatable, requests, limits, and usage are represented separately.

## Phase 2: Live Cluster MVP

- [ ] Task 5: Add live Kubernetes collectors. Implemented; needs a representative-cluster integration run.
- [ ] Task 6: Add optional Metrics API collector. Implemented; needs a Metrics API integration run.
- [x] Task 7: Implement MVP local-document-cited rule engine.
- [x] Task 8: Render JSON, Markdown, and static HTML reports.

## Checkpoint B

- [ ] Live cluster analysis works against kind or minikube.
- [ ] Missing metrics or RBAC gaps produce partial-data warnings.
- [ ] Findings include severity, evidence, recommendation, affected resource, local document reference, and canonical source provenance.

## Phase 3: Safety and Verification

- [x] Task 9: Add read-only RBAC manifests.
- [ ] Task 10: Add integration test harness and golden reports.

## Checkpoint C

- [ ] Read-only operation is documented.
- [ ] Required Kubernetes API permissions are explicit.
- [ ] Unit tests run without a cluster.

## Phase 4: Optional Product Expansion

- [ ] Task 11: Add historical in-site Prometheus-based metrics and right-sizing.
- [x] Task 12: Add dashboard backed by generated JSON reports and local docs.

## Planning Decisions Needed

- [ ] Confirm CLI/static report as MVP, dashboard later.
- [ ] Confirm first target cluster type.
- [ ] Confirm whether metrics-server and Prometheus are expected.
- [ ] Decide whether cloud cost estimation belongs in v1.
- [ ] Confirm the Kubernetes minor versions to package in the first offline documentation bundle.
