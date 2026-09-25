#!/usr/bin/env python3
"""Constitution gate — proves the Engineering Constitution is enforced, not decorative.

Why this file exists (audit task-185, ``CONSTITUTION_ENFORCEMENT_AUDIT_2026-09-25.md``):
the enforcement of ``ENGINEERING_CONSTITUTION.md`` used to live *inside*
``tests/unit/test_engineering_constitution.py``.  Deleting, stubbing, or skipping that
one file therefore deleted the enforcement, and nothing else in the repository
noticed (measured: 97 other governance tests stayed green).  This gate is the
independent half of the enforcement:

* it is a **separate file** in a separate directory, run by a **separate CI job**
  (``lint-fast``, which installs nothing and reports in seconds), so removing the
  enforcement requires editing three unrelated places at once;
* it is **stdlib only** — the same zero-dependency rule as
  ``scripts/check_version_lockstep.py`` and ``scripts/extras_matrix.py``;
* it **fails closed**: an unreadable file, a missing git object, or an unknown state
  is a failure with an actionable message, never a silent pass.

What it checks
--------------
1. the constitution document exists and carries a version, a date, an owner, a
   precedence statement, and a **content identity** (SHA-256 of each part);
2. that content identity still matches the file — editing the law is possible, but
   only as a deliberate, reviewable act (re-stamp + bump the version), never as a
   silent edit (Articles 5, 6, 9);
3. all 14 articles exist **exactly once**, in order, in both languages, and each
   carries a non-trivial body — a gutted, duplicated, or reordered law is a red gate;
4. every entry point an agent actually reads points at the constitution
   (``AGENTS.md`` rule-zero section, ``CONTRIBUTING.md``, ``README.md``, the board's
   ``protocol.constitution``, the board CLI reminder, the PR template's Final Gate);
5. the enforcement test file still exists and cannot be skipped out of the default
   suite (no ``slow`` marker, no pytest filter that would deselect it);
6. this gate is still wired into CI, and the two long-standing governance suites
   still carry their cross-guard back to it;
7. board evidence is **SHA-bound**: a governance claim that cites evidence must cite
   a commit that exists, is reachable, and still covers the work being submitted
   (Articles 10 and 14; the stale-evidence gap this audit reproduced live).

Usage
-----
    python scripts/constitution_gate.py            # exit 0 = enforced, 1 = gap
    python scripts/constitution_gate.py --print-hashes   # re-stamp after a real edit
    python scripts/constitution_gate.py --json     # machine-readable report
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSTITUTION = ROOT / "ENGINEERING_CONSTITUTION.md"
AGENTS_MD = ROOT / "AGENTS.md"
CONTRIBUTING_MD = ROOT / "CONTRIBUTING.md"
README_MD = ROOT / "README.md"
BOARD_PATH = ROOT / ".agents" / "board.json"
BOARD_CLI = ROOT / "scripts" / "agent_board.py"
PR_TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
ENFORCEMENT_TEST = ROOT / "tests" / "unit" / "test_engineering_constitution.py"
CROSS_GUARDS = (
    ROOT / "tests" / "unit" / "test_docs_integrity.py",
    ROOT / "tests" / "unit" / "test_agent_board.py",
)
PYPROJECT = ROOT / "pyproject.toml"

ARTICLE_COUNT = 14
FINAL_GATE_GATES = (
    "CODE",
    "TEST",
    "SECURITY",
    "CI",
    "OBSERVABILITY",
    "DOCUMENTATION",
    "BOARD EVIDENCE",
)
#: Claims in these zones are the constitution's own claims: they must cite evidence.
GOVERNANCE_ZONE_PREFIX = "governance-constitution"
MIN_ARTICLE_BODY_WORDS = 12
#: The enforcement test must keep a substantive body of assertions (M11b: replacing the module
#: with a green stub must not pass) and must keep checking each fact the law depends on.
MIN_ENFORCEMENT_ASSERTS = 20
REQUIRED_TEST_ANCHORS = (
    "PERSIAN_HEADINGS",
    "ENGLISH_HEADINGS",
    "FINAL_GATE_GATES",
    "MIN_ARTICLE_BODY_WORDS",
    "RULE_ZERO_HEADING",
    "evidence_sha",
)
#: The exact heading of the mandatory entry gate in AGENTS.md — the section is found by this
#: string, so shortening it is a visible change, not a silent one.
RULE_ZERO_HEADING = "## 0. Rule zero — read the Engineering Constitution first"

PERSIAN_HEADINGS = (
    "ماده 1 — هیچ کدی بدون تحقیق",
    "ماده 2 — Deep Search برای هر فایل",
    "ماده 3 — بهترین گزینه ≠ محبوب‌ترین گزینه",
    "ماده 4 — Innovation با Guardrail",
    "ماده 5 — Evidence First",
    "ماده 6 — Fail Closed",
    "ماده 7 — تغییر کوچک، نتیجه کامل",
    "ماده 8 — Scope Firewall",
    "ماده 9 — No Silent Debt",
    "ماده 10 — Reproducibility",
    "ماده 11 — Security Before Convenience",
    "ماده 12 — مالکیت تصمیم",
    "ماده 13 — Never Optimize the Metric",
    "ماده 14 — Final Gate",
)
ENGLISH_HEADINGS = (
    "Article 1 — No code without research",
    "Article 2 — Deep search for every file",
    "Article 3 — The best option is not the most popular option",
    "Article 4 — Innovation with guardrails",
    "Article 5 — Evidence first",
    "Article 6 — Fail closed",
    "Article 7 — Small change, complete result",
    "Article 8 — Scope firewall",
    "Article 9 — No silent debt",
    "Article 10 — Reproducibility",
    "Article 11 — Security before convenience",
    "Article 12 — Decision ownership",
    "Article 13 — Never optimize the metric",
    "Article 14 — Final gate",
)
#: Fragments of the owner's Persian original that must survive verbatim.
PERSIAN_MARKERS = (
    "SEARCH → UNDERSTAND → COMPARE → DESIGN → IMPLEMENT → TEST → VERIFY",
    "کدنویسی از روی حدس ممنوع است.",
    "حداقل 3 منبع مستقل معتبر لازم است",
    "اگر راه‌حل فعلی بهتر است، تغییر نده.",
    "نوآوری بدون مهندسی = رد.",
    '"explicit refusal > fake success"',
    "یک patch کوچک که contract را کامل نمی‌کند",
    "dependency واقعی اثبات شود",
    "هیچ finding نباید در هوا رها شود.",
    "امنیت، integrity و data correctness اولویت دارند.",
    "قرار است مثل owner subsystem تصمیم مهندسی بگیرد.",
    '"green dashboard"',
    "test حذف نمی‌شود",
    "این قانون اساسی است و در تمام تصمیم‌های معماری بر قوانین محلی مقدم است.",
)

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
HEADING_LINE = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.MULTILINE)
HASH_ROW = re.compile(r"^\|\s*\*\*(?P<label>[^*]+)\*\*\s*\|\s*(?P<value>[^|]+)\|\s*$", re.MULTILINE)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _failures() -> list[str]:
    return []


def _normalise(text: str) -> str:
    """Whitespace-insensitive canonical form, so a re-wrap is not a law change."""
    lines = [re.sub(r"^#+\s*", "", line) for line in text.splitlines()]
    return re.sub(r"\s+", " ", "\n".join(lines)).strip()


def _part(text: str, start_marker: str, end_marker: str | None) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker) if end_marker else len(text)
    return text[start:end]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(*args: str) -> str:
    proc = subprocess.run(  # noqa: S603 (fixed argv, repository root only)
        ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    return proc.stdout.strip()


def _git_ok(*args: str) -> bool:
    proc = subprocess.run(  # noqa: S603
        ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    return proc.returncode == 0


def _article_body(text: str, heading: str) -> str:
    """The article's own section: from its heading to the next ``## `` heading."""
    marker = f"## {heading}"
    start = text.index(marker) + len(marker)
    rest = text[start:]
    next_heading = rest.find("\n## ")
    return rest[:next_heading] if next_heading != -1 else rest


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def check_document(failures: list[str], report: dict) -> str:
    if not CONSTITUTION.is_file():
        failures.append("ENGINEERING_CONSTITUTION.md is missing from the repository root")
        return ""
    text = CONSTITUTION.read_text(encoding="utf-8")
    report["constitution_lines"] = len(text.splitlines())

    header = _part(text, "# NEXUS ENGINEERING CONSTITUTION", "\n---")
    rows = {
        match.group("label").strip(): match.group("value").strip()
        for match in HASH_ROW.finditer(header)
    }
    for field in ("Version", "Date", "Owner", "Precedence", "Language"):
        if field not in rows:
            failures.append(f"the constitution header lost its {field} row")
    if "Version" in rows and not SEMVER.match(rows["Version"]):
        failures.append(f"the constitution version {rows['Version']!r} is not semver")
    report["constitution_version"] = rows.get("Version", "?")

    for language, part_start, part_end in (
        ("Persian", "## ماده 1 —", "# Part II"),
        ("English", "# Part II — English rendering", "## 15."),
    ):
        key = next(
            (k for k in rows if k.startswith("Content identity") and language in k),
            None,
        )
        if key is None:
            failures.append(f"the constitution header lost its Content identity ({language}) row")
            continue
        recorded = rows[key].split()[0].strip("`") if rows[key] else ""
        body = _part(text, part_start, part_end)
        computed = _sha256(_normalise(body))
        report[f"content_identity_{language.lower()}"] = {
            "recorded": recorded,
            "computed": computed,
            "match": recorded == computed,
        }
        if recorded != computed:
            failures.append(
                f"Content identity ({language}) drifted: recorded {recorded[:16]}…, "
                f"computed {computed[:16]}… — "
                "re-stamp deliberately with `python scripts/constitution_gate.py --print-hashes` "
                "and bump the Version row (a silent edit of the law is not a law change)"
            )
    return text


def check_articles(text: str, failures: list[str], report: dict) -> None:
    for language, headings in (("Persian", PERSIAN_HEADINGS), ("English", ENGLISH_HEADINGS)):
        positions: list[int] = []
        for heading in headings:
            count = text.count(f"## {heading}")
            if count == 0:
                failures.append(f"{language} article is missing or renamed: {heading!r}")
                continue
            if count > 1:
                failures.append(
                    f"{language} article appears {count} times (duplicated): {heading!r}"
                )
            positions.append(text.index(f"## {heading}"))
            body = _article_body(text, heading)
            if len(body.split()) < MIN_ARTICLE_BODY_WORDS:
                failures.append(
                    f"{language} article body is gutted "
                    f"(<{MIN_ARTICLE_BODY_WORDS} words): {heading!r}"
                )
        if positions == sorted(positions) and len(positions) == ARTICLE_COUNT:
            report[f"{language.lower()}_articles_in_order"] = True
        else:
            failures.append(
                f"{language} articles are not present exactly once, in order 1..{ARTICLE_COUNT}"
            )

    for marker in PERSIAN_MARKERS:
        if marker not in text:
            failures.append(f"the owner's original wording was altered: {marker[:48]!r}")


def check_entry_points(failures: list[str], report: dict) -> None:
    # AGENTS.md: the link must be inside the rule-zero section, not merely anywhere.
    if not AGENTS_MD.is_file():
        failures.append("AGENTS.md is missing")
    else:
        agents = AGENTS_MD.read_text(encoding="utf-8")
        section = agents.split(RULE_ZERO_HEADING, 1)
        if len(section) < 2:
            failures.append(
                f"AGENTS.md lost its {RULE_ZERO_HEADING!r} section (M06b: a truncated or renamed "
                "rule-zero heading is not the entry gate)"
            )
        else:
            rule_zero = section[1].split("\n## ", 1)[0]
            if "](ENGINEERING_CONSTITUTION.md)" not in rule_zero:
                failures.append(
                    "AGENTS.md rule-zero no longer links the constitution (a link elsewhere in the "
                    "file is not the entry gate)"
                )
            if "prevails over" not in rule_zero:
                failures.append(
                    "AGENTS.md rule-zero no longer states the constitution's precedence"
                )
            if "before" not in rule_zero.lower():
                failures.append(
                    "AGENTS.md rule-zero no longer says the constitution is read before work"
                )

    for path, label in ((CONTRIBUTING_MD, "CONTRIBUTING.md"), (README_MD, "README.md")):
        linked = path.is_file() and "](ENGINEERING_CONSTITUTION.md)" in path.read_text(
            encoding="utf-8"
        )
        if not linked:
            failures.append(f"{label} does not link the constitution")

    if not BOARD_PATH.is_file():
        failures.append(".agents/board.json is missing")
    else:
        protocol = json.loads(BOARD_PATH.read_text(encoding="utf-8")).get("protocol", {})
        for key in ("constitution", "constitution_en"):
            if key not in protocol:
                failures.append(f"the board protocol lost its {key} entry")
            elif "ENGINEERING_CONSTITUTION.md" not in protocol[key]:
                failures.append(f"the board protocol's {key} entry does not name the document")

    cli = BOARD_CLI.read_text(encoding="utf-8") if BOARD_CLI.is_file() else ""
    if "print(_constitution_line(board))" not in cli:
        failures.append("the board CLI no longer prints the constitution reminder")
    elif cli.count("print(_constitution_line(board))") < 3:
        failures.append(
            "the board CLI prints the reminder from fewer than three commands "
            "(show / next / claim all must print it)"
        )

    if not PR_TEMPLATE.is_file():
        failures.append(".github/PULL_REQUEST_TEMPLATE.md is missing")
    else:
        template = PR_TEMPLATE.read_text(encoding="utf-8")
        for gate in FINAL_GATE_GATES:
            if f"- [ ] **{gate}**" not in template:
                failures.append(f"the PR template lost its Final Gate checklist item for {gate}")


def check_test_is_live(failures: list[str], report: dict) -> None:
    if not ENFORCEMENT_TEST.is_file():
        failures.append(
            "tests/unit/test_engineering_constitution.py is missing — the enforcement test file "
            "was deleted; this gate exists precisely so that deletion cannot pass silently"
        )
        return
    source = ENFORCEMENT_TEST.read_text(encoding="utf-8")
    for token in ("mark.slow", "pytest.skip", "pytest.xfail", "pytest.mark.skip"):
        if token in source:
            failures.append(f"the enforcement test contains {token} — it can be skipped out of CI")
    asserts = source.count("assert ")
    report["enforcement_asserts"] = asserts
    if asserts < MIN_ENFORCEMENT_ASSERTS:
        failures.append(
            f"the enforcement test carries only {asserts} assertions "
            f"(< {MIN_ENFORCEMENT_ASSERTS}) - a stubbed module must not pass (M11b)"
        )
    for required in REQUIRED_TEST_ANCHORS:
        if required not in source:
            failures.append(
                f"the enforcement test no longer checks {required!r} - the guard lost a fact "
                "it was responsible for"
            )

    pyproject = PYPROJECT.read_text(encoding="utf-8") if PYPROJECT.is_file() else ""
    addopts = re.search(r"^addopts\s*=\s*(.+)$", pyproject, re.MULTILINE)
    if addopts:
        for banned in ("--ignore", "--deselect", "-k ", "--skip", "-p no:"):
            if banned in addopts.group(1):
                failures.append(f"pytest addopts contains {banned!r}, which can deselect the gate")
    report["enforcement_test_lines"] = len(source.splitlines())

    for guard in CROSS_GUARDS:
        if not guard.is_file():
            failures.append(f"cross-guard {guard.name} is missing")
        elif "constitution_gate" not in guard.read_text(encoding="utf-8"):
            failures.append(f"cross-guard {guard.name} no longer references the constitution gate")


def check_ci_wiring(failures: list[str]) -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8") if CI_WORKFLOW.is_file() else ""
    if "python scripts/constitution_gate.py" not in workflow:
        failures.append(
            "ci.yml does not run `python scripts/constitution_gate.py` — the gate is not wired "
            "into any job (the workflow file is part of every PR, so this must be checked)"
        )
    # The cross-guards must name the enforcement test so its deletion is caught twice over.
    if not any(
        "tests/unit/test_engineering_constitution.py" in path.read_text(encoding="utf-8")
        for path in CROSS_GUARDS
        if path.is_file()
    ):
        failures.append(
            "no cross-guard names tests/unit/test_engineering_constitution.py — deleting the "
            "enforcement test would again be invisible outside its own file"
        )


def check_board_evidence(failures: list[str], report: dict) -> None:
    """Article 10 + 14: evidence must be attributable to a real, current commit."""
    if not BOARD_PATH.is_file():
        return
    board = json.loads(BOARD_PATH.read_text(encoding="utf-8"))
    # ``rev-list --parents -n 1`` prints "<sha> <parent…>": a merge commit carries two parents
    # (the PR head is the second), a plain commit carries one (itself is the head).
    parents = _git("rev-list", "--parents", "-n", "1", "HEAD").split()
    evidence_ref = parents[2] if len(parents) > 2 else parents[0]

    for claim in board.get("claims", []):
        sha = claim.get("evidence_sha")
        if not sha:
            continue
        label = f"{claim['task']} ({claim.get('agent_branch') or 'unowned'})"
        if not SHA40.match(str(sha)):
            failures.append(f"{label}: evidence_sha {sha!r} is not a 40-hex commit id")
            continue
        if not _git_ok("cat-file", "-e", f"{sha}^{{commit}}"):
            failures.append(f"{label}: evidence_sha {sha[:12]} does not exist in this repository")
            continue
        if not _git_ok("merge-base", "--is-ancestor", str(sha), evidence_ref):
            failures.append(
                f"{label}: evidence_sha {sha[:12]} is not reachable from the submitted head "
                f"{evidence_ref[:12]} — evidence from another branch is not evidence"
            )
            continue
        if not claim.get("zone", "").startswith(GOVERNANCE_ZONE_PREFIX):
            continue
        stale = _git(
            "rev-list",
            "--count",
            f"{sha}..{evidence_ref}",
            "--",
            ".",
            ":(exclude).agents/board.json",
        )
        report.setdefault("governance_evidence", {})[claim["task"]] = {
            "evidence_sha": sha[:12],
            "head": evidence_ref[:12],
            "unverified_non_board_commits": stale,
        }
        if stale not in ("0", ""):
            failures.append(
                f"{label}: evidence_sha {sha[:12]} is stale — {stale} non-board commit(s) have "
                "landed since it was verified; re-run the gates and re-cite the SHA"
            )


def print_hashes() -> int:
    text = CONSTITUTION.read_text(encoding="utf-8")
    print("# paste these into the 'Content identity' rows of ENGINEERING_CONSTITUTION.md")
    print(f"Persian: {_sha256(_normalise(_part(text, '## ماده 1 —', '# Part II')))}")
    print(f"English: {_sha256(_normalise(_part(text, '# Part II — English rendering', '## 15.')))}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="NEXUS Engineering Constitution gate")
    parser.add_argument("--print-hashes", action="store_true", help="re-stamp content identity")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args()

    if args.print_hashes:
        return print_hashes()

    failures: list[str] = []
    report: dict = {"root": str(ROOT)}
    text = check_document(failures, report)
    if text:
        check_articles(text, failures, report)
    check_entry_points(failures, report)
    check_test_is_live(failures, report)
    check_ci_wiring(failures)
    check_board_evidence(failures, report)
    report["failures"] = failures
    report["enforced"] = not failures

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    elif failures:
        print("CONSTITUTION GATE: FAIL — the Engineering Constitution is not fully enforced\n")
        for failure in failures:
            print(f"  ✗ {failure}")
        print(f"\n{len(failures)} gap(s). Fix them, or change the law deliberately (Article 5).")
    else:
        print(
            "CONSTITUTION GATE: PASS — 14/14 articles intact and in order (Persian + English), "
            "content identity verified, all entry points live, enforcement test unskippable, "
            "CI wiring present, board evidence SHA-bound."
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
