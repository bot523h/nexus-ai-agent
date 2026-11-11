"""Destructive-write guard shared by the standalone PostgreSQL proof scripts.

Arena finding F-004: ``scripts/p0_benchmark.py``, ``dr_proof.py``,
``p0_type_fidelity.py`` and ``p0_evidence.py`` each default to a live
``NEXUS_DATABASE_URL`` and then ``DROP TABLE`` / ``CREATE DATABASE`` / write
rows. Run on a developer machine — or any host where ``NEXUS_DATABASE_URL``
points at the real (Neon) database — they mutate production data with no
opt-in at all. The test modules already require ``NEXUS_PG_TEST_DESTRUCTIVE=1``;
the scripts did not, so the two paths disagreed about what "safe" means.

``require_destructive_opt_in`` is that same contract for scripts: only the
exact string ``"1"`` arms it (``"0"``/``"false"`` are truthy strings), the
process refuses to continue otherwise, and it names the database it is about
to write so the operator can see the target before any statement runs.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlsplit

#: The only accepted value. A truthiness check would treat ``"0"`` as enabled.
_ARMED = "1"


def _identity(url: str) -> str:
    """``host:port/db`` — never the credential."""
    parts = urlsplit(url)
    host = parts.hostname or "?"
    port = parts.port or 5432
    return f"{host}:{port}{(parts.path or '')}"


def require_destructive_opt_in(target_url: str, *, what: str) -> None:
    """Abort unless ``NEXUS_PG_TEST_DESTRUCTIVE=1`` authorises *what*.

    Prints the (credential-free) target identity so the operator sees exactly
    which database the run will mutate, then exits non-zero when the opt-in is
    absent or not the exact string ``"1"``.
    """
    armed = os.environ.get("NEXUS_PG_TEST_DESTRUCTIVE") == _ARMED
    sys.stderr.write(
        f"[pg-safety] {what} → target {_identity(target_url)} "
        f"(destructive opt-in {'ARMED' if armed else 'NOT SET'})\n"
    )
    if not armed:
        sys.stderr.write(
            "[pg-safety] refusing to run: this script drops/writes tables and\n"
            "  may be pointed at a production database via NEXUS_DATABASE_URL.\n"
            "  Re-run with NEXUS_PG_TEST_DESTRUCTIVE=1 to authorise it against a\n"
            "  disposable target.\n"
        )
        raise SystemExit(2)


def refuse_production_target(target_url: str, *, what: str) -> None:
    """Refuse an obviously-production host even when the opt-in is armed.

    The opt-in says "I intend a destructive run"; it does not say "this host is
    disposable". A Neon/managed endpoint is never a safe target for a script
    whose whole job is ``DROP TABLE``/``CREATE DATABASE``, so it is refused
    outright — arming the flag must not be enough to aim at production.
    """
    host = (urlsplit(target_url).hostname or "").lower()
    managed_markers = ("neon.tech", "aws.neon", "render.com", "supabase.co", "amazonaws.com")
    if any(marker in host for marker in managed_markers):
        sys.stderr.write(
            f"[pg-safety] refusing {what}: {_identity(target_url)} looks like a "
            "managed/production endpoint. Use a disposable local cluster.\n"
        )
        raise SystemExit(2)
