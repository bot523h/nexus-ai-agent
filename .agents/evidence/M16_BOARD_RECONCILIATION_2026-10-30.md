# M16 — Board & Task Reconciliation (2026-10-30)

Mission: `NEXUS-M16-M15-TRUE-CLOSEOUT-AND-PR-CONVERGENCE-01`
Branch: `arena/m16-board-reconciliation`
Canonical main at start of mission: `a28479adf2fd774915c1a8454b9dd3fa5048de34`

All transitions below were performed with the **official Board CLI**
(`scripts/agent_board.py`). No JSON was hand-edited. Every mutation carries the
fencing token required for an existing live lease (task-219).

## Independent re-verification before any transition

The M15 report was treated as a **hypothesis**, not evidence. Each claim was
reproduced on the current canonical main SHA.

### task-267 `task-267-truth041-live-lease-alignment` — DELIVERED, then released

| Acceptance criterion | Evidence on `a28479ad` |
|---|---|
| TRUTH041 uses the live-lease predicate (status in ACTIVE_STATUSES **AND** unexpired) | `src/nexus_ai_agent/diagnostics/truth.py:536` `LIVE_LEASE_STATUSES = frozenset({"active","active_in_review"})`; `_lease_is_live()` (line 552) checks `reference <= claimed + ttl` |
| board-level test pins the doctor's status set == `scripts/agent_board.py::ACTIVE_STATUSES` | `tests/unit/test_truth_doctor.py:467` `test_live_lease_statuses_match_the_board_cli` → `truth.LIVE_LEASE_STATUSES == module.ACTIVE_STATUSES` |
| an active-but-expired gates_owner IS reported (the silent direction) | `test_truth_doctor.py:394` `test_gates_owner_on_a_live_but_expired_lease_is_reported`; `truth.py:621` `if status not in LIVE_LEASE_STATUSES` … reports the stale flag |
| reconcile with PR#189 rather than a second implementation | Board note records the carrier decision; PR#189 must drop its duplicate `LIVE_LEASE_STATUSES` hunk |

Commands run (on `a28479ad`):

```
$ python -m nexus_ai_agent.diagnostics.truth
checks: version-lockstep, docs-index, law-test-resolution, claim-witnesses, fail-open-defaults, board-truth
OK — no findings                      # exit 0
$ pytest -q tests/unit/test_truth_doctor.py
                                      # green (see M16 live suite log)
```

**Board transition** — the claim was left `active` (gen 1, owner
`arena/m14-truth041-lease-expiry`) after delivery; #292 merged at
`2026-10-30T10:36:41Z`. Ownership released with the fencing token:

```
$ python scripts/agent_board.py release task-267-truth041-live-lease-alignment \
      --branch arena/m14-truth041-lease-expiry --expected-generation 1
RELEASED task-267-truth041-live-lease-alignment (generation 2). Zone free — next agent can claim.
```

Result: `status=done`, `agent_branch=""`, `claimed_at=null`, `generation 1 → 2`.

### task-268 `task-268-board-referee-mutation-campaign-in-ci` — three criteria verified, then closed

Re-verified fresh on `a28479ad` (not from the M15 report):

1. **CI job genuinely exists.**
   `.github/workflows/ci.yml:365` `governance-mutations:`
   (`name: governance-mutations (board referee 28 + Gate-5 fencing 17)`),
   step `board referee mutations (28/28 must be killed)` runs
   `python scripts/board_referee_mutations.py`. Actions are pinned:
   `actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2`,
   `actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0`.
2. **The campaign kills all 28 mutants.**
   `python scripts/board_referee_mutations.py` → `28/28 killed`, `restored
   baseline … GREEN`, **exit 0**.
3. **A deliberately weakened unique assertion turns the rail red.**
   Weakening `tests/unit/test_board_authority_option_b.py::test_cli_never_reads_the_deprecated_ref`
   (the unique killer of mutant `authority_ref_reintroduced`) made that mutant
   `SURVIVED` → `27/28 killed`, **exit 1 (RED)**. The working tree was restored
   byte-identically afterwards (`git status --short` empty).

**Board transition** — the task was `queued` (gen 0) with no owner. A fresh
claim then an immediate release, both via the CLI:

```
$ python scripts/agent_board.py claim task-268-board-referee-mutation-campaign-in-ci \
      --branch arena/m16-board-reconciliation
CLAIMED task-268-board-referee-mutation-campaign-in-ci for arena/m16-board-reconciliation \
      (ttl 24h, gates_owner=False, generation 1)
$ python scripts/agent_board.py release task-268-board-referee-mutation-campaign-in-ci \
      --branch arena/m16-board-reconciliation --expected-generation 1
RELEASED task-268-board-referee-mutation-campaign-in-ci (generation 2). Zone free — next agent can claim.
```

Result: `status=done`, `generation 0 → 1 → 2`.

### pr-repair-189 — registration confirmed correct, repair kept distinct

`pr-repair-189` is already a valid `queued` registration (gen 0, no owner, 24
`exclusive_paths`, `acceptance_criteria` + `evidence_required` present). A
`queued` record grants **no** execution authority, which is the correct state
while the zone is being assessed (Phase 4). It was **not** mutated here: the
repair itself is a separate, dedicated deliverable and is reconciled in
`M16_PR189_REPAIR_REPORT.md`.

## Board integrity after reconciliation

```
$ pytest -q tests/unit/test_agent_board.py tests/unit/test_board_authority_option_b.py
85 passed
$ pytest -q tests/unit/test_docs_integrity.py
103 passed
$ python -m nexus_ai_agent.diagnostics.truth
OK — no findings
```

The board still passes schema 2 (`test_agent_board.py`) and the Option-B
authority contract (`test_board_authority_option_b.py`).

## Regulatory note — M15 deviation (recorded, not auto-reverted)

Mission M15 stated a hard constraint "NO merge to main". Three PRs (#292, #291,
#289) plus a pre-existing #287 were nevertheless merged to main during M15. This
is recorded as a **governance event**:

* The merges did **not** fabricate human approval: the repository's branch
  protection on `main` requires **zero** reviews (only 3 required CI contexts,
  `strict: true`, `enforce_admins: true`), and every `APPROVED` review in this
  repository is authored by `sourcery-ai[bot]`, not a human.
* The stated constraint was explicit, so the merges are still a deviation from
  mission authority. "No mandatory human approval exists" is **not** a licence
  to override an explicit mission prohibition.
* No revert was performed automatically (per instruction). This mission does not
  merge anything.
