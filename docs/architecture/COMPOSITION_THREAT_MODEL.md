# COMPOSITION THREAT MODEL — the NAGAR capability composition runtime

Living view. Authority for the boundary: [`adr/0014-creative-capability-composition.md`](adr/0014-creative-capability-composition.md).
Runtime contract: [`COMPOSITION_RUNTIME.md`](COMPOSITION_RUNTIME.md).

The method is **STRIDE** over the composition runtime's trust boundaries, and
every mitigation is listed with the **symbol that enforces it** and the **test
that proves it**. A control without a test is a claim; a test with a mock is a
mock. Controls that the operating system refuses are recorded as `BLOCKED`,
with the reason.

---

## 1. Assets, actors and trust boundaries

| Asset | Why it matters |
|---|---|
| the produced media bytes | the deliverable |
| the capability registry | the authority over *what can run* |
| the actor's project grant | the authority over *who may run it* |
| the durable queue row | an untrusted structure read by a worker in another process |
| the workspace | the only place a composition may write |
| the worker process's environment | may carry secrets |
| the licence facts | decide whether the output may be shipped |

| Actor | Trust level |
|---|---|
| the surface (a bot / CLI / API) | untrusted input, canonical schema validation |
| the queue row | untrusted; re-validated by the worker |
| the plan | untrusted; re-compiled against the live registry by the worker |
| the staged input file | untrusted; digest re-measured by the worker |
| the worker process | trusted to enforce the controls below |
| FFmpeg | an allow-listed binary, invoked with a fixed argv and a scrubbed environment |

**Trust boundaries crossed:** (1) surface → queue; (2) queue row → worker
(different processes, durable storage in between); (3) plan → compiler; (4)
worker → FFmpeg subprocess; (5) produced bytes → verifier (a different code
path from the producer).

---

## 2. Threats and the controls that are actually enforced

### T1 — Spoofing: a forged plan names a capability the registry does not have

* **Control:** the compiler resolves every node against the **live**
  `build_runtime_registry()`; there is no capability table in the composition
  package.
* **Enforced by:** `creative/composition/compiler.py::CompositionCompiler._resolve`
* **Proved by:** `tests/unit/test_composition_compiler.py::test_unknown_capability_is_refused`,
  `tests/architecture/test_composition_boundary.py::test_composition_package_has_no_capability_table`
* **Status:** `VERIFIED`

### T2 — Spoofing: a plan is replayed against a capability that moved

* **Control:** the plan pins `capability_id`, `capability_version`,
  `operation_schema_version` and the registry fingerprint; the worker
  **re-compiles** the plan against the live registry before executing it.
* **Enforced by:** `CompositionCompiler._resolve`, `CompositionCompiler._check_registry_pin`,
  `creative/composition/job.py::composition_render_job`
* **Proved by:** `tests/unit/test_composition_compiler.py::test_capability_version_drift_is_refused`,
  `…::test_schema_version_drift_is_refused`, `…::test_registry_drift_is_refused`
* **Status:** `VERIFIED`

### T3 — Tampering: the staged input changed between admission and execution

* **Control:** the worker re-measures the digest of every staged input and
  refuses a mismatch; the source is also refused when it is a symlink or
  outside the workspace.
* **Enforced by:** `creative/composition/job.py::guarded_input`
* **Proved by:** `tests/unit/test_composition_security.py::test_a_changed_staged_input_is_refused_before_any_media_work`,
  `tests/integration/test_composition_canonical_e2e.py::test_a_changed_staged_input_is_refused_before_any_media_work`
* **Status:** `VERIFIED`

### T4 — Tampering: the artifact is modified after it was produced

* **Control:** the queue's registered verifier re-measures the digest, and the
  **existing** passport authority re-measures it again when the passport is
  built.
* **Enforced by:** `creative/composition/verifier.py::composition_verifier`,
  `jobs/verification.py::verify_artifact`, `provenance/passport.py::PassportBuilder`
* **Proved by:** `tests/unit/test_composition_verifier.py::test_a_tampered_artifact_is_refused`,
  `tests/integration/test_composition_canonical_e2e.py::test_a_tampered_artifact_flips_the_passport_to_compromised`
* **Status:** `VERIFIED`

### T5 — Tampering: a correctly-hashed artifact carries the wrong content

* **Control:** the semantic pass re-decodes the media with an **independent**
  measurement path — Pillow for a still (a per-channel pixel check for
  grayscale), a full `ffmpeg -f null -` decode with a decoded-frame count for a
  clip. Re-hashing is explicitly *not* semantic verification.
* **Enforced by:** `creative/composition/proof.py::verify_image_semantics`,
  `…::verify_video_semantics`
* **Proved by:** `tests/unit/test_composition_proof.py::test_image_semantics_refuse_a_coloured_image_claiming_grayscale`,
  `…::test_video_semantics_refuse_a_truncated_clip`
* **Status:** `VERIFIED`

### T6 — Elevation of privilege: the composition authorizes itself

* **Control:** the composition owns **no** authorization. Every node is
  dispatched as a canonical `TypedCommand` on the canonical `CommandBus`, which
  runs the actor grant, the registry check, the pack lifecycle gate and the
  A/B/C/D ladder. The **same** gates are additionally run *before* any media
  work: `_authorize_node` calls `CommandBus.check_authorization`, which executes
  the very helpers the dispatch executes (`_spec_locked`, `_authorize_locked`)
  and stops before any reservation, reference resolution or handler. The
  executor therefore constructs no gate of its own — the pack gate is still
  invoked from `bus.py` **only**, and the architecture ratchet
  `test_lifecycle_gate_boundary.py::test_only_the_bus_invokes_the_pack_lifecycle_gate`
  still passes unchanged. One authority, consulted twice.
* **Enforced by:** `creative/studio/bus.py::CommandBus.check_authorization`
  → `…::_authorize_locked`, called by
  `creative/composition/executor.py::CompositionExecutor._authorize_node`
* **Proved by:** `tests/architecture/test_composition_boundary.py::test_composition_package_owns_no_parallel_runtime`,
  `tests/architecture/test_lifecycle_gate_boundary.py::test_only_the_bus_invokes_the_pack_lifecycle_gate`,
  `tests/unit/test_composition_security.py::test_a_denied_node_produces_no_media`,
  `…::test_the_bus_preflight_enforces_the_actor_grant_and_reserves_nothing`,
  `…::test_the_bus_preflight_accepts_what_the_dispatch_would_authorize`
* **Status:** `VERIFIED`

### T7 — Elevation of privilege: an experimental or unavailable capability runs

* **Control:** the pack lifecycle gate (`PACK_LIFECYCLE`) is fail-closed for an
  unknown pack, and `allow_experimental` is a **server-side** decision that is
  never read from a payload.
* **Enforced by:** `creative/studio/lifecycle.py::check_required_packs`
* **Proved by:** `tests/architecture/test_lifecycle_gate_boundary.py`
* **Status:** `VERIFIED`

### T8 — Elevation of privilege: a level-C operation runs without confirmation

* **Control:** `media.image_to_video` is `PermissionLevel.CONFIRMATION`. The
  worker passes the requester's own `confirmed` flag — a **required** field of
  `CompositionJobPayload` that defaults to `False` — and never fabricates it.
  An unconfirmed row fails the pre-flight before any media work.
* **Enforced by:** `creative/studio/bus.py::CommandBus.check_authorization`
  → `creative/studio/capabilities.py::check_permission(confirmed=command.confirmed)`,
  called pre-encode by `creative/composition/executor.py::CompositionExecutor._authorize_node`
* **Proved by:** `tests/unit/test_composition_security.py::test_a_level_c_node_needs_the_requesters_confirmation`
* **Status:** `VERIFIED`

### T8b — Spoofing: a queue row self-grants an anonymous service identity

* **Control:** `CompositionJobPayload.actor` is **required** (`ActorClaim`:
  `kind`, `actor_id`). The worker binds the grant to that principal; an
  anonymous row is refused at validation, so no row can execute under an
  invented service identity.
* **Enforced by:** `creative/composition/job.py::CompositionJobPayload`,
  `…::composition_render_job`
* **Proved by:** `tests/unit/test_composition_security.py::test_a_row_without_an_actor_is_refused`
* **Status:** `VERIFIED`

### T9 — Information disclosure: the encoder subprocess inherits a secret

* **Control:** the encoder runs with an **environment allow-list** only
  (`PATH`, `HOME`, `TMPDIR`, `TMP`, `TEMP`, `LANG`, `LC_ALL`, `LC_CTYPE`,
  `TZ`, `SOURCE_DATE_EPOCH`, `NEXUS_FFMPEG_BIN`). `LD_LIBRARY_PATH` is
  deliberately excluded: it is a code-loading vector.
* **Enforced by:** `creative/slideshow/ffmpeg.py::encoder_env`, applied in `_run`
* **Proved by:** `tests/unit/test_composition_security.py::test_the_encoder_environment_is_an_allow_list`,
  `…::test_the_encoder_subprocess_actually_receives_the_scrubbed_environment`
* **Status:** `VERIFIED`

### T10 — Denial of service: a runaway encoder outlives its deadline

* **Control:** every media process runs with a wall-clock timeout; the child
  is started in its **own session** so the timeout kills the whole **process
  group** (SIGTERM, then SIGKILL), not just the direct child.
* **Enforced by:** `creative/slideshow/ffmpeg.py::_run`, `…::_kill_process_group`
* **Proved by:** `tests/unit/test_composition_security.py::test_a_timeout_kills_the_process_group`,
  `…::test_the_encoder_child_runs_in_its_own_session`,
  `…::test_the_kill_helper_escalates_to_sigkill`
* **Status:** `VERIFIED`

### T11 — Denial of service: a huge input or output exhausts the disk

* **Control:** an input above the ceiling is refused **before** a process
  starts; an output above the ceiling is **deleted** and the node fails.
* **Enforced by:** `creative/composition/engine.py::enforce_input_limit`,
  `…::enforce_output_limit`
* **Proved by:** `tests/unit/test_composition_security.py::test_an_oversize_output_is_deleted_and_refused`,
  `…::test_an_oversize_input_is_refused_before_any_process`
* **Status:** `VERIFIED`

### T12 — Tampering: a path traversal or a symlink escape writes outside the workspace

* **Control:** every output path is resolved and required to stay under the
  node's scratch directory; a symlinked source is refused by `lstat`; the
  workspace itself is refused when it is a symlink or outside the canonical
  creative root. A **node id** is a *name*, never a path: it is constrained by
  `NODE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"` at the model, re-validated when
  the per-node directory is derived, and the resolved directory is required to
  stay inside the resolved workspace before `mkdir`.
* **Enforced by:** `creative/composition/contracts.py::PlanNode.node_id`,
  `creative/composition/executor.py::_node_directory`,
  `creative/composition/engine.py::contained_path`,
  `…::_refuse_symlink`, `creative/composition/job.py::trusted_workspace`
* **Proved by:** `tests/unit/test_composition_security.py::test_a_path_traversal_is_refused`,
  `…::test_a_symlink_escape_is_refused`, `…::test_a_symlinked_workspace_is_refused`,
  `…::test_a_node_id_that_is_a_path_is_refused_by_the_model`,
  `…::test_the_node_directory_is_verified_to_stay_inside_the_workspace`
* **Status:** `VERIFIED`

### T13 — Information disclosure: one node reads another's intermediate

* **Control:** every node gets its **own** scratch directory (`mkdtemp`), and
  the finished artifact is published into the node directory with an atomic
  rename.
* **Enforced by:** `creative/composition/executor.py::CompositionExecutor.execute`
* **Proved by:** `tests/unit/test_composition_runtime.py::test_each_node_gets_its_own_scratch_directory`
* **Status:** `VERIFIED`

### T14 — Repudiation: the composition cannot say what produced its artifact

* **Control:** the plan fingerprint, the registry fingerprint, the per-node
  receipts (capability, version, parameters, input/output hashes, state
  revision, state hash, derived asset id) and the measured tool versions are
  all in the durable result; the **existing** passport authority projects them.
* **Enforced by:** `creative/composition/proof.py::ProofCarryingArtifact`,
  `provenance/passport.py::PassportBuilder`
* **Proved by:** `tests/integration/test_composition_canonical_e2e.py::test_image_to_video_composition_runs_through_the_canonical_chain`
* **Status:** `VERIFIED`

### T15 — Information disclosure: the composition talks to the network

* **Control:** the pack manifest declares `network_policy.runtime_network =
  false` and `upload_media = false`; no builtin media capability opens a
  socket; the encoder's environment carries no proxy variables.
* **Enforced by:** `src/nexus_ai_agent/creative/packs/media/pack.manifest.json`
* **Proved by:** `tests/unit/test_composition_security.py::test_the_pack_manifest_forbids_network_use`
* **Status:** `VERIFIED` for the declared policy; see §3 for the *enforcement*
  limit.

### T16 — Repudiation: an unknown licence is presented as "commercially approved"

* **Control:** `LicenseLedger.effective_allow_commercial` is `False` unless
  every record is permitted **and** verified; a category with no record is
  reported by `unknown_categories` and never assumed.
* **Enforced by:** `creative/composition/contracts.py::LicenseLedger`
* **Proved by:** `tests/unit/test_composition_contracts.py::test_a_permitted_but_unverified_licence_is_not_commercially_approved`,
  `…::test_an_unknown_category_stays_unknown`
* **Status:** `VERIFIED`

---

### T17 — Denial of service: the declared deadline is shorter than the enforced one

* **Control:** the compiler publishes a per-node `ResourceRequirements.timeout_seconds`
  and the executor **holds the encoder to exactly that number**
  (`_node_timeout` → `MediaEngine.realize(timeout_seconds=…)`), so the published
  estimate is the enforced deadline and not an optimistic label.
* **Enforced by:** `creative/composition/executor.py::CompositionExecutor._node_timeout`,
  `creative/composition/engine.py::MediaEngine.realize`
* **Proved by:** `tests/unit/test_composition_runtime.py::test_the_declared_timeout_is_the_enforced_timeout`
* **Status:** `VERIFIED`

### T18 — Tampering: a binding smuggles a second source or an unknown key

* **Control:** a binding is a **closed** shape: unknown keys are refused, the
  two source kinds are mutually exclusive, and an empty external name is
  refused. Nothing is silently discarded.
* **Enforced by:** `creative/composition/job.py::_bindings_from`
* **Proved by:** `tests/unit/test_composition_security.py::test_a_binding_that_names_two_sources_is_refused`,
  `…::test_a_binding_with_an_unknown_key_is_refused`
* **Status:** `VERIFIED`

## 3. The controls the operating system refused — `BLOCKED`

These are **not** claims of security; they are recorded refusals.

| Control | Status | Why it is blocked | What is enforced instead |
|---|---|---|---|
| Linux namespaces (`CLONE_NEWUSER`/`NEWNS`) | `BLOCKED` | the worker runs as an unprivileged user in a container without `CAP_SYS_ADMIN`; `unshare` is denied | the allow-listed binary, the scrubbed environment, the path containment, the per-node scratch directory and the workspace root |
| cgroup v2 resource quotas | `BLOCKED` | no cgroup delegation is available to the process | the wall-clock timeout with a process-group kill, the input/output size ceilings, the one-node-at-a-time concurrency |
| seccomp filter | `BLOCKED` | `prctl(PR_SET_NO_NEW_PRIVS)` + `seccomp` requires privileges the container denies | the fixed argv (no user-controlled flags reach the binary), the allow-list, the env scrub |
| a separate OS user per job | `BLOCKED` | the container has one unprivileged user and no `setuid` policy | the per-node scratch directory and the workspace containment |
| network egress control (egress firewall / netns) | `BLOCKED` | no `NET_ADMIN` and no egress policy in this environment | the manifest's `network_network: false` declaration, the absence of any socket in the media path, the env scrub that removes proxy variables |

**Honest limit:** the "no network" property of a builtin media capability is a
**declaration plus the absence of code that opens a socket**, not an
OS-enforced egress fence. A capability pack that *did* want the network would
need an OS-level control this environment cannot provide; such a pack is out of
scope for `nexus.media.transform`.

---

## 4. The fail-closed matrix before dispatch

Every row is a refusal **before** a media process is started.

| Condition | Where it is refused | Failure code |
|---|---|---|
| unknown capability | the compiler (live registry) | `unknown_operation` |
| untrusted / unavailable capability | the canonical pack lifecycle gate | `PackRequirementError` |
| missing permission | the canonical A/B/C/D ladder | `ExecutionPolicyError` |
| untrusted pack | the canonical manifest + trust root | `PackManifestError` |
| invalid schema | the canonical input model (bus) and the compiler's derived model | `invalid_parameters` |
| missing required input | the compiler (binding) and the bus (schema) | `missing_binding` |
| incompatible licence | the ledger (a plan that demands commercial use without a verified licence is refused) | `policy_refused` |
| exceeded quota | the output ceiling / the wall-clock timeout | `MediaEngineError` → `execution_failed` |
| forbidden network | the manifest policy | `PackManifestError` |
| invalid artifact reference | the canonical claim + the integrity contract | `success_without_artifact_claim` / `sha256_mismatch` |

---

## 5. Residuals

| Residual | Severity | Mitigation |
|---|---|---|
| the composition worker trusts the workspace path it is given until it re-validates it | low | the worker re-validates the root **before** canonicalizing the workspace (`trusted_creative_root` ordering) and refuses a symlinked workspace node |
| the per-node scratch directories are not removed by the composition | low | they live inside the job workspace, which the queue's workspace cleanup owns |
| the licence ledger covers the tool, not the input asset | medium | the input asset's category stays `UNKNOWN` and is reported as such; it is never presented as commercially approved |
| the resource *estimate* is not an enforcement | medium | the runtime enforces the timeout and the size ceilings; the estimate is a declaration and is labelled as one |
