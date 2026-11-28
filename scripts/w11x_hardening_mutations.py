"""W11X hardening mutation campaign (15 mutants, bytecode-cache safe).

Every mutant removes or inverts one *specific* defensive decision made in this
mission and runs only the focused test module that is supposed to notice. A
mutant is **killed** only by exit code 1 with a real test failure; collection
errors, syntax errors and other infrastructure failures are classified
``invalid`` instead of being counted as kills. Source bytes are restored in
``finally`` and verified against their pre-flight SHA-256.

The rule for adding a mutant: it must correspond to a *behaviour someone could
regress*, not to a line. ``M01`` (the forbidden factory guard) and ``M10``
(a dry run that mutates) are the two that would cause real damage: a synthetic
SQLite artifact in a production bucket, or thirty pull requests closed by a
command that a tired operator ran without flags.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
# Unambiguous run-level failures: the suite never reached a test.
_INFRA_TOKENS = (
    "ERROR collecting",
    "SyntaxError",
    "no tests ran",
    "collected 0 items",
    "INTERNALERROR",
)
# Import failures are ambiguous. At *collection* time they mean the mutant broke
# the module (exit code 2, already invalid). Inside a *failing test* they are the
# mutant's own consequence — a provider the mutant now reaches imports an
# optional extra the CI job does not install — so they only condemn a run that
# reports no failing test at all.
_SOFT_INFRA_TOKENS = ("ImportError", "ModuleNotFoundError")
_FAILED_PATTERN = re.compile(r"\b[1-9]\d*\s+failed\b")

BACKUP = "src/nexus_ai_agent/maintenance/backup.py"
SELFTEST = "src/nexus_ai_agent/maintenance/ci_selftest.py"
HOUSEKEEPING = "src/nexus_ai_agent/maintenance/housekeeping.py"
SURFACE = "src/nexus_ai_agent/api/studio_surface.py"
CLOSE_PRS = ".agents/evidence/pr-convergence-2026-11-27/tools/close_prs.py"
RECONCILE = ".agents/evidence/pr-convergence-2026-11-27/tools/board_reconcile.py"
ANALYZE = ".agents/evidence/pr-convergence-2026-11-27/tools/analyze.py"

ISOLATION_TEST = "tests/unit/test_maintenance_selftest_isolation.py"
RESIDUAL_TEST = "tests/unit/test_maintenance_selftest_residuals.py"
SURFACE_TEST = "tests/unit/test_studio_surface_security.py"
RECOVERY_TEST = "tests/unit/test_pr_convergence_recovery.py"


@dataclass(frozen=True, slots=True)
class Mutation:
    name: str
    source: str
    test: str
    old: str
    new: str


MUTATIONS: tuple[Mutation, ...] = (
    # --- the factory guard (the invariant the whole CI lane rests on) ---------
    Mutation(
        "M01_factory_guard_removed",
        BACKUP,
        ISOLATION_TEST,
        "    if selftest_active():\n        raise RuntimeError(SELFTEST_FACTORY_FORBIDDEN)",
        "    if False:\n        raise RuntimeError(SELFTEST_FACTORY_FORBIDDEN)",
    ),
    Mutation(
        "M02_guard_fails_open_on_exit",
        BACKUP,
        ISOLATION_TEST,
        "        if previous is None:\n            os.environ.pop(SELFTEST_FLAG_ENV, None)\n"
        "        else:\n            os.environ[SELFTEST_FLAG_ENV] = previous",
        "        pass",
    ),
    Mutation(
        "M03_guard_ignores_the_environment",
        BACKUP,
        ISOLATION_TEST,
        '    return _SELFTEST_ACTIVE.get() or os.environ.get(SELFTEST_FLAG_ENV) == "1"',
        "    return _SELFTEST_ACTIVE.get()",
    ),
    Mutation(
        "M04_legacy_alias_bypasses_the_guard",
        BACKUP,
        ISOLATION_TEST,
        "def _build_provider(settings: Settings) -> BlobProvider:\n"
        '    """Deprecated alias for :func:`build_blob_provider`."""\n'
        "    return build_blob_provider(settings)",
        "def _build_provider(settings: Settings) -> BlobProvider:\n"
        '    """Deprecated alias for :func:`build_blob_provider`."""\n'
        "    return _select_provider_unguarded(settings)",
    ),
    # --- the self-test's own isolation ---------------------------------------
    Mutation(
        "M05_schema_is_never_created",
        SELFTEST,
        ISOLATION_TEST,
        "    asyncio.run(create_all_tables(str(path)))",
        "    pass  # the scratch database is never created",
    ),
    Mutation(
        "M06_run_does_not_hold_the_guard",
        SELFTEST,
        ISOLATION_TEST,
        "    with forbid_remote_provider_factory():",
        "    with contextlib.nullcontext():",
    ),
    Mutation(
        "M07_housekeeping_leg_ignores_its_provider",
        SELFTEST,
        RESIDUAL_TEST,
        '        provider=provider,\n    )\n    deleted = list(result["backups_deleted"])',
        '        provider=None,\n    )\n    deleted = list(result["backups_deleted"])',
    ),
    Mutation(
        "M08_wal_requirement_dropped",
        SELFTEST,
        RESIDUAL_TEST,
        '    if journal_mode != "wal":',
        '    if journal_mode != "wal" and False:',
    ),
    Mutation(
        "M09_housekeeping_tolerates_a_missing_provider",
        HOUSEKEEPING,
        ISOLATION_TEST,
        "    provider = provider or build_blob_provider(settings)",
        "    provider = build_blob_provider(settings)",
    ),
    # --- the studio surface's own controls -----------------------------------
    Mutation(
        "M10_marker_check_removed",
        SURFACE,
        SURFACE_TEST,
        "    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:",
        "    if False:",
    ),
    Mutation(
        "M11_limiter_never_blocks",
        SURFACE,
        SURFACE_TEST,
        "            if len(recent) < self._max_failures:",
        "            if True:",
    ),
    Mutation(
        "M12_limiter_never_forgets_a_success",
        SURFACE,
        SURFACE_TEST,
        "        with self._lock:\n            self._failures.pop(key, None)",
        "        with self._lock:\n            pass",
    ),
    Mutation(
        "M13_session_quota_removed",
        SURFACE,
        SURFACE_TEST,
        "            while self._count_for(principal_id) >= self._per_principal:\n"
        "                self._evict_oldest_locked(principal_id=principal_id)",
        "            while False:\n"
        "                self._evict_oldest_locked(principal_id=principal_id)",
    ),
    # --- the convergence tooling ---------------------------------------------
    Mutation(
        "M14_dry_run_becomes_apply",
        CLOSE_PRS,
        RECOVERY_TEST,
        "    apply = bool(args.apply) and not bool(args.dry_run)",
        "    apply = True",
    ),
    Mutation(
        "M15_comment_reposted_on_resume",
        CLOSE_PRS,
        RECOVERY_TEST,
        "            if has_existing_closure_comment(comments):\n"
        "                journal.transition(number, COMMENTED, comment_url=None, recovered=True)",
        "            if False:\n"
        "                journal.transition(number, COMMENTED, comment_url=None, recovered=True)",
    ),
    Mutation(
        "M16_journal_allows_skipping",
        CLOSE_PRS,
        RECOVERY_TEST,
        "        if state != allowed:\n            raise JournalStateError(",
        "        if False:\n            raise JournalStateError(",
    ),
    Mutation(
        "M17_zone_prefixes_truncated",
        RECONCILE,
        RECOVERY_TEST,
        "    return sorted(prefixes)",
        "    return sorted(prefixes)[:24]",
    ),
    Mutation(
        "M18_zone_prefixes_emits_broad_roots",
        RECONCILE,
        RECOVERY_TEST,
        "        if parent in BROAD_ROOT_PREFIXES:",
        "        if parent in ():",
    ),
    Mutation(
        "M19_parity_label_flattened",
        ANALYZE,
        RECOVERY_TEST,
        'PARITY_METHOD = "structural_signature_parity"',
        'PARITY_METHOD = "behavioural_parity"',
    ),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _purge_bytecode_cache(source_path: Path) -> None:
    try:
        cached = Path(importlib.util.cache_from_source(str(source_path)))
    except (NotImplementedError, ValueError):  # pragma: no cover - exotic layouts
        cached = None
    if cached is not None:
        cached.unlink(missing_ok=True)
        cache_dir = cached.parent
        if cache_dir.is_dir():
            for stale in cache_dir.glob(f"{source_path.stem}.*.pyc"):
                stale.unlink(missing_ok=True)
    importlib.invalidate_caches()


def preflight(repo_root: Path = ROOT) -> list[Mutation]:
    seen: set[str] = set()
    root = Path(repo_root)
    for mutation in MUTATIONS:
        if mutation.name in seen:
            raise MutationCampaignError(f"duplicate mutation name: {mutation.name}")
        seen.add(mutation.name)
        source = root / mutation.source
        test = root / mutation.test
        if not source.is_file():
            raise MutationCampaignError(f"missing source: {mutation.source}")
        if not test.is_file():
            raise MutationCampaignError(f"missing test: {mutation.test}")
        if mutation.old == mutation.new:
            raise MutationCampaignError(f"no-op mutation: {mutation.name}")
        occurrences = source.read_text(encoding="utf-8").count(mutation.old)
        if occurrences != 1:
            raise MutationCampaignError(
                f"mutation {mutation.name!r} matches {occurrences} times in {mutation.source}"
            )
    return list(MUTATIONS)


class MutationCampaignError(RuntimeError):
    """The campaign definition itself is wrong (a stale snippet, a dead test)."""


def run_mutation(
    mutation: Mutation, repo_root: Path = ROOT, *, explain: list[str] | None = None
) -> str:
    root = Path(repo_root)
    source = root / mutation.source
    original = source.read_bytes()
    original_digest = hashlib.sha256(original).hexdigest()
    _purge_bytecode_cache(source)
    try:
        mutated = original.decode("utf-8").replace(mutation.old, mutation.new, 1)
        source.write_bytes(mutated.encode("utf-8"))
        _purge_bytecode_cache(source)
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", mutation.test],
                cwd=root,
                env=_test_env(root),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "not_run"
        verdict = _classify_mutant_outcome(completed)
        if explain is not None and verdict != "killed":
            explain.extend(
                (
                    f"exit={completed.returncode}",
                    *((completed.stdout or "") + (completed.stderr or "")).splitlines()[-40:],
                )
            )
        return verdict
    finally:
        source.write_bytes(original)
        _purge_bytecode_cache(source)
        if _sha256(source) != original_digest:  # pragma: no cover - restoration guard
            raise MutationCampaignError(f"FAILED to restore {mutation.source} byte-for-byte")
        importlib.invalidate_caches()


def _test_env(repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    source_root = str(Path(repo_root) / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source_root if not existing else source_root + os.pathsep + existing
    return env


def _classify_mutant_outcome(completed: subprocess.CompletedProcess[str]) -> str:
    """Only a focused test failure with exit code 1 counts as a killed mutant."""
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode == 0:
        return "survived"
    if completed.returncode != 1:
        return "invalid"
    if any(token in output for token in _INFRA_TOKENS):
        return "invalid"
    if not (_FAILED_PATTERN.search(output) or "FAILED" in output):
        return "invalid"
    if any(token in output for token in _SOFT_INFRA_TOKENS) and not _FAILED_PATTERN.search(output):
        return "invalid"
    return "killed"


def run_baseline(repo_root: Path = ROOT, mutations: Sequence[Mutation] | None = None) -> bool:
    root = Path(repo_root)
    selected = tuple(mutations) if mutations is not None else tuple(preflight(root))
    tests = sorted({mutation.test for mutation in selected})
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *tests],
            cwd=root,
            env=_test_env(root),
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    if completed.returncode != 0:
        print("BASELINE FAILURE", flush=True)
        print((completed.stdout or "")[-4000:], flush=True)
        print((completed.stderr or "")[-2000:], flush=True)
    return completed.returncode == 0


def _prepare_sandbox(repo_root: Path, mutations: Sequence[Mutation]) -> Path:
    """A disposable copy of ``src``, ``scripts`` and the focused tests.

    Kept for callers that want an isolated run; :func:`run_mutation` restores in
    place and is safe for the campaign the repository actually runs.
    """
    sandbox = Path(tempfile.mkdtemp(prefix="w11x-mutations-"))
    shutil.copytree(Path(repo_root) / "src", sandbox / "src")
    for directory in ("tests", "scripts", ".agents"):
        shutil.copytree(Path(repo_root) / directory, sandbox / directory)
    for mutation in mutations:
        if not (sandbox / mutation.test).is_file():
            raise MutationCampaignError(f"missing test in sandbox: {mutation.test}")
    return sandbox


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the W11X hardening mutation campaign.")
    parser.add_argument("--only", action="append", help="run one mutant by name")
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help="skip the pre-flight baseline run (the focused tests must be green)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    mutations = preflight()
    if args.only:
        wanted = set(args.only)
        mutations = [mutation for mutation in mutations if mutation.name in wanted]
        if len(mutations) != len(wanted):
            missing = wanted - {mutation.name for mutation in mutations}
            raise SystemExit(f"unknown mutant(s): {sorted(missing)}")
    if not args.no_baseline and not run_baseline(mutations=mutations):
        raise SystemExit("baseline is red: a mutation campaign over a failing suite proves nothing")

    tally: dict[str, int] = {}
    survivors: list[str] = []
    for mutation in mutations:
        evidence: list[str] = []
        outcome = run_mutation(mutation, explain=evidence)
        tally[outcome] = tally.get(outcome, 0) + 1
        print(f"{outcome:9} {mutation.name}", flush=True)
        if outcome != "killed":
            # A survivor or an invalid run must be explainable from the log
            # alone: the campaign is the evidence, so its failure has to be
            # readable without re-running it locally.
            for line in evidence:
                print(f"          | {line}", flush=True)
            survivors.append(mutation.name)

    total = len(mutations)
    print(
        f"\n{total} mutants: killed={tally.get('killed', 0)} "
        f"survived={tally.get('survived', 0)} invalid={tally.get('invalid', 0)} "
        f"not_run={tally.get('not_run', 0)}",
        flush=True,
    )
    if survivors:
        print("NOT KILLED: " + ", ".join(survivors), flush=True)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())
