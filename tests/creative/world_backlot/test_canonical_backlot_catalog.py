from __future__ import annotations

import itertools
import json
from dataclasses import FrozenInstanceError, replace

import numpy as np
import pytest

from nexus_ai_agent.creative.world_backlot import (
    ArchitectureStyle,
    ArchiveKind,
    CanonicalWorldBacklotCatalog,
    DirectorSceneRecipe,
    Observer,
    RecipeQuality,
    RecipeSeal,
    SurfaceKind,
    WeatherKind,
    normalize_query,
    parse_scene_description,
    resolve_director_scene_recipe,
)


def test_twelve_bilingual_cc0_archives_expose_more_than_100000_real_lazy_profiles(
    catalog: CanonicalWorldBacklotCatalog,
) -> None:
    assert [a.kind for a in catalog.archives] == list(ArchiveKind)
    assert len(catalog) == 122_880
    assert catalog.combination_count == 10240**12
    assert all(a.label_en and a.label_fa and len(a.families) == 10 for a in catalog.archives)
    identifiers: set[str] = set()
    count = 0
    # Walk the entire virtual archive. No output files or eager asset list.
    for asset in catalog.iter_entries():
        identifiers.add(asset.entry_id)
        count += 1
        assert asset.license == "CC0-Procedural"
        assert asset.parameters and asset.tags_en and asset.tags_fa
    assert len(identifiers) == count == 122_880
    assert catalog.entry_at(100_000).parameters


def test_profile_hashes_ids_and_parameters_are_stable_and_distinct_in_all_families(
    catalog: CanonicalWorldBacklotCatalog,
) -> None:
    hashes: set[str] = set()
    for archive in catalog.archives:
        for family in archive.families:
            first = catalog.entry(archive.kind, family.key, 0)
            other = catalog.entry(archive.kind, family.key, 1)
            last = catalog.entry(archive.kind, family.key, 1023)
            assert catalog.get(first.entry_id) == first
            assert first.parameters != other.parameters != last.parameters
            assert first.sha256 == catalog.get(first.entry_id).sha256
            assert len(first.sha256) == len(first.seed_sha256) == 64
            hashes.update((first.sha256, other.sha256, last.sha256))
            with pytest.raises(FrozenInstanceError):
                first.license = "other"  # type: ignore[misc]
    assert len(hashes) == 360
    assert catalog.entry("spoken_language", "fa").parameters[1] == ("direction", "rtl")
    assert catalog.entry("spoken_language", "en").parameters[1] == ("direction", "ltr")
    assert catalog.entry("architecture", "orbital_station").parameters[0] == (
        "style",
        ArchitectureStyle.SPACE_SCIFI.value,
    )


def test_bilingual_search_normalizes_arabic_letters_diacritics_zwnj_and_case(
    catalog: CanonicalWorldBacklotCatalog,
) -> None:
    assert normalize_query("نیمه‌شب، كُوير ايران") == "نیمه شب کویر ایران"
    assert normalize_query("  GLASS_Tower!! ") == "glass tower"
    fa = catalog.search("كاروانسرا", limit=3)
    en = catalog.search("caravanserai", limit=3)
    assert fa == en and len(fa) == 3
    assert all(a.family == "iranian_caravanserai" for a in fa)
    assert catalog.search("نیمه‌شب", archive=ArchiveKind.SPACETIME)[0].family == "midnight"
    assert catalog.search("GLASS TOWER")[0].family == "glass_tower"
    assert catalog.search("ماه", archive="spacetime")
    assert catalog.search("impossible-token") == ()
    assert catalog.search("caravanserai", archive="camera") == ()
    assert catalog.search("", limit=0) == ()


def test_search_pagination_is_stable_bounded_and_crosses_variant_families_correctly(
    catalog: CanonicalWorldBacklotCatalog,
) -> None:
    first = catalog.search("", archive="weather", limit=3, offset=1023)
    assert [(a.family, a.variant) for a in first] == [("clear", 1023), ("wind", 0), ("wind", 1)]
    page1 = catalog.search("", limit=4)
    page2 = catalog.search("", limit=4, offset=4)
    assert page1 + page2 == tuple(itertools.islice(catalog.iter_entries(), 8))
    assert catalog.search("", offset=len(catalog)) == ()
    small = CanonicalWorldBacklotCatalog(variants_per_family=2)
    assert len(tuple(small.iter_entries("weather"))) == 20
    assert len(small.search("caravanserai", limit=100)) == 2
    assert len(small.search("", archive="weather", limit=100)) == 20
    assert small.entry_at(len(small) - 1).family == "low_angle"
    assert small.entry_at(0) == small.entry("face", "oval", 0)
    with pytest.raises(AttributeError):
        catalog.variants_per_family = 1  # type: ignore[misc]


def test_catalogue_construction_and_search_do_not_materialize_unrequested_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus_ai_agent.creative.world_backlot import canonical_backlot_catalog as module

    original = module._profile
    calls: list[str] = []

    def counted(
        kind: ArchiveKind, family: str, seed: str | bytes | int
    ) -> tuple[module.Parameter, ...]:
        calls.append(family)
        return original(kind, family, seed)

    monkeypatch.setattr(module, "_profile", counted)
    registry = CanonicalWorldBacklotCatalog()
    assert calls == []
    assert len(registry.search("", limit=3, offset=100_000)) == 3
    assert len(calls) == 3


@pytest.mark.parametrize(
    ("text", "time"),
    [
        ("midnight", "midnight"),
        ("سپیده‌دم", "dawn"),
        ("صبح", "morning"),
        ("midday", "noon"),
        ("بعدازظهر", "afternoon"),
        ("ساعت طلایی", "sunset"),
        ("گرگ و میش", "twilight"),
        ("full moon", "full_moon"),
        ("ماه نو", "new_moon"),
        ("راه شیری", "milky_way"),
        ("starry night", "midnight"),
    ],
)
def test_director_time_lexicon(text: str, time: str) -> None:
    assert parse_scene_description(text).time_family == time


@pytest.mark.parametrize(
    ("text", "family"),
    [
        ("Iranian caravanserai", "iranian_caravanserai"),
        ("ایوان", "persian_courtyard"),
        ("معبد", "eastern_temple"),
        ("skyscraper", "glass_tower"),
        ("modern apartment", "urban_apartment"),
        ("قلعه", "medieval_castle"),
        ("palace", "classical_palace"),
        ("سایبرپانک", "cyberpunk_factory"),
        ("ایستگاه مداری", "orbital_station"),
        ("پایگاه سیاره‌ای", "planetary_base"),
    ],
)
def test_director_architecture_lexicon(text: str, family: str) -> None:
    assert parse_scene_description(text).architecture_family == family


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("rain", WeatherKind.RAIN),
        ("thunderstorm rain", WeatherKind.STORM),
        ("برف", WeatherKind.SNOW),
        ("گردوغبار", WeatherKind.DUST),
        ("volcanic ash", WeatherKind.ASH),
        ("برگ پاییزی", WeatherKind.LEAVES),
        ("مه غلیظ", WeatherKind.FOG),
        ("sparks", WeatherKind.SPARKS),
        ("نسیم", WeatherKind.WIND),
    ],
)
def test_director_weather_lexicon(text: str, kind: WeatherKind) -> None:
    assert parse_scene_description(text).weather_kinds[0] is kind


def test_negations_boundaries_priorities_surfaces_and_explicit_fallback_warnings() -> None:
    assert parse_scene_description("midnight city without rain and no fog").weather_kinds == (
        WeatherKind.CLEAR,
    )
    assert parse_scene_description("no rain but fog").weather_kinds == (WeatherKind.FOG,)
    assert parse_scene_description("no heavy rain but wind").weather_kinds == (WeatherKind.WIND,)
    assert parse_scene_description("without light rain and no thick fog").weather_kinds == (
        WeatherKind.CLEAR,
    )
    assert parse_scene_description("بدون باران و فاقد برف، باد ملایم").weather_kinds == (
        WeatherKind.WIND,
    )
    assert parse_scene_description("train in a building").weather_kinds == (WeatherKind.CLEAR,)
    assert parse_scene_description("urban rain").surface_kind is SurfaceKind.WET_ASPHALT
    assert parse_scene_description("rain").surface_kind is SurfaceKind.WET_SOIL
    assert parse_scene_description("rocky ground").surface_kind is SurfaceKind.ROCK
    assert parse_scene_description("desert snow").surface_kind is SurfaceKind.SNOW
    assert parse_scene_description("desert").surface_kind is SurfaceKind.SAND
    assert parse_scene_description("heavy wind").wind_strength == 0.9
    assert parse_scene_description("wind").wind_strength == 0.5
    assert parse_scene_description("gentle wind").wind_strength == 0.25
    assert parse_scene_description("mystery").wind_strength == 0.1
    assert parse_scene_description("mystery").warnings == (
        "No explicit time recognized; civil noon selected.",
        "No architecture recognized; Iranian caravanserai selected.",
    )
    assert parse_scene_description("noon factory neon").lighting_family == "neon"
    assert parse_scene_description("noon overcast").lighting_family == "overcast"
    assert parse_scene_description("sunset").lighting_family == "golden"
    assert parse_scene_description("interior crane").interior
    assert parse_scene_description("interior crane").camera_family == "crane"
    assert parse_scene_description("static shot").camera_family == "static"


def test_persian_scene_eagerly_materializes_all_artifacts_and_seals_every_dependency(
    recipe: DirectorSceneRecipe,
) -> None:
    assert recipe.verify()
    assert len(recipe.selected_assets) == 12
    assert {a.archive for a in recipe.selected_assets} == set(ArchiveKind)
    assert recipe.intent.time_family == "midnight" and recipe.intent.starry
    assert recipe.intent.architecture_family == "iranian_caravanserai"
    assert recipe.intent.weather_kinds == (WeatherKind.DUST, WeatherKind.WIND)
    assert recipe.intent.surface_kind is SurfaceKind.SAND
    assert recipe.intent.wind_strength == 0.25 and recipe.intent.lighting_family == "moonlight"
    assert recipe.layout.spec.style is ArchitectureStyle.IRANIAN_EASTERN
    assert len(recipe.layout.layers) == 4
    assert recipe.sky_state.observer.hour == 0 and recipe.sky_state.sun.elevation_deg < -18
    assert len(recipe.starfield.temperatures_k) == 2048
    assert sum(len(p.positions_m) for p in recipe.particle_fields) == 1024
    assert recipe.cube_text.startswith('TITLE "NAGAR')
    assert recipe.wav_bytes.startswith(b"RIFF") and len(recipe.wav_bytes) == 24044
    assert recipe.camera_curve.keyframes[-1].time_seconds == 8
    assert len(recipe.sha256) == 64 and len(recipe.equations) == 8
    manifest = json.loads(json.dumps(recipe.manifest(), ensure_ascii=False, allow_nan=False))
    assert manifest["license"] == "CC0-Procedural"
    assert manifest["scene_dna"] == recipe.layout.seal.sha256
    assert len(manifest["assets"]) == 12


def test_recipe_repeatability_variation_observer_override_and_caller_quality(
    recipe: DirectorSceneRecipe,
    catalog: CanonicalWorldBacklotCatalog,
    small_quality: RecipeQuality,
) -> None:
    same = resolve_director_scene_recipe(recipe.intent.description, seed="phase3")
    assert same.sha256 == recipe.sha256
    assert same.wav_bytes == recipe.wav_bytes and same.cube_text == recipe.cube_text
    changed = catalog.resolve_director_scene_recipe(
        recipe.intent.description, seed="other", quality=small_quality
    )
    assert changed.sha256 != recipe.sha256
    observer = Observer(-33.8, 355, 14, longitude_deg=151.2, utc_offset_hours=11)
    explicit = resolve_director_scene_recipe(
        "noon modern city rain", seed=1, quality=small_quality, observer=observer, catalog=catalog
    )
    assert explicit.sky_state.observer == observer
    assert len(explicit.starfield.temperatures_k) == 64
    assert explicit.cube.values.shape == (27, 3)
    assert explicit.ambient_audio.duration_seconds == 0.02
    assert explicit.surface.kind is SurfaceKind.WET_ASPHALT
    assert explicit.verify()


def test_geometry_is_independent_of_time_weather_and_preview_quality(
    catalog: CanonicalWorldBacklotCatalog,
    small_quality: RecipeQuality,
) -> None:
    noon = catalog.resolve_director_scene_recipe("noon Iran wind", seed=42, quality=small_quality)
    night = catalog.resolve_director_scene_recipe("midnight Iran rain", seed=42)
    assert noon.layout == night.layout
    assert noon.layout.seal.sha256 == night.layout.seal.sha256
    assert noon.cube_text != night.cube_text and noon.wav_bytes != night.wav_bytes
    assert noon.starfield.seed_sha256 == night.starfield.seed_sha256


def test_multiple_weather_fields_share_a_total_budget_and_empty_scenes_stay_valid(
    catalog: CanonicalWorldBacklotCatalog,
) -> None:
    quality = RecipeQuality(star_count=0, particle_count=11, cube_size=2, audio_seconds=1 / 48000)
    combined = catalog.resolve_director_scene_recipe(
        "noon modern rain snow dust fog", quality=quality
    )
    assert [len(p.positions_m) for p in combined.particle_fields] == [3, 3, 3, 2]
    assert combined.verify() and len(combined.wav_bytes) == 46
    clear = catalog.resolve_director_scene_recipe("noon Iran", quality=quality)
    assert len(clear.particle_fields) == 1 and len(clear.particle_fields[0].positions_m) == 0
    assert list(clear.iter_frames(1))[0].star_flux.size == 0


def test_frames_advance_weather_and_camera_without_changing_any_sealed_scene_geometry(
    recipe: DirectorSceneRecipe,
) -> None:
    frames = list(recipe.iter_frames(4, fps=24))
    again = list(recipe.iter_frames(4, fps=24))
    assert [f.time_seconds for f in frames] == [0, 1 / 24, 2 / 24, 3 / 24]
    assert all(f.scene_dna_sha256 == recipe.layout.seal.sha256 for f in frames)
    assert frames[0].particles == recipe.particle_fields
    for i, frame in enumerate(frames):
        assert frame.particles[0].time_seconds == pytest.approx(frame.time_seconds)
        assert frame.particles[0].step_index == i
        assert frame.particles[0].sha256 == again[i].particles[0].sha256
    assert not np.array_equal(
        frames[0].particles[0].positions_m, frames[-1].particles[0].positions_m
    )
    assert not np.array_equal(frames[0].star_flux, frames[-1].star_flux)
    assert recipe.verify() and recipe.particle_fields[0].step_index == 0
    assert list(recipe.iter_frames(0)) == []


def test_recipe_tampering_is_detected_for_each_artifact_and_frame_generation_refuses_it(
    recipe: DirectorSceneRecipe,
) -> None:
    alterations = (
        replace(recipe, cube_text=recipe.cube_text + "# changed\n"),
        replace(recipe, seal=RecipeSeal("0" * 64)),
        replace(recipe, seed_sha256="0" * 64),
        replace(recipe, selected_assets=recipe.selected_assets[::-1]),
        replace(recipe, layout=replace(recipe.layout, objects=recipe.layout.objects[::-1])),
        replace(recipe, surface=replace(recipe.surface, roughness=0.99)),
    )
    for corrupted in alterations:
        assert not corrupted.verify()
    with pytest.raises(ValueError, match="integrity"):
        list(alterations[0].iter_frames(1))
    with pytest.raises(ValueError, match="duration"):
        list(recipe.iter_frames(1000))
    with pytest.raises(ValueError):
        list(recipe.iter_frames(1, fps=0))


def test_invalid_catalogue_queries_ids_and_quality_are_rejected(
    catalog: CanonicalWorldBacklotCatalog,
) -> None:
    for bad in ("", "wb1.face.oval.0000.foreign", "x.face.oval.0000.foreign"):
        with pytest.raises(ValueError):
            catalog.get(bad)
    valid = catalog.entry("face", "oval").entry_id
    with pytest.raises(ValueError):
        catalog.get(valid.replace(".0000.", ".-001."))
    with pytest.raises(ValueError):
        CanonicalWorldBacklotCatalog("foreign").get(valid)
    for category, family, variant in (
        ("face", "unknown", 0),
        ("unknown", "oval", 0),
        ("face", "oval", 1024),
    ):
        with pytest.raises(ValueError):
            catalog.entry(category, family, variant)
    with pytest.raises(ValueError):
        catalog.entry_at(len(catalog))
    with pytest.raises(ValueError):
        CanonicalWorldBacklotCatalog(variants_per_family=0)
    with pytest.raises(ValueError):
        catalog.search("", limit=101)
    with pytest.raises(ValueError):
        catalog.search("", offset=-1)
    with pytest.raises(ValueError):
        normalize_query("x" * 4097)
    with pytest.raises(ValueError):
        parse_scene_description(" ، ")
    for kwargs in (
        {"star_count": 32_769},
        {"particle_count": -1},
        {"cube_size": 34},
        {"audio_seconds": 6},
        {"camera_seconds": 0},
    ):
        with pytest.raises(ValueError):
            RecipeQuality(**kwargs)
