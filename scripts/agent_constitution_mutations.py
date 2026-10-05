"""Adversarial mutation campaign for the agent-constitution layer (constitution §2 L4).

A guard that has never been attacked is not evidence. ``tests/architecture/
test_agent_constitution.py`` is the enforcer of ``NAGAR_AGENT_CONSTITUTION.md``,
so this harness attacks the *contract* the same way
``scripts/pack_trust_mutations.py`` attacks the pack trust boundary: every
mutant is a real bypass attempt a future session could make — bury the
bootstrap block, soften a law, drop a status, break the mutual pin, silence an
entry point — and the enforcer must turn red for every one of them.

Guarantees:

* **stdlib only** — no dependency, no network, no production import;
* **byte-for-byte restore** — every touched file's SHA-256 is compared before
  and after, and a mismatch is a hard failure;
* **duplicate detection** — two mutants that mutate the same file in the same
  way would mean one of them proves nothing;
* **exit code** — ``0`` only when the baseline is green, every mutant is
  killed, and every file is restored. Anything else is a real defect.

Usage::

    python scripts/agent_constitution_mutations.py            # run the campaign
    python scripts/agent_constitution_mutations.py --list     # show the mutants
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
README = ROOT / "README.md"
DOCS_DOOR = ROOT / "docs" / "architecture.md"
PROTOCOL = ROOT / "docs" / "MULTI_AGENT_PROTOCOL.md"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CAMPAIGN = ROOT / "scripts" / "agent_constitution_mutations.py"
CONSTITUTION = ROOT / "NAGAR_AGENT_CONSTITUTION.md"
AGENTS = ROOT / "AGENTS.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
MODULE_MAP = ROOT / "docs" / "architecture" / "MODULE_MAP.md"
DOCS_INDEX = ROOT / "docs" / "README.md"

TARGET = "tests/architecture/test_agent_constitution.py"
BOOTSTRAP_HEADING = "MANDATORY SESSION BOOTSTRAP"
REFUSAL = "MUST NOT begin implementation before reading and applying these files"
ENFORCER = "tests/architecture/test_agent_constitution.py"


# --------------------------------------------------------------------------- #
# mutants — each returns the mutated text for one file
# --------------------------------------------------------------------------- #
def _move_bootstrap_below_the_first_section(text: str) -> str:
    start = text.index("> ### " + BOOTSTRAP_HEADING)
    end = text.index("This file is the **entry** contract")
    return f"{text[:start]}{text[end:]}\n\n> ### {BOOTSTRAP_HEADING}\n\n> read me eventually.\n"


def _rename_the_bootstrap_heading(text: str) -> str:
    return text.replace(BOOTSTRAP_HEADING, "Suggested reading")


def _soften_the_bootstrap_refusal(text: str) -> str:
    return text.replace(REFUSAL, "Should read these files when convenient")


def _make_agents_md_a_copy_of_the_laws(text: str) -> str:
    laws = "\n".join(f"### L{n} — copy of the law" for n in range(1, 6))
    return f"{text}\n{laws}\n{'padding word ' * 4000}"


def _rename_a_law_to_guidance(text: str) -> str:
    return text.replace("### L3 — NO FAKE VERIFICATION", "### L3 — verification guidance")


def _delete_a_law(text: str) -> str:
    return text.replace("### L9 — TEST HACKING IS FAILURE\n", "", 1)


def _bump_the_version_silently(text: str) -> str:
    """Bump the patch component without touching the version pinned in the enforcer."""
    return re.sub(
        r"CONSTITUTION_VERSION:\*\* `(\d+)\.(\d+)\.(\d+)`",
        lambda m: f"CONSTITUTION_VERSION:** `{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}`",
        text,
        count=1,
    )


def _break_the_mutual_pin(text: str) -> str:
    return text.replace(ENFORCER, "tests/architecture/legacy.py")


def _drop_a_gate_dimension(text: str) -> str:
    return text.replace("| 10 | adversarial review |", "| 10 | spot checks |")


def _drop_a_status_definition(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith("| `DEFERRED` |"))


def _remove_the_gates_owner_deference(text: str) -> str:
    start = text.index("**Coordination deference.**")
    end = text.index("## 6. Mandatory completion report")
    return text[:start] + text[end:]


def _delete_a_loophole_entry(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith("| B7 |"))


def _silence_contributing(text: str) -> str:
    return text.replace("NAGAR_AGENT_CONSTITUTION.md", "the engineering law file")


def _deregister_the_law_from_the_module_map(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith("| R14 |"))


def _silence_the_docs_index(text: str) -> str:
    return text.replace("../NAGAR_AGENT_CONSTITUTION.md", "../AGENTS.md")


def _silence_the_readme(text: str) -> str:
    return text.replace("`AGENTS.md` + `NAGAR_AGENT_CONSTITUTION.md`", "`AGENTS.md` and friends")


def _silence_the_architecture_front_door(text: str) -> str:
    return text.replace(" · [`NAGAR_AGENT_CONSTITUTION.md`](../NAGAR_AGENT_CONSTITUTION.md) ", " ")


def _silence_the_protocol(text: str) -> str:
    return text.replace("`NAGAR_AGENT_CONSTITUTION.md` (the persistent", "the persistent")


def _drop_the_ci_campaign_job(text: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line == "  agent-constitution-mutations:")
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "  migrate-postgres:")
    return "\n".join(lines[:start] + lines[end:])


def _gut_the_campaign(text: str) -> str:
    """Reduce the campaign to a decorative shell: no mutants left to run."""
    start = text.index("MUTANTS: tuple[tuple[str, Path, object], ...] = (")
    end = text.index("\n)\n", start)
    return text[:start] + "MUTANTS: tuple[tuple[str, Path, object], ...] = (" + text[end:]


def _blind_the_campaign(text: str) -> str:
    """Point the campaign at files that are not the contract."""
    return text.replace("NAGAR_AGENT_CONSTITUTION.md", "legacy_constitution.md")


def _make_the_ci_campaign_non_blocking(text: str) -> str:
    return text.replace(
        "  agent-constitution-mutations:\n    name: agent-constitution-mutations (agent contract "
        "plane)\n    runs-on: ubuntu-latest\n",
        "  agent-constitution-mutations:\n    name: agent-constitution-mutations (agent contract "
        "plane)\n    runs-on: ubuntu-latest\n    continue-on-error: true\n",
    )


MUTANTS: tuple[tuple[str, Path, object], ...] = (
    (
        "M01 bootstrap block moved below the first section",
        AGENTS,
        _move_bootstrap_below_the_first_section,
    ),
    ("M02 bootstrap heading renamed to a suggestion", AGENTS, _rename_the_bootstrap_heading),
    ("M03 bootstrap refusal softened", AGENTS, _soften_the_bootstrap_refusal),
    (
        "M04 AGENTS.md becomes a copy of the laws (role swap)",
        AGENTS,
        _make_agents_md_a_copy_of_the_laws,
    ),
    ("M05 law L3 renamed to guidance", CONSTITUTION, _rename_a_law_to_guidance),
    ("M06 law L9 deleted", CONSTITUTION, _delete_a_law),
    ("M07 constitution version bumped silently", CONSTITUTION, _bump_the_version_silently),
    ("M08 mutual pin broken (enforcer reference removed)", CONSTITUTION, _break_the_mutual_pin),
    ("M09 gate dimension 'adversarial review' dropped", CONSTITUTION, _drop_a_gate_dimension),
    ("M10 status DEFERRED definition dropped", CONSTITUTION, _drop_a_status_definition),
    ("M11 gates_owner deference clause removed", CONSTITUTION, _remove_the_gates_owner_deference),
    ("M12 loophole B7 deleted from the register", CONSTITUTION, _delete_a_loophole_entry),
    ("M13 CONTRIBUTING stops naming the constitution", CONTRIBUTING, _silence_contributing),
    ("M14 R14 row removed from MODULE_MAP §3", MODULE_MAP, _deregister_the_law_from_the_module_map),
    ("M15 docs index stops naming the constitution", DOCS_INDEX, _silence_the_docs_index),
    ("M16 README stops naming the constitution", README, _silence_the_readme),
    (
        "M17 architecture front door stops naming the constitution",
        DOCS_DOOR,
        _silence_the_architecture_front_door,
    ),
    ("M18 multi-agent protocol stops naming the constitution", PROTOCOL, _silence_the_protocol),
    ("M19 CI job that runs the mutation campaign is deleted", WORKFLOW, _drop_the_ci_campaign_job),
    (
        "M20 CI job that runs the campaign becomes non-blocking",
        WORKFLOW,
        _make_the_ci_campaign_non_blocking,
    ),
    (
        "M21 the mutation campaign is gutted to an empty shell",
        CAMPAIGN,
        _gut_the_campaign,
    ),
    ("M22 the mutation campaign is blinded to the real files", CAMPAIGN, _blind_the_campaign),
)


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #
def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _enforcer_is_red() -> bool:
    """True when the enforcing suite fails — i.e. the mutant was detected."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", TARGET, "--noconftest", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode != 0


def _check_for_duplicate_mutants() -> list[str]:
    """Two mutants with identical effect on the same file prove only one thing."""
    seen: dict[tuple[Path, str], str] = {}
    duplicates: list[str] = []
    for name, path, mutate in MUTANTS:
        original = path.read_text(encoding="utf-8")
        key = (path, mutate(original))
        if key in seen:
            duplicates.append(f"{name} duplicates {seen[key]}")
        seen[key] = name
    return duplicates


def run_campaign() -> int:
    duplicates = _check_for_duplicate_mutants()
    if duplicates:
        print("DUPLICATE MUTANTS (a mutant that proves nothing):")
        print("\n".join(f"  - {dup}" for dup in duplicates))
        return 1

    guarded = sorted({path for _, path, _ in MUTANTS} | {CONSTITUTION})
    digests = {path: _digest(path) for path in guarded}

    if _enforcer_is_red():
        print("BASELINE: RED — the enforcing suite must be green before it can be attacked")
        return 1
    print("BASELINE: GREEN\n")

    killed: list[str] = []
    survived: list[str] = []
    for name, path, mutate in MUTANTS:
        original = path.read_text(encoding="utf-8")
        try:
            path.write_text(mutate(original), encoding="utf-8")
            detected = _enforcer_is_red()
        finally:
            path.write_bytes(original.encode("utf-8"))
        (killed if detected else survived).append(name)
        print(f"  {'RED  (killed)' if detected else 'GREEN (SURVIVED)'}  {name}")

    # The nuclear mutant: the law leaves the repository root entirely.
    moved_to = ROOT / "docs" / CONSTITUTION.name
    shutil.move(str(CONSTITUTION), str(moved_to))
    moved_detected = _enforcer_is_red()
    shutil.move(str(moved_to), str(CONSTITUTION))
    print(
        f"  {'RED  (killed)' if moved_detected else 'GREEN (SURVIVED)'}  "
        "M23 constitution moved out of the repository root"
    )

    unrestored = [str(p) for p in guarded if _digest(p) != digests[p]]
    if unrestored:
        print("\nRESTORE FAILURE — these files were not restored byte for byte:")
        print("\n".join(f"  - {p}" for p in unrestored))
        return 1

    total = len(MUTANTS) + 1
    all_killed = len(killed) + int(moved_detected)
    print(f"\nkilled {all_killed}/{total} · survived: {survived or 'none'}")
    print("tree restored byte for byte")
    if survived or not moved_detected:
        print("CAMPAIGN: FAILED — a surviving mutant is a hole in the contract")
        return 1
    print("CAMPAIGN: PASSED")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="list the mutants and exit")
    args = parser.parse_args()
    if args.list:
        for name, path, _ in MUTANTS:
            print(f"{name}  ->  {path.relative_to(ROOT)}")
        print(
            "M23 constitution moved out of the repository root  ->  "
            f"{CONSTITUTION.relative_to(ROOT)}"
        )
        return 0
    return run_campaign()


if __name__ == "__main__":
    raise SystemExit(main())
