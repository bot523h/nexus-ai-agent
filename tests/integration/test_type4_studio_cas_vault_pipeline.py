"""Type-4 studio pipeline E2E: storyboard -> SegmentCAS shot cache -> FFmpeg -> passport -> vault.

These tests run the real compiler, real FFmpeg (``libx264``), the real
content-addressed segment store and the real encrypted vault driver on disk.
Nothing is mocked: a cache hit must genuinely restore the previous segment bytes
and skip the encoder, and a vault round trip must genuinely re-encrypt, dedupe,
restore and re-verify the film.

FFmpeg and ffprobe are taken from ``PATH`` (``NEXUS_FFMPEG`` / ``NEXUS_FFPROBE``
override them); the module is skipped when either binary is unavailable, exactly
like the other real-media tests in this suite.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from nexus_ai_agent.creative import materializer as mz
from nexus_ai_agent.creative import studio_cli
from nexus_ai_agent.creative.artifact_passport import (
    PASSPORT_SUFFIX,
    verify_artifact_passport,
)
from nexus_ai_agent.creative.artifact_verifier import PASS, verify_mp4
from nexus_ai_agent.creative.studio_cas_vault_bridge import (
    SHOT_SEGMENT_SCHEMA,
    CasStoryboardRenderResult,
    render_storyboard_with_segment_cas,
    shot_cache_key_hash,
    sync_and_verify_studio_bundle_in_vault,
)

SIGNING_KEY = "s" * 48
VAULT_SECRET = b"studio-vault-master-secret"
SCRIPT = (
    "INT. KITCHEN - DAY\n"
    "Sara: Hello, are you there?\n"
    "Ali: Yes, I am here.\n"
    "EXT. STREET - NIGHT\n"
    "سارا: سلام، حالت چطوره؟\n"
)
#: Only the middle shot's line changes (longer), so shots 1 and 3 keep their bytes.
SCRIPT_SHOT_2_CHANGED = SCRIPT.replace("Yes, I am here.", "Yes, I am here and waiting.")
PROFILE = {"fps": 12, "width": 320, "height": 180}

FFMPEG = os.environ.get("NEXUS_FFMPEG") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("NEXUS_FFPROBE") or shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="real ffmpeg and ffprobe are required for the Type-4 media pipeline",
)


def _render(
    root: Path,
    *,
    script: str = SCRIPT,
    tag: str = "a",
    cas: Path | None = None,
    images: dict[str, Path] | None = None,
) -> CasStoryboardRenderResult:
    return render_storyboard_with_segment_cas(
        script,
        f"Type-4 {tag}",
        root / "renders" / tag,
        cas if cas is not None else root / "cas",
        f"job-{tag}",
        f"approval-{tag}",
        "owner-1",
        fps=PROFILE["fps"],
        width=PROFILE["width"],
        height=PROFILE["height"],
        local_images=images,
        signing_key=SIGNING_KEY,
        project_id="proj-type4",
        ffmpeg=FFMPEG or "ffmpeg",
        ffprobe=FFPROBE or "ffprobe",
        clock=lambda: 1_700_000_000.0,
    )


def _assert_render_verified(result: CasStoryboardRenderResult) -> None:
    """Every seal the pipeline produces must verify independently."""
    assert result.verification.status == PASS
    assert result.artifact.shot_hashes
    assert len(result.passport["shot_inclusion_proofs"]) == len(result.artifact.shot_hashes)
    check = verify_artifact_passport(result.passport_path, result.output_mp4_path, SIGNING_KEY)
    assert check.passport["shot_merkle_root"] == result.artifact.shot_merkle_root
    verified = verify_mp4(
        result.output_mp4_path,
        expected_duration_s=result.artifact.total_duration_s,
        tolerance_s=0.05 + len(result.artifact.shot_hashes) / result.artifact.profile.fps,
        ffprobe=FFPROBE or "ffprobe",
    )
    assert verified.status == PASS
    assert verified.output_sha256 == result.verification.output_sha256


def test_incremental_cas_storyboard_render_bypasses_unchanged_shots_and_verifies(
    tmp_path: Path,
) -> None:
    """Cold render -> warm render -> one-shot edit: 0/3, then 3/0, then 2/1."""
    first = _render(tmp_path, tag="a")
    assert (first.cas_hits, first.cas_misses) == (0, 3)
    assert len(first.artifact.shot_hashes) == 3
    _assert_render_verified(first)
    for index, proof in enumerate(first.passport["shot_inclusion_proofs"]):
        assert proof["leaf_index"] == index
        assert proof["leaf_hash"] == first.artifact.shot_hashes[index]
        assert proof["root_hash"] == first.artifact.shot_merkle_root
        assert proof["total_leaves"] == 3
    assert first.peak_memory_bytes < 120 * 1024 * 1024

    # Second pass over the identical screenplay: every shot comes back from the CAS.
    second = _render(tmp_path, tag="b")
    assert (second.cas_hits, second.cas_misses) == (3, 0)
    _assert_render_verified(second)
    assert second.artifact.shot_merkle_root == first.artifact.shot_merkle_root
    assert second.shot_segment_hashes == first.shot_segment_hashes
    assert second.verification.output_sha256 == first.verification.output_sha256

    # Third pass edits only shot 2: exactly one re-render, two cache restorations.
    third = _render(tmp_path, script=SCRIPT_SHOT_2_CHANGED, tag="c")
    assert (third.cas_hits, third.cas_misses) == (2, 1)
    _assert_render_verified(third)
    assert third.artifact.shot_merkle_root != first.artifact.shot_merkle_root
    unchanged = {0, 2}
    for index in unchanged:
        assert third.artifact.shot_hashes[index] == first.artifact.shot_hashes[index]
        assert third.shot_segment_hashes[index] == first.shot_segment_hashes[index]
    assert third.artifact.shot_hashes[1] != first.artifact.shot_hashes[1]
    assert third.verification.output_sha256 != first.verification.output_sha256

    # The cache really is a cache: the index maps shot keys to CAS content hashes.
    index_dir = tmp_path / "cas" / "shot_index"
    entries = sorted(index_dir.glob("*.json"))
    assert len(entries) == 4  # three original shots + the edited second shot
    for entry_path in entries:
        entry = json.loads(entry_path.read_text())
        assert entry["schema"] == SHOT_SEGMENT_SCHEMA
        assert entry_path.stem == entry["segment_key_hash"]
        assert len(entry["cas_key"]) == 64 and len(entry["content_sha"]) == 64


def test_cache_key_is_domain_separated_and_content_addressed() -> None:
    """The key must be a stable, domain-separated SHA-256 of the shot's inputs."""
    kwargs = {
        "shot_id": "s000-sh000",
        "shot_content_hash": "a" * 64,
        "duration_ms": 1500,
        "fps": 12,
        "width": 320,
        "height": 180,
        "image_sha256": "b" * 64,
    }
    key = shot_cache_key_hash(**kwargs)
    assert key == shot_cache_key_hash(**kwargs)
    assert (
        key
        == hashlib.sha256(
            b"nagar-shot-cas-key-v1\x00"
            + mz.canonical_json(
                {
                    "schema": "nagar-shot-segment-v1",
                    "shot_id": "s000-sh000",
                    "shot_content_hash": "a" * 64,
                    "duration_ms": 1500,
                    "fps": repr(12.0),
                    "width": 320,
                    "height": 180,
                    "image_sha256": "b" * 64,
                }
            )
        ).hexdigest()
    )
    assert shot_cache_key_hash(**{**kwargs, "duration_ms": 1501}) != key
    assert shot_cache_key_hash(**{**kwargs, "image_sha256": "c" * 64}) != key


def test_local_image_override_changes_only_that_shots_segment(tmp_path: Path) -> None:
    """A swapped plate for one shot invalidates that shot alone."""
    base = _render(tmp_path, tag="a")
    plate = tmp_path / "plate.png"
    mz.write_slate_png(plate, width=320, height=180, seed=b"\x07" * 32, label="plate")
    swapped = _render(tmp_path, tag="b", images={base.artifact.assets[0].shot_id: plate})
    assert (swapped.cas_hits, swapped.cas_misses) == (2, 1)
    _assert_render_verified(swapped)
    assert swapped.artifact.shot_hashes[0] != base.artifact.shot_hashes[0]
    assert swapped.artifact.shot_hashes[1:] == base.artifact.shot_hashes[1:]
    assert swapped.artifact.assets[0].kind == "local_image"


def test_vault_encrypted_backup_and_restore_preserves_mp4_and_merkle_passport(
    tmp_path: Path,
) -> None:
    """Encrypt -> push -> pull -> re-verify: bytes and every Merkle proof survive."""
    result = _render(tmp_path, tag="a")
    restore_dir = tmp_path / "restored"
    report = sync_and_verify_studio_bundle_in_vault(
        render_result=result,
        vault_storage_dir=tmp_path / "vault",
        vault_journal_db_path=tmp_path / "journal" / "vault_journal.jsonl",
        restore_dir=restore_dir,
        master_secret=VAULT_SECRET,
        project_id="proj-type4",
        signing_key=SIGNING_KEY,
        device_id="studio-test-node",
        ffprobe=FFPROBE or "ffprobe",
    )

    assert report.snapshot_seq == 1
    assert report.manifest_id
    assert report.pushed_chunks >= 1
    assert report.restored_files_count == 3
    assert report.restored_passport_verification.passport["shot_merkle_root"] == (
        result.artifact.shot_merkle_root
    )
    assert report.restored_artifact_verification.status == PASS
    assert report.restored_mp4_path.read_bytes() == result.output_mp4_path.read_bytes()
    assert report.restored_passport_path.name == result.passport_path.name

    # The restored MP4 is what the sealed passport is about, byte for byte.
    restored_check = verify_artifact_passport(
        report.restored_passport_path, report.restored_mp4_path, SIGNING_KEY
    )
    assert (
        restored_check.passport["output_sha256"]
        == hashlib.sha256(report.restored_mp4_path.read_bytes()).hexdigest()
    )
    assert len(restored_check.passport["shot_inclusion_proofs"]) == 3

    # The vault directory really holds encrypted chunks, not plaintext MP4s.
    chunk_bytes = b"".join(
        path.read_bytes() for path in (tmp_path / "vault" / "chunks").iterdir() if path.is_file()
    )
    assert result.output_mp4_path.read_bytes() not in chunk_bytes

    # A second sync is sequenced and deduplicated by the encrypted vault.
    second = sync_and_verify_studio_bundle_in_vault(
        render_result=result,
        vault_storage_dir=tmp_path / "vault",
        vault_journal_db_path=tmp_path / "journal" / "vault_journal.jsonl",
        restore_dir=tmp_path / "restored2",
        master_secret=VAULT_SECRET,
        project_id="proj-type4",
        signing_key=SIGNING_KEY,
        ffprobe=FFPROBE or "ffprobe",
    )
    assert second.snapshot_seq == 2
    assert second.pushed_chunks == 0
    assert second.deduplicated_chunks >= 1
    assert second.restored_artifact_verification.status == PASS

    journal = (tmp_path / "journal" / "vault_journal.jsonl").read_text().splitlines()
    assert [json.loads(line)["snapshot_seq"] for line in journal] == [1, 2]


def test_vault_restore_of_a_tampered_chunk_is_refused(tmp_path: Path) -> None:
    """Fail-closed: a single flipped byte in the vault must abort the restore."""
    from nexus_ai_agent.creative.studio_cas_vault_bridge import StudioBridgeError

    result = _render(tmp_path, tag="a")
    sync_and_verify_studio_bundle_in_vault(
        render_result=result,
        vault_storage_dir=tmp_path / "vault",
        vault_journal_db_path=tmp_path / "journal.jsonl",
        restore_dir=tmp_path / "restored",
        master_secret=VAULT_SECRET,
        project_id="proj-type4",
        signing_key=SIGNING_KEY,
        ffprobe=FFPROBE or "ffprobe",
    )
    chunk = next(p for p in (tmp_path / "vault" / "chunks").iterdir() if p.is_file())
    raw = bytearray(chunk.read_bytes())
    raw[-1] ^= 0xFF
    chunk.write_bytes(bytes(raw))

    with pytest.raises(StudioBridgeError) as info:
        sync_and_verify_studio_bundle_in_vault(
            render_result=result,
            vault_storage_dir=tmp_path / "vault",
            vault_journal_db_path=tmp_path / "journal2.jsonl",
            restore_dir=tmp_path / "restored-tampered",
            master_secret=VAULT_SECRET,
            project_id="proj-type4",
            signing_key=SIGNING_KEY,
            ffprobe=FFPROBE or "ffprobe",
        )
    assert info.value.reason in {"vault_pull_failed", "vault_restored_bytes_differ"}


def test_studio_cli_with_cas_and_vault_flags_end_to_end(tmp_path: Path) -> None:
    """The CLI drives the same Type-4 pipeline and reports real cache/vault stats."""
    out = tmp_path / "cli-out"
    script_file = tmp_path / "script.txt"
    script_file.write_text(SCRIPT, encoding="utf-8")
    cas_dir = tmp_path / "cli-cas"
    vault_dir = tmp_path / "cli-vault"
    argv = [
        "render",
        "--script-file",
        str(script_file),
        "--title",
        "CLI type-4",
        "--job-id",
        "cli-job",
        "--project-id",
        "proj-type4",
        "--output-dir",
        str(out),
        "--fps",
        "12",
        "--width",
        "320",
        "--height",
        "180",
        "--cas-dir",
        str(cas_dir),
        "--vault-dir",
        str(vault_dir),
        "--vault-journal",
        str(tmp_path / "cli-journal.jsonl"),
        "--restore-dir",
        str(tmp_path / "cli-restored"),
        "--vault-secret",
        VAULT_SECRET.decode(),
        "--signing-key",
        SIGNING_KEY,
        "--ffmpeg",
        FFMPEG or "ffmpeg",
        "--ffprobe",
        FFPROBE or "ffprobe",
    ]

    import io

    buffer = io.StringIO()
    code = studio_cli.run_studio_cli(argv, out=buffer)
    assert code == 0, buffer.getvalue()
    payload = json.loads(buffer.getvalue())
    assert payload["status"] == "rendered"
    assert payload["verifier_status"] == PASS
    assert (payload["cas_hits"], payload["cas_misses"]) == (0, 3)
    assert payload["vault_snapshot_seq"] == 1
    assert payload["vault_restored_verified"] is True
    assert payload["shot_inclusion_proof_count"] == 3
    assert Path(payload["output_path"]).is_file()
    assert Path(payload["passport_path"]).is_file()
    assert Path(payload["manifest_path"]).is_file()
    assert Path(payload["vault_restored_mp4_path"]).is_file()

    # Second CLI run: the shot cache answers, the vault stays encrypted and deduped.
    second = io.StringIO()
    assert studio_cli.run_studio_cli(argv, out=second) == 0
    again = json.loads(second.getvalue())
    assert (again["cas_hits"], again["cas_misses"]) == (3, 0)
    assert again["vault_snapshot_seq"] == 2
    assert again["vault_restored_verified"] is True

    # The CLI's own main() wrapper is the same entry point.
    assert studio_cli.main(["init", "--workspace", str(tmp_path / "ws")]) == 0
    passport_path = Path(again["passport_path"])
    assert passport_path.name.endswith(PASSPORT_SUFFIX)
    on_disk = json.loads(passport_path.read_text())
    assert on_disk["verifier_status"] == PASS
    assert on_disk["shot_merkle_root"] == again["shot_merkle_root"]
