# Verification Closure & Cross-PR Truth Gate — GAP report (task-180, Agent D, 2026-09-24)

Mission principle: **Execution success ≠ Job success ≠ Artifact verified.**
Nothing in this report is PASS by prose; every PASS names an executable test
that can be re-run. Where evidence does not exist, the report says MISSING or
BLOCKED instead.

Base: `main` @ `035a896` (PR#65 merged) + PR#71 head `9e9c753` (fast-forward;
task-178's contract is preserved commit-for-commit — nothing was redesigned).
See [CROSS_PR_TRUTH_2026-09-24.md](CROSS_PR_TRUTH_2026-09-24.md) for the
merged-vs-branch matrix, and
[VERIFICATION_TRUTH_MATRIX.json](VERIFICATION_TRUTH_MATRIX.json) for the
machine-readable chain matrix.

---

## GAP-A — `slideshow_render` (CLOSED)

**Before (measured):** PR#71's built-in registry contained only
`creative_render`; `slideshow_render` rows completed on the handler's word
("historical unverified semantics" — `default_artifact_verifiers` docstring).
A slideshow handler returning `success=True` with a missing/zero-byte/tampered
master would have been marked COMPLETED. PR#71's own board note listed this
as a known GAP.

**What the artifact really is:** the encoded master `.mp4` written by
`worker_adapter._render_sync` to the dispatched, workspace-contained
`output_path`, with runtime-measured `content_sha256` / `duration_us` /
`size_bytes` (the claim dialect already existed in
`ArtifactClaim.from_handler_result` — only the wiring was missing).

**Closure (wiring + smallest real verifier):**
`jobs.creative_verification.slideshow_render_verifier` — expected path =
dispatch-time `payload["output_path"]` (resolved, matching `_inside`'s
normalization), containment in `payload["workspace_dir"]`, size > 0,
recomputed sha256 equals the claim, and the runtime's own allow-listed-binary
media probe. Registered in `default_artifact_verifiers()`.

**Proof:** `tests/integration/test_verification_gap_closure.py::
test_gap_a_slideshow_chain_completes_only_with_verified_artifact` — REAL
queue + REAL handler + REAL FFmpeg encode (bundled static binary) + REAL
probe; the COMPLETED row carries `artifact_verification.status = verified`
with re-measured digest and probe facts. Attack E (success without artifact)
proves the historical hole is closed: FAILED with
`verification_failed:success_without_artifact_claim` **against the default
registry** (nothing unregistered).

---

## GAP-B — `pdf_extract` (CLOSED — with one honest behavior change)

**What the real artifact is (measured, not assumed):** the job's only
*measurable* output is the **extracted text layer** (real `pypdf`
extraction). The RAG ingestion that follows is an external side effect on an
LLM-backed engine — there is no deterministic artifact to re-measure there,
so it is deliberately **not** claimed as verified (industry rule: absent
independent evidence ⇒ pending, never done).

**Artifact dialect (new, explicit):** exists → size > 0 → exact expected
path → containment → recomputed sha256 → whole-file UTF-8 decode. The
expected path is derived from the **payload alone**
(`<file stem>.extracted.txt` next to the source PDF —
`jobs.feature_verification.pdf_text_artifact_path`), so a handler cannot
relocate its own artifact. Publication is atomic (temp in-dir → `os.replace`;
a crash never leaves a truncated artifact at the expected path).

**Closure:** `worker.process_pdf_job` now persists the extracted text and
claims path/sha/size; `pdf_extract_verifier` is registered by default.

**Behavior change (recorded, not hidden):** an extraction that yields an
empty text layer (e.g. image-only PDF) now FAILS with
`verification_failed:empty_artifact` instead of reporting
"Successfully processed" — that was a false success by the mission's own
definition. RAG-failure semantics are unchanged
(`test_pdf_extract_rag_failure_still_fails_job`).

**Proof:** `test_gap_b_pdf_extract_completes_with_verified_text_artifact` —
real offset-correct PDF, real pypdf extraction ("Hello PDF world", 15 chars),
real verification block; the RAG engine is the ONLY double. CLI drain path
proven by `tests/unit/test_jobs_cli.py::test_resume_drains_seeded_pdf_job`
(unchanged, green).

---

## GAP-C — `story` (CLOSED)

**What the real artifact is (measured):** a locally rendered **PNG image**
(1080×1920, Pillow, deterministic, offline — `features/story_gen.py`; the
"AI" in the class name is historical, no LLM/network is involved). The old
handler returned only `{"output_path": …}` — no digest, nothing to
cross-check.

**Artifact dialect (new, image-specific — not a guess-generalized media
verifier):** exists → size > 0 → exact dispatch-time path → containment in
the dispatched directory → recomputed sha256 equals the handler's new claim
→ Pillow structural decode (format/width/height/mode recorded as probe
facts).

**Closure:** `worker.generate_story_job` now claims measured sha256 + size;
`story_verifier` registered by default.

**Proof:** `test_gap_c_story_completes_with_verified_png` — ZERO doubles in
the entire path (real generator, real bytes, real verifier, real queue).

---

## GAP-D — Legacy `/creative` HTTP lane (NOT closed — formal GAP, zero code change)

The lane (`POST /creative/video-edit`, `GET /creative/jobs/{id}`) is frozen
+ deprecated by D-0010; the mission forbids migrating or redesigning it.
Task-180 converted the six GAP-D questions into **executable evidence**
(`tests/architecture/test_legacy_lane_verification_gap.py`, read-only):

| Question | Answer (evidence) |
|---|---|
| Still reachable? | Yes, code-reachable in the FastAPI app — both routes `deprecated=True`, behind the fail-closed HMAC gate (source-scan assertions) |
| Can it claim false success? | **Yes** — its own `JobRegistry` persists `done` with no artifact measurement (characterization test `test_legacy_registry_can_record_done_without_any_artifact` pins this) |
| Production surface? | **No** — the deployed container CMD runs `run-bot` (`test_legacy_lane_is_not_on_the_deployed_production_surface`); the API app is an optional secondary surface |
| Owner? | `task-165-legacy-creative-hardening` (delivered via merged PR#65); freeze enforced by `tests/architecture/test_legacy_creative_boundary.py` |
| Keep frozen/deprecated? | Yes — removal is sequenced after PR#58 lands (D-0010) |
| Gap transferred? | Recorded here as the formal GAP below |

**Formal GAP-D record**

* **owner:** task-165 successor / orchestrator (legacy-lane disposition owner; the lane's hardening was delivered by merged PR#65, its removal is explicitly sequenced after PR#58).
* **risk:** MEDIUM-LOW. A signed caller (HMAC key holder) of the *optional* API surface can receive `"done"` for a job whose output mp4 was never measured — execution success posing as job success. Not on the deployed production surface (bot), not reachable without the key.
* **dependency:** PR#58 (security-boundary hardening) must land before removal per D-0010; migrating the lane onto the canonical queue would change ownership/architecture — out of task-180 scope by mission rule.
* **acceptance test (for the future closure):** a legacy-lane job may only reach its terminal success state through a registered artifact verifier on the canonical queue, or the lane is removed; `test_legacy_registry_can_record_done_without_any_artifact` is the tripwire — closing the gap must delete/replace that characterization test deliberately.
* **evidence required:** CI-green PR removing the routes (or wiring them through `InProcessJobQueue` with a verifier), plus an updated `test_legacy_creative_boundary.py`.

---

## Adversarial evidence (attacks A–H, all against the DEFAULT registry)

| Attack | Scenario | Expected | Result (test) |
|---|---|---|---|
| A | handler `success=True`, artifact absent | FAILED | ✅ `verification_failed:missing_artifact` — `test_attack_a_success_without_artifact_fails` |
| B | artifact exists, zero bytes | FAILED | ✅ `verification_failed:empty_artifact` — `test_attack_b_zero_byte_artifact_fails` |
| C | valid artifact outside expected location | FAILED | ✅ `verification_failed:unexpected_artifact_path` — `test_attack_c_artifact_outside_expected_location_fails` (+ unit relocation tests; `outside_expected_root` covered in PR#71's M-series) |
| D | sha changed after the claim | FAILED | ✅ `verification_failed:size_mismatch`/`sha256_mismatch` (typed, fail-closed) — `test_attack_d_sha_changed_after_write_fails`; sha-path pinned by `test_story_verifier_tampered_sha_fails` + PR#71 M4 |
| E | slideshow success without verifier coverage | FAIL | ✅ FAILED `success_without_artifact_claim` against the default registry — `test_attack_e_slideshow_success_without_artifact_fails` + registry ratchet |
| F | same key + same payload | same job, one effect | ✅ one row, handler ran once — `test_attack_f_same_key_same_payload_is_one_job_one_effect` |
| G | same key + different payload | conflict | ✅ same job, first payload wins, structured conflict log `job_idempotency_payload_conflict` — `test_attack_g_same_key_different_payload_conflicts_first_wins` (semantics = PR#71's first-dispatch-wins contract) |
| H | re-verify a valid artifact | same deterministic result, no destructive side effect | ✅ identical summaries, artifact digest unchanged — `test_attack_h_reverification_is_deterministic_and_read_only` |

## Mutation evidence (3 real mutations, each killed, all reverted)

| # | Mutation | Killed by | Reverted |
|---|---|---|---|
| 1 | `verifier = None` in the queue dispatch (bypass registry) | 11 tests failed (attacks A–E, all three gap proofs, pdf/story queue tests) | ✅ `grep -c MUTATION` = 0 |
| 2 | expected-path check disabled in `verify_artifact` | 3 tests failed (attack C + 2 unit relocation tests) | ✅ |
| 3 | sha comparison disabled in `verify_artifact` | 2 tests failed (unit sha-tamper + PR#71's own M4) | ✅ |

## Regression evidence

* `pytest -q -m "not slow"` → **1970 passed, 20 skipped** (baseline before
  task-180 edits on this branch: PR#71's suite green at 37/37 locally) +
  task-180's 70 new/updated tests green; the 4 initially-red tests were the
  honest contract ripples (result-dialect assertions) — strengthened, never
  deleted or skipped.
* `ruff check .` → All checks passed; `ruff format --check .` → clean;
  `mypy src` → no issues in 237 files.
* `scripts/agent_board.py check --files <changed> --branch arena/01a0d475…`
  → `no overlap — safe to proceed` (exit 0).
* No flaky test was skipped or deleted. The mocked-encoder flow suite keeps
  its documented FFmpeg-independence via the queue's official
  `artifact_verifiers={}` opt-out, cross-referenced to the real-encode proof.

## Final gate

**READY_WITH_NONBLOCKING_GAPS**

* Closed with executable proof: GAP-A, GAP-B, GAP-C (+ the registry ratchet
  that makes regression structurally visible).
* Open, recorded, owned: **GAP-D** (legacy lane — frozen by design, owner +
  acceptance + tripwire recorded above) and the pdf_extract **RAG-ingestion**
  receipt (external side effect, NOT_APPLICABLE for artifact verification —
  honestly unverified rather than falsely green).
* Merge dependency: this branch stacks on **PR#71** (unmerged at report
  time); it must land after or together with #71.
