"""Static 3.10 compatibility guard for the OMEGA audio zone.

Why a test and not a CI job: the 3.10 leg exists in CI and is blocking, but CI
can only tell you a construct failed *after* it is committed. A grep-level guard
tells you at the point of writing, and — more importantly — it records *why* the
3.10-compatible spelling is deliberate in the places that already use it.

This is not a substitute for running 3.10. It is the half of the audit that can
be done without an interpreter.

Registered as evidence for ``creative.packs.audio``: the audio pack's handlers
import the whole `audio_engine`, so a 3.11-only construct anywhere in this zone
takes the pack down on the oldest supported interpreter, and CI's 3.10 leg only
discovers that after the commit.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ZONE = REPO / "src" / "nexus_ai_agent" / "creative"
SCRIPTS = (REPO / "scripts" / "mutate_omega_audio.py",)

#: (pattern, why it breaks on 3.10, what to write instead)
FORBIDDEN = (
    (
        "from enum import StrEnum",
        "StrEnum is 3.11+",
        "class X(str, Enum)",
    ),
    (
        "datetime.UTC",
        "datetime.UTC is 3.11+",
        "datetime.timezone.utc",
    ),
    (
        "ExceptionGroup",
        "ExceptionGroup / except* are 3.11+",
        "collect errors into a list and raise once",
    ),
    (
        "tomllib",
        "tomllib is 3.11+",
        "tomli, behind a try/except ImportError fallback",
    ),
)


def _zone_files() -> list[Path]:
    files = sorted(path for path in ZONE.rglob("*.py") if "__pycache__" not in path.parts)
    files.extend(path for path in SCRIPTS if path.is_file())
    assert files, "the zone scan found no files; the guard would pass vacuously"
    return files


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Lines that are code, with comments and docstrings removed.

    A docstring that *names* ``datetime.UTC`` to explain why it is avoided is
    not a usage, and a guard that cannot tell the difference would force the
    explanation out of the file.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                start = body[0].lineno
                end = body[0].end_lineno or start
                docstring_lines.update(range(start, end + 1))
    lines = source.splitlines()
    return [
        (number, text)
        for number, text in enumerate(lines, start=1)
        if number not in docstring_lines and not text.lstrip().startswith("#")
    ]


@pytest.mark.parametrize(("pattern", "reason", "instead"), FORBIDDEN)
def test_no_311_only_construct_appears_in_the_zone(pattern: str, reason: str, instead: str) -> None:
    hits: list[str] = []
    for path in _zone_files():
        for number, text in _code_lines(path):
            if pattern in text:
                hits.append(f"{path.relative_to(REPO)}:{number}: {text.strip()}")
    assert not hits, f"{reason} — use {instead} instead:\n" + "\n".join(hits)


def test_typing_self_is_not_used_without_a_typing_extensions_fallback() -> None:
    """``typing.Self`` is 3.11+. ``typing_extensions`` is a 3.10-compatible host."""
    hits: list[str] = []
    for path in _zone_files():
        source = path.read_text(encoding="utf-8")
        for number, text in _code_lines(path):
            if "Self" not in text:
                continue
            # A `Self` annotation is fine only when the module imports it from
            # typing_extensions (or defines its own alias).
            if "typing_extensions" in source or "Self = " in source:
                continue
            if "Self]" in text or "Self," in text or ": Self" in text or "-> Self" in text:
                hits.append(f"{path.relative_to(REPO)}:{number}: {text.strip()}")
    assert not hits, "typing.Self without typing_extensions is 3.11+ only:\n" + "\n".join(hits)


def test_every_zone_module_compiles_under_the_running_interpreter() -> None:
    """A syntax guard: 3.10-only-too-old syntax would fail here on any version."""
    for path in _zone_files():
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as error:  # pragma: no cover - would be a real defect
            pytest.fail(f"{path.relative_to(REPO)} does not compile: {error}")


def test_the_utc_helper_uses_the_3_10_spelling() -> None:
    """The one place this mattered, asserted directly.

    ``_utc_now`` explains the choice in its docstring; this pins the code so the
    explanation cannot go stale.
    """
    from nexus_ai_agent.creative.audio_engine.verify import _utc_now

    stamp = _utc_now()
    assert stamp.endswith("Z")
    assert "+00:00" not in stamp
    assert "." not in stamp, "microsecond precision must be stripped for stable hashing"
    source = (ZONE / "audio_engine" / "verify.py").read_text(encoding="utf-8")
    assert "datetime.datetime.now(datetime.timezone.utc)" in source
