from __future__ import annotations

import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from nexus_ai_agent.creative.biophysical_avatar.avatar_layer_renderer import (
    MAX_RENDER_PIXELS,
    AvatarLayerRenderer,
    AvatarScene,
    Camera,
    LayeredFrame,
    RGBAFrame,
    encode_sequence,
    estimate_frame_memory_bytes,
    face_grid_triangles,
)
from nexus_ai_agent.creative.biophysical_avatar.craniofacial_body_manifold import (
    FaceManifold,
    Skeleton3D,
)
from nexus_ai_agent.creative.biophysical_avatar.entity_dna_seal import ParametricAvatarCatalog
from nexus_ai_agent.creative.biophysical_avatar.locomotion_and_cloth_physics import make_cloth_grid
from nexus_ai_agent.creative.biophysical_avatar.vocal_tract_and_prosody import (
    ENGLISH_PHONEMES,
    MAX_SYNTHESIS_SECONDS,
    PERSIAN_PHONEMES,
    PHONEME_FORMANTS,
    Emotion,
    MouthPose,
    PhonemeSegment,
    PhonemeTarget,
    SpeechSynthesis,
    VisemeKeyframe,
    VisemeTrajectory,
    VoiceProfile,
    _apply_formants,
    _glottal_source,
    _resonant_spectrum,
    build_viseme_trajectory,
    phonemize_text,
    synthesize_wav,
    voice_profile_from_seal,
)


def test_persian_and_english_phoneme_archives_are_complete() -> None:
    assert len(PERSIAN_PHONEMES) == 29
    assert set(PERSIAN_PHONEMES) == set(PHONEME_FORMANTS)
    assert set(ENGLISH_PHONEMES) == set("abcdefghijklmnopqrstuvwxyz")
    assert all(len(target.formants_hz) == 5 for target in PHONEME_FORMANTS.values())
    persian = phonemize_text("آ اَ اِ اُ ای او، سلام", "fa")
    assert {"آ", "اَ", "اِ", "اُ", "ای", "او"}.issubset(set(persian))
    assert "sil" in persian
    assert phonemize_text("ث", "fa") == ("س",)
    assert phonemize_text("The quick", "en")
    assert phonemize_text("sh ch th ph ng ee oo", "en-us")
    with pytest.raises(ValueError, match="language"):
        phonemize_text("hello", "fr")
    with pytest.raises(ValueError, match="empty"):
        phonemize_text("  ", "fa")
    with pytest.raises(ValueError, match="no supported"):
        phonemize_text("...!!!", "en")
    with pytest.raises(ValueError, match="string"):
        phonemize_text(12)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="language must be a string"):
        phonemize_text("hello", 3)  # type: ignore[arg-type]
    assert phonemize_text("œ", "en") == ("ه",)
    assert phonemize_text("é", "fa") == ("ه",)


def test_voice_profiles_lock_vocal_tract_scaling_to_entity_dna() -> None:
    for name, profile in (
        ("epic_male", VoiceProfile("epic", 100.0, 1.12)),
        ("young_male", VoiceProfile("young", 130.0)),
        ("expressive_female", VoiceProfile("female", 220.0, 0.88)),
        ("gentle_female", VoiceProfile("gentle", 185.0, 0.91)),
        ("elderly", VoiceProfile("elderly", 150.0, 0.98)),
        ("child", VoiceProfile("child", 290.0, 0.76)),
        ("robotic_sci_fi", VoiceProfile("robot", 125.0, 1.08)),
    ):
        assert name in {
            "epic_male",
            "young_male",
            "expressive_female",
            "gentle_female",
            "elderly",
            "child",
            "robotic_sci_fi",
        }
        assert 0.75 <= profile.vocal_tract_scale <= 1.35
        assert profile.vocal_tract_length_m > 0.0
    seal = ParametricAvatarCatalog().profile(81)
    profile = voice_profile_from_seal(seal, archetype="epic_male")
    assert profile.name.startswith("epic_male:")
    assert profile.vocal_tract_length_m == pytest.approx(
        seal.invariant_signature().vocal_tract_length, abs=1e-12
    )
    assert profile.f0_hz != 102.0
    with pytest.raises(ValueError, match="archetype"):
        voice_profile_from_seal(seal, archetype="unknown")
    with pytest.raises(ValueError, match="name"):
        VoiceProfile("", 100.0)
    with pytest.raises(ValueError, match="f0_hz"):
        VoiceProfile("bad", 700.0)
    with pytest.raises(ValueError, match="vocal_tract_scale"):
        VoiceProfile("bad", 100.0, 1.5)
    with pytest.raises(ValueError, match="open_quotient"):
        VoiceProfile("bad", 100.0, open_quotient=0.1)
    with pytest.raises(ValueError, match="jitter"):
        VoiceProfile("bad", 100.0, jitter=0.2)
    with pytest.raises(ValueError, match="shimmer"):
        VoiceProfile("bad", 100.0, shimmer=0.3)
    with pytest.raises(ValueError, match="breathiness"):
        VoiceProfile("bad", 100.0, breathiness=0.6)


def test_viseme_timeline_interpolates_at_audio_sample_precision() -> None:
    segments, trajectory = build_viseme_trajectory(("اَ", "ب", "sil", "او"), rate=1.2)
    assert len(segments) == 4
    assert trajectory.duration_seconds > segments[-1].start_seconds
    assert trajectory.sample(-1.0) == trajectory.keyframes[0].pose
    assert trajectory.sample(trajectory.duration_seconds + 1.0) == trajectory.keyframes[-1].pose
    middle = (trajectory.keyframes[0].time_seconds + trajectory.keyframes[1].time_seconds) / 2.0
    assert 0.0 <= trajectory.sample(middle).mouth_open <= 1.0
    assert trajectory.sample_at_audio_sample(240, 48_000) == trajectory.sample(0.005)
    one_segment, one_trajectory = build_viseme_trajectory(("ب",))
    assert one_segment[0].end_seconds == pytest.approx(one_trajectory.duration_seconds)
    with pytest.raises(ValueError, match="not be empty"):
        build_viseme_trajectory(())
    with pytest.raises(ValueError, match="rate"):
        build_viseme_trajectory(("ب",), rate=0.0)
    with pytest.raises(ValueError, match="unknown phoneme"):
        build_viseme_trajectory(("unknown",))
    with pytest.raises(ValueError, match="finite"):
        trajectory.sample(float("nan"))
    with pytest.raises(ValueError, match="sample_index"):
        trajectory.sample_at_audio_sample(-1, 48_000)
    with pytest.raises(ValueError, match="strictly increasing"):
        VisemeTrajectory(
            (
                VisemeKeyframe(1.0, MouthPose(0.0, 0.0, 0.0)),
                VisemeKeyframe(1.0, MouthPose(0.0, 0.0, 0.0)),
            )
        )
    with pytest.raises(ValueError, match="at least one"):
        VisemeTrajectory(())
    with pytest.raises(ValueError, match="finite"):
        VisemeTrajectory((VisemeKeyframe(float("nan"), MouthPose(0.0, 0.0, 0.0)),))
    with pytest.raises(ValueError, match="finite and non-empty"):
        PhonemeSegment("", 0.0, 0.1)
    with pytest.raises(ValueError, match="positive duration"):
        PhonemeSegment("a", 0.1, 0.1)
    with pytest.raises(ValueError, match="five positive formants"):
        PhonemeTarget((0.0, 1.0, 2.0, 3.0, 4.0), 0.0, 0.0, 0.0)
    with pytest.raises(ValueError, match="[0, 1]"):
        PhonemeTarget((1.0, 2.0, 3.0, 4.0, 5.0), 1.1, 0.0, 0.0)


def test_source_filter_synthesis_writes_real_48khz_wav_and_is_reproducible(
    tmp_path: Path,
) -> None:
    profile = VoiceProfile("steady", 142.0, 1.03, jitter=0.005, shimmer=0.02, breathiness=0.03)
    speech = synthesize_wav(
        "سلام، جهان",
        voice=profile,
        emotion=Emotion.CALM,
        language="fa",
        sample_rate=48_000,
        seed=44,
    )
    repeated = synthesize_wav(
        "سلام، جهان",
        voice=profile,
        emotion="calm",
        language="fa-ir",
        sample_rate=48_000,
        seed=44,
    )
    different = synthesize_wav("hello world", voice=profile, language="en", seed=45)
    assert speech.sample_rate == 48_000
    assert speech.samples.dtype == np.float32
    assert speech.samples.ndim == 1
    assert np.array_equal(speech.samples, repeated.samples)
    assert not np.array_equal(speech.samples, different.samples[: speech.samples.size])
    assert np.max(np.abs(speech.samples)) <= 0.98
    assert speech.duration_seconds == pytest.approx(speech.visemes.duration_seconds, abs=1 / 48_000)
    assert speech.visemes.sample_at_audio_sample(120, 48_000).mouth_open >= 0.0
    assert not speech.samples.flags.writeable
    output = speech.write_wav(tmp_path / "voice.wav")
    with wave.open(str(output), "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getframerate() == 48_000
        assert audio.getsampwidth() == 2
        assert audio.getnframes() == speech.samples.size
        assert audio.readframes(8)


def test_emotion_presets_and_synthesis_validation_and_memory_budget() -> None:
    for emotion in Emotion:
        result = synthesize_wav("hello", emotion=emotion, language="en", seed=1)
        assert result.emotion is emotion
        assert np.isfinite(result.samples).all()
    with pytest.raises(ValueError, match="emotion"):
        synthesize_wav("hello", emotion="surprised")
    with pytest.raises(ValueError, match="sample_rate"):
        synthesize_wav("hello", sample_rate=1000)
    with pytest.raises(ValueError, match="seed"):
        synthesize_wav("hello", seed=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="language"):
        synthesize_wav("bonjour", language="fr")
    long_text = "a " * 400
    with pytest.raises(ValueError, match=f"{MAX_SYNTHESIS_SECONDS:g}s"):
        synthesize_wav(long_text, language="en")


def test_private_glottal_and_parallel_resonator_handle_zero_and_voiced_cases() -> None:
    rng = np.random.default_rng(3)
    profile = VoiceProfile("robot", 120.0, jitter=0.0, shimmer=0.0, breathiness=0.0)
    source = _glottal_source(
        400,
        8_000,
        np.full(400, 120.0),
        profile,
        type("Style", (), {"breath_multiplier": 1.0})(),  # type: ignore[arg-type]
        rng,
    )
    assert np.isfinite(source).all()
    assert np.max(np.abs(source)) > 0.0
    assert (
        _resonant_spectrum(np.zeros(0), 48_000, (500.0, 1500.0, 2500.0, 3500.0, 4500.0)).size == 0
    )
    filtered = _resonant_spectrum(
        np.sin(np.arange(1024, dtype=np.float64) * 0.1),
        48_000,
        (500.0, 1500.0, 2500.0, 3500.0, 4500.0),
    )
    assert filtered.shape == (1024,)
    assert np.isfinite(filtered).all()
    base = synthesize_wav("a", voice=profile, language="en", seed=4)
    skipped = _apply_formants(
        np.zeros(10, dtype=np.float64),
        48_000,
        (PhonemeSegment("ب", 1.0, 2.0),),
        profile,
    )
    assert np.array_equal(skipped, np.zeros(10))
    with pytest.raises(ValueError, match="mono signal"):
        SpeechSynthesis(
            np.zeros((2, 2), dtype=np.float32),
            base.sample_rate,
            base.text,
            base.language,
            base.voice,
            base.emotion,
            base.segments,
            base.visemes,
        )
    with pytest.raises(ValueError, match="sample_rate"):
        SpeechSynthesis(
            np.zeros(2, dtype=np.float32),
            0,
            base.text,
            base.language,
            base.voice,
            base.emotion,
            base.segments,
            base.visemes,
        )


def test_camera_projection_handles_visible_near_and_behind_points() -> None:
    camera = Camera(200, 100, position=(0.0, -4.0, 1.0), pitch_radians=0.0)
    pixels, depth = camera.project(((0.0, 0.0, 1.0), (0.1, 0.0, 1.0)))
    assert pixels[0] == pytest.approx((100.0, 50.0))
    assert depth == pytest.approx((4.0, 4.0))
    near_pixels, near_depth = camera.project(((0.0, -3.99, 1.0),))
    assert near_depth[0] < camera.near_clip_m
    assert np.isnan(near_pixels[0]).all()
    behind, _ = camera.project(((0.0, -5.0, 1.0),))
    assert np.isnan(behind[0]).all()
    with pytest.raises(ValueError, match="dimensions"):
        Camera(0, 100)
    with pytest.raises(ValueError, match="position"):
        Camera(100, 100, position=(0.0, 1.0))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="field of view"):
        Camera(100, 100, vertical_fov_degrees=180.0)
    with pytest.raises(ValueError, match="camera parameters"):
        Camera(100, 100, yaw_radians=float("nan"))


def test_layer_renderer_projects_geometry_shades_and_writes_rgba_png(tmp_path: Path) -> None:
    camera = Camera(160, 120, position=(0.0, -4.0, 1.25), pitch_radians=0.0)
    seal = ParametricAvatarCatalog().profile(120)
    face = FaceManifold.default().evaluate(seal.craniofacial) + np.asarray((0.0, 0.0, 1.5))
    face_triangles = face_grid_triangles()
    cloth = make_cloth_grid(0.9, 1.25, columns=5, rows=6, origin=(0.0, 0.0, 1.35))
    skeleton = Skeleton3D(seal.morphology).pose(root_translation=(0.0, 0.0, 0.72))
    scene = AvatarScene(
        skeleton=skeleton,
        face_vertices=face,
        face_triangles=face_triangles,
        cloth_vertices=cloth.vertices,
        cloth_triangles=cloth.triangles,
    )
    frame = AvatarLayerRenderer().render(scene, camera, time_seconds=0.25)
    assert set(frame.layers) == {"body", "face", "garment"}
    assert frame.composite.rgba.shape == (120, 160, 4)
    assert frame.composite.rgba[:, :, 3].any()
    assert frame.estimated_memory_bytes == estimate_frame_memory_bytes(160, 120)
    assert frame.time_seconds == 0.25
    path = frame.composite.write_png(tmp_path / "avatar.png")
    assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert frame.composite.width == 160
    assert frame.composite.height == 120


def test_renderer_supports_points_empty_scenes_and_rejects_bad_meshes() -> None:
    camera = Camera(64, 64, position=(0.0, -4.0, 0.0), pitch_radians=0.0)
    renderer = AvatarLayerRenderer()
    empty = renderer.render(AvatarScene(), camera)
    assert not empty.composite.rgba.any()
    point_scene = AvatarScene(face_vertices=np.asarray(((0.0, 0.0, 0.0),)))
    point_frame = renderer.render(point_scene, camera)
    assert point_frame.layers["face"].rgba[:, :, 3].any()
    hidden = renderer.render(AvatarScene(face_vertices=np.asarray(((0.0, -5.0, 0.0),))), camera)
    assert not hidden.layers["face"].rgba.any()
    behind_skeleton = Skeleton3D(np.zeros(24)).pose(root_translation=(0.0, -5.0, 0.0))
    behind_body = renderer.render(AvatarScene(skeleton=behind_skeleton), camera)
    assert not behind_body.layers["body"].rgba.any()
    hidden_triangle = renderer.render(
        AvatarScene(
            face_vertices=np.asarray(((0.0, -5.0, 0.0), (0.1, -5.0, 0.0), (0.0, -5.0, 0.1))),
            face_triangles=((0, 1, 2),),
        ),
        camera,
    )
    assert not hidden_triangle.layers["face"].rgba.any()
    outside_triangle = renderer.render(
        AvatarScene(
            face_vertices=np.asarray(((100.0, 0.0, 0.0), (101.0, 0.0, 0.0), (100.0, 0.0, 1.0))),
            face_triangles=((0, 1, 2),),
        ),
        camera,
    )
    assert not outside_triangle.layers["face"].rgba.any()
    flat_triangle = renderer.render(
        AvatarScene(
            face_vertices=np.asarray(((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 0.0, 0.0))),
            face_triangles=((0, 1, 2),),
        ),
        camera,
    )
    assert not flat_triangle.layers["face"].rgba.any()
    with pytest.raises(ValueError, match="frame exceeds"):
        renderer.render(AvatarScene(), Camera(1001, 1000))
    with pytest.raises(ValueError, match="time_seconds"):
        renderer.render(AvatarScene(), camera, time_seconds=-1.0)
    with pytest.raises(ValueError, match="face_triangles require"):
        AvatarScene(face_triangles=((0, 1, 2),))
    with pytest.raises(ValueError, match="cloth_triangles require"):
        AvatarScene(cloth_triangles=((0, 1, 2),))
    with pytest.raises(ValueError, match="index is out of bounds"):
        AvatarScene(face_vertices=np.zeros((3, 3)), face_triangles=((0, 1, 4),))
    with pytest.raises(ValueError, match="three indices"):
        AvatarScene(face_vertices=np.zeros((3, 3)), face_triangles=((0, 1),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="numeric"):
        AvatarScene(face_vertices=np.asarray((object(),), dtype=object))
    with pytest.raises(ValueError, match="finite"):
        AvatarScene(face_vertices=np.zeros((2, 2)))
    with pytest.raises(ValueError, match="byte channels"):
        AvatarScene(face_color=(1, 2, 3, 999))
    with pytest.raises(ValueError, match="shape"):
        RGBAFrame(np.zeros((2, 2), dtype=np.uint8), np.zeros((2, 2), dtype=np.float32))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="match rgba"):
        RGBAFrame(np.zeros((2, 2, 4), dtype=np.uint8), np.zeros((1, 1), dtype=np.float32))
    with pytest.raises(ValueError, match="uint8"):
        RGBAFrame(np.zeros((2, 2, 4), dtype=np.float32), np.zeros((2, 2), dtype=np.float32))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        estimate_frame_memory_bytes(1, 1, layers=0)
    with pytest.raises(ValueError, match="non-negative"):
        LayeredFrame({}, empty.composite, -1.0)
    assert MAX_RENDER_PIXELS == 1_000_000


def test_ffmpeg_sequence_encoder_is_safe_and_reports_missing_or_failed_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nexus_ai_agent.creative.biophysical_avatar.avatar_layer_renderer as renderer_module

    calls: list[tuple[list[str], dict[str, object]]] = []

    def record_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(renderer_module.shutil, "which", lambda binary: "/usr/bin/ffmpeg")
    monkeypatch.setattr(renderer_module.subprocess, "run", record_run)
    destination = encode_sequence(
        tmp_path / "frame-%04d.png",
        tmp_path / "movie.mp4",
        fps=24.0,
        audio_path=tmp_path / "voice.wav",
    )
    assert destination == tmp_path / "movie.mp4"
    command = calls[0][0]
    assert command[command.index("-framerate") + 1] == "24.0"
    assert "-shortest" in command and "-c:a" in command
    assert calls[0][1]["check"] is True
    encode_sequence(tmp_path / "frame-%04d.png", tmp_path / "silent.mp4", fps=30.0)
    assert "-an" in calls[1][0]
    with pytest.raises(ValueError, match="fps"):
        encode_sequence("frames.png", "video.mp4", fps=0.0)
    monkeypatch.setattr(renderer_module.shutil, "which", lambda binary: None)
    with pytest.raises(RuntimeError, match="not found"):
        encode_sequence("frames.png", "video.mp4", fps=24.0)
    monkeypatch.setattr(renderer_module.shutil, "which", lambda binary: "/usr/bin/ffmpeg")

    def fail_run(command: list[str], **kwargs: object) -> None:
        raise renderer_module.subprocess.CalledProcessError(1, command, stderr="bad input")

    monkeypatch.setattr(renderer_module.subprocess, "run", fail_run)
    with pytest.raises(RuntimeError, match="failed"):
        encode_sequence("frames.png", "video.mp4", fps=24.0)
