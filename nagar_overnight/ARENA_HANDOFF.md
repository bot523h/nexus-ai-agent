# Nagar Overnight ŌĆö ARENA HANDOFF

Specialist follow-ups that are architecture- or security-sensitive, with
reproduction, evidence, affected lines, risk, confidence, and why an overnight
patch would be unsafe.

---

## A-1 ŌĆö Bind a real model provider behind `CognitionPort` (adapter IMPLEMENTED; live binding deferred)

- **Reproduction:** `python -c "from nexus_ai_agent.nagar.cognition import LocalCognition"`
  ŌĆö the adapter exists now; what is still missing is a composition root that
  constructs a live `LLMProvider` (Gemini/local-server/fake) and binds it.
- **Evidence:** `src/nexus_ai_agent/nagar/cognition/adapter.py` (`LocalCognition`,
  `TextGenerator`), `src/nexus_ai_agent/llm/provider.py:6` (the existing
  `LLMProvider` ABC it satisfies structurally).
- **Status:** the adapter is **IMPLEMENTED + VERIFIED** (unit + end-to-end with a
  fake provider). Live-provider binding in the bot/worker runtime is deferred.
- **Affected files:** the composition root that constructs providers (runtime
  wiring), not the adapter.
- **Risk:** a provider adapter is the first place untrusted model text becomes a
  proposal. `LocalCognition` already routes every response through
  `parse_proposal` and cannot reach the bus directly; the remaining risk is in
  *choosing* a provider via the deterministic router, which must not let a model
  influence routing or privilege.
- **Confidence:** high that the adapter is correct (27 unit + 11 e2e tests, and
  the full suite green); medium on the runtime binding because it touches the
  highest-conflict `bot/handlers.py` surface.
- **Why not patched overnight:** binding a live provider adds an external
  dependency and touches the bot surface; doing it without a token/latency
  metric story (the mission's ┬¦31 metrics) and without touching
  `bot/handlers.py` would risk scope creep and conflict.
- **Recommended investigation:** construct `LocalCognition(provider)` in the
  worker composition root behind a feature flag, select it with
  `DeterministicRouter`, and add token/latency instrumentation to `LLMProvider`
  (the contract currently exposes no usage field, so the adapter deliberately
  reports none).

## A-2 ŌĆö Causal Project Graph + Artifact Passport (NOT-FOUND on main)

- **Reproduction:** `grep -rn -iE "artifact.passport|project.graph|causal" src` ŌåÆ
  no matches (only unrelated checkpoint `verify_lineage`).
- **Evidence:** `docs/overnight/RECON.md` ┬¦"FOUND/NOT-FOUND".
- **Risk:** high ŌĆö this is where "why does this artifact exist?" becomes durable
  truth. Getting the canonical/derived/immutable distinction wrong would bake a
  second source of truth into the product.
- **Why not patched overnight:** the mission explicitly says treat the causal
  graph as one of the most important assets and define which facts are canonical
  / derived / immutable / superseded *before* implementing. That is a design +
  storage-migration task, not a one-night change.
- **Recommended investigation:** design the fact taxonomy against the existing
  `Job`/`Attempt`/`EditTransaction`/`AssetRecord` records, then implement a
  projection (passport) over canonical evidence only.

## A-3 ŌĆö Legacy chat/agent paths can treat model text as a decision

- **Reproduction:** `grep -rn "LLMProvider" src/nexus_ai_agent/agents` ŌåÆ
  `chat_agent.py:13`, `qwen_agent.py:26`, `gemma_agent.py:32`, `phi_agent.py:27`
  call `generate(prompt)` and consume free text directly.
- **Evidence:** `docs/overnight/RECON.md` ┬¦B and ┬¦F.
- **Risk:** high ŌĆö this is the "hidden authority" surface: on these paths there
  is no typed proposal and no bus gate, so a caller *could* act on model text.
  The studio path is safe (everything goes through `CommandBus`); these legacy
  paths are not.
- **Why not patched overnight:** replacing them touches working capability and
  the `bot/handlers.py` highest-conflict file; the mission forbids destroying
  working capability to impose a preferred architecture.
- **Recommended investigation:** migrate these call sites to `CognitionPort`
  incrementally, one agent at a time, with a contract test per migration.

## A-4 — `phi_agent.moderate` fail-open (policy weakness) — **RESOLVED**

- **Reproduction (pre-fix):** `src/nexus_ai_agent/agents/phi_agent.py:30-36` —
  `moderate()` parsed a model JSON verdict and returned
  `{"safe": True, "reason": "parse_error"}` when the output was not valid JSON.
- **Risk:** medium — a *policy* weakness (an unparseable safety verdict read as
  "safe"), never an execution bypass (the verdict does not select or grant an
  operation).
- **Resolution:** fixed in commit `b257d5f` (fail-closed). An unparseable verdict
  is `{"safe": False, "reason": "parse_error"}`; a non-dict / `safe`-less verdict
  is `malformed_verdict`; `safe` is normalised so only JSON `true` counts. Covered
  by `tests/unit/test_phi_moderation_fail_closed.py`.

## A-5 — Phase 5 convergence not remotely delivered

- **Symptom:** `git push` → `403 Permission to bot523h/nexus-ai-agent.git denied`.
- **Effect:** commits `c6e6bc2`, `44a821b`, `3ff43a8`, `4addc9d` (and the whole
  `overnight/nagar-20261004` branch) exist only locally; no PR, no CI, no main
  integration. Status is **VERIFIED (local)**, never **REMOTE-DELIVERED**.
- **Recommended action:** grant the App installation `contents: write`, then
  push the branch and open one PR (do not re-apply any commit).

## A-6 — PR #155 diverged from `main`; resolved by fast-forward (Phase 3)

- **Symptom:** PR #155 (`overnight/nagar-20261004` @ `4572322`) reported
  `mergeable: CONFLICTING` / `mergeStateStatus: DIRTY` against `main` @ `6e41123`.
- **Root cause:** #155 was branched from the old main (`e5b326b`, per
  `git merge-base`) and is 15 commits behind; newer main-bound PRs touched the
  same files.
- **Evidence (read-only):** `git merge-base --is-ancestor overnight/nagar-20261004 HEAD`
  -> true; `git rev-list --left-right --count overnight/nagar-20261004...HEAD` -> `0  19`.
  i.e. #155 is a strict ancestor of the converged branch `sync/nagar-gatec-20261005`
  (merge-base == `origin/main`).
- **Resolution (no history rewrite):** fast-forward the PR branch to this branch's
  head once pushed. `git push origin sync/nagar-gatec-20261005:overnight/nagar-20261004`
  is a **fast-forward** — no force-push, no rebase, no protected ref touched. This
  updates the existing PR #155 in place.
- **Confidence:** high (ancestry proven locally; refs read-only).
- **Status:** pending the push step below.
