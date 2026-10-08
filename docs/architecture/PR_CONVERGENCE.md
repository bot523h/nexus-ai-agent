# PR convergence (law R20)

**Status:** living view · **Authority:** `scripts/pr_convergence.py` ·
**Enforcing tests:** `tests/architecture/test_pr_convergence.py`

---

## 1. The problem, measured

On 2026-10-07 against `main` @ `6122c9b`, this repository had **49 open pull
requests**, all based on `main`, spread across **13 distinct base SHAs**, from
three authors, touching 967 file entries between them. The board held **zero
active leases** for any of those 49 head branches.

That is not a plan; it is a queue nobody can read. The consequence is concrete:
**36 of the 49 PRs touch `.agents/board.json`**, 19 touch `docs/README.md`, 12
touch `docs/DECISION_LOG.md`, and 783 pairs of open PRs share at least one
changed path. Almost every merge therefore has to resolve a conflict, and nobody
can say which PR should go first.

## 2. What this tool does — and what it refuses to do

`scripts/pr_convergence.py` turns the queue into a **deterministic convergence
graph**. Its verbs are: DISCOVER, CLASSIFY, CORRELATE, DETECT, RANK, RECOMMEND.

**It never merges, closes, deletes, retargets, labels or comments.** That is not
a promise, it is a checked property: `test_the_tool_never_mutates_github` fails
if the source grows an HTTP write verb, a `gh pr merge|close|edit` call, or a
board write; `test_the_board_it_is_handed_is_not_mutated` proves the board it is
given is byte-identical afterwards.

It is also **not** a second coordinator. The board, its leases, lease fencing and
the GitHub transport stay in `scripts/agent_board.py`; this module imports them
(`ACTIVE_STATUSES`, `_parse`, `_gh_get`, `gc_expired`, `load_board`). What is new
is only the analysis.

Lease *liveness* is deliberately not imported: `agent_board._claim_live` compares
against the wall clock, which would make `--as-of` a lie. `claim_live_at(claim,
moment)` restates the board's own rule — active status, parseable `claimed_at`,
`moment <= claimed_at + ttl` — against the report's moment, and imports
`ACTIVE_STATUSES` so the definition of "active" cannot drift. `gc_expired`, which
rewrites claim status against the wall clock, runs only when the report moment
*is* the wall clock, so one clock decides everything in a report.

## 3. Classification

Every PR gets exactly one **primary** class, and *every* matched condition is
recorded in `signals`, so a PR that is both unowned and superseded reports both
facts instead of hiding one behind the other.

| Class | Meaning |
|---|---|
| `BLOCKED` | the evidence needed to classify it is missing or truncated — no conclusion is drawn |
| `DUPLICATE` | a member of a duplicate cluster that lost the survivor rank |
| `CANONICAL` | the chosen survivor of a duplicate cluster |
| `SUPERSEDING` | its change set contains another open PR's, or the board says so |
| `STALE` | composite staleness (see §6) |
| `ORPHANED` | no live board claim owns its head branch |
| `DEPENDENT` | shares changed files with another open PR |
| `ACTIVE` | recent activity, fresh evidence, no correlation pressure |

Precedence, most actionable first: `BLOCKED` → `DUPLICATE`/`CANONICAL` →
`SUPERSEDING` → `STALE` → `ORPHANED` → `DEPENDENT` → `ACTIVE`. A PR whose
evidence cannot be read is never given a substantive class, because that class
would be a guess.

## 4. Duplicate detection: proof, confidence, witnesses

A title is not proof. Each signal contributes a **witness string carrying the
measured value**, so a reviewer can re-derive the confidence by hand:

| Signal | Weight | Witness |
|---|---|---|
| S1 identical `head_sha` (same commits pushed twice) | 1.00 | the SHA |
| S2 near-identical change set (`jaccard ≥ 0.90`, `≥ 5` shared) | 0.65 | jaccard, shared, union |
| S2 strong file overlap (`jaccard ≥ 0.75`, `≥ 3` shared) | 0.50 | jaccard, shared, union |
| S3 same live board task | 0.30 | the task ids |
| S4 same live board zone | 0.15 | the zone ids |
| S5 same architectural objective (`≥ 5` shared paths, `≥ 70%` under one depth-3 subtree) | 0.20 | the subtree and the ratio |
| S6 title token overlap `≥ 0.60` | 0.10 | the overlap |

Declared a duplicate when the summed confidence reaches `duplicate_confidence`
(default `0.60`), capped at 1.0.

Two calibration facts, both measured against the live queue:

* **Incidental coordination overlap must not fire.** Thirty-six PRs share
  `.agents/board.json`; a pair sharing only that file plus two docs pages is
  *correlated*, not duplicated. S5 requires subtree concentration precisely so
  that coordination noise cannot carry a verdict.
* **A real near-duplicate must fire.** PR#140 and PR#144 shared 9 paths
  (`creative/temporal/*`, `delivery/operations.py` and their tests) at
  jaccard `0.82`, with PR#140's change set a strict subset of PR#144's. An
  earlier weight table scored that pair below the floor and the detector found
  **zero** duplicates on a queue that contained one. S2's tier and S5 exist
  because of that measurement. The pair is reported as a *supersession*
  (containment 1.0), which is the more accurate relationship.

## 5. Supersession

`PR A supersedes PR B` is recorded as a **relationship**, never as an action.
The superseded PR stays open; only the owner closes it.

* **Explicit** — a board claim whose status names the winner
  (`superseded_by_PR33`, seen live on `task-110-duplicate-F`) or a `supersedes`
  field. Confidence `1.0`, source `board`.
* **Derived** — the older PR's change set is a subset of the newer one's and the
  commits differ. Confidence `0.8` when both branches share a live zone, else
  `0.6`. Identical `head_sha` is excluded: that is a duplicate, not a
  supersession.

## 6. Staleness is composite, and inactivity is necessary

Age alone never retires a PR — an old PR that is still being updated is alive.
Base drift and "a newer PR touches the same file" are each true for most of this
queue, so either alone would retire the whole board. The rule is therefore:

> **stale** ⇐ explicit supersession, **or** (inactive ≥ `inactive_days` **and**
> at least one of: age ≥ `stale_age_days`, base drifted, a newer PR conflicts)

Thresholds are parameters on `Thresholds`, overridable from the CLI, and every
report carries the values it was produced with. They were chosen from the
measured shape of the queue, not hard-coded blindly.

## 7. Evidence freshness

Evidence recorded against an old SHA is never generalised to a new head. Per PR:

* `UNVERIFIABLE` — the changed-file list is truncated or empty → `BLOCKED`
* `PARTIAL` — some input is missing, or the base drifted, or the live `main`
  head was not supplied so drift is unmeasurable
* `FRESH` — every input present and consistent

`base_drift` is measured as `pr.base_sha != live_main_sha`. When no live head is
supplied the report says so explicitly instead of reporting `base_drift: false`
as if it had been measured. At capture time 42 of 49 PRs were `PARTIAL`, driven
by base drift across 13 base SHAs.

## 8. The dependency graph

| Relation | Source |
|---|---|
| `conflicts_with` | two open PRs share ≥ 1 changed path (witness: count + sample) |
| `duplicates` | a duplicate cluster edge, with confidence and witnesses |
| `supersedes` | §5 |
| `depends_on` | the board's declared `prerequisites`, resolved task → PR via head branch |
| `blocks` / `unlocks` | the inverse of `depends_on` |

A prerequisite naming a task no open PR owns produces **no** edge: an invented
dependency must never fabricate a merge order. A dependency cycle is reported in
`dependency_cycles` rather than hidden behind a plausible-looking order.

## 9. Recommended merge order — a recommendation, never a plan

Kahn's algorithm over the board's declared dependencies, tie-broken by a
measured score: class rank (`CANONICAL` → `ACTIVE` → `SUPERSEDING` →
`DEPENDENT` → `STALE` → `ORPHANED` → `BLOCKED` → `DUPLICATE`), then how many PRs
the merge unlocks, then conflict pressure, then scope size, then evidence
freshness, then PR number.

Merge authority stays with GitHub and the owner. The same input always produces
the same order, byte for byte — verified by running the tool twice over the live
inventory and comparing digests.

## 10. Live snapshot — dated, not live truth

`.agents/evidence/PR_CONVERGENCE_2026-10-07.json` holds the measured report for
`main` @ `6122c9b`: 49 PRs, classes `{STALE: 28, ORPHANED: 18, DEPENDENT: 2,
ACTIVE: 1}`, evidence `{FRESH: 7, PARTIAL: 42, UNVERIFIABLE: 0}`, 783
`conflicts_with` edges, 1 explicit supersession, no dependency cycles.

```bash
python scripts/pr_convergence.py --repo bot523h/nexus-ai-agent --json
python scripts/pr_convergence.py --prs-json inventory.json --main-sha <sha> --as-of <ts> --json
```

Exit codes: `0` report produced · `1` a `--fail-on <CLASS>` threshold tripped ·
`2` a source was unreadable (never a report).

## 11. Honest limits

1. **No patch-level comparison.** Duplicate detection uses change *sets*, board
   correlation, commit identity and titles — not diff similarity. Two PRs that
   edit the same file in disjoint ways are reported as `conflicts_with`, not as
   duplicates. That is the conservative direction.
2. **`ORPHANED` is a statement about the board, not about the work.** 18 PRs had
   no *live* claim for their head branch at capture time; several contain real,
   finished work. It means "this branch is unfenced right now", not "delete it".
3. **No CI or review state.** The report does not read check runs or reviews;
   "stale CI" from the staleness definition is therefore approximated by base
   drift and inactivity, and is labelled as such.
4. **The order is advisory.** It encodes the board's declared prerequisites and
   measured pressure. It knows nothing a human reviewer knows.

Persian summary: این ابزار صف PRها را به یک گراف همگرایی deterministic تبدیل
می‌کند — دسته‌بندی، تشخیص تکراری با شاهد و اطمینان، رابطهٔ جانشینی، گراف
وابستگی و ترتیب پیشنهادی. هیچ PR را merge/close/delete نمی‌کند؛ اختیار ادغام با
مالک مخزن است.
