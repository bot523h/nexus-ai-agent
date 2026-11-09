# LIVE TRUTH REPORT — NAGAR Creative Capability Composition Runtime

> **Gate 0 deliverable.** Recorded *before* any code change, from the live workspace.
> Every SHA, path and command output below was produced in this session.
> Author: Arena (branch `arena/da6837a6-nexus-ai-agent`).

---

## 1. Exact identity of this session

| Field | Value |
|---|---|
| Working directory | `/home/user/nexus-ai-agent` |
| Git top level | `/home/user/nexus-ai-agent` |
| Branch | `arena/da6837a6-nexus-ai-agent` |
| HEAD SHA | `5ed6d4e10be55f5baf5e9f2a8aa99775577621db` |
| `origin/main` SHA | `5ed6d4e10be55f5baf5e9f2a8aa99775577621db` (identical to HEAD) |
| Base SHA of this delivery | `5ed6d4e10be55f5baf5e9f2a8aa99775577621db` |
| Remote | `https://github.com/bot523h/nexus-ai-agent.git` |
| Clone depth at session start | **shallow** (`.git/shallow` present, grafted at `5ed6d4e`) → **unshallowed during this session** (`git fetch --unshallow`, now `FULL`) |

Commands run (verbatim, session start):

```
pwd                                   -> /home/user/nexus-ai-agent
git rev-parse --show-toplevel         -> /home/user/nexus-ai-agent
git branch --show-current             -> arena/da6837a6-nexus-ai-agent
git rev-parse HEAD                    -> 5ed6d4e10be55f5baf5e9f2a8aa99775577621db
git status --short --branch            -> ## arena/da6837a6-nexus-ai-agent   (clean)
git log -10 --oneline --decorate       -> 5ed6d4e (grafted, HEAD -> arena/..., origin/main, origin/HEAD, main)
                                          Merge pull request #322 from bot523h/nagar-one-handoff-report
git remote -v                         -> origin https://github.com/bot523h/nexus-ai-agent.git (fetch/push)
git diff --stat                       -> (empty)
git diff --cached --stat              -> (empty)
git diff --check                      -> (empty)
git ls-files --others --exclude-standard -> (empty)
```

**Working tree at session start: completely clean.** No uncommitted changes, no untracked
files, no media artifacts. Every artefact the previous agent produced lives **only** on
its remote branch (§3).

---

## 2. Commit history (live)

`git log -10 --oneline --decorate` at session start returned exactly **one** commit
(the clone was grafted). After `git fetch --unshallow` the real history is available:

```
5ed6d4e  Merge pull request #322 from bot523h/nagar-one-handoff-report   (2026-11-08 09:10:51 +0300)
```

`origin/main` and `main` point at the same SHA. The session branch was created from it.

---

## 3. Recovery of the previous work (the "Grok" session)

The handoff brief named branch `trae/agent-CCO4Fr` with reported HEAD `ac9c16d6`.
Live verification:

```
git ls-remote --heads origin | grep trae
  -> b7dd454e6f46f199276b710ccd605801f2b1a47a  refs/heads/trae/agent-CCO4Fr
```

* The reported HEAD `ac9c16d6` is **not** the branch tip. The real tip is **`b7dd454`**.
* `ac9c16d6` **does** exist as the second commit of that branch
  (`chore(board): claim task-266 nagar-creative-composition zone`).
* Commit dates on the branch are **2026-11-09 09:17 / 09:31 / 09:42 UTC** — i.e. the
  previous session ended **~15 minutes before this session started**.

```
git merge-base origin/main origin/trae/agent-CCO4Fr -> cf8b7f665d8dc3ec94d52b3ea337ef2491961f33
git rev-list --count origin/main..origin/trae/agent-CCO4Fr -> 7
git rev-list --count origin/trae/agent-CCO4Fr..origin/main -> 624
```

So the previous branch is **624 commits behind `main`** and carries only 7 commits that
`main` does not have. It was **never merged**.

### 3.1 Files that exist ONLY on `origin/trae/agent-CCO4Fr`

```
src/nexus_ai_agent/creative/composition/__init__.py        (79 lines)
src/nexus_ai_agent/creative/composition/bench.py           (148)
src/nexus_ai_agent/creative/composition/canonical.py       (309)
src/nexus_ai_agent/creative/composition/canonical_job.py   (202)
src/nexus_ai_agent/creative/composition/catalog.py         (160)
src/nexus_ai_agent/creative/composition/compiler.py        (466)
src/nexus_ai_agent/creative/composition/proof.py           (371)
src/nexus_ai_agent/creative/composition/runtime.py         (389)
src/nexus_ai_agent/creative/composition/spec.py            (332)
src/nexus_ai_agent/creative/composition/workers.py         (363)
tests/unit/test_composition_runtime.py                      (343)
scripts/bench_composition.py
docs/architecture/adr/0013-creative-capability-composition.md
docs/architecture/COMPOSITION_THREAT_MODEL.md
docs/architecture/NAGAR_COMPOSITION_RUNTIME_REPORT.md
docs/ops/COMPOSITION_RUNTIME.md
```

Total ≈ 3 162 lines of Python across the package + the single test module.

Content hashes of the recovered files (sha256, first 16 hex) taken from the git object
store (the files are **not** in the working tree, so no artefact was lost):

```
src/nexus_ai_agent/creative/composition/compiler.py   a33434145f6db736
src/nexus_ai_agent/creative/composition/runtime.py     cb4219aae30f1afc
tests/unit/test_composition_runtime.py                 03f135a30aa702fd
origin/trae/agent-CCO4Fr^{tree}                        6e7c5563d61557fe24b81e6edfae6c6496d4af49
```

A full copy was extracted read-only to `/tmp/prevwork/` for study; the git objects are
the durable source of truth and were **not** modified, rebased, or deleted.

### 3.2 What the recovered code actually is (honest assessment)

The recovered prototype is **well documented and internally honest** — its own
`catalog.py` docstring states:

> *"These are therefore *composition-local* operations: they are **not yet registered**
> as first-class operations in the live NEXUS registry."*

That is precisely the architectural defect this mission was called to repair:

| Defect in the recovered prototype | Evidence |
|---|---|
| **Parallel capability registry** | `catalog.py::builtin_capabilities()` returns a private `dict[str, CapabilitySpec]` of `nagar.*` ids that the live NEXUS registry does not know. `canonical.py::build_snapshot()` wraps *that* dict in a `RegistrySnapshot`. The live registry is only consulted to *reject* claims (`validate_capability`), never to *resolve* capabilities. |
| **Parallel execution path** | `runtime.py::CompositionRuntime.execute()` runs workers itself; `canonical_job.py::composition_render_job` is a bare `async def` that is **not registered** in `worker.py::default_job_handlers()`, so no queue ever dispatches it. |
| **Parallel permission model** | `RuntimePolicy` (`allow_network`, `allow_commercial`, `quota`, `allow_experimental`) is a composition-local policy object, not the canonical `ProjectAccess` / `PermissionLevel` ladder. |
| **Parallel provenance** | `proof.py::ProofCarryingArtifact` is a composition-local evidence record, not the canonical `ArtifactPassport` / `provenance.journal`. |
| **Own process boundary** | `workers.py::_run()` spawns FFmpeg/ImageMagick directly with its own env allow-list, instead of going through `creative/slideshow/ffmpeg.py` (`resolve_ffmpeg_bin`, `upscale_image`, `encode`, `probe_video`). |
| **Wrong ADR number** | `docs/architecture/adr/0013-creative-capability-composition.md` collides with `main`'s existing `0013-three-layer-operation-truth.md`. The trae branch **deletes** main's ADR 0013 to make room — a merge hazard that must not be carried over. |
| **Board damage** | The trae branch's board diff removes large parts of `main`'s `.agents/board.json` (zones `cognition-quality-evaluation`, `pr-repair-campaign`, …) because it was based on a 624-commit-old `main`. |

**Recovery verdict:** the prototype is a *design* artefact, not a delivery. It is
reused as a source of ideas (typed media contract, DAG compiler, proof-carrying
artifact, semantic verifier) and re-implemented against the canonical substrate.

---

## 4. Governance handoff (task-266)

`AGENTS.md`, `.agents/board.json` and `scripts/agent_board.py` were read in full.

### 4.1 The previous claim

The previous agent claimed, **on its own branch only**:

```
task: task-266-nagar-composition-hardening
zone: nagar-creative-composition
branch: trae/agent-CCO4Fr
claimed_at: 2026-11-09T09:31:16Z   (24 h TTL)
```

Live state on the **authoritative** board (`origin/main:.agents/board.json`, schema 2):

* the task id `task-266-nagar-composition-hardening` **does not exist**;
* the zone id `nagar-creative-composition` **does not exist**;
* the claim was **never merged to `main`**, therefore it was never visible to any other
  sandbox and never fenced anything.

Per `AGENTS.md` §4 *"The single authoritative coordination board is `.agents/board.json`
on `main`."* — there is **no live lease over the composition scope in the authoritative
board**, and `agent_board.py check` (which consults the local board + sibling
worktrees + `origin/main` + every pushed open-PR head) is the authoritative overlap
verdict. No open PR exists for `trae/agent-CCO4Fr` (44 open PRs enumerated via
`gh pr list`; none on that branch), so that branch is not a lease-bearing source either.

**No manual board edit was used to bypass the claim.** A new, independent task and zone
were registered through `scripts/agent_board.py` (§4.3).

### 4.2 Other agents' active claims (live)

```
$ python3 scripts/agent_board.py show
[gc] auto-released expired leases: task-282-slice-2-artifact-assetversion-binding
```

Exactly **one** lease was live at session start
(`task-282-slice-2-artifact-assetversion-binding`, zone `durable-project-state-lifecycle`,
branch `nagar-one-slice2-artifact-revision-binding`, claimed 2026-11-06) and it **expired
before this session's work began** — the board's own `gc` released it. Its exclusive
paths (`src/nexus_ai_agent/creative/project_state/`, `src/nexus_ai_agent/storage/models.py`,
`migrations/versions/`, `src/nexus_ai_agent/cli.py`, …) are **not touched** by this
delivery.

### 4.3 This session's registration

See §9 for the exact commands and their exit codes.

---

## 5. File inventory (main @ `5ed6d4e`)

Canonical substrate discovered and audited (full table in
`CANONICAL_INFRASTRUCTURE_AUDIT.md`):

```
src/nexus_ai_agent/creative/packs/runtime.py          build_runtime_registry() — THE registry
src/nexus_ai_agent/creative/packs/registry.py          PackRegistry, activation, trust
src/nexus_ai_agent/creative/packs/manifest.py          CapabilityPackManifest
src/nexus_ai_agent/creative/packs/trust.py             Ed25519 pack trust
src/nexus_ai_agent/creative/studio/capabilities.py     CapabilityRegistry, OperationSpec
src/nexus_ai_agent/creative/studio/bus.py              CommandBus (7-stage dispatch)
src/nexus_ai_agent/creative/studio/authorization.py    ProjectAccess / ProjectAuthorizer
src/nexus_ai_agent/creative/studio/lifecycle.py        PACK_LIFECYCLE (fail-closed gate)
src/nexus_ai_agent/creative/render_jobs.py             creative_render_job (canonical worker)
src/nexus_ai_agent/worker.py                           default_job_handlers() — worker registry
src/nexus_ai_agent/adapters/in_process_job_queue.py    InProcessJobQueue (the only queue)
src/nexus_ai_agent/application/ports/job_queue.py      JobQueuePort protocol
src/nexus_ai_agent/jobs/lifecycle.py                   job state machine
src/nexus_ai_agent/jobs/verification.py                artifact verifier contract
src/nexus_ai_agent/provenance/passport.py              ArtifactPassport / PassportBuilder
src/nexus_ai_agent/creative/slideshow/ffmpeg.py        resolve_ffmpeg_bin / encode / upscale_image
src/nexus_ai_agent/creative/slideshow/probe.py         probe_image / probe_audio
src/nexus_ai_agent/creative/rendering/                 the apply lane (IR/compiler/executor)
src/nexus_ai_agent/nagar/creative/handoff.py           the canonical durable handoff pattern
```

**`src/nexus_ai_agent/creative/composition/` does NOT exist on `main`** — it exists only
on the abandoned `trae/agent-CCO4Fr` branch.

---

## 6. Environment

| Item | Value |
|---|---|
| OS | Debian GNU/Linux 12 (bookworm) |
| Kernel | `Linux 6.1.158+ #1 SMP PREEMPT_DYNAMIC` |
| CPU | Intel Xeon @ 2.60 GHz, **2 vCPU** |
| RAM | 3 939 MB total (~3 688 MB available) |
| Disk | `/` 21 GB total, 13 GB free at session start |
| GPU | **none** — no `nvidia-smi`, no `/dev/dri` |
| Python | **3.11.2** (system) — repo requires `>=3.10`; `mypy` config targets `3.12` |
| Virtualenv | `.venv` (Python 3.11.2) created in-session |
| Package install | `pip install -e ".[dev]"` — the documented path in `AGENTS.md` §3 |

## 7. Available media tools

| Tool | Status at session start | Note |
|---|---|---|
| `ffmpeg` / `ffprobe` on `PATH` | **ABSENT** | `command not found` |
| `imageio-ffmpeg` wheel | declared in `[dev]` extras → the canonical resolver's **fallback** (`resolve_ffmpeg_bin`) | installed by `pip install -e ".[dev]"`; this becomes the real encoder used by the render lane |
| ImageMagick | **PRESENT** — `ImageMagick 6.9.11-60 Q16 x86_64 2021-01-25` | delegates: `jpeg png tiff webp heic …`; licence link `https://imagemagick.org/script/license.php` |

**Consequence for the previous session's claim of "a real H.264 MP4 at 1280×720":**
there is no system FFmpeg in this environment. The only honest FFmpeg source is the
`imageio-ffmpeg` static wheel that the repository itself declares as the dev/test
convenion. Every FFmpeg fact recorded in this delivery (build configuration, enabled
encoders, `libx264` presence, licence) is measured from **that exact binary**, not
assumed.

## 8. Hardware / isolation capability

| Capability | State | Consequence |
|---|---|---|
| Linux namespaces (`unshare`) | not exercised in this delivery | **not claimed** |
| cgroups v2 write access | not exercised | **not claimed** |
| seccomp filters | not exercised | **not claimed** |
| user separation (uid split) | not exercised | **not claimed** |
| Network egress control | not exercised | **not claimed** |

The sandbox is a **2 vCPU / 4 GB** container without GPU. Any OS-level isolation claim
would be unproven; the delivery therefore declares process-level isolation only and
labels the OS-level items **BLOCKED (not exercised)** rather than asserting them.

---

## 9. Unknowns and honest gaps

| # | Item | State |
|---|---|---|
| U1 | FFmpeg build configuration / enabled encoders / `libx264` | measured in this session from the resolved binary; see the License Ledger |
| U2 | Whether the previous agent's 13 tests ever passed | **NOT VERIFIED** — the test file was never executed against `main`; it referenced `catalog.builtin_capabilities()` ids that do not exist in the canonical registry |
| U3 | Whether the previous agent ever produced a real MP4 | **NOT VERIFIED** — no media artefact exists in the branch tree (`git ls-tree` shows no `.mp4`/`.png` blob) |
| U4 | Previous session's *intent* behind `canonical.py` / `canonical_job.py` | recovered from code + docstrings only; the session transcript is unavailable |
| U5 | Whether `nagar.*` capability ids were intended as permanent public ids or placeholders | the docstrings call them "composition-local"; treated as placeholders and replaced by canonical operation ids |
| U6 | OS-level sandboxing (namespaces/cgroups/seccomp) | **BLOCKED — not exercised**; explicitly out of the verified surface |

---

## 10. Deviations from the handoff brief

| Brief said | Live reality | Action |
|---|---|---|
| branch `trae/agent-CCO4Fr` HEAD `ac9c16d6` | tip is `b7dd454`; `ac9c16d6` is an ancestor | recorded; branch fetched read-only |
| prototype commit `05db0c29` | exists on the branch | used as the recovery source |
| remote main `5ed6d4e` | confirmed exactly | base SHA = `5ed6d4e10be55f5baf5e9f2a8aa99775577621db` |
| "13 tests passed" | unverifiable | re-derived from scratch; no prior test was trusted |
| "a real H.264 MP4 at 1280×720 was produced" | no media artefact exists in the branch | re-produced and re-measured in this session |
| ADR `0013-creative-capability-composition` | `0013` is taken on `main` by `0013-three-layer-operation-truth.md` | renumbered to **`0014`**; main's ADR 0013 is untouched |
| task `task-266-nagar-composition-hardening` | not on the authoritative board | new independent task registered through the board CLI (no manual board edit) |
| files "already created" listed under `src/nexus_ai_agent/creative/composition/` | they exist **only** on the abandoned branch | recovered from git objects; re-implemented against the canonical substrate |
