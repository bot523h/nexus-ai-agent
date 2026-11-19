"""Coverage closure for the artifact passport and MP4 verifier refusal paths.

Each test pins one fail-closed branch that the happy-path suite in
``test_artifact_verifier_passport.py`` does not reach: a malformed passport
field, a sealed-but-not-PASS passport, a box layout defect, or an ffprobe
output that does not describe a playable video.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from pathlib import Path

import pytest

from nexus_ai_agent.creative import artifact_passport as ap
from nexus_ai_agent.creative import artifact_verifier as av

KEY = "k" * 40


def _fields(mp4: Path, **overrides: object) -> dict:
    digest = hashlib.sha256(mp4.read_bytes()).hexdigest()
    base = dict(
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
        verifier_status="PASS",
        created_at_unix_s=1_700_000_000,
        signing_key=KEY,
    )
    base.update(overrides)
    return base


def _write_passport(tmp_path: Path, mp4: Path, **overrides: object) -> Path:
    pp = tmp_path / "storyboard.passport.json"
    ap.write_passport(pp, ap.build_passport(**_fields(mp4, **overrides)))  # type: ignore[arg-type]
    return pp


@pytest.fixture()
def clip(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real, PASS-worthy MP4 copied into the test workspace."""
    import subprocess

    src = tmp_path_factory.mktemp("src") / "clip.mp4"
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
            "color=c=red:s=320x180:d=1:r=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(src),
        ],
        check=True,
    )
    dst = tmp_path / "storyboard.mp4"
    dst.write_bytes(src.read_bytes())
    return dst


# ----------------------------- passport builder refusals -----------------------------


def test_signing_key_must_be_text_or_bytes() -> None:
    with pytest.raises(ap.PassportError) as info:
        ap._signing_key(12345)
    assert info.value.reason == "invalid_key"


def test_bytes_signing_key_is_accepted_when_long_enough() -> None:
    assert ap._signing_key(bytearray(b"k" * 40)) == b"k" * 40
    with pytest.raises(ap.PassportError):
        ap._signing_key(b"short")


@pytest.mark.parametrize(
    "override",
    [
        {"source_sha256": "not-hex"},
        {"manifest_sha256": 7},
        {"render_profile_hash": "f" * 63},
        {"asset_merkle_root": ""},
        {"output_sha256": "z" * 64},
    ],
)
def test_build_refuses_a_malformed_digest(clip: Path, tmp_path: Path, override: dict) -> None:
    with pytest.raises(ap.PassportError) as info:
        ap.build_passport(**_fields(clip, **override))
    assert info.value.reason == "invalid_digest"


@pytest.mark.parametrize(
    "override, reason",
    [
        ({"probed_duration_s": True}, "invalid_number"),
        ({"probed_duration_s": "1.0"}, "invalid_number"),
        ({"probed_duration_s": math.nan}, "invalid_number"),
        ({"probed_duration_s": 0.0}, "invalid_number"),
        ({"output_size_bytes": 0}, "invalid_number"),
    ],
)
def test_build_refuses_a_non_positive_or_non_finite_number(
    clip: Path, override: dict, reason: str
) -> None:
    with pytest.raises(ap.PassportError) as info:
        ap.build_passport(**_fields(clip, **override))
    assert info.value.reason == reason


def test_build_refuses_a_passport_that_is_not_a_pass(clip: Path) -> None:
    with pytest.raises(ap.PassportError) as info:
        ap.build_passport(**_fields(clip, verifier_status="VIOLATED"))
    assert info.value.reason == "verifier_not_pass"


# ----------------------------- passport verification refusals -----------------------------


def test_passport_missing_is_refused(clip: Path, tmp_path: Path) -> None:
    with pytest.raises(ap.PassportError) as info:
        ap.verify_artifact_passport(tmp_path / "nope.json", clip, KEY)
    assert info.value.reason == "passport_missing"


def test_passport_larger_than_the_limit_is_refused(clip: Path, tmp_path: Path) -> None:
    pp = tmp_path / "huge.passport.json"
    pp.write_bytes(b" " * (ap.MAX_PASSPORT_BYTES + 1))
    with pytest.raises(ap.PassportError) as info:
        ap.verify_artifact_passport(pp, clip, KEY)
    assert info.value.reason == "passport_size"


def test_passport_that_is_not_utf8_json_is_unreadable(clip: Path, tmp_path: Path) -> None:
    pp = tmp_path / "bad.passport.json"
    pp.write_bytes(b"\xff\xfe not json")
    with pytest.raises(ap.PassportError) as info:
        ap.verify_artifact_passport(pp, clip, KEY)
    assert info.value.reason == "passport_unreadable"


def test_passport_that_is_a_json_list_is_malformed(clip: Path, tmp_path: Path) -> None:
    pp = tmp_path / "list.passport.json"
    pp.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ap.PassportError) as info:
        ap.verify_artifact_passport(pp, clip, KEY)
    assert info.value.reason == "passport_malformed"


def test_passport_with_another_version_is_refused(clip: Path, tmp_path: Path) -> None:
    pp = _write_passport(tmp_path, clip)
    data = json.loads(pp.read_text(encoding="utf-8"))
    data["passport_version"] = 99
    pp.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ap.PassportError) as info:
        ap.verify_artifact_passport(pp, clip, KEY)
    assert info.value.reason == "passport_version_unsupported"


def test_a_validly_sealed_non_pass_passport_is_refused(clip: Path, tmp_path: Path) -> None:
    """The seal holds, so only the verdict check can stop a VIOLATED passport."""
    # Version 2 contract: start from a real, fully sealed v2 passport (per-shot
    # Merkle tree included), flip the verdict and re-seal. Only the verdict
    # check can then refuse it.
    pp = _write_passport(tmp_path, clip)
    passport = json.loads(pp.read_text(encoding="utf-8"))
    passport["verifier_status"] = "VIOLATED"
    passport["hmac_sha256"] = ap._seal(passport, ap._signing_key(KEY))
    violated = tmp_path / "violated.passport.json"
    violated.write_text(json.dumps(passport), encoding="utf-8")
    with pytest.raises(ap.PassportError) as info:
        ap.verify_artifact_passport(violated, clip, KEY)
    assert info.value.reason == "verifier_not_pass"


def test_passport_that_is_a_symlink_is_refused(clip: Path, tmp_path: Path) -> None:
    real = _write_passport(tmp_path, clip)
    link = tmp_path / "linked.passport.json"
    os.symlink(real, link)
    with pytest.raises(ap.PassportError) as info:
        ap.verify_artifact_passport(link, clip, KEY)
    assert info.value.reason == "passport_symlink_refused"


def test_artifact_missing_or_symlinked_is_refused(clip: Path, tmp_path: Path) -> None:
    pp = _write_passport(tmp_path, clip)
    gone = tmp_path / "gone.mp4"
    with pytest.raises(ap.PassportError) as info:
        ap.verify_artifact_passport(pp, gone, KEY)
    assert info.value.reason == "artifact_missing"

    decoy = tmp_path / "decoy.mp4"
    decoy.write_bytes(clip.read_bytes())
    link = tmp_path / "link.mp4"
    os.symlink(decoy, link)
    with pytest.raises(ap.PassportError) as info:
        ap.verify_artifact_passport(pp, link, KEY)
    assert info.value.reason == "artifact_symlink_refused"


def test_artifact_that_is_a_directory_is_not_regular(clip: Path, tmp_path: Path) -> None:
    pp = _write_passport(tmp_path, clip)
    folder = tmp_path / "dir.mp4"
    folder.mkdir()
    with pytest.raises(ap.PassportError) as info:
        ap.verify_artifact_passport(pp, folder, KEY)
    assert info.value.reason == "artifact_not_regular"


def test_lstat_regular_classifies_a_fifo_as_not_regular(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ap.PassportError) as info:
        ap._lstat_regular(fifo, "artifact")
    assert info.value.reason == "artifact_not_regular"
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)


def test_a_sealed_passport_with_a_wrong_output_size_is_refused(clip: Path, tmp_path: Path) -> None:
    pp = _write_passport(tmp_path, clip, output_size_bytes=clip.stat().st_size + 1)
    with pytest.raises(ap.PassportError):
        ap.verify_artifact_passport(pp, clip, KEY)


# ----------------------------- MP4 box walker -----------------------------


def _box(kind: bytes, payload: bytes = b"") -> bytes:
    import struct

    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def test_box_walker_reports_a_truncated_header(tmp_path: Path) -> None:
    f = tmp_path / "short.mp4"
    f.write_bytes(_box(b"ftyp") + b"\x00\x00")
    _, problems = av.read_top_level_boxes(f)
    assert any("truncated box header" in p for p in problems)


def test_box_walker_reads_a_largesize_box(tmp_path: Path) -> None:
    import struct

    f = tmp_path / "large.mp4"
    payload = b"x" * 8
    large = struct.pack(">I4sQ", 1, b"free", 16 + len(payload)) + payload
    f.write_bytes(_box(b"ftyp") + large)
    boxes, problems = av.read_top_level_boxes(f)
    assert "free" in boxes
    assert not any("largesize" in p for p in problems)


def test_box_walker_reports_a_truncated_largesize(tmp_path: Path) -> None:
    import struct

    f = tmp_path / "trunc-large.mp4"
    f.write_bytes(_box(b"ftyp") + struct.pack(">I4s", 1, b"free")[:8])
    _, problems = av.read_top_level_boxes(f)
    assert any("truncated largesize" in p for p in problems)


def test_box_walker_treats_size_zero_as_running_to_end_of_file(tmp_path: Path) -> None:
    import struct

    f = tmp_path / "open.mp4"
    f.write_bytes(_box(b"ftyp") + struct.pack(">I4s", 0, b"mdat") + b"y" * 12)
    boxes, _ = av.read_top_level_boxes(f)
    assert boxes[-1] == "mdat"


def test_box_walker_refuses_a_box_that_overruns_the_file(tmp_path: Path) -> None:
    import struct

    f = tmp_path / "overrun.mp4"
    f.write_bytes(_box(b"ftyp") + struct.pack(">I4s", 999, b"mdat"))
    _, problems = av.read_top_level_boxes(f)
    assert any("overruns" in p for p in problems)


# ----------------------------- verify_mp4 edge verdicts -----------------------------


def _fake_ffprobe(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-ffprobe"
    script.write_text(f"#!/bin/sh\nprintf '%s' '{body}'\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def test_a_directory_at_the_artifact_path_is_violated(tmp_path: Path) -> None:
    d = tmp_path / "dir.mp4"
    d.mkdir()
    verdict = av.verify_mp4(d, expected_duration_s=1.0, tolerance_s=0.2)
    assert verdict.status == av.VIOLATED
    assert "not_regular_file" in verdict.reasons


def test_an_empty_file_is_violated_as_zero_bytes(tmp_path: Path) -> None:
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    verdict = av.verify_mp4(empty, expected_duration_s=1.0, tolerance_s=0.2)
    assert verdict.status == av.VIOLATED
    assert "zero_bytes" in verdict.reasons


def test_an_ffprobe_failure_is_unobserved(clip: Path, tmp_path: Path) -> None:
    failing = tmp_path / "fail-ffprobe"
    failing.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    failing.chmod(failing.stat().st_mode | stat.S_IXUSR)
    verdict = av.verify_mp4(clip, expected_duration_s=1.0, tolerance_s=0.2, ffprobe=str(failing))
    assert verdict.status == av.UNOBSERVED


def test_ffprobe_output_that_is_not_utf8_is_unobserved(clip: Path, tmp_path: Path) -> None:
    binary = tmp_path / "bin-ffprobe"
    binary.write_text("#!/bin/sh\nprintf '\\377\\376'\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    verdict = av.verify_mp4(clip, expected_duration_s=1.0, tolerance_s=0.2, ffprobe=str(binary))
    assert verdict.status == av.UNOBSERVED


def test_ffprobe_json_that_is_not_an_object_is_unobserved(clip: Path, tmp_path: Path) -> None:
    listing = _fake_ffprobe(tmp_path, "[]")
    verdict = av.verify_mp4(clip, expected_duration_s=1.0, tolerance_s=0.2, ffprobe=listing)
    assert verdict.status == av.UNOBSERVED


def test_ffprobe_without_a_video_stream_is_violated(clip: Path, tmp_path: Path) -> None:
    body = json.dumps({"streams": [{"codec_type": "audio"}], "format": {"duration": "1.0"}})
    verdict = av.verify_mp4(
        clip, expected_duration_s=1.0, tolerance_s=0.2, ffprobe=_fake_ffprobe(tmp_path, body)
    )
    assert verdict.status == av.VIOLATED
    assert "no_video_stream" in verdict.reasons


def test_ffprobe_without_a_usable_duration_is_violated(clip: Path, tmp_path: Path) -> None:
    body = json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": "oops"}})
    verdict = av.verify_mp4(
        clip, expected_duration_s=1.0, tolerance_s=0.2, ffprobe=_fake_ffprobe(tmp_path, body)
    )
    assert verdict.status == av.VIOLATED
    assert "probed_duration_not_positive_finite" in verdict.reasons


def test_a_non_numeric_probed_duration_is_treated_as_missing(clip: Path, tmp_path: Path) -> None:
    body = json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": [1]}})
    verdict = av.verify_mp4(
        clip, expected_duration_s=1.0, tolerance_s=0.2, ffprobe=_fake_ffprobe(tmp_path, body)
    )
    assert "probed_duration_not_positive_finite" in verdict.reasons


def test_a_file_that_does_not_start_with_ftyp_is_violated(tmp_path: Path) -> None:
    f = tmp_path / "no-ftyp.mp4"
    f.write_bytes(_box(b"free") + _box(b"moov") + _box(b"mdat"))
    boxes, problems = av.read_top_level_boxes(f)
    assert boxes[0] == "free"
    assert "first box is not ftyp" in problems


def test_a_non_positive_tolerance_is_violated(clip: Path) -> None:
    verdict = av.verify_mp4(clip, expected_duration_s=1.0, tolerance_s=0.0)
    assert verdict.status == av.VIOLATED
    assert "tolerance_not_positive_finite" in verdict.reasons
