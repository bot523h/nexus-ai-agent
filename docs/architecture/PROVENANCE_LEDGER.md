# The Causal Provenance Ledger & the Artifact Passport (task-231)

*Living view — updated in the same PR as the code it describes. Enforcing
tests: `tests/unit/test_provenance_chain.py`, `tests/unit/test_provenance_journal.py`,
`tests/integration/test_provenance_queue_recording.py`,
`tests/integration/test_provenance_passport_e2e.py`,
`tests/architecture/test_provenance_boundary.py`.*

## 1. What this is, and what it is deliberately not

The causal ledger is the **durable, tamper-evident account of why every job
result (artifact or typed failure) exists** — the Project-Graph question
*"exactly why and how did this artifact come into existence?"* — answered
from facts the system already commits, plus nothing.

It is **not** a second authority:

| Fact | Source of truth (unchanged) | Ledger role |
|---|---|---|
| job status / attempt / payload / result | `nexus_job_queue` row (`adapters/in_process_job_queue.py`) | digests of the content **at transition time** |
| artifact bytes / measured sha256 / probe | the artifact file + the queue's independent verifier block | re-measurement at passport time; recorded digests |
| causal order and completeness of the story | **the journal** (`provenance/journal.py`) — authoritative for the account itself | — |

Nothing in `provenance/` executes, authorizes, or mutates job state. It
observes durable facts after their commit and proves — or honestly refuses to
prove — what happened. Enforced by
`tests/architecture/test_provenance_boundary.py`.

## 2. Model

```
enqueue ─→ reserve(attempt N) ─→ verify(attempt N) ─→ complete(attempt N)
   │              │                     │                    │
   └──────────────┴───── fail / reopen / takeover ─────────┘
                    every edge: ONE hash-chained journal record
```

* **Node identity** is the system's own: `job_id` (durable queue id),
  `idempotency_key` (logical request identity), `attempt` (fencing token).
  No parallel identity space is invented.
* **Record** = one durable transition
  (`EventKind`: `job_enqueued`, `job_enqueue_duplicate`, `job_reserved`,
  `job_verification_started`, `job_completed`, `job_failed`, `job_reopened`,
  `job_takeover`), carrying canonical digests of the payload/result the row
  held at that moment (`payload_digest`, `result_digest`), the typed status
  and error, and a small `detail` (artifact identity facts on completion;
  takeover mode; backfill labels).
* **Chain**: flat single-writer hash chain from a genesis predecessor
  (`GENESIS_HASH`), domain-separated (`RECORD_DOMAIN`), over the exact
  canonical record bytes. Editing any byte of any record, deleting a middle
  record, splicing, or re-sequencing breaks `verify_chain` at the exact
  first broken `seq` (`test_provenance_chain.py` proves each attack class).
* **Exactly-once per transition**: transition records dedupe on
  `(kind, job_id, attempt)` under a UNIQUE key — a retry after a lost
  acknowledgement re-appends nothing. Observation records
  (`job_enqueue_duplicate`) never dedupe: their count *is* the duplicate/ACK
  evidence.
* **Serialization**: one lock + one transaction per append (head read, hash,
  insert) — concurrent job tasks never interleave a chain
  (`test_concurrent_appends_stay_serialized_and_chained`).

## 3. Where it lives, and the honest gap model

The journal is its own SQLite sidecar next to the queue's
(`worker.job_queue_db_path` → `provenance.paths.causal_journal_db_path`):
`<db>.jobs.sqlite3.causal.sqlite3`. The ledger is never a schema guest inside
the authority it observes, and never shares its transaction — a shared
transaction would let ledger failure veto execution, i.e. hand the ledger
authority (rejected, D-0024).

The cost is a real **crash window**: a process death between the queue's
durable commit and the journal commit leaves a hole. The system responds the
only honest way:

1. **Detection** — the passport cross-checks the chain against the live row
   and reports every unaccounted transition (`missing_transition_record`).
2. **Recovery** — `provenance.backfill.backfill_journal` re-derives the
   transitions the row *proves* (creation + the winning attempt's terminal
   record) and appends them flagged `backfilled: true`,
   `reconstructed_from: "nexus_job_queue"`. Idempotent under repeated
   crashes (dedupe). Mid-flight history is *not* reconstructed — one row
   cannot prove what it never recorded, and the gap stays visible.
3. **Status honesty** — any backfilled record caps the passport at
   `VERIFIED_WITH_LIMITATIONS`, never `VERIFIED`.

## 4. The Artifact Passport

`provenance.passport.PassportBuilder` is a **read-only projection** built,
per request, from exactly: the verified chain, the authoritative row
(`InProcessJobQueue.get_job_facts`), and — when the bytes are reachable — a
fresh re-measurement via the runtime's own `sha256_file` (the producer is
never the judge of its own output).

Shape is deliberately in-toto/SLSA-aligned (`subject` = artifact name +
`sha256` digest; `predicate` = the typed causal account: job, authority,
verification block, chain, reconciliation findings), so a future signing or
external-anchoring layer can wrap it without redesign. It is a local,
dependency-free dialect; no network claim is made.

Status contract (fail-closed; enforced in `test_provenance_passport_e2e.py`
over the real FFmpeg chain):

| Status | Meaning | Proven by |
|---|---|---|
| `VERIFIED` | chain intact + row fully accounted + live digests match the recorded ones + bytes re-measure to the recorded digest (failure passports expect no artifact) | the e2e render test |
| `VERIFIED_WITH_LIMITATIONS` | as above, but bytes are no longer reachable (delivered/retention-cleaned — normal here) and/or history contains labeled backfills | the removal + backfill tests |
| `INCOMPLETE` | missing transition records, no recorded artifact identity on a completed job, job not terminal, or row unknown | the broken-ledger + fresh-journal tests |
| `COMPROMISED` | broken chain, payload/result/status divergence between ledger and live row, artifact bytes that no longer match the recorded identity, or a row whose authoritative status cannot be parsed (`unparseable_row_status` — an impossible persisted state) | the tamper + corrupt-journal + unknown-status tests |

Reconciliation findings (typed, severities `note < limitation < incomplete <
compromised`) are part of the passport document — the reader sees *why* a
status was assigned, every time.

## 5. Threat model — what this proves, and what it cannot

| Attack | Outcome |
|---|---|
| Edit a journal record | `COMPROMISED` (`chain_broken`, exact seq) |
| Delete a middle record | `COMPROMISED` at the first anomaly |
| Splice a foreign record with forged seq | `COMPROMISED` (bytes↔column binding) |
| Rewrite the row's payload/result after the fact | `COMPROMISED` (digest divergence) |
| Swap the artifact bytes | `COMPROMISED` (`artifact_digest_mismatch`) |
| Claim a success that never ran (row forged without records) | `INCOMPLETE` (completeness check) |
| Truncate the journal tail | detected **for the affected job** (missing records); truncation of *other* jobs' tails requires an externally anchored head — `predicate.chain.journal_head` is published precisely so a future signed checkpoint can close this (documented limitation, not hidden) |
| Ledger storage loss at runtime | jobs unaffected (`causal_ledger_observe_failed` degradation log); passport honestly `INCOMPLETE` |
| Out-of-process SQLite rewrite of one row | caught by chain verification (`test_provenance_journal.py`) |
| Redeliver the same transition with a *different* claim (buggy recovery, lying writer) | `CausalConflictError` raised + the rejected claim quarantined as an `EVENT_CONFLICT` record (best-effort write): on the normal path the quarantine is durable and the passport reports `COMPROMISED`/`event_conflict_recorded`; if the quarantine write itself fails, nothing durable is left — the failure is logged (`causal_journal_quarantine_failed`) and the raised error **carries** the full rejected claim (`.rejected_event`, `.kept_seq`) as the durable carrier. The kept truth is never overwritten; identical redelivery stays an honest duplicate (`TestEvidenceConflicts`, `TestQuarantineDurability`) |
| Fabricate an ownership transfer (restart re-lists a pending row) | no takeover record: reservation is minted exactly once, per real attempt (`TestTakeoverTruthfulness`); shutdown re-lists only record `JOB_REOPENED` |
| Corrupt/locked sidecar at startup or mid-run | execution plane never dies: `try_open_causal_journal` degrades to `None` (`causal_journal_unavailable` log); record-time failures degrade per event (`causal_ledger_observe_failed`); passport honestly reports absent evidence (`TestTryOpenCausalJournal`, `TestRealJournalFailureDuringExecution`) |
| Two processes appending concurrently to one sidecar | serialized by `BEGIN IMMEDIATE` over head-read→hash→insert (retries on `IntegrityError` as defense-in-depth); proven by real-OS-process storm + cross-process dedupe tests (`TestCrossProcessSerialization`, `TestCrossProcessDedupe`) |

The known ceiling: an attacker who rewrites the *entire* journal consistently
and the row coherently defeats a local verifier — exactly the gap external
anchoring (RFC 3161 / a transparency log) exists for. The design publishes
`journal_head` and keeps the chain verify-only so that anchor can be added
without touching any producer.

## 6. Extension points (deliberately small)

* **Typed Intent / Plan identities** — the spine work (PRs #126/#127/#150)
  attaches `intent_id`/`plan_id` by extending `CausalEvent.detail`; no schema
  break. The passport projects `detail` verbatim.
* **Signed checkpoints** — anchor `journal.head()` periodically; the passport
  already publishes it.
* **More observers** — `CausalObserver` is a one-method port; a remote
  append-only mirror implements the same protocol without touching the queue.

## 7. Evidence classes (per the repository's evidence law)

| Claim | Class | Evidence |
|---|---|---|
| Chain tamper-evidence | VERIFIED | `tests/unit/test_provenance_chain.py` (attack matrix) |
| Exactly-once / dedupe / crash survive | VERIFIED | `tests/unit/test_provenance_journal.py` |
| Queue records every durable edge, fail-safe | VERIFIED | `tests/integration/test_provenance_queue_recording.py` |
| Passport over the real FFmpeg artifact | ARTIFACT-PROVEN | `tests/integration/test_provenance_passport_e2e.py` (+ CI run on the exact head SHA) |
| Production behavior at scale | NOT CLAIMED | no live-bot soak yet — do not upgrade this status without one |
