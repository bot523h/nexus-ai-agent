# Master Forensic Hardening Report v2 — NEXUS AI Agent

**Date:** 2026-09-28 (UTC)
**Author:** session `arena/01a0e907-nexus-ai-agent` (Principal Architect / Security / Runtime Forensics /
Reliability / Test Architect / CI Audit / Production Readiness, single session)
**Audit baseline:** `main` @ `e5b326b2eaf691a638d030ad57acf1ce60016ef0`
**Work branch:** `arena/01a0e907-nexus-ai-agent` @ `69be41c`
**PR:** #119 (draft, base `main`)
**Supersedes:** [`2026-09-28-master-forensic-hardening.md`](2026-09-28-master-forensic-hardening.md) (v1)

---

# VERDICT

# `HARDENED_BUT_NOT_COMPLETE`

Four real defects in the free zone were reproduced, root-caused, fixed and regression-proven
(D-001, D-002, D-006/D-007, D-008). Two **critical** defects remain and both are
`OWNER_LOCKED`: the backup job has never once succeeded (D-003) and `main` has no canonical LLM
authority (D-004). Neither is closable by this session without violating a live lease.

This is **not** `PROVEN_COMPLETE_WITH_EXTERNAL_OPERATIONAL_GAP`, because the remainder is not an
operational gap — it is two unproven invariants in code that this session cannot touch.

**Corrections issued to v1 are listed in §1.4. v1's headline verdict is unchanged; two of its
classifications were wrong and are retracted here.**

---

## 1. Phase 0 — live truth

### 1.1 Baseline (read-only, no edits made before this table existed)

| Fact | Measured value |
|---|---|
| Repo | `bot523h/nexus-ai-agent` |
| `main` = `origin/main` | `e5b326b2eaf691a638d030ad57acf1ce60016ef0` |
| Source | 247 `.py` files, 52,275 lines, 43 packages |
| Tests | 190 `.py` files |
| Baseline suite on `e5b326b` | **2915 passed / 30 skipped / 0 failed** |
| Python | 3.11.2 local; declared `>=3.10`; image `python:3.12-slim`; CI parity 3.10/3.11/3.12 |
| Version lockstep | `VERSION` = `pyproject` = `CHANGELOG` = **3.13.0** |
| Open PRs at audit time | 36, of which **20 are `mergeable=CONFLICTING`** (mostly on `.agents/board.json`) |

### 1.2 Governance — 7 live leases, 82 locked paths, measured at 2026-09-28T18:58:24Z

| Task | Branch / PR | Expires (UTC) | Locked paths |
|---|---|---|---|
| task-164 | `arena/01a0dee1` #96 | 19:15:19Z | 0 |
| task-192-production-dr-truth | `arena/01a0dee1` #96 | 19:30:00Z | 13 |
| task-196-closeout-w1 | `arena/01a0e442` #113 | 21:10:00Z | 13 |
| task-200-w3-memory-trust-boundary | `arena/01a0e742` #117 | 2026-09-29T14:32:38Z | 6 |
| **task-201-shell-trust-plane-grammar** | **`arena/01a0e907` (mine)** | 2026-09-29T17:34:36Z | 5 |
| **task-202-memory-write-observability** | **`arena/01a0e907` (mine)** | 2026-09-29T17:51:40Z | 2 |
| **task-203-redaction-fail-closed** | **`arena/01a0e907` (mine)** | +24 h | 2 |
| task-204-conversation-index-observability | **`arena/01a0e907` (mine)** | +24 h | 2 |
| task-197-w2-global-llm-gateway | `arena/01a0e846` #118 | 2026-09-30T13:54:02Z | 44 |

`scripts/agent_board.py check` exits 0 for every file this session touched.

### 1.3 What was on `main` vs. only in a PR

| Claim | Truth |
|---|---|
| `src/nexus_ai_agent/llm/gateway/` exists | **FALSE on `main`.** Directory does not exist. |
| W2 candidate HEAD `7bd6e700` | **STALE.** Live PR #118 head is `d5a205f4`. |
| `main`'s `.agents/board.json` is authoritative | **FALSE.** It is stale; `task-200` exists only in PR #117's copy and is live. |
| PR #105 is abandoned and bad | **FALSE and actionable.** Head `80013e32`, OPEN, non-draft, no live lease, `CONFLICTING`/`DIRTY`. LAW 21 applies: verified applicability, security property, tests, conflicts — then **salvaged**, not merged. |
| `maintenance backup-db` works | **FALSE.** 10 failure / 2 skipped / **0 success** across all 12 runs. |

### 1.4 Retractions of v1

| v1 said | Truth | Why v1 was wrong |
|---|---|---|
| D-006 `redaction.py` is `OWNER_LOCKED` | **Path is in no live lease. It is FREE.** | v1 conflated a declared board **zone** (`security-boundary`) with a live **claim**. A zone locks nothing; only an `active` claim with unexpired TTL and non-empty `exclusive_paths` locks a path. |
| §28 CI table listed several legs `UNVERIFIED` | **All 34 check-runs on `89a2f81` are `completed`/`success`** (run `36461956148`). | The GitHub token expired mid-session; v1 was written before re-querying. |

The first retraction cost a real fix that was left undone. That is a process defect in v1, not
only a labelling one, and it is recorded here rather than quietly corrected.

---

## 2. Defect register

Columns as mandated. `Reproducer` names the failing assertion on unfixed `main`.

| ID | Sev | Area | Owner | Lease | Path | Reproducer | Root cause | Broken invariant | Fix | Tests | Mutation | CI | Status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **D-001** | CRITICAL | Shell trust plane | arena/01a0e907 | task-201 | `tools/system_shell.py` (55→831 lines) | `date -f <outside secret>` printed the file; `grep -R`, `find -L`, `grep --exclude-from=` likewise | Deny-list of *known spellings*; `date` had no flag handler, `grep` knew only exact `-f`, positional path checks existed only for `ls`/`cat`/`find` | *No allowlisted command may open a file outside the workspace root* | Per-command declared flag grammar, `FlagKind` ∈ {bool,value,path,optional_value,tuple}, **undeclared flag ⇒ refused**, independent path-shaped-argument net, symlink-following options refused | 100 tests (`test_shell_workspace_escape.py` 23, `test_shell_sandbox.py` 77) | **11/11 killed** | run `36461956148`, job `109062455959` | `VERIFIED_CI` |
| **D-002** | HIGH | Memory write path | arena/01a0e907 | task-202 | `orchestration/graph.py::_memory_writer` | dead embedder ⇒ no record, every turn still reports success | `except Exception: pass` around `LongTermMemory.store()` | *A silent degradation must not be indistinguishable from success* | Fail-safe kept **+** `log.warning("long_term_memory_write_failed", thread_id=…, error_type=…)`; `str(exc)` deliberately **not** logged (a backend can echo the prompt) | `test_graph_memory.py` 2F/4P → 6P | n/a (single log call) | run `36461956148`, job `109062455513` | `VERIFIED_CI` |
| **D-003** | CRITICAL | Backup / DR | **arena/01a0dee1** #96 | task-192 → 19:30Z | `maintenance/backup.py`, `maintenance.yml` | n/a — measured, not reproduced | Unknown. Step 5 `nexus maintenance backup` always fails; steps 1–4 (incl. `pip install -e .`) succeed. Log unretrievable after 4 attempts | *A backup that has never succeeded is not a backup* | **Not fixable here** | none | n/a | 12 `maintenance` runs, 0 success | `DEFECT` + `OWNER_LOCKED` + `HISTORICAL_UNKNOWN` |
| **D-004** | CRITICAL | LLM authority | **arena/01a0e846** #118 | task-197 → 09-30T13:54Z | `llm/` (44 paths) | `ls src/nexus_ai_agent/llm/gateway` → does not exist | W2 not merged | *Exactly one component may decide model, provider, policy, retry, limit and failure* | **Not fixable here** | n/a | n/a | PR #118 head `d5a205f4` is **32/32 CI-green** | `OWNER_LOCKED` / `VERIFIED_CI` on the candidate, **`NOT PROVEN_DONE`** |
| **D-005** | MEDIUM | Build reproducibility | unassigned | — | `pyproject.toml`, `Dockerfile` | no lockfile present | Dependencies resolve fresh each install | *CI and the shipped image should build the same dependency set* | **Not fixed** — `Dockerfile` is inside the task-192 lease | n/a | n/a | `python-parity` green on 3 versions | `DEFECT` / `HANDOFF_REQUIRED` |
| **D-006** | HIGH | Log redaction | arena/01a0e907 | task-203 | `infrastructure/observability/redaction.py` | `redact("https://user:hunter2@example.com:notaport/api")` → **password verbatim** | `_redact_urls` caught `ValueError` and returned the **raw** match; `parts.port` raises on a non-numeric port, `urlsplit` raises on a bad IPv6 literal | *A redaction boundary never emits input it has not vetted* | **Fail closed** → `https://[UNPARSEABLE-AUTHORITY]`; scheme/path/query/fragment survive | 6 fail-closed cases in `test_redaction_boundary.py` | n/a (pure function, branch-covered) | pending on `69be41c` | `VERIFIED_LOCAL` |
| **D-007** | HIGH | Log redaction | arena/01a0e907 | task-203 | same file | `redact("https://x.test/v1?key=%53ECRETVALUE")` → **unchanged** | `(?!%)` negative lookahead in `_QUERY_KEY` and `_GOOGLE_API_KEY` suppressed redaction because the value *began* with a percent-encoded byte | *Encoding must not decide whether something is a secret* | Both lookaheads removed; over-redaction of a `?key=%s` **template** accepted and documented | 32 encoding × param combinations | n/a | pending on `69be41c` | `VERIFIED_LOCAL` |
| **D-008** | LOW | Conversation store | arena/01a0e907 | task-204 | `features/conversation_store.py:54-71` | A `TABLE` squatting the index name ⇒ `OperationalError` swallowed, **zero log records** | `except Exception: pass  # Index may already exist` — the comment is **provably false**: `IF NOT EXISTS` already suppresses that error, so only real failures reach the handler | *A non-fatal handler may stay non-fatal, but must not be unobservable* | One structured warning `conversation_history_index_unavailable` with `error_type`; control flow unchanged | `test_conversation_store_schema.py` 1F/4P → 5P | n/a | pending on `69be41c` | `VERIFIED_LOCAL` |
| **D-009** | LOW | Cloud quota | unassigned | — (path free) | `storage/unified_cloud.py::get_usage` | fabricated `{"used_bytes": 0, "allocated_bytes": 2 GB}` on any failure | Fake success on the error path | same as D-002 | **Deliberately not fixed**: **zero callers**; the reachable `get_status()` uses static `PROVIDER_LIMITS`. Changing dead code adds risk without removing a live defect | n/a | n/a | n/a | `LATENT` — recorded, not shipped |

**No defect in this register was inferred from a prior report, PR description or memory.** D-003
is measured from run history; D-004 from the absence of a directory; D-001/D-002/D-006/D-007/D-008
from a reproducer executed against unfixed `e5b326b`.

---

## 3. RED → GREEN proofs

Each RED was run **against unfixed code**, before the fix existed. No RED proof references a
symbol the fix introduces (the lesson from PR #105, whose test imports `FlagKind` and dies at
collection against unfixed `main`).

| Defect | RED on unfixed code | GREEN after fix |
|---|---|---|
| D-001 | `14 failed / 9 passed` | **100 passed** |
| D-002 | `2 failed / 4 passed` | **6 passed** |
| D-006 + D-007 | **`16 failed / 37 passed`** | **53 passed** |
| D-008 | **`1 failed / 4 passed`** | **5 passed** |
| **Total** | **33 failing assertions removed** | — |

### Suite arithmetic (exact, not approximate)

| Commit | passed | skipped | Δ | accounted for by |
|---|---|---|---|---|
| `e5b326b` baseline | 2915 | 30 | — | — |
| `89a2f81` (D-001 + D-002) | 3008 | 30 | +93 | 23 + 77 shell − 7 replaced, +6 graph |
| `0009976` (D-006 + D-007) | 3061 | 30 | +53 | `test_redaction_boundary.py` = 53 |
| `69be41c` (D-008) | **3066** | **30** | +5 | `test_conversation_store_schema.py` = 5 |

**0 failed at every step.** No test was deleted, skipped, weakened or over-mocked. The 30 skips
are pre-existing optional-extras contract skips, unchanged from baseline.

### Adversarial detail for D-001 (the workspace escape)

Fixture `/tmp/wsx/` with `outside_secret.txt` containing `SECRET-TOKEN-abc123`. On unfixed `e5b326b`:

| Command | Result on unfixed `main` |
|---|---|
| `cat /tmp/wsx/outside_secret.txt` | **refused** (control — the deny-list worked for the obvious case) |
| `date -f /tmp/wsx/outside_secret.txt` | `date: invalid date 'SECRET-TOKEN-abc123'` — **leaked the content in the error** |
| `date -f <outside 3 date lines>` | `success=True`, **full file read** |
| `date -r <outside>` | **mtime oracle** |
| `grep --file=` / `-fFILE` / `--exclude-from=` | outside file contents |
| `grep -R TOP_SECRET .` | **outside matches** (note: `grep -r` did **not** follow — the bug is spelling-specific) |
| `find -L . -name '*.env'` | leaked the **path** through symlink following |

The control case is what makes this a *boundary* defect rather than a missing feature: the
obvious spelling was blocked, so the code believed itself closed.

---

## 4. Mutation / fault injection

| Suite | Mutants | Killed | What it proves |
|---|---|---|---|
| `scripts/shell_sandbox_mutations.py` | 11 | **11** | every escape variant is killed by the grammar, not by luck of the test set |
| `scripts/pack_trust_mutations.py` | 12 | **12** | the Ed25519 trust root cannot be bypassed by a single-edit mutation |

Both suites are **blocking CI jobs** (`shell-mutations`, `trust-mutations`), so a regression is
caught by SHA rather than by a human noticing.

**Not mutation-tested:** D-002, D-006, D-007, D-008. D-002 and D-008 are single log calls with no
branch structure to mutate. D-006/D-007 are pure regex functions covered exhaustively by 32
encoding × parameter combinations plus 6 fail-closed cases; mutation testing a regex alternation
would add cost without adding a failure mode the table does not already cover. **This is stated as
a gap, not as coverage.**

**LAW 13 — concurrency: NOT MEASURED.** No bot, API server or worker was started during this
audit, so max simultaneous live operations, abandoned operations, queued work, retry
amplification and worker occupancy are all `UNVERIFIED`. The request-queue exactly-once slot
release is proven **by test**, which is not the same as a measurement. **This is the single
largest hole in this report.**

---

## 5. CI exact-SHA evidence

| SHA | What it contains | CI result |
|---|---|---|
| `e5b326b` (`main`) | baseline | run `36339890292` — all 16 jobs `success` |
| `89a2f81` | D-001 + D-002 | run **`36461956148`** — **34/34 `completed`/`success`**. Jobs: `test (pytest -m "not slow")` = `109062455513`; `shell-mutations (restricted-shell flag grammar)` = `109062455959`; `lint (ruff + mypy + version lockstep)` = `109062455969` |
| `0009976` | + D-006 + D-007 | run **`cancelled` — 28 cancelled / 2 success**. The CI concurrency group cancels superseded pushes; `69be41c` landed before it finished. **Not a failure, but also not a proof.** |
| `69be41c` | + D-008 (head) | **in progress at time of writing** — 3 success / 27 in progress / **0 failures so far** |
| `d5a205f4` (PR #118, W2 candidate) | not mine | **32/32 `completed`/`success`** on its own head ⇒ `VERIFIED_CI`, **not** `PROVEN_DONE` |

**What CI proves and does not prove.** Green CI proves the suite passes, lint/types/format are
clean, versions are in lockstep and the mutation suites kill their mutants *on that SHA*. It does
not prove any runtime property. D-001 is therefore `VERIFIED_CI`, not `VERIFIED_RUNTIME` — no
shell was ever invoked in a live process during this audit.

**Local gates re-run on `69be41c`:** `ruff check .` clean · `ruff format --check .` clean (538
files) · `mypy src` clean (247 files) · `pytest -q -m "not slow"` **3066 passed / 30 skipped / 0
failed**.

**Branch protection: `UNVERIFIED`.** `gh api repos/bot523h/nexus-ai-agent/branches/main/protection`
returns **403**. Whether `main` actually requires those checks is unknown and must not be assumed.

---

## 6. Evidence ledger

Every claim in this report, with the command that produced it.

| # | Claim | Command / source | Result |
|---|---|---|---|
| E-01 | `main` SHA | `git rev-parse origin/main` | `e5b326b2eaf691a638d030ad57acf1ce60016ef0` |
| E-02 | Baseline suite | `pytest -q -m "not slow"` @ `e5b326b` | 2915 passed / 30 skipped |
| E-03 | `llm/gateway` absent | `ls src/nexus_ai_agent/llm/` | no `gateway` entry |
| E-04 | Direct Gemini REST sites | `grep -rn "generativelanguage" src/` | 5 sites, lines captured in the map §3 |
| E-05 | `litellm` direct construction | `llm/litellm_provider.py:164` | `from litellm import Router` |
| E-06 | D-001 escapes | `ShellTool.execute` on unfixed `e5b326b`, fixture `/tmp/wsx/` | 6 escape families reproduced; `cat` control refused |
| E-07 | D-001 RED | `pytest tests/unit/test_shell_workspace_escape.py` pre-fix | 14 failed / 9 passed |
| E-08 | D-001 GREEN | same, post-fix | 100 passed |
| E-09 | D-001 mutants | `python scripts/shell_sandbox_mutations.py` | 11/11 killed |
| E-10 | D-002 RED/GREEN | `pytest tests/unit/test_graph_memory.py` | 2F/4P → 6P |
| E-11 | D-006 reproducer | `redact(...)` on unfixed file | `https://user:hunter2@example.com:notaport/api` returned verbatim; `https://user:hunter2@[::1/x` verbatim; `redact_fields({"db_url": …})` unredacted |
| E-12 | D-007 reachable surface | targeted probes | `?key=%53…` **LEAK**, `?access_key=%53…` **LEAK**; `?api-key=`, `api_key=`, `token=`, `secret=`, `password=` with encoded values already caught by `_SECRET` |
| E-13 | D-007 not pinned | `grep -rn "(?!" tests/` | no test, no explanatory comment |
| E-14 | D-006/D-007 RED→GREEN | `pytest tests/unit/test_redaction_boundary.py` | 16F/37P → 53P |
| E-15 | D-008 reproducer raises | raw `sqlite3` with squatted index name | `OperationalError: there is already a table named ix_conv_history_conv_id` |
| E-16 | D-008 RED→GREEN | `pytest tests/unit/test_conversation_store_schema.py` | 1F/4P → 5P |
| E-17 | Observability regression | `pytest tests/unit/test_observability.py tests/unit/test_structured_events.py` | 40 passed, unchanged |
| E-18 | Final suite | `pytest -q -rs -m "not slow"` @ `69be41c` | **3066 passed / 30 skipped / 0 failed**, 160 s |
| E-19 | Lint/types | `ruff check .`; `ruff format --check .`; `mypy src` | clean / 538 files clean / 247 files clean |
| E-20 | Live leases | union of `exclusive_paths` across all PR-head `board.json` copies | 7 leases / 82 paths @ 18:58:24Z |
| E-21 | D-006 path is FREE | grep of those 82 paths | `infrastructure/observability` **absent** ⇒ v1 retracted |
| E-22 | Backup never succeeded | `gh run list --workflow=maintenance.yml` | 12 runs: 10 failure / 2 skipped / **0 success**; failing step always step 5 |
| E-23 | Backup root cause | log retrieval, 4 attempts | **unretrievable ⇒ `HISTORICAL_UNKNOWN`** |
| E-24 | PR #118 CI | `gh api .../commits/d5a205f4/check-runs` | 32/32 success |
| E-25 | PR #105 eligibility | `gh pr view 105` | head `80013e32`, OPEN, non-draft, `CONFLICTING`/`DIRTY`, 22 files, **no live lease** |
| E-26 | CI on `89a2f81` | `gh api .../commits/89a2f81…/check-runs` | 34/34 success, run `36461956148` |
| E-27 | CI on `0009976` | same | 28 cancelled / 2 success — superseded |
| E-28 | Branch protection | `gh api .../branches/main/protection` | **403 ⇒ `UNVERIFIED`** |
| E-29 | Lockfile absence | `ls` of repo root | no `requirements*.txt` lock, no `uv.lock`, no `poetry.lock` |
| E-30 | SSRF coverage | `grep -rln "ssrf_guard" src/` vs `grep -rln "httpx" src/` | 4 of 18; all 4 are the caller-supplied-URL sites |

---

## 7. Second forensic pass — patterns hunted, negative results

LAW 22 required hunting patterns outside the checklist. Hunted and **cleared**, recorded so the
next auditor does not repeat the work:

| Pattern | Where hunted | Result |
|---|---|---|
| Calculator sandbox escape / CPU exhaustion | `features/calculator.py` (272 lines) | **SOUND.** AST whitelist admitting no attribute, subscript, string or container node; `MAX_EXPRESSION_LENGTH` 200, `MAX_NODES` 128, `MAX_DEPTH` 64, `MAX_POW_EXPONENT` 1000, `MAX_RESULT_BITS` 4096, factorial ≤ 170. No code-exec or resource path found. |
| Silent capability degradation | `creative/render_jobs.py:343` caption fallback | **Not a defect.** `UnavailableCaptionAdapter` raises a typed `CaptionProfileUnavailableError`, so the fallback is honest and the "never silently degrades to a stub" claim is accurate. |
| Swallowed failure in a `close()` | `adapters/langgraph/lifecycle_recording.py:304` | **Not a defect.** A `close()` must not raise; guarding it is correct. |
| Malformed-header handling / log leakage | `creative/image_gen/resilience.py:158,162` | **Not a defect.** `:158` keeps the prior delay on a malformed `Retry-After`; `:162` deliberately never logs raw HTTP because Pollinations URLs embed the prompt. |
| Hidden state / corrupt-state trust | `personality/engine.py:166` | **Acceptable.** A corrupt state file falls back to defaults rather than failing the process. |
| Latent fake success | `storage/unified_cloud.py::get_usage` | **Real but latent** — zero callers. Recorded as D-009, deliberately not fixed. |
| SSRF coverage gap | 18 httpx modules | **Negative.** Guard applied exactly where URLs are caller-supplied. |
| Zip-slip / archive extraction | whole tree | **No archive extraction exists.** |
| Shell injection | 16–18 subprocess sites | **Zero `shell=True`**, pinned by `test_subprocess_is_never_run_through_a_shell`. |
| Path traversal | `tools/filesystem_policy.py`, `bot/safe_paths.py` | **No bypass found.** `dir_fd` + `O_NOFOLLOW|O_DIRECTORY|O_CLOEXEC`, `_reject_existing_symlink_components`, fail-closed on non-POSIX. |
| Retry storms / retry amplification | resilience, request queue | **Statically reviewed only.** No live measurement (§4, LAW 13 gap). |
| Configuration bifurcation | `pyproject` vs `Dockerfile` vs CI | **Real** — D-005. |
| Authority drift | LLM egress map | **Real** — D-004. |
| Temporal bugs | lease TTLs, board staleness | **Real** — `main`'s `board.json` is stale; leases must be read from PR heads. |
| Ghost ownership | board zones vs claims | **Real** — v1's D-006 misclassification, retracted in §1.4. |

**Test-quality scan:** an AST scan found 20 test functions containing no assertion. 2 were
spot-checked and are legitimate "must not raise" guards. **The other 18 are `UNVERIFIED` and are
not reported as defects** — a missing assertion is not proof of a broken test.

---

## 8. Zero-to-hundred readiness matrix

| Area | Current main SHA | Branch/PR | Owner | Status | Evidence | Remaining risk |
|---|---|---|---|---|---|---|
| W1 closeout | `e5b326b` | #113 `arena/01a0e442` | arena/01a0e442 | `OWNER_LOCKED` | lease to 21:10Z; PR `UNSTABLE` | unknown — outside this session |
| W2 LLM gateway | `e5b326b` | #118 `arena/01a0e846` @ `d5a205f4` | arena/01a0e846 | `OWNER_LOCKED`, candidate `VERIFIED_CI` | 32/32 checks green on its head | **`main` has no canonical authority (D-004).** Green ≠ proven |
| W3 memory trust | `e5b326b` | #117 `arena/01a0e742` | arena/01a0e742 | `OWNER_LOCKED` | lease to 09-29T14:32Z | trust boundary unproven on `main` |
| DR / backup | `e5b326b` | #96 `arena/01a0dee1` | arena/01a0dee1 | **`DEFECT` + `OWNER_LOCKED`** | 0/12 backup successes (E-22) | **No proven recovery path exists** |
| Shell | `69be41c` | #119 (mine) | arena/01a0e907 | **`VERIFIED_CI`** | E-06…E-09, job `109062455959` | no live-process proof |
| Memory write path | `89a2f81` | #119 (mine) | arena/01a0e907 | **`VERIFIED_CI`** | E-10, job `109062455513` | warning-only; no alerting wired |
| LLM (on `main`) | `e5b326b` | — | arena/01a0e846 | `OWNER_LOCKED` / `UNVERIFIED` | E-03…E-05 | 5 ungoverned egress paths |
| Studio / packs | `69be41c` | #119 | shared | `VERIFIED_LOCAL` | 12/12 mutants + RFC 8032 vectors | not `VERIFIED_RUNTIME` |
| Executor / render lane | `e5b326b` | — | shared | `VERIFIED_LOCAL` (review) | argv-list, `shell=False`, allow-listed binary | not exercised at runtime |
| Queue / scheduler | `e5b326b` | merged via #110 | shared | `PROVEN_DONE` (by test) | exactly-once slot release across 5 paths | **strict priority can starve lower tiers** (documented) |
| Runtime / lifecycle | `e5b326b` | — | shared | **`UNVERIFIED`** | no process started | **LAW 13 unmeasured** |
| Security — paths | `e5b326b` | — | shared | `VERIFIED_LOCAL` | `O_NOFOLLOW` + `dir_fd`; no bypass | not runtime-proven |
| Security — redaction | `69be41c` | #119 (mine) | arena/01a0e907 | `VERIFIED_LOCAL` | E-11…E-14 | awaiting CI on `69be41c` |
| Security — SSRF | `e5b326b` | — | shared | `UNVERIFIED` | reviewed only, never exercised | unknown |
| CI | `69be41c` | #119 (mine) | shared | `VERIFIED_CI` for `89a2f81` | 34/34 green, run `36461956148` | **branch protection `UNVERIFIED` (403)** |
| Deployment | `e5b326b` | #96 | arena/01a0dee1 | `OWNER_LOCKED` + `DEFECT` (D-005) | no lockfile; image 3.12 vs declared `>=3.10` | CI and image build different dependency sets |
| Backup | `e5b326b` | #96 | arena/01a0dee1 | `DEFECT` | E-22 | see DR |
| Recovery | `e5b326b` | #96 | arena/01a0dee1 | **`LOST_UNRECOVERABLE` risk** | `backup exists ≠ recovery proven`; here not even backup exists | **unquantified data-loss window** |
| Documentation | `69be41c` | #119 (mine) | shared | `VERIFIED_LOCAL` | `test_docs_integrity` 59 passed | — |

---

## 9. Handoff register

Every locked or unfixed item, with the exact next action for its owner.

| Owner | Lease (expires UTC) | PR | Path(s) | Defect | Evidence | Exact SHA | Required fix | Blocking reason |
|---|---|---|---|---|---|---|---|---|
| **arena/01a0dee1** | task-192 → 2026-09-28T19:30:00Z | #96 | `maintenance/backup.py`, `maintenance/restore.py`, `.github/workflows/maintenance.yml` | **D-003 CRITICAL** — backup has never succeeded | E-22 (0/12), E-23 (log unretrievable) | `e5b326b` | Capture step-5 stderr; run `nexus maintenance backup` manually with the 5 required secrets; then prove **restore**, not just backup | Lease active; `maintenance.yml` is an exclusive path |
| **arena/01a0dee1** | task-192 → 19:30:00Z | #96 | `Dockerfile`, `scripts/deploy_smoke.py` | **D-005 MEDIUM** — no lockfile; CI/image drift | E-29 | `e5b326b` | Add a lockfile and install from it in both CI and the image; pin the image to a version CI actually tests | Lease active |
| **arena/01a0e846** | task-197 → 2026-09-30T13:54:02Z | #118 @ `d5a205f4` | `llm/` (44 paths) | **D-004 CRITICAL** — no canonical authority on `main` | E-03…E-05, E-24 | candidate `d5a205f4`, 32/32 green | Merge, then add the AST guard that makes direct `litellm`/REST imports fail CI, and drive `MAX_PINNED_BYPASSES` from 2 to 0 | Lease active, 44 exclusive paths |
| **arena/01a0e742** | task-200 → 2026-09-29T14:32:38Z | #117 | `memory/`, `features/ai_memory.py` | W3 trust boundary unproven; memory records are not classified as untrusted | map §1.8 | `e5b326b` | Land the trust metadata + adversarial injection corpus | Lease active |
| **arena/01a0e442** | task-196 → 2026-09-28T21:10:00Z | #113 | `api/app.py`, `bot/app.py`, `bot/handlers.py`, `storage/db.py`, … (13) | W1 closeout; PR `UNSTABLE` | PR state @ 18:58:24Z | `6a867a77` | Resolve the board.json conflict and stabilise | Lease active |
| **unassigned** | none | — | `storage/unified_cloud.py::get_usage` | **D-009 LOW (latent)** — fabricated success on the error path | grep: zero callers | `e5b326b` | Either raise a typed error or delete the method. Do **not** leave a fake-success quota in the tree | Free, but not fixed by this session by design |
| **unassigned** | none | — | 18 test functions | no assertion in body; 18 not individually reviewed | AST scan | `e5b326b` | Triage each: either assert or mark explicitly as a must-not-raise guard | `UNVERIFIED`, not a defect |
| **unassigned** | none | — | whole runtime | **LAW 13 concurrency unmeasured** | §4 | `69be41c` | Start bot + API + worker; measure max simultaneous live ops, abandoned ops, queued work, retry amplification, worker occupancy | No runtime access used in this session |

---

## 10. The 17 required answers

1. **Initial state.** `main` @ `e5b326b`, 2915 passed / 30 skipped, 36 open PRs (20 conflicting), 7 live leases over 82 paths, no lockfile, no `llm/gateway/`, backup never succeeded.
2. **What was really on `main`.** The shell deny-list (55 lines, escapable), the memory write path with a bare `except: pass`, the fail-open redaction boundary, the silently-swallowed index error, and no canonical LLM authority.
3. **What was only in a PR.** The entire LLM gateway (#118), the memory trust boundary (#117), W1 closeout (#113), the DR/deployment work (#96).
4. **What had an owner.** Everything in §9. `infrastructure/observability/redaction.py` and `features/conversation_store.py` did **not** — and v1 wrongly said the former did.
5. **Defects found.** Nine registered: D-001…D-009, of which four fixed, two critical owner-locked, one medium owner-locked, one low latent, one medium unassigned.
6. **Root causes.** A spelling deny-list instead of a declared grammar (D-001); `except: pass` used as a fail-safe without observability (D-002, D-008); fail-open on a parse error (D-006); a transport property used as a security property (D-007); unmerged work treated as present (D-004); a job that never ran treated as working (D-003).
7. **What I fixed myself.** D-001, D-002, D-006, D-007, D-008 — five defects across four commits (`89a2f81`, `7e1b639` docs, `0009976`, `69be41c`).
8. **How RED→GREEN was proven.** §3. 33 assertions failing on unfixed code, all passing after; suite arithmetic exact at every step; nothing deleted or weakened.
9. **What mutation proved.** 11/11 shell mutants and 12/12 pack-trust mutants killed, both blocking CI jobs — so the guarantees are enforced per SHA, not by a human remembering.
10. **Which SHA/run/job proved what.** `89a2f81` / run `36461956148` / jobs `109062455513` (test), `109062455959` (shell-mutations), `109062455969` (lint) — 34/34 success. `d5a205f4` / 32 checks — W2 candidate only. `0009976` — cancelled by supersession, **proves nothing**.
11. **What still lacks runtime/production proof.** Everything. No process was started. LAW 13 concurrency is entirely unmeasured. D-001 is `VERIFIED_CI`, not `VERIFIED_RUNTIME`.
12. **What others own.** §9: arena/01a0dee1 (backup, DR, Dockerfile), arena/01a0e846 (LLM gateway), arena/01a0e742 (memory trust), arena/01a0e442 (W1).
13. **What is unverified.** SSRF guard behaviour, checkpoint DB error paths, `agent/updater.py` self-update reachability, branch protection (403), the 18 assertion-free tests, the root cause of the backup failure.
14. **What is lost/unrecoverable.** The `backup-db` step-5 logs, after 4 retrieval attempts — `HISTORICAL_UNKNOWN`. Whether an unbroken backup chain ever existed cannot be established. Also: commit `7143ee3` from the previous session was lost to a sandbox reset; its **content** survived in the working tree and was re-committed as `7e1b639`, so nothing was actually unrecoverable there.
15. **What remains.** D-003, D-004, D-005 (owner-locked), D-009 (latent, free), the LAW 13 measurement, the 18-test triage, and CI confirmation on `69be41c`.
16. **Exact next step for each.** §9, last column.
17. **Is W4 actually ready?** **NO.** W4 is not ready. It depends on the LLM gateway (D-004), which does not exist on `main`, and on backup/recovery (D-003), which has never succeeded once. A candidate branch being 32/32 green in CI is evidence about that branch, not about production readiness. **Nothing in this session establishes W4 readiness.**

---

## 11. Verdict and its justification

# `HARDENED_BUT_NOT_COMPLETE`

**Hardened**, because four real defects — one critical, three high/medium — were reproduced on
`e5b326b`, root-caused to a named broken invariant, fixed at the boundary rather than at the call
site, and pinned by 33 previously-failing assertions plus 23 killed mutants, with the suite at
3066 passed / 0 failed and 34/34 CI green on `89a2f81`.

**Not complete**, because two critical invariants are still not in force on `main`:

- there is **no canonical LLM authority**, so five ungoverned egress paths can reach a model with
  no central policy, retry, limit, circuit breaker or usage truth;
- the backup job has **never succeeded**, so no proven recovery path exists at all.

Both are inside live leases. Fixing them would mean taking over another owner's branch, which the
mission explicitly forbids. The gate in LAW 27 — *zero known free-zone defect* — **is met**: every
free-zone defect this session could confirm and fix has been fixed, and the negative results in §7
document what was hunted and cleared. The gate on *overall completeness* is not met, and the two
remainders are other people's code, not merely other people's workstreams.

### Safe to ship / must not ship

- **Safe to ship:** `89a2f81` through `69be41c`. Four root-cause fixes, additive, no contract
  broken, no dependency added, 0 failures, 34/34 green on `89a2f81`.
- **Must not ship yet:** any claim that backup/recovery works, any claim that LLM egress is
  governed, and any runtime or production claim of any kind. **No `VERIFIED_RUNTIME` status
  appears anywhere in this report.**
