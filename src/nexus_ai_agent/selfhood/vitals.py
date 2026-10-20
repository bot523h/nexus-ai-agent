"""Vital signs: scheduled interoception persisted in the Self-Ledger (Phase 2).

Meters are plain callables supplied by the composition root; the *setpoints*
come only from the charter (constitutional zone), so a learner cannot loosen
the threshold it is judged by. A failing probe is recorded as a deviation —
an unreadable vital is never silently treated as healthy.

Naming: a charter setpoint ``<vital>_max`` bounds a vital from above and
``<vital>_min`` from below.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from nexus_ai_agent.selfhood.charter import CHARTER, Charter
from nexus_ai_agent.selfhood.ledger import SelfEntryKind, SelfLedger

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Setpoint:
    name: str
    limit: float
    direction: str  # "max": value must be <= limit; "min": value must be >= limit

    def violated_by(self, value: float) -> bool:
        return value > self.limit if self.direction == "max" else value < self.limit


@dataclass(frozen=True)
class VitalReading:
    name: str
    value: float | None
    setpoint: Setpoint
    deviating: bool
    error: str | None = None


def setpoints_from(charter: Charter) -> dict[str, Setpoint]:
    result: dict[str, Setpoint] = {}
    for key, limit in charter.setpoints.items():
        for direction in ("max", "min"):
            suffix = f"_{direction}"
            if key.endswith(suffix):
                name = key[: -len(suffix)]
                result[name] = Setpoint(name, float(limit), direction)
    return result


class VitalSigns:
    def __init__(
        self,
        ledger: SelfLedger,
        probes: Mapping[str, Callable[[], float]],
        *,
        charter: Charter = CHARTER,
    ) -> None:
        self.setpoints = setpoints_from(charter)
        unknown = sorted(set(probes) - set(self.setpoints))
        if unknown:
            raise ValueError(f"probes without a charter setpoint: {unknown}")
        self._ledger = ledger
        self._probes = dict(probes)

    def sample(self) -> list[VitalReading]:
        readings: list[VitalReading] = []
        for name, probe in self._probes.items():
            setpoint = self.setpoints[name]
            try:
                measured = float(probe())
                if not math.isfinite(measured):
                    raise ValueError(f"non-finite reading {measured!r}")
                value: float | None = measured
                error = None
                deviating = setpoint.violated_by(measured)
            except Exception as exc:  # a broken meter is a finding, not a crash
                value, error, deviating = None, f"{type(exc).__name__}: {exc}", True
            reading = VitalReading(name, value, setpoint, deviating, error)
            detail = {
                "value": value,
                "limit": setpoint.limit,
                "direction": setpoint.direction,
                "error": error,
            }
            self._ledger.append(SelfEntryKind.VITAL_READING, subject=f"vital:{name}", detail=detail)
            if deviating:
                self._ledger.append(
                    SelfEntryKind.VITAL_DEVIATION, subject=f"vital:{name}", detail=detail
                )
            readings.append(reading)
        return readings


def latest_readings(ledger: SelfLedger) -> dict[str, VitalReading]:
    """Most recent reading per vital, rebuilt from the chain (restart-safe).

    Judged against the limit and direction stored WITH each reading, so the
    result always agrees with the ``vital_deviation`` record in the same chain,
    even after a later charter amendment.
    """
    latest: dict[str, VitalReading] = {}
    for record in ledger.records():
        if record.kind is not SelfEntryKind.VITAL_READING:
            continue
        name = str(record.payload["subject"]).removeprefix("vital:")
        detail = record.payload["detail"]
        setpoint = Setpoint(name, float(detail["limit"]), str(detail["direction"]))
        value = detail.get("value")
        deviating = True if value is None else setpoint.violated_by(float(value))
        latest[name] = VitalReading(name, value, setpoint, deviating, detail.get("error"))
    return latest


class ProviderHealthTracker:
    """Sliding window over LLM calls: how often the primary provider failed."""

    def __init__(self, window: int = 50) -> None:
        self._calls: deque[bool] = deque(maxlen=window)
        self._lock = threading.Lock()

    def record(self, *, primary_ok: bool) -> None:
        with self._lock:
            self._calls.append(primary_ok)

    def fallback_ratio(self) -> float:
        with self._lock:
            if not self._calls:
                return 0.0
            return sum(1 for ok in self._calls if not ok) / len(self._calls)


class VitalsScheduler:
    """Periodic sampler with a kill switch (``stop``); single event loop only."""

    def __init__(self, vitals: VitalSigns, *, period_s: float) -> None:
        if period_s <= 0:
            raise ValueError("period_s must be positive")
        self._vitals = vitals
        self._period = period_s
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._vitals.sample)
            except Exception:  # sampling must never kill the scheduler
                _LOGGER.exception("vital sampling failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._period)
            except asyncio.TimeoutError:
                pass


__all__ = [
    "ProviderHealthTracker",
    "Setpoint",
    "VitalReading",
    "VitalSigns",
    "VitalsScheduler",
    "latest_readings",
    "setpoints_from",
]
