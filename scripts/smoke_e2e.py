#!/usr/bin/env python3
"""E2E smoke — wave-4 step9 (stub for now, green without compose).

When the packaging MR (#33) lands and ``docker-compose.yml`` gains a slim
target, this script will:

* build the slim image,
* bring up ``compose``,
* probe ``GET /healthz``,
* drive one fake Telegram flow (Bot API stub).

For now (pre-#33) it is a *contract* probe: offline checks that pass without
Docker, so the bench/ops runbook remain green.  The compose half is
sequenced post-#33 per ``.agents/board.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def offline_checks() -> dict[str, bool]:
    """Offline manifest/contract checks (no Docker, no network)."""
    checks: dict[str, bool] = {}
    # 1. At least one pack manifest must exist (slideshow)
    checks["pack_manifest_exists"] = any(
        Path(f"src/nexus_ai_agent/creative/packs/{p}/pack.manifest.json").exists()
        for p in ("slideshow", "delivery")
    )
    # 2. Creative surface mapper must be importable
    try:
        from nexus_ai_agent.bot.creative_surface import CreativeSurfaceMapper

        checks["creative_surface_importable"] = True
        # 3. Mapper must enforce the 30 s limit
        m = CreativeSurfaceMapper()
        from nexus_ai_agent.bot.creative_surface import CreativeRequest

        bad = m.map(CreativeRequest("edit", "trim", (), "fid", 99.0))
        from nexus_ai_agent.bot.creative_surface import CreativeFailure

        checks["creative_limit_enforced"] = isinstance(bad, CreativeFailure)
    except Exception as exc:  # pragma: no cover
        checks["creative_surface_importable"] = False
        checks["creative_limit_enforced"] = False
        checks["error"] = False  # type: ignore[assignment]
        print(f"offline check error: {exc}", file=sys.stderr)
    return checks


def main() -> int:
    p = argparse.ArgumentParser(description="E2E smoke (offline contract + live probe)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--offline-only", action="store_true", help="skip compose/healthz (pre-#33)")
    args = p.parse_args()

    results = offline_checks()
    ok = all(v is True for k, v in results.items() if k != "error")

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for k, v in results.items():
            print(f"{'✅' if v else '❌'} {k}")

    if not ok:
        print("offline contract failed", file=sys.stderr)
        return 1
    if args.offline_only:
        print("offline-only smoke passed")
        return 0
    # Live probe would go here post-#33 (compose + /healthz)
    print("E2E smoke: offline contract passed (compose half deferred post-#33)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
