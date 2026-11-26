"""``nexus.analysis.perception``: evidence contracts and the three pure operations.

No process is spawned here.  Evidence is built with the same ``seal`` the adapter
uses, so these tests pin the contract the adapter must satisfy as well as the
operations that consume it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.packs.perception.models import (
    ANALYZER_FFMPEG,
    MAX_MARKERS_PER_COMMAND,
    MAX_SPANS,
    OPERATION_ASSESS_LOUDNESS,
    OPERATION_MARK_SCENE_CUTS,
    OPERATION_MARK_SILENCES,
    PERCEPTION_PACKAGE_ID,
    AssessLoudnessInput,
    LoudnessEvidence,
    MarkSilencesInput,
    SceneCutEvidence,
    SilenceEvidence,
    SilenceSpan,
    canonical_evidence_hash,
)
from nexus_ai_agent.creative.packs.perception.operations import (
    build_perception_registry,
    register_perception_operations,
)
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    Clip,
    CommandValidationError,
    MediaRef,
    PermissionLevel,
    Project,
    Timeline,
    TimeRangeUS,
    Track,
    TypedCommand,
    new_project,
)

SEC = 1_000_000
CONTENT = "sha256:" + hashlib.sha256(b"the-clip-bytes").hexdigest()
OTHER = "sha256:" + hashlib.sha256(b"someone-else").hexdigest()


def _silence(
    spans: list[tuple[float, float]],
    *,
    sha: str = CONTENT,
    duration_s: float = 20.0,
) -> SilenceEvidence:
    return SilenceEvidence.seal(
        engine="ffmpeg 6.1.1",
        asset_sha256=sha,
        duration_us=int(duration_s * SEC),
        noise_floor_db=-40.0,
        min_silence_us=500_000,
        spans=tuple(SilenceSpan(start_us=int(a * SEC), end_us=int(b * SEC)) for a, b in spans),
    )


def _scenes(cuts: list[float], *, sha: str = CONTENT) -> SceneCutEvidence:
    return SceneCutEvidence.seal(
        engine="ffmpeg 6.1.1",
        asset_sha256=sha,
        duration_us=20 * SEC,
        threshold=0.3,
        cuts_us=tuple(int(c * SEC) for c in cuts),
    )


def _loudness(
    integrated: float | None,
    peak: float | None,
    *,
    sha: str = CONTENT,
    lra: float | None = 4.0,
) -> LoudnessEvidence:
    return LoudnessEvidence.seal(
        engine="ffmpeg 6.1.1",
        asset_sha256=sha,
        duration_us=20 * SEC,
        integrated_lufs=integrated,
        loudness_range_lu=lra,
        true_peak_dbfs=peak,
    )


def _clip(
    *,
    source: tuple[float, float] = (0.0, 20.0),
    placed: tuple[float, float] = (0.0, 20.0),
    sha: str = CONTENT,
) -> Clip:
    return Clip(
        clip_id="clip_a",
        media_ref=MediaRef(
            asset_id="asset_a", content_sha256=sha, media_kind="video", duration_us=20 * SEC
        ),
        source_range=TimeRangeUS(start_us=int(source[0] * SEC), end_us=int(source[1] * SEC)),
        timeline_range=TimeRangeUS(start_us=int(placed[0] * SEC), end_us=int(placed[1] * SEC)),
    )


def _bus(clip: Clip | None = None, *, kind: str = "video") -> CommandBus:
    registry = build_wave1_registry()
    register_perception_operations(registry)
    track = Track(track_id="v1", name="V1", kind="video", clips=[clip or _clip()])
    timeline = Timeline(timeline_id="tl", duration_us=60 * SEC, tracks=[track])
    project: Project = new_project("perception_proj", "Perception", timeline)
    project = project.model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="asset_a",
                    media_kind=kind,  # type: ignore[arg-type]
                    content_sha256=CONTENT,
                    duration_us=20 * SEC,
                )
            ]
        }
    )
    return CommandBus(project, registry=registry)


_serial = iter(range(10_000))


def _cmd(operation: str, payload: dict[str, Any], **extra: Any) -> TypedCommand:
    return TypedCommand(
        command_id=f"cmd_{next(_serial)}",
        operation=operation,
        input=json.loads(json.dumps(payload)),
        **extra,
    )


def _silence_payload(evidence: SilenceEvidence, **extra: Any) -> dict[str, Any]:
    return {
        "track_id": "v1",
        "clip_id": "clip_a",
        "evidence": evidence.model_dump(mode="json"),
        **extra,
    }


# ---------------------------------------------------------------------------
# evidence contracts
# ---------------------------------------------------------------------------


def test_sealed_evidence_round_trips_through_json() -> None:
    evidence = _silence([(2.0, 3.5)])
    again = SilenceEvidence.model_validate(json.loads(evidence.model_dump_json(by_alias=True)))
    assert again == evidence
    assert again.analyzer == ANALYZER_FFMPEG
    assert evidence.evidence_hash == canonical_evidence_hash(evidence.model_dump(mode="json"))


def test_hash_is_independent_of_key_order() -> None:
    payload = _silence([(2.0, 3.5)]).model_dump(mode="json")
    reordered = dict(reversed(list(payload.items())))
    assert canonical_evidence_hash(payload) == canonical_evidence_hash(reordered)


def test_tampered_evidence_is_rejected() -> None:
    payload = _silence([(2.0, 3.5)]).model_dump(mode="json")
    payload["spans"][0]["end_us"] += 1
    with pytest.raises(ValidationError, match="evidence_hash does not match"):
        SilenceEvidence.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("analyzer", "someone.elses.engine"),
        ("asset_sha256", "sha256:ABC"),
        ("asset_sha256", "md5:" + "0" * 64),
        ("evidence_hash", "not-a-hash"),
        ("engine", ""),
        ("engine", " padded "),
        ("engine", "tab\there"),
        ("evidence_schema", "nexus.analysis-evidence.v2"),
        ("duration_us", -1),
        ("duration_us", 8 * 24 * 3600 * SEC),
        ("surprise", 1),
    ],
)
def test_malformed_common_fields_are_rejected(field: str, value: Any) -> None:
    payload = _silence([(2.0, 3.5)]).model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError):
        SilenceEvidence.model_validate(payload)


def test_evidence_is_immutable() -> None:
    evidence = _silence([(2.0, 3.5)])
    with pytest.raises(ValidationError):
        evidence.duration_us = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "spans",
    [
        [(2.0, 3.0), (2.5, 4.0)],  # overlap
        [(5.0, 6.0), (1.0, 2.0)],  # not ascending
        [(19.0, 21.0)],  # past the media duration
        [(3.0, 3.0)],  # empty span
    ],
)
def test_silence_span_invariants(spans: list[tuple[float, float]]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        _silence(spans)


def test_adjacent_silence_spans_are_allowed() -> None:
    assert len(_silence([(1.0, 2.0), (2.0, 3.0)]).spans) == 2


def test_silence_parameter_bounds() -> None:
    base = _silence([(1.0, 2.0)]).model_dump(mode="json")
    for field, value in (
        ("noise_floor_db", -100.0),
        ("noise_floor_db", 0.0),
        ("min_silence_us", 9_999),
    ):
        with pytest.raises(ValidationError):
            SilenceEvidence.model_validate({**base, field: value})


def test_span_cap_is_enforced() -> None:
    spans = tuple(SilenceSpan(start_us=i * 10, end_us=i * 10 + 5) for i in range(MAX_SPANS + 1))
    with pytest.raises(ValidationError):
        SilenceEvidence.seal(
            engine="ffmpeg",
            asset_sha256=CONTENT,
            duration_us=10 * SEC,
            noise_floor_db=-40.0,
            min_silence_us=500_000,
            spans=spans,
        )


@pytest.mark.parametrize("cuts", [[3.0, 3.0], [5.0, 2.0], [25.0]])
def test_scene_cut_invariants(cuts: list[float]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        _scenes(cuts)


@pytest.mark.parametrize("threshold", [0.0, 1.0, -0.1, float("nan")])
def test_scene_threshold_bounds(threshold: float) -> None:
    with pytest.raises((ValidationError, ValueError)):
        SceneCutEvidence.seal(
            engine="ffmpeg",
            asset_sha256=CONTENT,
            duration_us=SEC,
            threshold=threshold,
            cuts_us=(),
        )


@pytest.mark.parametrize(
    ("integrated", "peak"),
    [(-71.0, None), (11.0, None), (None, -151.0), (None, 21.0), (float("nan"), None)],
)
def test_loudness_bounds(integrated: float | None, peak: float | None) -> None:
    with pytest.raises((ValidationError, ValueError)):
        _loudness(integrated, peak)


def test_loudness_allows_unmeasurable_values() -> None:
    evidence = _loudness(None, None, lra=None)
    assert evidence.integrated_lufs is None and evidence.true_peak_dbfs is None


def test_input_models_forbid_extras_and_bad_identifiers() -> None:
    evidence = _silence([(1.0, 2.0)])
    base = _silence_payload(evidence)
    MarkSilencesInput.model_validate(base)
    for patch in (
        {"surprise": True},
        {"track_id": "has space"},
        {"clip_id": "../etc"},
        {"label_prefix": ""},
        {"label_prefix": " pad "},
        {"label_prefix": "x" * 61},
        {"label_prefix": "bad\nlabel"},
        {"edge": "middle"},
    ):
        with pytest.raises(ValidationError):
            MarkSilencesInput.model_validate({**base, **patch})
    with pytest.raises(ValidationError):
        AssessLoudnessInput.model_validate(
            {
                "asset_id": "a",
                "evidence": _loudness(-20.0, -3.0).model_dump(mode="json"),
                "target_lufs": 0,
            }
        )


# ---------------------------------------------------------------------------
# registry shape
# ---------------------------------------------------------------------------


def test_operation_specs_levels_and_pack_requirement() -> None:
    registry = build_perception_registry()
    expected = {
        OPERATION_MARK_SILENCES: PermissionLevel.REVERSIBLE,
        OPERATION_MARK_SCENE_CUTS: PermissionLevel.REVERSIBLE,
        OPERATION_ASSESS_LOUDNESS: PermissionLevel.IMMEDIATE,
    }
    assert set(registry.list_operations()) == set(expected)
    for operation, level in expected.items():
        spec = registry.get_spec(operation)
        assert spec.permission_level is level
        assert spec.required_packs == (PERCEPTION_PACKAGE_ID,)
        assert spec.deterministic is True
        assert registry.describe(operation).pack_provider == PERCEPTION_PACKAGE_ID


# ---------------------------------------------------------------------------
# analysis.mark_silences / analysis.mark_scene_cuts
# ---------------------------------------------------------------------------


def _markers(bus: CommandBus) -> list[tuple[int, str]]:
    return sorted((m.timecode_us, m.label) for m in bus.project.timeline.markers)


def test_mark_silences_places_markers_in_timeline_time() -> None:
    # source 0-20s placed at 10-30s on the timeline: asset 2.0s -> timeline 12.0s
    bus = _bus(_clip(placed=(10.0, 30.0)))
    result = bus.dispatch(
        _cmd(OPERATION_MARK_SILENCES, _silence_payload(_silence([(2.0, 3.5), (8.0, 9.0)])))
    )
    assert result.status == "applied"
    assert result.output["markers_added"] == 2
    assert result.output["skipped_outside_clip"] == 0
    assert [t for t, _ in _markers(bus)] == [12 * SEC, 18 * SEC]
    assert _markers(bus)[0][1] == "silence 1 in (1.50s)"


def test_mark_silences_edges_start_end_both() -> None:
    for edge, expected in (("start", 1), ("end", 1), ("both", 2)):
        bus = _bus()
        result = bus.dispatch(
            _cmd(OPERATION_MARK_SILENCES, _silence_payload(_silence([(2.0, 3.5)]), edge=edge))
        )
        assert result.output["markers_added"] == expected
    bus = _bus()
    bus.dispatch(
        _cmd(OPERATION_MARK_SILENCES, _silence_payload(_silence([(2.0, 3.5)]), edge="both"))
    )
    assert [t for t, _ in _markers(bus)] == [2 * SEC, int(3.5 * SEC)]
    assert _markers(bus)[1][1].startswith("silence 1 out")


def test_retimed_clip_scales_the_mapping() -> None:
    # 20 s of source squeezed into 10 s of timeline: asset 4s -> timeline 2s
    bus = _bus(_clip(placed=(0.0, 10.0)))
    bus.dispatch(_cmd(OPERATION_MARK_SILENCES, _silence_payload(_silence([(4.0, 6.0)]))))
    assert [t for t, _ in _markers(bus)] == [2 * SEC]


def test_instants_outside_the_clip_window_are_counted_not_clamped() -> None:
    # the clip shows source 5s..15s only
    bus = _bus(_clip(source=(5.0, 15.0), placed=(0.0, 10.0)))
    result = bus.dispatch(
        _cmd(
            OPERATION_MARK_SILENCES,
            _silence_payload(_silence([(1.0, 2.0), (6.0, 7.0), (16.0, 17.0)])),
        )
    )
    assert result.output["markers_added"] == 1
    assert result.output["skipped_outside_clip"] == 2
    assert [t for t, _ in _markers(bus)] == [1 * SEC]


def test_window_is_half_open() -> None:
    bus = _bus(_clip(source=(5.0, 15.0), placed=(0.0, 10.0)))
    result = bus.dispatch(
        _cmd(
            OPERATION_MARK_SCENE_CUTS,
            {
                "track_id": "v1",
                "clip_id": "clip_a",
                "evidence": _scenes([5.0, 15.0]).model_dump(mode="json"),
            },
        )
    )
    # 5.0 (== source.start) is inside, 15.0 (== source.end) is outside
    assert result.output["markers_added"] == 1
    assert result.output["skipped_outside_clip"] == 1
    assert [t for t, _ in _markers(bus)] == [0]


def test_replaying_the_same_evidence_is_idempotent() -> None:
    bus = _bus()
    payload = _silence_payload(_silence([(2.0, 3.5), (8.0, 9.0)]))
    first = bus.dispatch(_cmd(OPERATION_MARK_SILENCES, payload))
    second = bus.dispatch(_cmd(OPERATION_MARK_SILENCES, payload))
    assert first.output["markers_added"] == 2
    assert second.output["markers_added"] == 0
    assert second.output["already_present"] == 2
    assert len(bus.project.timeline.markers) == 2


def test_marker_ids_are_content_derived_and_deterministic() -> None:
    a, b = _bus(), _bus()
    payload = _silence_payload(_silence([(2.0, 3.5)]))
    ids_a = a.dispatch(_cmd(OPERATION_MARK_SILENCES, payload)).output["marker_ids"]
    ids_b = b.dispatch(_cmd(OPERATION_MARK_SILENCES, payload)).output["marker_ids"]
    assert ids_a == ids_b and ids_a[0].startswith("mk_")


def test_undo_removes_the_markers_of_one_transaction() -> None:
    bus = _bus()
    bus.dispatch(_cmd(OPERATION_MARK_SILENCES, _silence_payload(_silence([(2.0, 3.5)]))))
    assert bus.project.timeline.markers
    bus.dispatch(_cmd("system.undo", {}))
    assert bus.project.timeline.markers == []


def test_no_evidence_means_no_change_but_still_succeeds() -> None:
    bus = _bus()
    result = bus.dispatch(_cmd(OPERATION_MARK_SILENCES, _silence_payload(_silence([]))))
    assert result.output["markers_added"] == 0
    assert bus.project.timeline.markers == []


def test_foreign_evidence_is_refused() -> None:
    bus = _bus()
    with pytest.raises(CommandValidationError, match="different content"):
        bus.dispatch(
            _cmd(OPERATION_MARK_SILENCES, _silence_payload(_silence([(2.0, 3.0)], sha=OTHER)))
        )
    with pytest.raises(CommandValidationError, match="different content"):
        bus.dispatch(
            _cmd(
                OPERATION_MARK_SCENE_CUTS,
                {
                    "track_id": "v1",
                    "clip_id": "clip_a",
                    "evidence": _scenes([2.0], sha=OTHER).model_dump(mode="json"),
                },
            )
        )
    assert bus.project.timeline.markers == []


def test_unknown_track_or_clip_is_refused() -> None:
    bus = _bus()
    payload = _silence_payload(_silence([(2.0, 3.0)]))
    with pytest.raises(CommandValidationError, match="track not found"):
        bus.dispatch(_cmd(OPERATION_MARK_SILENCES, {**payload, "track_id": "nope"}))
    with pytest.raises(CommandValidationError, match="clip not found"):
        bus.dispatch(_cmd(OPERATION_MARK_SILENCES, {**payload, "clip_id": "nope"}))


def test_marker_flood_is_refused_not_truncated() -> None:
    count = MAX_MARKERS_PER_COMMAND + 1
    spans = [(i * 0.04, i * 0.04 + 0.02) for i in range(count)]
    bus = _bus()
    with pytest.raises(CommandValidationError, match="per-command limit"):
        bus.dispatch(
            _cmd(OPERATION_MARK_SILENCES, _silence_payload(_silence(spans, duration_s=100.0)))
        )
    assert bus.project.timeline.markers == []


def test_mark_scene_cuts_labels_and_output() -> None:
    bus = _bus()
    result = bus.dispatch(
        _cmd(
            OPERATION_MARK_SCENE_CUTS,
            {
                "track_id": "v1",
                "clip_id": "clip_a",
                "label_prefix": "cut",
                "evidence": _scenes([1.0, 2.0, 7.5]).model_dump(mode="json"),
            },
        )
    )
    assert result.output["cuts_in_evidence"] == 3
    assert result.output["threshold"] == 0.3
    assert _markers(bus) == [(SEC, "cut 1"), (2 * SEC, "cut 2"), (int(7.5 * SEC), "cut 3")]


# ---------------------------------------------------------------------------
# analysis.assess_loudness
# ---------------------------------------------------------------------------


def _assess(evidence: LoudnessEvidence, **extra: Any) -> dict[str, Any]:
    bus = _bus()
    before = bus.project.state_hash
    result = bus.dispatch(
        _cmd(
            OPERATION_ASSESS_LOUDNESS,
            {
                "asset_id": "asset_a",
                "evidence": evidence.model_dump(mode="json"),
                **extra,
            },
        )
    )
    assert bus.project.state_hash == before, "assessment must be read-only"
    assert result.status == "applied"
    return dict(result.output)


def test_too_quiet_recommends_positive_gain() -> None:
    out = _assess(_loudness(-22.0, -18.1))
    assert out["verdict"] == "too_quiet"
    assert out["gain_db"] == 8.0
    assert out["projected_true_peak_dbfs"] == -10.1
    assert out["limiting_required"] is False
    assert out["normalize_arguments"] == {
        "audio_asset_id": "asset_a",
        "target_lufs": -14.0,
        "true_peak_db": -1.0,
    }


def test_too_loud_recommends_negative_gain() -> None:
    out = _assess(_loudness(-8.0, -0.5))
    assert out["verdict"] == "too_loud"
    assert out["gain_db"] == -6.0


def test_within_tolerance_boundary_is_inclusive() -> None:
    assert _assess(_loudness(-15.0, -3.0))["verdict"] == "within_tolerance"
    assert _assess(_loudness(-15.1, -3.0))["verdict"] == "too_quiet"
    assert _assess(_loudness(-15.0, -3.0), tolerance_lu=0.5)["verdict"] == "too_quiet"


def test_limiting_is_flagged_when_the_boost_would_clip() -> None:
    out = _assess(_loudness(-22.0, -3.0))  # +8 dB -> +5 dBFS
    assert out["verdict"] == "too_quiet"
    assert out["projected_true_peak_dbfs"] == 5.0
    assert out["limiting_required"] is True


def test_unmeasurable_loudness_gives_no_signal() -> None:
    out = _assess(_loudness(None, None, lra=None))
    assert out["verdict"] == "no_signal"
    assert out["gain_db"] is None and out["normalize_arguments"] is None


def test_missing_peak_still_gives_a_gain() -> None:
    out = _assess(_loudness(-22.0, None))
    assert out["gain_db"] == 8.0
    assert out["projected_true_peak_dbfs"] is None
    assert out["limiting_required"] is False


def test_assessment_refuses_unknown_foreign_or_wrong_kind() -> None:
    bus = _bus()
    evidence = _loudness(-20.0, -3.0).model_dump(mode="json")
    with pytest.raises(CommandValidationError, match="unknown asset"):
        bus.dispatch(_cmd(OPERATION_ASSESS_LOUDNESS, {"asset_id": "ghost", "evidence": evidence}))
    foreign = _loudness(-20.0, -3.0, sha=OTHER).model_dump(mode="json")
    with pytest.raises(CommandValidationError, match="different content"):
        bus.dispatch(_cmd(OPERATION_ASSESS_LOUDNESS, {"asset_id": "asset_a", "evidence": foreign}))
    image_bus = _bus(kind="image")
    with pytest.raises(CommandValidationError, match="audio or video"):
        image_bus.dispatch(
            _cmd(OPERATION_ASSESS_LOUDNESS, {"asset_id": "asset_a", "evidence": evidence})
        )
