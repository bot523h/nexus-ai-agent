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


def test_governed_spine_without_authorizer_fails_closed_by_default(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(gov.AtlasGovernanceRefusalError) as exc:
        gov.run_staged_spine_governed(_film(), fs, out_dir="spine_out")
    assert exc.value.code == "AUTHORIZER_REQUIRED"
    # zero side effects on disk: no artifact directory, no lineage, no quarantine.
    assert list(tmp_path.iterdir()) == []


def test_governed_master_without_authorizer_fails_closed_by_default(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.atlas.showrunner_bible import (
        Character,
        Episode,
        ShowrunnerBible,
    )

    bible = ShowrunnerBible()
    bible.add_character(Character("a", "Alice"))
    bible.add_episode(Episode(1, "Pilot"))
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(gov.AtlasGovernanceRefusalError) as exc:
        gov.run_olympus_master_governed(bible, fs, out_dir="m_out")
    assert exc.value.code == "AUTHORIZER_REQUIRED"
    assert list(tmp_path.iterdir()) == []


def test_governed_spine_explicit_lab_mode_requires_opt_in_flag(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    ledger = gov.run_staged_spine_governed(
        _film(), fs, allow_unauthenticated_lab_mode=True, out_dir="spine_out"
    )
    assert ledger.computed >= 1
    # The result carries an explicit, machine-readable lab-mode marker.
    assert "authority_mode=LAB_UNAUTHENTICATED" in ledger.notes


def test_governed_master_explicit_lab_mode_marks_receipt(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.atlas.showrunner_bible import (
        Character,
        Episode,
        ShowrunnerBible,
    )

    bible = ShowrunnerBible()
    bible.add_character(Character("a", "Alice"))
    bible.add_episode(Episode(1, "Pilot"))
    fs = WorkspaceFilesystem(tmp_path)
    ledger = gov.run_olympus_master_governed(
        bible,
        fs,
        allow_unauthenticated_lab_mode=True,
        side=16,
        frames=1,
        fps=1,
        out_dir="m_out",
    )
    assert "authority_mode=LAB_UNAUTHENTICATED" in ledger.notes


def test_authorizer_decision_must_permit_the_exact_target(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)

    class _WrongTarget:
        def authorize(self, *, operation, actor, project_id, payload):
            # Authorized in general, but permits only a *different* target.
            return gov.CommandBusDecision(
                authorized=True,
                authority_mode="AUTHORIZED",
                permits=lambda p, r, o, a, f: r == "rev-OTHER",
            )

    with pytest.raises(gov.SpineAuthorizationError):
        gov.run_staged_spine_governed(
            _film(), fs, command_bus_authorizer=_WrongTarget(), revision_id="rev-1"
        )
    assert list(tmp_path.iterdir()) == []


def test_authorizer_decision_permits_the_exact_target(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)

    class _Exact:
        def authorize(self, *, operation, actor, project_id, payload):
            return gov.CommandBusDecision(
                authorized=True,
                authority_mode="AUTHORIZED",
                permits=lambda p, r, o, a, f: p == "atlas" and r == "rev-1" and f == 1,
            )

    ledger = gov.run_staged_spine_governed(
        _film(), fs, command_bus_authorizer=_Exact(), revision_id="rev-1", fencing_token=1
    )
    assert "authority_mode=AUTHORIZED" in ledger.notes


def test_governed_spine_failure_is_quarantined(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    # out_dir with a parent-traversal component makes run_staged_spine raise.
    with pytest.raises(Exception):  # noqa: B017 - the spine raises SpineError
        gov.run_staged_spine_governed(
            _film(),
            fs,
            allow_unauthenticated_lab_mode=True,
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
            _film(),
            fs,
            allow_unauthenticated_lab_mode=True,
            attempt_id="a2",
            fencing_token=2,
            out_dir="spine_out",
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
        gov.run_staged_spine_governed(
            _film(),
            fs,
            allow_unauthenticated_lab_mode=True,
            attempt_id="cap",
            fencing_token=1,
            out_dir="out",
        )
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
            bible,
            fs,
            allow_unauthenticated_lab_mode=True,
            attempt_id="m1",
            fencing_token=3,
            out_dir="m_out",
        )
    qdir = tmp_path / ".quarantine" / "m1_3"
    assert (qdir / "partial.json").read_bytes() == b"partial"
    receipt = json.loads((qdir / "quarantine_receipt.json").read_text())
    assert receipt["fencing_token"] == 3
    assert "post-disk mismatch" in receipt["failure_reason"]


# --- 2b. atomic single-document ledger: concurrency, torn-write recovery, JCS -----


def test_jcs_canonical_bytes_are_sorted_and_compact() -> None:
    assert gov.jcs_canonical_bytes({"b": 1, "a": [True, None, 2.0], "c": "x"}) == (
        b'{"a":[true,null,2],"b":1,"c":"x"}'
    )
    # Integral floats canonicalise to integers; bools are not numbers.
    assert gov.jcs_canonical_bytes({"a": 2.0, "b": 3}) == b'{"a":2,"b":3}'


def test_jcs_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError):
        gov.jcs_canonical_bytes({"x": float("inf")})


def test_jcs_rejects_unsupported_type() -> None:
    with pytest.raises(TypeError):
        gov.jcs_canonical_bytes({"x": object()})


def test_authoritative_document_is_single_and_generational(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    store = gov.AtlasLineageStore(fs)
    store.commit(asset_version=_record(1), passport=_passport(1), fencing_token=1)
    store.commit(asset_version=_record(2), passport=_passport(2), fencing_token=2)
    authoritative = tmp_path / "_lineage" / "lineage_ledger.json"
    assert authoritative.is_file()
    doc = json.loads(authoritative.read_text())
    assert doc["highest_fencing_token"] == 2
    assert doc["generation_id"] == 2
    gens = sorted((tmp_path / "_lineage" / "generations").glob("ledger_gen_*.json"))
    assert [p.name for p in gens] == sorted(p.name for p in gens)
    assert len(gens) == 2
    assert store.generation_id == 2


def test_view_files_are_derived_and_consistent_with_authoritative(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    store = gov.AtlasLineageStore(fs)
    store.commit(asset_version=_record(1), passport=_passport(1), fencing_token=1)
    authoritative = json.loads((tmp_path / "_lineage" / "lineage_ledger.json").read_text())
    assets_view = (tmp_path / "_lineage" / "asset_versions.json").read_bytes()
    passports_view = (tmp_path / "_lineage" / "artifact_passports.json").read_bytes()
    assert assets_view == gov.jcs_canonical_bytes(authoritative["asset_versions"])
    assert passports_view == gov.jcs_canonical_bytes(authoritative["artifact_passports"])


def test_torn_view_file_is_healed_on_read(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    store = gov.AtlasLineageStore(fs)
    store.commit(asset_version=_record(1), passport=_passport(1), fencing_token=1)
    # Simulate a crash mid-update: the passports view is truncated/garbage while
    # the authoritative document is intact.
    passports_view = tmp_path / "_lineage" / "artifact_passports.json"
    passports_view.write_text("[garbage")
    recovered = store.records()
    assert recovered["artifact_passports"][0]["passport_digest"] == "b" * 64
    # The torn view is healed back to the canonical bytes.
    assert json.loads(passports_view.read_text())[0]["passport_digest"] == "b" * 64


def test_crash_after_authoritative_promotion_leaves_consistent_state(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    store = gov.AtlasLineageStore(fs)
    store.commit(asset_version=_record(1), passport=_passport(1), fencing_token=1)
    # A crash between the authoritative swap and the view derivation leaves stale
    # (generation-0) views. A fresh reader must recover from the authoritative doc.
    (tmp_path / "_lineage" / "asset_versions.json").write_bytes(b"[]")
    (tmp_path / "_lineage" / "artifact_passports.json").write_bytes(b"[]")
    reopened = gov.AtlasLineageStore(WorkspaceFilesystem(tmp_path))
    recs = reopened.records()
    assert len(recs["asset_versions"]) == 1
    assert len(recs["artifact_passports"]) == 1
    # No orphan records: the two sets stay in lockstep.
    assert len(recs["asset_versions"]) == len(recs["artifact_passports"])


def test_fencing_monotonicity_holds_across_instances(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    # instance_B is created BEFORE instance_A commits: it must not act on a stale
    # in-memory token.
    instance_a = gov.AtlasLineageStore(fs)
    instance_b = gov.AtlasLineageStore(WorkspaceFilesystem(tmp_path))
    instance_a.commit(asset_version=_record(10), passport=_passport(10), fencing_token=10)
    with pytest.raises(gov.StaleFencingTokenError):
        instance_b.commit(asset_version=_record(9), passport=_passport(9), fencing_token=9)
    # A later token on instance_B is still accepted (no lost liveness).
    res = instance_b.commit(asset_version=_record(11), passport=_passport(11), fencing_token=11)
    assert res.committed and res.highest_token == 11
    assert instance_a.highest_token == 11


def test_fencing_monotonicity_holds_across_processes(tmp_path: Path) -> None:
    """Concurrent, independent processes must serialise through the flock.

    Eight processes each try to commit a distinct token 1..8 concurrently. With
    the interprocess lock and an in-lock re-read, the outcome is deterministic in
    its *invariants* regardless of scheduling: the two record sets stay the same
    length, tokens are unique, the highest committed token equals the maximum
    token that succeeded, and every process that exited 0 is represented.
    """
    import os
    import subprocess
    import sys

    fs = WorkspaceFilesystem(tmp_path)
    gov.AtlasLineageStore(fs)  # create the directory so workers race on the lock
    worker = "\n".join(
        [
            "import sys",
            "from nexus_ai_agent.creative.atlas import atlas_spine_governance as g",
            "from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem",
            "root, tok = sys.argv[1], int(sys.argv[2])",
            "fs = WorkspaceFilesystem(root)",
            "st = g.AtlasLineageStore(fs)",
            "a = g.AtlasAssetVersionRecord('a', '0' * 64, 1, tok, 'att', 'p')",
            "p = g.AtlasArtifactPassportRecord('a', '0' * 64, tok, 'b' * 64, 'p')",
            "try:",
            "    st.commit(asset_version=a, passport=p, fencing_token=tok)",
            "    print('OK')",
            "except g.StaleFencingTokenError:",
            "    print('STALE')",
        ]
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3] / "src")
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", worker, str(tmp_path), str(tok)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for tok in range(1, 9)
    ]
    ok_tokens: set[int] = set()
    for tok, proc in zip(range(1, 9), procs, strict=False):
        out, err = proc.communicate(timeout=120)
        if proc.returncode == 0 and "OK" in out:
            ok_tokens.add(tok)
    reopened = gov.AtlasLineageStore(WorkspaceFilesystem(tmp_path))
    recs = reopened.records()
    tokens = [r["fencing_token"] for r in recs["asset_versions"]]
    # No torn state: the two sets are always the same length.
    assert len(recs["asset_versions"]) == len(recs["artifact_passports"])
    # No lost updates and no duplicates.
    assert len(tokens) == len(set(tokens))
    # Every successful process is represented; the ledger's highest token is the max.
    assert ok_tokens <= set(tokens)
    assert reopened.highest_token == max(tokens) == max(ok_tokens)


def test_lineage_dir_symlink_is_refused(tmp_path: Path) -> None:
    import os

    fs = WorkspaceFilesystem(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, tmp_path / "_lineage")
    store = gov.AtlasLineageStore(fs)
    with pytest.raises(gov.AtlasGovernanceRefusalError) as exc:
        store.records()
    assert exc.value.code == "UNSAFE_LINEAGE_PATH"


# --- 2c. remaining branch coverage: JCS edges, atomic-write refusal, decisions ----


def test_jcs_handles_false_and_non_integral_floats() -> None:
    # ``false`` literal and a genuinely fractional float (repr fallback).
    assert gov.jcs_canonical_bytes({"a": False, "b": 0.5}) == b'{"a":false,"b":0.5}'


def test_atomic_write_refuses_a_path_that_escapes_the_root(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises(gov.AtlasGovernanceRefusalError) as exc:
        gov._atomic_write(root, root.parent / "evil.json", b"x")
    assert exc.value.code == "UNSAFE_LINEAGE_PATH"


def test_atomic_write_refuses_a_symlink_leaf(tmp_path: Path) -> None:
    import os

    root = tmp_path / "ws"
    root.mkdir()
    target = root / "real.json"
    target.write_bytes(b"old")
    link = root / "link.json"
    os.symlink(target, link)
    with pytest.raises(gov.AtlasGovernanceRefusalError) as exc:
        gov._atomic_write(root, link, b"new")
    assert exc.value.code == "UNSAFE_LINEAGE_PATH"
    assert target.read_bytes() == b"old"  # never followed


def test_atomic_write_cleans_up_staging_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ws"
    root.mkdir()

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(gov.os, "replace", _boom)
    with pytest.raises(OSError):
        gov._atomic_write(root, root / "x.json", b"payload")
    # No orphan staging file is left behind.
    leftovers = [p.name for p in root.iterdir() if p.name.startswith(".staging-")]
    assert leftovers == []


def test_authority_receipt_unauthenticated_flag() -> None:
    lab = gov.AuthorityReceipt(
        authority_mode="LAB_UNAUTHENTICATED",
        operation="op",
        project_id="p",
        revision_id="r",
        attempt_id="a",
        fencing_token=1,
    )
    auth = gov.AuthorityReceipt(
        authority_mode="AUTHORIZED",
        operation="op",
        project_id="p",
        revision_id="r",
        attempt_id="a",
        fencing_token=1,
    )
    assert lab.unauthenticated is True
    assert auth.unauthenticated is False


def test_authorizer_dict_decision_is_interpreted(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)

    class _DictAuthorizer:
        def authorize(self, *, operation, actor, project_id, payload):
            return {"authorized": True, "authority_mode": "AUTHORIZED"}

    ledger = gov.run_staged_spine_governed(_film(), fs, command_bus_authorizer=_DictAuthorizer())
    assert "authority_mode=AUTHORIZED" in ledger.notes


def test_authorizer_dict_decision_can_deny(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)

    class _DictDeny:
        def authorize(self, *, operation, actor, project_id, payload):
            return {"authorized": False}

    with pytest.raises(gov.SpineAuthorizationError):
        gov.run_staged_spine_governed(_film(), fs, command_bus_authorizer=_DictDeny())


def test_authorizer_truthy_fallback_is_interpreted(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)

    class _Truthy:
        def authorize(self, *, operation, actor, project_id, payload):
            return 1  # not a bool / dict / CommandBusDecision -> truthiness fallback

    ledger = gov.run_staged_spine_governed(_film(), fs, command_bus_authorizer=_Truthy())
    assert "authority_mode=AUTHORIZED" in ledger.notes


def test_with_authority_note_is_idempotent() -> None:
    ledger = _film()
    receipt = gov.AuthorityReceipt(
        authority_mode="AUTHORIZED",
        operation="op",
        project_id="p",
        revision_id="r",
        attempt_id="a",
        fencing_token=1,
    )
    once = gov._with_authority_note(ledger, receipt)
    twice = gov._with_authority_note(once, receipt)
    assert once is not ledger
    assert twice is once  # second call is a no-op (note already present)


def test_first_commit_at_token_zero_is_accepted(tmp_path: Path) -> None:
    """The empty ledger's highest token is 0; a token-0 first commit is valid."""
    store = gov.AtlasLineageStore(WorkspaceFilesystem(tmp_path))
    res = store.commit(asset_version=_record(0), passport=_passport(0), fencing_token=0)
    assert res.committed
    assert res.highest_token == 0
    assert res.asset_version_count == 1
