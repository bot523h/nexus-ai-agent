"""Audit A-13 — the strict-typing closeout changed three *runtime* boundaries.

Switching ``mypy --strict`` on for ``src/`` was mostly annotations, but three
``no-any-return`` sites were closed with a real check instead of a ``cast``:

* ``storage.providers.github_releases`` — GitHub JSON that is not the expected
  shape is now a typed ``StorageError`` instead of a late ``AttributeError``;
* ``features.calculator._check_result_magnitude`` — returns the number it
  checked and refuses anything that is not an ``int``/``float``;
* ``adapters.in_process_job_queue._row_or_none`` — a queue connection without
  the ``sqlite3.Row`` factory is a programming error, not a silently wrong row.

Each is pinned here so the fail-closed behaviour cannot regress into a cast.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx
import pytest

from nexus_ai_agent.adapters.in_process_job_queue import _row_or_none
from nexus_ai_agent.features.calculator import CalculatorError, _check_result_magnitude
from nexus_ai_agent.storage.providers.base import StorageError
from nexus_ai_agent.storage.providers.github_releases import GitHubReleasesProvider


def _client(payload: Any, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_release_lookup_refuses_a_non_object_payload() -> None:
    provider = GitHubReleasesProvider(token="t", repo="o/r")
    async with _client(["not", "an", "object"]) as client:
        with pytest.raises(StorageError, match="GitHub release payload is not a JSON object"):
            await provider._get_or_create_release(client)


async def test_release_lookup_returns_the_object_payload() -> None:
    provider = GitHubReleasesProvider(token="t", repo="o/r")
    async with _client({"id": 7, "assets_url": "https://x/assets"}) as client:
        assert await provider._get_or_create_release(client) == {
            "id": 7,
            "assets_url": "https://x/assets",
        }


async def test_asset_listing_refuses_malformed_shapes() -> None:
    provider = GitHubReleasesProvider(token="t", repo="o/r")
    release = {"assets_url": "https://api.github.com/repos/o/r/releases/1/assets"}
    async with _client({"not": "a list"}) as client:
        with pytest.raises(StorageError, match="not a JSON array"):
            await provider._find_asset(client, release=release, asset_name="a.bin")
    async with _client(["not an object"]) as client:
        with pytest.raises(StorageError, match="not a JSON object"):
            await provider._find_asset(client, release=release, asset_name="a.bin")


async def test_asset_listing_finds_by_name_or_returns_none() -> None:
    provider = GitHubReleasesProvider(token="t", repo="o/r")
    release = {"assets_url": "https://api.github.com/repos/o/r/releases/1/assets"}
    assets = [{"name": "other.bin", "id": 1}, {"name": "a.bin", "id": 2}]
    async with _client(assets) as client:
        assert await provider._find_asset(client, release=release, asset_name="a.bin") == {
            "name": "a.bin",
            "id": 2,
        }
        assert await provider._find_asset(client, release=release, asset_name="zzz") is None


@pytest.mark.parametrize("value", [0, 7, -3, 2.5, 1e300])
def test_check_result_magnitude_returns_plain_numbers(value: int | float) -> None:
    assert _check_result_magnitude(value) == value


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (True, "boolean results are not allowed"),
        (1 + 2j, "complex results are not supported"),
        ("12", "non-numeric result"),
        (None, "non-numeric result"),
    ],
)
def test_check_result_magnitude_refuses_non_numbers(value: object, message: str) -> None:
    with pytest.raises(CalculatorError, match=message):
        _check_result_magnitude(value)


def test_row_or_none_narrows_or_refuses() -> None:
    assert _row_or_none(None) is None
    connection = sqlite3.connect(":memory:")
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT 1 AS one").fetchone()
        assert _row_or_none(row) is row
        with pytest.raises(TypeError, match="sqlite3.Row factory"):
            _row_or_none((1,))  # a tuple row from a connection without the factory
    finally:
        connection.close()
