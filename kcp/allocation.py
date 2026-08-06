from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from kcp.docs import DocumentRegistry
from kcp.models import ResourceValues


@dataclass(frozen=True)
class AllocationNode:
    name: str
    allocatable: ResourceValues
    requested: ResourceValues
    remaining: ResourceValues
    has_pressure: bool


@dataclass(frozen=True)
class AllocationRecommendation:
    identity: str
    current_request: ResourceValues
    observed_peak: ResourceValues | None
    suggested_request: ResourceValues | None
    sample_count: int
    status: str
    severity: str
    recommendation: str
    source: dict[str, str]


@dataclass(frozen=True)
class AllocationPlan:
    total_allocatable: ResourceValues
    total_requested: ResourceValues
    total_remaining: ResourceValues
    nodes: list[AllocationNode]
    recommendations: list[AllocationRecommendation]
    metric_snapshot_count: int
    capacity_source: dict[str, str]


def build_allocation_plan(
    snapshot: dict[str, Any], historical_snapshots: Iterable[dict[str, Any]], docs: DocumentRegistry
) -> AllocationPlan:
    nodes = [_allocation_node(node) for node in snapshot.get("nodes", [])]
    total_allocatable = _sum_resources(node.allocatable for node in nodes)
    total_requested = _sum_resources(node.requested for node in nodes)
    total_remaining = _sum_resources(node.remaining for node in nodes)
    observed_peaks, metric_snapshot_count = _observed_peaks(historical_snapshots)

    recommendations = [
        _recommend_workload(workload, observed_peaks.get(_workload_identity(workload)), docs)
        for workload in snapshot.get("workloads", [])
    ]
    return AllocationPlan(
        total_allocatable=total_allocatable,
        total_requested=total_requested,
        total_remaining=total_remaining,
        nodes=sorted(nodes, key=lambda node: node.name),
        recommendations=sorted(recommendations, key=lambda recommendation: recommendation.identity),
        metric_snapshot_count=metric_snapshot_count,
        capacity_source=docs.source_for_rule("node-headroom"),
    )


def _allocation_node(node: dict[str, Any]) -> AllocationNode:
    allocatable = _resources(node.get("allocatable"))
    requested = _resources(node.get("requested"))
    remaining = ResourceValues(
        cpu_millicores=max(0, allocatable.cpu_millicores - requested.cpu_millicores),
        memory_bytes=max(0, allocatable.memory_bytes - requested.memory_bytes),
        ephemeral_storage_bytes=max(0, allocatable.ephemeral_storage_bytes - requested.ephemeral_storage_bytes),
    )
    return AllocationNode(
        name=str(node.get("name", "Unknown node")),
        allocatable=allocatable,
        requested=requested,
        remaining=remaining,
        has_pressure=bool(node.get("conditions")),
    )


def _recommend_workload(
    workload: dict[str, Any], observed: tuple[ResourceValues, int] | None, docs: DocumentRegistry
) -> AllocationRecommendation:
    identity = _workload_identity(workload)
    current_request = _resources(workload.get("requests"))
    if observed is None or _is_zero(observed[0]):
        return AllocationRecommendation(
            identity=identity,
            current_request=current_request,
            observed_peak=None,
            suggested_request=None,
            sample_count=0 if observed is None else observed[1],
            status="collect-metrics",
            severity="info",
            recommendation=(
                "No usable resource observation is retained for this workload. Collect Metrics API snapshots "
                "before choosing a numeric request."
            ),
            source=docs.source_for_rule("metrics-availability"),
        )

    observed_peak, sample_count = observed
    suggested_request = current_request.maximum(observed_peak)
    missing_requests = bool(workload.get("missing_requests"))
    if missing_requests:
        status = "set-requests"
        severity = "warning"
        recommendation = (
            "Set CPU and memory requests. The suggested floor is the highest retained total workload usage; "
            "validate demand and distribute it appropriately across replicas before applying."
        )
    elif _request_below_observed(current_request, observed_peak):
        status = "increase-request"
        severity = "warning"
        recommendation = (
            "Current requests are below retained observed usage. Raise the affected request to at least the "
            "shown floor before planning additional workload demand."
        )
    else:
        status = "covered"
        severity = "info"
        recommendation = (
            "Current requests cover the retained observations. This planner does not recommend reducing "
            "requests from retained Metrics API snapshots alone."
        )

    return AllocationRecommendation(
        identity=identity,
        current_request=current_request,
        observed_peak=observed_peak,
        suggested_request=suggested_request if status != "covered" else None,
        sample_count=sample_count,
        status=status,
        severity=severity,
        recommendation=recommendation,
        source=docs.source_for_rule("missing-requests"),
    )


def _observed_peaks(
    snapshots: Iterable[dict[str, Any]],
) -> tuple[dict[str, tuple[ResourceValues, int]], int]:
    observations: dict[str, tuple[ResourceValues, int]] = {}
    metric_snapshot_count = 0
    for snapshot in snapshots:
        if not snapshot.get("metrics_available"):
            continue
        metric_snapshot_count += 1
        for workload in snapshot.get("workloads", []):
            usage = workload.get("usage")
            if not isinstance(usage, dict):
                continue
            identity = _workload_identity(workload)
            observed = _resources(usage)
            previous = observations.get(identity)
            observations[identity] = (
                observed if previous is None else previous[0].maximum(observed),
                1 if previous is None else previous[1] + 1,
            )
    return observations, metric_snapshot_count


def _resources(value: Any) -> ResourceValues:
    value = value if isinstance(value, dict) else {}
    return ResourceValues(
        cpu_millicores=int(value.get("cpu_millicores", 0) or 0),
        memory_bytes=int(value.get("memory_bytes", 0) or 0),
        ephemeral_storage_bytes=int(value.get("ephemeral_storage_bytes", 0) or 0),
    )


def _sum_resources(values: Iterable[ResourceValues]) -> ResourceValues:
    total = ResourceValues()
    for value in values:
        total = total.add(value)
    return total


def _workload_identity(workload: dict[str, Any]) -> str:
    return "/".join(
        [
            str(workload.get("namespace", "default")),
            str(workload.get("kind", "Pod")),
            str(workload.get("name", "unknown")),
        ]
    )


def _request_below_observed(request: ResourceValues, observed: ResourceValues) -> bool:
    return request.cpu_millicores < observed.cpu_millicores or request.memory_bytes < observed.memory_bytes


def _is_zero(resources: ResourceValues) -> bool:
    return resources.cpu_millicores == 0 and resources.memory_bytes == 0
