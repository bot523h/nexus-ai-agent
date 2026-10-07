"""Wave 3 — the durable creative graph: identity, history, lineage, recovery.

The acceptance test this file exists for is the one at the end: build the whole
vertical slice, **stop the process**, start a new one, and ask *"where did this
artifact come from?"* — and get a documented, queryable answer. If the graph
were in-memory, that question would have no answer after the restart.

Also covered: deterministic identity (the same logical state twice is one
truth, not two), history that is appended and never rewritten, explicit
lifecycle transitions, the invalidation query, rebuild from the causal journal
with an honest ``UNMIGRATED`` report for what the ledger cannot witness, and
backup / restore / integrity.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from nexus_ai_agent.provenance import (
    CreativeGraph,
    GraphStateError,
    node_identity,
)
from nexus_ai_agent.provenance.journal import CausalJournal
from nexus_ai_agent.provenance.models import CausalEvent, EventKind

REPO_ROOT = Path(__file__).parents[2]


# --------------------------------------------------------------------------- #
# a whole vertical slice, in one helper
# --------------------------------------------------------------------------- #
def build_slice(graph: CreativeGraph, *, job_id: str = "job-1", digest: str = "digest-1") -> dict:
    ids: dict[str, str] = {}
    for kind, identity, payload in (
        ("PROJECT", {"slug": "demo"}, {"title": "Demo project"}),
        ("INTENT", {"project": "demo", "text": "a 30 second teaser"}, {"kind": "teaser"}),
        ("REVISION", {"intent": "demo/teaser", "n": 1}, {"compiler": "v1"}),
        ("PLAN", {"revision": "demo/teaser#1"}, {"steps": 3}),
        (
            "COMMAND",
            {"plan": "demo/teaser#1", "op": "timeline.trim"},
            {"protocol": "nagar.command.v1"},
        ),
        ("EXECUTION", {"job_id": job_id, "attempt": 1}, {"status": "completed"}),
        ("ARTIFACT", {"job_id": job_id, "result_digest": digest}, {"bytes": 12}),
        ("VERIFICATION", {"job_id": job_id, "attempt": 1}, {"ok": True}),
        ("PASSPORT", {"job_id": job_id, "result_digest": digest}, {"status": "VERIFIED"}),
    ):
        ids[kind] = graph.put_node(kind, identity, payload).node.node_id
    for relation, src, dst in (
        ("PROJECT_HAS_INTENT", "PROJECT", "INTENT"),
        ("INTENT_COMPILED_TO_REVISION", "INTENT", "REVISION"),
        ("REVISION_HAS_PLAN", "REVISION", "PLAN"),
        ("PLAN_EMITS_COMMAND", "PLAN", "COMMAND"),
        ("COMMAND_EXECUTED_AS", "COMMAND", "EXECUTION"),
        ("EXECUTION_PRODUCED_ARTIFACT", "EXECUTION", "ARTIFACT"),
        ("ARTIFACT_VERIFIED_BY", "ARTIFACT", "VERIFICATION"),
        ("ARTIFACT_HAS_PASSPORT", "ARTIFACT", "PASSPORT"),
    ):
        graph.put_edge(relation, ids[src], ids[dst])
    return ids


# --------------------------------------------------------------------------- #
# identity: deterministic, stable, scoped, auditable
# --------------------------------------------------------------------------- #
def test_the_same_logical_state_created_twice_is_one_truth(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    first = graph.put_node("PROJECT", {"slug": "demo"}, {"title": "Demo"})
    second = graph.put_node("PROJECT", {"slug": "demo"}, {"title": "Demo"})
    assert first.created and not second.created
    assert second.unchanged, "re-writing identical state must be a no-op, not a new revision"
    assert first.node.node_id == second.node.node_id
    assert graph.counts()["nodes"] == 1
    assert graph.counts()["history"] == 1


def test_identity_is_scoped_by_kind() -> None:
    assert node_identity("PROJECT", {"id": "x"}) != node_identity("ARTIFACT", {"id": "x"})


def test_identity_is_stable_and_auditable(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    identity = {"job_id": "job-1", "attempt": 1}
    node = graph.put_node("EXECUTION", identity, {"status": "completed"}).node
    assert node.node_id == node_identity("EXECUTION", identity)
    assert node.identity == identity, "the identity payload is stored, so a digest is checkable"
    assert graph.find("EXECUTION", identity).node_id == node.node_id


def test_an_unknown_kind_or_relation_is_refused(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    with pytest.raises(ValueError):
        graph.put_node("UNIVERSE", {"id": 1})
    with pytest.raises(ValueError):
        graph.put_node("PROJECT", {})
    node = graph.put_node("PROJECT", {"slug": "demo"}).node
    with pytest.raises(ValueError):
        graph.put_edge("PROJECT_LOVES_INTENT", node.node_id, node.node_id)


def test_an_edge_to_a_missing_endpoint_is_refused(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    node = graph.put_node("PROJECT", {"slug": "demo"}).node
    with pytest.raises(GraphStateError):
        graph.put_edge("PROJECT_HAS_INTENT", node.node_id, "INTENT:sha256:does-not-exist")
    assert graph.counts()["edges"] == 0


# --------------------------------------------------------------------------- #
# history: append and supersede, never rewrite
# --------------------------------------------------------------------------- #
def test_a_changed_payload_appends_history_and_supersedes(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    identity = {"intent": "demo/teaser", "n": 1}
    first = graph.put_node("REVISION", identity, {"compiler": "v1"})
    second = graph.put_node("REVISION", identity, {"compiler": "v2"})
    assert first.created and second.superseded_previous
    assert second.node.payload == {"compiler": "v2"}
    assert second.node.revision_count == 2
    history = graph.history(second.node.node_id)
    assert [row["payload"]["compiler"] for row in history] == ["v1", "v2"], (
        "the previous truth must still be readable"
    )


def test_an_invalidated_node_is_never_silently_rewritten(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    identity = {"job_id": "job-1", "result_digest": "d"}
    node = graph.put_node("ARTIFACT", identity, {"bytes": 1}).node
    graph.set_status(node.node_id, "invalidated")
    with pytest.raises(GraphStateError):
        graph.put_node("ARTIFACT", identity, {"bytes": 2})
    assert graph.get_node(node.node_id).payload == {"bytes": 1}


def test_revoking_an_edge_is_explicit_and_blocks_a_silent_reassert(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    ids = build_slice(graph)
    edge_id = f"ARTIFACT_HAS_PASSPORT:{ids['ARTIFACT']}->{ids['PASSPORT']}"
    with graph._write_connection() as connection:  # noqa: SLF001 - the test is the attacker
        connection.execute(
            "UPDATE nexus_graph_edge SET status = 'revoked' WHERE edge_id = ?", (edge_id,)
        )
    with pytest.raises(GraphStateError):
        graph.put_edge("ARTIFACT_HAS_PASSPORT", ids["ARTIFACT"], ids["PASSPORT"])


def test_traversal_skips_inactive_nodes_unless_asked(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    ids = build_slice(graph)
    graph.set_status(ids["PLAN"], "invalidated")
    kinds = {row["node"]["kind"] for row in graph.descendants(ids["INTENT"])}
    assert "PLAN" not in kinds, "an invalidated node is not a live descendant"
    all_kinds = {
        row["node"]["kind"] for row in graph.descendants(ids["INTENT"], include_inactive=True)
    }
    assert "PLAN" in all_kinds, "but the past stays readable on request"


# --------------------------------------------------------------------------- #
# lineage and invalidation
# --------------------------------------------------------------------------- #
def test_the_artifact_answers_where_it_came_from(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    ids = build_slice(graph)
    lineage = graph.lineage(ids["ARTIFACT"])
    assert lineage["complete"] is True
    chain = [
        (level["relation"], [n["kind"] for n in level["found"]]) for level in lineage["levels"]
    ]
    assert chain == [
        ("EXECUTION_PRODUCED_ARTIFACT", ["EXECUTION"]),
        ("COMMAND_EXECUTED_AS", ["COMMAND"]),
        ("PLAN_EMITS_COMMAND", ["PLAN"]),
        ("REVISION_HAS_PLAN", ["REVISION"]),
        ("INTENT_COMPILED_TO_REVISION", ["INTENT"]),
        ("PROJECT_HAS_INTENT", ["PROJECT"]),
    ]
    assert [n["kind"] for n in lineage["attachments"]["ARTIFACT_VERIFIED_BY"]] == ["VERIFICATION"]
    assert [n["kind"] for n in lineage["attachments"]["ARTIFACT_HAS_PASSPORT"]] == ["PASSPORT"]
    assert lineage["deepest_kind"] == "PROJECT"


def test_a_broken_chain_reports_where_it_stops_instead_of_guessing(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    ids = build_slice(graph)
    graph.set_status(ids["REVISION"], "invalidated")
    lineage = graph.lineage(ids["ARTIFACT"])
    assert lineage["complete"] is False
    reached = [level["relation"] for level in lineage["levels"] if level["found"]]
    expected = ["EXECUTION_PRODUCED_ARTIFACT", "COMMAND_EXECUTED_AS", "PLAN_EMITS_COMMAND"]
    assert reached == expected, "the walk must stop at the break and say so"
    assert lineage["deepest_kind"] == "PLAN"


def test_changing_an_intent_names_every_affected_node(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    ids = build_slice(graph)
    report = graph.invalidation_candidates(ids["INTENT"])
    assert report["by_kind"] == {
        "ARTIFACT": [ids["ARTIFACT"]],
        "COMMAND": [ids["COMMAND"]],
        "EXECUTION": [ids["EXECUTION"]],
        "PASSPORT": [ids["PASSPORT"]],
        "PLAN": [ids["PLAN"]],
        "REVISION": [ids["REVISION"]],
        "VERIFICATION": [ids["VERIFICATION"]],
    }
    assert report["affected_count"] == 7


def test_traversal_is_deterministic(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    ids = build_slice(graph)
    first = json.dumps(graph.descendants(ids["PROJECT"]), sort_keys=True)
    second = json.dumps(graph.descendants(ids["PROJECT"]), sort_keys=True)
    assert first == second


def test_a_cycle_cannot_hang_the_traversal(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    a = graph.put_node("ARTIFACT", {"id": "a"}).node
    b = graph.put_node("ARTIFACT", {"id": "b"}).node
    graph.put_edge("ARTIFACT_DERIVED_FROM", a.node_id, b.node_id)
    graph.put_edge("ARTIFACT_DERIVED_FROM", b.node_id, a.node_id)
    rows = graph.descendants(a.node_id)
    assert [row["node"]["node_id"] for row in rows] == [b.node_id], (
        "the start node is not its own descendant, and the cycle must terminate"
    )
    assert [row["depth"] for row in rows] == [1]


# --------------------------------------------------------------------------- #
# rebuild from the canonical source, honestly
# --------------------------------------------------------------------------- #
def _journal(tmp_path: Path) -> CausalJournal:
    journal = CausalJournal(tmp_path / "journal.sqlite")
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id="job-1",
            job_type="slideshow",
            attempt=None,
            status="pending",
            payload_digest="pd",
        )
    )
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_RESERVED,
            job_id="job-1",
            job_type="slideshow",
            attempt=1,
            status="processing",
        )
    )
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_VERIFICATION_STARTED,
            job_id="job-1",
            job_type="slideshow",
            attempt=1,
            status="verifying",
        )
    )
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_COMPLETED,
            job_id="job-1",
            job_type="slideshow",
            attempt=1,
            status="completed",
            result_digest="sha256:res",
        )
    )
    return journal


def test_the_graph_is_rebuildable_from_the_ledger(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    report = graph.rebuild_from_journal(journal.all_records())
    assert report["projected_nodes"]["EXECUTION"] >= 1
    assert report["projected_nodes"]["ARTIFACT"] == 1
    assert report["projected_nodes"]["PASSPORT"] == 1
    assert graph.counts()["nodes"] >= 4
    assert graph.integrity_check().ok, graph.integrity_check().to_dict()


def test_rebuild_is_idempotent(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    graph.rebuild_from_journal(journal.all_records())
    before = graph.counts()
    graph.rebuild_from_journal(journal.all_records())
    assert graph.counts() == before, "replaying the ledger must not duplicate the projection"


def test_rebuild_reports_what_the_ledger_cannot_witness(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    report = graph.rebuild_from_journal(journal.all_records())
    for level in ("PROJECT", "INTENT", "REVISION", "PLAN", "COMMAND"):
        assert report["unmigrated_levels"][level].startswith("UNMIGRATED"), (
            f"{level} must be declared UNMIGRATED, never guessed"
        )
    assert "nexus_causal_journal" in report["canonical_source"]


def test_the_projection_names_the_ledger_record_it_came_from(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    graph.rebuild_from_journal(journal.all_records())
    node = graph.find("ARTIFACT", {"job_id": "job-1", "result_digest": "sha256:res"})
    assert node.source == "causal_journal"
    assert node.source_seq is not None
    assert node.payload["ledger_seq"] == node.source_seq


# --------------------------------------------------------------------------- #
# recovery: backup, restore, integrity
# --------------------------------------------------------------------------- #
def test_backup_and_restore_round_trip(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    ids = build_slice(graph)
    before = graph.counts()
    snapshot = graph.backup(tmp_path / "backup.sqlite")
    assert snapshot.is_file()

    damaged = CreativeGraph(tmp_path / "graph.sqlite")
    with damaged._write_connection() as connection:  # noqa: SLF001 - the test is the attacker
        connection.execute("DELETE FROM nexus_graph_edge")
    assert damaged.counts()["edges"] == 0
    assert not damaged.integrity_check().ok or damaged.counts()["edges"] == 0

    damaged.restore(snapshot)
    assert damaged.counts() == before
    assert damaged.integrity_check().ok
    assert damaged.lineage(ids["ARTIFACT"])["complete"] is True


def test_integrity_detects_a_dangling_edge(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    ids = build_slice(graph)
    assert graph.integrity_check().ok
    with graph._write_connection() as connection:  # noqa: SLF001 - the test is the attacker
        connection.execute("DELETE FROM nexus_graph_node WHERE node_id = ?", (ids["PLAN"],))
    report = graph.integrity_check()
    assert not report.ok
    assert any(f.code == "GRAPH021" for f in report.findings)


def test_integrity_detects_a_tampered_payload(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    ids = build_slice(graph)
    with graph._write_connection() as connection:  # noqa: SLF001 - the test is the attacker
        connection.execute(
            "UPDATE nexus_graph_node SET payload_json = ? WHERE node_id = ?",
            ('{"bytes":999}', ids["ARTIFACT"]),
        )
    report = graph.integrity_check()
    assert not report.ok
    assert any(f.code == "GRAPH012" for f in report.findings)


_BUILDER = """
from nexus_ai_agent.provenance import CreativeGraph

graph = CreativeGraph({db!r})
ids = {{}}
for kind, identity, payload in (
    ("PROJECT", {{"slug": "demo"}}, {{"title": "Demo"}}),
    ("INTENT", {{"project": "demo", "text": "teaser"}}, {{"kind": "teaser"}}),
    ("REVISION", {{"intent": "i", "n": 1}}, {{"compiler": "v1"}}),
    ("PLAN", {{"revision": "r"}}, {{"steps": 3}}),
    ("COMMAND", {{"plan": "p", "op": "timeline.trim"}}, {{"protocol": "nagar.command.v1"}}),
    ("EXECUTION", {{"job_id": "job-9", "attempt": 1}}, {{"status": "completed"}}),
    ("ARTIFACT", {{"job_id": "job-9", "result_digest": "d9"}}, {{"bytes": 7}}),
    ("VERIFICATION", {{"job_id": "job-9", "attempt": 1}}, {{"ok": True}}),
    ("PASSPORT", {{"job_id": "job-9", "result_digest": "d9"}}, {{"status": "VERIFIED"}}),
):
    ids[kind] = graph.put_node(kind, identity, payload).node.node_id
for relation, src, dst in (
    ("PROJECT_HAS_INTENT", "PROJECT", "INTENT"),
    ("INTENT_COMPILED_TO_REVISION", "INTENT", "REVISION"),
    ("REVISION_HAS_PLAN", "REVISION", "PLAN"),
    ("PLAN_EMITS_COMMAND", "PLAN", "COMMAND"),
    ("COMMAND_EXECUTED_AS", "COMMAND", "EXECUTION"),
    ("EXECUTION_PRODUCED_ARTIFACT", "EXECUTION", "ARTIFACT"),
    ("ARTIFACT_VERIFIED_BY", "ARTIFACT", "VERIFICATION"),
    ("ARTIFACT_HAS_PASSPORT", "ARTIFACT", "PASSPORT"),
):
    graph.put_edge(relation, ids[src], ids[dst])
print(ids["ARTIFACT"])
"""


def test_the_graph_survives_a_closed_process(tmp_path: Path) -> None:
    """The acceptance question, asked by a *different* process than the writer.

    Process 1 builds the whole vertical slice and exits. Process 2 opens the
    same file and asks "where did this artifact come from?". If the graph were
    in-memory, or if the projection were not durable, process 2 would have
    nothing to answer with.
    """
    db = tmp_path / "graph.sqlite"
    builder = tmp_path / "builder.py"
    builder.write_text(_BUILDER.format(db=str(db)), encoding="utf-8")
    first = subprocess.run(
        [sys.executable, str(builder)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )
    assert first.returncode == 0, first.stderr
    artifact_id = first.stdout.strip().splitlines()[-1]

    asker = tmp_path / "asker.py"
    asker.write_text(
        "import json\n"
        "from nexus_ai_agent.provenance import CreativeGraph\n"
        f"graph = CreativeGraph({str(db)!r})\n"
        f"print(json.dumps(graph.lineage({artifact_id!r})))\n",
        encoding="utf-8",
    )
    second = subprocess.run(
        [sys.executable, str(asker)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )
    assert second.returncode == 0, second.stderr
    lineage = json.loads(second.stdout.strip().splitlines()[-1])
    assert lineage["complete"] is True, "the answer must survive the restart"
    assert lineage["deepest_kind"] == "PROJECT"
    kinds = [n["kind"] for level in lineage["levels"] for n in level["found"]]
    assert kinds == ["EXECUTION", "COMMAND", "PLAN", "REVISION", "INTENT", "PROJECT"]
    assert [n["kind"] for n in lineage["attachments"]["ARTIFACT_HAS_PASSPORT"]] == ["PASSPORT"]


def test_two_processes_can_write_the_same_graph(tmp_path: Path) -> None:
    """Cross-process safety comes from SQLite's write lock, not the in-process mutex."""
    db = tmp_path / "graph.sqlite"
    script = (
        "from nexus_ai_agent.provenance import CreativeGraph;"
        f"g = CreativeGraph({str(db)!r});"
        "[g.put_node('EVIDENCE', {'job_id': 'j', 'n': n}, {'n': n}) for n in range(25)];"
        "print(g.counts()['nodes'])"
    )
    procs = [
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outs = [p.communicate(timeout=120) for p in procs]
    for stdout, stderr in outs:
        assert stdout.strip().splitlines()[-1] == "25", stderr
    assert CreativeGraph(db).counts()["nodes"] == 25, "no lost write and no duplicate"
