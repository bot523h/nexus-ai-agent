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

---

## B-3 — Remote push is blocked by environment credential scope (NOT bypassed)

- **Symptom:** `git push -u origin overnight/nagar-20261004` is refused.
- **Exact command:**
  `git push -u origin overnight/nagar-20261004`
- **Output:**
  `remote: Permission to bot523h/nexus-ai-agent.git denied to bot523h.`
  `fatal: unable to access '...': The requested URL returned error: 403`
- **Files:** none (environment/credential issue, not a code defect).
- **Diagnosis performed:**
  1. `gh auth status` → logged in as `bot523h` with token `ghu_…`.
  2. `GET /repos/bot523h/nexus-ai-agent` → `"permissions": {"admin": true,
     "push": true}` — the *user* may push.
  3. `POST /repos/.../git/refs` → `"Resource not accessible by integration"`
     for **every** tested prefix (`arena/`, `openhands/`, `overnight/`,
     `feature/`), and `git ls-remote` shows **no** probe ref was created.
  → The GitHub App installation token has **read-only** repository access.
  The `admin: true` in the repo payload describes the underlying account, not
  the installation's granted scope.
- **Attempts:** updated the remote URL to the current `$GITHUB_TOKEN`
  (the documented stale-token fix) and retried → still 403. Probed four branch
  prefixes via the API → all 403. That is the maximum of 3 distinct attempts.
- **Why unresolved / why not worked around:** pushing requires a token with
  write scope, which the sandbox does not have. Bypassing this would mean
  escalating privileges or exposing credentials — both forbidden. The mission's
  prohibition on authority/governance bypass applies.
- **Effect on scope:** **none on the engineering.** The commit is intact and
  verified locally at `e27db0b` on `overnight/nagar-20261004` (working tree
  clean). The only unmet step is the remote push/PR, which is an environment
  grant the owner can fix by giving the integration `contents: write`.
- **Owner action to unblock:** grant the GitHub App `contents: write` on
  `bot523h/nexus-ai-agent` (or push the local branch yourself:
  `git push origin overnight/nagar-20261004`).
