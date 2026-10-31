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
