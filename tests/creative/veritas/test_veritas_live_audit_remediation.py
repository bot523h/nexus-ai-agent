"""Live audit regression checks: independent containers, retention, durable lineage."""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas import omnia_cinema_physics as physics
from nexus_ai_agent.creative.veritas import veritas_spine_adapter as spine
from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import (
    compile_and_heal_real_media_shot,
)


def request(root: Path) -> spine.VeritasSpinePublishRequest:
    return spine.VeritasSpinePublishRequest(
        "project",
        "revision",
        "shot",
        "attempt",
        7,
        root,
        root / "published",
        input_frames_rgb=np.zeros((2, 8, 8, 3)),
        input_audio_pcm=np.zeros(100),
    )


@pytest.mark.parametrize("m", [1, 2, 6])
def test_angular_harmonic_bound_and_independent_coordinates(m: int) -> None:
    reference = np.random.default_rng(31).random((8, 8, 3))
    sequence = physics.synthesize_rotary_subframes(
        reference,
        angular_velocity_rad_s=200,
        fps=24,
        angular_harmonic_m=m,
    )
    q = int(np.ceil(m * 200 / (0.85 * np.pi * 24)))
    assert sequence.output_fps == 24 * q
    assert len(sequence.frames) == 2 * q
    assert not physics.validate_rotary_subframes(sequence)
    spine._verify_rotary_coordinates(sequence)
    # A forged low harmonic cannot retroactively legalize under-sampled spokes.
    undersampled = replace(sequence, angular_harmonic_m=100)
    assert "nyquist_rotary_aliasing" in physics.validate_rotary_subframes(undersampled)
    with pytest.raises(ValueError, match="Nyquist"):
        spine._verify_rotary_coordinates(undersampled)


@pytest.mark.parametrize("m", [0, -1, True, 1.5])
def test_invalid_harmonic_refusal(m: Any) -> None:
    with pytest.raises(physics.StroboscopicAliasingError, match="harmonic"):
        physics.synthesize_rotary_subframes(
            np.ones((4, 4)), angular_velocity_rad_s=200, fps=24, angular_harmonic_m=m
        )
    sequence = physics.synthesize_rotary_subframes(
        np.ones((4, 4)), angular_velocity_rad_s=200, fps=24
    )
    assert "invalid_angular_harmonic" in physics.validate_rotary_subframes(
        replace(sequence, angular_harmonic_m=m)
    )
    with pytest.raises(ValueError, match="harmonic"):
        spine._verify_rotary_coordinates(replace(sequence, angular_harmonic_m=m))


def test_independent_pixel_invariants_and_transform_refuse() -> None:
    sequence = physics.synthesize_rotary_subframes(
        np.random.default_rng(9).random((8, 8, 3)), angular_velocity_rad_s=200, fps=24
    )
    with pytest.raises(ValueError, match="range"):
        spine._verify_rotary_coordinates(replace(sequence, frames=sequence.frames + 2))
    with pytest.raises(ValueError, match="coordinate"):
        spine._verify_rotary_coordinates(replace(sequence, frames=sequence.frames[::-1]))
    with pytest.raises(ValueError, match="phase step"):
        spine._verify_rotary_coordinates(replace(sequence, phases_rad=sequence.phases_rad**2))


@pytest.mark.parametrize("kind", ["tamper", "stale"])
def test_forensic_quarantine_retains_failed_bytes(tmp_path: Path, kind: str) -> None:
    token = [7]
    expected: dict[str, str] = {}

    def fault(stage: Path) -> None:
        if kind == "tamper":
            (stage / "frame_0000.png").write_bytes(b"tampered")
        else:
            token[0] = 8
        expected["frame_0000.png"] = hashlib.sha256(
            (stage / "frame_0000.png").read_bytes()
        ).hexdigest()

    with pytest.raises(spine.VeritasSpineVerificationError):
        spine.VeritasStagedSpineAdapter(lambda: token[0], fault).execute(request(tmp_path))
    assert not (tmp_path / "published").exists()
    quarantine = tmp_path / ".quarantine" / "attempt_7"
    receipt = json.loads((quarantine / "quarantine_receipt.json").read_text())
    assert receipt["attempt_id"] == "attempt" and receipt["fencing_token"] == 7
    assert receipt["failure_reason"]
    assert receipt["observed_digests"]["frame_0000.png"] == expected["frame_0000.png"]
    assert (quarantine / "frame_0000.png").is_file()
    assert not (tmp_path / ".staging" / "attempt_7").exists()


def test_ledger_restart_exact_retry_and_stale_cas(tmp_path: Path) -> None:
    req = request(tmp_path)
    result = spine.VeritasStagedSpineAdapter(lambda: 7).execute(req)
    root = req.published_root / "_lineage"
    assert json.loads((root / "asset_versions.json").read_text()) == [asdict(result.asset_version)]
    assert json.loads((root / "artifact_passports.json").read_text()) == [
        asdict(result.artifact_passport)
    ]
    assert result.artifact_passport.law_pass_count == 2
    assert result.artifact_passport.observed_law_count == 2
    assert result.artifact_passport.unobserved_law_count == 13
    # A brand new adapter and store read committed disk state, not an in-memory cache.
    retry = spine.VeritasStagedSpineAdapter(lambda: 7).execute(req)
    assert retry.asset_version == result.asset_version
    assert len(json.loads((root / "asset_versions.json").read_text())) == 1
    with pytest.raises(spine.VeritasStaleFencingTokenError, match="durable"):
        spine.VeritasStagedSpineAdapter(lambda: 6).execute(replace(req, fencing_token=6))
    store = spine.VeritasLineageStore(req.published_root, tmp_path / "lock")
    with store.transaction():
        with pytest.raises(spine.VeritasStaleFencingTokenError, match="durable"):
            store.commit(replace(result.asset_version, fencing_token=6), result.artifact_passport)
        with pytest.raises(spine.VeritasSpineVerificationError, match="conflicting"):
            store.commit(
                replace(result.asset_version, output_frames_content_sha256="0" * 64),
                result.artifact_passport,
            )
    assert len(store.records()[0]) == 1
    # Different input is NOT an idempotent retry of an existing publication.
    with pytest.raises(spine.VeritasSpineVerificationError, match="exists"):
        spine.VeritasStagedSpineAdapter(lambda: 7).execute(
            replace(req, input_frames_rgb=np.ones((2, 8, 8, 3)) * 0.4)
        )


def test_command_bus_authorization_before_staging(tmp_path: Path) -> None:
    seen: list[str] = []

    def authorize(req: spine.VeritasSpinePublishRequest) -> bool:
        seen.append(req.attempt_id)
        return False

    with pytest.raises(spine.VeritasSpineVerificationError, match="CommandBus"):
        spine.VeritasStagedSpineAdapter(lambda: 7, command_bus_authorizer=authorize).execute(
            request(tmp_path)
        )
    assert seen == ["attempt"]
    assert not (tmp_path / ".staging").exists()
    assert not (tmp_path / "published").exists()


@pytest.mark.parametrize("fault", ["count", "rate", "corrupt"])
def test_independent_real_container_refuses_anchored_wrong_stream(
    tmp_path: Path, fault: str
) -> None:
    bundle = compile_and_heal_real_media_shot(
        tmp_path, input_frames=np.zeros((2, 8, 8, 3)), input_audio_pcm=np.zeros(100)
    )
    if bundle.mp4_path is None:
        pytest.skip("local FFmpeg unavailable")
    binary = importlib.import_module("imageio_ffmpeg").get_ffmpeg_exe()
    path = Path(bundle.mp4_path)
    if fault == "corrupt":
        path.write_bytes(b"invalid-stream")
    else:
        count, fps = (1, 24) if fault == "count" else (2, 12)
        subprocess.run(
            [
                binary,
                "-v",
                "error",
                "-y",
                "-framerate",
                str(fps),
                "-i",
                str(tmp_path / "frame_%04d.png"),
                "-frames:v",
                str(count),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
        )
    # Anchor the NEW container hash, so this tests its actual stream, not the digest guard.
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path = tmp_path / "veritas_merkle_bundle.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["output_mp4_sha256"] = digest
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(spine.VeritasMediaVerificationError, match="MP4"):
        spine.verify_compiled_veritas_bundle_on_disk(replace(bundle, output_mp4_sha256=digest))


def test_mp4_metadata_rate_and_timestamps_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Byte count alone is insufficient even when decode exits successfully.
    completed = SimpleNamespace(
        returncode=0, stdout=bytes(2 * 8 * 8 * 3), stderr=b"pts_time:0 pts_time:0.04"
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: completed)
    with pytest.raises(ValueError, match="rate or duration"):
        spine._verify_mp4_stream("ffmpeg", Path("video.mp4"), 2, 8, 8, 24)


def test_invalid_compiler_harmonic_fails_before_writing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="harmonic"):
        compile_and_heal_real_media_shot(tmp_path / "not-created", angular_harmonic_m=0)
    assert not (tmp_path / "not-created").exists()


def test_injected_store_failure_rolls_back_media_and_retains_evidence(tmp_path: Path) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    class RefusingStore:
        @contextmanager
        def transaction(self) -> Iterator[None]:
            yield

        def records(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            return [], []

        def commit(
            self,
            asset: spine.VeritasAssetVersionRecord,
            passport: spine.VeritasArtifactPassportRecord,
        ) -> None:
            raise OSError("injected storage failure")

    root = tmp_path / "published"
    root.mkdir()
    sentinel = root / "sentinel"
    sentinel.write_bytes(b"prior publication")
    with pytest.raises(spine.VeritasSpineVerificationError, match="storage failure"):
        spine.VeritasStagedSpineAdapter(lambda: 7, lineage_store=RefusingStore()).execute(
            request(tmp_path)
        )
    assert list(root.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"prior publication"
    assert (tmp_path / ".quarantine/attempt_7/quarantine_receipt.json").is_file()


def test_invalid_lineage_pointer_refused(tmp_path: Path) -> None:
    published = tmp_path / "published"
    published.mkdir()
    (published / "_lineage").symlink_to(tmp_path, target_is_directory=True)
    store = spine.VeritasLineageStore(published, tmp_path / "lock")
    with pytest.raises(spine.VeritasSpineVerificationError, match="generation"):
        store.records()


def test_quarantine_collision_retains_both_attempts(tmp_path: Path) -> None:
    def tamper(stage: Path) -> None:
        (stage / "dialogue.wav").write_bytes(b"bad")

    adapter = spine.VeritasStagedSpineAdapter(lambda: 7, tamper)
    for _ in range(2):
        with pytest.raises(spine.VeritasSpineVerificationError):
            adapter.execute(request(tmp_path))
    assert len(list((tmp_path / ".quarantine").rglob("quarantine_receipt.json"))) == 2


def test_mp4_last_frame_hold_is_not_accepted_as_nominal_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = SimpleNamespace(
        returncode=0,
        stdout=bytes(2 * 8 * 8 * 3),
        stderr=(
            b"config in time_base: 1/12288, frame_rate: 24/1 "
            b"pts_time:0 duration_time:0.0416667 "
            b"pts_time:0.0416667 duration_time:0.5"
        ),
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: completed)
    with pytest.raises(ValueError, match="frame duration"):
        spine._verify_mp4_stream("ffmpeg", Path("video.mp4"), 2, 8, 8, 24)


def test_three_way_observability_and_backward_report_fields() -> None:
    from nexus_ai_agent.creative.veritas import (
        CinemaShot,
        VeritasAuditReport,
        VeritasRealityAuditor,
    )
    from nexus_ai_agent.creative.veritas.reality_auditor import LAW_IDENTIFIERS

    auditor = VeritasRealityAuditor()
    empty = auditor.audit_and_heal(CinemaShot(), heal=False)
    assert isinstance(empty, VeritasAuditReport)
    assert empty.observed_law_count == empty.passed_law_count == 0
    assert empty.unobserved_law_count == 15
    assert empty.unobserved_laws == LAW_IDENTIFIERS
    assert set(empty.law_statuses.values()) == {"UNOBSERVED"}
    assert empty.total_violations == 0 and empty.is_reality_consistent
    assert len(empty.violations) == len(empty.receipts) == 15
    clean = auditor.audit_and_heal(CinemaShot(anamorphic=np.ones((4, 4, 3)) * 0.5), heal=False)
    assert clean.observed_law_count == clean.passed_law_count == 1
    assert clean.unobserved_law_count == 14
    bad = np.ones((4, 4, 3)) * 0.5
    bad[..., 0] = 0.9
    violated = auditor.audit_and_heal(CinemaShot(anamorphic=bad), heal=False)
    assert violated.law_statuses[LAW_IDENTIFIERS[11]] == "VIOLATED"
    assert violated.passed_law_count == 0 and violated.observed_law_count == 1
    assert not violated.is_reality_consistent
    assert violated.as_dict()["pre_audit"]["observed_law_count"] == 1


def test_observation_erasure_is_refused_by_healer_and_compiler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus_ai_agent.creative.veritas import CinemaShot, VeritasRealityAuditor

    bad = np.ones((4, 4, 3)) * 0.5
    bad[..., 0] = 0.9
    shot = CinemaShot(anamorphic=bad, eyes=(np.zeros(3), np.ones(3), np.ones(3) * 2))
    monkeypatch.setattr(
        VeritasRealityAuditor,
        "_heal_geometric_observations",
        lambda self, source: replace(source, eyes=None),
    )
    with pytest.raises(ValueError, match="observation erasure"):
        VeritasRealityAuditor().heal_cinema_shot(shot)
    monkeypatch.setattr(
        VeritasRealityAuditor, "heal_cinema_shot", lambda self, source: CinemaShot()
    )
    with pytest.raises(ValueError, match="observation erasure"):
        compile_and_heal_real_media_shot(
            tmp_path,
            shot=CinemaShot(anamorphic=bad),
            input_frames=np.ones((2, 4, 4, 3)) * 0.5,
            input_audio_pcm=np.zeros(100),
            mux_mp4=False,
        )


def test_no_fabricated_mouth_fluid_or_rotor_on_mechanical_texture() -> None:
    from nexus_ai_agent.creative.veritas import VeritasRealityAuditor
    from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import (
        extract_cinema_shot_from_media,
    )
    from nexus_ai_agent.creative.veritas.reality_auditor import LAW_IDENTIFIERS

    rng = np.random.default_rng(84)
    reference = rng.random((8, 8, 3))
    shot = extract_cinema_shot_from_media(np.stack([reference] * 2), np.zeros(100))
    assert shot.viseme is None and shot.doppler is None
    assert shot.fluid is None and shot.rotor is None
    assert shot.phase is not None
    np.testing.assert_array_equal(shot.phase[1], 0)
    report = VeritasRealityAuditor().audit_and_heal(shot, heal=False)
    assert report.law_statuses[LAW_IDENTIFIERS[10]] == "PASS"
    assert report.law_statuses[LAW_IDENTIFIERS[12]] == "UNOBSERVED"
    explicit = extract_cinema_shot_from_media(
        np.stack([reference] * 2),
        np.zeros(100),
        vocal_viseme_carrier=True,
        fluid_carrier=True,
        doppler_velocity_m_s=0,
    )
    assert (
        explicit.viseme is not None and explicit.fluid is not None and explicit.doppler is not None
    )


def test_empirical_profile_dt_and_flicker_are_not_erased() -> None:
    from nexus_ai_agent.creative.veritas import VeritasRealityAuditor
    from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import (
        extract_cinema_shot_from_media,
    )

    frames = np.ones((3, 4, 8, 3)) * 0.3
    frames[1] = 0.8  # not merely the final transition is checked
    for fps in (24, 96, 312):
        shot = extract_cinema_shot_from_media(frames, np.zeros(100), fps=fps)
        assert shot.phase_dt == 1 / fps
        assert shot.phase is not None
        np.testing.assert_array_equal(shot.phase[1], 0)
        report = VeritasRealityAuditor().audit_and_heal(shot, heal=False)
        assert "madelung_continuity_residual_exceeded" in report.details_before[11]


def test_radiometric_certificate_quantization_and_unknown_prior_bias() -> None:
    from nexus_ai_agent.creative.veritas import calibrate_catadioptric_optics

    image = np.ones((8, 8, 3)) * 0.4
    image[..., 0] *= 1.2
    image[..., 2] *= 0.8
    result = calibrate_catadioptric_optics(image)
    certificate = result.calibration_certificate
    assert certificate is not None
    assert certificate.calibration_mode == "MODEL_RELATIVE_GREEN"
    assert certificate.reference_channel == "G"
    assert not certificate.prior_bias_bounded
    assert certificate.uint8_quantization_sigma == 1 / (255 * np.sqrt(12))
    assert certificate.combined_uncertainty_bound == np.hypot(
        certificate.uint8_quantization_sigma, certificate.post_calibration_chromatic_rms
    )
    assert certificate.estimated_gain_r_to_g > 0 and certificate.estimated_gain_b_to_g > 0


def test_product_manifest_ledger_certificate_and_all_leaf_binding(tmp_path: Path) -> None:
    bundle = compile_and_heal_real_media_shot(
        tmp_path,
        input_frames=np.ones((2, 8, 8, 3)) * 0.4,
        input_audio_pcm=np.zeros(100),
        mux_mp4=False,
    )
    path = tmp_path / "veritas_manifest.json"
    product = json.loads(path.read_text())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == bundle.product_manifest_sha256
    assert all(leaf["product_manifest_sha256"] == digest for leaf in bundle.leaf_payloads)
    for scope in ("pre_audit", "post_audit", "disk_re_audit"):
        ledger = product[scope]
        assert ledger["observed_law_count"] == ledger["passed_law_count"] == 2
        assert ledger["unobserved_law_count"] == len(ledger["unobserved_laws"]) == 13
    assert spine.verify_compiled_veritas_bundle_on_disk(bundle)
    product["radiometric_calibration_certificate"]["combined_uncertainty_bound"] = 0
    with pytest.raises(ValueError, match="uncertainty"):
        spine._verify_radiometric_certificate(product, bundle.healed_frames.astype(float) / 255)
    product["post_audit"]["law_statuses"]["law_12_catadioptric_optics"] = "UNOBSERVED"
    with pytest.raises(ValueError, match="accounting|provenance"):
        spine._verify_observation_ledgers(product)
    from nexus_ai_agent.creative.veritas.reality_auditor import observation_summary

    product["post_audit"] = json.loads(
        json.dumps(
            observation_summary(
                product["post_audit"]["law_statuses"],
                {
                    **product["post_audit"]["law_observation_provenance"],
                    "law_12_catadioptric_optics": "UNOBSERVED",
                },
            )
        )
    )
    with pytest.raises(ValueError, match="observation erasure"):
        spine._verify_observation_ledgers(product)
    path.write_text(json.dumps(product))
    with pytest.raises(spine.VeritasMediaVerificationError, match="product manifest digest"):
        spine.verify_compiled_veritas_bundle_on_disk(bundle)


def test_illegal_status_enum_cannot_masquerade_as_pass() -> None:
    product = {"pre_audit": {"law_statuses": {"invented": "PASS"}}}
    with pytest.raises(ValueError, match="observability status"):
        spine._verify_observation_ledgers(product)


def test_empirical_potential_uses_temporal_flux_not_spatial_edge_angle() -> None:
    from nexus_ai_agent.creative.veritas import VeritasRealityAuditor
    from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import (
        extract_cinema_shot_from_media,
    )

    width, fps = 16, 96
    phase = np.zeros(width)
    phase[4:12] = np.sin(np.linspace(0, np.pi, 8)) * 0.01
    derivative = np.gradient(np.eye(width), axis=0)
    density = np.ones(width) * 0.5
    previous = density + derivative @ (density * (derivative @ phase)) / fps
    frames = np.stack([np.tile(previous, (4, 1)), np.tile(density, (4, 1))])
    rgb = np.repeat(frames[..., None], 3, axis=-1)
    shot = extract_cinema_shot_from_media(rgb, np.zeros(100), fps=fps)
    assert shot.phase is not None
    assert np.max(np.abs(shot.phase[1])) > 0
    report = VeritasRealityAuditor().audit_and_heal(shot, heal=False)
    assert report.law_violations_before[11] == 0


def test_absent_unidentifiable_calibration_makes_no_measured_claim() -> None:
    spine._verify_radiometric_certificate(
        {"radiometric_calibration_certificate": None}, np.zeros((2, 4, 4, 3))
    )


def test_scene_carrier_missing_on_disk_is_not_a_pass(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.veritas import CinemaShot

    shot = CinemaShot(facs=np.zeros((2, 26)))
    bundle = compile_and_heal_real_media_shot(
        tmp_path,
        shot=shot,
        input_frames=np.ones((2, 8, 8, 3)) * 0.4,
        input_audio_pcm=np.zeros(100),
        mux_mp4=False,
    )
    assert bundle.post_heal_violations == 0
    with pytest.raises(spine.VeritasMediaVerificationError, match="scene carrier absent"):
        spine.verify_compiled_veritas_bundle_on_disk(bundle)


def test_provenance_reports_manifest_and_passport_are_explicit(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.veritas import (
        CinemaShot,
        VeritasRealityAuditor,
        build_corrupted_15_law_cinema_shot,
    )
    from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import (
        extract_cinema_shot_from_media,
    )
    from nexus_ai_agent.creative.veritas.reality_auditor import LAW_IDENTIFIERS

    source = extract_cinema_shot_from_media(np.ones((2, 8, 8, 3)) * 0.4, np.zeros(4000))
    report = VeritasRealityAuditor().audit_and_heal(source, heal=False)
    assert report.law_observation_provenance[LAW_IDENTIFIERS[10]] == "ESTIMATED_PROXY"
    assert report.law_observation_provenance[LAW_IDENTIFIERS[11]] == "MEASURED_MEDIA"
    assert report.law_observation_provenance[LAW_IDENTIFIERS[12]] == "UNOBSERVED"
    assert "never a physical quantum wavefunction" in report.as_dict()["law_11_interpretation"]
    supplied = VeritasRealityAuditor().audit_and_heal(
        CinemaShot(anamorphic=np.ones((4, 4, 3)) * 0.5)
    )
    assert supplied.law_observation_provenance[LAW_IDENTIFIERS[11]] == "MODEL_SUPPLIED"
    synthetic = VeritasRealityAuditor().audit_and_heal(build_corrupted_15_law_cinema_shot())
    assert set(synthetic.law_observation_provenance.values()) == {"SYNTHETIC_FIXTURE"}
    result = spine.VeritasStagedSpineAdapter(lambda: 7).execute(request(tmp_path))
    manifest = json.loads((result.published_dir / "veritas_manifest.json").read_text())
    assert (
        result.artifact_passport.law_observation_provenance
        == manifest["law_observation_provenance"]
    )
    assert (
        result.artifact_passport.law_observation_provenance[LAW_IDENTIFIERS[10]]
        == "ESTIMATED_PROXY"
    )
    # Provenance cannot hide a present observation behind UNOBSERVED.
    manifest["disk_re_audit"]["law_observation_provenance"][LAW_IDENTIFIERS[10]] = "UNOBSERVED"
    with pytest.raises(ValueError, match="provenance"):
        spine._verify_observation_ledgers(manifest)


def test_gum_fields_separate_residual_from_type_b_standard_uncertainty() -> None:
    from nexus_ai_agent.creative.veritas import calibrate_catadioptric_optics

    result = calibrate_catadioptric_optics(np.random.default_rng(900).random((8, 8, 3)))
    certificate = result.calibration_certificate
    assert certificate is not None
    assert certificate.type_b_quantization_sigma == 1 / (255 * np.sqrt(12))
    assert (
        certificate.empirical_chromatic_rms_residual == certificate.post_calibration_chromatic_rms
    )
    assert certificate.rss_diagnostic_bound == np.hypot(
        certificate.type_b_quantization_sigma, certificate.empirical_chromatic_rms_residual
    )
    assert certificate.rss_diagnostic_bound == certificate.combined_uncertainty_bound
    assert certificate.uncalibrated_prior_bias_status == "UNBOUNDED_WITHOUT_REFERENCE_CHART"
    assert "not GUM combined standard uncertainty" in certificate.rss_interpretation


def test_versioned_revisions_and_attempts_never_overwrite(tmp_path: Path) -> None:
    req = request(tmp_path)
    adapter = spine.VeritasStagedSpineAdapter.for_production(
        lambda: 7, command_bus_authorizer=lambda _: True
    )
    first = adapter.execute(req)
    second = adapter.execute(replace(req, revision_id="revision2"))
    third = adapter.execute(replace(req, revision_id="revision2", attempt_id="attempt2"))
    assert first.published_dir == req.published_root / "project/revision/shot/attempt"
    assert second.published_dir == req.published_root / "project/revision2/shot/attempt"
    assert third.published_dir == req.published_root / "project/revision2/shot/attempt2"
    assert len({first.published_dir, second.published_dir, third.published_dir}) == 3
    assert all(
        (r.published_dir / "veritas_manifest.json").is_file() for r in (first, second, third)
    )
    assert (req.published_root / "shot").resolve() == third.published_dir
    assert len(json.loads((req.published_root / "_lineage/asset_versions.json").read_text())) == 3
    retry = adapter.execute(replace(req, revision_id="revision2", attempt_id="attempt2"))
    assert retry.asset_version == third.asset_version


def test_production_missing_denied_and_throwing_authorizer(tmp_path: Path) -> None:
    with pytest.raises(spine.VeritasAuthorizationRefusalError, match="mandatory"):
        spine.VeritasStagedSpineAdapter.for_production(lambda: 7)
    with pytest.raises(spine.VeritasAuthorizationRefusalError, match="mandatory"):
        spine.VeritasStagedSpineAdapter(lambda: 7, require_authorizer=True).execute(
            request(tmp_path)
        )

    def broken(_: spine.VeritasSpinePublishRequest) -> bool:
        raise RuntimeError("authority unavailable")

    for authorize in (broken, lambda _: False):
        with pytest.raises(spine.VeritasAuthorizationRefusalError):
            spine.VeritasStagedSpineAdapter.for_production(
                lambda: 7, command_bus_authorizer=authorize
            ).execute(request(tmp_path))
    assert not (tmp_path / ".staging").exists()
    assert not (tmp_path / "published").exists()


@pytest.mark.parametrize(
    "fault", ["black_video", "silent_audio", "unrelated_audio", "missing_audio"]
)
def test_reanchored_container_semantic_substitution_is_rejected(tmp_path: Path, fault: str) -> None:
    from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import _write_png, _write_wav

    duration = 4 / 24
    pcm = 0.35 * np.sin(2 * np.pi * 440 * np.arange(int(48000 * duration)) / 48000)
    bundle = compile_and_heal_real_media_shot(
        tmp_path / "bundle",
        input_frames=np.ones((4, 16, 16, 3)) * 0.4,
        input_audio_pcm=pcm,
        mux_mp4=True,
    )
    assert bundle.mp4_path is not None
    assert spine.verify_compiled_veritas_bundle_on_disk(bundle)
    binary = importlib.import_module("imageio_ffmpeg").get_ffmpeg_exe()
    command = [binary, "-v", "error", "-y", "-framerate", "24"]
    if fault == "black_video":
        black = tmp_path / "black.png"
        _write_png(black, np.zeros((16, 16, 3), dtype=np.uint8))
        command += ["-loop", "1", "-i", str(black)]
    else:
        command += ["-i", str(Path(bundle.output_dir) / "frame_%04d.png")]
    if fault != "missing_audio":
        track = Path(bundle.wav_path)
        if fault != "black_video":
            track = tmp_path / "substitute.wav"
            wrong = (
                np.zeros_like(pcm)
                if fault == "silent_audio"
                else 0.35 * np.sin(2 * np.pi * 1100 * np.arange(len(pcm)) / 48000)
            )
            _write_wav(track, wrong, 48000)
        command += ["-i", str(track), "-c:a", "aac"]
    command += ["-frames:v", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", bundle.mp4_path]
    subprocess.run(command, check=True, capture_output=True)
    digest = hashlib.sha256(Path(bundle.mp4_path).read_bytes()).hexdigest()
    manifest_path = Path(bundle.output_dir) / "veritas_merkle_bundle.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["output_mp4_sha256"] = digest
    manifest_path.write_text(json.dumps(manifest))
    # New trusted digest bypasses byte-integrity detection. Actual decoded
    # video/audio MUST still be compared to the reference PNG/WAV content.
    with pytest.raises(
        spine.VeritasMediaVerificationError, match="semantic video|semantic audio|audio stream"
    ):
        spine.verify_compiled_veritas_bundle_on_disk(replace(bundle, output_mp4_sha256=digest))


def test_mp4_semantic_receipt_is_bound_to_manifest_and_recomputed(tmp_path: Path) -> None:
    bundle = compile_and_heal_real_media_shot(
        tmp_path, input_frames=np.ones((2, 8, 8, 3)) * 0.4, input_audio_pcm=np.zeros(4000)
    )
    product = json.loads((tmp_path / "veritas_manifest.json").read_text())
    receipt = product["mp4_verification_receipt"]
    assert receipt["mp4_video_max_mad"] < 0.08
    assert receipt["mp4_video_min_psnr_db"] >= 22
    assert receipt["mp4_av_sync_drift_s"] <= 0.065
    assert receipt["mp4_audio_rms_delta"] == 0
    assert receipt["mp4_audio_duration_s"] > 0
    assert spine.verify_compiled_veritas_bundle_on_disk(bundle)


def test_disjoint_badge_publication_preserves_textured_body_and_measures_audio(
    tmp_path: Path,
) -> None:
    from nexus_ai_agent.creative.veritas import CinemaShot
    from nexus_ai_agent.creative.veritas.reality_auditor import LAW_IDENTIFIERS

    y, x = np.indices((16, 16))
    texture = 0.5 + 0.25 * np.cos(x * 0.8) * np.cos(y * 0.8)
    frames = np.repeat(np.stack([texture] * 4)[..., None], 3, axis=-1)
    pcm = 0.3 * np.sin(2 * np.pi * 800 * np.arange(8000) / 48000)
    bad_viseme = (np.zeros(4), np.ones(4), np.ones(4) * 800, np.ones(4) * 0.3)
    req = replace(
        request(tmp_path),
        shot=CinemaShot(viseme=bad_viseme),
        input_frames_rgb=frames,
        input_audio_pcm=pcm,
    )
    result = spine.VeritasStagedSpineAdapter.for_production(
        lambda: 7, command_bus_authorizer=lambda _: True
    ).execute(req)
    decoded = result.compile_result.healed_frames
    assert decoded.shape == (4, 24, 16, 3)
    assert np.std(decoded[:, :16].astype(float) / 255) > 0.08
    assert result.post_disk_verified
    ledger = json.loads((result.published_dir / "veritas_manifest.json").read_text())[
        "disk_re_audit"
    ]
    assert ledger["law_statuses"][LAW_IDENTIFIERS[12]] == "PASS"
    assert ledger["law_observation_provenance"][LAW_IDENTIFIERS[12]] == "MEASURED_MEDIA"
    assert spine.verify_compiled_veritas_bundle_on_disk(result.compile_result)


def test_badge_pixel_audio_and_roi_tampering_is_refused() -> None:
    from nexus_ai_agent.creative.veritas.media_carriers import (
        extract_disjoint_viseme_badge,
        render_disjoint_viseme_badge,
    )

    body = np.ones((4, 8, 12, 3)) * 0.4
    pcm = 0.3 * np.sin(2 * np.pi * 800 * np.arange(8000) / 48000)
    composite = render_disjoint_viseme_badge(body, pcm, 24, 48000)
    observation = extract_disjoint_viseme_badge(composite, pcm, 24, 48000)
    assert np.min(observation[0]) >= 0.5  # measured aperture, not wheel ROI texture
    assert np.allclose(composite[:, -8:].sum(axis=(1, 2, 3)), 4 * 12 * 3)
    with pytest.raises(ValueError, match="synchronization"):
        extract_disjoint_viseme_badge(composite, np.zeros_like(pcm), 24, 48000)
    tampered = composite.copy()
    tampered[:, -8, 0] = 0.2
    with pytest.raises(ValueError, match="raster"):
        extract_disjoint_viseme_badge(tampered, pcm, 24, 48000)
    with pytest.raises(ValueError, match="dimensions"):
        extract_disjoint_viseme_badge(body, pcm, 24, 48000)
    with pytest.raises(ValueError, match="width"):
        render_disjoint_viseme_badge(body[:, :, :4], pcm, 24, 48000)


@pytest.mark.parametrize(
    "fault", ["clock_only", "frame_count", "no_audio_pts", "audio_gap", "audio_offset"]
)
def test_independent_decode_clock_negative_paths(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    video = SimpleNamespace(
        returncode=0,
        stdout=bytes(2 * 8 * 8 * 3),
        stderr=b"config in time_base: 1/12288, frame_rate: 24/1 pts_time:0 pts_time:0.0416667",
    )
    if fault == "frame_count":
        video.stdout = b""
    audio = SimpleNamespace(
        returncode=0,
        stdout=bytes(8000),
        stderr=b"pts_time:0 nb_samples:1024 pts_time:0.0213333 nb_samples:2976",
    )
    if fault == "no_audio_pts":
        audio.stderr = b""
    elif fault == "audio_gap":
        audio.stderr = b"pts_time:0 nb_samples:1024 pts_time:0.5 nb_samples:2976"
    elif fault == "audio_offset":
        audio.stderr = b"pts_time:0.2 nb_samples:1024 pts_time:0.2213333 nb_samples:2976"
    responses = iter([video, audio])
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: next(responses))
    if fault == "clock_only":
        assert spine._verify_mp4_stream("ffmpeg", Path("video.mp4"), 2, 8, 8, 24) == {}
    else:
        with pytest.raises(ValueError, match="MP4"):
            spine._verify_mp4_stream(
                "ffmpeg", Path("video.mp4"), 2, 8, 8, 24, np.zeros((2, 8, 8, 3)), np.zeros(4000)
            )


def test_symlink_version_and_legacy_boundaries(tmp_path: Path) -> None:
    root = tmp_path / "published"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "project").symlink_to(elsewhere, target_is_directory=True)
    adapter = spine.VeritasStagedSpineAdapter(lambda: 7)
    with pytest.raises(spine.VeritasSpineVerificationError, match="version boundary"):
        adapter.execute(request(tmp_path))
    (root / "project").unlink()
    (root / "shot").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(spine.VeritasSpineVerificationError, match="legacy publication pointer"):
        adapter.execute(request(tmp_path))


def coupled_request(root: Path) -> spine.VeritasSpinePublishRequest:
    from nexus_ai_agent.creative.veritas import CinemaShot, build_corrupted_15_law_cinema_shot

    y, x = np.indices((24, 24))
    green = 0.5 + 0.38 * np.cos(4 * np.arctan2(y - 11.5, x - 11.5))
    fringe = np.stack([green * 1.1, green, green * 0.8], axis=-1)
    corrupt = build_corrupted_15_law_cinema_shot(height=24, width=24)
    scene = CinemaShot(
        phase=corrupt.phase,
        anamorphic=fringe,
        viseme=corrupt.viseme,
        doppler=corrupt.doppler,
        fluid=corrupt.fluid,
        rotor=(green, 200.0, 24.0),
    )
    return replace(
        request(root),
        shot=scene,
        angular_harmonic_m=4,
        input_frames_rgb=np.stack([fringe * 0.8, fringe]),
        input_audio_pcm=0.3 * np.sin(2 * np.pi * 800 * np.arange(4000) / 48000),
    )


def test_coupled_textured_hfr_five_law_publication_and_disk_reaudit(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.veritas.reality_auditor import LAW_IDENTIFIERS

    req = coupled_request(tmp_path)
    result = spine.VeritasStagedSpineAdapter.for_production(
        lambda: 7, command_bus_authorizer=lambda _: True
    ).execute(req)
    bundle = result.compile_result
    product = json.loads((result.published_dir / "veritas_manifest.json").read_text())
    assert bundle.output_fps == 312 and len(bundle.png_paths) == 26
    assert product["disk_re_audit"]["total_violations"] == 0
    for identifier in LAW_IDENTIFIERS[10:]:
        assert product["pre_audit"]["law_statuses"][identifier] == "VIOLATED"
        assert product["post_audit"]["law_statuses"][identifier] == "PASS"
        assert product["disk_re_audit"]["law_statuses"][identifier] == "PASS"
        assert product["disk_re_audit"]["law_observation_provenance"][identifier] != "UNOBSERVED"
    assert result.artifact_passport.law_pass_count == 5
    assert result.artifact_passport.unobserved_law_count == 10
    body = bundle.healed_frames[:, :-8]
    assert np.min(np.std(body[..., 1] / 255, axis=(1, 2))) >= 0.08
    assert not np.array_equal(body[0], body[1])
    assert np.all(body[..., 1].sum(axis=1) == body[0, ..., 1].sum(axis=0)[None])
    # Includes the badge: its complementary halves conserve radiance as it talks.
    totals = bundle.healed_frames.astype(np.int64).sum(axis=(1, 2, 3))
    assert np.all(totals == totals[0])
    assert bundle.mp4_path is not None
    assert product["mp4_verification_receipt"]["mp4_video_min_psnr_db"] >= 22
    assert product["rotary_motion_parameter_provenance"] == "MODEL_SUPPLIED"
    assert result.post_disk_verified and spine.verify_compiled_veritas_bundle_on_disk(bundle)


@pytest.mark.parametrize("fault", ["badge", "wav", "rgb", "flicker", "drop"])
def test_coupled_textured_tampering_quarantines_without_publication(
    tmp_path: Path, fault: str
) -> None:
    from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import (
        _read_png_rgb,
        _write_png,
        _write_wav,
    )

    def tamper(stage: Path) -> None:
        path = stage / "frame_0001.png"
        if fault == "drop":
            path.unlink()
        elif fault == "wav":
            _write_wav(stage / "dialogue.wav", np.zeros(4000), 48000)
        else:
            pixels = _read_png_rgb(path)
            if fault == "badge":
                pixels[-8, 0] ^= 255
            elif fault == "rgb":
                pixels[:24, :, 0] = 255
            else:
                pixels[:24] //= 2
            _write_png(path, pixels)

    with pytest.raises(spine.VeritasSpineVerificationError):
        spine.VeritasStagedSpineAdapter(lambda: 7, tamper).execute(coupled_request(tmp_path))
    assert not (tmp_path / "published").exists()
    assert (tmp_path / ".quarantine/attempt_7/quarantine_receipt.json").is_file()


def test_coupled_boundary_oracle_and_renderer_detect_conservative_teleportation() -> None:
    from nexus_ai_agent.creative.veritas.coupled_rotary_transport import verify_balanced_radiance

    y, x = np.indices((16, 16))
    gray = 0.5 + 0.3 * np.cos(4 * np.arctan2(y - 7.5, x - 7.5))
    reference = np.repeat(gray[..., None], 3, axis=-1)
    sequence = physics.synthesize_rotary_subframes(
        reference, angular_velocity_rad_s=200, fps=24, angular_harmonic_m=4, stabilize_radiance=True
    )
    verify_balanced_radiance(sequence.frames, reference)
    assert not physics.validate_rotary_subframes(sequence, quantized=True)
    reversed_rows = sequence.frames[:, ::-1].copy()
    verify_balanced_radiance(reversed_rows, reference)  # mass alone is not sufficient
    assert "subframe_render_mismatch" in physics.validate_rotary_subframes(
        replace(sequence, frames=reversed_rows), quantized=True
    )
    bad_color = sequence.frames.copy()
    bad_color[..., 0] = 1
    with pytest.raises(ValueError, match="boundary invariant"):
        verify_balanced_radiance(bad_color, reference)
    with pytest.raises(physics.StroboscopicAliasingError, match="RGB reference"):
        physics.synthesize_rotary_subframes(
            gray, angular_velocity_rad_s=200, fps=24, stabilize_radiance=True
        )


def test_flattening_a_textured_reference_is_refused() -> None:
    from nexus_ai_agent.creative.veritas.coupled_rotary_transport import verify_balanced_radiance

    y, _ = np.indices((8, 8))
    gray = (128 + 64 * (2 * (y % 2) - 1)) / 255
    reference = np.repeat(gray[..., None], 3, axis=-1)
    flat = np.full((2, 8, 8, 3), 128 / 255)
    with pytest.raises(ValueError, match="texture contrast"):
        verify_balanced_radiance(flat, reference)


def test_regularized_flow_without_bound_model_is_refused() -> None:
    from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import (
        extract_cinema_shot_from_media,
    )

    with pytest.raises(ValueError, match="bound known-motion model"):
        extract_cinema_shot_from_media(
            np.ones((2, 4, 4, 3)) * 0.5, np.zeros(100), regularized_rotary_flow=True
        )


def test_observability_counts_cannot_claim_extra_passes(tmp_path: Path) -> None:
    bundle = compile_and_heal_real_media_shot(
        tmp_path,
        input_frames=np.ones((2, 8, 8, 3)) * 0.4,
        input_audio_pcm=np.zeros(4000),
        mux_mp4=False,
    )
    product = json.loads((tmp_path / "veritas_manifest.json").read_text())
    product["disk_re_audit"]["passed_law_count"] = 15
    with pytest.raises(ValueError, match="accounting"):
        spine._verify_observation_ledgers(product)
    assert bundle.product_manifest_sha256


def test_unsafe_quarantine_root_keeps_staging_evidence(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".quarantine").symlink_to(outside, target_is_directory=True)

    def tamper(stage: Path) -> None:
        (stage / "dialogue.wav").write_bytes(b"forensic-test")

    with pytest.raises(spine.VeritasSpineVerificationError, match="unsafe quarantine"):
        spine.VeritasStagedSpineAdapter(lambda: 7, tamper).execute(request(tmp_path))
    assert (tmp_path / ".staging/attempt_7/dialogue.wav").read_bytes() == b"forensic-test"
    assert list(outside.iterdir()) == []
    assert not (tmp_path / "published").exists()


def test_disabling_coupled_boundary_model_cannot_publish_textured_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus_ai_agent.creative.veritas import real_media_veritas_compiler as compiler

    original = physics.synthesize_rotary_subframes

    def unbalanced(reference: compiler.FloatArray, **kwargs: Any) -> physics.RotarySubframeSequence:
        kwargs["stabilize_radiance"] = False
        return original(reference, **kwargs)

    monkeypatch.setattr(compiler, "synthesize_rotary_subframes", unbalanced)
    with pytest.raises(spine.VeritasSpineVerificationError):
        spine.VeritasStagedSpineAdapter(lambda: 7).execute(coupled_request(tmp_path))
    assert not (tmp_path / "published").exists()


def test_explicit_vocal_flag_and_compatibility_compiler_alias(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.veritas import compile_real_media_veritas_bundle

    bundle = compile_real_media_veritas_bundle(
        tmp_path,
        input_frames=np.ones((2, 8, 8, 3)) * 0.4,
        input_audio_pcm=np.zeros(4000),
        vocal_viseme_carrier=True,
        mux_mp4=False,
    )
    assert bundle.healed_frames.shape == (2, 16, 8, 3)
    assert spine.verify_compiled_veritas_bundle_on_disk(bundle)
