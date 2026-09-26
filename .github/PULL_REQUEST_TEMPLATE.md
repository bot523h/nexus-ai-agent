<!--
Pull-request body contract for NEXUS AI Agent.

Two layers, in this order:
  1. AGENTS.md §2 — task id, zone, acceptance criteria, the exact commands run and their
     observed result ("tests pass" without a command is not evidence);
  2. ENGINEERING_CONSTITUTION.md Article 14 — the Final Gate: CODE + TEST + SECURITY + CI +
     OBSERVABILITY + DOCUMENTATION + BOARD EVIDENCE, all present together.

Keep the headings: the body is read by humans and by the next agent, and
tests/unit/test_engineering_constitution.py checks that this template still names every gate.
-->

## Task & zone

- **Task id:** `task-…` (from `.agents/board.json`)
- **Branch (canonical identity):** `arena/…`
- **Zone / exclusive paths:**
- **Board state:** claimed → released / deferred (push the board change, not just this PR)

## Article 14 — Final Gate

No lane is DONE unless all seven are present together. Mark each `[x]` only with evidence.

- [ ] **CODE** — the change is complete on this branch; no placeholder stands in for a real implementation
- [ ] **TEST** — command(s) run and observed result(s):
- [ ] **SECURITY** — security boundary untouched, or re-verified against `docs/architecture/SECURITY.md`
- [ ] **CI** — jobs green on this head SHA (`lint`, `test`, `extras-matrix`, `python-parity`, `migrate-postgres`, `release-lineage`, `lint-fast`)
- [ ] **OBSERVABILITY** — structured events / metrics for new behaviour (`docs/architecture/OBSERVABILITY.md`)
- [ ] **DOCUMENTATION** — living views updated in this PR; docs gates green (`pytest -q tests/unit/test_docs_integrity.py`)
- [ ] **BOARD EVIDENCE** — claim + acceptance criteria named; commands and results above; deferral recorded if stopped

## Article 12 — decision record

`OPTIONS` · `TRADEOFFS` · `DECISION` · `WHY` · `RISK` · `TEST` (required for every non-trivial decision;
write "no alternative considered — mechanical change" only when that is literally true)

## Article 9 — findings

Every problem found while working, closed in exactly one state:
`FIXED` · `VERIFIED` · `DEFERRED_WITH_REASON` · `KNOWN_ENVIRONMENTAL` · `NOT_REPRODUCIBLE` · `FALSE_POSITIVE`

## Evidence log

```
$ <command>
<observed output / artifact / SHA>
```

## Gates ownership

- [ ] I hold `gates_owner` on the board and ran the full gates (`make lint && make types && make test`)
- [ ] Full gates deferred to the gates owner / to CI (local diagnostics only: `ruff check`, targeted `pytest`)
