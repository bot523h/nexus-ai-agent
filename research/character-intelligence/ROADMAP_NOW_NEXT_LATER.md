# Roadmap — NOW / NEXT / LATER

Every item carries the status vocabulary of this mission. Nothing in NEXT or LATER
is claimed to exist.

## NOW — done in this mission (all executed here)

| item | status | evidence |
|---|---|---|
| capsule model with identity/revision scoping and typed refusals | VERIFIED | `tests/character_capsule/test_capsule_model.py` (16) |
| wardrobe timeline + era rules + plan checking | VERIFIED | same file + scenario 11 |
| canon engine (immutable/mutable facts, story ledger, conflicts) | VERIFIED | scenario 10 |
| measurement pipeline (face/body/hair/wardrobe/scene/voice/motion) | VERIFIED | `test_vision_measurement.py` (9) |
| continuity engine with 8 states, precedence, quality gates | VERIFIED | `test_continuity_states.py` (14) |
| calibration fitted on a real corpus, evaluated on a held-out character | VERIFIED | `calibration.json`, `BENCHMARK_RESULTS.md` |
| 12-scenario benchmark, deterministic corpus | VERIFIED | 12/12, corpus digest reproduced |
| evidence bundles with hard binding + optional signature | VERIFIED (UNSIGNED here) | `test_evidence_bundle.py` (11) |
| encryption at rest, consent ledger, access policy, audit trail | VERIFIED (prototype) | `security.py` sealed-blob tests |
| three CLIs (benchmark, verify, check evidence) | VERIFIED | `scripts/character_capsule/` |
| Arena 1 compatibility report | VERIFIED | zero protected-path changes |

## NEXT — highest value, still cheap (each ≤ a day of work)

1. **Gallery observation cache** — cut per-shot verification from 249 ms to < 25 ms
   (pure refactor, no schema change). Status: EXPERIMENTAL (designed, not built).
2. **Hair-change samples** in the corpus (3 per character) so hair gets a calibrated
   threshold instead of being advisory. Status: EXPERIMENTAL.
3. **Occlusion cases** (scarf over coat, hair over face) to measure how wardrobe and
   hair measurement fail. Status: EXPERIMENTAL.
4. **Second and third look-alikes**, to turn a single hard impostor into a FAR curve
   with more than ten samples. Status: EXPERIMENTAL.
5. **Signed calibration artifacts** so an attacker cannot rewrite thresholds.
   Status: EXPERIMENTAL (design in `THREAT_MODEL.md` T-calibration).
6. **SFace embedding seam** — implement the `embedding` strategy behind the existing
   `distance()` interface, licence-clean (Apache-2.0) with the provenance caveat.
   Status: EXPERIMENTAL.

## LATER — needs decisions or resources we do not have

| item | why later |
|---|---|
| photographic corpus + human review study | needs consented data and reviewers; `NOT_MEASURED` today |
| C2PA container/signing with a real certificate chain | needs a CA process and a trust list |
| body descriptor decoupled from pose/wardrobe | needs a differentiable body model (GPU) |
| real video (ffmpeg + true motion) | ffmpeg absent; gait is synthetic |
| multi-tenant key custody / HSM | infrastructure decision |
| Arena 1 adapter (`/v1/verify` client) | blocked on Arena 1's own contract; proposal only |
| season-scale scheduling / cost model | needs a generation budget that this mission excludes |

## Explicitly out of scope (by decision, not by accident)

Video render engine · universal creative runtime · composition compiler · new
queue/CommandBus/registry · paid API dependency · full season · character
marketplace · payments. Each of these is `DEFERRED` with the reason that it belongs
to a different owner.

## Success criterion for the next reader

> Clone the branch, run two commands, and get 58 green tests plus a 12-scenario
> benchmark whose failures are named rather than hidden.

If a future change makes the benchmark pass *harder* scenarios while keeping the
held-out numbers honest, that is progress. If it only makes the table greener, it is
the opposite.
