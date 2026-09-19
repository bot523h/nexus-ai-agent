# LLM providers — multi-provider routing chain (v3.7.0)

Since v3.7.0 the LLM engine is no longer bound to a single provider
(Gemini). Generation is routed through a priority chain of free-tier
providers orchestrated by `litellm.Router`.

## Priority chain

```
1. Ollama     (local, unlimited)   → nexus-ollama
2. Groq       (free tier, fast)    → nexus-groq
3. Gemini     (free tier, current) → nexus-gemini
4. OpenRouter (":free", last resort) → nexus-openrouter
```

- Only providers with credentials/settings configured enter the chain.
  `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`,
  `NEXUS_OLLAMA_MODEL` (+ `NEXUS_OLLAMA_BASE_URL`).
- The chain is a strict ordered fallback: deployment *i* falls back to every
  later deployment (`fallbacks` rules). `num_retries=0` — a failing
  deployment is never retried in place; the chain moves down instead.

## Cooldowns and the anti retry-storm rule

| Deployment | `allowed_fails` | `cooldown_time` | Rationale |
|---|---|---|---|
| nexus-ollama | 2 | 300s | local outages are temporary |
| nexus-groq / nexus-gemini / nexus-openrouter | 1 | 86400s (config: `NEXUS_LLM_CLOUD_COOLDOWN`) | daily-capped free tiers |

litellm places a deployment on cooldown **immediately on a 429**. With
`cooldown_time=86400` a drained daily quota is skipped for ~24h (process
lifetime) instead of being hammered with retries.

## Strict privacy flag

`NEXUS_LLM_STRICT_PRIVACY=true` removes OpenRouter `:free` deployments from
the chain: free OpenRouter endpoints may train on user prompts. Ollama, Groq
and Gemini do not train on prompts and always stay. A paid (non-`:free`)
OpenRouter model also stays, since it is served under standard provider
terms.

## Layering (unchanged contracts)

```
FallbackProvider            ← outer layer (existing, untouched)
  primary:   LiteLLMRoutingProvider   (new, llm/litellm_provider.py)
  fallback:  FakeLLMProvider          (degrade + disclaimer)
```

- `LLMProvider` (`generate`/`embed`), `FallbackProvider`, `GeminiProvider`,
  `FakeLLMProvider`, `LocalLlamaCppProvider` are untouched.
- When the router exhausts every deployment (all failed/cooling down),
  `LiteLLMRoutingProvider` raises `RouterExhaustedError`; its message carries
  the rate-limit keywords the outer `FallbackProvider` matches on, so the
  bot degrades to `FakeLLM` with a disclaimer instead of crashing.
- `embed()` keeps the deterministic 384-dim hash vector (parity with
  `GeminiProvider.embed`) so stored vectors stay compatible; real embedding
  models are deferred to a later phase.
- Legacy path (`nexus run-bot`): with `NEXUS_LLM_ROUTING_ENABLED=false`, or
  when litellm is unavailable / no provider is configured, the pre-3.7.0
  behaviour applies (local GGUF if present, else `FakeLLM`).

## Router wiring reference (llm/litellm_provider.py)

- `model_list`: one entry per configured provider, `model_name` =
  `nexus-<provider>`, `litellm_params.model` prefixed `ollama/`, `groq/`,
  `gemini/`, `openrouter/`.
- Per-deployment `model_info`: `cooldown_time`, `allowed_fails`.
- Router defaults: `routing_strategy="simple-shuffle"`,
  `timeout=NEXUS_LLM_REQUEST_TIMEOUT` (default 60s).

## Known pattern, deferred on purpose

`agents/{gemma,phi,qwen}` are separate classes but all share the **same**
injected `LLMProvider`; their "model identity" is cosmetic — the
personalities come entirely from `PersonalityEngine` prompts (`PersonalityEngine("gemma"|"phi"|"qwen")`,
built on one shared provider instance). Nothing in the routing layer treats
them as different models. Resolving this naming/pattern properly (e.g.
persona-scoped system prompts or per-persona routing hints) is deliberately
deferred to the next phase.
