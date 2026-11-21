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
            lineage.merkle_root(invalid)


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
