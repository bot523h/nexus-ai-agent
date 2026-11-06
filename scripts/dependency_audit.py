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


def _run_pip_audit_stdout() -> str:
    """Run ``pip-audit`` and return its raw JSON text.

    ``sys.executable -m pip_audit`` is a *fixed* argv: no shell, no interpolated
    input, no user-controlled argument, so there is no command-injection surface.
    Only the audit's exit codes 0 (clean) and 1 (vulnerabilities found) are
    expected; anything else means the tool itself failed and is raised so the
    gate can never mistake a broken run for a clean one.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pip_audit", "--format=json", "--progress-spinner=off"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"pip-audit failed to run (exit {proc.returncode})")
    return proc.stdout


def _collect_ids(report: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Return ``{(package, advisory_id): {"package", "version", "fix_versions"}}``.

    The key is the *pair*: an advisory id is a namespace that can be reported for
    more than one package, so keying by id alone would let one finding silently
    overwrite another and drop an advisory from the gate entirely.  The
    ``fix_versions`` list pip-audit already resolved is carried through so a
    ``no-fix-released`` acceptance can expire the moment a fix is published.
    """
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for dep in report.get("dependencies", []):
        pkg = dep.get("name", "")
        ver = dep.get("version", "")
        for vuln in dep.get("vulns", []):
            found[(pkg.lower(), vuln.get("id", ""))] = {
                "package": pkg,
                "version": ver,
                "fix_versions": list(vuln.get("fix_versions") or []),
            }
    return found


def _marker_applies(req: Requirement) -> bool:
    """Whether *req*'s environment marker is active for this interpreter.

    A cap guarded by ``sys_platform == 'win32'`` (or any other environment marker)
    does not constrain a Linux install, so it must not be read as "still blocking"
    here — that would keep an acceptance alive for a platform the project does not
    ship on, and hide a genuinely reachable upgrade. An absent marker always
    applies; a marker that cannot be evaluated is treated as *applicable* (the
    conservative choice: it keeps the cap and never silently drops a real blocker).
    """
    if req.marker is None:
        return True
    try:
        return bool(req.marker.evaluate())
    except Exception:
        return True


def _pin_still_holds(
    entry: dict[str, Any],
    versions: dict[str, str],
    reqs: dict[str, list[str]],
    fix_versions: list[str] | None = None,
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
                # Only a cap that applies to *this* environment can block here; a
                # marker-guarded cap for another platform must not keep the entry
                # alive on a platform it does not constrain.
                if req.name.lower() == pkg and req.specifier and _marker_applies(req):
                    if Version(fixed) not in req.specifier:
                        blockers.append(f"{cap_name} requires {req.specifier}")
        if blockers:
            return True, "blocked by " + "; ".join(blockers)
        return False, (
            f"no installed dependency caps {pkg} below {fixed} in this environment — "
            f"bump to {fixed} and drop this entry"
        )

    if reason == "no-fix-released":
        which = entry.get("id", "the advisory")
        # Self-expiry: pip-audit resolved the fixed versions from the advisory
        # database.  A non-empty list means a fix exists, so "no fix released" is
        # no longer true — the acceptance is stale and the package must be bumped.
        if fix_versions:
            return False, (
                f"a fix is now released for {which} ({pkg} {', '.join(fix_versions)}) — "
                f"bump {pkg} and drop this entry"
            )
        return True, f"no released version of {pkg} fixes {which} (installed {installed})"

    return False, f"unknown acceptance reason {reason!r}"


def _decide_advisory(
    key: tuple[str, str],
    meta: dict[str, Any],
    allowlist: dict[str, dict[str, Any]],
    versions: dict[str, str],
    reqs: dict[str, list[str]],
) -> tuple[bool, str]:
    """Decide one finding, keyed by ``(package, advisory_id)``: ``(accepted, message)``.

    An advisory id is a namespace that can be reported for more than one package.
    ``_collect_ids`` already keys findings by the *pair*, so a shared id yields two
    independent findings that are each examined — neither can overwrite the other.
    The allow-list is keyed by advisory id with the accepted ``package`` named
    inside; an entry therefore accepts exactly one ``(package, id)`` pair, and a
    finding for the same id in a *different* package is a MISMATCH failure, never a
    silent pass. (A single JSON object cannot hold two identical keys, so this is
    how a pair-keyed acceptance is expressed without losing the second package.)
    """
    pkg_key, advisory_id = key
    entry = allowlist.get(advisory_id)
    if entry is None:
        return False, f"UNACCEPTED {advisory_id} in {meta['package']} {meta['version']}"
    if entry.get("package", "").lower() != pkg_key:
        return False, (
            f"MISMATCH   {advisory_id} is reported for {meta['package']}, "
            f"but the allow-list names {entry.get('package')!r}"
        )
    holds, why = _pin_still_holds(
        {**entry, "id": advisory_id}, versions, reqs, meta.get("fix_versions")
    )
    if holds:
        return True, f"ACCEPTED  {advisory_id} ({meta['package']}) — {why}"
    return False, f"STALE     {advisory_id} ({meta['package']}) — {why}"


def _load_allowlist(path: Path) -> dict[str, dict[str, Any]]:
    """Load the reviewed acceptances, failing closed on any structural surprise.

    A missing, unreadable, non-object, or malformed allow-list is a hard error —
    never "no acceptances", which would silently red the build for the wrong
    reason and could hide a real advisory behind a confusing message.
    """
    if not path.is_file():
        raise SystemExit(f"no allow-list at {path}; every advisory would be a failure")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"allow-list {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"allow-list {path} must be a JSON object")
    accepted = data.get("accepted")
    if not isinstance(accepted, dict):
        raise SystemExit(f"allow-list {path} has no 'accepted' object")
    for advisory_id, entry in accepted.items():
        if not isinstance(entry, dict):
            raise SystemExit(f"allow-list entry {advisory_id!r} is not an object")
        if not isinstance(entry.get("package"), str) or not entry["package"]:
            raise SystemExit(f"allow-list entry {advisory_id!r} names no package")
        if entry.get("reason") not in {"transitive-cap", "no-fix-released"}:
            raise SystemExit(
                f"allow-list entry {advisory_id!r} has unknown reason {entry.get('reason')!r}"
            )
        if entry["reason"] == "transitive-cap" and not (
            entry.get("fixed") and entry.get("capped_by")
        ):
            raise SystemExit(
                f"allow-list entry {advisory_id!r} (transitive-cap) needs `fixed` and `capped_by`"
            )
    return accepted


def _parse_pip_audit(stdout: str) -> dict[str, Any]:
    """Parse pip-audit's JSON, refusing anything that is not a well-formed report.

    An empty, truncated, or unexpected payload must never be read as "no
    vulnerabilities" — a gate that passes because it could not read its input is
    worse than no gate.
    """
    if not stdout.strip():
        raise SystemExit("pip-audit produced no output; refusing to pass vacuously")
    try:
        report = json.loads(stdout)
    except ValueError as exc:
        raise SystemExit(f"pip-audit output is not valid JSON: {exc}") from exc
    if not isinstance(report, dict) or not isinstance(report.get("dependencies"), list):
        raise SystemExit("pip-audit output has no 'dependencies' list; refusing to pass")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    args = parser.parse_args(argv)

    allowlist = _load_allowlist(args.allowlist)
    report = _parse_pip_audit(_run_pip_audit_stdout())
    found = _collect_ids(report)
    versions = _installed_versions()
    reqs = _installed_requirements()

    failures: list[str] = []
    accepted: list[str] = []
    for key, meta in sorted(found.items()):
        ok, message = _decide_advisory(key, meta, allowlist, versions, reqs)
        (accepted if ok else failures).append(message)

    for line in accepted:
        print(line)
    for line in failures:
        print(line, file=sys.stderr)

    print(f"\n{len(found)} advisories: {len(accepted)} accepted, {len(failures)} blocking")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
