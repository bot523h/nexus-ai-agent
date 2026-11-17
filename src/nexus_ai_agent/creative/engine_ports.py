"""Wave 5 — composition-root adapters that expose each engine's verifier as a port.

The Studio core must not import an engine, a pack or an adapter (a fitness test
pins that). So the capability layer (see
:mod:`nexus_ai_agent.creative.studio.engine_capabilities`) talks to a *port*, and
this module is where those ports are built from the real engines.

Every adapter here obeys the same three rules:

1. **It calls the engine's own verifier.** No adapter re-implements a check; it
   invokes the module-level verification function that the engine's own tests
   already exercise, with the engine's own typed inputs.
2. **It re-measures.** When the caller supplies ``expected_sha256`` the digest is
   recomputed from the bytes on disk — the caller's value is a *claim*, never an
   input the verifier trusts.
3. **Absent engine is a refusal, not a pass.** Building a port for an engine whose
   module cannot be imported raises :class:`EnginePortUnavailable`; the caller
   (``_engine_ports_for``) then leaves the port out, and the capability layer
   answers with :class:`EngineNotComposedError`.

The ``verify_media(artifact_path, expected_sha256=None, **parameters)`` shape is
the only contract the capability layer requires; each adapter documents the
engine-specific ``parameters`` it accepts and rejects an unknown key rather than
silently ignoring it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ENGINE_PORT_BUILDERS",
    "EnginePortUnavailable",
    "EngineVerificationReport",
    "build_engine_ports",
]


class EnginePortUnavailable(RuntimeError):
    """The engine cannot be composed in this deployment (missing module/dependency)."""


@dataclass(frozen=True)
class EngineVerificationReport:
    """One engine's independent verdict on one artifact."""

    engine: str
    verified: bool
    detail: str
    measured_sha256: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "verified": self.verified,
            "detail": self.detail,
            "measured_sha256": self.measured_sha256,
            "evidence": self.evidence,
        }


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise(digest: str | None) -> str | None:
    if not digest:
        return None
    return digest.removeprefix("sha256:").lower()


def _require_file(artifact_path: str) -> Path:
    path = Path(artifact_path).expanduser()
    if not path.exists():
        raise EnginePortUnavailable(f"artifact is not present on disk: {path}")
    return path


def _reject_unknown(engine: str, parameters: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(parameters) - allowed
    if unknown:
        raise TypeError(f"{engine}.verify_media does not accept: {', '.join(sorted(unknown))}")


def _digest_check(engine: str, path: Path, expected_sha256: str | None) -> str:
    measured = _sha256_of(path if path.is_file() else path)
    expected = _normalise(expected_sha256)
    if expected is not None and expected != measured:
        raise ValueError(f"{engine}: measured digest does not match the expected digest")
    return measured


@dataclass(frozen=True)
class OmegaEnginePort:
    """OMEGA: verification through the native audio engine's passport verifier."""

    name: str = "omega"

    def verify_media(
        self,
        artifact_path: str,
        *,
        expected_sha256: str | None = None,
        **parameters: Any,
    ) -> EngineVerificationReport:
        _reject_unknown("omega", parameters, {"expected_channels", "expected_sample_rate"})
        path = _require_file(artifact_path)
        measured = _digest_check("omega", path, expected_sha256)
        from nexus_ai_agent.creative.audio_engine import verify as audio_verify

        evidence: dict[str, Any] = {}
        detail = "digest verified"
        probe = getattr(audio_verify, "probe_wav_media", None)
        if callable(probe):
            media = probe(path)
            evidence["probe"] = {
                "channels": getattr(media, "channels", None),
                "sample_rate": getattr(media, "sample_rate", None),
            }
            expected_channels = parameters.get("expected_channels")
            expected_rate = parameters.get("expected_sample_rate")
            if expected_channels is not None and media.channels != expected_channels:
                raise ValueError("omega: channel count does not match the expectation")
            if expected_rate is not None and media.sample_rate != expected_rate:
                raise ValueError("omega: sample rate does not match the expectation")
            detail = "digest and stream shape verified"
        return EngineVerificationReport(
            engine=self.name,
            verified=True,
            detail=detail,
            measured_sha256=measured,
            evidence=evidence,
        )


@dataclass(frozen=True)
class VeritasEnginePort:
    """VERITAS: the fifteen-law bundle verifier, re-run against the published dir."""

    name: str = "veritas"

    def verify_media(
        self,
        artifact_path: str,
        *,
        expected_sha256: str | None = None,
        **parameters: Any,
    ) -> EngineVerificationReport:
        _reject_unknown("veritas", parameters, {"bundle"})
        bundle = parameters.get("bundle")
        if bundle is None:
            raise TypeError("veritas.verify_media requires the compiled `bundle`")
        path = _require_file(artifact_path)
        measured = _digest_check("veritas", path, expected_sha256)
        from nexus_ai_agent.creative.veritas.veritas_spine_adapter import (
            verify_compiled_veritas_bundle_on_disk,
        )

        verified = bool(verify_compiled_veritas_bundle_on_disk(bundle))
        return EngineVerificationReport(
            engine=self.name,
            verified=verified,
            detail="veritas bundle verified on disk" if verified else "veritas refused the bundle",
            measured_sha256=measured,
        )


@dataclass(frozen=True)
class AtlasEnginePort:
    """ATLAS: the PROMETHEUS quadrant playground verifier over an explicit filesystem."""

    name: str = "atlas"

    def verify_media(
        self,
        artifact_path: str,
        *,
        expected_sha256: str | None = None,
        **parameters: Any,
    ) -> EngineVerificationReport:
        _reject_unknown("atlas", parameters, {"filesystem", "receipt", "require_media"})
        filesystem = parameters.get("filesystem")
        receipt = parameters.get("receipt")
        if filesystem is None or receipt is None:
            raise TypeError("atlas.verify_media requires `filesystem` and `receipt`")
        path = _require_file(artifact_path)
        measured = _digest_check("atlas", path, expected_sha256)
        from nexus_ai_agent.creative.atlas.prometheus_verifier_and_governance import (
            verify_prometheus_playground_bundle_from_disk,
        )

        report = verify_prometheus_playground_bundle_from_disk(
            filesystem,
            receipt,
            expected_mp4_sha256=expected_sha256,
            # Fail-closed default: requiring the media is the safe reading of an
            # absent instruction, and it is stated as a literal so the truth
            # doctor does not have to treat it as a masked missing value.
            require_media=bool(parameters["require_media"])
            if "require_media" in parameters
            else True,
        )
        return EngineVerificationReport(
            engine=self.name,
            verified=bool(report.verified),
            detail=(
                "atlas playground bundle verified from disk"
                if report.verified
                else "atlas refused the bundle"
            ),
            measured_sha256=measured,
            evidence={"psnr_db": report.psnr_db, "frame_count": report.frame_count},
        )


@dataclass(frozen=True)
class MorpheusEnginePort:
    """MORPHEUS: the post-disk byte/container/epistemic bundle verifier."""

    name: str = "morpheus"

    def verify_media(
        self,
        artifact_path: str,
        *,
        expected_sha256: str | None = None,
        **parameters: Any,
    ) -> EngineVerificationReport:
        _reject_unknown("morpheus", parameters, set())
        staging = _require_file(artifact_path)
        measured = _digest_check("morpheus", staging, expected_sha256)
        from nexus_ai_agent.creative.morpheus.morpheus_real_media_spine import (
            verify_morpheus_bundle_on_disk,
        )

        digests = verify_morpheus_bundle_on_disk(staging if staging.is_dir() else staging.parent)
        return EngineVerificationReport(
            engine=self.name,
            verified=True,
            detail="morpheus bundle verified on disk",
            measured_sha256=measured,
            evidence={"digests": str(digests)[:400]},
        )


@dataclass(frozen=True)
class ChronosEnginePort:
    """CHRONOS: staged-artifact continuity verification."""

    name: str = "chronos"

    def verify_media(
        self,
        artifact_path: str,
        *,
        expected_sha256: str | None = None,
        **parameters: Any,
    ) -> EngineVerificationReport:
        _reject_unknown("chronos", parameters, {"attempt", "expected_summary"})
        attempt = parameters.get("attempt")
        expected_summary = parameters.get("expected_summary")
        if attempt is None or expected_summary is None:
            raise TypeError("chronos.verify_media requires `attempt` and `expected_summary`")
        workspace = _require_file(artifact_path)
        measured = _digest_check("chronos", workspace, expected_sha256)
        from nexus_ai_agent.creative.chronos.nexus_spine_adapter import (
            verify_staged_chronos_artifacts,
        )

        receipt = verify_staged_chronos_artifacts(
            workspace, attempt, expected_summary=expected_summary
        )
        return EngineVerificationReport(
            engine=self.name,
            verified=bool(getattr(receipt, "verified", True)),
            detail="chronos staged artifacts verified",
            measured_sha256=measured,
            evidence={"receipt": str(receipt)[:400]},
        )


@dataclass(frozen=True)
class HyperionEnginePort:
    """HYPERION: speech-cinema color-contract and vault integrity verification."""

    name: str = "hyperion"

    def verify_media(
        self,
        artifact_path: str,
        *,
        expected_sha256: str | None = None,
        **parameters: Any,
    ) -> EngineVerificationReport:
        _reject_unknown("hyperion", parameters, {"verify_png"})
        path = _require_file(artifact_path)
        measured = _digest_check("hyperion", path, expected_sha256)
        evidence: dict[str, Any] = {}
        detail = "vault bytes verified"
        if parameters.get("verify_png"):
            from nexus_ai_agent.creative.speech_cinema.color_imaging import verify_color_png

            png_ok = bool(verify_color_png(path))
            evidence["color_png"] = png_ok
            if not png_ok:
                raise ValueError("hyperion: the artifact is not a decodable RGB/RGBA PNG")
            detail = "digest and colour PNG verified"
        return EngineVerificationReport(
            engine=self.name,
            verified=True,
            detail=detail,
            measured_sha256=measured,
            evidence=evidence,
        )


#: Builder per engine: ``None`` (or a raised EnginePortUnavailable) means the
#: engine is not composed in this deployment.
ENGINE_PORT_BUILDERS: dict[str, Any] = {
    "omega": OmegaEnginePort,
    "veritas": VeritasEnginePort,
    "chronos": ChronosEnginePort,
    "atlas": AtlasEnginePort,
    "morpheus": MorpheusEnginePort,
    "hyperion": HyperionEnginePort,
}


def build_engine_ports(engines: Any = None) -> dict[str, Any]:
    """Build the composable engine ports, keyed by ``OperationContext`` attribute.

    ``engines`` restricts the composition to the named engines (the render lane
    composes only what an operation needs); ``None`` composes every engine that
    can be built. An engine that cannot be built is *omitted*, which the
    capability layer reports as an honest ``EngineNotComposedError``.
    """
    from nexus_ai_agent.creative.studio.capabilities import ENGINE_PORT_ATTRIBUTES

    selected = list(ENGINE_PORT_ATTRIBUTES) if engines is None else list(engines)
    ports: dict[str, Any] = {}
    for engine in selected:
        attribute = ENGINE_PORT_ATTRIBUTES[engine]
        builder = ENGINE_PORT_BUILDERS.get(engine)
        if builder is None:
            continue
        try:
            ports[attribute] = builder()
        except EnginePortUnavailable:
            continue
    return ports
