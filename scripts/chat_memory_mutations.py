#!/usr/bin/env python3
"""Shadow-tree mutation harness for the Chat→Memory reachability contract.

WHY THIS EXISTS
---------------
The fix for the reachability gap is one line in
``src/nexus_ai_agent/orchestration/graph.py`` — a file this repository's
governance currently leases to ``task-202-memory-write-observability``
(branch ``arena/01a0e907-nexus-ai-agent``, PR #119).  The lease is not
bypassed here, and neither is the file written to.

Instead the harness proves the fix **out of tree**:

1. copy ``src/`` into a temporary directory;
2. edit the copy;
3. run the contract suite with ``PYTHONPATH`` pointing at the copy, which
   shadows the editable install (a ``.pth`` entry appends to ``sys.path``
   *after* ``PYTHONPATH``, so the copy wins);
4. record which tests xfail, which xpass, and which pass.

The real ``src/`` is never modified — the working tree stays byte-identical
to ``git status`` before and after a run.

WHAT IT PROVES
--------------
* **M1 (the H1 patch)** turns the reachability tripwires green.  This is the
  deliverable the lease owner inherits: a patch that is verified, not merely
  proposed.
* **M2–M5** are mutants of that patch.  A guard that cannot fail is not a
  guard, so each mutant has to push a named test back into ``xfail``/``FAILED``.
  The kill table is printed.

Usage::

    python scripts/chat_memory_mutations.py          # full run
    python scripts/chat_memory_mutations.py --json   # machine-readable

Exit code 0 iff the H1 patch behaved as claimed and every mutant was killed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
SUITE = "tests/unit/test_chat_memory_reachability.py"

GRAPH = "nexus_ai_agent/orchestration/graph.py"
LONG_TERM = "nexus_ai_agent/memory/long_term.py"
PERSONALITY = "nexus_ai_agent/personality/engine.py"
HANDLERS = "nexus_ai_agent/bot/handlers.py"

# ── the H1 patch itself ───────────────────────────────────────────────────── #
# ``route_intent`` currently forwards every non-task, non-memory intent
# straight to ``route_persona``.  The chat path therefore never performs a
# memory read, even though the persona agents already render
# ``memory_context`` and the writer already stores every turn.
H1_BEFORE = """    def route_intent(state: NexusState) -> str:
        intent = state.get("intent", "chat")
        if intent == "task":
            return "memory_reader_task"
        if intent == "memory":
            return "memory_reader_chat"
        return "route_persona"
"""

H1_AFTER = """    def route_intent(state: NexusState) -> str:
        intent = state.get("intent", "chat")
        if intent == "task":
            return "memory_reader_task"
        # H1: every remaining intent is a conversational turn, and a
        # conversational turn is exactly the one that asks "what did I tell
        # you?".  Route it through the reader before the persona agent.
        return "memory_reader_chat"
"""


@dataclass
class Outcome:
    passed: list[str] = field(default_factory=list)
    xfailed: list[str] = field(default_factory=list)
    xpassed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def detected(self) -> list[str]:
        """Tests that reported the defect: xfail *or* failure."""
        return self.xfailed + self.failed

    def summary(self) -> str:
        return (
            f"passed={len(self.passed)} xfailed={len(self.xfailed)} "
            f"xpassed={len(self.xpassed)} failed={len(self.failed)}"
        )


def _shade() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="nexus-chat-memory-"))
    shutil.copytree(
        SRC,
        tmp / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return tmp


def _edit(tree: Path, rel: str, before: str, after: str, count: int = 1) -> None:
    """Exact, loud replacement.

    A silent no-op here would turn the whole harness into a rubber stamp, so
    a drifted anchor is a hard error rather than a skipped mutation.
    """
    path = tree / "src" / rel
    text = path.read_text(encoding="utf-8")
    found = text.count(before)
    if found < count:
        raise SystemExit(
            f"ANCHOR DRIFT: {rel} contains the expected block {found}x, needed "
            f"{count}x.\nThe harness must be re-based on the current source; it "
            "will not guess."
        )
    path.write_text(text.replace(before, after, count), encoding="utf-8")


_LINE = re.compile(r"^(XFAIL|XPASS|PASSED|FAILED)\s+(tests/\S.*?)(?:\s+-\s+.*)?$")

#: Tripwires the one-line H1 wiring is *not* expected to satisfy.  Reachability,
#: typed failure and group scoping are three separate changes; naming the
#: exceptions here keeps the M1 verdict honest instead of quietly redefining
#: "done", and keeps the report honest about what H1 alone does not buy.
EXPECTED_STILL_RED = {
    "test_an_unavailable_backend_is_not_reported_as_a_successful_empty_recall": (
        "LAW 11 — needs a typed retrieval outcome, a second change (task-215)"
    ),
    "test_a_group_member_cannot_recall_another_members_memory": (
        "task-210 — needs a product decision on group vs per-user scoping"
    ),
}


def run_suite(tree: Path) -> Outcome:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tree / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", SUITE, "-q", "--tb=no", "-p", "no:cacheprovider", "-rA"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    out = Outcome()
    for line in proc.stdout.splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        kind, nodeid = m.group(1), m.group(2)
        name = nodeid.split("::", 1)[1]
        {
            "PASSED": out.passed,
            "XFAIL": out.xfailed,
            "XPASS": out.xpassed,
            "FAILED": out.failed,
        }[kind].append(name)
    if not (out.passed or out.xfailed or out.xpassed or out.failed):
        raise SystemExit(
            "could not parse pytest output — the harness is blind, refusing to "
            f"report a result.\nSTDOUT:\n{proc.stdout[-4000:]}\nSTDERR:\n{proc.stderr[-2000:]}"
        )
    return out


# ── mutants ──────────────────────────────────────────────────────────────── #
def apply_h1(tree: Path) -> None:
    _edit(tree, GRAPH, H1_BEFORE, H1_AFTER)


MUTANTS: list[dict] = [
    {
        "id": "M2",
        "name": "read dropped",
        "why": "the H1 wiring is reverted at the reader, so chat is routed but nothing is fetched",
        "must_detect": ["test_a_recall_question_delivers_stored_memory_to_the_model"],
        "patch": lambda t: _edit(
            t,
            GRAPH,
            """    async def memory_reader_chat_node(state: NexusState) -> NexusState:
        return await _memory_reader(long_term_memory, state)
""",
            """    async def memory_reader_chat_node(state: NexusState) -> NexusState:
        return state
""",
        ),
    },
    {
        "id": "M3",
        "name": "thread filter bypassed",
        "why": "the SQL scope on thread_id is dropped — one user's memory can answer for another",
        # The authoritative detector is the store-level one.  The two
        # end-to-end leak tests are deliberately NOT listed: with top_k=3 and
        # a lane-dependent ordering they can hide a widened filter behind a
        # lucky ranking, which is why test_a_read_never_returns_another_threads_memories
        # exists.
        "must_detect": ["test_a_read_never_returns_another_threads_memories"],
        "patch": lambda t: [
            # the recency lane, used both on the no-vec fast path and as the
            # exception fallback — both copies must lose their scope
            _edit(
                t,
                LONG_TERM,
                "SELECT content FROM memories WHERE thread_id=? ORDER BY id DESC LIMIT ?",
                "SELECT content FROM memories ORDER BY id DESC LIMIT ?",
                count=2,
            ),
            # the dense lane
            _edit(
                t,
                LONG_TERM,
                "                SELECT content FROM memories\n"
                "                WHERE thread_id=?\n"
                "                ORDER BY vec_distance_cosine(embedding, ?) ASC\n",
                "                SELECT content FROM memories\n"
                "                ORDER BY vec_distance_cosine(embedding, ?) ASC\n",
            ),
        ],
    },
    {
        "id": "M4",
        "name": "failure no longer contained",
        "why": "a dead memory backend takes the whole conversation down with it",
        "must_detect": ["test_an_unavailable_backend_never_aborts_the_conversation"],
        "patch": lambda t: _edit(
            t,
            GRAPH,
            """    try:
        results = await long_term_memory.search(state["thread_id"], last, top_k=3)
        state["memory_context"] = await long_term_memory.format_context(results)
    except Exception:
        state.setdefault("memory_context", "")
    return state
""",
            """    results = await long_term_memory.search(state["thread_id"], last, top_k=3)
    state["memory_context"] = await long_term_memory.format_context(results)
    return state
""",
        ),
    },
    {
        "id": "M6",
        "name": "every user collapsed into one thread",
        "why": "the Telegram entry point stops deriving thread_id from the chat",
        "must_detect": ["test_the_real_entry_point_threads_the_callers_identity"],
        "patch": lambda t: _edit(
            t,
            HANDLERS,
            '"thread_id": f"tg:{_chat_id(update)}",',
            '"thread_id": "tg:everyone",',
        ),
    },
    {
        "id": "M7",
        "name": "group scoping silently switched to per-user",
        "why": "thread_id derived from user_id — the plausible wrong fix for task-210",
        "must_detect": ["test_thread_id_is_derived_from_the_chat_alone_not_the_user"],
        "patch": lambda t: _edit(
            t,
            HANDLERS,
            '"thread_id": f"tg:{_chat_id(update)}",',
            '"thread_id": f"tg:{_user_id(update)}",',
        ),
    },
    {
        "id": "M5",
        "name": "context fetched but never rendered",
        "why": "the memory is read and then dropped on the floor before the prompt",
        "must_detect": [
            "test_persona_agents_render_memory_context_into_the_system_prompt",
            "test_a_recall_question_delivers_stored_memory_to_the_model",
        ],
        "patch": lambda t: _edit(
            t,
            PERSONALITY,
            '        mem = f"\\nMemory:\\n{memory_context}" if memory_context else ""',
            '        mem = ""',
        ),
    },
]


# ═══════════════════════════════════════════════════════════════════════════ #
# U1 — what is the chat-path retrieval lane actually worth, on this base?
# ═══════════════════════════════════════════════════════════════════════════ #
#: Paraphrase pairs.  Chosen so the *query* shares almost no content words with
#: the stored sentence: the point is to separate "the lane understands the
#: question" from "the lane matched a keyword", which a keyword-friendly corpus
#: (the one W2 used) cannot do.
U1_PAIRS: list[tuple[str, str]] = [
    ("The R2 blob tier stores database backups.", "Where are our database snapshots kept?"),
    ("The user named the project Aurora Lantern.", "What was my project called again?"),
    ("Deployments run on Sunday nights.", "When does the release job fire?"),
    ("The primary Postgres lives on Neon.", "Which managed database do we use?"),
    ("Telegram is the only supported chat surface.", "Which messaging platform is supported?"),
    ("We keep secrets in AWS Secrets Manager.", "How are credentials stored?"),
    ("The render lane shells out to ffmpeg.", "What binary encodes the video?"),
    ("Nightly DB backup writes to Cloudflare R2.", "How is the nightly backup handled?"),
]
U1_DISTRACTORS = 16
U1_TOP_K = 3


def _lexical_overlap(a: str, b: str) -> float:
    wa = set(re.findall(r"[a-z]+", a.lower()))
    wb = set(re.findall(r"[a-z]+", b.lower()))
    return len(wa & wb) / max(1, len(wa | wb))


def _u1_lane(use_vector: bool) -> dict:
    import asyncio

    from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
    from nexus_ai_agent.memory.long_term import LongTermMemory

    async def run() -> dict:
        llm = FakeLLMProvider()
        mem = LongTermMemory(":memory:", llm)
        mem._conn_()
        # Force the lane.  ``_use_vec`` is what ``search`` branches on, and it
        # is environment-dependent — sqlite-vec may or may not be installed —
        # so a single unforced run would not be reproducible across machines.
        mem._use_vec = use_vector
        thread = "u1"
        for doc, _ in U1_PAIRS:
            await mem.store(thread, doc)
        for i in range(U1_DISTRACTORS):
            await mem.store(thread, f"Unrelated note number {i}.")

        hits, rr, overlap = [], [], []
        for doc, query in U1_PAIRS:
            results = await mem.search(thread, query, top_k=U1_TOP_K)
            ranks = [i for i, r in enumerate(results) if r == doc]
            hits.append(1 if ranks else 0)
            rr.append(1.0 / (ranks[0] + 1) if ranks else 0.0)
            overlap.append(_lexical_overlap(doc, query))

        corpus = len(U1_PAIRS) + U1_DISTRACTORS
        return {
            "lane": "dense (vec_distance_cosine)" if use_vector else "recency (ORDER BY id DESC)",
            "queries": len(hits),
            "corpus": corpus,
            "top_k": U1_TOP_K,
            "mean_lexical_overlap": round(sum(overlap) / len(overlap), 3),
            "hit_at_k": round(sum(hits) / len(hits), 3),
            "mrr": round(sum(rr) / len(rr), 3),
            "chance_hit_at_k": round(U1_TOP_K / corpus, 3),
        }

    return asyncio.run(run())


def run_u1(json_out: bool = False, repeat: int = 1) -> int:
    """U1 evaluation.

    ``repeat`` deliberately re-runs in **fresh subprocesses**.  ``str.__hash__``
    is salted once per interpreter (``PYTHONHASHSEED``), and
    ``FakeLLMProvider.embed`` seeds ``random.Random(hash(text))`` — so repeated
    calls inside one process are identical by construction and would report a
    reproducibility that does not exist.  The variance that matters is between
    runs of the program, which is what CI and every operator actually sees.
    """
    if repeat > 1 and not json_out:
        samples: dict[str, list[float]] = {"recency": [], "dense": []}
        base = _u1_lane(False)
        for _ in range(repeat):
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--u1", "--json"],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            for row in json.loads(proc.stdout):
                key = "dense" if row["lane"].startswith("dense") else "recency"
                samples[key].append(row["hit_at_k"])
        print("=" * 78)
        print("U1 — paraphrase retrieval on this base (LongTermMemory, FakeLLM embedder)")
        print("=" * 78)
        print(f"\nqueries {base['queries']} · corpus {base['corpus']} · top_k {base['top_k']}")
        print(f"mean lexical overlap query↔target : {base['mean_lexical_overlap']}")
        print(
            f"chance hit@{base['top_k']} ({base['top_k']}/{base['corpus']})"
            f" : {base['chance_hit_at_k']}"
        )
        for key, label in (
            ("recency", "recency (ORDER BY id DESC)"),
            ("dense", "dense (vec_distance_cosine)"),
        ):
            hits = samples[key]
            mean = sum(hits) / len(hits)
            print(f"\nlane: {label}   [{repeat} independent processes]")
            print(f"  hit@{base['top_k']} samples : {hits}")
            print(
                f"  mean {round(mean, 3)}  range [{min(hits)}, {max(hits)}]  → "
                f"{'REPRODUCIBLE' if min(hits) == max(hits) else 'NOT REPRODUCIBLE'}"
            )
            if min(hits) != max(hits):
                print(
                    "    cause: FakeLLMProvider.embed seeds random.Random(hash(text)) and\n"
                    "    str.__hash__ is salted per process, so this lane's score is a random\n"
                    "    variable. A regression baseline pinned on it would pin noise."
                )
        print(
            "\nNOTE: this measures the retrieval lane on this branch only. It is not a\n"
            "quality claim about PR #121's hybrid retriever, which is unmerged and\n"
            "under that PR's live lease."
        )
        print("=" * 78)
        return 0

    rows = [_u1_lane(False), _u1_lane(True)]
    if json_out:
        print(json.dumps(rows, indent=2))
        return 0
    print("=" * 78)
    print("U1 — paraphrase retrieval on this base (LongTermMemory, FakeLLM embedder)")
    print("=" * 78)
    for r in rows:
        verdict = (
            "ABOVE chance"
            if r["hit_at_k"] > r["chance_hit_at_k"]
            else ("AT chance" if r["hit_at_k"] == r["chance_hit_at_k"] else "AT/BELOW chance")
        )
        print(f"\nlane: {r['lane']}")
        print(f"  queries {r['queries']} · corpus {r['corpus']} · top_k {r['top_k']}")
        print(f"  mean lexical overlap query↔target : {r['mean_lexical_overlap']}")
        print(f"  hit@{r['top_k']} : {r['hit_at_k']}    MRR@{r['top_k']} : {r['mrr']}")
        print(
            f"  chance hit@{r['top_k']} ({r['top_k']}/{r['corpus']})"
            f" : {r['chance_hit_at_k']}  → {verdict}"
        )
    print(
        "\nNOTE: this measures the retrieval lane on this branch only. It is not a\n"
        "quality claim about PR #121's hybrid retriever, which is unmerged and\n"
        "under that PR's live lease.\n"
        "Use --repeat N to measure run-to-run variance across independent processes."
    )
    print("=" * 78)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--u1",
        action="store_true",
        help="run the U1 paraphrase-retrieval evaluation instead of the mutations",
    )
    ap.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="with --u1, re-measure each lane N times to expose run-to-run variance",
    )
    args = ap.parse_args()
    if args.u1:
        return run_u1(json_out=args.json, repeat=max(1, args.repeat))
    results: list[dict] = []
    failures = 0

    # ── baseline: the repository as it is ─────────────────────────────────── #
    tree = _shade()
    try:
        base = run_suite(tree)
    finally:
        shutil.rmtree(tree, ignore_errors=True)
    results.append(
        {
            "id": "M0",
            "name": "baseline (unmodified main)",
            "expectation": "reachability tripwires must be RED",
            "outcome": base.summary(),
            "detected": sorted(base.detected),
            "ok": len(base.xfailed) >= 5,
        }
    )
    if not results[-1]["ok"]:
        failures += 1

    # ── the H1 patch ──────────────────────────────────────────────────────── #
    tree = _shade()
    try:
        apply_h1(tree)
        fixed = run_suite(tree)
    finally:
        shutil.rmtree(tree, ignore_errors=True)
    results.append(
        {
            "id": "M1",
            "name": "H1 patch: chat/unknown route through memory_reader_chat",
            "expectation": (
                "every REACHABILITY tripwire turns green. Two invariants are "
                "excluded on purpose: the one-line wiring cannot satisfy them, "
                "and that is the finding, not a harness failure."
            ),
            "outcome": fixed.summary(),
            "still_detected": sorted(fixed.detected),
            "ok": not any(t not in EXPECTED_STILL_RED for t in fixed.detected),
        }
    )
    if not results[-1]["ok"]:
        failures += 1

    # ── mutants of the patch ──────────────────────────────────────────────── #
    for spec in MUTANTS:
        tree = _shade()
        try:
            apply_h1(tree)
            spec["patch"](tree)
            got = run_suite(tree)
        finally:
            shutil.rmtree(tree, ignore_errors=True)
        survivors = [t for t in spec["must_detect"] if not any(t in d for d in got.detected)]
        killed = not survivors
        if not killed:
            failures += 1
        results.append(
            {
                "id": spec["id"],
                "name": spec["name"],
                "why": spec["why"],
                "expectation": f"must re-detect: {', '.join(spec['must_detect'])}",
                "outcome": got.summary(),
                "killed": killed,
                "survivors": survivors,
                "also_detected": sorted(
                    d for d in got.detected if not any(t in d for t in spec["must_detect"])
                ),
            }
        )

    if args.json:
        print(json.dumps(results, indent=2))
        return 1 if failures else 0

    width = 78
    print("=" * width)
    print("CHAT→MEMORY REACHABILITY — shadow-tree mutation report")
    print("=" * width)
    print(f"repo     : {REPO}")
    print(f"suite    : {SUITE}")
    print("method   : src/ copied to a temp dir, patched there, run with PYTHONPATH")
    print("           shadowing the editable install. The real src/ is never written.")
    print()
    for r in results:
        mark = "OK  " if r.get("ok", r.get("killed")) else "FAIL"
        print(f"[{mark}] {r['id']}  {r['name']}")
        print(f"       {r['expectation']}")
        print(f"       {r['outcome']}")
        if "detected" in r:
            for t in r["detected"]:
                print(f"         detected: {t}")
        if "still_detected" in r:
            for t in r["still_detected"]:
                why = EXPECTED_STILL_RED.get(t, "UNEXPECTED — the fix should have moved this")
                print(f"         STILL RED after the fix: {t}")
                print(f"           → {why}")
        if "survivors" in r and r["survivors"]:
            for t in r["survivors"]:
                print(f"         SURVIVED (guard is too weak): {t}")
        print()

    print("=" * width)
    killed_n = sum(1 for r in results if r.get("killed"))
    print(f"H1 patch verified: {results[1]['ok']}   mutants killed: {killed_n}/{len(MUTANTS)}")
    print("=" * width)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
