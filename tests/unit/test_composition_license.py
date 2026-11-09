"""The licence ledger of a composition (Gate 5).

The rules under test are the ones that keep a runtime from claiming a
permission it never had:

1. the **twelve categories stay apart** — a tool's licence never becomes the
   output's rights;
2. **``UNKNOWN`` stays unknown** — a category that admits it does not know is
   reported as unknown, and never counts as "commercially approved";
3. the FFmpeg facts are **measured**, not remembered — the version, the
   ``-buildconf`` flags and the active encoders are read back from the binary
   that actually runs here;
4. ``libx264`` being present says nothing about the produced media's rights.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.composition.contracts import CATEGORIES, LicenseLedger
from nexus_ai_agent.creative.composition.license import (
    compose_ledger,
    ffmpeg_build_facts,
    ffmpeg_ledger,
    input_asset_record,
    ledger_report,
    unknown_ledger,
)


def test_the_twelve_categories_are_exactly_the_ones_the_mission_names() -> None:
    assert CATEGORIES == (
        "adapter_code",
        "runtime_software",
        "library",
        "model",
        "model_weights",
        "dataset",
        "input_asset",
        "distribution_restrictions",
        "commercial_permission",
        "attribution_notices",
        "copyleft",
        "network_use",
    )
    assert len(CATEGORIES) == 12


def test_an_empty_ledger_states_nothing_and_verifies_nothing() -> None:
    ledger = unknown_ledger()
    assert ledger.records == ()
    assert ledger.unknown_categories == CATEGORIES
    assert ledger.effective_allow_commercial is False


def test_the_ffmpeg_facts_are_measured_from_the_binary() -> None:
    facts = ffmpeg_build_facts()
    assert facts["binary"]
    assert facts["version"] != "unknown", "the FFmpeg version was not measured"
    assert facts["configuration"], "the build configuration was not measured"
    assert isinstance(facts["encoders"], dict)
    # The encoder list is read back from the binary, so at least one entry
    # must have been determined.
    assert any(value is True for value in facts["encoders"].values())


def test_the_measured_ledger_separates_the_tool_from_the_output() -> None:
    ledger = ffmpeg_ledger()
    categories = {record.category for record in ledger.records}
    assert "runtime_software" in categories
    assert "library" in categories
    assert "adapter_code" in categories
    # The tool's licence never says anything about the media it produces.
    assert "input_asset" not in categories
    assert "commercial_permission" not in categories
    report = ledger_report(ledger)
    assert report["categories"] == list(CATEGORIES)
    assert "input_asset" in report["unknown_categories"]
    assert "commercial_permission" in report["unknown_categories"]
    assert report["effective_allow_commercial"] is False


def test_the_gpl_flag_is_reported_as_a_fact_about_the_tool() -> None:
    facts = ffmpeg_build_facts()
    ledger = ffmpeg_ledger()
    runtime = ledger.of("runtime_software")
    assert runtime
    record = runtime[0]
    if facts["gpl"]:
        assert record.copyleft is True
        assert "GPL" in record.spdx_id
    else:
        assert record.copyleft is False
    # Whatever the tool's licence says, the record's own scope must not
    # claim anything about the produced media.
    assert "media" in record.distribution_scope.lower()


def test_an_unknown_input_asset_keeps_the_commercial_verdict_false() -> None:
    ledger = compose_ledger(ffmpeg_ledger(), {"source.png": "a" * 64})
    assert ledger.of("input_asset")
    record = ledger.of("input_asset")[0]
    assert record.spdx_id == "UNKNOWN"
    assert record.allow_commercial is None
    assert record.commercial_use_verified is False
    # An explicit UNKNOWN record must still be *reported* as unknown.
    assert "input_asset" in ledger.unknown_categories
    assert ledger.effective_allow_commercial is False


def test_a_record_that_admits_ignorance_is_not_treated_as_a_permission() -> None:
    record = input_asset_record("clip.mp4", "b" * 64)
    assert record.effective_allow_commercial is False
    ledger = LicenseLedger(records=(record,))
    assert ledger.effective_allow_commercial is False
    assert "input_asset" in ledger.unknown_categories


def test_a_proven_record_does_not_rescue_the_missing_categories() -> None:
    """One proven category cannot make a ledger commercially approved."""
    ledger = compose_ledger(ffmpeg_ledger(), {})
    assert ledger.of("runtime_software")[0].effective_allow_commercial is True
    assert ledger.effective_allow_commercial is False
    assert ledger.unknown_categories  # nine categories are still unresolved


def test_the_report_never_conflates_the_tool_with_the_output() -> None:
    report = ledger_report(ffmpeg_ledger())
    assert "never implies" in report["note"]
    assert report["commercial_use_verified"] is False


@pytest.mark.parametrize(
    "category",
    [
        "adapter_code",
        "runtime_software",
        "library",
        "model",
        "model_weights",
        "dataset",
        "input_asset",
        "distribution_restrictions",
        "commercial_permission",
        "attribution_notices",
        "copyleft",
        "network_use",
    ],
)
def test_no_category_is_silently_dropped_from_the_report(category: str) -> None:
    report = ledger_report(ffmpeg_ledger())
    assert category in report["categories"]
