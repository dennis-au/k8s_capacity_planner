from __future__ import annotations

import unittest

from kcp.analysis import analyze_snapshot
from kcp.docs import DocumentRegistry
from kcp.models import ClusterSnapshot, NamespaceSummary, NodeSummary, QuotaUsage, ResourceValues, WorkloadSummary


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = DocumentRegistry(__import__("pathlib").Path("kcp/assets/k8s-docs"))

    def test_reports_capacity_and_policy_findings_with_local_sources(self) -> None:
        snapshot = ClusterSnapshot(
            cluster_version="v1.35.2",
            metrics_available=False,
            nodes=[
                NodeSummary(
                    name="worker-a",
                    allocatable=ResourceValues(cpu_millicores=1000, memory_bytes=1_000),
                    requested=ResourceValues(cpu_millicores=950, memory_bytes=850),
                    limits=ResourceValues(cpu_millicores=2_000, memory_bytes=1_500),
                    conditions=["MemoryPressure"],
                )
            ],
            namespaces=[
                NamespaceSummary(
                    name="payments",
                    has_limit_range=False,
                    quotas={"requests.cpu": QuotaUsage(used=95, hard=100)},
                )
            ],
            workloads=[
                WorkloadSummary(
                    namespace="payments",
                    kind="Deployment",
                    name="api",
                    replicas=2,
                    requests=ResourceValues(cpu_millicores=500, memory_bytes=400),
                    limits=ResourceValues(cpu_millicores=600, memory_bytes=500),
                    usage=ResourceValues(),
                    qos="BestEffort",
                    missing_requests=True,
                    has_hpa=False,
                )
            ],
        )

        findings = analyze_snapshot(snapshot, self.docs)
        rule_ids = {finding.rule_id for finding in findings}

        self.assertTrue(
            {
                "version-compatibility",
                "metrics-availability",
                "node-headroom",
                "node-pressure",
                "quota-pressure",
                "limit-range-coverage",
                "missing-requests",
                "qos-eviction",
                "hpa-coverage",
            }.issubset(rule_ids)
        )
        finding = next(finding for finding in findings if finding.rule_id == "node-headroom")
        self.assertEqual(finding.source["document_id"], "node-allocatable")
        self.assertEqual(finding.severity, "critical")

    def test_quantity_parsing_handles_cpu_and_binary_memory(self) -> None:
        self.assertEqual(ResourceValues.from_quantities({"cpu": "1.5", "memory": "512Mi"}).cpu_millicores, 1500)
        self.assertEqual(ResourceValues.from_quantities({"cpu": "250m", "memory": "1Gi"}).memory_bytes, 1_073_741_824)
