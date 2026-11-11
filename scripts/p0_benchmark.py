"""Benchmark backup + restore on a live PostgreSQL cluster (mission §14 item 14).

Seeds a realistic dataset, then measures wall-clock dump, artifact size, restore
apply, and post-restore re-verification. Results are written as JSON so the
numbers are reproducible evidence, not prose.

Usage: python3 scripts/p0_benchmark.py [--rows 20000] [--out docs/evidence/p0]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = os.environ.get("NEXUS_DATABASE_URL", "postgresql://postgres:pg@127.0.0.1:5433/nexus")
DST = os.environ.get(
    "NEXUS_BENCH_TARGET", "postgresql://postgres:pg@127.0.0.1:5433/nexus_restore_target"
)


def psql(url: str, sql: str) -> str:
    return subprocess.run(
        ["psql", url, "-tAc", sql], capture_output=True, text=True, check=True
    ).stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=20000)
    ap.add_argument("--out", default="docs/evidence/p0")
    args = ap.parse_args()

    pg_dump = shutil.which("pg_dump")
    psql_bin = shutil.which("psql")
    if not pg_dump or not psql_bin:
        print("pg_dump/psql not on PATH — benchmark needs postgresql-client")
        return 2

    # Realistic seed: a wide-ish table plus a secondary with text payloads.
    psql(SRC, "DROP TABLE IF EXISTS bench_rows")
    psql(SRC, "CREATE TABLE bench_rows(id serial primary key, name text, body text)")
    psql(
        SRC,
        f"INSERT INTO bench_rows(name, body) SELECT 'row-' || g, "
        f"repeat(md5(g::text), 8) FROM generate_series(1, {args.rows}) g",
    )
    rows = psql(SRC, "SELECT count(*) FROM bench_rows")

    with tempfile.TemporaryDirectory(prefix="p0_bench_") as tmp:
        art = Path(tmp) / "bench.sql"

        t0 = time.perf_counter()
        subprocess.run(
            [pg_dump, "--no-owner", "--clean", "--if-exists", "--file", str(art), SRC],
            check=True,
            capture_output=True,
        )
        dump_s = time.perf_counter() - t0
        size = art.stat().st_size

        psql(DST, "DROP TABLE IF EXISTS bench_rows")
        t0 = time.perf_counter()
        subprocess.run(
            [psql_bin, "-v", "ON_ERROR_STOP=1", "--dbname", DST, "--file", str(art)],
            check=True,
            capture_output=True,
        )
        restore_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        restored = psql(DST, "SELECT count(*) FROM bench_rows")
        verify_s = time.perf_counter() - t0

    result = {
        "schema": "nexus-p0-benchmark/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "rows": int(rows),
        "artifact_bytes": size,
        "dump_seconds": round(dump_s, 3),
        "restore_seconds": round(restore_s, 3),
        "verify_seconds": round(verify_s, 3),
        "restored_rows": int(restored),
        "row_count_matches": int(rows) == int(restored),
        "throughput_rows_per_s_restore": round(int(rows) / restore_s, 1) if restore_s else None,
    }
    out_dir = REPO / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "benchmark.json"
    dest.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    print(f"wrote {dest.relative_to(REPO)}")
    return 0 if result["row_count_matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
