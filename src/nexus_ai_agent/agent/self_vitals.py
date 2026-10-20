"""Real meters for the Living Core's vital signs (measured, never fabricated).

Only quantities that are actually measured here are offered. The provider
fallback ratio is intentionally absent until the provider chain feeds a
``ProviderHealthTracker`` — a probe that always reads 0.0 would be a lie.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Callable
from typing import Any

import psutil  # type: ignore


def default_probes(*, max_ram_mb: int, disk_path: str = "/") -> dict[str, Callable[[], float]]:
    def ram_percent() -> float:
        rss_mb = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
        return rss_mb / max_ram_mb * 100.0

    def disk_free_gb() -> float:
        return shutil.disk_usage(disk_path).free / (1024**3)

    return {"ram_percent": ram_percent, "disk_free_gb": disk_free_gb}


# ── Opt-in runtime wiring ───────────────────────────────────────────────────


class VitalsHandle:
    """A running vitals scheduler; ``stop`` is idempotent and awaits the task."""

    def __init__(self, scheduler: Any, task: asyncio.Task[None]) -> None:
        self._scheduler = scheduler
        self._task = task

    async def stop(self) -> None:
        self._scheduler.stop()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except Exception:  # noqa: BLE001 — shutdown must not raise
            self._task.cancel()


async def start_vitals(
    *, enabled: bool, db_path: str, period_s: float, max_ram_mb: int
) -> VitalsHandle | None:
    """Boot the Self-Ledger and start sampling — only when enabled AND healthy.

    Returns ``None`` (observation off) when disabled, when the sidecar is
    unavailable, or when the ledger/charter failed verification.
    """
    if not enabled:
        return None
    from nexus_ai_agent.selfhood.runtime import SelfBootStatus, self_ledger_db_path, try_boot_self
    from nexus_ai_agent.selfhood.vitals import VitalSigns, VitalsScheduler

    boot = try_boot_self(self_ledger_db_path(db_path))
    if boot.status is not SelfBootStatus.OK or boot.state is None:
        return None
    vitals = VitalSigns(boot.state.ledger, default_probes(max_ram_mb=max_ram_mb))
    scheduler = VitalsScheduler(vitals, period_s=period_s)
    return VitalsHandle(scheduler, asyncio.create_task(scheduler.run()))


__all__ = ["VitalsHandle", "default_probes", "start_vitals"]
