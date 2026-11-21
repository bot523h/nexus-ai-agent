"""Authorization gate for artifact downloads: every refusal returns its typed status.

No FFmpeg is needed: the gate authorizes the bytes on disk against a sealed passport,
so each case builds the passport directly and changes one fact at a time.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from nexus_ai_agent.creative import artifact_download as ad
from nexus_ai_agent.creative import artifact_passport as ap

KEY = "g" * 48
REL = "projects/proj-g/renders/job-g/storyboard.mp4"
BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def _workspace(tmp_path: Path, *, rel: str = REL, body: bytes = BYTES) -> Path:
    root = tmp_path / "ws"
    mp4 = root / rel
    mp4.parent.mkdir(parents=True, exist_ok=True)
    mp4.write_bytes(body)
    passport = ap.build_passport(
        job_id="job-g",
        project_id="proj-g",
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        render_profile_hash="c" * 64,
        asset_merkle_root="d" * 64,
        total_asset_leaves=3,
        output_rel_path=rel,
        output_size_bytes=len(body),
        output_sha256=hashlib.sha256(body).hexdigest(),
        probed_duration_s=1.0,
        verifier_status="PASS",
        created_at_unix_s=1_700_000_000,
        signing_key=KEY,
    )
    ap.write_passport(mp4.parent / "storyboard.passport.json", passport)
    return root


def _refusal(root: Path, rel: str) -> ad.DownloadRefused:
    with pytest.raises(ad.DownloadRefused) as info:
        ad.authorize_artifact_download(root, rel, KEY)
    return info.value


def test_an_honest_artifact_is_authorized(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    auth = ad.authorize_artifact_download(root, REL, KEY)
    assert auth.size_bytes == len(BYTES)
    assert auth.sha256 == hashlib.sha256(BYTES).hexdigest()
    assert b"".join(ad.iter_authorized_chunks(auth.path)) == BYTES


def test_a_path_that_is_not_a_string_or_is_too_long_is_malformed(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    assert _refusal(root, None).status_code == 422  # type: ignore[arg-type]
    assert _refusal(root, "a/" * 200 + "x.mp4").status_code == 422


@pytest.mark.parametrize("rel", ["/etc/x.mp4", "projects\\x.mp4", "a\x00b.mp4", "notes.txt"])
def test_a_malformed_path_is_422(tmp_path: Path, rel: str) -> None:
    assert _refusal(_workspace(tmp_path), rel).status_code == 422


def test_traversal_is_403(tmp_path: Path) -> None:
    refusal = _refusal(_workspace(tmp_path), "projects/../../escape.mp4")
    assert (refusal.status_code, refusal.reason) == (403, "path_traversal")


def test_a_symlink_on_the_path_is_403(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    real = root / REL
    alias_dir = root / "projects" / "alias"
    os.symlink(real.parent, alias_dir)
    refusal = _refusal(root, "projects/alias/storyboard.mp4")
    assert refusal.status_code == 403


def test_a_symlinked_artifact_is_refused_even_when_the_path_is_clean(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    target = root / REL
    moved = target.with_name("real.mp4")
    target.rename(moved)
    os.symlink(moved, target)
    refusal = _refusal(root, REL)
    assert refusal.status_code == 403


def test_an_absent_artifact_is_404(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / REL).unlink()
    refusal = _refusal(root, REL)
    assert (refusal.status_code, refusal.reason) == (404, "artifact_not_found")


def test_a_directory_at_the_artifact_path_is_404(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / REL).unlink()
    (root / REL).mkdir()
    assert _refusal(root, REL).status_code == 404


def test_an_artifact_without_a_passport_is_409(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / REL).parent.joinpath("storyboard.passport.json").unlink()
    refusal = _refusal(root, REL)
    assert (refusal.status_code, refusal.reason) == (409, "passport_missing")


def test_a_passport_that_fails_verification_is_409(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    passport = (root / REL).parent / "storyboard.passport.json"
    passport.write_text("{not json", encoding="utf-8")
    refusal = _refusal(root, REL)
    assert refusal.status_code == 409
    assert refusal.reason.startswith("passport_")


def test_a_sealed_passport_for_another_file_is_409(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    other_rel = "projects/proj-g/renders/job-g/other.mp4"
    (root / other_rel).write_bytes(BYTES)
    # The passport names REL, but it is re-sealed for a different location.
    passport_path = (root / REL).parent / "other.passport.json"
    passport = ap.build_passport(
        job_id="job-g",
        project_id="proj-g",
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        render_profile_hash="c" * 64,
        asset_merkle_root="d" * 64,
        total_asset_leaves=3,
        output_rel_path=REL,
        output_size_bytes=len(BYTES),
        output_sha256=hashlib.sha256(BYTES).hexdigest(),
        probed_duration_s=1.0,
        verifier_status="PASS",
        created_at_unix_s=1_700_000_000,
        signing_key=KEY,
    )
    ap.write_passport(passport_path, passport)
    refusal = _refusal(root, other_rel)
    assert refusal.status_code == 409
    assert refusal.reason == "passport_for_other_artifact"


def test_artifact_rel_path_is_authorized_only_for_its_own_project() -> None:
    row = {"input_data": {"project_id": "proj-g"}, "result": {"output_rel_path": REL}}
    assert ad.artifact_rel_path_for_job(row, "proj-g") == REL
    with pytest.raises(ad.DownloadRefused) as info:
        ad.artifact_rel_path_for_job(row, "someone-else")
    assert info.value.status_code == 404
    with pytest.raises(ad.DownloadRefused) as info:
        ad.artifact_rel_path_for_job(
            {"input_data": {"project_id": "proj-g"}, "result": {}}, "proj-g"
        )
    assert info.value.status_code == 409
    with pytest.raises(ad.DownloadRefused) as info:
        ad.artifact_rel_path_for_job(None, "proj-g")
    assert info.value.status_code == 404


def test_a_fifo_at_the_artifact_path_is_404_not_read(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / REL).unlink()
    os.mkfifo(root / REL)
    assert _refusal(root, REL).status_code == 404


def test_the_containment_check_stands_even_if_the_symlink_walk_is_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.mp4").write_bytes(BYTES)
    link_dir = root / "projects" / "escape"
    os.symlink(outside, link_dir)
    monkeypatch.setattr(ad, "refuse_symlink_chain", lambda _root, _candidate: None)
    refusal = _refusal(root, "projects/escape/x.mp4")
    assert (refusal.status_code, refusal.reason) == (403, "path_outside_workspace")
