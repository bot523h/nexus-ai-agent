"""Operational Continuum gate: publish a snapshot for an exact SHA, then attack it.

Unit tests prove the verifier's logic with fixtures; this gate proves the
*deployed chain* on a real checkout of the commit under test::

    clone HEAD (full history) -> ``nexus continuum publish`` -> commit
      -> ``nexus continuum verify`` must exit 0          (control)
      -> every attack of the threat model must exit 1   (scenarios)

Each scenario starts from a pristine reset of the published commit, mutates
exactly one thing (source, history, snapshot bytes, environment, git), runs
the real CLI in a fresh interpreter whose ``PYTHONPATH`` points at the clone,
and records the observed exit code and diagnostics.  A scenario passes only
when the exit code **and** the expected diagnosis both match; the gate passes
only when the control and every scenario pass.

Commits inside the clone use a fixed identity and fixed timestamps, and
temporary paths are redacted, so two runs over the same SHA with the same
interpreter produce byte-identical reports (the reproducibility evidence).

The committed ``.nexus/continuum.json`` is *reported* (``committed_snapshot``,
``blocking: false``) but not gated: it is machine-bound and refreshed only at a
release cut (DECISION_LOG D-0006/D-0023).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from nexus_ai_agent.continuum import provenance

GATE_SCHEMA = "nexus.continuum-gate/1"
SNAPSHOT_RELATIVE = ".nexus/continuum.json"
_FIXED_GIT_ENVIRONMENT = {
    "GIT_AUTHOR_NAME": "continuum-gate",
    "GIT_AUTHOR_EMAIL": "continuum-gate@invalid",
    "GIT_COMMITTER_NAME": "continuum-gate",
    "GIT_COMMITTER_EMAIL": "continuum-gate@invalid",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    "GIT_CONFIG_NOSYSTEM": "1",
}
_TIMEOUT_SECONDS = 900


class GateSetupError(RuntimeError):
    """The gate could not build the environment it needs to judge the verifier."""


@dataclass(frozen=True)
class Invocation:
    """How one scenario runs the CLI (defaults: the workspace clone, normal env)."""

    mode: str = "verify"
    root: Path | None = None
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass
class Workspace:
    """A disposable clone of the commit under test with a published snapshot."""

    root: Path
    source_commit: str
    published_commit: str = ""

    def git(self, *arguments: str, root: Path | None = None) -> str:
        environment = {**os.environ, **_FIXED_GIT_ENVIRONMENT}
        try:
            outcome = subprocess.run(
                ["git", *arguments],
                cwd=root or self.root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise GateSetupError(f"cannot execute git: {exc}") from exc
        if outcome.returncode != 0:
            raise GateSetupError(
                f"git {' '.join(arguments)} exited {outcome.returncode}: "
                f"{(outcome.stderr or outcome.stdout).strip()}"
            )
        return outcome.stdout.strip()

    def path(self, relative: str) -> Path:
        return self.root / relative

    def write(self, relative: str, text: str) -> None:
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def append(self, relative: str, text: str) -> None:
        with self.path(relative).open("a", encoding="utf-8") as handle:
            handle.write(text)

    def commit_all(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "--quiet", "--no-verify", "-m", message)
        return self.git("rev-parse", "HEAD")

    def reset(self) -> None:
        self.git("checkout", "--quiet", "--force", "--detach", self.published_commit)
        self.git("reset", "--quiet", "--hard", self.published_commit)
        self.git("clean", "-fdxq")

    def snapshot_data(self) -> dict[str, object]:
        data = json.loads(self.path(SNAPSHOT_RELATIVE).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise GateSetupError("published snapshot is not a JSON object")
        return data

    def write_snapshot_text(self, text: str, *, commit: bool = True) -> None:
        self.write(SNAPSHOT_RELATIVE, text)
        if commit:
            self.commit_all("gate: tamper snapshot")

    def write_snapshot_data(self, data: Mapping[str, object], *, commit: bool = True) -> None:
        self.write_snapshot_text(canonical_snapshot_text(data), commit=commit)


def canonical_snapshot_text(data: Mapping[str, object]) -> str:
    """The canonical serialisation the verifier accepts (so only *content* is attacked)."""

    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


Prepare = Callable[[Workspace], Invocation | None]


@dataclass(frozen=True)
class Scenario:
    """One attack: how to mutate the workspace and what the verdict must be."""

    id: str
    attack: str
    expected_fragment: str
    prepare: Prepare
    expected_exit: int = 1


@dataclass(frozen=True)
class ScenarioResult:
    id: str
    attack: str
    expected_exit: int
    expected_fragment: str
    observed_exit: int | None
    observed: tuple[str, ...]
    passed: bool
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "attack": self.attack,
            "expected_exit": self.expected_exit,
            "expected_fragment": self.expected_fragment,
            "observed_exit": self.observed_exit,
            "observed": list(self.observed),
            "passed": self.passed,
            "error": self.error,
        }


def judge(
    scenario: Scenario, observed_exit: int | None, observed: Sequence[str], error: str | None
) -> ScenarioResult:
    """A scenario passes only on the exact exit code *and* the expected diagnosis."""

    passed = (
        error is None
        and observed_exit == scenario.expected_exit
        and any(scenario.expected_fragment in line for line in observed)
    )
    return ScenarioResult(
        id=scenario.id,
        attack=scenario.attack,
        expected_exit=scenario.expected_exit,
        expected_fragment=scenario.expected_fragment,
        observed_exit=observed_exit,
        observed=tuple(observed),
        passed=passed,
        error=error,
    )


# ---------------------------------------------------------------------------
# scenario preparations (each starts from a pristine published commit)
# ---------------------------------------------------------------------------

_PROBE_MODULE = "src/nexus_ai_agent/_continuum_gate_probe.py"
_IGNORED_PROBE = "src/nexus_ai_agent/models/_continuum_gate_probe.py"
_PACKAGE_INIT = "src/nexus_ai_agent/__init__.py"


def _control(_ws: Workspace) -> None:
    return None


def _tracked_edit(ws: Workspace) -> None:
    ws.append(_PACKAGE_INIT, "\n# continuum-gate: uncommitted drift\n")


def _untracked_source(ws: Workspace) -> None:
    ws.write(_PROBE_MODULE, "VALUE = 1\n")


def _ignored_source(ws: Workspace) -> None:
    ws.write(_IGNORED_PROBE, "VALUE = 1\n")
    # precondition: the probe really is invisible to plain `git status`
    ws.git("check-ignore", "--quiet", _IGNORED_PROBE)


def _partial_publication(ws: Workspace) -> None:
    ws.write(".nexus/.continuum.json.interrupted.tmp", '{"partial": ')


def _committed_source_drift(ws: Workspace) -> None:
    ws.append(_PACKAGE_INIT, "\n# continuum-gate: committed drift\n")
    ws.commit_all("gate: source drift")


def _committed_migration_drift(ws: Workspace) -> None:
    ws.append("migrations/env.py", "\n# continuum-gate: migration drift\n")
    ws.commit_all("gate: migration drift")


def _committed_new_test(ws: Workspace) -> None:
    ws.write(
        "tests/unit/test_continuum_gate_probe.py",
        "def test_continuum_gate_probe():\n    assert True\n",
    )
    ws.commit_all("gate: new test")


def _snapshot_field(key: str, value: object) -> Prepare:
    def prepare(ws: Workspace) -> None:
        data = ws.snapshot_data()
        data[key] = value
        ws.write_snapshot_data(data)

    return prepare


def _forged_count(ws: Workspace) -> None:
    data = ws.snapshot_data()
    count = data["test_count_expected"]
    if type(count) is not int:
        raise GateSetupError("published snapshot has no integer test count")
    data["test_count_expected"] = count + 1
    ws.write_snapshot_data(data)


def _forged_environment(ws: Workspace) -> None:
    data = ws.snapshot_data()
    environment = data["env_fingerprint"]
    if not isinstance(environment, dict):
        raise GateSetupError("published snapshot has no environment object")
    data["env_fingerprint"] = {**environment, "sqlalchemy": "0.0.0-forged"}
    ws.write_snapshot_data(data)


def _unreachable_step(ws: Workspace) -> None:
    orphan = ws.git("commit-tree", "HEAD^{tree}", "-m", "gate: orphan")
    data = ws.snapshot_data()
    data["step"] = orphan
    ws.write_snapshot_data(data)


def _abbreviated_step(ws: Workspace) -> None:
    data = ws.snapshot_data()
    data["step"] = str(data["step"])[:12]
    ws.write_snapshot_data(data)


def _extra_key(ws: Workspace) -> None:
    data = ws.snapshot_data()
    data["verified"] = True
    ws.write_snapshot_data(data)


def _missing_key(ws: Workspace) -> None:
    data = ws.snapshot_data()
    del data["env_fingerprint"]
    ws.write_snapshot_data(data)


def _non_canonical(ws: Workspace) -> None:
    data = ws.snapshot_data()
    ws.write_snapshot_text(json.dumps(data, ensure_ascii=False, indent=4, sort_keys=True) + "\n")


def _duplicate_key(ws: Workspace) -> None:
    text = canonical_snapshot_text(ws.snapshot_data())
    forged = text.replace('  "plan":', '  "plan": "shadowed",\n  "plan":', 1)
    ws.write_snapshot_text(forged)


def _nan_ledger(ws: Workspace) -> None:
    text = canonical_snapshot_text(ws.snapshot_data())
    if '"ledger": []' in text:
        forged = text.replace('"ledger": []', '"ledger": [{"score": NaN}]', 1)
    else:
        forged = text.replace('"ledger": [', '"ledger": [{"score": NaN}, ', 1)
    if forged == text:
        raise GateSetupError("could not place NaN into the ledger")
    ws.write_snapshot_text(forged)


def _malformed(ws: Workspace) -> None:
    ws.write_snapshot_text('{"schema_version": 2,')


def _missing_snapshot(ws: Workspace) -> None:
    ws.git("rm", "--quiet", SNAPSHOT_RELATIVE)
    ws.git("commit", "--quiet", "--no-verify", "-m", "gate: delete snapshot")


def _copied_to_other_history(ws: Workspace) -> None:
    ws.git("checkout", "--quiet", "--orphan", "continuum-gate-copy")
    ws.git("commit", "--quiet", "--no-verify", "-m", "gate: same tree, other history")


def _rolled_back_tree(ws: Workspace) -> None:
    snapshot_text = ws.path(SNAPSHOT_RELATIVE).read_text(encoding="utf-8")
    ws.git("checkout", "--quiet", "--detach", f"{ws.source_commit}~1")
    ws.write_snapshot_text(snapshot_text)


def _shallow_clone(ws: Workspace) -> Invocation:
    shallow = ws.root.parent / "shallow"
    shutil.rmtree(shallow, ignore_errors=True)
    ws.git("branch", "--force", "continuum-gate-published", ws.published_commit)
    ws.git(
        "clone",
        "--quiet",
        "--depth",
        "1",
        "--no-local",
        "--branch",
        "continuum-gate-published",
        f"file://{ws.root}",
        str(shallow),
        root=ws.root.parent,
    )
    if ws.git("rev-parse", "HEAD", root=shallow) != ws.published_commit:
        raise GateSetupError("shallow clone did not check out the published commit")
    return Invocation(root=shallow)


def _git_unavailable(ws: Workspace) -> Invocation:
    empty = ws.root.parent / "no-git-bin"
    empty.mkdir(exist_ok=True)
    return Invocation(environment={"PATH": str(empty)})


def _show_malformed(ws: Workspace) -> Invocation:
    _malformed(ws)
    return Invocation(mode="show")


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("S01", "uncommitted edit to tracked source", "working tree drift", _tracked_edit),
    Scenario("S02", "untracked importable source file", "working tree drift", _untracked_source),
    Scenario(
        "S03",
        "ignored-but-importable source file (hidden from plain git status)",
        "working tree drift",
        _ignored_source,
    ),
    Scenario(
        "S04",
        "leftover temp file of an interrupted publication",
        "working tree drift",
        _partial_publication,
    ),
    Scenario("S05", "later committed source drift", "source state drift", _committed_source_drift),
    Scenario(
        "S06",
        "later committed migration drift",
        "source state drift",
        _committed_migration_drift,
    ),
    Scenario(
        "S07",
        "later committed new test (collection drift)",
        "test count mismatch",
        _committed_new_test,
    ),
    Scenario("S08", "forged test count", "test count mismatch", _forged_count),
    Scenario(
        "S09",
        "forged environment fingerprint",
        "environment fingerprint mismatch",
        _forged_environment,
    ),
    Scenario(
        "S10", "step is an unreachable (orphan) commit", "state loss detected", _unreachable_step
    ),
    Scenario(
        "S11",
        "step names a commit that does not exist",
        "state loss detected",
        _snapshot_field("step", "0" * 40),
    ),
    Scenario(
        "S12",
        "symbolic step that always re-resolves (HEAD)",
        "full lowercase hexadecimal commit id",
        _snapshot_field("step", "HEAD"),
    ),
    Scenario(
        "S13",
        "abbreviated step",
        "full lowercase hexadecimal commit id",
        _abbreviated_step,
    ),
    Scenario("S14", "malformed JSON", "snapshot unreadable", _malformed),
    Scenario("S15", "extra key (forged verdict)", "unexpected keys", _extra_key),
    Scenario("S16", "missing key", "missing keys", _missing_key),
    Scenario(
        "S17",
        "wrong type for the test count",
        "must be a non-negative integer",
        _snapshot_field("test_count_expected", "5"),
    ),
    Scenario(
        "S18",
        "bool masquerading as the test count",
        "must be a non-negative integer",
        _snapshot_field("test_count_expected", True),
    ),
    Scenario("S19", "non-canonical bytes (same content)", "not canonical", _non_canonical),
    Scenario("S20", "duplicate key hiding a second value", "duplicate key", _duplicate_key),
    Scenario("S21", "NaN smuggled into the ledger", "non-finite", _nan_ledger),
    Scenario("S22", "snapshot deleted", "snapshot unreadable", _missing_snapshot),
    Scenario(
        "S23",
        "snapshot copied onto another history with an identical tree",
        "state loss detected",
        _copied_to_other_history,
    ),
    Scenario(
        "S24",
        "snapshot carried back onto an older tree",
        "state loss detected",
        _rolled_back_tree,
    ),
    Scenario("S25", "shallow clone (history unavailable)", "shallow repository", _shallow_clone),
    Scenario("S26", "git executable unavailable", "git verification unavailable", _git_unavailable),
    Scenario(
        "S27", "`continuum show` on a malformed snapshot", "snapshot unreadable", _show_malformed
    ),
)

CONTROL = Scenario(
    "S00", "pristine published snapshot (control)", "matches checkout", _control, expected_exit=0
)


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


def _redact(text: str, replacements: Mapping[str, str]) -> str:
    for needle, label in sorted(replacements.items(), key=lambda item: -len(item[0])):
        text = text.replace(needle, label)
    return text


def run_cli(
    root: Path, mode: str, *, extra_environment: Mapping[str, str] | None = None
) -> tuple[int, tuple[str, ...]]:
    """Run ``nexus continuum <mode>`` for the checkout at *root* in a fresh interpreter."""

    environment = provenance.isolated_python_environment(root)
    environment.update(extra_environment or {})
    completed = subprocess.run(
        [sys.executable, "-m", "nexus_ai_agent.cli", "continuum", mode],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=_TIMEOUT_SECONDS,
    )
    lines = [
        line.strip()
        for line in (completed.stdout + "\n" + completed.stderr).splitlines()
        if line.strip().startswith(("✓", "✗"))
    ]
    return completed.returncode, tuple(lines)


def _imports_from(root: Path) -> Path:
    environment = provenance.isolated_python_environment(root)
    completed = subprocess.run(
        [sys.executable, "-c", "import nexus_ai_agent; print(nexus_ai_agent.__file__)"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise GateSetupError(f"cannot import nexus_ai_agent from the clone: {completed.stderr}")
    return Path(completed.stdout.strip()).resolve()


def _run_scenario(
    ws: Workspace, scenario: Scenario, redactions: Mapping[str, str]
) -> ScenarioResult:
    try:
        ws.reset()
        invocation = scenario.prepare(ws) or Invocation()
        observed_exit, observed = run_cli(
            invocation.root or ws.root,
            invocation.mode,
            extra_environment=invocation.environment,
        )
    except (GateSetupError, OSError, subprocess.SubprocessError, KeyError) as exc:
        return judge(scenario, None, (), _redact(f"{type(exc).__name__}: {exc}", redactions))
    return judge(scenario, observed_exit, [_redact(line, redactions) for line in observed], None)


def _committed_snapshot_status(source_root: Path) -> dict[str, object]:
    from nexus_ai_agent.continuum import snapshot

    if snapshot._REPO_ROOT.resolve() != source_root.resolve():
        problems = [f"verifier imported from {snapshot._REPO_ROOT}, not {source_root}"]
    else:
        problems = snapshot.verify_snapshot()
    return {
        "path": SNAPSHOT_RELATIVE,
        "status": "FRESH" if not problems else "STALE",
        "problems": problems,
        "blocking": False,
        "policy": (
            "machine-bound release-cut record (DECISION_LOG D-0006/D-0023): reported, "
            "not gated; the verifier itself is gated by the scenarios above"
        ),
    }


def run_gate(
    source_root: Path = provenance.REPO_ROOT,
    *,
    scenarios: Sequence[Scenario] = SCENARIOS,
    workdir: Path | None = None,
    include_committed_status: bool = True,
) -> dict[str, object]:
    """Run the control and every scenario; return the machine-readable report."""

    report: dict[str, object] = {
        "schema": GATE_SCHEMA,
        "interpreter": provenance.interpreter_identity(),
        "source_commit": None,
        "published_commit": None,
        "problems": [],
        "control": None,
        "scenarios": [],
        "passed": False,
    }
    problems: list[str] = []
    try:
        source_commit = provenance.current_commit(source_root)
        report["source_commit"] = source_commit
        drift = provenance.working_tree_drift(source_root, scope=provenance.EVIDENCE_SOURCE_PATHS)
        if drift:
            problems.append(
                "the gate certifies a commit: the source checkout has uncommitted evidence "
                f"changes ({', '.join(drift[:5])})"
            )
        if provenance.is_shallow_repository(source_root):
            problems.append("the source checkout is shallow: fetch full history (fetch-depth: 0)")
    except RuntimeError as exc:
        problems.append(f"cannot identify the commit under test: {exc}")
    if problems:
        report["problems"] = problems
        return report

    with tempfile.TemporaryDirectory(prefix="nexus-continuum-gate-", dir=workdir) as directory:
        clone = Path(directory) / "repo"
        ws = Workspace(root=clone, source_commit=source_commit)
        redactions = {str(Path(directory).resolve()): "<gate>", directory: "<gate>"}
        try:
            ws.git(
                "clone",
                "--quiet",
                "--no-hardlinks",
                str(source_root),
                str(clone),
                root=Path(directory),
            )
            ws.git("checkout", "--quiet", "--detach", source_commit)
            imported = _imports_from(clone)
            if not imported.is_relative_to(clone.resolve()):
                raise GateSetupError(
                    f"the clone imports nexus_ai_agent from {imported}: the gate would verify the "
                    "wrong checkout"
                )
            published_exit, published = _publish(clone)
            if published_exit != 0:
                raise GateSetupError(
                    "publishing the snapshot failed: "
                    + _redact("; ".join(published) or "no diagnostics", redactions)
                )
            ws.published_commit = ws.commit_all("gate: publish continuum snapshot")
            report["published_commit"] = ws.published_commit
        except (GateSetupError, OSError, subprocess.SubprocessError) as exc:
            report["problems"] = [_redact(f"gate setup failed: {exc}", redactions)]
            return report

        control = _run_scenario(ws, CONTROL, redactions)
        results = [_run_scenario(ws, scenario, redactions) for scenario in scenarios]

    report["control"] = control.as_dict()
    report["scenarios"] = [result.as_dict() for result in results]
    failed = [result.id for result in results if not result.passed]
    if not control.passed:
        problems.append("control failed: a freshly published snapshot did not verify")
    if failed:
        problems.append(f"attacks not rejected as specified: {', '.join(failed)}")
    if not results:
        problems.append("no attack scenarios ran")
    report["problems"] = problems
    report["passed"] = not problems
    if include_committed_status:
        report["committed_snapshot"] = _committed_snapshot_status(source_root)
    return report


def _publish(clone: Path) -> tuple[int, tuple[str, ...]]:
    environment = provenance.isolated_python_environment(clone)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "nexus_ai_agent.cli",
            "continuum",
            "publish",
            "--plan",
            "continuum-gate",
            "--next",
            "none (disposable gate snapshot)",
        ],
        cwd=clone,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=_TIMEOUT_SECONDS,
    )
    lines = tuple(
        line.strip()
        for line in (completed.stdout + "\n" + completed.stderr).splitlines()
        if line.strip()
    )
    return completed.returncode, lines


def canonical_report(report: Mapping[str, object]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def report_problems(report: Mapping[str, object]) -> list[str]:
    problems = report.get("problems")
    return [str(problem) for problem in problems] if isinstance(problems, list) else []


def summary_lines(report: Mapping[str, object]) -> list[str]:
    """Human-readable PASS/FAIL rows for the control and every scenario."""

    rows: list[object] = [report.get("control")]
    scenarios = report.get("scenarios")
    if isinstance(scenarios, list):
        rows.extend(scenarios)
    lines = [
        f"{'PASS' if row.get('passed') is True else 'FAIL'} {row.get('id')} "
        f"exit={row.get('observed_exit')} {row.get('attack')}"
        for row in rows
        if isinstance(row, dict)
    ]
    committed = report.get("committed_snapshot")
    if isinstance(committed, dict):
        lines.append(f"committed snapshot: {committed.get('status')} (blocking: false)")
    return lines


__all__ = [
    "CONTROL",
    "GATE_SCHEMA",
    "SCENARIOS",
    "GateSetupError",
    "Invocation",
    "Scenario",
    "ScenarioResult",
    "Workspace",
    "canonical_report",
    "canonical_snapshot_text",
    "judge",
    "report_problems",
    "run_cli",
    "run_gate",
    "summary_lines",
]
