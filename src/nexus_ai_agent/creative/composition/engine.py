"""Composition engine: schedule, execute and seal a verified composition."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from nexus_ai_agent.creative.composition.compiler import (
    CompositionCompiler,
    CompositionPlan,
)
from nexus_ai_agent.creative.composition.contracts import CapabilityContract
from nexus_ai_agent.creative.composition.license_lattice import LicenseRegime
from nexus_ai_agent.creative.composition.verifier import (
    CompositionExecutionReceipt,
    CompositionProof,
    CompositionVerificationError,
    CompositionVerifier,
    NodeOutput,
)

__all__ = [
    "CapabilityRunner",
    "CompositionEngine",
    "CompositionExecution",
    "CompositionExecutionError",
]

CapabilityRunner = Callable[
    [CapabilityContract, Mapping[str, tuple[float, ...]]],
    Mapping[str, tuple[float, ...]],
]


class CompositionExecutionError(ValueError):
    """Raised when a runner violates the contract it was scheduled for."""


@dataclass(frozen=True, slots=True)
class CompositionExecution:
    plan: CompositionPlan
    receipt: CompositionExecutionReceipt
    proof: CompositionProof

    def __post_init__(self) -> None:
        if not isinstance(self.plan, CompositionPlan):
            raise TypeError("plan must be a CompositionPlan")
        if not isinstance(self.receipt, CompositionExecutionReceipt):
            raise TypeError("receipt must be a CompositionExecutionReceipt")
        if not isinstance(self.proof, CompositionProof):
            raise TypeError("proof must be a CompositionProof")


class CompositionEngine:
    """Execute a compiled plan topologically and produce a verified proof."""

    def __init__(
        self,
        compiler: CompositionCompiler | None = None,
        verifier: CompositionVerifier | None = None,
    ) -> None:
        self._compiler = compiler if compiler is not None else CompositionCompiler()
        self._verifier = verifier if verifier is not None else CompositionVerifier()

    def execute(
        self,
        contracts: Sequence[CapabilityContract],
        edges: Sequence[tuple[str, str, str, str]],
        licenses: Mapping[str, LicenseRegime],
        runner: CapabilityRunner,
        durations: Mapping[str, Fraction],
        *,
        commercial_intent: bool = False,
        max_energy: float = 1e12,
    ) -> CompositionExecution:
        plan = self._compiler.compile(
            contracts, edges, licenses, commercial_intent=commercial_intent
        )
        incoming: dict[str, dict[str, tuple[str, str]]] = {node: {} for node in plan.order}
        for source, output, target, input_port in plan.edges:
            incoming[target][input_port] = (source, output)
        produced: dict[tuple[str, str], tuple[float, ...]] = {}
        outputs: list[NodeOutput] = []
        for node in plan.order:
            contract = plan.contract(node)
            inputs = {
                input_port: produced[(source, output)]
                for input_port, (source, output) in incoming[node].items()
            }
            result = runner(contract, inputs)
            if set(result) != {port.name for port in contract.outputs}:
                raise CompositionExecutionError(
                    f"runner for {node!r} produced the wrong output ports"
                )
            for port_name, samples in result.items():
                vector = tuple(float(sample) for sample in samples)
                produced[(node, port_name)] = vector
                outputs.append(NodeOutput(node, port_name, vector))
        if set(durations) != set(plan.order):
            raise CompositionExecutionError("durations must cover exactly the plan nodes")
        exact: dict[str, Fraction] = {}
        for node, value in durations.items():
            if not isinstance(value, (Fraction, int)):
                raise TypeError("durations must be exact Fraction values")
            duration = Fraction(value)
            if duration < 0:
                raise CompositionExecutionError("durations must be non-negative")
            exact[node] = duration
        master = sum(exact.values(), Fraction(0, 1))
        receipt = CompositionExecutionReceipt(
            plan_digest=plan.plan_digest,
            outputs=tuple(outputs),
            durations=exact,
            master_duration=master,
        )
        proof = self._verifier.verify(plan, receipt, max_energy=max_energy)
        if proof.plan_digest != plan.plan_digest:  # pragma: no cover - verifier guarantees it
            raise CompositionVerificationError("proof does not match the plan")
        return CompositionExecution(plan=plan, receipt=receipt, proof=proof)
