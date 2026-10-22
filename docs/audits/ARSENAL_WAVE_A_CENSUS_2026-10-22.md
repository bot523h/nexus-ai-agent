# Nagar OSS Arsenal — Wave A: Candidate Evaluation & Capability Census

> **Mission:** NEXUS × Nagar — OSS Arsenal, Native Skills and Professional Extension Hub.
> **Wave:** A (Inventory and architecture plan). **No source, test or manifest is changed by this document.**
> **Date:** 2026-10-22 · **Branch:** `arena/f6713d82-nexus-ai-agent`
> **Base `origin/main`:** `04469ed0f46fdc66752cdea36d25ce2145db3f1e` · **Head:** see §1
> **Companion:** [ARSENAL_WAVE_A_PLAN_2026-10-22.md](ARSENAL_WAVE_A_PLAN_2026-10-22.md) (sequenced plan, waves B–F)
> **Evidence standard:** every upstream fact below was fetched from the GitHub REST API or the PyPI JSON
> API on 2026-10-22 and is reproducible with the commands in §9. Nothing here is inferred from a
> README claim, a star count or a search snippet.

---

## 1. Live repository truth (verified, not assumed)

| Fact | Value | How verified |
|---|---|---|
| `origin` | `https://github.com/bot523h/nexus-ai-agent.git` | `git remote -v` |
| `origin/main` SHA | `04469ed0f46fdc66752cdea36d25ce2145db3f1e` | `git ls-remote origin refs/heads/main` |
| Working branch | `arena/f6713d82-nexus-ai-agent` | `git branch --show-current` |
| Working tree | clean at census time | `git status --porcelain` → empty |
| Package version | `3.13.0` | `pyproject.toml`, `VERSION` |
| Python floor | `requires-python = ">=3.10"` | `pyproject.toml` |
| Repo license | **MIT** (`Copyright (c) 2026 bot523h`) | `LICENSE` line 1–3 |
| Scale | `src/` 69,250 LOC · `tests/` 71,919 LOC · 281 test files · 43 architecture-gate modules (**249 tests**, all passing — see §1.3) | `find … \| xargs wc -l` |
| Open PRs | 40+ open against `main` | `gh pr list --state open` |
| Board protocol | schema **2** · 102 claim records · 16 `queued` · 18 `expired` · 50 `done` | `.agents/board.json` |
| **Live leases** | **none** — no claim carries an unexpired lease | `agent_board.py show` (auto-released `task-living-core-p2b-wiring`) |
| **`gates_owner`** | **no current holder** | board scan: zero claims with `gates_owner: true` |

### 1.1 Governance findings that change how this mission must be executed

**G-1. `AGENTS.md` §rule 4 ("ONE GATES OWNER") is contradicted by an open PR.**
`AGENTS.md` on `main` mandates exactly one `gates_owner`. But **PR #211** —
*"governance: retire mandatory global gates_owner; keep board-integrity + referee guarantees
(READY_FOR_HUMAN_REVIEW)"* — is open and marked as awaiting a human decision. The board
**currently has no `gates_owner`**, which is consistent with #211's intent but not with
`AGENTS.md` as merged.

> **Consequence for this mission:** I will not self-appoint as gates owner, and I will not
> claim to have run "the full gates". Local diagnostic checks (`ruff check`, targeted
> `pytest`) are explicitly permitted for everyone by `AGENTS.md` §rule 4 and will be labelled
> as diagnostics, never as gate results. **This needs an owner ruling** (see §10, question Q1).

**G-2. `agent_board.py next` proposes stale work.** It currently returns
`feature-wiring-batch` (expired 2026-09-22, tied to PR#32 from September). The board's
`next_work` array (task-255 … task-264) is the live forward network; the `claims` array is
historical. Any plan built on `next` alone would be building on September's priorities.

**G-2b. The mandated non-overlap check currently cannot return a pass for anyone.** Measured, not
inferred. `agent_board.py check` was run on this wave's four changed paths, twice:

```
$ python scripts/agent_board.py check --files <4 doc paths> --branch arena/f6713d82-nexus-ai-agent
UNVERIFIABLE sources (will only matter if no conflict is proven):
  - open-PR branches: GITHUB_REPOSITORY unset and --repo not given (cannot enumerate PR branches)
check scope: local + sibling worktrees + origin/main + pushed open-PR branches — 2 source(s) read
UNVERIFIABLE: exactly one active, unexpired gates_owner is required; found 0 (zero or multiple is fail-closed)
EXIT_CODE=2

$ python scripts/agent_board.py check … --repo bot523h/nexus-ai-agent     # removes the first cause
check scope: … — 27 source(s) read
UNVERIFIABLE: exactly one active, unexpired gates_owner is required; found 0 (zero or multiple is fail-closed)
EXIT_CODE=2
```

Supplying `--repo` raised readable sources from 2 to 27 and cleared the first cause, but the
verdict stayed **exit 2** because of the second: the referee requires *exactly one* active,
unexpired `gates_owner` and there are **zero** (G-1). Per ADR 0007 and `AGENTS.md` §rule 2,
exit 2 is explicitly **not a pass**.

> **Measurement caution recorded from this very session.** Piping the referee into `tail`
> (`… check … | tail -6; echo EXIT=$?`) reported `EXIT_CODE=0`, because `$?` captured **`tail`'s**
> status, not the referee's. Re-run with the output redirected to a file, the true code was
> **2**. An exit-2 blocker was one shell pipe away from being recorded as a clean pass — which
> is precisely the failure mode ADR 0007 exists to prevent. **Never pipe `agent_board.py check`;
> redirect, then read `$?`.**

> **Consequence:** no agent on this repository can currently obtain a clean overlap verdict.
> This is a genuine governance blocker for the whole Arsenal programme, and it is the same root
> cause as G-1. It is resolved by an owner ruling on PR #211 (retire the mandatory global
> `gates_owner`) or by appointing one. **No file-level overlap was proven against my four
> documentation paths** — the check simply could not reach a verdict. I record that honestly
> rather than treating "no conflict proven" as "no conflict".

**G-2c. `agent_board.py show` mutates the board.** Running it auto-released an expired lease and
rewrote `.agents/board.json`:

```
[gc] auto-released expired leases: task-living-core-p2b-wiring
-  "updated_at": "2026-10-20T05:32:52Z"      -  "status": "active"
+  "updated_at": "2026-10-22T06:44:11Z"      +  "status": "expired"
                                            +  "release_reason": "auto-released by gc at
                                               2026-10-22T06:44:06Z (stale active lease)"
```

The GC is legitimate (a 24 h TTL lease from 2026-10-20 had expired), but it is a write to the
`coordination` zone performed by a *read-only* command. **I reverted it**
(`git checkout -- .agents/board.json`; the file is byte-identical to `origin/main` again) because
this wave makes no board edit and I hold no claim on that zone. Any agent running `show` should
expect this and decide deliberately whether to keep or revert the GC delta.

**G-3. There is no claimable task for this mission.** No task in `next_work` covers
candidate evaluation, the skill system, or an extension registry. Per `AGENTS.md` §rule 1 a
task must exist with `acceptance_criteria` and `evidence_required` before it can be claimed,
and `tests/unit/test_agent_board.py` fails the board if one appears without them. Registering
those tasks edits `.agents/board.json`, which sits in the **`coordination`** zone. Precedent
exists (PR #203 *"chore(board): make Task-256 claimable"*, PR #208 *"chore(board): register
task-269"*), but it is still a governed edit.

> **Consequence:** Wave A is delivered as documentation only. The board task entries are
> **proposed** in the companion plan (§4) with full `acceptance_criteria`,
> `evidence_required` and `exclusive_paths`, ready to register on owner authorization. I did
> not modify `.agents/board.json` unilaterally.

### 1.2 Open PRs that overlap this mission (must be reconciled, not ignored)

| PR | Title (abridged) | Why it matters here |
|---|---|---|
| **#243** | `feat(app-core): Principal identity, ChannelPort and telegram-coupling ratchet (F1 step 1)` | The mission's §4 requires "Principal/project permissions". That primitive is **in flight, not merged**. Any extension contract must be designed against #243's `Principal`, not invented beside it. |
| **#240** | `docs: app-first product frame (Telegram is one channel adapter)` | Matches the mission's "Telegram remains one adapter/surface". Doc-level, unmerged. |
| **#244** | `feat(storage): provider-neutral object-store contract + Neon Object Storage backend (task-272)` | Extension artifacts/credentials storage would land here. Overlaps Wave E. |
| **#211** | `governance: retire mandatory global gates_owner` | See G-1. |
| **#205** | `feat(nagar): Asset Library V1 — secure content-addressed ingest + durable recovery (task-266)` | The Asset Library is **already merged to `main`** (`migrations/versions/e575469d90b9_nagar_asset_library_v1.py`); #205 is follow-on. Confirms the output-contract target exists. |
| **#206** | `fix(passport): bind evidence to AttemptStaging identity (task-264)` | Provenance/evidence plumbing for any new capability's artifacts. |

### 1.3 Diagnostic test runs actually executed for this census

`AGENTS.md` §rule 4 permits local *diagnostic* checks for everyone while reserving "the full
gates" to a gates owner. These are diagnostics, labelled as such, and they are **not** a claim
that this branch is merge-ready.

Environment: a minimal venv (Python **3.11.2**, pytest **9.1.1**) with the package installed
`pip install -e . --no-deps` plus only the modules `tests/conftest.py` transitively imports
(`pydantic`, `pydantic-settings`, `httpx==0.28.1`, `sqlalchemy==2.0.54`, `sqlmodel==0.0.42`,
`aiosqlite`, `structlog`, `pytest-asyncio`, `numpy`, `pillow`, `typer`). The heavy runtime
dependencies (`chromadb`, `sentence-transformers`, `llama-cpp-python`, `litellm`, `boto3`, …)
were deliberately **not** installed, so this is not the CI dependency set.

```
tests/architecture/                                        249 passed in 11.31s
tests/unit/test_docs_integrity.py                           77 passed in 0.44s
tests/architecture/test_repo_truth_consistency.py            8 passed   (law R16, repo truth)
tests/unit/test_truth_doctor.py                             29 passed
six pack-relevant gates (manifest-is-data-only, pack_trust_boundary,
  pack_activation_completeness, rendering_lane_boundary,
  no_orphan_modules, port_signatures)                       41 passed
```

What this establishes:

- The boundary laws this plan depends on (**R7, R8, R9, R16, R20**, port signatures) are
  **live and green**, so §6's constraints are executable facts, not prose.
- The architecture suite is **43 modules / 249 tests**, correcting the file-count estimate
  (40) recorded in the earlier audit of the same date.
- `test_docs_integrity.py` passes **only after** this wave indexed its own documents. Before
  that fix it would have failed: the previously committed
  `EXTENSION_HUB_PROMPT_AUDIT_2026-10-22.md` was unindexed, which
  `test_every_document_is_indexed_in_docs_readme` rejects. **A real defect, introduced by this
  branch and repaired by this branch.**

Not run: the full unit/integration suites. They need the complete dependency set and, per
`AGENTS.md` §3, an editable install plus services that this sandbox does not provide. No
result is claimed for them.

---

## 2. Capability census — what actually exists

The mission asks to "identify duplicates, dead code, missing wiring and actual reachability".
Measured results:

### 2.1 Already implemented and reachable

| Capability | Where | Status |
|---|---|---|
| Capability registry (Domain ▸ Capability ▸ Operation) | `creative/studio/capabilities.py:160` `CapabilityRegistry` — *"the only operation allow-list"* | **VERIFIED**; rejects duplicate ops, requires `extra="forbid"` on every input schema, pins `execution_modes ⊆ {local, preview}` |
| Typed command bus | `creative/studio/bus.py:149` `CommandBus`, protocol `nagar.command.v1`, 399 LOC | **VERIFIED**; envelope validation, fingerprint/idempotency, revision + state hash, guarded apply, precondition checks |
| Capability packs | 6 shipped manifests, **52 declared capabilities** | **VERIFIED** (`nexus packs list/verify/activate`, `cli.py:512/567/618`) |
| Pack trust / signing | `creative/packs/{trust,verify,registry}.py`, Ed25519, 9 trust states, canonical signing bytes, revocation, ADR 0006 | **VERIFIED**; `scripts/pack_trust_mutations.py` proves the gate is killable |
| FFmpeg foundation | `creative/slideshow/ffmpeg.py:462` `resolve_ffmpeg_bin()` — `NEXUS_FFMPEG_BIN` → `PATH` → `imageio-ffmpeg` wheel, typed `FfmpegUnavailableError` | **VERIFIED**; `ffmpeg` appears in 94 files |
| Render lane (IR → compiler → executor) | `creative/rendering/{ir,compiler,executor}.py` | **VERIFIED**; law R9 keeps `executor.py` as the lane's single `subprocess` site |
| Local upscale | `creative/slideshow/upscale.py` — FFmpeg `scale=…:flags=lanczos`, capped 4×, lossless PNG, staging file | **VERIFIED**; `tests/unit/test_slideshow_upscale.py` runs real FFmpeg |
| Local image analysis | `creative/slideshow/analysis.py` — numpy/Pillow grayscale, `sharpness_of`, luma balance, aspect score, optional Gemini | **VERIFIED**; local path needs no API key |
| Local speech (ASR) | `adapters/whisper_local.py` (524 LOC) — `WhisperLocalCaptionEngine(CaptionEnginePort)`, lazy `importlib.import_module("faster_whisper")`, `is_available()`, `transcribe_sync`, **`diarize_sync`**, `ArgosLocalTranslator` | **VERIFIED and WIRED** — reachable from `creative/render_jobs.py:381` |
| Word-level alignment (native) | `adapters/whisper_local.py:74` `energy_anchors()` + `:134` `turns_from_anchors()` | **VERIFIED** — a native substitute for WhisperX's forced aligner |
| Caption pack | `creative/packs/caption/` — 10 capabilities, 3 hashed artifacts, ASS/RTL support (Wave 4b) | **VERIFIED**; one of only two packs with a *real* (non-placeholder) signature |
| OTIO export | `creative/packs/delivery/operations.py:300` `_export_otio()` — emits `Timeline.1` / `Stack.1` / `Track.1` / `Clip.1` JSON with exact rational frame math and `temporal_conversion` loss evidence | **VERIFIED_WITH_LIMITATIONS** — see F-2 |
| Asset Library V1 | `creative/assets/` — content-addressed blob store, measured digest/size, closed MIME allowlist, `AuthorizationSeam`, idempotent ingest, recovery | **VERIFIED**; law R13/R19 + `test_asset_library_boundary.py` |
| Provenance / evidence | `provenance/` — hash-chained `CausalJournal`, `ArtifactPassport` that **re-measures** artifacts, `verify_chain()` | **VERIFIED**; law R1b, D-0024 |
| Execution core | `execution/{contract,staging}.py`, `adapters/native_local_backend.py`, fenced attempts | **VERIFIED**; D-0020, I1–I10 mutation proofs |
| Optional extras + CI matrix | `pyproject.toml` extras `pdf`, `speech`, `translate`; `scripts/extras_matrix.py` binds each extra to a dedicated CI leg with a smoke test and **no `continue-on-error`** | **VERIFIED** |
| Dependency truth | `deptry` in `make lint` + CI lint job; `.github/DEPENDENCY_MATRIX.md` regenerated-and-compared | **VERIFIED** |
| Restricted shell sandbox | `tools/system_shell.py` — declared flag grammar (ADR 0011) + `tools/filesystem_policy.py` (`O_NOFOLLOW`, `dir_fd`, NUL/symlink rejection) | **VERIFIED**; `scripts/shell_sandbox_mutations.py` |

### 2.2 Genuine gaps (measured absence)

| Gap | Evidence of absence | Severity |
|---|---|---|
| **Scene / shot-boundary detection** | `grep -ri "scenedetect\|scene_detect"` over `src tests scripts pyproject.toml .github` → **0 files**. No `nexus.vision.scene` pack ships (only 6 packs exist). | **High** — and already designed, see §4 |
| **Skill system** | `find . -iname '*skill*' -o -iname '*prompt*'` over the whole repo → **1 hit, and it is my own audit doc from the previous turn.** There is no skills directory, no prompt-file catalogue, no skill manifest. | **High** — Wave C is greenfield |
| **OCR** | `grep -ri "tesseract\|pytesseract"` → **0 files** | Medium |
| **Extension registry for third-party code** | `grep -ril "extension_hub\|plugin_registry\|ExtensionHub"` → **0 files**. `trust_root.json` ships `"publishers": {}`. | Medium — deliberately fail-closed today |
| **Video-frame visual QA** (black/frozen-frame, continuity) | `slideshow/analysis.py` scores *stills*; no temporal frame analysis path | Medium |
| **Resource measurement at activation** | `hardware_requirements` is declared in all 6 manifests; `grep -ri "vram\|nvidia\|detect_gpu"` over `src/` → **0 files**. `psutil` is used only for self-monitoring RSS/disk (`agent/self_monitor.py`, `agent/self_vitals.py`). | Medium |
| **License/commercial-use gate on artifacts** | `ArtifactSpec.license` is `Field(min_length=1)`; `grep -n license creative/packs/verify.py` → **no license validation at all** | **High** — see F-4 |

### 2.3 Duplicates and dead code

- **No duplicate capability found** among the candidates and existing code, with one nuance:
  `moviepy` would duplicate the FFmpeg render lane that law R9 deliberately narrows to one
  subprocess site. Already rejected — see §5.
- **Dead/vestigial surface found: `ExportOtioInput.include_markers`** (see F-2). This is the
  only dead field the census surfaced in the candidate-adjacent code.
- **Reachability (law R20)** is enforced by `tests/architecture/test_no_orphan_modules.py`;
  its docstring records that 9 aspirational modules were **deleted** in the 2026-10-15 audit.
  Any new capability module must be wired or it fails CI. This is why §6 insists each wave
  ships a *reachable* path, not a library.

---

## 3. The four findings that should drive the plan

### F-1 · BLOCKER — OpenMontage is **AGPL-3.0**, not MIT-compatible

The mission lists OpenMontage as an optional integration candidate. Verified from the
license file itself (not just API metadata):

```
GET /repos/calesthio/OpenMontage/license
  license.spdx_id = AGPL-3.0
  decoded content begins:
    "GNU AFFERO GENERAL PUBLIC LICENSE / Version 3, 19 November 2007"
```

NEXUS is **MIT** (`LICENSE`). AGPL-3.0 is the *strongest* copyleft in common use: §13
extends the source-provision obligation to **network use**, so offering Nagar as a hosted
Creative Operating System while incorporating AGPL code would require releasing the service
source. The mission's own §3 states: *"Do not copy GPL/AGPL code or upstream skills into the
MIT-licensed core"* and *"An external process or adapter does not automatically settle all
legal implications."*

**Decision: `DEFERRED` — do not integrate, do not vendor, do not import skills from
OpenMontage while it is AGPL-3.0.** It may be studied for *ideas* (its 700+ agent skills are
a useful market signal) but no artifact may enter the repo. Adopting it would require an
explicit owner/legal ruling and, realistically, a separate process with no shared code and a
documented network-boundary argument. Even then AGPL §13 is not settled by process isolation.

Its metadata is otherwise strong (70,680 stars, pushed 2026-10-16, 415 open issues) — the
blocker is purely legal, and it is absolute.

### F-2 · `ExportOtioInput.include_markers` is a silent no-op — OTIO interop is untested

`creative/packs/delivery/models.py:146` declares `include_markers: bool = True`.
A scan of the whole `_export_otio` handler (`operations.py:300–440`) for `marker` returns
**zero matches**. Repo-wide, `Marker.1` and `OtioMarker` appear **nowhere** in `src/`, and
no test references `include_markers`.

Two consequences:

1. **A declared input flag does nothing.** The schema promises marker emission; the handler
   never emits one. Under this repo's own honesty standard (law R16, D-0012 *"Backup
   'success' must be measured … never asserted"*) that is a defect, not a TODO.
2. **The OTIO output has never been round-tripped through the real library.**
   `tests/unit/test_otio_interop.py` is named as an `exclusive_paths` entry in the board's
   `delivery-interop` and `delivery-interop-markers` zones — **but the file does not exist.**
   The OTIO JSON is produced by hand-rolled pydantic models (`OtioClip`, `OtioTrack`,
   `RationalTime.1`, `TimeRange.1`) with **zero dependency on the `opentimelineio` package**.
   Correct-looking schema tags are not proof a real NLE can open the file.

This is exactly board **task-121** (*"مدل OtioMarker (Marker.2 روی Stack، point→1frame، پالت
رنگ در metadata) + emission از timeline.markers + ۲ تست round-trip با OTIO واقعی"*), still
`available_sequenced_post_33`.

### F-3 · auto-editor's installable artifact is stale and its metadata is unverifiable

| Source | Version | Published | Files |
|---|---|---|---|
| PyPI `auto-editor` | **29.3.1** | (latest on PyPI) | 2 (`whl`, `tar.gz`) |
| GitHub releases | **31.7.2** | 2026-10-03 | 7 assets |

PyPI's newest release is **29.3.1** while upstream ships **31.7.x** — a two-major-version
gap, and PyPI has only 36 releases against a much longer GitHub release history. Worse for a
governed pipeline: the PyPI metadata reports `requires_dist = []`, i.e. **zero declared
dependencies**, for a package that plainly has them.

The mission's §4 requires "manifest validation and **dependency preflight**". A package whose
published metadata declares no dependencies **cannot be preflighted** — and installing the
PyPI artifact would pin Nagar to a stale major version.

**Decision: `DEFERRED`.** Rough-cut/silence-removal is a genuine product need, but it should
be met either by a native implementation over the existing FFmpeg resolver (`silencedetect`
+ segment/concat, which the render lane can already express) or by re-evaluating auto-editor
only if upstream republishes current versions to PyPI with real metadata. Note also that its
license is **Unlicense** (public-domain dedication) — permissive, but combined with the
`NOASSERTION`-style ambiguity of a Nim-primary repo shipping Python wheels, provenance
recording would need care.

### F-4 · The repo already rejected several of these candidates — with reasons that still hold

`docs/DECISION_LOG.md` (2,105 lines) contains prior verdicts. Re-litigating them silently
would be exactly the "archaeology churn" the repo hygiene report warns about. Verified
entries:

| Candidate | Prior verdict | Recorded reason | Still valid on 2026-10-22? |
|---|---|---|---|
| **librosa** | Deferred (`DECISION_LOG.md:21`, `:182`, `:184`) | *"librosa 1.0.0 requires Python ≥ 3.12 while the project supports ≥ 3.10 and brings numba/llvmlite/scikit-learn"* | **YES — measured today:** PyPI `librosa 1.0.0`, `requires_python = ">=3.12"`, 11 core deps incl. `numba>=0.61.0`, `numpy>=2.1.0`, `scipy>=1.15.0`, `scikit-learn>=1.6.0`. Repo floor is `>=3.10`. Beat detection is instead a numpy energy-flux estimator behind the same `BeatGrid` contract. |
| **Real-ESRGAN** | Deferred (`:21`, `:182`, `:184`, `:556–604`) | *"realesrgan 0.3.0 (2022) needs torch + basicsr + facexlib + gfpgan, and the ncnn route needs libomp/libvulkan"*; superseded by an optional FFmpeg Lanczos stage | **YES** — and upstream is now **stale**: last push **2024-08-06**, 649 open issues. Code is BSD-3-Clause but model weights carry separate terms. |
| **moviepy** | Rejected (`:202`) | *"adding a Python video library such as `imageio`/`moviepy` (a dependency for a job one process already does correctly)"* | **YES** — FFmpeg remains the single declared external binary; law R9 keeps one subprocess site. moviepy last pushed 2026-08-26, release v2.2.1 from 2025-05-21. |
| **WhisperX** | Excluded from the caption substrate (`:628`, `test_caption_substrate_boundary.py:61`) | *"no torch, whisperx, or pyannote imports"* in the pack | **YES — and stronger than recorded.** Measured today: whisperX 3.8.6 pulls **14 core deps** including `torch~=2.8.0`, `torchaudio~=2.8.0`, `torchvision~=0.23.0`, `triton`, `pyannote-audio>=4.0.0`, `pandas`, `nltk`. That is incompatible with local/offline-first and zero-cost. Meanwhile the native `energy_anchors()`/`turns_from_anchors()` already provide alignment + diarization. |
| **ComfyUI** | Not previously recorded | — | New evaluation: **GPL-3.0** code, 5,313 open issues. Treat as isolated per the mission; see §5. |
| **Blender** | Not previously recorded | — | New evaluation: license file states *"Blender uses the GNU General Public License … Apart from the GNU GPL, Blender is not available under other licenses."* GitHub API reports `NOASSERTION`. **GPL** — see §5. |

---

## 4. The decisive census insight: `scene.detect_shot_boundaries` is already specified

The mission's first implementation priority is scene analysis. The census found something
better than "a gap to fill":

`docs/NAGAR_70_OPERATIONS_TDD.md:321` already specifies the operation, inside a designed pack
that has **never shipped**:

```
Pack: nexus.vision.scene                       (TDD §1.4)
| scene.detect_shot_boundaries | ClipRef + threshold → ShotBoundarySet | histogram/embedding | A |
```

Ten operations are specified for `nexus.vision.scene`
(`segment_subject`, `remove_object`, `replace_sky`, `remove_background`, `track_object`,
`track_face`, `detect_shot_boundaries`, `find_subject_moment`, `remove_logo`,
`auto_reframe_subject`), and `nexus.vision.portrait` has a further set (`task-134` in the
board covers five of them). Neither pack exists in `src/`; the six shipped packs are
`audio.studio`, `language.caption`, `color.delivery`, `edit.timeline`, `motion.graphics`,
`slideshow.compose`.

So the ledger holds **68 specified operations**, of which **52 are declared** by shipped packs.
Adopting PySceneDetect is therefore **not** "adding a popular library" — it is *implementing an
already-specified, already-typed, Level-A operation in an already-designed pack*, which is the
repo's own documented extension recipe (`MODULE_MAP.md §5`, "Add a pack operation").

That reframing is what makes it a defensible Wave B rather than dependency creep.

---

## 5. Candidate evaluation matrix (all facts fetched 2026-10-22)

License = GitHub `license.spdx_id`; **Py floor** and **core deps** = PyPI JSON `info`.
"Core deps" excludes environment-marker-only extras where PyPI separates them.

| # | Candidate | License (verified) | Latest | Py floor | Core deps | Maintenance | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | **PySceneDetect** `scenedetect` | **BSD-3-Clause** | 0.7.2 (2026-10-12) | `>=3.10` ✅ | **5**: `click~=8.0`, `numpy`, `opencv-python`, `platformdirs`, `tqdm` | pushed 2026-10-22, 5,278★ | **ADOPT (conditional)** — permissive, floor-compatible, actively maintained, fills a *specified* op. Condition: the `opencv-python` dep must stay out of the packs/studio/render-lane boundary (§6). |
| 2 | **OpenTimelineIO** `opentimelineio` | **Apache-2.0** | 0.18.1 | `>=3.9` ✅ | **0** ✅ | pushed 2026-10-17, 2,033★, ASF project | **ADOPT (as a verification extra)** — zero core deps makes it the cheapest possible way to *prove* the hand-rolled OTIO output round-trips. Fixes F-2. |
| 3 | **faster-whisper** | MIT | 1.2.1 (2025-10-31) | `>=3.9` ✅ | 6: `ctranslate2<5`, `huggingface-hub`, `tokenizers`, `onnxruntime<2`, `av>=11`, `tqdm` | pushed 2026-10-16, 26,031★ | **ALREADY ADOPTED** — `[speech]` extra + `adapters/whisper_local.py`, wired at `render_jobs.py:381`, CI-matrix leg. Mission priority #3 ("fix missing wiring") is **already satisfied**; do not re-add. |
| 4 | **Kinocut** | **Apache-2.0** | v1.16.2 (2026-10-21) | — (repo primary lang Python, `default_branch=master`) | — | pushed 2026-10-21, 217★ | **DEFERRED (Wave D candidate)** — permissive license and *very* active, but it is an **MCP server** ("Guardrailed video editing MCP server for AI agents"). Adopting it means (a) adding MCP to the dependency graph and (b) executing an external process. Both are Wave D/E questions, not Wave B. Low star count = small bus factor. |
| 5 | **WhisperX** | BSD-2-Clause | 3.8.6 (2026-05-25) | `<3.14,>=3.10` | **14** incl. `torch~=2.8.0`, `torchaudio`, `torchvision`, `triton`, `pyannote-audio>=4.0.0`, `pandas`, `nltk` | pushed 2026-10-18, 24,770★ | **REJECT** — permissive license is not the issue; the torch/triton/pyannote tree is fundamentally incompatible with local/offline-first and zero-cost, and `test_caption_substrate_boundary.py` already bans it from the pack. Native `energy_anchors`/`turns_from_anchors` cover the need. Revisit only if a *separately installed, process-isolated* aligner is ever justified by measured quality loss. |
| 6 | **auto-editor** | Unlicense | PyPI **29.3.1** vs GitHub **31.7.2** | `>=3.9` | **0 declared (broken metadata)** | GitHub active (2026-10-22), 5,586★; **PyPI stale** | **DEFERRED** — see F-3. Unpreflightable + stale artifact. |
| 7 | **MoviePy** | MIT | v2.2.1 (2025-05-21) | none declared | 7 incl. `imageio`, `imageio_ffmpeg`, `pillow`, `numpy` | pushed 2026-08-26, 15,012★ | **REJECT** — already rejected in `DECISION_LOG.md:202`; duplicates the FFmpeg render lane and would pressure law R9's single-subprocess-site invariant. |
| 8 | **Editly** | MIT | — | — | — | **pushed 2025-05-12** (~17 months stale), TypeScript, 5,538★ | **REJECT** — introduces a second language runtime (Node) for a job FFmpeg already does. The mission itself flags "evaluate whether a second runtime is worth the maintenance burden"; measured staleness answers it. |
| 9 | **OpenCV** `opencv-python-headless` | **Apache-2.0** | 5.0.0.93 | `>=3.6` ✅ | **1**: `numpy` | pushed 2026-10-22, 91,282★ | **ADOPT ONLY TRANSITIVELY** — it arrives with `scenedetect`. Do **not** add it as a direct dependency: `cv2`/`opencv` are on the forbidden-import list in **9** architecture gates. Use the **headless** wheel (server-side, no GUI libs). |
| 10 | **Real-ESRGAN** | code **BSD-3-Clause**; weights separate | — | — | torch + basicsr + facexlib + gfpgan | **pushed 2024-08-06 (stale)**, 649 open issues, 37,172★ | **REJECT / keep optional-off** — already deferred (`DECISION_LOG.md:182`, `:556`); upstream now stale; FFmpeg Lanczos stage already ships and is tested with real FFmpeg. Model-weight licensing would need separate clearance even if adopted. |
| 11 | **Tesseract OCR** `pytesseract` | **Apache-2.0** | 0.3.13 | `>=3.8` ✅ | **2**: `packaging`, `Pillow` (both already present) | pushed 2026-10-22, 77,106★ | **DEFERRED to a named use case** — trivial Python footprint, but it needs the `tesseract` **system binary**, which becomes a second declared external binary beside FFmpeg. That is an architectural decision (ADR-worthy), not a casual extra. No current requirement names on-screen-text detection. |
| 12 | **librosa** | ISC | 1.0.0 (2026-08-11) | **`>=3.12`** ❌ | 11 incl. `numba`, `scipy`, `scikit-learn`, `soundfile`, `soxr` | pushed 2026-10-06, 8,699★ | **REJECT** — hard Python-floor conflict with `requires-python = ">=3.10"`. Already rejected; re-measured and confirmed (§3, F-4). |
| 13 | **ComfyUI** | **GPL-3.0** | — | — | large diffusion stack | pushed 2026-10-22, 141,714★, **5,313 open issues** | **DEFERRED — isolated only** — GPL-3.0 code cannot enter the MIT core. Permissible *only* as a separately deployed service that Nagar talks to over a network boundary, with no code vendoring, plus an owner/legal ruling on GPL obligations for the *client* side. Not before Waves B–C. |
| 14 | **Blender** | **GPL** (API: `NOASSERTION`) | — | — | — | pushed 2026-10-22, 21,141★ (mirror) | **DEFERRED — isolated only** — license file: *"Apart from the GNU GPL, Blender is not available under other licenses."* Same reasoning as ComfyUI. Note the API's `NOASSERTION` is itself a provenance signal: automated tooling cannot classify it, so any SBOM must record it manually. |
| 15 | **OpenMontage** | **AGPL-3.0** | no GitHub release | — | — | pushed 2026-10-16, 70,680★, 415 open issues | **BLOCKED** — see F-1. Strongest copyleft; §13 reaches hosted services. |

### 5.1 License-compatibility summary against the MIT core

```
Permissive — safe to incorporate with attribution:
  Apache-2.0  OpenTimelineIO, OpenCV, Tesseract, Kinocut
  BSD-3       PySceneDetect, Real-ESRGAN (code only; weights separate)
  BSD-2       WhisperX
  MIT         faster-whisper (already in), MoviePy
  ISC         librosa
  Unlicense   auto-editor (public-domain dedication)

Copyleft — MUST NOT enter the MIT core:
  GPL-3.0     ComfyUI          → isolated service only, owner ruling required
  GPL         Blender          → isolated service only, owner ruling required
  AGPL-3.0    OpenMontage      → BLOCKED (network copyleft; §13)
```

Attribution obligations for the permissive set are real but cheap: Apache-2.0 requires
preserving `NOTICE`/attribution and stating changes; BSD/MIT require retaining the copyright
notice. Both are satisfiable with a `THIRD_PARTY_NOTICES` record per adopted component
(proposed in the companion plan, Wave B). No permissive candidate here triggers a
source-disclosure obligation for a hosted service.

---

## 6. Boundary-law constraints that shape any adoption

These are not opinions; each is an executable gate that will fail CI.

| Law | Constraint | Effect on this mission |
|---|---|---|
| **R7** | A pack may import only stdlib, pydantic, `creative.studio`, `creative.packs`. Enforced by **allowlist**: `ALLOWED_PACK_TOP_LEVEL` = 14 modules in `test_audio_pack_boundary.py` / `test_edit_pack_boundary.py` — `subprocess`, `socket`, `ctypes`, `cv2` are **not** in it. | `scenedetect`/`cv2` can **never** be imported inside `creative/packs/**`. Scene detection must live in an **adapter** (`adapters/`) behind a port, exactly as `whisper_local.py` does for `faster_whisper`. |
| **R9** | The render lane has **exactly one** `subprocess` site (`rendering/executor.py`), never `shell=True`, no spawning from packs. | Any external-tool invocation either extends that one site or becomes a new, separately legislated site with its own ADR + gate. This is the single hardest constraint on Wave D. |
| **R6** | The studio core imports only stdlib + pydantic + itself; its default registry is **exactly** the Wave-1 catalogue. | New operations register through the documented composition path (`packs/runtime.py::COMPOSITION`), never by widening the frozen Wave-1 catalogue. |
| **R8** | A pack's declared capabilities must equal the operations its registrar actually adds. | Adding `scene.detect_shot_boundaries` requires manifest + registrar + gate to move together. |
| **R20** | Every production module must be reachable (imported by `src`/`tests`/`scripts`/`migrations`) or be a declared entry point. | No "foundation for later" modules. Each wave must ship a **wired** path. |
| **R4** | No Celery/Redis, no distributed dispatch; compose has exactly `{bot, dashboard}`. | Rules out a container-per-extension runtime in the near term; Wave E must be in-process-isolated or a documented external service. |
| **R1/R5** | `domain/` and `application/ports/` never import adapters; ports keep typed async surfaces; destructive ops take an `idempotency_key`. | The scene-detection seam must be a `Protocol` in `application/ports/` + adapter in `adapters/`, wired only from a composition root. |
| **R16** | One repo truth: version lock-step, docs index/link integrity, boundary-law→test resolution. | Every new doc must be indexed in `docs/README.md`; every new law must name its test in `MODULE_MAP.md §3`. |
| **`deptry` + `extras_matrix.py`** | Every import must be a declared dependency; every shipping extra must have a dedicated CI leg with a smoke test and **no `continue-on-error`**; `.github/DEPENDENCY_MATRIX.md` is regenerated and compared. | A new extra (e.g. `[scene]`) is not a one-line `pyproject` edit — it is extra + CI leg + smoke test + regenerated matrix, all in one PR. |

---

## 7. Free/local-first and honest-cost classification (mission §7)

Applying the mission's own taxonomy to the adopted/shortlisted set, measured:

| Component | Class | Cost truth |
|---|---|---|
| FFmpeg (existing) | Local processing, no provider charge | Free software; requires the binary. Resolver already degrades with typed `FfmpegUnavailableError`. |
| PySceneDetect + OpenCV-headless | Local processing, no provider charge | Free software; CPU cost only. **Not** "free" in the hardware sense — scene detection over a long interview is CPU-bound and must carry a timeout + resource bound. |
| OpenTimelineIO | Local, in-process | Zero deps, negligible footprint. |
| faster-whisper | Local **with a model download** | Free software, but `whisper-small-int8` weights must be fetched; the caption manifest already declares it as an artifact with `download_size_mb`. Honest status = *free, but requires a model download and RAM*. |
| Gemini image analysis (existing, opt-in) | **Paid provider** | Must stay opt-in; the local `analysis.py` path already works without a key. |
| Kinocut / ComfyUI / Blender (if ever) | External process/service | Cost depends on deployment; unknown until measured. Must be labelled `unknown`, never `free`. |

**Policy note the census proves is already honoured:** missing GPU/model/credentials does not
prevent startup — `whisper_local.is_available()` + the typed-unavailable adapter pattern
(`creative/caption/unavailable_adapter.py`) and `resolve_ffmpeg_bin()` both degrade instead of
crashing. Any new capability must copy that pattern rather than invent one.

---

## 8. What this census changes about the mission's assumptions

The mission is well-constructed, but four of its premises do not survive contact with the tree:

1. **"Inspect the existing faster-whisper optional extra and speech path; fix missing wiring or
   evidence gaps before adding another package."** — Inspected. There is **no missing wiring**:
   the adapter is lazy-imported, wired from `render_jobs.py:381`, exercised by
   `test_caption_engine_adapters.py` and `test_optional_extras.py` (which asserts it is
   *absent* on the core leg and *real-imported* on the speech leg). Priority #3 is **already
   done**; the remaining work is diarization *quality* measurement, not wiring.
2. **"Evaluate OpenTimelineIO against Nagar's existing timeline model."** — Nagar does not
   merely have a timeline model; it has a **hand-rolled OTIO serializer with zero OTIO
   dependency and zero interop test** (F-2). The evaluation is therefore not "adopt vs not"
   but "adopt as a *verification* dependency to prove the output we already emit is valid".
   That is a materially different and much cheaper decision.
3. **"Audit all existing skills."** — There are none (F: §2.2). Wave C is greenfield, which is
   good news (no duplicate prompt files to reconcile) but it means the "audit first" step has
   a null result and the effort estimate shifts to design.
4. **"Integrate or improve PySceneDetect only where current tools are insufficient."** —
   Current tools are not merely insufficient, they are **absent** (0 files). And the operation
   is already specified in the TDD ledger (§4). This is the clearest verified gap in the whole
   shortlist.

---

## 9. Reproduction commands

```bash
# live repo truth
git ls-remote origin refs/heads/main
git rev-parse HEAD && git branch --show-current && git status --porcelain
gh pr list --state open --limit 40
python scripts/agent_board.py show

# census
grep -ril "scenedetect|scene_detect|tesseract|esrgan|comfyui|blender|openmontage|kinocut|editly|auto_editor" \
     --include=*.py --include=*.toml --include=*.json --include=*.yml --include=*.md src tests scripts pyproject.toml .github
find . -path ./.git -prune -o \( -iname '*skill*' -o -iname '*prompt*' \) -print
python3 -c "import json;m=[json.load(open(p)) for p in __import__('glob').glob('src/nexus_ai_agent/creative/packs/*/pack.manifest.json')];print(sum(len(x['capabilities']) for x in m))"

# upstream truth (license / release / floor / deps)
curl -sS https://api.github.com/repos/<owner>/<repo>
curl -sS https://api.github.com/repos/<owner>/<repo>/license      # decode base64 'content'
curl -sS https://api.github.com/repos/<owner>/<repo>/releases/latest
curl -sS https://pypi.org/pypi/<dist>/json                        # info.requires_python, info.requires_dist
```

---

## 10. Owner decisions required before Wave B starts

| # | Question | Why it blocks |
|---|---|---|
| **Q1** | Is PR #211 (retire mandatory `gates_owner`) accepted? Who runs full gates on main-bound work in the meantime? | `AGENTS.md` and the live board disagree (G-1). I will not self-appoint. |
| **Q2** | Authorize registering new board tasks for this mission (edits `.agents/board.json`, `coordination` zone)? | Without a claimable task, `AGENTS.md` §rule 1 forbids coding (G-3). Entries are drafted in the companion plan §4. |
| **Q3** | Confirm OpenMontage is **out** on AGPL-3.0 grounds (F-1), or state the legal theory under which it is in. | The mission names it as a candidate; the license is absolute. |
| **Q4** | Approve `[scene]` as a new optional extra (scenedetect + opencv-python-headless), given it needs a CI matrix leg + smoke test + regenerated `DEPENDENCY_MATRIX.md`. | Dependency-graph change; `deptry`/`extras_matrix` make it a multi-file PR. |
| **Q5** | Approve `opentimelineio` as a **dev/verification-only** dependency (0 core deps) to round-trip-test the existing export, and decide whether `include_markers` should be implemented (task-121) or **removed** as a dead flag. | F-2 is a live defect either way; the two fixes are opposite. |
| **Q6** | Wave C skill system: is a new top-level zone (`skills/`) acceptable, and does a skill manifest become a **third** schema beside `nexus.capability-pack.v1` and `nexus.pack-trust-root.v1`, or a section of the pack manifest? | The prior audit ([EXTENSION_HUB_PROMPT_AUDIT_2026-10-22.md](EXTENSION_HUB_PROMPT_AUDIT_2026-10-22.md)) showed schema proliferation is this repo's main drift risk. |

---

## 11. Status vocabulary used in this document

Per the mission's §11 requirement, and honestly applied:

- **VERIFIED** — I read the code/config and it does what is claimed; where behaviour matters,
  a named test exercises it.
- **VERIFIED_WITH_LIMITATIONS** — real and reachable, with a measured caveat (e.g. OTIO export
  works but is never round-tripped and ignores `include_markers`).
- **BLOCKED** — cannot proceed for a hard external reason (AGPL-3.0 vs MIT core).
- **DEFERRED** — proceedable in principle, deliberately sequenced later or awaiting a decision.

No claim in this document is marked VERIFIED on the strength of documentation alone. Where I
did **not** execute something, I say so: no test suite was run for this census (see the
companion plan §6 for the diagnostic commands I am authorized to run and their results).
