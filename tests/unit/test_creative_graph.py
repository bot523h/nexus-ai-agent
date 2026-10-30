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
import sqlite3
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


# --------------------------------------------------------------------------- #
# rebuild atomicity and terminal-state safety
# --------------------------------------------------------------------------- #
# Reproduced pre-fix: (a) a rebuild after a caller invalidated a projected node
# raised GraphStateError partway through, and (b) `replace=True` deleted the old
# projection in one transaction and wrote the new one in many, so a crash in
# between left an observer looking at 1 node / 0 edges where a healthy
# 5 nodes / 3 edges projection had stood.
def _journal_for(tmp_path, *, digest: str = "d" * 64):
    from nexus_ai_agent.provenance.journal import CausalJournal
    from nexus_ai_agent.provenance.models import CausalEvent, EventKind

    journal = CausalJournal(tmp_path / "ledger.sqlite")
    for kind, attempt, status, result in (
        (EventKind.JOB_ENQUEUED, None, "pending", None),
        (EventKind.JOB_RESERVED, 1, "processing", None),
        (EventKind.JOB_VERIFICATION_STARTED, 1, "verifying", None),
        (EventKind.JOB_COMPLETED, 1, "completed", digest),
    ):
        journal.append(
            CausalEvent(
                kind=kind,
                job_id="job_rebuild",
                job_type="creative.render",
                idempotency_key="key-rebuild",
                attempt=attempt,
                status=status,
                result_digest=result,
                occurred_at="2026-10-07T00:00:00Z",
            )
        )
    return journal.all_records(), digest


def test_a_rebuild_skips_a_node_a_caller_invalidated(tmp_path: Path) -> None:
    """An explicit lifecycle decision outranks the projection."""
    records, digest = _journal_for(tmp_path)
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    graph.rebuild_from_journal(records)
    artifact_id = node_identity("ARTIFACT", {"job_id": "job_rebuild", "result_digest": digest})
    graph.set_status(artifact_id, "invalidated")
    history_before = graph.counts()["history"]

    report = graph.rebuild_from_journal(records)

    assert [s["node_id"] for s in report["skipped_terminal"]] == [artifact_id]
    assert report["skipped_terminal"][0]["status"] == "invalidated"
    # the terminal state survived, and no synthetic history was appended
    assert (
        graph.find("ARTIFACT", {"job_id": "job_rebuild", "result_digest": digest}).status
        == "invalidated"
    )
    assert graph.counts()["history"] == history_before


def test_a_rebuild_skips_a_revoked_node_too(tmp_path: Path) -> None:
    records, digest = _journal_for(tmp_path)
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    graph.rebuild_from_journal(records)
    artifact_id = node_identity("ARTIFACT", {"job_id": "job_rebuild", "result_digest": digest})
    graph.set_status(artifact_id, "revoked")

    report = graph.rebuild_from_journal(records)
    assert [s["status"] for s in report["skipped_terminal"]] == ["revoked"]
    assert (
        graph.find("ARTIFACT", {"job_id": "job_rebuild", "result_digest": digest}).status
        == "revoked"
    )


def test_replace_mode_does_not_undo_an_invalidation(tmp_path: Path) -> None:
    """A from-scratch rebuild of the projection is not a licence to revive."""
    records, digest = _journal_for(tmp_path)
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    graph.rebuild_from_journal(records)
    artifact_id = node_identity("ARTIFACT", {"job_id": "job_rebuild", "result_digest": digest})
    graph.set_status(artifact_id, "invalidated")

    report = graph.rebuild_from_journal(records, replace=True)
    assert [s["node_id"] for s in report["skipped_terminal"]] == [artifact_id]
    assert (
        graph.find("ARTIFACT", {"job_id": "job_rebuild", "result_digest": digest}).status
        == "invalidated"
    )


@pytest.mark.parametrize("crash_after", [0, 1, 2, 3])
def test_a_crash_mid_rebuild_never_publishes_a_partial_projection(
    tmp_path: Path, crash_after: int
) -> None:
    """§14 crash injection: all-or-nothing, observed from a NEW process.

    Either the previous healthy projection stands, or the new generation stands
    whole.  A half-built projection that looks healthy is the failure this kills.
    """
    records, _ = _journal_for(tmp_path)
    db = tmp_path / "graph.sqlite"
    graph = CreativeGraph(db)
    first = graph.rebuild_from_journal(records)
    healthy = graph.counts()
    assert first["projected_nodes"]["ARTIFACT"] == 1

    calls = {"n": 0}
    real_put = CreativeGraph._put_node_tx

    def exploding(self, connection, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] > crash_after:
            raise RuntimeError("injected crash mid-rebuild")
        return real_put(self, connection, *args, **kwargs)

    CreativeGraph._put_node_tx = exploding
    try:
        with pytest.raises(RuntimeError, match="injected crash"):
            CreativeGraph(db).rebuild_from_journal(records, replace=True)
    finally:
        CreativeGraph._put_node_tx = real_put

    # A separate object reopens the file, as a new process would.
    observer = CreativeGraph(db)
    assert observer.counts() == healthy, (
        "the transaction must roll back; a partial projection was observable"
    )
    assert observer.integrity_check().ok, "the surviving projection must still be sound"


def test_rebuild_is_stable_across_repeated_runs(tmp_path: Path) -> None:
    """§15: 1x, 2x, 3x — idempotent, with no synthetic history growth."""
    records, _ = _journal_for(tmp_path)
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    snapshots = []
    for _ in range(3):
        graph.rebuild_from_journal(records)
        snapshots.append(graph.counts())
    assert snapshots[0] == snapshots[1] == snapshots[2], snapshots

    replace_snapshots = []
    for _ in range(3):
        graph.rebuild_from_journal(records, replace=True)
        replace_snapshots.append(graph.counts())
    assert replace_snapshots[0] == replace_snapshots[1] == replace_snapshots[2]
    assert replace_snapshots[0] == snapshots[0], "replace must converge to the same graph"


def test_rebuild_never_manufactures_causal_history(tmp_path: Path) -> None:
    """§16: the projection is reconstructed; history is only ever appended.

    The pre-fix defect replayed every ledger transition, so one replay grew the
    history log (7 -> 10) — a rebuild that was really an append in disguise.
    """
    records, _ = _journal_for(tmp_path)
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    graph.rebuild_from_journal(records)
    counts_after_first = graph.counts()

    def all_history() -> dict:
        connection = sqlite3.connect(tmp_path / "graph.sqlite")
        try:
            ids = [
                r[0]
                for r in connection.execute(
                    "SELECT node_id FROM nexus_graph_node WHERE source = 'causal_journal'"
                )
            ]
        finally:
            connection.close()
        return {node_id: graph.history(node_id) for node_id in sorted(ids)}

    history_after_first = all_history()

    graph.rebuild_from_journal(records)
    assert graph.counts() == counts_after_first, "a plain replay must add nothing"
    assert all_history() == history_after_first, "a plain replay must not append history"

    graph.rebuild_from_journal(records, replace=True)
    assert graph.counts() == counts_after_first, "a replace rebuild must add nothing"
    assert all_history() == history_after_first, "a replace rebuild must not append history"

    # Every projected node keeps exactly its creation record: a rebuild
    # reconstructs the projection, it does not manufacture causal history.
    connection = sqlite3.connect(tmp_path / "graph.sqlite")
    try:
        node_ids = [
            r[0]
            for r in connection.execute(
                "SELECT node_id FROM nexus_graph_node WHERE source = 'causal_journal'"
            )
        ]
    finally:
        connection.close()
    assert node_ids, "the projection should contain nodes"
    for node_id in node_ids:
        rows = graph.history(node_id)
        assert len(rows) == 1, f"{node_id} carries {len(rows)} history rows, expected 1"


def test_graph030_still_fires_after_the_membership_rewrite(tmp_path: Path) -> None:
    """The O(nodes+history) rewrite must not weaken the check.

    GRAPH030 used to re-scan the whole history table per node.  Replacing that
    with a membership set is only safe if a node with no history row is still
    reported — and reported against the right node.
    """
    db = tmp_path / "graph.sqlite"
    graph = CreativeGraph(db)
    for i in range(50):
        graph.put_node("EVIDENCE", {"n": i}, {"n": i})
    assert graph.integrity_check().ok

    connection = sqlite3.connect(db)
    try:
        stripped = connection.execute("SELECT node_id FROM nexus_graph_node LIMIT 1").fetchone()[0]
        connection.execute("DELETE FROM nexus_graph_node_history WHERE node_id = ?", (stripped,))
        connection.commit()
    finally:
        connection.close()

    report = graph.integrity_check()
    assert not report.ok
    findings = [f for f in report.findings if f.code == "GRAPH030"]
    assert len(findings) == 1, "exactly the stripped node is missing its history row"
    assert findings[0].witness == stripped


def test_a_replace_rebuild_keeps_the_edge_to_a_revoked_passport(tmp_path: Path) -> None:
    """Skipping a terminal node must never skip its edge.

    `project()` returns None for a terminal passport, and the edge used to be
    gated on that result.  In replace mode the edge purge has already removed the
    old link, so the artifact silently lost ARTIFACT_HAS_PASSPORT — with
    integrity_check still reporting green.
    """
    records, digest = _journal_for(tmp_path)
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    graph.rebuild_from_journal(records)
    edges_before = graph.counts()["edges"]

    passport_id = node_identity("PASSPORT", {"job_id": "job_rebuild", "result_digest": digest})
    graph.set_status(passport_id, "revoked")
    report = graph.rebuild_from_journal(records, replace=True)

    assert [s["kind"] for s in report["skipped_terminal"]] == ["PASSPORT"]
    assert graph.counts()["edges"] == edges_before, "the passport edge was dropped"
    assert graph.find("PASSPORT", {"job_id": "job_rebuild", "result_digest": digest}).status == (
        "revoked"
    )
    assert graph.integrity_check().ok


def test_an_in_memory_rebuild_is_atomic_too(tmp_path: Path) -> None:
    """`:memory:` skipped BEGIN IMMEDIATE, so it never rolled back either.

    The all-or-nothing property has to hold for the in-memory graph as well: the
    shared connection keeps the implicit transaction Python opens before the
    first DML, so without an explicit rollback a failed rebuild left its partial
    writes visible.
    """
    records, _ = _journal_for(tmp_path)
    graph = CreativeGraph(":memory:")
    graph.rebuild_from_journal(records)
    healthy = graph.counts()

    calls = {"n": 0}
    real_put = CreativeGraph._put_node_tx

    def exploding(self, connection, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("injected crash mid-rebuild")
        return real_put(self, connection, *args, **kwargs)

    CreativeGraph._put_node_tx = exploding
    try:
        with pytest.raises(RuntimeError, match="injected crash"):
            graph.rebuild_from_journal(records, replace=True)
    finally:
        CreativeGraph._put_node_tx = real_put

    assert graph.counts() == healthy, "a partial in-memory projection was observable"


# --------------------------------------------------------------------------- #
# R4 — concurrent reader isolation on the shared :memory: connection
# --------------------------------------------------------------------------- #
# Reproduced pre-fix: the WRITE paths hold ``_lock`` for the whole transaction,
# but the READ paths (``counts`` / ``edges`` / ``get_node`` / ``history`` /
# ``integrity_check``) did not — and on ``:memory:`` there is ONE shared
# connection.  A SELECT issued while ``rebuild_from_journal(replace=True)`` was
# mid-transaction therefore ran on that same connection and saw the writer's
# UNCOMMITTED deletes and half-written rows: a partial projection, a zero-edge
# intermediate state, a half-written graph.
#
# The pause below is test-side timing control (a monkeypatched ``_put_node_tx``
# that parks the writer mid-transaction), so the reader's sample point is
# deterministic, not a lucky race.
def test_a_concurrent_reader_never_observes_a_partial_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tempfile
    import threading

    from nexus_ai_agent.provenance.journal import CausalJournal
    from nexus_ai_agent.provenance.models import CausalEvent, EventKind

    graph = CreativeGraph(":memory:")

    # A healthy old state: a chain of 120 nodes / 119 edges.
    old_ids = [graph.put_node("ARTIFACT", {"n": i}, {"i": i}).node.node_id for i in range(120)]
    for i in range(119):
        graph.put_edge("ARTIFACT_DERIVED_FROM", old_ids[i], old_ids[i + 1])
    old_counts = graph.counts()
    old_edges = {e.edge_id for e in graph.edges(old_ids[0])}
    assert old_counts == {"nodes": 120, "edges": 119, "history": 120}
    assert len(old_edges) == 1

    # A journal that rebuilds into a DIFFERENT complete state.
    with tempfile.TemporaryDirectory() as tmp:
        journal = CausalJournal(Path(tmp) / "ledger.sqlite")
        digest = "e" * 64
        for j in range(100):
            for kind, attempt, status, result in (
                (EventKind.JOB_ENQUEUED, None, "pending", None),
                (EventKind.JOB_RESERVED, 1, "processing", None),
                (EventKind.JOB_VERIFICATION_STARTED, 1, "verifying", None),
                (EventKind.JOB_COMPLETED, 1, "completed", digest),
            ):
                journal.append(
                    CausalEvent(
                        kind=kind,
                        job_id=f"job-{j}",
                        job_type="creative.render",
                        idempotency_key=f"key-{j}",
                        attempt=attempt,
                        status=status,
                        result_digest=result,
                        occurred_at="2026-10-07T00:00:00Z",
                    )
                )
        records = journal.all_records()

        # Park the writer at a controlled point INSIDE its transaction: the
        # replace-deletes have run, the first node write is about to happen.
        mid_transaction = threading.Event()
        release_writer = threading.Event()
        real_put = graph._put_node_tx
        state = {"calls": 0}

        def pausing_put(connection, kind, identity, payload=None, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                mid_transaction.set()
                assert release_writer.wait(15), "reader never released the writer"
            return real_put(connection, kind, identity, payload, **kwargs)

        monkeypatch.setattr(graph, "_put_node_tx", pausing_put)

        barrier = threading.Barrier(2)
        done = threading.Event()
        observations: list[tuple[dict, set, object]] = []
        errors: list[BaseException] = []

        def writer() -> None:
            barrier.wait()
            graph.rebuild_from_journal(records, replace=True)
            done.set()

        def reader() -> None:
            barrier.wait()
            # Wait until the writer is provably mid-transaction, then sample.
            assert mid_transaction.wait(15), "writer never reached mid-transaction"
            release_writer.set()
            while not done.is_set():
                try:
                    observations.append(
                        (
                            graph.counts(),
                            {e.edge_id for e in graph.edges(old_ids[0])},
                            graph.get_node(old_ids[0]),
                        )
                    )
                except BaseException as exc:  # noqa: BLE001 - any raise is an anomaly
                    errors.append(exc)
                if len(observations) >= 5:
                    break

        w = threading.Thread(target=writer)
        r = threading.Thread(target=reader)
        w.start()
        r.start()
        w.join(60)
        r.join(60)
        assert not w.is_alive(), "writer hung"
        assert not r.is_alive(), "reader hung"
        assert not errors, f"reader raised: {errors[:3]}"
        assert observations, "reader never sampled"

    new_counts = graph.counts()
    new_edges = {e.edge_id for e in graph.edges(old_ids[0])}
    assert new_counts != old_counts, "the rebuild did not actually replace the state"

    bad = [
        obs
        for obs in observations
        if obs[0] not in (old_counts, new_counts) or obs[1] not in (old_edges, new_edges)
    ]
    assert not bad, (
        f"the reader observed a partial rebuild: {bad[:3]} "
        f"(old={old_counts}/{sorted(old_edges)} new={new_counts}/{sorted(new_edges)})"
    )
    assert graph.integrity_check().ok


# --------------------------------------------------------------------------- #
# R5 — the restore scope boundary
# --------------------------------------------------------------------------- #
# Reproduced pre-fix: restore() is a WHOLE-FILE replacement (the SQLite backup
# API cannot target a subset of tables).  On a file shared with the causal
# journal — the authority the graph is a projection OF — a projection restore
# silently rewound the ledger from 6 events to 3.  The graph must never
# overwrite the authority by accident.
def _shared_file(tmp_path: Path, *, journal_events: int = 3) -> tuple:
    """One file holding a causal journal, a graph projection, and a marker."""
    import sqlite3

    from nexus_ai_agent.provenance.journal import CausalJournal
    from nexus_ai_agent.provenance.models import CausalEvent, EventKind

    shared = tmp_path / "shared.sqlite"
    journal = CausalJournal(shared)
    for i in range(journal_events):
        journal.append(
            CausalEvent(
                kind=EventKind.JOB_COMPLETED,
                job_id=f"job-{i}",
                job_type="creative.render",
                idempotency_key=f"key-{i}",
                attempt=1,
                status="completed",
                result_digest="d" * 64,
                occurred_at="2026-10-07T00:00:00Z",
            )
        )
    graph = CreativeGraph(shared)
    graph.put_node("ARTIFACT", {"job_id": "job-0", "result_digest": "d" * 64}, {"bytes": 1})
    marker = sqlite3.connect(str(shared))
    marker.execute("CREATE TABLE independent_marker (id INTEGER PRIMARY KEY, note TEXT)")
    marker.execute("INSERT INTO independent_marker (note) VALUES ('do-not-touch')")
    marker.commit()
    marker.close()

    def counts() -> tuple[int, int, int]:
        connection = sqlite3.connect(str(shared))
        try:
            journal_rows = connection.execute(
                "SELECT COUNT(*) FROM nexus_causal_journal"
            ).fetchone()[0]
            marker_rows = connection.execute("SELECT COUNT(*) FROM independent_marker").fetchone()[
                0
            ]
            graph_rows = connection.execute("SELECT COUNT(*) FROM nexus_graph_node").fetchone()[0]
            return journal_rows, marker_rows, graph_rows
        finally:
            connection.close()

    return graph, counts


def test_restore_refuses_to_overwrite_a_shared_file_by_default(tmp_path: Path) -> None:
    """The reproduced hazard: a projection restore must not rewind the ledger."""
    from nexus_ai_agent.provenance.models import CausalEvent, EventKind

    graph, counts = _shared_file(tmp_path, journal_events=3)
    backup = graph.backup(tmp_path / "backup.sqlite")
    before = counts()

    # the world moves on: three more ledger events the backup does not have
    from nexus_ai_agent.provenance.journal import CausalJournal

    journal = CausalJournal(tmp_path / "shared.sqlite")
    for i in range(3, 6):
        journal.append(
            CausalEvent(
                kind=EventKind.JOB_COMPLETED,
                job_id=f"job-{i}",
                job_type="creative.render",
                idempotency_key=f"key-{i}",
                attempt=1,
                status="completed",
                result_digest="d" * 64,
                occurred_at="2026-10-07T00:00:00Z",
            )
        )
    assert counts() == (6, 1, 1)

    with pytest.raises(GraphStateError, match="nexus_causal_journal"):
        graph.restore(backup)

    # the refusal changed nothing: the ledger kept its newer events
    assert counts() == (6, 1, 1), "a refused restore must not touch the file"
    assert before == (3, 1, 1)


def test_restore_with_whole_database_replaces_exactly_the_whole_file(
    tmp_path: Path,
) -> None:
    """Explicit acknowledgement: WHAT is replaced is the entire file, nothing less.

    The independent marker table was created BEFORE the backup, so a whole-file
    restore brings its old row back; a marker row created AFTER the backup is
    destroyed.  Both halves prove the scope is the file, not the projection.
    """
    import sqlite3

    graph, counts = _shared_file(tmp_path, journal_events=3)
    backup = graph.backup(tmp_path / "backup.sqlite")

    connection = sqlite3.connect(str(tmp_path / "shared.sqlite"))
    connection.execute("INSERT INTO independent_marker (note) VALUES ('after-backup')")
    connection.commit()
    connection.close()
    assert counts() == (3, 2, 1)

    graph.restore(backup, whole_database=True)
    # journal rewound to the backup, the post-backup marker row destroyed
    assert counts() == (3, 1, 1)


def test_restore_refuses_a_source_that_is_not_a_graph_backup(tmp_path: Path) -> None:
    """Source validation: a foreign file is refused, not half-restored."""
    import sqlite3

    graph = CreativeGraph(tmp_path / "graph.sqlite")
    graph.put_node("ARTIFACT", {"n": 1}, {"i": 1})

    foreign = tmp_path / "foreign.sqlite"
    connection = sqlite3.connect(str(foreign))
    connection.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(GraphStateError, match="not a graph backup"):
        graph.restore(foreign)
    # the destination is untouched
    assert graph.counts() == {"nodes": 1, "edges": 0, "history": 1}


def test_restore_refuses_a_source_that_is_not_readable(tmp_path: Path) -> None:
    """A file that is not a database at all fails closed, not with a raw error."""
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    graph.put_node("ARTIFACT", {"n": 1}, {"i": 1})

    garbage = tmp_path / "garbage.sqlite"
    garbage.write_bytes(b"this is not a database file " * 64)

    with pytest.raises(GraphStateError, match="not a readable database"):
        graph.restore(garbage)
    assert graph.counts() == {"nodes": 1, "edges": 0, "history": 1}


def test_restore_refuses_a_structurally_corrupt_source(tmp_path: Path) -> None:
    """Table names alone are not proof: a corrupt b-tree must be rejected too.

    The schema page can stay readable while a data page is malformed, so the
    name check passes and ``Connection.backup`` would copy the corruption over
    the (possibly journal-bearing) destination.  ``PRAGMA integrity_check`` is
    the structural gate that catches it.
    """
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    graph.put_node("ARTIFACT", {"n": 1}, {"i": 1})

    damaged = tmp_path / "damaged.sqlite"
    connection = sqlite3.connect(str(damaged))
    for table in ("nexus_graph_node", "nexus_graph_edge", "nexus_graph_node_history"):
        connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, blob TEXT)")
    for row in range(8000):
        connection.execute("INSERT INTO nexus_graph_node VALUES (?, ?)", (row, "y" * 300))
    connection.commit()
    connection.close()

    # Flip bytes in a data page, leaving the schema (page 1) intact.
    payload = bytearray(damaged.read_bytes())
    for offset in range(4096 * 5 + 200, 4096 * 5 + 600):
        payload[offset] ^= 0xFF
    damaged.write_bytes(bytes(payload))
    # The table names are still readable — that is exactly why the name check
    # alone is insufficient.
    probe = sqlite3.connect(str(damaged))
    assert probe.execute("SELECT name FROM sqlite_master").fetchall()
    probe.close()

    with pytest.raises(GraphStateError, match="integrity_check"):
        graph.restore(damaged)
    assert graph.counts() == {"nodes": 1, "edges": 0, "history": 1}


def test_restore_on_a_graph_only_file_needs_no_acknowledgement(tmp_path: Path) -> None:
    """The ordinary case is unchanged: a graph-only file restores directly."""
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    ids = build_slice(graph)
    before = graph.counts()
    backup = graph.backup(tmp_path / "backup.sqlite")
    with graph._write_connection() as connection:  # noqa: SLF001 - the test is the attacker
        connection.execute("DELETE FROM nexus_graph_edge")
    graph.restore(backup)
    assert graph.counts() == before
    assert graph.lineage(ids["ARTIFACT"])["complete"] is True


def test_restore_refuses_to_overwrite_a_shared_memory_graph() -> None:
    """The :memory: path has the same scope contract as the file path."""
    import tempfile

    graph = CreativeGraph(":memory:")
    graph.put_node("ARTIFACT", {"n": 1}, {"i": 1})
    # something non-graph lives in the same in-memory database
    graph._memory_connection.execute(  # noqa: SLF001 - the test is the fixture
        "CREATE TABLE independent_marker (id INTEGER PRIMARY KEY)"
    )
    graph._memory_connection.commit()

    other = CreativeGraph(":memory:")
    other.put_node("ARTIFACT", {"n": 2}, {"i": 2})
    backup_path = Path(tempfile.mkdtemp()) / "backup.sqlite"
    other.backup(backup_path)

    with pytest.raises(GraphStateError, match="independent_marker"):
        graph.restore(backup_path)
    assert graph.counts() == {"nodes": 1, "edges": 0, "history": 1}
    graph.restore(backup_path, whole_database=True)
    assert graph.counts() == {"nodes": 1, "edges": 0, "history": 1}  # other's slice


# --------------------------------------------------------------------------- #
# CodeRabbit round 3 (10:25Z) — superseded is an explicit lifecycle decision
# --------------------------------------------------------------------------- #
# Reproduced pre-fix, three facets of one defect:
#   a. put_node on a SUPERSEDED node reactivated it (status -> active,
#      superseded_by -> NULL), undoing the decision.
#   b. rebuild_from_journal(replace=True) re-projected a superseded
#      journal-sourced node back to active and destroyed its supersede
#      history row.
#   c. the replace-mode delete filters excluded only invalidated/revoked, so a
#      superseded node the journal does not re-project was deleted together
#      with its history.
def test_a_write_must_not_reactivate_a_superseded_node(tmp_path: Path) -> None:
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    node_id = graph.put_node("ARTIFACT", {"n": 1}, {"v": 1}).node.node_id
    graph.set_status(node_id, "superseded", superseded_by="successor")
    with pytest.raises(GraphStateError, match="superseded"):
        graph.put_node("ARTIFACT", {"n": 1}, {"v": 2})
    node = graph.get_node(node_id)
    assert node is not None
    assert node.status == "superseded", "the lifecycle decision must survive the write"
    assert node.superseded_by == "successor"


def test_a_rebuild_preserves_a_superseded_node_and_its_history(tmp_path: Path) -> None:
    records, _digest = _journal_for(tmp_path)
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    graph.rebuild_from_journal(records)
    node_id = graph.find("ARTIFACT", {"job_id": "job_rebuild", "result_digest": "d" * 64})
    assert node_id is not None
    node_id = node_id.node_id
    graph.set_status(node_id, "superseded", superseded_by="successor")
    history_before = len(graph.history(node_id))

    report = graph.rebuild_from_journal(records, replace=True)

    node = graph.get_node(node_id)
    assert node is not None, "a superseded node must survive a from-scratch rebuild"
    assert node.status == "superseded", "the rebuild must not reactivate it"
    assert node.superseded_by == "successor"
    assert len(graph.history(node_id)) == history_before, "its history must be kept"
    assert any(
        entry.get("status") == "superseded" and entry.get("node_id") == node_id
        for entry in report.get("skipped_terminal", [])
    ), "the skip must be reported, not silent"


def test_a_rebuild_does_not_delete_a_superseded_node_the_journal_cannot_reproject(
    tmp_path: Path,
) -> None:
    """Facet c: the replace-mode delete filters must keep superseded nodes."""
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    node_id = graph.put_node("ARTIFACT", {"n": 7}, {"v": 1}).node.node_id
    # mark the row as journal-sourced so the replace-mode filters apply to it
    with graph._write_connection() as connection:  # noqa: SLF001 - the test is the fixture
        connection.execute(
            "UPDATE nexus_graph_node SET source = 'causal_journal' WHERE node_id = ?",
            (node_id,),
        )
    graph.set_status(node_id, "superseded", superseded_by="successor")

    graph.rebuild_from_journal([], replace=True)  # an empty journal re-projects nothing

    node = graph.get_node(node_id)
    assert node is not None, "a superseded node must not be deleted by a rebuild"
    assert node.status == "superseded"
    assert len(graph.history(node_id)) >= 1, "its history must not be purged"


def test_integrity_check_reports_malformed_json_instead_of_raising(
    tmp_path: Path,
) -> None:
    """A corrupt row is a FINDING, never an exception escaping the check.

    Reproduced pre-fix: `payload_json = '{not json'` made integrity_check
    raise JSONDecodeError instead of reporting GRAPH012.
    """
    graph = CreativeGraph(tmp_path / "graph.sqlite")
    ids = build_slice(graph)
    with graph._write_connection() as connection:  # noqa: SLF001 - the test is the attacker
        connection.execute(
            "UPDATE nexus_graph_node SET payload_json = '{not json' WHERE node_id = ?",
            (ids["ARTIFACT"],),
        )
        connection.execute(
            "UPDATE nexus_graph_node SET identity_json = '[]' WHERE node_id = ?",
            (ids["PLAN"],),
        )

    report = graph.integrity_check()  # must not raise

    assert not report.ok
    codes = {f.code for f in report.findings}
    assert "GRAPH012" in codes, "malformed payload_json must be reported"
    assert "GRAPH013" in codes, "a non-object identity_json must be reported"
    messages = {f.message for f in report.findings}
    assert any("not a JSON object" in m for m in messages)
