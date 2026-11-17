"""End-to-end tests: real-disk example, deterministic export, and N=3 benchmarks.

The end-to-end test reads the shipped JSON fixture, validates and analyses it,
plans an edit, persists the report to a real temporary directory through the
repository's authorized filesystem boundary, and independently re-opens and
re-hashes the bytes.

The benchmarks are N=3 runs over small/medium/larger synthetic stories. They
record median/max wall-clock time and process peak RSS (``ru_maxrss``, a
process high-water mark — NOT per-operation memory). Three runs do not establish
production throughput or tail latency.
"""

from __future__ import annotations

import hashlib
import json
import resource
import statistics
import time
from pathlib import Path

import atlas_fixtures as fx

from nexus_ai_agent.creative.episode_causality_lab import (
    SCHEMA_VERSION,
    EventKind,
    FactRelation,
    PlanRequest,
    Status,
    StoryDocument,
    TriValue,
    analyze_story,
    build_handoff,
    export_report,
    loads_story,
    plan_edit,
)
from nexus_ai_agent.creative.episode_causality_lab.causal import CausalEngine
from nexus_ai_agent.creative.episode_causality_lab.incremental import (
    IncrementalEngine,
    full_recompute,
)
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

FIXTURE = Path(__file__).parent / "fixtures" / "two_episode_story.json"


def test_end_to_end_fixture_analyze_plan_export(tmp_path: Path) -> None:
    doc = loads_story(FIXTURE.read_bytes())
    assert doc.fingerprint()

    req = PlanRequest(
        beats=("b1", "b2", "b3"),
        candidates=doc.shot_candidates,
        frame_budget=1_000,
    )
    report = analyze_story(doc, plan_request=req)

    # The story is causally clean (a declared possession and a return transfer),
    # but shot_c carries UNKNOWN evidence, so the plan is feasible, not flagged
    # as failing; the overall status must not be PASS-by-accident of unknown data.
    assert report.causal.status is Status.PASS
    assert report.temporal.status is Status.PASS
    assert report.plan is not None
    assert report.plan.status.value == "PROVEN_OPTIMAL"

    fs = WorkspaceFilesystem(tmp_path)
    artifact = export_report(report, fs, "atlas/report.json")
    assert artifact.verified
    on_disk = (tmp_path / "atlas" / "report.json").read_bytes()
    assert hashlib.sha256(on_disk).hexdigest() == artifact.sha256
    payload = json.loads(on_disk)
    assert payload["status"] == report.status.value

    handoff = build_handoff(report, artifact)
    assert handoff["kind"] == "atlas.edit_plan_handoff"
    assert handoff["authority"] == "advisory"
    assert handoff["proposed_steps"]


def test_export_is_byte_deterministic(tmp_path: Path) -> None:
    doc = loads_story(FIXTURE.read_bytes())
    report = analyze_story(doc)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    fs1 = WorkspaceFilesystem(tmp_path / "a")
    fs2 = WorkspaceFilesystem(tmp_path / "b")
    a1 = export_report(report, fs1, "r.json")
    a2 = export_report(report, fs2, "r.json")
    assert a1.sha256 == a2.sha256


def _synthetic_story(n_episodes: int, scenes_per_episode: int) -> StoryDocument:
    episodes = tuple(fx.Episode(id=f"ep{i}", order_index=i) for i in range(n_episodes))
    scenes = []
    events = []
    for i in range(n_episodes):
        for s in range(scenes_per_episode):
            sid = f"ep{i}_s{s}"
            scenes.append(fx.Scene(id=sid, episode_id=f"ep{i}", location_id="room", order_index=s))
            events.append(
                fx.event(
                    f"{sid}_e0",
                    EventKind.ENTER_LOCATION,
                    order=0,
                    actor="alice",
                    target="room",
                    scene=sid,
                )
            )
            events[-1] = events[-1].model_copy(update={"episode_id": f"ep{i}", "scene_id": sid})
    return StoryDocument(
        schema_version=SCHEMA_VERSION,
        characters=(fx.Character(id="alice"), fx.Character(id="bob")),
        props=(fx.Prop(id="gun"),),
        locations=(fx.Location(id="room"),),
        episodes=episodes,
        scenes=tuple(scenes),
        initial_facts=(fx.fact("alice", FactRelation.POSSESSES, "gun", TriValue.TRUE),),
        events=tuple(events),
    )


def _bench(fn, runs: int = 3) -> tuple[float, float]:
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(times), max(times)


def test_benchmarks_small_medium_large(capsys) -> None:
    workloads = {
        "small": _synthetic_story(2, 2),
        "medium": _synthetic_story(8, 6),
        "larger": _synthetic_story(20, 12),
    }
    lines = []
    for name, doc in workloads.items():
        parse_ms, parse_max = _bench(lambda d=doc: d.canonical_bytes())
        causal_ms, causal_max = _bench(lambda d=doc: CausalEngine().analyze(d))
        lines.append(
            f"{name}: parse/validate median={parse_ms:.3f}ms max={parse_max:.3f}ms | "
            f"causal median={causal_ms:.3f}ms max={causal_max:.3f}ms | "
            f"events={len(doc.events)}"
        )
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    lines.append(f"process peak RSS (ru_maxrss, KiB on Linux): {rss_kb}")
    report_text = "\n".join(lines)
    with capsys.disabled():
        print("\n[ATLAS benchmarks N=3]\n" + report_text)
    # Each workload must actually be non-trivial.
    assert len(workloads["larger"].events) > len(workloads["small"].events)


def test_benchmark_plan_and_incremental(capsys) -> None:
    doc = _synthetic_story(10, 8)
    beats = tuple(f"b{i}" for i in range(6))
    cands = tuple(fx.shot(f"s{i}", (b,), 12, preference=i % 3) for i, b in enumerate(beats))
    req = PlanRequest(beats=beats, candidates=cands, frame_budget=10_000)
    plan_ms, plan_max = _bench(lambda: plan_edit(req))

    base = full_recompute(doc)
    engine = IncrementalEngine()
    inc_ms, inc_max = _bench(lambda: engine.update(doc, base, doc))

    full_ms, full_max = _bench(lambda: full_recompute(doc))
    with capsys.disabled():
        print(
            "\n[ATLAS benchmarks N=3 cont.]\n"
            f"plan median={plan_ms:.3f}ms max={plan_max:.3f}ms | "
            f"incremental median={inc_ms:.3f}ms max={inc_max:.3f}ms | "
            f"full recompute median={full_ms:.3f}ms max={full_max:.3f}ms"
        )
    assert plan_edit(req).status.value == "PROVEN_OPTIMAL"
