"""Coordination-ref board store (protocol 11, audit A-04) — adversarial contract.

The live board is a commit on ``refs/nexus/board`` written with a
compare-and-swap push; the tree file ``.agents/board.json`` is a frozen
archive.  These tests build a *local bare origin* plus one or two clones, so
every property below is exercised against real ``git`` plumbing — no network,
no mocks of git — and each adversarial case asserts the refusal path, not just
the happy path.

Properties (each has its own test):

* a claim is published on the ref and visible from a second clone;
* the code tree stays clean (``.agents/board.json`` untouched, ``git status``
  empty) after claim/release;
* a lost compare-and-swap race is redone from a fresh read: a competitor who
  claimed first keeps the lease, the loser is refused, and exactly one commit
  per winner lands on the ref;
* an expired lease can be taken over and the generation advances; a stale
  ``--expected-generation`` is refused;
* an unreadable origin is exit 2 (never a pass) and nothing is written;
* ``check`` consults *only* the ref: a hostile ``gates_owner``/exclusive path
  planted in the code tree is ignored, a hostile ref overlap is reported;
* malformed boards (schema≠2, self-excluding ``exclusive_paths`` such as
  ``""``, ``"/"``, ``"."``, two live gates owners) cannot crash the referee and
  cannot grant a pass;
* ``--store tree`` can never write the authority;
* ``guard`` pins the frozen archive and ``init-ref`` never overwrites an
  existing ref.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "agent_board.py"
REF = "refs/nexus/board"
BRANCH_A = "arena/aaaa-agent-a"
BRANCH_B = "arena/bbbb-agent-b"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check, timeout=60
    )


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _board(*claims: dict[str, object]) -> dict[str, object]:
    """Minimal schema-2 board with exactly one live gates owner (plus *claims*)."""
    now = datetime.now(timezone.utc)
    gates = _claim(
        "task-gates",
        "arena/gates-owner",
        paths=["src/nexus_ai_agent/gates_only/"],
        gates_owner=True,
    )
    fresh = {  # never claimed: generation 0, no fencing token needed
        "task": "task-500",
        "status": "queued",
        "agent_branch": "",
        "zone": "core",
        "generation": 0,
        "gates_owner": False,
        "exclusive_paths": ["src/nexus_ai_agent/five_hundred.py"],
        "acceptance_criteria": ["x"],
        "evidence_required": ["y"],
    }
    others = [c for c in claims if c["task"] != "task-500"]
    override = [c for c in claims if c["task"] == "task-500"]
    return {
        "schema": 2,
        "updated_at": _iso(now),
        "protocol": {"protocol_version": 2},
        "zones": [{"id": "core", "paths": ["src/"]}],
        "claims": [gates, *(override or [fresh]), *others],
        "deferred_log": [],
        "next_work": [],
        "history": [],
    }


def _claim(
    task: str,
    branch: str,
    *,
    paths: list[str],
    hours: float = 20,
    generation: int = 1,
    gates_owner: bool = False,
) -> dict[str, object]:
    """An active lease; liveness is ``claimed_at + ttl_hours`` (negative *hours* = expired)."""
    now = datetime.now(timezone.utc)
    ttl = 24
    return {
        "task": task,
        "status": "active",
        "agent_branch": branch,
        "zone": "core",
        "claimed_at": _iso(now - timedelta(hours=ttl - hours)),
        "ttl_hours": ttl,
        "generation": generation,
        "gates_owner": gates_owner,
        "exclusive_paths": paths,
        "acceptance_criteria": ["x"],
        "evidence_required": ["y"],
    }


class Sandbox:
    """A bare origin + clones; ``publish`` seeds the ref with a board."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.origin = root / "origin.git"
        _git(root, "init", "--bare", "-q", "-b", "main", str(self.origin))
        seed = root / "seed"
        seed.mkdir()
        _git(seed, "init", "-q", "-b", "main")
        _git(seed, "config", "user.email", "t@example.invalid")
        _git(seed, "config", "user.name", "t")
        (seed / ".agents").mkdir()
        self.frozen = json.dumps({"schema": 2, "frozen": True}, indent=2) + "\n"
        (seed / ".agents" / "board.json").write_text(self.frozen, encoding="utf-8")
        (seed / "README.md").write_text("seed\n", encoding="utf-8")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "seed")
        _git(seed, "remote", "add", "origin", str(self.origin))
        _git(seed, "push", "-q", "origin", "main")
        self.frozen_sha = hashlib.sha256(self.frozen.encode()).hexdigest()
        self._n = 0

    def clone(self, branch: str) -> Path:
        self._n += 1
        path = self.root / f"clone{self._n}"
        _git(self.root, "clone", "-q", str(self.origin), str(path))
        _git(path, "config", "user.email", "t@example.invalid")
        _git(path, "config", "user.name", "t")
        _git(path, "checkout", "-q", "-b", branch)
        return path

    def ref_tip(self) -> str | None:
        out = _git(self.origin, "rev-parse", "--verify", "-q", REF, check=False)
        return out.stdout.strip() or None

    def ref_board(self) -> dict[str, object]:
        raw = _git(self.origin, "show", f"{REF}:board.json").stdout
        loaded: dict[str, object] = json.loads(raw)
        return loaded

    def ref_commits(self) -> int:
        out = _git(self.origin, "rev-list", "--count", REF).stdout
        return int(out.strip())

    def publish(self, board: dict[str, object]) -> str:
        """Write *board* directly onto the ref in origin (bypassing the CLI)."""
        text = json.dumps(board, indent=2) + "\n"
        blob = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=self.origin,
            input=text,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "mktree"],
            cwd=self.origin,
            input=f"100644 blob {blob}\tboard.json\n",
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        parent = self.ref_tip()
        args = ["commit-tree", tree, "-m", "test publish"]
        if parent:
            args += ["-p", parent]
        identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        commit = _git(self.origin, *identity, *args).stdout.strip()
        _git(self.origin, "update-ref", REF, commit)
        return commit


def _load(
    clone: Path, monkeypatch: pytest.MonkeyPatch, frozen_sha: str | None = None
) -> types.ModuleType:
    """Import the script bound to *clone* (ROOT/BOARD), ref store, no GitHub env."""
    spec = importlib.util.spec_from_file_location(f"agent_board_ref_{clone.name}", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", clone)
    monkeypatch.setattr(module, "BOARD", clone / ".agents" / "board.json")
    monkeypatch.setattr(module, "BOARD_REF", REF)
    if frozen_sha is not None:
        monkeypatch.setattr(module, "FROZEN_BOARD_SHA256", frozen_sha)
    monkeypatch.delenv("NEXUS_BOARD_STORE", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    return module


def _run(module: types.ModuleType, argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(sys, "argv", ["agent_board.py", *argv])
    return int(module.main())


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


# ── happy path + clean tree ──────────────────────────────────────────────


def test_claim_lands_on_the_ref_and_is_visible_from_a_second_clone(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox.publish(_board())
    a = sandbox.clone(BRANCH_A)
    mod_a = _load(a, monkeypatch, sandbox.frozen_sha)
    assert _run(mod_a, ["claim", "task-500", "--branch", BRANCH_A], monkeypatch) == 0
    out = capsys.readouterr().out
    assert "published refs/nexus/board" in out
    assert "do NOT add .agents/board.json" in out
    assert "git add .agents/board.json" not in out

    board = sandbox.ref_board()
    claims = {c["task"]: c for c in board["claims"]}  # type: ignore[union-attr]
    assert claims["task-500"]["agent_branch"] == BRANCH_A
    assert claims["task-500"]["status"] == "active"

    # the other sandbox sees the lease without any code-tree traffic
    b = sandbox.clone(BRANCH_B)
    mod_b = _load(b, monkeypatch, sandbox.frozen_sha)
    rc = _run(
        mod_b,
        ["check", "--files", "src/nexus_ai_agent/five_hundred.py", "--branch", BRANCH_B],
        monkeypatch,
    )
    assert rc == 1
    assert "task-500" in capsys.readouterr().out


def test_code_tree_stays_clean_after_claim_and_release(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox.publish(_board())
    a = sandbox.clone(BRANCH_A)
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    assert _run(mod, ["claim", "task-500", "--branch", BRANCH_A], monkeypatch) == 0
    rc = _run(
        mod,
        ["release", "task-500", "--branch", BRANCH_A, "--expected-generation", "1"],
        monkeypatch,
    )
    assert rc == 0
    assert (a / ".agents" / "board.json").read_text(encoding="utf-8") == sandbox.frozen
    assert _git(a, "status", "--porcelain").stdout == ""
    assert not (a / ".agents" / "board.lock").exists()
    released = {c["task"]: c for c in sandbox.ref_board()["claims"]}  # type: ignore[union-attr]
    assert released["task-500"]["status"] == "done"
    assert sandbox.ref_commits() == 3  # seed + claim + release


# ── compare-and-swap ─────────────────────────────────────────────────────


def test_lost_cas_race_is_redone_and_the_first_claimant_wins(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Competitor B publishes between A's read and A's push: A must not overwrite."""
    sandbox.publish(_board())
    a = sandbox.clone(BRANCH_A)
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    original_push = mod.push_ref_board
    raced = {"done": False}

    def racing_push(board: dict[str, object], expected: str | None, message: str) -> str:
        if not raced["done"]:
            raced["done"] = True
            sandbox.publish(
                _board(_claim("task-500", BRANCH_B, paths=["src/nexus_ai_agent/five_hundred.py"]))
            )
        return str(original_push(board, expected, message))

    monkeypatch.setattr(mod, "push_ref_board", racing_push)
    rc = _run(mod, ["claim", "task-500", "--branch", BRANCH_A], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 2, out  # the redo saw B's live lease and refused (existing contract: exit 2)
    assert "[cas]" in out and "moved during attempt 1" in out
    assert "ACTIVELY claimed by arena/bbbb-agent-b" in out
    winner = {c["task"]: c for c in sandbox.ref_board()["claims"]}  # type: ignore[union-attr]
    assert winner["task-500"]["agent_branch"] == BRANCH_B
    assert sandbox.ref_commits() == 2  # seed + B's claim; A wrote nothing


def test_persistent_ref_movement_is_refused_after_bounded_retries(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox.publish(_board())
    a = sandbox.clone(BRANCH_A)
    mod = _load(a, monkeypatch, sandbox.frozen_sha)

    def always_moving(board: dict[str, object], expected: str | None, message: str) -> str:
        sandbox.publish(_board())  # the ref advances every time
        raise mod.BoardCASConflict("stale info")

    monkeypatch.setattr(mod, "push_ref_board", always_moving)
    rc = _run(mod, ["claim", "task-500", "--branch", BRANCH_A], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 2
    assert "REFUSED" in out and "3 attempts" in out
    claims = {c["task"]: c for c in sandbox.ref_board()["claims"]}  # type: ignore[union-attr]
    assert claims["task-500"]["status"] == "queued"  # A never landed anything


def test_real_remote_rejects_a_stale_lease_without_mocks(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CAS primitive itself: --force-with-lease on a moved ref is a conflict."""
    sandbox.publish(_board())
    a = sandbox.clone(BRANCH_A)
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    board, tip = mod.fetch_ref_board()
    sandbox.publish(_board())  # moved
    with pytest.raises(mod.BoardCASConflict):
        mod.push_ref_board(board, tip, "stale write")
    assert sandbox.ref_commits() == 2


# ── leases, generations, takeover ────────────────────────────────────────


def test_expired_lease_takeover_advances_generation(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    expired = _claim(
        "task-500",
        BRANCH_B,
        paths=["src/nexus_ai_agent/five_hundred.py"],
        hours=-2,
        generation=4,
    )
    sandbox.publish(_board(expired))
    a = sandbox.clone(BRANCH_A)
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    # a takeover of an existing lease record needs the fencing token (task-219)
    assert _run(mod, ["claim", "task-500", "--branch", BRANCH_A], monkeypatch) == 2
    assert sandbox.ref_commits() == 1
    rc = _run(
        mod, ["claim", "task-500", "--branch", BRANCH_A, "--expected-generation", "4"], monkeypatch
    )
    assert rc == 0
    claims = {c["task"]: c for c in sandbox.ref_board()["claims"]}  # type: ignore[union-attr]
    assert claims["task-500"]["agent_branch"] == BRANCH_A
    assert claims["task-500"]["generation"] == 5


def test_live_foreign_lease_is_refused_and_nothing_is_written(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox.publish(_board(_claim("task-500", BRANCH_B, paths=["src/x.py"])))
    a = sandbox.clone(BRANCH_A)
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    assert _run(mod, ["claim", "task-500", "--branch", BRANCH_A], monkeypatch) == 2
    assert sandbox.ref_commits() == 1


def test_stale_expected_generation_is_refused_on_the_ref(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox.publish(_board(_claim("task-500", BRANCH_A, paths=["src/x.py"], generation=3)))
    a = sandbox.clone(BRANCH_A)
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    rc = _run(
        mod,
        ["release", "task-500", "--branch", BRANCH_A, "--expected-generation", "2"],
        monkeypatch,
    )
    assert rc == 2
    assert "generation" in capsys.readouterr().out.lower()
    assert sandbox.ref_commits() == 1
    rc = _run(
        mod,
        ["release", "task-500", "--branch", BRANCH_A, "--expected-generation", "3"],
        monkeypatch,
    )
    assert rc == 0
    claims = {c["task"]: c for c in sandbox.ref_board()["claims"]}  # type: ignore[union-attr]
    assert claims["task-500"]["generation"] == 4


# ── unreadable / incompatible authority ──────────────────────────────────


def test_unreachable_origin_is_exit_2_and_writes_nothing(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox.publish(_board())
    a = sandbox.clone(BRANCH_A)
    _git(a, "remote", "set-url", "origin", str(sandbox.root / "does-not-exist.git"))
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    for argv in (
        ["claim", "task-500", "--branch", BRANCH_A],
        ["check", "--files", "src/x.py", "--branch", BRANCH_A],
        ["show"],
    ):
        assert _run(mod, argv, monkeypatch) == 2, argv
        assert "UNVERIFIABLE" in capsys.readouterr().out
    assert _git(a, "status", "--porcelain").stdout == ""
    assert (a / ".agents" / "board.json").read_text(encoding="utf-8") == sandbox.frozen


def test_missing_ref_is_unverifiable_not_an_empty_pass(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    a = sandbox.clone(BRANCH_A)  # origin has no refs/nexus/board yet
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    assert _run(mod, ["check", "--files", "src/x.py", "--branch", BRANCH_A], monkeypatch) == 2
    assert "UNVERIFIABLE" in capsys.readouterr().out


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"schema": 3, "claims": []}, id="schema-3"),
        pytest.param({"schema": 2, "claims": {"not": "a list"}}, id="claims-not-list"),
        pytest.param([], id="json-array"),
    ],
)
def test_incompatible_board_generation_on_the_ref_is_exit_2(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: object,
) -> None:
    sandbox.publish(payload)  # type: ignore[arg-type]
    a = sandbox.clone(BRANCH_A)
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    assert _run(mod, ["check", "--files", "src/x.py", "--branch", BRANCH_A], monkeypatch) == 2
    assert _run(mod, ["claim", "task-500", "--branch", BRANCH_A], monkeypatch) == 2
    assert "UNVERIFIABLE" in capsys.readouterr().out
    assert sandbox.ref_commits() == 1


def test_non_json_board_on_the_ref_is_exit_2(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox.publish(_board())
    a = sandbox.clone(BRANCH_A)
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    monkeypatch.setattr(mod, "BOARD_REF_FILE", "README.md")  # exists in no ref tree → show fails
    assert _run(mod, ["check", "--files", "src/x.py", "--branch", BRANCH_A], monkeypatch) == 2


# ── the ref is the only source ───────────────────────────────────────────


def test_hostile_tree_board_is_ignored_in_ref_mode(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A planted tree board (fake gates owner, world-fence path) must not matter.

    Only the archive-modified verdict fires — and it fires as exit 1 with a fix
    hint, never as a pass and never by *adopting* the planted claims.
    """
    sandbox.publish(_board())
    a = sandbox.clone(BRANCH_A)
    hostile = _board(
        _claim("task-evil", BRANCH_B, paths=["", "/", ".", "src/"], gates_owner=True),
    )
    (a / ".agents" / "board.json").write_text(json.dumps(hostile), encoding="utf-8")
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    rc = _run(mod, ["check", "--files", "src/x.py", "--branch", BRANCH_A], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 1
    assert "task-evil" not in out  # the planted claims were never read as authority
    assert "ARCHIVE MODIFIED" in out and "git checkout origin/main -- .agents/board.json" in out
    # restoring the archive makes the same check pass against the real ref
    (a / ".agents" / "board.json").write_text(sandbox.frozen, encoding="utf-8")
    assert _run(mod, ["check", "--files", "src/x.py", "--branch", BRANCH_A], monkeypatch) == 0
    assert "single authority" in capsys.readouterr().out


@pytest.mark.parametrize("fence", ["", "/", ".", "./", "*", "**", "../"])
def test_world_fence_on_the_ref_is_inert_and_cannot_crash(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, fence: str
) -> None:
    """A foreign claim trying to fence the whole world blocks nobody and crashes nothing.

    The matcher honours directory prefixes (``dir/``) and exact files only, so
    ``""``/``"/"``/``"."``/``"*"`` cannot be used to deny every other agent —
    while a genuinely scoped sibling claim on the same board still fences.
    """
    hostile = _claim("task-fence", BRANCH_B, paths=[fence])
    honest = _claim("task-honest", "arena/cccc-agent-c", paths=["src/honest/"])
    sandbox.publish(_board(hostile, honest))
    a = sandbox.clone(BRANCH_A)
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    assert _run(mod, ["check", "--files", "docs/x.md", "--branch", BRANCH_A], monkeypatch) == 0
    assert (
        _run(mod, ["check", "--files", "src/honest/a.py", "--branch", BRANCH_A], monkeypatch) == 1
    )


@pytest.mark.parametrize(
    "paths",
    [pytest.param(None, id="none"), pytest.param(42, id="int"), pytest.param({"a": 1}, id="dict")],
)
def test_non_list_exclusive_paths_on_the_ref_is_unverifiable_not_a_pass(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, paths: object
) -> None:
    hostile = _claim("task-weird", BRANCH_B, paths=[])
    hostile["exclusive_paths"] = paths
    sandbox.publish(_board(hostile))
    a = sandbox.clone(BRANCH_A)
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    rc = _run(mod, ["check", "--files", "docs/x.md", "--branch", BRANCH_A], monkeypatch)
    assert rc in (0, 2)  # inert or unverifiable — a crash (traceback) would be a test error


def test_malformed_claim_on_the_ref_is_unverifiable(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    board = _board(_claim("task-bad", BRANCH_B, paths=["src/"], gates_owner=True))
    board["claims"][2]["claimed_at"] = 12345  # type: ignore[index]  # not a timestamp string
    sandbox.publish(board)
    a = sandbox.clone(BRANCH_A)
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    rc = _run(mod, ["check", "--files", "src/x.py", "--branch", BRANCH_A], monkeypatch)
    assert rc == 2
    assert "UNVERIFIABLE" in capsys.readouterr().out


def test_two_live_gates_owners_on_the_ref_is_exit_2(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox.publish(_board(_claim("task-g2", BRANCH_B, paths=["src/other/"], gates_owner=True)))
    a = sandbox.clone(BRANCH_A)
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    rc = _run(mod, ["check", "--files", "docs/x.md", "--branch", BRANCH_A], monkeypatch)
    assert rc == 2
    assert "exactly one live gates_owner" in capsys.readouterr().out


def test_expired_gates_owner_is_not_a_live_authority(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    board = _board()
    board["claims"][0]["claimed_at"] = _iso(datetime.now(timezone.utc) - timedelta(hours=30))  # type: ignore[index]
    sandbox.publish(board)
    a = sandbox.clone(BRANCH_A)
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    rc = _run(mod, ["check", "--files", "docs/x.md", "--branch", BRANCH_A], monkeypatch)
    assert rc == 2
    assert "found 0" in capsys.readouterr().out


# ── bypass attempts ──────────────────────────────────────────────────────


def test_tree_store_cannot_touch_the_authority(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox.publish(_board())
    a = sandbox.clone(BRANCH_A)
    tree_board = _board()
    (a / ".agents" / "board.json").write_text(json.dumps(tree_board), encoding="utf-8")
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    before = sandbox.ref_tip()
    assert (
        _run(mod, ["--store", "tree", "claim", "task-500", "--branch", BRANCH_A], monkeypatch) == 0
    )
    assert sandbox.ref_tip() == before  # authority unchanged
    on_ref = {c["task"]: c for c in sandbox.ref_board()["claims"]}  # type: ignore[union-attr]
    assert on_ref["task-500"]["status"] == "queued"
    assert on_ref["task-500"]["agent_branch"] != BRANCH_A
    # ...and the CI guard catches the tree edit that tree mode produced
    assert _run(mod, ["guard"], monkeypatch) == 1


def test_env_store_tree_is_overridden_by_explicit_ref(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox.publish(_board())
    a = sandbox.clone(BRANCH_A)
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    monkeypatch.setenv("NEXUS_BOARD_STORE", "tree")
    assert mod.resolve_store(None) == "tree"
    assert (
        _run(mod, ["--store", "ref", "claim", "task-500", "--branch", BRANCH_A], monkeypatch) == 0
    )
    assert any(c["task"] == "task-500" for c in sandbox.ref_board()["claims"])  # type: ignore[union-attr]


def test_unknown_store_is_rejected() -> None:
    spec = importlib.util.spec_from_file_location("agent_board_store_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(SystemExit):
        module.resolve_store(types.SimpleNamespace(store="github"))


def test_init_ref_never_overwrites_an_existing_ref(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    a = sandbox.clone(BRANCH_A)
    seed = a / "seed.json"
    seed.write_text(json.dumps(_board()), encoding="utf-8")
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    assert _run(mod, ["init-ref", "--from", str(seed)], monkeypatch) == 0
    first = sandbox.ref_tip()
    assert first is not None
    authority = sandbox.ref_board()["protocol"]
    assert authority["board_authority"]["ref"] == REF  # type: ignore[index, call-overload]
    assert _run(mod, ["init-ref", "--from", str(seed)], monkeypatch) == 2
    assert "never overwrites" in capsys.readouterr().out
    assert sandbox.ref_tip() == first


def test_init_ref_refuses_non_schema_2_seed(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = sandbox.clone(BRANCH_A)
    seed = a / "seed.json"
    seed.write_text(json.dumps({"schema": 1}), encoding="utf-8")
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    assert _run(mod, ["init-ref", "--from", str(seed)], monkeypatch) == 2
    assert sandbox.ref_tip() is None


# ── frozen archive ───────────────────────────────────────────────────────


def test_guard_pins_the_frozen_archive(sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    a = sandbox.clone(BRANCH_A)
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    assert _run(mod, ["guard", "--print"], monkeypatch) == 0
    (a / ".agents" / "board.json").write_text(sandbox.frozen + "\n", encoding="utf-8")
    assert _run(mod, ["guard"], monkeypatch) == 1
    (a / ".agents" / "board.json").unlink()
    assert _run(mod, ["guard"], monkeypatch) == 1


def test_repository_archive_matches_the_pinned_digest() -> None:
    """The real tree file is byte-identical to the frozen constant (what CI enforces)."""
    spec = importlib.util.spec_from_file_location("agent_board_pin_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    actual = hashlib.sha256((REPO / ".agents" / "board.json").read_bytes()).hexdigest()
    assert actual == module.FROZEN_BOARD_SHA256


def test_show_in_ref_mode_is_read_only_unless_gc_frees_a_lease(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox.publish(_board())
    a = sandbox.clone(BRANCH_A)
    mod = _load(a, monkeypatch, sandbox.frozen_sha)
    assert _run(mod, ["show"], monkeypatch) == 0
    assert sandbox.ref_commits() == 1  # nothing to gc → nothing written
    sandbox.publish(_board(_claim("task-old", BRANCH_B, paths=["src/old/"], hours=-3)))
    assert _run(mod, ["show"], monkeypatch) == 0
    assert sandbox.ref_commits() == 3  # gc of the expired lease was persisted
    assert "published" in capsys.readouterr().out
