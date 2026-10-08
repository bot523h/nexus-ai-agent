"""Law R21 — the creative graph is a projection, never a second authority.

The repository already had three durable authorities: the job queue row (R14),
the hash-chained causal ledger (D-0024) and the Artifact Passport. The graph
added in Wave 3 must not quietly become a fourth one. These gates pin that:

* the graph module reaches no execution, authorization, bus, studio or adapter
  code — it cannot become a write path;
* it stays pure-stdlib + its own package, so it runs wherever ``provenance`` does
  (R1b's discipline);
* the projection is genuinely **rebuildable** from the canonical source, which is
  what makes it safe to be wrong: a projection you cannot rebuild is a second
  source of truth wearing a costume;
* nothing in the execution tree may import the graph, so no runtime decision can
  depend on it.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
SRC = REPO_ROOT / "src" / "nexus_ai_agent"
GRAPH = SRC / "provenance" / "graph.py"

#: Reaching any of these would make the graph an authority, not a projection.
FORBIDDEN_ROOTS = {
    "nexus_ai_agent.jobs",
    "nexus_ai_agent.creative.studio",
    "nexus_ai_agent.creative.spine",
    "nexus_ai_agent.creative.packs",
    "nexus_ai_agent.creative.rendering",
    "nexus_ai_agent.adapters",
    "nexus_ai_agent.bot",
    "nexus_ai_agent.llm",
    "nexus_ai_agent.tools",
}

#: Third-party imports: the graph must run on the bare interpreter + stdlib.
FORBIDDEN_THIRD_PARTY = {"sqlmodel", "sqlalchemy", "pydantic", "alembic", "langgraph", "telegram"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module)
    return roots


def test_the_graph_module_exists_and_is_pure_stdlib() -> None:
    assert GRAPH.is_file(), "provenance/graph.py is the Wave 3 deliverable"
    roots = _imports(GRAPH)
    offenders = {r for r in roots if r.split(".")[0] in FORBIDDEN_THIRD_PARTY}
    assert not offenders, f"the graph must stay stdlib-only, found {offenders}"


def test_the_graph_never_reaches_an_execution_or_authority_module() -> None:
    roots = _imports(GRAPH)
    offenders = {
        root
        for root in roots
        if any(
            root == forbidden or root.startswith(f"{forbidden}.") for forbidden in FORBIDDEN_ROOTS
        )
    }
    assert not offenders, (
        f"provenance/graph.py must be a projection, not a write path; it reached {offenders}"
    )


def test_no_runtime_module_depends_on_the_graph() -> None:
    """A projection nothing reads at runtime cannot become an authority.

    Anything that needs the graph must go through the provenance package's public
    surface; a direct ``provenance.graph`` import from the execution tree would
    couple a decision to the projection.
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path == GRAPH or path.parent.name == "provenance":
            continue
        for root in _imports(path):
            if root == "nexus_ai_agent.provenance.graph" or root.startswith(
                "nexus_ai_agent.provenance.graph."
            ):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"runtime modules must not import the graph directly: {offenders}"


def test_the_declared_model_is_closed_and_documented() -> None:
    """The node/relation vocabulary is a contract, so it is checked, not implied."""
    from nexus_ai_agent.provenance import LINEAGE_CHAIN, NODE_KINDS, RELATIONS

    assert set(NODE_KINDS) == {
        "PROJECT",
        "INTENT",
        "REVISION",
        "DECISION",
        "PLAN",
        "COMMAND",
        "EXECUTION",
        "ARTIFACT",
        "EVIDENCE",
        "VERIFICATION",
        "PASSPORT",
        "ACTOR",
    }
    assert "ARTIFACT_DEPENDS_ON" not in RELATIONS, (
        "ARTIFACT_DERIVED_FROM already expresses this; two names for one fact is how a "
        "graph acquires contradictory truths"
    )
    for relation, direction in LINEAGE_CHAIN:
        assert relation in RELATIONS
        assert direction in ("in", "out")


def test_every_lineage_relation_is_traversable_in_the_declared_direction() -> None:
    """A chain relation whose direction is wrong answers 'unknown' forever."""
    from nexus_ai_agent.provenance import LINEAGE_CHAIN, CreativeGraph

    graph = CreativeGraph(":memory:")
    identities = {
        "PROJECT": {"slug": "demo"},
        "INTENT": {"n": 1},
        "REVISION": {"n": 1},
        "PLAN": {"n": 1},
        "COMMAND": {"n": 1},
        "EXECUTION": {"n": 1},
        "ARTIFACT": {"n": 1},
    }
    ids = {
        kind: graph.put_node(kind, identity).node.node_id for kind, identity in identities.items()
    }
    order = ["PROJECT", "INTENT", "REVISION", "PLAN", "COMMAND", "EXECUTION", "ARTIFACT"]
    by_relation = dict(LINEAGE_CHAIN)
    for parent, child in zip(order, order[1:], strict=False):
        relation = next(
            (r for r, direction in LINEAGE_CHAIN if direction == "in" and r.endswith(child)),
            None,
        )
        relation = (
            relation
            or {
                ("PROJECT", "INTENT"): "PROJECT_HAS_INTENT",
                ("INTENT", "REVISION"): "INTENT_COMPILED_TO_REVISION",
                ("REVISION", "PLAN"): "REVISION_HAS_PLAN",
                ("PLAN", "COMMAND"): "PLAN_EMITS_COMMAND",
                ("COMMAND", "EXECUTION"): "COMMAND_EXECUTED_AS",
                ("EXECUTION", "ARTIFACT"): "EXECUTION_PRODUCED_ARTIFACT",
            }[(parent, child)]
        )
        graph.put_edge(relation, ids[parent], ids[child])
        assert by_relation[relation] == "in", (
            f"{relation} must be walked inbound from the artifact side"
        )
    lineage = graph.lineage(ids["ARTIFACT"])
    assert lineage["complete"] is True
    assert [n["kind"] for level in lineage["levels"] for n in level["found"]] == [
        "EXECUTION",
        "COMMAND",
        "PLAN",
        "REVISION",
        "INTENT",
        "PROJECT",
    ]


def test_the_graph_is_rebuildable_from_the_canonical_source() -> None:
    """The property that keeps a projection from becoming a second truth."""
    import tempfile

    from nexus_ai_agent.provenance import CreativeGraph
    from nexus_ai_agent.provenance.journal import CausalJournal
    from nexus_ai_agent.provenance.models import CausalEvent, EventKind

    with tempfile.TemporaryDirectory() as tmp:
        journal = CausalJournal(Path(tmp) / "journal.sqlite")
        for kind, attempt, status, digest in (
            (EventKind.JOB_ENQUEUED, None, "pending", None),
            (EventKind.JOB_RESERVED, 1, "processing", None),
            (EventKind.JOB_COMPLETED, 1, "completed", "sha256:x"),
        ):
            journal.append(
                CausalEvent(
                    kind=kind,
                    job_id="job-r",
                    job_type="slideshow",
                    attempt=attempt,
                    status=status,
                    result_digest=digest,
                )
            )
        records = journal.all_records()
        first = CreativeGraph(Path(tmp) / "a.sqlite")
        first.rebuild_from_journal(records)
        second = CreativeGraph(Path(tmp) / "b.sqlite")
        second.rebuild_from_journal(records)
        assert first.counts() == second.counts(), "rebuild must be reproducible"
        assert first.integrity_check().ok and second.integrity_check().ok

        replaced = CreativeGraph(Path(tmp) / "a.sqlite")
        before = replaced.counts()
        replaced.rebuild_from_journal(records, replace=True)
        after = replaced.counts()
        assert after["nodes"] == before["nodes"], "a from-scratch rebuild must not lose nodes"
        assert after["history"] == before["history"], "or silently grow the history log"
