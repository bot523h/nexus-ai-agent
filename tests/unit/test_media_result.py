"""The media boundary rejects ambiguous success before touching the filesystem."""

from pathlib import Path

import pytest

from nexus_ai_agent.features.media_result import MediaContractError, media_path


def test_canonical_path_and_metadata_are_preserved() -> None:
    result = {"success": True, "path": "cache/a.png", "error": None, "prompt": "cat"}
    assert media_path(result) == Path("cache/a.png")
    assert result["prompt"] == "cat"


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"success": True, "file_path": "old.png", "error": None},
        {"success": False, "path": "a.png", "error": None},
        {"success": "true", "path": "a.png", "error": None},
        {"success": 1, "path": "a.png", "error": None},
        {"success": True, "path": "a.png", "error": "provider failed"},
        {"success": True, "path": "a.png"},
        {"success": True, "path": None, "error": None},
        {"success": True, "path": "", "error": None},
        {"success": True, "path": "  ", "error": None},
        {"success": True, "path": "a\x00.png", "error": None},
        {"success": True, "path": 123, "error": None},
    ],
)
def test_malformed_or_failed_result_is_rejected(result: dict) -> None:
    with pytest.raises(MediaContractError):
        media_path(result)
