"""Wave 7 — large-scale episodic end-to-end stress (REAL COMPUTE).

Exercises the whole AETHEL pipeline at scale: a synthetic 10-episode /
1000-scene / 2000-event story is built in memory, then run through the causal
engine, the exact-integer temporal network, the incremental engine (compared
against a full-recompute oracle), a real-disk report export that is reopened and
digest-checked, and the composition DAG compiler.

Everything here is REAL COMPUTATION IN MEMORY plus REAL DISK (the export uses a
temporary ``WorkspaceFilesystem``/``AttemptStaging`` under pytest ``tmp_path``).
Nothing is mocked.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nexus_ai_agent.creative.composition import (
    ClipCandidate,
    MontageRequest,
    compile_atlas_plan_to_dag,
    compile_montage_to_timeline_dag,
    optimize_rhythmic_montage,
)
from nexus_ai_agent.creative.director import (
    compile_fountain_to_story_document,
    parse_fountain,
)
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
    PlanRequest,
    Prop,
    Scene,
    ShotCandidate,
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
    plan_edit,
)
from nexus_ai_agent.execution.staging import AttemptStaging
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

EPISODES = 10
SCENES_PER_EPISODE = 100
TOTAL_SCENES = EPISODES * SCENES_PER_EPISODE


def _build_large_doc(*, break_at: int | None = None) -> StoryDocument:
    """A consistent 1000-scene story; optionally break one transfer.

    ``break_at`` injects a conflict by making the transferor at that global scene
    index hand over a prop it is explicitly known not to hold.
    """
    src = SourceRef(path="stress/ep", line=1)
    characters = (Character(id="alice"), Character(id="bob"))
    props = (Prop(id="relic"),)
    locations = tuple(Location(id=f"loc{i}") for i in range(EPISODES))
    episodes = tuple(Episode(id=f"ep{i}", order_index=i) for i in range(EPISODES))

    scenes: list[Scene] = []
    events: list[Event] = []
    temporal: list[TemporalConstraint] = []
    beats: list[StoryBeat] = []

    for i in range(EPISODES):
        for j in range(SCENES_PER_EPISODE):
            sid = f"ep{i}_sc{j}"
            scenes.append(
                Scene(
                    id=sid,
                    episode_id=f"ep{i}",
                    location_id=f"loc{i}",
                    order_index=j,
                    time_of_day=TimeOfDay.DAY if j % 2 == 0 else TimeOfDay.NIGHT,
                )
            )
            beats.append(StoryBeat(id=f"beat_{sid}", episode_id=f"ep{i}", order_index=len(beats)))
            events.append(
                Event(
                    id=f"{sid}_enter",
                    episode_id=f"ep{i}",
                    scene_id=sid,
                    order_index=0,
                    story_tick=len(scenes) * 1000,
                    kind=EventKind.ENTER_LOCATION,
                    actor_id="alice" if j % 2 == 0 else "bob",
                    target_id=f"loc{i}",
                    source=src,
                )
            )
            g = i * SCENES_PER_EPISODE + j
            holder = "alice" if g % 2 == 0 else "bob"
            taker = "bob" if g % 2 == 0 else "alice"
            if break_at == g:
                holder = "alice"  # alice does not hold the relic at an odd g -> conflict
            events.append(
                Event(
                    id=f"{sid}_xfer",
                    episode_id=f"ep{i}",
                    scene_id=sid,
                    order_index=1,
                    story_tick=len(scenes) * 1000,
                    kind=EventKind.TRANSFER_PROP,
                    actor_id=holder,
                    target_id=taker,
                    prop_id="relic",
                    source=src,
                )
            )
            anchor = f"scene:{sid}"
            if j > 0:
                temporal.append(
                    TemporalConstraint(
                        id=f"tc_{sid}",
                        a=f"scene:ep{i}_sc{j - 1}",
                        b=anchor,
                        lower=1,
                        upper=10_000_000,
                        source=src,
                    )
                )

    initial = (
        DeclaredFact(
            key=FactKey(subject_id="alice", relation=FactRelation.POSSESSES, target_id="relic"),
            value=TriValue.TRUE,
            source=src,
        ),
    )
    return StoryDocument(
        schema_version=SCHEMA_VERSION,
        characters=characters,
        props=props,
        locations=locations,
        episodes=episodes,
        scenes=tuple(scenes),
        initial_facts=initial,
        events=tuple(events),
        temporal_constraints=tuple(temporal),
        beats=tuple(beats),
        shot_candidates=(),
    )


def test_large_story_scales_and_passes() -> None:
    doc = _build_large_doc()
    assert len(doc.scenes) == TOTAL_SCENES
    assert len(doc.events) == TOTAL_SCENES * 2

    report = analyze_story(doc)
    assert report.status is Status.PASS
    assert len(report.causal.event_order) == TOTAL_SCENES * 2
    # the relic moved 1000 times; an even count returns it to alice
    assert (
        report.causal.final_state.get_tuple("alice", FactRelation.POSSESSES, "relic")
        is TriValue.TRUE
    )
    assert (
        report.causal.final_state.get_tuple("bob", FactRelation.POSSESSES, "relic")
        is TriValue.FALSE
    )


def test_large_temporal_network_is_consistent() -> None:
    doc = _build_large_doc()
    extra = tuple(sorted({e.id for e in doc.events}))
    result = check_consistency(doc.temporal_constraints, extra_nodes=extra)
    assert result.status is Status.PASS
    assert (
        len(doc.temporal_constraints) == TOTAL_SCENES - EPISODES
    )  # one link per scene after the first


def test_large_incremental_matches_full_recompute() -> None:
    base = _build_large_doc()
    base_report = full_recompute(base)

    # Mutate ONE event in episode 0 (scene 1 -> a conflict) and verify the whole
    # downstream analysis is invalidated, not just the adjacent scene.
    mutated = _build_large_doc(break_at=1)
    engine = IncrementalEngine()
    inc = engine.update(base, base_report, mutated)
    full = full_recompute(mutated)

    assert inc.report.status == full.status
    assert inc.report.findings == full.findings
    assert inc.report.final_state.items() == full.final_state.items()
    assert inc.report.event_order == full.event_order
    # non-vacuous: the early mutation must change the overall verdict
    assert inc.report.status is Status.FAIL
    assert inc.mode == "incremental"


def test_large_report_exports_to_real_disk_and_reopens(tmp_path: Path) -> None:
    doc = _build_large_doc()
    report = analyze_story(doc)
    fs = WorkspaceFilesystem(tmp_path)
    artifact = export_report(report, fs, "atlas/report.json")

    assert artifact.sha256 == artifact.reopened_sha256
    on_disk = (tmp_path / "atlas" / "report.json").read_bytes()
    assert hashlib.sha256(on_disk).hexdigest() == artifact.sha256
    assert artifact.size_bytes == len(on_disk)


def test_large_report_exports_through_staging(tmp_path: Path) -> None:
    doc = _build_large_doc()
    report = analyze_story(doc)
    fs = WorkspaceFilesystem(tmp_path)
    staging = AttemptStaging(tmp_path / "stage", job_id="j1", attempt_id="a1", final_root=tmp_path)
    artifact = export_report(report, fs, "atlas/staged.json", staging=staging)
    assert artifact.sha256 == artifact.reopened_sha256
    assert (tmp_path / "atlas" / "staged.json").exists()


def test_plan_compiles_to_timeline_dag() -> None:
    beats = tuple(f"beat_{i}" for i in range(40))
    candidates = tuple(
        ShotCandidate(
            id=f"shot_{i}_{k}",
            asset_ref=f"asset://shot_{i}_{k}",
            covers_beats=(f"beat_{i}",),
            frames=24 + k,
            preference=k,
            evidence_status=TriValue.TRUE,
            evidence_ref=f"evidence://shot_{i}_{k}",
            source=SourceRef(path=f"ep1/sc{i}", line=1),
        )
        for i in range(40)
        for k in range(3)
    )
    plan = plan_edit(PlanRequest(beats=beats, candidates=candidates, frame_budget=40 * 30))
    assert plan.status.value == "PROVEN_OPTIMAL"
    dag = compile_atlas_plan_to_dag(plan, track_id="v1")
    order = dag.topological_order()
    assert len(order) == len(plan.steps) + 1  # clips + the track node
    assert dag.total_frames == sum(s.frames for s in plan.steps)


def test_montage_compiles_to_timeline_dag() -> None:
    clips = tuple(ClipCandidate(f"c{i}", 4, 24) for i in range(12))
    sched = optimize_rhythmic_montage(MontageRequest(clips=clips, pace_target=(6, 6)))
    dag = compile_montage_to_timeline_dag(sched)
    assert len(dag.topological_order()) == len(sched.clip_ids) + 1


def test_fountain_parser_handles_thousand_scene_script() -> None:
    lines: list[str] = ["Title: Stress", ""]
    for i in range(TOTAL_SCENES):
        lines.append(f"INT. LOC{i % 10} - DAY")
        lines.append("")
        lines.append("Alice waits for the signal.")
        lines.append("")
    script = "\n".join(lines)
    parsed = parse_fountain(script)
    assert len(parsed.scenes) == TOTAL_SCENES
    doc = compile_fountain_to_story_document(parsed, "ep_stress")
    assert len(doc.scenes) == TOTAL_SCENES


@pytest.mark.parametrize("break_at", [1, 501, 999])
def test_conflict_is_found_at_every_depth(break_at: int) -> None:
    doc = _build_large_doc(break_at=break_at)
    report = analyze_story(doc)
    assert report.status is Status.FAIL
