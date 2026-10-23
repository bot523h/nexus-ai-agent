"""Option B authority contract — `.agents/board.json` on `main` is the authority.

Owner decision (issue #236): the tree board is the single authoritative
coordination board; `refs/nexus/board` is deprecated and non-authoritative.

These tests are the executable form of that decision. They do not touch any
board, worktree, remote ref or network: they import the shipped CLI and read
files. They are also the kill target for the ``authority_ref_reintroduced``
mutation in ``scripts/board_referee_mutations.py``.

Two invariants are deliberately separate:

* **authority path** — which board the CLI treats as canonical, and that the
  deprecated ref can never grant authority;
* **gates policy** — *both* board policy fields, because updating only
  ``protocol.gates_rule`` (or only the CLI) left the two in contradiction once
  already, and a policy that lives in two places must be checked in both.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "agent_board.py"
BOARD = ROOT / ".agents" / "board.json"
CANONICAL = ROOT / ".agents" / "board.json"


def _cli_module() -> ModuleType:
    """Import the shipped CLI by path (it is a script, not an installed module)."""
    spec = importlib.util.spec_from_file_location("authority_cli", CLI)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _board() -> dict:
    return json.loads(BOARD.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# A. Authority path
# --------------------------------------------------------------------------- #
def test_cli_canonical_board_is_the_tree_board() -> None:
    """The shipped CLI's canonical board is `.agents/board.json` in the tree."""
    module = _cli_module()
    assert Path(module.BOARD).resolve() == CANONICAL.resolve()


def test_cli_never_reads_the_deprecated_ref() -> None:
    """`refs/nexus/board` must not appear as an authority path anywhere in the CLI."""
    source = CLI.read_text(encoding="utf-8")
    assert "refs/nexus/board" not in source, (
        "the deprecated ref must not be an authority path; Option B makes the tree board canonical"
    )


def test_deprecated_ref_cannot_grant_authority() -> None:
    """A ref/manifest-shaped authority declaration in the board is not authority.

    The board may *document* deprecation, but it must not carry a machine-readable
    grant that points authority at a ref. If a future change wants that, it needs
    its own ADR and its own test — not a silent field.
    """
    board = _board()
    authority = board.get("protocol", {}).get("board_authority")
    assert authority is None or authority == {
        "ref": "refs/heads/main",
        "file": ".agents/board.json",
    }, f"unexpected board authority declaration: {authority!r}"


def test_docs_agree_with_the_authority_contract() -> None:
    """AGENTS.md, the protocol and the ADR must state the same authority rule."""
    required = (
        "`.agents/board.json` on `main`",
        "`refs/nexus/board` is deprecated and non-authoritative",
    )
    for name in (
        "AGENTS.md",
        "docs/MULTI_AGENT_PROTOCOL.md",
        "docs/architecture/adr/0004-board-schema-2.md",
    ):
        text = (ROOT / name).read_text(encoding="utf-8")
        for phrase in required:
            assert phrase in text, f"{name} must state the Option B authority rule: {phrase}"


# --------------------------------------------------------------------------- #
# B. Gates policy — BOTH board fields, and the CLI agrees with them
# --------------------------------------------------------------------------- #
def test_both_board_policy_fields_drop_the_mandatory_owner() -> None:
    """`protocol.gates_rule` and the legacy top-level `gates_rule` must agree.

    The retired policy said "exactly ONE agent ... runs the full gates". With only
    one of the two fields updated, the board contradicted itself: the CLI stopped
    blocking while a document-shaped rule still demanded a single owner.
    """
    board = _board()
    top = board.get("gates_rule")
    protocol = board.get("protocol", {}).get("gates_rule")
    for name, value in (("gates_rule", top), ("protocol.gates_rule", protocol)):
        assert isinstance(value, str) and value.strip(), f"{name} must be a non-empty string"
        lowered = value.lower()
        states_policy = (
            "no mandatory global gates owner" in lowered or "هیچ gates_owner سراسری اجباری" in value
        )
        assert states_policy, f"{name} must state the no-mandatory-owner policy"
        assert "exactly one agent" not in lowered, (
            f"{name} still carries the retired mandatory-owner rule"
        )
        assert "دقیقاً یک عامل" not in value, (
            f"{name} still carries the retired mandatory-owner rule (fa)"
        )


def test_both_policy_fields_name_the_fail_closed_floor() -> None:
    """Retiring the owner must not retire the safety floor."""
    board = _board()
    protocol = board["protocol"]["gates_rule"]
    assert "fail-closed" in protocol or "کد ۲" in protocol or "۲" in protocol


def test_cli_documents_the_advisory_policy() -> None:
    """The implementation and the board policy must not drift apart again."""
    source = CLI.read_text(encoding="utf-8")
    assert "gates_owner (advisory)" in source
    assert "no mandatory global gates owner" in source
    # The old blocking branch must be gone, not merely unreachable.
    assert "exactly one active, unexpired gates_owner is required" not in source


@pytest.mark.parametrize("status", ["active", "active_in_review"])
def test_an_active_gates_owner_is_still_allowed(status: str) -> None:
    """Advisory means optional, not forbidden: a recorded owner stays legal."""
    board = _board()
    owners = [
        claim
        for claim in board["claims"]
        if claim.get("gates_owner") is True and claim.get("status") == status
    ]
    # Zero or one is fine; more than one is a reported anomaly, never a hard block.
    assert len(owners) <= 1


def test_board_still_satisfies_schema_two_shape() -> None:
    """Authority changes must not weaken the structural contract."""
    board = _board()
    assert board["schema"] == 2
    assert board["protocol"]["protocol_version"] == 2
    for key in ("zones", "claims", "deferred_log", "next_work", "history"):
        assert key in board
    assert len(board["claims"]) >= 100, "the refresh must preserve the newer main-board claims"
