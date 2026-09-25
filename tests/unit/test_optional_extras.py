"""Per-leg optional-extras smoke tests — the executable half of task-132.

``scripts/extras_matrix.py`` proves the *declarations* agree (pyproject ↔ CI
matrix ↔ this file).  This file proves the *behaviour* in the environment the
CI matrix job installs for each leg:

``core``
    ``pip install -e .`` with no extras: every optional module is absent, the
    CLI import tree runs, the whole package imports without ``ImportError``,
    and each optional path **fails closed** with its typed, actionable error
    instead of an unguarded ``ImportError``.
``pdf`` / ``speech`` / ``translate``
    ``pip install -e '.[<extra>]'``: the extra's wheels really resolve and
    their top-level modules import, the availability adapters report
    available, and the focused behavioural contract holds.

Leg selection is environment-driven (``NEXUS_EXTRA_LEG``, set by the
``extras-matrix`` CI job) so the *same* file runs in every leg without edits.
Without a leg the file still runs whatever is provable in the current
environment and **visibly skips** the rest (``-rs`` shows each reason) — a
skip is never allowed to hide a broken install path silently.

Classification of the failures this file can produce (the taxonomy the core
contract uses):

``MODULE_IMPORT``
    a package module hard-imports an optional dependency at import time;
``OPTIONAL_GUARD``
    an optional path raised raw ``ImportError`` instead of the typed error;
``PACKAGING``
    the extra's declared requirements did not install what the code imports;
``RUNTIME``
    behaviour wrong after a successful import;
``TEST``
    the leg environment itself is inconsistent (module of another leg leaked
    in, or the active leg's module is missing).
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
LEG_SCRIPT = REPO_ROOT / "scripts" / "extras_matrix.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures"

#: The active CI leg (``core`` or an extra name); unset in the default jobs.
LEG = os.environ.get("NEXUS_EXTRA_LEG") or None


def _load_leg_definitions() -> dict[str, tuple[str, ...]]:
    """Import the leg registry from the script by path (scripts/ is unpackaged)."""
    spec = importlib.util.spec_from_file_location("extras_matrix_legs", LEG_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # register before exec: @dataclass resolves annotations via sys.modules
    sys.modules["extras_matrix_legs"] = module
    spec.loader.exec_module(module)
    return {
        name: tuple(definition.import_modules)
        for name, definition in module.LEG_DEFINITIONS.items()
    }


LEG_MODULES = _load_leg_definitions()
PDF_HINT = "nexus-ai-agent[pdf]"
SPEECH_HINT = "nexus-ai-agent[speech]"
TRANSLATE_HINT = "nexus-ai-agent[translate]"


def _module_available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ImportError:
        return False


def _skip_without_leg(extra: str) -> None:
    """Visible, intentional skip when this environment is not the extra's leg."""
    pytest.skip(
        f"[{extra}] smoke is proven by the `extras-matrix` CI leg "
        f"(NEXUS_EXTRA_LEG={extra}); not installed in this environment"
    )


# --------------------------------------------------------------------------- #
# 1. install + import smoke (PACKAGING / TEST)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("extra,modules", sorted(LEG_MODULES.items()))
def test_extra_modules_importability_matches_the_active_leg(
    extra: str, modules: tuple[str, ...]
) -> None:
    present = {module: _module_available(module) for module in modules}
    if LEG == extra:
        # PACKAGING: the extra installed, but its module does not import.
        missing = [module for module, ok in present.items() if not ok]
        assert not missing, (
            f"leg {extra!r}: `pip install -e '.[{extra}]'` completed but these modules "
            f"do not import: {missing}"
        )
    elif LEG == "core":
        # TEST: the core-only install leaked an optional package in.
        leaked = [module for module, ok in present.items() if ok]
        assert not leaked, (
            f"core-only install must not provide optional module(s) {leaked} — "
            "the core contract is only provable when extras are genuinely absent"
        )
    elif LEG is None and any(present.values()):
        # default job (dev extra): whatever is present must genuinely import.
        for module, ok in present.items():
            if ok:
                importlib.import_module(module)
    else:
        # Another extra's leg: optional modules of *other* extras must not leak
        # into this one (each leg installs exactly one extra).
        leaked = [module for module, ok in present.items() if ok]
        assert not leaked, f"leg {LEG!r} must only install the {LEG!r} extra, but found {leaked}"


@pytest.mark.parametrize("extra,modules", sorted(LEG_MODULES.items()))
def test_extra_import_smoke(extra: str, modules: tuple[str, ...]) -> None:
    """Import every module the extra's wheels provide (real import, not find_spec)."""
    if LEG == extra or (LEG is None and all(_module_available(m) for m in modules)):
        for module in modules:
            imported = importlib.import_module(module)
            assert imported is not None
    else:
        _skip_without_leg(extra)


# --------------------------------------------------------------------------- #
# 2. core-only contract (MODULE_IMPORT / OPTIONAL_GUARD / RUNTIME)
# --------------------------------------------------------------------------- #
def _require_core_leg(reason: str) -> None:
    if LEG != "core":
        pytest.skip(f"core-only contract (proven by the `core` CI leg): {reason}")


def test_core_leg_cli_import_tree_runs() -> None:
    """`nexus --help` and `nexus run-bot --help` must run without optional extras."""
    _require_core_leg("CLI help must work on a bare install")
    for arguments in (["--help"], ["run-bot", "--help"], ["packs", "--help"]):
        completed = subprocess.run(
            [sys.executable, "-m", "nexus_ai_agent.cli", *arguments],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert completed.returncode == 0, (
            f"`nexus {' '.join(arguments)}` exited {completed.returncode} on the "
            f"core-only install:\n{completed.stderr[-2000:]}"
        )


def test_core_leg_package_imports_without_optional_dependencies() -> None:
    """Walk the whole package: no module may hard-import an optional package."""
    _require_core_leg("full import walk")
    import pkgutil

    import nexus_ai_agent

    failures: list[str] = []
    modules: list[str] = []

    def _walk_error(name: str) -> None:
        failures.append(f"MODULE_IMPORT {name}: ImportError during package walk")

    # walk_packages imports every *package* as it descends (onerror records a
    # broken package); the loop below then imports every plain module too.
    for info in pkgutil.walk_packages(
        nexus_ai_agent.__path__, prefix="nexus_ai_agent.", onerror=_walk_error
    ):
        modules.append(info.name)
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as error:  # noqa: BLE001 - the classification IS the test
            failures.append(f"MODULE_IMPORT {name}: {type(error).__name__}: {error}")
    assert failures == [], (
        f"core-only install: {len(failures)} module(s) failed to import "
        f"({len(modules)} walked):\n" + "\n".join(sorted(failures)[:25])
    )


def test_core_leg_pdf_extraction_fails_closed_with_install_hint() -> None:
    _require_core_leg("pdf fail-closed error")
    from nexus_ai_agent.worker import extract_pdf_text

    source = FIXTURES / "minimal.pdf"
    assert source.is_file(), "tests/fixtures/minimal.pdf is missing"
    with pytest.raises(RuntimeError, match=r"nexus-ai-agent\[pdf\]"):
        asyncio.run(extract_pdf_text(str(source)))


def test_core_leg_speech_profile_fails_closed() -> None:
    _require_core_leg("speech fail-closed error")
    from nexus_ai_agent.adapters.whisper_local import WhisperLocalCaptionEngine
    from nexus_ai_agent.application.ports.caption_engine import (
        CaptionProfileUnavailableError,
    )

    engine = WhisperLocalCaptionEngine()
    assert engine.is_available() is False, (
        "faster_whisper must be unavailable on the core leg (if this fails, the "
        "core environment is contaminated — TEST classification)"
    )
    # minimal.pdf is a real file (so the missing-file guard does not fire first)
    # but not audio: the load must fail closed on the missing extra instead.
    with pytest.raises(CaptionProfileUnavailableError) as exc_info:
        asyncio.run(engine.transcribe(str(FIXTURES / "minimal.pdf")))
    assert "caption_profile_unavailable" in str(exc_info.value)
    assert SPEECH_HINT in str(exc_info.value)


def test_core_leg_translate_profile_fails_closed() -> None:
    _require_core_leg("translate fail-closed error")
    from nexus_ai_agent.adapters.whisper_local import (
        ArgosLocalTranslator,
        TranslateProfileUnavailableError,
    )

    translator = ArgosLocalTranslator()
    assert translator.is_available() is False
    from nexus_ai_agent.creative.packs.caption.models import TranscriptRef, TranscriptSegment

    transcript = TranscriptRef(
        transcript_id="t-core",
        source_asset_id="a-core",
        language="en",
        segments=(TranscriptSegment(start_us=0, end_us=1_000_000, text="hello"),),
        duration_us=1_000_000,
    )
    with pytest.raises(TranslateProfileUnavailableError) as exc_info:
        translator.translate_transcript_sync(transcript, "fa")
    assert "translate_profile_unavailable" in str(exc_info.value)
    assert TRANSLATE_HINT in str(exc_info.value)


# --------------------------------------------------------------------------- #
# 3. per-extra behavioural contracts (RUNTIME)
# --------------------------------------------------------------------------- #
def test_pdf_leg_extracts_real_text_layer() -> None:
    if LEG == "pdf" or (LEG is None and _module_available("pypdf")):
        from nexus_ai_agent.worker import extract_pdf_text

        text = asyncio.run(extract_pdf_text(str(FIXTURES / "minimal.pdf")))
        assert isinstance(text, str) and text, "pypdf is installed but extraction returned nothing"
    else:
        _skip_without_leg("pdf")


def test_speech_leg_reports_available() -> None:
    if LEG == "speech":
        from nexus_ai_agent.adapters.whisper_local import WhisperLocalCaptionEngine

        engine = WhisperLocalCaptionEngine()
        assert engine.is_available() is True, (
            "faster_whisper is installed on this leg but the adapter reports "
            "unavailable — the guard is lying about the environment"
        )
        importlib.import_module("faster_whisper")  # real import, not just find_spec
    elif LEG is None and _module_available("faster_whisper"):
        from nexus_ai_agent.adapters.whisper_local import WhisperLocalCaptionEngine

        assert WhisperLocalCaptionEngine().is_available() is True
    else:
        _skip_without_leg("speech")


def test_translate_leg_reports_available() -> None:
    if LEG == "translate":
        from nexus_ai_agent.adapters.whisper_local import ArgosLocalTranslator

        translator = ArgosLocalTranslator()
        assert translator.is_available() is True, (
            "argostranslate is installed on this leg but the adapter reports "
            "unavailable — the guard is lying about the environment"
        )
        importlib.import_module("argostranslate.translate")
    elif LEG is None and _module_available("argostranslate"):
        from nexus_ai_agent.adapters.whisper_local import ArgosLocalTranslator

        assert ArgosLocalTranslator().is_available() is True
    else:
        _skip_without_leg("translate")


# --------------------------------------------------------------------------- #
# 4. leg consistency (TEST)
# --------------------------------------------------------------------------- #
def test_active_leg_is_a_known_leg() -> None:
    if LEG is None:
        pytest.skip("no active leg (default jobs) — leg validation is inert here")
    assert LEG == "core" or LEG in LEG_MODULES, (
        f"NEXUS_EXTRA_LEG={LEG!r} is neither 'core' nor a declared extra ({sorted(LEG_MODULES)})"
    )


def test_leg_environment_matches_declared_extras() -> None:
    """The leg registry in the script must mirror pyproject (double-entry bookkeeping)."""
    import tomllib

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    shipping = {name for name in data["project"]["optional-dependencies"] if name != "dev"}
    assert set(LEG_MODULES) == shipping, (
        "scripts/extras_matrix.py LEG_DEFINITIONS drifted from pyproject "
        f"optional-dependencies: script={sorted(LEG_MODULES)} pyproject={sorted(shipping)}"
    )
