#!/usr/bin/env python3
"""Fail-closed witness for the *GitHub enforcement plane* (law R19).

The #143 incident produced ``scripts/merge_base_guard.py`` (law R18): a
main-bound merge requires ``base == main``.  R18 protects the *decision*.
Nothing in the repository protected the *enforcement of that decision*:

* the three required status checks live only in GitHub's branch-protection
  settings.  Remove one from the settings page and every test in this
  repository stays green — the gate silently stops being required.
* a required check is only as strong as the job that produces it.  Deleting
  the guard's ``run:`` step while leaving the job in place turns the gate into
  a no-op that still reports green.  (Demonstrated on ``main`` @6122c9b:
  removing the invocation left all 12 R18 tests passing.)
* changing a pull request's base branch fires a ``pull_request`` event of type
  ``edited``, which is **not** one of GitHub's default activity types
  (``opened``/``synchronize``/``reopened``).  A PR opened against ``main`` and
  then retargeted to ``arena/*`` therefore reuses the previous green check —
  retargeting becomes a bypass of R18.

This script binds those three planes together mechanically:

* ``check-offline`` — no network.  Every declared required context must be
  produced by exactly one real CI job, the merge-base context's job must
  actually execute the guard with an event and a base, it must not be
  soft-failed, and the workflow must re-run on every PR activity type that can
  move a base branch.
* ``check-live`` — reads GitHub.  ``protected`` must be true, the live required
  contexts must equal the declared ones, and enforcement must apply to
  everyone.  Sub-settings this token cannot read are reported ``UNKNOWN`` and
  force a ``BLOCKED`` verdict: an unreadable governance source is never a pass.

Exit codes: ``0`` VERIFIED; ``1`` VIOLATION; ``2`` BLOCKED (a source was
unreadable — never a pass).

Persian note: قانون R18 تصمیم را محافظت می‌کند؛ R19 اجرای آن را. سه check لازم
باید واقعاً در GitHub لازم باشند، job مربوطه واقعاً guard را اجرا کند، و تغییر
base یک PR نتواند gate را دور بزند. منبع ناخوانا = BLOCKED، هرگز سبز.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# the declared policy — the single source of truth for "what must be required"
# --------------------------------------------------------------------------- #

#: The required status checks that protect ``main``.  Each entry must be the
#: exact ``name:`` (or job key, when a job declares no name) of one CI job:
#: that equality is what makes "GitHub requires it" mean anything.
REQUIRED_CONTEXTS: tuple[str, ...] = (
    "lint (ruff + mypy + version lockstep)",
    'test (pytest -m "not slow")',
    "merge-base-guard (base == main)",
)

#: The context whose job must actually execute the R18 guard.
BASE_GUARD_CONTEXT = "merge-base-guard (base == main)"
BASE_GUARD_SCRIPT = "scripts/merge_base_guard.py"
BASE_GUARD_SUBCOMMAND = "check-event"

#: PR activity types that can move the base branch or (re)introduce commits.
#: GitHub's defaults are opened/synchronize/reopened; ``edited`` covers a
#: retarget, ``ready_for_review`` covers a draft that becomes mergeable.
BASE_CHANGING_PR_TYPES: tuple[str, ...] = (
    "opened",
    "synchronize",
    "reopened",
    "edited",
    "ready_for_review",
)

DEFAULT_BRANCH = "main"

EXIT_VERIFIED = 0
EXIT_VIOLATION = 1
EXIT_BLOCKED = 2


# --------------------------------------------------------------------------- #
# findings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str  # "violation" | "unknown" | "ok"
    message: str
    witness: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "witness": self.witness,
        }


@dataclass
class Report:
    scope: str
    findings: list[Finding] = field(default_factory=list)

    def add(self, code: str, severity: str, message: str, witness: str) -> None:
        self.findings.append(Finding(code, severity, message, witness))

    @property
    def violations(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "violation"]

    @property
    def unknowns(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "unknown"]

    def verdict(self) -> str:
        if self.violations:
            return "VIOLATION"
        if self.unknowns:
            return "BLOCKED"
        return "VERIFIED"

    def exit_code(self) -> int:
        return {
            "VIOLATION": EXIT_VIOLATION,
            "BLOCKED": EXIT_BLOCKED,
            "VERIFIED": EXIT_VERIFIED,
        }[self.verdict()]

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "verdict": self.verdict(),
            "violations": [f.to_dict() for f in self.violations],
            "unknowns": [f.to_dict() for f in self.unknowns],
            "checks": [f.to_dict() for f in self.findings],
        }

    def format_text(self) -> str:
        lines = [f"governance-guard: scope={self.scope} verdict={self.verdict()}"]
        for finding in self.findings:
            lines.append(
                f"  [{finding.severity.upper():9}] {finding.code}: {finding.message}"
                f"\n              witness: {finding.witness}"
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# a deliberately tiny workflow reader (stdlib-only: no PyYAML, it must run
# before `pip install`, exactly like merge_base_guard.py)
# --------------------------------------------------------------------------- #


@dataclass
class Step:
    condition: str
    runs: list[str]


@dataclass
class Job:
    key: str
    name: str
    condition: str
    runs: list[str]
    soft_fail: bool
    steps: list[Step] = field(default_factory=list)

    @property
    def context(self) -> str:
        return self.name or self.key


@dataclass
class Workflow:
    triggers: dict[str, list[str]]
    jobs: dict[str, Job]


_JOB_KEY = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*(?:#.*)?$")
#: A `key: value` line, tolerating a leading YAML list marker so both step
#: spellings are read: `- run: cmd` (inline) and `- name: x` / `run: |` (block).
_SCALAR = re.compile(r"^\s*(?:-\s+)?([A-Za-z0-9_-]+):\s*(.*?)\s*(?:#.*)?$")


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _flow_list(value: str) -> list[str]:
    """``[a, b, c]`` -> ['a', 'b', 'c'] (empty for a non-flow scalar)."""
    value = value.strip()
    if not value.startswith("[") or not value.endswith("]"):
        return []
    return [_strip_quotes(part) for part in value[1:-1].split(",") if part.strip()]


def parse_workflow(text: str) -> Workflow:
    """Read the subset of the CI workflow this guard reasons about.

    Not a YAML parser: it reads top-level ``on:`` triggers and the two-space
    job map under ``jobs:``, capturing each job's ``name``/``if``, its ``run``
    bodies, and whether any step is soft-failed.  It is exact enough for this
    repository's workflow and refuses to invent data it cannot see.
    """
    lines = text.splitlines()
    triggers: dict[str, list[str]] = {}
    jobs: dict[str, Job] = {}

    section = ""
    current_job: Job | None = None
    collecting_run: list[str] | None = None
    run_indent = 0
    trigger_event = ""
    current_step: Step | None = None

    for raw in lines:
        if collecting_run is not None:
            stripped = raw.strip()
            indent = len(raw) - len(raw.lstrip())
            if not stripped or indent >= run_indent:
                if stripped:
                    collecting_run.append(raw[run_indent:] if indent >= run_indent else stripped)
                continue
            assert current_job is not None
            current_job.runs.append("\n".join(collecting_run))
            if current_step is not None:
                current_step.runs.append("\n".join(collecting_run))
            collecting_run = None

        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())

        if indent == 0:
            current_job = None
            current_step = None
            trigger_event = ""
            key, _, inline = raw.partition(":")
            key = key.strip()
            section = key if key in ("on", "jobs") else ""
            # `on: [push, pull_request]` — the flow form declares events with no
            # activity types at all, i.e. GitHub's defaults.
            if section == "on":
                for event in _flow_list(inline):
                    triggers[event] = []
            continue

        if section == "on":
            if indent == 2:
                event = raw.strip().rstrip(":").strip()
                rest = raw.split(":", 1)[1].strip() if ":" in raw else ""
                if event.startswith("- "):
                    event = event[2:].strip()
                    triggers.setdefault(event, [])
                    trigger_event = ""
                    continue
                triggers.setdefault(event, [])
                trigger_event = event
                if rest:
                    triggers[event] = _flow_list(rest) or [_strip_quotes(rest)]
            elif indent >= 4 and trigger_event:
                match = _SCALAR.match(raw)
                if match and match.group(1) == "types":
                    triggers[trigger_event] = _flow_list(match.group(2))
            continue

        if section != "jobs":
            continue

        if indent == 2:
            match = _JOB_KEY.match(raw)
            if not match:
                continue
            current_job = Job(key=match.group(1), name="", condition="", runs=[], soft_fail=False)
            current_step = None
            jobs[current_job.key] = current_job
            continue

        if current_job is None:
            continue

        # A `steps:` item begins with `- ` at the steps list indentation.  Each
        # step keeps its own `if:` so the guard can refuse a gate that a step
        # condition switches off (GOV024): the job's `if:` alone is not proof.
        if raw.lstrip().startswith("-"):
            current_step = Step(condition="", runs=[])
            current_job.steps.append(current_step)

        match = _SCALAR.match(raw)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if current_step is None:
            if key == "name" and indent == 4 and not current_job.name:
                current_job.name = _strip_quotes(value)
            elif key == "if" and indent == 4:
                current_job.condition = _strip_quotes(value)
            elif key == "continue-on-error" and _strip_quotes(value).lower() == "true":
                current_job.soft_fail = True
            elif key == "run":
                if value in ("|", "|-", ">", ">-"):
                    collecting_run = []
                    run_indent = indent + 2
                elif value:
                    current_job.runs.append(_strip_quotes(value))
        else:
            if key == "if":
                current_step.condition = _strip_quotes(value)
            elif key == "continue-on-error" and _strip_quotes(value).lower() == "true":
                # A step-level continue-on-error can mask that step's failure
                # (including the guard step's), so it still marks the job soft.
                current_job.soft_fail = True
            elif key == "run":
                if value in ("|", "|-", ">", ">-"):
                    collecting_run = []
                    run_indent = indent + 2
                elif value:
                    current_step.runs.append(_strip_quotes(value))
                    current_job.runs.append(_strip_quotes(value))

    if collecting_run is not None and current_job is not None:
        current_job.runs.append("\n".join(collecting_run))
        if current_step is not None:
            current_step.runs.append("\n".join(collecting_run))

    return Workflow(triggers=triggers, jobs=jobs)


# --------------------------------------------------------------------------- #
# offline plane — the repository itself
# --------------------------------------------------------------------------- #


#: A condition naming an event by equality, e.g. ``github.event_name == 'push'``.
_EVENT_EQUALITY = re.compile(r"github\.event_name\s*==\s*['\"]([A-Za-z_]+)['\"]")

#: The only forms that *prove* a job selects the pull_request event.  Each one
#: compares the literal against ``github.event_name`` itself, so a
#: ``pull_request`` appearing anywhere else — a variable's value, a bare string,
#: a comment, an unrelated ``contains`` haystack — is deliberately not matched.
#: Quotation style is accepted either way because GitHub allows both.
_PR_PRESERVING = re.compile(
    r"github\.event_name\s*==\s*['\"]pull_request['\"]"
    r"|['\"]pull_request['\"]\s*==\s*github\.event_name"
    r"|contains\s*\(\s*github\.event_name\s*,\s*['\"]pull_request['\"]\s*\)",
)


def classify_pr_condition(condition: str) -> tuple[str, str]:
    """Decide whether a job's ``if:`` still lets it run on ``pull_request``.

    Three outcomes, not two.  A binary check is exactly what produced the false
    green this replaces: ``if: false`` and ``if: github.event_name == 'push'``
    both silently reported ``ok``, so the merge-base gate — and with it law R18 —
    could be switched off without the guard noticing.  A condition we cannot
    *prove* preserves ``pull_request`` is reported ``unknown``, which the Report
    turns into BLOCKED rather than VERIFIED.  Failing closed is the point: an
    undecidable condition must never count as a pass.

    The condition is split on its **top-level** ``||`` then ``&&`` operators and
    each term is classified on its own.  Matching a whitelisted form anywhere in
    the text was the earlier hole: a blocking sibling in an ``&&`` chain went
    unexamined, so ``... == 'pull_request' && vars.ENABLE == 'yes'`` and even the
    impossible ``... == 'pull_request' && ... == 'push'`` read as ``ok``.
    """
    cond = (condition or "").strip()
    if not cond:
        return "ok", "has no condition, so it runs on every subscribed event"
    return _classify_boolean(cond.lower(), condition)


def _split_top_level(expr: str, op: str) -> list[str]:
    """Split ``expr`` on ``op`` occurrences outside parentheses or quotes.

    ``&&`` inside ``'a && b'`` is data, and one nested in ``( ... )`` belongs to
    a deeper precedence level; neither is a top-level operator.
    """
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    i = 0
    while i < len(expr):
        ch = expr[i]
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and expr.startswith(op, i):
            parts.append(expr[start:i])
            i += len(op)
            start = i
            continue
        i += 1
    parts.append(expr[start:])
    return parts


#: Status functions that are true in an ordinary run and so neither add nor
#: remove ``pull_request``: a benign extra conjunct.  ``cancelled()`` is NOT one
#: of them — it is true only when the workflow run was *cancelled*, so a job
#: gated on it never runs on a normal pull_request execution.  ``failure()`` is
#: likewise false on an ordinary run.  Both must fail closed to ``unknown``.
_NEUTRAL_FUNCTIONS = re.compile(r"^(?:always|success)\s*\(\s*\)$")


def _strip_outer_parens(text: str) -> str:
    """Remove parentheses that wrap the *whole* term, e.g. ``(a || b)`` → ``a || b``.

    Only a pair that encloses the entire term is stripped; an inner group such as
    ``contains(...)`` or ``(a) && b`` is left intact, so a function call is never
    mistaken for a redundant wrapper.
    """
    t = text.strip()
    while len(t) >= 2 and t.startswith("(") and t.endswith(")"):
        depth = 0
        wraps_all = True
        for index, ch in enumerate(t):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and index != len(t) - 1:
                    wraps_all = False
                    break
        if not wraps_all:
            break
        t = t[1:-1].strip()
    return t


def _term_runs_on_pr(term: str) -> str:
    """Classify one conjunction term: ``ok``, ``unknown`` or ``violation``."""
    text = _strip_outer_parens(term)
    if not text:
        # A leading, trailing or doubled operator is malformed input, and a
        # malformed condition is not a proof of anything: fail closed.
        return "unknown"
    low = text.lower()
    unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", " ", low).strip()
    # A term that can never be true can never run.
    if unquoted in ("false", "!true"):
        return "violation"
    # `!= 'pull_request'` / `!contains(...)` is a proven exclusion, before the
    # general negation test which would otherwise only say ``unknown``.
    if re.search(r"!=\s*['\"]?pull_request", low) or re.search(
        r"!\s*contains\s*\(|\bnot\s+contains\s*\(", low
    ):
        return "violation"
    if _NEUTRAL_FUNCTIONS.match(unquoted):
        return "ok"
    # Any other negation means we cannot prove this term keeps pull_request: e.g.
    # `!(github.event_name == 'pull_request')` names it and still never runs.
    if "!" in unquoted.replace("!=", " ") or re.search(r"\bnot\b", unquoted):
        return "unknown"
    # A comparison that positively selects pull_request is evidence.
    if _PR_PRESERVING.search(low):
        return "ok"
    # A term naming a *different* event by equality never runs for pull_request.
    events = _EVENT_EQUALITY.findall(low)
    if events and "pull_request" not in events:
        return "violation"
    # Anything else — a mention, a variable, an undecidable negation — is not a
    # proof that this term keeps pull_request, so it fails closed.
    return "unknown"


def _classify_boolean(low: str, original: str) -> tuple[str, str]:
    """Combine per-term verdicts over the top-level ``||`` then ``&&`` operators."""
    or_groups = _split_top_level(low, "||")
    if any(not group.strip() for group in or_groups):
        # A leading, trailing or doubled `||` is malformed input; the "any branch
        # may pass" rule must not let a well-formed branch launder it.
        return (
            "unknown",
            f"condition {original!r} has an empty branch — malformed, never a pass",
        )
    if len(or_groups) > 1:
        verdicts = [_combine_and(group) for group in or_groups]
        # A disjunction runs if *any* branch runs.
        if "ok" in verdicts:
            return "ok", f"condition {original!r} has a branch that runs on pull_request"
        if "unknown" in verdicts:
            return (
                "unknown",
                f"condition {original!r} has no branch provably running on pull_request — "
                "recorded as BLOCKED, never as a pass",
            )
        return "violation", f"condition {original!r} never runs on pull_request"
    verdict = _combine_and(low)
    if verdict == "ok":
        return "ok", f"condition {original!r} runs on pull_request under every term"
    if verdict == "violation":
        return "violation", f"condition {original!r} can never run on pull_request"
    return (
        "unknown",
        f"condition {original!r} has a term that cannot be shown to preserve "
        "pull_request — recorded as BLOCKED, never as a pass",
    )


def _combine_and(expr: str) -> str:
    """A conjunction runs only if every term runs; violation dominates."""
    verdicts = [_term_runs_on_pr(term) for term in _split_top_level(expr, "&&")]
    if "violation" in verdicts:
        return "violation"
    if "unknown" in verdicts:
        return "unknown"
    return "ok"


def check_offline(root: Path) -> Report:
    report = Report("offline")
    path = root / ".github" / "workflows" / "ci.yml"
    try:
        workflow = parse_workflow(path.read_text(encoding="utf-8"))
    except OSError as exc:
        report.add(
            "GOV001", "unknown", f"cannot read the CI workflow: {exc}", ".github/workflows/ci.yml"
        )
        return report

    if not workflow.jobs:
        report.add(
            "GOV002",
            "violation",
            "the CI workflow parsed to zero jobs — the reader and the file disagree",
            ".github/workflows/ci.yml",
        )
        return report
    report.add(
        "GOV002",
        "ok",
        f"parsed {len(workflow.jobs)} CI job(s)",
        ", ".join(sorted(workflow.jobs)),
    )

    by_context: dict[str, list[Job]] = {}
    for job in workflow.jobs.values():
        by_context.setdefault(job.context, []).append(job)

    # GOV010 — a required context that no job produces is a permanently red
    # merge (fail-closed, but governance drift); two jobs producing one context
    # make the requirement ambiguous.
    for context in REQUIRED_CONTEXTS:
        producers = by_context.get(context, [])
        if not producers:
            report.add(
                "GOV010",
                "violation",
                f"required check {context!r} is produced by no CI job — GitHub would "
                "require a context that can never report",
                "scripts/governance_guard.py REQUIRED_CONTEXTS vs .github/workflows/ci.yml",
            )
        elif len(producers) > 1:
            report.add(
                "GOV010",
                "violation",
                f"required check {context!r} is produced by {len(producers)} jobs: "
                + ", ".join(j.key for j in producers),
                ".github/workflows/ci.yml",
            )
        else:
            report.add(
                "GOV010",
                "ok",
                f"required check {context!r} is produced by job {producers[0].key!r}",
                ".github/workflows/ci.yml",
            )

    guard = (by_context.get(BASE_GUARD_CONTEXT) or [None])[0]
    if guard is None:
        report.add(
            "GOV011",
            "violation",
            f"no job produces the required context {BASE_GUARD_CONTEXT!r}",
            ".github/workflows/ci.yml",
        )
        return report

    # GOV020 — the required check must be the job that *runs* the guard.  A job
    # that merely exists reports green without deciding anything.
    body = "\n".join(guard.runs)
    if BASE_GUARD_SCRIPT not in body:
        report.add(
            "GOV020",
            "violation",
            f"job {guard.key!r} produces the required merge-base context but never executes "
            f"{BASE_GUARD_SCRIPT} — the required check would pass without deciding anything",
            f".github/workflows/ci.yml job {guard.key}",
        )
    elif BASE_GUARD_SUBCOMMAND not in body:
        report.add(
            "GOV020",
            "violation",
            f"job {guard.key!r} runs {BASE_GUARD_SCRIPT} without the "
            f"{BASE_GUARD_SUBCOMMAND!r} subcommand",
            f".github/workflows/ci.yml job {guard.key}",
        )
    else:
        report.add(
            "GOV020",
            "ok",
            f"job {guard.key!r} executes {BASE_GUARD_SCRIPT} {BASE_GUARD_SUBCOMMAND}",
            f".github/workflows/ci.yml job {guard.key}",
        )

    # GOV021 — the event and the base must both be passed, or the decision is
    # made on absent input (which R18 treats as unreadable, exit 2).
    for flag in ("--event", "--base"):
        if flag not in body:
            report.add(
                "GOV021",
                "violation",
                f"job {guard.key!r} invokes the guard without {flag}",
                f".github/workflows/ci.yml job {guard.key}",
            )
    if "--event" in body and "--base" in body:
        report.add(
            "GOV021",
            "ok",
            f"job {guard.key!r} passes --event and --base",
            f".github/workflows/ci.yml job {guard.key}",
        )

    # GOV022 — a soft-failed gate is not a gate.  This covers every job that
    # produces a required context, not only the merge-base one.
    for context in REQUIRED_CONTEXTS:
        for producer in by_context.get(context, []):
            if producer.soft_fail:
                report.add(
                    "GOV022",
                    "violation",
                    f"job {producer.key!r} produces required check {context!r} but carries "
                    "continue-on-error: true",
                    f".github/workflows/ci.yml job {producer.key}",
                )
            else:
                report.add(
                    "GOV022",
                    "ok",
                    f"job {producer.key!r} (required check {context!r}) is blocking",
                    f".github/workflows/ci.yml job {producer.key}",
                )

    # GOV023 — the job must actually run for pull requests.
    severity, reason = classify_pr_condition(guard.condition)
    report.add(
        "GOV023",
        severity,
        f"job {guard.key!r} {reason}",
        f".github/workflows/ci.yml job {guard.key}: if: {guard.condition or '(none)'}",
    )

    # GOV024 — the *step* that runs the guard must also run for pull requests.
    # A job-level `if:` is not enough: a step carrying `if: false` (or an
    # event-exclusive condition) is skipped while the job stays green, so the
    # required context reports success without the merge-base decision ever being
    # made.  Reproduced pre-fix: GOV020/GOV021 see the command in a skipped step
    # and GOV023 examines only the job condition, so all three read ok.
    guard_steps = [s for s in guard.steps if any(BASE_GUARD_SCRIPT in r for r in s.runs)]
    if not guard_steps:
        report.add(
            "GOV024",
            "ok",
            f"job {guard.key!r} runs {BASE_GUARD_SCRIPT} inline in the job (no step condition "
            "to switch it off)",
            f".github/workflows/ci.yml job {guard.key}",
        )
    else:
        for step in guard_steps:
            step_severity, step_reason = classify_pr_condition(step.condition)
            report.add(
                "GOV024",
                step_severity,
                f"the step in job {guard.key!r} that runs {BASE_GUARD_SCRIPT} {step_reason}",
                f".github/workflows/ci.yml job {guard.key}: step if: {step.condition or '(none)'}",
            )

    # GOV030 — retarget protection.  Moving a PR's base is an `edited` event;
    # without it the previous green check is reused and R18 is bypassed.
    github_default_pr_types = ("opened", "synchronize", "reopened")
    pr_types = workflow.triggers.get("pull_request")
    if pr_types is None:
        report.add(
            "GOV030",
            "violation",
            "the CI workflow does not subscribe to pull_request at all",
            ".github/workflows/ci.yml on:",
        )
    else:
        # An empty list is the flow form (`on: [push, pull_request]`): the
        # workflow subscribes with GitHub's default activity types.
        declared = tuple(pr_types) or github_default_pr_types
        missing = [t for t in BASE_CHANGING_PR_TYPES if t not in declared]
        if missing:
            report.add(
                "GOV030",
                "violation",
                "retargeting a PR's base re-runs nothing: pull_request is missing activity "
                "type(s) " + ", ".join(missing) + " — an already-green check is reused "
                "against the new base (#143 shape)",
                f"on: pull_request types={list(declared)}"
                + (" (GitHub defaults)" if not pr_types else ""),
            )
        else:
            report.add(
                "GOV030",
                "ok",
                "every base-changing PR activity type re-runs the workflow",
                f"on: pull_request types={list(declared)}",
            )

    return report


# --------------------------------------------------------------------------- #
# live plane — GitHub itself
# --------------------------------------------------------------------------- #


#: Sub-settings whose *absence* is a violation and whose unreadability is BLOCKED.
#:
#: Readability alone is not assurance.  Recording ``ok`` for any HTTP 200 was a
#: security false-green, reproduced with an all-200 fixture whose policy allowed
#: force-pushes, allowed deletion, required zero approvals and had ``strict``
#: false — the guard answered VERIFIED.  Each entry therefore declares the value
#: semantics it must satisfy, taken from this repository's own governance
#: contract (GOVERNANCE_ENFORCEMENT.md acceptance items C/D/E/J/K/L), not from a
#: guess about what GitHub usually returns.
#:
#: A field that is missing, null or of the wrong type is *ambiguous*: it becomes
#: ``unknown`` (BLOCKED), never ``ok``.  Only a readable value that contradicts
#: the policy is a ``violation``.
@dataclass(frozen=True)
class DetailPolicy:
    endpoint: str
    code: str
    label: str
    #: (field, tri-state evaluator, human expectation)
    expectations: tuple[tuple[str, Callable[[object], str], str], ...]


#: A tri-state verdict for one declared value.  ``unknown`` is not a synonym for
#: "not yet checked": it means the response was readable but the value is of a
#: shape GitHub does not send, so the setting cannot be decided either way and
#: must not be reported as satisfied.
def _flag_is(want: bool) -> Callable[[object], str]:
    def check(value: object) -> str:
        if not isinstance(value, bool):
            return "unknown"  # wrong type: ambiguous, not a contradiction
        return "ok" if value is want else "violation"

    return check


def _approvals_at_least_one(value: object) -> str:
    # ``isinstance(True, int)`` is True in Python, so a boolean must be rejected
    # explicitly or ``required_approving_review_count: true`` would read as 1.
    if isinstance(value, bool) or not isinstance(value, int):
        return "unknown"  # wrong type: ambiguous, not a contradiction
    return "ok" if value >= 1 else "violation"


_DETAIL_ENDPOINTS: tuple[DetailPolicy, ...] = (
    DetailPolicy(
        "required_pull_request_reviews",
        "GOV050",
        "review requirement (>=1 approval, stale reviews dismissed)",
        (
            ("required_approving_review_count", _approvals_at_least_one, ">= 1"),
            ("dismiss_stale_reviews", _flag_is(True), "true"),
        ),
    ),
    DetailPolicy(
        "required_conversation_resolution",
        "GOV051",
        "conversation resolution requirement",
        (("enabled", _flag_is(True), "true"),),
    ),
    DetailPolicy(
        "allow_force_pushes",
        "GOV052",
        "force-push prohibition",
        (("enabled", _flag_is(False), "false"),),
    ),
    DetailPolicy(
        "allow_deletions",
        "GOV053",
        "branch-deletion prohibition",
        (("enabled", _flag_is(False), "false"),),
    ),
    DetailPolicy(
        "required_status_checks",
        "GOV054",
        "required-status-check detail (strict / up-to-date branch)",
        (("strict", _flag_is(True), "true"),),
    ),
)


def _api_get(url: str, token: str | None) -> tuple[object | None, str | None, int]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "nexus-governance-guard",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8")), None, response.status
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}: {exc.reason}", exc.code
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}", 0


def check_live(repo: str, branch: str, token: str | None) -> Report:
    report = Report("live")
    base = f"https://api.github.com/repos/{repo}"

    data, err, status = _api_get(f"{base}/branches/{branch}", token)
    if err or not isinstance(data, dict):
        report.add(
            "GOV040",
            "unknown",
            f"cannot read {branch} branch state ({err}) — governance is unverifiable, not green",
            f"GET {base}/branches/{branch}",
        )
        return report

    if data.get("protected") is not True:
        report.add(
            "GOV040",
            "violation",
            f"branch {branch!r} is NOT protected — no GitHub-side gate exists",
            f"GET {base}/branches/{branch} -> protected={data.get('protected')!r}",
        )
        return report
    report.add("GOV040", "ok", f"branch {branch!r} is protected", f"GET {base}/branches/{branch}")

    protection = data.get("protection") or {}
    checks = protection.get("required_status_checks") or {}
    live_contexts = tuple(checks.get("contexts") or [])
    enforcement = checks.get("enforcement_level")

    if not live_contexts:
        report.add(
            "GOV041",
            "violation",
            f"branch {branch!r} has protection but no required status checks",
            f"protection.required_status_checks={checks!r}",
        )
    else:
        missing = [c for c in REQUIRED_CONTEXTS if c not in live_contexts]
        extra = [c for c in live_contexts if c not in REQUIRED_CONTEXTS]
        if missing:
            report.add(
                "GOV041",
                "violation",
                "GitHub does not require: " + ", ".join(missing),
                f"live contexts={list(live_contexts)}",
            )
        elif extra:
            # Not a bypass, but it is drift: a required context this repository
            # does not declare either blocks every merge or is a renamed job.
            report.add(
                "GOV041",
                "violation",
                "GitHub requires undeclared context(s): " + ", ".join(extra),
                f"live contexts={list(live_contexts)}",
            )
        else:
            report.add(
                "GOV041",
                "ok",
                "live required contexts equal the declared policy",
                f"live contexts={list(live_contexts)}",
            )

    if enforcement and enforcement != "everyone":
        report.add(
            "GOV042",
            "violation",
            f"required checks are enforced for {enforcement!r}, not everyone",
            f"enforcement_level={enforcement!r}",
        )
    elif enforcement:
        report.add("GOV042", "ok", "required checks apply to everyone", f"{enforcement}")
    else:
        report.add(
            "GOV042", "unknown", "enforcement_level is absent from the live payload", "protection"
        )

    # GOV043 — rulesets are the *other* GitHub mechanism that can enforce this
    # policy, and unlike the protection sub-settings they ARE readable with a
    # metadata-only token.  Recording them is real live evidence rather than a
    # guess: an empty list means classic branch protection is the only
    # enforcement path, so the sub-settings above are the whole story.  A non-empty
    # list means a second mechanism exists that this guard does not yet evaluate,
    # which is governance drift and must not read as green.
    rules, rules_err, _ = _api_get(f"{base}/rules/branches/{branch}", token)
    if rules_err:
        report.add(
            "GOV043",
            "unknown",
            f"ruleset metadata unreadable ({rules_err}) — recorded as BLOCKED, never a pass",
            f"GET {base}/rules/branches/{branch}",
        )
    elif isinstance(rules, list) and not rules:
        report.add(
            "GOV043",
            "ok",
            f"no ruleset applies to {branch!r} — classic branch protection is the only "
            "enforcement path",
            f"GET {base}/rules/branches/{branch} -> []",
        )
    else:
        report.add(
            "GOV043",
            "violation",
            f"{len(rules) if isinstance(rules, list) else 'an unknown number of'} ruleset "
            f"rule(s) apply to {branch!r}; this guard evaluates classic branch protection "
            "only, so the enforced policy is not fully covered",
            f"GET {base}/rules/branches/{branch}",
        )

    # Sub-settings: each is either verified, contradicted, or unreadable.  An
    # unreadable sub-setting can never contribute to a VERIFIED verdict.
    for policy in _DETAIL_ENDPOINTS:
        url = f"{base}/branches/{branch}/protection/{policy.endpoint}"
        payload, err, status = _api_get(url, token)
        source = f"GET {url}"
        if err or not isinstance(payload, dict):
            # The dedicated endpoint is unreadable: 403 with a metadata-only token,
            # and the allow_*/conversation endpoints answer a misleading 404
            # "Branch not found", which must never be read as "the setting is off".
            # Before giving up, fall back to the same-named field inside the
            # `protection` object that GET /branches/{branch} already returned — a
            # second, repository-supported source for the same truth.  It is
            # evaluated with the exact same predicates, so it can only produce the
            # same three outcomes; an absent or null field there stays BLOCKED.
            fallback = protection.get(policy.endpoint)
            if isinstance(fallback, dict):
                payload = fallback
                status = 200
                source = f"GET {base}/branches/{branch} -> protection.{policy.endpoint}"
            elif err:
                report.add(
                    policy.code,
                    "unknown",
                    f"{policy.label}: source unreadable ({err}) and absent from the branch "
                    "protection object — recorded as BLOCKED, never as a pass",
                    source,
                )
                continue
            else:
                report.add(
                    policy.code,
                    "unknown",
                    f"{policy.label}: HTTP {status} returned {type(payload).__name__}, not "
                    "an object — ambiguous, so BLOCKED rather than a pass",
                    source,
                )
                continue
        # Every declared expectation must hold on the *value*, not on readability.
        for field_name, holds, expectation in policy.expectations:
            if field_name not in payload or payload[field_name] is None:
                report.add(
                    policy.code,
                    "unknown",
                    f"{policy.label}: {field_name} is absent or null — ambiguous, so "
                    "BLOCKED rather than a pass",
                    source,
                )
                continue
            actual = payload[field_name]
            verdict = holds(actual)
            if verdict == "ok":
                report.add(
                    policy.code,
                    "ok",
                    f"{policy.label}: {field_name} is {actual!r} (expected {expectation})",
                    f"HTTP {status} {json.dumps(payload)[:160]} via {source}",
                )
            elif verdict == "violation":
                report.add(
                    policy.code,
                    "violation",
                    f"{policy.label}: {field_name} is {actual!r}, expected {expectation}",
                    f"HTTP {status} {json.dumps(payload)[:160]} via {source}",
                )
            else:
                report.add(
                    policy.code,
                    "unknown",
                    f"{policy.label}: {field_name} is {type(actual).__name__} {actual!r}, "
                    f"not a decidable value (expected {expectation}) — BLOCKED, never a pass",
                    f"HTTP {status} {json.dumps(payload)[:160]} via {source}",
                )

    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _default_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", dest="as_json", action="store_true")
    common.add_argument("--root", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "check-offline", help="verify the repository plane (no network)", parents=[common]
    )

    p_live = sub.add_parser(
        "check-live", help="verify the GitHub plane (network)", parents=[common]
    )
    p_live.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY") or "")
    p_live.add_argument("--branch", default=DEFAULT_BRANCH)
    p_live.add_argument("--token", default=None)

    sub.add_parser("plan", help="print the declared policy", parents=[common])

    args = parser.parse_args(argv)
    root = _default_root() if not getattr(args, "root", None) else Path(args.root)

    if args.command == "plan":
        print(
            json.dumps(
                {
                    "required_contexts": list(REQUIRED_CONTEXTS),
                    "base_guard_context": BASE_GUARD_CONTEXT,
                    "base_guard_invocation": f"{BASE_GUARD_SCRIPT} {BASE_GUARD_SUBCOMMAND}",
                    "base_changing_pr_types": list(BASE_CHANGING_PR_TYPES),
                    "verdict_vocabulary": ["VERIFIED", "BLOCKED", "VIOLATION"],
                },
                indent=2,
            )
        )
        return EXIT_VERIFIED

    reports = []
    if args.command == "check-offline":
        reports.append(check_offline(root))
    else:
        reports.append(check_offline(root))
        token = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not args.repo:
            blocked = Report("live")
            blocked.add(
                "GOV043",
                "unknown",
                "no --repo and no GITHUB_REPOSITORY: the live plane cannot be addressed",
                "argv",
            )
            reports.append(blocked)
        else:
            reports.append(check_live(args.repo, args.branch, token))

    combined = Report("+".join(r.scope for r in reports))
    for report in reports:
        combined.findings.extend(report.findings)

    if args.as_json:
        print(json.dumps(combined.to_dict(), indent=2))
    else:
        for report in reports:
            print(report.format_text())
        print(f"governance-guard: combined verdict={combined.verdict()}")
    return combined.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
