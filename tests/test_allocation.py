from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kcp.allocation import DeploymentDemand, build_allocation_plan, build_management_decision, evaluate_deployment_fit
from kcp.docs import DocumentRegistry
from kcp.models import ResourceValues


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
                    "capacity": {"cpu_millicores": 2500, "memory_bytes": 2_500},
                    "allocatable": {"cpu_millicores": 2000, "memory_bytes": 2_000},
                    "requested": {"cpu_millicores": 800, "memory_bytes": 700},
                    "conditions": [],
                },
                {
                    "name": "worker-b",
                    "capacity": {"cpu_millicores": 1500, "memory_bytes": 1_500},
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
        self.assertEqual(plan.total_node_capacity.cpu_millicores, 4000)
        self.assertEqual(plan.total_not_allocatable.memory_bytes, 1000)
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

    def test_capacity_state_uses_per_node_reserve_and_eligibility(self) -> None:
        snapshot = {
            "metrics_available": False,
            "nodes": [
                {
                    "name": "worker-a",
                    "allocatable": {"cpu_millicores": 1000, "memory_bytes": 1_000},
                    "requested": {"cpu_millicores": 700, "memory_bytes": 700},
                    "conditions": [],
                    "ready": True,
                    "schedulable": True,
                },
                {
                    "name": "worker-b",
                    "allocatable": {"cpu_millicores": 1000, "memory_bytes": 1_000},
                    "requested": {"cpu_millicores": 100, "memory_bytes": 100},
                    "conditions": [],
                    "ready": False,
                    "schedulable": True,
                },
            ],
            "workloads": [],
        }

        plan = build_allocation_plan(snapshot, [], self.docs, planning_reserve_percent=20)

        self.assertEqual(plan.total_remaining.cpu_millicores, 1_200)
        self.assertEqual(plan.total_planning_safe.cpu_millicores, 100)
        self.assertEqual(plan.eligible_node_count, 1)
        self.assertEqual(plan.nodes[0].planning_safe.memory_bytes, 100)
        self.assertFalse(plan.nodes[1].eligible)
        self.assertEqual(plan.capacity_status.state, "Ready")
        self.assertEqual(plan.capacity_status.confidence, "Request-based")

    def test_capacity_state_is_constrained_at_the_reserve_threshold(self) -> None:
        snapshot = {
            "metrics_available": True,
            "nodes": [
                {
                    "name": "worker-a",
                    "allocatable": {"cpu_millicores": 1000, "memory_bytes": 1_000},
                    "requested": {"cpu_millicores": 800, "memory_bytes": 600},
                    "conditions": [],
                    "ready": True,
                    "schedulable": True,
                }
            ],
            "workloads": [],
        }

        plan = build_allocation_plan(snapshot, [], self.docs, planning_reserve_percent=20)

        self.assertEqual(plan.total_planning_safe.cpu_millicores, 0)
        self.assertEqual(plan.capacity_status.state, "Constrained")
        self.assertEqual(plan.capacity_status.confidence, "Usage available")

    def test_capacity_state_is_blocked_by_pressure_or_no_eligible_nodes(self) -> None:
        pressure_snapshot = {
            "metrics_available": True,
            "nodes": [
                {
                    "name": "worker-a",
                    "allocatable": {"cpu_millicores": 1000, "memory_bytes": 1_000},
                    "requested": {"cpu_millicores": 100, "memory_bytes": 100},
                    "conditions": ["MemoryPressure"],
                    "ready": True,
                    "schedulable": True,
                }
            ],
            "workloads": [],
        }
        unavailable_snapshot = {
            "metrics_available": True,
            "nodes": [
                {
                    "name": "worker-a",
                    "allocatable": {"cpu_millicores": 1000, "memory_bytes": 1_000},
                    "requested": {"cpu_millicores": 100, "memory_bytes": 100},
                    "conditions": [],
                    "ready": False,
                    "schedulable": True,
                }
            ],
            "workloads": [],
        }

        pressure_plan = build_allocation_plan(pressure_snapshot, [], self.docs, planning_reserve_percent=20)
        unavailable_plan = build_allocation_plan(unavailable_snapshot, [], self.docs, planning_reserve_percent=20)

        self.assertEqual(pressure_plan.capacity_status.state, "Blocked")
        self.assertIn("MemoryPressure", pressure_plan.capacity_status.summary)
        self.assertEqual(unavailable_plan.capacity_status.state, "Blocked")
        self.assertIn("No Ready", unavailable_plan.capacity_status.summary)

    def test_management_decision_requires_expansion_when_safe_capacity_is_exhausted(self) -> None:
        snapshot = _snapshot_at(
            datetime(2026, 8, 7, tzinfo=UTC),
            requested_cpu=800,
            requested_memory=600,
        )

        plan = build_allocation_plan(snapshot, [snapshot], self.docs, planning_reserve_percent=20)
        decision = build_management_decision(plan, report_state="Current", report_warnings=[])

        self.assertEqual(decision.state, "Expansion Required")
        self.assertEqual(decision.scheduling_confidence, "Current request-based data")
        self.assertEqual(decision.observed_usage, "Observed usage unavailable")
        self.assertIn("planning-safe CPU", decision.summary)

    def test_management_decision_requires_review_for_stale_or_pressured_data(self) -> None:
        current = _snapshot_at(
            datetime(2026, 8, 7, tzinfo=UTC),
            requested_cpu=100,
            requested_memory=100,
        )
        plan = build_allocation_plan(current, [current], self.docs, planning_reserve_percent=20)

        stale = build_management_decision(plan, report_state="Stale", report_warnings=[])

        self.assertEqual(stale.state, "Decision Needs Review")
        self.assertIn("stale", stale.summary.lower())

        pressured = _snapshot_at(
            datetime(2026, 8, 7, tzinfo=UTC),
            requested_cpu=100,
            requested_memory=100,
            conditions=["MemoryPressure"],
        )
        pressured_plan = build_allocation_plan(pressured, [pressured], self.docs, planning_reserve_percent=20)
        decision = build_management_decision(pressured_plan, report_state="Current", report_warnings=[])

        self.assertEqual(decision.state, "Decision Needs Review")
        self.assertIn("MemoryPressure", decision.summary)

    def test_request_trend_flags_expansion_with_preliminary_confidence(self) -> None:
        newest_at = datetime(2026, 8, 7, tzinfo=UTC)
        older = _snapshot_at(
            newest_at - timedelta(days=2), requested_cpu=200, requested_memory=200
        )
        newest = _snapshot_at(newest_at, requested_cpu=700, requested_memory=200)

        plan = build_allocation_plan(newest, [newest, older], self.docs, planning_reserve_percent=20)
        decision = build_management_decision(plan, report_state="Current", report_warnings=[])

        self.assertEqual(plan.trend.state, "Expansion Planning Required")
        self.assertEqual(plan.trend.confidence, "Preliminary trend")
        self.assertEqual(plan.trend.sample_count, 2)
        self.assertEqual(plan.trend.span_days, 2)
        self.assertEqual(decision.state, "Expansion Planning Required")
        self.assertIn("within 30 days", decision.summary)

    def test_request_trend_stays_available_when_metrics_are_missing(self) -> None:
        newest_at = datetime(2026, 8, 7, tzinfo=UTC)
        older = _snapshot_at(
            newest_at - timedelta(days=10), requested_cpu=100, requested_memory=100
        )
        newest = _snapshot_at(newest_at, requested_cpu=200, requested_memory=200)

        plan = build_allocation_plan(newest, [newest, older], self.docs, planning_reserve_percent=20)

        self.assertEqual(plan.trend.state, "Capacity Available")
        self.assertEqual(plan.trend.confidence, "Preliminary trend")
        self.assertEqual(plan.capacity_status.confidence, "Request-based")

    def test_resource_trend_uses_recorded_capacity_requests_limits_and_usage(self) -> None:
        newest_at = datetime(2026, 8, 7, tzinfo=UTC)
        older = _snapshot_at(newest_at - timedelta(days=1), requested_cpu=100, requested_memory=200)
        older["nodes"][0]["limits"] = {"cpu_millicores": 300, "memory_bytes": 400}

        newest = _snapshot_at(newest_at, requested_cpu=500, requested_memory=600)
        newest["metrics_available"] = True
        newest["nodes"][0]["limits"] = {"cpu_millicores": 700, "memory_bytes": 800}
        newest["nodes"][0]["usage"] = {"cpu_millicores": 250, "memory_bytes": 350}

        plan = build_allocation_plan(newest, [newest, older], self.docs)

        self.assertEqual(plan.resource_trend.points[0].total_capacity, ResourceValues(cpu_millicores=1200, memory_bytes=1200))
        self.assertEqual(plan.resource_trend.points[0].requested, ResourceValues(cpu_millicores=100, memory_bytes=200))
        self.assertEqual(plan.resource_trend.points[0].limits, ResourceValues(cpu_millicores=300, memory_bytes=400))
        self.assertIsNone(plan.resource_trend.points[0].usage)
        self.assertEqual(plan.resource_trend.points[1].requested, ResourceValues(cpu_millicores=500, memory_bytes=600))
        self.assertEqual(plan.resource_trend.points[1].limits, ResourceValues(cpu_millicores=700, memory_bytes=800))
        self.assertEqual(plan.resource_trend.points[1].usage, ResourceValues(cpu_millicores=250, memory_bytes=350))

    def test_dashboard_plan_can_exclude_control_plane_capacity_and_history(self) -> None:
        collected_at = datetime(2026, 8, 7, tzinfo=UTC)
        current = {
            "collected_at": collected_at.isoformat(),
            "metrics_available": True,
            "nodes": [
                {
                    "name": "control-plane-a",
                    "control_plane": True,
                    "capacity": {"cpu_millicores": 2000, "memory_bytes": 2000},
                    "allocatable": {"cpu_millicores": 1500, "memory_bytes": 1500},
                    "requested": {"cpu_millicores": 900, "memory_bytes": 900},
                    "limits": {"cpu_millicores": 1200, "memory_bytes": 1200},
                    "usage": {"cpu_millicores": 700, "memory_bytes": 700},
                    "conditions": [],
                },
                {
                    "name": "worker-a",
                    "capacity": {"cpu_millicores": 4000, "memory_bytes": 4000},
                    "allocatable": {"cpu_millicores": 3500, "memory_bytes": 3500},
                    "requested": {"cpu_millicores": 1000, "memory_bytes": 1000},
                    "limits": {"cpu_millicores": 2000, "memory_bytes": 2000},
                    "usage": {"cpu_millicores": 800, "memory_bytes": 800},
                    "conditions": [],
                },
            ],
            "workloads": [],
        }
        older = {
            **current,
            "collected_at": (collected_at - timedelta(days=1)).isoformat(),
        }

        full_plan = build_allocation_plan(current, [older], self.docs)
        dashboard_plan = build_allocation_plan(current, [older], self.docs, exclude_control_plane=True)

        self.assertEqual(full_plan.total_node_capacity.cpu_millicores, 6000)
        self.assertEqual(dashboard_plan.total_node_capacity.cpu_millicores, 4000)
        self.assertEqual(dashboard_plan.total_requested.cpu_millicores, 1000)
        self.assertEqual(dashboard_plan.total_observed_usage.cpu_millicores, 800)
        self.assertEqual(dashboard_plan.resource_trend.points[0].limits.cpu_millicores, 2000)
        self.assertEqual(dashboard_plan.resource_trend.points[1].usage.cpu_millicores, 800)

    def test_deployment_approval_reports_capacity_gaps_and_per_pod_fit(self) -> None:
        snapshot = {
            "metrics_available": False,
            "nodes": [
                {
                    "name": "worker-a",
                    "allocatable": {"cpu_millicores": 1_000, "memory_bytes": 1_000},
                    "requested": {"cpu_millicores": 500, "memory_bytes": 500},
                    "conditions": [],
                    "ready": True,
                    "schedulable": True,
                },
                {
                    "name": "worker-b",
                    "allocatable": {"cpu_millicores": 1_000, "memory_bytes": 1_000},
                    "requested": {"cpu_millicores": 500, "memory_bytes": 500},
                    "conditions": [],
                    "ready": True,
                    "schedulable": True,
                },
            ],
            "namespaces": [],
            "workloads": [],
        }
        plan = build_allocation_plan(snapshot, [], self.docs, planning_reserve_percent=20)

        approved = evaluate_deployment_fit(
            snapshot,
            plan,
            DeploymentDemand(1, ResourceValues(cpu_millicores=200, memory_bytes=200)),
            self.docs,
        )
        blocked = evaluate_deployment_fit(
            snapshot,
            plan,
            DeploymentDemand(3, ResourceValues(cpu_millicores=300, memory_bytes=300)),
            self.docs,
        )
        per_pod = evaluate_deployment_fit(
            snapshot,
            plan,
            DeploymentDemand(1, ResourceValues(cpu_millicores=400, memory_bytes=400)),
            self.docs,
        )

        self.assertEqual(approved.status, "Approved")
        self.assertTrue(approved.approved)
        self.assertEqual(approved.capacity_shortfall, ResourceValues())
        self.assertFalse(approved.policy_blocked)
        self.assertEqual(blocked.status, "Not approved")
        self.assertFalse(blocked.approved)
        self.assertTrue(blocked.capacity_blocked)
        self.assertEqual(blocked.maximum_safe_replicas, 2)
        self.assertEqual(blocked.capacity_shortfall.cpu_millicores, 300)
        self.assertEqual(blocked.capacity_shortfall.memory_bytes, 300)
        self.assertEqual(per_pod.maximum_safe_replicas, 0)
        self.assertEqual(per_pod.per_pod_shortfall.cpu_millicores, 100)
        self.assertEqual(per_pod.per_pod_shortfall.memory_bytes, 100)

    def test_deployment_approval_separates_policy_and_data_quality_blockers(self) -> None:
        snapshot = {
            "metrics_available": False,
            "nodes": [
                {
                    "name": "worker-a",
                    "allocatable": {"cpu_millicores": 1_000, "memory_bytes": 1_000},
                    "requested": {"cpu_millicores": 0, "memory_bytes": 0},
                    "conditions": [],
                    "ready": True,
                    "schedulable": True,
                }
            ],
            "namespaces": [
                {
                    "name": "payments",
                    "quotas": {
                        "requests.cpu": {"used": 800, "hard": 1_000},
                        "requests.memory": {"used": 800, "hard": 1_000},
                    },
                    "limit_ranges": [
                        {"type": "Pod", "minimum": {"cpu_millicores": 300, "memory_bytes": 300}},
                    ],
                }
            ],
            "workloads": [],
        }
        plan = build_allocation_plan(snapshot, [], self.docs, planning_reserve_percent=20)
        demand = DeploymentDemand(1, ResourceValues(cpu_millicores=200, memory_bytes=200), "payments")

        policy = evaluate_deployment_fit(snapshot, plan, demand, self.docs)
        stale = evaluate_deployment_fit(snapshot, plan, demand, self.docs, report_state="Stale")

        self.assertEqual(policy.status, "Not approved")
        self.assertFalse(policy.capacity_blocked)
        self.assertTrue(policy.policy_blocked)
        self.assertFalse(policy.approved)
        self.assertTrue(all(issue.category == "policy" for issue in policy.issues))
        self.assertEqual(stale.status, "Review required")
        self.assertTrue(stale.review_required)
        self.assertTrue(any(issue.category == "data-quality" for issue in stale.issues))

    def test_deployment_approval_requires_review_for_node_pressure_without_namespace(self) -> None:
        snapshot = _snapshot_at(
            datetime(2026, 8, 7, tzinfo=UTC),
            requested_cpu=100,
            requested_memory=100,
            conditions=["MemoryPressure"],
        )
        plan = build_allocation_plan(snapshot, [], self.docs, planning_reserve_percent=20)

        fit = evaluate_deployment_fit(
            snapshot,
            plan,
            DeploymentDemand(1, ResourceValues(cpu_millicores=100, memory_bytes=100)),
            self.docs,
        )

        self.assertEqual(fit.status, "Review required")
        self.assertTrue(fit.review_required)
        self.assertFalse(fit.policy_blocked)

    def test_rolling_update_capacity_uses_one_conservative_envelope_per_namespace(self) -> None:
        snapshot = {
            "nodes": [
                {
                    "name": "worker-a",
                    "allocatable": {"cpu_millicores": 1_000, "memory_bytes": 1_000},
                    "requested": {"cpu_millicores": 750, "memory_bytes": 700},
                    "conditions": [],
                    "ready": True,
                    "schedulable": True,
                }
            ],
            "workloads": [
                {
                    "namespace": "payments",
                    "kind": "Deployment",
                    "name": "api",
                    "desired_replicas": 4,
                    "deployment_strategy": "RollingUpdate",
                    "rolling_update_max_surge": "25%",
                    "template_requests": {"cpu_millicores": 100, "memory_bytes": 50},
                    "template_missing_requests": False,
                },
                {
                    "namespace": "payments",
                    "kind": "Deployment",
                    "name": "reports",
                    "desired_replicas": 2,
                    "deployment_strategy": "RollingUpdate",
                    "rolling_update_max_surge": "1",
                    "template_requests": {"cpu_millicores": 80, "memory_bytes": 150},
                    "template_missing_requests": False,
                },
                {
                    "namespace": "checkout",
                    "kind": "Deployment",
                    "name": "web",
                    "desired_replicas": 1,
                    "deployment_strategy": "RollingUpdate",
                    "template_requests": {"cpu_millicores": 100, "memory_bytes": 100},
                    "template_missing_requests": False,
                },
            ],
        }

        plan = build_allocation_plan(snapshot, [], self.docs)
        rollout = plan.rolling_update_capacity

        self.assertEqual(rollout.status, "Sufficient")
        self.assertEqual(rollout.namespace_count, 2)
        self.assertEqual(rollout.deployment_count, 3)
        self.assertEqual(rollout.additional_requests.cpu_millicores, 200)
        self.assertEqual(rollout.additional_requests.memory_bytes, 250)
        self.assertEqual(rollout.remaining_after.cpu_millicores, 50)
        self.assertEqual(rollout.remaining_after.memory_bytes, 50)
        payments = next(item for item in rollout.namespaces if item.namespace == "payments")
        self.assertEqual(payments.cpu_peak_deployment, "api")
        self.assertEqual(payments.memory_peak_deployment, "reports")
        self.assertEqual(payments.additional_requests.cpu_millicores, 100)
        self.assertEqual(payments.additional_requests.memory_bytes, 150)

    def test_rolling_update_capacity_reports_resource_shortfall(self) -> None:
        snapshot = {
            "nodes": [
                {
                    "name": "worker-a",
                    "allocatable": {"cpu_millicores": 1_000, "memory_bytes": 1_000},
                    "requested": {"cpu_millicores": 300, "memory_bytes": 500},
                    "conditions": [],
                    "ready": True,
                    "schedulable": True,
                }
            ],
            "workloads": [
                {
                    "namespace": "payments",
                    "kind": "Deployment",
                    "name": "api",
                    "desired_replicas": 3,
                    "deployment_strategy": "RollingUpdate",
                    "rolling_update_max_surge": 2,
                    "template_requests": {"cpu_millicores": 300, "memory_bytes": 300},
                    "template_missing_requests": False,
                },
                {
                    "namespace": "logging",
                    "kind": "Deployment",
                    "name": "collector",
                    "desired_replicas": 1,
                    "deployment_strategy": "RollingUpdate",
                    "rolling_update_max_surge": 1,
                    "template_requests": {"cpu_millicores": 100, "memory_bytes": 100},
                    "template_missing_requests": False,
                },
            ],
        }

        plan = build_allocation_plan(snapshot, [], self.docs)
        rollout = plan.rolling_update_capacity

        self.assertEqual(rollout.status, "Insufficient")
        self.assertEqual(rollout.additional_requests.cpu_millicores, 700)
        self.assertEqual(rollout.additional_requests.memory_bytes, 700)
        self.assertEqual(rollout.shortfall.cpu_millicores, 0)
        self.assertEqual(rollout.shortfall.memory_bytes, 200)

    def test_rolling_update_capacity_marks_missing_rollout_data_incomplete(self) -> None:
        snapshot = {
            "nodes": [
                {
                    "name": "worker-a",
                    "allocatable": {"cpu_millicores": 1_000, "memory_bytes": 1_000},
                    "requested": {"cpu_millicores": 100, "memory_bytes": 100},
                    "conditions": [],
                    "ready": True,
                    "schedulable": True,
                }
            ],
            "workloads": [
                {
                    "namespace": "payments",
                    "kind": "Deployment",
                    "name": "api",
                    "desired_replicas": 2,
                    "deployment_strategy": "RollingUpdate",
                    "rolling_update_max_surge": "50%",
                    "template_requests": {"cpu_millicores": 100, "memory_bytes": 100},
                    "template_missing_requests": False,
                },
                {
                    "namespace": "legacy",
                    "kind": "Deployment",
                    "name": "worker",
                    "requests": {"cpu_millicores": 100, "memory_bytes": 100},
                },
                {
                    "namespace": "no-requests",
                    "kind": "Deployment",
                    "name": "batch",
                    "desired_replicas": 1,
                    "deployment_strategy": "RollingUpdate",
                    "rolling_update_max_surge": 1,
                    "template_requests": {"cpu_millicores": 0, "memory_bytes": 0},
                    "template_missing_requests": True,
                },
            ],
        }

        plan = build_allocation_plan(snapshot, [], self.docs)
        rollout = plan.rolling_update_capacity

        self.assertEqual(rollout.status, "Incomplete")
        self.assertEqual(rollout.additional_requests, ResourceValues(cpu_millicores=100, memory_bytes=100))
        self.assertEqual(len(rollout.data_gaps), 2)


def _snapshot_at(
    collected_at: datetime,
    requested_cpu: int,
    requested_memory: int,
    conditions: list[str] | None = None,
) -> dict:
    return {
        "collected_at": collected_at.isoformat(),
        "metrics_available": False,
        "nodes": [
            {
                "name": "worker-a",
                "capacity": {"cpu_millicores": 1200, "memory_bytes": 1200},
                "allocatable": {"cpu_millicores": 1000, "memory_bytes": 1000},
                "requested": {"cpu_millicores": requested_cpu, "memory_bytes": requested_memory},
                "conditions": conditions or [],
                "ready": True,
                "schedulable": True,
            }
        ],
        "workloads": [],
    }
