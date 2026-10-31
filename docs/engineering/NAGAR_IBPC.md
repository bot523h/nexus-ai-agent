# NAGAR IBPC — Intent-Bound Proof Capsules for live editing (mission NAGAR-IBPC-LIVE-EDITING-01)

Status: **M0–M2 implemented, M3–M5 in progress on branch `arena/7622c61d-nexus-ai-agent`** (Draft PR).
Audit basis: [NAGAR_LIVE_EDITING_LIVE_AUDIT.md](NAGAR_LIVE_EDITING_LIVE_AUDIT.md) (M0 — 21-component inventory, real execution path, calibration).

The slice adds **proof-carrying live editing** on top of the existing canonical chain. It adds no new
execution authority: there is no second CommandBus, queue, renderer, registry or provenance store, and
no LLM ever picks the actor, permission, queue, renderer or path.

## The chain (one turn)

```
intent text / voice audio            (untrusted input; stored only as sha256 digest)
  -> nagar.ibpc.intent               deterministic parser — asks (Clarification), never guesses
  -> nagar.ibpc.models.TypedCreativeAST   frozen typed contract, ast_digest
  -> nagar.ibpc.compiler             validates against the LIVE injected CapabilityRegistry
  -> CompiledPlan                    plan_digest binds capability + input-schema digest
  -> nagar.ibpc.preview              plan_derived PreviewManifest + Readback (typed fields only)
  -> nagar.ibpc.approval             single-use ApprovalBinding: principal + revision + all digests
  -> nagar.ibpc.session              fenced turn (sequence, revision, transcript state machine)
  -> nagar.ibpc.service.execute      re-validates everything, consumes the approval ONCE,
                                     enqueues ONE canonical `creative_render` job through the
                                     injected JobQueuePort, drains, re-measures the artifact
  -> nagar.ibpc.footprint            independent verifier on DECODED PIXELS of the real artifact
  -> IBPCCapsule                     binds intent/ast/scope/capability/plan/preview/approval/
                                     attempt/artifact/verifier digests + existing journal+passport
```

## Red lines → where each is enforced (all by tests)

| Red line | Mechanism | Proving tests |
|---|---|---|
| Intent ≠ Authority | parser emits AST/clarification/refusal only; execution requires a human approval binding | `test_ibpc_intent_compiler.py`, `test_ibpc_e2e.py` |
| Voice ≠ Execution | transcript is parse input only; PARTIAL can never parse; FINAL still needs approval | `test_ibpc_session.py`, voice e2e |
| Preview ≠ Approval | preview digest is bound into the approval; tampering it refuses consume | `test_ibpc_approval.py` |
| Approval ≠ Permission | the binding grants nothing; the canonical Policy/CommandBus/queue still authorize | `test_ibpc_intent_compiler.py` (registry gate) |
| Enqueue ≠ Render ≠ Verification | three separate stages; the capsule is minted only after the independent verifier ran on the real artifact | `test_ibpc_e2e.py` |
| Model Output ≠ Evidence | no model on the path at all in this slice; evidence = digests + journal + passport | boundary test |
| Digest ≠ Proof of Preservation | PASS requires **bit-exact** kept-window frames; lossy ⇒ INCONCLUSIVE; provable change ⇒ FAIL | `test_ibpc_footprint.py` |

## Semantics correction (recorded honestly)

The canonical `timeline.trim` (TrimInput `in_point_us`/`out_point_us`) is a **keep window**: output
duration = `out − in`; the complement is removed (verified against the real lane: keep `[0.5s,1.5s)` of
a 2 s@30 fps source → 30 frames mapping to source frames 15..44). The scope model therefore uses
`effect="keep"` (preserved window) / `effect="modify"` (in-place change allowed, rest preserved), and
the readback states the keep/remove split explicitly so the approved preview is the executed truth.

## Verifier calibration (2026-10-31, ffmpeg 7.0.2-static)

`testsrc` 320x240@15 / 1280x720@30, libx264: unchanged-content re-encode at crf 20 → per-frame
mean|Δ| ≤ 0.47 (max 63); a real brightness change → mean|Δ| ≈ 31. `NOISE_MEAN_MAX = 4.0` sits between
with ~8× margin. PASS = bit-exact on all mapped pairs **and** proven intended change; INCONCLUSIVE =
codec noise (never upgraded); FAIL = provable violation. Frame mapping is discovered by decoded-content
matching (downsampled cost matrix, strictly-increasing argmin), never by time math alone; the kept
window is checked with ±1 boundary frame of codec rounding.

## Fencing rules (session.py)

Closed transition table; PARTIAL transcripts have no path to parsing; duplicate `turn_id` refused;
sequence ≤ session head ⇒ STALE (approvals revoked, all later ops refused); turn pinned to a stale
project revision refused; interrupt/cancel revoke the turn's approvals and an EXECUTING turn cannot be
interrupted; approvals are single-use (APPROVED→CONSUMED, replay refused) and bound to
principal + revision + ast/plan/preview/asset digests.

## Privacy (privacy.py)

`LOCAL_VAULT` is the default; `TRUSTED_CLOUD`/`EXPERIMENTAL_CLOUD` require an explicit recorded
`ConsentRecord` — and even then no provider is wired in this slice, so any egress is a typed refusal.
Logs/records carry transcripts and audio as digests only (`redact_mapping`).

## Package map (`src/nexus_ai_agent/nagar/ibpc/`)

| Module | Role |
|---|---|
| `canonical.py` | re-exports the single canonical JSON/digest functions (no second spelling) |
| `models.py` | frozen pydantic contracts + schema IDs (`nexus.ibpc.*.v1`) |
| `intent.py` | deterministic en/fa trim parser; Clarification/Refusal; digest-only intent |
| `compiler.py` | AST → registry-validated CompiledPlan (fail-closed gates) |
| `preview.py` | plan-derived PreviewManifest + Readback |
| `approval.py` | single-use digest-bound ApprovalLedger |
| `session.py` | LiveSessionManager + VoiceTurn fencing |
| `privacy.py` | modes, consent, redaction |
| `footprint.py` | the ONLY subprocess owner: read-only decode + content-matched verification |
| `service.py` | IBPCService composing injected ports only (queue/registry/journal/facts/caption) |

Boundary enforcement: `tests/architecture/test_ibpc_boundary.py` (no imports of adapters/worker/
render_jobs/bus/pack-runtime; subprocess only in footprint; queue constants pinned against the canonical
worker's own map; every module imported by name from the package `__init__`).

## Status enum (final report will use)

`VERIFIED` / `VERIFIED_WITH_LIMITATIONS` / `HARDENED_BUT_NOT_COMPLETE` / `BLOCKED` / `DEFERRED` —
current slice state is tracked per milestone in the PR body, not here.

## Security closure (mission NAGAR-IBPC-SECURITY-CLOSURE-01, 2026-10-31)

Scope: the four IBPC trust boundaries — `session.py` (audio ingress), `approval.py` (binding ledger),
`models.py` (frozen contracts), `service.py` (identity resolution / execution). Every fix below is
covered by adversarial tests and by a mutation that provably turns those tests red.

### Threat model (Phase A — confirmed in code before fixing)

| Threat | Entry point | Root cause (before) | Impact | Fix | Regression test |
|---|---|---|---|---|---|
| Path traversal via caller-supplied `turn_id` | `LiveSessionManager.submit_audio` (`audio_dir / f"{turn_id}.bin"`) | turn id used directly as a file name | write/overwrite ANY file the process can write (`../../`, absolute, backslash) | `TURN_ID_RE = ^[A-Za-z0-9_-]{1,64}$` enforced in `begin_turn` + `submit_audio`; physical name is an independent random `audio_<32hex>.bin` | `TestTurnIdBoundary` (13 malicious ids, nothing created) |
| Predictable/colliding staging name overwrites a file | `_stage_audio` | fixed name ⇒ clobber | destroy another turn's or the user's file | `O_WRONLY\|O_CREAT\|O_EXCL` (never overwrites, even symlinks), up to 8 random attempts, typed `audio_stage_failed` | `test_oexcl_*`, `test_all_name_collisions_*`, `test_concurrent_staging_*` |
| Staged audio world-readable | `_stage_audio` | umask-dependent perms | other local users read voice audio | created 0600 **and** `fchmod(0600)` after open | `test_file_name_is_random...` (mode==0600 observed during engine), mutation `world_readable_staging` |
| Audio left on disk (success/exception/timeout/cancel) | `submit_audio` | no cleanup on failure paths | voice bytes persist past the turn | `try/finally` cleanup; `_cleanup_audio` unlinks ONLY the exact created regular non-symlink file inside the resolved root | `test_cleanup_on_{engine_exception,timeout,cancellation}`, decoy survives |
| Symlinked/spelled audio dir escapes containment | `_stage_audio` | textual `startswith` check | write outside the audio dir | `Path.resolve()`/`realpath` containment (`relative_to(root)` + `parent == root`) against the REAL dir | `test_containment_uses_resolved_real_path` |
| Approval replayed (double execution) | `ApprovalLedger.consume` | no single-use enforcement | one approval ⇒ N renders | atomic CAS APPROVED→CONSUMED under `RLock`; replay ⇒ typed `replay` | `test_replay_after_consume_is_refused`, `test_concurrent_consume_has_exactly_one_winner`, e2e `test_concurrent_double_execute_has_exactly_one_winner` |
| Cross-context approval use (other session/turn/tenant/project/principal/sequence) | `service.approve/execute` | trusted `approval_id` + `self._contexts[approval_id]` | execute a plan in the wrong scope | `_resolve_identity`: 8-step fail-closed validation; per-field typed `identity_mismatch_<field>` at service AND ledger | `TestServiceIdentityIsolation` (queue stays empty), ledger param test (11 fields) |
| Stale revision executes | `execute` | revision not re-checked | edit against moved project state | `source_revision` re-read at execute; mismatch ⇒ `stale_revision`, approval NOT consumed | `test_stale_revision_refused_no_job` |
| Source asset swapped after preview | `execute` | digest trusted from submit time | approved preview ≠ executed input | `sha256_file` re-hash at execute; `asset_unreadable`/`asset_changed` | `test_asset_changed_after_preview_refused_no_job` |
| Tampered AST/plan/preview in context | `execute` | context trusted | execute something other than what was approved | ctx digests re-compared to binding; `ast_mismatch`/`plan_mismatch`/`preview_tampered` | `test_tampered_preview_refused_no_job` |
| Deleted/vanished context | `execute` | `KeyError`/500 | crash or worse | `unknown_context` typed refusal, queue untouched | `test_deleted_context_fails_closed_no_job` |
| Invalid audio payload strands the turn | `submit_audio` | state transition before input validation | turn stuck in TRANSCRIBING | `_validate_audio` BEFORE the transition (size/type/25 MiB cap), `unknown_turn` typed guard | `test_invalid_audio_payloads_refused_and_stage_nothing` |

### Approval identity contract (Phase C)

`ApprovalIdentity` = principal + tenant + project + session + turn + sequence + source revision +
source asset digest + ast/plan/preview digests. It is checked at **approve** AND at **consume**
(ledger `_check_identity`, per-field typed errors + whole-digest check). Execution order is fixed:
**Context Validation → Session/Turn Validation → Binding Validation → Atomic Consume → Canonical
Enqueue**; a refused request never consumes the approval and never enqueues (asserted against a
recording queue port). `execute()` serializes the sensitive section with an asyncio lock; the
consume itself is the ledger's atomic CAS, so concurrent double-execute yields exactly one winner.

### Mutation evidence (Phase D)

`scripts/ibpc_security_mutations.py` — 13 source weakening mutations (turn-id regex disabled,
O_EXCL→O_TRUNC, 0600→0666+no fchmod, cleanup removed, tenant/sequence not compared, single-use
disabled, session/turn/principal/revision/asset-hash/preview checks removed). Result: **13/13
killed** (suite RED per mutant, restored GREEN after), exit 0, verified in the full `.venv` AND in
the minimal CI env (28 packages) — same interpreter recipe the `trust-mutations` CI job now uses.
Worktree unchanged after the run (`git diff --exit-code` in CI).

## Lifecycle & durability (Phase E — honest accounting)

| Entity | Current storage | After process restart | Multi-process | Production claim |
|---|---|---|---|---|
| Live sessions / turns | in-process dict in `LiveSessionManager` | **gone** — typed `unknown_session` (test: `TestPersistenceTruth`) | NOT shared — each process has its own; no cross-process fencing claimed | in-process only; production use needs the existing job-queue/DB ports first |
| Approvals (bindings) | in-process dict in `ApprovalLedger` | **gone** — typed `unknown_approval` | NOT shared; single-use is per-process | in-process only; no durability claim |
| Execution contexts (`_TurnContext`) | in-process dict in `IBPCService` | gone | not shared | in-process only |
| Staged audio files | temp files under the resolved audio dir, 0600, random name | cleaned in `finally` on every path; survivors only if the process is SIGKILLed mid-transcribe | per-call unique names; no cross-process delete | no persistence claim |
| Rendered artifacts / capsules / journal | EXISTING stores (workspace dirs, sqlite journal, job queue) — unchanged | durable via existing ports | via existing queue | existing claims only, no new ones |

NEXT (not done, deliberately): durable approval ledger on the existing sqlite journal; multi-process
session fencing via the job queue; then re-run the same mutation harness against the durable store.
No durability, multi-process or production-readiness claim is made for the in-memory IBPC state.
