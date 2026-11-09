# ADR-0002 — A closed eight-state verdict, with a fixed precedence

- Status: **Accepted** (implemented and tested)
- Date: 2026-11-09

## Context

A verifier that returns a similarity score forces every caller to invent its own
policy, and every caller invents a different one. Worse, the most dangerous outcome
is not "wrong" but "confidently wrong": a numeric score hides the difference between
*we compared and they differ* and *we could not compare*.

## Decision

The verifier emits exactly one of eight states:

`VERIFIED`, `VERIFIED_WITH_LIMITATIONS`, `REVIEW_REQUIRED`, `IDENTITY_DRIFT`,
`WARDROBE_DRIFT`, `BODY_DRIFT`, `CANON_CONFLICT`, `INSUFFICIENT_EVIDENCE`

with a fixed precedence for aggregation:

```
CANON_CONFLICT > IDENTITY_DRIFT > WARDROBE_DRIFT > BODY_DRIFT
> REVIEW_REQUIRED > INSUFFICIENT_EVIDENCE > VERIFIED_WITH_LIMITATIONS > VERIFIED
```

`INSUFFICIENT_EVIDENCE` deliberately outranks the verified states: absence of
evidence is not evidence of continuity.

## Consequences

* Callers cannot invent a policy: they either accept a state or escalate it.
* `INSUFFICIENT_EVIDENCE` is not a failure of the run, it is a *finding*, and the
  report must always name which dimension produced it and why.
* The vocabulary is closed, so a consumer can switch exhaustively on it; adding a
  state is a breaking schema change.
* Dimension-level states are retained in the report even when the aggregate verdict
  is more severe, so a reviewer can see the whole picture.

## Alternatives rejected

| alternative | why rejected |
|---|---|
| numeric score + caller threshold | pushes the decision to many callers, all different |
| boolean pass/fail | cannot express "review" or "cannot tell" |
| free-text verdict | not machine-checkable, not auditable |

## Evidence

`experiments/character_capsule/continuity.py` (`VerificationState`, `STATE_PRECEDENCE`,
`aggregate_verdict`), `tests/character_capsule/test_continuity_states.py`
(`test_state_vocabulary_is_closed_and_ordered`,
`test_precedence_prefers_the_most_severe_state`,
`test_empty_evidence_is_insufficient_not_verified`).
