"""Regressions for the silent-failure audit (board task-234).

Two defects were found by an AST sweep of every exception handler in ``src/``:

1. ``PhiAgent.moderate`` reported ``{"safe": True}`` when the model answer could
   not be parsed — a moderation gate that **fails open** on exactly the input it
   cannot judge.
2. The storage layer returned ``[]`` when a provider could not answer, so
   "every cloud is unreachable" was indistinguishable from "the bucket is empty",
   and an unmeasurable quota was reported as a confident ``0 / 2 GiB``.

Every test in this module fails against the pre-fix code.
"""

from __future__ import annotations

import json

import pytest

from nexus_ai_agent.agents.phi_agent import PhiAgent, moderation_allows
from nexus_ai_agent.llm.provider import LLMProvider
from nexus_ai_agent.storage import AIStorageManager, ProviderConfig
from nexus_ai_agent.storage.providers.base import ProviderUnavailable, StorageError
from nexus_ai_agent.storage.unified_cloud import (
    UnifiedCloudStorage,
    _DropboxProvider,
    _InternxtProvider,
    _PcloudProvider,
)


class _ScriptedLLM(LLMProvider):
    """An LLM that always answers with one fixed string — models a broken model."""

    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def generate(self, prompt: str, system: str = "") -> str:
        _ = prompt, system
        return self._reply

    async def embed(self, text: str) -> list[float]:
        return [0.0] * 4


class _FakeProvider:
    """A storage provider whose behaviour the test decides: keys, or an outage."""

    def __init__(self, name: str = "fake", keys: tuple[str, ...] = (), fail: bool = False) -> None:
        self.name = name
        self._keys = list(keys)
        self._fail = fail

    def is_configured(self) -> bool:
        return True

    async def list_files(self, *, prefix: str = "") -> list[str]:
        if self._fail:
            raise StorageError(f"{self.name} is unreachable")
        return list(self._keys)


# ───────────────────────────── moderation gate ──────────────────────────────


@pytest.mark.parametrize(
    "verdict",
    [
        {},
        {"safe": None},
        {"safe": "true"},
        {"safe": 1},
        {"reason": "ok"},
        None,
        "safe",
        0,
        [],
    ],
)
def test_moderation_blocks_everything_that_is_not_a_positive_assertion(verdict: object) -> None:
    """Silence is not consent: only an explicit ``{"safe": true}`` lets content through."""
    assert moderation_allows(verdict) is False


def test_moderation_allows_an_explicit_true() -> None:
    assert moderation_allows({"safe": True, "reason": "ok"}) is True


@pytest.mark.asyncio
async def test_moderate_fails_closed_on_an_unparseable_answer() -> None:
    result = await PhiAgent(_ScriptedLLM("this is not json")).moderate("hello")
    assert result["safe"] is False
    assert result["reason"] == "verdict_unparseable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    ['{"reason": "ok"}', '{"safe": "true"}', '{"safe": null}', "[]", '"safe"', "42"],
)
async def test_moderate_fails_closed_on_a_malformed_verdict(answer: str) -> None:
    result = await PhiAgent(_ScriptedLLM(answer)).moderate("hello")
    assert moderation_allows(result) is False


@pytest.mark.asyncio
async def test_moderate_still_passes_a_clean_verdict() -> None:
    """The gate must not degenerate into 'block everything'."""
    agent = PhiAgent(_ScriptedLLM(json.dumps({"safe": True, "reason": "ok"})))
    result = await agent.moderate("hello")
    assert result["safe"] is True


@pytest.mark.asyncio
async def test_moderate_preserves_an_explicit_refusal() -> None:
    agent = PhiAgent(_ScriptedLLM(json.dumps({"safe": False, "reason": "violence"})))
    result = await agent.moderate("hello")
    assert result["safe"] is False
    assert result["reason"] == "violence"


# ─────────────────────────── storage: honest listings ───────────────────────


@pytest.mark.asyncio
async def test_manager_reports_a_total_outage_instead_of_an_empty_bucket(tmp_path) -> None:
    manager = AIStorageManager(cache_dir=tmp_path, config=ProviderConfig())
    manager.cache = _FakeProvider(name="cache", fail=True)
    manager._download_candidates = lambda: []

    with pytest.raises(StorageError, match="every storage provider failed"):
        await manager.list_files()


@pytest.mark.asyncio
async def test_manager_returns_partial_results_when_one_provider_is_down(tmp_path) -> None:
    manager = AIStorageManager(cache_dir=tmp_path, config=ProviderConfig())
    manager.cache = _FakeProvider(name="cache", keys=["a.txt"])
    manager._download_candidates = lambda: [_FakeProvider(name="remote", fail=True)]

    assert await manager.list_files() == ["a.txt"]


@pytest.mark.asyncio
async def test_unified_cloud_reports_a_total_outage_instead_of_an_empty_drive() -> None:
    cloud = UnifiedCloudStorage()
    cloud._providers = [_FakeProvider(name="dropbox", fail=True)]

    with pytest.raises(StorageError, match="every configured cloud provider failed"):
        await cloud.list_all_files()


@pytest.mark.asyncio
async def test_unified_cloud_keeps_listing_when_one_provider_is_down() -> None:
    cloud = UnifiedCloudStorage()
    cloud._providers = [
        _FakeProvider(name="dropbox", fail=True),
        _FakeProvider(name="pcloud", keys=["x.bin"]),
    ]

    files = await cloud.list_all_files()
    assert [f["name"] for f in files] == ["x.bin"]


@pytest.mark.asyncio
async def test_an_unconfigured_provider_admits_that_it_cannot_list() -> None:
    """An empty answer from a provider that was never asked is still a lie."""
    with pytest.raises(ProviderUnavailable):
        await _DropboxProvider(token=None).list_files()


@pytest.mark.asyncio
async def test_a_provider_without_a_listing_endpoint_says_so() -> None:
    with pytest.raises(ProviderUnavailable):
        await _InternxtProvider(token="token").list_files()


# ─────────────────────────── storage: honest quota ──────────────────────────


@pytest.mark.asyncio
async def test_usage_that_cannot_be_measured_is_unknown_not_zero() -> None:
    assert await _DropboxProvider(token=None).get_usage() is None
    assert await _PcloudProvider(token="token").get_usage() is None
    assert await _InternxtProvider(token="token").get_usage() is None
