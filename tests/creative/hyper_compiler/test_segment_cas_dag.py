"""Segment-CAS DAG, RFC 6962 Merkle tree and artifact-integrity tests."""

from __future__ import annotations

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
    atomic_write_bytes,
    canonical_spec_bytes,
    merkle_root,
    merkle_root_hex,
    segment_cas_key,
    timeline_merkle_root,
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


def test_segment_cas_key_changes_with_any_field() -> None:
    base = segment_cas_key(make_spec(1))
    assert base == segment_cas_key(make_spec(1))
    assert base != segment_cas_key(make_spec(1, dna="other"))


def test_segment_cas_key_hmac_mode() -> None:
    plain = segment_cas_key(make_spec(1))
    keyed = segment_cas_key(make_spec(1), hmac_key=b"secret")
    assert keyed != plain
    assert keyed == segment_cas_key(make_spec(1), hmac_key=b"secret")
    assert len(keyed) == 64


# ── RFC 6962 Merkle tree ────────────────────────────────────────────────────
def test_merkle_root_of_empty_tree_is_sha256_of_empty() -> None:
    import hashlib

    assert merkle_root([]) == hashlib.sha256(b"").digest()


def test_merkle_root_single_leaf_is_prefixed_hash() -> None:
    import hashlib

    assert merkle_root([b"x"]) == hashlib.sha256(b"\x00x").digest()


def test_merkle_root_odd_leaf_promotes_without_duplication() -> None:
    leaves = [b"a", b"b", b"c", b"d", b"e"]
    # A "duplicate the last node" construction would yield a different root for
    # the odd tail, so the two constructions must disagree.
    dup_leaves = [b"a", b"b", b"c", b"d", b"e", b"e"]
    assert merkle_root(leaves) != merkle_root(dup_leaves)
    assert merkle_root_hex(leaves) == merkle_root(leaves).hex()


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


def test_decode_artifact_rejects_truncated_header() -> None:
    with pytest.raises(SegmentIntegrityError):
        dag._decode_artifact(b"NAGARSEG1")


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
    import hashlib

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
    assert planner.verify_segment_artifact(key, path) == b"verified-body"


def test_planner_hmac_key_changes_plan_keys(tmp_path: Path) -> None:
    plain = SegmentCASPlanner(tmp_path)
    keyed = SegmentCASPlanner(tmp_path, hmac_key=b"k")
    assert plain.cas_key(make_spec(0)) != keyed.cas_key(make_spec(0))


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
