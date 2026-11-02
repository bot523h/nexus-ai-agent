"""task-nagar-workspace-redelivery-01 — the redelivery harness must not be an injection surface.

External review (Sourcery / ``dangerous-subprocess-use-audit``) flagged
``scripts/creative_redelivery_mutations.py`` for calling ``subprocess.run``.  Analysis, not
suppression, is the resolution — the same disposition ``task-283`` reached for
``scripts/queue_ownership_mutations.py``:

* the call passes an **argv list**, never a string, with ``shell=False`` stated explicitly;
* the program is ``sys.executable`` and the leading flags are fixed literals;
* the only variable arguments are pytest *test node ids*;
* those ids are frozen constants in :data:`PROBES` **and** are re-validated against a strict
  pattern before every spawn (``_run`` validates, so no future caller can bypass it);
* the harness accepts no command-line arguments that could carry a selection.

These tests pin both halves of the trust boundary: the data path is inert, and anything that is
not a plain ``tests/<path>.py[::name]`` node id aborts the campaign as a harness failure
(exit 2) instead of being executed.  Testing the *rejection* behaviour is the point: it is what
keeps a future, possibly generated, probe from widening the surface.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _harness() -> Any:
    path = REPO_ROOT / "scripts" / "creative_redelivery_mutations.py"
    spec = importlib.util.spec_from_file_location("creative_redelivery_mutations", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_probe_test_id_is_a_plain_node_id() -> None:
    """The frozen probe table is inert data — no flags, absolute paths or metacharacters."""
    harness = _harness()
    assert harness.PROBES, "the harness must define probes"
    for probe in harness.PROBES:
        assert probe.tests, f"{probe.name} selects no tests"
        harness._validate_test_selection(probe.tests)
        for node in probe.tests:
            assert (REPO_ROOT / node.split("::")[0]).is_file(), f"{node} is not a test file"


@pytest.mark.parametrize(
    "node",
    [
        "-k something",  # would become a pytest option
        "--pdb",
        "tests/x.py; rm -rf /",  # shell metacharacters
        "tests/../../etc/passwd",
        "/etc/passwd",
        "tests/../tests/integration/test_x.py",
        "",
        "tests/integration/test_x.txt",  # not a python test path
        "  tests/integration/test_x.py",  # surrounding whitespace
        "tests/integration/test_x.py\nrm -rf /",
        "$(id)",
        "tests/integration/test_x.py|cat",
    ],
)
def test_validate_test_selection_refuses_anything_but_a_plain_node_id(node: str) -> None:
    harness = _harness()
    with pytest.raises(SystemExit) as excinfo:
        harness._validate_test_selection((node,))
    assert excinfo.value.code == 2, (
        "an invalid selection must abort the campaign, not kill a mutant"
    )


def test_a_valid_selection_is_accepted() -> None:
    """Positive control: the guard must not block the ids the probes actually use."""
    harness = _harness()
    harness._validate_test_selection(
        (
            "tests/unit/test_creative_surface.py",
            "tests/integration/test_creative_surface_redelivery.py::test_same_key_same_bytes_"
            "is_one_job_and_one_immutable_input",
        )
    )


def test_the_spawn_uses_an_argv_list_with_shell_explicitly_disabled() -> None:
    """Pin the call shape itself: a future rewrite to a string command would fail here."""
    harness = _harness()
    source = inspect.getsource(harness._run)
    assert "shell=False" in source, "shell=False must be explicit, not implied"
    assert "[sys.executable" in source, "argv[0] must be the fixed interpreter"
    assert "shell=True" not in source


def test_the_harness_takes_no_argument_that_can_select_tests() -> None:
    """The only CLI flag is ``--list``; nothing external can substitute a test selection."""
    harness = _harness()
    source = inspect.getsource(harness.main)
    assert '"--list"' in source
    assert "add_argument" in source
    # Exactly one parser option, and it is the listing flag.
    assert source.count("add_argument") == 1


# ── a kill must be an assertion failure, not a broken mutant ──


def test_an_unparsable_mutant_is_not_a_kill() -> None:
    """The campaign must refuse a SyntaxError-shaped "kill".

    M5 was reported as a kill while the mutant was not valid Python at all: the
    probe replaced an opening docstring quote, leaving an unterminated literal, so
    pytest died in collection (exit 2, ``errors``) rather than failing an
    assertion. That is evidence the probe is wrong, not that the invariant is
    defended, and it must never be counted.
    """
    import tempfile
    from pathlib import Path

    harness = _harness()
    with tempfile.TemporaryDirectory() as tmp:
        broken = Path(tmp) / "mutant.py"
        broken.write_text(
            'def f():\n    """opening quote never closed\n    return 1\n', encoding="utf-8"
        )
        assert harness._parses((broken,)) is False

        healthy = Path(tmp) / "healthy.py"
        healthy.write_text("def f() -> int:\n    return 1\n", encoding="utf-8")
        assert harness._parses((healthy,)) is True


def test_the_m5_probe_removes_the_guard_call_and_stays_parsable() -> None:
    """The surface probe must disable the *call*, not shred the docstring.

    A probe that edits prose rather than behaviour measures the parser, not the
    security invariant.
    """
    import ast

    harness = _harness()
    probe = next(p for p in harness.PROBES if p.name.startswith("M5-surface-workspace-symlink"))
    source = (harness.ROOT / probe.edits[0].path).read_text(encoding="utf-8")
    assert probe.edits[0].old in source, "anchor must exist"
    mutated = source.replace(probe.edits[0].old, probe.edits[0].new, 1)
    ast.parse(mutated)  # must not raise
    assert "refuse_symlinked_node(path, label)" in mutated
