"""The dependency-audit gate must accept a still-blocked advisory and fail a stale one.

The gate's whole value is that an acceptance *expires on its own*: a documented
`transitive-cap` acceptance is valid only while an installed dependency really
caps the package below the fixed version.  These tests pin that behaviour with
pure functions (no network, no `pip-audit` subprocess), so the anti-vacuity
property is itself a contract.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dependency_audit.py"
ALLOWLIST = ROOT / ".github" / "dependency-audit.json"

_spec = importlib.util.spec_from_file_location("dependency_audit", SCRIPT)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
sys.modules["dependency_audit"] = gate
_spec.loader.exec_module(gate)


def _reqs(*lines: str) -> dict[str, list[str]]:
    return {"litellm": list(lines)}


def _filelock_entry() -> dict:
    return {
        "package": "filelock",
        "reason": "transitive-cap",
        "fixed": "4.2.1",
        "capped_by": ["litellm"],
    }


def test_transitive_cap_holds_when_a_dependency_really_caps_the_package() -> None:
    holds, why = gate._pin_still_holds(
        _filelock_entry(), {"filelock": "3.32.7"}, _reqs("filelock<4.0,>=3.16.1")
    )
    assert holds is True
    assert "litellm" in why


def test_transitive_cap_is_stale_when_no_dependency_caps_any_more() -> None:
    holds, why = gate._pin_still_holds(
        _filelock_entry(), {"filelock": "3.32.7"}, _reqs("filelock>=3.16.1")
    )
    assert holds is False
    assert "bump to 4.2.1" in why


def test_transitive_cap_is_stale_when_the_capping_dependency_is_absent() -> None:
    holds, _ = gate._pin_still_holds(_filelock_entry(), {"filelock": "3.32.7"}, {})
    assert holds is False


def test_no_fix_released_entry_holds() -> None:
    entry = {"package": "chromadb", "reason": "no-fix-released"}
    holds, why = gate._pin_still_holds(entry, {"chromadb": "1.5.9"}, {})
    assert holds is True
    assert "no released version" in why


def test_entry_is_stale_when_the_package_is_not_installed() -> None:
    entry = {"package": "chromadb", "reason": "no-fix-released"}
    holds, why = gate._pin_still_holds(entry, {}, {})
    assert holds is False
    assert "not installed" in why


def test_unknown_reason_is_never_accepted() -> None:
    entry = {"package": "x", "reason": "because-i-said-so"}
    holds, _ = gate._pin_still_holds(entry, {"x": "1.0"}, {})
    assert holds is False


def test_collect_ids_indexes_every_advisory() -> None:
    report = {
        "dependencies": [
            {"name": "filelock", "version": "3.32.7", "vulns": [{"id": "CVE-1"}, {"id": "CVE-2"}]},
            {"name": "chromadb", "version": "1.5.9", "vulns": [{"id": "PYSEC-9"}]},
        ]
    }
    found = gate._collect_ids(report)
    assert set(found) == {"CVE-1", "CVE-2", "PYSEC-9"}
    assert found["PYSEC-9"]["package"] == "chromadb"


def test_an_allowlist_entry_for_a_different_package_is_rejected() -> None:
    # Advisory ids are reused across packages; an entry must name the package it
    # covers, or a same-id advisory in another package would pass silently.
    allowlist = {"CVE-X": {"package": "otherpkg", "reason": "no-fix-released"}}
    ok, message = gate._decide_advisory(
        "CVE-X", {"package": "chromadb", "version": "1.5.9"}, allowlist, {"chromadb": "1.5.9"}, {}
    )
    assert ok is False
    assert "MISMATCH" in message


def test_a_matching_entry_is_accepted() -> None:
    allowlist = {"CVE-X": {"package": "chromadb", "reason": "no-fix-released"}}
    ok, message = gate._decide_advisory(
        "CVE-X", {"package": "chromadb", "version": "1.5.9"}, allowlist, {"chromadb": "1.5.9"}, {}
    )
    assert ok is True
    assert "ACCEPTED" in message


def test_an_unknown_advisory_is_rejected() -> None:
    ok, message = gate._decide_advisory(
        "CVE-NEW", {"package": "chromadb", "version": "1.5.9"}, {}, {"chromadb": "1.5.9"}, {}
    )
    assert ok is False
    assert "UNACCEPTED" in message


def test_the_shipped_allowlist_is_well_formed() -> None:
    data = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    accepted = data["accepted"]
    assert accepted, "an empty allow-list would make the gate vacuously green only when clean"
    for advisory_id, entry in accepted.items():
        assert entry.get("package"), f"{advisory_id} has no package"
        assert entry.get("reason") in {"transitive-cap", "no-fix-released"}, advisory_id
        if entry["reason"] == "transitive-cap":
            assert entry.get("fixed") and entry.get("capped_by"), advisory_id
