# Chat→Memory Reachability Audit — task-205

> **One-line verdict:** a memory stored by this repository could **not** be reached by a
> user on the chat path — 4 of 5 real recall phrasings were answered with an empty memory
> context. The fix is **delivered** in this branch: it was deferred for 13 hours while the
> target file was leased, verified out of tree during that window, and landed the moment
> the lease lapsed.

| | |
|---|---|
| **Task** | `task-205-chat-memory-reachability-contract` (zone `chat-memory-reachability`) |
| **H1** | `task-206` (zone `orchestration-routing`) — **delivered**, see §5.2 |
| **Branch** | `arena/01a0eade-nexus-ai-agent` |
| **Base** | `main` @ `e5b326b2eaf691a638d030ad57acf1ce60016ef0` |
| **Board claim** | `8113996`, pushed before any code was written |
| **Date** | 2026-09-29 |
| **Gates** | **deferred to the gates owner** per `AGENTS.md` §1.4 |

---

## 1. BEFORE — the live repository, not a report

Everything below was read from the repository, not from a handoff document.

```
git rev-parse HEAD                     → e5b326b2eaf691a638d030ad57acf1ce60016ef0
git rev-parse main origin/main          → e5b326b2eaf691a638d030ad57acf1ce60016ef0
git status                             → clean
open PRs                               → 39
PR #121 (W2 memory truth)              → OPEN, MERGEABLE, head arena/01a0e9a1, 2 commits
PR #119 (shell + memory observability)  → OPEN (draft), head arena/01a0e907
```

**Live leases** (every `status: active` claim across all 39 open PR boards, with TTL
expiry computed at 2026-09-29T02:00Z):

| PR | Task | Exclusive path that matters | Expires |
|---|---|---|---|
| #119 | `task-202-memory-write-observability` | **`src/nexus_ai_agent/orchestration/graph.py`** | 2026-09-29T17:51:40Z |
| #121 | `task-203-memory-retrieval-truth` | `memory/`, `retrieval/`, **`orchestration/router.py`**, all `agents/*` | 2026-09-29T21:39:02Z |
| #119 | `task-201-shell-trust-plane-grammar` | `tools/system_shell.py` | 2026-09-29T17:34:36Z |
| #119 | `task-203-redaction-fail-closed` | `infrastructure/observability/redaction.py` | 2026-09-29T19:04:02Z |
| #119 | `task-204-conversation-index-observability` | `features/conversation_store.py` | 2026-09-29T19:15:01Z |

**PR #121 is unmerged.** Its `orchestration/router.py::should_read_memory` policy, its
`retrieval/` package and its typed evidence **do not exist on this branch**. Main's
retrieval is still the pre-W2 implementation.

---

## 2. THE CALL GRAPH, traced from the entry point to the prompt

`cli.py::run_bot` → `orchestration.graph.compile_graph` → LangGraph. The Telegram surface
reaches the same graph from `bot/handlers.py::on_message` (`graph.ainvoke`).

```
  user message  (bot/handlers.py::on_message  →  _base_state, memory_context: "")
        │
        ▼
  _router_node ── classify_intent() ── select_persona()          graph.py:19
        │
        ▼
  route_intent ────────────────────────────────────────────►   graph.py:216
        ├── intent == "task"   → memory_reader_task → planner → executor ──┐
        ├── intent == "memory" → memory_reader_chat ────────────────────────┤
        └── anything else      → route_persona  ◄── NO MEMORY READ ────────┐ │
                                                                          │ │
  route_persona → phi_agent / qwen_agent / gemma_agent                    │ │
        │        └─ PersonalityEngine.build_system_prompt(memory_context)  │ │
        ▼                                                                  │ │
  moderation (PhiAgent.moderate) ◄────────────────────────────────────────┘ │
        ▼
  memory_writer  ── stores EVERY turn, whatever the intent ────────────────
        ▼
       END
```

| stage | owner file | reads memory | writes memory | thread-scoped | tested |
|---|---|---|---|---|---|
| Telegram entry | `bot/handlers.py:907-911` | no | no | `thread_id = f"tg:{chat_id}"` | indirectly |
| intent + persona | `graph.py:19 _router_node` | no | no | n/a | `test_router*.py` |
| **routing** | **`graph.py:216 route_intent`** | **no** | no | n/a | **no** |
| memory read | `graph.py:29 _memory_reader` | **yes** (`search` + `format_context`) | no | yes — `state["thread_id"]` | `test_graph_memory.py` (leased) |
| prompt render | `personality/engine.py:115` | no | no | n/a | no |
| persona agents | `agents/{phi,qwen,gemma}_agent.py` | no | no | n/a | `test_agents.py` |
| **memory write** | `graph.py:149 _memory_writer` | no | **yes** | yes | `test_graph_memory.py` (leased) |
| dead code | `graph.py:39 _chat_agent` | — | — | — | never wired (H2) |

**Two things follow immediately.**

1. **The read asymmetry.** `_memory_writer` runs for *every* intent. `route_intent` reads
   for *two*. A `chat` turn is persisted and then, by construction, unreachable.
2. **The sink is not the problem.** `build_system_prompt` already appends
   `memory_context`, and all three persona agents already pass `state["memory_context"]`
   into it. No work on the agents or the personality engine can fix the symptom — this
   suite proves it (`test_persona_agents_render_memory_context_into_the_system_prompt`,
   green on main today).

---

## 3. ROOT CAUSE

```python
# src/nexus_ai_agent/orchestration/graph.py:216
def route_intent(state: NexusState) -> str:
    intent = state.get("intent", "chat")
    if intent == "task":
        return "memory_reader_task"
    if intent == "memory":
        return "memory_reader_chat"
    return "route_persona"  # ◄── chat and unknown land here
```

`classify_intent` returns `"memory"` only when the text contains a literal from
`MEMORY_KEYWORDS` (`remember`, `recall`, `what did`, `last time`, `my name is`,
`who am i`, and 11 Persian equivalents). A user asking *"What was my project called
again?"* contains none of them.

**Is the bypass deliberate?** There is no comment, no flag, no setting, and no test
asserting it. The graph has two identical memory-reader nodes wired to two intents —
the shape of a policy that was started and not finished, not one that was chosen.

---

## 4. REPRODUCTION (before any fix)

Five recall phrasings a real user would type, one shared store, one real compiled graph,
and an `LLMProvider` that records every system prompt it is handed. The fact exists
**only** in the store, never in the visible messages, so it can only appear in a prompt
if the reader actually ran.

```
Q: 'What was my project called again?'      intent='chat'    MEMORY NEVER REACHES LLM
Q: 'Where did we leave off?'                intent='chat'    MEMORY NEVER REACHES LLM
Q: 'Do you remember the name I gave my project?'  intent='memory'  REACHES LLM
Q: 'What was the thing I told you earlier?' intent='chat'    MEMORY NEVER REACHES LLM
Q: 'یادم هست اسم پروژه چی بود؟'              intent='chat'    MEMORY NEVER REACHES LLM

RESULT: 4 of 5 recall questions lost their memory
```

This is now a **committed, CI-enforced** artefact rather than a throwaway script:

```
python -m pytest tests/unit/test_chat_memory_reachability.py -rxX
→ 11 passed, 10 xfailed, 2 xpassed
```

**The hypothesis was not rejected.** 4/5 recall phrasings lose their memory, and the one
that works does so only because it happens to contain the word "remember".

---

## 5. WHY H1 WAS DEFERRED FIRST — AND HOW IT LANDED

`src/nexus_ai_agent/orchestration/graph.py` is the file the fix belongs in, and it is
under a **live lease**:

```
task-202-memory-write-observability · branch arena/01a0e907 (PR #119)
claimed 2026-09-28T17:51:40Z · ttl 24h · expires 2026-09-29T17:51:40Z
exclusive_paths: ["src/nexus_ai_agent/orchestration/graph.py",
                  "tests/unit/test_graph_memory.py"]
```

PR #119 carries an unmerged 25-line diff to that exact file (memory-write observability
in `_memory_writer`). Editing `route_intent` would have produced a text conflict with an
open PR — the precise outcome the board protocol exists to prevent.

### 5.1 A governance blind spot worth reporting

```
$ python scripts/agent_board.py check \
    --files "src/nexus_ai_agent/orchestration/graph.py,tests/unit/test_graph_memory.py" \
    --branch arena/01a0eade-nexus-ai-agent
no overlap — safe to proceed.          # exit 0
```

**That answer is wrong, and it is a defect in the tool, not a licence.** `cmd_check` reads
`.agents/board.json` from the *local working tree*. The live `task-202` and `task-203`
claims live on the unmerged boards of PR #119 and #121, so a main-based branch cannot see
them. Any agent starting from `main` gets a green check on files that are actively leased.

Live truth outranks the tool. The lease was honoured regardless of the exit code, and
**no governance file, test, or rule was modified to make H1 executable.**
Recommendation: `cmd_check` should union the local board with the boards of open PR head
branches (the same traversal `scripts/agent_board.py praudit` already performs) — filed as
`task-207`.

### 5.2 The lease lapsed, and the fix landed

At `2026-09-29T17:51:40Z` the 24 h TTL elapsed. Before touching the file, live state was
re-checked rather than assumed:

```
PR #119              state=OPEN draft=true  updated=2026-09-28T19:22:53Z  (22 h idle)
task-202 claim       status=active  claimed_at=2026-09-28T17:51:40Z  ttl=24h
                     → expires 2026-09-29T17:51:40Z   ← elapsed
                     released_at=null                            ← never renewed
origin/main          e5b326b2  (unchanged; already in this branch history)
cross-PR scan        41 open-PR boards → no live foreign lease on graph.py
```

`AGENTS.md` §1.5: *leases expire (24 h TTL); `show`/`next`/`gc` auto-release stale leases.*
The claim was never renewed and its PR had been idle for 22 hours, so the scope was
released by the protocol's own rule — not taken by force.

The sequence was therefore: **claim first** (`task-206`, zone `orchestration-routing`,
commit `13a33b9`, pushed), `check` → exit 0, cross-PR scan → CLEAR, *then* edit.

**Collision risk with PR #119 was measured, not hoped away.** A read-only three-way merge
of this branch against PR #119's head:

```
$ git merge-tree --write-tree HEAD origin/arena/01a0e907-nexus-ai-agent
Auto-merging .agents/board.json
CONFLICT (content): Merge conflict in .agents/board.json
```

`graph.py` merges **cleanly** — PR #119's hunks are at lines 9 and 165, this one at 212.
The only conflict is the coordination file, which `AGENTS.md` §6 resolves as *newest state
wins*.

### 5.3 The guards got stronger after the fix landed

While the fix was pending, the reachability tripwires were `xfail(strict=False)` so the
suite was green in both worlds. Now that the fix is **in**, that is the wrong shape: an
XPASS can be overlooked. Ten tripwires were promoted to **hard assertions**, so a
regression now turns CI red:

```
before the fix:  13 passed, 12 xfailed, 2 xpassed
after  the fix:  25 passed,  2 xfailed
```

The two remaining `xfail`s are honest and each names a *decision*, not a lease: `task-215`
(LAW 11) and `task-210` (group scope). The mutation harness confirms the promotion did not
weaken anything — the same six mutants now die as **hard failures** (15–17 `failed` where
they previously produced `xfailed`).

---

## 6. THE FIX — verified out of tree, then landed

The patch is four lines of diff in `graph.py::route_intent`:

```diff
     def route_intent(state: NexusState) -> str:
         intent = state.get("intent", "chat")
         if intent == "task":
             return "memory_reader_task"
-        if intent == "memory":
-            return "memory_reader_chat"
-        return "route_persona"
+        # H1: every remaining intent is a conversational turn, and a
+        # conversational turn is exactly the one that asks "what did I tell
+        # you?".  Route it through the reader before the persona agent.
+        return "memory_reader_chat"
```

**Design answers, settled before any edit:**

1. **Lowest-risk wiring point** — `route_intent`. It is a pure function of `state["intent"]`,
   it owns no I/O, and it is the single place that decides whether a reader runs. There is
   no smaller surface. A second reader inside a persona agent would create two owners for
   one responsibility and break the ONE-OWNER property this repository tests.
2. **Import boundary** — unchanged. `graph.py` already imports from
   `orchestration.router`; no new edge, no cycle, and `memory/` stays a leaf
   (`tests/architecture/test_memory_boundaries.py` untouched and green).
3. **User/thread isolation** — unchanged. The patch adds no query. `_memory_reader` scopes
   by `state["thread_id"]`, and the SQL scope is separately proven below.
4. **`task` / `memory` behaviour** — unchanged. Only the fall-through branch is rewritten;
   `memory_reader_task → planner → executor` keeps its own path, and
   `memory → memory_reader_chat` is the branch the patch generalises.

### 6.1 How it was verified without touching the leased file

`scripts/chat_memory_mutations.py` copies `src/` to a temp directory, patches the copy, and
runs the suite with `PYTHONPATH` pointed at the copy. A `setuptools` editable install adds
its path through a `.pth` file, which appends to `sys.path` **after** `PYTHONPATH`, so the
copy shadows the installed package. The real `src/` is never written.

```
$ python scripts/chat_memory_mutations.py
[OK  ] M0  baseline (unmodified main)              passed=13 xfailed=12 xpassed=2 failed=0
[OK  ] M1  H1 patch: chat/unknown → memory_reader_chat
                                                  passed=13 xfailed=2  xpassed=12 failed=0
         STILL RED after the fix: test_a_group_member_cannot_recall_another_members_memory
           → task-210 — needs a product decision on group vs per-user scoping
         STILL RED after the fix: test_an_unavailable_backend_is_not_reported_as_a_successful_empty_recall
           → LAW 11 — needs a typed retrieval outcome, a second change (task-215)
[OK  ] M2  read dropped                           passed=8  xfailed=13 xpassed=1 failed=5
[OK  ] M3  thread filter bypassed                 passed=9  xfailed=12 xpassed=2 failed=4
[OK  ] M4  failure no longer contained            passed=12 xfailed=2  xpassed=12 failed=1
[OK  ] M6  every user collapsed into one thread    passed=12 xfailed=2  xpassed=12 failed=1
[OK  ] M7  group scoping switched to per-user      passed=11 xfailed=2  xpassed=12 failed=2
[OK  ] M5  context fetched but never rendered     passed=8  xfailed=12 xpassed=2 failed=5

H1 patch verified: True   mutants killed: 6/6
```

**M1 leaves two tripwires red, and both are findings rather than harness noise.** The
one-line wiring buys *reachability only*. It does not type the failure (LAW 11,
`task-215`) and it does not change the memory *scope* (`task-210`). Listing them
explicitly in `EXPECTED_STILL_RED` is what keeps the M1 verdict honest: if a future
patch satisfies them, the harness says so, and if it satisfies neither while claiming
to close the gap, the table shows it.

`git status --porcelain src/` is empty after every run. **The lease was not touched.**

---

## 7. SECURITY PROOF — thread A ≠ thread B, user A ≠ user B

Wiring memory into the chat path is only safe if the fence holds. Four independent
invariants, none of which mocks the code under test:

| # | invariant | test | lane-independent? |
|---|---|---|---|
| 1 | thread A's memory never reaches thread B's prompts or answer | `test_thread_a_memory_never_reaches_thread_b` | end-to-end, both turns forced through a reading intent so "no leak" is not vacuous |
| 2 | the Telegram identity triple (`chat_id` + `user_id`) does not widen the fence | `test_user_a_memory_never_reaches_user_b` | end-to-end |
| 3 | every `search()` the runtime issues carries the caller's own `thread_id` | `test_every_memory_read_is_scoped_to_the_calling_thread` | API boundary |
| 4 | **a read never returns another thread's rows** | `test_a_read_never_returns_another_threads_memories` | **yes — `top_k=64` over a 24-row corpus** |
| 5 | the **production entry point** threads the caller's identity into the state | `test_the_real_entry_point_threads_the_callers_identity` | real `_base_state` from `bot/handlers.py` |
| 6 | a recall sent through the real entry point reaches the model | `test_a_recall_sent_through_the_real_entry_point_reaches_the_model` (tripwire) | real `_base_state` → real graph |

Test 4 exists because of an honest correction. Tests 1–3 all *survived* mutant M3 (the
`WHERE thread_id = ?` clause deleted from both SQL lanes). Two reasons, both worth
recording:

- tests 1 and 2 read with `top_k=3` through a ranking lane whose ordering depends on
  hash-salted vectors, so a widened filter can still hide the other user's row behind a
  lucky ordering — a **probabilistic** detector;
- test 3 asserts the *argument* passed to `search()`, which a store that ignores that
  argument cannot violate.

Only test 4 observes the **result set**, which is where `memory_context` actually comes
from. It killed M3 deterministically. A guard that cannot fail is not a guard.

**Result: no cross-thread or cross-user leak was observed, before or after the H1 patch,
and the mutant that removes the fence is caught.**

### 7.1 A second finding, found only by looking at the real entry point

Tests 1–3 all build their `NexusState` by hand, which is a weakness: the production
constructor is `bot/handlers.py::_base_state`, and nothing above would notice if that
path changed. Adding invariants 5–6 exposed a genuine scoping decision that the earlier
tests had silently assumed away:

```python
"thread_id": f"tg:{_chat_id(update)}",     # bot/handlers.py:180
```

`thread_id` is a function of **`chat_id` alone**. So "user A ≠ user B" is true only for
private chats: **two members of one Telegram group share a single memory scope**, and one
member's stored turn is recallable in the next member's turn. This is pinned as a
characterisation (`test_thread_id_is_derived_from_the_chat_alone_not_the_user`, green) so
that changing the scope is a deliberate act, and asserted as a desired invariant
(`test_a_group_member_cannot_recall_another_members_memory`, **red**, `task-210`) so the gap
is not forgotten. Mutant M7 — the plausible *wrong* fix, deriving `thread_id` from
`user_id` — is killed by the characterisation, which is precisely why the characterisation
exists.

`bot/handlers.py` is under no live lease, but **seven open PRs rewrite it** (including
#117). It is therefore read here and never edited; the tests live in this PR's own path.

---

## 8. U1 — HONEST EVALUATION OF PARAPHRASE RETRIEVAL

W2 stated plainly that its 24-document fixture is keyword-overlap friendly and therefore
cannot measure the dense lane's real value. That is taken at face value and re-measured
here, on `main`, with a fixture designed to be hostile to lexical matching:

```
python scripts/chat_memory_mutations.py --u1 --repeat 8
```

| | |
|---|---|
| dataset | 8 paraphrase pairs + 16 unrelated distractors = 24 rows |
| queries | 8 |
| **mean lexical overlap (query ↔ target)** | **0.081** — genuinely low |
| top_k | 3 |
| chance hit@3 (3/24) | 0.125 |

| lane | hit@3 | MRR@3 | reproducible? |
|---|---|---|---|
| recency (`ORDER BY id DESC`) | **0.000** (8/8 runs) | 0.000 | **yes** — deterministically wrong |
| dense (`vec_distance_cosine`) | **0.125 mean**, samples `[0.125, 0.0, 0.125, 0.0, 0.125, 0.25, 0.125, 0.25]`, range **[0.000, 0.250]** | 0.188 | **no** |

**Two findings, both new relative to W2's report:**

1. **On `main`, paraphrase recall is at chance.** The recency lane scores exactly 0.000 —
   it returns the newest rows regardless of the question. The dense lane averages exactly
   chance and swings across a fourfold range run to run.
2. **The dense lane is not even measurable.** `FakeLLMProvider.embed` seeds
   `random.Random(hash(text))`, and `str.__hash__` is salted once per interpreter
   (`PYTHONHASHSEED`). Every identical invocation produces a different score. **A
   regression baseline pinned on this lane would be pinning noise** — which is precisely
   the failure mode of the old `eval.py` baseline, one level deeper.

**Explicit non-claim:** this measures `main`'s `LongTermMemory` only. It is **not** a
quality verdict on PR #121's hybrid retriever, which is unmerged and under that PR's live
lease; a real dense lane needs a deterministic embedder and a real model, neither of
which this sandbox can load. `sentence-transformers` / `llama-cpp-python` are declared
dependencies and are not installed here.

---

## 9. WHAT CHANGED

| path | kind | what |
|---|---|---|
| `src/nexus_ai_agent/orchestration/graph.py` | **modified** | **H1 — the 4-line `route_intent` fix (§6)** |
| `tests/unit/test_chat_memory_reachability.py` | new | 27 tests, 10 promoted to hard assertions |
| `scripts/chat_memory_mutations.py` | new | mutation + U1 harness |
| `.agents/board.json` | modified | zones, `task-205`/`task-206` claims, next work |

**Exactly one file under `src/` was modified**, and it is the file the board fences for
`task-206`. No existing test was modified — `tests/unit/test_graph_memory.py` was read,
not touched, and its assertions (memory *write* and executor behaviour) do not cover
routing. No governance rule, board test, or threshold was changed. `scripts/h1_repro.py` was written during PHASE 2 and
then **deleted** once the test suite made it redundant — a second implementation of the
same proof is debt, not evidence.

### The tests that are `xfail`, and why that is not fake success

`xfail` is a declaration, not a pass. Each of the 10 carries the lease that blocks it:

```
H1 is blocked: src/nexus_ai_agent/orchestration/graph.py is under the live
lease of task-202 (branch arena/01a0e907, PR #119, expires 2026-09-29T17:51:40Z)
```

They are **not** vacuous, on three independent grounds:

- a **control test** (`test_control_an_intent_that_reads_memory_really_delivers_it`)
  proves the harness detects a positive, so a "not reached" result means something;
- `xfail` is **non-strict**, so when H1 lands the tests XPASS and pytest reports them as
  passed — the fix can never be followed by a stale red;
- the mutation harness **kills 4/4 mutants**, which is what proves the guards bite.

`tests/unit/test_chat_memory_reachability.py::test_the_writer_and_the_reader_cover_the_same_intents`
is the one guard that caught **its own author**: an earlier draft asserted only "some read
happened during the session" and XPASSed, because one of the five questions happens to
classify as `memory`. It now asserts **per turn** and fails correctly.

---

## 10. REGRESSION PROOF

```
$ ruff check tests/unit/test_chat_memory_reachability.py scripts/chat_memory_mutations.py
All checks passed!
$ ruff format --check tests/unit/test_chat_memory_reachability.py scripts/chat_memory_mutations.py
2 files already formatted

$ python -m pytest tests/integration/test_graph.py tests/integration/test_agents.py \
    tests/unit/test_router.py tests/unit/test_router_multilingual.py \
    tests/unit/test_execution_truth.py tests/unit/test_agent_board.py \
    tests/architecture/test_memory_boundaries.py tests/architecture/test_import_boundaries.py -q
63 passed in 1.52s

$ python -m pytest tests/unit tests/architecture -q
25 failed, 2751 passed, 19 skipped,  2 xfailed, 16 warnings in 98.90s

$ python -m pytest tests/unit tests/architecture -q --ignore=tests/unit/test_chat_memory_reachability.py
25 failed, 2726 passed, 19 skipped, 16 warnings in 96.68s
  # baseline run with H1 stashed and the reachability suite deselected
```

**The 25 failures are identical with and without this change** — creative render, ffmpeg,
litellm and postgres-engine tests that need binaries and packages absent from this
sandbox. Net effect of this PR, including the fix itself: **+25 passed, 2 declared xfail,
0 regressions.**  The 25 failures are byte-identical across both runs.

Full release gates are **deferred to the gates owner** (`AGENTS.md` §1.4). Nothing in this
document is a production or `main` capability claim.

### 10.1 Remote CI on this branch — 16/16 green

Final run, on the commit that carries H1 (`065ec63`, PR #122): `lint (ruff + mypy +
version lockstep)`, `lint-fast`, `test (pytest -m "not slow")`, `python-parity`
3.10/3.11/3.12, `continuum-evidence` 3.10/3.11/3.12, `extras-matrix` core/pdf/speech/
translate, `trust-mutations`, `migrate-postgres`, `release-lineage` — **all pass, zero
failures**.

The two earlier runs on this branch were also green once fixed, and the red one is
recorded rather than hidden, because it is the reason a Markdown-only diff here is not
automatically safe:

| commit | outcome |
|---|---|
| `1d92797` | **lint red** — `ruff format` also formats Python fences inside Markdown; two hand-aligned comments in this audit did not match |
| `f4d082a` … `ed53898` | 15/15 → 16/16 green (contract only; H1 not yet landed) |
| `b42effd`, `13a33b9` | green (harness idempotence; board claim) |
| `065ec63` | **16/16 green, carries H1** |

**A green branch is not a main capability.** H1 is merged into *this* branch only;
`main` still routes chat around the memory reader, and nothing in this document has run
against Telegram, a real LLM, a real embedder, Neon or Postgres.

---

## 11. KNOWN LIMITATIONS

1. **H1 is delivered on this branch, not on `main`.** The user-visible symptom is still
   present in production. This is a **branch capability**, not a released one.
2. **Two invariants remain open by decision, not by accident:** `task-215` (a dead backend
   is still reported as a successful empty recall) and `task-210` (a Telegram group shares
   one memory scope across its members). Both are stated as `xfail` with a task id.
3. **The suite is green, and two tests are green *by declaration*.** 25 pass; the 2
   `xfail`s are the open decisions above. A reviewer who reads only the pass count is
   misreading it.
4. **No production verification.** No Telegram run, no real LLM, no real embedder, no
   Neon/Postgres, no sqlite-vec load in CI. `FakeLLMProvider` is the only provider exercised.
5. **Isolation tests 1–2 are probabilistic detectors** (see §7). Test 4 is the
   authoritative one.
6. **U1 measures `main`**, not PR #121's retriever. A real dense lane needs a deterministic
   embedder and a model this sandbox cannot load.
7. **`agent_board.py check` gave a false green** (§5.1). Every agent working from `main` is
   currently blind to leases held on unmerged PR branches.
8. **No observability was added.** After the H1 wiring a chat turn still emits no
   structured record of *whether* it read memory, *how many* hits it got, or *which lane*
   answered. `NexusState` carries no retrieval outcome. Tracked as `task-215`.
8. **PR #121 unmerged** means this branch measures pre-W2 retrieval throughout. The two
   efforts will need reconciling.

---

## 12. WHAT THE NEXT AGENT OWES

**Merge this before anything else in the memory area.** `main` still has the gap.

1. Merge PR #122. `graph.py` merges cleanly with PR #119 (measured in §5.2); the only
   conflict is `.agents/board.json`, resolved by *newest state wins* (`AGENTS.md` §6).
2. After merge, the two `xfail`s are the honest backlog: `task-215` then `task-210`. Both
   are decisions, not accidents, and both carry an assertion that turns green when settled.
3. `scripts/chat_memory_mutations.py` now runs on the **fixed** branch and is the permanent
   regression harness — six mutants, all of which must keep dying. It is idempotent-aware:
   it recognises its own patch instead of erroring on a stale anchor.

```bash
python scripts/chat_memory_mutations.py              # regression harness
python -m pytest tests/unit/test_chat_memory_reachability.py -q
# expect: 25 passed, 2 xfailed
```

---

## 13. NEXT WORK

The ten tasks registered on the board as `next_work` are all derived from the findings
above. The five that unblock the rest, in order:

| id | title | why it is first |
|---|---|---|
| `task-206` | Apply the verified H1 wiring to `route_intent` | the actual user-visible fix; everything else is instrumentation of a system that still cannot recall |
| `task-207` | Make `agent_board.py check` see leases on unmerged PR branches | a tool that returns "safe to proceed" for a leased file will eventually lose someone's work |
| `task-215` | Typed retrieval outcome — stop reporting a dead backend as an empty recall | LAW 11; the one tripwire the one-line fix provably cannot satisfy |
| `task-208` | Deterministic test embedder — `hash()`-seeded vectors make the dense lane unmeasurable | every retrieval baseline pinned on the current embedder is pinned on noise |
| `task-209` | Paraphrase benchmark on the merged hybrid retriever | U1 was answered for `main` only; it must be re-answered for the retriever that actually ships |

---

## 14. POST-H1 CONSOLIDATION MISSION (2026-09-29, 19:45Z – 21:20Z)

A second mission was run after H1 landed: verify the #119/#122 relationship, prove
merge safety, and pick the next P0 by a priority gate rather than by convenience.
H1 was **not** redone and no second memory-routing implementation was written.

### 14.1 A CORRECTION TO MY OWN EARLIER LEASE SCAN

The "41-board scan returned CLEAR" reported in §5 was produced by a parser that
looked for a `tasks` array. The board schema is **`claims` + `zones[].paths`**;
the correct parse finds **seven** overlapping claims, not zero:

```
PR    task                              zone                                lease      files
119   task-202-memory-write-observability conversation-memory-observability EXPIRED  graph.py
119   task-201-shell-trust-plane-grammar shell-trust-plane                 EXPIRED  system_shell.py
121   task-203-memory-retrieval-truth    memory-retrieval-truth            LIVE     memory/, router.py
117   task-200-w3-memory-trust-boundary  task-200-w3-…                      EXPIRED  memory/
112   task-196-w1-runtime-composition    runtime-composition-w1             EXPIRED  handlers.py
113   task-196-closeout-w1               runtime-composition-w1             EXPIRED  handlers.py
99    task-193-owner-audit-command-truth command-truth-and-runtime-hardening EXPIRED  handlers.py
```

The **conclusion happened to be right** for `graph.py` — task-202's lease is
`claimed_at=2026-09-28T17:51:40Z + ttl_hours=24 = 2026-09-29T17:51:40Z`, and the
edit landed at ~17:55Z, three and a half minutes after the protocol's own TTL
expired — but it was reached with a parser that proved nothing. The method was
wrong, not merely verbose. The schema mismatch is filed as `task-207`.

### 14.2 ONE LIVE LEASE, AND WHAT IT BLOCKS

`memory/` and `orchestration/router.py` are held by **PR #121 `task-203`**, live
until `2026-09-29T21:39:02Z`. That is why `task-215` (typed retrieval outcome) is
**not** the next task: its honest design may need the store to raise a typed
error, and the store is being rewritten by someone else right now. Everything
else in the P0 remainder is free.

### 14.3 #119 IS MERGE-READY, COMPLETE, AND ABANDONED

Re-derived at `e78e8c4`, not read from the PR body:

| | evidence |
|---|---|
| CI | **17/17 pass**, 0 failures (includes `shell-mutations`) |
| shell sandbox | `scripts/shell_sandbox_mutations.py` → **11/11 killed**, baseline GREEN → restored GREEN |
| its P0 suites | 87 passed (`test_shell_workspace_escape`, `test_redaction_boundary`, `test_conversation_store_schema`, `test_graph_memory`) |
| the actual escape, main vs #119 | `date -f <outside>` on main returns `date: invalid date 'WSX-SECRET-abc123'` — **the file is echoed back**; on #119 all five probes are `safe` |
| redaction, main vs #119 | 4/4 leaks on main (bad-port userinfo, invalid-IPv6 userinfo, `?key=%…`, `?access_key=%…`); 4/4 `safe` or `[REDACTED]` on #119 |
| memory-write observability | main: dead `store()` → **0 warnings**. #119: emits `long_term_memory_write_failed error_type=RuntimeError thread_id=tg:1`, leaks no prompt text, `state["error"]` stays `None` |

**But all five of #119's leases (task-201/202/203/204) have expired**, the last at
`2026-09-29T19:15:01Z`, and the PR has been a **draft**, untouched, since
`2026-09-28T19:22:53Z`. Nobody will merge it. `main` is still exposed to
`date -f <outside file>`.

### 14.4 MERGE PROOF — BOTH FIXES, BUILT AND TESTED, NOT JUST MERGE-TEXTED

`graph.py` auto-merges (exit 0, no conflict). The only conflict in the whole
tree is `.agents/board.json`, resolved newest-state-wins (`AGENTS.md` §6). A
throwaway merge of `cbec307 + e78e8c4` was actually committed and run:

```
merged graph.py  A memory-write observability : True   (log.warning at :184)
                 B chat->memory_reader_chat   : True   (route_intent at :233)
                 parses as python             : True

pytest (union of #119 + #122 suites)   -> 200 passed, 2 xfailed
scripts/chat_memory_mutations.py      -> 6/6 killed
scripts/shell_sandbox_mutations.py    -> 11/11 killed
scripts/telegram_e2e_mutations.py     -> 4/4 killed
```

Neither fix was discarded to make the other merge. Nothing was force-pushed and
no history was rewritten; the probe lived in a detached worktree.

### 14.5 task-214 — THE LAST UNTESTED SPAN, NOW CLOSED

`task-205` drove `_base_state` and invoked the graph itself. That left everything
`on_message` does *around* the graph untested: presence, auth, rate limiting, the
force-join gate, document-chat routing, the consent prompt, the user/chat upsert,
and the `AgentManager` lookup. A gate that started returning early would have
left the entire suite green while the bot answered from nothing.

`tests/unit/test_telegram_memory_e2e.py` — **7 passed**, `telegram_e2e_mutations.py`
— **4/4 killed**, each mutant by a named test.

**Two findings worth more than the tests that produced them:**

1. **`on_message` sets `state["intent"] = "unknown"` in two places** — inside
   `_base_state` and again just before `graph.ainvoke`. It is correct today *only*
   because `_router_node` recomputes the intent with `classify_intent` before
   `route_intent` reads it (the log line for that very call ends `intent=chat`).
   That is an ordering accident, not a guarantee. Pinned by
   `test_intent_unknown_is_overwritten_by_the_router`.

2. **The active-agent branch never reads memory at all.** When
   `AgentManager.get_active` returns an agent, `on_message` answers through
   `active_agent.respond(...)` and never builds a graph state. That user gets no
   durable-memory read on **any** turn, and **H1 cannot fix it** — the reader is
   inside the branch that is not taken. Pinned by
   `test_an_active_agent_bypasses_the_memory_path_entirely`. This is a third P0
   gap and it was not on any board.

**A mutant that correctly did not survive.** `E4_memory_context_wiped_before_the_graph`
set `state["memory_context"] = ""` after `_base_state` and **stayed green** — not
a hole, but a false premise: `_memory_reader` assigns that key unconditionally, so
the pre-seed is overwritten before anything reads it. The original task-214 record
claimed this was "the live defect"; it is not. The mutant was **dropped and
replaced** with a load-bearing one, because a guard asserting a non-invariant is a
fake guard.

### 14.6 A SELF-INFLICTED REGRESSION, CAUGHT AND FIXED

The first cut of the e2e file imported its doubles with
`from tests.unit.test_chat_memory_reachability import …`. That is the exact shape
of incident **91fbaff**: it resolves under `python -m pytest` and breaks under the
bare `pytest` entry point CI uses. `test_test_suite_hygiene.py` went red — 26
failures against a 25 baseline. The rule's own docstring names the two legal
answers; thirty lines were repeated into the module and
`test_chat_memory_reachability.py` was left untouched.

```
pytest (bare)  … -> 32 passed, 2 xfailed
python -m pytest tests/unit tests/architecture -q -> 25 failed, 2758 passed
```

2751 → 2758 is this file's seven tests. The 25 failures are the unchanged
pre-existing sandbox baseline. **Zero regressions.**

---

## 15. task-211 — THE READER QUERIED THE ASSISTANT (reproduced, then fixed)

The hypothesis was that `_memory_reader` queries `state["messages"][-1]`, which
can be the assistant's own last turn. It was tested on the real compiled graph
with a real checkpointer **before** any code changed.

```
case A  on_message's own flow (a fresh one-message state)
        -> queried the user's words.                       OK
case B  the caller replays the previous RESULT state
        -> queried 'ASSISTANT-ANSWER'.                     DEFECT
case C  a state whose last message is the assistant's
        -> queried 'Noted, Sara.'                          DEFECT
case D  no messages at all
        -> queried ''.                                     no recall, no crash
```

Case A is the only one production exercises, which is why this was latent rather
than loud. But **case B is the graph's own output state** — the most natural thing
for any caller to hand back — and the reader and writer disagreed about what "this
turn" means: `_memory_writer` walks backwards for the last **user** message,
`_memory_reader` took whatever was last.

The fix is six lines and mirrors the writer exactly. No new dependency, no
signature change, no new abstraction.

**A mutant the first version of the suite could not kill.** `M2` rewrites the fix
to take the *first* user message. It **survived**, because every state those tests
used held exactly one user turn, so first and last are the same string. A reviewer
who "fixed" this by reading the first user message would ship a query that goes
staler every turn. The new multi-turn guard kills it.

```
tests/unit/test_memory_reader_query.py            7 passed
scripts/memory_reader_query_mutations.py         3/3 killed
scripts/chat_memory_mutations.py                 6/6 killed
scripts/telegram_e2e_mutations.py                4/4 killed
python -m pytest tests/unit tests/architecture   25 failed, 2765 passed
```

The 25 are the unchanged sandbox baseline; 2758 → 2765 is this file's seven tests.
**Zero regressions.**

A side effect worth recording: the task-211 edit renamed `last` to `last_user`,
which **broke an anchor in `chat_memory_mutations.py`**. That harness refused to
run and demanded a re-base instead of guessing — the behaviour that keeps a stale
harness honest, and the reason a green run is meaningful.

## 16. task-218 (NEW, P0) — THE CHECKPOINTER PERSISTS NO CONVERSATION HISTORY

Found while reproducing task-211, and it is larger than the symptom.

```
two turns on one thread_id ->
  state messages : [('user','What is my name?'), ('assistant','ACK')]
  turn_count     : 1        (after TWO turns)
  turn-1 text present in the turn-2 state?  No
```

**Root cause:** `NexusState` is a plain `TypedDict` and `messages: list[dict]`
carries no `Annotated(..., add_messages)` reducer, so it is a **LastValue**
channel. `_base_state` hands the graph a fresh one-message state every turn, which
*replaces* the checkpointed history; the same replacement zeroes `turn_count`
before `_router_node` increments it straight back to 1.

**Consequence:** the agent has no short-term conversational memory at all. Long-term
recall is therefore the **only** continuity mechanism — so a recall failure is not
degraded UX, it is amnesia for that turn. That raises the stakes on H1 and on
task-215, and it was on no board.

**Not fixed here.** Choosing between an `add_messages` reducer, an explicit history
merge, and *deliberately stateless* is an architecture decision with repo-wide state
semantics. It belongs in an ADR before code.

### Merge re-verified with task-211 in place

```
conflicts                      : 1  (.agents/board.json only)
graph.py                       : auto-merges, zero conflicts
union of #119 + #122 suites    : 207 passed, 2 xfailed
chat_memory_mutations.py       : 6/6 killed
memory_reader_query_mutations  : 3/3 killed
telegram_e2e_mutations.py      : 4/4 killed
```

Both fixes still present, neither discarded, no force-push, no history rewritten.
