# COMPOSITION RUNTIME — the capability contract, the IR and the licence ledger

Living view of the NAGAR Creative Capability Composition Runtime.
Authority for the boundary: [`adr/0014-creative-capability-composition.md`](adr/0014-creative-capability-composition.md).
Audit: [`CANONICAL_INFRASTRUCTURE_AUDIT.md`](CANONICAL_INFRASTRUCTURE_AUDIT.md).
Threat model: [`COMPOSITION_THREAT_MODEL.md`](COMPOSITION_THREAT_MODEL.md).
Operator guide: [`../ops/COMPOSITION_RUNTIME.md`](../ops/COMPOSITION_RUNTIME.md).

---

## 1. The capability contract

### 1.1 Identity and versioning

A composable capability is identified by three independent facts, all resolved
from the **live** registry (`creative/packs/runtime.py::build_runtime_registry()`):

| Fact | Where it lives | Who resolves it | Drift is |
|---|---|---|---|
| `operation` | the operation id, e.g. `media.resize_image` | `CapabilityRegistry.get_spec` | `unknown_operation` |
| `capability_id` | `CapabilityRegistry.describe(operation).capability_id` | the compiler | `capability_version_drift` |
| `capability_version` | `CapabilityRegistry.describe(operation).version` | the compiler | `capability_version_drift` |
| `operation_schema_version` | `OperationSpec.schema_version` | the compiler | `schema_version_drift` |
| `registry_fingerprint` | `compiler.registry_fingerprint` | the compiler | `registry_drift` |

`registry_fingerprint` is `sha256:` over the sorted
`operation@capability_id#version#schema#permission#packs` lines of the whole
registry. A plan compiled against a different registry therefore has a
different fingerprint from the same plan compiled today, and the plan's own
`registry_fingerprint` field makes the drift *detectable* rather than silent.

### 1.2 The typed media contract

`creative/studio/models.py::MediaContract` is the canonical shape of a media
value. Every field is a **constraint**, not a description: `None` means
"unconstrained on this axis"; a concrete value means "a produced value must
match". `MediaContract.accepts(produced)` is the compatibility predicate, and
it is fail-closed — an unknown axis on the produced side never satisfies a
constrained axis on the required side.

| Axis | Type | Meaning |
|---|---|---|
| `kind` | `image \| video \| audio \| data` | the media family |
| `container` | `str \| None` | e.g. `png`, `mp4` |
| `codec` | `str \| None` | e.g. `png`, `h264` |
| `pixel_format` | `str \| None` | e.g. `yuv420p`, `gray` |
| `color_space` | `str \| None` | e.g. `gray`, `bt709` |
| `frame_rate` | `float \| None` | video only |
| `time_base` | `str \| None` | e.g. `1/12800` |
| `sample_rate` | `int \| None` | audio/video |
| `channel_layout` | `str \| None` | e.g. `stereo` |
| `bit_depth` | `int \| None` | e.g. `8` |
| `alpha` | `none \| opaque \| straight \| premultiplied \| None` | the alpha channel |
| `width`, `height` | `int \| None` | the pixel geometry |
| `duration_us_min`, `duration_us_max` | `int \| None` | the duration bounds |

A capability declares its media ports on the **canonical** `OperationSpec`:

```python
OperationSpec(
    ...,
    media_inputs=(MediaPort(name="source", contract=MediaContract(kind="image")),),
    media_outputs=(
        MediaPort(
            name="resized", contract=MediaContract(kind="image", container="png", codec="png")
        ),
    ),
    media_derived_fields=("source_asset_id", "source_sha256", "output_path", "output_sha256", ...),
)
```

`register_operation` **refuses** a duplicate media port name, because an
ambiguous binding would silently pick the first match.

### 1.3 The versioned parameter schema

A composable capability's parameters are the canonical `OperationSpec.input_model`
— a Pydantic model with `extra="forbid"`. The compiler derives a *caller-facing*
model from it: the executor-filled fields (`media_derived_fields`) become
optional, everything else keeps its type, range and enum constraints, and
`extra="forbid"` stays. The compiler therefore validates against the **same**
schema the command bus validates against, not a copy of it.

| Rule | Enforcement | Failure code |
|---|---|---|
| required / optional | the derived model | `invalid_parameters` |
| type | the derived model | `invalid_parameters` |
| range / enum | the derived model | `invalid_parameters` |
| unknown rejection | `extra="forbid"` | `unknown_parameter` |
| compatibility | the pinned capability + schema versions | `capability_version_drift` / `schema_version_drift` |
| normalization | none is applied silently: the canonical handler normalizes | — |

### 1.4 The resource model

The resource model keeps three different numbers apart, because conflating them
is how a runtime under-provisions itself:

| Field | Meaning |
|---|---|
| `peak_ram_mb` | the **maximum** node requirement — the composition runs one node at a time |
| `total_ram_mb` | the **sum** over the nodes — what a parallel execution would need |
| `peak_concurrent_ram_mb` | the peak under the composition's own concurrency (1) — equal to `peak_ram_mb` |
| `total_disk_mb` / `scratch_mb` | the durable and the temporary footprint |
| `cpu_threads` / `gpu_memory_mb` | the compute shape |
| `network_egress` | whether any node needs the network (false for every builtin media capability) |
| `max_timeout_seconds` | the slowest node's wall-clock bound |
| `node_count` | the number of nodes |

The estimate is a **declaration**, not an enforcement. What the runtime actually
enforces: a wall-clock timeout per process (with a process-group kill), an input
size ceiling, an output size ceiling, a per-node scratch directory, and a
concurrency of one. What it *measures*: the peak RSS and the CPU time of the
worker process (see the benchmark report).

---

## 2. The composition IR

### 2.1 The objects

```python
CompositionPlan(
    plan_version=1,
    nodes=(PlanNode(...), ...),
    external_inputs=(ExternalInput(name="src", media_kind="image"),),
    expected_outputs=(ExpectedOutput(name="final", node_id="video", output_port="video"),),
    registry_fingerprint="sha256:…",
)

PlanNode(
    node_id="resize",
    operation="media.resize_image",
    capability_id="media.resize_image",
    capability_version="1.0.0",
    operation_schema_version=1,
    params={"width": 1280, "height": 720},
    bindings=(PortBinding(input_port="source", source="external", external="src"),),
)

PortBinding(
    input_port="source",
    source="external" | "node_output",
    external="src",  # when source == "external"
    node_id="resize",  # when source == "node_output"
    output_port="resized",
)
```

Every model is `frozen=True, extra="forbid"`. A plan that arrives from an
untrusted source is therefore parsed into a shape that cannot widen its own
policy.

### 2.2 The fingerprint

```
plan_fingerprint = sha256( sha256(canonical_json(plan)) + ":" + registry_fingerprint )
canonical_json(plan) = json.dumps(plan.model_dump(mode="json"),
                                   ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False)
```

Two consequences: the fingerprint is **stable** across processes and machines
(the JSON is sorted and the separators are fixed), and it **binds the plan to
the registry** (a plan compiled elsewhere cannot be replayed silently).

### 2.3 The compiler's rules

| Rule | Failure code |
|---|---|
| every node's operation exists in the live registry | `unknown_operation` |
| the node's `capability_id` is the operation's own capability | `capability_version_drift` |
| the pinned capability version matches | `capability_version_drift` |
| the pinned schema version matches | `schema_version_drift` |
| the operation declares media ports (it is composable) | `not_composable` |
| every required media input is bound exactly once | `missing_binding` |
| every bound port exists, and every target resolves | `unknown_binding` |
| the graph is acyclic | `cyclic_graph` |
| the produced contract satisfies the required one | `type_mismatch` |
| the parameters validate against the canonical schema | `invalid_parameters` |
| no unknown parameter | `unknown_parameter` |
| the plan's registry fingerprint matches the live registry | `registry_drift` |
| the plan has at least one node | `empty_plan` |

The execution order is **Kahn's algorithm with a deterministic tie-break**
(node id), so the same plan always compiles to the same order.

### 2.3.1 Where the execution is authorized

The compiler authorizes nothing. The executor asks the **bus** before it
encodes anything:

```python
CommandBus.check_authorization(command)  # actor grant, capability, packs, A/B/C/D
# ... on refusal: CompositionExecutionError, and no media file exists
```

`check_authorization` runs the same private helpers the dispatch runs
(`_spec_locked`, `_authorize_locked`) and stops before reference resolution,
reservation and the handler. The input model is not validated there on purpose:
the canonical operations take the produced artifact's hash as input, and that
hash only exists *after* the encode, which the dispatch itself validates before
anything is applied. The pack lifecycle gate is still called from `bus.py`
only — `tests/architecture/test_lifecycle_gate_boundary.py` enforces that with
an AST scan of `src/`.

### 2.4 The derived capability

A composition does **not** create a capability. It *derives* a plan over
capabilities, and the derivation keeps the exact pinned dependencies:

```
lineage = ("resize:media.resize_image@1.0.0",
           "gray:media.grayscale_image@1.0.0",
           "video:media.image_to_video@1.0.0")
```

Each entry names the node, the canonical operation and the **pinned**
capability version — so a derived artifact states which version of which
capability produced it, and the artifact passport can be re-derived from the
durable row.

---

## 3. The licence / dependency ledger

The ledger keeps **twelve** categories apart
(`creative/composition/contracts.py::CATEGORIES`):

| # | Category | What it describes |
|---|---|---|
| 1 | `adapter_code` | the code this repository adds |
| 2 | `runtime_software` | the executable that does the work (FFmpeg) |
| 3 | `library` | the libraries that executable links |
| 4 | `model` | a model artifact / architecture |
| 5 | `model_weights` | the trained weights of that model |
| 6 | `dataset` | the data a model was trained or built on |
| 7 | `input_asset` | the media the composition consumes |
| 8 | `distribution_restrictions` | where the output may be shipped |
| 9 | `commercial_permission` | whether use is commercially allowed |
| 10 | `attribution_notices` | the notices that must be reproduced |
| 11 | `copyleft` | whether obligations propagate |
| 12 | `network_use` | obligations created by network use |

### 3.1 The two rules

1. **The tool's licence is not the output's rights.** FFmpeg being GPL says
   nothing about whether *the produced media* may be used commercially — that
   is a property of the input asset and of the distribution terms, recorded in
   their own categories.
2. **`UNKNOWN` stays unknown.** A category with no record is reported by
   `LicenseLedger.unknown_categories`, and `effective_allow_commercial` is
   `False` unless *every* record is both permitted **and verified**
   (`commercial_use_verified=True`).

### 3.2 The measured FFmpeg facts

`creative/composition/license.py::ffmpeg_build_facts()` reads the facts back
out of the binary that actually ran — nothing is remembered:

| Fact | How it is measured | Observed value in this environment |
|---|---|---|
| version | `ffmpeg -version`, first line | `ffmpeg version n9.0.2-17-g2a571b6068-20260930` |
| build configuration | `ffmpeg -version`, `configuration:` line | `--enable-gpl --enable-version3 --enable-libx264 --enable-libx265 …` |
| GPL | `--enable-gpl` present | `True` |
| nonfree | `--enable-nonfree` present | `False` |
| static | `--pkg-config-flags=--static` present | `True` |
| libx264 encoder | `ffmpeg -encoders` | present (`V....D libx264`) |
| libx265 encoder | `ffmpeg -encoders` | present |
| enabled components | the `--enable-*` flags | 80 flags |

The recorded `runtime_software` licence is therefore `GPL-2.0-or-later`
(copyleft, commercial use permitted *by the licence text*, verified from the
build configuration), and the `input_asset` category stays **UNKNOWN** — the
composition cannot know the rights of the media it was handed.

---

## 4. The evidence

`creative/composition/proof.py::ProofCarryingArtifact` carries:

| Layer | Contents |
|---|---|
| integrity | the recorded digest, the independently re-measured digest, the size, the method name |
| semantic | the independently measured dimensions, colour space, pixel format, codec, frame rate, duration, **decoded frame count**, and the findings |
| identity | the artifact id, the output name, the plan fingerprint, the registry fingerprint |
| lineage | the ordered `node:operation@version` chain |
| receipts | one per node: the operation, the capability, the parameters, the input hashes, the output hash and path, the state revision and state hash, the derived asset id, and the node's **own** semantic report |
| tool versions | the measured FFmpeg version and the digest of its build configuration |
| licence | the ledger report, with the unknown categories named |

The integrity layer and the semantic layer are **separate mechanisms**: the
first re-hashes the bytes, the second re-decodes the media. A file whose bytes
hash correctly but whose content is wrong fails the second.
