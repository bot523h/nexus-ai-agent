"""Application service: controlled ingestion, evidence-grounded search, status.

Responsibilities (the smallest complete V1 slice, see
``docs/architecture/KNOWLEDGE_RETRIEVAL.md``):

* **index** — scan the configured (authorized) roots, hash every candidate,
  skip unchanged content, re-ingest changed content, drop sources that
  disappeared, and record typed outcomes for rejected/unsupported files;
  the canonical registry is written **after** the derived index write
  succeeds, so an interrupted run can never make the database claim a
  version the index does not have — rerunning converges idempotently;
* **search** — query the index and build citations that are *verified
  against the registry*: a hit whose ``sha256`` no longer matches the
  registered version is dropped as stale, never presented as current;
* **status** — an honest, never-raising report of enabled/configured state,
  store health, provider availability and registry counts;
* **rebuild** — clear the derived store and the registry view, then reindex
  from the authoritative sources on disk (deterministic);
* **unregister** — remove one source from the registry *and* the index.

Boundary rules enforced here:

* retrieved text is **data** — it is returned as evidence and never acted
  upon; this package imports no tool, bus, executor or LLM surface
  (``tests/architecture/test_knowledge_retrieval_boundary.py``);
* failures are typed (``errors.py``), logged with counts and safe paths,
  and never leak document content or stack traces to callers;
* disabled/unconfigured states fail closed with distinct errors instead of
  reporting a fake empty success.
"""

from __future__ import annotations

import bisect
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai_agent.knowledge_retrieval.config import (
    KnowledgeConfig,
    provider_availability,
)
from nexus_ai_agent.knowledge_retrieval.errors import (
    IndexUnavailableError,
    KnowledgeConfigError,
    KnowledgeRetrievalError,
    KnowledgeStoreError,
)
from nexus_ai_agent.knowledge_retrieval.models import (
    ChunkPayload,
    Evidence,
    IndexStatus,
    IngestReport,
    QueryState,
    SearchResponse,
    SourceMeta,
    SourceRecord,
    SourceStatus,
)
from nexus_ai_agent.knowledge_retrieval.ports import KnowledgeIndexPort
from nexus_ai_agent.knowledge_retrieval.repository import KnowledgeSourceRepository
from nexus_ai_agent.knowledge_retrieval.scanner import (
    scan_roots,
    source_id_for,
)
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.db import get_session

__all__ = ["KnowledgeRetrievalService"]

logger = get_logger(__name__)


#: Upper bound of per-run error strings carried in a report (the rest are
#: counted, never dumped): an operator sees the shape of the failure, not a
#: wall of paths.
def _distinctive_tokens(text: str) -> set[str]:
    """Query/hit vocabulary for the lexical-overlap guard.

    Same tokenizer as the hash embedder (``features.rag_core`` — lowercase,
    unicode-normalised), restricted to tokens of >= 4 characters so short
    stopwords ("with", "from", ...) cannot manufacture a fake overlap.
    """
    from nexus_ai_agent.features.rag_core import tokenize

    return {token for token in tokenize(text) if len(token) >= 4}


_MAX_REPORT_ERRORS = 20

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class KnowledgeRetrievalService:
    """Orchestrates the registry, the scanner and the index port."""

    def __init__(
        self,
        config: KnowledgeConfig,
        index: KnowledgeIndexPort,
        *,
        session_factory: SessionFactory | None = None,
        repository: KnowledgeSourceRepository | None = None,
    ) -> None:
        self._config = config
        self._index = index
        self._session_factory = session_factory or get_session
        self._repository = repository or KnowledgeSourceRepository(self._session_factory)

    @property
    def config(self) -> KnowledgeConfig:
        return self._config

    # ── ingestion ───────────────────────────────────────────────────────

    async def index_sources(self) -> IngestReport:
        """Incremental (re)index of every configured root. Idempotent."""
        roots = self._config.require_roots()
        started = time.monotonic()
        run_id = uuid.uuid4().hex[:16]

        outcome = scan_roots(self._config, roots)
        candidates = outcome.sources[: self._config.max_files]
        truncated = len(outcome.sources) > self._config.max_files
        # Stale detection answers "does this source still exist anywhere in
        # the authorized roots?" — against the COMPLETE scan, never against
        # the max_files processing budget. A file beyond the budget is
        # unprocessed this run, not deleted (its evidence stays intact).
        scanned_ids = {source_id_for(source.root, source.rel_path) for source in outcome.sources}

        registry = {record.source_id: record for record in await self._repository.list_all()}
        counters = {"new": 0, "modified": 0, "unchanged": 0, "failed": 0, "chunks": 0}
        removed = 0
        errors: list[str] = []

        def note(message: str) -> None:
            if len(errors) < _MAX_REPORT_ERRORS:
                errors.append(message)

        for issue in outcome.issues:
            note(f"{issue.path}: {issue.reason}")
        if truncated:
            note(f"budget: max_files={self._config.max_files} reached, run truncated")

        for candidate in candidates:
            source_id = source_id_for(candidate.root, candidate.rel_path)
            prior = registry.get(source_id)
            try:
                content = _read_content(candidate.path, self._config, root=candidate.root)
            except _ContentRejected as rejection:
                # The file stopped being indexable (binary/decode/empty/
                # unreadable): never keep serving its previous version.
                if prior is not None:
                    await self._drop_source(source_id)
                    await self._repository.set_error(
                        source_id,
                        root=str(candidate.root),
                        path=candidate.rel_path,
                        reason=rejection.reason,
                    )
                counters["failed"] += 1
                note(f"{candidate.rel_path}: {rejection.reason}")
                continue

            sha256 = content.sha256
            if (
                prior is not None
                and prior.status is SourceStatus.INGESTED
                and prior.sha256 == sha256
            ):
                counters["unchanged"] += 1
                continue

            try:
                payloads = build_chunk_payloads(
                    content.text, content.pages, self._config, suffix=candidate.path.suffix
                )
                if not payloads:
                    raise _ContentRejected("empty")
                await self._index.replace_source(
                    SourceMeta(
                        source_id=source_id,
                        path=candidate.rel_path,
                        root=str(candidate.root),
                        sha256=sha256,
                        ingested_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                    payloads,
                )
                await self._repository.upsert_ingested(
                    source_id=source_id,
                    root=str(candidate.root),
                    path=candidate.rel_path,
                    sha256=sha256,
                    size_bytes=len(content.raw),
                    chunks=len(payloads),
                )
            except KnowledgeStoreError:
                raise  # database-level failures are systemic, not per-file
            except KnowledgeRetrievalError as exc:
                if prior is not None:
                    await self._drop_source(source_id)
                    await self._repository.set_error(
                        source_id,
                        root=str(candidate.root),
                        path=candidate.rel_path,
                        reason=exc.message,
                    )
                counters["failed"] += 1
                note(f"{candidate.rel_path}: {exc.code}")
                continue
            except Exception as exc:  # noqa: BLE001 — one bad file must not kill the run
                if prior is not None:
                    await self._drop_source(source_id)
                    await self._repository.set_error(
                        source_id,
                        root=str(candidate.root),
                        path=candidate.rel_path,
                        reason=type(exc).__name__,
                    )
                counters["failed"] += 1
                note(f"{candidate.rel_path}: {type(exc).__name__}")
                continue

            counters["chunks"] += len(payloads)
            if prior is None:
                counters["new"] += 1
            else:
                counters["modified"] += 1

        # Stale cleanup: registry rows whose source is no longer offered by
        # ANY configured root (deleted, renamed, root removed from config).
        # Sources merely skipped by the max_files budget remain in
        # ``scanned_ids`` and are never touched here.
        stale = [source_id for source_id in registry if source_id not in scanned_ids]
        if stale:
            removed = await self._drop_sources(stale)

        report = IngestReport(
            run_id=run_id,
            scanned=len(outcome.sources),
            ingested=counters["new"] + counters["modified"],
            new=counters["new"],
            modified=counters["modified"],
            unchanged=counters["unchanged"],
            removed=removed,
            unsupported=sum(1 for i in outcome.issues if i.reason == "unsupported"),
            rejected=sum(
                1
                for i in outcome.issues
                if i.reason in {"symlink", "traversal", "secret", "oversize", "unreadable"}
            ),
            failed=counters["failed"],
            chunks_written=counters["chunks"],
            truncated=truncated,
            errors=tuple(errors),
            duration_s=time.monotonic() - started,
        )
        logger.info(
            "knowledge_index_run",
            run_id=run_id,
            ingested=report.ingested,
            unchanged=report.unchanged,
            removed=report.removed,
            rejected=report.rejected,
            failed=report.failed,
            chunks=report.chunks_written,
            truncated=report.truncated,
            duration_s=round(report.duration_s, 3),
        )
        return report

    async def rebuild(self) -> IngestReport:
        """Clear derived state, then reindex everything from disk (explicit)."""
        self._config.require_roots()
        self._config.require_enabled()
        started = time.monotonic()
        await self._index.clear()
        existing = [record.source_id for record in await self._repository.list_all()]
        await self._repository.delete_many(existing)
        report = await self.index_sources()
        logger.info(
            "knowledge_index_rebuild",
            run_id=report.run_id,
            dropped=len(existing),
            ingested=report.ingested,
            duration_s=round(time.monotonic() - started, 3),
        )
        return IngestReport(
            run_id=report.run_id,
            scanned=report.scanned,
            ingested=report.ingested,
            new=report.new,
            modified=report.modified,
            unchanged=report.unchanged,
            removed=report.removed,
            unsupported=report.unsupported,
            rejected=report.rejected,
            failed=report.failed,
            chunks_written=report.chunks_written,
            truncated=report.truncated,
            errors=report.errors,
            duration_s=time.monotonic() - started,
            rebuilt=True,
        )

    # ── retrieval ───────────────────────────────────────────────────────

    async def search(self, query: str, top_k: int | None = None) -> SearchResponse:
        """Evidence-grounded search with registry-verified citations."""
        self._config.require_enabled()
        stripped = (query or "").strip()
        if not stripped:
            return SearchResponse(query=query or "", state=QueryState.EMPTY_QUERY)

        limit = top_k if top_k is not None else self._config.search_top_k
        if limit < 1:
            raise KnowledgeConfigError("top_k must be >= 1")
        if limit > 50:
            raise KnowledgeConfigError("top_k must be <= 50")

        # Probe the embedding provider before touching the store: an
        # unavailable provider is a typed configuration problem, and must not
        # be misdiagnosed as a corrupt index on disk.
        provider_ok, provider_reason = provider_availability(self._config)
        if not provider_ok:
            raise IndexUnavailableError(
                f"embedding provider '{self._config.embedding_provider}' unavailable"
                + (f": {provider_reason}" if provider_reason else ""),
                hint="set NEXUS_KNOWLEDGE_EMBEDDING_MODEL_PATH to an existing "
                "local sentence-transformers directory, or use provider=hash",
                code="embedding",
            )

        info = await self._index.inspect()
        if info.corrupt:
            raise IndexUnavailableError(
                "knowledge index store is corrupt",
                hint="run `nexus knowledge rebuild` to recreate the derived index",
            )
        if not info.present:
            return SearchResponse(query=stripped, state=QueryState.EMPTY_INDEX)

        # A vector store always yields *some* nearest neighbour.  Presenting
        # a zero-overlap neighbour as evidence would fabricate relevance, so
        # queries without a single distinctive token (>= 4 chars) and hits
        # without any shared distinctive token are treated as "no match".
        query_tokens = _distinctive_tokens(stripped)
        if not query_tokens:
            return SearchResponse(
                query=stripped,
                state=QueryState.NO_MATCHES,
                index_generation=info.generation,
            )

        hits = await self._index.search(stripped, limit)
        if not hits:
            return SearchResponse(
                query=stripped,
                state=QueryState.NO_MATCHES,
                index_generation=info.generation,
            )

        rows = await self._repository.get_many(list(dict.fromkeys(h.source_id for h in hits)))
        evidence: list[Evidence] = []
        dropped_stale = 0
        filtered_unrelated = 0
        for hit in hits:
            row = rows.get(hit.source_id)
            if row is None or row.status is not SourceStatus.INGESTED or row.sha256 != hit.sha256:
                dropped_stale += 1
                continue
            if not (query_tokens & _distinctive_tokens(hit.text)):
                # Nearest neighbour, but no shared distinctive token with the
                # query: unrelated, not evidence (e.g. an off-topic doc that
                # merely embeds "close" for a niche query).
                filtered_unrelated += 1
                continue
            evidence.append(
                Evidence(
                    source_id=hit.source_id,
                    path=row.path,
                    root=row.root,
                    sha256=row.sha256,
                    chunk_index=hit.chunk_index,
                    start=hit.start,
                    end=hit.end,
                    score=hit.score,
                    excerpt=hit.text,
                    page=hit.page,
                )
            )

        state = QueryState.OK if evidence else QueryState.NO_MATCHES
        logger.info(
            "knowledge_search",
            query_chars=len(stripped),
            state=state.value,
            hits=len(hits),
            evidence=len(evidence),
            dropped_stale=dropped_stale,
            filtered_unrelated=filtered_unrelated,
        )
        return SearchResponse(
            query=stripped,
            state=state,
            evidence=tuple(evidence),
            dropped_stale=dropped_stale,
            filtered_unrelated=filtered_unrelated,
            index_generation=info.generation,
        )

    # ── status / listing / unregister ───────────────────────────────────

    async def status(self) -> IndexStatus:
        """Honest subsystem status; this method never raises."""
        configured = bool(self._config.source_roots) and all(
            root.is_dir() for root in self._config.source_roots
        )
        info = await self._inspect_safely()
        records = await self._list_safely()
        provider_ok, provider_reason = provider_availability(self._config)
        ingested = sum(1 for r in records if r.status is SourceStatus.INGESTED)
        errors = sum(1 for r in records if r.status is SourceStatus.ERROR)

        degraded: str | None = None
        if info is None:
            degraded = "index_inspect_failed"
        elif info.corrupt:
            degraded = "index_store_corrupt"
        elif not provider_ok:
            degraded = provider_reason
        elif records and ingested > 0 and not info.present:
            degraded = "index_missing_rebuild_required"
        elif info.load_error and not info.corrupt:
            degraded = info.load_error

        return IndexStatus(
            enabled=self._config.enabled,
            configured=configured,
            store_present=bool(info and info.present),
            store_corrupt=bool(info and info.corrupt),
            node_count=(info.node_count if info else None),
            generation=(info.generation if info else None),
            source_count=len(records),
            ingested_count=ingested,
            error_count=errors,
            embedding_provider=self._config.embedding_provider,
            provider_available=provider_ok,
            degraded_reason=degraded,
            index_path=str(self._config.index_path),
        )

    async def list_sources(self) -> list[SourceRecord]:
        """Registry contents (operator view). Disabled state is still listed:
        reading rows is not an operation that must fail closed."""
        return await self._list_safely()

    async def unregister(self, reference: str) -> bool:
        """Remove one source (by id or by its path) from index + registry."""
        self._config.require_enabled()
        ref = (reference or "").strip()
        if not ref:
            raise KnowledgeConfigError("source reference must not be empty")
        records = await self._repository.list_all()
        matches = [r for r in records if r.source_id == ref]
        if not matches:
            matches = [r for r in records if r.path == ref]
        if not matches:
            # Suffix fallback for "sub/dir/name.md" references — only on a
            # path-segment boundary, so unregister("a.md") can never silently
            # match "data.md".
            suffix = "/" + ref.lstrip("/")
            matches = [r for r in records if r.path.endswith(suffix)]
        if not matches:
            return False
        if len(matches) > 1:
            raise KnowledgeConfigError(
                f"source reference {ref!r} is ambiguous ({len(matches)} matches)",
                hint="use the source_id printed by `nexus knowledge sources list --json`",
            )
        target = matches[0]
        await self._index.remove_source(target.source_id)
        await self._repository.delete(target.source_id)
        logger.info("knowledge_source_unregistered", source_id=target.source_id, path=target.path)
        return True

    # ── internals ───────────────────────────────────────────────────────

    async def _drop_source(self, source_id: str) -> None:
        """Best-effort removal of one source's derived index entries."""
        try:
            await self._index.remove_source(source_id)
        except KnowledgeRetrievalError:
            # The registry write below still records the failure; search
            # verifies sha256 against the registry, so a leftover node can
            # never surface as current evidence.
            logger.warning("knowledge_drop_index_failed", source_id=source_id)

    async def _drop_sources(self, source_ids: Sequence[str]) -> int:
        removed = 0
        for source_id in source_ids:
            await self._drop_source(source_id)
            removed += 1
        await self._repository.delete_many(source_ids)
        return removed

    async def _inspect_safely(self):  # type: ignore[no-untyped-def]
        try:
            return await self._index.inspect()
        except Exception:  # noqa: BLE001 — status must never raise
            return None

    async def _list_safely(self) -> list[SourceRecord]:
        try:
            return await self._repository.list_all()
        except KnowledgeRetrievalError:
            return []


# ── content reading & chunking (pure, unit-tested) ────────────────────────


class _ContentRejected(Exception):
    """Internal: a file's content could not be indexed (safe reason string)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Content:
    def __init__(
        self,
        raw: bytes,
        text: str,
        sha256: str,
        pages: list[tuple[int, int, int]] | None = None,
    ) -> None:
        self.raw = raw
        self.text = text
        self.sha256 = sha256
        self.pages = pages


def _open_under_root(root: Path, rel: Path) -> int:
    """Open the file ``root / rel`` through a descriptor-relative walk.

    Trust anchor: the **directory handle** opened on the configured root, not
    a pathname that is re-resolved later. Every component — intermediate *and*
    final — is opened with ``O_NOFOLLOW`` relative to the previous
    descriptor, so:

    * a parent directory swapped to a symlink before the open cannot steer
      the walk outside (the symlink component is refused — ``ELOOP`` or, under
      ``O_DIRECTORY``, ``ENOTDIR``);
    * there is nothing to race *after* the open: the path is never consulted
      again — no identity check and no containment re-check whose timing an
      attacker could dodge with a redirect-then-restore (identity passes
      while redirected, containment passes after restore). Bytes come from an
      unbroken chain of descriptors that began at the root handle.

    Every descriptor this function opens — the root handle and every
    intermediate directory — is closed before it returns, on success *and* on
    failure; only the final file descriptor is handed to the caller. A platform
    without ``dir_fd`` support **fails closed** with reason ``unsupported``:
    a pathname-based substitute would reintroduce the race the walk exists to
    remove.
    """
    import errno
    import os

    base = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    parts = tuple(part for part in rel.parts if part not in ("", "."))
    if not parts or ".." in parts:
        raise _ContentRejected("traversal")
    try:
        root_fd = os.open(root, base | os.O_DIRECTORY | nofollow)
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ELOOP:
            raise _ContentRejected("symlink") from None
        raise _ContentRejected("unreadable") from None
    current = root_fd
    try:
        for index, part in enumerate(parts):
            flags = base | nofollow
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            try:
                next_fd = os.open(part, flags, dir_fd=current)
            except NotImplementedError:
                # No dir_fd on this platform: refusing beats guessing — a
                # path-based fallback would reopen the parent-redirect race.
                raise _ContentRejected(
                    "unsupported",
                ) from None
            except OSError as exc:
                # Linux: final-component symlink → ELOOP; symlink under
                # O_DIRECTORY → ENOTDIR ("not a directory" — the link is
                # never resolved). Both are the no-follow refusal we want.
                if getattr(exc, "errno", None) in (errno.ELOOP, errno.ENOTDIR):
                    raise _ContentRejected("symlink") from None
                raise _ContentRejected("unreadable") from None
            if current is not root_fd:
                os.close(current)
            current = next_fd
    except BaseException:
        if current is not root_fd:
            os.close(current)
        os.close(root_fd)
        raise
    # Success: the walk is complete — the root handle is no longer needed and
    # must not leak (the caller owns only the final file descriptor).
    os.close(root_fd)
    return current


def _read_content(path: Path, config: KnowledgeConfig, *, root: Path | None = None) -> _Content:
    """Read, hash and decode one candidate; raises ``_ContentRejected``.

    With ``root`` (every production call site) the bytes come from the
    descriptor-relative walk of :func:`_open_under_root` — containment is
    structural, not a later pathname comparison, and the same descriptor is
    size-bounded and hashed: the PDF parser consumes these exact bytes, so a
    citation's text provably belongs to the sha256 served. Without ``root``
    (direct unit use) the file is opened no-follow with the same bounds.

    Deterministic failure taxonomy (also the report's reason vocabulary):
    ``symlink`` (a component refused the no-follow open — including a
    symlinked parent swapped in after the scan), ``traversal`` (path outside
    the root / ``..`` component), ``unsupported`` (platform without
    ``dir_fd`` — the secure walk refuses rather than guessing), ``unreadable`` (OSError / not a regular
    file), ``oversize`` (byte or text bound), ``binary`` (NUL sniff),
    ``decode`` (not UTF-8), ``empty`` (no retrievable text), ``pdf`` (parser
    failure), ``unsupported`` (pypdf absent — scan-time classification
    normally prevents reaching it).
    """
    import os

    if root is not None:
        try:
            rel = path.relative_to(root)
        except ValueError:
            raise _ContentRejected("traversal") from None
        fd = _open_under_root(root, rel)
    else:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            import errno

            if getattr(exc, "errno", None) == errno.ELOOP:
                raise _ContentRejected("symlink") from None
            raise _ContentRejected("unreadable") from None
    try:
        import hashlib
        import stat as stat_module

        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode):
            raise _ContentRejected("unreadable")
        if info.st_size > config.max_file_bytes:
            raise _ContentRejected("oversize")
        chunks: list[bytes] = []
        remaining = config.max_file_bytes + 1
        while remaining > 0:
            piece = os.read(fd, min(1 << 20, remaining))
            if not piece:
                break
            chunks.append(piece)
            remaining -= len(piece)
        raw = b"".join(chunks)
        if len(raw) > config.max_file_bytes:
            raise _ContentRejected("oversize")
    finally:
        os.close(fd)

    sha256 = hashlib.sha256(raw).hexdigest()
    if path.suffix.lower() == ".pdf":
        text, pages = _extract_pdf_text(raw, config.max_file_bytes)
    else:
        pages = None
        if b"\x00" in raw[:8192]:
            raise _ContentRejected("binary")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise _ContentRejected("decode") from None
        if len(text) > config.max_file_bytes:
            raise _ContentRejected("oversize")
    if not text.strip():
        raise _ContentRejected("empty")
    return _Content(raw=raw, text=text, sha256=sha256, pages=pages)


def _extract_pdf_text(data: bytes, max_chars: int) -> tuple[str, list[tuple[int, int, int]]]:
    """Extract text + per-page ``(start, end, page)`` spans (1-based pages).

    ``pypdf`` is an optional extra; a deployment without it classifies PDFs
    at scan time (unsupported), so this path only runs when the package is
    actually present.  Page spans let chunk metadata carry a real page number
    instead of a guess.  The parser consumes the already-hashed bytes — the
    text served as an excerpt can never come from a different file version
    than the one behind ``sha256``.
    """
    import io

    try:
        from pypdf import PdfReader
    except ImportError:
        raise _ContentRejected("unsupported") from None
    try:
        reader = PdfReader(io.BytesIO(data))
        pieces: list[str] = []
        spans: list[tuple[int, int, int]] = []
        cursor = 0
        for page_number, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
            start = cursor
            pieces.append(page_text)
            cursor += len(page_text)
            spans.append((start, cursor, page_number))
            if cursor > max_chars:
                raise _ContentRejected("oversize")
        return "".join(pieces), spans
    except _ContentRejected:
        raise
    except Exception:
        raise _ContentRejected("pdf") from None


def build_chunk_payloads(
    text: str,
    pages: list[tuple[int, int, int]] | None,
    config: KnowledgeConfig,
    *,
    suffix: str = "",
) -> list[ChunkPayload]:
    """Chunk ``text`` and attach deterministic location metadata.

    ``pages`` (optional PDF page spans, already extracted once by
    ``_read_content``) map each chunk's start offset to a real 1-based page;
    formats without pages yield ``page=None`` rather than an invented number.
    """
    from nexus_ai_agent.features.rag_core import chunk_text

    chunks = chunk_text(
        text,
        chunk_tokens=config.chunk_tokens,
        overlap_ratio=config.chunk_overlap_ratio,
    )

    page_starts: list[int] = []
    page_numbers: list[int] = []
    if pages:
        for start, _end, number in pages:
            page_starts.append(start)
            page_numbers.append(number)

    payloads: list[ChunkPayload] = []
    for chunk in chunks:
        page: int | None = None
        if page_starts:
            position = bisect.bisect_right(page_starts, chunk.start) - 1
            if 0 <= position < len(page_numbers):
                page = page_numbers[position]
        payloads.append(
            ChunkPayload(
                chunk_index=chunk.index,
                start=chunk.start,
                end=chunk.end,
                text=chunk.text,
                page=page,
            )
        )
    return payloads
