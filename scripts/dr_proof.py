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

os.environ["NEXUS_DATABASE_URL"] = "postgresql://postgres:pg@127.0.0.1:5433/nexus"
os.environ["NEXUS_REQUIRE_POSTGRES"] = "1"

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.maintenance.backup import (
    _dump_postgres,
    _verify_postgres_dump,
)
from nexus_ai_agent.maintenance.restore import restore_drill

SRC = "postgresql://postgres:pg@127.0.0.1:5433/nexus"
DST = "postgresql://postgres:pg@127.0.0.1:5433/nexus_restore_target"
SENTINEL = "hello-durability-42"


def psql(url: str, sql: str) -> str:
    out = subprocess.run(["psql", url, "-tAc", sql], check=True, capture_output=True, text=True)
    return out.stdout.strip()


def main() -> int:
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
