# 08 — PR #218 split plan, dependency order, and per-file reconciliation (2026-10-16)

Live-truth pass of 2026-10-16 (session 3). Companion machine-readable artifact:
[`pr218_per_file_assignment.csv`](pr218_per_file_assignment.csv) — **301 rows**, one per path the
seven layer boundaries touch (299 of the 300 files in #218's final diff, plus
`.agents/board.json`, which nets to zero — §4).

**#218 is not split, not closed and not superseded by this document.** It records the plan,
the evidence for and against it, and the one action only the repository owner can take.

## 1. Why #218 cannot merge today (verified, not assumed)

| Fact | Live value |
|---|---|
| `main` | `6b398565cc1ce0c51fac77d7623350b299c3938f` — unmoved; it is an ancestor of the head, so no main-only content can be lost |
| #218 head | `5a727d7e581c2c57d70aebc1eff449e5e20df1a4` |
| Diff | 300 files, +67 462 / −1 787, 28 commits, no merge commits, no history rewrite |
| `mergeStateStatus` | `BLOCKED` — `reviewDecision=REVIEW_REQUIRED`, **0 reviews, 0 open review threads** |
| Required checks | **exactly three** — `lint (ruff + mypy + version lockstep)`, `merge-base-guard (base == main)`, `test (pytest -m "not slow")` — and all three **pass** on this head |
| CI on this head | `38892246018` success; `38895444312` success (both 21/21 jobs) |
| CodeRabbit | skipped — *"Review skipped: 182 files exceed the limit of 100"* (comment 6197253259) |
| Reviewer roster (all 63 open PRs) | `coderabbitai[bot]` submitted **every** review (11 APPROVED / 48 CHANGES_REQUESTED / 27 COMMENTED / 12 DISMISSED); `bot523h` only comments; `arena-ai-coding-agent[bot]` submits none |

So exactly one thing blocks the merge: a required approving review, which in this repository
only CodeRabbit can give, and CodeRabbit refuses this diff. `--admin`, `--auto`, self-approval
and head rewriting were not used, and will not be.

## 2. The seven layers that already exist (no rewrite needed)

The 28 commits form a linear chain, so a split is a **choice of boundaries**, not a rewrite:
each layer is a prefix of the same chain and the last layer's head **is** `5a727d7` — the
identical tree. Merging the seven in order reproduces #218 exactly; no file is dropped.

| Layer | base | head | files vs base | files vs main | migrations in layer | CI on that exact head |
|---|---|---|---|---|---|---|
| P1 | `main` | `807c197` | 94 | 94 | b5d8e2f4a917_telegram_ids_bigint.py | no run has ever executed on this head |
| P2 | `807c197` | `60a044f` | 2 | 95 | — | no run has ever executed on this head |
| P3 | `60a044f` | `0632801` | 87 | 161 | — | 38752924865 = **failure** (fixed only by later commits) |
| P4 | `0632801` | `6fb0a97` | 54 | 183 | c7a1e5d2b9f3_conversation_history.py | 38773326297 = success; 38777822652 = success |
| P5 | `6fb0a97` | `7bfd628` | 57 | 215 | — | no run has ever executed on this head |
| P6 | `7bfd628` | `8a733a7` | 65 | 269 | — | 38810548962 = success |
| P7 | `8a733a7` | `5a727d7` | 34 | 300 | — | 38892246018 = success; 38895444312 = success |

Layer themes:

* **P1** — A-17 storage/PG BIGINT + deeper validation of A-01/A-02/A-03; carries the board-lease commit
* **P2** — A-06 OpenAPI fail-closed route inventory
* **P3** — A-14 ruff ratchet + A-18 retired-engine disposal
* **P4** — A-11 complexity ratchet + queue decomposition + closeout A-15/A-16/A-19
* **P5** — A-13 strict typing + A-12 async offload + A-16 container + #209 remainder
* **P6** — A-04 board-ref cutover + Asset Library CAS + overnight evidence package
* **P7** — completion audit P1-1/P1-2 + 63-PR reconciliation

### The decisive finding: no green boundary exists below 182 files

| Boundary head | files vs main | exact-head CI |
|---|---|---|
| `9d31b39` | 28 | 38729021539 = **failure** |
| `a357b0f` | 62 | no run |
| `8fd4c44` | 76 | no run |
| `8690a4c` | 79 | no run |
| `807c197` | 94 | no run |
| `50abdb0` | 182 | 38768000380 + 38763520070 = **success** |
| `6fb0a97` | 183 | 38773326297 = **success** |
| `0632801` | 181 | 38752924865 = **failure** |
| `a0c2027` | 214 | 38793879776 = cancelled |
| `bd8aab5` | 266 | 38806486544 = **success** |
| `8a733a7` | 269 | 38810548962 = **success** |
| `5a727d7` | 300 | 38892246018 + 38895444312 = **success** |

Read with §2 this is the whole argument: **every boundary at or below 100 files has no green
CI** — the only two that were ever run are red — and **every boundary with green CI is ≥182
files**, i.e. above CodeRabbit's 100-file cap. A ≤100-file split therefore does not inherit
this evidence; it needs new fix commits per layer to become green, on branches this session is
not permitted to create (its identity is fixed to `arena/26f7d958-nexus-ai-agent`).

## 3. Per-PR contract for the split (if the owner chooses it)

| Requirement | Value for every layer |
|---|---|
| Acceptance criteria | the three required checks (`lint`, `merge-base-guard`, `test`) green **on that exact head**, plus a CodeRabbit review that completed (not skipped) and no unresolved `CHANGES_REQUESTED` |
| Evidence required | CI run id + per-job conclusions for the layer head; CodeRabbit review id; `gh pr checks <n>` output; the layer's `git diff --name-status` list |
| Tests | the layer's own suite must pass on its head — for P1–P3 this is **not** true today (§2), so each needs its own fix commit first |
| Migration order | `47903d282ede` → `f4a9c2e71b08` → `7c2f9d41e8a3` (main head) → **`b5d8e2f4a917`** (P1) → **`c7a1e5d2b9f3`** (P4); single head at every layer |
| Base/head | strictly stacked: P1 `main→807c197`, P2 `807c197→60a044f`, P3 `60a044f→0632801`, P4 `0632801→6fb0a97`, P5 `6fb0a97→7bfd628`, P6 `7bfd628→8a733a7`, P7 `8a733a7→5a727d7` |
| Conflict status | none today: `main` is an ancestor of every layer head, so each is a clean fast-forward candidate while `main` does not move |
| Revert method | `git revert -m 1 <merge-commit>` per layer, in reverse order; layers are separate commits, so reverting P_k leaves P_k+1's content intact |
| Review coverage | every layer carries src, tests, migrations, workflows and docs in proportion to its commits (see the CSV) |

## 4. Per-file reconciliation (all 300 files)

[`pr218_per_file_assignment.csv`](pr218_per_file_assignment.csv) has **301 rows**: for each path,
the first layer that lands it, its status vs `main`, how many commits touch it, and the commit
SHAs. The extra row is `.agents/board.json`: commit `2c2ebaf` changes it (board lease) and the
A-04 cutover (`5bc146e`) moves the authority to `refs/nexus/board`, so it nets back to `main`'s
content and does **not** appear in #218's final diff. 299 + that one = 300 accounted for,
**0 unassigned**.

| Layer | files first landed | example paths |
|---|---|---|
| P1 | 94 | `.agents/board.json`, `.env.example` |
| P2 | 1 | `tests/integration/test_api_gate_inventory.py` |
| P3 | 66 | `scripts/agent_board.py`, `scripts/board_referee_mutations.py` |
| P4 | 22 | `AGENTS.md`, `Dockerfile` |
| P5 | 32 | `src/nexus_ai_agent/adapters/in_process_job_queue.py`, `src/nexus_ai_agent/adapters/langgraph/lifecycle_recording.py` |
| P6 | 55 | `.github/workflows/dependency-audit.yml`, `docs/MULTI_AGENT_PROTOCOL.md` |
| P7 | 31 | `docs/audits/2026-10-16-completion/01_LIVE_TRUTH.md`, `docs/audits/2026-10-16-completion/02_PR218_INDEPENDENT_AUDIT.md` |

## 5. The two lawful routes (and who can take them)

**Route A — owner, one action: raise the review capacity.** CodeRabbit is configured from the
**Organization UI** on the **Advanced** plan (its own run-configuration block states this). Its
per-review file limit is an organization setting; the usage-based add-on
(Organization Settings → Usage-based add-on) can authorize a large-PR review up to 300 files
through an explicit on-demand action. This PR's CodeRabbit file count was 182 at the last skip
and is ≤300, so it is inside that bound. No repository file can change this, and no secret is
involved.

**Route B — split into the seven stacked PRs of §2.** Requires (i) creating seven branches at
the SHAs above, (ii) new fix commits so the P1–P3 heads are green, (iii) seven review cycles
and seven merges. This session cannot execute it: its branch identity is fixed to
`arena/26f7d958-nexus-ai-agent`, and creating or pushing to other branches is out of scope.

**Route B is not recommended while Route A is available** — it multiplies review cycles by
seven and still depends on the same reviewer, which is the actual bottleneck.

## 6. What is deliberately untouched

* #218 stays **open**; nothing is closed, marked superseded, force-pushed or rebased.
* No branch of another PR was written to; #210's two findings remain its author's to fix.
* No new Nagar Creative Intelligence feature was added in this stabilization path.
* Backups/restore stay `BLOCKED_BY_EXTERNAL_DEPENDENCY` (five repository secrets absent; names
  only, never values).

