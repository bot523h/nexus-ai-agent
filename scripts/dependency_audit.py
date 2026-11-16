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
``no-fix-released``: no version of the package fixes it yet;
``fix-requires-newer-python``: a fix is published but every fixed release
declares a ``Requires-Python`` this interpreter does not satisfy — the entry
expires the moment a fix becomes installable here, and it stands only while
``pyproject.toml`` really carries the floor for the interpreters that *can*
install it).  If the pin no longer holds — because an upstream cap was relaxed,
a fix was published, or the floor was dropped — the entry is reported STALE and
the gate fails, forcing a re-review instead of a silent pass.

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
import platform
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the 3.10 CI leg
    # The repository supports 3.10 (`requires-python = ">=3.10"`, and CI runs
    # 3.10/3.11/3.12), so the pyproject reader must work there too: `tomllib` is
    # 3.11+. `tomli` is declared in the `dev` extra for exactly this leg rather
    # than being borrowed from a transitive (deptry/pip-audit both happen to ship
    # it, which is not a contract).
    import tomli as tomllib  # type: ignore[no-redef]

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

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


def _pypi_requires_python(package: str, version: str) -> str | None:
    """The ``Requires-Python`` PyPI publishes for one release, or ``None``.

    ``None`` means "could not be established" (no network, unknown release, bad
    payload). Callers treat it as *unknown*, never as permission: the acceptance
    that depends on it is only granted when the metadata is actually readable.
    """
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=20) as response:  # noqa: S310 - fixed host
            payload = json.load(response)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    requires = info.get("requires_python")
    return requires if isinstance(requires, str) and requires else None


def _pyproject_requirements(pyproject: Path | None = None) -> list[Requirement]:
    """Every requirement string ``pyproject.toml`` declares, parsed.

    Both ``[project].dependencies`` and every extra are read: a floor declared in
    an extra still protects the install that asks for that extra, so ignoring it
    would report a live floor as missing.
    """
    path = pyproject or (REPO_ROOT / "pyproject.toml")
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    project = data.get("project")
    if not isinstance(project, dict):
        return []
    raw: list[str] = []
    deps = project.get("dependencies")
    if isinstance(deps, list):
        raw.extend(str(item) for item in deps)
    extras = project.get("optional-dependencies")
    if isinstance(extras, dict):
        for group in extras.values():
            if isinstance(group, list):
                raw.extend(str(item) for item in group)
    parsed: list[Requirement] = []
    for item in raw:
        try:
            parsed.append(Requirement(item))
        except InvalidRequirement:
            continue
    return parsed


def _required_version(req: Requirement) -> Version | None:
    """The highest version *req* demands (``>=4.1.1`` -> ``4.1.1``), else ``None``."""
    versions: list[Version] = []
    for specifier in req.specifier:
        if specifier.operator in {">=", "==", "===", "~=", ">"}:
            try:
                versions.append(Version(specifier.version))
            except InvalidVersion:
                continue
    return max(versions) if versions else None


def _lowest_installable(published: str) -> str | None:
    """The lowest interpreter version satisfying a ``Requires-Python`` specifier.

    ``>=3.11.5`` -> ``"3.11.5"``. This is the interpreter the floor has to cover:
    an acceptance that says "a fix exists but you need a newer Python" is only
    honest if the *newest* interpreter that can install it is actually asked for
    the fixed release.
    """
    try:
        specifier_set = SpecifierSet(published)
    except InvalidSpecifier:
        return None
    candidates: list[Version] = []
    for specifier in specifier_set:
        if specifier.operator in {">=", "==", "===", "~=", ">"}:
            try:
                candidates.append(Version(specifier.version))
            except InvalidVersion:
                continue
    if not candidates:
        return None
    return str(max(candidates))


def _declared_floor_problem(declared: str, *, probe: str | None) -> str | None:
    """Why the project does not really ask for *declared* where the fix can install.

    Returns ``None`` when ``pyproject.toml`` carries a requirement that (a) names
    the same package, (b) pins at least the declared floor, and (c) has an
    environment marker that is **true** for *probe* — the lowest interpreter that
    can install the fixed release.

    This is the check that a substring search cannot do. The floor was written as
    ``soupsieve>=4.1.1; python_version >= '3.11.5'``; PEP 508 defines
    ``python_version`` as ``major.minor`` only, so ``"3.11" >= "3.11.5"`` is
    false and the floor **never applied on any 3.11 interpreter**. A textual
    check called that present. Evaluating the marker at the probe interpreter
    calls it what it was: dead.
    """
    try:
        floor_req = Requirement(declared)
    except InvalidRequirement:
        return f"{declared!r} is not a parseable requirement"
    if probe is None:
        return (
            "cannot establish which interpreter can install the fixed release, so the "
            f"floor {declared!r} cannot be shown to apply"
        )
    required = _required_version(floor_req)
    if required is None:
        return f"the floor {declared!r} demands no concrete version"
    # A plain dict, not packaging's typed ``Environment``: the marker is evaluated
    # against an interpreter we are *asking about*, not the one running this gate.
    environment: dict[str, str] = {key: str(value) for key, value in default_environment().items()}
    environment["python_full_version"] = probe
    environment["python_version"] = ".".join(probe.split(".")[:2])
    seen: list[str] = []
    for req in _pyproject_requirements():
        if req.name.lower().replace("_", "-") != floor_req.name.lower().replace("_", "-"):
            continue
        declared_marker = str(req.marker) if req.marker is not None else ""
        if req.marker is not None:
            try:
                applies = bool(req.marker.evaluate(environment))
            except Exception:  # noqa: BLE001 - an unevaluable marker is not a floor
                applies = False
            if not applies:
                seen.append(f"{declared_marker} is false at python_full_version {probe}")
                continue
        pinned = _required_version(req)
        if pinned is None or pinned < required:
            seen.append(f"{req} does not pin {required}")
            continue
        return None
    detail = "; ".join(seen) if seen else "no requirement for that package at all"
    return (
        f"pyproject.toml does not ask for {floor_req.name} {required} on the interpreters "
        f"that can install it ({detail})"
    )


def _declared_floor_is_present(declared: str, *, probe: str | None = None) -> bool:
    """Convenience wrapper: is the floor declared *and* applicable at *probe*?"""
    return _declared_floor_problem(declared, probe=probe) is None


def _pin_still_holds(
    entry: dict[str, Any],
    versions: dict[str, str],
    reqs: dict[str, list[str]],
    fix_versions: list[str] | None = None,
    *,
    interpreter: str | None = None,
    requires_python_of: Callable[[str, str], str | None] | None = None,
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

    if reason == "fix-requires-newer-python":
        fixed = entry.get("fixed")
        declared_requires = entry.get("requires_python")
        which = entry.get("id", "the advisory")
        if not fixed or not declared_requires:
            return False, ("fix-requires-newer-python entry needs `fixed` and `requires_python`")
        here = interpreter or platform.python_version()
        # Self-expiry 1: the advisory database must still name a fix, and the
        # project's floor must be at least that new.  (pip-audit reports the
        # *first* fixed release — 3.0.2 / 4.0.0 here — while the floor asks for a
        # later one; pinning below the reported fix would be a stale acceptance.)
        reported = [version for version in (fix_versions or []) if version]
        if not reported:
            return False, f"pip-audit names no fix for {which} any more — re-review this entry"
        try:
            first_fix = min(Version(version) for version in reported)
            declared_fix = Version(fixed)
        except Exception:
            return False, f"entry or advisory names an unparseable version ({fixed!r})"
        if declared_fix < first_fix:
            return False, (
                f"the advisory is first fixed in {first_fix} but this entry pins {fixed} — "
                f"raise the floor and re-review"
            )
        # Self-expiry 2: if this interpreter could run that fix, the entry is a lie.
        try:
            declared_spec = SpecifierSet(declared_requires)
        except InvalidSpecifier:
            return False, f"entry declares an unparseable Requires-Python {declared_requires!r}"
        if declared_spec.contains(here, prereleases=True):
            return False, (
                f"{pkg} {fixed} declares Requires-Python {declared_requires}, which this "
                f"interpreter ({here}) satisfies — install it and drop this entry"
            )
        # Self-expiry 3: any *other* published fix installable here ends it too.
        lookup = requires_python_of or _pypi_requires_python
        published = lookup(pkg, fixed)
        if published is None:
            return False, (
                f"cannot read PyPI metadata for {pkg} {fixed}; the interpreter gap that "
                f"justifies this entry is unverified"
            )
        try:
            if SpecifierSet(published).contains(here, prereleases=True):
                return False, (
                    f"PyPI says {pkg} {fixed} requires Python {published}, which this "
                    f"interpreter ({here}) satisfies — install it and drop this entry"
                )
        except InvalidSpecifier:
            return False, f"PyPI publishes an unparseable Requires-Python for {pkg} {fixed}"
        for candidate in [*reported, fixed]:
            if candidate == fixed:
                continue
            other = lookup(pkg, candidate)
            if other is None:
                continue
            try:
                if SpecifierSet(other).contains(here, prereleases=True):
                    return False, (
                        f"{pkg} {candidate} is installable on this interpreter ({here}) — "
                        f"bump to {candidate} and drop this entry"
                    )
            except InvalidSpecifier:
                continue
        # Self-expiry 4: the project must still ask for the fix where it can be had,
        # and its marker must actually be true there (a marker that never applies is
        # a floor that does not exist, however the text reads).
        floor = entry.get("declared_floor")
        if not isinstance(floor, str) or not floor:
            return False, "fix-requires-newer-python entry needs a `declared_floor`"
        floor_problem = _declared_floor_problem(floor, probe=_lowest_installable(published))
        if floor_problem is not None:
            return False, (
                f"the floor {floor!r} is not enforced where the fix is installable — "
                f"{floor_problem}; the entries that can install the fix would silently regress"
            )
        return True, (
            f"every published fix ({', '.join(reported)}) declares Requires-Python "
            f"{published}; this interpreter is {here} (installed {installed}), and "
            f"{floor!r} covers the interpreters that can take it"
        )

    return False, f"unknown acceptance reason {reason!r}"


def _floor_covers_advisory(
    advisory_id: str,
    meta: dict[str, Any],
    floor_entry: dict[str, Any],
    versions: dict[str, str],
    reqs: dict[str, list[str]],
    *,
    interpreter: str | None = None,
) -> tuple[bool, str]:
    """Accept an *unlisted* advisory that the declared package floor already resolves.

    The three reviewed `soupsieve` entries accept three specific CVEs. That is
    precise but brittle in one direction: a fourth advisory against the same
    package, fixed in a release at or below the declared floor, would block the
    gate on an interpreter that cannot install the fix — the exact situation the
    floor exists for — for no security reason, because a 4.1.1 install removes it
    just as it removes the other three.

    So a package may declare *one* floor, and that floor covers any advisory for
    that package whose **every** reported fix is at or below the floor. It cannot
    cover an advisory fixed in a later release (the floor would not resolve it),
    and it cannot cover an advisory pip-audit reports with no fix at all. The
    same four self-expiry checks still run, so the coverage dies the moment the
    floor stops being declared where it can be installed.

    *interpreter* exists because this decision depends on it: the same floor
    covers an advisory on an interpreter that cannot install the fix and must
    report STALE on one that can. It defaults to the running interpreter in
    production, and tests pin it, because a test that silently used whatever
    interpreter CI happened to run would pass on 3.11.2 and fail on 3.12 — which
    is exactly what the first version of this test did.
    """
    pkg = meta["package"].lower()
    reported = [version for version in (meta.get("fix_versions") or []) if version]
    ceiling = floor_entry.get("fixed")
    if not reported:
        return False, (
            f"UNACCEPTED {advisory_id} in {meta['package']} {meta['version']} — pip-audit "
            f"names no fix, so the {floor_entry.get('declared_floor')} floor cannot be "
            "shown to resolve it"
        )
    if not isinstance(ceiling, str) or not ceiling:
        return False, f"package floor for {pkg} names no concrete `fixed` version"
    try:
        ceiling_version = Version(ceiling)
        highest = max(Version(version) for version in reported)
    except InvalidVersion:
        return False, f"package floor for {pkg} names an unparseable version ({ceiling!r})"
    if highest > ceiling_version:
        return False, (
            f"UNACCEPTED {advisory_id} in {meta['package']} {meta['version']} — the "
            f"advisory is fixed in {highest}, which the {pkg}>={ceiling} floor does not "
            "reach; raise the floor and review it"
        )
    holds, why = _pin_still_holds(
        {**floor_entry, "package": meta["package"], "id": advisory_id},
        versions,
        reqs,
        reported,
        interpreter=interpreter,
    )
    if holds:
        return True, (
            f"ACCEPTED  {advisory_id} ({meta['package']}) — covered by the package floor "
            f"{floor_entry.get('declared_floor')}: fixed in {highest} <= {ceiling}. {why}"
        )
    return False, f"STALE     {advisory_id} ({meta['package']}) — package floor: {why}"


def _decide_advisory(
    key: tuple[str, str],
    meta: dict[str, Any],
    allowlist: dict[str, dict[str, Any]],
    versions: dict[str, str],
    reqs: dict[str, list[str]],
    package_floors: dict[str, dict[str, Any]] | None = None,
    *,
    interpreter: str | None = None,
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
        floor_entry = (package_floors or {}).get(pkg_key)
        if floor_entry is not None:
            return _floor_covers_advisory(
                advisory_id, meta, floor_entry, versions, reqs, interpreter=interpreter
            )
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
        if entry.get("reason") not in {
            "transitive-cap",
            "no-fix-released",
            "fix-requires-newer-python",
        }:
            raise SystemExit(
                f"allow-list entry {advisory_id!r} has unknown reason {entry.get('reason')!r}"
            )
        if entry["reason"] == "transitive-cap" and not (
            entry.get("fixed") and entry.get("capped_by")
        ):
            raise SystemExit(
                f"allow-list entry {advisory_id!r} (transitive-cap) needs `fixed` and `capped_by`"
            )
        if entry["reason"] == "fix-requires-newer-python" and not (
            entry.get("fixed") and entry.get("requires_python") and entry.get("declared_floor")
        ):
            raise SystemExit(
                f"allow-list entry {advisory_id!r} (fix-requires-newer-python) needs "
                "`fixed`, `requires_python` and `declared_floor`"
            )
    return accepted


def _load_package_floors(path: Path) -> dict[str, dict[str, Any]]:
    """Load the optional ``package_floors`` block, failing closed on bad shapes.

    Absent is legitimate (most trees declare no floor); malformed is not, and a
    floor that cannot be re-derived is never silently dropped.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"allow-list {path} is not valid JSON: {exc}") from exc
    floors = data.get("package_floors")
    if floors is None:
        return {}
    if not isinstance(floors, dict):
        raise SystemExit(f"allow-list {path} has a non-object 'package_floors'")
    for package, entry in floors.items():
        if not isinstance(entry, dict):
            raise SystemExit(f"package floor {package!r} is not an object")
        if entry.get("reason") != "fix-requires-newer-python":
            raise SystemExit(
                f"package floor {package!r} must use reason 'fix-requires-newer-python' "
                f"(got {entry.get('reason')!r})"
            )
        for field in ("fixed", "requires_python", "declared_floor"):
            if not isinstance(entry.get(field), str) or not entry[field]:
                raise SystemExit(f"package floor {package!r} needs a non-empty {field!r}")
    return floors


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
    package_floors = _load_package_floors(args.allowlist)
    report = _parse_pip_audit(_run_pip_audit_stdout())
    found = _collect_ids(report)
    versions = _installed_versions()
    reqs = _installed_requirements()

    failures: list[str] = []
    accepted: list[str] = []
    for key, meta in sorted(found.items()):
        ok, message = _decide_advisory(key, meta, allowlist, versions, reqs, package_floors)
        (accepted if ok else failures).append(message)

    for line in accepted:
        print(line)
    for line in failures:
        print(line, file=sys.stderr)

    print(f"\n{len(found)} advisories: {len(accepted)} accepted, {len(failures)} blocking")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
