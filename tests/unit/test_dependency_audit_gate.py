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
        assert entry.get("reason") in {
            "transitive-cap",
            "no-fix-released",
            "fix-requires-newer-python",
        }, advisory_id
        if entry["reason"] == "transitive-cap":
            assert entry.get("fixed") and entry.get("capped_by"), advisory_id
        if entry["reason"] == "fix-requires-newer-python":
            assert entry.get("fixed"), advisory_id
            assert entry.get("requires_python"), advisory_id
            assert entry.get("declared_floor"), advisory_id


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


# ── `fix-requires-newer-python`: a fix exists but no build of it runs here ────


def _newer_python_entry(**overrides: object) -> dict:
    entry = {
        "package": "soupsieve",
        "reason": "fix-requires-newer-python",
        "fixed": "4.1.1",
        "requires_python": ">=3.11.5",
        "declared_floor": "soupsieve>=4.1.1",
    }
    entry.update(overrides)
    return entry


def _versions(package: str, version: str) -> dict[str, str]:
    return {package: version}


def _lookup(mapping: dict[str, str | None]):
    def _inner(_package: str, version: str) -> str | None:
        return mapping.get(version, ">=3.11.5")

    return _inner


def test_a_newer_python_fix_holds_while_no_build_runs_on_this_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "_declared_floor_problem", lambda _floor, *, probe: None)
    holds, why = gate._pin_still_holds(
        _newer_python_entry(),
        _versions("soupsieve", "2.10"),
        {},
        ["3.0.2", "4.0.0"],
        interpreter="3.11.2",
        requires_python_of=_lookup({}),
    )
    assert holds, why
    assert "3.11.2" in why and "soupsieve>=4.1.1" in why


def test_the_newer_python_acceptance_expires_when_this_interpreter_could_run_the_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "_declared_floor_problem", lambda _floor, *, probe: None)
    holds, why = gate._pin_still_holds(
        _newer_python_entry(),
        _versions("soupsieve", "2.10"),
        {},
        ["3.0.2", "4.0.0"],
        interpreter="3.12.1",
        requires_python_of=_lookup({}),
    )
    assert not holds
    assert "satisfies" in why


def test_the_acceptance_expires_when_any_published_fix_is_installable_here(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3.0.2/4.0.0 declare >=3.11.5, but a later release could relax it."""
    monkeypatch.setattr(gate, "_declared_floor_problem", lambda _floor, *, probe: None)
    holds, why = gate._pin_still_holds(
        _newer_python_entry(),
        _versions("soupsieve", "2.10"),
        {},
        ["3.0.2", "4.0.0"],
        interpreter="3.11.2",
        requires_python_of=_lookup({"3.0.2": ">=3.9"}),
    )
    assert not holds
    assert "installable on this interpreter" in why


def test_the_acceptance_expires_when_pypi_metadata_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "_declared_floor_problem", lambda _floor, *, probe: None)
    holds, why = gate._pin_still_holds(
        _newer_python_entry(),
        _versions("soupsieve", "2.10"),
        {},
        ["4.0.0"],
        interpreter="3.11.2",
        requires_python_of=lambda _p, _v: None,
    )
    assert not holds
    assert "unverified" in why


def test_the_acceptance_expires_when_the_floor_is_no_longer_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gate, "_declared_floor_problem", lambda _floor, *, probe: "not declared (test)"
    )
    holds, why = gate._pin_still_holds(
        _newer_python_entry(),
        _versions("soupsieve", "2.10"),
        {},
        ["4.0.0"],
        interpreter="3.11.2",
        requires_python_of=_lookup({}),
    )
    assert not holds
    assert "not enforced where the fix is installable" in why


def test_the_acceptance_expires_when_the_advisory_names_a_newer_fix_than_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "_declared_floor_problem", lambda _floor, *, probe: None)
    holds, why = gate._pin_still_holds(
        _newer_python_entry(),
        _versions("soupsieve", "2.10"),
        {},
        ["5.0.0"],
        interpreter="3.11.2",
        requires_python_of=_lookup({}),
    )
    assert not holds
    assert "first fixed in" in why


def test_the_acceptance_refuses_to_badge_an_unspecified_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "_declared_floor_problem", lambda _floor, *, probe: None)
    holds, why = gate._pin_still_holds(
        _newer_python_entry(requires_python=None),
        _versions("soupsieve", "2.10"),
        {},
        ["4.0.0"],
        interpreter="3.11.2",
        requires_python_of=_lookup({}),
    )
    assert not holds
    assert "requires_python" in why


def test_the_shipped_floor_really_is_declared_for_compliant_interpreters() -> None:
    """The acceptance above is only honest while pyproject carries the floor.

    "Carries it" means the marker is *true* on the interpreters that can install
    the fix — not that the substring appears in the file.
    """
    for probe in ("3.11.5", "3.11.9", "3.12.0"):
        assert gate._declared_floor_is_present("soupsieve>=4.1.1", probe=probe), probe
    assert not gate._declared_floor_is_present("soupsieve>=4.1.1", probe="3.10.14")


# ── F1: the floor's *marker* is part of the claim, not its text ──────────────
#
# The shipped floor read `soupsieve>=4.1.1; python_version >= '3.11.5'`. PEP 508
# defines `python_version` as major.minor, so the comparison was `"3.11" >=
# "3.11.5"`, i.e. False on every 3.11 interpreter: the floor never applied. A
# substring check called that "declared". These tests pin the semantics.


def _marker(requirement: str) -> object:
    from packaging.requirements import Requirement

    return Requirement(requirement).marker


def _evaluates(requirement: str, full_version: str) -> bool:
    from packaging.markers import default_environment

    env = {key: str(value) for key, value in default_environment().items()}
    env["python_full_version"] = full_version
    env["python_version"] = ".".join(full_version.split(".")[:2])
    marker = _marker(requirement)
    assert marker is not None
    return bool(marker.evaluate(env))


def _shipped_floor() -> str:
    """The soupsieve requirement exactly as `pyproject.toml` declares it."""

    for req in gate._pyproject_requirements():
        if req.name.lower() == "soupsieve":
            return str(req)
    raise AssertionError("pyproject.toml declares no soupsieve floor")


def test_the_shipped_floor_applies_exactly_where_the_fix_is_installable() -> None:
    """The reviewer's matrix: 3.11.5 / 3.11.9 / 3.12.0 / 3.12.3 apply; 3.10.14 / 3.11.4 do not."""
    from packaging.markers import Marker

    requirement = _shipped_floor()
    assert "python_full_version" in requirement
    marker_expr = requirement.split(";", 1)[1].strip()
    marker = Marker(marker_expr)
    for full_version in ("3.11.5", "3.11.9", "3.12.0", "3.12.3"):
        assert _evaluates(requirement, full_version), full_version
        assert marker.evaluate({"python_full_version": full_version}), full_version
    for full_version in ("3.10.14", "3.11.4"):
        assert not _evaluates(requirement, full_version), full_version
        assert not marker.evaluate({"python_full_version": full_version}), full_version
    # And the same through the gate's own parser, which is what actually runs.
    for probe in ("3.11.5", "3.11.9", "3.12.0", "3.12.3"):
        assert gate._declared_floor_problem("soupsieve>=4.1.1", probe=probe) is None, probe
    for probe in ("3.10.14", "3.11.4"):
        problem = gate._declared_floor_problem("soupsieve>=4.1.1", probe=probe)
        assert problem is not None and "does not ask for soupsieve" in problem


def test_a_major_dot_minor_marker_with_a_patch_level_never_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact bug: `python_version >= '3.11.5'` is false even at 3.11.9."""
    from packaging.requirements import Requirement

    buggy = Requirement("soupsieve>=4.1.1; python_version >= '3.11.5'")
    assert not _evaluates(str(buggy), "3.11.9")

    monkeypatch.setattr(gate, "_pyproject_requirements", lambda *a, **k: [buggy])
    problem = gate._declared_floor_problem("soupsieve>=4.1.1", probe="3.11.5")
    assert problem is not None
    assert "is false at python_full_version 3.11.5" in problem


def test_a_floor_whose_marker_never_applies_is_a_stale_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: the typo would have turned the acceptance STALE, not accepted."""
    from packaging.requirements import Requirement

    monkeypatch.setattr(
        gate,
        "_pyproject_requirements",
        lambda *a, **k: [Requirement("soupsieve>=4.1.1; python_version >= '3.11.5'")],
    )
    holds, why = gate._pin_still_holds(
        _newer_python_entry(),
        _versions("soupsieve", "2.10"),
        {},
        ["3.0.2", "4.0.0"],
        interpreter="3.11.2",
        requires_python_of=_lookup({}),
    )
    assert not holds
    assert "not enforced where the fix is installable" in why


def test_the_lowest_installable_interpreter_is_read_from_requires_python() -> None:
    assert gate._lowest_installable(">=3.11.5") == "3.11.5"
    assert gate._lowest_installable(">=3.9,>=3.12.1") == "3.12.1"
    assert gate._lowest_installable(">=3") == "3"
    assert gate._lowest_installable("not a specifier") is None


# ── F1b: one package floor covers an unlisted advisory it can resolve ────────


def _floor_entry(**overrides: object) -> dict:
    entry = {
        "reason": "fix-requires-newer-python",
        "fixed": "4.1.1",
        "requires_python": ">=3.11.5",
        "declared_floor": "soupsieve>=4.1.1",
    }
    entry.update(overrides)
    return entry


def _advisory(fix_versions: list[str], package: str = "soupsieve") -> dict:
    return {"package": package, "version": "2.10", "fix_versions": list(fix_versions)}


def test_a_fourth_advisory_for_the_same_package_is_covered_by_the_floor() -> None:
    """CVE-2026-126192 is the ID the audit asked about: covered, not accepted blindly.

    The interpreter is pinned to one that cannot install 4.1.1 (3.11.2, the same
    environment the acceptances were written for). Without pinning it, this test
    passed on 3.11.2 and failed on 3.12 — where the acceptance *correctly*
    expires — which is what the CI run on `a7c7283` caught.
    """
    ok, message = gate._decide_advisory(
        ("soupsieve", "CVE-2026-126192"),
        _advisory(["3.0.2"]),
        {},  # no per-advisory entry at all
        _versions("soupsieve", "2.10"),
        {},
        {"soupsieve": _floor_entry()},
        interpreter="3.11.2",
    )
    assert ok, message
    assert "covered by the package floor" in message and "4.1.1" in message


def test_the_package_floor_expires_on_an_interpreter_that_can_install_the_fix() -> None:
    """The same floor, on an interpreter the fix supports: STALE, not accepted."""
    ok, message = gate._decide_advisory(
        ("soupsieve", "CVE-2026-126192"),
        _advisory(["3.0.2"]),
        {},
        _versions("soupsieve", "2.10"),
        {},
        {"soupsieve": _floor_entry()},
        interpreter="3.12.16",
    )
    assert not ok
    assert "STALE" in message and "3.12.16" in message


def test_the_package_floor_refuses_an_advisory_fixed_past_it() -> None:
    """A 5.0 fix is not resolved by a 4.1.1 floor — that must block, not pass."""
    ok, message = gate._decide_advisory(
        ("soupsieve", "CVE-2099-000001"),
        _advisory(["5.0.0"]),
        {},
        _versions("soupsieve", "2.10"),
        {},
        {"soupsieve": _floor_entry()},
        interpreter="3.11.2",
    )
    assert not ok
    assert "does not reach" in message


def test_the_package_floor_refuses_an_advisory_with_no_published_fix() -> None:
    ok, message = gate._decide_advisory(
        ("soupsieve", "CVE-2099-000002"),
        _advisory([]),
        {},
        _versions("soupsieve", "2.10"),
        {},
        {"soupsieve": _floor_entry()},
        interpreter="3.11.2",
    )
    assert not ok
    assert "names no fix" in message


def test_the_package_floor_expires_with_the_declared_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gate, "_declared_floor_problem", lambda _f, *, probe: "the floor is gone (test)"
    )
    ok, message = gate._decide_advisory(
        ("soupsieve", "CVE-2026-126192"),
        _advisory(["3.0.2"]),
        {},
        _versions("soupsieve", "2.10"),
        {},
        {"soupsieve": _floor_entry()},
        interpreter="3.11.2",
    )
    assert not ok
    assert "STALE" in message


def test_a_package_floor_does_not_cover_a_different_package() -> None:
    ok, message = gate._decide_advisory(
        ("otherpkg", "CVE-2026-126192"),
        _advisory(["1.0.1"], package="otherpkg"),
        {},
        {"otherpkg": "1.0"},
        {},
        {"soupsieve": _floor_entry()},
        interpreter="3.11.2",
    )
    assert not ok and "UNACCEPTED" in message


def test_load_package_floors_fails_closed_on_malformed_shapes(tmp_path: Path) -> None:
    good = {"package_floors": {"soupsieve": _floor_entry()}}
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps(good))
    assert gate._load_package_floors(path) == good["package_floors"]

    for broken in (
        {"package_floors": []},
        {"package_floors": {"soupsieve": "nope"}},
        {"package_floors": {"soupsieve": _floor_entry(reason="no-fix-released")}},
        {"package_floors": {"soupsieve": _floor_entry(fixed="")}},
    ):
        path.write_text(json.dumps(broken))
        with pytest.raises(SystemExit):
            gate._load_package_floors(path)

    path.write_text(json.dumps({"accepted": {}}))
    assert gate._load_package_floors(path) == {}


def test_the_shipped_allowlist_declares_the_soupsieve_floor() -> None:
    floors = gate._load_package_floors(ALLOWLIST)
    assert "soupsieve" in floors
    assert floors["soupsieve"]["declared_floor"] == "soupsieve>=4.1.1"
    assert floors["soupsieve"]["fixed"] == "4.1.1"


def test_the_toml_reader_exists_on_every_supported_interpreter() -> None:
    """`requires-python = ">=3.10"` and CI runs a 3.10 leg: the reader must too.

    `tomllib` is 3.11+, and importing it unconditionally broke the 3.10 parity
    and continuum legs (a collection error in this very module). The fallback is
    declared in the `dev` extra instead of borrowed from deptry/pip-audit.
    """
    assert hasattr(gate, "tomllib")
    if sys.version_info < (3, 11):
        assert gate.tomllib.__name__ == "tomli"
    else:
        assert gate.tomllib.__name__ == "tomllib"
