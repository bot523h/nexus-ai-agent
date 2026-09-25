"""The Engineering Constitution is enforced, not merely written.

The owner's 14-article Engineering Constitution (``ENGINEERING_CONSTITUTION.md``) is
the law every agent in this repository works under (``AGENTS.md`` §0).  A law that
lives only in prose is exactly the "green dashboard" its own Article 13 forbids
optimising for, so each of its entry points is pinned here:

* the document itself exists, is substantive, and still carries all 14 articles in
  **both** languages (Persian original + English rendering) — a silently truncated
  constitution is a red build;
* the agent front door (``AGENTS.md``), the contributor instructions
  (``CONTRIBUTING.md``) and the human front door (``README.md``) all link it;
* the coordination board declares it (``.agents/board.json`` → ``protocol``), because
  the board is the only medium shared by parallel sandboxes;
* the board CLI prints the reminder from ``show`` / ``next`` / ``claim`` — the three
  commands an arriving agent runs before it writes a single line — including the
  fail-closed default when the board does not declare the text itself (Article 6);
* the per-PR Final Gate checklist exists, because Article 14 requires CODE + TEST +
  SECURITY + CI + OBSERVABILITY + DOCUMENTATION + BOARD EVIDENCE together;
* no placeholder token (Article 6) and no dead relative link (Article 10) survives.

Stdlib + pytest only, no network, milliseconds — it runs in the same ``pytest`` job as
``tests/unit/test_docs_integrity.py``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
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
FINDING_STATES = (
    "FIXED",
    "VERIFIED",
    "DEFERRED_WITH_REASON",
    "KNOWN_ENVIRONMENTAL",
    "NOT_REPRODUCIBLE",
    "FALSE_POSITIVE",
)
DECISION_FIELDS = ("OPTIONS", "TRADEOFFS", "DECISION", "WHY", "RISK", "TEST")

#: Fragments of the owner's Persian original that must survive verbatim.  The
#: constitution is the owner's law, not a paraphrase of it.
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

#: The nine selection criteria of Article 3 — an implementation choice must be argued
#: against them, which is impossible if the list quietly loses a member.
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

_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


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
                "zone": "governance-constitution",
                "exclusive_paths": ["ENGINEERING_CONSTITUTION.md"],
            }
        ],
        "zones": [{"id": "governance-constitution", "paths": ["ENGINEERING_CONSTITUTION.md"]}],
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


#: The full heading of every article, in both languages.  A constitution that keeps the
#: numbering but loses a title has been edited, and that must be a visible decision.
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


def test_every_article_is_registered_in_both_languages() -> None:
    text = _read(CONSTITUTION)
    assert len(PERSIAN_HEADINGS) == ARTICLE_COUNT
    assert len(ENGLISH_HEADINGS) == ARTICLE_COUNT
    for number, (persian, english) in enumerate(
        zip(PERSIAN_HEADINGS, ENGLISH_HEADINGS, strict=True), start=1
    ):
        assert f"## {persian}" in text, f"Persian Article {number} heading is missing or renamed"
        assert f"## {english}" in text, f"English Article {number} heading is missing or renamed"


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
def test_agents_md_is_the_entry_gate() -> None:
    text = _read(AGENTS_MD)
    assert "](ENGINEERING_CONSTITUTION.md)" in text, "AGENTS.md does not link the constitution"
    assert "Rule zero" in text, "AGENTS.md lost its rule-zero section"
    assert "before" in text.lower(), "AGENTS.md must state the constitution is read before work"


def test_contributing_and_readme_point_at_the_constitution() -> None:
    for path in (CONTRIBUTING_MD, README_MD):
        text = _read(path)
        linked = "](ENGINEERING_CONSTITUTION.md)" in text
        assert linked, f"{path.name} does not link the constitution"


def test_pull_request_template_carries_the_final_gate() -> None:
    """Article 14 needs a per-PR form, or the gate is only ever claimed in prose."""
    assert PR_TEMPLATE.is_file(), ".github/PULL_REQUEST_TEMPLATE.md is missing"
    text = _read(PR_TEMPLATE)
    missing = [gate for gate in FINAL_GATE_GATES if gate not in text]
    assert not missing, f"the PR template lost a Final Gate gate: {missing}"


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
