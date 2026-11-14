"""Edge-path tests for scripts/merge_agent_board.py: CLI, refusals, and no-loss checks."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "merge_agent_board.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("merge_agent_board_hardening", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mab = _load()


def _claim(task: str, **extra: object) -> dict[str, object]:
    return {"task": task, "status": "done", **extra}


def test_cli_writes_the_merged_board_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    board = tmp_path / "a.json"
    board.write_text(json.dumps({"claims": [_claim("t1")]}), encoding="utf-8")
    assert mab.main([str(board)]) == 0
    captured = capsys.readouterr()
    assert "t1" in captured.out
    assert "merged 1 boards" in captured.err


def test_cli_writes_to_the_output_file_when_asked(tmp_path: Path) -> None:
    board = tmp_path / "a.json"
    board.write_text(json.dumps({"claims": [_claim("t1")]}), encoding="utf-8")
    out = tmp_path / "merged.json"
    assert mab.main([str(board), "-o", str(out)]) == 0
    assert "t1" in out.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "content",
    ["{not json", json.dumps([1, 2, 3])],
    ids=["malformed-json", "non-object-board"],
)
def test_cli_refuses_unreadable_or_non_object_boards(tmp_path: Path, content: str) -> None:
    board = tmp_path / "bad.json"
    board.write_text(content, encoding="utf-8")
    assert mab.main([str(board)]) == 1


def test_cli_refuses_a_missing_file(tmp_path: Path) -> None:
    assert mab.main([str(tmp_path / "absent.json")]) == 1


def test_claim_without_a_task_id_is_refused() -> None:
    with pytest.raises(mab.MergeError, match="without a task id"):
        mab.merge_boards([("a", {"claims": [{"status": "done"}]})])


def test_duplicate_task_inside_one_board_is_refused() -> None:
    board = {"claims": [_claim("t1"), _claim("t1")]}
    with pytest.raises(mab.MergeError, match="duplicate task"):
        mab.merge_boards([("a", board)])


def test_merging_nothing_or_a_non_object_is_refused() -> None:
    with pytest.raises(mab.MergeError, match="at least one board"):
        mab.merge_boards([])
    with pytest.raises(mab.MergeError, match="must be a JSON object"):
        mab.merge_boards([("a", ["not", "an", "object"])])  # type: ignore[list-item]


def test_conflicting_scalar_values_are_both_preserved_not_silently_dropped() -> None:
    first = {"updated_at": "2026-01-01", "owner": "alpha"}
    second = {"updated_at": "2026-01-02", "owner": "beta"}
    merged = mab.merge_boards([("a", first), ("b", second)])
    text = json.dumps(merged)
    assert "alpha" in text and "beta" in text


def test_list_fields_are_unioned_and_deduplicated() -> None:
    merged = mab.merge_boards([("a", {"tags": ["x", "y"]}), ("b", {"tags": ["y", "z"]})])
    assert sorted(merged["tags"]) == ["x", "y", "z"]


def test_verify_no_loss_refuses_a_merge_that_dropped_a_task() -> None:
    payloads = [("a", {"claims": [_claim("t1"), _claim("t2")]})]
    merged = {"claims": [_claim("t1")]}
    with pytest.raises(mab.MergeError, match="was lost"):
        mab.verify_no_loss(payloads, merged)


def test_verify_no_loss_refuses_a_merge_that_dropped_a_list_item() -> None:
    payloads = [("a", {"tags": ["x", "y"]})]
    merged = {"tags": ["x"]}
    with pytest.raises(mab.MergeError, match="an item of 'tags' was lost"):
        mab.verify_no_loss(payloads, merged)


def test_verify_no_loss_refuses_a_task_whose_record_changed() -> None:
    payloads = [("a", {"claims": [_claim("t1", owner="alpha")]})]
    merged = {"claims": [_claim("t1", owner="beta")]}
    with pytest.raises(mab.MergeError, match="a record for task 't1' was lost"):
        mab.verify_no_loss(payloads, merged)


def test_verify_no_loss_counts_every_verified_record() -> None:
    payloads = [("a", {"claims": [_claim("t1")]}), ("b", {"claims": [_claim("t2")]})]
    merged = mab.merge_boards(payloads)
    assert mab.verify_no_loss(payloads, merged) == 2
