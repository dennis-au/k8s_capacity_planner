from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kcp.docs import DocumentRegistry
from kcp.models import ClusterSnapshot, NodeSummary, ResourceValues, WorkloadSummary


WARNING_RATIO = 0.80
CRITICAL_RATIO = 0.90
LIMIT_CRITICAL_RATIO = 0.95
BASELINE_MINOR = "1.36"


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: str
    title: str
    resource: str
    evidence: str
    recommendation: str
    source: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "title": self.title,
            "resource": self.resource,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
            "source": self.source,
        }


def analyze_snapshot(snapshot: ClusterSnapshot, docs: DocumentRegistry) -> list[Finding]:
    findings: list[Finding] = []
    if _minor_version(snapshot.cluster_version) != BASELINE_MINOR:
        findings.append(
            _finding(
                docs,
                "version-compatibility",
                "warning",
                "Documentation baseline differs from cluster version",
                "cluster",
                f"Cluster reports {snapshot.cluster_version}; the bundled guidance baseline is v{BASELINE_MINOR}.",
                "Review the local source guidance as a compatibility reference before applying version-specific changes.",
            )
        )
    if not snapshot.metrics_available:
        findings.append(
            _finding(
                docs,
                "metrics-availability",
                "warning",
                "Live resource metrics are unavailable",
                "cluster",
                "The metrics.k8s.io API did not provide node and pod usage.",
                "Install or repair the in-site metrics pipeline before relying on usage-based recommendations.",
            )
        )
    for node in snapshot.nodes:
        findings.extend(_node_findings(node, docs))
    for namespace in snapshot.namespaces:
        if not namespace.has_limit_range:
            findings.append(
                _finding(
                    docs,
                    "limit-range-coverage",
                    "warning",
                    "Namespace has no LimitRange",
                    f"namespace/{namespace.name}",
                    "No LimitRange was found for the namespace.",
                    "Consider namespace defaults and bounds that fit the team's resource policy.",
                )
            )
        for resource, quota in namespace.quotas.items():
            severity = _severity(quota.ratio)
            if severity:
                findings.append(
                    _finding(
                        docs,
                        "quota-pressure",
                        severity,
                        "ResourceQuota is near its hard limit",
                        f"namespace/{namespace.name}",
                        f"{resource} is using {quota.used} of {quota.hard} ({quota.ratio:.0%}).",
                        "Review quota usage and planned workload growth before new workloads are admitted.",
                    )
                )
    for workload in snapshot.workloads:
        findings.extend(_workload_findings(workload, snapshot.metrics_available, docs))
    return sorted(findings, key=lambda finding: (_severity_rank(finding.severity), finding.resource, finding.title))


def _node_findings(node: NodeSummary, docs: DocumentRegistry) -> list[Finding]:
    findings: list[Finding] = []
    for resource, ratio in node.requested.ratios(node.allocatable).items():
        severity = _severity(ratio)
        if severity:
            findings.append(
                _finding(
                    docs,
                    "node-headroom",
                    severity,
                    "Schedulable node headroom is low",
                    f"node/{node.name}",
                    f"Requested {resource} is {ratio:.0%} of node allocatable {resource}.",
                    "Review requests, node-pool capacity, and pending workload demand before scheduling additional work.",
                )
            )
    for condition in node.conditions:
        if condition in {"MemoryPressure", "DiskPressure", "PIDPressure"}:
            findings.append(
                _finding(
                    docs,
                    "node-pressure",
                    "critical",
                    "Node reports resource pressure",
                    f"node/{node.name}",
                    f"Node condition {condition} is active.",
                    "Investigate the pressured resource and eviction risk before adding workload demand.",
                )
            )
    return findings


def _workload_findings(
    workload: WorkloadSummary, metrics_available: bool, docs: DocumentRegistry
) -> list[Finding]:
    findings: list[Finding] = []
    if workload.missing_requests:
        findings.append(
            _finding(
                docs,
                "missing-requests",
                "warning",
                "Workload has containers without CPU or memory requests",
                workload.identity,
                "At least one container does not set both CPU and memory requests.",
                "Set requests from observed demand so scheduling and QoS classification have an explicit baseline.",
            )
        )
    if workload.qos == "BestEffort":
        findings.append(
            _finding(
                docs,
                "qos-eviction",
                "warning",
                "BestEffort workload has the highest eviction exposure",
                workload.identity,
                "The workload is classified as BestEffort.",
                "Set CPU and memory requests and limits where appropriate for the workload policy.",
            )
        )
    elif workload.qos == "Burstable":
        findings.append(
            _finding(
                docs,
                "qos-eviction",
                "info",
                "Burstable workload can be evicted under node pressure",
                workload.identity,
                "The workload is classified as Burstable.",
                "Review requests and memory limits alongside node-pressure findings.",
            )
        )
    if metrics_available and workload.usage is not None:
        for resource, ratio in workload.usage.ratios(workload.limits).items():
            severity = _limit_severity(ratio)
            if severity:
                findings.append(
                    _finding(
                        docs,
                        "limit-pressure",
                        severity,
                        "Workload usage is close to its configured limit",
                        workload.identity,
                        f"Live {resource} usage is {ratio:.0%} of the configured limit.",
                        "Review the limit and demand profile; CPU can throttle and memory can trigger OOM termination.",
                    )
                )
    if not workload.has_hpa:
        findings.append(
            _finding(
                docs,
                "hpa-coverage",
                "info",
                "Workload has no HorizontalPodAutoscaler",
                workload.identity,
                "No HorizontalPodAutoscaler targets this workload.",
                "Confirm that replica scaling is intentional for this workload's demand profile.",
            )
        )
    return findings


def _finding(
    docs: DocumentRegistry,
    rule_id: str,
    severity: str,
    title: str,
    resource: str,
    evidence: str,
    recommendation: str,
) -> Finding:
    return Finding(rule_id, severity, title, resource, evidence, recommendation, docs.source_for_rule(rule_id))


def _severity(ratio: float) -> str | None:
    if ratio >= CRITICAL_RATIO:
        return "critical"
    if ratio >= WARNING_RATIO:
        return "warning"
    return None


def _limit_severity(ratio: float) -> str | None:
    if ratio >= LIMIT_CRITICAL_RATIO:
        return "critical"
    if ratio >= WARNING_RATIO:
        return "warning"
    return None


def _minor_version(version: str) -> str:
    normalized = version.lstrip("v")
    parts = normalized.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else normalized


def _severity_rank(severity: str) -> int:
    return {"critical": 0, "warning": 1, "info": 2}.get(severity, 3)
