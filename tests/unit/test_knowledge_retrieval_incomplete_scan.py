"""Remediation proof for the incomplete-scan data-loss defect (Phase 3).

A failed, interrupted, unreadable or budget-limited scan must never be
treated as proof that a source is absent.  This module locks that rule
with failing-first regressions (defect reproduced before the scanner /
service fix, green afterwards) against a real SQLite registry and a
temporary file tree — exactly the reproduction the gate requires.

Cases:
  1. unreadable nested directory (os.walk onerror)
  2. incomplete traversal (walk raises / onerror)
  3. budget truncation (max_files)
  4. true source removal (complete scan proves absence → deleted)
  5. subsequent successful recovery (incomplete → complete)
  6. stale indexed chunks cannot be returned after confirmed deletion
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.knowledge_retrieval.config import (
    DEFAULT_ALLOWED_EXTENSIONS,
    KnowledgeConfig,
)
from nexus_ai_agent.knowledge_retrieval.models import QueryState
from nexus_ai_agent.knowledge_retrieval.repository import KnowledgeSourceRepository
from nexus_ai_agent.knowledge_retrieval.scanner import ScanOutcome, ScannedSource, scan_roots, source_id_for
from nexus_ai_agent.knowledge_retrieval.service import KnowledgeRetrievalService
from nexus_ai_agent.storage.db import create_all_tables, get_session


class FakeIndex:
    """Deterministic in-memory index port."""

    def __init__(self) -> None:
        self.docs: dict[str, tuple] = {}
        self.generation = 0

    async def replace_source(self, source, chunks):  # type: ignore[no-untyped-def]
        self.docs[source.source_id] = (source, list(chunks))
        return len(chunks)

    async def remove_source(self, source_id: str) -> bool:
        return self.docs.pop(source_id, None) is not None

    async def search(self, query: str, top_k: int):  # type: ignore[no-untyped-def]
        from nexus_ai_agent.features.rag_core import tokenize

        tokens = set(tokenize(query))
        from nexus_ai_agent.knowledge_retrieval.ports import IndexHit

        hits = []
        for sid, (meta, chunks) in self.docs.items():
            for ch in chunks:
                if tokens & set(tokenize(ch.text)):
                    hits.append(
                        IndexHit(
                            source_id=sid,
                            sha256=meta.sha256,
                            path=meta.path,
                            chunk_index=ch.chunk_index,
                            start=ch.start,
                            end=ch.end,
                            score=float(len(tokens & set(tokenize(ch.text)))),
                            text=ch.text,
                            page=ch.page,
                        )
                    )
        hits.sort(key=lambda h: (-h.score, h.source_id))
        return hits[:top_k]

    async def clear(self) -> None:
        self.docs.clear()
        self.generation += 1

    async def inspect(self):  # type: ignore[no-untyped-def]
        from nexus_ai_agent.knowledge_retrieval.ports import StoreInfo

        present = bool(self.docs)
        return StoreInfo(
            present=present,
            corrupt=False,
            node_count=sum(len(c[1]) for c in self.docs.values()),
            generation=self.generation,
            rebuilt_at=None,
        )


def _config(root: Path, tmp_path: Path, **overrides) -> KnowledgeConfig:  # type: ignore[no-untyped-def]
    base: dict[str, object] = {
        "enabled": True,
        "source_roots": (root.resolve(),),
        "index_path": tmp_path / "index",
        "embedding_provider": "hash",
        "embedding_model_path": "",
        "max_file_bytes": 100_000,
        "max_files": 100,
        "chunk_tokens": 48,
        "chunk_overlap_ratio": 0.1,
        "search_top_k": 5,
        "allowed_extensions": DEFAULT_ALLOWED_EXTENSIONS,
    }
    base.update(overrides)
    return KnowledgeConfig(**base)  # type: ignore[arg-type]


@pytest.fixture()
async def db_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_file = tmp_path / "app.sqlite"
    monkeypatch.setenv("NEXUS_DB_PATH", str(db_file))
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings_module.get_settings.cache_clear()
    await create_all_tables(str(db_file))
    return db_file


def make_service(root: Path, tmp_path: Path, **overrides):  # type: ignore[no-untyped-def]
    fake = overrides.pop("fake", None)
    index = fake if fake is not None else FakeIndex()
    cfg = _config(root, tmp_path, **overrides)
    svc = KnowledgeRetrievalService(cfg, index, session_factory=get_session)
    return svc, index


# ── 1. unreadable nested directory ────────────────────────────────────────


async def test_unreadable_nested_directory_preserves_registry(
    tmp_path: Path, db_ready: Path
) -> None:
    """A nested directory that cannot be listed must not delete its children."""
    root = tmp_path / "kb"
    root.mkdir()
    sub = root / "nested"
    sub.mkdir()
    (root / "top.md").write_text("Top-level content about kubernetes.")
    (sub / "deep.md").write_text("Deep content about quantum turnips.")
    svc, index = make_service(root, tmp_path)
    first = await svc.index_sources()
    assert first.ingested == 2
    assert len(await svc.list_sources()) == 2
    # Search proves both are indexed
    assert (await svc.search("quantum turnips")).state == QueryState.OK

    # Simulate unreadable nested directory: os.walk reports an error via onerror
    import os

    original_walk = os.walk

    def mock_walk(top, topdown=True, onerror=None, followlinks=False):  # type: ignore[no-untyped-def]
        # Yield only the top directory; simulate listdir failure for nested/
        # by invoking onerror.
        if onerror is not None:
            err = OSError(13, "Permission denied", str(sub))
            err.filename = str(sub)
            onerror(err)
        # Normal walk for top only
        yield (str(root), ["nested"], ["top.md"])
        # Do not descend into nested/ at all

    with patch("nexus_ai_agent.knowledge_retrieval.scanner.os.walk", side_effect=mock_walk):
        # Also need to patch service's import path (service imports scan_roots at runtime)
        # scan_roots itself uses scanner.os.walk, so patching scanner is enough
        second = await svc.index_sources()
        # Incomplete scan must not delete
        assert second.removed == 0, "incomplete scan must not delete registry rows"
        assert len(await svc.list_sources()) == 2
        # Deep content still searchable (stale chunks preserved via registry)
        assert (await svc.search("quantum turnips")).state == QueryState.OK
        assert "unreadable" in " ".join(second.errors).lower() or "incomplete" in " ".join(
            second.errors
        ).lower()


# ── 2. incomplete traversal via walk exception ────────────────────────────


async def test_incomplete_traversal_preserves_all(
    tmp_path: Path, db_ready: Path
) -> None:
    """Any walk exception must preserve the registry."""
    root = tmp_path / "kb2"
    root.mkdir()
    (root / "a.md").write_text("Content about webhooks.")
    (root / "b.md").write_text("Content about retention.")
    svc, _ = make_service(root, tmp_path)
    await svc.index_sources()
    assert len(await svc.list_sources()) == 2

    # Patch scan_roots to return an incomplete outcome directly
    incomplete_outcome = ScanOutcome(
        sources=tuple(),
        issues=tuple(),
        complete=False,
        incomplete_reason="simulated_walk_error",
    )
    with patch(
        "nexus_ai_agent.knowledge_retrieval.service.scan_roots", return_value=incomplete_outcome
    ):
        report = await svc.index_sources()
        assert report.removed == 0
        assert len(await svc.list_sources()) == 2


# ── 3. budget truncation ─────────────────────────────────────────────────


async def test_budget_truncation_never_deletes_unprocessed_sources(
    tmp_path: Path, db_ready: Path
) -> None:
    """max_files truncation must not treat skipped files as deleted."""
    root = tmp_path / "kb3"
    root.mkdir()
    (root / "alpha.md").write_text("Alpha vintage routers")
    (root / "zeta.md").write_text("Zeta quantum turnips")
    svc, index = make_service(root, tmp_path)
    first = await svc.index_sources()
    assert first.ingested == 2

    # Budget 1: only alpha processed
    capped, _ = make_service(root, tmp_path, max_files=1, fake=index)  # type: ignore[arg-type]
    # Need to reuse same index/docs but new service with capped config
    capped._index = index  # type: ignore[attr-defined]
    report = await capped.index_sources()
    assert report.truncated is True
    assert report.removed == 0
    assert {r.path for r in await capped.list_sources()} == {"alpha.md", "zeta.md"}
    # Skipped file still searchable
    assert (await capped.search("quantum turnips")).state == QueryState.OK


# ── 4. true source removal ─────────────────────────────────────────────────


async def test_true_source_removal_deletes_and_stale_chunks_not_returned(
    tmp_path: Path, db_ready: Path
) -> None:
    """A genuinely deleted file must be removed from registry and index."""
    root = tmp_path / "kb4"
    root.mkdir()
    (root / "keep.md").write_text("Webhook gateway configuration alpha bravo")
    (root / "drop.md").write_text("Ancient fax modem xray yankee zulu")
    svc, index = make_service(root, tmp_path)
    await svc.index_sources()
    assert len(await svc.list_sources()) == 2
    assert (await svc.search("xray yankee")).state == QueryState.OK

    # Delete drop.md on disk and run a complete scan
    (root / "drop.md").unlink()
    report = await svc.index_sources()
    assert report.removed == 1
    assert len(await svc.list_sources()) == 1
    assert (await svc.list_sources())[0].path == "keep.md"
    # Stale chunks must not be returned — query that only matched drop.md now empty
    assert (await svc.search("xray yankee")).state == QueryState.NO_MATCHES
    assert index.docs.keys() == {next(iter(index.docs.keys()))}
    # Verify no hit returns the deleted path
    hits = await index.search("xray yankee", 5)
    assert all(h.path != "drop.md" for h in hits)


# ── 5. subsequent successful recovery ─────────────────────────────────────


async def test_subsequent_successful_recovery_after_incomplete(
    tmp_path: Path, db_ready: Path
) -> None:
    """Incomplete preserves → next complete run with file truly gone deletes."""
    root = tmp_path / "kb5"
    root.mkdir()
    (root / "persist.md").write_text("Persisted content about retention")
    (root / "temp.md").write_text("Temporary content about ephemeral")
    svc, _ = make_service(root, tmp_path)
    await svc.index_sources()
    assert len(await svc.list_sources()) == 2

    # Simulate incomplete scan (e.g., root temporarily unreadable)
    incomplete = ScanOutcome(sources=tuple(), issues=tuple(), complete=False, incomplete_reason="unreadable_root")
    with patch(
        "nexus_ai_agent.knowledge_retrieval.service.scan_roots", return_value=incomplete
    ):
        r1 = await svc.index_sources()
        assert r1.removed == 0
        assert len(await svc.list_sources()) == 2

    # Now delete temp.md and run a complete scan — should delete
    (root / "temp.md").unlink()
    r2 = await svc.index_sources()
    assert r2.removed == 1
    assert len(await svc.list_sources()) == 1
    assert (await svc.search("ephemeral")).state == QueryState.NO_MATCHES
    assert (await svc.search("retention")).state == QueryState.OK


# ── 6. scanner completeness flag ───────────────────────────────────────────


def test_scan_marks_unreadable_root_incomplete(tmp_path: Path) -> None:
    root = tmp_path / "does_not_exist"
    cfg = _config(tmp_path / "kb_dummy", tmp_path)
    # Use a non-existent root
    outcome = scan_roots(cfg, (root,))
    assert outcome.complete is False
    assert outcome.incomplete_reason is not None
    assert any(i.reason == "unreadable" for i in outcome.issues)


def test_scan_complete_for_normal_tree(tmp_path: Path) -> None:
    root = tmp_path / "kb6"
    root.mkdir()
    (root / "a.md").write_text("hello")
    cfg = _config(root, tmp_path)
    outcome = scan_roots(cfg, (root,))
    assert outcome.complete is True
    assert len(outcome.sources) == 1


def test_scan_budget_truncation_does_not_affect_completeness(tmp_path: Path) -> None:
    """Budget truncation is a processing limit, not a scan incompleteness."""
    root = tmp_path / "kb7"
    root.mkdir()
    for name in ["a.md", "b.md", "c.md"]:
        (root / name).write_text(f"content {name}")
    cfg = _config(root, tmp_path, max_files=1)
    outcome = scan_roots(cfg, (root,))
    # Scanner itself is complete — budget is applied in service
    assert outcome.complete is True
    assert len(outcome.sources) == 3
