from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any


_BINARY_UNITS = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
}
_DECIMAL_UNITS = {"k": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "P": 1000**5, "E": 1000**6}
_QUANTITY = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))(n|u|m|Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E)?$")


@dataclass(frozen=True)
class ResourceValues:
    cpu_millicores: int = 0
    memory_bytes: int = 0
    ephemeral_storage_bytes: int = 0

    @classmethod
    def from_quantities(cls, quantities: dict[str, Any] | None) -> "ResourceValues":
        quantities = quantities or {}
        return cls(
            cpu_millicores=_parse_cpu(quantities.get("cpu", 0)),
            memory_bytes=_parse_bytes(quantities.get("memory", 0)),
            ephemeral_storage_bytes=_parse_bytes(quantities.get("ephemeral-storage", 0)),
        )

    def add(self, other: "ResourceValues") -> "ResourceValues":
        return ResourceValues(
            cpu_millicores=self.cpu_millicores + other.cpu_millicores,
            memory_bytes=self.memory_bytes + other.memory_bytes,
            ephemeral_storage_bytes=self.ephemeral_storage_bytes + other.ephemeral_storage_bytes,
        )

    def maximum(self, other: "ResourceValues") -> "ResourceValues":
        return ResourceValues(
            cpu_millicores=max(self.cpu_millicores, other.cpu_millicores),
            memory_bytes=max(self.memory_bytes, other.memory_bytes),
            ephemeral_storage_bytes=max(self.ephemeral_storage_bytes, other.ephemeral_storage_bytes),
        )

    def ratios(self, capacity: "ResourceValues") -> dict[str, float]:
        return {
            "cpu": _ratio(self.cpu_millicores, capacity.cpu_millicores),
            "memory": _ratio(self.memory_bytes, capacity.memory_bytes),
            "ephemeral_storage": _ratio(self.ephemeral_storage_bytes, capacity.ephemeral_storage_bytes),
        }


@dataclass(frozen=True)
class QuotaUsage:
    used: int
    hard: int

    @property
    def ratio(self) -> float:
        return _ratio(self.used, self.hard)


@dataclass(frozen=True)
class NodeSummary:
    name: str
    allocatable: ResourceValues
    requested: ResourceValues
    limits: ResourceValues
    usage: ResourceValues = field(default_factory=ResourceValues)
    conditions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NamespaceSummary:
    name: str
    has_limit_range: bool
    quotas: dict[str, QuotaUsage] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkloadSummary:
    namespace: str
    kind: str
    name: str
    replicas: int
    requests: ResourceValues
    limits: ResourceValues
    usage: ResourceValues | None
    qos: str
    missing_requests: bool
    has_hpa: bool
    events: list[str] = field(default_factory=list)

    @property
    def identity(self) -> str:
        return f"{self.namespace}/{self.kind}/{self.name}"


@dataclass(frozen=True)
class ClusterSnapshot:
    cluster_version: str
    metrics_available: bool
    nodes: list[NodeSummary]
    namespaces: list[NamespaceSummary]
    workloads: list[WorkloadSummary]
    warnings: list[str] = field(default_factory=list)
    collected_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["collected_at"] = self.collected_at.isoformat()
        return payload


def _parse_cpu(value: Any) -> int:
    number, suffix = _parse_quantity(value)
    multiplier = {None: Decimal(1000), "m": Decimal(1), "u": Decimal("0.001"), "n": Decimal("0.000001")}.get(suffix)
    if multiplier is None:
        raise ValueError(f"invalid CPU quantity: {value}")
    return math.ceil(number * multiplier)


def _parse_bytes(value: Any) -> int:
    number, suffix = _parse_quantity(value)
    if suffix in {"n", "u", "m"}:
        multiplier = {"n": Decimal("0.000000001"), "u": Decimal("0.000001"), "m": Decimal("0.001")}[suffix]
    elif suffix in _BINARY_UNITS:
        multiplier = Decimal(_BINARY_UNITS[suffix])
    elif suffix in _DECIMAL_UNITS:
        multiplier = Decimal(_DECIMAL_UNITS[suffix])
    elif suffix is None:
        multiplier = Decimal(1)
    else:
        raise ValueError(f"invalid byte quantity: {value}")
    return math.ceil(number * multiplier)


def _parse_quantity(value: Any) -> tuple[Decimal, str | None]:
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)), None
    match = _QUANTITY.fullmatch(str(value).strip())
    if not match:
        raise ValueError(f"unsupported resource quantity: {value}")
    return Decimal(match.group(1)), match.group(2)


def _ratio(value: int, total: int) -> float:
    return value / total if total > 0 else 0.0
