"""WAVE 19 — tests for the TITAN feature-film compiler and its real-disk export.

Fixtures are built inline through the ATLAS public API so this suite does not
depend on a sibling test module's import path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.creative.atlas import titan_feature_film_compiler as tf
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
from nexus_ai_agent.execution.staging import AttemptStaging
from nexus_ai_agent.tools.filesystem_policy import FilesystemBoundaryError, WorkspaceFilesystem

_SRC = SourceRef(path="ep01/sc01", line=1)


def _story(*, beats: tuple[str, ...] = ("b1", "b2")) -> StoryDocument:
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


def _shot(
    sid: str, covers: tuple[str, ...], frames: int, *, verified: bool = True
) -> ShotCandidate:
    return ShotCandidate(
        id=sid,
        asset_ref=f"asset://{sid}",
        covers_beats=covers,
        frames=frames,
        preference=0,
        evidence_status=TriValue.TRUE if verified else TriValue.UNKNOWN,
        evidence_ref=f"evidence://{sid}" if verified else "",
        source=_SRC,
    )


def _request(*, budget: int = 1000) -> PlanRequest:
    return PlanRequest(
        beats=("b1", "b2"),
        candidates=(_shot("s1", ("b1", "b2"), 48),),
        frame_budget=budget,
    )


# --- compilation -----------------------------------------------------------------


def test_compile_always_passes_are_scheduled() -> None:
    film = tf.compile_feature_film(_story(), plan_request=_request())
    ids = {p.pass_id for p in film.passes}
    assert "titan.wave14.atmosphere" in ids
    assert "titan.wave17.anamorphic_optics" in ids
    assert "titan.wave18.pacing" in ids


def test_compile_conditional_passes_require_flags() -> None:
    film = tf.compile_feature_film(_story(), plan_request=_request())
    ids = {p.pass_id for p in film.passes}
    assert "titan.wave15.crowd_heal" not in ids
    assert "titan.wave16.fracture_ledger" not in ids


def test_compile_with_scene_flags_adds_passes() -> None:
    film = tf.compile_feature_film(
        _story(), plan_request=_request(), scene_flags=frozenset({"crowd", "destruction"})
    )
    ids = {p.pass_id for p in film.passes}
    assert "titan.wave15.crowd_heal" in ids
    assert "titan.wave16.fracture_ledger" in ids


def test_compile_all_passes_are_proposed() -> None:
    film = tf.compile_feature_film(
        _story(),
        plan_request=_request(),
        scene_flags=frozenset({"non_euclidean", "crowd", "destruction"}),
    )
    assert film.passes
    assert all(p.pass_status == "PROPOSED" for p in film.passes)


def test_compile_total_frames_and_estimate() -> None:
    film = tf.compile_feature_film(_story(), plan_request=_request(), frame_rate=24.0)
    assert film.total_frames == 48
    assert film.estimated_seconds == pytest.approx(2.0)


def test_compile_is_deterministic() -> None:
    a = tf.compile_feature_film(_story(), plan_request=_request())
    b = tf.compile_feature_film(_story(), plan_request=_request())
    assert a.canonical_bytes() == b.canonical_bytes()


def test_compile_pass_content_hash_is_stable() -> None:
    film = tf.compile_feature_film(_story(), plan_request=_request())
    again = tf.compile_feature_film(_story(), plan_request=_request())
    assert [p.content_hash for p in film.passes] == [p.content_hash for p in again.passes]


def test_compile_notes_when_no_flags() -> None:
    film = tf.compile_feature_film(_story(), plan_request=_request())
    assert any("scene_flags" in n for n in film.notes)


def test_compile_handles_infeasible_plan() -> None:
    # Budget too small to place the only candidate -> plan has no steps.
    req = PlanRequest(
        beats=("b1", "b2"), candidates=(_shot("s1", ("b1", "b2"), 999),), frame_budget=10
    )
    film = tf.compile_feature_film(_story(), plan_request=req)
    assert film.pass_count == 0
    assert any("no steps" in n for n in film.notes)


def test_compile_rejects_bad_frame_rate() -> None:
    with pytest.raises(tf.CompilerError):
        tf.compile_feature_film(_story(), plan_request=_request(), frame_rate=0.0)


def test_compile_to_dict_roundtrip_shape() -> None:
    film = tf.compile_feature_film(_story(), plan_request=_request())
    d = film.to_dict()
    assert d["kind"] == "titan.compiled_feature_film"
    assert d["pass_count"] == len(d["passes"])
    assert d["schema_version"] == 1


# --- real-disk export ------------------------------------------------------------


def test_export_writes_and_verifies(tmp_path: Path) -> None:
    film = tf.compile_feature_film(_story(), plan_request=_request())
    fs = WorkspaceFilesystem(tmp_path)
    art = tf.export_compiled_film(film, fs, "out/compiled_film.json")
    assert art.verified
    assert art.sha256 == art.reopened_sha256
    assert art.size_bytes == len(film.canonical_bytes())


def test_export_bytes_match_canonical(tmp_path: Path) -> None:
    film = tf.compile_feature_film(_story(), plan_request=_request())
    fs = WorkspaceFilesystem(tmp_path)
    art = tf.export_compiled_film(film, fs, "compiled_film.json")
    reopened = fs.read_bytes(art.relative_path, max_bytes=tf.MAX_COMPILED_BYTES)
    assert reopened == film.canonical_bytes()


def test_export_rejects_path_traversal(tmp_path: Path) -> None:
    film = tf.compile_feature_film(_story(), plan_request=_request())
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(FilesystemBoundaryError):
        tf.export_compiled_film(film, fs, "../escape.json")


def test_export_creates_parent_directories(tmp_path: Path) -> None:
    film = tf.compile_feature_film(_story(), plan_request=_request())
    fs = WorkspaceFilesystem(tmp_path)
    art = tf.export_compiled_film(film, fs, "a/b/c/compiled_film.json")
    assert (tmp_path / art.relative_path).is_file()


# --- handoff ---------------------------------------------------------------------


def test_handoff_is_advisory_and_inert(tmp_path: Path) -> None:
    film = tf.compile_feature_film(_story(), plan_request=_request())
    fs = WorkspaceFilesystem(tmp_path)
    art = tf.export_compiled_film(film, fs, "compiled_film.json")
    ho = tf.compiled_film_handoff(film, art)
    assert ho["authority"] == "advisory"
    assert ho["requires_authorization"] is True
    assert "PROPOSED" in ho["pass_status_note"]
    assert ho["artifact"]["verified"] is True


def test_compile_notes_when_no_plan_request() -> None:
    # analyze_story with plan_request=None -> analysis.plan is None.
    doc = _story()
    film = tf.compile_feature_film(doc, plan_request=None)  # type: ignore[arg-type]
    assert film.pass_count == 0
    assert any("no plan request" in n for n in film.notes)


def test_export_via_staging_publish(tmp_path: Path) -> None:
    film = tf.compile_feature_film(_story(), plan_request=_request())
    staging_root = tmp_path / "staging_root"
    final_root = tmp_path / "final"
    staging = AttemptStaging(staging_root, job_id="job1", attempt_id="att1", final_root=final_root)
    fs = WorkspaceFilesystem(final_root)
    art = tf.export_compiled_film(film, fs, "titan/compiled_film.json", staging=staging)
    assert art.verified
    assert (final_root / "titan" / "compiled_film.json").is_file()


def test_export_rejects_over_budget(monkeypatch, tmp_path: Path) -> None:
    film = tf.compile_feature_film(_story(), plan_request=_request())
    monkeypatch.setattr(tf, "MAX_COMPILED_BYTES", 4)
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(tf.CompilerError):
        tf.export_compiled_film(film, fs, "compiled_film.json")
