"""Independent MP4 verifier and HMAC passport: PASS, VIOLATED and UNOBSERVED paths."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from pathlib import Path

import pytest

from nexus_ai_agent.creative import artifact_passport as ap
from nexus_ai_agent.creative import artifact_verifier as av

KEY = "k" * 40


@pytest.fixture(scope="module")
def real_mp4(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("mp4") / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=10:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000:duration=1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(out),
        ],
        check=True,
        timeout=60,
    )
    return out


# ----------------------------- verifier -----------------------------


def test_real_mp4_passes_all_checks(real_mp4: Path) -> None:
    verdict = av.verify_mp4(real_mp4, expected_duration_s=1.0, tolerance_s=0.2)
    assert verdict.status == av.PASS
    assert verdict.has_video is True
    assert verdict.boxes[:1] == ("ftyp",)
    assert math.isfinite(verdict.probed_duration_s) and verdict.probed_duration_s > 0
    assert verdict.output_sha256 == hashlib.sha256(real_mp4.read_bytes()).hexdigest()


def test_duration_outside_tolerance_is_violated(real_mp4: Path) -> None:
    verdict = av.verify_mp4(real_mp4, expected_duration_s=5.0, tolerance_s=0.2)
    assert verdict.status == av.VIOLATED
    assert any(r.startswith("duration_mismatch") for r in verdict.reasons)


def test_zero_byte_file_is_violated(tmp_path: Path) -> None:
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    verdict = av.verify_mp4(empty, expected_duration_s=1.0, tolerance_s=0.2)
    assert verdict.status == av.VIOLATED
    assert verdict.reasons == ("zero_bytes",)


def test_garbage_bytes_fail_box_layout_and_are_violated(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"this is not an mp4 at all" * 40)
    verdict = av.verify_mp4(junk, expected_duration_s=1.0, tolerance_s=0.2)
    assert verdict.status == av.VIOLATED
    assert any(r.startswith("box_layout") for r in verdict.reasons)


def test_truncated_mp4_is_violated_by_box_walk(real_mp4: Path, tmp_path: Path) -> None:
    cut = tmp_path / "cut.mp4"
    data = real_mp4.read_bytes()
    cut.write_bytes(data[: len(data) - 40])
    verdict = av.verify_mp4(cut, expected_duration_s=1.0, tolerance_s=0.2)
    assert verdict.status == av.VIOLATED


def test_missing_ffprobe_is_unobserved_not_pass(real_mp4: Path) -> None:
    verdict = av.verify_mp4(
        real_mp4,
        expected_duration_s=1.0,
        tolerance_s=0.2,
        ffprobe="/nonexistent/ffprobe-binary",
    )
    assert verdict.status == av.UNOBSERVED
    assert verdict.passed is False


def test_symlink_is_violated_and_not_followed(real_mp4: Path, tmp_path: Path) -> None:
    link = tmp_path / "link.mp4"
    os.symlink(real_mp4, link)
    verdict = av.verify_mp4(link, expected_duration_s=1.0, tolerance_s=0.2)
    assert verdict.status == av.VIOLATED
    assert verdict.reasons == ("symlink_refused",)


def test_missing_file_is_unobserved(tmp_path: Path) -> None:
    verdict = av.verify_mp4(tmp_path / "nope.mp4", expected_duration_s=1.0, tolerance_s=0.2)
    assert verdict.status == av.UNOBSERVED


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, 0.0, -1.0])
def test_non_finite_or_non_positive_expectation_is_violated(real_mp4: Path, bad: float) -> None:
    verdict = av.verify_mp4(real_mp4, expected_duration_s=bad, tolerance_s=0.2)
    assert verdict.status == av.VIOLATED


def test_box_walker_reports_overrun(tmp_path: Path) -> None:
    bad = tmp_path / "overrun.mp4"
    # ftyp declares 1000 bytes but the file is only 16 bytes long.
    bad.write_bytes((1000).to_bytes(4, "big") + b"ftyp" + b"\x00" * 8)
    _, problems = av.read_top_level_boxes(bad)
    assert any("overruns" in p for p in problems)


# ----------------------------- passport -----------------------------


def _passport(mp4: Path, *, status: str = "PASS", key: str = KEY) -> dict:
    digest = hashlib.sha256(mp4.read_bytes()).hexdigest()
    return ap.build_passport(
        job_id="job-1",
        project_id="proj-1",
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        render_profile_hash="c" * 64,
        asset_merkle_root="d" * 64,
        total_asset_leaves=6,
        output_rel_path="projects/proj-1/renders/job-1/storyboard.mp4",
        output_size_bytes=mp4.stat().st_size,
        output_sha256=digest,
        probed_duration_s=1.0,
        verifier_status=status,
        created_at_unix_s=1_700_000_000,
        signing_key=key,
    )


def test_passport_round_trip_verifies(tmp_path: Path, real_mp4: Path) -> None:
    mp4 = tmp_path / "storyboard.mp4"
    mp4.write_bytes(real_mp4.read_bytes())
    pp = tmp_path / "storyboard.passport.json"
    ap.write_passport(pp, _passport(mp4))
    check = ap.verify_artifact_passport(pp, mp4, KEY)
    assert check.passport["verifier_status"] == "PASS"


def test_passport_refuses_to_seal_anything_but_pass(real_mp4: Path) -> None:
    with pytest.raises(ap.PassportError) as info:
        _passport(real_mp4, status="VIOLATED")
    assert info.value.reason == "verifier_not_pass"


def test_passport_seal_detects_field_tamper(tmp_path: Path, real_mp4: Path) -> None:
    mp4 = tmp_path / "storyboard.mp4"
    mp4.write_bytes(real_mp4.read_bytes())
    pp = tmp_path / "storyboard.passport.json"
    data = _passport(mp4)
    data["project_id"] = "someone-else"
    pp.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ap.PassportError) as info:
        ap.verify_artifact_passport(pp, mp4, KEY)
    assert info.value.reason == "seal_mismatch"


def test_passport_wrong_key_is_refused(tmp_path: Path, real_mp4: Path) -> None:
    mp4 = tmp_path / "storyboard.mp4"
    mp4.write_bytes(real_mp4.read_bytes())
    pp = tmp_path / "storyboard.passport.json"
    ap.write_passport(pp, _passport(mp4))
    with pytest.raises(ap.PassportError) as info:
        ap.verify_artifact_passport(pp, mp4, "x" * 40)
    assert info.value.reason == "seal_mismatch"


def test_passport_detects_single_byte_flip_in_artifact(tmp_path: Path, real_mp4: Path) -> None:
    mp4 = tmp_path / "storyboard.mp4"
    mp4.write_bytes(real_mp4.read_bytes())
    pp = tmp_path / "storyboard.passport.json"
    ap.write_passport(pp, _passport(mp4))
    raw = bytearray(mp4.read_bytes())
    raw[-10] ^= 0xFF
    mp4.write_bytes(bytes(raw))
    with pytest.raises(ap.PassportError) as info:
        ap.verify_artifact_passport(pp, mp4, KEY)
    assert info.value.reason == "artifact_mismatch"


def test_passport_rejects_short_signing_key(real_mp4: Path) -> None:
    with pytest.raises(ap.PassportError) as info:
        _passport(real_mp4, key="short")
    assert info.value.reason == "invalid_key"


def test_passport_missing_field_is_malformed(tmp_path: Path, real_mp4: Path) -> None:
    mp4 = tmp_path / "storyboard.mp4"
    mp4.write_bytes(real_mp4.read_bytes())
    pp = tmp_path / "storyboard.passport.json"
    data = _passport(mp4)
    del data["asset_merkle_root"]
    pp.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ap.PassportError) as info:
        ap.verify_artifact_passport(pp, mp4, KEY)
    assert info.value.reason == "passport_malformed"


def test_passport_rejects_non_finite_duration(real_mp4: Path) -> None:
    with pytest.raises(ap.PassportError):
        ap.build_passport(
            job_id="j",
            project_id="p",
            source_sha256="a" * 64,
            manifest_sha256="b" * 64,
            render_profile_hash="c" * 64,
            asset_merkle_root="d" * 64,
            total_asset_leaves=1,
            output_rel_path="x.mp4",
            output_size_bytes=10,
            output_sha256="e" * 64,
            probed_duration_s=math.nan,
            verifier_status="PASS",
            created_at_unix_s=1,
            signing_key=KEY,
        )
