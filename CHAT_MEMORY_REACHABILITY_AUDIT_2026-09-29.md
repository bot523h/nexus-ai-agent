# Chat→Memory Reachability Audit — task-205

> **One-line verdict:** a memory stored by this repository **cannot** be reached by a
> user on the chat path. The one-line fix exists and is verified here, but it lives in a
> file another agent currently leases, so it is **deferred, not delivered**.

| | |
|---|---|
| **Task** | `task-205-chat-memory-reachability-contract` (zone `chat-memory-reachability`) |
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

## 5. WHY H1 WAS NOT APPLIED

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
branches (the same traversal `scripts/agent_board.py praudit` already performs).

---

## 6. THE FIX — verified out of tree, ready to apply

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

| path | kind | lines |
|---|---|---|
| `tests/unit/test_chat_memory_reachability.py` | new | 27 tests |
| `scripts/chat_memory_mutations.py` | new | mutation + U1 harness |
| `.agents/board.json` | modified | zone, claim, deferral |

**No file under `src/` was modified.** No existing test was modified. No governance rule,
board test, or threshold was changed. `scripts/h1_repro.py` was written during PHASE 2 and
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
25 failed, 2739 passed, 19 skipped, 12 xfailed, 2 xpassed in 120.98s

$ python -m pytest tests/unit tests/architecture -q --ignore=tests/unit/test_chat_memory_reachability.py
25 failed, 2726 passed, 19 skipped, 16 warnings in 118.16s
```

**The 25 failures are identical with and without this change** — creative render, ffmpeg,
litellm and postgres-engine tests that need binaries and packages absent from this
sandbox. Net effect of this PR: **+13 passed, +12 declared xfail, +2 xpass, 0 regressions.**

Full release gates are **deferred to the gates owner** (`AGENTS.md` §1.4). Nothing in this
document is a production or `main` capability claim.

---

## 11. KNOWN LIMITATIONS

1. **H1 is not delivered.** The user-visible symptom is unchanged on this branch. Any
   capability described here is a **branch-local, guarded, not-yet-wired** state.
2. **The suite is green because the failures are declared.** A reviewer who only reads
   "11 passed" is misreading it; the RED state is in the `xfail` count and in the M0 row of
   the mutation report.
3. **No production verification.** No Telegram run, no real LLM, no real embedder, no
   Neon/Postgres, no sqlite-vec load in CI. `FakeLLMProvider` is the only provider exercised.
4. **Isolation tests 1–2 are probabilistic detectors** (see §7). Test 4 is the
   authoritative one.
5. **U1 measures `main`**, not PR #121's retriever. A real dense lane needs a deterministic
   embedder and a model this sandbox cannot load.
6. **`agent_board.py check` gave a false green** (§5.1). Every agent working from `main` is
   currently blind to leases held on unmerged PR branches.
7. **No observability was added.** After the H1 wiring a chat turn still emits no
   structured record of *whether* it read memory, *how many* hits it got, or *which lane*
   answered. `NexusState` carries no retrieval outcome. Tracked as `task-215`.
8. **PR #121 unmerged** means this branch measures pre-W2 retrieval throughout. The two
   efforts will need reconciling.

---

## 12. THE NEXT AGENT'S FIRST MOVE

```bash
# 1. confirm the lease is gone
git fetch origin && python -c "
import json,subprocess
for pr in json.loads(subprocess.run(['gh','pr','list','--state','open','--json','number,headRefName'],capture_output=True,text=True).stdout):
    b=json.loads(subprocess.run(['git','show',f\"origin/{pr['headRefName']}:.agents/board.json\"],capture_output=True,text=True).stdout or '{}')
    for c in b.get('claims',[]):
        if c.get('status')=='active' and any('graph.py' in p for p in c.get('exclusive_paths') or []):
            print('LEASED BY', c['task'], c['claimed_at'], c.get('ttl_hours'))
"

# 2. apply the patch from §6, then
python scripts/chat_memory_mutations.py     # M1 must go green, M0 must go quiet
python -m pytest tests/unit/test_chat_memory_reachability.py -rxX
```

Expected after the patch: **0 xfailed except the typed-failure one**, which needs the
second change described in `task-215`.

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
