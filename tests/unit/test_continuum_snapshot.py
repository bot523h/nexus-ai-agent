from __future__ import annotations
from nexus_ai_agent.continuum.snapshot import ContinuumSnapshot, EnvFingerprint, read_snapshot, write_snapshot

def sample():
    return ContinuumSnapshot(2, "C2", "recorded-head", "next", [{"id":"D8","status":"reverted"}], 22, EnvFingerprint("3.12.0","1.20.0","2.0.54"))
def test_round_trip(tmp_path, monkeypatch):
    import nexus_ai_agent.continuum.snapshot as mod
    monkeypatch.setattr(mod, "SNAPSHOT_PATH", tmp_path / "continuum.json")
    write_snapshot(sample()); assert read_snapshot().plan == "C2"
def test_schema_uses_plan_step_next_ledger():
    assert set(sample().__dict__) == {"schema_version","plan","step","next","ledger","test_count_expected","env_fingerprint"}
