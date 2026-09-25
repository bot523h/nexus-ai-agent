# NAGAR / NEXUS — Master Session 2026-09-25 (task-191)

Branch `arena/01a0da5a-nexus-ai-agent` → **PR #88** · base `main 2351f09` · board claim `92cf5be`.
Mission: close the real REDs → pin main truth → prove a real artifact → first vertical slice.
Every statement below names the command that produced it. "Local" means sandbox, Python 3.11.2.
"CI" means GitHub Actions on an exact SHA.

**Verdict:**
- `#86 → superseded by #88`: root cause fixed, mutation-proved, exact-SHA CI in §11.
- `#87 → NEEDS_FIX`.
- `artifact chain → L4_PROOF_PARTIAL`: the artifact leg is verified; master is simulated; L4 = 0.
- `release → RELEASE_NOT_READY`.

---

## 1. LIVE_MAIN

- `main = 2351f099e2f789b80de05c6b382554b5cdcec0f3` (Merge #82).
- Main's own CI: run `36176954176` = **success** (push).
- The latest `maintenance` schedule run (`36115146739`, on `52ab7e8`) = failure. It predates main's head and is outside this session's scope (deferred, §14).

## 2. OPEN_PR_MATRIX (at session end)

| PR | Head | vs main | CI | Session verdict |
|---|---|---|---|---|
| #88 (this) | see §11 | on `2351f09` | §11 | supersedes #86; artifact-proof slice; #87 probe |
| #87 Studio Core | `2ed4495` | MERGEABLE | 11/12. `python-parity (3.10)` fails, **undiagnosed**: logs unreachable, GitHub refused a rerun | **NEEDS_FIX**: 4 contract invariants violated (§6) |
| #86 PACK-SEC-001 | `2683d6d` | MERGEABLE | RED: lint, test, parity ×3 | root cause found, fixed in #88 (§5); left open for its owner |
| #84 Constitution | `493f405` (moved twice during this session) | MERGEABLE | pending on the new head | **read-only**: another agent is active; not re-designed, not touched |
| #83 Operation Truth | `372643a` | CONFLICTING, **only** `.agents/board.json` | green on its base `52ab7e8` | integration recipe + drift hazard recorded (§12) |
| #80 Gate-5 alt | `4f1da98` | CONFLICTING, 22 behind | — | **CLOSED** this session: superseded by #81, provenance comment, branch kept |
| #57–#70, #73 | — | CONFLICTING | — | out of scope (deferred, §14) |

## 3. TODAY_ALREADY_ACCEPTED (baseline, not re-done)

- #79 (S3/S5 security)
- #81 (Gate 5: fencing + recoverable publication)
- #82 (CI extras matrix + 3.10–3.12 parity + release lineage)

All three are on main. This session used them as-is:
- #81's `creative_render_verifier` is the queue verifier in §9.
- #82's matrix is the parity gate in §11.

## 4. RED_ITEMS

| Item | State at session end |
|---|---|
| #86 exact-SHA RED | root cause fixed in #88; see §11 for #88's CI |
| #87 3.10 parity leg | RED, undiagnosed (no log access) |
| #87 contract | 4 invariants violated (§6) |
| #84 | CI pending on a head that is still moving |
| #83 | board-only conflict; becomes a truth-gate drift when combined with #88 (§12) |
| `delivery.render_master_4k` | **simulated master** with a misleading `content_sha256` (§7) |
| `lane_ir_hash` | location-dependent: includes the absolute media path (§9) |

## 5. #86_SECURITY_ROOT_CAUSE

**Environment evidence.**
- `gh api .../actions/jobs/<id>/logs` and `gh run download` both fail with Azure blob `EOF`. CI logs can't be read from this sandbox.
- So the failure was **reproduced locally at the exact SHA**:
  - `git worktree add /tmp/wt86 2683d6d`
  - `PYTHONPATH` pinned to the worktree, so the editable install can't shadow it.

| CI job | Local reproduction | Root cause |
|---|---|---|
| `lint` | `mypy src` → `registry.py:157: error: Non-overlapping equality check (left operand type: "Literal['placeholder', 'format_only_unverified']", right operand type: "Literal['verified']") [comparison-overlap]` | The guard compared against a state **no verifier can emit**. There is no Ed25519 verifier. |
| `test`, `python-parity 3.10/3.11/3.12` | `pytest -m "not slow"` → `1 failed, 2314 passed`. The failure is `test_external_pack_with_unverified_signature_cannot_activate`: `ValidationError: capability 'media.play' is not in the 'nexus.external.test' namespace` | The regression fixture was invalid. The test died in `model_validate` and **never reached `activate()`**, so it was RED with or without the guard: not a discriminating test. |

**Fix (commit `20c8855`, only the root cause):**
- `verify.py`: added a `SignatureState` alias and `TRUSTED_SIGNATURE_STATES: frozenset[str] = frozenset()`. The set is empty by construction and documented as such.
- `registry.py`: the guard now reads `signature_state not in TRUSTED_SIGNATURE_STATES`.
- Tests:
  - valid fixture (`nexus.media.external_probe` + `media.play`, zero pending capabilities);
  - a registration-time `activate=True` refusal;
  - a **builtin control arm**: the same manifest activates, so the guard is the *only* thing that refuses;
  - a pin that the trusted set stays disjoint from every emittable state.

**Mutation proof** (`pytest -q tests/unit/test_pack_manifest_verify.py`, baseline `41 passed`):

| Mutant | Result |
|---|---|
| M1: guard removed (`False and pack.anchor == "external"`) | `2 failed, 39 passed` |
| M2: trusted set widened to `{"format_only_unverified","placeholder"}` | `3 failed, 38 passed` |
| M3: anchor inverted (`== "builtin"`) | `4 failed, 37 passed` |
| byte-identical restore (`cmp`) | `41 passed` |

**Other checks:**
- Pack-registry suite (every test file that imports `creative.packs`, 30 files) → `462 passed, 10 skipped`.
- `ruff check .` / `ruff format --check .` / `mypy src` → clean.

**Not claimed:**
- cryptographic verification;
- a trusted publisher/key chain.

External packs are **refused**, not verified. `docs/architecture/SECURITY.md` T11 now says exactly that. The #86 audit file carries a dated correction block, with its original text kept verbatim.

**Python 3.10/3.12 locally:** unavailable. `uv python install` is blocked by TLS interception of GitHub release assets. Parity evidence therefore comes only from the exact-SHA CI matrix (§11).

## 6. #87_CONTRACT_TRUTH

**Method.** `scripts/pr87_contract_probe.py` (on #88) runs from a **detached worktree** of `2ed4495`. No file in #87's zone was edited:

```
cd /tmp/wt87 && PYTHONPATH=/tmp/wt87/src pytest -q scripts/pr87_contract_probe.py
```

Result: `4 failed, 7 passed`.

| Invariant (brief, Rule 6) | Result | Evidence |
|---|---|---|
| D1 registry change → identity changes | PASS | `surface_identity` differs after a capability version bump |
| **D2 stale `surface_identity` → reject** | **VIOLATED** | no enforcement point. `TypedCommand` is `extra="forbid"` with no identity field, and nothing compares a planned identity with the live surface |
| D3 stale snapshot after a major bump → reject | PASS | `CapabilityVersionError` |
| D4 excluded op → not advertised and cannot dispatch | PASS | `PACK_STUB` exclusion; dispatch refused; `state_revision` unchanged |
| P1 preview realization → never master | PASS | stamp `authoritative=False` |
| **P2 forged `authoritative=True` → insufficient** | **VIOLATED** | a hand-built `CommandResult` → `is_master_evidence` is `True` |
| P3 missing stamp → reject | PASS | |
| **P4 preview result restamped → not master** | **VIOLATED** | `model_copy(update=diagnostics…)` → `True` |
| **P5 verifier absence → no master/L4** | **VIOLATED** | a no-op local execution with no artifact counts as "master evidence". The function is a *mode stamp*, not artifact evidence. |
| Agent 2: discover → plan → snapshot → command → dispatch → result → undo | PASS | the state hash is restored exactly |
| Agent 3: register → compose → execute → result stamp | PASS | (the publication leg is covered by main's queue verifier, not by #87) |

**Mutation against #87's own suite** (5 test files, baseline `148 passed`):

| Mutant | Result |
|---|---|
| weaken master gate (unstamped counts) | `1 failed` |
| bus always stamps `authoritative=True` | `2 failed` |
| `authoritative_for` preview always True | `3 failed` |
| dispatch excluded op (remove bus pack gate 4b) | `9 failed` |
| remove the capability-availability check | `3 failed` |
| disable stale-identity guard | **cannot run: the guard does not exist** |
| restore | `148 passed` |

**Verdict:** **NEEDS_FIX**, not CONTRACT_VERIFIED. Fix direction is Agent 01's call and is posted on #87. The contract-verification mutation set is incomplete by construction until a stale-identity guard exists.

## 7. AGENT_02_REALITY_CHECK (Rule 8)

| Surface | What exists on main | Classification |
|---|---|---|
| Preview (`ExecutionPolicy.mode="preview"`) | an enum value. On main no operation advertises preview; `preview_semantics` exists only in #87 | **contract / metadata** |
| Preview as media | `delivery.make_proxy_480p` goes through the real render lane. §9 verifies an 854×480 artifact | **real render, verified** |
| Master (`delivery.render_master_4k`) | handler writes an `AssetRecord` with `is_master: True`, `render_engine: "nagar.delivery.ffmpeg.native.v1"`, and `content_sha256 = sha256(render-spec string)`. **No file is rendered.** Proven by `master_reality_check()` in §9 | **SIMULATED**, and the record's hash field misleads (deferred to the delivery-pack owner) |
| Shared Playhead / Range / Undo | state edits through the real CommandBus; undo proven on the executing bus (§9) | executable, state-only |
| Persian RTL / Student Mode / Guidance / Web cockpit | not exercised this session | **implemented ≠ verified**: no evidence produced |

"Master Ready" is **not** claimed anywhere.

## 8. L4_ARTIFACT_CHAIN

| Stage | Component (shipped unless noted) | Status |
|---|---|---|
| Real media | deterministic ffmpeg fixture (testsrc2 640×360@25 + sine, bitexact) | REAL. Generated twice → identical sha256 |
| Discovery | `build_runtime_registry().describe("timeline.trim")` + `pack_lifecycle` | REAL (main's Gate-2 view; `nagar.discovery.v1` is #87, not on main) |
| Intent | `/edit trim 0.5 2.5` → `SURFACE_TO_CANONICAL` | REAL |
| TypedCommand | `render_jobs._dispatch` builds `TypedCommand` (`nagar.command.v1`) | REAL |
| CommandBus | `CommandBus.dispatch`, full Gate-2 pipeline | REAL. Captured by a pass-through recorder (no mock) |
| Runtime | `creative_render_job` (production worker adapter) | REAL |
| Renderer | Lane IR → `render_lane` → FFmpeg 7.0.2 (imageio-ffmpeg) | REAL |
| Preview | `delivery.make_proxy_480p` | REAL, verified |
| **Master** | `delivery.render_master_4k` | **SIMULATED** |
| Artifact | `output.mp4` | REAL |
| Verifier | queue `creative_render_verifier` + independent (§10) | REAL |
| Hash / reopen | stdlib sha256; second full pass bit-identical | REAL |
| Provenance | command, transaction, revision, state hash, lane IR hash, mode, verification | REAL. Gap: the worker result itself omits command/transaction/revision |
| Undo | `system.undo` on the same bus | REAL (state only; published bytes are not retracted) |
| Operation Truth | #83 not on main; candidate entry emitted, not written | NOT INTEGRATED |

## 9. REAL_ARTIFACT_EVIDENCE

Command: `python scripts/l4_artifact_proof.py --out proof.json`, exit 0, local, at tree `bde0f9e` + the harness. Run twice.

**Reproducible across runs:**
- input sha256
- artifact sha256
- bus `state_hash`
- decoded-frame digest

**Not reproducible:**
- `lane_ir_hash`. Root cause: `LaneSource.path`, an absolute path, is hashed.
- 480p proxy bytes. Not root-caused.

| Field | Value |
|---|---|
| input sha256 | `sha256:de9a244ede9b728044286a8fd37a7b83a25e8626f67862bd356fb6e2a50b7092` (304 320 B) |
| command_id / operation / mode | `cmd-l4proof-timeline.trim` / `timeline.trim` / `local` |
| command input | `{"clip_asset_id":"src","in_point_us":500000,"out_point_us":2500000}` |
| bus result | `status=applied`, `state_revision=1`, `state_hash=sha256:4a9f3049…37ac4` |
| **artifact sha256** | **`sha256:e4ed981a160f079b826acdcb4c775666dc279c852ce2788d133819c33113d9be`** (505 948 B) |
| container (pure-Python walk) | `ftyp(isom) moov free mdat`; movie 2.000 s; tracks `vide 1280×720 2.000 s`, `soun 2.021 s` |
| full decode | exit 0, empty stderr, **60** video frames (= 2.0 s × `LaneProfile().fps` 30) |
| probe (`probe_video`) | 2 000 000 µs, 1280×720, audio present |
| queue verifier | `ok=True`, `reason_code=None` |
| reopen | identical = True; ok = True |
| undo | `undone_operation=timeline.trim`, revision 2, state hash changed, artifact bytes untouched |
| preview | `delivery.make_proxy_480p` → ok, 854×480, 90 frames, queue verifier ok |
| master | dispatch `applied`, record claims `is_master`, **no artifact path**, verdict SIMULATED |

**Note on expected properties.** Expected size and fps come from the shipped `LaneProfile()` (1280×720@30), not from the source (640×360@25). The render lane normalises every artifact. The harness's first run asserted the source shape and failed. That failure is how the fact was found.

## 10. VERIFIER_EVIDENCE

**Independent of the producer:**
- bytes: `hashlib`, not the runtime's `sha256_file`;
- container: a pure-Python ISO-BMFF walk, no FFmpeg;
- decode: every frame decoded via `-f framemd5`, exit code and stderr checked.

**Attacks the verifier must refuse** (`tests/integration/test_l4_artifact_proof.py`, 15 tests, all green):
- 4 KiB XOR inside `mdat`;
- truncation to one third;
- PNG bytes named `output.mp4`;
- a well-formed box stream without `ftyp`;
- the source shape as the expectation;
- a foreign hash claim.

**Harness mutation** (baseline `15 passed`):

| Mutant | Result |
|---|---|
| H1 verifier always `ok` | 3 failed |
| H2 hash check blinded | 2 failed |
| H3 `ftyp` rule removed | first 14/14 **survived** → added the non-MP4 box-stream test → 1 failed |
| restore | 15 passed |

## 11. EXACT_SHA_CI

**Local, py3.11:**
- `pytest -q -rs -m "not slow"` at the harness commit → `2333 passed, 30 skipped, 0 failed`. Baseline at #86 head: 2314 passed + 1 failed. The delta is +3 pack tests and +15 harness tests.
- `ruff check .`, `ruff format --check .`, `mypy src` → clean.

**CI on this branch:**
- `bde0f9e` (fix commits only): `lint (ruff + mypy)` = **success**. This independently confirms root cause #1 on CI. The remaining legs were cancelled by the next push (`cancel-in-progress`).
- `6dae667` (fix + harness + probe): superseded by the final head before the runners picked it up. Under `cancel-in-progress`, only the last head of the branch produces a complete run.
- **Final head** (the commit adding this document and releasing the board lease): its complete matrix (lint, test, extras ×4, python-parity 3.10/3.11/3.12, migrate-postgres, release-lineage) is recorded in the PR #88 comment titled **"EXACT-SHA CI"**, with the run id and per-job conclusions. The final commit changes only this file and `.agents/board.json` relative to `6dae667`.
- `SECURITY_VERIFIED` is claimed for PACK-SEC-001 **only if** that comment shows every leg green. Otherwise the status remains `NEEDS_FIX`.

## 12. OPERATION_TRUTH

- Truth is **unchanged**: 70 TDD ops · 57 runtime · 23 missing · **L4 = 0**. This session edited no truth file.
- #83's ladder defines L4 as L3 + artifact proof + a production-like measurement, and "no repository source defines or measures production-like". A sandbox/CI harness is not production-like. So even the verified artifact leg of §9 **cannot and must not raise L4**.
- The harness emits `proof_registry_candidate = {operation_id: timeline.trim, kind: artifact_evidence, method, recorded_at, source_revision}` in #83's `nagar.operation_proof_registry.v1` shape. It is not written anywhere, because the registry is not on main.
- **Integration hazard, measured:**
  - Merged tree = this branch + #83, with the board conflict taken from #83.
  - `python -m nexus_ai_agent.nagar --check` → **RED**: `evidence_drift` on `timeline.trim` / `media.play` / `delivery.make_proxy_480p` `suite_reference_files`, plus `proof_drift`.
  - `--docs` regenerates with **0 level changes over 80 ops** (L0 23 · L1 45 · L2 0 · L3 12 · L4 0), and `--check` returns GREEN.
  - Whichever of #83 / #88 lands second must regenerate.

## 13. BOARD_STATE

- Claimed `task-191-nagar-redclose-l4-proof` (new zone `nagar-redclose-l4-proof`, exclusive paths = exactly the files changed). Committed and pushed first (`92cf5be`), before any code.
- `agent_board.py check` before each code push → `no overlap`.
- `docs/README.md` is fenced by `task-181-gate5-closure`: owner `arena/01a0d5a1`, #81 merged, 48 h TTL until `2026-09-26T23:06Z`. So no new doc went under `docs/`; this report is at the root, per the existing `NAGAR_AGENT_03_FINAL_*` convention. The foreign lease was **not** released, because the protocol forbids it.
- `show` auto-gc'd two expired leases (`task-183-pr67-lifecycle-integration`, `sec-boundary-salvage-01a0d563`).
- `task-191` is released at session end. The PR stays open for review and the gates owner.

## 14. DEFERRED_ITEMS

1. #87 NEEDS_FIX, Agent 01's zone:
   - make master evidence unforgeable (bus-issued stamp bound to `transaction_id`/`state_hash` and checked against history);
   - add a stale-`surface_identity` enforcement point;
   - separate "authoritative state" from "verified artifact".
2. #87 `python-parity (3.10)`: needs log access or an owner rerun.
3. `delivery.render_master_4k`: either render and verify a real file, or stop writing `is_master`/`render_engine`/`content_sha256` claims on a spec hash. Delivery-pack owner.
4. `lane_ir_hash` includes the absolute media path: make the IR identity content-addressed. Rendering owner.
5. The worker result omits `command_id` / `transaction_id` / `state_revision`: add them to the provenance contract (task-181 zone, still leased).
6. #83: resolve the board conflict, regenerate truth after #88 (or vice versa), then ingest the `artifact_evidence` candidate.
7. #84: verification only, after its head stops moving.
8. `task-181-gate5-closure` zombie lease: owner release, or auto-gc after 2026-09-26T23:06Z.
9. Cryptographic pack signatures (Ed25519 + publisher key chain): not started; external packs stay refused.
10. `maintenance` schedule failure on `52ab7e8`; stale conflicting PRs #57–#70, #73.

## 15. NEXT_10_TASKS

1. Merge #88 once its exact-SHA CI is green (gates owner). Then #86 can be closed as superseded.
2. #87: fix P2/P4/P5 (unforgeable, artifact-bound master evidence) and add the D2 guard. Re-run `scripts/pr87_contract_probe.py` → 11/11, plus the stale-identity mutant.
3. #87: diagnose the 3.10 parity leg with log access.
4. #83: rebase or merge on the new main, resolve the board conflict, `--docs`, ingest the artifact-evidence candidate (L4 still 0).
5. Delivery: a real `render_master_4k` (4K file) verified by `scripts/l4_artifact_proof.py`-style checks, or retract its claims.
6. Rendering: a content-addressed `lane_ir_hash`.
7. Job contract: put command/transaction/revision in the worker result, so provenance doesn't need a recorder.
8. Define a *production-like* measurement source (the environment and what's measured). Only then can L4 be computed.
9. RTL / Student Mode / cockpit: produce evidence (browser E2E) or keep them marked unverified.
10. Pack signatures: an Ed25519 verifier + trusted key chain, extending `SignatureState` and `TRUSTED_SIGNATURE_STATES` together.

---

### What is real, what is simulated, what is L4

- **ON MAIN:** #79, #81, #82 (plus everything before them).
- **OPEN:** #88, #87, #86, #84, #83 (#80 closed).
- **VERIFIED (this session):**
  - the PACK-SEC-001 refusal (mutation-proved);
  - the `timeline.trim` artifact chain and the 480p proxy (independent verifier, reopen, hash);
  - #87's pass-set of 7 invariants.
- **ONLY IMPLEMENTED:** #87 discovery / preview semantics; RTL, Student Mode, guidance, cockpit (not exercised).
- **REAL:** fixture → bus → render lane → artifact → verification.
- **SIMULATED:** master 4K; "preview" as an execution mode on main.
- **L4:** none.
- **NOT L4:** everything, including the verified artifact leg.
- **Why:** the canonical ladder requires a production-like measurement that no source defines.
