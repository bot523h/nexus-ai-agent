"""Wave 5 — the six creative engines are admitted, gated and refused honestly.

The property under test is not "the engines exist"; it is that the Studio
CommandBus is the *only* way to reach them, that reading is all they may do, and
that an engine the deployment did not compose is a typed refusal rather than a
synthetic PASS.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.creative.engine_ports import (
    EnginePortUnavailable,
    EngineVerificationReport,
    build_engine_ports,
)
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.capabilities import (
    ENGINE_PORT_ATTRIBUTES,
    CapabilityRegistry,
    OperationContext,
    build_wave1_registry,
)
from nexus_ai_agent.creative.studio.engine_capabilities import (
    ENGINE_CAPABILITIES,
    EngineNotComposedError,
    register_engine_operations,
    verify_operation_id,
)
from nexus_ai_agent.creative.studio.models import (
    AuthorizationError,
    CapabilityError,
    ExecutionPolicyError,
    PermissionLevel,
    Project,
    Timeline,
    TypedCommand,
    new_project,
)

PROJECT = "engine-cap-project"


def _project() -> Project:
    return new_project(PROJECT, "engines", Timeline(timeline_id="engine-tl", duration_us=0))


def _command(operation: str, **input_data: Any) -> TypedCommand:
    return TypedCommand(command_id=f"cmd-{operation}", operation=operation, input=dict(input_data))


def _registry(extra: CapabilityRegistry | None = None) -> CapabilityRegistry:
    return extra if extra is not None else register_engine_operations(build_wave1_registry())


class _FakeVerifier:
    """A port double: records the call and answers with a fixed verdict."""

    def __init__(self, verified: bool = True) -> None:
        self.verified = verified
        self.calls: list[tuple[str, str | None]] = []

    def verify_media(
        self, artifact_path: str, *, expected_sha256: str | None = None, **parameters: Any
    ) -> EngineVerificationReport:
        self.calls.append((artifact_path, expected_sha256))
        return EngineVerificationReport(engine="double", verified=self.verified, detail="double")


def test_all_six_engines_are_registered_with_a_distinct_port_attribute() -> None:
    assert set(ENGINE_PORT_ATTRIBUTES) == {
        "omega",
        "veritas",
        "chronos",
        "atlas",
        "morpheus",
        "hyperion",
    }
    assert len(set(ENGINE_PORT_ATTRIBUTES.values())) == 6
    for attribute in ENGINE_PORT_ATTRIBUTES.values():
        assert attribute in OperationContext.__dataclass_fields__, attribute
    assert len(ENGINE_CAPABILITIES) == 6


def test_registration_adds_one_read_only_operation_per_engine() -> None:
    registry = _registry()
    for engine in ENGINE_PORT_ATTRIBUTES:
        spec = registry.get_spec(verify_operation_id(engine))
        assert spec.permission_level is PermissionLevel.IMMEDIATE, engine
        assert spec.deterministic is True
        assert spec.execution_modes == ("local",)
        assert spec.input_model.model_config.get("extra") == "forbid"


def test_uncomposed_engine_is_a_typed_refusal_not_a_pass() -> None:
    bus = CommandBus(
        state=_project(),
        registry=_registry(),
        allow_experimental=True,
    )
    with pytest.raises(EngineNotComposedError) as excinfo:
        bus.dispatch(_command(verify_operation_id("atlas"), artifact_path="/tmp/x.mp4"))
    assert "not composed" in str(excinfo.value)
    assert isinstance(excinfo.value, CapabilityError)
    # The refusal must not have advanced the project state.
    assert bus.state_revision == _project().state_revision


def test_composed_engine_verifies_and_leaves_state_untouched() -> None:
    verifier = _FakeVerifier()
    bus = CommandBus(
        state=_project(),
        registry=_registry(),
        allow_experimental=True,
        engine_ports={"atlas_engine": verifier},
    )
    before = bus.state_hash
    result = bus.dispatch(
        _command(verify_operation_id("atlas"), artifact_path="/tmp/x.mp4", expected_sha256="a" * 64)
    )
    assert result.output["verified"] is True
    assert result.output["engine"] == "atlas"
    assert verifier.calls == [("/tmp/x.mp4", "a" * 64)]
    assert bus.state_hash == before, "a verification is read-only"


def test_an_engine_verdict_of_false_is_reported_not_raised() -> None:
    """A refused artifact is a *result*; only an absent engine is an error."""
    bus = CommandBus(
        state=_project(),
        registry=_registry(),
        allow_experimental=True,
        engine_ports={"morpheus_engine": _FakeVerifier(verified=False)},
    )
    result = bus.dispatch(_command(verify_operation_id("morpheus"), artifact_path="/tmp/x.mp4"))
    assert result.output["verified"] is False


def test_the_capability_requires_the_experimental_opt_in() -> None:
    bus = CommandBus(
        state=_project(),
        registry=_registry(),
        allow_experimental=False,
        engine_ports={"veritas_engine": _FakeVerifier()},
    )
    with pytest.raises(ExecutionPolicyError):
        bus.dispatch(_command(verify_operation_id("veritas"), artifact_path="/tmp/x.mp4"))


def test_unknown_operation_still_fails_closed() -> None:
    bus = CommandBus(state=_project(), registry=_registry(), allow_experimental=True)
    with pytest.raises(Exception) as excinfo:
        bus.dispatch(_command("engine.not_an_engine.verify_media", artifact_path="/tmp/x.mp4"))
    assert "unknown operation" in str(excinfo.value) or "invalid" in str(excinfo.value)


def test_bus_rejects_an_unknown_engine_port_key() -> None:
    with pytest.raises(CapabilityError):
        CommandBus(
            state=_project(), registry=_registry(), engine_ports={"klingon_engine": object()}
        )


def test_a_port_without_a_verifier_is_refused() -> None:
    class _Executor:
        def execute(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("an executor must never be consulted as a verifier")

    bus = CommandBus(
        state=_project(),
        registry=_registry(),
        allow_experimental=True,
        engine_ports={"chronos_engine": _Executor()},
    )
    with pytest.raises(CapabilityError):
        bus.dispatch(_command(verify_operation_id("chronos"), artifact_path="/tmp/x.mp4"))


# --------------------------------------------------------------------------- #
# the composition-root adapters
# --------------------------------------------------------------------------- #


def test_build_engine_ports_composes_every_real_adapter() -> None:
    ports = build_engine_ports()
    assert set(ports) == set(ENGINE_PORT_ATTRIBUTES.values())
    for attribute, port in ports.items():
        assert callable(port.verify_media), attribute


def test_build_engine_ports_can_be_narrowed_to_one_engine() -> None:
    ports = build_engine_ports(["omega"])
    assert set(ports) == {"audio_engine"}


def test_omega_port_verifies_a_real_wav_digest(tmp_path: Path) -> None:
    """The OMEGA adapter re-measures the bytes; the caller's digest is a claim."""
    import wave

    import numpy as np

    wav = tmp_path / "tone.wav"
    samples = (np.zeros(4800, dtype=np.int16) + 100).astype(np.int16)
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48_000)
        handle.writeframes(samples.tobytes())
    digest = hashlib.sha256(wav.read_bytes()).hexdigest()

    port = build_engine_ports(["omega"])["audio_engine"]
    report = port.verify_media(str(wav), expected_sha256=digest)
    assert report.verified is True
    assert report.measured_sha256 == digest

    with pytest.raises(ValueError):
        port.verify_media(str(wav), expected_sha256="0" * 64)


def test_engine_adapters_refuse_unknown_parameters(tmp_path: Path) -> None:
    wav = tmp_path / "t.wav"
    wav.write_bytes(b"not-a-wav")
    port = build_engine_ports(["omega"])["audio_engine"]
    with pytest.raises(TypeError):
        port.verify_media(str(wav), unexpected="value")


def test_a_missing_artifact_is_a_typed_unavailability(tmp_path: Path) -> None:
    port = build_engine_ports(["omega"])["audio_engine"]
    with pytest.raises(EnginePortUnavailable):
        port.verify_media(str(tmp_path / "absent.wav"))


def test_authorizer_still_runs_before_the_engine() -> None:
    """An unauthorized command never reaches the engine port."""
    verifier = _FakeVerifier()

    class _Deny:
        def authorize(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AuthorizationError("denied")

    bus = CommandBus(
        state=_project(),
        registry=_registry(),
        allow_experimental=True,
        authorizer=_Deny(),
        engine_ports={"atlas_engine": verifier},
    )
    with pytest.raises((AuthorizationError, Exception)):
        bus.dispatch(_command(verify_operation_id("atlas"), artifact_path="/tmp/x.mp4"))
    assert verifier.calls == []
