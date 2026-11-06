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
import subprocess
import sys
from pathlib import Path

import pytest

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
            {
                "name": "filelock",
                "version": "3.32.7",
                "vulns": [{"id": "CVE-1"}, {"id": "CVE-2"}],
            },
            {"name": "chromadb", "version": "1.5.9", "vulns": [{"id": "PYSEC-9"}]},
        ]
    }
    found = gate._collect_ids(report)
    assert set(found) == {("filelock", "CVE-1"), ("filelock", "CVE-2"), ("chromadb", "PYSEC-9")}
    assert found[("chromadb", "PYSEC-9")]["package"] == "chromadb"


def test_collect_ids_keeps_a_shared_advisory_id_for_each_package() -> None:
    # A single advisory id reported against two packages must produce TWO entries;
    # keying by id alone would drop one and let it ship unexamined.
    report = {
        "dependencies": [
            {"name": "package-a", "version": "1.0", "vulns": [{"id": "CVE-X"}]},
            {"name": "package-b", "version": "2.0", "vulns": [{"id": "CVE-X"}]},
        ]
    }
    found = gate._collect_ids(report)
    assert set(found) == {("package-a", "CVE-X"), ("package-b", "CVE-X")}


def test_no_fix_released_expires_when_pip_audit_reports_a_fix() -> None:
    # The whole point of a `no-fix-released` acceptance is that it stops being true
    # the moment a fix ships; pip-audit's own `fix_versions` is the signal.
    entry = {"package": "chromadb", "reason": "no-fix-released", "id": "PYSEC-9"}
    holds, why = gate._pin_still_holds(entry, {"chromadb": "1.5.9"}, {}, ["1.6.0"])
    assert holds is False
    assert "fix is now released" in why


def test_no_fix_released_holds_while_no_fix_is_reported() -> None:
    entry = {"package": "chromadb", "reason": "no-fix-released", "id": "PYSEC-9"}
    holds, _ = gate._pin_still_holds(entry, {"chromadb": "1.5.9"}, {}, [])
    assert holds is True


def test_an_allowlist_entry_for_a_different_package_is_rejected() -> None:
    # Advisory ids are reused across packages; an entry must name the package it
    # covers, or a same-id advisory in another package would pass silently.
    allowlist = {"CVE-X": {"package": "otherpkg", "reason": "no-fix-released"}}
    ok, message = gate._decide_advisory(
        ("chromadb", "CVE-X"),
        {"package": "chromadb", "version": "1.5.9", "fix_versions": []},
        allowlist,
        {"chromadb": "1.5.9"},
        {},
    )
    assert ok is False
    assert "MISMATCH" in message


def test_a_shared_advisory_id_is_examined_for_every_package() -> None:
    # One advisory id reported for two packages must be decided twice: the accepted
    # package passes, and the other package is a MISMATCH failure — never dropped,
    # masked, or conflated with the first.
    allowlist = {"CVE-X": {"package": "package-a", "reason": "no-fix-released"}}
    ok_a, msg_a = gate._decide_advisory(
        ("package-a", "CVE-X"),
        {"package": "package-a", "version": "1.0", "fix_versions": []},
        allowlist,
        {"package-a": "1.0"},
        {},
    )
    ok_b, msg_b = gate._decide_advisory(
        ("package-b", "CVE-X"),
        {"package": "package-b", "version": "2.0", "fix_versions": []},
        allowlist,
        {"package-b": "2.0"},
        {},
    )
    assert ok_a is True and "ACCEPTED" in msg_a
    assert ok_b is False and "MISMATCH" in msg_b


def test_a_matching_entry_is_accepted() -> None:
    allowlist = {"CVE-X": {"package": "chromadb", "reason": "no-fix-released"}}
    ok, message = gate._decide_advisory(
        ("chromadb", "CVE-X"),
        {"package": "chromadb", "version": "1.5.9", "fix_versions": []},
        allowlist,
        {"chromadb": "1.5.9"},
        {},
    )
    assert ok is True
    assert "ACCEPTED" in message


def test_a_matching_entry_goes_stale_when_a_fix_appears() -> None:
    allowlist = {"CVE-X": {"package": "chromadb", "reason": "no-fix-released"}}
    ok, message = gate._decide_advisory(
        ("chromadb", "CVE-X"),
        {"package": "chromadb", "version": "1.5.9", "fix_versions": ["1.6.0"]},
        allowlist,
        {"chromadb": "1.5.9"},
        {},
    )
    assert ok is False
    assert "STALE" in message


def test_an_unknown_advisory_is_rejected() -> None:
    ok, message = gate._decide_advisory(
        ("chromadb", "CVE-NEW"),
        {"package": "chromadb", "version": "1.5.9", "fix_versions": []},
        {},
        {"chromadb": "1.5.9"},
        {},
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


def test_a_marker_guarded_cap_does_not_block_a_platform_it_excludes() -> None:
    # A cap that only applies on Windows must NOT be read as "still blocking" on
    # Linux: the fixed version is genuinely reachable here, so the acceptance is
    # stale and the gate must fail rather than hide the reachable upgrade.
    holds, why = gate._pin_still_holds(
        _filelock_entry(),
        {"filelock": "3.32.7"},
        _reqs("filelock<4.0,>=3.16.1; sys_platform == 'win32'"),
    )
    assert holds is False
    assert "in this environment" in why


def test_a_marker_guarded_cap_still_blocks_the_platform_it_selects() -> None:
    # The same cap evaluated where the marker selects (Linux) really does block.
    holds, why = gate._pin_still_holds(
        _filelock_entry(),
        {"filelock": "3.32.7"},
        _reqs("filelock<4.0,>=3.16.1; sys_platform == 'linux'"),
    )
    assert holds is True
    assert "blocked by" in why


def test_an_unmarked_cap_is_unaffected_by_the_marker_check() -> None:
    holds, _ = gate._pin_still_holds(
        _filelock_entry(), {"filelock": "3.32.7"}, _reqs("filelock<4.0,>=3.16.1")
    )
    assert holds is True


def test_a_cap_guarded_by_a_non_matching_extra_marker_is_dropped() -> None:
    # A cap guarded by an extra that is not installed does not apply here, so the
    # entry is stale — the gate must not accept on the strength of a cap that does
    # not constrain this environment.
    holds, _ = gate._pin_still_holds(
        _filelock_entry(),
        {"filelock": "3.32.7"},
        _reqs("filelock<4.0; extra == 'not-installed-extra'"),
    )
    assert holds is False


def test_load_allowlist_fails_closed_on_every_malformed_shape(tmp_path: Path) -> None:
    bad = {
        "missing": None,
        "not-json": "{ nope",
        "not-object": "[1, 2, 3]",
        "no-accepted": '{"other": 1}',
        "entry-not-object": '{"accepted": {"X": "string"}}',
        "no-package": '{"accepted": {"X": {"reason": "no-fix-released"}}}',
        "bad-reason": '{"accepted": {"X": {"package": "p", "reason": "trust-me"}}}',
        "cap-missing-fields": '{"accepted": {"X": {"package": "p", "reason": "transitive-cap"}}}',
    }
    for name, payload in bad.items():
        path = tmp_path / f"{name}.json"
        if payload is not None:
            path.write_text(payload, encoding="utf-8")
        with pytest.raises(SystemExit):
            gate._load_allowlist(path)


def test_load_allowlist_accepts_a_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "ok.json"
    path.write_text(
        '{"accepted": {"CVE-X": {"package": "p", "reason": "no-fix-released"}}}',
        encoding="utf-8",
    )
    assert gate._load_allowlist(path) == {"CVE-X": {"package": "p", "reason": "no-fix-released"}}


def test_parse_pip_audit_refuses_empty_truncated_or_wrong_shape() -> None:
    for payload in ("", "   ", "{ not json", "[]", '{"foo": 1}', '{"dependencies": "x"}'):
        with pytest.raises(SystemExit):
            gate._parse_pip_audit(payload)


def test_parse_pip_audit_accepts_a_clean_report() -> None:
    assert gate._parse_pip_audit('{"dependencies": []}') == {"dependencies": []}


def test_pip_audit_uses_a_fixed_argv_with_no_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    # Prove the audit invocation cannot be turned into shell injection: argv is a
    # constant list, ``shell`` is never enabled, and no input is interpolated.
    seen: dict[str, object] = {}

    def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, '{"dependencies": []}', "")

    monkeypatch.setattr(gate.subprocess, "run", _fake_run)
    gate._run_pip_audit_stdout()
    assert isinstance(seen["argv"], list)
    assert seen["argv"][0] == sys.executable
    assert seen["argv"][1:3] == ["-m", "pip_audit"]
    assert "shell" not in seen["kwargs"]  # shell is never enabled
