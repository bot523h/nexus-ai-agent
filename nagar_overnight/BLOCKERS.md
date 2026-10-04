# Nagar Overnight — BLOCKERS

Every blocker below has: symptom, exact command, output, files:lines,
hypothesis, attempts, why unresolved, effect on scope.

---

## B-1 — Pre-existing flaky test in the baseline (NOT caused by this session)

- **Symptom:** full-suite run reports 1 failure under concurrency.
- **Command:** `pytest -q`
- **Output:** `FAILED tests/unit/test_knowledge_hardening.py::test_r_f28_concurrent_identical_learns_collapse_into_one`
- **Files:** `tests/unit/test_knowledge_hardening.py:678-705`
- **Hypothesis:** timing-sensitive "cache stampede" assertion: it parks the
  winner and sleeps 50×5 ms expecting the 5 rivals to have reached the
  summariser. Under a loaded full-suite machine the sleep window can elapse
  before rivals arrive, so `len(llm.prompts)` can be `!= 1`.
- **Attempts:**
  1. Ran it in isolation 3× → **3 passed**. Confirms it is environmental, not a
     deterministic product defect.
- **Why unresolved:** the mission forbids loosening assertions to obtain green,
  and this is a pre-existing baseline failure outside this session's scope.
  It is documented, not "fixed".
- **Effect on scope:** none — the session's final suite is compared against the
  same baseline; the failure set must not grow (it did not).

---

## B-2 — No model provider adapter wired behind the cognition port (by design)

- **Symptom:** `nagar.cognition` ships `NullCognition` only; there is no
  `LocalCognition`/`CloudCognition` that calls an existing `LLMProvider`.
- **Files:** `src/nexus_ai_agent/nagar/cognition/port.py:33`,
  `src/nexus_ai_agent/llm/provider.py:6`
- **Hypothesis:** wiring a real provider is not required to prove the *boundary*;
  it is required to prove the *integration*. The boundary is the durable asset;
  the adapter is a follow-up with its own test/observability needs.
- **Attempts:** none — deliberately deferred (see ARENA_HANDOFF A-1).
- **Why unresolved:** adding a live provider overnight would require network
  configuration and a model-quality evaluation harness; the mission forbids
  "real external network calls in tests" and "claiming production readiness
  without production proof".
- **Effect on scope:** the boundary is IMPLEMENTED + VERIFIED with the null
  provider; a model-backed provider is DESIGNED, not implemented.
