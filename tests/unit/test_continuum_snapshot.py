"""Unit tests for the continuum snapshot module."""

from __future__ import annotations

import json

from nexus_ai_agent.continuum.snapshot import (
    ContinuumSnapshot,
    EnvFingerprint,
    read_snapshot,
    verify_snapshot,
    write_snapshot,
)


def _sample() -> ContinuumSnapshot:
    return ContinuumSnapshot(
        schema_version=1,
        phase="D",
        phase_status="complete",
        last_good_commit="recorded-head",
        last_good_branch="arena/01a0af6a-nexus-ai-agent",
        test_count_expected=195,
        env_fingerprint=EnvFingerprint(python="3.11", alembic="1.20.0", sqlalchemy="2.0.54"),
        next_phase="E",
    )


def _point_snapshot_at(tmp_path, monkeypatch, snap: ContinuumSnapshot) -> None:
    import nexus_ai_agent.continuum.snapshot as mod

    target = tmp_path / "continuum.json"
    monkeypatch.setattr(mod, "SNAPSHOT_PATH", target)
    write_snapshot(snap)


class TestRoundTrip:
    def test_write_then_read_matches(self, tmp_path, monkeypatch) -> None:
        _point_snapshot_at(tmp_path, monkeypatch, _sample())
        snap = read_snapshot()
        assert snap.phase == "D"
        assert snap.test_count_expected == 195
        assert snap.next_phase == "E"
        assert snap.env_fingerprint.alembic == "1.20.0"

    def test_json_schema_keys_round_trip(self, tmp_path, monkeypatch) -> None:
        import nexus_ai_agent.continuum.snapshot as mod

        target = tmp_path / "continuum.json"
        monkeypatch.setattr(mod, "SNAPSHOT_PATH", target)
        write_snapshot(_sample())
        raw = json.loads(target.read_text(encoding="utf-8"))
        # JSON-emitted keys must match what read_snapshot expects.
        for key in ("schema_version", "phase", "phase_status", "last_good_commit"):
            assert key in raw
        read_snapshot()  # must not raise


class TestVerify:
    def test_healthy_when_recorded_commit_is_reachable(self, tmp_path, monkeypatch) -> None:
        import nexus_ai_agent.continuum.snapshot as mod

        _point_snapshot_at(tmp_path, monkeypatch, _sample())
        monkeypatch.setattr(mod, "current_commit", lambda: "newer-head")
        monkeypatch.setattr(mod, "_is_ancestor", lambda a, d: True)  # recorded reachable
        assert verify_snapshot() == []

    def test_state_loss_reported_when_not_reachable(self, tmp_path, monkeypatch) -> None:
        import nexus_ai_agent.continuum.snapshot as mod

        _point_snapshot_at(tmp_path, monkeypatch, _sample())
        monkeypatch.setattr(mod, "current_commit", lambda: "newer-head")
        monkeypatch.setattr(mod, "_is_ancestor", lambda a, d: False)  # lost
        problems = verify_snapshot()
        assert any("state loss" in p for p in problems)
        assert "recorded-head" in problems[0]
