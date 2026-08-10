from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from kubernetes import client

from kcp.kubernetes import (
    _namespace_summaries,
    _node_ready,
    KubernetesCollector,
    inspect_kubeconfig,
    inspect_kubeconfig_text,
    pod_qos,
    pod_resources,
)


class KubernetesNormalizationTests(unittest.TestCase):
    def test_pod_resources_use_regular_sum_init_max_and_overhead(self) -> None:
        pod = client.V1Pod(
            spec=client.V1PodSpec(
                containers=[
                    client.V1Container(
                        name="api",
                        image="example",
                        resources=client.V1ResourceRequirements(requests={"cpu": "250m", "memory": "256Mi"}),
                    ),
                    client.V1Container(
                        name="sidecar",
                        image="example",
                        resources=client.V1ResourceRequirements(requests={"cpu": "100m", "memory": "128Mi"}),
                    ),
                ],
                init_containers=[
                    client.V1Container(
                        name="init",
                        image="example",
                        resources=client.V1ResourceRequirements(requests={"cpu": "500m", "memory": "64Mi"}),
                    )
                ],
                overhead={"cpu": "10m", "memory": "1Mi"},
            )
        )

        resources = pod_resources(pod)

        self.assertEqual(resources.cpu_millicores, 510)
        self.assertEqual(resources.memory_bytes, 385 * 1024 * 1024)

    def test_pod_qos_follows_request_limit_shape(self) -> None:
        guaranteed = client.V1Pod(
            spec=client.V1PodSpec(
                containers=[
                    client.V1Container(
                        name="api",
                        image="example",
                        resources=client.V1ResourceRequirements(
                            requests={"cpu": "1", "memory": "1Gi"}, limits={"cpu": "1", "memory": "1Gi"}
                        ),
                    )
                ]
            )
        )
        best_effort = client.V1Pod(spec=client.V1PodSpec(containers=[client.V1Container(name="api", image="example")]))

        self.assertEqual(pod_qos(guaranteed), "Guaranteed")
        self.assertEqual(pod_qos(best_effort), "BestEffort")

    def test_kubeconfig_uses_selected_static_context_and_https_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kubeconfig = _write_kubeconfig(Path(temp_dir), context="production")

            details = inspect_kubeconfig(kubeconfig, "production", "10.20.30.40")

        self.assertEqual(details.context, "production")
        self.assertEqual(details.endpoint, "https://10.20.30.40:6443")
        self.assertEqual(details.tls_server_name, "production.darksite.local")

    def test_kubeconfig_text_uses_current_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kubeconfig = _write_kubeconfig(Path(temp_dir), context="production")
            details = inspect_kubeconfig_text(kubeconfig.read_text(encoding="utf-8"))

        self.assertEqual(details.context, "production")
        self.assertEqual(details.endpoint, "https://production.darksite.local:6443")

    def test_kubeconfig_rejects_exec_authentication_and_insecure_tls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exec_kubeconfig = _write_kubeconfig(root, user="exec:\n      command: credential-helper")
            with self.assertRaisesRegex(ValueError, "exec or auth-provider"):
                inspect_kubeconfig(exec_kubeconfig)

            insecure_kubeconfig = _write_kubeconfig(root, filename="insecure", insecure=True)
            with self.assertRaisesRegex(ValueError, "TLS verification"):
                inspect_kubeconfig(insecure_kubeconfig)

            valid_kubeconfig = _write_kubeconfig(root, filename="valid")
            with self.assertRaisesRegex(ValueError, "valid IPv4 or IPv6"):
                inspect_kubeconfig(valid_kubeconfig, api_ip="not-an-ip")

    def test_collector_loads_kubeconfig_without_persisting_it(self) -> None:
        with patch("kcp.kubernetes.kube_config.new_client_from_config") as new_client, patch(
            "kcp.kubernetes.ApiClient", wraps=client.ApiClient
        ) as configured_client:
            new_client.return_value = client.ApiClient()
            with patch("kcp.kubernetes.inspect_kubeconfig") as inspect:
                inspect.return_value = type(
                    "Details",
                    (),
                    {
                        "context": "production",
                        "endpoint": "https://10.20.30.40:6443",
                        "tls_server_name": "production.darksite.local",
                    },
                )()

                new_client.return_value.configuration.proxy = "http://proxy.darksite.local:3128"
                KubernetesCollector.from_kubeconfig(
                    "/run/kcp/production.kubeconfig", "production", "10.20.30.40", disable_proxy=True
                )

        new_client.assert_called_once_with(
            config_file="/run/kcp/production.kubeconfig",
            context="production",
            persist_config=False,
        )
        configuration = configured_client.call_args.args[0]
        self.assertEqual(configuration.host, "https://10.20.30.40:6443")
        self.assertEqual(configuration.tls_server_name, "production.darksite.local")
        self.assertEqual(configuration.assert_hostname, "production.darksite.local")
        self.assertIsNone(configuration.proxy)
        self.assertTrue(callable(getattr(KubernetesCollector, "collect", None)))

    def test_connection_test_reads_only_the_kubernetes_version_endpoint(self) -> None:
        collector = KubernetesCollector.__new__(KubernetesCollector)
        collector.version = Mock()
        collector.version.get_code.return_value = Mock(git_version="v1.36.1")

        self.assertEqual(collector.test_connection(), "v1.36.1")
        collector.version.get_code.assert_called_once_with(_request_timeout=10)

    def test_collection_includes_node_capacity_before_allocatable_resources(self) -> None:
        collector = KubernetesCollector.__new__(KubernetesCollector)
        collector.version = Mock()
        collector.version.get_code.return_value = Mock(git_version="v1.36.1")
        collector.core = Mock()
        collector.apps = Mock()
        collector.autoscaling = Mock()
        collector.custom = Mock()
        node = client.V1Node(
            metadata=client.V1ObjectMeta(name="worker-a"),
            spec=client.V1NodeSpec(),
            status=client.V1NodeStatus(
                capacity={"cpu": "2500m", "memory": "3Gi"},
                allocatable={"cpu": "2", "memory": "2Gi"},
                conditions=[client.V1NodeCondition(type="Ready", status="True")],
            ),
        )
        collector.core.list_node.return_value = Mock(items=[node])
        namespace = client.V1Namespace(metadata=client.V1ObjectMeta(name="payments"))
        quota = client.V1ResourceQuota(
            metadata=client.V1ObjectMeta(name="object-count", namespace="payments"),
            status=client.V1ResourceQuotaStatus(hard={"pods": "2k"}, used={"pods": "1k"}),
        )
        collector.core.list_namespace.return_value = Mock(items=[namespace])
        collector.core.list_pod_for_all_namespaces.return_value = Mock(items=[])
        collector.core.list_resource_quota_for_all_namespaces.return_value = Mock(items=[quota])
        collector.core.list_limit_range_for_all_namespaces.return_value = Mock(items=[])
        collector.core.list_event_for_all_namespaces.return_value = Mock(items=[])
        collector.apps.list_deployment_for_all_namespaces.return_value = Mock(items=[])
        collector.apps.list_replica_set_for_all_namespaces.return_value = Mock(items=[])
        collector.autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces.return_value = Mock(items=[])
        collector.custom.list_cluster_custom_object.return_value = {"items": []}

        snapshot = collector.collect()

        self.assertEqual(snapshot.nodes[0].capacity.cpu_millicores, 2500)
        self.assertEqual(snapshot.nodes[0].capacity.memory_bytes, 3 * 1024**3)
        self.assertEqual(snapshot.nodes[0].allocatable.cpu_millicores, 2000)
        self.assertEqual(snapshot.namespaces[0].quotas["pods"].used, 1000)
        self.assertEqual(snapshot.namespaces[0].quotas["pods"].hard, 2000)
        for api in (collector.core, collector.apps, collector.autoscaling, collector.custom):
            self.assertTrue(all(call[0].startswith("list_") for call in api.method_calls))

    def test_collection_records_deployment_rolling_update_requests_without_active_pods(self) -> None:
        collector = KubernetesCollector.__new__(KubernetesCollector)
        collector.version = Mock()
        collector.version.get_code.return_value = Mock(git_version="v1.36.1")
        collector.core = Mock()
        collector.apps = Mock()
        collector.autoscaling = Mock()
        collector.custom = Mock()
        deployment = client.V1Deployment(
            metadata=client.V1ObjectMeta(name="api", namespace="payments"),
            spec=client.V1DeploymentSpec(
                replicas=4,
                selector=client.V1LabelSelector(match_labels={"app": "api"}),
                strategy=client.V1DeploymentStrategy(
                    type="RollingUpdate",
                    rolling_update=client.V1RollingUpdateDeployment(max_surge="50%"),
                ),
                template=client.V1PodTemplateSpec(
                    spec=client.V1PodSpec(
                        containers=[
                            client.V1Container(
                                name="api",
                                image="example",
                                resources=client.V1ResourceRequirements(
                                    requests={"cpu": "250m", "memory": "128Mi"}
                                ),
                            )
                        ]
                    )
                ),
            ),
        )
        collector.core.list_node.return_value = Mock(items=[])
        collector.core.list_namespace.return_value = Mock(items=[])
        collector.core.list_pod_for_all_namespaces.return_value = Mock(items=[])
        collector.core.list_resource_quota_for_all_namespaces.return_value = Mock(items=[])
        collector.core.list_limit_range_for_all_namespaces.return_value = Mock(items=[])
        collector.core.list_event_for_all_namespaces.return_value = Mock(items=[])
        collector.apps.list_deployment_for_all_namespaces.return_value = Mock(items=[deployment])
        collector.apps.list_replica_set_for_all_namespaces.return_value = Mock(items=[])
        collector.autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces.return_value = Mock(items=[])
        collector.custom.list_cluster_custom_object.return_value = {"items": []}

        snapshot = collector.collect()

        self.assertEqual(len(snapshot.workloads), 1)
        workload = snapshot.workloads[0]
        self.assertEqual(workload.identity, "payments/Deployment/api")
        self.assertEqual(workload.desired_replicas, 4)
        self.assertEqual(workload.deployment_strategy, "RollingUpdate")
        self.assertEqual(workload.rolling_update_max_surge, "50%")
        self.assertEqual(workload.template_requests.cpu_millicores, 250)
        self.assertEqual(workload.template_requests.memory_bytes, 128 * 1024**2)
        self.assertFalse(workload.template_missing_requests)
        collector.apps.list_deployment_for_all_namespaces.assert_called_once_with(_request_timeout=20)

    def test_node_ready_distinguishes_ready_from_not_ready_conditions(self) -> None:
        ready = client.V1Node(
            metadata=client.V1ObjectMeta(name="worker-a"),
            spec=client.V1NodeSpec(),
            status=client.V1NodeStatus(
                conditions=[client.V1NodeCondition(type="Ready", status="True")]
            ),
        )
        not_ready = client.V1Node(
            metadata=client.V1ObjectMeta(name="worker-b"),
            spec=client.V1NodeSpec(unschedulable=True),
            status=client.V1NodeStatus(
                conditions=[client.V1NodeCondition(type="Ready", status="False")]
            ),
        )

        self.assertTrue(_node_ready(ready))
        self.assertFalse(_node_ready(not_ready))
        self.assertTrue(not_ready.spec.unschedulable)

    def test_namespace_summary_captures_limit_range_policy(self) -> None:
        summaries = _namespace_summaries(
            [client.V1Namespace(metadata=client.V1ObjectMeta(name="payments"))],
            [],
            [
                client.V1LimitRange(
                    metadata=client.V1ObjectMeta(name="limits", namespace="payments"),
                    spec=client.V1LimitRangeSpec(
                        limits=[
                            client.V1LimitRangeItem(
                                type="Pod",
                                min={"cpu": "100m", "memory": "64Mi"},
                                max={"cpu": "1", "memory": "1Gi"},
                            )
                        ]
                    ),
                )
            ],
        )

        policy = summaries[0].limit_ranges[0]
        self.assertTrue(summaries[0].has_limit_range)
        self.assertEqual(policy.type, "Pod")
        self.assertEqual(policy.minimum.cpu_millicores, 100)
        self.assertEqual(policy.maximum.memory_bytes, 1024 * 1024 * 1024)

    def test_namespace_summary_uses_the_most_restrictive_duplicate_quota(self) -> None:
        summaries = _namespace_summaries(
            [client.V1Namespace(metadata=client.V1ObjectMeta(name="payments"))],
            [
                client.V1ResourceQuota(
                    metadata=client.V1ObjectMeta(name="restricted", namespace="payments"),
                    status=client.V1ResourceQuotaStatus(hard={"requests.cpu": "100m"}, used={"requests.cpu": "90m"}),
                ),
                client.V1ResourceQuota(
                    metadata=client.V1ObjectMeta(name="broad", namespace="payments"),
                    status=client.V1ResourceQuotaStatus(
                        hard={"requests.cpu": "1", "pods": "2k"},
                        used={"requests.cpu": "100m", "pods": "1k"},
                    ),
                ),
            ],
            [],
        )

        quota = summaries[0].quotas["requests.cpu"]
        self.assertEqual(quota.used, 90)
        self.assertEqual(quota.hard, 100)
        pods = summaries[0].quotas["pods"]
        self.assertEqual(pods.used, 1000)
        self.assertEqual(pods.hard, 2000)


def _write_kubeconfig(root: Path, context: str = "production", user: str = "token: read-only-token", filename: str = "kubeconfig", insecure: bool = False) -> Path:
    kubeconfig = root / filename
    insecure_line = "    insecure-skip-tls-verify: true\n" if insecure else ""
    kubeconfig.write_text(
        f"""apiVersion: v1
kind: Config
clusters:
- name: production
  cluster:
    server: https://production.darksite.local:6443
{insecure_line}contexts:
- name: {context}
  context:
    cluster: production
    user: kcp-reader
current-context: {context}
users:
- name: kcp-reader
  user:
    {user}
""",
        encoding="utf-8",
    )
    return kubeconfig
