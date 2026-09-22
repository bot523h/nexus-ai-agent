# 03 — Capability / Tool Registry Research

> **Agent 4 — Independent Research**
> **Date:** 2026-09-22
> **Status:** VERIFIED where spec-cited, otherwise PARTIALLY VERIFIED, CONFIDENCE H/M/L

---

## Scope

How modern systems define, version, discover, authorize, evolve, and harden tools/capabilities/plugins. Coverage: MCP, OpenAPI, JSON Schema, Function Calling, Tool Registry, Capability Registry, Plugin Systems, Permission Systems, Sandboxing.

---

## 1) Tool Identity — How Is a Tool Named and Addressed?

Three models dominate 2026:

| Model | Identity form | Example | Uniqueness scope | Authority |
|-------|---------------|---------|------------------|-----------|
| **MCP** | `server_id + tool name` (+ version in registry) | `io.github.user/filesystem::read_file` | Registry namespace (`io.github.*` via OAuth, `com.company/*` via DNS) | Registry + server |
| **OpenAPI** | `operationId` (+ path+method fallback) | `getWeather` (`GET /weather`) | Spec file | Spec author |
| **Function Calling (OpenAI/Anthropic)** | `function.name` | `search` | Per-request `tools:[]` array | Caller |
| **NEXUS / Command-Bus** | `pack_id + operation_id` (e.g., `edit.crop`, `motion.pan`) | `slideshow.stitch` | `creative/packs/*/manifest.json` registry | Pack manifest |
| **Registry (MCP Registry)** | `server.json: {name, version, packages[where to download]}` | `npm:weather-mcp@1.2.0` | NPM/PyPI/Docker/ GH Releases metaregistry | NPM/PyPI trust + registry denylist |

**Finding [VERIFIED, HIGH]:** Identity is never bare string in prod systems; it is namespaced + versioned, bound to provenance (where code lives). MCP Registry formalizes this: registry stores *metadata* only, package stores code — two-level provenance. Contradicted pattern = bare tool name without namespace (collision + spoof).

**Primary:** MCP Registry Spec https://modelcontextprotocol.info/tools/registry/, MCP spec 2025-06-18 tools/list, NEXUS `creative/packs/manifest.py` (manifest is pack identity + operations list)
**Independent:** Descope MCP guide (NxM problem), DigitalApplied 2026 (MCP transports/primitives)

---

## 2) Versioning — How Are Tool Schemas Versioned?

| Strategy | Mechanism | Break detection | Example |
|----------|-----------|----------------|---------|
| **SemVer on Tool/Schema** | `tool_version` field in manifest/registry, bump major on breaking inputSchema | CI diff of JSON Schema | MCP Registry `server.json` + `packages` versioning; NEXUS `manifest.json` + `pack.manifest.json` version |
| **JSON Schema dialect lock** | Pin dialect (`2020-12` since MCP 2025-11-25) | Validator rejects unknown keywords | MCP spec 2025-11-25 "JSON Schema 2020-12 as default dialect" |
| **OutputSchema versioning** | `outputSchema` (MCP 2025-06-18) separate from input | Can evolve input without breaking output if compatibility matrix checked | MCP `outputSchema` field |
| **Registry immutability** | Published server version immutable; new version = new package publish | Registry ETL will not overwrite | MCP Registry: `publish` endpoint creates new version |

**Anti-pattern [VERIFIED, HIGH]:** In-place schema mutation without version bump breaks LLM tool-calling — models cache schemas. Cache hints (`ttlMs`, `cacheScope`) in MCP 2026-07-28 make this explicit; clients cache tool catalogs. Therefore schema evolution must be additive-first or new version.

**Mitigation matrix:**

- Additive non-breaking: add optional property → minor bump → safe
- Required field addition → major bump
- Type narrowing → major bump
- Description change only → patch (model behavior may shift — see "tool description as attack surface")

---

## 3) Permission — How Is Tool Invocation Authorized?

**2026 state-of-art is deny-by-default + per-tool capability gating + user consent:**

| Layer | Where enforced | Granularity | Example |
|-------|---------------|-------------|---------|
| **Host access guard** | Before handler dispatch (group -1) | Per user/command | NEXUS `bot/access_guard.py` allow-list; Telegram `get_chat_member` force-join gate |
| **Bus permission ladder** | Inside command bus before handler | Per operation level | NEXUS `PermissionLevel` A–D (A=inspect, B=edit, C=motion/audio, D=delivery) |
| **MCP authorization** | OAuth 2.1 + CIMD (2025-11-25), EMA extension | Per server / per tool scope | MCP spec: Resource Indicators (RFC 8707), incremental scopes via WWW-Authenticate |
| **Tool annotations** | Advisory, but untrusted unless from trusted server | `readOnly`, `destructive`, `idempotent`, `openWorld` | MCP `annotations` field — client SHOULD treat as untrusted |
| **Sandbox / allow-list** | Process boundary | Binary / path / command | NEXUS FFmpeg lane: allow-listed binary order, no shell, argv typed, `.part` staging |
| **Consent gates** | Feature flags | Per data-egress capability | NEXUS `strict-privacy`, `slideshow_allow_image_upload`, `ai_memory_consent` tri-state |

**Finding [VERIFIED, HIGH]:** Annotation ≠ enforcement. MCP spec explicitly: "descriptions of tool behavior such as annotations should be considered untrusted, unless obtained from a trusted server." Host must enforce.

**Principle:** Least privilege + single-owner guard test (NEXUS board task-124: exactly one wiring path per engine). Duplicate wiring = elevation vulnerability (audit P0-8).

**Primary:** MCP spec Security & Trust & Safety sections (user consent, tool safety), NEXUS `docs/architecture/SECURITY.md` B2/B5/B7 boundaries + FFmpeg boundary, `bot/access_guard.py`, `continuous/pack_coverage.py`
**Independent:** OWASP LLM06 Excessive Agency + ASI02 Tool Misuse, MITRE ATLAS

---

## 4) Schema Evolution — How Is Evolution Handled Without Breaking Agents?

**Mechanisms observed:**

1. **Additive-first rule:** New optional fields with defaults; never rename/remove in place.
2. **Schema registry + compatibility check:** CI compares `inputSchema` diff, fails on breaking change without major bump (Confluent-style for Kafka, analogous for tool registry).
3. **Version negotiation:** MCP initialize handshake (pre-2026) / per-request `MCP-Protocol-Version` header (2026-07-28). Client declares wanted version, server responds with supported.
4. **Deprecation lifecycle:** MCP 2026-07-28 formalized deprecation policy (what DCR → CIMD transition demonstrates). Mark deprecated → warn → remove.
5. **Pack purity gate:** NEXUS `tests/architecture/test_pack_manifest_is_data_only.py` — manifest must be JSON-data only, no executable key at any depth; external pack cannot register unknown operations; activation refuses pending capabilities. This prevents schema smuggling via executable payload.

**Failure mode [VERIFIED]:** LLMs embed tool schemas in system prompt; a schema change mid-conversation (e.g., new required param) causes tool call validation failure → LLM hallucinates args. Solution: atomic deploy of model + registry + validation, or cache-bust via `listChanged` notification.

**Primary:** MCP spec 2026-07-28 (version per request, deprecation policy), NEXUS `creative/packs/verify.py` + architecture tests
**Independent:** OpenAPI evolution best practices (Stoplight, AsyncAPI), Kafka Schema Registry docs (confluent)

---

## 5) Discovery — How Does an Agent Find Tools?

| Mode | Latency | Freshness | Example |
|------|---------|-----------|---------|
| **Static bind** | Zero | Stale until deploy | OpenAI `tools: [...]` per request |
| **File-based discovery** | Low | On pull | MCP "/tools/*.json" directory (2025-11 spec RC) |
| **List call** | Medium | Real-time | MCP `tools/list` JSON-RPC with pagination + `nextCursor` + `ttlMs` cache hints |
| **Registry ETL** | Low (cached) | Eventual | MCP Registry → sub-registries → client cache |
| **Registry `listChanged`** | Push | Immediate | MCP `notifications/tools/list_changed` |
| **Local manifest scan** | Low | On startup | NEXUS `creative/packs/registry.py` filesystem scan |

**Stateless 2026 implication [VERIFIED]:** Since 2026-07-28, any MCP request can land on any instance behind round-robin LB (no handshake/session). Therefore discovery must be cacheable + deterministic order (spec adds deterministic order + cache hints). Clients *should* cache tool catalogs; prompt caches stay stable across reconnects.

**Trade-off:** Discovery freshness vs LLM prompt stability. Cache hits save tokens but delay new tool availability until TTL expiry.

**Primary:** MCP spec tools/list pagination, cache hints (SEP-2549), notifications/tools/list_changed
**Independent:** DigitalApplied 2026 (protocol details), Hidekazu Konishi version timeline (spec evolution)

---

## 6) Tool Failure — How Is Failure Communicated and Recovered?

**Two layers:**

**a) Transport/protocol layer:**
- MCP JSON-RPC error object + `isError` flag on result.
- MCP sampling/elicitation errors typed.

**b) Capability/design layer:**

| Pattern | Description | When used |
|---------|-------------|-----------|
| **Typed closed failure vocabulary** | Finite enum of error codes, no free-form LLM string | NEXUS image lane: transport error vs 4xx vs invalid image vs safety refusal (only transport/5xx retryable) |
| **Bounded retry with backoff** | Jittered exponential, capped `Retry-After` (NEXUS 8s), max 3 attempts | Transient failures |
| **Shared result serialization** | Identical concurrent requests share result (NEXUS per-adapter serialization) | Dedupe/cost saving |
| **Result validation before LLM** | Client validates outputSchema before passing to LLM | MCP spec: "Clients SHOULD validate tool results before passing to LLM" |
| **Cache negative avoidance** | Failed/cancelled never cached | NEXUS adapter cost events: cache hits emit no cost, failures not cached |
| **Timeout per boundary** | One timeout per media process / tool call | FFmpeg lane, MCP client `implement timeouts for tool calls` |

**Anti-pattern:** Unlimited retries on 4xx (client error) → wastes quota & cost. NEXUS correctly: other 4xx, redirects, invalid images, safety refusals are NOT retried.

**Primary:** MCP spec Tools → Security Considerations (validate results, timeouts, log usage), MCP spec 2025-11-25 sampling, NEXUS `docs/DECISION_LOG` image lane retries
**Independent:** Temporal docs (activity retry policies with timers + compensation), Markaicode Celery vs Temporal (retry comparison)

---

## 7) Malicious Tool — How Is a Malicious Plugin Contained?

Threat taxonomy mapped to controls:

| Attack | Surface | Control | Residual risk |
|--------|---------|---------|---------------|
| **Tool description injection** | LLM reads tool description as instruction | Treat descriptions/annotations untrusted; MCP spec mandates | LLM may still be influenced — need input validation layer |
| **Malicious package** | NPM/PyPI package with hidden exec | Registry metaregistry + existing package registry security + denylist moderation queue | Supply chain remains; need AI-BOM audit |
| **Capability smuggling** | Manifest declares executable code or unknown op | Data-only manifest gate + unknown-operation refusal (NEXUS pack boundary tests) | 0 if gate is CI-enforced |
| **Permission escalation via tool chain** | Tool A returns output that tricks agent to call privileged Tool B | Bus permission ladder + per-command authorization + least-privilege wiring | Multi-step chain still needs human-approval gate for privileged ops |
| **Data exfil via tool args** | Tool exfiltrates via args to external URL | SSRF guard, allow-listed providers, show inputs to user before call (MCP SHOULD), consent gates | LLM may encode data in innocuous arg — need output redaction |
| **Resource exhaustion** | Tool loops / unbounded consumption | Rate limiting, single timeout per tool, cost event per successful HTTP | Streaming abuse still needs byte cap |

**Registry moderation [VERIFIED, PARTIALLY]:** MCP Registry is community-owned, moderated via issue-based denylist for spam/malicious/impersonation. Not yet automated scanning per spec; sub-registries are expected to add ratings/security scanning. Implication: registry ≠ security guarantee; host must still validate.

**Sandboxing options:**
- **In-process (weak):** Python sandbox — bypassable, not recommended for untrusted tool.
- **Process isolation:** Separate process with allow-listed binary (NEXUS FFmpeg), no shell.
- **Container/microVM:** gVisor/Firecracker per tool invocation.
- **WASM:** MCP 2026 roadmap mentions native WASM extension interface (delta updates, streaming) — future.

**Finding [VERIFIED, HIGH]:** Sandboxing is not a replacement for permission model; it is defense in depth. Most 2026 frameworks lack native sandbox — they delegate to host (Cordum 2026: "No native policy gate" for LangGraph/CrewAI/AutoGen/LangChain).

**Primary:** MCP spec Security & Trust & Safety + Tool Safety + Registry moderation, NEXUS SECURITY.md B7 + T11 supply chain (T11), `safe_paths.py`
**Independent:** OWASP LLM03 Supply Chain + ASI04 Agentic Supply Chain, NIST AI 100-2 E2025 (agent supply chain), CSA MAESTRO framework

---

## 8) OpenAPI vs MCP vs Function Calling — Converged View

| Dimension | OpenAPI | MCP (2026-07-28) | OpenAI Function Calling | NEXUS Capability Bus |
|-----------|---------|----------------|------------------------|----------------------|
| Interface | REST contract | JSON-RPC tools/resources/prompts | JSON Schema functions | Typed command envelopes |
| Discovery | Spec fetch | Registry / tools/list / file-based | Per-request inline | Manifest scan / registry |
| Versioning | SemVer spec | SemVer + protocol version per request | No built-in | Pack manifest version |
| Auth | OAuth / API key / per-op | OAuth 2.1 + CIMD + EMA | API key | Bus ladder A-D + consent flag |
| Validation | Client + server | Both + client SHOULD validate results | Client-side | Bus validation before handler |
| Extensibility | `x-` extensions | Formal extensions framework (Tasks, MCP Apps) | Tool choice param | Manifest data-only, no `x-` exec |
| Trust model | Spec author trusted | Server trusted; annotations untrusted | Provider trusted | Pack author + CI gate trusted |

**Trend [VERIFIED, HIGH]:** 2026 convergence = JSON Schema everywhere. MCP 2025-11-25 pins JSON Schema 2020-12; OpenAPI 3.1 uses JSON Schema; OpenAI functions use JSON Schema. Therefore tooling can unify on JSON Schema validators.

---

## CAPABILITY REGISTRY DESIGN REPORT — Recommendations for Future Architecture

> **Disclaimer per instruction: technical & documented, not opinion. Each recommendation cites source & trade-off.**

### R1 — Identity: Namespace + Version + Provenance Triangle [VERIFIED]

Use `namespace/tool@v` tuple where namespace is validated (GitHub OAuth or DNS) and provenance is separate package reference (npm/PyPI/Docker/GH Release). Store metadata (registry) separate from code (package registry) as MCP Registry does. Do not allow bare names.

### R2 — Schema: Pin Dialect, Require outputSchema, CI Diff Gate [VERIFIED]

Pin one JSON Schema dialect (2020-12). Require both `inputSchema` and `outputSchema` (adopt MCP 2025-06-18 addition). Add CI job that diffs schemas: breaking → require major bump; additive optional → minor allowed. Provide deterministic ordering for `tools/list`.

### R3 — Permission: Bus-Enforced Ladder, Not Annotation [VERIFIED]

Permission must be enforced in bus before handler, not inferred from tool description. Use at least 3 levels (read / mutate / destructive) as MCP annotations hint, but enforce server-side. Single-owner wiring test + deny-by-default.

### R4 — Discovery: Cacheable + ListChanged [VERIFIED]

Support `ttlMs` + `cacheScope` + `notifications/tools/list_changed`. Default cacheable with 5-15 min TTL; bypass on error path. Stateless LB-compatible (per-request version header).

### R5 — Sandboxing: Process-Isolated for Untrusted, In-Process Only for Pure [PARTIALLY VERIFIED, MEDIUM]

Pure transforms (crop, format) can be in-process (stdlib+pydantic). Anything touching network, filesystem outside sandbox, or binary execution must be process/container isolated with allow-list, no shell, staging + atomic publish, single timeout.

### R6 — Failure Vocabulary: Closed Enum + Bounded Retry [VERIFIED]

Publish closed error codes. Retry only transport/5xx + 429 with `Retry-After` cap. Never cache failures. Validate outputs before LLM.

### R7 — Evolution: Atomic Deploy of Model Prompt + Registry + Validator [PARTIALLY VERIFIED]

Adopt MCP deprecation policy pattern (deprecated → warning → removal). Treat LLM prompt + tool catalog as atomic deploy unit to avoid mid-conversation schema mismatch.

---

## Gaps & Unknowns

- **GAP1:** No industry benchmark for registry lookup latency at scale (>1M tools) — UNKNOWN.
- **GAP2:** WASM extension interface for MCP is roadmap-only (2026-07-28 future highlights) — UNVERIFIED whether will ship as spec.
- **GAP3:** Supply-chain scanning for MCP Registry not yet mandatory — PARTIALLY VERIFIED (community denylist exists, automated scanning deferred to sub-registries).
- **GAP4:** Optimal TTL for tool catalog cache (prompt cache stability vs freshness) — no empirical study found [UNKNOWN].

---

## Sources Cited (Stage 3)

1. **Primary:** https://modelcontextprotocol.io/specification/2025-11-25 + 2025-06-18/server/tools (tools, list, pagination, annotations, security)
2. **Primary:** https://blog.modelcontextprotocol.io/posts/2026-07-28/ (stateless core, cache hints, MRTR, extensions framework, authorization hardening, deprecation)
3. **Primary:** https://modelcontextprotocol.info/tools/registry/ + https://blog.modelcontextprotocol.io/posts/2025-09-08-mcp-registry-preview/ (metaregistry, deployment methods, moderation)
4. **Primary:** Hidekazu Konishi MCP spec version timeline (5 versions, negotiation model, adoption milestones) — cross-validates above
5. **Primary:** NEXUS `creative/packs/manifest.py`, `creative/packs/registry.py`, `creative/packs/verify.py`, `creative/studio/bus.py` + docs/architecture/CREATIVE_STUDIO.md + docs/architecture/SECURITY.md T11 (READ-ONLY)
6. **Independent:** Descope MCP guide (NxM, isolation) + DigitalApplied 2026 MCP adoption stats (platform support)
7. **Independent:** OpenAPI spec evolution / AsyncAPI (schema registry analogy) — for cross-protocol comparison
8. **Independent:** Cordum 2026 (no native policy gate across frameworks) — for permission gap
9. **Independent:** OWASP LLM03/AG04 supply chain + NIST AI 100-2 E2025 agent supply chain (for malicious tool taxonomy)

