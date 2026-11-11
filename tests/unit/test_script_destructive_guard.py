"""F-004: every destructive DB script requires an explicit, exact opt-in.

Arena finding F-004: the standalone proof scripts default to a live
``NEXUS_DATABASE_URL`` and then ``DROP TABLE``/``CREATE TABLE``/write rows, but
none of them required the ``NEXUS_PG_TEST_DESTRUCTIVE=1`` opt-in the test
modules enforce. On a machine where ``NEXUS_DATABASE_URL`` points at the real
database, running one of them mutates production with no confirmation.

These tests drive the shared guard (``scripts/_pg_safety.py``) directly and
also assert — by reading the scripts — that each one actually calls it, so a
future script cannot be added without the fence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.maintenance import pg_safety as guard

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"

#: Scripts that DROP/CREATE/write against ``NEXUS_DATABASE_URL``-style URLs.
DESTRUCTIVE_SCRIPTS = (
    SCRIPTS / "p0_benchmark.py",
    SCRIPTS / "dr_proof.py",
    SCRIPTS / "p0_type_fidelity.py",
    SCRIPTS / "p0_evidence.py",
)


@pytest.mark.parametrize("value", [None, "", "0", "false", "False", "no", "off", "yes", "2"])
def test_opt_in_is_required_and_must_be_exactly_one(monkeypatch, value) -> None:
    if value is None:
        monkeypatch.delenv("NEXUS_PG_TEST_DESTRUCTIVE", raising=False)
    else:
        monkeypatch.setenv("NEXUS_PG_TEST_DESTRUCTIVE", value)
    with pytest.raises(SystemExit) as excinfo:
        guard.require_destructive_opt_in("postgresql://u:p@127.0.0.1:5433/nexus", what="t")
    assert excinfo.value.code == 2


def test_opt_in_armed_with_exactly_one(monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_PG_TEST_DESTRUCTIVE", "1")
    guard.require_destructive_opt_in("postgresql://u:p@127.0.0.1:5433/nexus", what="t")


def test_managed_endpoint_is_refused_even_when_armed(monkeypatch, capsys) -> None:
    monkeypatch.setenv("NEXUS_PG_TEST_DESTRUCTIVE", "1")
    with pytest.raises(SystemExit) as excinfo:
        guard.refuse_production_target("postgresql://u:p@ep-x.neon.tech/nexus", what="t")
    assert excinfo.value.code == 2


def test_guard_never_prints_the_credential(monkeypatch, capsys) -> None:
    monkeypatch.setenv("NEXUS_PG_TEST_DESTRUCTIVE", "1")
    guard.require_destructive_opt_in("postgresql://u:SUPERSECRET@127.0.0.1:5433/nexus", what="t")
    assert "SUPERSECRET" not in capsys.readouterr().err


@pytest.mark.parametrize("script", DESTRUCTIVE_SCRIPTS, ids=lambda p: p.name)
def test_each_destructive_script_calls_the_guard(script: Path) -> None:
    """The fence must be wired in, not merely importable."""
    source = script.read_text(encoding="utf-8")
    assert "require_destructive_opt_in(" in source, (
        f"{script.name} performs destructive writes but never calls the opt-in guard"
    )
    assert "refuse_production_target(" in source, (
        f"{script.name} must also refuse a managed/production host"
    )


def test_guard_identity_never_leaks_the_password() -> None:
    assert guard._identity("postgresql://u:SUPERSECRET@h:5432/db") == "h:5432/db"
