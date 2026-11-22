"""Cinema DAG integration, numeric boundaries and cryptographic evidence."""

import importlib

import numpy as np
import pytest

from nexus_ai_agent.creative.audio_engine import forensic_qc as qc
from nexus_ai_agent.creative.audio_engine import spine_lineage as lineage
from nexus_ai_agent.creative.audio_engine import stereo_hpss as hpss
from nexus_ai_agent.creative.audio_engine.cinema_kernels import eyring_t60
from nexus_ai_agent.creative.audio_engine.master_cinema_pipeline import MasterCinemaAudioPipeline

MODULES = (
    "cinema_kernels",
    "stereo_hpss",
    "psychoacoustic_bark_mastering",
    "spectral_voice_restoration",
    "ambisonics_binaural_fdn",
    "av_subframe_foley_sync",
    "forensic_qc",
    "spine_lineage",
)


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("bad", [True, np.bool_(False), np.nan, np.inf, -np.inf])
def test_module_numeric_guards(module, bad):
    m = importlib.import_module("nexus_ai_agent.creative.audio_engine." + module)
    with pytest.raises((ValueError, TypeError)):
        m._require_finite_scalar(bad, name="test")
    with pytest.raises((ValueError, TypeError)):
        m._require_finite_array([bad], name="test")


@pytest.mark.parametrize("bad", [0.0, 1.0, -1.0, np.nan, np.inf, True])
def test_eyring_excludes_logarithmic_pole(bad):
    with pytest.raises((ValueError, TypeError)):
        eyring_t60(100.0, 100.0, bad)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_nonfinite_audio_is_violated(bad):
    report = qc.analyze_forensic_audio_qc(np.array([0.0, bad]), sample_rate=48000)
    assert report.status == "VIOLATED"
    assert not report.ok


@pytest.mark.parametrize("bad", [True, np.nan, np.inf])
def test_qc_threshold_guards(bad):
    with pytest.raises((TypeError, ValueError)):
        qc.analyze_forensic_audio_qc(np.zeros(100), sample_rate=48000, clip_threshold=bad)


def test_silent_hpss_partition():
    a, b = hpss.hpss_masks(np.zeros((10, 10)))
    np.testing.assert_array_equal(a + b, np.ones((10, 10)))
    with pytest.raises(ValueError):
        hpss.hpss_masks(np.ones((10, 10)), kernel_harmonic=2)


def test_merkle_size_binding_and_invalid_hashes():
    payloads = [str(i).encode() for i in range(8)]
    leaves = [lineage.merkle_leaf_hash(x, total_leaves=8) for x in payloads]
    root = lineage.merkle_root(leaves)
    proof = lineage.merkle_inclusion_proof(leaves, 0)
    assert lineage.verify_merkle_inclusion(
        leaf=payloads[0], index=0, total_leaves=8, proof=proof, root=root, bind_tree_size=True
    )
    assert not lineage.verify_merkle_inclusion(
        leaf=payloads[0], index=0, total_leaves=7, proof=proof, root=root, bind_tree_size=True
    )
    for invalid in ([], [b"bad"]):
        with pytest.raises(ValueError):
            lineage.merkle_root(invalid, strict=True)


def _stems():
    sr = 24000
    t = np.arange(sr) / sr
    dialogue = 0.1 * np.sin(2 * np.pi * 220 * t)
    foley = 0.01 * np.random.default_rng(2).standard_normal(sr)
    return dialogue, dialogue * 0.5, foley, np.abs(np.sin(np.arange(24)))


def test_pipeline_deterministic_master_and_all_inclusion_proofs():
    pipeline = MasterCinemaAudioPipeline(sample_rate=24000)
    one = pipeline.run(*_stems())
    two = pipeline.run(*_stems())
    assert one.status == "PASS"
    assert one.lufs == pytest.approx(-24.0, abs=0.1)
    assert one.root == two.root
    assert len(one.proofs) == len(one.stage_hashes)
    np.testing.assert_array_equal(one.samples, two.samples)
    assert not one.samples.flags.writeable


def test_pipeline_qc_fail_closed(monkeypatch):
    from dataclasses import replace

    original = qc.analyze_forensic_audio_qc

    def violated(*args, **kwargs):
        return replace(original(*args, **kwargs), codes=("injected_violation",))

    monkeypatch.setattr(qc, "analyze_forensic_audio_qc", violated)
    with pytest.raises(ValueError, match="VIOLATED"):
        MasterCinemaAudioPipeline(sample_rate=24000).run(*_stems())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sample_rate": 8000},
        {"sample_rate": 24000.5},
        {"room_dims_m": (1.0, 2.0)},
        {"room_dims_m": (0.0, 2.0, 3.0)},
    ],
)
def test_pipeline_config_boundaries(kwargs):
    with pytest.raises(ValueError):
        MasterCinemaAudioPipeline(**kwargs)


def test_pipeline_shape_hpss_lineage_guards(monkeypatch):
    from dataclasses import replace

    from nexus_ai_agent.creative.audio_engine import master_cinema_pipeline as module

    d, m, f, v = _stems()
    pipeline = MasterCinemaAudioPipeline(sample_rate=24000)
    for invalid in (
        (d[:100], m, f, v),
        (d, m, f[:-1], v),
        (d, m[:-1], f, v),
        (d[None, :], m, f, v),
        (d, m, f[None, :], v),
    ):
        with pytest.raises(ValueError):
            pipeline.run(*invalid)
    original = hpss.hpss_separate
    for field in ("mask_sum_error", "reconstruction_error"):
        with monkeypatch.context() as patch:
            patch.setattr(
                hpss,
                "hpss_separate",
                lambda *a, _field=field, **k: replace(original(*a, **k), **{_field: 0.1}),
            )
            with pytest.raises(ValueError, match="HPSS"):
                pipeline.run(d, m, f, v)
    with monkeypatch.context() as patch:
        patch.setattr(lineage, "verify_merkle_inclusion", lambda **k: False)
        with pytest.raises(ValueError, match="inclusion"):
            pipeline.run(d, m, f, v)
    result = module.run_master_cinema_audio_pipeline(d, np.vstack((m, m)), f, v, sample_rate=24000)
    assert result.status == "PASS"


def test_fdn_unitarity_and_contractive_feedback_checks(monkeypatch):
    from nexus_ai_agent.creative.audio_engine import ambisonics_binaural_fdn as ambi

    with monkeypatch.context() as patch:
        patch.setattr(ambi.np.linalg, "norm", lambda *a, **k: 1.0)
        with pytest.raises(ValueError, match="unitary"):
            ambi.householder_matrix()

    class UnsafeRoom:
        rt60_low = float("inf")
        rt60_mid = float("inf")
        rt60_high = float("inf")
        volume_m3 = 120.0
        surface_area_m2 = 150.0

    with pytest.raises(ValueError, match="feedback"):
        ambi.fdn_reverb(np.zeros(100), sample_rate=24000, room=UnsafeRoom(), tail_seconds=0.0)


def test_legacy_merkle_primitives_and_strict_root():
    import hashlib

    assert lineage.merkle_root([]) == hashlib.sha256(b"").digest()
    assert lineage.merkle_node_hash(b"raw", b"") == hashlib.sha256(b"\x01raw").digest()
    assert len(lineage.merkle_root([b"x" * 32], strict=True)) == 32


def test_qc_boolean_input_is_rejected():
    with pytest.raises(TypeError):
        qc.analyze_forensic_audio_qc(np.ones(5, dtype=bool), sample_rate=48000)


def test_pipeline_rejects_multichannel_music_before_hpss():
    d, m, f, v = _stems()
    with pytest.raises(ValueError, match="music"):
        MasterCinemaAudioPipeline(sample_rate=24000).run(d, np.vstack((m, m, m)), f, v)


def test_aces_conversion_errors_remain_domain_specific():
    from nexus_ai_agent.creative.atlas import aces_hald_film_optic as aces

    for value in (["invalid"], [[1.0], [2.0, 3.0]], object()):
        with pytest.raises(aces.FilmOpticError):
            aces._require_finite_array(value, name="input")
