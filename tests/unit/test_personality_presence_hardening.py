from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from nexus_ai_agent.personality.engine import (
    EmotionalState,
    PersonalityEngine,
    StateLoadError,
    StatePersistenceError,
)
from nexus_ai_agent.presence import PresenceClockError, PresenceStore


def test_emotional_snapshot_is_not_an_alias() -> None:
    engine = PersonalityEngine()
    snapshot = engine.snapshot()
    snapshot.valence = -1.0
    assert engine.es.valence == EmotionalState().valence


def test_personality_updates_are_thread_safe_and_state_remains_bounded() -> None:
    engine = PersonalityEngine()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(100):
                engine.update("good! helpful")
                snapshot = engine.snapshot()
                assert -1.0 <= snapshot.valence <= 1.0
                assert 0.0 <= snapshot.arousal <= 1.0
                assert 0.0 <= snapshot.trust <= 1.0
                assert 0.0 <= snapshot.engagement <= 1.0
        except BaseException as exc:  # pragma: no cover - only reports a thread failure
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


def test_concurrent_engines_never_publish_partial_json(tmp_path: Path) -> None:
    path = tmp_path / "shared.json"
    engines = [PersonalityEngine("gemma", str(path)) for _ in range(8)]
    errors: list[BaseException] = []

    def worker(engine: PersonalityEngine) -> None:
        try:
            for _ in range(25):
                engine.update("good")
        except BaseException as exc:  # pragma: no cover - only reports a thread failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(engine,)) for engine in engines]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    restored = PersonalityEngine("gemma", str(path))
    assert -1.0 <= restored.es.valence <= 1.0


def test_symlink_state_paths_are_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "state.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")

    with pytest.raises(StateLoadError):
        PersonalityEngine(state_path=str(link))
    assert target.read_text(encoding="utf-8") == "{}"


def test_read_permission_or_decode_failures_are_visible(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"\xff")
    with pytest.raises(StateLoadError):
        PersonalityEngine(state_path=str(path))


def test_fsync_failure_is_not_swallowed_and_old_state_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    engine = PersonalityEngine(state_path=str(path))
    engine.update("good")
    before = path.read_bytes()

    original_fsync = os.fsync
    calls = 0

    def fail_first_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated power loss")
        original_fsync(fd)

    monkeypatch.setattr("nexus_ai_agent.personality.engine.os.fsync", fail_first_fsync)
    with pytest.raises(StatePersistenceError):
        engine.update("bad")
    assert path.read_bytes() == before
    assert engine.snapshot().valence > 0.4


def test_invalid_clock_value_is_observable() -> None:
    for value in (float("nan"), float("inf"), "not-a-number"):
        store = PresenceStore(clock=lambda value=value: value)
        with pytest.raises(PresenceClockError):
            store.is_online(1)
