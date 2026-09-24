"""Gate 2.2 — confirm the derived surface against a real execution.

The architecture gate derives the ``/slideshow`` surface by *parsing* the
service module: it collects the ``bus.dispatch(_command(OPERATION_*, …))`` call
sites and treats those as the operations the surface reaches.  A static
derivation is only trustworthy if a runtime measurement agrees with it.

This module is that second, independent measurement.  It runs the real
slideshow planning/render path on synthesized media and compares the commands
the ``CommandBus`` actually recorded against the set the parser predicted.  The
two are derived by completely different means — one reads code, the other
executes it — so a disagreement means one of them is wrong, and the gate reds
rather than quietly keeping a stale claim.

The repository ships no binary media: every fixture is generated
(``tests/unit/slideshow_media.py``), so this stays reproducible.
"""

from __future__ import annotations

import pytest
from slideshow_media import SlideshowMedia

from nexus_ai_agent.creative.slideshow.service import (
    PlanningRequest,
    plan_from_files,
    render_from_files,
)
from nexus_ai_agent.nagar import sources

#: ``render_from_files`` adds the render step that ``plan_from_files`` stops short of.
#: See ``creative/slideshow/service.py`` (``commands=(*session.outcome.commands,
#: OPERATION_RENDER)``) — the runtime measurement is what keeps this honest.
_PLANNING_ONLY = frozenset({"slideshow.render"})


def _request(media: SlideshowMedia) -> PlanningRequest:
    return PlanningRequest(images=media.images, target_duration_us=60_000_000, audio=media.audio)


def test_the_parser_predicts_what_the_planner_executes(slideshow_media: SlideshowMedia) -> None:
    """The static surface derivation must cover every command the bus recorded."""
    predicted = set(sources.probe_slideshow_entrypoint().operations)
    outcome = plan_from_files(_request(slideshow_media))
    executed = set(outcome.commands)

    assert executed, "the planner dispatched nothing — the confirmation would be vacuous"
    assert executed <= predicted, (
        "the slideshow surface executed operations the parser did not predict: "
        f"{sorted(executed - predicted)}.  Either the parser is stale or the surface "
        "gained a path the gate cannot see."
    )


def test_the_planner_reaches_the_operations_the_gate_claims(
    slideshow_media: SlideshowMedia,
) -> None:
    """The predicted set and the executed set differ by exactly the render step.

    Recording the *difference* rather than demanding equality is deliberate: the
    planning half and the full render half of the same surface legitimately reach
    different operations, and the gate must say which is which instead of
    rounding them together.
    """
    predicted = set(sources.probe_slideshow_entrypoint().operations)
    planning = set(plan_from_files(_request(slideshow_media)).commands)
    assert predicted - planning == _PLANNING_ONLY, (
        f"predicted-but-not-planned={sorted(predicted - planning)} "
        f"planned-but-not-predicted={sorted(planning - predicted)}"
    )


@pytest.mark.slow
def test_the_full_render_path_reaches_every_predicted_operation(
    slideshow_media: SlideshowMedia, tmp_path
) -> None:
    """Run the real render lane and require it to match the prediction exactly.

    This is the expensive half of the confirmation and the only place the gate
    executes media.  It is the *artifact* lane, so it produces a verified file
    and the bus records the render step the planner alone never reaches.

    Marked ``slow`` because a real encode costs ~2.5 minutes: the fast suite
    keeps the static confirmation (which is what catches surface drift in a
    second), and the dedicated CI step below runs this one on every pull
    request.  Excluding it from ``-m "not slow"`` therefore loses no signal —
    it is `named` as its own step instead of hidden in the bulk suite.
    """
    outcome = render_from_files(
        _request(slideshow_media),
        output_path=tmp_path / "master.mp4",
        overwrite=True,
    )
    executed = set(outcome.commands)
    predicted = set(sources.probe_slideshow_entrypoint().operations)

    assert executed == predicted, (
        "the runtime slideshow surface and the parsed surface disagree: "
        f"executed-only={sorted(executed - predicted)} "
        f"predicted-only={sorted(predicted - executed)}"
    )
    assert outcome.artifact, "a render with no artifact proves nothing"
