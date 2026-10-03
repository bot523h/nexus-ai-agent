# Creative Intelligence Plane

**Status:** living view · **Owner zone:** `creative-intelligence-plane` · **Slices:** task-224 (Typed Creative IR), task-225 (creative semantics) · **Enforcing tests:** [`test_creative_intelligence_boundary.py`](../../tests/architecture/test_creative_intelligence_boundary.py), [`test_creative_ir.py`](../../tests/unit/test_creative_ir.py), [`test_creative_ir_determinism.py`](../../tests/unit/test_creative_ir_determinism.py), [`test_creative_semantics.py`](../../tests/unit/test_creative_semantics.py)

The Creative Intelligence Plane is the part of NEXUS that turns *what a person
meant* into *what should be built*. It sits upstream of execution and stops
there.

```mermaid
flowchart LR
    A[User intent] --> B[Understanding]
    B --> C[Strategy]
    C --> D[Typed Creative IR]
    D --> E[Compilation]
    E --> F[Executable plan]
    F --> G[CommandBus]
    G --> H[Execution]
    subgraph plane [Creative Intelligence Plane — this package]
        B
        C
        D
        E
        F
    end
    subgraph substrate [Canonical Creative Execution Substrate — not this package]
        G
        H
    end
```

The plane **proposes**; it never executes. It contains no `subprocess`, no file
or network I/O, no database access, no event loop, and no reference to the
CommandBus or to transactions. That is enforced structurally, not by
convention — see [§7](#7-boundaries-and-their-enforcers).

## 1. Why this layer exists

Before task-224 the repository had no representation of a creative piece at the
level of *meaning*. Verified against `main` at `e5b326b`:

| Question | Answer on `main` @ `e5b326b` | Evidence |
|---|---|---|
| Is there a `Strategy` abstraction in `creative/`? | No. The only hit for `strategy` in `src/` is provider routing in `llm/litellm_provider.py`. | `grep -rni strategy src/nexus_ai_agent` |
| Is there a `Recipe` type? | No occurrences of `recipe` anywhere in `src/`. | `grep -rni recipe src/nexus_ai_agent` |
| Is there a semantic diff between two creative states? | No. | `grep -rniE "creative.diff\|semantic.diff" src/` returns nothing |
| Is there an authoring-level creative IR? | No. `creative/rendering/ir.py` is shaped like an FFmpeg lane (`LaneOp` → filtergraph); `creative/studio/models.py` `Project`/`Timeline` is *execution state* the bus mutates. Neither expresses "a hook, a subject, a call to action, fast paced, premium". | reading both modules |
| What sits between a user request and a capability operation? | On the unmerged spine lineage (PR #126) a keyword table maps five phrases onto five registry operations. Nothing models the piece itself. | `gh pr diff 126` |

So the gap was not a missing feature but a missing *type*: there was nothing for
a strategy to produce, nothing for a compiler to consume, nothing for a revision
to edit, and nothing for a diff to compare. This layer supplies that type.

## 2. The Typed Creative IR

[`src/nexus_ai_agent/creative/intelligence/ir.py`](../../src/nexus_ai_agent/creative/intelligence/ir.py)
defines `CreativeWork` — the plane's one exchange type.

```mermaid
flowchart TD
    W[CreativeWork] --> BR[CreativeBrief]
    W --> AS[Asset]
    W --> EF[Effect]
    W --> SC[Scene]
    W --> TR[Transition]
    W --> CO[Constraint]
    W --> OU[OutputRequirement]
    W --> TK[Track layout]
    SC --> LY[Layer]
    LY --> MC[MediaContent]
    LY --> TC[TextContent]
    MC --> SG[Segment]
    SG --> TM[Timing]
```

Every concept earns its place against a capability operation the repository
already exposes (52 operations across the six pack manifests):

| IR concept | Consumed today by |
|---|---|
| `Asset` | `slideshow.scan_assets`, `AssetRecord` |
| `Scene` | `slideshow.compose`, `ShotPlan` |
| `Segment` / `Timing` | `TimeRangeUS`, `timeline.trim` |
| `Transition` | `motion.add_transition` |
| `Effect` | `motion.add_glow`, `color.apply_lut`, `color.adjust_exposure` |
| `AudioIntent` | the ten `audio.*` operations |
| `TextSpec` | `motion.add_title`, the ten `caption.*` operations |
| `OutputRequirement` | `delivery.render_master_4k`, `delivery.make_proxy_480p` |

Two have no precedent in the repository and are the reason the slice exists:

* **`SemanticRole`** — *why* an element is present (`subject`, `hook`, `cta`,
  `music`, `voiceover`). Without it, "make it more emotional but keep the
  rhythm" has no addressable target and a revision can only rebuild blindly.
* **`Constraint`** — a requirement that survives compilation and can be
  *checked*. All five kinds are decidable against the IR itself, so a
  constraint is a predicate rather than a comment.

### 2.1 What the IR deliberately does not model

**Tracks are layout, not authoring.** `Track` exists but only as compiler output:
an authored document carries `layout=()` and its layers carry a role plus a
timing; the normaliser decides track allocation, after which the layout must be
complete, single-assigned and non-overlapping. Track allocation is an opinion of
whatever renders the piece; letting a strategy author tracks would freeze every
future backend to one NLE's structure.

**No filtergraph, no argv, no codec.** `Effect.operation` names a capability
operation. Turning that into an FFmpeg filter belongs to
[`creative/rendering/`](../../src/nexus_ai_agent/creative/rendering/), downstream
of the compiler.

**No floats.** Time is integer microseconds, normalised quantities are integer
per-mille (0..1000), and signed continuous parameters are integer milli-units
(`+1.5` EV is `1500`). This is a determinism requirement, not a style choice —
see [§4](#4-determinism-and-identity).

## 3. Two gates with different contracts

A document passes through two checks, and they are not interchangeable:

| Gate | Answers | Runs on | Fails with |
|---|---|---|---|
| `assert_valid()` | Is this document *well-formed*? | Authored (unsealed) documents, before identity exists | `IRValidationError` carrying **every** problem found |
| `seal()` | Assign the true content address | Well-formed documents | `DanglingReferenceError`, because sealing resolves references |
| `verify_identity()` | Does this document match its own content? | Sealed documents | `IdentityError` naming each drifted id |
| `check_constraints()` / `assert_constraints()` | Is this what was *asked for*? | Sealed documents | `ConstraintViolationError` (hard violations only) |

`assert_valid()` reports all problems at once rather than raising on the first,
because a strategy layer that produced a broken document needs one actionable
list, not one rebuild per discovery.

It is named `assert_valid` and not `validate` because pydantic's
`BaseModel.validate` is a *classmethod* with a different signature; shadowing it
on a model would break Liskov substitution for anyone reaching for the pydantic
API. [`test_creative_ir.py`](../../tests/unit/test_creative_ir.py) pins this.

### 3.1 Constraints are predicates

| Kind | Meaning | Decidable against the IR |
|---|---|---|
| `TimingConstraint` | the work, or one scene, must fall in a duration band | yes |
| `OrderConstraint` | one scene must precede another | yes |
| `ExclusionConstraint` | an operation or asset kind must not appear | yes |
| `EmphasisConstraint` | a role must claim at least a share of attention | yes |
| `QualityConstraint` | a floor on resolution, frame rate or duration | yes |

`priority` separates a requirement that blocks the job (`HARD`) from a
preference the compiler may trade away and must report (`SOFT`).

## 4. Determinism and identity

Every identity in the IR is the SHA-256 of the element's own semantic payload
joined with the identities it refers to. The document is a **Merkle tree**.

```mermaid
flowchart BT
    A[Asset] --> L[Layer]
    E[Effect] --> L
    L --> S[Scene]
    S --> W[CreativeWork]
    S --> T[Transition]
    T --> W
    C[Constraint] --> W
```

Four properties follow, and each one is a capability a later slice cannot get
any other way:

1. **Deterministic compilation.** Building the same document twice yields
   byte-identical identities. There is no `uuid4` in the identity path — an
   AST-level test asserts it.
2. **Cheap semantic diff.** A change confined to one scene changes exactly that
   subtree and the root. Every other identity is bit-identical, so "what changed
   between v1 and v2" is a tree walk rather than a text diff.
3. **Cheap semantic revision.** A revision that rewrites one scene re-hashes one
   subtree. Untouched elements keep their identities, which is what makes a
   revision an *edit* rather than a rebuild.
4. **Self-verification.** `verify_identity()` re-derives every id, so a
   hand-edited or stale document is caught before it reaches a compiler.

`seal()` is a pure function of content: it ignores whatever provisional ids a
builder used, and `seal(seal(w)) == seal(w)`.

## 5. Creative semantics

[`semantics.py`](../../src/nexus_ai_agent/creative/intelligence/semantics.py)
turns the words people actually say into bounds a compiler can meet.

`"cinematic"`, `"fast paced"`, `"minimal"`, `"premium"` are not compilable. Left
as strings they are worse than useless: they read like requirements while being
unfalsifiable, so a plan can ignore them and nothing notices. Here a recognised
phrase becomes a typed `Constraint` with a number in it — *fast paced* becomes
*no scene longer than three seconds* — which the IR checks, a compiler can
satisfy, and a test can falsify.

```mermaid
flowchart LR
    P["surface phrase<br/>fast paced / ریتم تند"] --> N[normalize_phrase]
    N --> M[SemanticMeaning]
    M --> D[SemanticDirective]
    D --> C[typed Constraint]
    P2["unrecognised phrase"] --> U["brief.unresolved_intents"]
```

Three boundaries are deliberate:

**The lexicon is data, and it is built, not written.** `_SURFACE_FORMS` maps
phrases as a person types them onto meanings; `LEXICON` is produced by running
every key through `normalize_phrase`, so a surface form can never be stored in a
spelling the normaliser would never produce. That defect class is invisible in
review and makes the entry silently unreachable — it was found in this package
during review of this slice, and the builder is what prevents it recurring. Two
forms that normalise onto the same key with *different* meanings raise at import
time.

**Collisions are arithmetic, not a list of known bad pairs.** When duration
bands on one target leave an empty interval, or emphasis shares sum past 1000
permille, `detect_collisions` reports it on the numbers. A brand-new phrase that
contradicts an old one is therefore caught without teaching the detector about
the pair. The resolver refuses to pick a winner: choosing silently is the exact
failure this plane exists to prevent.

**Style targets are declared, not applied.** A directive states that *premium*
means warmer and less dense; the resolution reports those axis targets. Which
layers they land on is a strategy decision, and a lexicon that rewrote layer
styles would be making creative choices it has no basis for — so
`apply_semantics` provably never changes a layer identity.

`apply_semantics` is idempotent, and provably so: derived constraints are
content-addressed, so re-deriving them from an unchanged brief reproduces the
same identities and de-duplication removes them.

### 5.1 The layer is falsifiable

The reference test document asks for *"cinematic, premium, fast paced, dramatic
reveal"* and the semantic layer correctly reports that it violates three of them:
the climax applies the glow *premium* forbids, one scene runs five seconds
against the three-second pacing ceiling, and the subject's attention share falls
just under what a dramatic reveal requires. A semantic layer that always agrees
with the document is not measuring anything.

### 5.2 Diff and revision stay typed, explicit and local

[`diff.py`](../../src/nexus_ai_agent/creative/intelligence/diff.py) and
[`revision.py`](../../src/nexus_ai_agent/creative/intelligence/revision.py)
consume the same sealed identities as the IR itself.

* `diff_works(before, after)` is not a field comparer. It first checks the root
  identity, prunes any unchanged subtree in O(1), then reports only the semantic
  edits that remain: scene timing, constraint changes, text/audio/effect intent,
  attention-share edits and explicit additions/removals.
* `apply_revision(work, intent)` is not free mutation. A revision carries an
  explicit typed target (`NodeTarget` or `SceneRoleTarget`) plus a typed change.
  Ambiguous role targets fail closed with a typed error instead of guessing.
* Revision and diff are connected by contract: a successful revision yields a
  new sealed `CreativeWork`, an IR delta, the affected-node list and the
  semantic diff between the old and new versions.
* Merkle locality remains the invariant. Unchanged branches keep byte-identical
  ids, layout-only `track_id` churn does not rewrite authored meaning, and role
  or constraint matching never falls back to array position or string similarity.

These contracts deliberately stop at the plane boundary: they do no execution,
no storage, no queueing, no bus calls and no network I/O.

## 6. Provenance is data

Every element carries an `Origin` — `source` (`user`, `reference`, `strategy`,
`recipe`, `system`), a `detail` string, and an optional `reference_id`.
Explainability is therefore a structural property of the document: any scene can
answer *why are you here*, and a later slice can show a user that a given beat
exists because they asked for a dramatic reveal rather than because a model
invented one.

`CreativeBrief` carries the raw phrases a user actually said
(`semantic_intents`) alongside the ones nothing resolved
(`unresolved_intents`), and the model rejects an unresolved phrase that was
never declared. An instruction that could not be honoured is a **declared gap**,
never a silent disappearance.

## 7. Boundaries and their enforcers

Every rule below is a build failure, not a comment. Enforced by
[`tests/architecture/test_creative_intelligence_boundary.py`](../../tests/architecture/test_creative_intelligence_boundary.py).

| Rule | Why | Enforcing test |
|---|---|---|
| No `subprocess`, `os`, `socket`, `httpx`, `sqlite3`, `asyncio`, `tempfile`, `urllib` | The plane must be unable to become a second execution authority | `test_no_module_imports_an_execution_or_heavy_dependency` |
| No `open()`, `read_text()`, `write_text()`, `mkdir()` | No file I/O anywhere in the plane | `test_no_file_performs_io` |
| No import of `creative.studio`, `creative.spine`, `creative.rendering`, `creative.packs`, `creative.slideshow`, `storage`, `llm`, `jobs`, `adapters`, `bot`, `api`, `features`, `application` | Coupling would make the IR a view onto state the plane does not own, and would put this package inside another agent's zone | `test_the_plane_never_reaches_into_another_ownership_zone` |
| Only stdlib + `pydantic` + the plane itself | Understanding and compilation are pure transformations; anything needing a provider sits behind a port | `test_the_dependency_surface_is_closed` |
| No `CommandBus`, `PlanTransaction`, `EditTransaction`, `TypedCommand`, `execute`, `dispatch`, `undo` symbol | The plane names no execution machinery, even in a symbol | `test_the_plane_never_names_the_execution_authority` |
| `MICROSECONDS_PER_SECOND` equals the studio constant | The plane redeclares it (the import is forbidden), so a ratchet keeps the promise | `test_the_time_base_matches_the_execution_substrate` |
| `__all__` equals the exported surface | The plane's contract with its consumers stays legible | `test_the_public_surface_matches_the_declared_surface` |
| No `CompiledPlan` / `CreativeCompiler` symbol yet | Honest scope: this slice is the IR, and the compiler slice must update this test deliberately | `test_the_plane_produces_no_plan_or_command_type_yet` |

### 7.1 Relationship to the other substrates

* **Canonical Creative Execution Substrate** (`creative/studio/bus.py`,
  transactions, undo, idempotency): the plane produces plans *for* it and
  imports nothing from it. Authority to execute stays there.
* **Creative execution spine** (`creative/spine/`, PR #126 / #127 lineage): a
  separate, claimed zone. The plane does not import it and does not duplicate
  it. Where the spine maps an intent onto registry operations, the plane models
  the piece those operations are meant to produce; the two are complementary and
  the integration is a later, explicit decision.
* **Persistent Recovery Substrate** (storage, DB init, backup): the IR is
  serialisable to canonical JSON, which is all the plane contributes. It never
  opens a store.

## 8. Honest status

| Capability | Status |
|---|---|
| Typed Creative IR, validation, identity, canonical serialization | **Implemented and tested** |
| Semantic layer: phrases → typed constraints, collision detection, declared gaps | **Implemented and tested** |
| Creative diff | **Implemented and tested** — semantic, lineage-aware, subtree-pruning diff over sealed IR identities |
| Semantic revision | **Implemented and tested** — typed explicit targets, fail-closed ambiguity, deterministic `revision_id`, IR delta + semantic diff in one contract |
| Deterministic compiler (IR → plan) | **Deliberately deferred** — see the capability gap below. The gate in §7 keeps its absence visible. |
| Reference → Creative Recipe | Not built. `Origin.source="recipe"` is reserved for it. |
| Reachable from a user command | **No.** Nothing outside the plane constructs a `CreativeWork` yet. These slices are a foundation, not a feature. |

Current plane evidence: **177 plane tests green** (`test_creative_ir.py` 55,
`test_creative_ir_determinism.py` 21, `test_creative_semantics.py` 42,
`test_creative_diff.py` 4, `test_creative_revision.py` 11,
`test_creative_intelligence_boundary.py` 44), plus full
`tests/architecture` **169 passed**, `test_agent_board.py` **18 passed** and
`test_docs_integrity.py` **59 passed**.

### 8.1 Why the compiler is deferred

Not caution — a measured gap. Verified on `main` @ `e5b326b`: **no capability
assembles a general timeline from assets.** `slideshow.compose` rejects non-image
assets outright ("'assets' must contain image evidence only"), and the bus
registry exposes exactly five operations — `media.play`, `media.pause`,
`timeline.mark`, `timeline.split_at_playhead`, `system.undo` — none of which
places a clip. A compiler that lowered an arbitrary `CreativeWork` today would
have to invent operation ids the registry does not expose, producing a plan the
bus could not run. That is a false green, and it is exactly what the assurance
pass exists to catch.

Unblocked by either (a) a timeline-assembly capability, or (b) scoping the first
lowering to the image-only subset that genuinely does compile to
`slideshow.compose`. Recorded as `deferred_by_this_wave` on the `task-225` board
claim.

## 9. Decision record

See `D-0025`, `D-0026` and `D-0027` in [`DECISION_LOG.md`](../DECISION_LOG.md)
for the accepted decisions, the rejected alternatives (a scene-graph over the
studio `Project`, reusing `rendering/ir.py` as the authoring surface, UUID
identities, float quantities, free dict mutation and ambiguity-by-guessing) and
the evidence.
