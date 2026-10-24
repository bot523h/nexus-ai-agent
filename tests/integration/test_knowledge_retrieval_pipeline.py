"""End-to-end knowledge-retrieval pipeline against the real LlamaIndex adapter.

Everything here is local-first and deterministic: hash embeddings (no model,
no credentials, no network), a temporary SQLite database, temporary source
roots and the shipped ``LlamaIndexKnowledgeIndex``.  The suite proves the
mission-critical behaviours end to end: evidence citations that resolve to
actual indexed bytes, restart persistence, staleness handling, corrupt-store
recovery via rebuild, interrupted-run convergence, fail-closed security
boundaries, and the no-network/no-LLM guarantee.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import socket
import sys
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.llamaindex_retrieval import LlamaIndexKnowledgeIndex
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.knowledge_retrieval import (
    KnowledgeConfig,
    KnowledgeRetrievalService,
)
from nexus_ai_agent.knowledge_retrieval.errors import IndexUnavailableError
from nexus_ai_agent.knowledge_retrieval.models import QueryState
from nexus_ai_agent.observability.logging import configure_logging


@pytest.fixture()
def kb_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated roots/DB/index; returns the source root."""
    source_root = tmp_path / "kb"
    source_root.mkdir()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("NEXUS_KNOWLEDGE_SOURCE_ROOTS", str(source_root))
    monkeypatch.setenv("NEXUS_KNOWLEDGE_INDEX_PATH", str(tmp_path / "knowledge_index"))
    monkeypatch.setenv("NEXUS_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("NEXUS_KNOWLEDGE_EMBEDDING_PROVIDER", "hash")
    monkeypatch.setenv("NEXUS_LOG_LEVEL", "WARNING")
    settings_module.get_settings.cache_clear()
    configure_logging("WARNING")

    from nexus_ai_agent.storage.db import create_all_tables

    asyncio.run(create_all_tables(str(tmp_path / "app.sqlite")))
    return source_root


def _service() -> KnowledgeRetrievalService:
    """Fresh stack (fresh adapter) — equivalent to a process restart."""
    settings = settings_module.get_settings()
    config = KnowledgeConfig.from_settings(settings)
    return KnowledgeRetrievalService(config, LlamaIndexKnowledgeIndex(config))


async def test_ingest_search_citation_roundtrip_and_restart(kb_env: Path) -> None:
    full_text = (
        "Deployment rollback runbook.\n"
        "Step one: identify the failing release in GitHub Actions.\n"
        "Step two: redeploy the previous immutable image tag.\n"
    )
    (kb_env / "runbook.md").write_text(full_text)

    report = await _service().index_sources()
    assert report.ingested == 1 and report.failed == 0

    response = await _service().search("redeploy previous image tag")
    assert response.state == "ok"
    ev = response.evidence[0]
    assert ev.path == "runbook.md"
    assert ev.sha256 == hashlib.sha256(full_text.encode()).hexdigest()
    assert 0 <= ev.start < ev.end <= len(full_text)
    assert ev.excerpt.strip() in full_text
    assert ev.citation == f"runbook.md@{ev.sha256[:12]}#{ev.chunk_index}"
    assert ev.score is not None and ev.score > 0

    # Process restart: a brand-new adapter must reload the persisted index and
    # return the same evidence (index state is derived, but durable).
    restarted = await _service().search("redeploy previous image tag")
    assert restarted.state == "ok"
    assert restarted.evidence[0].sha256 == ev.sha256
    assert restarted.index_generation == response.index_generation

    status = await _service().status()
    assert status.store_present and not status.store_corrupt
    assert status.degraded_reason is None
    assert status.node_count and status.node_count >= 1


async def test_modified_file_serves_new_version_and_tamper_is_dropped(
    kb_env: Path,
) -> None:
    v1 = "Release checklist v1: freeze the master branch."
    v2 = "Release checklist v2: freeze the trunk branch."
    target = kb_env / "checklist.md"
    target.write_text(v1)
    service = _service()
    await service.index_sources()

    target.write_text(v2)
    await service.index_sources()
    fresh = await service.search("freeze the trunk branch")
    assert fresh.state == "ok"
    assert fresh.evidence[0].sha256 == hashlib.sha256(v2.encode()).hexdigest()

    # The old wording is no longer in the store at all (replace semantics).
    old = await service.search("freeze the master branch")
    assert all(v1.split(":")[1].strip()[:20] not in ev.excerpt for ev in old.evidence)

    # Tampering with the registry must drop the stale hit instead of serving
    # content the authoritative database no longer vouches for.
    from sqlalchemy import update as sa_update

    from nexus_ai_agent.storage.models import KnowledgeSource

    async with service._session_factory() as session:
        await session.execute(sa_update(KnowledgeSource).values(sha256="0" * 64))
        await session.commit()
    tampered = await service.search("freeze the trunk branch")
    assert tampered.state == "no_matches"
    assert tampered.dropped_stale >= 1


async def test_unregister_and_file_removal_cleanly_converge(kb_env: Path) -> None:
    (kb_env / "keep.md").write_text("Webhook gateway configuration lives in ghouls-file.")
    (kb_env / "drop.md").write_text("Ancient fax modem baud rate notes.")
    service = _service()
    await service.index_sources()

    # Unregister one source: its evidence disappears, unrelated data stays.
    assert await service.unregister("drop.md") is True
    gone = await service.search("fax modem baud rate")
    assert gone.state == "no_matches"
    survivor = await service.search("webhook gateway configuration")
    assert survivor.state == "ok"
    assert {ev.path for ev in survivor.evidence} == {"keep.md"}

    # Deleting the file on disk converges on the next index run.
    (kb_env / "keep.md").unlink()
    report = await service.index_sources()
    assert report.removed == 1
    assert (await service.search("webhook gateway configuration")).state == "no_matches"


async def test_corrupt_store_is_typed_and_rebuild_recovers(kb_env: Path) -> None:
    (kb_env / "doc.md").write_text("Recovery target content about cache warmup.")
    await _service().index_sources()

    index_dir = Path(str(settings_module.get_settings().knowledge_index_path))
    (index_dir / "docstore.json").write_text("{ this is not json")

    status = await _service().status()
    assert status.degraded_reason == "index_store_corrupt"

    with pytest.raises(IndexUnavailableError, match="corrupt") as first:
        await _service().search("cache warmup")
    assert "rebuild" in (first.value.hint or "")

    rebuilt = await _service().rebuild()
    assert rebuilt.rebuilt is True and rebuilt.ingested == 1

    healthy = await _service().status()
    assert healthy.degraded_reason is None
    back = await _service().search("cache warmup")
    assert back.state == "ok" and back.evidence[0].path == "doc.md"


async def test_interrupted_index_fails_one_file_then_converges(
    kb_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (kb_env / "doc.md").write_text("Version one of the incident timeline.")
    service = _service()
    await service.index_sources()

    (kb_env / "doc.md").write_text("Version two of the incident timeline.")
    adapter = service._index
    original = adapter.replace_source
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IndexUnavailableError("simulated adapter crash")
        return await original(*args, **kwargs)

    monkeypatch.setattr(adapter, "replace_source", flaky)
    failed_run = await service.index_sources()
    assert failed_run.failed == 1

    status = await service.status()
    assert status.error_count == 1  # the failure is visible to operators

    # Nothing stale is served after the interrupted write: the prior nodes
    # were dropped and the registry row is in `error`, so the only correct
    # answer is `no_matches` — accepting "ok" here would let a regression
    # that still serves the v1 chunk pass unnoticed.
    interrupted = await service.search("incident timeline")
    assert interrupted.state == QueryState.NO_MATCHES
    assert interrupted.evidence == ()
    rows = await service.list_sources()
    assert rows[0].status.value == "error"

    # The next run converges: the file is re-ingested and served normally.
    monkeypatch.setattr(adapter, "replace_source", original)
    recovered = await service.index_sources()
    assert recovered.ingested == 1 and recovered.failed == 0
    healed = await service.search("incident timeline")
    assert healed.state == "ok"
    assert (
        healed.evidence[0].sha256
        == hashlib.sha256(b"Version two of the incident timeline.").hexdigest()
    )


async def test_security_boundaries_end_to_end(kb_env: Path, tmp_path: Path) -> None:
    (kb_env / "ok.md").write_text("Legitimate runbook about load shedding.")
    (kb_env / ".env").write_text("NEXUS_SUPER_SECRET=hunter2")
    outside = tmp_path / "outside-root.md"
    outside.write_text("Confidential payroll spreadsheet contents for q4.")
    (kb_env / "escape.md").symlink_to(outside)

    report = await _service().index_sources()
    assert report.ingested == 1
    assert report.rejected >= 2  # .env secret + symlink

    # Escaped/secret content must never appear as evidence.
    for query in ("hunter2", "confidential payroll spreadsheet"):
        response = await _service().search(query)
        assert response.state in {"no_matches", "empty_index"}
        assert response.evidence == ()

    rows = await _service().list_sources()
    assert [r.path for r in rows] == ["ok.md"]


async def test_pdf_ingest_cites_page_number(kb_env: Path) -> None:
    shutil.copy("tests/fixtures/minimal.pdf", kb_env / "minimal.pdf")
    report = await _service().index_sources()
    assert report.ingested == 1

    response = await _service().search("Hello NEXUS job queue")
    assert response.state == "ok"
    ev = response.evidence[0]
    assert ev.path == "minimal.pdf"
    assert ev.page == 1
    assert ev.citation.startswith("minimal.pdf@")


async def test_missing_model_directory_is_typed_and_status_degrades(
    kb_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (kb_env / "doc.md").write_text("Content that cannot be embedded right now.")
    monkeypatch.setenv("NEXUS_KNOWLEDGE_EMBEDDING_PROVIDER", "sentence-transformers")
    monkeypatch.setenv(
        "NEXUS_KNOWLEDGE_EMBEDDING_MODEL_PATH",
        str(Path.cwd() / "definitely-not-a-model"),
    )
    settings_module.get_settings.cache_clear()

    status = await _service().status()
    assert status.degraded_reason == "embedding_model_directory_missing"

    # Per-file failures are reported in the run (one broken provider must not
    # crash the whole command), but the error is typed and visible.
    report = await _service().index_sources()
    assert report.failed == 1
    assert any("embedding" in line or "sentence" in line for line in report.errors)

    # Search fails closed with the typed provider error (service probes the
    # provider before touching the store).
    with pytest.raises(IndexUnavailableError) as search_error:
        await _service().search("anything")
    assert "sentence-transformers" in search_error.value.message


async def test_no_network_and_no_llm_stack_during_operations(
    kb_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (kb_env / "doc.md").write_text("Offline content for the network-ban test.")

    def _blocked(*_args, **_kwargs):
        raise AssertionError("network access attempted during knowledge operation")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)

    service = _service()
    assert (await service.index_sources()).ingested == 1
    assert (await service.search("offline content")).state == "ok"
    assert (await service.rebuild()).rebuilt is True
    await service.status()

    # No LLM integration is installed or imported: retrieval never calls a
    # model API, and OpenTelemetry auto-instrumentation is forced off.
    import importlib.util

    def _spec(name: str):
        try:
            return importlib.util.find_spec(name)
        except (ImportError, ModuleNotFoundError):
            return None

    assert _spec("llama_index.llms.openai") is None
    assert "llama_index.llms.openai" not in sys.modules
    assert os.environ.get("OTEL_SDK_DISABLED") == "true"


async def test_index_recreates_a_deleted_store_without_rebuild(kb_env: Path) -> None:
    """Deleting the derived directory must heal with a plain `index` run.

    Review finding: registry "unchanged" verdicts used to skip every file
    after the store was deleted, so `index` left an empty store while the
    docs promise it recreates one (search must return `ok` again).
    """
    import shutil

    (kb_env / "doc.md").write_text("Recreatable store content about broker health.")
    service = _service()
    assert (await service.index_sources()).ingested == 1
    assert (await service.search("broker health")).state == QueryState.OK

    index_dir = Path(str(settings_module.get_settings().knowledge_index_path))
    shutil.rmtree(index_dir)
    assert not index_dir.exists()

    report = await _service().index_sources()
    assert report.unchanged == 0, "nothing may be 'unchanged' when the store is gone"
    assert report.ingested == 1
    healed = await _service().search("broker health")
    assert healed.state == QueryState.OK
    assert healed.evidence[0].path == "doc.md"


async def test_provider_failure_is_not_store_corruption(kb_env: Path, monkeypatch) -> None:
    """An existing store + a broken embedder must degrade as a provider issue.

    Review finding: `_inspect_sync` used to mark embedding failures
    `corrupt=True`, sending operators to `rebuild --yes` — which would wipe a
    valid index and then fail every file on the same provider error.
    """
    (kb_env / "doc.md").write_text("Stored content that outlives a provider swap.")
    assert (await _service().index_sources()).ingested == 1  # store exists (hash)

    # Swap to a provider whose model directory does not exist.
    monkeypatch.setenv("NEXUS_KNOWLEDGE_EMBEDDING_PROVIDER", "sentence-transformers")
    monkeypatch.setenv(
        "NEXUS_KNOWLEDGE_EMBEDDING_MODEL_PATH",
        str(Path.cwd() / "definitely-not-a-model"),
    )
    settings_module.get_settings.cache_clear()

    status = await _service().status()
    assert status.degraded_reason == "embedding_model_directory_missing"
    assert status.store_corrupt is False, "a provider failure must not read as corruption"
    assert status.store_present is True

    with pytest.raises(IndexUnavailableError) as caught:
        await _service().search("stored content")
    assert caught.value.code == "embedding"


def test_sentence_transformer_identity_distinguishes_same_basename_paths(
    tmp_path: Path,
) -> None:
    from nexus_ai_agent.adapters.llamaindex_retrieval import _sentence_transformer_model_identity

    first = tmp_path / "primary" / "minilm"
    second = tmp_path / "archive" / "minilm"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    first_identity = _sentence_transformer_model_identity(str(first))
    second_identity = _sentence_transformer_model_identity(str(second))

    assert first_identity != second_identity
    assert first_identity.startswith("sentence-transformers:minilm:")
    assert str(tmp_path) not in first_identity
    assert _sentence_transformer_model_identity(str(first)) == first_identity


async def test_manifest_write_failure_is_typed_and_rebuild_recovers(
    kb_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (kb_env / "doc.md").write_text("Manifest persistence must be part of index success.")
    index_dir = Path(str(settings_module.get_settings().knowledge_index_path))
    manifest_path = index_dir / "manifest.json"
    original_write_text = Path.write_text

    def fail_manifest_write(self: Path, data: str, **kwargs) -> int:
        if self == manifest_path:
            raise OSError("simulated manifest write failure")
        return original_write_text(self, data, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_manifest_write)
    service = _service()
    report = await service.index_sources()
    assert report.ingested == 0 and report.failed == 1
    assert report.errors == ("doc.md: embedding_model_manifest_write_failed",)
    assert await service.list_sources() == []

    monkeypatch.setattr(Path, "write_text", original_write_text)
    recovered = _service()
    status = await recovered.status()
    assert status.degraded_reason == "embedding_model_changed_rebuild_required"
    rebuilt = await recovered.rebuild()
    assert rebuilt.rebuilt and rebuilt.ingested == 1
    assert (await recovered.search("manifest persistence")).state == QueryState.OK


@pytest.mark.parametrize(
    "legacy_model",
    ["sentence-transformers:legacy-basename", None],
    ids=("old-identity-format", "missing-identity"),
)
async def test_embedding_model_change_requires_explicit_rebuild(
    kb_env: Path, legacy_model: str | None
) -> None:
    """A store built in another embedding space is never served or fed.

    Review finding: provider/model identity was not recorded, so swapping
    embedders silently scored old-space vectors with the new model (garbage
    retrieval) and incremental indexing would have mixed spaces. The manifest
    now records the embedding identity; inspection refuses on mismatch and
    both `search` and `index_sources` demand an explicit rebuild.
    """
    import json

    (kb_env / "doc.md").write_text("Embedding space content about tundra health.")
    service = _service()
    assert (await service.index_sources()).ingested == 1
    assert (await service.search("tundra health")).state == QueryState.OK

    index_dir = Path(str(settings_module.get_settings().knowledge_index_path))
    manifest_path = index_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.get("embedding_model") == "nexus-hash-v1-256dim"

    # Simulate a provider/model swap: the store keeps the old identity while
    # configuration now implies another one.
    if legacy_model is None:
        manifest.pop("embedding_model", None)
    else:
        manifest["embedding_model"] = legacy_model
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    status = await _service().status()
    assert status.degraded_reason == "embedding_model_changed_rebuild_required"
    assert status.store_present is True and status.store_corrupt is False

    with pytest.raises(IndexUnavailableError) as search_error:
        await _service().search("tundra health")
    assert search_error.value.code == "rebuild_required"

    with pytest.raises(IndexUnavailableError) as index_error:
        await _service().index_sources()  # no silent partial re-ingest
    assert index_error.value.code == "rebuild_required"

    rebuilt = await _service().rebuild()
    assert rebuilt.rebuilt is True
    healed = await _service().search("tundra health")
    assert healed.state == QueryState.OK
    manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_after.get("embedding_model") == "nexus-hash-v1-256dim"
    assert (await _service().status()).degraded_reason is None
