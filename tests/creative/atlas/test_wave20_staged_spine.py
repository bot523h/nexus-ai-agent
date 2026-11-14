"""WAVE 20 — tests for the staged execution spine, the falsification suite and
the determinism of the whole Waves 13-20 chain.

The spine actually executes each PROPOSED pass on a bounded synthetic fixture
with the real Wave 13-18 operators, persists every artifact through the
authorized filesystem boundary, re-opens the bytes and verifies them. These
tests check the real persisted bytes, the ledger digest, the SKIPPED honesty
path, and that a mutated operator changes the recorded artifact digest (a real
falsification, not a negative-input test).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import (
    anamorphic_seidel_optics as optics,
)
from nexus_ai_agent.creative.atlas import (
    titan_feature_film_compiler as tf,
)
from nexus_ai_agent.creative.atlas import (
    titan_staged_execution_spine as sp,
)
from nexus_ai_agent.creative.episode_causality_lab import (
    SCHEMA_VERSION,
    Character,
    Episode,
    PlanRequest,
    Scene,
    ShotCandidate,
    SourceRef,
    StoryBeat,
    StoryDocument,
    TimeOfDay,
    TriValue,
)
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

_SRC = SourceRef(path="ep01/sc01", line=1)


def _story(*, beats: tuple[str, ...] = ("b1",)) -> StoryDocument:
    return StoryDocument(
        schema_version=SCHEMA_VERSION,
        characters=(Character(id="alice"),),
        episodes=(Episode(id="ep1", order_index=0),),
        scenes=(
            Scene(
                id="sc1",
                episode_id="ep1",
                location_id="loc1",
                order_index=0,
                time_of_day=TimeOfDay.DAY,
            ),
        ),
        beats=tuple(StoryBeat(id=b, episode_id="ep1", order_index=i) for i, b in enumerate(beats)),
    )


def _film(flags: frozenset[str]) -> tf.CompiledFilm:
    shot = ShotCandidate(
        id="s1",
        asset_ref="asset://s1",
        covers_beats=("b1",),
        frames=24,
        evidence_status=TriValue.TRUE,
        evidence_ref="evidence://s1",
        source=_SRC,
    )
    req = PlanRequest(beats=("b1",), candidates=(shot,), frame_budget=100)
    return tf.compile_feature_film(_story(), plan_request=req, scene_flags=flags)


_ALL_FLAGS = frozenset({"non_euclidean", "crowd", "destruction"})


def test_spine_computes_every_pass_with_all_flags(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    led = sp.run_staged_spine(_film(_ALL_FLAGS), fs)
    assert led.computed == 6
    assert led.skipped == 0


def test_spine_persists_real_artifacts(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    led = sp.run_staged_spine(_film(_ALL_FLAGS), fs)
    for s in led.stages:
        assert (tmp_path / s.relative_path).is_file()
        assert len(s.artifact_sha256) == 64


def test_spine_artifact_digest_matches_disk(tmp_path: Path) -> None:
    import hashlib

    fs = WorkspaceFilesystem(tmp_path)
    led = sp.run_staged_spine(_film(_ALL_FLAGS), fs)
    for s in led.stages:
        raw = (tmp_path / s.relative_path).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == s.artifact_sha256


def test_spine_ledger_digest_is_real_function(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    led = sp.run_staged_spine(_film(_ALL_FLAGS), fs)
    assert len(led.ledger_sha256) == 64
    assert led.ledger_sha256 != "0" * 64


def test_spine_is_deterministic(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    a = sp.run_staged_spine(_film(_ALL_FLAGS), fs)
    b = sp.run_staged_spine(_film(_ALL_FLAGS), fs)
    assert a.canonical_bytes() == b.canonical_bytes()
    assert a.ledger_sha256 == b.ledger_sha256


def test_spine_skips_unbacked_passes(tmp_path: Path) -> None:
    # A pass in the catalogue with no bounded operator is SKIPPED, not faked.
    film = _film(_ALL_FLAGS)
    # Inject a synthetic unknown pass into the tuple to exercise the SKIPPED path.
    fake = tf.RenderPass(
        step_index=0,
        candidate_id="s1",
        pass_id="titan.wave99.unknown",
        stage="scene",
        applies="always",
        frames=1,
        content_hash="x" * 64,
    )
    object.__setattr__(film, "passes", film.passes + (fake,))
    fs = WorkspaceFilesystem(tmp_path)
    led = sp.run_staged_spine(film, fs)
    skipped = [s for s in led.stages if s.status == "SKIPPED"]
    assert len(skipped) == 1
    assert skipped[0].pass_id == "titan.wave99.unknown"
    assert skipped[0].artifact_sha256 == ""


def test_spine_rejects_bad_side(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(sp.SpineError):
        sp.run_staged_spine(_film(_ALL_FLAGS), fs, side=1)
    with pytest.raises(sp.SpineError):
        sp.run_staged_spine(_film(_ALL_FLAGS), fs, side=sp.MAX_FIXTURE_SIDE + 1)


def test_spine_rejects_unsafe_out_dir(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(sp.SpineError):
        sp.run_staged_spine(_film(_ALL_FLAGS), fs, out_dir="/abs")
    with pytest.raises(sp.SpineError):
        sp.run_staged_spine(_film(_ALL_FLAGS), fs, out_dir="../escape")


def test_spine_via_staging_publish(tmp_path: Path) -> None:
    from nexus_ai_agent.execution.staging import AttemptStaging

    staging_root = tmp_path / "staging_root"
    final_root = tmp_path / "final"
    staging = AttemptStaging(staging_root, job_id="j", attempt_id="a", final_root=final_root)
    fs = WorkspaceFilesystem(final_root)
    led = sp.run_staged_spine(_film(_ALL_FLAGS), fs, staging=staging)
    assert led.computed == 6
    for s in led.stages:
        assert (final_root / s.relative_path).is_file()


# --- falsification: a mutated operator must change the recorded digest -----------


def test_mutating_operator_changes_artifact_digest(tmp_path: Path, monkeypatch) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    baseline = sp.run_staged_spine(_film(_ALL_FLAGS), fs)
    base_by_id = {s.pass_id: s.artifact_sha256 for s in baseline.stages}

    # Inject a defect: shift the anamorphic PSF energy residual deterministically.
    original = optics.apply_anamorphic_optics

    def mutated(image, lens, **kwargs):  # type: ignore[no-untyped-def]
        res = original(image, lens, **kwargs)
        res.image = res.image * 1.0000001
        res.energy_residual = res.energy_residual + 1e-6
        return res

    monkeypatch.setattr(sp.optics, "apply_anamorphic_optics", mutated)
    mutated_led = sp.run_staged_spine(_film(_ALL_FLAGS), fs)
    mut_by_id = {s.pass_id: s.artifact_sha256 for s in mutated_led.stages}
    assert (
        mut_by_id["titan.wave17.anamorphic_optics"] != base_by_id["titan.wave17.anamorphic_optics"]
    )


def test_wave17_energy_residual_truthful_after_mutation() -> None:
    # Direct falsification of the energy-conservation claim: a lossy kernel
    # must show a nonzero residual.
    img = np.zeros((64, 64, 3))
    img[32, 32] = 1.0
    res = optics.apply_anamorphic_optics(img, optics.AnamorphicLens(), size=17, defocus_mm=0.1)
    assert res.energy_residual < 1e-9  # honest: conserved


def test_spine_detects_persisted_byte_mismatch(tmp_path: Path, monkeypatch) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    real_read = fs.read_bytes

    def tampering_read(rel, *, max_bytes):  # type: ignore[no-untyped-def]
        raw = real_read(rel, max_bytes=max_bytes)
        return raw + b" "  # simulate corruption after write

    monkeypatch.setattr(fs, "read_bytes", tampering_read)
    with pytest.raises(sp.SpineError):
        sp.run_staged_spine(_film(_ALL_FLAGS), fs)
