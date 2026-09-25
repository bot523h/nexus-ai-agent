"""The Engineering Constitution is enforced, not merely written.

The owner's 14-article Engineering Constitution (``ENGINEERING_CONSTITUTION.md``) is the law every
agent in this repository works under (``AGENTS.md`` §0).  A law that lives only in prose is exactly
the "green dashboard" its own Article 13 forbids optimising for, so each of its entry points is
pinned here.

This module is the *behavioural* half of the enforcement; ``scripts/constitution_gate.py`` is the
independent half (stdlib, wired into the ``lint-fast`` CI job), and two long-standing suites carry
cross-guards back to both.  That split exists because audit task-185 proved the obvious failure
mode: when the enforcement lived only inside this file, deleting this file left 97 other governance
tests green (mutation M10).

The hardening below closes the gaps that audit measured, each named after its mutation id:

* **M05 / gutted text** — every article, in both languages, must carry a non-trivial body;
* **M06 / link elsewhere** — ``AGENTS.md`` must link the constitution *inside its rule-zero
  section*, not merely somewhere in the file;
* **M09 / gate renamed** — the PR template must carry the checklist *line* for each Final Gate;
* **M11 / weakened or stubbed assertions** — the gate script and the cross-guards assert this file
  still exists and cannot be skipped out of the default suite;
* **M12–M14 / duplicate, reorder** — every article heading must appear exactly once, in order;
* **M18 / silent edit of the law** — the document carries a content identity (sha256 per part) that
  must match, and a semver version;
* **M19 / fabricated evidence** — board ``evidence_sha`` values must be real, reachable commits that
  still cover the work being submitted (Articles 10 and 14).

Stdlib + pytest only, no network, milliseconds — it runs in the same ``pytest`` job as
``tests/unit/test_docs_integrity.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).parents[2]
CONSTITUTION = REPO_ROOT / "ENGINEERING_CONSTITUTION.md"
AGENTS_MD = REPO_ROOT / "AGENTS.md"
CONTRIBUTING_MD = REPO_ROOT / "CONTRIBUTING.md"
README_MD = REPO_ROOT / "README.md"
BOARD_PATH = REPO_ROOT / ".agents" / "board.json"
BOARD_SCRIPT = REPO_ROOT / "scripts" / "agent_board.py"
PR_TEMPLATE = REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
GATE_SCRIPT = REPO_ROOT / "scripts" / "constitution_gate.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"
CROSS_GUARDS = (
    REPO_ROOT / "tests" / "unit" / "test_docs_integrity.py",
    REPO_ROOT / "tests" / "unit" / "test_agent_board.py",
)

ARTICLE_COUNT = 14
MIN_ARTICLE_BODY_WORDS = 12
#: The exact heading of the mandatory entry gate; the section is located by this string.
RULE_ZERO_HEADING = "## 0. Rule zero — read the Engineering Constitution first"
FINAL_GATE_GATES = (
    "CODE",
    "TEST",
    "SECURITY",
    "CI",
    "OBSERVABILITY",
    "DOCUMENTATION",
    "BOARD EVIDENCE",
)
FINDING_STATES = (
    "FIXED",
    "VERIFIED",
    "DEFERRED_WITH_REASON",
    "KNOWN_ENVIRONMENTAL",
    "NOT_REPRODUCIBLE",
    "FALSE_POSITIVE",
)
DECISION_FIELDS = ("OPTIONS", "TRADEOFFS", "DECISION", "WHY", "RISK", "TEST")

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

#: Fragments of the owner's Persian original that must survive verbatim.  The constitution is the
#: owner's law, not a paraphrase of it.
PERSIAN_MARKERS = (
    "ماده 1 — هیچ کدی بدون تحقیق",
    "SEARCH → UNDERSTAND → COMPARE → DESIGN → IMPLEMENT → TEST → VERIFY",
    "کدنویسی از روی حدس ممنوع است.",
    "ماده 2 — Deep Search برای هر فایل",
    "حداقل 3 منبع مستقل معتبر لازم است",
    "بهترین گزینه ≠ محبوب‌ترین گزینه",
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

#: The nine selection criteria of Article 3 — an implementation choice must be argued against
#: them, which is impossible if the list quietly loses a member.
ARTICLE_3_CRITERIA = (
    "Correctness",
    "Security",
    "Maintainability",
    "Testability",
    "Observability",
    "Performance",
    "Operational simplicity",
    "Compatibility",
    "Failure behavior",
)

PLACEHOLDER_PATTERNS = (
    re.compile(r"\b(?:TODO|FIXME|XXX|TBD|HACK)\s*:"),
    re.compile(r"<placeholder", re.IGNORECASE),
)

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
_HEADER_ROW = re.compile(
    r"^\|\s*\*\*(?P<label>[^*]+)\*\*\s*\|\s*(?P<value>[^|]+)\|\s*$", re.MULTILINE
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _relative_links(text: str) -> list[str]:
    links = []
    for raw in _LINK.findall(text):
        target = raw.strip()
        if target.startswith(("http://", "https://", "mailto:", "#", "tel:")):
            continue
        if not target.split("#")[0]:
            continue
        links.append(target)
    return links


def _strip_code(text: str) -> str:
    """Drop fenced blocks and inline code spans — the constitution *writes about* rules."""
    without_fences: list[str] = []
    inside_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside_fence = not inside_fence
            continue
        if not inside_fence:
            without_fences.append(line)
    return re.sub(r"`[^`\n]*`", "", "\n".join(without_fences))


def _normalise(text: str) -> str:
    """Whitespace-insensitive canonical form: a re-wrap is not a law change."""
    lines = [re.sub(r"^#+\s*", "", line) for line in text.splitlines()]
    return re.sub(r"\s+", " ", "\n".join(lines)).strip()


def _part(text: str, start_marker: str, end_marker: str | None) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker) if end_marker else len(text)
    return text[start:end]


def _article_body(text: str, heading: str) -> str:
    marker = f"## {heading}"
    start = text.index(marker) + len(marker)
    rest = text[start:]
    next_heading = rest.find("\n## ")
    return rest[:next_heading] if next_heading != -1 else rest


def _git(*args: str) -> str:
    proc = subprocess.run(  # noqa: S603 (fixed argv, repository root only)
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60
    )
    return proc.stdout.strip()


def _git_ok(*args: str) -> bool:
    proc = subprocess.run(  # noqa: S603
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60
    )
    return proc.returncode == 0


def _load_board_cli() -> ModuleType:
    name = "agent_board_constitution_under_test"
    spec = importlib.util.spec_from_file_location(name, BOARD_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _probe_board(protocol: dict | None = None) -> dict:
    """A minimal board with one queued task, used only to drive the CLI's output."""
    return {
        "schema": 2,
        "updated_at": "2026-09-25T00:00:00Z",
        "protocol": protocol if protocol is not None else {"protocol_version": 2},
        "claims": [
            {
                "task": "constitution-probe",
                "status": "queued",
                "zone": "governance-constitution-audit",
                "exclusive_paths": ["ENGINEERING_CONSTITUTION.md"],
            }
        ],
        "zones": [
            {"id": "governance-constitution-audit", "paths": ["ENGINEERING_CONSTITUTION.md"]}
        ],
        "deferred_log": [],
        "next_work": [],
        "history": {"waves_closed": [], "pull_requests": [], "incidents": []},
    }


@pytest.fixture()
def board_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The board CLI pointed at a throwaway board, so the real one is never mutated."""
    module = _load_board_cli()
    copy = tmp_path / "board.json"
    copy.write_text(json.dumps(_probe_board(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(module, "BOARD", copy)
    return module


# --------------------------------------------------------------------------- #
# the document itself
# --------------------------------------------------------------------------- #
def test_constitution_document_exists_and_is_substantive() -> None:
    assert CONSTITUTION.is_file(), "ENGINEERING_CONSTITUTION.md is missing from the repository root"
    text = _read(CONSTITUTION)
    assert len(text.splitlines()) > 200, "the constitution looks truncated"
    for part in ("# Part I — Persian original", "# Part II — English rendering"):
        assert part in text, f"the constitution lost its {part!r} part"


def test_every_article_is_registered_in_both_languages() -> None:
    text = _read(CONSTITUTION)
    assert len(PERSIAN_HEADINGS) == ARTICLE_COUNT
    assert len(ENGLISH_HEADINGS) == ARTICLE_COUNT
    for number, (persian, english) in enumerate(
        zip(PERSIAN_HEADINGS, ENGLISH_HEADINGS, strict=True), start=1
    ):
        assert f"## {persian}" in text, f"Persian Article {number} is missing"
        assert f"## {english}" in text, f"English Article {number} is missing"


def test_each_article_heading_appears_exactly_once_and_in_order() -> None:
    """M12/M13/M14: a duplicated or reordered article must not pass as an intact law."""
    text = _read(CONSTITUTION)
    for language, headings in (("Persian", PERSIAN_HEADINGS), ("English", ENGLISH_HEADINGS)):
        positions: list[int] = []
        for number, heading in enumerate(headings, start=1):
            count = text.count(f"## {heading}")
            assert count == 1, (
                f"{language} Article {number} heading appears {count} times "
                f"(expected exactly 1): {heading!r}"
            )
            positions.append(text.index(f"## {heading}"))
        assert positions == sorted(positions), f"{language} articles are out of order"


def test_no_article_body_is_gutted() -> None:
    """M05: keeping the heading while deleting the obligation must fail."""
    text = _read(CONSTITUTION)
    for language, headings in (("Persian", PERSIAN_HEADINGS), ("English", ENGLISH_HEADINGS)):
        for number, heading in enumerate(headings, start=1):
            body = _article_body(text, heading)
            assert len(body.split()) >= MIN_ARTICLE_BODY_WORDS, (
                f"{language} Article {number} body is gutted "
                f"({len(body.split())} words < {MIN_ARTICLE_BODY_WORDS}): {heading!r}"
            )


def test_persian_original_is_preserved_verbatim() -> None:
    text = _read(CONSTITUTION)
    missing = [marker for marker in PERSIAN_MARKERS if marker not in text]
    assert not missing, f"the owner's original wording was altered: {missing}"


def test_article_3_lists_all_nine_criteria() -> None:
    text = _read(CONSTITUTION)
    missing = [criterion for criterion in ARTICLE_3_CRITERIA if criterion not in text]
    assert not missing, f"Article 3 lost selection criteria: {missing}"


def test_article_14_final_gate_names_all_seven_gates() -> None:
    text = _read(CONSTITUTION)
    missing = [gate for gate in FINAL_GATE_GATES if gate not in text]
    assert not missing, f"the Final Gate lost a gate: {missing}"


def test_article_9_names_all_six_finding_states() -> None:
    text = _read(CONSTITUTION)
    missing = [state for state in FINDING_STATES if state not in text]
    assert not missing, f"the finding states lost a state: {missing}"


def test_article_12_names_all_six_decision_fields() -> None:
    text = _read(CONSTITUTION)
    missing = [field for field in DECISION_FIELDS if field not in text]
    assert not missing, f"the decision record lost a field: {missing}"


def test_constitution_declares_its_own_precedence() -> None:
    text = _read(CONSTITUTION)
    assert "prevails over" in text and "بر قوانین محلی مقدم است" in text, (
        "the constitution must state that it prevails over local rules (its Article 14)"
    )


def test_constitution_version_is_semver() -> None:
    header = _part(_read(CONSTITUTION), "# NEXUS ENGINEERING CONSTITUTION", "\n---")
    rows = {
        m.group("label").strip(): m.group("value").strip() for m in _HEADER_ROW.finditer(header)
    }
    assert "Version" in rows, "the constitution header lost its Version row"
    assert SEMVER.match(rows["Version"]), f"version {rows['Version']!r} is not semver"


def test_constitution_content_identity_matches_the_document() -> None:
    """M03/M04/M05/M17/M18: the law cannot be edited silently — the hash is stamped in it."""
    text = _read(CONSTITUTION)
    header = _part(text, "# NEXUS ENGINEERING CONSTITUTION", "\n---")
    rows = {
        m.group("label").strip(): m.group("value").strip() for m in _HEADER_ROW.finditer(header)
    }
    for language, start, end in (
        ("Persian", "## ماده 1 —", "# Part II"),
        ("English", "# Part II — English rendering", "## 15."),
    ):
        key = next((k for k in rows if k.startswith("Content identity") and language in k), None)
        assert key is not None, f"the header lost its Content identity ({language}) row"
        recorded = rows[key].split()[0].strip("`")
        computed = hashlib.sha256(_normalise(_part(text, start, end)).encode("utf-8")).hexdigest()
        assert recorded == computed, (
            f"Content identity ({language}) drifted: recorded {recorded[:16]}…, "
            f"computed {computed[:16]}… — re-stamp with "
            "`python scripts/constitution_gate.py --print-hashes` and bump the version"
        )


def test_constitution_has_no_placeholder_tokens() -> None:
    """Article 6: no placeholder may present itself as finished text."""
    body = _strip_code(_read(CONSTITUTION))
    found = [pattern.pattern for pattern in PLACEHOLDER_PATTERNS if pattern.search(body)]
    assert not found, f"the constitution still contains placeholder markers {found}"


def test_every_relative_link_in_the_constitution_resolves() -> None:
    dead = [
        target
        for target in _relative_links(_read(CONSTITUTION))
        if not (REPO_ROOT / target.split("#")[0]).resolve().exists()
    ]
    assert not dead, f"dead relative links in the constitution: {dead}"


# --------------------------------------------------------------------------- #
# the entry points an agent actually reads
# --------------------------------------------------------------------------- #
def test_agents_md_rule_zero_links_the_constitution() -> None:
    """M06: the link must be in the rule-zero section, not merely somewhere in the file."""
    agents = _read(AGENTS_MD)
    assert "](ENGINEERING_CONSTITUTION.md)" in agents, "AGENTS.md does not link the constitution"
    assert RULE_ZERO_HEADING in agents, (
        f"AGENTS.md lost its {RULE_ZERO_HEADING!r} section — a truncated or renamed rule-zero "
        "heading is not the entry gate"
    )
    rule_zero = agents.split(RULE_ZERO_HEADING, 1)[1].split("\n## ", 1)[0]
    assert "](ENGINEERING_CONSTITUTION.md)" in rule_zero, (
        "AGENTS.md rule-zero no longer links the constitution — a link elsewhere in the file "
        "is not the entry gate"
    )
    assert "prevails over" in rule_zero, "rule-zero lost the precedence statement"
    assert "before" in rule_zero.lower(), "rule-zero must say the constitution is read before work"


def test_contributing_and_readme_point_at_the_constitution() -> None:
    for path in (CONTRIBUTING_MD, README_MD):
        text = _read(path)
        linked = "](ENGINEERING_CONSTITUTION.md)" in text
        assert linked, f"{path.name} does not link the constitution"


def test_pull_request_template_carries_the_final_gate_checklist() -> None:
    """Article 14 needs a per-PR form, and M09 showed a renamed gate must not pass."""
    assert PR_TEMPLATE.is_file(), ".github/PULL_REQUEST_TEMPLATE.md is missing"
    text = _read(PR_TEMPLATE)
    missing = [gate for gate in FINAL_GATE_GATES if f"- [ ] **{gate}**" not in text]
    assert not missing, f"the PR template lost a Final Gate checklist line: {missing}"


def test_board_protocol_declares_the_constitution() -> None:
    """The board is the only medium shared by parallel sandboxes — the law lives there too."""
    board = json.loads(BOARD_PATH.read_text(encoding="utf-8"))
    protocol = board["protocol"]
    assert "constitution" in protocol, "the board protocol lost its constitution entry"
    assert CONSTITUTION.name in protocol["constitution"], (
        "the board's constitution entry must name the document"
    )
    assert "constitution_en" in protocol, "the board protocol lost its English constitution entry"


# --------------------------------------------------------------------------- #
# the enforcement is not self-hosted (M10 / M11 / M20)
# --------------------------------------------------------------------------- #
def test_independent_gate_script_exists_and_is_wired_into_ci() -> None:
    assert GATE_SCRIPT.is_file(), "scripts/constitution_gate.py is missing"
    workflow = _read(CI_WORKFLOW)
    assert "python scripts/constitution_gate.py" in workflow, (
        "ci.yml does not run the constitution gate — the workflow file ships with every PR, so "
        "removing the gate must be a visible, reviewable act"
    )


def test_cross_guards_reference_the_gate_and_the_enforcement_test() -> None:
    """M10: deleting this file must be caught by modules nobody can remove silently."""
    naming: list[str] = []
    for guard in CROSS_GUARDS:
        assert guard.is_file(), f"cross-guard {guard.name} is missing"
        text = _read(guard)
        assert "constitution_gate" in text, f"{guard.name} no longer references the gate script"
        if "tests/unit/test_engineering_constitution.py" in text:
            naming.append(guard.name)
    assert naming, (
        "no cross-guard names tests/unit/test_engineering_constitution.py — deleting the "
        "enforcement test would again be invisible outside its own file"
    )


#: Assembled from fragments so this file never contains the tokens it forbids (a literal
#: list would make the guard fail on itself).
SKIP_TOKENS = ("mark." + "slow", "pytest." + "skip", "pytest." + "xfail", "mark." + "skipif")


def test_the_enforcement_suite_cannot_be_skipped_out_of_ci() -> None:
    """M11b: a test that CI never runs is not enforcement."""
    source = _read(Path(__file__))
    for token in SKIP_TOKENS:
        assert token not in source, f"the enforcement suite contains {token} — CI may skip it"
    pyproject = _read(PYPROJECT)
    addopts = re.search(r"^addopts\s*=\s*(.+)$", pyproject, re.MULTILINE)
    if addopts:
        for banned in ("--ignore", "--deselect", "-k ", "--skip", "-p no:"):
            assert banned not in addopts.group(1), (
                f"pytest addopts contains {banned!r}, which can deselect the enforcement suite"
            )


# --------------------------------------------------------------------------- #
# evidence is attributable to a real, current commit (Articles 10 and 14)
# --------------------------------------------------------------------------- #
def test_board_evidence_shas_are_well_formed() -> None:
    """M19: a fabricated or foreign evidence SHA must not look like evidence."""
    board = json.loads(BOARD_PATH.read_text(encoding="utf-8"))
    for claim in board["claims"]:
        sha = claim.get("evidence_sha")
        if not sha:
            continue
        assert SHA40.match(str(sha)), (
            f"{claim['task']}: evidence_sha {sha!r} is not a 40-hex commit id"
        )
        assert _git_ok("cat-file", "-e", f"{sha}^{{commit}}"), (
            f"{claim['task']}: evidence_sha {sha[:12]} does not exist in this repository"
        )


def test_governance_claim_evidence_still_covers_the_submitted_head() -> None:
    """The stale-evidence gap reproduced live in task-184: the board cited CI at 0152cdb while the
    PR head had already moved to 67e8ff3, and nothing noticed.

    Rules proven by that reproduction, in force for claims in a ``governance-constitution*`` zone:

    * the claim must cite ``evidence_sha`` at all — an unevidenced governance claim is a claim
      without evidence (Article 5), and an artificial skip here would be exactly what
      Article 13 forbids;
    * the cited commit must exist and be reachable from the submitted head (evidence from another
      branch is not evidence);
    * once the work is **under review** (``active_in_review``), the citation must still cover the
      head: no non-board commit may have landed since it was verified.
    """
    board = json.loads(BOARD_PATH.read_text(encoding="utf-8"))
    # ``rev-list --parents -n 1`` prints "<sha> <parent…>": a merge commit carries two parents
    # (the PR head is the second one), a plain commit carries one (itself is the head).
    parents = _git("rev-list", "--parents", "-n", "1", "HEAD").split()
    assert parents, "git is not available — refusing to claim the evidence check passed"
    evidence_ref = parents[2] if len(parents) > 2 else parents[0]

    checked = 0
    for claim in board["claims"]:
        sha = claim.get("evidence_sha")
        if not sha or not claim.get("zone", "").startswith("governance-constitution"):
            continue
        checked += 1
        label = f"{claim['task']} ({claim.get('agent_branch') or 'unowned'})"
        assert _git_ok("cat-file", "-e", f"{sha}^{{commit}}"), (
            f"{label}: evidence_sha {sha[:12]} does not exist in this repository"
        )
        assert _git_ok("merge-base", "--is-ancestor", str(sha), evidence_ref), (
            f"{label}: evidence_sha {sha[:12]} is not reachable from {evidence_ref[:12]}"
        )
        if claim.get("status") != "active_in_review":
            continue
        stale = _git(
            "rev-list",
            "--count",
            f"{sha}..{evidence_ref}",
            "--",
            ".",
            ":(exclude).agents/board.json",
        )
        assert stale in ("0", ""), (
            f"{label}: evidence_sha {sha[:12]} is stale — {stale} non-board commit(s) landed "
            "since it was verified; re-run the gates and re-cite the SHA"
        )
    assert checked >= 1, (
        "no governance-constitution claim carries evidence_sha — the evidence binding that "
        "closes the stale-evidence gap is not in force"
    )


# --------------------------------------------------------------------------- #
# the runtime reminder (show / next / claim)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command", ["show", "next", "claim"])
def test_board_cli_prints_the_constitution_reminder(
    board_cli: ModuleType, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    """An arriving agent cannot start work without passing the reminder."""
    if command == "show":
        args: argparse.Namespace = argparse.Namespace()
    elif command == "next":
        args = argparse.Namespace(branch="arena/probe-branch")
    else:
        args = argparse.Namespace(
            task="constitution-probe", branch="arena/probe-branch", ttl=24, gates=False
        )
    assert board_cli.main.__globals__[f"cmd_{command}"](args) == 0
    out = capsys.readouterr().out
    assert "constitution:" in out, f"`{command}` no longer prints the constitution reminder"
    assert CONSTITUTION.name in out, f"`{command}` reminder does not point at the document"


def test_board_cli_reminder_fails_closed_to_the_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Article 6: with no board wording the reminder still points at the constitution."""
    module = _load_board_cli()
    copy = tmp_path / "board.json"
    copy.write_text(json.dumps(_probe_board(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(module, "BOARD", copy)
    assert module.cmd_show(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert module.CONSTITUTION_DOC in out, "the default reminder lost the document name"
