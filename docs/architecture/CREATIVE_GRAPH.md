# Durable creative graph and lineage (law R21)

**Status:** living view · **Authority:** `src/nexus_ai_agent/provenance/graph.py` ·
**Enforcing tests:** `tests/unit/test_creative_graph.py`,
`tests/architecture/test_creative_graph_boundary.py`

---

## 1. What was missing

The repository already had three durable authorities, and this page adds none:

| Authority | What it owns | Law |
|---|---|---|
| `nexus_job_queue` row | the sole job-state / outcome truth | R14 |
| `nexus_causal_journal` | append-only, hash-chained record of committed transitions — *evidence, never authority* | D-0024 |
| `ArtifactPassport` | read-only, fail-closed projection that re-measures artifacts | R14, D-0024 |

None of them can answer, directly, the question the creative archive exists for:

> **Where did this artifact come from — and if its intent changes, what else is affected?**

The ledger can answer only by scanning every record; the queue row knows one job;
the passport knows one artifact. The **creative graph** is the durable, indexed,
**rebuildable projection** that turns that answer into a query.

## 2. The model

Twelve node kinds, eleven relations — and deliberately no more. A kind nobody
writes is a kind nobody can verify; two names for one fact is how a graph
acquires contradictory truths (which is why `ARTIFACT_DEPENDS_ON` does not exist:
`ARTIFACT_DERIVED_FROM` already says it).

```
PROJECT --PROJECT_HAS_INTENT--> INTENT --INTENT_COMPILED_TO_REVISION--> REVISION
REVISION --REVISION_HAS_PLAN--> PLAN --PLAN_EMITS_COMMAND--> COMMAND
COMMAND --COMMAND_EXECUTED_AS--> EXECUTION --EXECUTION_PRODUCED_ARTIFACT--> ARTIFACT
ARTIFACT --ARTIFACT_VERIFIED_BY--> VERIFICATION
ARTIFACT --ARTIFACT_HAS_PASSPORT--> PASSPORT
ARTIFACT --ARTIFACT_DERIVED_FROM--> ARTIFACT
DECISION --DECISION_AFFECTS--> *        ACTOR --ACTOR_AUTHORIZED--> *
```

`LINEAGE_CHAIN` records the direction each relation must be walked **from the
artifact's side**. The domain relations point downstream (an execution *produces*
an artifact), so reaching the execution from the artifact follows an **incoming**
edge. Getting this backwards does not crash — it answers "unknown" forever, which
is why `test_every_lineage_relation_is_traversable_in_the_declared_direction`
exists.

## 3. Identity: deterministic, stable, scoped, auditable

```
node_id = f"{kind}:{sha256(canonical_json(identity))}"
```

* **Scoped** by kind, so a `PROJECT` and an `ARTIFACT` sharing a name are
  different nodes.
* **Stable** across processes and restarts — it is a digest, not a rowid.
* **Auditable** — `identity_json` and `identity_digest` are stored, so the
  identity can be recomputed and checked (`GRAPH013`).

Creating the same logical state twice yields **one** node and reports
`unchanged`. Two runs cannot produce two contradictory truths about one thing.

## 4. History is appended, never rewritten

`nexus_graph_node_history` records every state a node has *established*, oldest
first. A changed payload:

1. updates the current row,
2. appends the new state to the history log,
3. increments `revision_count`.

The previous payload stays readable — `graph.history(node_id)` returns the whole
sequence. `invalidate` / `revoke` / `supersede` are **explicit** transitions
through `set_status`, each of which also appends a history row. And a node that
is already `invalidated` or `revoked` **refuses** a write rather than being
silently revived (`GraphStateError`). There is no `UPDATE history = new_truth`
path in this module.

## 5. It is a projection, and that is enforced

| Gate | What it proves |
|---|---|
| `test_the_graph_never_reaches_an_execution_or_authority_module` | `graph.py` imports no jobs/studio/spine/packs/rendering/adapters/bot/llm/tools module — it cannot be a write path |
| `test_no_runtime_module_depends_on_the_graph` | nothing in the execution tree imports the graph, so no runtime decision can depend on a projection |
| `test_the_graph_is_rebuildable_from_the_canonical_source` | two graphs rebuilt from the same ledger are identical, integrity-clean, and `replace=True` reproduces the same node **and** history counts |
| `test_the_graph_module_exists_and_is_pure_stdlib` | stdlib + its own package only (R1b's discipline) |

A projection you cannot rebuild is a second source of truth wearing a costume.
Rebuildability is the property that keeps this honest.

`rebuild_from_journal` is **idempotent by construction**: the transitions belong
to the ledger, so the projection collapses each node identity to the state the
*last* record establishes and writes it once. Replaying the ledger yields the
same graph byte for byte instead of re-archiving every intermediate transition.
Every projected node carries `source = "causal_journal"` and the `source_seq` of
the ledger record it came from.

Two further properties are enforced, because a rebuild that can corrupt the
projection is worse than no rebuild:

* **Terminal state survives a rebuild.** A node a caller marked `invalidated` or
  `revoked` is *skipped*, never rewritten: that decision is not in the ledger, so
  the ledger cannot license undoing it. Each skip is reported in the result's
  `skipped_terminal` list with `kind`, `node_id` and `status`. `replace=True`
  keeps terminal nodes too — a from-scratch rebuild of the *projection* is not a
  licence to revive a revoked truth. Without this, one caller invalidation made
  the whole rebuild throw `GraphStateError` partway through, leaving earlier
  records projected and later ones not.
* **A rebuild is atomic.** The delete (in `replace` mode) and every node and edge
  write happen inside one `BEGIN IMMEDIATE`. Nothing is observable until it
  commits, so an observer either sees the previous healthy projection or the new
  generation whole — never a half-built graph that looks healthy. This is why
  `put_node`/`put_edge` are split into `_put_node_tx`/`_put_edge_tx`: the rebuild
  drives those on a caller-owned connection, and the public methods are unchanged
  wrappers. Crash-injection tests reopen the file from a separate object at four
  injection points and require the previous projection plus a passing
  `integrity_check`.

`GRAPH030` ("every node has history") answers from a membership set built once,
so the check is O(nodes + history) rather than a history scan per node.

### What the ledger cannot witness is declared, not guessed

```
PROJECT / INTENT / REVISION / PLAN / COMMAND / DECISION / ACTOR → UNMIGRATED
```

The ledger observes durable *execution* facts only. Intent, revision, plan and
command belong to the spine and the `CommandBus`, which the ledger never sees.
`rebuild_from_journal` therefore reports those levels `UNMIGRATED` with the
reason, and names its canonical source
(`nexus_causal_journal` + `nexus_job_queue`). No historical datum is invented.

## 6. Lineage and invalidation

```python
graph.lineage(artifact_id)
# -> {"artifact": …, "attachments": {ARTIFACT_VERIFIED_BY: […], ARTIFACT_HAS_PASSPORT: […]},
#     "levels": [EXECUTION, COMMAND, PLAN, REVISION, INTENT, PROJECT],
#     "complete": True, "deepest_kind": "PROJECT"}
```

A broken chain reports **where it stopped** instead of guessing: `complete`
becomes `False`, the empty level is present, and `deepest_kind` names the last
kind that actually resolved. Invalidating a `REVISION` mid-chain makes
`lineage()["deepest_kind"] == "PLAN"` — a truthful partial answer, not a silent
one.

```python
graph.invalidation_candidates(intent_id)
# -> {"affected_count": 7,
#     "by_kind": {"REVISION": […], "PLAN": […], "COMMAND": […], "EXECUTION": […],
#                 "ARTIFACT": […], "VERIFICATION": […], "PASSPORT": […]}}
```

Traversal is breadth-first with a cycle guard and a depth limit, ordered by
`(depth, node_id)`, so it is deterministic and cannot hang. Inactive nodes are
excluded by default (they are not live descendants) but remain reachable with
`include_inactive=True` — the past stays readable.

## 7. Recovery

| Operation | Semantics |
|---|---|
| `backup(path)` | SQLite **online backup API**, not a file copy — consistent under concurrent readers. Captures the **whole file**, journal included if shared |
| `restore(path, *, whole_database=False)` | **whole-file** replacement — see the scope contract below |
| `integrity_check()` | `GRAPH010`–`GRAPH013` node kind/status/payload/identity digests, `GRAPH020`–`GRAPH022` edge relation/endpoints/digest, `GRAPH030` every node has history |
| `rebuild_from_journal(records, replace=True)` | from-scratch rebuild in **one transaction**; terminal nodes are skipped and reported in `skipped_terminal` |

`restore` takes the in-process writer lock. For `:memory:`, it backs up into the
shared connection (there is no path on disk to reopen); for file-backed
databases, it uses a dedicated destination connection.
`Connection.backup` cannot target a connection inside an open transaction, which
is why it does not use the `BEGIN IMMEDIATE` wrapper.

### The restore scope contract

The SQLite backup API cannot target a subset of tables, so `backup`/`restore`
operate on the **whole database file** — not just the graph projection. That is
coherent for disaster recovery (one backup, one restore, journal included) and
dangerous if aimed at the wrong file: the projection is designed to share its
file with the causal journal, which is the authority the graph is rebuilt FROM.
A projection restore must never overwrite the authority by accident —
reproduced before the guard: `graph.restore(backup)` on a shared file silently
rewound the ledger from 6 events to 3.

The guard therefore enforces:

- **Source validation** — the source must contain the graph schema tables; a
  foreign file is refused.
- **Destination scope** — if the destination file holds any table beyond the
  graph's own (`GRAPH_TABLES`), `restore` refuses by default and names the
  foreign tables. `whole_database=True` is the explicit acknowledgement that
  the caller owns the entire file and wants it all replaced.
- A graph-only file restores with no acknowledgement, as before.

### Concurrency contract

Every read and write takes the in-process lock. On `:memory:` there is one
shared connection, so without this a reader sampling mid-rebuild would see the
writer's uncommitted rows — reproduced before the fix: a reader observed
`{nodes: 120, edges: 119, history: 121}`, a state that is neither the old nor
the new committed graph. The lock is reentrant because composite reads
(`traverse`, `lineage`, `integrity_check`) hold it across the leaf readers they
call; a multi-step read therefore cannot straddle a rebuild either.

Cross-process write safety comes from **SQLite's write lock**, not the in-process
mutex: `put_node`/`put_edge` run inside `BEGIN IMMEDIATE`, so a second process
waits out the busy timeout and then sees the committed state.
`test_two_processes_can_write_the_same_graph` runs two real processes against one
file and asserts no lost write and no duplicate.

## 8. Passport integration

The passport stays the artifact's authority. The graph connects to it rather than
replacing it:

* `ARTIFACT_HAS_PASSPORT` links the artifact node to its passport node, and
  `lineage()["attachments"]` returns it;
* the passport's own evidence (artifact digest, verification, capability, actor,
  policy) remains where it is — the graph stores the **link** and the identity,
  not a second copy of the judgement;
* `rebuild_from_journal` creates the `PASSPORT` node from the completion record's
  `result_digest`, which is the same identity the passport builder uses.

No parallel passport is created, and `provenance/passport.py` is unchanged.

## 9. The acceptance question, answered by a different process

`test_the_graph_survives_a_closed_process` is the acceptance test:

1. process 1 builds `Project → Intent → Revision → Plan → Command → Execution →
   Artifact → Verification → Passport` and **exits**;
2. process 2 opens the same file and asks *"where did this artifact come from?"*;
3. it gets `complete: True`, `deepest_kind: "PROJECT"`, and the chain
   `EXECUTION ← COMMAND ← PLAN ← REVISION ← INTENT ← PROJECT`.

If the creative state were still only in memory, step 3 would have nothing to
answer with.

## 10. Honest limits

1. **Levels above `EXECUTION` are not yet populated from a canonical source.**
   The spine holds intent/revision/plan in memory per instance, and the
   `CommandBus` holds command state in memory. Until those are durable, the
   graph can only be *written* at those levels by a caller; `rebuild_from_journal`
   reports them `UNMIGRATED`. This is the real remaining gap, and it is named
   rather than papered over.
2. **No event sourcing was added.** The ledger already is an append-only
   hash-chained record; the graph is a relational projection over it. Adding a
   second event log would have created a second authority.
3. **No graph database.** The queries needed (typed edge traversal by relation,
   indexed by endpoint) are served by three indexed SQLite tables beside the
   ledger. Neo4j would have added an operational dependency with no measured
   query need.
4. **`DECISION` and `ACTOR` nodes have no writer yet.** They exist in the
   vocabulary because the domain has them; nothing records them today, and that
   is reported as `UNMIGRATED`, not faked.

Persian summary: گراف خلاق projection بازساختنیِ دفتر کل رویدادها است، نه یک
authority دوم. هویت deterministic است، تاریخچه فقط append می‌شود، و سؤال
«این artifact از کجا آمده؟» حتی بعد از بسته‌شدن process پاسخ مستند دارد.
سطوح بالاتر از EXECUTION هنوز UNMIGRATED هستند — صادقانه ثبت شده، نه حدس زده.
