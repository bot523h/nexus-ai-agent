# NAGAR Mathematical Engineering Doctrine

**Status:** normative. This document binds every module in `creative/` that
turns a measurement into a claim. Where the code and this document disagree, the
code is wrong.

**Scope:** the audio execution plane (`creative/audio_engine`), the studio port
it implements (`creative/studio/audio_engine.py`), the evidence ladder
(`creative/studio/evidence.py`, `lifecycle.py`) and any future kernel that
measures media. The rules are stated once here so a new module cannot invent a
weaker interpretation.

---

## 1. The four layers

Work in this repository is only ever in one of four layers. Naming the layer is
part of the claim, and a claim may not borrow authority from a layer above it.

| Layer | Name | What it asserts | What it may never assert |
| --- | --- | --- | --- |
| **A** | **Contract** | A named operation exists, has a typed request and a typed result, and its failure modes are enumerable. | That it ran. |
| **B** | **Execution** | Real bytes moved through a real tool; the process exited; the artifact exists at a path with a known SHA-256. | That the bytes are correct, reach a target, or are the ones a recipe implies. |
| **C** | **Measurement** | A number was read from the artifact *after* it was produced, by an instrument that is not the producer. Uncertainty is stated. | That the number proves intent, taste, or perceptual quality. |
| **D** | **Evidence** | A measurement has been independently verified and bound to a recipe, a source hash and a signer, and the resulting state is one of the named rungs. | Anything beyond the rung it reached (§6). |

**MUST NOT:** present a layer-B fact as a layer-D conclusion. "FFmpeg exited 0"
is layer B. It is not "the audio reaches −14 LUFS" (layer C) and it is certainly
not "this is a streaming master" (layer D).

---

## 2. The eight-part scientific contract

Every operation that produces media **MUST** be able to state all eight parts.
An operation that cannot fill a part **MUST** report the part as
`NOT_MEASURED` and lower its own status; it may not omit the part.

1. **Hypothesis** — the falsifiable statement the operation makes
   (*"after this render, the integrated loudness of the published bytes is
   within ±1.0 LU of −14.0 LUFS"*).
2. **Inputs and their provenance** — asset ids, the SHA-256 of every input
   byte sequence, and the registry entry that hash was checked against.
3. **Method** — the exact tool and filter chain, with versions
   (`ffmpeg -version`, filter names, the canonical stream profile id).
4. **Instrument** — what performed the measurement, and why it is independent
   of the producer (a second decode, a different filter, a re-read of the file).
5. **Result** — the measured value, with units, and the target it is compared
   against.
6. **Uncertainty** — a stated interval (§3), not a bare number.
7. **Falsification record** — what a failure would have looked like, and
   evidence that the check can in fact fail (a mutation probe, §8).
8. **Decision** — publish, refuse, or degrade, and the reason code.

---

## 3. Uncertainty (JCGM GUM)

Measurements are reported as `value ± U` with the coverage factor stated.

* **Type A** uncertainty comes from *repeated observation*: run the measurement
  N times and take the standard deviation of the mean. It shrinks with N and
  **MUST** be reported whenever the instrument is stochastic (any analysis that
  depends on onset detection, spectral flux, or frame boundaries).
* **Type B** uncertainty comes from *specification*: the instrument's own
  resolution and the tolerance declared by the standard. EBU R128's gate
  behaviour and the ±0.1 LU repeatability of `ebur128` are Type B.
* **Combined** uncertainty is `u_c = sqrt(sum(u_i^2))` under the assumption of
  independence. Where inputs are correlated, the covariance term **MUST** be
  included or the correlation **MUST** be shown to be negligible.
* **Expanded** uncertainty is `U = k * u_c`. The engine uses `k = 1` for
  internal gates and states it; a user-facing claim uses `k = 2` and says so.

**MUST NOT:** report a single decimal place the instrument cannot support. A
loudness measurement is reported to 0.1 LU, not to 0.001 LU.

### 3.1 Nonlinear sensitivity

When a result is a function of several inputs, `y = f(x_1 … x_n)`, the reported
uncertainty **MUST** be propagated through the **Jacobian**, not through a
percentage rule of thumb:

```
u_c^2(y) = J^T · C · J ,     J_i = ∂f/∂x_i
```

For a linear map (`y = a·x`), `J = a` and the rule reduces to the familiar
`u(y) = |a|·u(x)`. For anything else — a `loudnorm` dynamic-mode gain, a
camera projection, a relighting solve — the derivative **MUST** be evaluated at
the operating point, by analytic differentiation where possible and by a
finite-difference check in a test where not.

The camera kernel (STAGE-B, K2) is the canonical example: the reprojection
uncertainty in pixels is `∂H_px/∂Z_c · u(Z_c)` and **MUST** be computed from the
projection's actual partial derivative, not from a fixed pixel tolerance.

---

## 4. Confidence ≠ probability

`confidence` is **not** a probability and **MUST NOT** be presented as one,
converted to a percentage, or multiplied as if it were independent of the
evidence that produced it.

* A tempo `confidence` in this codebase is `1 − (variation / tolerance)`: a
  **normalised margin** against a stated acceptance window. It answers "how far
  inside the window is this estimate", not "how likely is it to be right".
* A probability requires a generative model, a sample space and a calibration
  set. None of those exist for "is this 96 BPM". Presenting a margin as a
  probability is the single most common way a media pipeline lies to its user.
* Where a genuine probability *is* available (a calibrated classifier with a
  held-out set and a reliability diagram), it **MUST** be labelled
  `probability` with its calibration method and set size.

**MUST:** store the margin and the window it was measured against, so the number
is reproducible. **MUST NOT:** name a margin `probability`, `p`, `score%`, or
`certainty`.

---

## 5. Verification before publication

The engine's one ordering rule:

```
RENDER IN ATTEMPT STAGING
      ↓
INDEPENDENT DECODE / MEASURE        (a different decoder read, not the encoder's opinion)
      ↓
ATOMIC PUBLISH                      (one rename; before this, no final path exists)
      ↓
POST-PUBLISH SHA RECHECK            (published bytes == verified bytes)
      ↓
FENCED COMMIT                       (the evidence row lands only after the bytes do)
```

Consequences that are **MUST**s:

* An artifact that fails verification **MUST NOT** ever be readable at its final
  path. There is no "publish, then quarantine" window.
* Reuse of an existing artifact **MUST** be keyed on the full recipe identity
  (operation, source hash, every parameter, every profile, engine identity), not
  on the output id. A cache key that omits a parameter is a false evidence
  generator.
* A cancellation **MUST** reach the child process group, and a cancelled run
  **MUST** leave neither a published artifact nor staging residue.
* Exit status is **not** verification. FFmpeg exits 0 on corrupt input; the
  decode must be checked for its own corruption markers.

---

## 6. The evidence ladder (statuses are earned, never assigned)

Only these statuses may be reported, and each names exactly what has been
established:

| Status | Established |
| --- | --- |
| `VERIFIED` | The artifact exists, was independently measured after publication, and every stated target was met. |
| `VERIFIED_WITH_LIMITATIONS` | `VERIFIED`, and the limitations are named (e.g. a stream-shape check that needs an absent `ffprobe`, a perceptual claim that needs listening). |
| `HARDENED_BUT_NOT_COMPLETE` | The defect class is closed and mutation-proved, but a stated part of the scope is not done. The unfinished part is named. |
| `BLOCKED` | Progress requires an authority outside this change (a claim owned by another task, a missing external service). The blocker is named with its gate. |
| `DEFERRED` | Deliberately not done, with the trigger that would un-defer it. |

**MUST NOT:** promote a status because the tests pass, because a gate is green,
or because work is running out. A status is a measurement, and a measurement
needs a measurement to change it.

`Confidence ≠ Probability` (§4) applies to statuses too: "I am 90% sure this is
verified" is not a status.

---

## 7. The single-authority rule

There is exactly one of each of the following, and a new module **MUST** use it
rather than build a second:

| Authority | Module |
| --- | --- |
| Attempt staging + atomic publish | `execution/staging.py::AttemptStaging` |
| Filesystem boundary + bounded writes | `tools/filesystem_policy.py::WorkspaceFilesystem` |
| Child process creation | `creative/audio_engine/process.py::ProcessGovernor` |
| Audio workspace (engine-internal) | `creative/audio_engine/workspace.py::RuntimeWorkspace` |
| Artifact passport / verification | `creative/audio_engine/verify.py` |
| Recipe identity | `creative/audio_engine/recipe.py` |
| Command dispatch | `creative/studio/commands.py::CommandBus` |

A second implementation of any of these is a defect even when it works, because
two authorities disagree eventually and the disagreement is invisible.

---

## 8. Loudness targets are earned, not labelled

A loudness target is a **processing contract**, not a number to be announced.

* A profile that may only apply **gain** **MUST** be labelled `STRICT_LINEAR`,
  and its peak ceiling **MUST** be treated as an absolute refusal: a programme
  whose crest factor exceeds `TP - target` cannot reach the target without a peak
  ceiling breach, and the engine **MUST** refuse with the arithmetic in the
  message rather than render a file that undershoots the target.
* A profile that may apply **gain plus true-peak limiting** **MUST** be labelled
  `DYNAMIC_ALLOWED`, **MUST** carry a `max_crest_reduction_db` destruction bound,
  and **MUST** report the crest reduction it actually applied.
* `-14 LUFS` **MUST NOT** be presented as a broadcast standard. No body mandates
  it; it is a delivery preset, and the profile is named `nagar_streaming_preset`
  for that reason.
* A reported **crest reduction** **MUST** reconstruct as
  `source_crest - output_crest` from measurements of the published file. It is
  **MUST NOT** be derived from the profile that was requested.
* When two measurements are compared across a channel-count change, the
  dual-mono up-mix term `10*log10(N_out/N_in)` **MUST** be accounted for
  explicitly. Two crest factors taken in different channel domains differ by
  exactly that term, and comparing them without it manufactures 3.01 dB of
  "reduction" out of nothing.

## 9. Measurement debt is declared, not hidden

* **Benchmarks** **MUST** run at least `N = 3` times and report the median, the
  spread, the Peak RSS, and the tool version. A single run is an anecdote.
* **Peak RSS** that was not measured is reported as `NOT_MEASURED`. It is never
  estimated from a formula and presented as a measurement.
* **Mutation probes** are how a test suite proves it is load-bearing: revert the
  fix, and the named test **MUST** fail. A suite with no mutation coverage is a
  claim without a falsification record (§2, part 7).
* **Environment gaps** (`ffprobe` absent, a model not installed) are reported as
  `UNAVAILABLE` with the remedy. Downloading a missing capability at runtime to
  make a gate pass is forbidden; it converts an honest limitation into an
  unreproducible green.

---

## 10. What this doctrine does not cover

It does not claim the engine's output is *good*. Loudness, crest factor and
spectral centroid are measurable properties, not judgements about whether a mix
sounds right. A claim about quality **MUST** come from a listening test with a
stated protocol, and until then the honest word is "measured", not "good".

---

## 11. Phase-2 handoff contracts (out-of-zone items, stated so they are 1-line plug-ins)

OMEGA Wave-2 works inside `creative/audio_engine/**`, `creative/packs/audio/**`,
`creative/studio/audio_engine.py`, `creative/studio/evidence.py`,
`creative/slideshow/audio.py`, `scripts/mutate_omega_audio.py` and the audio
tests. Two forward items cannot be closed from inside that zone. They are
specified here rather than silently dropped, and each one names the **exact**
signature the out-of-zone owner must provide, so integration is a call site and
not a redesign.

### 11.1 `AssetVersion` persistence (task-282 `DB` schema)

**What the engine already produces.** `ArtifactEvidence` (in
`creative/studio/audio_engine.py`) carries every field a version row needs, and
`build_passport` already returns a signed `ArtifactPassport` whose
`payload_sha256` is a stable identity for the measured artifact.

**What Phase-2 must add.** A persistence adapter with this signature:

```python
def record_asset_version(
    *,
    session: AsyncSession,
    asset_id: str,
    evidence: ArtifactEvidence,      # creative.studio.audio_engine
    passport: ArtifactPassport,      # creative.audio_engine.verify
    parent_version_id: str | None = None,
) -> str:                            # returns the new version id
```

**Invariants the adapter must preserve**, in this order:

1. **Append-only.** A version row is never updated. A re-render produces a new
   row with a new `parent_version_id`; it never mutates its ancestor.
2. **The passport is stored verbatim**, not re-derived. Rebuilding it at read
   time would let a schema migration silently change what was signed.
3. **`passport.payload_sha256` is the uniqueness key**, not the file's
   `sha256` alone: two renders of identical bytes under different profiles are
   different versions, and both facts must survive.
4. **A row is only written when `passport` verifies.** The adapter must call
   `verify_passport` and refuse on `report.passed is False`; storing an
   unverified passport converts a refusal into a historical fact.

**Why this is not being done here:** it requires a schema migration and a
Postgres test fixture outside the audio zone. The engine side is complete and
covered; the gap is entirely persistence.

### 11.2 `studio/bus.py` — publishing an audio artifact

**What the engine already guarantees.** `RuntimeWorkspace.publish(staged,
destination, *, replace=False, protected=())` performs an atomic, no-symlink,
no-overwrite rename that returns a `PublishResult` carrying `sha256`,
`size_bytes` and the resolved path. It refuses to publish over a run input by
both path and inode identity.

**What Phase-2 must add.** The bus side of that contract:

```python
async def publish_audio_artifact(
    *,
    workspace: RuntimeWorkspace,
    staged: Path,
    destination_key: str,            # bus-owned logical key, not a filesystem path
    replace: bool = False,
) -> PublishResult:
```

**Invariants the caller must preserve:**

1. **The bus resolves the key; the engine never does.** Passing a bus key as a
   path would reintroduce the path-traversal surface the workspace exists to
   remove.
2. **`replace=False` is the default and a retry must reuse the same key.** A
   publish that fails and is retried with a fresh key leaves two artifacts and
   only one of them verified.
3. **The bus must not call `publish` before the passport verifies.** Publication
   is the *last* step: verify → publish → record. Reordering any two of those
   makes the artifact's existence a claim rather than a fact.

**Why this is not being done here:** `studio/bus.py` is another agent's zone in
the Phase-2 convergence, and editing it from this branch would create the
overlapping-zone conflict the board guard exists to prevent.
