# NAGAR Composition Layer — Threat Model

Scope: `src/nexus_ai_agent/creative/composition/` — the capability
composition compiler, the synchronous media runtime, the FFmpeg/ImageMagick
workers, and the proof-carrying artifact layer.

This is a **v1** threat model. It maps threats to the existing mitigations and
to open risks.

## Threat catalogue

| # | Threat | Where | Mitigation (v1) | Residual risk |
|---|---|---|---|---|
| T1 | Malicious capability spec (forged media types, unbounded resources) | registry input | `CapabilitySpec` is a frozen pydantic model with `extra="forbid"`; resource fields are bounded (`ram_mb` 1..1e6, etc.); compiler sums resources and refuses over-quota graphs | A spec with *valid* but huge resources still passes until quota is hit — caller must set a realistic quota |
| T2 | Type confusion (feeding a video to an image-only op) | graph bindings | `CompositionCompiler._validate_types` checks `MediaType.compatible_with` on every edge before execution | Media-type equality is coarse-grained; pixel-format / sample-rate predicates are not evaluated in v1 |
| T3 | Cycle in composition graph | graph structure | Kahn topological sort; cycle raises `CompositionError` before execution | — |
| T4 | Worker path traversal (param names a file outside scratch) | worker subprocess | `_safe_resolve` refuses any path that escapes the node scratch dir; every subprocess runs with `cwd=node_dir` and `shell=False` | A malicious *worker implementation* (not a param) could still escape — workers are trusted code |
| T5 | Arbitrary command injection via params | worker argv | All worker commands use explicit argument lists (`shell=False`); only integer/float/string params are interpolated; drawtext text is escaped | A new worker that builds shell strings would reintroduce this — code review required |
| T6 | Resource exhaustion (RAM/disk/time) | runtime | Compiler enforces resource quota; each worker has a `timeout`; `max_nodes` caps graph size | Quota is per-composition, not per-host; a hostile caller could run many compositions in parallel — needs a host-level limiter |
| T7 | Network egress from a capability | worker subprocess | `RuntimePolicy.allow_network=False` by default; compiler refuses any composition whose derived spec has `network_required=True` when quota forbids network | FFmpeg itself could be tricked into fetching a remote URL via a crafted input path — input paths are copied into scratch first, so the worker never sees a remote URL |
| T8 | Output tampering (bytes changed after execution) | artifact | `verify_proof` independently re-hashes the output bytes and compares to the recorded `output_hash`; mismatch → `verified=False` | Tampering *before* the hash is recorded is not detected — the hash is recorded immediately after the worker returns |
| T9 | Licence violation (commercial use of NC component) | composition | `LicenseInfo.effective_allow_commercial` is derived per component; runtime refuses `commercial_use=True` if any component is NC | Model licence is self-declared in the spec; a misdeclared model licence is not detected — needs a licence ledger with signed attestations |
| T10 | Copyleft propagation hidden | composition | `_derive_license` sets `copyleft=True` if any component is copyleft; the derived spec carries it | Same as T9 — relies on accurate self-declaration |
| T11 | Non-deterministic output presented as deterministic | composition | `_derive_license`/determinism check: if any node is non-deterministic, the derived spec is `deterministic=False` | A node declared `deterministic=True` but implemented stochastically would not be caught — needs golden-output tests per capability |
| T12 | Secret exfiltration via worker env | worker subprocess | Workers inherit the parent env; no secrets are passed explicitly | The runtime does not strip the environment; a compromised worker could read env vars — needs a clean-env subprocess wrapper |
| T13 | Supply-chain compromise of FFmpeg/ImageMagick | host binaries | Binaries come from the OS package manager; versions are recorded in the artifact | No signature verification of the binaries themselves — host integrity is assumed |
| T14 | Replay of a stale artifact | artifact | Artifact carries `generated_at` and a fresh `output_hash`; replay is only meaningful if the bytes still match | No anti-replay nonce; a downstream consumer must re-verify the hash itself |

## Architectural mitigations (fail-closed by construction)

1. **Compile before execute.** Every graph passes structure/type/resource/
   licence/determinism validation *before* any subprocess is spawned.
2. **No shell.** Every worker command is an explicit argv list with
   `shell=False`.
3. **Scratch-dir isolation.** Each node runs in its own directory; input paths
   are copied in and resolved against that directory.
4. **Independent verification.** The proof-carrying artifact's output hash is
   re-measured by `verify_proof`, not trusted.
5. **Policy gate.** Network and commercial-use decisions are made by
   `RuntimePolicy`, not by the graph or the worker.

## Open risks (not mitigated in v1)

- Host-level resource limiting (cgroups/namespaces) for parallel compositions.
- Clean-environment subprocess execution (strip secrets from worker env).
- Signed licence attestations per capability (self-declaration today).
- Golden-output determinism tests per capability (declarative today).
- Anti-replay nonces in the proof-carrying artifact.
