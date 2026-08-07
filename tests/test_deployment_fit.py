from __future__ import annotations

import unittest
from pathlib import Path

from kcp.allocation import DeploymentDemand, build_allocation_plan, evaluate_deployment_fit
from kcp.docs import DocumentRegistry
from kcp.models import ResourceValues


class DeploymentFitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = DocumentRegistry(Path("kcp/assets/k8s-docs"))

    def test_request_based_fit_packs_replicas_per_eligible_node(self) -> None:
        snapshot = _snapshot()
        plan = build_allocation_plan(snapshot, [], self.docs, planning_reserve_percent=20)

        fit = evaluate_deployment_fit(
            snapshot,
            plan,
            DeploymentDemand(replicas=2, requests=ResourceValues(cpu_millicores=300, memory_bytes=300)),
            self.docs,
        )

        self.assertEqual(fit.status, "Fits")
        self.assertTrue(fit.fits)
        self.assertEqual(fit.maximum_safe_replicas, 2)
        self.assertEqual(fit.issues, [])

    def test_fit_blocks_demand_that_exceeds_per_node_safe_capacity(self) -> None:
        snapshot = _snapshot()
        plan = build_allocation_plan(snapshot, [], self.docs, planning_reserve_percent=20)

        fit = evaluate_deployment_fit(
            snapshot,
            plan,
            DeploymentDemand(replicas=3, requests=ResourceValues(cpu_millicores=300, memory_bytes=300)),
            self.docs,
        )

        self.assertEqual(fit.status, "Blocked")
        self.assertFalse(fit.fits)
        self.assertIn("only 2", fit.summary)
        self.assertEqual(fit.issues[0].source["document_id"], "node-allocatable")

    def test_namespace_quota_and_pod_limit_range_can_block_fit(self) -> None:
        snapshot = _snapshot(
            quotas={"requests.cpu": {"used": 500, "hard": 900}},
            limit_ranges=[
                {
                    "type": "Pod",
                    "minimum": {"cpu_millicores": 100, "memory_bytes": 100},
                    "maximum": {"cpu_millicores": 350, "memory_bytes": 350},
                    "default_request": {},
                }
            ],
        )
        plan = build_allocation_plan(snapshot, [], self.docs, planning_reserve_percent=20)

        quota_fit = evaluate_deployment_fit(
            snapshot,
            plan,
            DeploymentDemand(
                replicas=2,
                requests=ResourceValues(cpu_millicores=300, memory_bytes=300),
                namespace="payments",
            ),
            self.docs,
        )
        limit_range_fit = evaluate_deployment_fit(
            snapshot,
            plan,
            DeploymentDemand(
                replicas=1,
                requests=ResourceValues(cpu_millicores=400, memory_bytes=300),
                namespace="payments",
            ),
            self.docs,
        )

        self.assertEqual(quota_fit.status, "Blocked")
        self.assertIn("ResourceQuota", quota_fit.summary)
        self.assertEqual(quota_fit.issues[0].source["document_id"], "resource-quota")
        self.assertEqual(limit_range_fit.status, "Blocked")
        self.assertIn("LimitRange", limit_range_fit.summary)
        self.assertEqual(limit_range_fit.issues[0].source["document_id"], "limit-range")

    def test_namespace_is_optional_and_container_policy_requires_review(self) -> None:
        snapshot = _snapshot(
            limit_ranges=[
                {
                    "type": "Container",
                    "minimum": {"cpu_millicores": 100},
                    "maximum": {},
                    "default_request": {},
                }
            ]
        )
        plan = build_allocation_plan(snapshot, [], self.docs, planning_reserve_percent=20)

        no_namespace = evaluate_deployment_fit(
            snapshot,
            plan,
            DeploymentDemand(replicas=1, requests=ResourceValues(cpu_millicores=300, memory_bytes=300)),
            self.docs,
        )
        namespace_selected = evaluate_deployment_fit(
            snapshot,
            plan,
            DeploymentDemand(
                replicas=1,
                requests=ResourceValues(cpu_millicores=300, memory_bytes=300),
                namespace="payments",
            ),
            self.docs,
        )

        self.assertEqual(no_namespace.status, "Fits")
        self.assertEqual(namespace_selected.status, "Constrained")
        self.assertIn("Container LimitRange", namespace_selected.summary)


def _snapshot(
    quotas: dict[str, dict[str, int]] | None = None, limit_ranges: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "metrics_available": True,
        "nodes": [
            {
                "name": "worker-a",
                "allocatable": {"cpu_millicores": 1000, "memory_bytes": 1000},
                "requested": {"cpu_millicores": 200, "memory_bytes": 200},
                "conditions": [],
                "ready": True,
                "schedulable": True,
            },
            {
                "name": "worker-b",
                "allocatable": {"cpu_millicores": 1000, "memory_bytes": 1000},
                "requested": {"cpu_millicores": 600, "memory_bytes": 600},
                "conditions": [],
                "ready": True,
                "schedulable": True,
            },
        ],
        "namespaces": [
            {
                "name": "payments",
                "has_limit_range": bool(limit_ranges),
                "quotas": quotas or {},
                "limit_ranges": limit_ranges or [],
            }
        ],
        "workloads": [],
    }
