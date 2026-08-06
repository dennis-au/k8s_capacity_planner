from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from kubernetes import client, config as kube_config
from kubernetes.client import ApiClient
from yaml import safe_load

from kcp.models import ClusterSnapshot, NamespaceSummary, NodeSummary, QuotaUsage, ResourceValues, WorkloadSummary


@dataclass(frozen=True)
class KubeconfigDetails:
    context: str
    endpoint: str
    tls_server_name: str | None


class KubernetesCollector:
    def __init__(self, api_client: ApiClient) -> None:
        self.core = client.CoreV1Api(api_client)
        self.apps = client.AppsV1Api(api_client)
        self.autoscaling = client.AutoscalingV2Api(api_client)
        self.version = client.VersionApi(api_client)
        self.custom = client.CustomObjectsApi(api_client)

    @classmethod
    def from_kubeconfig(
        cls, kubeconfig_file: str, context: str | None = None, api_ip: str | None = None
    ) -> "KubernetesCollector":
        details = inspect_kubeconfig(kubeconfig_file, context, api_ip)
        api_client = kube_config.new_client_from_config(
            config_file=kubeconfig_file,
            context=details.context,
            persist_config=False,
        )
        configuration = api_client.configuration
        configuration.host = details.endpoint
        if details.tls_server_name:
            configuration.tls_server_name = details.tls_server_name
            if api_ip:
                configuration.assert_hostname = details.tls_server_name
        configuration.connection_pool_maxsize = 8
        return cls(ApiClient(configuration))

    def collect(self) -> ClusterSnapshot:
        version = self.version.get_code().git_version
        nodes = self.core.list_node(_request_timeout=20).items
        namespaces = self.core.list_namespace(_request_timeout=20).items
        pods = self.core.list_pod_for_all_namespaces(_request_timeout=30).items
        quotas = self.core.list_resource_quota_for_all_namespaces(_request_timeout=20).items
        limit_ranges = self.core.list_limit_range_for_all_namespaces(_request_timeout=20).items
        hpas = self.autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces(_request_timeout=20).items
        replica_sets = self.apps.list_replica_set_for_all_namespaces(_request_timeout=20).items
        events = self.core.list_event_for_all_namespaces(_request_timeout=20).items

        warnings: list[str] = []
        node_usage: dict[str, ResourceValues] = {}
        pod_usage: dict[tuple[str, str], ResourceValues] = {}
        metrics_available = True
        try:
            node_usage = _node_metrics(self.custom)
            pod_usage = _pod_metrics(self.custom)
        except Exception as exc:  # Metrics API is optional by design.
            metrics_available = False
            warnings.append(f"Metrics API unavailable: {type(exc).__name__}")

        replica_set_owners = _replica_set_owners(replica_sets)
        hpa_targets = _hpa_targets(hpas)
        pod_events = _pod_events(events)
        active_pods = [pod for pod in pods if _is_active_pod(pod)]
        node_requested: dict[str, ResourceValues] = defaultdict(ResourceValues)
        workloads: dict[tuple[str, str, str], _WorkloadAccumulator] = {}

        for pod in active_pods:
            requests = pod_resources(pod)
            limits = pod_resources(pod, "limits")
            namespace = pod.metadata.namespace or "default"
            pod_name = pod.metadata.name
            node_name = pod.spec.node_name
            if node_name:
                node_requested[node_name] = node_requested[node_name].add(requests)
            kind, name = _workload_owner(pod, replica_set_owners)
            key = (namespace, kind, name)
            accumulator = workloads.setdefault(key, _WorkloadAccumulator(namespace, kind, name))
            accumulator.add(
                requests=requests,
                limits=limits,
                usage=pod_usage.get((namespace, pod_name)),
                qos=pod_qos(pod),
                missing_requests=_has_missing_requests(pod),
                events=pod_events.get((namespace, pod_name), []),
            )

        node_summaries = [
            NodeSummary(
                name=node.metadata.name,
                allocatable=ResourceValues.from_quantities(node.status.allocatable),
                requested=node_requested.get(node.metadata.name, ResourceValues()),
                limits=_node_limits(active_pods, node.metadata.name),
                usage=node_usage.get(node.metadata.name, ResourceValues()),
                conditions=[
                    condition.type
                    for condition in node.status.conditions or []
                    if str(condition.status).lower() == "true"
                    and condition.type in {"MemoryPressure", "DiskPressure", "PIDPressure"}
                ],
            )
            for node in nodes
        ]
        namespace_summaries = _namespace_summaries(namespaces, quotas, limit_ranges)
        workload_summaries = [
            accumulator.to_summary(
                has_hpa=(accumulator.namespace, accumulator.kind, accumulator.name) in hpa_targets
            )
            for accumulator in workloads.values()
        ]
        return ClusterSnapshot(
            cluster_version=version,
            metrics_available=metrics_available,
            nodes=sorted(node_summaries, key=lambda node: node.name),
            namespaces=sorted(namespace_summaries, key=lambda namespace: namespace.name),
            workloads=sorted(workload_summaries, key=lambda workload: workload.identity),
            warnings=warnings,
        )

    def test_connection(self) -> str:
        version = self.version.get_code(_request_timeout=10)
        return str(version.git_version or "unknown Kubernetes version")


def inspect_kubeconfig(
    kubeconfig_file: str | Path, context: str | None = None, api_ip: str | None = None
) -> KubeconfigDetails:
    path = Path(kubeconfig_file)
    if not path.is_file():
        raise ValueError("Kubeconfig file must point to a readable mounted file.")
    try:
        contents = path.read_text(encoding="utf-8")
    except Exception as exc:
        raise ValueError("Kubeconfig file is invalid or unreadable.") from exc
    return inspect_kubeconfig_text(contents, context, api_ip)


def inspect_kubeconfig_text(
    contents: str, context: str | None = None, api_ip: str | None = None
) -> KubeconfigDetails:
    try:
        raw_config = safe_load(contents)
    except Exception as exc:
        raise ValueError("Kubeconfig file is invalid or unreadable.") from exc
    if not isinstance(raw_config, dict):
        raise ValueError("Kubeconfig file is invalid or unreadable.")
    selected_name = context or raw_config.get("current-context")
    if not isinstance(selected_name, str) or not selected_name:
        raise ValueError("Kubeconfig must define a current context or specify one explicitly.")
    context_data = _kubeconfig_entry(raw_config.get("contexts"), "context", selected_name)
    if context_data is None:
        raise ValueError(f"Kubeconfig context {selected_name!r} was not found.")
    cluster_name = context_data.get("cluster")
    user_name = context_data.get("user")
    if not isinstance(cluster_name, str) or not isinstance(user_name, str):
        raise ValueError("Kubeconfig context must name a cluster and a static credential user.")
    cluster_data = _kubeconfig_entry(raw_config.get("clusters"), "cluster", cluster_name)
    user_data = _kubeconfig_entry(raw_config.get("users"), "user", user_name)
    if cluster_data is None or user_data is None:
        raise ValueError("Kubeconfig context must reference an existing cluster and user.")
    if _enabled(cluster_data.get("insecure-skip-tls-verify")):
        raise ValueError("Kubeconfig must keep TLS verification enabled.")
    if cluster_data.get("proxy-url"):
        raise ValueError("Kubeconfig proxy-url is not supported.")
    if user_data.get("exec") or user_data.get("auth-provider"):
        raise ValueError("Kubeconfig exec or auth-provider credentials are not supported.")
    endpoint = _https_endpoint(cluster_data.get("server"))
    tls_server_name = _tls_server_name(cluster_data, endpoint)
    if api_ip:
        endpoint = _endpoint_for_ip(endpoint, api_ip)
    return KubeconfigDetails(context=selected_name, endpoint=endpoint, tls_server_name=tls_server_name)


def _kubeconfig_entry(entries: object, entry_type: str, name: str) -> dict[str, Any] | None:
    if not isinstance(entries, list):
        return None
    for item in entries:
        if not isinstance(item, dict) or item.get("name") != name:
            continue
        entry = item.get(entry_type)
        if isinstance(entry, dict):
            return entry
    return None


def _enabled(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _https_endpoint(value: object) -> str:
    endpoint = str(value or "").rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Kubeconfig cluster server must be a valid HTTPS endpoint.")
    if parsed.query or parsed.fragment:
        raise ValueError("Kubeconfig cluster server cannot include a query or fragment.")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Kubeconfig cluster server must include a valid port.") from exc
    return endpoint


def _tls_server_name(cluster_data: dict[str, Any], endpoint: str) -> str | None:
    configured_name = cluster_data.get("tls-server-name")
    if isinstance(configured_name, str) and configured_name:
        return configured_name
    host = urlsplit(endpoint).hostname
    if not host:
        return None
    try:
        ip_address(host)
    except ValueError:
        return host
    return None


def _endpoint_for_ip(endpoint: str, api_ip: str) -> str:
    try:
        address = ip_address(api_ip.strip())
    except ValueError as exc:
        raise ValueError("Kubernetes API IP must be a valid IPv4 or IPv6 address.") from exc
    parsed = urlsplit(endpoint)
    host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


@dataclass
class _WorkloadAccumulator:
    namespace: str
    kind: str
    name: str
    replicas: int = 0
    requests: ResourceValues = ResourceValues()
    limits: ResourceValues = ResourceValues()
    usage: ResourceValues = ResourceValues()
    has_usage: bool = False
    qos_values: list[str] | None = None
    missing_requests: bool = False
    events: list[str] | None = None

    def __post_init__(self) -> None:
        self.qos_values = []
        self.events = []

    def add(
        self,
        requests: ResourceValues,
        limits: ResourceValues,
        usage: ResourceValues | None,
        qos: str,
        missing_requests: bool,
        events: list[str],
    ) -> None:
        self.replicas += 1
        self.requests = self.requests.add(requests)
        self.limits = self.limits.add(limits)
        if usage is not None:
            self.usage = self.usage.add(usage)
            self.has_usage = True
        self.qos_values.append(qos)
        self.missing_requests = self.missing_requests or missing_requests
        self.events.extend(events)

    def to_summary(self, has_hpa: bool) -> WorkloadSummary:
        qos = "Guaranteed"
        if "BestEffort" in self.qos_values:
            qos = "BestEffort"
        elif "Burstable" in self.qos_values:
            qos = "Burstable"
        return WorkloadSummary(
            namespace=self.namespace,
            kind=self.kind,
            name=self.name,
            replicas=self.replicas,
            requests=self.requests,
            limits=self.limits,
            usage=self.usage if self.has_usage else None,
            qos=qos,
            missing_requests=self.missing_requests,
            has_hpa=has_hpa,
            events=sorted(set(self.events)),
        )


def pod_resources(pod: client.V1Pod, field: str = "requests") -> ResourceValues:
    app_resources = ResourceValues()
    for container in pod.spec.containers or []:
        app_resources = app_resources.add(_container_resources(container, field))
    init_resources = ResourceValues()
    for container in pod.spec.init_containers or []:
        init_resources = init_resources.maximum(_container_resources(container, field))
    overhead = ResourceValues.from_quantities(getattr(pod.spec, "overhead", None))
    return app_resources.maximum(init_resources).add(overhead)


def pod_qos(pod: client.V1Pod) -> str:
    containers = [*(pod.spec.containers or []), *(pod.spec.init_containers or [])]
    if not containers:
        return "BestEffort"
    has_any = False
    guaranteed = True
    for container in containers:
        resources = container.resources or client.V1ResourceRequirements()
        requests = resources.requests or {}
        limits = resources.limits or {}
        if requests or limits:
            has_any = True
        for resource in ("cpu", "memory"):
            if resource not in requests or resource not in limits or str(requests[resource]) != str(limits[resource]):
                guaranteed = False
    if guaranteed:
        return "Guaranteed"
    return "Burstable" if has_any else "BestEffort"


def _container_resources(container: client.V1Container, field: str) -> ResourceValues:
    resources = container.resources or client.V1ResourceRequirements()
    return ResourceValues.from_quantities(getattr(resources, field, None))


def _has_missing_requests(pod: client.V1Pod) -> bool:
    for container in [*(pod.spec.containers or []), *(pod.spec.init_containers or [])]:
        requests = (container.resources.requests if container.resources else None) or {}
        if "cpu" not in requests or "memory" not in requests:
            return True
    return False


def _is_active_pod(pod: client.V1Pod) -> bool:
    return (pod.status.phase or "") not in {"Succeeded", "Failed"}


def _workload_owner(
    pod: client.V1Pod, replica_set_owners: dict[tuple[str, str], tuple[str, str]]
) -> tuple[str, str]:
    namespace = pod.metadata.namespace or "default"
    references = pod.metadata.owner_references or []
    owner = next((reference for reference in references if reference.controller), references[0] if references else None)
    if owner is None:
        return "Pod", pod.metadata.name
    if owner.kind == "ReplicaSet":
        return replica_set_owners.get((namespace, owner.name), (owner.kind, owner.name))
    return owner.kind, owner.name


def _replica_set_owners(replica_sets: list[client.V1ReplicaSet]) -> dict[tuple[str, str], tuple[str, str]]:
    result: dict[tuple[str, str], tuple[str, str]] = {}
    for replica_set in replica_sets:
        references = replica_set.metadata.owner_references or []
        owner = next((reference for reference in references if reference.controller), None)
        if owner:
            result[(replica_set.metadata.namespace or "default", replica_set.metadata.name)] = (owner.kind, owner.name)
    return result


def _hpa_targets(hpas: list[client.V2HorizontalPodAutoscaler]) -> set[tuple[str, str, str]]:
    return {
        (
            hpa.metadata.namespace or "default",
            hpa.spec.scale_target_ref.kind,
            hpa.spec.scale_target_ref.name,
        )
        for hpa in hpas
    }


def _node_limits(pods: list[client.V1Pod], node_name: str) -> ResourceValues:
    total = ResourceValues()
    for pod in pods:
        if pod.spec.node_name == node_name:
            total = total.add(pod_resources(pod, "limits"))
    return total


def _node_metrics(custom: client.CustomObjectsApi) -> dict[str, ResourceValues]:
    payload = custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes")
    return {
        item["metadata"]["name"]: ResourceValues.from_quantities(item.get("usage"))
        for item in payload.get("items", [])
    }


def _pod_metrics(custom: client.CustomObjectsApi) -> dict[tuple[str, str], ResourceValues]:
    payload = custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "pods")
    result: dict[tuple[str, str], ResourceValues] = {}
    for item in payload.get("items", []):
        usage = ResourceValues()
        for container in item.get("containers", []):
            usage = usage.add(ResourceValues.from_quantities(container.get("usage")))
        result[(item["metadata"].get("namespace", "default"), item["metadata"]["name"])] = usage
    return result


def _namespace_summaries(
    namespaces: list[client.V1Namespace],
    quotas: list[client.V1ResourceQuota],
    limit_ranges: list[client.V1LimitRange],
) -> list[NamespaceSummary]:
    quota_map: dict[str, dict[str, QuotaUsage]] = defaultdict(dict)
    for quota in quotas:
        hard = (quota.status.hard if quota.status else None) or {}
        used = (quota.status.used if quota.status else None) or {}
        for resource, hard_value in hard.items():
            if resource in used:
                quota_map[quota.metadata.namespace or "default"][resource] = QuotaUsage(
                    used=_quota_value(resource, used[resource]), hard=_quota_value(resource, hard_value)
                )
    limit_range_namespaces = {limit_range.metadata.namespace or "default" for limit_range in limit_ranges}
    return [
        NamespaceSummary(
            name=namespace.metadata.name,
            has_limit_range=namespace.metadata.name in limit_range_namespaces,
            quotas=quota_map.get(namespace.metadata.name, {}),
        )
        for namespace in namespaces
    ]


def _quota_value(resource: str, value: Any) -> int:
    if resource.endswith(".cpu") or resource == "cpu":
        return ResourceValues.from_quantities({"cpu": value}).cpu_millicores
    if "memory" in resource or "storage" in resource:
        return ResourceValues.from_quantities({"memory": value}).memory_bytes
    return int(str(value))


def _pod_events(events: list[client.V1Event]) -> dict[tuple[str, str], list[str]]:
    result: dict[tuple[str, str], list[str]] = defaultdict(list)
    for event in events:
        involved = event.involved_object
        if involved and involved.kind == "Pod" and event.type == "Warning":
            result[(event.metadata.namespace or "default", involved.name)].append(event.reason or "Warning")
    return result
