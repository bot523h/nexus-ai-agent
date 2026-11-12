"""The evidence ladder, checked against a real execution (OMEGA-01 §lifecycle).

``studio.evidence`` promotes an operation only when a *signed* verifier
corroboration names the artifact hash the execution claimed.  These tests run the
real engine, build the bundle from what came back, and pin:

* the honest ceiling for one signing key is ``ARTIFACT_VERIFIED``;
* ``PRODUCTION_PROVEN`` needs two independent key ids and is refused with one;
* a refusal is ``CONTRADICTED`` and cannot be promoted past it;
* the lifecycle table's ``PACK_EVIDENCE`` equals what the measurement supports,
  so the table cannot drift ahead of the proof.
"""

from __future__ import annotations

import omega_media as media
import pytest

from nexus_ai_agent.creative.audio_engine.ed25519_sign import derive_public_key, key_id_for
from nexus_ai_agent.creative.audio_engine.execution import (
    AudioExecutionSettings,
    LocalDirectorySourceCatalog,
    NativeAudioEngine,
)
from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import FfmpegTranscoder, FfmpegUnavailableError
from nexus_ai_agent.creative.audio_engine.workspace import hash_file
from nexus_ai_agent.creative.studio.audio_engine import LoudnessRequest
from nexus_ai_agent.creative.studio.evidence import (
    EvidenceBundle,
    EvidenceStateError,
    OperationEvidenceState,
    VerifierCorroboration,
    assert_verified,
    assess_state,
)
from nexus_ai_agent.creative.studio.lifecycle import (
    PACK_EVIDENCE,
    PACK_LIFECYCLE,
    LifecycleState,
    pack_evidence_state,
    pack_lifecycle,
)

SEED = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
KEY_ID = key_id_for(derive_public_key(SEED))


@pytest.fixture(scope="module")
def normalized(tmp_path_factory: pytest.TempPathFactory):
    """A real, published, verified artifact plus the source hash it came from."""
    transcoder = FfmpegTranscoder()
    try:
        _ = transcoder.binary
    except FfmpegUnavailableError:
        pytest.skip("no FFmpeg binary available", allow_module_level=True)
    root = tmp_path_factory.mktemp("evidence")
    media_dir = root / "media"
    media_dir.mkdir()
    media.write_sine(media_dir / "tone.wav", amplitude=0.2)
    engine = NativeAudioEngine(
        catalog=LocalDirectorySourceCatalog(media_dir),
        settings=AudioExecutionSettings(
            workspace_root=root / "ws", signing_key=SEED, signer_key_id=KEY_ID
        ),
        transcoder=transcoder,
    )
    evidence = engine.normalize_loudness(
        LoudnessRequest(
            request_id="evidence-run",
            source_asset_id="tone",
            output_asset_id="tone_master",
            source_sha256=hash_file(media_dir / "tone.wav"),
            profile_id="streaming_master",
            target_lufs=-14.0,
            true_peak_dbtp=-1.0,
            target_lra_lu=11.0,
        )
    )
    return evidence


def _bundle(evidence, *, corroborations) -> EvidenceBundle:
    assert evidence.sha256
    return EvidenceBundle(
        operation="audio.normalize_loudness",
        implemented=True,
        executed=True,
        execution_evidence={
            "loudness_passed": evidence.loudness_passed,
            "integrated_lufs": evidence.integrated_lufs,
        },
        artifact_sha256=evidence.sha256,
        corroborations=tuple(corroborations),
    )


def test_one_signed_verifier_proves_the_artifact_but_not_production(normalized) -> None:
    sha = normalized.sha256
    bundle = _bundle(
        normalized,
        corroborations=[
            VerifierCorroboration(
                verifier_key_id=KEY_ID,
                artifact_sha256=sha,
                verified=True,
                signed=True,
                detail="independent re-decode after the final encode",
            )
        ],
    )
    assessment = assess_state(bundle)
    assert assessment.state is OperationEvidenceState.ARTIFACT_VERIFIED
    assert assessment.verified and assessment.attested
    assert "two independent verifier key ids" in " ".join(assessment.reasons)
    with pytest.raises(EvidenceStateError):
        assert_verified(bundle, at_least=OperationEvidenceState.PRODUCTION_PROVEN)


def test_two_independent_keys_plus_a_run_reach_production_proven(normalized) -> None:
    sha = normalized.sha256
    bundle = _bundle(
        normalized,
        corroborations=[
            VerifierCorroboration(
                verifier_key_id=KEY_ID, artifact_sha256=sha, verified=True, signed=True
            ),
            VerifierCorroboration(
                verifier_key_id="verifier-secondary",
                artifact_sha256=sha,
                verified=True,
                signed=True,
            ),
        ],
    ).model_copy(update={"production_runs": ("s01e01",)})
    assert assess_state(bundle).state is OperationEvidenceState.PRODUCTION_PROVEN


def test_the_same_key_twice_is_not_two_verifiers(normalized) -> None:
    sha = normalized.sha256
    corr = VerifierCorroboration(
        verifier_key_id=KEY_ID, artifact_sha256=sha, verified=True, signed=True
    )
    bundle = _bundle(normalized, corroborations=[corr, corr]).model_copy(
        update={"production_runs": ("s01e01",)}
    )
    assert assess_state(bundle).state is OperationEvidenceState.ARTIFACT_VERIFIED


def test_a_verifier_refusal_is_a_red_state(normalized) -> None:
    bundle = _bundle(
        normalized,
        corroborations=[
            VerifierCorroboration(
                verifier_key_id=KEY_ID,
                artifact_sha256=normalized.sha256,
                verified=False,
                code="decode_failed",
            )
        ],
    )
    assessment = assess_state(bundle)
    assert assessment.state is OperationEvidenceState.CONTRADICTED
    assert not assessment.verified
    with pytest.raises(EvidenceStateError, match="contradicts"):
        assert_verified(bundle, at_least=OperationEvidenceState.EXECUTION_PROVEN)


def test_corroborating_a_different_artifact_promotes_nothing(normalized) -> None:
    bundle = _bundle(
        normalized,
        corroborations=[
            VerifierCorroboration(
                verifier_key_id=KEY_ID,
                artifact_sha256="sha256:" + "0" * 64,
                verified=True,
                signed=True,
            )
        ],
    )
    assessment = assess_state(bundle)
    assert assessment.state is OperationEvidenceState.EXECUTION_PROVEN
    assert "different artifact hash" in " ".join(assessment.reasons)


def test_unsigned_verification_is_not_corroboration(normalized) -> None:
    bundle = _bundle(
        normalized,
        corroborations=[
            VerifierCorroboration(
                verifier_key_id=KEY_ID,
                artifact_sha256=normalized.sha256,
                verified=True,
                signed=False,
            )
        ],
    )
    assert assess_state(bundle).state is OperationEvidenceState.EXECUTION_PROVEN


def test_the_table_never_claims_more_than_the_proof(normalized) -> None:
    """``PACK_EVIDENCE`` is a claim about this checkout — keep it honest."""
    assert set(PACK_EVIDENCE) == set(PACK_LIFECYCLE)
    # No pack claims production proof: nothing in the canonical evidence carries
    # two independent verifier key ids.
    assert OperationEvidenceState.PRODUCTION_PROVEN not in PACK_EVIDENCE.values()
    # The audio pack reaches exactly what the real run above supports.
    assert pack_evidence_state("nexus.audio.studio") is OperationEvidenceState.ARTIFACT_VERIFIED
    # A pack whose handlers only edit state is not artifact-verified.
    for pack_id in ("nexus.vision.portrait", "nexus.vision.scene"):
        assert pack_evidence_state(pack_id) is OperationEvidenceState.IMPLEMENTED
    # And the ladder agrees with the engine's own verdict.
    assert normalized.verified is True
    assert normalized.loudness_passed is True


def test_an_executable_pack_carries_its_evidence_state() -> None:
    audio = pack_lifecycle("nexus.audio.studio")
    assert audio.state is LifecycleState.EXPERIMENTAL
    assert audio.evidence_state is OperationEvidenceState.ARTIFACT_VERIFIED
    assert pack_lifecycle("nexus.slideshow.compose").evidence_state is (
        OperationEvidenceState.EXECUTION_PROVEN
    )


def test_unknown_pack_has_proven_nothing() -> None:
    assert pack_evidence_state("nexus.made.up") is OperationEvidenceState.PLANNED


def test_stub_and_retired_cannot_be_promoted_by_existence() -> None:
    """Maturity gates execution; evidence never overrides a refusal."""
    from nexus_ai_agent.creative.studio.lifecycle import PackLifecycle, PackRequirementError

    record = PackLifecycle(
        pack_id="test.stub",
        state=LifecycleState.STUB,
        reason="probe",
        evidence_state=OperationEvidenceState.PRODUCTION_PROVEN,
    )
    with pytest.raises(PackRequirementError):
        record.require_executable(allow_experimental=True)
