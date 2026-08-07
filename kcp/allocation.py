from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from kcp.docs import DocumentRegistry
from kcp.models import ResourceValues


@dataclass(frozen=True)
class AllocationNode:
    name: str
    capacity: ResourceValues | None
    allocatable: ResourceValues
    requested: ResourceValues
    usage: ResourceValues
    remaining: ResourceValues
    planning_safe: ResourceValues
    ready: bool
    schedulable: bool
    conditions: list[str]
    has_pressure: bool
    eligible: bool


@dataclass(frozen=True)
class CapacityStatus:
    state: str
    confidence: str
    summary: str
    blockers: list[str]


@dataclass(frozen=True)
class DeploymentDemand:
    replicas: int
    requests: ResourceValues
    namespace: str | None = None


@dataclass(frozen=True)
class FitIssue:
    message: str
    source: dict[str, str]
    blocking: bool


@dataclass(frozen=True)
class DeploymentFit:
    status: str
    fits: bool
    summary: str
    maximum_safe_replicas: int
    issues: list[FitIssue]


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
    total_node_capacity: ResourceValues | None
    total_not_allocatable: ResourceValues | None
    total_allocatable: ResourceValues
    total_requested: ResourceValues
    total_observed_usage: ResourceValues
    total_remaining: ResourceValues
    total_planning_safe: ResourceValues
    nodes: list[AllocationNode]
    recommendations: list[AllocationRecommendation]
    metric_snapshot_count: int
    planning_reserve_percent: int
    eligible_node_count: int
    capacity_status: CapacityStatus
    capacity_source: dict[str, str]


def build_allocation_plan(
    snapshot: dict[str, Any],
    historical_snapshots: Iterable[dict[str, Any]],
    docs: DocumentRegistry,
    planning_reserve_percent: int = 20,
) -> AllocationPlan:
    if not 0 <= planning_reserve_percent <= 50:
        raise ValueError("planning reserve must be between 0 and 50 percent")
    nodes = [_allocation_node(node, planning_reserve_percent) for node in snapshot.get("nodes", [])]
    total_allocatable = _sum_resources(node.allocatable for node in nodes)
    total_node_capacity = _total_node_capacity(nodes)
    total_not_allocatable = (
        ResourceValues(
            cpu_millicores=max(0, total_node_capacity.cpu_millicores - total_allocatable.cpu_millicores),
            memory_bytes=max(0, total_node_capacity.memory_bytes - total_allocatable.memory_bytes),
            ephemeral_storage_bytes=max(
                0,
                total_node_capacity.ephemeral_storage_bytes - total_allocatable.ephemeral_storage_bytes,
            ),
        )
        if total_node_capacity is not None
        else None
    )
    total_requested = _sum_resources(node.requested for node in nodes)
    total_observed_usage = _sum_resources(node.usage for node in nodes)
    total_remaining = _sum_resources(node.remaining for node in nodes)
    total_planning_safe = _sum_resources(node.planning_safe for node in nodes if node.eligible)
    observed_peaks, metric_snapshot_count = _observed_peaks(historical_snapshots)

    recommendations = [
        _recommend_workload(workload, observed_peaks.get(_workload_identity(workload)), docs)
        for workload in snapshot.get("workloads", [])
    ]
    return AllocationPlan(
        total_node_capacity=total_node_capacity,
        total_not_allocatable=total_not_allocatable,
        total_allocatable=total_allocatable,
        total_requested=total_requested,
        total_observed_usage=total_observed_usage,
        total_remaining=total_remaining,
        total_planning_safe=total_planning_safe,
        nodes=sorted(nodes, key=lambda node: node.name),
        recommendations=sorted(recommendations, key=lambda recommendation: recommendation.identity),
        metric_snapshot_count=metric_snapshot_count,
        planning_reserve_percent=planning_reserve_percent,
        eligible_node_count=sum(1 for node in nodes if node.eligible),
        capacity_status=_capacity_status(nodes, total_remaining, total_planning_safe, snapshot.get("metrics_available") is True),
        capacity_source=docs.source_for_rule("node-headroom"),
    )


def _allocation_node(node: dict[str, Any], planning_reserve_percent: int) -> AllocationNode:
    capacity_data = node.get("capacity")
    capacity = _resources(capacity_data) if isinstance(capacity_data, dict) else None
    allocatable = _resources(node.get("allocatable"))
    requested = _resources(node.get("requested"))
    usage = _resources(node.get("usage"))
    remaining = ResourceValues(
        cpu_millicores=max(0, allocatable.cpu_millicores - requested.cpu_millicores),
        memory_bytes=max(0, allocatable.memory_bytes - requested.memory_bytes),
        ephemeral_storage_bytes=max(0, allocatable.ephemeral_storage_bytes - requested.ephemeral_storage_bytes),
    )
    ready = node.get("ready", True) is True
    schedulable = node.get("schedulable", True) is True
    has_pressure = bool(node.get("conditions"))
    eligible = ready and schedulable and not has_pressure
    planning_safe = _planning_safe(remaining, allocatable, planning_reserve_percent) if eligible else ResourceValues()
    return AllocationNode(
        name=str(node.get("name", "Unknown node")),
        capacity=capacity,
        allocatable=allocatable,
        requested=requested,
        usage=usage,
        remaining=remaining,
        planning_safe=planning_safe,
        ready=ready,
        schedulable=schedulable,
        conditions=[str(condition) for condition in node.get("conditions", [])],
        has_pressure=has_pressure,
        eligible=eligible,
    )


def _total_node_capacity(nodes: list[AllocationNode]) -> ResourceValues | None:
    if not nodes or any(node.capacity is None for node in nodes):
        return None
    return _sum_resources(node.capacity for node in nodes if node.capacity is not None)


def _planning_safe(remaining: ResourceValues, allocatable: ResourceValues, planning_reserve_percent: int) -> ResourceValues:
    reserve = lambda value: math.ceil(value * planning_reserve_percent / 100)
    return ResourceValues(
        cpu_millicores=max(0, remaining.cpu_millicores - reserve(allocatable.cpu_millicores)),
        memory_bytes=max(0, remaining.memory_bytes - reserve(allocatable.memory_bytes)),
        ephemeral_storage_bytes=max(
            0, remaining.ephemeral_storage_bytes - reserve(allocatable.ephemeral_storage_bytes)
        ),
    )


def _capacity_status(
    nodes: list[AllocationNode],
    total_remaining: ResourceValues,
    total_planning_safe: ResourceValues,
    metrics_available: bool,
) -> CapacityStatus:
    confidence = "Usage available" if metrics_available else "Request-based"
    pressured = [node for node in nodes if node.has_pressure]
    if pressured:
        details = ", ".join(node.name for node in pressured)
        conditions = ", ".join(
            sorted({condition for node in pressured for condition in _node_conditions(node)})
        )
        return CapacityStatus(
            "Blocked",
            confidence,
            f"Blocked: {details} reports {conditions}.",
            [f"Active node pressure on {details}."],
        )
    eligible = [node for node in nodes if node.eligible]
    if not eligible:
        return CapacityStatus(
            "Blocked",
            confidence,
            "Blocked: No Ready and schedulable nodes are available.",
            ["No eligible nodes are available for additional Pods."],
        )
    exhausted = [
        resource
        for resource, value in {
            "CPU": total_remaining.cpu_millicores,
            "memory": total_remaining.memory_bytes,
        }.items()
        if value <= 0
    ]
    if exhausted:
        label = " and ".join(exhausted)
        return CapacityStatus(
            "Blocked",
            confidence,
            f"Blocked: raw remaining {label} capacity is exhausted.",
            [f"Raw remaining {label} capacity is exhausted."],
        )
    constrained = [
        resource
        for resource, value in {
            "CPU": total_planning_safe.cpu_millicores,
            "memory": total_planning_safe.memory_bytes,
        }.items()
        if value <= 0
    ]
    if constrained:
        label = " and ".join(constrained)
        return CapacityStatus(
            "Constrained",
            confidence,
            f"Constrained: planning reserve is reached for {label}.",
            [f"No planning-safe {label} capacity remains after the reserve."],
        )
    return CapacityStatus(
        "Ready",
        confidence,
        "Ready: eligible nodes retain planning-safe CPU and memory capacity.",
        [],
    )


def _node_conditions(node: AllocationNode) -> list[str]:
    return node.conditions


def evaluate_deployment_fit(
    snapshot: dict[str, Any], plan: AllocationPlan, demand: DeploymentDemand, docs: DocumentRegistry
) -> DeploymentFit:
    if demand.replicas < 1:
        raise ValueError("replicas must be at least 1")
    if demand.requests.cpu_millicores <= 0 or demand.requests.memory_bytes <= 0:
        raise ValueError("CPU and memory requests must be greater than zero")

    maximum_safe_replicas = sum(_replicas_that_fit(node.planning_safe, demand.requests) for node in plan.nodes if node.eligible)
    issues: list[FitIssue] = []
    if maximum_safe_replicas < demand.replicas:
        issues.append(
            FitIssue(
                f"Known eligible nodes can accommodate only {maximum_safe_replicas} planning-safe replicas.",
                docs.source_for_rule("node-headroom"),
                True,
            )
        )

    if demand.namespace:
        namespace = next(
            (item for item in snapshot.get("namespaces", []) if item.get("name") == demand.namespace), None
        )
        if namespace is None:
            raise ValueError("selected namespace is not present in the current snapshot")
        issues.extend(_namespace_fit_issues(namespace, demand, docs))

    blockers = [issue for issue in issues if issue.blocking]
    if blockers:
        return DeploymentFit(
            "Blocked",
            False,
            f"Blocked: {blockers[0].message}",
            maximum_safe_replicas,
            issues,
        )
    if issues:
        return DeploymentFit(
            "Constrained",
            True,
            f"Constrained: {issues[0].message}",
            maximum_safe_replicas,
            issues,
        )
    return DeploymentFit(
        "Fits",
        True,
        f"Fits: {demand.replicas} replicas fit within known planning-safe CPU and memory capacity.",
        maximum_safe_replicas,
        [],
    )


def _replicas_that_fit(capacity: ResourceValues, request: ResourceValues) -> int:
    cpu_replicas = capacity.cpu_millicores // request.cpu_millicores
    memory_replicas = capacity.memory_bytes // request.memory_bytes
    return min(cpu_replicas, memory_replicas)


def _namespace_fit_issues(
    namespace: dict[str, Any], demand: DeploymentDemand, docs: DocumentRegistry
) -> list[FitIssue]:
    issues: list[FitIssue] = []
    total = _multiply(demand.requests, demand.replicas)
    quotas = namespace.get("quotas") if isinstance(namespace.get("quotas"), dict) else {}
    for quota_name, proposed in {"requests.cpu": total.cpu_millicores, "requests.memory": total.memory_bytes}.items():
        quota = quotas.get(quota_name)
        if not isinstance(quota, dict):
            continue
        used = int(quota.get("used", 0) or 0)
        hard = int(quota.get("hard", 0) or 0)
        if used + proposed > hard:
            issues.append(
                FitIssue(
                    f"ResourceQuota {quota_name} would exceed its hard limit in namespace {demand.namespace}.",
                    docs.source_for_rule("quota-pressure"),
                    True,
                )
            )
    policies = namespace.get("limit_ranges") if isinstance(namespace.get("limit_ranges"), list) else []
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        policy_type = str(policy.get("type", ""))
        minimum = _resources(policy.get("minimum", policy.get("min")))
        maximum = _resources(policy.get("maximum", policy.get("max")))
        default_request = _resources(policy.get("default_request"))
        if policy_type == "Pod":
            for label, requested, lower, upper in (
                ("CPU", demand.requests.cpu_millicores, minimum.cpu_millicores, maximum.cpu_millicores),
                ("memory", demand.requests.memory_bytes, minimum.memory_bytes, maximum.memory_bytes),
            ):
                if lower > 0 and requested < lower:
                    issues.append(
                        FitIssue(
                            f"Pod LimitRange minimum {label} request is not met in namespace {demand.namespace}.",
                            docs.source_for_rule("limit-range-coverage"),
                            True,
                        )
                    )
                if upper > 0 and requested > upper:
                    issues.append(
                        FitIssue(
                            f"Pod LimitRange maximum {label} request would be exceeded in namespace {demand.namespace}.",
                            docs.source_for_rule("limit-range-coverage"),
                            True,
                        )
                    )
        elif policy_type == "Container" and not _is_zero(minimum.add(maximum).add(default_request)):
            issues.append(
                FitIssue(
                    f"Container LimitRange policy in namespace {demand.namespace} requires manifest review.",
                    docs.source_for_rule("limit-range-coverage"),
                    False,
                )
            )
    return issues


def _multiply(resources: ResourceValues, multiplier: int) -> ResourceValues:
    return ResourceValues(
        cpu_millicores=resources.cpu_millicores * multiplier,
        memory_bytes=resources.memory_bytes * multiplier,
        ephemeral_storage_bytes=resources.ephemeral_storage_bytes * multiplier,
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
