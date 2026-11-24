"""Wave 10: typed composition contracts, license lattice, compiler, verifier, engine."""

from __future__ import annotations

import dataclasses
import math
from fractions import Fraction

import pytest

from nexus_ai_agent.creative.composition.compiler import (
    CompositionCompileError,
    CompositionCompiler,
    CompositionGraph,
    CompositionPlan,
    canonical_json,
    sha256_digest,
)
from nexus_ai_agent.creative.composition.contracts import (
    CapabilityContract,
    CompositionContractError,
    PortSpec,
    PortType,
)
from nexus_ai_agent.creative.composition.engine import (
    CompositionEngine,
    CompositionExecution,
    CompositionExecutionError,
)
from nexus_ai_agent.creative.composition.license_lattice import (
    LicenseCombination,
    LicenseError,
    LicenseRegime,
    combine_licenses,
    join,
)
from nexus_ai_agent.creative.composition.verifier import (
    CompositionExecutionReceipt,
    CompositionProof,
    CompositionVerificationError,
    CompositionVerifier,
    NodeOutput,
    float_digest,
)

F = Fraction
AUDIO = PortType("audio", sample_rate=48000, channels=1)
AUDIO_OUT = PortSpec("audio", AUDIO)
VIDEO = PortType("video", color_space="Rec709", shape=(1920, 1080))
VIDEO_OUT = PortSpec("video", VIDEO)


def source_contract(identifier: str = "src") -> CapabilityContract:
    return CapabilityContract(identifier, "1.0", (), (AUDIO_OUT,), 1.0, 0.0, {"gain": 1.0})


def gain_contract(identifier: str = "gain") -> CapabilityContract:
    return CapabilityContract(
        identifier, "1.0", (PortSpec("audio", AUDIO),), (AUDIO_OUT,), 1.0, 0.0
    )


def encode_contract(identifier: str = "enc") -> CapabilityContract:
    return CapabilityContract(
        identifier, "1.0", (PortSpec("audio", AUDIO),), (VIDEO_OUT,), 1.0, 0.0
    )


def make_plan() -> CompositionPlan:
    return CompositionCompiler().compile(
        (source_contract(), gain_contract(), encode_contract()),
        (
            ("src", "audio", "gain", "audio"),
            ("gain", "audio", "enc", "audio"),
        ),
        {
            "src": LicenseRegime.PERMISSIVE_MIT_APACHE,
            "gain": LicenseRegime.ATTRIBUTION_CC_BY,
            "enc": LicenseRegime.PUBLIC_DOMAIN,
        },
    )


# ---------------------------------------------------------------- contracts


def test_port_type_validation_and_subtyping() -> None:
    assert AUDIO.is_subtype_of(AUDIO) is True
    assert AUDIO.is_subtype_of(PortType("audio")) is True
    assert AUDIO.is_subtype_of(PortType("audio", sample_rate=44100)) is False
    assert AUDIO.is_subtype_of(PortType("video")) is False
    assert PortType("audio", shape=(2,)).is_subtype_of(PortType("audio", shape=(2,))) is True
    assert PortType("audio", shape=(2,)).is_subtype_of(PortType("audio", shape=(3,))) is False
    assert AUDIO.is_subtype_of(PortType("audio", channels=2)) is False
    assert (
        PortType("audio", color_space="sRGB").is_subtype_of(PortType("audio", color_space="Rec709"))
        is False
    )
    with pytest.raises(CompositionContractError):
        PortType("")
    with pytest.raises(CompositionContractError):
        PortType("audio", sample_rate=0)
    with pytest.raises(CompositionContractError):
        PortType("audio", sample_rate=True)  # type: ignore[arg-type]
    with pytest.raises(CompositionContractError):
        PortType("audio", channels=0)
    with pytest.raises(CompositionContractError):
        PortType("audio", color_space="")
    with pytest.raises(CompositionContractError):
        PortType("audio", shape=(0,))
    with pytest.raises(CompositionContractError):
        PortType("audio", shape=(True,))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AUDIO.is_subtype_of("audio")  # type: ignore[arg-type]


def test_port_spec_and_contract_validation() -> None:
    with pytest.raises(CompositionContractError):
        PortSpec("", AUDIO)
    with pytest.raises(TypeError):
        PortSpec("audio", "audio")  # type: ignore[arg-type]
    with pytest.raises(CompositionContractError):
        CapabilityContract("", "1.0", (), (AUDIO_OUT,), 1.0, 0.0)
    with pytest.raises(CompositionContractError):
        CapabilityContract("x", "", (), (AUDIO_OUT,), 1.0, 0.0)
    with pytest.raises(CompositionContractError):
        CapabilityContract("x", "1.0", (), (), 1.0, 0.0)
    with pytest.raises(CompositionContractError):
        CapabilityContract("x", "1.0", (AUDIO_OUT, AUDIO_OUT), (AUDIO_OUT,), 1.0, 0.0)
    with pytest.raises(CompositionContractError):
        CapabilityContract("x", "1.0", (), (AUDIO_OUT, AUDIO_OUT), 1.0, 0.0)
    with pytest.raises(TypeError):
        CapabilityContract("x", "1.0", ("audio",), (AUDIO_OUT,), 1.0, 0.0)  # type: ignore[arg-type]
    with pytest.raises(CompositionContractError):
        CapabilityContract("x", "1.0", (), (AUDIO_OUT,), 0.0, 0.0)
    with pytest.raises(CompositionContractError):
        CapabilityContract("x", "1.0", (), (AUDIO_OUT,), 1.0, -1.0)
    with pytest.raises(CompositionContractError):
        CapabilityContract("x", "1.0", (), (AUDIO_OUT,), 1.0, 0.0, {"p": math.nan})
    with pytest.raises(CompositionContractError):
        CapabilityContract("x", "1.0", (), (AUDIO_OUT,), 1.0, 0.0, {"p": None})  # type: ignore[dict-item]
    with pytest.raises(CompositionContractError):
        CapabilityContract("x", "1.0", (), (AUDIO_OUT,), 1.0, 0.0, {"": 1})


def test_contract_accessors_and_payload() -> None:
    contract = gain_contract()
    assert contract.input("audio").name == "audio"
    assert contract.output("audio").name == "audio"
    with pytest.raises(CompositionContractError):
        contract.input("missing")
    with pytest.raises(CompositionContractError):
        contract.output("missing")
    payload = contract.canonical_payload()
    assert payload["capability_id"] == "gain"
    assert payload["inputs"][0]["media"] == "audio"  # type: ignore[index]
    assert "parameters" in payload
    assert (
        CapabilityContract(
            "x", "1.0", (), (AUDIO_OUT,), 1.0, 0.0, {"b": True, "a": 2, "c": 1.5, "d": "s"}
        ).parameters["b"]
        is True
    )


# ---------------------------------------------------------------- licenses


def test_join_semilattice_axioms() -> None:
    regimes = list(LicenseRegime)
    for first in regimes:
        assert join(first, first) is first  # idempotent
        for second in regimes:
            assert join(first, second) is join(second, first)  # commutative
    for first in regimes:
        for second in regimes:
            for third in regimes:
                left = join(join(first, second), third)
                right = join(first, join(second, third))
                assert left is right  # associative
    for regime in regimes:
        assert join(LicenseRegime.PUBLIC_DOMAIN, regime) is regime
        assert (
            join(LicenseRegime.INCOMPATIBLE_CONFLICT, regime) is LicenseRegime.INCOMPATIBLE_CONFLICT
        )


def test_specific_joins_and_conflicts() -> None:
    assert (
        join(LicenseRegime.PERMISSIVE_MIT_APACHE, LicenseRegime.ATTRIBUTION_CC_BY)
        is LicenseRegime.ATTRIBUTION_CC_BY
    )
    assert (
        join(LicenseRegime.ATTRIBUTION_CC_BY, LicenseRegime.SHARE_ALIKE_COPYLEFT)
        is LicenseRegime.SHARE_ALIKE_COPYLEFT
    )
    assert (
        join(LicenseRegime.SHARE_ALIKE_COPYLEFT, LicenseRegime.NON_COMMERCIAL_ONLY)
        is LicenseRegime.INCOMPATIBLE_CONFLICT
    )
    assert (
        join(LicenseRegime.SHARE_ALIKE_COPYLEFT, LicenseRegime.PROPRIETARY_COMMERCIAL_RESTRICTED)
        is LicenseRegime.INCOMPATIBLE_CONFLICT
    )
    with pytest.raises(TypeError):
        join("PD", LicenseRegime.PUBLIC_DOMAIN)  # type: ignore[arg-type]


def test_combine_licenses() -> None:
    combination = combine_licenses(
        (LicenseRegime.PUBLIC_DOMAIN, LicenseRegime.PERMISSIVE_MIT_APACHE)
    )
    assert combination.supremum is LicenseRegime.PERMISSIVE_MIT_APACHE
    assert combination.compatible is True
    blocked = combine_licenses((LicenseRegime.NON_COMMERCIAL_ONLY,), commercial_intent=True)
    assert blocked.supremum is LicenseRegime.INCOMPATIBLE_CONFLICT
    assert blocked.compatible is False
    mixed = combine_licenses(
        (LicenseRegime.SHARE_ALIKE_COPYLEFT, LicenseRegime.PROPRIETARY_COMMERCIAL_RESTRICTED),
        commercial_intent=True,
    )
    assert mixed.supremum is LicenseRegime.INCOMPATIBLE_CONFLICT
    with pytest.raises(LicenseError):
        combine_licenses(())
    with pytest.raises(TypeError):
        combine_licenses(("PD",))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        combine_licenses((LicenseRegime.PUBLIC_DOMAIN,), commercial_intent=1)  # type: ignore[arg-type]
    with pytest.raises(LicenseError):
        LicenseCombination((), LicenseRegime.PUBLIC_DOMAIN, False)
    with pytest.raises(TypeError):
        LicenseCombination(("PD",), LicenseRegime.PUBLIC_DOMAIN, False)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        LicenseCombination((LicenseRegime.PUBLIC_DOMAIN,), "PD", False)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        LicenseCombination((LicenseRegime.PUBLIC_DOMAIN,), LicenseRegime.PUBLIC_DOMAIN, 1)  # type: ignore[arg-type]


# ---------------------------------------------------------------- compiler


def test_compile_plan_and_determinism() -> None:
    plan = make_plan()
    assert plan.order == ("src", "gain", "enc")
    assert len(plan.plan_digest) == 64
    assert set(plan.node_digests) == {"src", "gain", "enc"}
    assert plan.licenses.supremum is LicenseRegime.ATTRIBUTION_CC_BY
    assert plan.contract("gain").capability_id == "gain"
    with pytest.raises(CompositionCompileError):
        plan.contract("missing")
    assert make_plan().plan_digest == plan.plan_digest
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert len(sha256_digest({"b": 1, "a": 2})) == 64
    with pytest.raises(CompositionCompileError):
        canonical_json({"x": object()})
    with pytest.raises(CompositionCompileError):
        canonical_json({"x": math.nan})


def test_graph_validation_and_structure() -> None:
    graph = CompositionGraph(
        (source_contract(), gain_contract()),
        (("src", "audio", "gain", "audio"),),
    )
    assert graph.topological_order() == ("src", "gain")
    assert graph.incoming("gain") == {"audio": ("src", "audio")}
    assert graph.contract("src").capability_id == "src"
    with pytest.raises(CompositionCompileError):
        graph.contract("missing")
    with pytest.raises(CompositionCompileError):
        CompositionGraph((), ())
    with pytest.raises(TypeError):
        CompositionGraph(("x",), ())  # type: ignore[arg-type]
    with pytest.raises(CompositionCompileError):
        CompositionGraph((source_contract(), source_contract()), ())
    with pytest.raises(CompositionCompileError):
        CompositionGraph((source_contract(),), (("src", "audio", "missing", "audio"),))
    with pytest.raises(CompositionCompileError):
        CompositionGraph((source_contract(),), (("src", "audio", "src", "audio"),))
    with pytest.raises(CompositionCompileError):
        CompositionGraph((source_contract(), gain_contract()), (("src", "bad", "gain", "audio"),))
    with pytest.raises(CompositionCompileError):
        CompositionGraph((source_contract(),), (("src", "audio"),))  # type: ignore[arg-type]
    with pytest.raises(CompositionCompileError):
        CompositionGraph(
            (source_contract(), gain_contract()),
            (("src", "audio", "gain", "audio"), ("src", "audio", "gain", "audio")),
        )


def test_compile_rejects_bad_graphs_and_licenses() -> None:
    compiler = CompositionCompiler()
    with pytest.raises(CompositionCompileError):
        compiler.compile((), (), {})
    with pytest.raises(CompositionCompileError):
        compiler.compile(
            (source_contract(), source_contract()),
            (),
            {"src": LicenseRegime.PUBLIC_DOMAIN},
        )
    with pytest.raises(CompositionCompileError):
        compiler.compile(
            (source_contract(), gain_contract()),
            (("src", "audio", "gain", "audio"),),
            {"src": LicenseRegime.PUBLIC_DOMAIN},
        )
    with pytest.raises(CompositionCompileError):
        compiler.compile(
            (source_contract(), gain_contract()),
            (("src", "audio", "gain", "audio"),),
            {
                "src": LicenseRegime.NON_COMMERCIAL_ONLY,
                "gain": LicenseRegime.PROPRIETARY_COMMERCIAL_RESTRICTED,
            },
            commercial_intent=True,
        )
    # Wrong media type on the edge.
    video_source = CapabilityContract("vsrc", "1.0", (), (VIDEO_OUT,), 1.0, 0.0)
    with pytest.raises(CompositionCompileError):
        compiler.compile(
            (video_source, gain_contract()),
            (("vsrc", "video", "gain", "audio"),),
            {"vsrc": LicenseRegime.PUBLIC_DOMAIN, "gain": LicenseRegime.PUBLIC_DOMAIN},
        )
    # Two producers into one input port.
    with pytest.raises(CompositionCompileError):
        compiler.compile(
            (source_contract("a"), source_contract("b"), gain_contract()),
            (
                ("a", "audio", "gain", "audio"),
                ("b", "audio", "gain", "audio"),
            ),
            {
                "a": LicenseRegime.PUBLIC_DOMAIN,
                "b": LicenseRegime.PUBLIC_DOMAIN,
                "gain": LicenseRegime.PUBLIC_DOMAIN,
            },
        )
    # Cycle.
    with pytest.raises(CompositionCompileError):
        compiler.compile(
            (gain_contract("a"), gain_contract("b")),
            (("a", "audio", "b", "audio"), ("b", "audio", "a", "audio")),
            {"a": LicenseRegime.PUBLIC_DOMAIN, "b": LicenseRegime.PUBLIC_DOMAIN},
        )


def test_plan_and_graph_validation() -> None:
    plan = make_plan()
    with pytest.raises(CompositionCompileError):
        dataclasses.replace(plan, order=())
    with pytest.raises(CompositionCompileError):
        dataclasses.replace(plan, order=("src", "src", "gain", "enc"))
    with pytest.raises(CompositionCompileError):
        dataclasses.replace(plan, plan_digest="x")
    with pytest.raises(CompositionCompileError):
        dataclasses.replace(plan, node_digests={"src": "0" * 64})
    with pytest.raises(CompositionCompileError):
        dataclasses.replace(plan, node_digests={"src": "x", "gain": "0" * 64, "enc": "0" * 64})
    with pytest.raises(CompositionCompileError):
        dataclasses.replace(plan, contracts={"src": plan.contract("src")})
    with pytest.raises(TypeError):
        dataclasses.replace(plan, licenses="licenses")
    with pytest.raises(CompositionCompileError):
        CompositionGraph((source_contract(),), (("src", "audio"),))  # type: ignore[arg-type]


# ---------------------------------------------------------------- verifier / engine


def _outputs() -> tuple[NodeOutput, ...]:
    return (
        NodeOutput("src", "audio", (1.0, 2.0)),
        NodeOutput("gain", "audio", (2.0, 4.0)),
        NodeOutput("enc", "video", (5.0,)),
    )


def _receipt(plan: CompositionPlan) -> CompositionExecutionReceipt:
    return CompositionExecutionReceipt(
        plan_digest=plan.plan_digest,
        outputs=_outputs(),
        durations={"src": F(1, 2), "gain": F(1, 3), "enc": F(1, 6)},
        master_duration=F(1),
    )


def test_verifier_proof_and_float_digest() -> None:
    plan = make_plan()
    receipt = _receipt(plan)
    proof = CompositionVerifier().verify(plan, receipt)
    assert proof.verified is True
    assert proof.plan_digest == plan.plan_digest
    assert proof.duration_sum == F(1)
    assert proof.energy_total == pytest.approx(50.0)
    assert len(proof.merkle_root) == 64
    assert float_digest((1.0, 2.0)) == float_digest((1.0, 2.0))
    assert float_digest((1.0, 2.0)) != float_digest((2.0, 1.0))
    assert CompositionVerifier().verify(plan, _receipt(plan)).merkle_root == proof.merkle_root


def test_verifier_rejections() -> None:
    plan = make_plan()
    receipt = _receipt(plan)
    verifier = CompositionVerifier()
    with pytest.raises(TypeError):
        verifier.verify("plan", receipt)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        verifier.verify(plan, "receipt")  # type: ignore[arg-type]
    with pytest.raises(CompositionVerificationError):
        verifier.verify(plan, receipt, max_energy=0.0)
    with pytest.raises(CompositionVerificationError):
        verifier.verify(plan, receipt, max_energy=1.0)
    with pytest.raises(CompositionVerificationError):
        verifier.verify(plan, dataclasses.replace(receipt, plan_digest="1" * 64))
    with pytest.raises(CompositionVerificationError):
        verifier.verify(plan, dataclasses.replace(receipt, outputs=_outputs()[:2]))
    with pytest.raises(CompositionVerificationError):
        verifier.verify(
            plan,
            dataclasses.replace(receipt, outputs=_outputs() + (NodeOutput("x", "y", (1.0,)),)),
        )
    with pytest.raises(CompositionVerificationError):
        verifier.verify(
            plan,
            dataclasses.replace(receipt, durations={"src": F(1, 2), "gain": F(1, 3)}),
        )
    with pytest.raises(CompositionVerificationError):
        verifier.verify(plan, dataclasses.replace(receipt, master_duration=F(2)))


def test_receipt_and_proof_validation() -> None:
    with pytest.raises(CompositionVerificationError):
        CompositionExecutionReceipt("short", (), {}, F(0))
    with pytest.raises(TypeError):
        CompositionExecutionReceipt("0" * 64, (object(),), {}, F(0))  # type: ignore[arg-type]
    with pytest.raises(CompositionVerificationError):
        CompositionExecutionReceipt(
            "0" * 64,
            (NodeOutput("x", "a", (1.0,)), NodeOutput("x", "a", (2.0,))),
            {"x": F(1)},
            F(1),
        )
    with pytest.raises(TypeError):
        CompositionExecutionReceipt("0" * 64, (), {"x": 1.0}, F(0))  # type: ignore[dict-item]
    with pytest.raises(CompositionVerificationError):
        CompositionExecutionReceipt("0" * 64, (), {"x": F(-1)}, F(0))
    with pytest.raises(CompositionVerificationError):
        CompositionExecutionReceipt("0" * 64, (), {"": F(0)}, F(0))
    with pytest.raises(TypeError):
        CompositionExecutionReceipt("0" * 64, (), {}, 1.0)  # type: ignore[arg-type]
    with pytest.raises(CompositionVerificationError):
        CompositionExecutionReceipt("0" * 64, (), {}, F(-1))
    with pytest.raises(CompositionVerificationError):
        NodeOutput("", "a", (1.0,))
    with pytest.raises(CompositionVerificationError):
        NodeOutput("x", "", (1.0,))
    with pytest.raises(CompositionVerificationError):
        NodeOutput("x", "a", ())
    with pytest.raises(CompositionVerificationError):
        NodeOutput("x", "a", (math.nan,))
    with pytest.raises(CompositionVerificationError):
        NodeOutput("x", "a", (True,))  # type: ignore[arg-type]
    with pytest.raises(CompositionVerificationError):
        CompositionProof("short", "0" * 64, LicenseRegime.PUBLIC_DOMAIN, F(1), F(1), 0.0, True)
    with pytest.raises(TypeError):
        CompositionProof("0" * 64, "0" * 64, "PD", F(1), F(1), 0.0, True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CompositionProof("0" * 64, "0" * 64, LicenseRegime.PUBLIC_DOMAIN, 1.0, F(1), 0.0, True)  # type: ignore[arg-type]
    with pytest.raises(CompositionVerificationError):
        CompositionProof("0" * 64, "0" * 64, LicenseRegime.PUBLIC_DOMAIN, F(1), F(2), 0.0, True)
    with pytest.raises(CompositionVerificationError):
        CompositionProof("0" * 64, "0" * 64, LicenseRegime.PUBLIC_DOMAIN, F(1), F(1), -1.0, True)
    with pytest.raises(CompositionVerificationError):
        CompositionProof("0" * 64, "0" * 64, LicenseRegime.PUBLIC_DOMAIN, F(1), F(1), 0.0, False)


def test_engine_executes_and_seals() -> None:
    def runner(
        contract: CapabilityContract, inputs: dict[str, tuple[float, ...]]
    ) -> dict[str, tuple[float, ...]]:
        if contract.capability_id == "src":
            return {"audio": (1.0, 2.0)}
        if contract.capability_id == "gain":
            return {"audio": tuple(2.0 * value for value in inputs["audio"])}
        return {"video": (5.0,)}

    engine = CompositionEngine()
    execution = engine.execute(
        (source_contract(), gain_contract(), encode_contract()),
        (
            ("src", "audio", "gain", "audio"),
            ("gain", "audio", "enc", "audio"),
        ),
        {
            "src": LicenseRegime.PERMISSIVE_MIT_APACHE,
            "gain": LicenseRegime.ATTRIBUTION_CC_BY,
            "enc": LicenseRegime.PUBLIC_DOMAIN,
        },
        runner,
        {"src": F(1, 2), "gain": F(1, 3), "enc": F(1, 6)},
    )
    assert isinstance(execution, CompositionExecution)
    assert [output.node_id for output in execution.receipt.outputs] == ["src", "gain", "enc"]
    assert execution.proof.verified is True
    assert execution.proof.energy_total == pytest.approx(50.0)
    assert execution.plan.order == ("src", "gain", "enc")


def test_engine_rejections() -> None:
    def bad_runner(
        contract: CapabilityContract, inputs: dict[str, tuple[float, ...]]
    ) -> dict[str, tuple[float, ...]]:
        return {"wrong": (1.0,)}

    def nan_runner(
        contract: CapabilityContract, inputs: dict[str, tuple[float, ...]]
    ) -> dict[str, tuple[float, ...]]:
        return {"audio": (math.nan,)} if contract.capability_id != "enc" else {"video": (1.0,)}

    engine = CompositionEngine()
    contracts = (source_contract(), gain_contract(), encode_contract())
    edges = (("src", "audio", "gain", "audio"), ("gain", "audio", "enc", "audio"))
    licenses = {
        "src": LicenseRegime.PUBLIC_DOMAIN,
        "gain": LicenseRegime.PUBLIC_DOMAIN,
        "enc": LicenseRegime.PUBLIC_DOMAIN,
    }
    durations = {"src": F(1, 2), "gain": F(1, 3), "enc": F(1, 6)}
    with pytest.raises(CompositionExecutionError):
        engine.execute(contracts, edges, licenses, bad_runner, durations)
    with pytest.raises(CompositionExecutionError):
        engine.execute(contracts, edges, licenses, lambda c, i: {"audio": (1.0,)}, durations)
    with pytest.raises(CompositionVerificationError):
        engine.execute(contracts, edges, licenses, nan_runner, durations)
    with pytest.raises(CompositionExecutionError):
        engine.execute(
            contracts, edges, licenses, lambda c, i: {"audio": (1.0,), "video": (1.0,)}, durations
        )

    def valid_runner(
        contract: CapabilityContract, inputs: dict[str, tuple[float, ...]]
    ) -> dict[str, tuple[float, ...]]:
        if contract.capability_id == "enc":
            return {"video": (1.0,)}
        return {"audio": (1.0,)}

    with pytest.raises(CompositionExecutionError):
        engine.execute(contracts, edges, licenses, valid_runner, {"src": F(1)})
    with pytest.raises(TypeError):
        engine.execute(
            contracts, edges, licenses, valid_runner, {"src": 1.0, "gain": F(1), "enc": F(1)}
        )  # type: ignore[dict-item]
    with pytest.raises(CompositionExecutionError):
        engine.execute(
            contracts, edges, licenses, valid_runner, {"src": F(-1), "gain": F(1), "enc": F(1)}
        )
    with pytest.raises(TypeError):
        CompositionExecution("plan", "receipt", "proof")  # type: ignore[arg-type]
    plan = make_plan()
    receipt = _receipt(plan)
    proof = CompositionVerifier().verify(plan, receipt)
    with pytest.raises(TypeError):
        CompositionExecution(plan, "receipt", proof)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CompositionExecution(plan, receipt, "proof")  # type: ignore[arg-type]
