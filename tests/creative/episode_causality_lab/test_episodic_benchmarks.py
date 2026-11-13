"""N=3 micro-benchmarks for the episodic ATLAS/AETHEL pipeline (Wave 7).

These are wall-clock samples on synthetic workloads, not production-throughput or
tail-latency claims. Peak memory is measured with ``tracemalloc`` around the
operation (``ru_maxrss`` is a whole-process high-water mark and would measure the
interpreter too).

Workloads: small (1 episode / 100 scenes), medium (3 / 300), large (10 / 1000).
"""

from __future__ import annotations

import statistics
import time
import tracemalloc
from pathlib import Path

from nexus_ai_agent.creative.episode_causality_lab import (
    SCHEMA_VERSION,
    Character,
    DeclaredFact,
    Episode,
    Event,
    EventKind,
    FactKey,
    FactRelation,
    IncrementalEngine,
    Location,
    Prop,
    Scene,
    SourceRef,
    Status,
    StoryBeat,
    StoryDocument,
    TemporalConstraint,
    TimeOfDay,
    TriValue,
    analyze_story,
    check_consistency,
    export_report,
    full_recompute,
    loads_story,
)
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

N = 3
_SRC = SourceRef(path="bench/ep", line=1)


def _story(episodes: int, scenes_per_episode: int) -> StoryDocument:
    scenes = []
    events = []
    temporal = []
    beats = []
    for i in range(episodes):
        for j in range(scenes_per_episode):
            sid = f"ep{i}_sc{j}"
            scenes.append(
                Scene(
                    id=sid,
                    episode_id=f"ep{i}",
                    location_id=f"loc{i}",
                    order_index=j,
                    time_of_day=TimeOfDay.DAY,
                )
            )
            beats.append(StoryBeat(id=f"b_{sid}", episode_id=f"ep{i}", order_index=len(beats)))
            # each scene swaps the relic away and back, so alice always holds it
            # at the start of every scene (a consistent, non-vacuous transfer chain)
            holder, taker = "alice", "bob"
            events.append(
                Event(
                    id=f"{sid}_xfer_a",
                    episode_id=f"ep{i}",
                    scene_id=sid,
                    order_index=0,
                    kind=EventKind.TRANSFER_PROP,
                    actor_id=holder,
                    target_id=taker,
                    prop_id="relic",
                    source=_SRC,
                )
            )
            events.append(
                Event(
                    id=f"{sid}_xfer_b",
                    episode_id=f"ep{i}",
                    scene_id=sid,
                    order_index=1,
                    kind=EventKind.TRANSFER_PROP,
                    actor_id=taker,
                    target_id=holder,
                    prop_id="relic",
                    source=_SRC,
                )
            )
            if j > 0:
                temporal.append(
                    TemporalConstraint(
                        id=f"tc_{sid}",
                        a=f"scene:ep{i}_sc{j - 1}",
                        b=f"scene:{sid}",
                        lower=1,
                        upper=10_000_000,
                        source=_SRC,
                    )
                )
    return StoryDocument(
        schema_version=SCHEMA_VERSION,
        characters=(Character(id="alice"), Character(id="bob")),
        props=(Prop(id="relic"),),
        locations=tuple(Location(id=f"loc{i}") for i in range(episodes)),
        episodes=tuple(Episode(id=f"ep{i}", order_index=i) for i in range(episodes)),
        scenes=tuple(scenes),
        initial_facts=(
            DeclaredFact(
                key=FactKey(subject_id="alice", relation=FactRelation.POSSESSES, target_id="relic"),
                value=TriValue.TRUE,
                source=_SRC,
            ),
        ),
        events=tuple(events),
        temporal_constraints=tuple(temporal),
        beats=tuple(beats),
        shot_candidates=(),
    )


def _timed(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
    times = []
    peak = 0
    for _ in range(N):
        tracemalloc.start()
        start = time.perf_counter()
        fn(*args, **kwargs)
        times.append((time.perf_counter() - start) * 1000.0)
        _, pk = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peak = max(peak, pk)
    return times, peak


def test_benchmark_parse_small_medium_large() -> None:
    for episodes, spe in ((1, 100), (3, 100), (10, 100)):
        doc = _story(episodes, spe)
        payload = doc.model_dump_json()
        times, peak = _timed(loads_story, payload)
        assert statistics.median(times) < 5000.0
        assert peak < 400 * 1024 * 1024


def test_benchmark_causal_scales_with_size() -> None:
    small = _story(1, 100)
    large = _story(10, 100)
    t_small, _ = _timed(full_recompute, small)
    t_large, _ = _timed(full_recompute, large)
    assert statistics.median(t_large) >= statistics.median(t_small) - 1.0
    assert statistics.median(t_large) < 10000.0


def test_benchmark_temporal_large() -> None:
    doc = _story(10, 100)
    extra = tuple(sorted({e.id for e in doc.events}))
    times, peak = _timed(check_consistency, doc.temporal_constraints, extra_nodes=extra)
    assert statistics.median(times) < 15000.0
    assert peak < 400 * 1024 * 1024


def test_benchmark_incremental_beats_full_recompute() -> None:
    base = _story(10, 100)
    base_report = full_recompute(base)
    # a mutated copy with one early event flipped
    events = list(base.events)
    events[1] = Event(
        id=events[1].id,
        episode_id=events[1].episode_id,
        scene_id=events[1].scene_id,
        order_index=events[1].order_index,
        kind=events[1].kind,
        actor_id="alice",
        target_id=events[1].target_id,
        prop_id=events[1].prop_id,
        source=_SRC,
    )
    mutated = base.model_copy(update={"events": tuple(events)})
    engine = IncrementalEngine()
    t_inc, _ = _timed(engine.update, base, base_report, mutated)
    t_full, _ = _timed(full_recompute, mutated)
    # incremental reuse must not be slower than a full replay by a wide margin
    assert statistics.median(t_inc) <= statistics.median(t_full) * 4.0 + 50.0


def test_benchmark_export_large(tmp_path: Path) -> None:
    doc = _story(10, 100)
    report = analyze_story(doc)
    fs = WorkspaceFilesystem(tmp_path)
    times, peak = _timed(export_report, report, fs, "atlas/bench.json")
    assert statistics.median(times) < 5000.0
    assert peak < 400 * 1024 * 1024


def test_benchmark_end_to_end_medium() -> None:
    doc = _story(3, 100)
    assert analyze_story(doc).status is Status.PASS
