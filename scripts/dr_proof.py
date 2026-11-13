"""Independent end-to-end DR proof on a live PostgreSQL 17 cluster.

Proves: real dump -> structural verify -> real restore into a fresh DB ->
the seeded row is byte-identical on the far side. Everything is the shipping
code path (no mocks): _dump_postgres, _verify_postgres_dump, restore_drill.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from nexus_ai_agent.maintenance.pg_safety import (  # noqa: E402
    refuse_production_target,
    require_destructive_opt_in,
    require_pg_client,
)

os.environ.setdefault("NEXUS_DATABASE_URL", "postgresql://postgres:pg@127.0.0.1:5433/nexus")
os.environ.setdefault("NEXUS_REQUIRE_POSTGRES", "1")

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.maintenance.backup import (
    _dump_postgres,
    _verify_postgres_dump,
)
from nexus_ai_agent.maintenance.restore import restore_drill

# F-009.6: respect the configured test environment. The previous version
# *overwrote* NEXUS_DATABASE_URL and pinned the source/target to a hard-coded
# 127.0.0.1:5433 pair, so the script could not be run against the CI service
# container (localhost:5432) or any other disposable cluster. The default is
# now the local dev cluster; an operator's environment wins.
SRC = os.environ.get("NEXUS_DATABASE_URL", "postgresql://postgres:pg@127.0.0.1:5433/nexus")
DST = os.environ.get(
    "NEXUS_DR_TARGET", "postgresql://postgres:pg@127.0.0.1:5433/nexus_restore_target"
)
SENTINEL = "hello-durability-42"


def psql(url: str, sql: str) -> str:
    out = subprocess.run(["psql", url, "-tAc", sql], check=True, capture_output=True, text=True)
    return out.stdout.strip()


def main() -> int:
    # F-004: this script drops and rewrites tables on SRC and DST. It may be
    # aimed at production through NEXUS_DATABASE_URL, so require the explicit
    # opt-in and refuse a managed/production host even then.
    require_destructive_opt_in(SRC, what="dr_proof source (DROP/CREATE/INSERT)")
    require_destructive_opt_in(DST, what="dr_proof target (DROP + restore)")
    refuse_production_target(SRC, what="dr_proof source")
    refuse_production_target(DST, what="dr_proof target")
    require_pg_client(what="dr_proof")

    # 1. Seed a uniquely identifiable row in the source database.
    psql(SRC, "DROP TABLE IF EXISTS dr_proof")
    psql(SRC, "CREATE TABLE dr_proof(v text)")
    psql(SRC, f"INSERT INTO dr_proof VALUES ('{SENTINEL}')")
    assert psql(SRC, "SELECT v FROM dr_proof") == SENTINEL

    # 2. Real pg_dump through the shipping function.
    with tempfile.TemporaryDirectory(prefix="dr_proof_") as tmp:
        art = Path(tmp) / "nexus-pg.sql"
        _dump_postgres(SRC, art)
        size = art.stat().st_size
        sha = hashlib.sha256(art.read_bytes()).hexdigest()
        print(f"[dump] artifact={art.name} bytes={size} sha256={sha}")

        # 3. Structural verification must accept a genuine PG17 dump.
        v = _verify_postgres_dump(art)
        print(f"[verify] {v}")

        # 4. Wipe the target and restore the artifact into it for real.
        psql(DST, "DROP TABLE IF EXISTS dr_proof")
        report = restore_drill(
            settings=get_settings(),
            from_local=art,
            target_url=DST,
            apply=True,
            force=True,
        )
        print(f"[restore] ok={report.ok} engine={report.engine} chain={report.chain}")

        # 5. The far side must carry the exact seeded row.
        got = psql(DST, "SELECT v FROM dr_proof")
        print(f"[check] restored row = {got!r}")
        assert got == SENTINEL, f"restored row mismatch: {got!r} != {SENTINEL!r}"

    print("DR-PROOF-OK: dump -> verify -> restore -> row preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
