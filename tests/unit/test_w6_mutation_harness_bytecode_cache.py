"""W6.7: the mutation harness must be bytecode-cache deterministic.

Root cause (P2 of the post-merge audit, 2026-11-24): CPython validates a
``.pyc`` against the source's **size** and its **mtime in whole seconds**.
Every W6 mutant is an equal-byte-length text swap, and the
mutate -> pytest -> restore cycle finishes inside a single second, so cached
bytecode could be considered valid for the wrong bytes.  On the pre-fix
harness that produced three observed symptoms, all of them timing flakes:

* a mutant "SURVIVED" because it was never imported (``pytest exit code 0``);
* the restored tree still looked red (``FATAL: suite still red after restore``);
* a mutated ``.pyc`` kept shadowing pristine source for later test runs.

The fix is defence in depth inside ``scripts/w6_mutations.py``: the harness
never writes bytecode itself, it runs its pytest subprocesses with
``PYTHONDONTWRITEBYTECODE=1``, and it purges the cached artefacts of a target
file before the pre-flight run, after writing the mutant, and after restoring
the original bytes.

The unique basename is deliberate: ``tests/`` carries no ``__init__.py``, so
pytest's prepend import mode requires every test module basename to be unique —
a second ``test_w6_science_mutations.py`` under ``tests/unit/`` collides with
the architecture gate of the same name ("import file mismatch", collection exit
code 2).

These tests pin the two guarantees:

* ``_purge_bytecode_cache`` breaks a same-second, equal-length staleness (the
  hazard is reproduced first, then removed by the helper), and clears every
  ``<stem>.*.pyc`` while leaving unrelated modules' caches alone;
* two back-to-back campaigns both report ``20/20 mutants killed`` with no
  survivors and no skips, and leave every target file byte-identical.
"""

from __future__ import annotations

import importlib.util
import os
import py_compile
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "scripts" / "w6_mutations.py"

TARGET_FILES = (
    "src/nexus_ai_agent/creative/science/photochemical_emulsion.py",
    "src/nexus_ai_agent/creative/science/physical_optics_radiometry.py",
    "src/nexus_ai_agent/creative/science/optimal_transport_spd.py",
    "src/nexus_ai_agent/creative/science/symplectic_hamiltonian_pipeline.py",
    "src/nexus_ai_agent/creative/character/model.py",
    "src/nexus_ai_agent/creative/character/continuity.py",
)

#: Loaded by path (never ``import scripts``, which is unpackaged and forbidden
#: by tests/architecture/test_scripts_import_boundary.py) in a throwaway
#: interpreter, so the harness's module-level ``sys.dont_write_bytecode`` never
#: leaks into the pytest process.
_PURGE_SNIPPET = """
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("w6_mutations_harness", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(module)
module._purge_bytecode_cache(Path(sys.argv[2]))
"""

_IMPORT_VALUE_SNIPPET = """
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("mutable_module_under_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
print(module.value())
"""


def _digests() -> dict[str, str]:
    import hashlib

    return {rel: hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() for rel in TARGET_FILES}


def _harness_env() -> dict[str, str]:
    env = dict(os.environ)
    src = str(ROOT / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not existing else os.pathsep.join([src, existing])
    return env


def _purge(source: Path, cwd: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PURGE_SNIPPET, str(HARNESS), str(source)],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"purge helper failed (exit {result.returncode})\n{result.stderr}"
    )


def _imported_value(source: Path, cwd: Path) -> str:
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_VALUE_SNIPPET, str(source)],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"import probe failed (exit {result.returncode})\n{result.stderr}"
    )
    return result.stdout.strip()


def test_purge_bytecode_cache_breaks_same_second_equal_length_staleness(
    tmp_path: Path,
) -> None:
    """Reproduce the P2 hazard, then prove the helper removes it."""
    original = "def value():\n    return 1\n"
    mutated = "def value():\n    return 2\n"
    assert len(original) == len(mutated), "the reproduction requires equal byte length"

    source = tmp_path / "mutable_module.py"
    source.write_text(original, encoding="utf-8")
    py_compile.compile(str(source), doraise=True)
    cache = Path(importlib.util.cache_from_source(str(source)))
    assert cache.exists(), "py_compile must leave a .pyc for the reproduction to be meaningful"
    assert _imported_value(source, tmp_path) == "1"

    # The harness's exact worst case: equal-length bytes written while the
    # whole-second mtime recorded in the .pyc still matches.
    same_second = int(source.stat().st_mtime)
    source.write_text(mutated, encoding="utf-8")
    os.utime(source, (same_second, same_second))
    assert source.stat().st_size == len(original.encode("utf-8"))
    assert int(source.stat().st_mtime) == same_second

    # Hazard: the stale .pyc is accepted for the mutated source.
    assert _imported_value(source, tmp_path) == "1"

    # Fix: purging the cache forces a recompile from the bytes on disk.
    _purge(source, tmp_path)
    assert not cache.exists()
    assert _imported_value(source, tmp_path) == "2"


def test_purge_bytecode_cache_clears_every_stem_cache_only(tmp_path: Path) -> None:
    source = tmp_path / "mutable_module.py"
    source.write_text("def value():\n    return 1\n", encoding="utf-8")
    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    other_interpreter = pycache / "mutable_module.cpython-310.pyc"
    unrelated = pycache / "other_module.cpython-311.pyc"
    other_interpreter.write_bytes(b"stale")
    unrelated.write_bytes(b"keep")

    _purge(source, tmp_path)

    assert not other_interpreter.exists(), "a sibling-interpreter cache of the same stem must go"
    assert unrelated.exists(), "unrelated modules must keep their cache"


def test_back_to_back_campaigns_are_cache_clean_and_kill_all_mutants() -> None:
    """Two consecutive full campaigns: 20/20 both times, tree byte-identical."""
    before = _digests()
    for round_no in (1, 2):
        result = subprocess.run(
            [sys.executable, str(HARNESS)],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=900,
            env=_harness_env(),
        )
        assert result.returncode == 0, (
            f"campaign round {round_no} failed (exit {result.returncode})\n"
            f"{result.stdout}\n{result.stderr}"
        )
        assert "20/20 mutants killed (VALID_KILL)" in result.stdout, result.stdout
        assert "survivors" not in result.stdout, result.stdout
        assert "SKIP" not in result.stdout, result.stdout
        assert _digests() == before, f"round {round_no} did not restore every target file"
