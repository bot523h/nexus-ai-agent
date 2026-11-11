"""Type-fidelity probe: dump -> restore must preserve exotic column values.

Run on a live cluster. Not part of the suite (needs PG); a manual evidence tool.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from nexus_ai_agent.maintenance.pg_safety import (  # noqa: E402
    refuse_production_target,
    require_destructive_opt_in,
)

os.environ.setdefault("NEXUS_DATABASE_URL", "postgresql://postgres:pg@127.0.0.1:5433/nexus")

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.maintenance.backup import _dump_postgres
from nexus_ai_agent.maintenance.restore import restore_drill

SRC = "postgresql://postgres:pg@127.0.0.1:5433/nexus"
DST = "postgresql://postgres:pg@127.0.0.1:5433/nexus_restore_target"

DDL = (
    "CREATE TABLE fidelity("
    "id bigint primary key, big int, n numeric(20,4), b boolean, "
    "ts timestamptz, d date, arr int[], j jsonb, txt text, byt bytea)"
)


def psql(url: str, sql: str) -> str:
    proc = subprocess.run(["psql", url, "-tAc", sql], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def main() -> int:
    # F-004: DROP/CREATE + restore against live databases — require the opt-in
    # and refuse a managed/production host.
    require_destructive_opt_in(SRC, what="p0_type_fidelity source (DROP/CREATE/INSERT)")
    require_destructive_opt_in(DST, what="p0_type_fidelity target (DROP + restore)")
    refuse_production_target(SRC, what="p0_type_fidelity source")
    refuse_production_target(DST, what="p0_type_fidelity target")

    psql(SRC, "DROP TABLE IF EXISTS fidelity")
    psql(SRC, DDL)
    insert = (
        "INSERT INTO fidelity VALUES ("
        "9223372036854775807, -2147483648, 1234567890123456.7891, true, "
        "'2026-11-09T08:51:00.123456+00:00', '2026-11-09', "
        "'{1,2,3}', '{\"k\":\"v\"}', E'multi\\nline\\ttext', '\\xdeadbeef')"
    )
    psql(SRC, insert)
    src_row = psql(SRC, "SELECT row_to_json(f)::text FROM fidelity f")

    with tempfile.TemporaryDirectory() as d:
        art = Path(d) / "fid.sql"
        _dump_postgres(SRC, art)
        psql(DST, "DROP TABLE IF EXISTS fidelity")
        restore_drill(
            settings=get_settings(), from_local=art, target_url=DST, apply=True, force=True
        )
        dst_row = psql(DST, "SELECT row_to_json(f)::text FROM fidelity f")

    print("SRC:", src_row)
    print("DST:", dst_row)
    ok = src_row == dst_row
    print("TYPES ROUND-TRIP EXACT:", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
