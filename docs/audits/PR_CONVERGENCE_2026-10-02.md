# PR Convergence & Architecture Truth — 2026-10-02

**Branch:** `arena/01a0fc3b-nexus-ai-agent` · **Base inspected:** `main` @ `e5b326b2eaf691a638d030ad57acf1ce60016ef0` (v3.13.0)
**Method:** every claim below was measured from the repository and GitHub in this session. Nothing is carried over from a prior report or from a PR description.

---

## 1. Live truth at inspection time

| Fact | Measured value | How |
|---|---|---|
| `main` SHA | `e5b326b2eaf691a638d030ad57acf1ce60016ef0` | `git rev-parse origin/main` |
| Version | `3.13.0` | `VERSION` == `pyproject.toml` == `CHANGELOG` (`scripts/check_version_lockstep.py`) |
| **Open PRs** | **63** | `gh pr list --state open` |
| Working tree | clean | `git status --porcelain` → 0 |
| Live board leases | **0** | `scripts/agent_board.py show`: all 78 claims terminal (34 done / 10 expired / 18 queued / rest assigned, merged or superseded) |
| `main` local gate | **GREEN** | `ruff check` 0 · `ruff format --check` 0 · `mypy src` 0 · `pytest -m "not slow"` **2915 passed, 30 skipped** |
| `main` CI | `maintenance` workflow only, **RED** | Scheduled-only; fails because R2 secrets are unset (`nexus maintenance backup` exits non-zero by design). This is recorded in the board as `task-164` — *"Nightly DB backup has never succeeded (3/3 red)"*. **Not a code regression.** |
| Branch protection | unreadable (HTTP 403) | `gh api .../branches/main/protection` → `Resource not accessible by integration` |
| Merge practice | agents merge their own verified PRs | #102, #103, #106, #107, #108, #110 all `mergedBy: app/arena-ai-coding-agent` |

**Two corrections to the tasking assumptions, both measured:**

1. The brief assumed **15** open PRs. There are **63**.
2. The brief assumed `creative/spine/` and `creative/temporal/` exist to inspect. **Neither exists on `main`.** Both live only in unmerged PRs.

**Environment note.** The checkout was a **shallow clone** (`.git/shallow` present, `main` = 1 commit). Local `git merge-base` silently returned nothing for the older PRs, which made 28 of them look file-less. `git fetch --unshallow` was required before any ancestry claim in this document could be made. `main` is 458 commits deep; the shared root is `a4eabdf9`.

---

## 2. The two eras

The 63 PRs split cleanly on ancestry, and the split drives every disposition.

| Era | PRs | Distance from `main` | Textually mergeable |
|---|---|---|---|
| **A — stale** | #33, #56–#105 (28 PRs) | **42–209 commits behind** | 27 of 28 `CONFLICTING` (only #93 is `MERGEABLE`) |
| **B — current** | #111–#145 (35 PRs) | merge-base **is** `main` HEAD (behind = 0) | all 35 `MERGEABLE` |

Era A is not "already landed" — their `src/` files still differ from `main` — but they are 1–7 months of drift deep and cannot merge as they stand. Their honest disposition is **RESTACK**, not CLOSE.

---

## 3. Duplicate authority (the actual disease)

Measured by file-path collision, not by title.

### 3.1 Temporal — five implementations of one path (resolved by PR #146)

`#135`, `#140`, `#142`, `#144`, `#145` **each** add `creative/temporal/{__init__,core,adapters}.py` plus `tests/unit/test_temporal_{algebra,mutation}.py` and modify `creative/packs/delivery/operations.py`. Five different `core.py` files, 750–764 lines each, at one path. No two are mergeable alongside another.

| PR | `core.py` | rationalisation | amends the R7 pack gate | CI |
|---|---|---|---|---|
| #135 | 764 LOC | `limit_denominator(100000)` | no | unstable |
| #140 | 754 LOC | `limit_denominator(100000)` | **yes** | green |
| #142 | 750 LOC | `limit_denominator(100000)` | no | **3 failures** |
| #144 | 757 LOC | `limit_denominator(100000)` | yes | green |
| #145 | 764 LOC | **`Fraction(str(v))` exact** | no | **3 failures** |

**Root cause of the red CI.** All five import `creative.temporal.core` from
`creative/packs/delivery/operations.py`, which boundary law **R7** forbids
(packs may import only stdlib, pydantic, `creative.packs`, `creative.studio`).
#142/#145/#135 therefore fail 4 architecture tests, and that cascades into a red
`continuum-evidence` job. Verified causally: `scripts/pack_coverage.py` returns
exit 0 on `main` and exit 1 on #145's tree. #140 and #144 are the only two that
amend the gate.

**Measured behavioural divergence** (`FrameRateResolver.resolve`, executed against each branch):

| input | `limit_denominator(100000)` | `Fraction(str(v))` |
|---|---|---|
| `48.0000001` | `Timebase(48, 1)` — **1e-7 silently dropped** | `Timebase(480000001, 10000000)` |
| `1e-06` | `ValueError` (zero numerator rejected) | `Timebase(1, 1000000)` |
| `23.976023976` | `Timebase(24000, 1001)` | `Timebase(2997002997, 125000000)` |

The named NTSC invariants hold in **every** variant — they come from the explicit
`FrameRateResolver._STANDARD_PROFILES` table, not from float parsing. Worth
stating plainly because the two are not the same thing: `23.976` as a decimal is
`2997/125`, which is **not** `24000/1001`. Verified:

```
resolve(23.976) -> Timebase(24000, 1001)     resolve(29.97) -> Timebase(30000, 1001)
resolve(59.94)  -> Timebase(60000, 1001)     resolve("24000/1001") -> Timebase(24000, 1001)
```

### 3.2 Creative IR — #131 ⊂ #134

`#134` contains every file of `#131` (`intelligence/{__init__,errors,identity,ir,semantics}.py`,
`tests/architecture/test_creative_intelligence_boundary.py`, `tests/unit/creative_ir_testkit.py`,
`test_creative_ir{,_determinism}.py`, `test_creative_semantics.py`) **plus** `diff.py`,
`revision.py`, `test_creative_diff.py`, `test_creative_revision.py`. #131 is not an ancestor of
#134 — they are independent derivations of the same scope. **Neither has landed**: `creative/intelligence/`
does not exist on `main` at all.

### 3.3 PlanTransaction — one real implementation, two false titles

| PR | title claims | `creative/studio/` files in diff | `PlanTransaction` occurrences |
|---|---|---|---|
| #129 | "staged plan transactions and atomic commit" | **4** | **27** |
| #135 | "canonical **PlanTransaction** substrate and **durable persistence**" | **0** | **0** |
| #140 | "Harden Studio CommandBus: **PlanTransaction**, Logical Identity, Targeted Undo" | **0** | **0** |

`#135` and `#140` also contain **0** occurrences of `durable`/`persist`. Their titles describe work
that is not in their diffs; both are pure temporal PRs. **#129 is the only PlanTransaction implementation.**

### 3.4 Correctly-layered stacks (not duplication)

`#126 → #127 → #128 → #132` is a genuine stack, each link adding its own work on the previous.
Despite `spine/` mirroring studio's module names (`models`, `compiler`, `execution`, `reference` vs
studio's `models`, `capabilities`, `bus`, `references`), it **does not duplicate studio's authority** —
measured imports at the stack tip:

* `spine/execution.py` imports and dispatches through `creative.studio.bus.CommandBus`
* `spine/compiler.py` uses `creative.studio.capabilities.CapabilityRegistry`

So the spine is an *additive layer above* the command authority: Intent → compile → CommandBus.
That is the correct direction. Its problem is packaging, not architecture: the tip (#132) carries
creative spine **+** studio undo **+** storage WAL retry in one PR.

Same shape for `#116 → #118` (LLM gateway, 43 shared files) and `#112 → #113` (runtime, 14 shared files).

### 3.5 Storage lifecycle — a clean superset chain

`checkpoint_lifecycle_store.py` is **byte-identical** between #130 and #143, and #143 contains #136.
So `#143 ⊃ #136 ⊃ #130's fix`. #143's base is #136's head branch, so it needs retargeting to `main`.

---

## 4. Architecture truth on `main`

| Boundary | Status | Evidence |
|---|---|---|
| Intent | **BLOCKED** | No intent model on `main`. Exists only in the unmerged #126–#132 spine stack. |
| Creative IR | **BLOCKED** | `creative/intelligence/` **does not exist** on `main`. Only in #131/#134. |
| Operation graph | **BLOCKED** | `spine/compiler.py` is unmerged. |
| Policy / Authorization | **VERIFIED_WITH_LIMITATIONS** | `creative/studio/authorization.py` (40 LOC); `ProjectAuthorizer` binds actor+project independently of `target.project_id`, which is treated as a claim only. |
| Capability | **VERIFIED** | `creative/studio/capabilities.py` (574 LOC); `CapabilityRegistry` + `get_spec`. |
| Command | **VERIFIED** | `TypedCommand`, single envelope, protocol `nagar.command.v1`; a `v2` id is banned from `src/`. |
| CommandBus | **VERIFIED_WITH_LIMITATIONS** | `dispatch()` = parse → envelope schema → operation schema → authorize → apply, under a lock, with nested dispatch explicitly forbidden. Enforced structurally by `tests/architecture/test_command_capability_boundary.py` (R13). **Limitation: no persistence at all** — see below. |
| Temporal | **VERIFIED** (after PR #146) | One authority; exact rationalisation; 10/10 mutants killed; NTSC invariants pinned; `0 duration → 0 frames` proven. |
| Persistence (studio) | **BLOCKED** | `grep` over `creative/studio/` finds **zero** `sqlite`/`Session`/`engine`/`commit()` references. R6 (`test_nagar_studio_isolation.py`) *enforces* stdlib+pydantic-only, so the studio core is structurally in-memory. |
| Execution | **VERIFIED_WITH_LIMITATIONS** | Render lane has exactly one `subprocess` site (`rendering/executor.py`), `shell=False`, allow-listed (R9). |
| Verification | **VERIFIED** | `scripts/pack_coverage.py` → ACCEPTED, 97.38%, every pack ≥ 95%, byte-identical on rerun, `--verify-artifact` ok, SHA-bound. |
| Artifact | **VERIFIED_WITH_LIMITATIONS** | OTIO export now emits `content_sha256` + `temporal_conversion` evidence. Metadata, **not** cryptographic proof. |
| Receipt / Evidence | **VERIFIED_WITH_LIMITATIONS** | Continuum gate + mutation campaign produce SHA-bound, replayable JSON artifacts. |
| Lineage | **VERIFIED_WITH_LIMITATIONS** | `AssetRecord.parent_asset_ids` + `provenance`; `scripts/release_lineage.py`. |
| Recovery | **BLOCKED** | No durable project/command state exists to recover. `slideshow/service.py` builds a **fresh `Project` with a new uuid and a fresh `CommandBus` on every call** — project state does not survive between commands. |

### Command-bus authority questions (§10 of the tasking), answered

* **Can a command execute without bus validation?** No — R13 asserts that in the creative tree only
  `CommandBus` calls an operation handler (`test_command_capability_boundary.py`).
* **Can the studio core reach a shell?** No — `test_studio_core_cannot_execute_shell_media_or_ui_automation`
  explicitly lists `nexus_ai_agent.tools.system_shell` as forbidden.
* **Can storage mutate without a transaction boundary?** Not applicable inside studio — there is no storage there.
* **Can artifacts be created without evidence?** No, on the OTIO path — loss metadata is emitted per clip.
* **Can state be silently lost?** **Yes, and it is.** Everything in `creative/studio/` is process-local.
  This is the single largest architectural gap and it is why #129/#135 exist.

### Bypass-path classification (§9)

`src/nexus_ai_agent/tools/system_shell.py` is a real LLM-reachable `subprocess.run` site, registered
at `cli.py:1108` as `ShellTool(enable_shell=True)`. Classified **SAFE (fenced, deny-by-default)**, on evidence:

* `enable_shell: bool = Field(default=False)` in `config/settings.py` — **off by default**
* allow-list is read-only: `ls, pwd, echo, cat, grep, find, date`
* `shell=False`, argv list, `cwd` pinned to the workspace root, 10 s timeout
* path arguments containment-checked via `WorkspaceFilesystem`; `find -exec/-execdir/-delete/-ok/-okdir` blocked
* covered by `tests/unit/test_shell_sandbox.py` and `tests/unit/test_tools_sandbox.py`
* fenced out of the creative/command path by R13

No `LLM → ffmpeg` or `LLM → arbitrary DB` path was found: the other `subprocess` sites are
`adapters/whisper_local.py`, `creative/ffmpeg_executor.py`, `creative/rendering/executor.py`,
`creative/slideshow/ffmpeg.py` (all in the fenced render lane), `continuum/*` and `maintenance/backup.py`
(operator tooling), and `agent/updater.py` (behind `auto_update`, also default `False`).

---

## 5. Test-quality truth (§17)

The temporal suite was **not** able to distinguish exact from approximate rationalisation. All five
variants passed the **identical 29 tests**. Three mutations were applied to the shipped core and it
stayed green:

| mutation | before | after PR #146 |
|---|---|---|
| float path → `limit_denominator(100000)` | **survived** | killed (4 tests) |
| string path → `limit_denominator(100000)` | **survived** | killed (2 tests) |
| rounding guard → `assert` (stripped under `-O`) | **survived** | killed (2 tests) |
| OTIO export → float truncation | **survived** | killed (1 test) |
| deserialized timebase → `int()` coercion | **survived** | killed (1 test) |

The fourth survived because every existing export test used a **lossless** rate (`24.0`) or a **zero**
duration. `5 s @ 23.976` separates them: exact NEAREST is **120** frames, `int(float(5)*float(24000/1001))`
is **119**.

`tests/unit/test_temporal_mutation.py` claimed in its docstring to "execute test assertions against 14
explicit mutated implementations and verify that EVERY mutant produces a test failure". It contains
**no** file rewrite, monkeypatch or subprocess — it mutates nothing. The docstring is corrected in #146
and the real harness is `scripts/temporal_mutations.py` (10 mutants, CI job `temporal-mutations`,
`make mutations-temporal`), following the shape of the pre-existing `scripts/pack_trust_mutations.py`.

---

## 6. PR convergence table (all 63)

`MERGE` · `MERGE_AFTER` · `RESTACK` · `SPLIT` · `SUPERSEDE` · `CLOSE` · `BLOCK`

| PR | Behind | Mergeable | CI | Recommendation | Reason (measured) |
|---|---|---|---|---|---|
| 33 | 209 | CONFLICTING | green | **RESTACK** | 256 commits of drift; v3.13.0 already on `main`; 25 src files differ |
| 56 | 108 | CONFLICTING | green | **CLOSE** | 0 src files; dead-branch janitorial, superseded by 7 subsequent merges |
| 57 | 108 | CONFLICTING | 4 fail | **RESTACK** | research-v2 claim/lease; re-review after rebase |
| 58 | 108 | CONFLICTING | 4 fail | **CLOSE** | superseded by #105 (fail-closed shell grammar) and by DECISION_LOG `r10`, which already re-landed S1–S5 with 9/9 mutations |
| 59 | 108 | CONFLICTING | green | **RESTACK** | transactional outbox; overlaps #139 retention work |
| 60 | 108 | CONFLICTING | 6 fail | **RESTACK** | runtime integration; overlaps #112/#113/#115/#117 |
| 63 | 108 | CONFLICTING | 3 fail | **RESTACK** | board/Git truth gate; overlaps #137 (already APPROVED) |
| 64 | 99 | CONFLICTING | 4 fail | **SUPERSEDE** | by the #126–#132 spine stack, which routes through CommandBus instead of around it |
| 66 | 99 | CONFLICTING | green | **SUPERSEDE** | "one production path" objective now carried by the spine stack |
| 67 | 90 | CONFLICTING | green | **RESTACK** | artifact-producing runtime; salvage the artifact-evidence tests before restacking |
| 68 | 90 | CONFLICTING | green | **SUPERSEDE** | versioned command/capability boundary already on `main` (R13, `nagar.command.v1`) |
| 69 | 90 | CONFLICTING | green | **RESTACK** | contains #67 + #68; 99 files — must be split before any rebase |
| 70 | 90 | CONFLICTING | green | **SUPERSEDE** | operation matrix reconciled by later merged waves |
| 73 | 90 | CONFLICTING | green | **SUPERSEDE** | evidence gate now enforced by `scripts/pack_coverage.py` + continuum gate |
| 83 | 58 | CONFLICTING | green | **SUPERSEDE** | operation evidence chain superseded by continuum evidence foundation (#102/#107 merged) |
| 84 | 50 | CONFLICTING | green | **RESTACK** | constitution-as-contract; 0 src files, 12 docs |
| 87 | 50 | CONFLICTING | 1 fail | **SUPERSEDE** | Studio Core is on `main` (`creative/studio/`, 2094 LOC) |
| 88 | 50 | CONFLICTING | green | **RESTACK** | PACK-SEC-001; verify against merged ADR 0006 trust root first |
| 89 | 50 | CONFLICTING | green | **RESTACK** | 12 command wirings; re-check against current registry |
| 92 | 50 | CONFLICTING | 8 fail | **CLOSE** | draft; SQLite defaults, superseded by #130/#136/#143 lifecycle work |
| 93 | 50 | MERGEABLE | green | **RESTACK** | the only Era-A PR that is textually clean; LLM provider-failure typing; rebase is cheap |
| 94 | 46 | CONFLICTING | green | **RESTACK** | personality/presence lifecycle bounds |
| 96 | 46 | CONFLICTING | green | **RESTACK** | DR/restore drill; overlaps `maintenance` workflow |
| 97 | 46 | CONFLICTING | green | **DEFERRED → CLOSE** | new capability pack (portrait/scene Vision) — outside the current freeze |
| 99 | 46 | CONFLICTING | 2 fail | **RESTACK** | 11 command wirings; overlaps #89 |
| 100 | 46 | CONFLICTING | green | **RESTACK** | caption lane hardening; `creative/caption/` exists on `main` |
| 104 | 43 | CONFLICTING | green | **CLOSE** | 2 docs files recording a transition that has already happened |
| 105 | 42 | CONFLICTING | green | **RESTACK** | fail-closed shell argument grammar — supersedes #58; highest-value Era-A restack |
| 111 | 0 | MERGEABLE | green | **MERGE** | 2 docs files, zero code, no contention beyond `board.json` |
| 112 | 0 | MERGEABLE | green | **MERGE_AFTER** | contained in #113; merge the tip instead |
| 113 | 0 | MERGEABLE | 1 fail | **BLOCK** | contains #112; 1 failing leg must be resolved first |
| 114 | 0 | MERGEABLE | green | **DEFERRED** | *Draft*; Persian capability explorer = new product surface, inside the freeze |
| 115 | 0 | MERGEABLE | 2 fail | **BLOCK** | 2 failing legs |
| 116 | 0 | MERGEABLE | 1 fail | **MERGE_AFTER** | contained in #118 |
| 117 | 0 | MERGEABLE | green | **MERGE** | session/media contracts + wheel; CI green, no duplicate authority. *Draft* — needs ready-for-review |
| 118 | 0 | MERGEABLE | green | **MERGE** | stack tip containing #116; one LLM gateway authority. *Draft* — needs ready-for-review |
| 119 | 0 | MERGEABLE | green | **MERGE** | closes the shell workspace escape (task-201); security-relevant. *Draft* — needs ready-for-review |
| 120 | 0 | MERGEABLE | green | **MERGE** | storage key observability, 7 files, no contention |
| 121 | 0 | MERGEABLE | green | **MERGE** | memory retrieval truth |
| 122 | 0 | MERGEABLE | green | **MERGE_AFTER** | depends on #121 (routes chat through the memory reader) |
| 123 | 0 | MERGEABLE | green | **MERGE** | deterministic fake embeddings, 4 files |
| 124 | 0 | MERGEABLE | green | **MERGE_AFTER** | spine execution trace — belongs after the spine stack is split and landed |
| 125 | 0 | MERGEABLE | green | **MERGE** | typed confirmation refusal, 4 files |
| 126 | 0 | MERGEABLE | cancelled | **MERGE_AFTER** | base of the spine stack |
| 127 | 0 | MERGEABLE | green | **MERGE_AFTER** | contains #126 |
| 128 | 0 | MERGEABLE | green | **MERGE_AFTER** | contains #126+#127 |
| 129 | 0 | MERGEABLE | green | **MERGE** | **the only real PlanTransaction implementation** (27 occurrences, 4 studio files) |
| 130 | 0 | MERGEABLE | green | **SUPERSEDE** | by #143 — store file byte-identical, #143 ⊃ #136 ⊃ this fix |
| 131 | 0 | MERGEABLE | 8 fail | **SUPERSEDE** | by #134, a strict superset (same files + `diff.py`/`revision.py`) |
| 132 | 0 | MERGEABLE | green | **SPLIT** | stack tip carrying creative spine + studio undo + storage WAL in one PR |
| 133 | 0 | MERGEABLE | green | **MERGE_AFTER** | read-only Studio API; land after #129 so the exposed state is real |
| 134 | 0 | MERGEABLE | 8 fail | **BLOCK** | canonical Creative IR candidate, but 8 failing legs |
| 135 | 0 | MERGEABLE | unstable | **SUPERSEDE** | by #146 for temporal; title claims PlanTransaction/persistence that are absent (0 studio files) |
| 136 | 0 | MERGEABLE | green | **SUPERSEDE** | by #143, which contains it and replaces the flaky race probe |
| 137 | 0 | MERGEABLE | green | **MERGE** | APPROVED; board collision preflight + governance invariants, 0 src files |
| 138 | 0 | MERGEABLE | green | **MERGE** | *Draft*, 2 files; AST-based 3.10 parity detector that unblocks #131/#134 |
| 139 | 0 | MERGEABLE | green | **MERGE** | retention verdict spans all lifecycle rows |
| 140 | 0 | MERGEABLE | green | **SUPERSEDE** | by #146 for temporal + its gate amendment; title claims CommandBus hardening that is absent |
| 141 | 0 | MERGEABLE | green | **MERGE** | *Draft*, 2 files; AST-based rendering-lane boundary (false-green fix) |
| 142 | 0 | MERGEABLE | 3 fail | **SUPERSEDE** | by #146 (which took its successor #145's exact arithmetic + #140's gate amendment) |
| 143 | 0 | MERGEABLE | green | **RESTACK** | *Draft*; correct tip of the storage chain but based on #136's head branch — retarget to `main`, then MERGE |
| 144 | 0 | MERGEABLE | unstable | **SPLIT** | audit report + `continuum/pack_coverage.py` + a private temporal copy; drop the temporal copy, keep the rest |
| 145 | 0 | MERGEABLE | 3 fail | **SUPERSEDE** | by #146 — its core is what #146 adopted, plus the gate amendment it lacked |
| **146** | 0 | MERGEABLE | **34/34 green** @ `c971498` | **MERGE** | the canonical temporal convergence (this session) |

---

## 7. Derived merge order

Ordered by dependency, not by number or age: foundation → contracts → infrastructure → integration.

1. **#146** — temporal authority. Unblocks #135/#140/#144 from needing their own copy.
2. **#137** (APPROVED) — governance invariants, 0 src files. Then **#138, #141** (both drafts, 2 files
   each) — CI/architecture detectors; #138 explicitly unblocks #131/#134.
3. **#111, #120, #123, #125, #139** — disjoint, CI-green, non-draft, no duplicate authority.
   Then **#117, #118, #119** on the same grounds *once their authors mark them ready* (all three are drafts).
4. **#121 → #122** — memory, then the orchestration change that depends on it.
5. **#129** — PlanTransaction (the only real one), then **#133** which exposes it read-only.
6. **#143** after retargeting to `main` — closes out #130 and #136.
7. **#132** only after being **SPLIT** into spine / studio-undo / storage-WAL.
8. **#134** once its 8 failing legs are green — the canonical Creative IR.
9. Era A restacks, highest value first: **#105** (shell grammar), **#93** (already clean), **#88**, **#99/#89**.

**Recompute after each merge.** 27 of the 35 Era-B PRs touch `.agents/board.json` and 13 touch
`docs/README.md`; those are guaranteed textual conflicts on every landing and must be re-resolved,
not assumed. A merge can invalidate a PR downstream of it.

---

## 8. Remaining risks (evidence-backed only)

1. **Studio state is process-local.** Zero persistence references in `creative/studio/`, and
   `slideshow/service.py` mints a fresh `Project` + `CommandBus` per call. There is no transaction,
   no receipt store and therefore nothing to recover. Blocks §13/§14 of the tasking outright.
2. **`board.json` is a 27-way contention point.** Every landing re-conflicts it. AGENTS.md's rule is
   "newest state wins, schema 2 must still pass `test_agent_board.py`" — that rule needs automating
   before the merge queue can move quickly.
3. **`test_r_f28_concurrent_identical_learns_collapse_into_one` is intermittently red under load.**
   Observed once in two full-suite runs here; passes 5/5 in isolation; `main` CI and 4 CI legs on
   #146 are green. Cause: the test gives a 5-way asyncio race a hard-coded `50 × 5 ms` budget to
   reach the summariser. Not caused by this session's change (0 `knowledge/` files touched, 0
   references to `knowledge` in any changed file) — but it is a real latent flake.
4. **`maintenance` CI is red on `main`** because R2 secrets are unset (`task-164`, "3/3 red").
   Not a code regression, but `main` has never had a green scheduled backup.
5. **PR titles are unreliable.** #135 and #140 advertise PlanTransaction/persistence work that is
   absent from their diffs. Any triage driven by titles rather than diffs will mis-converge.
6. **Nine PRs are drafts** — #58, #92, #114, #117, #118, #119, #138, #141, #143 — and ten carry
   `CHANGES_REQUESTED` (#129, #131, #133, #134, #135, #136, #139, #140, #142, #144). Only three are
   `APPROVED` (#130, #137, #145). None of those states were overridden in this session. Note the
   overlap: **#117, #118 and #119 are recommended MERGE on CI/architecture grounds but are still
   drafts**, so each needs its author to mark it ready before it can land.

---

## 9. Next single slice

**Make `creative/studio/` project state durable behind one port — nothing else.**

* **Problem.** The canonical path in §9 of the tasking terminates at "LINEAGE / PROJECT GRAPH", but on
  `main` there is no project graph to terminate at. `CommandBus` validates, authorizes and applies
  correctly and then the result is dropped on the floor when the process ends. Every downstream
  claim about atomicity, recovery, idempotency and lineage is therefore unverifiable, and three PRs
  (#129, #135, #133) are blocked on it.
* **Value.** It is the one change that converts the studio from a per-call calculator into a system
  with a recovery story, and it unblocks the PlanTransaction and read-only-API PRs.
* **Dependency.** #129 (PlanTransaction) and #146 (temporal) — both already CI-green.
* **Smallest complete implementation.** One `ProjectStorePort` in `application/ports/` with
  `load(project_id) -> Project | None` and `save(project) -> None`; one SQLite implementation in
  `storage/` (which is where R6 permits persistence to live — the studio core stays stdlib+pydantic);
  `CommandBus` gains an optional injected store and persists after each successful apply. No new
  feature, no new surface.
* **Proof.** A restart test: dispatch a command, construct a *new* `CommandBus`, and assert the
  project state is observed. Then a crash-boundary test that kills the process between apply and
  persist and asserts the divergence is **detectable**, not silent. Then a mutation that removes the
  post-apply `save()` and requires the restart test to go red.
