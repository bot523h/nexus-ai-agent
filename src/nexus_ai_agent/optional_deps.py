"""Typed, fail-closed guards for optional capability extras (task-107).

The core install is deliberately light: no torch, no chroma, no llama.cpp, no
boto3. Everything heavy lives behind an extra (``pip install
'nexus-ai-agent[rag]'``), so the import of such a package must never happen at
module scope — otherwise a core-only install crashes on startup.

This module is the single place where "is this capability installed?" is
answered, with one typed error type:

    from nexus_ai_agent.optional_deps import require

    chromadb = require("chromadb", extra="rag")

The error message always names the package *and* the exact install command, so
the failure is actionable from a Telegram error card or a job-queue record
instead of a bare ``ModuleNotFoundError`` traceback.

Importing this module must stay dependency-free: it is reachable from the bot
startup path, the CLI and the worker.
"""

from __future__ import annotations

import importlib
import importlib.util
from types import ModuleType

# Top-level import name -> the extra that provides it. Used to build the install
# hint when the caller did not pass ``extra`` explicitly.
MODULE_TO_EXTRA: dict[str, str] = {
    "chromadb": "rag",
    "flashrank": "rag",
    "sqlite_vec": "rag",
    "sentence_transformers": "rag",
    "llama_cpp": "local-llm",
    "gtts": "speech",
    "boto3": "r2",
    "botocore": "r2",
    "pypdf": "pdf",
    "psycopg": "postgres",
    "asyncpg": "postgres",
    # dotted: langgraph itself is core, only its Postgres saver is optional
    "langgraph.checkpoint.postgres": "postgres",
    "opentimelineio": "otio",
    "imageio_ffmpeg": "media",
}

# Extras that require a non-Python toolchain (compiler / model download). Shown
# in the hint so a Termux or slim-container user knows what is coming.
HEAVY_EXTRAS: frozenset[str] = frozenset({"rag", "local-llm"})


class OptionalDependencyMissing(RuntimeError):
    """A capability extra is not installed.

    Carries both names (``module`` for code, ``extra`` for the user-facing
    install command) so callers can log, persist or render it without
    re-parsing the message.
    """

    def __init__(self, module: str, extra: str | None = None) -> None:
        self.module = module
        self.extra = extra or MODULE_TO_EXTRA.get(module, module)
        super().__init__(self.build_message())

    def build_message(self) -> str:
        spec = f"nexus-ai-agent[{self.extra}]" if self.extra != self.module else self.module
        hint = (
            " This extra downloads large model weights / needs a C++ toolchain,"
            " which is why it is not part of the core install."
            if self.extra in HEAVY_EXTRAS
            else ""
        )
        return (
            f"The '{self.module}' package is required for this feature but is not "
            f"installed. Install it with: pip install '{spec}'{hint}"
        )


def is_installed(module: str) -> bool:
    """True when *module* can be imported without executing it.

    Uses :func:`importlib.util.find_spec`, so a heavy module is never imported
    just to answer "is it there?" (``sentence_transformers`` alone costs a
    multi-second torch import).
    """
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        # find_spec raises ModuleNotFoundError for a missing *parent* package
        # and ValueError for malformed namespace entries; both mean "absent".
        return False


def require(module: str, extra: str | None = None) -> ModuleType:
    """Import and return *module*, or raise :class:`OptionalDependencyMissing`.

    This is the fail-closed entry point: the returned module is guaranteed to
    be usable, so callers never need a ``try/except ImportError`` of their own.
    """
    if not is_installed(module):
        raise OptionalDependencyMissing(module, extra)
    return importlib.import_module(module)


def from_import_error(
    exc: ModuleNotFoundError, extra: str | None = None
) -> OptionalDependencyMissing:
    """Translate a failed optional import into the typed, actionable error.

    Use around a ``from ... import ...`` that pulls an optional dependency::

        try:
            from nexus_ai_agent.features.rag import AdvancedRAGEngine
        except ModuleNotFoundError as exc:
            raise from_import_error(exc) from exc

    Mapping from ``exc.name`` keeps the check *behavioural* rather than a
    static guess: a test (or an alternative implementation) that provides the
    module another way still works, while a genuinely absent extra produces the
    exact install command.
    """
    name = exc.name or "unknown"
    resolved = extra or MODULE_TO_EXTRA.get(name) or MODULE_TO_EXTRA.get(name.split(".")[0])
    return OptionalDependencyMissing(name, resolved)
