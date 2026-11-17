"""Wave 5 — the six NAGAR creative engines as first-class Studio capabilities.

The Studio command bus is the one admission gate for creative work: it validates
the envelope, the precondition revisions, the reference scope and the execution
policy *before* anything is enqueued. Until this module existed, the six
creative engines (OMEGA, VERITAS, CHRONOS, ATLAS, MORPHEUS, HYPERION) could only
be reached by calling their modules directly from a worker handler — outside
that gate, with no registered capability, no permission level and no honest
refusal when the engine was not composed in a deployment.

Design:

* **One canonical capability per engine**, holding the engine's read-only
  ``verify_media`` operation. Verification is policy **A** (immediate,
  non-destructive): it observes bytes and returns a verdict, it never writes.
  Anything that produces new media stays a render-lane operation whose artifacts
  must pass ``reference.verify``-style post-disk verification — a capability is
  never allowed to be the thing that both writes and vouches for a file.
* **The engine is a port, not an import.** The handler reads its engine from
  :class:`~nexus_ai_agent.creative.studio.capabilities.OperationContext`
  (``veritas_engine`` …), which the composition root injects. The Studio core
  therefore imports no pack, engine or adapter (a fitness test pins that), and a
  test can hand in a double without patching a module global.
* **Uncomposed is a typed refusal, never a fake pass.** When the port is
  ``None`` the handler raises :class:`EngineNotComposedError` (a
  :class:`~nexus_ai_agent.creative.studio.models.CapabilityError`). A caller
  cannot mistake "this deployment has no ATLAS engine" for "ATLAS verified the
  bytes".
* **All six operations are EXPERIMENTAL opt-ins.** They are added to the
  server-controlled ``EXPERIMENTAL_OPT_IN_OPERATIONS`` set in
  :mod:`nexus_ai_agent.creative.render_jobs`, so the bus's lifecycle gate refuses
  them unless the composition root opted in — a queue row can never opt itself
  in.

The engines and their verification entry points:

=========  ===============================  ====================================
engine     port attribute                   verification authority
=========  ===============================  ====================================
OMEGA      ``audio_engine``                 ``creative.audio_engine.verify``
VERITAS    ``veritas_engine``               ``verify_compiled_veritas_bundle…``
CHRONOS    ``chronos_engine``               ``verify_staged_chronos_artifacts``
ATLAS      ``atlas_engine``                 ``verify_prometheus_playground…``
MORPHEUS   ``morpheus_engine``              ``verify_morpheus_bundle_on_disk``
HYPERION   ``hyperion_engine``              speech-cinema vault/color contract
=========  ===============================  ====================================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.studio.capabilities import (
    ENGINE_PORT_ATTRIBUTES,
    CapabilityRegistry,
    OperationContext,
    OperationOutcome,
    OperationSpec,
)
from nexus_ai_agent.creative.studio.models import (
    CapabilityError,
    PermissionLevel,
    Project,
)

#: The one reviewed method name every engine port must expose to be composable
#: for verification. The port is free to be any object; this is the only shape
#: the capability layer requires of it.
VERIFY_METHOD: Final = "verify_media"

#: Source-level declaration of every registered operation id (Wave 5).
#:
#: The truth engine (``nexus_ai_agent.nagar.sources``) resolves declarations by
#: *importing* the operation modules and reading ``OPERATION_*`` string
#: constants, so each id below is the declaration that keeps
#: ``declared == registered`` true. They are literals on purpose: a
#: concatenation would hide the id from a reader grepping for it, and
#: ``tests/architecture/test_engine_capability_boundary.py`` pins that these
#: constants and :func:`verify_operation_id` never drift apart.
OPERATION_OMEGA_VERIFY_MEDIA = "audio.omega_verify_media"
OPERATION_VERITAS_VERIFY_MEDIA = "cinema.veritas_verify_media"
OPERATION_CHRONOS_VERIFY_MEDIA = "cinema.chronos_verify_media"
OPERATION_ATLAS_VERIFY_MEDIA = "cinema.atlas_verify_media"
OPERATION_MORPHEUS_VERIFY_MEDIA = "cinema.morpheus_verify_media"
OPERATION_HYPERION_VERIFY_MEDIA = "cinema.hyperion_verify_media"

EngineName = Literal["omega", "veritas", "chronos", "atlas", "morpheus", "hyperion"]


class EngineNotComposedError(CapabilityError):
    """The deployment did not compose this engine, so it cannot be consulted."""

    def __init__(self, engine: str, attribute: str) -> None:
        super().__init__(
            f"engine '{engine}' is not composed in this deployment "
            f"(OperationContext.{attribute} is None); refusing to report a verdict "
            "the engine never produced"
        )
        self.engine = engine
        self.attribute = attribute


class EngineVerifyInput(BaseModel):
    """Input for an engine verification: the bytes, and what they should be."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Workspace-relative or absolute path to the published artifact.
    artifact_path: str = Field(min_length=1)
    #: The digest the caller claims for those bytes (``sha256:<hex>`` or bare hex).
    expected_sha256: str | None = None
    #: Free-form, engine-specific verification parameters (never an authority:
    #: an engine must re-measure whatever it is handed).
    parameters: dict[str, Any] = Field(default_factory=dict)


def _resolve_engine(context: OperationContext, engine: str) -> Any:
    attribute = ENGINE_PORT_ATTRIBUTES[engine]
    port = getattr(context, attribute, None)
    if port is None:
        raise EngineNotComposedError(engine, attribute)
    return port


def _verify_handler(engine: str) -> Any:
    """Build the pure handler for ``engine.<name>.verify_media``."""

    def handler(project: Project, context: OperationContext) -> OperationOutcome:
        port = _resolve_engine(context, engine)
        verifier = getattr(port, VERIFY_METHOD, None)
        if not callable(verifier):
            raise CapabilityError(
                f"engine '{engine}' exposes no callable {VERIFY_METHOD}(); the "
                "capability requires an independent verifier, not an executor"
            )
        artifact_path = str(context.input_data["artifact_path"])
        expected = context.input_data.get("expected_sha256")
        parameters = dict(context.input_data.get("parameters") or {})
        report = verifier(artifact_path, expected_sha256=expected, **parameters)
        verified = bool(getattr(report, "verified", False))
        return OperationOutcome(
            project=project,
            history=context.history,
            output={
                "engine": engine,
                "artifact_path": artifact_path,
                "verified": verified,
                "report": _report_payload(report),
            },
        )

    handler.__name__ = f"_verify_with_{engine}_engine"
    handler.__doc__ = (
        f"Verify published media through the {engine.upper()} engine port.\n\n"
        "Read-only: the project state and history are returned unchanged. A "
        "missing port is a typed refusal (see EngineNotComposedError), never a "
        "synthetic PASS."
    )
    return handler


def _report_payload(report: Any) -> dict[str, Any]:
    """Project an engine's report into a JSON-serializable dict."""
    to_dict = getattr(report, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        return payload if isinstance(payload, dict) else {"report": str(payload)}
    if isinstance(report, dict):
        return report
    return {"report": str(report)}


@dataclass(frozen=True)
class _EngineCapability:
    engine: EngineName
    domain: str
    description: str


#: The canonical registry entries, one per creative engine.
ENGINE_CAPABILITIES: Final[tuple[_EngineCapability, ...]] = (
    _EngineCapability(
        "omega",
        "audio",
        "OMEGA native audio engine: loudness, denoise, beat and staging verification.",
    ),
    _EngineCapability(
        "veritas",
        "cinema",
        "VERITAS OMNIA: fifteen-law physical-cinema verification of a compiled bundle.",
    ),
    _EngineCapability(
        "chronos",
        "cinema",
        "CHRONOS: continuity, identity-lock and staged-artifact verification.",
    ),
    _EngineCapability(
        "atlas",
        "cinema",
        "ATLAS/PROMETHEUS: quadrant playground bundle verification from disk.",
    ),
    _EngineCapability(
        "morpheus",
        "cinema",
        "MORPHEUS: cranial/3DGS character pipeline bundle verification.",
    ),
    _EngineCapability(
        "hyperion",
        "cinema",
        "HYPERION: speech-cinema vault, color contract and integrity verification.",
    ),
)


#: The registry requires ``operation_id`` to carry its domain prefix, so the
#: five cinema engines live under ``cinema.*`` and OMEGA (an audio engine) under
#: ``audio.*``. The mapping is data, not a convention a caller may guess.
ENGINE_DOMAINS: Final[dict[str, str]] = {
    "omega": "audio",
    "veritas": "cinema",
    "chronos": "cinema",
    "atlas": "cinema",
    "morpheus": "cinema",
    "hyperion": "cinema",
}


def verify_operation_id(engine: str) -> str:
    """The canonical operation id for an engine's verification capability."""
    return f"{ENGINE_DOMAINS[engine]}.{engine}_verify_media"


def register_engine_operations(registry: CapabilityRegistry) -> CapabilityRegistry:
    """Register the six engine verification capabilities into *registry*.

    Idempotent: registering twice replaces the identical spec (the registry's
    own duplicate guard rejects a *different* spec under the same id, which is
    the behaviour we want — a changed schema must bump the operation version).
    """
    for entry in ENGINE_CAPABILITIES:
        registry.register_domain(entry.domain, "NAGAR creative engines")
        registry.register_operation(
            entry.domain,
            entry.engine,
            OperationSpec(
                operation_id=verify_operation_id(entry.engine),
                description=(
                    f"Read-only media verification through the "
                    f"{entry.engine.upper()} engine; refuses honestly when the "
                    "engine is not composed."
                ),
                permission_level=PermissionLevel.IMMEDIATE,
                input_model=EngineVerifyInput,
                handler=_verify_handler(entry.engine),
                reference_fields=(),
                deterministic=True,
                # No pack speaks for an engine verification, so the pack gate
                # cannot hold it: the opt-in is declared on the capability and
                # enforced by the bus.
                requires_opt_in=True,
            ),
        )
    return registry


__all__ = [
    "ENGINE_CAPABILITIES",
    "ENGINE_DOMAINS",
    "OPERATION_ATLAS_VERIFY_MEDIA",
    "OPERATION_CHRONOS_VERIFY_MEDIA",
    "OPERATION_HYPERION_VERIFY_MEDIA",
    "OPERATION_MORPHEUS_VERIFY_MEDIA",
    "OPERATION_OMEGA_VERIFY_MEDIA",
    "OPERATION_VERITAS_VERIFY_MEDIA",
    "ENGINE_PORT_ATTRIBUTES",
    "VERIFY_METHOD",
    "EngineNotComposedError",
    "EngineVerifyInput",
    "register_engine_operations",
    "verify_operation_id",
]
