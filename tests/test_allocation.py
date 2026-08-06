from __future__ import annotations

import unittest
from pathlib import Path

from kcp.allocation import build_allocation_plan
from kcp.docs import DocumentRegistry


class AllocationPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = DocumentRegistry(Path("kcp/assets/k8s-docs"))

    def test_recommends_request_floors_from_retained_metrics(self) -> None:
        current = {
            "metrics_available": True,
            "nodes": [
                {
                    "name": "worker-a",
                    "allocatable": {"cpu_millicores": 2000, "memory_bytes": 2_000},
                    "requested": {"cpu_millicores": 800, "memory_bytes": 700},
                    "conditions": [],
                },
                {
                    "name": "worker-b",
                    "allocatable": {"cpu_millicores": 1000, "memory_bytes": 1_000},
                    "requested": {"cpu_millicores": 500, "memory_bytes": 400},
                    "conditions": ["MemoryPressure"],
                },
            ],
            "workloads": [
                {
                    "namespace": "payments",
                    "kind": "Deployment",
                    "name": "api",
                    "requests": {"cpu_millicores": 300, "memory_bytes": 250},
                    "usage": {"cpu_millicores": 350, "memory_bytes": 280},
                    "missing_requests": False,
                },
                {
                    "namespace": "payments",
                    "kind": "Deployment",
                    "name": "worker",
                    "requests": {"cpu_millicores": 0, "memory_bytes": 0},
                    "usage": {"cpu_millicores": 80, "memory_bytes": 90},
                    "missing_requests": True,
                },
                {
                    "namespace": "payments",
                    "kind": "Job",
                    "name": "importer",
                    "requests": {"cpu_millicores": 100, "memory_bytes": 120},
                    "usage": None,
                    "missing_requests": False,
                },
            ],
        }
        older = {
            "metrics_available": True,
            "workloads": [
                {
                    "namespace": "payments",
                    "kind": "Deployment",
                    "name": "api",
                    "usage": {"cpu_millicores": 420, "memory_bytes": 400},
                },
                {
                    "namespace": "payments",
                    "kind": "Deployment",
                    "name": "worker",
                    "usage": {"cpu_millicores": 100, "memory_bytes": 120},
                },
            ],
        }

        plan = build_allocation_plan(current, [current, older], self.docs)

        self.assertEqual(plan.total_allocatable.cpu_millicores, 3000)
        self.assertEqual(plan.total_requested.memory_bytes, 1100)
        self.assertEqual(plan.total_remaining.cpu_millicores, 1700)
        self.assertEqual(plan.nodes[0].remaining.memory_bytes, 1300)
        self.assertTrue(plan.nodes[1].has_pressure)

        recommendations = {recommendation.identity: recommendation for recommendation in plan.recommendations}
        api = recommendations["payments/Deployment/api"]
        self.assertEqual(api.status, "increase-request")
        self.assertEqual(api.observed_peak.cpu_millicores, 420)
        self.assertEqual(api.suggested_request.memory_bytes, 400)
        self.assertEqual(api.source["document_id"], "resource-management")

        worker = recommendations["payments/Deployment/worker"]
        self.assertEqual(worker.status, "set-requests")
        self.assertEqual(worker.suggested_request.cpu_millicores, 100)

        importer = recommendations["payments/Job/importer"]
        self.assertEqual(importer.status, "collect-metrics")
        self.assertIsNone(importer.suggested_request)
        self.assertEqual(importer.source["document_id"], "resource-metrics")
