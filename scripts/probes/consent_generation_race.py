#!/usr/bin/env python3
"""Counterexample probe for the consent / extraction / forget invariant (8 cases).

NOT a green regression test and NOT a production database operation.  Run
against an explicitly selected, trusted source tree (--source-root).  It uses
the real AIMemoryEngine and real SQLModel persistence on a temporary SQLite
file, with a two-phase barrier fake LLM, so every interleaving is
deterministic.

Invariant under test (privacy):
    Once ``forget_user`` has completed, nothing may restore the erased
    profile: not a stale in-flight extraction, not a replayed writer, not a
    second concurrent extraction.

Exit codes: 1 = privacy invariant VIOLATED (evidence for the code's owner),
0 = no violation reproduced, 2 = probe itself not verified (controls broken).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
from collections import deque
from pathlib import Path
from types import SimpleNamespace

INVARIANT_CASES = {
    "forget_before_completion",
    "stale_writer_replay",
    "concurrent_extraction_dual_writer",
    "forget_during_cancellation",
    "replayed_request_denied",
    "idempotent_forget",
}
CASE_IDS = {
    "consent_granted_baseline": 1,
    "consent_denied_no_egress": 2,
    "forget_before_completion": 3,
    "forget_during_cancellation": 4,
    "stale_writer_replay": 5,
    "replayed_request_denied": 6,
    "idempotent_forget": 7,
    "concurrent_extraction_dual_writer": 8,
}


class Barrier:
    """Two-phase LLM gate: the provider signals ``started``, then awaits ``release``."""

    def __init__(self, auto_release: bool = False):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if auto_release:
            self.release.set()


class BarrierProvider:
    """Fake LLM.  Each call consumes the OLDEST registered barrier (FIFO)."""

    def __init__(self, payload: str = '{"name": "synthetic-stale-profile"}'):
        self.payload = payload
        self.calls = 0
        self._barriers: deque[Barrier] = deque()

    def new_barrier(self, auto_release: bool = False) -> Barrier:
        barrier = Barrier(auto_release)
        self._barriers.append(barrier)
        return barrier

    async def generate(self, **kwargs):
        self.calls += 1
        barrier = self._barriers.popleft() if self._barriers else Barrier()
        barrier.started.set()
        await barrier.release.wait()
        return self.payload


async def _run_case(module, factory, provider, case: str) -> dict:
    user_id = 91000 + CASE_IDS[case]
    message = "synthetic probe message (never production data)"
    writer = module.AIMemoryEngine(provider, min_egress_seconds=0)
    eraser = module.AIMemoryEngine(provider, min_egress_seconds=0)
    await writer.set_consent(user_id, True)

    async def row_snapshot():
        from sqlmodel import select

        async with factory() as session:
            stmt = select(module.UserMemory).where(module.UserMemory.user_id == user_id)
            return (await session.execute(stmt)).scalar_one_or_none()

    if case == "consent_granted_baseline":
        provider.new_barrier(auto_release=True)
        outcome = await writer.update_from_message(user_id, message)
        row = await row_snapshot()
        return {
            "case": case,
            "kind": "control",
            "request_outcome": str(outcome),
            "profile_persisted": bool(row and row.name),
            "final_consent": row.ai_memory_consent if row else None,
        }

    if case == "consent_denied_no_egress":
        await eraser.set_consent(user_id, False)
        provider.new_barrier()  # would block forever if the gate were consulted
        outcome = await writer.update_from_message(user_id, message)
        row = await row_snapshot()
        return {
            "case": case,
            "kind": "control",
            "request_outcome": str(outcome),
            "profile_persisted": bool(row and row.name),
            "provider_calls": provider.calls,
        }

    if case in {
        "forget_before_completion",
        "replayed_request_denied",
        "stale_writer_replay",
        "forget_during_cancellation",
    }:
        barrier = provider.new_barrier()
        pending = asyncio.create_task(writer.update_from_message(user_id, message))
        await asyncio.wait_for(barrier.started.wait(), timeout=5)
        await asyncio.wait_for(eraser.forget_user(user_id), timeout=5)
        erased = await row_snapshot()
        erased_before = not (erased and erased.name)

        if case == "replayed_request_denied":
            try:
                outcome = await asyncio.wait_for(
                    writer.update_from_message(user_id, message), timeout=3
                )
            except asyncio.TimeoutError:
                outcome = "blocked_on_fake_llm_after_forget"
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            row = await row_snapshot()
            return {
                "case": case,
                "kind": "invariant",
                "request_outcome": str(outcome),
                "erased_before_replay": erased_before,
                "stale_profile_persisted": bool(row and row.name),
                "provider_calls_after_forget": provider.calls - 1,
                "final_consent": row.ai_memory_consent if row else None,
            }

        if case == "forget_during_cancellation":
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            row = await row_snapshot()
            return {
                "case": case,
                "kind": "invariant",
                "erased_before_cancel": erased_before,
                "stale_profile_persisted": bool(row and row.name),
            }

        if case == "stale_writer_replay":
            stale_payload = {"name": "synthetic-stale-profile", "interests": ["stale"]}
            await writer._save_memory(user_id, stale_payload)  # captured-writer replay
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            row = await row_snapshot()
            return {
                "case": case,
                "kind": "invariant",
                "erased_before_replay": erased_before,
                "stale_profile_persisted": bool(row and row.name),
                "final_consent": row.ai_memory_consent if row else None,
            }

        barrier.release.set()  # let the stale extraction finish
        outcome = await asyncio.wait_for(pending, timeout=5)
        row = await row_snapshot()
        return {
            "case": case,
            "kind": "invariant",
            "request_outcome": str(outcome),
            "erased_before_resume": erased_before,
            "stale_profile_persisted": bool(row and row.name),
            "final_consent": row.ai_memory_consent if row else None,
        }

    if case == "idempotent_forget":
        barrier = provider.new_barrier()
        pending = asyncio.create_task(writer.update_from_message(user_id, message))
        await asyncio.wait_for(barrier.started.wait(), timeout=5)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await asyncio.wait_for(eraser.forget_user(user_id), timeout=5)
        once = await row_snapshot()
        await asyncio.wait_for(eraser.forget_user(user_id), timeout=5)
        twice = await row_snapshot()
        await asyncio.wait_for(eraser.forget_user(99777), timeout=5)  # absent user
        absent = await row_snapshot()
        return {
            "case": case,
            "kind": "invariant",
            "erased_after_first": not (once and once.name),
            "erased_after_second": not (twice and twice.name),
            "tombstone_after_second": twice.ai_memory_consent if twice else None,
            "absent_user_tombstone": absent.ai_memory_consent if absent else None,
        }

    # concurrent_extraction_dual_writer
    barrier_a = provider.new_barrier()
    barrier_b = provider.new_barrier()
    writer_b = module.AIMemoryEngine(provider, min_egress_seconds=0)
    pending_a = asyncio.create_task(writer.update_from_message(user_id, message))
    pending_b = asyncio.create_task(writer_b.update_from_message(user_id, message))
    await asyncio.wait_for(barrier_a.started.wait(), timeout=5)
    await asyncio.wait_for(barrier_b.started.wait(), timeout=5)
    await asyncio.wait_for(eraser.forget_user(user_id), timeout=5)
    barrier_a.release.set()
    barrier_b.release.set()
    outcomes = await asyncio.gather(pending_a, pending_b, return_exceptions=True)
    row = await row_snapshot()
    return {
        "case": case,
        "kind": "invariant",
        "request_outcomes": [str(o) for o in outcomes],
        "stale_profile_persisted": bool(row and row.name),
        "final_consent": row.ai_memory_consent if row else None,
    }


async def probe(root: Path) -> dict:
    sys.path.insert(0, str(root / "src"))
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from nexus_ai_agent.features import ai_memory as module
    from nexus_ai_agent.storage.models import UserMemory

    expected = (root / "src/nexus_ai_agent/features/ai_memory.py").resolve()
    if Path(module.__file__).resolve() != expected:
        raise RuntimeError("source origin mismatch")
    if not hasattr(module.AIMemoryEngine, "forget_user"):
        raise RuntimeError("target source has no forget_user")

    original_session, original_settings = module.get_session, module.get_settings
    cases: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="nexus-consent-probe-") as directory:
        engine = create_async_engine(f"sqlite+aiosqlite:///{directory}/probe.db")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(lambda conn: UserMemory.__table__.create(conn))
            factory = async_sessionmaker(engine, expire_on_commit=False)
            module.get_session = factory
            module.get_settings = lambda: SimpleNamespace(
                ai_memory_enabled=True, ai_memory_min_egress_seconds=0
            )
            for case in [
                "consent_granted_baseline",
                "consent_denied_no_egress",
                "forget_before_completion",
                "forget_during_cancellation",
                "stale_writer_replay",
                "replayed_request_denied",
                "idempotent_forget",
                "concurrent_extraction_dual_writer",
            ]:
                provider = BarrierProvider()
                cases.append(await _run_case(module, factory, provider, case))
        finally:
            module.get_session, module.get_settings = original_session, original_settings
            await engine.dispose()

    invariant_cases = [c for c in cases if c["kind"] == "invariant"]
    violations = []
    for c in invariant_cases:
        if c["case"] == "replayed_request_denied":
            if c.get("stale_profile_persisted") or c.get("provider_calls_after_forget", 0) > 0:
                violations.append(c["case"])
        elif c["case"] == "idempotent_forget":
            if not (c.get("erased_after_first") and c.get("erased_after_second")):
                violations.append(c["case"])
        elif c.get("stale_profile_persisted"):
            violations.append(c["case"])
    controls = [c for c in cases if c["kind"] == "control"]
    controls_ok = all(
        (c["case"] == "consent_granted_baseline" and c.get("profile_persisted"))
        or (
            c["case"] == "consent_denied_no_egress"
            and not c.get("profile_persisted")
            and c.get("provider_calls") == 0
        )
        for c in controls
    )
    if not controls_ok:
        outcome = "NOT_VERIFIED"
    elif violations:
        outcome = "FAILURE"
    else:
        outcome = "SUCCESS"
    source = Path(module.__file__)
    return {
        "outcome": outcome,
        "violated_cases": violations,
        "controls_ok": controls_ok,
        "scope": "real_engine_real_temporary_SQLite_two_phase_barrier_fake_LLM",
        "source_file": str(source.relative_to(root)),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "production_verified": False,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        required=True,
        help="trusted checkout/archive whose runtime will execute",
    )
    args = parser.parse_args()
    try:
        result = asyncio.run(probe(args.source_root.resolve()))
    except Exception as exc:  # noqa: BLE001 - probe must report, never crash
        import traceback

        result = {
            "outcome": "NOT_VERIFIED",
            "error_type": type(exc).__name__,
            "error": str(exc)[:200],
            "traceback": traceback.format_exc()[-900:],
        }
    print(json.dumps(result, indent=2))
    return {"SUCCESS": 0, "FAILURE": 1, "NOT_VERIFIED": 2}[result["outcome"]]


if __name__ == "__main__":
    sys.exit(main())
