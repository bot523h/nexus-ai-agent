"""Independent composition verifier and cryptographic proof generation.

The verifier never trusts the engine: it re-checks plan digests, output
coverage, NaN/Inf absence, energy bounds, exact rational duration conservation,
and then seals every node input digest, capability version, license supremum
and output digest into a SHA-256 Merkle root.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType

from nexus_ai_agent.creative.composition.compiler import CompositionPlan, canonical_json
from nexus_ai_agent.creative.composition.license_lattice import LicenseRegime

__all__ = [
    "CompositionExecutionReceipt",
    "CompositionProof",
    "CompositionVerificationError",
    "CompositionVerifier",
    "NodeOutput",
    "float_digest",
]

_DIGEST_LENGTH = 64


class CompositionVerificationError(ValueError):
    """Raised when an execution receipt violates a conservation or proof law."""


@dataclass(frozen=True, slots=True)
class NodeOutput:
    node_id: str
    port: str
    samples: tuple[float, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or not self.node_id:
            raise CompositionVerificationError("node_id must be a non-empty string")
        if not isinstance(self.port, str) or not self.port:
            raise CompositionVerificationError("port must be a non-empty string")
        samples = tuple(self.samples)
        if not samples:
            raise CompositionVerificationError("node output must not be empty")
        for sample in samples:
            if isinstance(sample, bool) or not isinstance(sample, (int, float)):
                raise CompositionVerificationError("samples must be real numbers")
            if not math.isfinite(float(sample)):
                raise CompositionVerificationError("node outputs must be finite")
        object.__setattr__(self, "samples", tuple(float(sample) for sample in samples))


def float_digest(samples: tuple[float, ...]) -> str:
    """SHA-256 of canonical little-endian IEEE-754 float64 bytes."""

    buffer = struct.pack(f"<{len(samples)}d", *samples)
    return hashlib.sha256(buffer).hexdigest()


@dataclass(frozen=True, slots=True)
class CompositionExecutionReceipt:
    plan_digest: str
    outputs: tuple[NodeOutput, ...]
    durations: Mapping[str, Fraction]
    master_duration: Fraction

    def __post_init__(self) -> None:
        if not isinstance(self.plan_digest, str) or len(self.plan_digest) != _DIGEST_LENGTH:
            raise CompositionVerificationError("plan_digest must be a SHA-256 hex string")
        outputs = tuple(self.outputs)
        if any(not isinstance(output, NodeOutput) for output in outputs):
            raise TypeError("outputs must contain NodeOutput values")
        seen: set[tuple[str, str]] = set()
        for output in outputs:
            key = (output.node_id, output.port)
            if key in seen:
                raise CompositionVerificationError("duplicate node output")
            seen.add(key)
        durations: dict[str, Fraction] = {}
        for node, value in self.durations.items():
            if not isinstance(node, str) or not node:
                raise CompositionVerificationError("duration keys must be non-empty strings")
            if not isinstance(value, (Fraction, int)):
                raise TypeError("durations must be exact Fraction values")
            duration = Fraction(value)
            if duration < 0:
                raise CompositionVerificationError("durations must be non-negative")
            durations[node] = duration
        if not isinstance(self.master_duration, (Fraction, int)):
            raise TypeError("master_duration must be an exact Fraction")
        master = Fraction(self.master_duration)
        if master < 0:
            raise CompositionVerificationError("master_duration must be non-negative")
        object.__setattr__(self, "outputs", outputs)
        object.__setattr__(self, "durations", MappingProxyType(durations))
        object.__setattr__(self, "master_duration", master)


@dataclass(frozen=True, slots=True)
class CompositionProof:
    plan_digest: str
    merkle_root: str
    license_supremum: LicenseRegime
    duration_sum: Fraction
    master_duration: Fraction
    energy_total: float
    verified: bool

    def __post_init__(self) -> None:
        for name in ("plan_digest", "merkle_root"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != _DIGEST_LENGTH:
                raise CompositionVerificationError(f"{name} must be a SHA-256 hex string")
        if not isinstance(self.license_supremum, LicenseRegime):
            raise TypeError("license_supremum must be a LicenseRegime")
        for name in ("duration_sum", "master_duration"):
            value = getattr(self, name)
            if not isinstance(value, (Fraction, int)):
                raise TypeError(f"{name} must be an exact Fraction")
            object.__setattr__(self, name, Fraction(value))
        if self.duration_sum != self.master_duration:
            raise CompositionVerificationError("duration conservation failed")
        if not math.isfinite(self.energy_total) or self.energy_total < 0.0:
            raise CompositionVerificationError("energy_total must be finite and non-negative")
        if not isinstance(self.verified, bool) or not self.verified:
            raise CompositionVerificationError("proof must be verified")


class CompositionVerifier:
    """Stateless verifier; callers can run it in a separate trust domain."""

    def verify(
        self,
        plan: CompositionPlan,
        receipt: CompositionExecutionReceipt,
        *,
        max_energy: float = 1e12,
    ) -> CompositionProof:
        if not isinstance(plan, CompositionPlan):
            raise TypeError("plan must be a CompositionPlan")
        if not isinstance(receipt, CompositionExecutionReceipt):
            raise TypeError("receipt must be a CompositionExecutionReceipt")
        if not math.isfinite(max_energy) or max_energy <= 0.0:
            raise CompositionVerificationError("max_energy must be finite and positive")
        if receipt.plan_digest != plan.plan_digest:
            raise CompositionVerificationError("receipt does not belong to this plan")
        if not plan.licenses.compatible:  # pragma: no cover - compiler rejects these
            raise CompositionVerificationError("plan carries an incompatible license")
        expected = {
            (node, port.name)
            for node, contract in plan.contracts.items()
            for port in contract.outputs
        }
        actual = {(output.node_id, output.port) for output in receipt.outputs}
        if actual != expected:
            raise CompositionVerificationError("outputs do not cover exactly the plan")
        if set(receipt.durations) != set(plan.order):
            raise CompositionVerificationError("durations must cover exactly the plan nodes")
        duration_sum = sum(receipt.durations.values(), Fraction(0, 1))
        if duration_sum != receipt.master_duration:
            raise CompositionVerificationError("clip durations do not sum to the master")
        energy_total = 0.0
        for output in receipt.outputs:
            for sample in output.samples:
                energy_total += sample * sample
        if not math.isfinite(energy_total) or energy_total > max_energy:
            raise CompositionVerificationError("output energy exceeds the conservation bound")

        digests = {
            (output.node_id, output.port): float_digest(output.samples)
            for output in receipt.outputs
        }
        incoming: dict[str, list[str]] = {node: [] for node in plan.order}
        for source, output_port, target, input_port in plan.edges:
            incoming[target].append(f"{input_port}:{digests[(source, output_port)]}")
        leaves = []
        for node in plan.order:
            contract = plan.contract(node)
            payload = {
                "node": node,
                "capability_digest": plan.node_digests[node],
                "capability_version": contract.version,
                "license_supremum": plan.licenses.supremum.value,
                "inputs": sorted(incoming[node]),
                "outputs": sorted(
                    f"{port.name}:{digests[(node, port.name)]}" for port in contract.outputs
                ),
            }
            leaves.append(hashlib.sha256(canonical_json(payload).encode("utf-8")).digest())
        root = self._merkle_root(leaves)
        return CompositionProof(
            plan_digest=plan.plan_digest,
            merkle_root=root,
            license_supremum=plan.licenses.supremum,
            duration_sum=duration_sum,
            master_duration=receipt.master_duration,
            energy_total=energy_total,
            verified=True,
        )

    @staticmethod
    def _merkle_root(leaves: list[bytes]) -> str:
        level = list(leaves)
        while len(level) > 1:
            next_level: list[bytes] = []
            for index in range(0, len(level), 2):
                left = level[index]
                right = level[index + 1] if index + 1 < len(level) else left
                next_level.append(hashlib.sha256(left + right).digest())
            level = next_level
        return level[0].hex()
