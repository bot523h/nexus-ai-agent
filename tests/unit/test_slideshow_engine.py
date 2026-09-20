"""Wave 2b — the host-side adapter on **real** generated media.

Nothing in this module mocks the media path: the images are real JPEGs, the
soundtrack is a real WAV, probing reads the real bytes and beat detection runs
on the real samples.  Only the hosted model (which would require a network and
a paid key) is replaced, and it is replaced at the HTTP transport boundary so
the request/response handling under test is the production code path.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from slideshow_media import CLICK_TRACK_BPM, SAMPLE_RATE, SlideshowMedia

from nexus_ai_agent.creative.slideshow.analysis import (
    AnalysisError,
    EgressNotAllowedError,
    analyze,
    analyze_with_gemini,
    local_analysis,
    local_score_sheet,
    sharpness_of,
)
from nexus_ai_agent.creative.slideshow.audio import (
    AudioError,
    beat_grid_for_file,
    read_wav_mono,
)
from nexus_ai_agent.creative.slideshow.probe import (
    ProbeError,
    content_sha256,
    probe_audio,
    probe_image,
    probe_media,
)
from nexus_ai_agent.creative.slideshow.service import PlanningRequest, plan_from_files

# ---------------------------------------------------------------------------
# probing (real bytes)
# ---------------------------------------------------------------------------


def test_probe_image_is_content_addressed_and_stable(slideshow_media: SlideshowMedia) -> None:
    first = probe_image(slideshow_media.crisp_image)
    second = probe_image(slideshow_media.crisp_image)
    assert first.evidence_id == second.evidence_id
    assert first.evidence_id.startswith("img_")
    assert first.content_sha256 == second.content_sha256
    assert first.content_sha256 == content_sha256(slideshow_media.crisp_image)
    assert (first.width, first.height) == (960, 540)
    assert 0.0 <= first.mean_luma <= 1.0


def test_different_bytes_get_different_evidence_ids(slideshow_media: SlideshowMedia) -> None:
    ids = {probe_image(path).evidence_id for path in slideshow_media.images}
    assert len(ids) == len(slideshow_media.images)


def test_probe_image_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ProbeError, match="image not found"):
        probe_image(tmp_path / "nope.jpg")


def test_probe_image_rejects_an_unsupported_suffix(tmp_path: Path) -> None:
    bogus = tmp_path / "notes.txt"
    bogus.write_text("hello", encoding="utf-8")
    with pytest.raises(ProbeError, match="unsupported image type"):
        probe_image(bogus)


def test_probe_audio_reads_the_real_duration(slideshow_media: SlideshowMedia) -> None:
    evidence = probe_audio(slideshow_media.audio)
    assert evidence.media_kind == "audio"
    assert evidence.evidence_id.startswith("aud_")
    assert abs(evidence.duration_us - 61_000_000) < 200_000


def test_probe_audio_rejects_compressed_formats_with_advice(tmp_path: Path) -> None:
    compressed = tmp_path / "song.mp3"
    compressed.write_bytes(b"\xff\xfb\x90\x00")
    with pytest.raises(ProbeError, match="convert to WAV"):
        probe_audio(compressed)


def test_probe_media_dispatches_on_suffix(slideshow_media: SlideshowMedia) -> None:
    probed = probe_media((slideshow_media.crisp_image, slideshow_media.audio))
    assert [item.media_kind for item in probed] == ["image", "audio"]


# ---------------------------------------------------------------------------
# audio decoding and beat detection (real samples)
# ---------------------------------------------------------------------------


def test_read_wav_mono_decodes_real_samples(slideshow_media: SlideshowMedia) -> None:
    audio = read_wav_mono(slideshow_media.audio)
    assert audio.sample_rate == SAMPLE_RATE
    assert audio.samples.dtype.name == "float32"
    assert audio.samples.ndim == 1
    assert abs(audio.duration_us - 61_000_000) < 200_000
    assert float(abs(audio.samples).max()) <= 1.0


def test_read_wav_mono_averages_stereo_channels(tmp_path: Path) -> None:
    import wave

    import numpy as np

    path = tmp_path / "stereo.wav"
    left = np.full(1000, 16000, dtype="<i2")
    right = np.full(1000, -16000, dtype="<i2")
    interleaved = np.empty(2000, dtype="<i2")
    interleaved[0::2] = left
    interleaved[1::2] = right
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(interleaved.tobytes())
    audio = read_wav_mono(path)
    assert audio.samples.size == 1000
    assert float(abs(audio.samples).max()) < 1e-3


def test_read_wav_mono_rejects_a_non_wav_file(tmp_path: Path) -> None:
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not a wav at all")
    with pytest.raises(AudioError, match="cannot read WAV"):
        read_wav_mono(broken)


def test_beat_detection_finds_the_synthetic_tempo(slideshow_media: SlideshowMedia) -> None:
    grid = beat_grid_for_file(slideshow_media.audio, fallback_bpm=100.0)
    assert grid.tempo_bpm is not None
    assert abs(grid.tempo_bpm - CLICK_TRACK_BPM) <= 3.0
    assert grid.tempo_source == "detected"
    assert grid.alignment_quality == "detected"
    assert grid.confidence > 0.35
    assert len(grid.beats_us) > 100
    assert list(grid.beats_us) == sorted(grid.beats_us)
    assert grid.strong_beats_us and grid.strong_beats_us[0] in grid.beats_us


def test_beat_detection_reports_intervals_that_match_the_tempo(
    slideshow_media: SlideshowMedia,
) -> None:
    grid = beat_grid_for_file(slideshow_media.audio, fallback_bpm=100.0)
    intervals = [
        current - previous
        for previous, current in zip(grid.beats_us, grid.beats_us[1:], strict=False)
    ]
    average_seconds = (sum(intervals) / len(intervals)) / 1_000_000
    assert abs(60.0 / average_seconds - CLICK_TRACK_BPM) <= 3.0


def test_silence_falls_back_to_the_template_bpm(slideshow_silence: Path) -> None:
    grid = beat_grid_for_file(slideshow_silence, fallback_bpm=96.0)
    assert grid.tempo_source in {"fallback_bpm", "equal_division"}
    assert grid.alignment_quality == "interpolated"
    assert grid.tempo_bpm == 96.0
    assert len(grid.beats_us) > 10


def test_beat_grid_is_never_negative_or_unordered(slideshow_media: SlideshowMedia) -> None:
    grid = beat_grid_for_file(slideshow_media.audio, fallback_bpm=100.0)
    assert min(grid.beats_us) >= 0
    assert list(grid.beats_us) == sorted(set(grid.beats_us))


# ---------------------------------------------------------------------------
# local image analysis (real pixels)
# ---------------------------------------------------------------------------


def test_local_score_sheet_prefers_the_crisp_image(slideshow_media: SlideshowMedia) -> None:
    crisp = probe_image(slideshow_media.crisp_image)
    blurred = probe_image(slideshow_media.blurred_image)
    assert sharpness_of(slideshow_media.crisp_image) > sharpness_of(slideshow_media.blurred_image)
    sheet = {score.evidence_id: score for score in local_score_sheet((crisp, blurred))}
    assert sheet[crisp.evidence_id].sharpness > sheet[blurred.evidence_id].sharpness
    assert 0.0 <= sheet[crisp.evidence_id].aesthetic <= 1.0
    assert "landscape" in sheet[crisp.evidence_id].labels


def test_local_analysis_is_labelled_as_local(slideshow_media: SlideshowMedia) -> None:
    evidence = tuple(probe_image(path) for path in slideshow_media.images)
    analysis = local_analysis(evidence)
    assert analysis.source == "local_heuristic"
    assert len(analysis.scores) == 12
    assert set(analysis.ordered_evidence_ids) == {item.evidence_id for item in evidence}


# ---------------------------------------------------------------------------
# hosted provider (transport boundary only)
# ---------------------------------------------------------------------------


def _gemini_transport(payload: dict[str, object], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(":generateContent")
        body = json.loads(request.content.decode("utf-8"))
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert any("inline_data" in part for part in body["contents"][0]["parts"])
        return httpx.Response(
            status, json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]}
        )

    return httpx.MockTransport(handler)


def test_gemini_requires_the_explicit_upload_opt_in(slideshow_media: SlideshowMedia) -> None:
    evidence = tuple(probe_image(path) for path in slideshow_media.images[:2])
    with pytest.raises(EgressNotAllowedError, match="NEXUS_SLIDESHOW_ALLOW_IMAGE_UPLOAD"):
        analyze(evidence, provider="gemini", api_key="k", allow_upload=False)


def test_gemini_needs_an_api_key(slideshow_media: SlideshowMedia) -> None:
    evidence = (probe_image(slideshow_media.crisp_image),)
    with pytest.raises(AnalysisError, match="no API key"):
        analyze(evidence, provider="gemini", api_key=None, allow_upload=True)


def test_gemini_score_sheet_is_parsed_and_validated(slideshow_media: SlideshowMedia) -> None:
    evidence = tuple(probe_image(path) for path in slideshow_media.images[:3])
    proposal = {
        "images": [
            {
                "evidence_id": evidence[2].evidence_id,
                "labels": ["golden hour"],
                "sharpness": 0.8,
                "aesthetic": 0.9,
                "subject": "skyline",
                "suggested_role": "opener",
            },
            {
                "evidence_id": evidence[0].evidence_id,
                "labels": ["portrait"],
                "sharpness": 0.6,
                "aesthetic": 0.5,
                "subject": "person",
                "suggested_role": "body",
            },
            {
                "evidence_id": "img_not_in_the_set",
                "sharpness": 1.0,
                "aesthetic": 1.0,
            },
        ],
        "recommended_template_id": "cinematic_epic",
        "reasoning": "wide golden-hour frames",
    }
    from nexus_ai_agent.creative.slideshow.analysis import GeminiConfig

    analysis = analyze_with_gemini(
        evidence,
        config=GeminiConfig(
            api_key="k", model="gemini-2.0-flash", transport=_gemini_transport(proposal)
        ),
        allow_upload=True,
    )
    assert analysis.source == "gemini"
    assert analysis.recommended_template_id == "cinematic_epic"
    assert analysis.ordered_evidence_ids[0] == evidence[2].evidence_id
    assert "img_not_in_the_set" not in analysis.ordered_evidence_ids
    # the frame the model forgot is appended, never dropped
    assert set(analysis.ordered_evidence_ids) == {item.evidence_id for item in evidence}


def test_gemini_http_error_is_a_typed_failure(slideshow_media: SlideshowMedia) -> None:
    evidence = (probe_image(slideshow_media.crisp_image),)
    from nexus_ai_agent.creative.slideshow.analysis import GeminiConfig

    with pytest.raises(AnalysisError, match="gemini request failed"):
        analyze_with_gemini(
            evidence,
            config=GeminiConfig(
                api_key="k", model="gemini-2.0-flash", transport=_gemini_transport({}, status=500)
            ),
            allow_upload=True,
        )


def test_gemini_invalid_json_is_a_typed_failure(slideshow_media: SlideshowMedia) -> None:
    evidence = (probe_image(slideshow_media.crisp_image),)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}
        )

    from nexus_ai_agent.creative.slideshow.analysis import GeminiConfig

    with pytest.raises(AnalysisError, match="failed validation"):
        analyze_with_gemini(
            evidence,
            config=GeminiConfig(
                api_key="k", model="gemini-2.0-flash", transport=httpx.MockTransport(handler)
            ),
            allow_upload=True,
        )


# ---------------------------------------------------------------------------
# end-to-end planning from real files
# ---------------------------------------------------------------------------


def _request(media: SlideshowMedia, **overrides: object) -> PlanningRequest:
    base: dict[str, object] = {
        "images": media.images,
        "target_duration_us": 60_000_000,
        "audio": media.audio,
    }
    base.update(overrides)
    return PlanningRequest(**base)  # type: ignore[arg-type]


def test_auto_planning_from_real_files_uses_the_detected_tempo(
    slideshow_media: SlideshowMedia,
) -> None:
    outcome = plan_from_files(_request(slideshow_media))
    assert len(outcome.plan["shots"]) == 12
    assert outcome.plan["shots"][-1]["slot"]["end_us"] == 60_000_000
    assert outcome.alignment_quality == "detected"
    assert outcome.tempo_bpm is not None and abs(outcome.tempo_bpm - CLICK_TRACK_BPM) <= 3.0
    assert outcome.commands == (
        "slideshow.scan_assets",
        "slideshow.score_images",
        "slideshow.suggest_tone",
        "slideshow.compose",
    )
    assert outcome.asset_count == 13  # 12 images + the soundtrack
    assert outcome.state_revision == 4


def test_auto_planning_is_reproducible_across_runs(slideshow_media: SlideshowMedia) -> None:
    first = plan_from_files(_request(slideshow_media))
    second = plan_from_files(_request(slideshow_media))
    durations = [
        (shot["slot"]["end_us"] - shot["slot"]["start_us"]) for shot in first.plan["shots"]
    ]
    repeated = [
        (shot["slot"]["end_us"] - shot["slot"]["start_us"]) for shot in second.plan["shots"]
    ]
    assert durations == repeated
    assert first.ordered_evidence_ids == second.ordered_evidence_ids
    assert first.template_id == second.template_id


def test_planning_without_audio_is_allowed_and_reported(slideshow_media: SlideshowMedia) -> None:
    outcome = plan_from_files(_request(slideshow_media, audio=None))
    assert outcome.tempo_bpm is None
    assert any("silent" in warning for warning in outcome.warnings)


def test_manual_planning_accepts_one_frame_of_rounding(slideshow_media: SlideshowMedia) -> None:
    outcome = plan_from_files(
        _request(
            slideshow_media,
            images=slideshow_media.images[:4],
            mode="manual",
            shot_seconds=(15.005, 15.0, 15.0, 15.0),
            template_id="minimal_clean",
        )
    )
    slots = [shot["slot"] for shot in outcome.plan["shots"]]
    assert slots[0]["start_us"] == 0
    assert slots[-1]["end_us"] == 60_000_000
    assert outcome.plan["mode"] == "manual"


def test_manual_planning_rejects_a_bad_sum(slideshow_media: SlideshowMedia) -> None:
    with pytest.raises(ValueError, match="more than one frame"):
        plan_from_files(
            _request(
                slideshow_media,
                images=slideshow_media.images[:3],
                mode="manual",
                shot_seconds=(10.0, 10.0, 10.0),
            )
        )


def test_manual_planning_needs_one_duration_per_image(slideshow_media: SlideshowMedia) -> None:
    with pytest.raises(ValueError, match="one duration per image"):
        plan_from_files(
            _request(
                slideshow_media,
                images=slideshow_media.images[:3],
                mode="manual",
                shot_seconds=(20.0, 40.0),
            )
        )


def test_planning_requires_images() -> None:
    with pytest.raises(ValueError, match="at least one image"):
        plan_from_files(PlanningRequest(images=(), target_duration_us=60_000_000))


def test_gemini_planning_stays_fail_closed_without_the_opt_in(
    slideshow_media: SlideshowMedia,
) -> None:
    with pytest.raises(EgressNotAllowedError):
        plan_from_files(_request(slideshow_media, provider="gemini", allow_image_upload=False))
