"""Unit proof: source identity, content hashing, scanning rules, citations.

Pure/fast layer of the knowledge-retrieval subsystem (task-273): no LlamaIndex
index writes, no database — just the deterministic pieces every citation and
every rejection decision is built from.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.knowledge_retrieval.config import (
    DEFAULT_ALLOWED_EXTENSIONS,
    KnowledgeConfig,
    provider_availability,
)
from nexus_ai_agent.knowledge_retrieval.embeddings import EMBEDDING_DIM, hash_embed
from nexus_ai_agent.knowledge_retrieval.errors import (
    IndexUnavailableError,
    KnowledgeConfigError,
    KnowledgeDisabledError,
    KnowledgeRetrievalError,
    KnowledgeStoreError,
    SourceRejectedError,
)
from nexus_ai_agent.knowledge_retrieval.models import (
    Evidence,
    IndexStatus,
    IngestReport,
    QueryState,
    ScanIssue,
    SearchResponse,
    SourceStatus,
)
from nexus_ai_agent.knowledge_retrieval.scanner import (
    EXCLUDED_DIR_NAMES,
    is_secret_filename,
    scan_roots,
    source_id_for,
    validate_member,
)
from nexus_ai_agent.knowledge_retrieval.service import (
    _ContentRejected,
    _read_content,
    build_chunk_payloads,
)


def _config(**overrides: object) -> KnowledgeConfig:
    base: dict[str, object] = {
        "enabled": True,
        "source_roots": (),
        "index_path": Path("/nonexistent-index"),
        "embedding_provider": "hash",
        "embedding_model_path": "",
        "max_file_bytes": 10_000,
        "max_files": 100,
        "chunk_tokens": 64,
        "chunk_overlap_ratio": 0.1,
        "search_top_k": 5,
        "allowed_extensions": DEFAULT_ALLOWED_EXTENSIONS,
    }
    base.update(overrides)
    return KnowledgeConfig(**base)  # type: ignore[arg-type]


# ── source identity ────────────────────────────────────────────────────────


def test_source_id_is_stable_and_content_independent() -> None:
    root = Path("/srv/kb")
    first = source_id_for(root, "docs/a.md")
    second = source_id_for(root, "docs/a.md")
    assert first == second
    assert len(first) == 32
    # Identity is path-based, not content-based: edits keep the id.
    assert first == source_id_for(root, "docs/a.md")


def test_source_id_separates_roots_and_paths() -> None:
    assert source_id_for(Path("/a"), "x.md") != source_id_for(Path("/b"), "x.md")
    assert source_id_for(Path("/a"), "x.md") != source_id_for(Path("/a"), "y.md")


def test_content_hash_matches_sha256(tmp_path: Path) -> None:
    target = tmp_path / "doc.md"
    payload = b"hello knowledge"
    target.write_bytes(payload)
    content = _read_content(target, _config())
    assert content.sha256 == hashlib.sha256(payload).hexdigest()
    assert content.text == "hello knowledge"
    assert content.raw == payload


# ── content rejection taxonomy ─────────────────────────────────────────────


def test_binary_and_decode_and_empty_are_typed_rejections(tmp_path: Path) -> None:
    binary = tmp_path / "b.txt"
    binary.write_bytes(b"abc\x00def")
    with pytest.raises(_ContentRejected) as excinfo:
        _read_content(binary, _config())
    assert excinfo.value.reason == "binary"

    undecodable = tmp_path / "u.txt"
    undecodable.write_bytes(b"\xff\xfe\xc3\x28 not utf-8 at all")
    with pytest.raises(_ContentRejected) as excinfo:
        _read_content(undecodable, _config())
    assert excinfo.value.reason == "decode"

    blank = tmp_path / "blank.txt"
    blank.write_text("   \n\t  ")
    with pytest.raises(_ContentRejected) as excinfo:
        _read_content(blank, _config())
    assert excinfo.value.reason == "empty"


def test_oversize_content_is_rejected(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    big.write_text("x" * 50)
    with pytest.raises(_ContentRejected) as excinfo:
        _read_content(big, _config(max_file_bytes=10))
    assert excinfo.value.reason == "oversize"


def test_missing_file_is_unreadable(tmp_path: Path) -> None:
    with pytest.raises(_ContentRejected) as excinfo:
        _read_content(tmp_path / "gone.md", _config())
    assert excinfo.value.reason == "unreadable"


# ── scanner: allowlist, secrets, symlinks, traversal, determinism ──────────


def test_secret_filenames_are_recognised() -> None:
    for name in (
        ".env",
        ".env.local",
        "prod.env",
        "id_rsa",
        "id_rsa.pub",
        "server.pem",
        "private.key",
        "credentials",
        "credentials.json",
        ".netrc",
        "api.token",
        "secrets.toml",
    ):
        assert is_secret_filename(name), name
    for name in ("guide.md", "env.md", "keyboard.txt", "my.pemnotes.md"):
        assert not is_secret_filename(name), name


def test_scan_skips_excluded_dirs_secrets_and_unsupported(tmp_path: Path) -> None:
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / "a.md").write_text("alpha")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.md").write_text("must never be scanned")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.md").write_text("dep")
    (tmp_path / ".env").write_text("SECRET_TOKEN=hunter2")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG")
    (tmp_path / "notes.docx").write_text("zipped")

    config = _config(source_roots=(tmp_path,))
    outcome = scan_roots(config, (tmp_path,))
    assert [s.rel_path for s in outcome.sources] == ["keep/a.md"]
    reasons = {(issue.path, issue.reason) for issue in outcome.issues}
    assert (".env", "secret") in reasons
    assert ("logo.png", "unsupported") in reasons
    assert ("notes.docx", "unsupported") in reasons
    # excluded directories are never descended into: no issue rows either
    assert not any(".git" in path or "node_modules" in path for path, _ in reasons)


def test_scan_rejects_symlinks_without_following_them(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "escape.md"
    secret.write_text("outside content that must not be indexed")
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.md").write_text("inside")
    (root / "link.md").symlink_to(secret)
    linked_dir = root / "dirlink"
    linked_dir.symlink_to(outside)

    outcome = scan_roots(_config(source_roots=(root,)), (root,))
    assert [s.rel_path for s in outcome.sources] == ["ok.md"]
    assert any(issue.reason == "symlink" for issue in outcome.issues)
    # symlinked directories are pruned, so their contents never appear
    assert not any("escape.md" in issue.path for issue in outcome.issues)


def test_validate_member_rejects_traversal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "elsewhere.md"
    target.write_text("x")
    with pytest.raises(SourceRejectedError):
        validate_member(root, target)
    # in-root member is fine
    inside = root / "doc.md"
    inside.write_text("x")
    validate_member(root, inside)


def test_scan_is_deterministically_sorted(tmp_path: Path) -> None:
    for name in ("c.md", "a.md", "b.md", "nested/z.md"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    config = _config(source_roots=(tmp_path,))
    first = [s.rel_path for s in scan_roots(config, (tmp_path,)).sources]
    second = [s.rel_path for s in scan_roots(config, (tmp_path,)).sources]
    assert first == second == ["a.md", "b.md", "c.md", "nested/z.md"]


def test_excluded_dir_names_cover_git_and_caches() -> None:
    assert {".git", "__pycache__", "node_modules", ".venv"} <= EXCLUDED_DIR_NAMES


def test_oversize_at_scan_time_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "huge.md").write_text("y" * 100)
    outcome = scan_roots(_config(source_roots=(tmp_path,), max_file_bytes=50), (tmp_path,))
    assert outcome.sources == ()
    assert any(issue.reason == "oversize" for issue in outcome.issues)


# ── configuration validation ───────────────────────────────────────────────


def test_settings_parse_roots_from_comma_and_json() -> None:
    settings = Settings(_env_file=None, NEXUS_KNOWLEDGE_SOURCE_ROOTS="a/b, c/d")
    assert settings.knowledge_source_roots == ["a/b", "c/d"]
    settings = Settings(_env_file=None, NEXUS_KNOWLEDGE_SOURCE_ROOTS='["x", "y"]')
    assert settings.knowledge_source_roots == ["x", "y"]
    settings = Settings(_env_file=None, NEXUS_KNOWLEDGE_SOURCE_ROOTS="")
    assert settings.knowledge_source_roots == []


def test_from_settings_rejects_unknown_provider() -> None:
    settings = Settings(_env_file=None, NEXUS_KNOWLEDGE_EMBEDDING_PROVIDER="cloud-api")
    with pytest.raises(KnowledgeConfigError):
        KnowledgeConfig.from_settings(settings)


@pytest.mark.parametrize(
    "field,value",
    [
        ("NEXUS_KNOWLEDGE_MAX_FILE_BYTES", 0),
        ("NEXUS_KNOWLEDGE_MAX_FILES", 0),
        ("NEXUS_KNOWLEDGE_CHUNK_TOKENS", 0),
        ("NEXUS_KNOWLEDGE_CHUNK_OVERLAP", 1.5),
        ("NEXUS_KNOWLEDGE_SEARCH_TOP_K", 0),
    ],
)
def test_settings_reject_bad_limits_at_validation_time(field: str, value: int | float) -> None:
    """Fail-closed happens while settings are parsed (Field constraints),
    before any code can read an out-of-range bound."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_require_roots_fails_closed(tmp_path: Path) -> None:
    unconfigured = _config(source_roots=())
    with pytest.raises(KnowledgeConfigError):
        unconfigured.require_roots()

    disabled = _config(enabled=False, source_roots=(tmp_path,))
    with pytest.raises(KnowledgeDisabledError):
        disabled.require_roots()
    with pytest.raises(KnowledgeDisabledError):
        disabled.require_enabled()

    missing = _config(source_roots=(tmp_path / "gone",))
    with pytest.raises(KnowledgeConfigError):
        missing.require_roots()


def test_provider_availability_probe() -> None:
    assert provider_availability(_config()) == (True, None)
    missing_path = _config(embedding_provider="sentence-transformers", embedding_model_path="")
    ok, reason = provider_availability(missing_path)
    assert not ok and reason == "embedding_model_path_empty"
    not_dir = _config(
        embedding_provider="sentence-transformers",
        embedding_model_path="/definitely/not/a/model/dir",
    )
    ok, reason = provider_availability(not_dir)
    assert not ok and reason == "embedding_model_directory_missing"


# ── chunk metadata & citations ─────────────────────────────────────────────


def test_chunk_payloads_cover_source_with_offsets() -> None:
    text = ("Paragraph one about retrieval systems. " * 8) + ("Paragraph two is different. " * 8)
    config = _config(chunk_tokens=48, chunk_overlap_ratio=0.1)
    payloads = build_chunk_payloads(text, None, config, suffix=".md")
    assert payloads
    assert [p.chunk_index for p in payloads] == list(range(len(payloads)))
    for payload in payloads:
        assert text[payload.start : payload.end] == payload.text
        assert payload.page is None  # text formats never invent page numbers
    # overlap: consecutive windows share characters (offsets move forward)
    assert payloads[1].start >= payloads[0].start


def test_chunk_payloads_map_pdf_pages(tmp_path: Path) -> None:
    # two synthetic pages: page 1 = chars 0..10, page 2 = chars 10..25
    pages = [(0, 10, 1), (10, 25, 2)]
    text = "x" * 25
    config = _config(chunk_tokens=4, chunk_overlap_ratio=0.0)
    payloads = build_chunk_payloads(text, pages, config, suffix=".pdf")
    assert payloads
    assert {p.page for p in payloads} == {1, 2}
    assert payloads[0].page == 1
    assert payloads[-1].page == 2


def test_evidence_citation_is_stable_and_resolvable() -> None:
    evidence = Evidence(
        source_id="abc",
        path="docs/runbook.md",
        root="/srv/kb",
        sha256="deadbeef" * 8,
        chunk_index=3,
        start=10,
        end=50,
        score=0.5,
        excerpt="excerpt",
    )
    assert evidence.citation == "docs/runbook.md@deadbeefdead#3"
    # every provenance field round-trips
    assert (evidence.path, evidence.chunk_index, evidence.start, evidence.end) == (
        "docs/runbook.md",
        3,
        10,
        50,
    )


def test_search_response_states_are_distinct_enums() -> None:
    states = {s.value for s in QueryState}
    assert states == {"ok", "empty_query", "empty_index", "no_matches"}
    empty_query = SearchResponse(query="  ", state=QueryState.EMPTY_QUERY)
    assert empty_query.evidence == ()
    assert empty_query.dropped_stale == 0


def test_report_and_status_serialise_for_json() -> None:
    report = IngestReport(run_id="r1", ingested=2, errors=("a.md: binary",))
    data = report.as_dict()
    assert data["ingested"] == 2 and data["errors"] == ["a.md: binary"]
    status = IndexStatus(
        enabled=True,
        configured=False,
        store_present=False,
        store_corrupt=False,
        node_count=0,
        generation=None,
        source_count=0,
        ingested_count=0,
        error_count=0,
        embedding_provider="hash",
        provider_available=True,
    )
    assert not status.degraded
    assert status.as_dict()["configured"] is False
    assert SourceStatus.INGESTED.value == "ingested"


def test_scan_issue_is_safe_data_only() -> None:
    issue = ScanIssue(path="x.md", reason="symlink")
    assert set(issue.__dataclass_fields__) == {"path", "reason"}


# ── error taxonomy ─────────────────────────────────────────────────────────


def test_error_codes_are_stable() -> None:
    assert KnowledgeRetrievalError("x").code == "knowledge_error"
    assert KnowledgeDisabledError("x").code == "disabled"
    assert KnowledgeConfigError("x").code == "misconfigured"
    assert SourceRejectedError("x").code == "source_rejected"
    assert IndexUnavailableError("x").code == "index_unavailable"
    assert KnowledgeStoreError("x").code == "store_error"
    assert KnowledgeDisabledError("x", hint="h").hint == "h"


# ── deterministic embeddings ───────────────────────────────────────────────


def test_hash_embed_is_deterministic_normalised_and_bounded() -> None:
    first = hash_embed(["Solar panels produce electricity"])
    second = hash_embed(["Solar panels produce electricity"])
    assert first == second
    assert len(first[0]) == EMBEDDING_DIM
    norm = sum(value * value for value in first[0]) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-9)
    different = hash_embed(["quantum chromodynamics lattice"])
    assert different != first
    zero = hash_embed([""])
    assert zero[0] == [0.0] * EMBEDDING_DIM
    with pytest.raises(ValueError):
        hash_embed(["x"], dim=0)


def test_hash_embed_folds_persian_variants_like_the_rag_core() -> None:
    persian_yeh = hash_embed(["کتاب خوب"])
    arabic_yeh = hash_embed(["كتاب خوب"])  # Arabic yeh/kaf forms
    assert persian_yeh == arabic_yeh
