from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
class CapacityTrendPoint:
    collected_at: datetime
    planning_safe: ResourceValues


@dataclass(frozen=True)
class CapacityTrend:
    state: str
    confidence: str
    summary: str
    sample_count: int
    span_days: int
    days_to_reserve: int | None
    limiting_resource: str | None
    points: list[CapacityTrendPoint]


@dataclass(frozen=True)
class ManagementDecision:
    state: str
    summary: str
    reasons: list[str]
    scheduling_confidence: str
    observed_usage: str


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
    category: str


@dataclass(frozen=True)
class DeploymentFit:
    status: str
    approved: bool
    fits: bool
    summary: str
    maximum_safe_replicas: int
    total_request: ResourceValues
    capacity_shortfall: ResourceValues
    per_pod_shortfall: ResourceValues
    capacity_blocked: bool
    policy_blocked: bool
    review_required: bool
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
    trend: CapacityTrend
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
    history = list(historical_snapshots)
    observed_peaks, metric_snapshot_count = _observed_peaks(history)

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
        trend=_capacity_trend([snapshot, *history], planning_reserve_percent),
        capacity_source=docs.source_for_rule("node-headroom"),
    )


def build_management_decision(
    plan: AllocationPlan,
    report_state: str,
    report_warnings: Iterable[str],
) -> ManagementDecision:
    warnings = [str(warning) for warning in report_warnings]
    non_metrics_warnings = [warning for warning in warnings if not warning.lower().startswith("metrics api unavailable")]
    observed_usage = "Usage available" if plan.capacity_status.confidence == "Usage available" else "Observed usage unavailable"
    if report_state != "Current":
        return ManagementDecision(
            "Decision Needs Review",
            "Decision needs review: the latest capacity snapshot is stale.",
            ["Take a fresh snapshot before approving a deployment or planning expansion."],
            "Stale request-based data",
            observed_usage,
        )
    if non_metrics_warnings:
        return ManagementDecision(
            "Decision Needs Review",
            "Decision needs review: collection completed with capacity data limitations.",
            non_metrics_warnings,
            "Partial request-based data",
            observed_usage,
        )

    pressured = [node for node in plan.nodes if node.has_pressure]
    if pressured:
        node_details = ", ".join(node.name for node in pressured)
        conditions = ", ".join(sorted({condition for node in pressured for condition in node.conditions}))
        return ManagementDecision(
            "Decision Needs Review",
            f"Decision needs review: {node_details} reports {conditions}.",
            [f"Resolve active node pressure before making an expansion decision."],
            "Current request-based data",
            observed_usage,
        )
    if not plan.eligible_node_count:
        return ManagementDecision(
            "Decision Needs Review",
            "Decision needs review: no Ready, schedulable nodes are eligible for additional Pods.",
            ["Restore an eligible node before approving new workload demand."],
            "Current request-based data",
            observed_usage,
        )

    raw_exhausted = _exhausted_resources(plan.total_remaining)
    if raw_exhausted:
        label = _resource_label(raw_exhausted)
        return ManagementDecision(
            "Expansion Required",
            f"Expansion required: raw remaining {label} capacity is exhausted.",
            [f"Add eligible schedulable {label} capacity before approving more workload demand."],
            "Current request-based data",
            observed_usage,
        )
    safe_exhausted = _exhausted_resources(plan.total_planning_safe)
    if safe_exhausted:
        label = _resource_label(safe_exhausted)
        return ManagementDecision(
            "Expansion Required",
            f"Expansion required: no planning-safe {label} capacity remains after the reserve.",
            [f"Add eligible schedulable {label} capacity or reduce planned demand before approval."],
            "Current request-based data",
            observed_usage,
        )
    if plan.trend.state == "Expansion Planning Required":
        return ManagementDecision(
            "Expansion Planning Required",
            plan.trend.summary,
            ["Plan additional eligible node capacity before the request trend reaches the planning reserve."],
            "Current request-based data",
            observed_usage,
        )
    return ManagementDecision(
        "Capacity Available",
        "Capacity available: eligible nodes retain planning-safe CPU and memory capacity.",
        ["Continue to review the request trend before approving sustained growth."],
        "Current request-based data",
        observed_usage,
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


def _capacity_trend(
    snapshots: Iterable[dict[str, Any]], planning_reserve_percent: int
) -> CapacityTrend:
    points = _trend_points(snapshots, planning_reserve_percent)
    if len(points) < 2:
        return CapacityTrend(
            "Trend unavailable",
            "Trend unavailable",
            "Trend unavailable: collect at least two snapshots to estimate capacity direction.",
            len(points),
            0,
            None,
            None,
            points,
        )
    span_seconds = (points[-1].collected_at - points[0].collected_at).total_seconds()
    if span_seconds <= 0:
        return CapacityTrend(
            "Trend unavailable",
            "Trend unavailable",
            "Trend unavailable: snapshots do not contain distinct collection times.",
            len(points),
            0,
            None,
            None,
            points,
        )
    span_days = max(1, math.ceil(span_seconds / timedelta(days=1).total_seconds()))
    confidence = "Established trend" if len(points) >= 7 and span_days >= 7 else "Preliminary trend"
    latest = points[-1].planning_safe
    earliest = points[0].planning_safe
    days_to_reserve = {
        "CPU": _days_to_zero(latest.cpu_millicores, earliest.cpu_millicores, span_seconds),
        "memory": _days_to_zero(latest.memory_bytes, earliest.memory_bytes, span_seconds),
    }
    forecast = [(resource, days) for resource, days in days_to_reserve.items() if days is not None]
    if forecast:
        limiting_resource, earliest_days = min(forecast, key=lambda item: item[1])
        rounded_days = max(1, math.ceil(earliest_days))
        if earliest_days <= 30:
            return CapacityTrend(
                "Expansion Planning Required",
                confidence,
                f"Expansion planning required: request trend reaches the planning reserve for {limiting_resource} within 30 days.",
                len(points),
                span_days,
                rounded_days,
                limiting_resource,
                points,
            )
    return CapacityTrend(
        "Capacity Available",
        confidence,
        "Capacity available: the request trend does not reach the planning reserve within 30 days.",
        len(points),
        span_days,
        None,
        None,
        points,
    )


def _trend_points(
    snapshots: Iterable[dict[str, Any]], planning_reserve_percent: int
) -> list[CapacityTrendPoint]:
    latest_at: datetime | None = None
    extracted: list[CapacityTrendPoint] = []
    for item in snapshots:
        snapshot = _historical_snapshot(item)
        collected_at = _snapshot_time(snapshot, item)
        if collected_at is None:
            continue
        nodes = [_allocation_node(node, planning_reserve_percent) for node in snapshot.get("nodes", [])]
        extracted.append(
            CapacityTrendPoint(
                collected_at,
                _sum_resources(node.planning_safe for node in nodes if node.eligible),
            )
        )
        latest_at = collected_at if latest_at is None or collected_at > latest_at else latest_at
    if latest_at is None:
        return []
    cutoff = latest_at - timedelta(days=30)
    by_time = {point.collected_at: point for point in extracted if point.collected_at >= cutoff}
    return [by_time[timestamp] for timestamp in sorted(by_time)]


def _historical_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    payload = item.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("snapshot"), dict):
        return payload["snapshot"]
    snapshot = item.get("snapshot")
    return snapshot if isinstance(snapshot, dict) else item


def _snapshot_time(snapshot: dict[str, Any], item: dict[str, Any]) -> datetime | None:
    value = snapshot.get("collected_at", item.get("collected_at"))
    if not isinstance(value, str):
        return None
    try:
        collected_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return collected_at.replace(tzinfo=UTC) if collected_at.tzinfo is None else collected_at.astimezone(UTC)


def _days_to_zero(latest: int, earliest: int, span_seconds: float) -> float | None:
    daily_change = (latest - earliest) / (span_seconds / timedelta(days=1).total_seconds())
    return latest / -daily_change if daily_change < 0 else None


def _exhausted_resources(resources: ResourceValues) -> list[str]:
    return [
        resource
        for resource, value in {"CPU": resources.cpu_millicores, "memory": resources.memory_bytes}.items()
        if value <= 0
    ]


def _resource_label(resources: list[str]) -> str:
    return " and ".join(resources)


def _node_conditions(node: AllocationNode) -> list[str]:
    return node.conditions


def evaluate_deployment_fit(
    snapshot: dict[str, Any],
    plan: AllocationPlan,
    demand: DeploymentDemand,
    docs: DocumentRegistry,
    report_state: str = "Current",
) -> DeploymentFit:
    if demand.replicas < 1:
        raise ValueError("replicas must be at least 1")
    if demand.requests.cpu_millicores <= 0 or demand.requests.memory_bytes <= 0:
        raise ValueError("CPU and memory requests must be greater than zero")

    eligible_nodes = [node for node in plan.nodes if node.eligible]
    total_request = _multiply(demand.requests, demand.replicas)
    maximum_safe_replicas = sum(_replicas_that_fit(node.planning_safe, demand.requests) for node in eligible_nodes)
    capacity_shortfall = _shortfall(total_request, plan.total_planning_safe)
    largest_node_capacity = _largest_node_capacity(eligible_nodes)
    per_pod_shortfall = _shortfall(demand.requests, largest_node_capacity)
    issues: list[FitIssue] = []
    capacity_blocked = maximum_safe_replicas < demand.replicas
    if capacity_blocked:
        issues.append(
            FitIssue(
                f"Known eligible nodes can accommodate only {maximum_safe_replicas} planning-safe replicas; {demand.replicas} are requested.",
                docs.source_for_rule("node-headroom"),
                True,
                "capacity",
            )
        )

    if demand.namespace:
        namespace = next(
            (item for item in snapshot.get("namespaces", []) if item.get("name") == demand.namespace), None
        )
        if namespace is None:
            raise ValueError("selected namespace is not present in the current snapshot")
        issues.extend(_namespace_fit_issues(namespace, demand, docs))

    if report_state != "Current":
        issues.append(
            FitIssue(
                "The latest snapshot is stale. Take a fresh snapshot before approving this deployment.",
                docs.source_for_rule("node-headroom"),
                True,
                "data-quality",
            )
        )
    else:
        pressured = [node for node in plan.nodes if node.has_pressure]
        if pressured:
            issues.append(
                FitIssue(
                    "One or more nodes report resource pressure. Resolve the pressure before approving this deployment.",
                    docs.source_for_rule("node-pressure"),
                    True,
                    "data-quality",
                )
            )
        elif not eligible_nodes:
            issues.append(
                FitIssue(
                    "No Ready, schedulable node is eligible for additional Pods. Restore eligible capacity before approval.",
                    docs.source_for_rule("node-headroom"),
                    True,
                    "data-quality",
                )
            )

    policy_blocked = any(issue.category == "policy" and issue.blocking for issue in issues)
    review_required = any(issue.category == "data-quality" for issue in issues)
    if review_required:
        summary = "Review required: the available evidence is not sufficient for a responsible approval decision."
    elif capacity_blocked and policy_blocked:
        summary = "Not approved: planning-safe capacity and namespace policy both block this deployment."
    elif capacity_blocked:
        summary = "Not approved: planning-safe cluster capacity cannot accommodate the requested replicas."
    elif policy_blocked:
        summary = "Not approved: namespace policy blocks this deployment even though cluster capacity is available."
    elif issues:
        summary = "Approved with policy review: resource capacity fits, but review the noted namespace policy before applying a manifest."
    else:
        summary = f"Approved: {demand.replicas} replicas fit within known planning-safe CPU and memory capacity."
    return DeploymentFit(
        "Review required" if review_required else "Approved" if not capacity_blocked and not policy_blocked else "Not approved",
        not review_required and not capacity_blocked and not policy_blocked,
        not capacity_blocked,
        summary,
        maximum_safe_replicas,
        total_request,
        capacity_shortfall,
        per_pod_shortfall,
        capacity_blocked,
        policy_blocked,
        review_required,
        issues,
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
                    "policy",
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
                            "policy",
                        )
                    )
                if upper > 0 and requested > upper:
                    issues.append(
                        FitIssue(
                            f"Pod LimitRange maximum {label} request would be exceeded in namespace {demand.namespace}.",
                            docs.source_for_rule("limit-range-coverage"),
                            True,
                            "policy",
                        )
                    )
        elif policy_type == "Container" and not _is_zero(minimum.add(maximum).add(default_request)):
            issues.append(
                FitIssue(
                    f"Container LimitRange policy in namespace {demand.namespace} requires manifest review.",
                    docs.source_for_rule("limit-range-coverage"),
                    False,
                    "policy",
                )
            )
    return issues


def _multiply(resources: ResourceValues, multiplier: int) -> ResourceValues:
    return ResourceValues(
        cpu_millicores=resources.cpu_millicores * multiplier,
        memory_bytes=resources.memory_bytes * multiplier,
        ephemeral_storage_bytes=resources.ephemeral_storage_bytes * multiplier,
    )


def _shortfall(required: ResourceValues, available: ResourceValues) -> ResourceValues:
    return ResourceValues(
        cpu_millicores=max(0, required.cpu_millicores - available.cpu_millicores),
        memory_bytes=max(0, required.memory_bytes - available.memory_bytes),
        ephemeral_storage_bytes=max(0, required.ephemeral_storage_bytes - available.ephemeral_storage_bytes),
    )


def _largest_node_capacity(nodes: Iterable[AllocationNode]) -> ResourceValues:
    largest = ResourceValues()
    for node in nodes:
        largest = largest.maximum(node.planning_safe)
    return largest


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
