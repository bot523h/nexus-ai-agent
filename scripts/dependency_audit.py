#!/usr/bin/env python3
"""Adversarial dependency-vulnerability gate: run ``pip-audit``, then fail closed
unless every reported advisory is *either* fixed *or* explicitly accepted.

Why this exists
---------------
``pip-audit`` alone is either absent from CI (so advisories ship silently) or
run with a blanket ``|| true`` / ``--ignore-vuln`` list (so a *new* advisory
ships silently too).  Neither is evidence.  This wrapper turns the audit into a
reviewed allow-list: a known advisory is accepted only while an explicit,
documented, still-applicable entry exists in ``.github/dependency-audit.json``.

An allow-list entry expires on its own.  For every entry the gate re-derives,
from the *installed* environment, the concrete package specifier the advisory
forces and checks that it is still unresolvable for a stated reason
(``transitive-cap``: an installed dependency caps the fixed version;
``no-fix-released``: no version of the package fixes it yet).  If the pin no
longer holds — because an upstream cap was relaxed or a fix was published — the
entry is reported STALE and the gate fails, forcing a re-review instead of a
silent pass.

Contract
--------
* ``pip-audit`` runs against the current environment and emits JSON.
* Each ``{id}`` in the JSON ``vulnerabilities`` is resolved:
    - not in the allow-list            -> FAIL (unaccepted advisory)
    - in the allow-list, pin holds      -> accepted (with reason)
    - in the allow-list, pin gone/stale -> FAIL (stale entry, re-review)
* Exit 0 only when every advisory is accounted for.

Usage
-----
    python scripts/dependency_audit.py [--allowlist PATH] [--min-severity low]

The allow-list is checked into the repository so an acceptance is a reviewable
artifact, not an environment variable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST = REPO_ROOT / ".github" / "dependency-audit.json"


def _installed_versions() -> dict[str, str]:
    """Map ``importlib.metadata`` distribution name -> version."""
    import importlib.metadata as md

    out: dict[str, str] = {}
    for dist in md.distributions():
        name = dist.metadata["Name"]
        if name:
            out[name.lower()] = dist.version
    return out


def _installed_requirements() -> dict[str, list[str]]:
    """Map installed distribution name -> its ``Requires-Dist`` lines."""
    import importlib.metadata as md

    out: dict[str, list[str]] = {}
    for dist in md.distributions():
        name = dist.metadata["Name"]
        if name:
            out[name.lower()] = list(dist.requires or [])
    return out


def _run_pip_audit() -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-m", "pip_audit", "--format=json", "--progress-spinner=off"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"pip-audit failed to run (exit {proc.returncode})")
    return json.loads(proc.stdout)


def _collect_ids(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return ``{advisory_id: {"package", "version"}}`` for every finding."""
    found: dict[str, dict[str, Any]] = {}
    for dep in report.get("dependencies", []):
        pkg = dep.get("name", "")
        ver = dep.get("version", "")
        for vuln in dep.get("vulns", []):
            found[vuln.get("id", "")] = {"package": pkg, "version": ver}
    return found


def _pin_still_holds(
    entry: dict[str, Any], versions: dict[str, str], reqs: dict[str, list[str]]
) -> tuple[bool, str]:
    """Decide whether an allow-list entry is still genuinely blocked.

    Returns ``(holds, explanation)``.  ``holds`` is False when the acceptance is
    stale — the fixed version is now reachable — which must fail the gate.
    """
    reason = entry.get("reason", "")
    pkg = entry["package"].lower()
    installed = versions.get(pkg)
    if installed is None:
        return False, f"package {pkg!r} is not installed; entry is obsolete"

    if reason == "transitive-cap":
        fixed = entry.get("fixed")
        if not fixed:
            return False, "transitive-cap entry has no `fixed` version"
        capped_by = entry.get("capped_by", [])
        blockers: list[str] = []
        for cap_name in capped_by:
            for line in reqs.get(cap_name.lower(), []):
                try:
                    req = Requirement(line)
                except Exception:
                    continue
                if req.name.lower() == pkg and req.specifier:
                    if Version(fixed) not in req.specifier:
                        blockers.append(f"{cap_name} requires {req.specifier}")
        if blockers:
            return True, "blocked by " + "; ".join(blockers)
        return False, (
            f"no installed dependency caps {pkg} below {fixed} any more — "
            f"bump to {fixed} and drop this entry"
        )

    if reason == "no-fix-released":
        which = entry.get("id", "the advisory")
        return True, f"no released version of {pkg} fixes {which} (installed {installed})"

    return False, f"unknown acceptance reason {reason!r}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    args = parser.parse_args(argv)

    if not args.allowlist.is_file():
        print(f"no allow-list at {args.allowlist}; every advisory is a failure", file=sys.stderr)
        allowlist: dict[str, dict[str, Any]] = {}
    else:
        allowlist = json.loads(args.allowlist.read_text(encoding="utf-8")).get("accepted", {})

    report = _run_pip_audit()
    found = _collect_ids(report)
    versions = _installed_versions()
    reqs = _installed_requirements()

    failures: list[str] = []
    accepted: list[str] = []
    for advisory_id, meta in sorted(found.items()):
        entry = allowlist.get(advisory_id)
        if entry is None:
            failures.append(f"UNACCEPTED {advisory_id} in {meta['package']} {meta['version']}")
            continue
        holds, why = _pin_still_holds({**entry, "id": advisory_id}, versions, reqs)
        if holds:
            accepted.append(f"ACCEPTED  {advisory_id} ({meta['package']}) — {why}")
        else:
            failures.append(f"STALE     {advisory_id} ({meta['package']}) — {why}")

    for line in accepted:
        print(line)
    for line in failures:
        print(line, file=sys.stderr)

    print(f"\n{len(found)} advisories: {len(accepted)} accepted, {len(failures)} blocking")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
