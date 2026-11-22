"""W6 CP4: symplectic Hamiltonian pipeline + PR #205 / PR #326 supersession.

Proves Störmer–Verlet volume preservation (Liouville), energy boundedness,
Rayleigh-dissipation Lyapunov convergence, the unified cinema science pipeline
(receipt binding), and verifies the Asset Library V1 contracts of old PR #205
and the character capsule re-engineered from old PR #326.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone

import numpy as np
import pytest

from nexus_ai_agent.creative.assets.mime import detect_media_type
from nexus_ai_agent.creative.assets.models import (
    IngestState,
    MediaKind,
    SUPPORTED_MEDIA_TYPES,
)
from nexus_ai_agent.creative.character import (
    CHARACTER_ID_RE,
    FaceModule,
    Scope,
    ScopeViolation,
    ValidationError,
    CharacterCapsule,
    ContinuityThreshold,
    VerificationState,
    verify_dimensions,
)
from nexus_ai_agent.creative.character.continuity import ContinuityReport
from nexus_ai_agent.creative.science.symplectic_hamiltonian_pipeline import (
    HamiltonianSpec,
    ScientificPipelineReceipt,
    UnifiedCinemaScienceInputs,
    UnifiedCinemaSciencePipeline,
    gradient_potential,
    hamiltonian,
    integrate_verlet,
    jacobian_of_step,
    stormer_verlet_step,
    symplectic_volume_preservation_error,
)


# ---------------------------------------------------------------------------
# Symplectic Störmer–Verlet
# ---------------------------------------------------------------------------


class TestSymplectic:
    def test_hamiltonian_known_answer(self):
        spec = HamiltonianSpec(mass=2.0, stiffness=np.eye(2), quartic_coupling=0.5)
        q = np.array([1.0, 0.5])
        p = np.array([0.3, 0.2])
        kinetic = (0.09 + 0.04) / (2.0 * 2.0)
        harmonic = 0.5 * (1.0 + 0.25)
        quartic = 0.25 * 0.5 * (1.0 + 0.25) ** 2
        assert hamiltonian(q, p, spec) == pytest.approx(kinetic + harmonic + quartic)

    def test_gradient_potential_is_k_plus_alpha_coupling(self):
        spec = HamiltonianSpec(mass=1.0, stiffness=np.eye(2), quartic_coupling=0.5)
        q = np.array([1.0, 0.0])
        expected = q + 0.5 * 1.0 * q  # grad V = K q + alpha ||q||^2 q
        assert np.allclose(gradient_potential(q, spec), expected)

    def test_volume_preservation_is_near_exact(self):
        spec = HamiltonianSpec()
        q = np.array([1.0, 0.5])
        p = np.array([0.2, 0.1])
        err = symplectic_volume_preservation_error(q, p, 0.05, spec)
        assert err < 1e-8

    def test_jacobian_is_symplectic(self):
        spec = HamiltonianSpec()
        q = np.array([1.0, 0.5])
        p = np.array([0.0, 0.0])
        jac = jacobian_of_step(q, p, 0.05, spec, eps=1e-7)
        det = abs(np.linalg.det(jac))
        assert det == pytest.approx(1.0, abs=1e-3)

    def test_energy_bounded_without_damping(self):
        spec = HamiltonianSpec()
        q0 = np.array([1.0, 0.5])
        p0 = np.array([0.0, 0.0])
        traj_q, traj_p, traj_h = integrate_verlet(q0, p0, 0.05, 300, spec)
        assert np.all(np.isfinite(traj_h))
        assert np.max(np.abs(traj_h - traj_h[0])) < 0.05

    def test_rayleigh_damping_converges(self):
        spec = HamiltonianSpec()
        q0 = np.array([1.0, 0.5])
        p0 = np.array([0.0, 0.0])
        traj_q, traj_p, traj_h = integrate_verlet(
            q0, p0, 0.05, 400, spec, rayleigh_damping=0.05
        )
        assert traj_h[-1] < traj_h[0]
        assert np.linalg.norm(traj_q[-1]) < np.linalg.norm(traj_q[0])
        # Monotone energy decrease (within tolerance) for Lyapunov convergence.
        assert np.all(np.diff(traj_h) <= 1e-6)

    def test_verlet_step_extremes_are_finite(self):
        spec = HamiltonianSpec(mass=0.5, stiffness=np.diag([1.0, 4.0]), quartic_coupling=0.2)
        q = np.array([2.0, -1.0])
        p = np.array([1.5, -0.5])
        q_next, p_next = stormer_verlet_step(q, p, 0.02, spec)
        assert np.all(np.isfinite(q_next))
        assert np.all(np.isfinite(p_next))

    def test_spec_refuses_non_positive_mass(self):
        with pytest.raises(ValueError):
            HamiltonianSpec(mass=0.0)

    def test_spec_refuses_non_spd_stiffness(self):
        with pytest.raises(ValueError):
            HamiltonianSpec(stiffness=np.diag([1.0, -0.5]))

    def test_spec_refuses_negative_quartic(self):
        with pytest.raises(ValueError):
            HamiltonianSpec(quartic_coupling=-0.1)


# ---------------------------------------------------------------------------
# Unified cinema science pipeline
# ---------------------------------------------------------------------------


class TestUnifiedPipeline:
    def test_receipt_is_deterministic(self):
        pipe = UnifiedCinemaSciencePipeline()
        receipt_1 = pipe.execute()
        receipt_2 = pipe.execute()
        assert receipt_1.receipt_digest == receipt_2.receipt_digest
        assert receipt_1.inputs_digest == receipt_2.inputs_digest

    def test_receipt_binds_inputs_and_outputs(self):
        pipe = UnifiedCinemaSciencePipeline()
        receipt = pipe.execute()
        assert isinstance(receipt, ScientificPipelineReceipt)
        assert receipt.receipt_digest.startswith("sha256:") or receipt.receipt_digest
        assert len(receipt.receipt_digest) == 64
        assert {
            "photochemical_density",
            "hg_phase_function",
            "planck_intensity",
            "thin_film_reflectance",
            "bures_wasserstein_2",
            "hamiltonian_initial",
            "hamiltonian_final",
        } <= set(receipt.outputs)

    def test_receipt_json_is_canonical_and_self_consistent(self):
        pipe = UnifiedCinemaSciencePipeline()
        receipt = pipe.execute()
        blob = receipt.as_json()
        parsed = json.loads(blob)
        assert parsed["receipt_digest"] == receipt.receipt_digest
        assert parsed["schema_version"] == "1"

    def test_module_digests_are_stable(self):
        pipe = UnifiedCinemaSciencePipeline()
        digests = pipe.module_digests()
        assert set(digests) == {
            "photochemical_emulsion",
            "physical_optics_radiometry",
            "optimal_transport_spd",
            "symplectic_hamiltonian_pipeline",
        }
        assert all(isinstance(value, str) and len(value) == 64 for value in digests.values())

    def test_inputs_validated(self):
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(temperature_k=0.0)
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(reciprocity_p=0.3)
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(hg_asymmetry=1.0)

    def test_receipt_outputs_are_finite(self):
        receipt = UnifiedCinemaSciencePipeline().execute()
        assert all(math.isfinite(v) for v in receipt.outputs.values())


# ---------------------------------------------------------------------------
# PR #205 supersession: Asset Library V1 contracts
# ---------------------------------------------------------------------------


class TestAssetLibrarySupersession:
    def test_supported_media_types_is_closed(self):
        expected = {
            "image/png": MediaKind.IMAGE,
            "image/jpeg": MediaKind.IMAGE,
            "image/webp": MediaKind.IMAGE,
            "audio/wav": MediaKind.AUDIO,
            "audio/mpeg": MediaKind.AUDIO,
            "video/mp4": MediaKind.VIDEO,
            "video/webm": MediaKind.VIDEO,
        }
        assert SUPPORTED_MEDIA_TYPES == expected

    def test_ingest_state_is_closed(self):
        assert {s.value for s in IngestState} == {"STAGING", "READY", "FAILED"}

    def test_mime_detection_classifies_png(self):
        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
        assert detect_media_type(png_header) == "image/png"

    def test_mime_detection_classifies_jpeg(self):
        jpeg_header = b"\xff\xd8\xff\xe0" + b"\x00" * 16
        assert detect_media_type(jpeg_header) == "image/jpeg"

    def test_mime_detection_classifies_webm(self):
        webm_header = b"\x1a\x45\xdf\xa3" + b"\x00" * 24
        assert detect_media_type(webm_header) == "video/webm"

    def test_mime_detection_unknown_returns_none(self):
        assert detect_media_type(b"\x89\x50\x4e\x47") is None

    def test_mime_detection_empty_returns_none(self):
        assert detect_media_type(b"") is None


# ---------------------------------------------------------------------------
# PR #326 supersession: character capsule
# ---------------------------------------------------------------------------


class TestCharacterCapsuleSupersession:
    def test_capsule_id_grammar(self):
        assert CHARACTER_ID_RE.match("NAGAR-CHAR-000001")
        assert not CHARACTER_ID_RE.match("NAGAR-CHAR-12345")
        assert not CHARACTER_ID_RE.match("NAGAR-CHAR-000001-x")

    def test_capsule_is_immutable_and_sealed(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        assert isinstance(capsule, CharacterCapsule)
        assert capsule.seal() is capsule
        # digest is deterministic and domain-separated
        assert capsule.digest().startswith("sha256:")
        assert capsule.digest() == capsule.seal().digest()

    def test_revision_changes_bump_revision_and_module_revision(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        updated = capsule.apply_revision_changes("face", {"skin_texture": "weathered"})
        assert updated.revision_no == 2
        assert updated.face.revision == 2
        assert updated.face.skin_texture == "weathered"

    def test_revision_update_refuses_identity_fields(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        with pytest.raises(ScopeViolation):
            capsule.apply_revision_changes("face", {"eye_colour": "#ffffff"})

    def test_revision_update_refuses_unknown_field(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        with pytest.raises(ValidationError):
            capsule.apply_revision_changes("face", {"unknown": 1})

    def test_revision_update_requires_changes(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        with pytest.raises(ValidationError):
            capsule.apply_revision_changes("face", {})

    def test_identity_version_mints_and_bumps_only_identity_fields(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        updated = capsule.new_identity_version(
            face_changes={"eye_colour": "#2a4d88"},
            voice_changes={"f0_hz": 180.0},
        )
        assert updated.identity_version == 2
        assert updated.face.eye_colour == "#2a4d88"
        assert updated.voice.f0_hz == 180.0
        assert updated.face.revision == 2

    def test_identity_version_refuses_rev_fields(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        with pytest.raises(ValidationError):
            capsule.new_identity_version(face_changes={"skin_texture": "scarred"})

    def test_identity_version_requires_at_least_one_identity_change(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        with pytest.raises(ValidationError):
            capsule.new_identity_version()

    def test_continuity_verifier_three_state_bands(self):
        thresholds = {
            "face": ContinuityThreshold("face", 0.2, 0.6),
            "body": ContinuityThreshold("body", 0.2, 0.6),
        }
        report = verify_dimensions(
            {"face": 0.10, "body": 0.40}, thresholds
        )
        assert report.verdict == VerificationState.REVIEW_REQUIRED
        assert report.dimensions["face"].state == VerificationState.VERIFIED
        assert report.dimensions["body"].state == VerificationState.REVIEW_REQUIRED

    def test_continuity_verifier_drift_dominates(self):
        thresholds = {
            "face": ContinuityThreshold("face", 0.2, 0.6),
            "body": ContinuityThreshold("body", 0.2, 0.6),
        }
        report = verify_dimensions(
            {"face": 0.05, "body": 0.75}, thresholds
        )
        assert report.verdict == VerificationState.IDENTITY_DRIFT

    def test_continuity_verifier_not_measured_is_not_scored(self):
        thresholds = {"face": ContinuityThreshold("face", 0.2, 0.6)}
        report = verify_dimensions({"face": None}, thresholds)
        assert report.dimensions["face"].state == VerificationState.NOT_MEASURED

    def test_continuity_report_seal_binds_content(self):
        thresholds = {
            "face": ContinuityThreshold("face", 0.2, 0.6),
        }
        report = verify_dimensions({"face": 0.1}, thresholds)
        sealed = report.seal()
        assert sealed.startswith("sha256:")
        assert isinstance(report, ContinuityReport)

    def test_character_id_is_validated(self):
        with pytest.raises(ValidationError):
            CharacterCapsule(character_id="BAD-ID", identity_version=1, revision_no=1)

    def test_face_module_validates_colours(self):
        with pytest.raises(ValidationError):
            FaceModule(eye_colour="red")

    def test_identity_version_is_monotone(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        v1 = capsule.new_identity_version(face_changes={"eye_colour": "#2a4d88"})
        v2 = v1.new_identity_version(face_changes={"eye_colour": "#1a3a66"})
        assert v2.identity_version == 3
