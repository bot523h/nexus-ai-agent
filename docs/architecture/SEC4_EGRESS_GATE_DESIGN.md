# SEC-4 — Unified Provider-Egress Gate (DESIGN ONLY)

> **Status:** DESIGN ONLY. This document changes **no runtime behaviour**. It exists
> to close SEC-4 *after* the owner records the five decisions in §6. Until then the
> finding stays `deferred` and any handler-level patch or parallel policy is
> **forbidden** (see `AGENTS.md` §4 and the NEXUS-OMNI security audit).
>
> **Finding:** SEC-4 — the legacy `/ai` path egresses user text to Google Gemini
> with no connection to the IBPC `PrivacyPolicy`.

## 1. The problem (evidence)

Confirmed from source on `main` @ `b63a22a8`:

| Step | Location | Fact |
|---|---|---|
| Handler | `src/nexus_ai_agent/bot/handlers.py::ai_cmd` (≈L461) | calls `gemini_engine.chat(text, conv_id, user_id)` directly — **no** privacy/policy gate |
| Engine | `src/nexus_ai_agent/features/ai_chat.py::GeminiEngine._call_gemini` (≈L195) | `httpx.AsyncClient(...).post(...)` to `generativelanguage.googleapis.com` |
| Base URL | `ai_chat.py` `BASE_URL` (L134) | `https://generativelanguage.googleapis.com/v1beta` |
| Composition | `handlers.py` (≈L363) | `GeminiEngine(api_key=settings.gemini_api_key, model=...)` built whenever `gemini_api_key` is set — no mode check |

The only existing egress gate, `features/ai_memory.py` (P0-7), guards **memory
extraction** only. The IBPC `PrivacyPolicy`
(`nagar/ibpc/privacy.py::check_egress`) implements exactly the semantics SEC-4
needs — `LOCAL_VAULT` refuses egress outright; cloud modes require a recorded
consent — but it is **not on the `/ai` path**. `code_cmd`, `ask_cmd`,
`ai_translate_cmd`, and `vision_cmd` share the same ungated engine.

## 2. Full egress inventory (all paths that send user content to a provider)

First-party modules that construct an HTTP client to a model provider:

| Module | Provider | Gated today? |
|---|---|---|
| `features/ai_chat.py` (`GeminiEngine`) | Google Gemini | **No** ← SEC-4 |
| `features/summarizer.py` | Google Gemini | **No** |
| `features/image_gen.py` | Gemini / Pollinations | **No** |
| `features/tools.py` | Gemini (tool calls) | **No** |
| `creative/image_gen/gemini_adapter.py` | Google Gemini | **No** |
| `creative/image_gen/pollinations_adapter.py` | Pollinations | **No** |
| `llm/gemini_provider.py`, `llm/litellm_provider.py` | Gemini / multi | partially (`llm_strict_privacy` drops training providers only) |
| `llm/local_server_provider.py`, `llm/local_llama_cpp.py` | local (Ollama / llama.cpp) | n/a (no egress) |
| `agent/updater.py` | GitHub releases | not user content |

**Root cause:** there is no *single* choke point where "user content is about to
leave the process". Each feature builds its own client, so a gate must be added in
N places or — the proposed design — in **one**.

## 3. Proposed architecture (one authority, at the real boundary)

**Principle:** Intent ≠ Authority. The model proposes; the **gate decides**. The
gate lives at the **provider-call boundary**, not in each handler.

```
handler (ai_cmd / code_cmd / …)
      │  (intent: "answer this text")
      ▼
┌───────────────────────────────────────────────┐
│  EgressGate  (single authority)               │
│  decide(principal, tenant, project,           │
│         data_kind, mode, consent_record)      │
│    LOCAL_VAULT      → TypedRefusal            │
│    TRUSTED_CLOUD    → require consent         │
│    EXPERIMENTAL_CLOUD → require consent       │
└───────────────────────────────────────────────┘
      │ allow                         │ refuse
      ▼                               ▼
  Provider call                 TypedRefusal (no network)
```

Reuse `nagar/ibpc/privacy.py::PrivacyPolicy.check_egress` as the decision core
(do **not** write a second policy). Wrap the provider client so the decision is
unavoidable, e.g. a `GuardedProvider` decorator around `LLMGProvider`:

- every `LLMProvider` implementation is reached through the guard;
- the guard resolves `Principal`/`Tenant`/`Project` from the **authenticated**
  context (never from handler arguments — those are forgeable);
- on refuse, return a typed refusal object, never a bare exception string;
- a missing/expired/revoked consent is treated as **no consent**.

## 4. Migration plan (small, reviewable steps)

1. **Step 0 (this doc):** owner decisions recorded. No code.
2. **Step 1:** add `EgressGate` + `GuardedProvider` with unit tests, **not wired**.
   No behaviour change; the guard is inert until a provider is wrapped.
3. **Step 2:** wrap the `llm/` provider chain (the routed path) first; prove the
   local providers still work and cloud modes refuse without consent.
4. **Step 3:** route `GeminiEngine`/`SummarizerEngine` through the guard.
5. **Step 4:** wire the legacy `/ai`, `/code`, `/ask`, `/translate`, `/vision`
   handlers to the guarded engines; delete the direct construction in
   `handlers.py`.
6. **Step 5:** default `LOCAL_VAULT`; make any non-local mode require an explicit,
   recorded, scoped consent.
7. **Step 6:** adversarial test suite (below) wired into CI.

Each step is one PR, one zone, one owner, with its own evidence.

## 5. Adversarial test definitions (to be built with the implementation)

- **T1 default-deny:** with `LOCAL_VAULT` (default) and no consent, every listed
  handler refuses and makes **zero** network calls (assert on a transport spy).
- **T2 consent scope:** a consent for "memory" does **not** authorise chat egress.
- **T3 revocation:** a revoked/expired consent refuses on the next call.
- **T4 principal forgery:** a forged `user_id`/`tenant` in the update does not
  widen scope; the gate uses the authenticated principal.
- **T5 no leak in refusal:** the typed refusal and logs contain no user text and
  no secret.
- **T6 cloud-with-consent:** `TRUSTED_CLOUD` + valid scoped consent **allows** and
  records provenance.
- **T7 local providers unaffected:** Ollama/llama.cpp paths keep working with no
  gate friction.

## 6. Owner decisions required (OPEN — nothing below is decided)

1. **Are all LLM egresses forbidden under `LOCAL_VAULT`?** (Recommendation: **yes**.)
2. **What is the consent scope for provider egress?** (e.g. per-provider,
   per-data-kind, per-conversation, TTL.)
3. **Does the current AI-Memory consent remain memory-only?** (Recommendation: **yes** —
   it must not be reused as chat-egress consent.)
4. **How is consent stored and revoked?** (Where the record lives, TTL, revocation path.)
5. **Which component is the final Composition Root owner** of the gate?

Until 1–5 are recorded, this stays design-only and SEC-4 stays `deferred`.

## 7. Explicitly out of scope

- No change to `bot/handlers.py`, `features/ai_chat.py`, or any provider today.
- No new policy class that duplicates `nagar/ibpc/privacy.py`.
- No change to the three privacy modes' names or the `AGENTS.md` authority contract.

---

_This document was created by an AI agent (OpenHands) on behalf of the owner._
