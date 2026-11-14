"""Fixtures are in-memory only; the repository-wide async DB fixture is unchanged."""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.world_backlot import (
    CanonicalWorldBacklotCatalog,
    DirectorSceneRecipe,
    RecipeQuality,
)

PERSIAN_SCENE = (
    "نیمه‌شب کویری ایران با آسمان پرستاره، باد و گردوغبار ملایم، "
    "معماری کاروانسرای سنتی و نور آبی مهتاب"
)


@pytest.fixture(scope="module")
def catalog() -> CanonicalWorldBacklotCatalog:
    return CanonicalWorldBacklotCatalog()


@pytest.fixture(scope="module")
def recipe(catalog: CanonicalWorldBacklotCatalog) -> DirectorSceneRecipe:
    return catalog.resolve_director_scene_recipe(PERSIAN_SCENE, seed="phase3")


@pytest.fixture()
def small_quality() -> RecipeQuality:
    return RecipeQuality(star_count=64, particle_count=32, cube_size=3, audio_seconds=0.02)
