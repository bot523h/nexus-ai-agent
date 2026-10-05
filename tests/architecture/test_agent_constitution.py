"""Agent-constitution layer: the cross-session contract is code, not prose (law R14).

The repository's own doctrine says a structural claim must name the test that
keeps it true (``docs/README.md``, "Rules of this map" §3; ``MODULE_MAP.md`` §3).
``NAGAR_AGENT_CONSTITUTION.md`` is such a claim: it binds *every future agent
session*, including sessions that inherit no memory of this one. A Markdown file
alone cannot bind anything — an agent can skip it, soften it, or delete it — so
the bootstrap law and the laws themselves are pinned here.

What this module proves, stdlib-only and in milliseconds:

1. the constitution exists at the repository root, is versioned, and carries an
   amendment record (so a silent rewrite is impossible);
2. every non-negotiable law L0–L10 is present under its canonical heading;
3. the completion vocabulary is the closed five-word set, each defined;
4. all sixteen completion-gate dimensions are enumerated;
5. the traceability chain and the "AI is not authority" pipeline are intact;
6. ``AGENTS.md`` carries the mandatory bootstrap block, *above* its first
   section, and does **not** become a copy of the constitution;
7. every entry point a fresh agent may land on points at the constitution, and
   the constitution points back at this enforcer (mutual pin — removing either
   one breaks the suite);
8. the constitution defers to the repository's existing authorities instead of
   bypassing them.

Two positive controls at the bottom prove the guards are not vacuous: a
tampered document must be detected, otherwise the guard is decoration.

Amendment discipline (constitution §8.1.3): changing a protected section
requires updating this file in the *same commit*. A red test after an edit is
the amendment process working, not an obstacle to route around.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
CONSTITUTION = ROOT / "NAGAR_AGENT_CONSTITUTION.md"
AGENTS = ROOT / "AGENTS.md"
README = ROOT / "README.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
DOCS_INDEX = ROOT / "docs" / "README.md"
ARCHITECTURE_DOOR = ROOT / "docs" / "architecture.md"
PROTOCOL = ROOT / "docs" / "MULTI_AGENT_PROTOCOL.md"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CAMPAIGN = "scripts/agent_constitution_mutations.py"
CI_JOB = "agent-constitution-mutations"
MODULE_MAP = ROOT / "docs" / "architecture" / "MODULE_MAP.md"

#: Pinned by the amendment law: bumping the constitution version without
#: updating this constant is exactly the "silent rewrite" the law forbids.
EXPECTED_VERSION = "1.0.0"
ENFORCER_PATH = "tests/architecture/test_agent_constitution.py"

#: Canonical law headings — the ids are the vocabulary agents cite in reports.
LAWS: dict[str, str] = {
    "L0": "BOOTSTRAP",
    "L1": "TRUTH BEFORE CODE",
    "L2": "NO SUPERFICIAL WORK",
    "L3": "NO FAKE VERIFICATION",
    "L4": "ADVERSARIAL ENGINEERING",
    "L5": "AUTHORITY DISCIPLINE",
    "L6": "AI IS NOT AUTHORITY",
    "L7": "NO FAKE DEFAULTS",
    "L8": "FUTURE-PROOFING",
    "L9": "TEST HACKING IS FAILURE",
    "L10": "EXACT-HEAD PROOF",
}

#: §4 — closed vocabulary. A sixth word, or a redefined one, breaks reporting.
STATUS_WORDS = (
    "VERIFIED",
    "VERIFIED_WITH_LIMITATIONS",
    "HARDENED_BUT_NOT_COMPLETE",
    "BLOCKED",
    "DEFERRED",
)

#: §5 — the sixteen gate dimensions, verbatim from the table's first column.
GATE_DIMENSIONS = (
    "implementation",
    "integration",
    "regression",
    "negative cases",
    "boundary cases",
    "failure & recovery",
    "security",
    "persistence",
    "concurrency",
    "adversarial review",
    "diff hygiene",
    "exact commit",
    "exact-head CI",
    "review feedback",
    "limitations",
    "future-generation impact",
)

#: §2 — the end-to-end traceability chain (L2) and the authority pipeline (L6).
TRACEABILITY_CHAIN = (
    "Caller → Intent → Decision → Authority → Execution → "
    "State → Verification → Evidence → Artifact"
)
AUTHORITY_PIPELINE = "Intent → Policy → Command → Execution → Verification"

#: §10 — the documented bypass attempts; each must remain on the register.
LOOPHOLE_IDS = tuple(f"B{n}" for n in range(1, 13))

BOOTSTRAP_HEADING = "MANDATORY SESSION BOOTSTRAP"
BOOTSTRAP_READINGS = (
    "`AGENTS.md`",
    "`NAGAR_AGENT_CONSTITUTION.md`",
    "mission-specific instructions",
)
BOOTSTRAP_REFUSAL = "MUST NOT begin implementation before reading and applying these files"
BOOTSTRAP_PURPOSE = "persistent cross-session engineering contract for Nagar"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _level2_section(text: str, title_start: str) -> str:
    """Return the body of a `## …` section (up to the next `## ` heading)."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("## ") and title_start in line]
    assert starts, f"no `## ` section whose title contains {title_start!r}"
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def _missing(text: str, needles: tuple[str, ...] | list[str]) -> list[str]:
    return [needle for needle in needles if needle not in text]


def _bootstrap_block(agents_text: str) -> str:
    """The bootstrap block as written — from its heading to the end of the quote."""
    start = agents_text.index(BOOTSTRAP_HEADING)
    end = agents_text.index(BOOTSTRAP_PURPOSE) + len(BOOTSTRAP_PURPOSE)
    return agents_text[start:end]


def _bootstrap_is_well_formed(agents_text: str) -> list[str]:
    """Return the defects of the bootstrap block in ``agents_text`` (empty = sound)."""
    problems: list[str] = []
    if BOOTSTRAP_HEADING not in agents_text:
        return [f"missing the {BOOTSTRAP_HEADING!r} heading"]
    missing = _missing(agents_text, [*BOOTSTRAP_READINGS, BOOTSTRAP_REFUSAL, BOOTSTRAP_PURPOSE])
    problems.extend(f"bootstrap block is missing {item!r}" for item in missing)
    first_section = agents_text.find("\n## 1.")
    if first_section == -1:
        problems.append("AGENTS.md has no `## 1.` section to place the bootstrap above")
    elif agents_text.find(BOOTSTRAP_HEADING) > first_section:
        problems.append("the bootstrap block sits below the first section — it can be skipped")
    return problems


# --------------------------------------------------------------------------- #
# 1. the constitution exists, is versioned, and is amendable only in the open
# --------------------------------------------------------------------------- #
def test_the_constitution_lives_at_the_repository_root() -> None:
    assert CONSTITUTION.is_file(), (
        "NAGAR_AGENT_CONSTITUTION.md is missing from the repository root — the "
        "cross-session contract has no home (constitution §8.2)."
    )
    assert len(_text(CONSTITUTION).splitlines()) > 200, "the constitution was emptied"


def test_the_constitution_is_versioned_and_carries_an_amendment_record() -> None:
    text = _text(CONSTITUTION)
    version = re.search(r"CONSTITUTION_VERSION:\*\* `([0-9]+\.[0-9]+\.[0-9]+)`", text)
    amended = re.search(r"AMENDED_AT:\*\* `(\d{4}-\d{2}-\d{2})`", text)
    assert version and amended, "the version header (CONSTITUTION_VERSION / AMENDED_AT) is missing"
    assert version.group(1) == EXPECTED_VERSION, (
        f"constitution version is {version.group(1)} but this enforcer pins {EXPECTED_VERSION}. "
        "A protected-section change must update BOTH, in one commit (constitution §8.1.3)."
    )
    record = _level2_section(text, "Amendment record")
    assert EXPECTED_VERSION in record, "the amendment record does not list the current version"
    amendment_law = _level2_section(text, "Amendment law")
    for clause in ("WEAKENED", "EXCEPTION", "CONSTITUTION_VERSION"):
        assert clause in amendment_law, f"the amendment law lost its {clause!r} clause"


# --------------------------------------------------------------------------- #
# 2. the non-negotiable laws
# --------------------------------------------------------------------------- #
def test_every_non_negotiable_law_is_present_under_its_canonical_heading() -> None:
    text = _text(CONSTITUTION)
    missing = [
        f"{law_id} — {title}"
        for law_id, title in LAWS.items()
        if f"### {law_id} — {title}" not in text
    ]
    assert not missing, "laws removed or renamed from the constitution:\n" + "\n".join(missing)


def test_the_future_proofing_law_states_the_four_generation_questions() -> None:
    law = _text(CONSTITUTION).split("### L8 — FUTURE-PROOFING")[1].split("### L9")[0]
    assert "7.3" in law, "L8 no longer points at the four-generation question set"
    questions = _level2_section(_text(CONSTITUTION), "Future-generation contract")
    for generation in ("Nagar 2", "Nagar 3", "Nagar 4"):
        assert generation in questions, (
            f"{generation} is missing from the future-generation contract"
        )


def test_the_traceability_chain_and_the_authority_pipeline_are_intact() -> None:
    text = _text(CONSTITUTION)
    assert TRACEABILITY_CHAIN in text, (
        "the Caller→…→Artifact chain was broken — completion becomes unprovable (L2)"
    )
    assert AUTHORITY_PIPELINE in text, (
        "the Intent→Policy→Command→Execution→Verification seam is gone (L6)"
    )


def test_exact_head_proof_keeps_the_six_states_distinct() -> None:
    law = _text(CONSTITUTION).split("### L10 — EXACT-HEAD PROOF")[1].split("## 3.")[0]
    for state in (
        "LOCAL BRANCH",
        "PR HEAD",
        "CI HEAD",
        "REVIEWED HEAD",
        "MERGED MAIN",
        "DEPLOYED ARTIFACT",
    ):
        assert state in law, f"L10 no longer distinguishes {state!r} — proof can be laundered"


# --------------------------------------------------------------------------- #
# 3. completion vocabulary and gate
# --------------------------------------------------------------------------- #
def test_the_completion_vocabulary_is_the_closed_set_of_five() -> None:
    vocabulary = _level2_section(_text(CONSTITUTION), "Completion vocabulary")
    missing = [word for word in STATUS_WORDS if f"`{word}` |" not in vocabulary]
    assert not missing, "status definitions missing from §4:\n" + "\n".join(missing)
    assert "five statuses exist" in vocabulary, "§4 no longer declares the vocabulary closed"
    assert "must not change a status to make a report look better" in vocabulary, (
        "the anti-shopping rule was softened"
    )


def test_the_completion_gate_enumerates_every_dimension() -> None:
    gate = _level2_section(_text(CONSTITUTION), "Completion Gate")
    missing = [dimension for dimension in GATE_DIMENSIONS if f"| {dimension} |" not in gate]
    assert not missing, "completion-gate dimensions dropped:\n" + "\n".join(missing)
    assert "N/A" in gate, "the gate lost its explicit N/A rule (a skipped row must be declared)"
    assert "gates_owner" in gate, (
        "the gate bypasses the board's single-gates-owner rule (AGENTS.md §1)"
    )


def test_the_mandatory_report_template_is_pinned() -> None:
    report = _level2_section(_text(CONSTITUTION), "Mandatory completion report")
    for field in (
        "FILES CREATED / CHANGED",
        "BOOTSTRAP ATTESTATION",
        "LIMITATIONS",
        "CONFLICT REGISTER",
        "EXCEPTIONS",
        "FUTURE-GENERATION IMPACT",
        "GIT PROOF",
        "STATUS",
    ):
        assert field in report, f"the completion report no longer requires {field!r}"


def test_the_loophole_register_is_present_and_pinned() -> None:
    register = _level2_section(_text(CONSTITUTION), "Loophole register")
    missing = [bypass for bypass in LOOPHOLE_IDS if f"| {bypass} |" not in register]
    assert not missing, "bypass entries removed from the loophole register:\n" + "\n".join(missing)


# --------------------------------------------------------------------------- #
# 4. bootstrap: AGENTS.md stays an entry contract, not a copy
# --------------------------------------------------------------------------- #
def test_agents_md_carries_the_mandatory_bootstrap_block_at_the_top() -> None:
    problems = _bootstrap_is_well_formed(_text(AGENTS))
    assert not problems, "AGENTS.md bootstrap defects:\n" + "\n".join(problems)


def test_agents_md_is_an_entry_contract_not_a_copy_of_the_constitution() -> None:
    agents_text = _text(AGENTS)
    constitution_text = _text(CONSTITUTION)
    restated = [
        f"{law_id} — {title}"
        for law_id, title in LAWS.items()
        if f"### {law_id} — {title}" in agents_text
    ]
    assert not restated, (
        "AGENTS.md restates the laws; it must stay a bootstrap/entry contract and defer to "
        "NAGAR_AGENT_CONSTITUTION.md:\n" + "\n".join(restated)
    )
    assert len(agents_text.split()) < len(constitution_text.split()), (
        "AGENTS.md is longer than the constitution — the two files have swapped roles"
    )
    assert "NAGAR_AGENT_CONSTITUTION.md" in agents_text, "AGENTS.md does not name the constitution"


# --------------------------------------------------------------------------- #
# 5. discovery: every door a fresh agent can walk through points at the law
# --------------------------------------------------------------------------- #
def test_every_entry_point_names_the_constitution() -> None:
    for path in (README, CONTRIBUTING, DOCS_INDEX, ARCHITECTURE_DOOR, PROTOCOL, MODULE_MAP):
        assert "NAGAR_AGENT_CONSTITUTION.md" in _text(path), (
            f"{path.relative_to(ROOT)} does not point at the constitution — an agent entering "
            "through it would never learn the law exists"
        )


def test_the_module_map_registers_this_enforcer_as_a_boundary_law() -> None:
    laws = _level2_section(_text(MODULE_MAP), "Boundary laws")
    assert "R14" in laws, "R14 (agent constitution) is not registered in MODULE_MAP §3"
    assert "test_agent_constitution.py" in laws, "R14 does not name its enforcing test"


# --------------------------------------------------------------------------- #
# 6. the constitution defers to existing authority instead of bypassing it
# --------------------------------------------------------------------------- #
def test_the_constitution_does_not_bypass_repository_authority() -> None:
    text = _text(CONSTITUTION)
    precedence = _level2_section(text, "Precedence and conflicts")
    for authority in ("docs/DECISION_LOG.md", ".agents/board.json", "AGENTS.md"):
        assert authority in precedence, f"the precedence order omits {authority!r}"
    assert "never a source of truth" in precedence, "memory/records are no longer demoted"
    assert ENFORCER_PATH in text, (
        "the constitution no longer names its enforcer — the mutual pin is broken (§10 B2)"
    )
    authority_map = _level2_section(text, "Authority map")
    for row in ("Source of truth", "Cache", "Projection", "Evidence"):
        assert row in authority_map, f"the authority map lost its {row!r} role (L5)"


def test_the_mutation_campaign_runs_in_ci_on_every_push() -> None:
    """A campaign nobody runs is decoration — the CI job is part of the contract."""
    lines = _text(WORKFLOW).splitlines()
    assert f"  {CI_JOB}:" in lines, f"the {CI_JOB!r} job was removed from ci.yml"
    start = lines.index(f"  {CI_JOB}:")
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("  ") and not line.startswith("    "):
            break
        body.append(line)
    executable = "\n".join(line for line in body if not line.strip().startswith("#"))
    assert f"python {CAMPAIGN}" in executable, f"{CI_JOB} no longer runs {CAMPAIGN}"
    assert "continue-on-error" not in executable, f"{CI_JOB} must stay blocking"
    assert "git diff --exit-code" in executable, f"{CI_JOB} no longer proves the tree was restored"


# --------------------------------------------------------------------------- #
# positive controls — a guard that cannot fail is not a guard
# --------------------------------------------------------------------------- #
def test_the_bootstrap_guard_rejects_a_removed_buried_or_softened_block() -> None:
    sound = _text(AGENTS)
    assert _bootstrap_is_well_formed(sound) == [], (
        "the guard is too strict for the current AGENTS.md"
    )
    block = _bootstrap_block(sound)

    removed = sound.replace(BOOTSTRAP_HEADING, "Suggested reading")
    assert _bootstrap_is_well_formed(removed), "a deleted bootstrap block was accepted"

    # The block survives but sits after the first section, where a skimming agent misses it.
    buried = "# AGENTS.md\n\n## 1. The five rules\n\nclaim before you code.\n\n" + block
    assert _bootstrap_is_well_formed(buried), (
        "a bootstrap block below the first section was accepted"
    )

    softened = sound.replace(BOOTSTRAP_REFUSAL, "Should read these files when convenient")
    assert _bootstrap_is_well_formed(softened), "a bootstrap block without the refusal was accepted"


def test_the_law_guard_rejects_a_softened_law() -> None:
    text = _text(CONSTITUTION)
    assert not _missing(text, [f"### {law_id} — {title}" for law_id, title in LAWS.items()])

    softened = text.replace("### L3 — NO FAKE VERIFICATION", "### L3 — verification guidance")
    assert _missing(softened, ["### L3 — NO FAKE VERIFICATION"]), "a softened law was accepted"

    gutted = text.replace("### L9 — TEST HACKING IS FAILURE\n", "", 1)
    assert _missing(gutted, ["### L9 — TEST HACKING IS FAILURE"]), "a deleted law was accepted"
