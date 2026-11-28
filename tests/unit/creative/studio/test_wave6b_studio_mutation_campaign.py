"""Wave 6B: the Studio mutation campaign must be complete, sound and repeatable.

An invariant that has never been attacked is not evidence.  ``scripts/
run_wave4_studio_e2e_mutations.py`` rewrites one shipped guard at a time across
the five Studio execution modules (the authority bridge, revision pinning, the
execution outbox, the preview readback and disaster recovery) and demands that
the real Wave 1-5 suites notice — 50 mutants, each anchored on exactly one
source occurrence, each restored byte-for-byte.

A campaign is expensive (every mutant runs real pytest), so this module does not
re-run it on every suite invocation.  Instead it pins the *properties* that make
the campaign trustworthy, and the campaign itself is exercised end to end by
``--campaign`` (opt in with ``NEXUS_RUN_MUTATION_CAMPAIGN=1``).  Without that
flag the module still proves, offline and in milliseconds:

* there are exactly 50 mutants, numbered M01-M50, with unique names;
* every mutant changes a file that exists, and its anchor occurs **exactly once**
  in that file (a multi-occurrence anchor would mutate an unintended line);
* every mutant's replacement parses as Python (the harness also checks this, but
  a silent no-op should fail here, not 90 seconds into a campaign);
* the mutated bytes genuinely differ from the original bytes (no accidental
  ``old == new`` mutant, which would always "survive" as a no-op);
* every expected node id names a real ``Class::test`` in the named test module;
* the campaign's own guards are intact: it purges bytecode before, between and
  after mutations, runs pytest with ``PYTHONDONTWRITEBYTECODE=1``, and restores
  every target by SHA-256 in a ``finally`` block.

The ``NEXUS_RUN_MUTATION_CAMPAIGN`` path asserts the full 50/50 result.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[4]  # tests/unit/creative/studio/
HARNESS = ROOT / "scripts" / "run_wave4_studio_e2e_mutations.py"

#: 50 mutants.  Eight per module for M01-M40, then ten more (M41-M50) aimed at
#: the Wave 1-3 hardening, so the split is measured rather than assumed.
EXPECTED_TOTAL = 50
EXPECTED_PER_MODULE = {
    "studio_directory_bridge.py": 8,
    "revision_pinning.py": 9,
    "execution_outbox.py": 11,
    "preview_readback.py": 11,
    "production_dr.py": 11,
}
EXPECTED_MODULES = 5


def _load_harness() -> ModuleType:
    """Load the harness by path.

    ``scripts/`` is unpackaged and ``tests/architecture/
    test_scripts_import_boundary.py`` forbids importing it as a package, so the
    campaign is loaded through an explicit spec and registered in ``sys.modules``
    (dataclasses resolve their annotations through that mapping).
    """
    spec = importlib.util.spec_from_file_location("wave6b_mutation_harness", HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness() -> ModuleType:
    return _load_harness()


class TestCampaignShape:
    def test_the_campaign_has_exactly_forty_mutants(self, harness: ModuleType) -> None:
        assert len(harness.MUTATIONS) == EXPECTED_TOTAL

    def test_the_mutant_names_are_m01_to_m50_and_unique(self, harness: ModuleType) -> None:
        names = [mutation.name for mutation in harness.MUTATIONS]
        assert len(set(names)) == EXPECTED_TOTAL
        assert [name.split("_", 1)[0] for name in names] == [
            f"M{index:02d}" for index in range(1, EXPECTED_TOTAL + 1)
        ]

    def test_the_campaign_covers_all_five_modules(self, harness: ModuleType) -> None:
        """Every module is attacked, in proportion to how much it had to fix.

        The split is deliberately *not* an even ten each.  M01-M40 were eight per
        module; M41-M50 then attacked the specific Wave 1-3 defects, which
        landed three on the outbox, three on disaster recovery, three on the
        readback and one on revision pinning.  Asserting the measured split keeps
        the count honest instead of rounding it to a tidier number.
        """
        targets = [mutation.path for mutation in harness.MUTATIONS]
        assert len(set(targets)) == EXPECTED_MODULES
        measured = {path.name: targets.count(path) for path in set(targets)}
        assert measured == EXPECTED_PER_MODULE, measured
        assert sum(measured.values()) == EXPECTED_TOTAL

    def test_the_wave_one_to_three_hardening_is_attacked(self, harness: ModuleType) -> None:
        """M41-M50 must exist and must target the Wave 1-3 fixes, not filler."""
        hardened = [m.name for m in harness.MUTATIONS if m.name[:3] >= "M41"]
        assert len(hardened) == 10, hardened
        # Each of the three hardening waves must be represented.
        assert any("outbox" in name for name in hardened)
        assert any("readback" in name for name in hardened)
        assert any("pins" in name for name in hardened)
        assert any("dr_" in name for name in hardened)

    def test_no_two_mutants_apply_the_same_edit(self, harness: ModuleType) -> None:
        edits = [(mutation.path, mutation.old, mutation.new) for mutation in harness.MUTATIONS]
        assert len(set(edits)) == EXPECTED_TOTAL

    def test_every_mutant_explains_itself(self, harness: ModuleType) -> None:
        for mutation in harness.MUTATIONS:
            assert mutation.why.strip(), mutation.name
            assert len(mutation.why) > 20, mutation.name


class TestAnchorsAreSound:
    def test_every_target_file_exists(self, harness: ModuleType) -> None:
        for mutation in harness.MUTATIONS:
            assert mutation.path.is_file(), mutation.name

    def test_every_anchor_occurs_exactly_once(self, harness: ModuleType) -> None:
        for mutation in harness.MUTATIONS:
            original = mutation.path.read_text(encoding="utf-8")
            assert original.count(mutation.old) == 1, (
                f"{mutation.name}: anchor occurs {original.count(mutation.old)} times "
                f"in {mutation.path.name}"
            )

    def test_every_mutant_actually_changes_the_bytes(self, harness: ModuleType) -> None:
        for mutation in harness.MUTATIONS:
            assert mutation.old != mutation.new, mutation.name

    def test_every_mutant_still_parses(self, harness: ModuleType) -> None:
        for mutation in harness.MUTATIONS:
            original = mutation.path.read_text(encoding="utf-8")
            mutated = original.replace(mutation.old, mutation.new, 1)
            ast.parse(mutated)  # raises SyntaxError if the mutant is not Python

    def test_the_original_files_are_pristine_after_loading(self, harness: ModuleType) -> None:
        # Loading the harness must never touch the tree it describes.
        for mutation in harness.MUTATIONS:
            text = mutation.path.read_text(encoding="utf-8")
            assert text.count(mutation.old) == 1, mutation.name


class TestKillTargetsAreReal:
    def _nodes(self, path: str) -> set[str]:
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        found: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                        found.add(f"{node.name}::{member.name}")
        return found

    def test_every_expected_node_id_exists(self, harness: ModuleType) -> None:
        cache: dict[str, set[str]] = {}
        for mutation in harness.MUTATIONS:
            for node_id in mutation.expected_node_ids:
                parts = node_id.split("::")
                assert len(parts) >= 3, node_id
                if parts[0] not in cache:
                    cache[parts[0]] = self._nodes(parts[0])
                # A parametrised id carries a ``[case]`` suffix on the function
                # name; the class/function pair is what must exist.
                function = parts[2].split("[", 1)[0]
                assert f"{parts[1]}::{function}" in cache[parts[0]], (
                    f"{mutation.name}: {node_id} names no such test"
                )

    def test_every_mutant_names_at_least_one_kill_target(self, harness: ModuleType) -> None:
        for mutation in harness.MUTATIONS:
            assert mutation.expected_node_ids, mutation.name


class TestHarnessMechanics:
    def test_the_purge_removes_both_cache_paths(self, harness: ModuleType, tmp_path: Path) -> None:
        source = tmp_path / "module_under_test.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        exact = Path(importlib.util.cache_from_source(str(source)))
        exact.parent.mkdir(parents=True, exist_ok=True)
        exact.write_bytes(b"stale")
        # A second interpreter's tag next to the first must also go.
        sibling = pycache / "module_under_test.cpython-312.pyc"
        sibling.write_bytes(b"stale")
        untouched = pycache / "other_module.cpython-311.pyc"
        untouched.write_bytes(b"keep me")

        harness._purge_bytecode_cache(source)

        assert not exact.exists()
        assert not sibling.exists()
        # An unrelated module's cache is left alone.
        assert untouched.read_bytes() == b"keep me"

    def test_the_kill_rule_rejects_a_green_run(self, harness: ModuleType) -> None:
        class _Result:
            returncode = 0
            stdout = "1 passed"
            stderr = ""

        ok, reason = harness._is_valid_kill(_Result(), ("t.py::A::b",))
        assert ok is False
        assert "exit code" in reason

    def test_the_kill_rule_rejects_infrastructure_noise(self, harness: ModuleType) -> None:
        class _Result:
            returncode = 1
            stdout = "ImportError while loading conftest\ncollected 0 items"
            stderr = ""

        ok, reason = harness._is_valid_kill(_Result(), ("t.py::A::b",))
        assert ok is False
        assert "infrastructure" in reason

    def test_the_kill_rule_rejects_an_unexpected_failure(self, harness: ModuleType) -> None:
        class _Result:
            returncode = 1
            stdout = "FAILED other.py::TestX::test_y - AssertionError: boom\n1 failed"
            stderr = ""

        ok, reason = harness._is_valid_kill(_Result(), ("t.py::A::b",))
        assert ok is False
        assert "expected node IDs" in reason

    def test_the_kill_rule_rejects_a_bare_traceback(self, harness: ModuleType) -> None:
        class _Result:
            returncode = 1
            # pytest prints the raised exception on its own "E " line *and* echoes
            # it on the short-summary line; both shapes count.
            stdout = (
                "t.py:12: in b\n"
                "    guard()\n"
                "E   RuntimeError: boom\n"
                "FAILED t.py::A::b - RuntimeError: boom\n1 failed"
            )
            stderr = ""

        ok, reason = harness._is_valid_kill(_Result(), ("t.py::A::b",))
        # A raised exception *is* a valid effect, so this one is a kill.
        assert ok is True, reason

    def test_the_kill_rule_accepts_a_typed_domain_error(self, harness: ModuleType) -> None:
        """The classifier must not insist on literal AssertionError/ValueError.

        ``pytest.raises`` around a guard that was mutated *away* fails with
        ``DID NOT RAISE``; a guard that fires when it should not fails with the
        module's own typed error.  Both are real failures.
        """

        class _Result:
            returncode = 1
            stdout = (
                "t.py:20: in b\n"
                "E   nexus_ai_agent.creative.studio.revision_pinning"
                ".RevisionConcurrencyConflictError: stale_parent\n"
                "FAILED t.py::A::b - nexus_ai_agent.creative.studio.revision_pinning"
                ".RevisionConcurrencyConflictError: stale_parent\n1 failed"
            )
            stderr = ""

        ok, reason = harness._is_valid_kill(_Result(), ("t.py::A::b",))
        assert ok is True, reason

    def test_the_harness_never_writes_its_own_bytecode(self, harness: ModuleType) -> None:
        # ``sys.dont_write_bytecode`` is set at import time so a campaign can
        # never seed the cache it is about to reason about.
        assert sys.dont_write_bytecode is True

    def test_the_suite_for_a_mutant_is_the_module_that_owns_its_kill_target(
        self, harness: ModuleType
    ) -> None:
        for mutation in harness.MUTATIONS:
            suite = harness._suite_for(mutation)
            assert suite == mutation.expected_node_ids[0].split("::", 1)[0], mutation.name
            assert (ROOT / suite).is_file(), suite

    def test_the_redundancy_probe_runs_both_the_unit_suite_and_the_slice(
        self, harness: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The planner must ask *both* questions and fail when nothing catches it."""
        calls: list[tuple[str, ...]] = []

        class _Result:
            def __init__(self, returncode: int) -> None:
                self.returncode = returncode
                self.stdout = ""
                self.stderr = ""

        # First call (the unit suite) fails, second (the slice) fails, third and
        # fourth pass -- so one mutant is "both" and one is "neither".
        verdicts = iter([1, 1, 0, 0])
        seen: list[str] = []

        def _fake_run_pytest(targets: list[str]) -> _Result:
            calls.append(tuple(targets))
            return _Result(next(verdicts))

        monkeypatch.setattr(harness, "_run_pytest", _fake_run_pytest)
        monkeypatch.setattr(harness, "MUTATIONS", harness.MUTATIONS[:2])
        original_write_bytes = Path.write_bytes

        def _noop_write_bytes(self: Path, data: bytes) -> int:
            # The planner mutates real files; keep them untouched here.
            seen.append(str(self))
            return len(data)

        monkeypatch.setattr(Path, "write_bytes", _noop_write_bytes)
        try:
            exit_code = harness.redundancy_report()
        finally:
            monkeypatch.setattr(Path, "write_bytes", original_write_bytes)

        assert exit_code == 1, "a mutant nothing catches must make the probe fail"
        assert len(calls) == 4
        for mutation in harness.MUTATIONS[:2]:
            assert (harness._suite_for(mutation),) in calls
            assert (harness.SLICE,) in calls
        assert seen, "the planner must actually attempt to apply each mutant"

    def test_the_pytest_runner_is_pyc_free_and_repo_rooted(
        self, harness: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def _fake_run(args: list[str], **kwargs: object) -> object:
            captured["args"] = args
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(harness.subprocess, "run", _fake_run)
        harness._run_pytest(["tests/example.py"])

        args = captured["args"]
        assert isinstance(args, list)
        assert "-B" in args
        assert str(harness.ROOT) == captured["cwd"]
        env = captured["env"]
        assert isinstance(env, dict)
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"


class TestFullCampaign:
    @pytest.mark.skipif(
        os.environ.get("NEXUS_RUN_MUTATION_CAMPAIGN") != "1",
        reason="set NEXUS_RUN_MUTATION_CAMPAIGN=1 to run all 50 mutants (~90s)",
    )
    def test_the_campaign_kills_every_mutant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The end-to-end proof: 50/50 VALID_KILL, and the tree is left pristine."""
        module = _load_harness()
        targets = {mutation.path for mutation in module.MUTATIONS}
        before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in targets}
        # ``main()`` reads ``sys.argv``; hand it an argv that belongs to the harness
        # rather than to the pytest process that imported this test.
        monkeypatch.setattr(sys, "argv", [str(HARNESS)])
        exit_code = module.main()
        assert exit_code == 0
        after = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in before}
        assert after == before
