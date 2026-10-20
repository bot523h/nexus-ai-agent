"""Phase 2 of the Living Core: vital signs (interoception) persisted in the Self-Ledger."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nexus_ai_agent.selfhood.charter import CHARTER
from nexus_ai_agent.selfhood.ledger import SelfEntryKind, SelfLedger
from nexus_ai_agent.selfhood.vitals import (
    ProviderHealthTracker,
    VitalSigns,
    VitalsScheduler,
    latest_readings,
)


def _ledger(tmp_path: Path) -> SelfLedger:
    return SelfLedger(tmp_path / "self.sqlite3")


def test_setpoints_come_from_the_charter_not_from_the_caller(tmp_path: Path) -> None:
    vitals = VitalSigns(_ledger(tmp_path), probes={"provider_fallback_ratio": lambda: 0.0})
    assert (
        vitals.setpoints["provider_fallback_ratio"].limit
        == CHARTER.setpoints["provider_fallback_ratio_max"]
    )


def test_probe_without_a_charter_setpoint_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        VitalSigns(_ledger(tmp_path), probes={"mood": lambda: 1.0})


def test_in_range_reading_is_recorded_without_deviation(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    VitalSigns(ledger, probes={"ram_percent": lambda: 10.0}).sample()
    kinds = [r.kind for r in ledger.records()]
    assert kinds == [SelfEntryKind.VITAL_READING]


def test_out_of_range_reading_records_a_deviation(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    readings = VitalSigns(ledger, probes={"ram_percent": lambda: 99.0}).sample()
    assert readings[0].deviating
    assert [r.kind for r in ledger.records()] == [
        SelfEntryKind.VITAL_READING,
        SelfEntryKind.VITAL_DEVIATION,
    ]


def test_min_setpoint_deviates_when_below(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    readings = VitalSigns(ledger, probes={"disk_free_gb": lambda: 0.01}).sample()
    assert readings[0].deviating


def test_a_failing_probe_is_itself_a_deviation_never_a_crash(tmp_path: Path) -> None:
    def broken() -> float:
        raise RuntimeError("probe exploded")

    ledger = _ledger(tmp_path)
    readings = VitalSigns(ledger, probes={"ram_percent": broken}).sample()
    assert readings[0].deviating and readings[0].value is None
    assert ledger.verify().ok


def test_vitals_survive_restart(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    VitalSigns(ledger, probes={"ram_percent": lambda: 42.0}).sample()
    reopened = SelfLedger(tmp_path / "self.sqlite3")
    assert latest_readings(reopened)["ram_percent"].value == 42.0


def test_provider_health_tracker_computes_fallback_ratio() -> None:
    tracker = ProviderHealthTracker(window=10)
    for _ in range(7):
        tracker.record(primary_ok=True)
    for _ in range(3):
        tracker.record(primary_ok=False)  # fell back to a secondary provider
    assert tracker.fallback_ratio() == pytest.approx(0.3)
    assert ProviderHealthTracker(window=5).fallback_ratio() == 0.0


def test_provider_health_window_forgets_old_calls() -> None:
    tracker = ProviderHealthTracker(window=4)
    for _ in range(4):
        tracker.record(primary_ok=False)
    for _ in range(4):
        tracker.record(primary_ok=True)
    assert tracker.fallback_ratio() == 0.0


def test_simulated_provider_outage_lands_in_the_ledger_within_one_period(tmp_path: Path) -> None:
    """A provider outage must land as a vital deviation.

    Deterministic by construction: the probe counts its own invocations and the
    test stops the scheduler once the required samples have actually run — there
    is no reliance on wall-clock sleeps racing the scheduler period.
    """
    ledger = _ledger(tmp_path)
    tracker = ProviderHealthTracker(window=10)
    samples = {"n": 0}

    def probe() -> float:
        samples["n"] += 1
        if samples["n"] >= 2:  # the outage begins after the first healthy sample
            for _ in range(10):
                tracker.record(primary_ok=False)
        return tracker.fallback_ratio()

    vitals = VitalSigns(ledger, probes={"provider_fallback_ratio": probe})
    scheduler = VitalsScheduler(vitals, period_s=0.01)

    async def scenario() -> None:
        task = asyncio.create_task(scheduler.run())
        while samples["n"] < 2:  # wait on real progress, not on a fixed sleep
            await asyncio.sleep(0)
        scheduler.stop()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    deviations = [r for r in ledger.records() if r.kind is SelfEntryKind.VITAL_DEVIATION]
    assert deviations and deviations[0].payload["subject"] == "vital:provider_fallback_ratio"
    assert ledger.verify().ok


def test_kill_switch_stops_sampling(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    samples = {"n": 0}

    def probe() -> float:
        samples["n"] += 1
        return 1.0

    scheduler = VitalsScheduler(VitalSigns(ledger, probes={"ram_percent": probe}), period_s=0.01)

    async def scenario() -> None:
        task = asyncio.create_task(scheduler.run())
        while samples["n"] < 1:  # wait for a real sample, not a fixed sleep
            await asyncio.sleep(0)
        scheduler.stop()
        await asyncio.wait_for(task, timeout=5)  # returns only if the kill switch works

    asyncio.run(scenario())
    assert samples["n"] >= 1
    count = len(ledger.records())
    asyncio.run(asyncio.sleep(0.05))  # a (broken) zombie loop would sample again here
    assert len(SelfLedger(ledger.db_path).records()) == count


def test_nan_reading_is_a_deviation_not_a_pass(tmp_path: Path) -> None:
    readings = VitalSigns(_ledger(tmp_path), probes={"ram_percent": lambda: float("nan")}).sample()
    assert readings[0].deviating and readings[0].value is None


def test_restart_judges_a_reading_by_the_limit_stored_with_it(tmp_path: Path) -> None:
    from dataclasses import replace
    from types import MappingProxyType

    loose = replace(
        CHARTER,
        setpoints=MappingProxyType({**CHARTER.setpoints, "ram_percent_max": 99.0}),
    )
    ledger = _ledger(tmp_path)
    VitalSigns(ledger, probes={"ram_percent": lambda: 95.0}, charter=loose).sample()
    reading = latest_readings(ledger)["ram_percent"]
    assert reading.setpoint.limit == 99.0 and not reading.deviating  # not re-judged by CHARTER
