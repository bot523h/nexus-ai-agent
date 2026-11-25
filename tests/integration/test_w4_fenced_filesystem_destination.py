"""Real local filesystem and subprocess fencing; no SQLite fencing is relied on."""

from __future__ import annotations

import select
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from nexus_ai_agent.adapters.fenced_filesystem_destination import (
    DestinationStateError,
    EffectPayloadMismatchError,
    FencedFilesystemEffectDestination,
    FilesystemObservationVerifier,
    StaleEpochError,
)
from nexus_ai_agent.creative.studio.durable_dedupe import ExternalEffectObservation

KEY = "a" * 64


def test_idempotent_publication_and_project_isolation(tmp_path: Path) -> None:
    destination = FencedFilesystemEffectDestination(tmp_path, project_id="project")
    verifier = FilesystemObservationVerifier(tmp_path, project_id="project")
    first = destination.apply(effect_key=KEY, fencing_epoch=1, payload=b"original")
    assert first.status == "PRESENT" and verifier.verify(first)
    assert destination.apply(effect_key=KEY, fencing_epoch=1, payload=b"original") == first
    with pytest.raises(EffectPayloadMismatchError):
        destination.apply(effect_key=KEY, fencing_epoch=2, payload=b"different")
    newer = destination.apply(effect_key=KEY, fencing_epoch=2, payload=b"original")
    assert verifier.verify(newer)
    assert not verifier.verify(first)  # persisted metadata advanced
    with pytest.raises(StaleEpochError):
        destination.apply(effect_key=KEY, fencing_epoch=1, payload=b"original")
    other = FencedFilesystemEffectDestination(tmp_path, project_id="other")
    assert other.apply(effect_key=KEY, fencing_epoch=1, payload=b"different").status == "PRESENT"
    assert not FilesystemObservationVerifier(tmp_path, project_id="other").verify(newer)


def test_absent_tombstone_survives_restart_and_blocks_same_and_older_epochs(tmp_path: Path) -> None:
    first = FencedFilesystemEffectDestination(tmp_path, project_id="project")
    absent = first.inspect(effect_key=KEY, fencing_epoch=2)
    assert absent.status == "ABSENT"
    assert FilesystemObservationVerifier(tmp_path, project_id="project").verify(absent)
    restarted = FencedFilesystemEffectDestination(tmp_path, project_id="project")
    for old in (1, 2):
        with pytest.raises(StaleEpochError):
            restarted.apply(effect_key=KEY, fencing_epoch=old, payload=b"late")
    assert not list(tmp_path.rglob("*.stage-*"))
    present = restarted.apply(effect_key=KEY, fencing_epoch=3, payload=b"new")
    assert FilesystemObservationVerifier(tmp_path, project_id="project").verify(present)
    assert not FilesystemObservationVerifier(tmp_path, project_id="project").verify(absent)


def test_stopped_subprocess_writer_is_fenced_after_sqlite_unlock(tmp_path: Path) -> None:
    # The worker waits for a pipe signal before entering the destination, not
    # inside SQLite. The parent releases SQLite before writing the tombstone.
    program = """
import sys
from pathlib import Path
from nexus_ai_agent.adapters.fenced_filesystem_destination import (
    FencedFilesystemEffectDestination, StaleEpochError,
)
print("ready", flush=True)
sys.stdin.readline()
try:
    destination = FencedFilesystemEffectDestination(Path(sys.argv[1]), project_id="project")
    destination.apply(effect_key=sys.argv[2], fencing_epoch=1, payload=b"late")
except StaleEpochError:
    print("stale", flush=True)
else:
    print("published", flush=True)
"""
    (tmp_path / "destination").mkdir()
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", program, str(tmp_path / "destination"), KEY],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], 15)[0], "writer did not become ready"
        assert child.stdout.readline().strip() == "ready"
        with sqlite3.connect(tmp_path / "plans.sqlite") as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS marker (value INTEGER)")
            db.commit()
        # A second connection proves there is no SQLite write lock here.
        with sqlite3.connect(tmp_path / "plans.sqlite", timeout=0) as db:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        destination = FencedFilesystemEffectDestination(
            tmp_path / "destination", project_id="project"
        )
        absent = destination.inspect(effect_key=KEY, fencing_epoch=1)
        assert FilesystemObservationVerifier(tmp_path / "destination", project_id="project").verify(
            absent
        )
        out, err = child.communicate("continue\n", timeout=15)
        assert child.returncode == 0, err
        assert out.strip() == "stale"
        assert not list((tmp_path / "destination").rglob("*.effect"))
        assert not list((tmp_path / "destination").rglob("*.stage-*"))
        assert (
            destination.apply(effect_key=KEY, fencing_epoch=2, payload=b"new").status == "PRESENT"
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()


def test_crash_after_metadata_before_publish_fences_then_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = FencedFilesystemEffectDestination(tmp_path, project_id="project")
    write = destination._atomic_write

    def interrupt(path: Path, data: bytes) -> None:
        if path.suffix == ".effect":
            raise OSError("simulated interruption")
        write(path, data)

    monkeypatch.setattr(destination, "_atomic_write", interrupt)
    with pytest.raises(OSError, match="simulated interruption"):
        destination.apply(effect_key=KEY, fencing_epoch=1, payload=b"same")
    restarted = FencedFilesystemEffectDestination(tmp_path, project_id="project")
    absent = restarted.inspect(effect_key=KEY, fencing_epoch=1)
    assert absent.status == "ABSENT"
    assert FilesystemObservationVerifier(tmp_path, project_id="project").verify(absent)
    with pytest.raises(StaleEpochError):
        restarted.apply(effect_key=KEY, fencing_epoch=1, payload=b"same")
    with pytest.raises(EffectPayloadMismatchError):
        restarted.apply(effect_key=KEY, fencing_epoch=2, payload=b"different")
    assert restarted.apply(effect_key=KEY, fencing_epoch=2, payload=b"same").status == "PRESENT"


def test_evidence_and_filesystem_corruption_fail_closed(tmp_path: Path) -> None:
    destination = FencedFilesystemEffectDestination(tmp_path, project_id="project")
    verifier = FilesystemObservationVerifier(tmp_path, project_id="project")
    observation = destination.apply(effect_key=KEY, fencing_epoch=1, payload=b"genuine")
    assert not verifier.verify(replace(observation, evidence_digest="f" * 64))
    (next(tmp_path.rglob("*.effect"))).write_bytes(b"tampered")
    with pytest.raises(DestinationStateError):
        verifier.verify(observation)
    with pytest.raises(DestinationStateError):
        destination.inspect(effect_key=KEY, fencing_epoch=1)
    next(tmp_path.rglob("*.json")).write_text("not json", encoding="utf-8")
    with pytest.raises(DestinationStateError):
        destination.apply(effect_key=KEY, fencing_epoch=2, payload=b"genuine")


def test_killed_writer_leaves_only_staging_and_next_inspection_cleans_it(tmp_path: Path) -> None:
    program = """
import os
import sys
from pathlib import Path
from nexus_ai_agent.adapters.fenced_filesystem_destination import FencedFilesystemEffectDestination
replace = os.replace
def stopped(source, target):
    if str(target).endswith(".effect"):
        print("staged", flush=True)
        sys.stdin.readline()
    replace(source, target)
os.replace = stopped
FencedFilesystemEffectDestination(Path(sys.argv[1]), project_id="project").apply(
    effect_key=sys.argv[2], fencing_epoch=1, payload=b"incomplete"
)
"""
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", program, str(tmp_path), KEY],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], 15)[0], "writer did not stage the effect"
        assert child.stdout.readline().strip() == "staged"
        assert list(tmp_path.rglob("*.effect.stage-*"))
        child.kill()
        child.communicate(timeout=15)
        restarted = FencedFilesystemEffectDestination(tmp_path, project_id="project")
        observed = restarted.inspect(effect_key=KEY, fencing_epoch=1)
        assert observed.status == "ABSENT"
        assert FilesystemObservationVerifier(tmp_path, project_id="project").verify(observed)
        assert not list(tmp_path.rglob("*.stage-*"))
        with pytest.raises(StaleEpochError):
            restarted.apply(effect_key=KEY, fencing_epoch=1, payload=b"incomplete")
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()


def test_destination_rejects_invalid_inputs_and_unprovisioned_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="provisioned"):
        FencedFilesystemEffectDestination(tmp_path / "missing", project_id="project")
    destination = FencedFilesystemEffectDestination(tmp_path, project_id="project")
    for key in ("../escape", "A" * 64):
        with pytest.raises(ValueError, match="effect_key"):
            destination.inspect(effect_key=key, fencing_epoch=1)
    for epoch in (True, 0, -1, "1"):
        with pytest.raises(ValueError, match="fencing_epoch"):
            destination.inspect(effect_key=KEY, fencing_epoch=cast(Any, epoch))
    with pytest.raises(TypeError, match="bytes"):
        destination.apply(effect_key=KEY, fencing_epoch=1, payload=cast(Any, "wrong"))


@pytest.mark.parametrize(
    "metadata",
    [
        "[]",
        "{}",
        '{"highest_epoch":true,"tombstone_epoch":0,"payload_digest":null}',
        '{"highest_epoch":0,"tombstone_epoch":1,"payload_digest":null}',
        '{"highest_epoch":0,"tombstone_epoch":-1,"payload_digest":null}',
        '{"highest_epoch":1,"tombstone_epoch":0,"payload_digest":"bad"}',
    ],
)
def test_destination_rejects_invalid_persisted_metadata(tmp_path: Path, metadata: str) -> None:
    destination = FencedFilesystemEffectDestination(tmp_path, project_id="project")
    destination.inspect(effect_key=KEY, fencing_epoch=1)
    next(tmp_path.rglob("*.json")).write_text(metadata, encoding="utf-8")
    with pytest.raises(DestinationStateError, match="metadata"):
        destination.inspect(effect_key=KEY, fencing_epoch=1)


def test_atomic_write_error_removes_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    destination = FencedFilesystemEffectDestination(tmp_path, project_id="project")

    def reject(_source: Any, _target: Any) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(os, "replace", reject)
    with pytest.raises(OSError, match="rename failed"):
        destination.apply(effect_key=KEY, fencing_epoch=1, payload=b"x")
    assert not list(tmp_path.rglob("*.stage-*"))


def test_verifier_checks_status_and_persistent_destination_state(tmp_path: Path) -> None:
    destination = FencedFilesystemEffectDestination(tmp_path, project_id="project")
    verifier = FilesystemObservationVerifier(tmp_path, project_id="project")
    unknown = ExternalEffectObservation(KEY, 1, "UNKNOWN", "")
    assert not verifier.verify(unknown)
    assert not verifier.verify(cast(Any, object()))
    forged_present = ExternalEffectObservation(KEY, 1, "PRESENT", "f" * 64)
    assert not verifier.verify(forged_present)
    forged_absent = ExternalEffectObservation(KEY, 1, "ABSENT", "f" * 64)
    assert not verifier.verify(forged_absent)
    present = destination.apply(effect_key=KEY, fencing_epoch=1, payload=b"x")
    assert destination.inspect(effect_key=KEY, fencing_epoch=1) == present
    assert not verifier.verify(forged_absent)
