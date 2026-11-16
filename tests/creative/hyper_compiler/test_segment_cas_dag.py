"""Segment-CAS DAG, RFC 6962 Merkle tree and artifact-integrity tests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
from pathlib import Path

import pytest

from nexus_ai_agent.creative.hyper_compiler import segment_cas_dag as dag
from nexus_ai_agent.creative.hyper_compiler.errors import (
    HyperCompilerError,
    SegmentIntegrityError,
)
from nexus_ai_agent.creative.hyper_compiler.segment_cas_dag import (
    PlannedSegment,
    SegmentCASPlanner,
    SegmentPlan,
    SegmentStatus,
    TimelineSegmentSpec,
    VerifiedSegment,
    atomic_write_bytes,
    canonical_spec_bytes,
    merkle_root,
    merkle_root_hex,
    segment_cas_key,
    timeline_merkle_root,
    validate_cas_key,
)


def make_spec(
    index: int = 0, *, dna: str = "dna", width: int = 64, height: int = 48
) -> TimelineSegmentSpec:
    return TimelineSegmentSpec(
        segment_id=f"seg{index}",
        start_frame=index * 10,
        end_frame=(index + 1) * 10,
        fps=24.0,
        width=width,
        height=height,
        shot_dna_digest=dna,
        camera_digest="cam",
        lighting_digest="lit",
        audio_digest="aud",
        audio_filter_digest="afx",
    )


def make_timeline(count: int) -> list[TimelineSegmentSpec]:
    return [make_spec(i) for i in range(count)]


# ── TimelineSegmentSpec ─────────────────────────────────────────────────────
def test_spec_accepts_valid_segment() -> None:
    spec = make_spec(3)
    assert spec.frame_count == 10
    assert spec.segment_id == "seg3"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"segment_id": ""},
        {"start_frame": -1},
        {"end_frame": 0},
        {"fps": 0.0},
        {"fps": float("nan")},
        {"width": 0},
        {"height": -1},
        {"shot_dna_digest": ""},
        {"camera_digest": ""},
        {"lighting_digest": ""},
        {"audio_digest": ""},
        {"audio_filter_digest": ""},
    ],
)
def test_spec_rejects_invalid_fields(kwargs: dict[str, object]) -> None:
    base: dict[str, object] = {
        "segment_id": "s",
        "start_frame": 0,
        "end_frame": 5,
        "fps": 24.0,
        "width": 64,
        "height": 48,
        "shot_dna_digest": "a",
        "camera_digest": "b",
        "lighting_digest": "c",
        "audio_digest": "d",
        "audio_filter_digest": "e",
    }
    base.update(kwargs)
    with pytest.raises(HyperCompilerError):
        TimelineSegmentSpec(**base)  # type: ignore[arg-type]


# ── content-addressed keys ──────────────────────────────────────────────────
def test_canonical_spec_bytes_is_deterministic() -> None:
    spec = make_spec(1)
    assert canonical_spec_bytes(spec) == canonical_spec_bytes(spec)
    assert canonical_spec_bytes(spec) != canonical_spec_bytes(make_spec(2))


def test_canonical_spec_bytes_folds_pipeline_digest() -> None:
    spec = make_spec(1)
    assert canonical_spec_bytes(spec) != canonical_spec_bytes(spec, pipeline_digest="abc")


def test_segment_cas_key_changes_with_any_field() -> None:
    base = segment_cas_key(make_spec(1))
    assert base == segment_cas_key(make_spec(1))
    assert base != segment_cas_key(make_spec(1, dna="other"))


def test_segment_cas_key_changes_with_pipeline_digest() -> None:
    spec = make_spec(1)
    assert segment_cas_key(spec) != segment_cas_key(spec, pipeline_digest="brightness=0.2")


def test_segment_cas_key_hmac_mode() -> None:
    plain = segment_cas_key(make_spec(1))
    keyed = segment_cas_key(make_spec(1), hmac_key=b"secret")
    assert keyed != plain
    assert keyed == segment_cas_key(make_spec(1), hmac_key=b"secret")
    assert len(keyed) == 64


def test_segment_cas_key_folds_mux_mode_and_renderer() -> None:
    spec = make_spec(1)
    cpu = segment_cas_key(spec, mux_mode="deterministic_cpu_mux")
    ffmpeg = segment_cas_key(spec, mux_mode="ffmpeg_subprocess_pipe")
    assert cpu != ffmpeg
    # A different renderer version must also change the key.
    assert segment_cas_key(spec, renderer_version="v1") != segment_cas_key(
        spec, renderer_version="v2"
    )


# ── RFC 6962 Merkle tree ────────────────────────────────────────────────────
def test_merkle_root_of_empty_tree_is_sha256_of_empty() -> None:
    assert merkle_root([]) == hashlib.sha256(b"").digest()


def test_merkle_root_single_leaf_is_prefixed_hash() -> None:
    assert merkle_root([b"x"]) == hashlib.sha256(b"\x00x").digest()


# Independent reference implementation (RFC 6962 §2.1) used to pin the tree
# against golden vectors for 0/1/2/3/5/7 leaves; the odd counts exercise the
# promote-not-duplicate tail.
def _reference_mth(data: list[bytes]) -> bytes:
    n = len(data)
    if n == 0:
        return hashlib.sha256(b"").digest()
    if n == 1:
        return hashlib.sha256(b"\x00" + data[0]).digest()
    k = 1
    while k * 2 < n:
        k *= 2
    left = _reference_mth(data[:k])
    right = _reference_mth(data[k:])
    return hashlib.sha256(b"\x01" + left + right).digest()


@pytest.mark.parametrize("count", [0, 1, 2, 3, 5, 7])
def test_merkle_root_matches_reference_golden_vectors(count: int) -> None:
    leaves = [bytes([0x61 + i]) for i in range(count)]
    assert merkle_root(leaves) == _reference_mth(leaves)
    assert merkle_root_hex(leaves) == _reference_mth(leaves).hex()


def test_merkle_root_odd_leaf_promotes_without_duplication() -> None:
    leaves = [b"a", b"b", b"c", b"d", b"e"]
    # A "duplicate the last node" construction would yield a different root for
    # the odd tail, so the two constructions must disagree.
    dup_leaves = [b"a", b"b", b"c", b"d", b"e", b"e"]
    assert merkle_root(leaves) != merkle_root(dup_leaves)


def test_merkle_root_balanced_tree_is_order_sensitive() -> None:
    assert merkle_root([b"a", b"b"]) != merkle_root([b"b", b"a"])
    assert merkle_root([b"a", b"b", b"c", b"d"]) != merkle_root([b"a", b"b", b"c"])


def test_merkle_hash_nodes_handles_single_and_power_of_two() -> None:
    single = dag._merkle_hash_nodes([b"only"])
    assert single == b"only"
    pair = dag._merkle_hash_nodes([b"l", b"r"])
    assert pair == dag._merkle_node_hash(b"l", b"r")


# ── timeline Merkle root ────────────────────────────────────────────────────
def test_timeline_merkle_root_binds_leaf_count() -> None:
    keys = [segment_cas_key(spec) for spec in make_timeline(3)]
    default = timeline_merkle_root(keys)
    assert default == timeline_merkle_root(keys, total_leaves=3)
    assert default != timeline_merkle_root(keys, total_leaves=4)


def test_timeline_merkle_root_rejects_negative_bound() -> None:
    with pytest.raises(HyperCompilerError):
        timeline_merkle_root([], total_leaves=-1)


def test_timeline_merkle_root_empty_is_stable() -> None:
    assert timeline_merkle_root([]) == timeline_merkle_root([], total_leaves=0)


# ── cas key validation & path containment (P1-3) ────────────────────────────
def test_validate_cas_key_accepts_hex() -> None:
    assert validate_cas_key("a" * 64) == "a" * 64


@pytest.mark.parametrize(
    "bad",
    [
        "z" * 64,  # non-hex letters
        "A" * 64,  # uppercase
        "a" * 63,
        "a" * 65,
        "../" + "a" * 61,  # traversal, still 64 chars
        "a" * 60 + "/../",  # traversal, still 64 chars
        "",
        "a" * 62 + "00"[:-1],
    ],
)
def test_validate_cas_key_rejects_non_canonical(bad: str) -> None:
    with pytest.raises(SegmentIntegrityError):
        validate_cas_key(bad)


@pytest.mark.parametrize("traversal", ["../" + "a" * 61, "a" * 60 + "/../"])
def test_artifact_and_manifest_paths_reject_traversal(tmp_path: Path, traversal: str) -> None:
    planner = SegmentCASPlanner(tmp_path)
    with pytest.raises(SegmentIntegrityError):
        planner.artifact_path(traversal)
    with pytest.raises(SegmentIntegrityError):
        planner.manifest_path(traversal)


@pytest.mark.parametrize("traversal", ["../" + "a" * 61, "a" * 60 + "/../"])
def test_write_segment_artifact_rejects_traversal(tmp_path: Path, traversal: str) -> None:
    planner = SegmentCASPlanner(tmp_path)
    with pytest.raises(SegmentIntegrityError):
        planner.write_segment_artifact(traversal, b"body")


def test_verify_segment_artifact_rejects_traversal(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    with pytest.raises(SegmentIntegrityError):
        planner.verify_segment_artifact("../" + "a" * 61)


def test_read_segment_artifact_rejects_traversal(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    with pytest.raises(SegmentIntegrityError):
        planner.read_segment_artifact("../" + "a" * 61)


def test_safe_cache_path_rejects_escape(tmp_path: Path) -> None:
    # The containment guard is the last line of defence: even a name that slips
    # past key validation must not resolve outside the cache directory.
    planner = SegmentCASPlanner(tmp_path / "cache")
    planner.cache_dir.mkdir(parents=True)
    with pytest.raises(SegmentIntegrityError):
        planner._safe_cache_path("../escape.nagarseg")
    assert planner._safe_cache_path("safe.nagarseg") == planner.cache_dir / "safe.nagarseg"


def test_commit_stream_rejects_negative_size(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    with pytest.raises(SegmentIntegrityError):
        planner.commit_stream("a" * 64, io.BytesIO(b"x"), size=-1, digest="00" * 32)


def test_verify_and_open_rejects_content_hash_mismatch(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    key = "d" * 64
    planner.write_segment_artifact(key, b"payload")
    # Corrupt the body without touching the manifest: the recomputed content
    # hash must disagree with the header hash.
    artifact = planner.artifact_path(key)
    raw = bytearray(artifact.read_bytes())
    raw[-1] ^= 0xFF
    artifact.write_bytes(bytes(raw))
    with pytest.raises(SegmentIntegrityError):
        planner.read_segment_artifact(key)


def test_verify_and_open_rejects_file_hash_mismatch(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    key = "f" * 64
    planner.write_segment_artifact(key, b"payload")
    manifest = json.loads(planner.manifest_path(key).read_text(encoding="utf-8"))
    manifest["file_sha256"] = "0" * 64
    planner.manifest_path(key).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SegmentIntegrityError):
        planner.read_segment_artifact(key)


def test_verify_and_open_rejects_length_mismatch(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    key = "e" * 64
    planner.write_segment_artifact(key, b"payload")
    artifact = planner.artifact_path(key)
    raw = bytearray(artifact.read_bytes())
    raw[dag._CONTENT_SHA_OFFSET - 8 : dag._CONTENT_SHA_OFFSET] = struct.pack(">Q", 999)
    artifact.write_bytes(bytes(raw))
    with pytest.raises(SegmentIntegrityError):
        planner.read_segment_artifact(key)


def test_commit_stream_rejects_traversal(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    with pytest.raises(SegmentIntegrityError):
        planner.commit_stream(
            "../" + "a" * 61, io.BytesIO(b"x"), size=1, digest=hashlib.sha256(b"x").hexdigest()
        )


# ── atomic write helper ─────────────────────────────────────────────────────
def test_atomic_write_bytes_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "artifact.bin"
    atomic_write_bytes(target, b"payload")
    assert target.read_bytes() == b"payload"
    assert list(tmp_path.rglob("*.tmp")) == []


def test_atomic_write_bytes_cleans_temp_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    target = tmp_path / "artifact.bin"
    with pytest.raises(OSError):
        atomic_write_bytes(target, b"payload")
    assert list(tmp_path.rglob("*.tmp")) == []


# ── artifact encoding ───────────────────────────────────────────────────────
def test_encode_artifact_rejects_bad_key_length() -> None:
    with pytest.raises(SegmentIntegrityError):
        dag._encode_artifact("short", b"body")


def test_parse_artifact_header_rejects_truncated() -> None:
    with pytest.raises(SegmentIntegrityError):
        dag._parse_artifact_header(b"NAGARSEG1")


def test_decode_artifact_rejects_bad_magic() -> None:
    raw = dag._encode_artifact("a" * 64, b"body")
    corrupted = b"X" + raw[1:]
    with pytest.raises(SegmentIntegrityError):
        dag._decode_artifact(corrupted)


def test_decode_artifact_rejects_length_mismatch() -> None:
    raw = bytearray(dag._encode_artifact("a" * 64, b"body"))
    raw[dag._CONTENT_SHA_OFFSET - 8 : dag._CONTENT_SHA_OFFSET] = struct.pack(">Q", 999)
    with pytest.raises(SegmentIntegrityError):
        dag._decode_artifact(bytes(raw))


def test_decode_artifact_rejects_content_hash_mismatch() -> None:
    raw = bytearray(dag._encode_artifact("a" * 64, b"body"))
    raw[dag._CONTENT_SHA_OFFSET] ^= 0xFF
    with pytest.raises(SegmentIntegrityError):
        dag._decode_artifact(bytes(raw))


def test_decode_artifact_roundtrip() -> None:
    key = "b" * 64
    body = b"frame-data"
    assert dag._decode_artifact(dag._encode_artifact(key, body)) == (key, body)


# ── SegmentCASPlanner ───────────────────────────────────────────────────────
def test_planner_paths_are_derived_from_key(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    key = planner.cas_key(make_spec(0))
    assert planner.artifact_path(key) == tmp_path / f"{key}.nagarseg"
    assert planner.manifest_path(key) == tmp_path / f"{key}.manifest.json"


def test_planner_first_run_renders_everything(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    plan = planner.plan(make_timeline(5))
    assert plan.cache_hits == 0
    assert plan.render_required == 5
    assert plan.cache_hit_ratio == 0.0
    assert plan.total_leaves == 5
    assert len(plan.render_targets()) == 5


def test_planner_reuses_cached_segments(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    for spec in make_timeline(5):
        planner.commit_segment(spec, b"payload-" + spec.segment_id.encode())
    plan = planner.plan(make_timeline(5))
    assert plan.cache_hits == 5
    assert plan.render_required == 0
    assert plan.cache_hit_ratio == 1.0
    assert plan.render_targets() == ()


def test_planner_records_content_sha_and_body_size(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    spec = make_spec(0)
    planner.commit_segment(spec, b"payload")
    planned = planner.plan([spec]).segments[0]
    assert planned.status is SegmentStatus.CACHE_HIT
    assert planned.content_sha == hashlib.sha256(b"payload").hexdigest()
    assert planned.body_size == len(b"payload")


def test_planner_marks_only_changed_segment_for_render(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    timeline = make_timeline(20)
    for spec in timeline:
        planner.commit_segment(spec, b"p")
    changed = list(timeline)
    changed[7] = make_spec(7, dna="changed")
    plan = planner.plan(changed)
    assert plan.cache_hits == 19
    assert plan.render_required == 1
    assert plan.cache_hit_ratio == 0.95
    assert plan.render_targets()[0].spec.segment_id == "seg7"


def test_planner_pipeline_digest_change_forces_render(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    timeline = make_timeline(3)
    digests = ["pipe-a"] * 3
    for spec in timeline:
        planner.write_segment_artifact(planner.cas_key(spec, pipeline_digest="pipe-a"), b"p")
    assert planner.plan(timeline, pipeline_digests=digests).cache_hits == 3
    flipped = ["pipe-a", "pipe-b", "pipe-a"]
    plan = planner.plan(timeline, pipeline_digests=flipped)
    assert plan.cache_hits == 2
    assert plan.render_targets()[0].spec.segment_id == "seg1"


def test_planner_mux_mode_separates_engine_caches(tmp_path: Path) -> None:
    # P1-A: a segment rendered by the CPU mux must NOT satisfy an FFmpeg request
    # (and vice versa) — the two engines can never share a cache entry.
    planner = SegmentCASPlanner(tmp_path)
    spec = make_spec(0)
    cpu_key = planner.cas_key(spec, mux_mode="deterministic_cpu_mux")
    ffmpeg_key = planner.cas_key(spec, mux_mode="ffmpeg_subprocess_pipe")
    assert cpu_key != ffmpeg_key
    planner.write_segment_artifact(cpu_key, b"cpu-body", mode="deterministic_cpu_mux")

    cpu_plan = planner.plan([spec], mux_modes=["deterministic_cpu_mux"])
    assert cpu_plan.cache_hits == 1
    assert cpu_plan.segments[0].mode == "deterministic_cpu_mux"

    ffmpeg_plan = planner.plan([spec], mux_modes=["ffmpeg_subprocess_pipe"])
    assert ffmpeg_plan.cache_hits == 0
    assert ffmpeg_plan.segments[0].status is SegmentStatus.RENDER_REQUIRED


def test_planner_rejects_mismatched_mux_modes(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    with pytest.raises(HyperCompilerError):
        planner.plan(make_timeline(3), mux_modes=["only-one"])


def test_manifest_records_mode_and_renderer_version(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    spec = make_spec(0)
    key = planner.cas_key(spec, mux_mode="ffmpeg_subprocess_pipe")
    planner.write_segment_artifact(key, b"body", mode="ffmpeg_subprocess_pipe")
    manifest = json.loads(planner.manifest_path(key).read_text(encoding="utf-8"))
    assert manifest["mode"] == "ffmpeg_subprocess_pipe"
    assert manifest["renderer_version"] == dag.RENDERER_VERSION


def test_verify_rejects_incompatible_renderer_version(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    spec = make_spec(0)
    key = planner.cas_key(spec)
    planner.commit_segment(spec, b"body")
    manifest = json.loads(planner.manifest_path(key).read_text(encoding="utf-8"))
    manifest["renderer_version"] = "hyper-compiler/0"
    planner.manifest_path(key).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SegmentIntegrityError):
        planner.verify_segment_artifact(key)


def test_read_manifest_rejects_oversized_file(tmp_path: Path) -> None:
    # P1-D/P2: a hostile multi-megabyte manifest must be refused before it is
    # read into memory.
    planner = SegmentCASPlanner(tmp_path)
    key = "a" * 64
    planner.cache_dir.mkdir(parents=True, exist_ok=True)
    planner.manifest_path(key).write_bytes(b"x" * (dag._MAX_MANIFEST_BYTES + 1))
    with pytest.raises(SegmentIntegrityError):
        planner.read_manifest(key)


def test_read_manifest_reports_stat_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner = SegmentCASPlanner(tmp_path)
    key = "a" * 64
    planner.cache_dir.mkdir(parents=True, exist_ok=True)
    planner.manifest_path(key).write_text("{}", encoding="utf-8")

    # Let the existence check pass, then make the size probe itself fail so the
    # OSError guard around os.stat is exercised.
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(os, "stat", lambda *a, **k: (_ for _ in ()).throw(OSError("stat failed")))
    with pytest.raises(SegmentIntegrityError):
        planner.read_manifest(key)


def test_verify_rejects_explicit_path_outside_cache_dir(tmp_path: Path) -> None:
    # P1-D: an explicit ``path`` must still be confined to cache_dir, even when
    # the key itself is canonical.
    planner = SegmentCASPlanner(tmp_path / "cache")
    spec = make_spec(0)
    key = planner.cas_key(spec)
    planner.commit_segment(spec, b"body")
    outside = tmp_path / "outside.nagarseg"
    outside.write_bytes(planner.artifact_path(key).read_bytes())
    with pytest.raises(SegmentIntegrityError):
        planner.verify_segment_artifact(key, outside)
    with pytest.raises(SegmentIntegrityError):
        planner.read_segment_artifact(key, outside)


def test_planner_rejects_mismatched_pipeline_digests(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    with pytest.raises(HyperCompilerError):
        planner.plan(make_timeline(3), pipeline_digests=["only-one"])


def test_planner_detects_tampered_artifact(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    spec = make_spec(0)
    path = planner.commit_segment(spec, b"original")
    path.write_bytes(path.read_bytes() + b"tamper")
    plan = planner.plan([spec])
    assert plan.cache_hits == 0
    assert plan.segments[0].status is SegmentStatus.RENDER_REQUIRED


def test_verify_reports_missing_artifact(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    with pytest.raises(SegmentIntegrityError):
        planner.verify_segment_artifact("f" * 64)


def test_verify_reports_missing_manifest(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    key = planner.cas_key(make_spec(0))
    atomic_write_bytes(planner.artifact_path(key), dag._encode_artifact(key, b"x"))
    with pytest.raises(SegmentIntegrityError):
        planner.verify_segment_artifact(key)


def test_verify_rejects_unreadable_manifest(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    spec = make_spec(0)
    key = planner.cas_key(spec)
    planner.commit_segment(spec, b"x")
    planner.manifest_path(key).write_text("{not json", encoding="utf-8")
    with pytest.raises(SegmentIntegrityError):
        planner.verify_segment_artifact(key)


def test_verify_rejects_non_object_manifest(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    spec = make_spec(0)
    key = planner.cas_key(spec)
    planner.commit_segment(spec, b"x")
    planner.manifest_path(key).write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(SegmentIntegrityError):
        planner.verify_segment_artifact(key)


def test_verify_rejects_size_mismatch(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    spec = make_spec(0)
    key = planner.cas_key(spec)
    planner.commit_segment(spec, b"x")
    manifest = json.loads(planner.manifest_path(key).read_text(encoding="utf-8"))
    manifest["size"] = manifest["size"] + 1
    planner.manifest_path(key).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SegmentIntegrityError):
        planner.verify_segment_artifact(key)


def test_verify_rejects_stored_key_mismatch(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    spec = make_spec(0)
    key = planner.cas_key(spec)
    other = "9" * 64
    raw = dag._encode_artifact(other, b"x")
    atomic_write_bytes(planner.artifact_path(key), raw)
    planner.manifest_path(key).write_text(
        json.dumps(
            {
                "cas_key": key,
                "content_sha256": hashlib.sha256(b"x").hexdigest(),
                "file_sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SegmentIntegrityError):
        planner.verify_segment_artifact(key)


def test_verify_rejects_manifest_key_mismatch(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    spec = make_spec(0)
    key = planner.cas_key(spec)
    planner.commit_segment(spec, b"x")
    manifest = json.loads(planner.manifest_path(key).read_text(encoding="utf-8"))
    manifest["cas_key"] = "0" * 64
    planner.manifest_path(key).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SegmentIntegrityError):
        planner.verify_segment_artifact(key)


def test_verify_rejects_content_hash_mismatch(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    spec = make_spec(0)
    key = planner.cas_key(spec)
    planner.commit_segment(spec, b"x")
    manifest = json.loads(planner.manifest_path(key).read_text(encoding="utf-8"))
    manifest["content_sha256"] = "0" * 64
    planner.manifest_path(key).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SegmentIntegrityError):
        planner.verify_segment_artifact(key)


def test_verify_accepts_explicit_path(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    spec = make_spec(0)
    key = planner.cas_key(spec)
    path = planner.commit_segment(spec, b"verified-body")
    verified = planner.verify_segment_artifact(key, path)
    assert verified.body_size == len(b"verified-body")
    assert planner.read_segment_artifact(key, path) == b"verified-body"


def test_stream_segment_body_copies_payload(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    spec = make_spec(0)
    planner.commit_segment(spec, b"streamed-body")
    sink = io.BytesIO()
    verified = planner.stream_segment_body(planner.cas_key(spec), sink)
    assert sink.getvalue() == b"streamed-body"
    assert verified.body_size == len(b"streamed-body")


def test_commit_stream_rejects_bad_digest(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    key = "a" * 64
    with pytest.raises(SegmentIntegrityError):
        planner.commit_stream(key, io.BytesIO(b"x"), size=1, digest="not-hex")
    with pytest.raises(SegmentIntegrityError):
        planner.commit_stream(key, io.BytesIO(b"x"), size=1, digest="00" * 16)


def test_commit_stream_rejects_size_mismatch(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    key = "a" * 64
    with pytest.raises(SegmentIntegrityError):
        planner.commit_stream(
            key, io.BytesIO(b"x"), size=5, digest=hashlib.sha256(b"x").hexdigest()
        )
    assert list(tmp_path.rglob("*.tmp")) == []


def test_commit_stream_rejects_hash_mismatch(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    key = "a" * 64
    with pytest.raises(SegmentIntegrityError):
        planner.commit_stream(
            key, io.BytesIO(b"x"), size=1, digest=hashlib.sha256(b"y").hexdigest()
        )


def test_commit_stream_roundtrips_and_verifies(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path)
    key = "c" * 64
    planner.commit_stream(
        key,
        io.BytesIO(b"chunked-body"),
        size=12,
        digest=hashlib.sha256(b"chunked-body").hexdigest(),
    )
    assert planner.read_segment_artifact(key) == b"chunked-body"


def test_planner_hmac_key_changes_plan_keys(tmp_path: Path) -> None:
    plain = SegmentCASPlanner(tmp_path)
    keyed = SegmentCASPlanner(tmp_path, hmac_key=b"k")
    assert plain.cas_key(make_spec(0)) != keyed.cas_key(make_spec(0))


# ── HMAC-sealed manifest (P2) ───────────────────────────────────────────────
def test_keyed_planner_seals_manifest(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path, hmac_key=b"secret")
    spec = make_spec(0)
    key = planner.cas_key(spec)
    planner.commit_segment(spec, b"payload")
    manifest = json.loads(planner.manifest_path(key).read_text(encoding="utf-8"))
    assert "manifest_hmac" in manifest
    # A keyed planner accepts its own sealed manifest.
    assert planner.read_manifest(key)["cas_key"] == key


def test_keyed_planner_rejects_tampered_manifest(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path, hmac_key=b"secret")
    spec = make_spec(0)
    key = planner.cas_key(spec)
    planner.commit_segment(spec, b"payload")
    manifest = json.loads(planner.manifest_path(key).read_text(encoding="utf-8"))
    manifest["size"] = manifest["size"] + 1
    planner.manifest_path(key).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SegmentIntegrityError):
        planner.verify_segment_artifact(key)


def test_keyed_planner_rejects_manifest_without_seal(tmp_path: Path) -> None:
    planner = SegmentCASPlanner(tmp_path, hmac_key=b"secret")
    spec = make_spec(0)
    key = planner.cas_key(spec)
    planner.commit_segment(spec, b"payload")
    manifest = json.loads(planner.manifest_path(key).read_text(encoding="utf-8"))
    manifest.pop("manifest_hmac")
    planner.manifest_path(key).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SegmentIntegrityError):
        planner.verify_segment_artifact(key)


def test_plain_planner_ignores_missing_seal(tmp_path: Path) -> None:
    # Without a key, the seal is neither required nor checked.
    planner = SegmentCASPlanner(tmp_path)
    spec = make_spec(0)
    key = planner.cas_key(spec)
    planner.commit_segment(spec, b"payload")
    assert planner.verify_segment_artifact(key).body_size == len(b"payload")


# ── plan value objects ──────────────────────────────────────────────────────
def test_empty_plan_ratio_is_zero() -> None:
    plan = SegmentPlan(
        segments=(), merkle_root="r", total_leaves=0, cache_hits=0, render_required=0
    )
    assert plan.cache_hit_ratio == 0.0
    assert plan.render_targets() == ()


def test_planned_segment_is_frozen_value() -> None:
    planned = PlannedSegment(make_spec(0), "k" * 64, SegmentStatus.CACHE_HIT, Path("/x"))
    assert planned.status is SegmentStatus.CACHE_HIT
    assert SegmentStatus.RENDER_REQUIRED.value == "RENDER_REQUIRED"


def test_verified_segment_is_frozen_value() -> None:
    verified = VerifiedSegment(cas_key="k" * 64, content_sha="s", body_size=3)
    assert verified.body_size == 3
