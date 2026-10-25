"""Configuration for the knowledge-retrieval subsystem.

Settings come from the existing :mod:`nexus_ai_agent.config.settings`
convention (env + ``.env``, ``NEXUS_*`` names); this module validates them
into an immutable :class:`KnowledgeConfig` and fails closed with
:class:`~nexus_ai_agent.knowledge_retrieval.errors.KnowledgeConfigError`
before any file is touched.

Authorization model: **the configured roots are the allowlist.**  A source is
readable only when it is discovered by walking an existing configured root —
there is no "index this arbitrary path" operation anywhere in the subsystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nexus_ai_agent.config.settings import Settings, get_settings
from nexus_ai_agent.knowledge_retrieval.errors import KnowledgeConfigError

__all__ = [
    "DEFAULT_ALLOWED_EXTENSIONS",
    "KnowledgeConfig",
    "optional_pdf_enabled",
    "provider_availability",
]


def provider_availability(config: KnowledgeConfig) -> tuple[bool, str | None]:
    """Cheap, non-raising probe of the configured embedding provider.

    Used by ``status``: ``hash`` is always available (pure arithmetic);
    ``sentence-transformers`` needs an existing local model directory and an
    importable package — never a download.
    """
    if config.embedding_provider == "hash":
        return True, None
    model_dir = (config.embedding_model_path or "").strip()
    if not model_dir:
        return False, "embedding_model_path_empty"
    if not Path(model_dir).expanduser().is_dir():
        return False, "embedding_model_directory_missing"
    import importlib.util

    if importlib.util.find_spec("sentence_transformers") is None:
        return False, "sentence_transformers_not_installed"
    return True, None


#: Text formats parsed unconditionally (UTF-8, deterministic).
DEFAULT_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".md", ".markdown", ".txt", ".rst"})

#: PDF is supported only when the existing ``pdf`` extra (``pypdf``) is
#: installed — the default CI suite installs it (dev extra), and a deployment
#: without it simply classifies ``.pdf`` files as unsupported instead of
#: failing the run.
PDF_EXTENSION = ".pdf"


def optional_pdf_enabled() -> bool:
    """True when the already-declared ``pypdf`` dependency is importable."""
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class KnowledgeConfig:
    """Validated, immutable settings for one service instance."""

    enabled: bool
    source_roots: tuple[Path, ...]  # resolved absolute roots (the allowlist)
    index_path: Path  # derived index store (rebuildable)
    embedding_provider: str  # "hash" | "sentence-transformers"
    embedding_model_path: str  # local model dir; required for s-t provider
    max_file_bytes: int
    max_files: int
    chunk_tokens: int
    chunk_overlap_ratio: float
    search_top_k: int
    allowed_extensions: frozenset[str]

    @property
    def configured(self) -> bool:
        """Authorized roots exist — the subsystem can actually ingest."""
        return bool(self.source_roots)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> KnowledgeConfig:
        """Build a validated config from the process settings.

        Validation is *structural* here (limits sane, provider known, paths
        absolute); root existence is checked by :meth:`require_roots` at
        operation time so ``status`` can still report a broken root honestly
        instead of failing to construct at all.
        """
        settings = settings or get_settings()
        provider = settings.knowledge_embedding_provider.strip().lower()
        if provider not in {"hash", "sentence-transformers"}:
            raise KnowledgeConfigError(
                f"unknown knowledge embedding provider {provider!r}",
                hint="set NEXUS_KNOWLEDGE_EMBEDDING_PROVIDER to 'hash' or 'sentence-transformers'",
            )
        if settings.knowledge_max_file_bytes < 1:
            raise KnowledgeConfigError("knowledge_max_file_bytes must be >= 1")
        if settings.knowledge_max_files < 1:
            raise KnowledgeConfigError("knowledge_max_files must be >= 1")
        if settings.knowledge_chunk_tokens < 1:
            raise KnowledgeConfigError("knowledge_chunk_tokens must be >= 1")
        if not 0.0 <= settings.knowledge_chunk_overlap_ratio < 1.0:
            raise KnowledgeConfigError("knowledge_chunk_overlap_ratio must be in [0, 1)")
        if settings.knowledge_search_top_k < 1:
            raise KnowledgeConfigError("knowledge_search_top_k must be >= 1")

        roots: list[Path] = []
        for raw in settings.knowledge_source_roots:
            candidate = Path(raw).expanduser()
            roots.append(candidate.resolve())
        extensions = frozenset(DEFAULT_ALLOWED_EXTENSIONS)
        if optional_pdf_enabled():
            extensions = extensions | {PDF_EXTENSION}
        return cls(
            enabled=settings.knowledge_enabled,
            source_roots=tuple(roots),
            index_path=Path(settings.knowledge_index_path).expanduser().resolve(),
            embedding_provider=provider,
            embedding_model_path=settings.knowledge_embedding_model_path,
            max_file_bytes=settings.knowledge_max_file_bytes,
            max_files=settings.knowledge_max_files,
            chunk_tokens=settings.knowledge_chunk_tokens,
            chunk_overlap_ratio=settings.knowledge_chunk_overlap_ratio,
            search_top_k=settings.knowledge_search_top_k,
            allowed_extensions=extensions,
        )

    def require_enabled(self) -> None:
        from nexus_ai_agent.knowledge_retrieval.errors import KnowledgeDisabledError

        if not self.enabled:
            raise KnowledgeDisabledError(
                "knowledge retrieval is disabled",
                hint="set NEXUS_KNOWLEDGE_ENABLED=true to enable it",
            )

    def require_roots(self) -> tuple[Path, ...]:
        """Return configured roots, failing closed when any is unusable."""
        from nexus_ai_agent.knowledge_retrieval.errors import (
            KnowledgeConfigError,
            KnowledgeDisabledError,
        )

        if not self.enabled:
            raise KnowledgeDisabledError(
                "knowledge retrieval is disabled",
                hint="set NEXUS_KNOWLEDGE_ENABLED=true to enable it",
            )
        if not self.source_roots:
            raise KnowledgeConfigError(
                "no authorized knowledge source roots are configured",
                hint="set NEXUS_KNOWLEDGE_SOURCE_ROOTS to one or more directories",
            )
        broken = [str(root) for root in self.source_roots if not root.is_dir()]
        if broken:
            raise KnowledgeConfigError(
                "configured knowledge source root(s) missing or not a directory: "
                + ", ".join(broken),
                hint="fix NEXUS_KNOWLEDGE_SOURCE_ROOTS",
            )
        return self.source_roots
