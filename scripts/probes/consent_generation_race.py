#!/usr/bin/env python3
"""Counterexample probe, NOT a green regression test or a production DB operation.

Run against an explicitly selected, trusted source archive. Uses actual AIMemoryEngine
and SQLModel persistence, a temporary SQLite file and a barrier-controlled fake LLM.
Exit 1 = privacy invariant violated; 0 = not reproduced; 2 = probe not verified.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


async def probe(root: Path) -> dict:
    sys.path.insert(0, str(root / "src"))
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlmodel import select

    from nexus_ai_agent.features import ai_memory as module
    from nexus_ai_agent.storage.models import UserMemory

    if Path(module.__file__).resolve() != root / "src/nexus_ai_agent/features/ai_memory.py":
        raise RuntimeError("source origin mismatch")

    class BarrierProvider:
        def __init__(self):
            self.started = asyncio.Event()
            self.resume = asyncio.Event()

        async def generate(self, **kwargs):
            self.started.set()
            await self.resume.wait()
            return '{"name": "synthetic-stale-profile"}'

    original_session, original_settings = module.get_session, module.get_settings
    results = []
    with tempfile.TemporaryDirectory(prefix="nexus-consent-probe-") as directory:
        engine = create_async_engine(f"sqlite+aiosqlite:///{directory}/probe.db")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(lambda conn: UserMemory.__table__.create(conn))
            factory = async_sessionmaker(engine, expire_on_commit=False)
            module.get_session = factory
            module.get_settings = lambda: SimpleNamespace(
                ai_memory_enabled=True,
                ai_memory_min_egress_seconds=0,
            )
            for user_id, regrant in [(91001, False), (91002, True)]:
                provider = BarrierProvider()
                writer = module.AIMemoryEngine(provider, min_egress_seconds=0)
                revoker = module.AIMemoryEngine(provider, min_egress_seconds=0)
                await writer.set_consent(user_id, True)
                pending = asyncio.create_task(writer.update_from_message(user_id, "synthetic only"))
                try:
                    await asyncio.wait_for(provider.started.wait(), timeout=5)
                    await asyncio.wait_for(revoker.forget_user(user_id), timeout=5)
                    erased_before_resume = not await revoker.get_context(user_id)
                    if regrant:
                        await revoker.set_consent(user_id, True)
                    provider.resume.set()
                    request_outcome = await asyncio.wait_for(pending, timeout=5)
                    async with factory() as session:
                        row = (
                            await session.execute(
                                select(UserMemory).where(UserMemory.user_id == user_id)
                            )
                        ).scalar_one()
                        resurrected = row.name == "synthetic-stale-profile"
                        final_consent = row.ai_memory_consent
                    results.append(
                        {
                            "case": (
                                "forget_regrant_stale_completion"
                                if regrant
                                else "forget_stale_completion"
                            ),
                            "outcome": "FAILURE" if resurrected else "SUCCESS",
                            "erased_before_resume": erased_before_resume,
                            "stale_profile_persisted": resurrected,
                            "final_consent": final_consent,
                            "request_outcome": request_outcome,
                            "invariant": (
                                "an extraction started before forget cannot restore erased data"
                            ),
                        }
                    )
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
        finally:
            module.get_session, module.get_settings = original_session, original_settings
            await engine.dispose()
    source = Path(module.__file__)
    return {
        "outcome": "FAILURE" if any(r["outcome"] == "FAILURE" for r in results) else "SUCCESS",
        "scope": "two_engine_instances_real_temporary_SQLite_fake_LLM",
        "source_file": str(source.relative_to(root)),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "production_verified": False,
        "cases": results,
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
    except Exception as exc:
        result = {"outcome": "NOT_VERIFIED", "error_type": type(exc).__name__}
    print(json.dumps(result, indent=2))
    return {"SUCCESS": 0, "FAILURE": 1, "NOT_VERIFIED": 2}[result["outcome"]]


if __name__ == "__main__":
    sys.exit(main())
