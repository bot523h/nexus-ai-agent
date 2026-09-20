"""Wave 2.5 r7 — 30 s joins the approved pack targets without bending the planner.

The Telegram surface may never exceed half a minute, so 30 s must be a
representable plan target; the pure planner has to tile it exactly, and the
previously approved durations must stay valid (additive change only).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from slideshow_media import make_image

from nexus_ai_agent.creative.packs.slideshow.models import TARGET_DURATIONS_US
from nexus_ai_agent.creative.packs.slideshow.planning import MIN_SHOT_US
from nexus_ai_agent.creative.slideshow.service import PlanningRequest, plan_from_files

THIRTY_SECONDS_US = 30_000_000


def _images(directory: Path, count: int) -> tuple[Path, ...]:
    return tuple(make_image(directory / f"shot_{i}.jpg", seed=i) for i in range(count))


def test_30s_joins_the_frozen_target_set_without_disturbing_it() -> None:
    assert 30_000_000 in TARGET_DURATIONS_US
    assert set(TARGET_DURATIONS_US) >= {60_000_000, 120_000_000, 300_000_000}
    assert TARGET_DURATIONS_US == tuple(sorted(TARGET_DURATIONS_US))


def test_plan_tiles_30_seconds_exactly_end_to_end(tmp_path: Path) -> None:
    images = _images(tmp_path, 4)
    outcome = plan_from_files(PlanningRequest(images=images, target_duration_us=THIRTY_SECONDS_US))
    shots = outcome.plan["shots"]
    assert len(shots) == 4
    assert shots[0]["slot"]["start_us"] == 0
    assert shots[-1]["slot"]["end_us"] == THIRTY_SECONDS_US
    for previous, following in zip(shots, shots[1:], strict=False):
        assert previous["slot"]["end_us"] == following["slot"]["start_us"]
    assert all(shot["slot"]["end_us"] - shot["slot"]["start_us"] >= MIN_SHOT_US for shot in shots)
    assert sum(s["slot"]["end_us"] - s["slot"]["start_us"] for s in shots) == THIRTY_SECONDS_US


def test_12_images_still_plan_at_30s(slideshow_media) -> None:
    """The shared 12-image fixture must fit 2.5 s shots (r7 lane validation)."""
    outcome = plan_from_files(
        PlanningRequest(images=slideshow_media.images, target_duration_us=THIRTY_SECONDS_US)
    )
    assert len(outcome.plan["shots"]) == 12
    assert outcome.plan["shots"][-1]["slot"]["end_us"] == THIRTY_SECONDS_US


def test_unapproved_duration_is_still_refused(tmp_path: Path) -> None:
    images = _images(tmp_path, 1)
    with pytest.raises(ValidationError):
        plan_from_files(PlanningRequest(images=images, target_duration_us=45_000_000))
