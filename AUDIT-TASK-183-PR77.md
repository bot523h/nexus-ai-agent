# TASK-183 / PR#77 — FINAL INDEPENDENT AUDIT

**Gate 2 × Creative Runtime — trust-boundary proof → stack closure**
Auditor session branch: `arena/01a0d547-nexus-ai-agent` (independent of the PR#77 branch; read-only audit, no production/test files changed or pushed)
Audit date: 2026-09-24 · Method: LIVE GITHUB → board → ancestry → source → runtime → adversarial → mutation → CI

---

## 1. LIVE GITHUB (verified twice: at audit start and at audit end — no drift)

| Item | Value (live) |
|---|---|
| Authenticated identity | `arena-ai-coding-agent[bot]` (GitHub App token; repo scoped) |
| PR#72 | **OPEN**, head `f450df273d94` (`arena/01a0d43c-nexus-ai-agent`), base `main`, MERGEABLE/CLEAN, no review yet |
| PR#75 | **OPEN**, head `e0cba268897e` (`arena/01a0d4c8-nexus-ai-agent`), base `arena/01a0d43c-nexus-ai-agent`, MERGEABLE/CLEAN |
| PR#77 | **OPEN**, head **`566207c6c4fa469d402e3f178eef0827052a6f5f`** ✅ = expected HEAD |
| PR#77 BASE | **`arena/01a0d43c-nexus-ai-agent`** ✅ = expected (= PR#72's branch) |
| PR#77 review | none submitted; 0 comments |
| mergeability | MERGEABLE / mergeStateStatus CLEAN (against its base, i.e. PR#72 head) |
| PR#77 CI @ `566207c` | **all green**: `test`, `lint`, `lint-fast`, `migrate-postgres` (runs 36056216519 + 36056222415) |
| PR#72 CI @ `f450df2` | all green (same 4 workflows) |
| Branch owners | all PRs authored by `app/arena-ai-coding-agent`; task-183 canonical branch = `arena/01a0d50d` (matches board claim) |

Nothing is stale: HEAD and BASE match the task brief exactly, so all verification below was performed against `566207c`.

## 2. STACK / ANCESTRY (real Git, not PR prose)

```
main 035a896dd2ed1293de6accf2ef4309da2fd64c89
  └─ PR#72 f450df273d944dc88e3a21337b915b40638ee4dd   (3 commits)
       └─ PR#75 e0cba268897e8d656c69c48104f6519f0dae47e1   (3 commits)
            └─ PR#77 566207c6c4fa469d402e3f178eef0827052a6f5f   (+3 delta commits)
```

- `git merge-base --is-ancestor` proofs: main ⊂ PR#72 ✅, PR#72 ⊂ PR#75 ✅, PR#72 ⊂ PR#77 ✅, **PR#75 ⊂ PR#77 ✅**
- `merge-base(PR#75, PR#77) = e0cba26` = PR#75 head → PR#77 is a **strict superset**: PR#75's three commits (`6718e61`, `91fbaff`, `e0cba26`) are present **unchanged** (identical SHAs); no rebase/rewrite/force-push of PR#75's history.
- PR#77 delta commits: `4eea7a6` (worker-policy opt-in), `6fc2c43` (docs), `566207c` (board).
- PR#77 remains dependent on PR#72 (base = PR#72 branch; board records merge order PR#72 → PR#77).

## 3. OWNERSHIP / BOARD (`.agents/board.json` @ `566207c`)

- task-183 claim = `task-183-pr67-lifecycle-integration`, status `active_in_review`, agent_branch `arena/01a0d50d-nexus-ai-agent` = PR#77 branch ✅ (canonical identity holds)
- Lease: claimed 2026-09-24T20:37:51Z, TTL 24 h → valid at audit time; exclusive_paths **empty** (no fence)
- Gates owner = `task-179-gate2-command-reconciliation` (PR#72 branch, `gates_owner: true`, lease valid to 2026-09-26) → **full-repo gates: deferred to gates owner** per AGENTS.md §1.4. Local diagnostics (ruff, targeted pytest, mypy) run by this audit are permitted for everyone.
- Overlap check (`agent_board.py check`): `creative/studio/bus.py` is fenced by task-179 — this is the **declared, acknowledged stacked overlap** (`overlap_ack` in the task-183 claim: stacked on PR#72 HEAD, no overwrite, merge order fixed). Consequence for this audit: **no production/test file was modified or pushed**; all mutations were local apply→RED→restore, never committed.
- PR#76 owner: branch `arena/01a0d4c7` (S1–S5 security salvage; distinct task, no task-183 overlap). PR#78 owner: branch `arena/01a0d4b6` (Gate 5, task-181). Neither overlaps task-183's paths.
- Takeover log records the PR#75→PR#77 continuation (session-bound-to-one-branch rule) with ancestry evidence.

## 4. PR#75 → PR#77 DELTA FORENSICS (file-by-file)

| File | Change | WHY | Security invariant | Test | Mutation |
|---|---|---|---|---|---|
| `creative/render_jobs.py` | −`allow_experimental` payload field; +`EXPERIMENTAL_OPT_IN_OPERATIONS` (3 ops); `_dispatch()` loses the boolean param; `allow_experimental=operation in EXPERIMENTAL_OPT_IN_OPERATIONS` | queue row carried lifecycle privilege | row cannot widen/narrow policy; opt-in = server module constant | `test_a_queue_row_cannot_carry_a_lifecycle_opt_in`, `test_direct_dispatch_offers_no_opt_in_parameter`, `test_experimental_opt_in_comes_from_worker_policy_not_the_row`, `test_opt_in_policy_is_exactly_...` | M1, M2, M3, M4, M8 |
| `bot/creative_surface.py` | −`"allow_experimental": req.command == "grade"` in `job_payload` | surface wrote privilege into the row | surface cannot invent policy | `test_job_payload_carries_no_lifecycle_opt_in` (5 commands × valid-row positive control) | M1 (via row) |
| `tests/…/test_gate2_lifecycle_seam.py` | +order pins 4b<5, 4b<6 with positive controls; `assert_untouched` also checks hash/revision | pin exact gate position | gate order is load-bearing | the new tests themselves | M5, M6 |
| `tests/architecture/test_lifecycle_gate_boundary.py` | +2 AST guards (no `.allow_experimental` attribute reads in src/; the only `CommandBus(allow_experimental=…)` site derives from the policy set, literal `True` forbidden) | structural singularity | exactly one trusted composition path | the guards themselves | M1, M2 |
| `tests/architecture/test_test_suite_hygiene.py` | new: no `tests.*` package imports (91fbaff CI root cause) | CI portability | tests installable/standalone | positive control included | — |
| `tests/unit/test_capability_lifecycle.py` | PR#67's suite, byte-identical | PR#67 semantics preserved | fail-closed 4-state machine | 15 tests | M7 (authz), behavior matrix |
| `tests/unit/test_creative_render_jobs.py` / `test_creative_surface.py` | trust-boundary battery replaces payload-flag tests | adversarial coverage | see §6 | 21+ tests | M1–M4, M8 |
| `docs/*` + `board.json` | contract §2/§11/§12, CREATIVE_STUDIO, ADR-0005 amendment, D-0013 amendment, board continuation | docs = code truth | no false merge claims | docs-integrity suite | — |

**Unrelated changes: none found.** Every delta maps to the task-183 trust boundary, its order pins, or its documentation.

## 5. TRUST BOUNDARY — full data flow, every edge labeled

```
Telegram update (user text/media)                          UNTRUSTED
  → creative_surface._dispatch: parse command/op/args      UNTRUSTED input
  → CreativeSurfaceMapper.map: closed allow-list           gate (refusal, no privilege)
  → job_payload: 10 fields, NO privilege field             UNTRUSTED data, zero authority
  → JobQueuePort.enqueue → durable row                     UNTRUSTED structure (attacker-writable model)
  → creative_render_job(dict) → CreativeRenderPayload      extra="forbid" validation — still untrusted
  → _guarded_workspace/_guarded_input                      containment under creative_temp_dir
  → canonical_id = SURFACE_TO_CANONICAL[(cmd,op)]          server table, closed set; else unsupported_operation
  → _dispatch(operation=canonical_id)                      operation id is the only row-derived input
  → CommandBus(allow_experimental = operation ∈
      EXPERIMENTAL_OPT_IN_OPERATIONS)                      TRUSTED (server module constant, code-reviewed)
  → check_required_packs(registry.required_packs, …)       TRUSTED (registry, never the client)
  → Gate 4b → policy → refs → idempotency → preconditions → handler → commit
```

**Answer to the central question:** `allow_experimental` is **no longer controllable from any untrusted structure**. The worker accepts exactly one degree of freedom from the row — *which canonical operation to request* — and the privilege answer to that question is a server-side frozenset. The bus constructor flag itself is reachable only from `render_jobs._dispatch`, and the AST guards pin that its value references the policy set and is never a literal.

All read/write/construct sites (exhaustive, production):
- `EXPERIMENTAL_OPT_IN_OPERATIONS`: defined `render_jobs.py:68`; read `render_jobs.py:219` (the only read). No env-var, settings, setattr or config path can alter it (grep-verified).
- `CommandBus(` construction: exactly 3 sites — `render_jobs.py:216` (passes the derived flag), `slideshow/service.py:173` and `slideshow/upscale.py:89` (default `False`; slideshow pack is AVAILABLE). Pinned by `test_no_second_command_bus_construction_path`.
- `check_required_packs`: called exactly once, in `bus.py` (AST-pinned).
- Execution entry points: `dispatch` → `_dispatch_locked` (single pipeline under RLock; nested dispatch forbidden) → `_apply_guarded` → `_apply`. No dispatch path bypasses the pipeline.

## 6. QUEUE-TAMPERING RESULTS (independent runtime probes, real production chain)

| Attack | Expected | Actual | Result |
|---|---|---|---|
| raw dict row `allow_experimental=True` | rejected | ValidationError (extra field) | ✅ |
| raw dict row `allow_experimental=False` | rejected | ValidationError | ✅ |
| serialized-JSON row (both values) | rejected | ValidationError | ✅ |
| database-style row w/ extra cols (both values) | rejected | ValidationError | ✅ |
| worker entrypoint, tampered row (both values) | typed `invalid_request` naming the field | `invalid_request`, detail names `allow_experimental` | ✅ |
| casing variants (`Allow_Experimental`, `allowExperimental`), nested smuggle | rejected | ValidationError | ✅ |
| `_dispatch()` opt-in parameter | absent | params = project/operation/input_data/idempotency_key only | ✅ |
| payload model field | absent | 10 fields, no opt-in | ✅ |
| server policy emptied → same grade/exposure row | refused, experimental named | `invalid_request: PackRequirementError … experimental` | ✅ |
| row asserting `False` for a policy-allowed op (server decides) | server policy wins | policy intact → same row executes end-to-end (real FFmpeg artifact) | ✅ |

**27/27 adversarial probes PASS** (probe drove `creative_render_job`, real `CommandBus`, real runtime registry, real FFmpeg render).

## 7. POLICY MATRIX (extracted from the live registry, not from docs)

Operation → pack → lifecycle → server policy (all 52 pack-gated ops enumerated; 5 wave-1 ops have `required_packs=()` → no gate, by design):

| Surface op | Canonical op | Required pack | Lifecycle | Server opt-in | Result |
|---|---|---|---|---|---|
| /edit trim, speed, reverse | timeline.* | nexus.edit.timeline | AVAILABLE | – | executes, no opt-in needed |
| /caption transcribe | caption.transcribe | nexus.language.caption | AVAILABLE | – | executes |
| /grade exposure | color.adjust_exposure | nexus.color.delivery | EXPERIMENTAL | ✅ in set | executes |
| /grade proxy | delivery.make_proxy_480p | nexus.color.delivery | EXPERIMENTAL | ✅ in set | executes |
| /grade otio | delivery.export_otio | nexus.color.delivery | EXPERIMENTAL | ✅ in set | executes |
| (not reachable) color.apply_lut, color.auto_balance, color.match_shot, delivery.render_master_4k, all audio.*, motion.* | — | EXPERIMENTAL packs | EXPERIMENTAL | ✗ not in set | refused if ever dispatched |

- **Over-grant:** none — the set is exactly the EXPERIMENTAL surface ops (pinned by `test_opt_in_policy_is_exactly_the_experimental_surface_operations`; M3 killed).
- **Under-grant:** none — all 3 surface-reachable EXPERIMENTAL ops are in the set (M4 killed).
- **Forbidden:** AVAILABLE ops need no opt-in (they don't ask for one); STUB/RETIRED/UNKNOWN refuse even with policy allow (behavior matrix: 0 handler calls in all three).
- `lut`/`burnin` are refused at the mapper AND absent from `SURFACE_TO_CANONICAL` → hand-queued rows die as `unsupported_operation`.

## 8. GATE ORDER (read from `CommandBus._dispatch_locked` source; behaviorally pinned)

1. parse + protocol version → 2. operation schema (registry lookup, schema_version, input model) → 3. **actor/project authorization** → 4. capability/version/permissions (`check_capability` + `require_permissions`) → **4b. lifecycle/pack gate** (`check_required_packs(required_packs, allow_experimental=self._allow_experimental)`, registry-declared packs only) → 5. execution policy (mode + A/B/C/D) → 6. input refs + time-ref pinning → 7. idempotency reservation → 8. revision preconditions → 9. atomic apply (handler on isolated copy, commit).

Gate 4b verified: **after** authorization ✅ (unauthorized actor never reaches it — same `AuthorizationError` for AVAILABLE/EXPERIMENTAL/UNKNOWN/STUB/RETIRED packs → **no pack-state oracle**, 0 handler calls in all 5 probe combinations), **before** policy ✅, **before** input refs ✅, **before** reservation ✅ (refused pack leaves no reservation: same key works once the gate opens), **before** handler ✅ (0 calls on every refusal).

## 9. REAL-SOURCE MUTATIONS (apply → RED → restore → GREEN; no test weakening)

| # | Mutation (production file) | RED? | Failing tests | Restored GREEN? |
|---|---|---|---|---|
| M1 | worker re-trusts `row.allow_experimental` (PR#75 shape) | **8 failed** | row-cannot-carry ×2, direct-dispatch signature, grade_otio runtime, both AST guards +2 | 93 pass ✅ |
| M2 | `allow_experimental=True` hard-coded in worker | **2 failed** | policy-withdrawn runtime test, derivation AST guard | 93 pass ✅ |
| M3 | policy over-grants `color.apply_lut` | **1 failed** | set-completeness pin | 93 pass ✅ |
| M4 | policy drops `delivery.export_otio` | **2 failed** | completeness pin + otio runtime refusal | 93 pass ✅ |
| M5 | Gate 4b moved after execution policy | **1 failed** | `test_lifecycle_runs_before_execution_policy` (ExecutionPolicyError surfaced first) | 93 pass ✅ |
| M6 | Gate 4b moved after input refs | **2 failed** | 4b<6 pin (+4b<5) | 93 pass ✅ |
| M7 | authorization removed before lifecycle | **4 failed** | seam E/E2/E3 + mutation-suite M4 | 93 pass ✅ |
| M8 | opt-in field returns to payload model (unused) | **3 failed** | row-cannot-carry ×2 + model-fields pin | 93 pass ✅ |

All 8 mutations killed; tree restored to `566207c` after each (git-verified clean).

## 10. TESTS

- Targeted battery (PR#77's own claim): `test_gate2_lifecycle_seam.py`, `test_gate2_lifecycle_mutations.py`, `test_creative_render_jobs.py`, `test_creative_surface.py`, `test_capability_lifecycle.py`, `test_lifecycle_gate_boundary.py`, `test_test_suite_hygiene.py` → **95 passed** locally (incl. real FFmpeg renders).
- **Test integrity:** the seam/mutation suites drive the **real production `CommandBus`** (test registries with probe ops are legitimate seam harnesses; the bus, lifecycle module, payload model, worker entrypoint and surface are production). `test_gate2_lifecycle_mutations.py` compiles mutated copies of the **production bus source** — a genuine mutation technique, not an implementation replica. The render/surface tests execute the real worker chain with real artifacts. No fake-pipeline-only acceptance found.
- Import hygiene: no `tests.*` imports in production code or in the tests tree (grep + `test_test_suite_hygiene.py` green, positive control included).
- Full local run (`-m "not slow"`, py3.11 minimal-deps venv): **2035 passed, 21 skipped, 6 failed**. All 6 failures reproduce **identically on PR#72 head (the PR#77 base)** in the same venv → environmental (no local Postgres service for `test_migrate_race_condition` ×4, no `test_database_url` real-engine network, no installed distribution for the version lockstep dist check). Not PR#77 regressions; GitHub CI runs all three green with proper services on the exact SHA.
- Full-repo gates on main-bound work: **deferred to gates owner** (task-179 / PR#72 branch, per board + AGENTS.md).

## 11. CI (exact SHA)

`566207c6c4fa469d402e3f178eef0827052a6f5f`: `test (pytest -m "not slow")` ✅ · `lint (ruff + mypy + version lockstep)` ✅ · `lint-fast (lockstep + pinned ruff 0.16.8)` ✅ · `migrate-postgres` ✅ — two trigger contexts (runs 36056216519, 36056222415), both all-green. Local: `ruff check .` ✅, `ruff format --check .` (467 files) ✅, `mypy src` 0 real errors (18 import-not-found artifacts of the minimal venv only; with pinned sqlmodel 0.0.42 the 2 arg-type artifacts also vanish; CI green on the SHA).

## 12. DOC TRUTH

- `COMMAND_CAPABILITY_CONTRACT.md` §11: "INTEGRATED at stage 4b … `MERGED` only after PR#72 lands and CI re-runs" — **truthful, no premature merge claim**; the PR#67 semantic recipe (keep 4b, drop 3.5 + payload/surface opt-in, add `color.apply_lut`) matches the actual code deltas I diffed between PR#67 @ `9c3a34f` and PR#77.
- `CREATIVE_STUDIO.md`: pipeline order and trust-boundary paragraph match source exactly.
- `ADR-0005` amendment + `DECISION_LOG.md` D-0013 amendment: byte-accurate, and the log **honestly records the pre-fix design** ("An earlier revision carried it as `CreativeRenderPayload.allow_experimental` … replaced before merge").
- `board.json`: task-183 `active_in_review` (not done), supersession + merge order recorded with evidence.
- Version lockstep: VERSION = pyproject = 3.13.0; lockstep CI green.
- No stale CLOSED/MERGED claims for PR#75/#77 anywhere in docs.

## 13. PR#67 COMPATIBILITY (compared real SHAs, not PR text)

- `creative/studio/lifecycle.py` @ PR#77 == @ PR#67 `9c3a34f`: **byte-identical** ✅; `tests/unit/test_capability_lifecycle.py`: **byte-identical** ✅ (15 tests pass on the PR#77 tree).
- States (`STUB<EXPERIMENTAL<AVAILABLE`, `RETIRED`), `PACK_LIFECYCLE` (8 packs: 3 AVAILABLE, 5 EXPERIMENTAL), `check_required_packs` fail-closed semantics: unchanged.
- PR#67's own seam is the pre-fix shape (stage 3.5 call site + `CreativeRenderPayload.allow_experimental` + surface flag + `confirmed=`) — **if PR#67 lands after PR#77 unchanged, the new guards go red** (row-carry, attribute-read, derivation, signature pins — proven by my M1/M8 mutations reproducing exactly that shape). The documented merge recipe is therefore load-bearing and correct.

## 14. PR#75 DISPOSITION

**SUPERSEDED** — proven: PR#75 head `e0cba26` is an ancestor of PR#77 (merge-base proof), its 3 commits are byte-unchanged inside PR#77, and PR#77 adds only the trust-boundary fix, order pins, docs and board records on top. PR#75 is redundant as a merge vehicle.
**Not closed yet** — per the closure preconditions (PR#72 merged + PR#77 accepted/merged + owner sign-off), PR#72 is still OPEN, so PR#75 stays open for now. Correct sequence remains: merge PR#72 → close PR#75 as superseded → retarget/reconcile PR#77 onto new main → fresh CI → merge PR#77.

## 15. MERGE DECISION

**BLOCKED-BY-PR72.** PR#72 is OPEN (rule 25: STOP — no merge of PR#77, no early merge of anything, no force-push, no rebase).

Pre-merge checklist status:
- [x] PR#77 HEAD verified (`566207c`), BASE correct (`arena/01a0d43c`)
- [x] stack/ancestry proven; PR#75 strictly superseded, history intact
- [x] task-183 ownership valid (active_in_review, canonical branch, live lease, no fence)
- [x] trust-boundary proof complete (source + runtime + adversarial + mutation, 4 evidence paths)
- [x] queue tampering blocked (27/27); server policy authoritative; no alternate CommandBus path
- [x] lifecycle gate order correct (4b between capability and policy); authorization boundary correct, no pack-state oracle
- [x] mutations M1–M8 killed and restored green
- [x] targeted tests green; local diagnostics green; exact-SHA CI green (all 4 workflows)
- [x] docs truthful; PR#67 byte-compatibility + load-bearing merge recipe verified
- [ ] **PR#72 merged** ← the only blocker
- [ ] post-PR#72: retarget PR#77 to new main, re-verify ancestry/diff/CI on the new SHA (fresh audit of the delta), then merge

Sections 29–31 (post-merge main proof / behavioral smoke on main / main CI) are **N/A — merge not performed** (by protocol, not by failure).

## 16. SECURITY FINDINGS

| # | Finding | Evidence | Verdict |
|---|---|---|---|
| S1 | Queue row could carry lifecycle privilege in PR#75 (`CreativeRenderPayload.allow_experimental` + surface flag) | PR#75↔PR#77 diff; PR#67 shape; PR body admits it | **Fixed in PR#77** (verified 4 ways) |
| S2 | Residual untrusted→privileged path for the opt-in | exhaustive symbol/AST/grep + runtime probes | **None found** |
| S3 | Second `CommandBus` construction path with opt-in | AST guard + grep (3 sites, 1 with flag, derived) | **None found** |
| S4 | Envelope-carried opt-in (`TypedCommand`) | `models.py` has no such field; injection → ValidationError | **Blocked** |
| S5 | Env-var/config override of policy | no settings/env path touches the set | **None found** |
| S6 | Pack-state oracle for unauthorized actors | 5 probe combos → identical AuthorizationError, 0 handler calls | **None** |
| S7 | PR body lacks the literal "deferred to gates owner" record for full gates (AGENTS.md §1.4 formality) | PR#77 body tail | **Minor, protocol-level** — mitigated by the recorded full local run + green exact-SHA CI; recommend adding the line |
| S8 | Worker bus uses the deprecated implicit-local-trust path (no authorizer) for claim-less commands | bus.py stage 3 comment; tracked as task-181 | **Pre-existing, documented, out of task-183 scope**; does not affect lifecycle privilege (proven) |

## 17. REMAINING WORK (real items only)

1. Merge PR#72 (owner: task-179 / PR#72 session).
2. After PR#72 lands: close PR#75 as superseded, retarget PR#77 to the new main, reconcile (PR#77 should merge cleanly — it contains PR#72's head; verify no new conflicts), re-run CI on the new head, re-verify the checklist on that SHA, then merge PR#77.
3. If PR#67 is merged afterwards, apply the recorded semantic recipe (guards will enforce it anyway).
4. Optional: add the "deferred to gates owner" line to the PR#77 body (S7).
5. Pre-existing (not this PR): task-181 explicit-grant migration for runtime bus call sites (S8).

---

### Verdict

**PR#77 is an accepted, evidence-complete implementation of the task-183 trust boundary.** The claim "no untrusted queue data can determine lifecycle privilege, and Gate 2 (stage 4b) remains the only valid execution boundary" holds across four independent evidence paths: production source, runtime behavior (27/27 adversarial probes), mutation testing (8/8 killed), and exact-SHA GitHub CI. The previous design (PR#75/PR#67) is superseded with its history preserved intact. Merge is blocked only by PR#72 being open — by protocol, not by any defect.

**MERGE DECISION: BLOCKED-BY-PR72** (merge-ready the moment PR#72 lands + retarget + fresh CI).
