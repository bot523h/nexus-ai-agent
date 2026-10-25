"""Phase 2b: fail-safe boot of the Self-Ledger + real probes + opt-in wiring."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from nexus_ai_agent.agent.self_vitals import default_probes
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.selfhood.boot import boot_self
from nexus_ai_agent.selfhood.charter import CHARTER
from nexus_ai_agent.selfhood.runtime import SelfBootStatus, self_ledger_db_path, try_boot_self


def test_path_sits_next_to_the_main_database() -> None:
    assert str(self_ledger_db_path("/data/nexus.db")).endswith("nexus.db.self.sqlite3")


def test_healthy_boot_is_ok(tmp_path: Path) -> None:
    boot = try_boot_self(tmp_path / "s.sqlite3")
    assert boot.status is SelfBootStatus.OK and boot.state is not None


def test_tampered_ledger_degrades_to_tampered_never_to_ok(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite3"
    boot_self(path)
    with sqlite3.connect(path) as raw:
        raw.execute("UPDATE nexus_self_ledger SET record_json = record_json || ' '")
    boot = try_boot_self(path)
    assert boot.status is SelfBootStatus.TAMPERED and boot.state is None


def test_charter_drift_is_reported_as_drift(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite3"
    boot_self(path)
    boot = try_boot_self(path, charter=replace(CHARTER, version=CHARTER.version + 1))
    assert boot.status is SelfBootStatus.CHARTER_DRIFT and boot.state is None


def test_unavailable_sidecar_degrades_without_raising(tmp_path: Path) -> None:
    boot = try_boot_self(tmp_path)  # a directory is not a database file
    assert boot.status is SelfBootStatus.UNAVAILABLE and boot.state is None


def test_default_probes_are_real_measurements() -> None:
    probes = default_probes(max_ram_mb=1500)
    assert set(probes) == {"ram_percent", "disk_free_gb"}  # no fabricated provider probe
    assert probes["ram_percent"]() > 0 and probes["disk_free_gb"]() > 0


def test_vitals_are_opt_in_by_default() -> None:
    settings = Settings()
    assert settings.self_vitals_enabled is False and settings.self_vitals_period_s >= 5


def test_start_vitals_disabled_is_a_noop(tmp_path: Path) -> None:
    import asyncio

    from nexus_ai_agent.agent.self_vitals import start_vitals

    handle = asyncio.run(
        start_vitals(enabled=False, db_path=str(tmp_path / "n.db"), period_s=5, max_ram_mb=1500)
    )
    assert handle is None and not list(tmp_path.iterdir())


def test_start_vitals_samples_into_the_ledger_and_stops(tmp_path: Path) -> None:
    import asyncio

    from nexus_ai_agent.agent.self_vitals import start_vitals
    from nexus_ai_agent.selfhood.ledger import SelfEntryKind, SelfLedger

    db = str(tmp_path / "n.db")

    async def scenario() -> None:
        handle = await start_vitals(enabled=True, db_path=db, period_s=5, max_ram_mb=1500)
        assert handle is not None
        await asyncio.sleep(0.3)  # first sample happens immediately
        await handle.stop()

    asyncio.run(scenario())
    ledger = SelfLedger(self_ledger_db_path(db))
    kinds = {r.kind for r in ledger.records()}
    assert SelfEntryKind.VITAL_READING in kinds and ledger.verify().ok


def test_start_vitals_refuses_a_tampered_ledger(tmp_path: Path) -> None:
    import asyncio

    from nexus_ai_agent.agent.self_vitals import start_vitals

    db = str(tmp_path / "n.db")
    boot_self(self_ledger_db_path(db))
    with sqlite3.connect(self_ledger_db_path(db)) as raw:
        raw.execute("UPDATE nexus_self_ledger SET record_json = record_json || ' '")
    assert asyncio.run(start_vitals(enabled=True, db_path=db, period_s=5, max_ram_mb=1500)) is None


def test_start_vitals_measures_the_volume_holding_the_data(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    from nexus_ai_agent.agent import self_vitals

    seen: dict[str, str] = {}
    real = self_vitals.default_probes

    def spy(*, max_ram_mb: int, disk_path: str = "/"):
        seen["disk_path"] = disk_path
        return real(max_ram_mb=max_ram_mb, disk_path=disk_path)

    monkeypatch.setattr(self_vitals, "default_probes", spy)

    async def go() -> None:
        handle = await self_vitals.start_vitals(
            enabled=True, db_path=str(tmp_path / "n.db"), period_s=5, max_ram_mb=1500
        )
        assert handle is not None
        await handle.stop()

    asyncio.run(go())
    assert seen["disk_path"] == str(tmp_path)
