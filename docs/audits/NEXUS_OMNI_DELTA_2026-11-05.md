# NEXUS-OMNI delta audit — 2026-11-05

Dated record. The six NEXUS-OMNI deliverables of mission
`NEXUS-OMNI-FORENSIC-RECONSTRUCTION-2026-11-03` already live in this directory
(`MASTER_FORENSIC`, `SECURITY_PRIVACY`, `CI_CD_SUPPLY_CHAIN`, `PR_CONVERGENCE_LEDGER`,
`RUNTIME_DATA_RECOVERY`, `MORNING_HANDOFF`, plus the `LIVE_CLOSEOUT_2026-11-04_FA`
follow-up). This page is the **delta** for the 2026-11-05 continuation: what was
re-verified against live `main`, the one new governance finding and its fix, and
the honest disposition of the open portfolio. It does not restate the six reports.

## 1. Ground truth re-verified (not assumed)

| Fact | Value | How it was checked |
|---|---|---|
| Live `main` | `aadaf53157670b64af6978fcf41b592e108ac9ae` | `git rev-parse origin/main` after fetch |
| PR #315 | **merged** 2026-11-05T13:13:23Z (merge commit `aadaf53157`) | GitHub API `pulls/315` |
| `main` push CI (`ci.yml`) | **success** on `aadaf53157` | run `42033855955` |
| `queue-ownership-mutations` on `main` | **success** | run `42033855945` |
| Open PRs | 42 (8 draft) | GitHub API `pulls?state=open` |

The prior session's `queue-ownership-mutations` disk-exhaustion defect
(`OSError: [Errno 28]`, run `41532014621`) is **already fixed on `main`**: the
workflow now installs only the dependency set the mutation suites import (no
torch/CUDA), then `pip install --no-deps -e .`. Re-verified locally:
`python scripts/queue_ownership_mutations.py` → **10/10 mutants killed**, tree
restored and green.

The `maintenance` workflow failure (`42014996179`) is **fail-closed by design,
not a defect**: the `backup-db` job's preflight refuses to run without
`NEXUS_DATABASE_URL` + a complete B2/R2 tier and exits non-zero on purpose
("an unmonitored missing backup is worse than a red job"). Job log:
`##[error]missing repository secrets: NEXUS_DATABASE_URL`. Owner action (configure
the secrets) — not a code repair.

## 2. New finding — board prerequisites were advisory, not enforced

**Severity P1 (governance / parallel-agent integrity).** `AGENTS.md` and the board
present dependencies as real (`task-282-slice-2` "waits on its dependency"), and
`.agents/board.json` carries a `prerequisites` field on claims. The shipped CLI
never read that field:

- `cmd_claim` granted a lease on blocked work;
- `cmd_next` proposed blocked work;
- the only test (`test_prerequisites_reference_existing_tasks`) checked that the
  referenced task *exists* — not that it was done.

So a dependency was a text note. A dependent slice could be started and its
`exclusive_paths` zone seized while its prerequisite was still
`queued`/`active`/`expired` — the exact "two owners, one zone" failure the board
exists to prevent.

### Fix (PR #316, branch `openhands/omni-recovery-integration-20261105`)

- `_unmet_prerequisites(board, claim)`: a prerequisite is satisfied **only** when
  its task exists and its status is terminal-`done`
  (`SATISFIED_STATUSES = {done, completed_merged}`). An `expired`/`queued`/
  `active`/`deferred` dependency, and a dependency on an unregistered task, are
  all **unmet**.
- `cmd_claim` refuses on unmet prerequisites (exit 2, board untouched);
  `--ignore-prerequisites` is the explicit owner-only override.
- `cmd_next` prints `BLOCKED <task> — unmet prerequisite(s): …` instead of
  proposing work `claim` will refuse.

### Evidence

```
$ python scripts/agent_board.py claim task-282-slice-2-artifact-assetversion-binding --branch test-branch
REFUSED: ... blocked by unmet prerequisite(s): task-282-durable-project-state-lifecycle — finish them (status 'done') first.
$ echo $?   # 2; board.json unchanged
```

- `python -m pytest tests/unit/test_agent_board.py -q` → **84 passed** (6 new:
  every non-`done` state incl. `expired`; `done` allows; ghost-task refuses;
  override flag; `next` never proposes blocked work; helper unit).
- `make lint` → All checks passed. `make types` → no issues in 361 source files.

## 3. Security / supply-chain re-check

- `pip-audit` on the runtime closure → 20 advisories in 3 packages:
  `filelock 3.32.7` (16 CVEs; fix ≥ 4.2.1), `chromadb 1.5.9` (5 PYSEC-2026-*),
  `diskcache 5.6.3` (1). `filelock`/`diskcache` are **transitive** (no
  first-party import); `chromadb` is imported **lazily** in
  `features/rag.py` behind the vector-store path. The remediation decision
  (version floor / accepted-risk) is recorded in
  `NEXUS_OMNI_CI_CD_SUPPLY_CHAIN_AUDIT_2026-11-03.md`; no new runtime dependency
  was added here.
- SSRF guard (`core/ssrf_guard.py`): https-only, every `getaddrinfo` address must
  be public, and the httpx transport re-resolves + re-checks at **each** connect
  (closes the DNS-rebinding TOCTOU). HMAC gates (`api/app.py`,
  `api/dashboard.py`) use `hmac.compare_digest`, fail closed with no key (503),
  and enforce a ±300 s freshness window.

## 4. PR portfolio disposition (delta)

42 open PRs, all base `main`. The board-touching ones (#313, #297, #285) are the
only ones that can conflict with `.agents/board.json`; the rest are large
feature branches from the historical `arena/*` fleet, all diverged far behind
`main`. Full per-PR classification A–G and the dependency-safe merge order are in
`NEXUS_OMNI_PR_CONVERGENCE_LEDGER_2026-11-03.md`. No PR was merged, rebased, or
closed by this session (owner-gated). #316 is the only PR opened.

## 5. Honest limits

- The `maintenance` backup/restore drill is **NOT_PROVEN** in this environment:
  it needs production `NEXUS_DATABASE_URL` + a B2/R2 tier that are not present.
  This is a configuration gap, not a code defect.
- Legacy PRs (#33, #58, #99, #129, #135, #140, #149, …) were re-confirmed as
  divergent/unmerged; their substrate is not re-audited line-by-line here.
- No runtime product code changed in this session; the fix is board-layer
  governance only.

## 6. Now / Next / Later

- **Now:** merge #316 (board prerequisite enforcement). Configure the
  `maintenance` backup secrets or accept the documented red as intentional.
- **Next:** decide the `filelock` version floor (raise it to ≥ 4.2.1 and prove
  `pip install -e ".[dev]"` still resolves); triage the 42 open PRs against the
  convergence ledger.
- **Later:** converge the largest feature branches (#66, #64, #69, #218) one at a
  time, dependency-safe, per the ledger's merge order.
