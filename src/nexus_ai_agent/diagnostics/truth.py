"""Living-truth diagnostics for the NEXUS repository.

The repository already refuses to *silently* lie in the runtime paths (fail-closed
verdicts, typed refusals, provenance passports).  The remaining honesty gap is the
*repository itself*: a version can drift between four declarations, a document can
name a test that no longer exists, an architecture law can point at a deleted
guard, a README can claim a capability is "simulated" while the code says
otherwise, and none of that fails a check.

:mod:`nexus_ai_agent.diagnostics.truth` closes that gap.  It is the single
"repo truth" engine: a pure-stdlib scanner that produces one :class:`TruthReport`
whose every :class:`Finding` carries a stable code, a severity, and a concrete
witness (a ``file:line`` or a value).  The same report object drives the CLI
(``python -m nexus_ai_agent.diagnostics.truth``), the unit tests and the
architecture gate, so there is exactly one authority for "is the tree telling the
truth".

Design rules (enforced by ``tests/architecture/test_repo_truth_consistency.py``):

* **stdlib only** — it must run in the fast rail, before ``pip install``.
* **no finding without a witness** — every finding names a file (and usually a
  line) or the exact conflicting values.
* **checks are data** — each check is registered in :data:`CHECKS`, so a removed
  check is observable (the gate asserts the registry is non-empty and each check
  fires on an injected defect).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- #
# report model
# --------------------------------------------------------------------------- #

SEVERITIES = ("error", "warning", "info")


@dataclass(frozen=True)
class Finding:
    """One truth defect.  ``evidence`` is mandatory: a finding must be checkable."""

    code: str
    severity: str
    summary: str
    evidence: str

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"illegal severity {self.severity!r}")
        if not self.evidence:
            raise ValueError(f"finding {self.code} has no evidence")

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "summary": self.summary,
            "evidence": self.evidence,
        }


@dataclass
class TruthReport:
    root: Path
    checks_run: tuple[str, ...] = ()
    findings: tuple[Finding, ...] = ()

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "warning")

    def exit_code(self, fail_on: str = "error") -> int:
        """0 when the tree is clean at ``fail_on``, 1 when it is not, 2 on misuse."""
        if fail_on not in SEVERITIES:
            return 2
        threshold = SEVERITIES.index(fail_on)
        offending = [f for f in self.findings if SEVERITIES.index(f.severity) <= threshold]
        return 1 if offending else 0

    def to_dict(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "checks_run": list(self.checks_run),
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "findings": [f.to_dict() for f in self.findings],
        }

    def format_text(self) -> str:
        lines = [f"truth doctor — {self.root}", f"checks: {', '.join(self.checks_run) or '(none)'}"]
        if not self.findings:
            lines.append("OK — no findings")
            return "\n".join(lines)
        for f in self.findings:
            lines.append(f"[{f.severity.upper()}] {f.code} {f.summary}")
            lines.append(f"    witness: {f.evidence}")
        lines.append(f"{len(self.errors)} error(s), {len(self.warnings)} warning(s)")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #

_IGNORED_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".ruff_cache"}


def _tracked_files(root: Path, suffix: str) -> Iterable[Path]:
    """Yield files under *root* with *suffix*, skipping VCS/build directories."""
    for path in root.rglob(f"*{suffix}"):
        if any(part in _IGNORED_DIRS for part in path.parts):
            continue
        yield path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _line_of(text: str, needle: str) -> int:
    """1-based line number of the first occurrence of *needle* (or 1)."""
    idx = text.find(needle)
    return text.count("\n", 0, idx) + 1 if idx >= 0 else 1


def symbol_exists(path: Path, symbol: str) -> bool:
    """True when *symbol* (dotted ``Class.method`` or bare name) is defined in *path*.

    A syntax error is treated as "unknown", never as "present": a claim that a
    symbol exists must be *provable*.
    """
    try:
        tree = ast.parse(_read(path))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return False
    parts = symbol.split(".")
    node: ast.AST = tree
    for part in parts:
        body = getattr(node, "body", None)
        found: ast.AST | None = None
        for child in body or []:
            name = getattr(child, "name", None)
            if name == part:
                found = child
                break
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name) and target.id == part:
                        found = child
                        break
            if found is not None:
                break
        if found is None:
            return False
        node = found
    return True


# --------------------------------------------------------------------------- #
# check 1 — release version lock-step across every declaration
# --------------------------------------------------------------------------- #

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_RELEASED_HEADING = re.compile(r"^##[ \t]*\[(\d+\.\d+\.\d+)\]", re.MULTILINE)
_README_VERSION = re.compile(r"^[> \t]*\*\*Version:[ \t]*v?(\d+\.\d+\.\d+)\*\*", re.MULTILINE)
_PROJECT_TABLE = re.compile(r"^\[project\][ \t]*$", re.MULTILINE)
_ANY_TABLE = re.compile(r"^\[[^\[\]]+\][ \t]*$", re.MULTILINE)
_PROJECT_VERSION = re.compile(r'^version[ \t]*=[ \t]*"([^"]+)"[ \t]*$', re.MULTILINE)


def _project_version(pyproject_text: str) -> str | None:
    start = _PROJECT_TABLE.search(pyproject_text)
    if start is None:
        return None
    rest = pyproject_text[start.end() :]
    end = _ANY_TABLE.search(rest)
    body = rest if end is None else rest[: end.start()]
    match = _PROJECT_VERSION.search(body)
    return match.group(1) if match else None


def _continuum_version(root: Path) -> tuple[str, str] | None:
    path = root / ".nexus" / "continuum.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(_read(path))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return (f"{path.relative_to(root)}", "<unparsable>")
    for key in ("release", "version"):
        value = data.get(key)
        if isinstance(value, str) and _SEMVER.match(value):
            return (f"{path.relative_to(root)}:{key}", value)
    return None


def check_version_lockstep(root: Path) -> list[Finding]:
    """``VERSION == pyproject == newest CHANGELOG heading == README banner``."""
    findings: list[Finding] = []
    declarations: list[tuple[str, str]] = []

    version_path = root / "VERSION"
    if version_path.is_file():
        declarations.append(("VERSION", _read(version_path).strip()))
    else:
        findings.append(Finding("TRUTH001", "error", "VERSION file is missing", "VERSION"))

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        got = _project_version(_read(pyproject))
        if got is None:
            findings.append(
                Finding(
                    "TRUTH001", "error", "pyproject [project].version not found", "pyproject.toml"
                )
            )
        else:
            declarations.append(("pyproject.toml", got))
    else:
        findings.append(Finding("TRUTH001", "error", "pyproject.toml is missing", "pyproject.toml"))

    changelog = root / "CHANGELOG.md"
    if changelog.is_file():
        match = _RELEASED_HEADING.search(_read(changelog))
        if match is None:
            findings.append(
                Finding(
                    "TRUTH001",
                    "error",
                    "CHANGELOG has no released version heading",
                    "CHANGELOG.md",
                )
            )
        else:
            declarations.append(("CHANGELOG.md", match.group(1)))

    readme = root / "README.md"
    if readme.is_file():
        match = _README_VERSION.search(_read(readme))
        if match is None:
            findings.append(
                Finding("TRUTH001", "error", "README has no Version banner", "README.md")
            )
        else:
            declarations.append(("README.md", match.group(1)))

    continuum = _continuum_version(root)
    if continuum is not None:
        declarations.append(continuum)

    for name, value in declarations:
        if not _SEMVER.match(value):
            findings.append(
                Finding(
                    "TRUTH002", "error", f"{name} is not a semantic version", f"{name}={value!r}"
                )
            )

    distinct = {value for _, value in declarations}
    if len(distinct) > 1:
        rendered = ", ".join(f"{name}={value}" for name, value in declarations)
        findings.append(
            Finding("TRUTH003", "error", "release version declarations disagree", rendered)
        )
    return findings


# --------------------------------------------------------------------------- #
# check 2 — docs index + relative link integrity
# --------------------------------------------------------------------------- #

_DOC_LINK = re.compile(r"\]\(([^)#]+\.md)\)")


def check_docs_index(root: Path) -> list[Finding]:
    """Every ``docs/**/*.md`` is indexed in ``docs/README.md`` and every link resolves."""
    findings: list[Finding] = []
    docs_dir = root / "docs"
    index = docs_dir / "README.md"
    if not index.is_file():
        return [Finding("TRUTH010", "error", "docs/README.md index is missing", "docs/README.md")]

    index_text = _read(index)
    linked = set(_DOC_LINK.findall(index_text))

    for doc in sorted(_tracked_files(docs_dir, ".md")):
        rel = doc.relative_to(docs_dir).as_posix()
        if rel == "README.md":
            continue
        if rel not in linked:
            findings.append(
                Finding(
                    "TRUTH010", "error", "document is not indexed in docs/README.md", f"docs/{rel}"
                )
            )

    for link in sorted(linked):
        target = (docs_dir / link).resolve()
        if not target.is_file():
            findings.append(
                Finding(
                    "TRUTH011",
                    "error",
                    "docs/README.md links a missing document",
                    f"docs/README.md -> {link}",
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# check 3 — every architecture law names a resolvable test
# --------------------------------------------------------------------------- #

_LAW_ROW = re.compile(r"^\|\s*(R\d+)\s*\|(.*)\|(.*)\|\s*$", re.MULTILINE)
#: A test reference inside backticks, optionally path-prefixed, optionally ``::symbol``.
_TEST_REF = re.compile(r"`((?:[A-Za-z0-9_./-]+/)?test_[A-Za-z0-9_]+\.py)(?:::([A-Za-z0-9_]+))?`")


def _module_map_section(root: Path) -> str:
    path = root / "docs" / "architecture" / "MODULE_MAP.md"
    if not path.is_file():
        return ""
    text = _read(path)
    marker = text.find("## 3")
    if marker < 0:
        return ""
    tail = text[marker:]
    end = tail.find("**Legacy baseline")
    return tail if end < 0 else tail[:end]


def check_law_test_resolution(root: Path) -> list[Finding]:
    """Each MODULE_MAP law must name at least one test that exists on disk.

    This is the machine enforcement of the AGENTS.md §7 rule "every boundary rule
    must name its enforcing test".  A law whose test is renamed or deleted, or a
    law that names no test at all, becomes a failing check instead of prose drift.
    """
    section = _module_map_section(root)
    if not section:
        return [
            Finding(
                "TRUTH020",
                "error",
                "MODULE_MAP.md §3 (boundary laws) is missing or unparsable",
                "docs/architecture/MODULE_MAP.md",
            )
        ]
    findings: list[Finding] = []
    tests_root = root / "tests"
    seen = 0
    for match in _LAW_ROW.finditer(section):
        law, _body, tests_cell = match.group(1), match.group(2), match.group(3)
        refs = _TEST_REF.findall(tests_cell)
        if not refs:
            findings.append(
                Finding(
                    "TRUTH021",
                    "error",
                    f"law {law} names no enforcing test",
                    f"MODULE_MAP.md {law}",
                )
            )
            continue
        seen += 1
        for filename, symbol in refs:
            if "/" in filename:
                direct = root / filename
                candidates = [direct] if direct.is_file() else []
            else:
                candidates = list(tests_root.rglob(filename))
            if not candidates:
                findings.append(
                    Finding(
                        "TRUTH020",
                        "error",
                        f"law {law} names a test file that does not exist",
                        f"MODULE_MAP.md {law} -> {filename}",
                    )
                )
                continue
            if symbol and not any(symbol_exists(c, symbol) for c in candidates):
                findings.append(
                    Finding(
                        "TRUTH020",
                        "error",
                        f"law {law} names a test symbol that does not exist",
                        f"MODULE_MAP.md {law} -> {filename}::{symbol}",
                    )
                )
    if seen == 0:
        findings.append(
            Finding(
                "TRUTH022",
                "error",
                "MODULE_MAP §3 parsed zero law rows",
                "docs/architecture/MODULE_MAP.md",
            )
        )
    return findings


# --------------------------------------------------------------------------- #
# check 4 — machine-readable documentation claims resolve to code
# --------------------------------------------------------------------------- #

#: A doc may assert that a symbol EXISTS::
#:     <!-- truth:file=src/nexus_ai_agent/cli.py symbol=run_bot -->
#: or that a symbol is ABSENT (the "no fake feature" direction)::
#:     <!-- truth-absent:file=src/nexus_ai_agent/agents/planner_agent.py symbol=real_planner -->
_CLAIM = re.compile(
    r"<!--\s*(truth|truth-absent):file=(?P<file>\S+)(?:\s+symbol=(?P<symbol>[A-Za-z0-9_.]+))?\s*-->"
)


def check_claim_witnesses(root: Path) -> list[Finding]:
    """Every ``truth:`` / ``truth-absent:`` marker in the docs must resolve to the tree."""
    findings: list[Finding] = []
    candidates = list(_tracked_files(root / "docs", ".md"))
    for extra in ("README.md", "AGENTS.md"):
        p = root / extra
        if p.is_file():
            candidates.append(p)

    for doc in candidates:
        text = _read(doc)
        for match in _CLAIM.finditer(text):
            kind = match.group(1)
            rel = match.group("file")
            symbol = match.group("symbol")
            line = _line_of(text, match.group(0))
            target = root / rel
            if not target.is_file():
                findings.append(
                    Finding(
                        "TRUTH023",
                        "error",
                        f"{kind} claim names a file that does not exist",
                        f"{doc.relative_to(root)}:{line} -> {rel}",
                    )
                )
                continue
            if symbol is None:
                continue
            present = symbol_exists(target, symbol)
            if kind == "truth" and not present:
                findings.append(
                    Finding(
                        "TRUTH023",
                        "error",
                        "truth claim names a symbol that does not exist",
                        f"{doc.relative_to(root)}:{line} -> {rel}::{symbol}",
                    )
                )
            if kind == "truth-absent" and present:
                findings.append(
                    Finding(
                        "TRUTH024",
                        "error",
                        "truth-absent claim names a symbol that DOES exist (doc is stale)",
                        f"{doc.relative_to(root)}:{line} -> {rel}::{symbol}",
                    )
                )
    return findings


# --------------------------------------------------------------------------- #
# check 5 — no silent-failure default creeps into src/
# --------------------------------------------------------------------------- #

#: ``.get("<verdict>", <truthy>)`` turns a missing verdict into success.  These
#: two sites were reviewed on 2026-10-06 and are accepted: ``graph.py`` normalises
#: via ``phi.moderate`` (which fails closed) before the read, and ``trust.py``'s
#: ``threshold`` is a policy default, not a safety verdict.  Anything NEW is a
#: warning so the class cannot silently spread.
_TRUTHY_DEFAULT = re.compile(
    r"""\.get\(\s*["'](?P<key>[^"']+)["']\s*,\s*(?P<default>True|1|["'](?:ok|success|safe|done|true|granted|verified|valid)["'])\s*\)"""
)
_ACCEPTED_FAIL_OPEN = {
    "src/nexus_ai_agent/orchestration/graph.py": "normalised by phi.moderate (fail-closed)",
    "src/nexus_ai_agent/creative/packs/trust.py": "policy threshold, not a safety verdict",
}


def check_fail_open_defaults(root: Path) -> list[Finding]:
    """Flag truthy-default ``.get`` verdict reads in ``src/`` (silent-success class)."""
    findings: list[Finding] = []
    src = root / "src"
    if not src.is_dir():
        return findings
    for path in _tracked_files(src, ".py"):
        rel = path.relative_to(root).as_posix()
        if rel in _ACCEPTED_FAIL_OPEN:
            continue
        text = _read(path)
        for match in _TRUTHY_DEFAULT.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            findings.append(
                Finding(
                    "TRUTH030",
                    "warning",
                    "truthy-default verdict read can mask a missing value as success",
                    f"{rel}:{line} .get({match.group('key')!r}, {match.group('default')})",
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# registry + driver
# --------------------------------------------------------------------------- #

CHECKS: tuple[tuple[str, Callable[[Path], list[Finding]]], ...] = (
    ("version-lockstep", check_version_lockstep),
    ("docs-index", check_docs_index),
    ("law-test-resolution", check_law_test_resolution),
    ("claim-witnesses", check_claim_witnesses),
    ("fail-open-defaults", check_fail_open_defaults),
)


def run_doctor(root: Path | str, only: Iterable[str] | None = None) -> TruthReport:
    """Run every registered check (or the subset in *only*) and collect findings."""
    root = Path(root).resolve()
    selected = set(only) if only is not None else None
    ran: list[str] = []
    findings: list[Finding] = []
    for name, check in CHECKS:
        if selected is not None and name not in selected:
            continue
        ran.append(name)
        findings.extend(check(root))
    findings.sort(key=lambda f: (f.severity, f.code, f.evidence))
    return TruthReport(root=root, checks_run=tuple(ran), findings=tuple(findings))


def _default_root() -> Path:
    # src/nexus_ai_agent/diagnostics/truth.py -> repository root
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m nexus_ai_agent.diagnostics.truth")
    parser.add_argument("--root", default=None, help="repository root (default: auto-detect)")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--fail-on", choices=SEVERITIES, default="error")
    parser.add_argument("--only", action="append", default=None, help="run one check (repeatable)")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else _default_root()
    report = run_doctor(root, only=args.only)
    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(report.format_text())
    return report.exit_code(fail_on=args.fail_on)


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    sys.exit(main())
