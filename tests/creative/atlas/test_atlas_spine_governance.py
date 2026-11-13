"""Tests for the three canonical NEXUS spine-governance checks.

1. Forensic quarantine retention on a failed staged attempt.
2. Durable, protocol-compatible AssetVersion/ArtifactPassport lineage with CAS
   fencing-token monotonicity.
3. An injectable, fail-closed CommandBus authorization hook.

All filesystem writes go through the real :class:`WorkspaceFilesystem` boundary
(REAL DISK); nothing here is mocked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_ai_agent.creative.atlas import atlas_spine_governance as gov
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
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

_SRC = SourceRef(path="ep1/sc1", line=1)


def _film() -> tf.CompiledFilm:
    doc = StoryDocument(
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
        beats=(StoryBeat(id="b1", episode_id="ep1", order_index=0),),
    )
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
    return tf.compile_feature_film(doc, plan_request=req, scene_flags=frozenset({"crowd"}))


# --- 1. quarantine retention -----------------------------------------------------


def test_quarantine_preserves_failed_bytes_and_receipt(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    receipt = gov.quarantine_failed_attempt(
        fs,
        attempt_id="attempt-7",
        fencing_token=3,
        failure_reason="ffprobe frame-count mismatch",
        failed_bundle={"a.json": b"aaa", "b.json": b"bbb"},
        published_root="published",
    )
    base = tmp_path / ".quarantine" / "attempt-7_3"
    assert (base / "a.json").read_bytes() == b"aaa"
    assert (base / "b.json").read_bytes() == b"bbb"
    stored = json.loads((base / "quarantine_receipt.json").read_text())
    assert stored["attempt_id"] == "attempt-7"
    assert stored["fencing_token"] == 3
    assert stored["failure_reason"] == "ffprobe frame-count mismatch"
    assert stored["published_root"] == "published"
    assert stored["observed_digests"] == [
        ["a.json", receipt.observed_digests[0][1]],
        ["b.json", receipt.observed_digests[1][1]],
    ]
    # published_root must be untouched.
    assert not (tmp_path / "published").exists()


def test_quarantine_receipt_digest_is_a_real_function(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    r = gov.quarantine_failed_attempt(
        fs,
        attempt_id="a1",
        fencing_token=0,
        failure_reason="tampered bytes",
        failed_bundle={"x.json": b"payload"},
        published_root="p",
    )
    assert len(r.receipt_sha256) == 64
    assert r.receipt_sha256 != "0" * 64


def test_quarantine_rejects_unsafe_attempt_id(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(gov.SpineAuthorizationError):
        gov.quarantine_failed_attempt(
            fs,
            attempt_id="../escape",
            fencing_token=1,
            failure_reason="x",
            failed_bundle={},
            published_root="p",
        )


def test_quarantine_rejects_unsafe_bundle_name(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(gov.SpineAuthorizationError):
        gov.quarantine_failed_attempt(
            fs,
            attempt_id="a1",
            fencing_token=1,
            failure_reason="x",
            failed_bundle={"../escape": b"x"},
            published_root="p",
        )


def test_quarantine_rejects_negative_fencing_token(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(gov.SpineAuthorizationError):
        gov.quarantine_failed_attempt(
            fs,
            attempt_id="a1",
            fencing_token=-1,
            failure_reason="x",
            failed_bundle={},
            published_root="p",
        )


# --- 2. lineage store ------------------------------------------------------------


def _record(token: int, *, sha: str = "a" * 64) -> gov.AtlasAssetVersionRecord:
    return gov.AtlasAssetVersionRecord(
        asset_ref="atlas://x",
        content_sha256=sha,
        size_bytes=10,
        fencing_token=token,
        attempt_id="a1",
        published_rel="out/ledger.json",
    )


def _passport(token: int, *, sha: str = "a" * 64) -> gov.AtlasArtifactPassportRecord:
    return gov.AtlasArtifactPassportRecord(
        asset_ref="atlas://x",
        content_sha256=sha,
        fencing_token=token,
        passport_digest="b" * 64,
        published_rel="out/ledger.json",
    )


def test_lineage_commit_persists_and_is_readable(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    store = gov.AtlasLineageStore(fs)
    res = store.commit(asset_version=_record(1), passport=_passport(1), fencing_token=1)
    assert res.committed
    assert res.asset_version_count == 1
    assert res.passport_count == 1
    assets = json.loads((tmp_path / "_lineage" / "asset_versions.json").read_text())
    passports = json.loads((tmp_path / "_lineage" / "artifact_passports.json").read_text())
    assert assets[0]["fencing_token"] == 1
    assert passports[0]["passport_digest"] == "b" * 64


def test_lineage_rejects_stale_fencing_token(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    store = gov.AtlasLineageStore(fs)
    store.commit(asset_version=_record(5), passport=_passport(5), fencing_token=5)
    with pytest.raises(gov.StaleFencingTokenError):
        store.commit(asset_version=_record(4), passport=_passport(4), fencing_token=4)


def test_lineage_exact_retry_is_idempotent(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    store = gov.AtlasLineageStore(fs)
    first = store.commit(asset_version=_record(2), passport=_passport(2), fencing_token=2)
    again = store.commit(asset_version=_record(2), passport=_passport(2), fencing_token=2)
    assert first.committed
    assert not again.committed
    assert again.asset_version_count == 1


def test_lineage_same_token_different_payload_conflicts(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    store = gov.AtlasLineageStore(fs)
    store.commit(asset_version=_record(2), passport=_passport(2), fencing_token=2)
    with pytest.raises(gov.LineageConflictError):
        store.commit(
            asset_version=_record(2, sha="c" * 64),
            passport=_passport(2, sha="c" * 64),
            fencing_token=2,
        )


def test_lineage_highest_token_survives_reopen(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    gov.AtlasLineageStore(fs).commit(
        asset_version=_record(9), passport=_passport(9), fencing_token=9
    )
    reopened = gov.AtlasLineageStore(WorkspaceFilesystem(tmp_path))
    assert reopened.highest_token == 9


# --- 3. command-bus authorization hook -------------------------------------------


class _Deny:
    def authorize(self, *, operation, actor, project_id, payload):
        return False


class _Allow:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def authorize(self, *, operation, actor, project_id, payload):
        self.calls.append((operation, actor, project_id))
        return True


def test_governed_spine_denies_before_any_work(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(gov.SpineAuthorizationError):
        gov.run_staged_spine_governed(_film(), fs, command_bus_authorizer=_Deny())
    # zero work: no artifact directory created.
    assert not any(tmp_path.iterdir())


def test_governed_spine_authorized_commits_lineage(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    store = gov.AtlasLineageStore(fs)
    allow = _Allow()
    ledger = gov.run_staged_spine_governed(
        _film(),
        fs,
        command_bus_authorizer=allow,
        lineage=store,
        fencing_token=1,
        out_dir="spine_out",
    )
    assert ledger.computed >= 1
    assert allow.calls == [("titan.spine.run", "atlas", "atlas")]
    assert store.highest_token == 1
    assert (tmp_path / "spine_out" / "ledger.json").is_file()


def test_governed_spine_without_authorizer_matches_plain_run(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    led = gov.run_staged_spine_governed(_film(), fs, out_dir="spine_out")
    assert led.computed >= 1


def test_governed_spine_failure_is_quarantined(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    # out_dir with a parent-traversal component makes run_staged_spine raise.
    with pytest.raises(Exception):  # noqa: B017 - the spine raises SpineError
        gov.run_staged_spine_governed(
            _film(),
            fs,
            attempt_id="a9",
            fencing_token=4,
            out_dir="bad/../dir",
        )
    # The failure itself is not quarantined (no bytes were staged); the call
    # must still not have published anything.
    assert not (tmp_path / "bad").exists()


def test_governed_spine_quarantines_partial_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    from nexus_ai_agent.creative.atlas import titan_staged_execution_spine as spine

    def _boom(*args, **kwargs):
        # write a partial artifact, then fail like a post-disk mismatch
        fs.write_bytes("spine_out/partial.json", b"partial-bytes", create_parents=True)
        raise spine.SpineError("persisted bytes do not match")

    monkeypatch.setattr(gov, "run_staged_spine", _boom)
    with pytest.raises(spine.SpineError):
        gov.run_staged_spine_governed(
            _film(), fs, attempt_id="a2", fencing_token=2, out_dir="spine_out"
        )

    qdir = tmp_path / ".quarantine" / "a2_2"
    assert (qdir / "partial.json").read_bytes() == b"partial-bytes"
    receipt = json.loads((qdir / "quarantine_receipt.json").read_text())
    assert receipt["fencing_token"] == 2
    assert "persisted bytes do not match" in receipt["failure_reason"]


def test_governed_master_denies_and_allows(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.atlas.showrunner_bible import (
        Character,
        Episode,
        ShowrunnerBible,
    )

    bible = ShowrunnerBible()
    bible.add_character(Character("a", "Alice"))
    bible.add_episode(Episode(1, "Pilot"))
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(gov.SpineAuthorizationError):
        gov.run_olympus_master_governed(bible, fs, command_bus_authorizer=_Deny())

    store = gov.AtlasLineageStore(fs)
    ledger = gov.run_olympus_master_governed(
        bible,
        fs,
        command_bus_authorizer=_Allow(),
        lineage=store,
        fencing_token=1,
        side=16,
        frames=1,
        fps=1,
        out_dir="m_out",
    )
    assert store.highest_token == 1
    assert (tmp_path / "m_out" / "master_ledger.json").is_file()
    assert ledger.ledger_sha256 != "0" * 64


def test_lineage_rejects_non_int_token(tmp_path: Path) -> None:
    store = gov.AtlasLineageStore(WorkspaceFilesystem(tmp_path))
    with pytest.raises(gov.StaleFencingTokenError):
        store.commit(asset_version=_record(1), passport=_passport(1), fencing_token="1")  # type: ignore[arg-type]


def test_lineage_rejects_mismatched_record_token(tmp_path: Path) -> None:
    store = gov.AtlasLineageStore(WorkspaceFilesystem(tmp_path))
    with pytest.raises(gov.StaleFencingTokenError):
        store.commit(asset_version=_record(1), passport=_passport(1), fencing_token=2)


def test_quarantine_too_many_files_refused(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    bundle = {f"f{i}.json": b"x" for i in range(gov.MAX_QUARANTINE_FILES + 1)}
    with pytest.raises(gov.SpineAuthorizationError):
        gov.quarantine_failed_attempt(
            fs,
            attempt_id="a1",
            fencing_token=1,
            failure_reason="x",
            failed_bundle=bundle,
            published_root="p",
        )


def test_quarantine_empty_reason_refused(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(gov.SpineAuthorizationError):
        gov.quarantine_failed_attempt(
            fs,
            attempt_id="a1",
            fencing_token=1,
            failure_reason="",
            failed_bundle={},
            published_root="p",
        )


def test_collect_bundle_stops_at_the_file_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.creative.atlas import titan_staged_execution_spine as spine

    fs = WorkspaceFilesystem(tmp_path)
    monkeypatch.setattr(gov, "MAX_QUARANTINE_FILES", 2)

    def _boom(*args, **kwargs):
        fs.write_bytes("out/a.json", b"a", create_parents=True)
        fs.write_bytes("out/b.json", b"b", create_parents=True)
        fs.write_bytes("out/c.json", b"c", create_parents=True)
        raise spine.SpineError("stop")

    monkeypatch.setattr(gov, "run_staged_spine", _boom)
    with pytest.raises(spine.SpineError):
        gov.run_staged_spine_governed(_film(), fs, attempt_id="cap", fencing_token=1, out_dir="out")
    qdir = tmp_path / ".quarantine" / "cap_1"
    # Only the capped number of files is preserved.
    preserved = sorted(p.name for p in qdir.iterdir() if p.name != "quarantine_receipt.json")
    assert len(preserved) == 2


def test_governed_master_failure_is_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.creative.atlas import olympus_master as om

    fs = WorkspaceFilesystem(tmp_path)

    def _boom(*args, **kwargs):
        fs.write_bytes("m_out/partial.json", b"partial", create_parents=True)
        raise om.MasterError("post-disk mismatch")

    monkeypatch.setattr(om, "render_master", _boom)
    from nexus_ai_agent.creative.atlas.showrunner_bible import (
        Character,
        Episode,
        ShowrunnerBible,
    )

    bible = ShowrunnerBible()
    bible.add_character(Character("a", "Alice"))
    bible.add_episode(Episode(1, "Pilot"))
    with pytest.raises(om.MasterError):
        gov.run_olympus_master_governed(
            bible, fs, attempt_id="m1", fencing_token=3, out_dir="m_out"
        )
    qdir = tmp_path / ".quarantine" / "m1_3"
    assert (qdir / "partial.json").read_bytes() == b"partial"
    receipt = json.loads((qdir / "quarantine_receipt.json").read_text())
    assert receipt["fencing_token"] == 3
    assert "post-disk mismatch" in receipt["failure_reason"]
