# 06 — Local-First / Offline AI Research

> **Agent 4 — Independent Research**
> **Date:** 2026-09-22
> **Status:** VERIFIED for mature components, PARTIALLY for WASM/WebGPU maturity, CONFIDENCE H/M/L

---

## 1) Why Local-First in 2026

- Privacy/consent-first (no transcript leaves device without flag), zero marginal cost at idle, offline resilience (air-gapped), regulatory edge (data residency), and cold-start control are primary drivers. Cloud fallback remains necessary for frontier reasoning + large context. The win is **ordered fallback chain** where local is default, cloud is opt-in — not cloud-or-nothing. NEXUS docs state this as Q1. [VERIFIED, HIGH — NEXUS docs/architecture/OVERVIEW.md Q1 + LLM_PROVIDERS.md ordered chain.]

---

## 2) Runtime Stack — What Runs Where

| Layer | Tool | What it is | Local/offline | When to use |
|-------|------|------------|---------------|-------------|
| **Experience / daemon** | **Ollama** | Daemon wrapping llama.cpp, model registry, OpenAI-ish API (`:11434`) | Full | Prototyping, single-user dev, air-gap via tarball transfer |
| | LM Studio | GUI for GGUF browsing/running | Full | Desktop GUI, non-technical |
| **Inference engine** | **llama.cpp + GGUF** | Portable C/C++ transformer inference, 2–8 bit K-quants + imatrix | Full | CPU-only, Apple Silicon (Metal), AMD (ROCm), Intel, Vulkan — universal fallback |
| | **MLX** | Apple array framework, fastest on M1-M4 | Full | Apple-only research, fine-tuning |
| | vLLM / TGI / TensorRT-LLM | Throughput servers (PagedAttention, continuous batching) | Full (but GPU) | Concurrent multi-user serving (Ollama serializes — switch here for production) |
| **Embedded / mobile** | ExecuTorch | PyTorch-native on-device (phone→µC), 1.0 late 2025 | Full | Phone/edge billions deployment |
| | Core ML / ONNX Runtime / MLC LLM | Apple on-device / cross-platform / compile-to-many (WebGPU/WASM) | Full | Cross-platform + browser |
| **Packaging** | GGUF / ONNX / Core ML | Weight formats + metadata | Full | Distribution |

**Maturity timeline [VERIFIED]:** GGUF replaced GGML; Ollama v0.11+ stable; llama.cpp >70k stars; MLX mature; ExecuTorch 1.0 late 2025 shipped to billions. WebGPU in stable browsers; ONNX 1.18+.

**Primary:** github.com/ggerganov/llama.cpp, ollama.ai/docs, ml-explore.github.io/mlx, onnxruntime.ai, pytorch.org/executorch
**Independent:** 1337Skills "LLM Inference at the Edge" 2026, ai-system-design-guide "On-Device and Edge Deployment" runtime stack table, DasRoot 2026 deployment guide

---

## 3) Quantization — Shrinking Models to Fit Local Memory

| Format | Bits | Typical 7B FP16 14GB → | Quality note | Engine |
|--------|------|------------------------|--------------|--------|
| **GGUF Q4_K_M** | 4 | ~4–5 GB + KV cache headroom | Default sweet spot; beyond Q2/Q3 hurts reasoning | llama.cpp / Ollama / LM Studio |
| GGUF Q8_0 | 8 | ~7–8 GB | Higher quality, larger | llama.cpp |
| GGUF Q2_K | 2.x | ~2.5 GB | Severe degradation | llama.cpp |
| AWQ / GPTQ (calibrated) | 4 | ~4 GB | Best NVIDIA-only quality via calibration | vLLM / TensorRT-LLM |
| EXL2 | 2–8 variable | Variable per layer | Good speed on NVIDIA | exl2 |
| MLX 4-bit | 4 | ~4 GB | Best on Apple Silicon | MLX |

**Common pitfalls [VERIFIED via ai-system-design-guide]:**
- Forgetting KV cache when sizing (long context × parallel slots dominates)
- Over-quantizing (Q2/Q3 reasoning loss)
- Treating Ollama as production server (serializes under load → switch to vLLM)
- Assuming NPU TOPS = LLM speed (memory bandwidth bound)

**Air-gapped workflow [VERIFIED]:**
```bash
# Connected host: fetch + store
ollama pull llama3.1:8b      # → ~/.ollama/models
# Transfer via approved media
tar -czf models.tar.gz ~/.ollama/models
# Air-gapped host: restore
tar -xzf models.tar.gz -C ~/ && ollama serve & ollama run llama3.1:8b
```

---

## 4) Model Packaging — How Are Offline Models Shipped?

Five distribution mechanisms observed (often combined):

| Mechanism | How | Where artifact lives | Integrity | Example |
|-----------|-----|---------------------|-----------|---------|
| **Ollama registry** | `ollama pull` | `~/.ollama/models` (GGUF blobs) | Implicit via registry TLS | `ollama run gemma3:1b` (815MB) |
| **Hugging Face hub** | `hf hub download` | Local cache / `models/` | SHA256 + GPG | `llama-3.2-3b.gguf` (2.0GB) |
| **Direct GGUF files** | HTTP(S) fetch + SSRF guard | App-specified cache | SHA256 probe + size cap | Pollinations vs Gemini fallback pattern |
| **ONNX bundle** | `onnxruntime` loads `.onnx` + tokenizer | App bundle / R2 | ONNX hash | Whisper ONNX, vision ONNX |
| **Core ML / ExecuTorch** | Compiled `.mlpackage` / `.pte` | App bundle | Signed | Mobile deployment |
| **NEXUS pack manifest** | Data-only JSON + external model cache | `storage/providers/r2.py` (blob tier) | SHA256 + atomic publish | NEXUS `PACK_RUNTIME`, `r2-storage.md` |

**NEXUS-specific [READ-ONLY]:** `docs/ops/r2-storage.md`, `storage/providers/r2.py` Cloudflare R2 blob tier, `continuum/pack_coverage.py`, `adapters/whisper_local.py` (faster-whisper int8 CPU) + ArgosTranslate offline translation. Local paths are real: `llama-cpp-python>=0.2`, `sentence-transformers`, `sqlite-vec`, `faster-whisper` `[speech]` extra, `argostranslate` `[translate]` extra. All fail closed if missing (typed unavailable adapter).

**Packaging best practices synthesized:**
- SHA256 over (provider, model, prompt, dims, seed) for image lane cache bounded 16 entries / 32 MiB / 1h (NEXUS pattern) — LRU + expiry.
- Atomic `.part` → rename publish prevents half-written model exposure (NEXUS render lane pattern transfers to model cache).
- Capability-gated access: no API key → feature disabled (never fake images/files).

---

## 5) Capability Discovery — How Does a Local System Know What It Can Do?

Three discovery layers for offline agents:

| Layer | Discovery primitive | Disclosure | Example |
|-------|--------------------|-----------|---------|
| **Hardware probe** | Runtime detects CPU/GPU/NPU, VRAM, accelerator (Metal/CUDA/ROCm) | Which quant/engine/model size fits | Ollama GPU detection; `psutil>=5.9` + `llama.cpp` device query; MLX device metadata |
| **Feature gate (extra system)** | `pyproject.toml` `optional-dependencies` (`speech`, `translate`, `pdf`) → import try/except → typed “Unavailability” adapter | Which capabilities are installed | NEXUS `creative/caption/unavailable_adapter.py`, `tests/unit/test_caption_engine_adapters.py` — fail closed with typed error, not silent degrade |
| **Model/capability registry scan** | Filesystem scan of manifests/models at startup | Which packs/models locally available | NEXUS `creative/packs/registry.py`, `adapters/in_process_job_queue.py`, `observability` healthz DB-free |

**Hardware compatibility decision tree (synthesized):**
```
Is Apple Silicon? → MLX 4-bit or llama.cpp Metal + GGUF Q4_K_M
NVIDIA + enough VRAM? → AWQ/EXL2 best quality; fallback GGUF partial GPU offload
NVIDIA limited VRAM? → GGUF with offload layers (e.g., 20/35 layers on GPU)
CPU only? → GGUF Q4_K_M, expect ~5-15 tok/s on modern CPU (7B)
Browser only? → ONNX/WASM (small models) or WebGPU (mid-size)
Phone/µC? → ExecuTorch / Core ML compiled
```

**Maturity [VERIFIED]:** No universal hardware-capability auto-downgrader exists today; teams hand-roll. Optimal auto-select (model size × quant × device) is open gap.

---

## 6) Offline Mode Preservation — How to Stay Offline Even If Code Was Written for Cloud

Principles derived from NEXUS Q1/Q2 (living docs):

| Principle | Mechanism | NEXUS evidence (READ-ONLY) |
|-----------|-----------|----------------------------|
| **No credential = disabled, not degraded** | Missing API key ⇒ path stays closed, returns typed error, no fake response | `NEXUS_IMAGE_GEN_PROVIDER` defaults `pollinations`, Gemini requires `NEXUS_IMAGE_GEN_PAID_TIER=true` + positive cost estimate; merely supplying key never authorizes |
| **Ordered fallback chain** | Try local first → Ollama → Groq → Gemini → OpenRouter; local chain never requires cloud | `src/nexus_ai_agent/llm/` router chain + `docs/architecture/LLM_PROVIDERS.md` |
| **Consent-gated egress** | Tri-state consent + `STRICT_PRIVACY` flag removes cloud provider from chain | `features/ai_memory.py`, `bot/access_guard.py`, `config/settings.py` 78 settings |
| **Liveness is DB-free** | `/healthz` does not touch DB so deploys without DB still report liveness | `api/app.py::healthz` |
| **Migrations idempotent** | Alembic idempotent, checkpoint reconciler heals unknown states | `storage/checkpoint_lifecycle*.py` |
| **In-process queue sidecar** | SQLite `.jobs.sqlite3` durable sidecar avoids Redis/Kafka dependency | `adapters/in_process_job_queue.py` |

**Offline smoke:** `nexus smoke` CLI validates whole core runs with no cloud creds. [VERIFIED]

---

## 7) Fallback Cloud Design — When Local Is Insufficient

**Decision dimensions:**
- Reasoning complexity: local 7B adequate for tool routing/classification, frontier needed for multi-step planning / long context synthesis.
- Context length: local models 8-32k typical; 128k+ frontier only.
- Cost vs latency: local = zero per-token, higher cold-start; cloud = per-token, lower for burst.
- Compliance: strict-privacy flag must override fallback (user data cannot leave).

**Pattern — Tiered fallback (NEXUS-inspired):**
```python
# Pseudocode (not project code) — pattern only
def llm_complete(prompt, privacy=strict):
    if privacy == "strict":   return local_llm(prompt)  # never egress
    try: return local_llm(prompt)
    except (Overloaded, ContextTooLong): pass
    for provider in [Groq, Gemini, OpenRouter]:  # ordered
        try: return provider(prompt)
        except (RateLimited, Unavailable): continue
    raise AllProvidersUnavailable
```
- LiteLLM / OpenRouter proxy pattern standardizes this across providers with OpenAI-compatible surface + retry + cost tracking (see T8 in Stage 1). [VERIFIED]

**Cost event honesty [READ-ONLY OBS]:** NEXUS `image_generation_cost` records once per *successful* HTTP response (including malformed that may still be billable), never on cache hits, with explicit disclaimer "estimates, not invoice" (timeout can bill despite failure, retries may double-bill). This pattern should be reused for LLM fallback metering.

---

## 8) Edge Deployment — Putting It Together

**Reference topologies:**

| Topology | Local stack | Cloud | State | When |
|----------|-------------|-------|-------|------|
| Fully offline workstation / Termux | llama.cpp + Ollama + sqlite-vec + SQLite sidecar | none | SQLite (`data/*.sqlite`), checkpoints on disk | Research, privacy-max, air-gap |
| Always-on polling (single host) | Same | Optional Groq/Gemini via LiteLLM | Same | Simplest operator: Koyeb/Neon/R2 not required |
| Webhook + scale-to-zero (Koyeb) | Fallback chain to Groq/Gemini | Postgres/Neon + R2 blob | Ephemeral disk → durable via Postgres/R2 | Zero-idle cost, cold-start latency |

**FFmpeg/binary allow-list note:** Edge deployment must pin `imageio-ffmpeg` wheel for render lane; allow-list order `NEXUS_FFMPEG_BIN` → PATH → wheel handles binary discovery without shell. [VERIFIED]

---

## 9) Gaps & Unknowns

- **GAP1:** No standardized "model capability card" (VRAM req, quant quality curve, languages supported for ArgosTranslate — Persian coverage incomplete) — each project hand-documents. [UNKNOWN]
- **GAP2:** Automatic VRAM headroom calculation including KV-cache for variable context × concurrency — not standardized; sized by trial. [PARTIALLY VERIFIED]
- **GAP3:** WebGPU/WASM LLM quality vs native for 3-7B models — maturing but not production-equivalent to llama.cpp; no head-to-head benchmark with same model/quant found [UNKNOWN]. 
- **GAP4:** ArgosTranslate Persian quality for formal vs colloquial — anecdotal only, no reproducible eval found [UNKNOWN].

---

## Sources Cited

1. **Primary (NEXUS READ-ONLY):** `docs/architecture/OVERVIEW.md` Q1/Q2 + `LLM_PROVIDERS.md` (ordered fallback chain) + `pyproject.toml` (`llama-cpp-python`, `faster-whisper`, `argostranslate` extras) + `storage/providers/r2.py` + `adapters/whisper_local.py` + `api/app.py::healthz` + `adapters/in_process_job_queue.py`
2. **Primary:** github.com/ggerganov/llama.cpp (GGUF spec, quantization 2-8 bit, K-quants/imatrix, backends) + ollama.ai/docs (registry, OpenAI compat) + ml-explore.github.io/mlx
3. **Primary:** onnxruntime.ai, webgpu.io, pytorch.org/executorch (ExecuTorch 1.0 late 2025)
4. **Independent:** 1337Skills "LLM Inference at Edge" 2026 (build flags, GGUF, Ollama, quantization decision tree, privacy air-gap workflow)
5. **Independent:** ai-system-design-guide "On-Device and Edge Deployment" (runtime stack table, why Ollama ≠ production server, vLLM switch, pitfalls)
6. **Independent:** DasRoot 2026 "Local LLM Deployment with Ollama and llama.cpp" (815MB gemma3:1b, 2.0GB llama3.2:3b, VRAM reduction 14GB→4-5GB)
7. **Independent:** NEXUS `docs/ops/r2-storage.md` + `docs/ops/deployment-koyeb.md` (deployment topologies)
8. **Independent:** MLC LLM / ExecuTorch launch notes (browser/WebGPU compile-to-many)

---

## Deliverable Summary — LOCAL_FIRST_AI_RESEARCH.md Checklist

- [x] ONNX / llama.cpp / Ollama / MLX / WebGPU / WASM / quantization / CPU|GPU|edge surveyed
- [x] Model packaging (6 mechanisms)
- [x] Capability discovery (hardware probe + feature gate + manifest scan)
- [x] Hardware compatibility detection (decision tree)
- [x] Fallback cloud design (tiered fallback + LiteLLM pattern + cost honesty)
- [x] Offline preservation (6 principles)
- [x] Edge topologies (3)
- [x] Gaps/unknowns listed with evidence tags

