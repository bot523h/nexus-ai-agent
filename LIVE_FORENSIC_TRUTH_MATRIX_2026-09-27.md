# Live Forensic Truth Matrix — 2026-09-27 (golden mission, PHASE 0)

**Session branch:** `arena/01a0e356-nexus-ai-agent`
**Measurement time:** 2026-09-27 ~14:45–16:45 UTC (all values re-measured live; nothing taken from prior reports without re-verification)
**Method:** `git` (full history, `fetch --unshallow`) + `gh` API + local execution. Starting evidence from the mission brief was treated as hypothesis and re-measured; every divergence is logged in §4.

## 1. Live repository state (measured)

| Item | Measured value | Notes |
|---|---|---|
| `origin/main` HEAD | `05dec617f5c596ef9023cab9c42acc689efec583` ("Merge PR #107") | matches starting evidence |
| Main CI for HEAD | Run `36325395306` — **completed, SUCCESS** | 16/16 jobs green, incl. `continuum-evidence (3.10)` 14:17:52→14:44:32, `(3.11)`, `(3.12)`, trust-mutations, migrate-postgres, release-lineage |
| `continuum-evidence (3.10)` on main | **completed / SUCCESS** | starting evidence said "still running — do not assume green"; it is now terminally green. Main@`05dec617` is GREEN, not merely assumed-green. |
| PR #107 merged head | `1581e13d246811616b96bc3fbad7358d9052217d` (verified via git history) | merge commit `05dec617` reachable from main |
| Open PR count | **29** | PRs: 108 105 104 100 99 97 96 94 93 92 89 88 87 84 83 73 70 69 68 67 66 64 63 60 59 58 57 56 33 |
| Mergeable open PRs | 1 (PR #93, CLEAN); 28 CONFLICTING/DIRTY | before this session's #109 |
| `maintenance` workflow on main | **FAILURE** ×2 most recent runs (`36308867958`, `36230324343`, head `6624a133`) | matches board `next_work` task-164 ("Nightly DB backup has never succeeded") — production backup recovery NOT proven (confirmed live, not just reported) |
| Active board claims (main's `.agents/board.json`) | **3 active**, 42 non-done rows incl. legacy markers | `task-154-pack-coverage-ci` + `task-184-continuum-evidence-hardening` → `arena/01a0e2bb` (claimed 2026-09-27T12:27:18Z, TTL 24h, continuum/ci-quality paths); `task-185-external-intelligence-hardening` → `arena/01a0df05` (claimed 2026-09-26T18:46:40Z, TTL 24h → expires 18:46Z, `knowledge/` + `integrations/` paths) |
| Active `gates_owner` on main board | none | protocol allows exactly one; `task-195-salvage` claimed it for the PR #109 wave |

## 2. PR #108 forensic record (pre-disposition)

| Item | Measured value |
|---|---|
| head | `5a4cb23b26fab15c7624d3e6c9bfc8f8006f8c35` (branch `arena/01a0e2f0`) |
| merge-base with main | `93c7809ad1bc1cd93212578386159c0a26c5e3f2` — **4 ahead / 30 behind** (verified post-`--unshallow`) |
| own commits | `565c429` (board claim), `1564ba4` (board scope), `260dfc6` (feature), `5a4cb23` (docs/board evidence) |
| changed files (5) | `.agents/board.json`, `MASTER_ENGINEERING_TRUTH_REPORT_2026-09-27.md`, `scripts/llm_queue_mutations.py`, `src/nexus_ai_agent/features/request_queue.py`, `tests/unit/test_request_queue.py` |
| intersect with main's delta since merge-base | only `.agents/board.json` (41 files changed on main `93c7809..05dec617`) |
| check runs on head | `python-parity (3.10)` **FAILURE ×2** (check runs `108632462990`, `108632643506`; runs `36323757857`, `36323747749`); all other legs SUCCESS |
| root cause | `request_queue.py:435` calls `asyncio.Task.cancelling()` — Python ≥3.11 only — inside the worker's `except asyncio.CancelledError`; first processed cancellation raises `AttributeError` on 3.10. Exercised by the PR's own cancellation tests. Only the 3.10 leg fails. |
| GH log/artifact access from sandbox | **BLOCKED** (results-receiver/blob hosts unreachable) — runtime repro executed via CI witness + local AST tripwire; documented as limit |

## 3. Other PRs that matter (measured)

| PR | State | Distance/notes |
|---|---|---|
| #105 (shell trust plane, ADR 0011) | CONFLICTING | merge-base `e6b06e0`; **2 ahead / 34 behind**; touches `tools/system_shell.py`, `creative/packs/trust.py`, `.github/workflows/ci.yml` — trust-root-adjacent; Requires owner-level reconcile; deferred |
| #93 (typed provider failure, LAW 10) | **MERGEABLE/CLEAN** | `llm/{errors,fallback_provider,litellm_provider}.py` + test + evidence files; candidate input to W2 — not merged blindly |
| #104 (docs trust) | CONFLICTING | board-only + trust doc |
| #100 (caption lane) | CONFLICTING | 17 files |
| #99 (command truth) | CONFLICTING | 37 files; touches `bot/handlers.py`, `config/settings.py`, `features/conversation_store.py`, `features/ai_memory.py`, `agents/store/agent_manager.py` |
| #96 (Production/DR/restore drill) | CONFLICTING | relevant to PHASE 6 backup/restore evidence — deferred |
| #97 (vision packs) / #94 (state) / #92 (storage) / #89 (command wiring) / #88/#87 (pack-security/studio) / #83/#73/#70/#69/#68/#67/#66/#64 (nagar stack) / #63 (board git truth) / #60 (M0 job queue) / #59 (outbox) / #58 (security boundary) / #57 (claim+lease) / #56 (hygiene) / #33 (v3.13.0 mega-PR) | CONFLICTING | all ≥30+ commits behind; each touches `.agents/board.json` (board churn is the dominant conflict source); none touch `request_queue.py` |
| W1-seam overlap map | — | `bot/app.py` touched by open #33/#60/#63/#66 (all stale, claims expired); `bot/handlers.py` by #33/#89/#99; `features/conversation_store.py` by #99; `storage/db.py` by #33/#92. W1 must claim narrowly with `overlap_ack` |

## 4. Divergences vs starting evidence / prior reports

1. **Main CI is now terminally GREEN** (last-observation said "continuum-evidence (3.10) still running — do not assume"). Measured: SUCCESS. Divergence resolved in main's favour with exact run ID `36325395306`.
2. **"32/32 SUCCESS" for PR #107** — not independently re-verifiable from this sandbox (historical check-run logs unavailable); replaced by the stronger current fact: main@`05dec617` CI 16/16 green. Treated as superseded evidence, not error.
3. **PR #108 "tested before"**: its head is RED on 3.10 parity (both runs) — "carried local-green evidence but remote 3.10 leg fails". Never merge on stale green.
4. **`MODULE_MAP.md` does not exist at repo root**; the owned module map is `docs/architecture/MODULE_MAP.md` (task-131 delivery, touched by open PRs #59/#68/#69/#87/#102/#105/#107).
5. **Version lineage**: `VERSION`=3.13.0 = `pyproject` = newest released CHANGELOG heading (`## [3.13.0] — 2026-09-21`) → lockstep holds. Newest git tag is **v3.5.0**, newest GitHub Release **v3.3.0**, no tag for 3.13.0 → per `scripts/release_lineage.py` contract this is an **OPEN, loudly-reported** item (release not cut), NOT a contradiction; CI `release-lineage` green is consistent with that contract. No silent drift.
6. **Board active claims**: none own `request_queue.py`/`features/` queue files (grep=0) — PR #108's `llm-request-queue-lifecycle` zone existed only inside its own PR (candidate truth); it is now on main's board via the #109 semantic merge.
7. Old audits (e.g. `OWNER_WIDE_FORENSIC_AUDIT_2026-09-26.md`, `LIVE_ENGINEERING_BASELINE_2026-09-26.md`) anchor at pre-`05dec617` SHAs; used as hypotheses only.
8. Sandbox constraints: shallow clone initially (fixed via `--unshallow`); no Python 3.10 interpreter; PyPI reachable; GitHub log/artifact blob hosts unreachable.

## 5. Candidate vs proven (classification)

**PROVEN on main@`05dec617` (CI-witnessed):** continuum evidence gate (3.10/3.11/3.12, exact-SHA snapshot + mutation campaign), pack coverage 95% contract, trust-mutations plane, ed25519 trust root, non-slow suite 2909+2, lint/types/version lockstep, migrate-postgres, release-lineage contract, docs integrity.

**PROVEN-FAILING (measured):** nightly DB backup pipeline (2/2 recent red; board task-164 says 3/3 red historically — consistent), i.e. production recovery is NOT proven.

**CANDIDATE (never promoted without exact-head CI vs current main):** PRs #93, #105, #104, #100, #99, #97, #96, #94, #92, #89, #88, #87, #84, #83, #73, #70, #69, #68, #67, #66, #64, #63, #60, #59, #58, #57, #56, #33; and #108 (salvaged as #109, whose own head must pass exact-head CI before merge).

**DELETED-NOTHING:** no history rewritten anywhere in this session; salvage preserved `565c429..5a4cb23` byte-for-byte.

## 6. Ownership taken this session

| Claim | Branch | Paths | TTL |
|---|---|---|---|
| `task-195-salvage` (continuation of `task-195-llm-queue-lifecycle`, takeover_log recorded) | `arena/01a0e356-nexus-ai-agent` | `src/nexus_ai_agent/features/request_queue.py`, `tests/unit/test_request_queue.py`, `scripts/llm_queue_mutations.py`, `MASTER_ENGINEERING_TRUTH_REPORT_2026-09-27.md`; `gates_owner=true` | 24h from 2026-09-27T15:12:52Z |

Respected active claims: `task-154`/`task-184` (`01a0e2bb`, continuum/ci-quality), `task-185` (`01a0df05`, `knowledge/`+`integrations/`). Next-wave W1 must exclude `knowledge/`, `integrations/`, `.github/` unless those claims expire or release.
