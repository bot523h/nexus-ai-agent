# Release truth — Agent C audit, 2026-09-25 UTC

Status: **targeted repairs validated; release/dependency/container follow-ups remain OPEN**.
This is a dated evidence record, not a claim that 3.13.0 has been published.
Owner: `arena/01a0da13-nexus-ai-agent`. Baseline and live main before mutation:
`2351f099e2f789b80de05c6b382554b5cdcec0f3` (GitHub commits/main API).
Initial working tree was clean; checkout was shallow with no local tags. Fetched
full history and tags before drawing lineage/PR conclusions. Board `show`/`next`
auto-expired leases (three field changes); retained the CLI's recorded expiry.

## RELEASE_TRUTH / drift decisions

Each row is SOURCE_A → SOURCE_B → CONFLICT → CANONICAL SOURCE → REQUIRED ACTION.

| SOURCE_A | SOURCE_B | CONFLICT / classification | CANONICAL SOURCE | REQUIRED ACTION |
|---|---|---|---|---|
| README current banner 3.12.0 | VERSION 3.13.0 | Actual presentation defect | VERSION + packaging/changelog lockstep | Banner corrected; regression guard added |
| VERSION 3.13.0 | pyproject + newest CHANGELOG heading 3.13.0 | No conflict | Existing lockstep contract | Preserve static metadata, no bump |
| VERSION 3.13.0 | newest tag v3.5.0 | Uncut release, explicitly OPEN in CI | Git refs + release-lineage contract | Document; do not manufacture tags |
| newest tag v3.5.0 | newest published release v3.3.0 | Tag and release are distinct records | GitHub API + refs | Preserve; owner decides release cut |
| PR #33 body: minimal core/multi-stage delivered on its branch | main pyproject/Dockerfile | Unmerged proposal, NOT main's state | Baseline tree | Preserve pending work; do not close as duplicate |
| RAG error advertises `[rag]` | pyproject has no rag extra | Real actionable-hint defect | Current declarations/imports | Defer coordinated extra + guard + CI migration; no fake extra alias |
| `.dockerignore` excludes only root `.env` | Dockerfile `COPY . .` | Environment variants/nested secrets could enter image | Docker context rules | Exclude `.env.*` and nested `.env*`, retain example templates |
| Dependency matrix says per-run freeze proves reproducibility | no lockfile / mutable base image | Freeze is evidence, not a complete replay guarantee | Resolver/build inputs | Record limitation, do not invent a lockfile |
| release helper maps any gh failure to absent release | transport/auth failures are not absence | Latent false diagnosis | API status needed | Defer tri-state API handling; never follow its delete-tag suggestion automatically |

## VERSION_POLICY

Repository **bump authority is VERSION**, enforced against static
`[project].version` and the newest numeric CHANGELOG heading. Installed runtime
uses distribution metadata first, then VERSION for an uninstalled checkout
(`bot/update_handlers.py`, read-only audit). VERSION is not automatically fed to
setuptools; pyproject is the actual wheel metadata input. Do not confuse these
roles. Preserve the existing manual bump procedure in
`docs/ops/RUNBOOK_HARDENING.md` and additionally sync the README current banner.
`test_readme_version_lockstep` now enforces that banner in the normal test job.
Historical feature headings such as README's v3.12.0 section remain historical.
No generated README or dynamic packaging migration is necessary for this defect.

History: `4af957f` 3.6.0 → `e4df153` 3.7.0 → `c0b9d9b` 3.8.0 →
`7fabea2` 3.9.0 → `bab27e1` 3.10.0 → `d14795e` 3.11.0 →
`92dcb47` 3.12.0 → `c25f7fe` 3.13.0. Numeric progression is valid.
This does **not** certify API compatibility across every intervening feature.
Commit subjects calling work a release are not evidence of publication.
No evidence proves that every skipped historical version was intentionally
unpublished; intent is unknown. What IS deliberate is the current CI policy that
an absent current-version tag is OPEN, not failing and not auto-created.

A future approved cut should tag the audited commit with `v<VERSION>`, verify
metadata/ancestry/CI, then publish release notes/assets. Never retag historical
versions merely to align the latest label. No tag/release mutation in this audit.

## TAG_ANALYSIS

Live remote tags before mutation (GitHub API, subsequently fetched):

| Tag | Commit |
|---|---|
| v3.5.0 | c42cccbe32e3c6642e392f69015d9b0af03c66fb |
| v3.3.0 | 2f018882f80f971dd54430a706ac1799da661de5 |
| v3.2.0 | 40a323de2e893c6839e21bcdb8846ee319c8c1fd |
| v3.1.0 | 635d12863346b449e830dacdbc913c5babaaf66a |
| v3.0.0 | 50beed8ee9e9d0644ced85c72cc533ccf89962c6 |
| v2.0.0 | 2cd997b0e96ab0446d671938d1fc2e3f5dc09dfa |
| v1.3.0 | 6be40ea4bea41923eb8bfccef9e0ec15a52549e8 |
| v1.2.0 | de0302a53dc4fdb823973fdff5f0de843ead0175 |
| v0.2.0-D | a12690a3a79734397e8fd646ce92b054987c038e |

`git merge-base --is-ancestor v3.5.0 2351f09` exits 0;
`git show v3.5.0:VERSION` prints 3.5.0. It is NOT a dangling tag.
Published GitHub releases: v1.2.0, v1.3.0, v2.0.0, v3.0.0, v3.1.0,
v3.2.0, v3.3.0. Latest v3.3.0 was published 2026-05-30T10:20:25Z.
There is also an unpublished draft v2.0.0 (release ID 331680814), separate from
published v2.0.0 (331732021, three assets). All other listed releases have zero
uploaded assets; GitHub source archives are not uploaded assets. Draft retained:
its purpose is unknown, not proven junk.

Only `ci.yml` and `maintenance.yml` exist. There is **no publishing workflow**.
CI's `release-lineage` job (introduced in `0d4e9fd`, merged via #82) fetches full
history/tags, runs `scripts/release_lineage.py`, and uploads JSON/summary evidence.
Missing current tag and older latest release are OPEN; lockstep/ancestry conflicts
are RED. It requires a GitHub release when the current tag exists (a project
policy stricter than GitHub's general tag model). Actions are SHA-pinned.
The helper's `--sha` is reported, not independently validated against HEAD;
GitHub failures are conflated with missing data. These are follow-up defects,
not grounds to create/delete releases. Publish permissions/attestations are not
added to a read-only audit job. Full release automation is not introduced.

## PR_HYGIENE

Read open PR bodies and compared their exact head SHAs to the baseline after
fetching `pull/N/head`. No PR was closed, branch deleted, or code imported.
For every audited PR below, `merge-base --is-ancestor HEAD_SHA BASELINE` returned
1; **none of their complete heads is merged**. `git cherry BASELINE HEAD_SHA`
found no equivalent non-merge commits (`-` count zero). Counts below include
merge commits in the unique column, but not in the cherry column.
This does not disprove squash/salvage of subsets; it DOES fail the user's
no-unique-unmerged-commit prerequisite for closing. Similar titles are insufficient.

| PR | Exact head | Unique commits | Non-equivalent patches (+) | Decision |
|---|---|---:|---:|---|
| [#80](https://github.com/bot523h/nexus-ai-agent/pull/80) | `4f1da989e58ee9039fd7890d36757c3387bd83a4` | 5 | 3 | Keep open; unique work/context unresolved |
| [#73](https://github.com/bot523h/nexus-ai-agent/pull/73) | `1a35ec0697457313f8d2fa604e503c18fd4e6340` | 5 | 5 | Keep open; unique work/context unresolved |
| [#70](https://github.com/bot523h/nexus-ai-agent/pull/70) | `94505f51e56b148f18d92255706df5a0e1452084` | 2 | 2 | Keep open; unique work/context unresolved |
| [#69](https://github.com/bot523h/nexus-ai-agent/pull/69) | `749807486709f08810ae801395004cf1c7945938` | 18 | 16 | Keep open; unique work/context unresolved |
| [#68](https://github.com/bot523h/nexus-ai-agent/pull/68) | `eef827641b0627f3581976d494eb0e71f4dd16b6` | 1 | 1 | Keep open; unique work/context unresolved |
| [#67](https://github.com/bot523h/nexus-ai-agent/pull/67) | `9c3a34f107b4b5f3c878a0ec2a7d82e8c24614d3` | 11 | 11 | Keep open; unique work/context unresolved |
| [#66](https://github.com/bot523h/nexus-ai-agent/pull/66) | `db18cf66f32f32ca4fff63443729973366ace8c2` | 3 | 3 | Keep open; unique work/context unresolved |
| [#64](https://github.com/bot523h/nexus-ai-agent/pull/64) | `c92244ceed5de06bc97981527672f834873bbcf2` | 18 | 18 | Keep open; unique work/context unresolved |
| [#63](https://github.com/bot523h/nexus-ai-agent/pull/63) | `3e376bea067f761016725e16d476721f92146813` | 1 | 1 | Keep open; unique work/context unresolved |
| [#60](https://github.com/bot523h/nexus-ai-agent/pull/60) | `efea9bae3ebd108ca7fcaef5fa559268ee82c0a4` | 6 | 6 | Keep open; unique work/context unresolved |
| [#59](https://github.com/bot523h/nexus-ai-agent/pull/59) | `b3385d428cdbdd33e94bb386ae7eef35c8a15ad2` | 3 | 3 | Keep open; unique work/context unresolved |
| [#58](https://github.com/bot523h/nexus-ai-agent/pull/58) | `bb06da1cb304aed43f0da76f7119ccfb803af4c8` | 2 | 2 | Keep open; unique work/context unresolved |
| [#57](https://github.com/bot523h/nexus-ai-agent/pull/57) | `2f591618e13b2ed969fb560cca5ba43f00dfb4a4` | 2 | 2 | Keep open; unique work/context unresolved |
| [#56](https://github.com/bot523h/nexus-ai-agent/pull/56) | `4c53b35a8ef8ebf653dad61e52e80f5584ac2b33` | 4 | 4 | Keep open; unique work/context unresolved |
| [#33](https://github.com/bot523h/nexus-ai-agent/pull/33) | `8f2029e75a83eba87edf878b2dc5233d103a6eab` | 7 | 6 | Keep open; unique work/context unresolved |

PR #83 (`372643afaafd60f2ab79462b14520e46c6ac50f8`) was inventoried only;
its code is explicitly out of scope. #33's task-107 packaging split differs
materially from main (rag/local-llm/postgres/r2 extras, guard wiring, Docker),
and also changes speech semantics; importing it wholesale would regress the
current speech/translate contract. #56's claimed file moves are not present
on main (the dated root docs still exist). #63 describes board-Git reconciliation
work absent from the current workflows. #70/#73 describe successive operation
truth approaches; supersession needs their owners, not a packaging agent.
#80 and merged #81 have similar mission titles but different heads/patches.
The first-parent merge log verifies #65 (`035a896`), #72 (`1099c76`),
#77 (`16daebc`), #79 (`fe95cf0`), #81 (`52ab7e8`), #82 (`2351f09`);
none is proof that a similarly named open PR is entirely redundant.

Reproduce against the recorded heads (not moving branch tips):

```sh
gh pr list --state open --limit 100 --json number,title,headRefOid,body,url
git fetch --unshallow origin # only for a shallow checkout
git fetch origin pull/33/head
git merge-base --is-ancestor 8f2029e75a83eba87edf878b2dc5233d103a6eab 2351f09
git rev-list --count 2351f09..8f2029e75a83eba87edf878b2dc5233d103a6eab
git cherry 2351f09 8f2029e75a83eba87edf878b2dc5233d103a6eab
git log --first-parent --oneline 2351f09
```

## DEPENDENCY_DECISIONS

No dependency removed/moved in this patch. Core-minimal is a valid target, **not
an achieved property**. Baseline declares 32 direct core requirements and
pdf/speech/translate/dev extras. No lockfile found. Current extras CI proves
core/pdf/speech/translate and Python 3.10–3.12; `extras_matrix.py check` requires
new extras to have matching evidence. Pyproject pins selected integration-sensitive
packages, not the full transitive graph. No latest-version comments were taken
as proof that a dependency should be upgraded. Costs below are qualitative,
not benchmark results; #33's GB/time measurements were not reproduced here.

| Dependency | WHY NEEDED / IMPORT SURFACE | COST / INSTALL FAILURE MODE | OPTIONALITY / RUNTIME FALLBACK | TEST IMPACT / decision |
|---|---|---|---|---|
| torch (transitive) / sentence-transformers | RAG embedding in `features/rag.py`, local provider `embed()` | Tensor runtime + models; platform wheels/disk/model downloads | Lazy, but dense embedding needs model; local embed has actionable ImportError | Retain pending absent-extra startup + embedding tests; do not remove torch alone |
| llama-cpp-python | `llm/local_llama_cpp.py` constructor; local backend selection in `litellm_provider.py` | Native C/C++ build/compiler/backend availability | Lazy import, missing GGUF checked; raw import failure still possible; server provider alternative is not automatic same-model fallback | Retain default installation behavior; migrate with explicit local-llm extra and failure contract |
| chromadb | RAG persistent vector client, lazy `_default_client()` | Vector database/transitive native stack and model assets | `RAGUnavailable` caught by docs surface; hint currently names nonexistent rag extra | Real hint defect deferred to coherent rag capability migration |
| flashrank | RAG lazy reranker and `RerankRequest` | Inference/model download cost; offline init can fail | Import/init failures disable reranking with log | Good optional candidate; preserve current default until rag extra/CI policy is agreed |
| psycopg / checkpoint-postgres / asyncpg | PG storage/checkpoint path; psycopg imported at module scope in `langgraph_checkpoint.py` and `checkpoint_reconciler.py` | Driver/native wheels, PG test service | Cannot simply remove: startup/module imports may fail even for SQLite | Defer startup guards to owning runtime lane before postgres extra |
| boto3 | Lazy R2 provider client in `storage/providers/r2.py` | SDK dependency graph, credentials/network for runtime | No equivalent local fallback for an R2 object; missing SDK lacks new extra contract | Keep exact compatibility pin; r2 extra requires provider error/CI tests |
| pdf / speech / translate | Existing pypdf / faster-whisper / argostranslate optional paths | Capability-specific models/binaries | Existing typed refusal/optional smoke contract | Preserve declaration and matrix unchanged |

No new partial `rag` alias: installing it while all heavy requirements remain
in core would falsely imply a lightweight core. Required migration handoff:
coordinate #33 salvage, preserve today's speech extra, audit absent-module
startup in a clean interpreter, verify local embeddings/lexical fallback/error
hints, add each extra's resolver/import smoke leg, then measure core/full builds.
Do not change runtime behavior in this release-truth lane to make removal safe.

## DOCKER_DECISIONS

Docker CLI/daemon is unavailable here (`command -v docker` returned no path).
Therefore no built-image size, vulnerability scan, non-root runtime or cold/warm
build benchmark is claimed. `Dockerfile` is intentionally unchanged this turn;
its remaining hardening findings are formally deferred, not dismissed.

| Finding | Classification / rationale | Required proof before change |
|---|---|---|
| No USER; default root | Security debt, not justified as an ideal state | UID/GID plan; writable data/cache/fonts; existing Compose `./data` and `./assets` bind-mount migration; bot and dashboard smoke |
| Single stage retains build-essential | Multi-stage is feasible in principle: same ABI builder/runtime, copy installed venv/wheels | Build llama native libs, inspect shared-library linkage, run FFmpeg and bot smoke; compare image size and cold/warm build time |
| FFmpeg in production | Required, not removable bloat: commit `8e56c6f` fixed slideshow failure | Preserve real encoder smoke and fonts; do not replace with a dev-only fallback |
| libgl1/libmagic1/fonts | Candidate reduction, not proven unused | Inspect runtime native linkage and rendering/font requirements before removal |
| `python:3.12-slim`, unpinned apt/transitives | Mutable, not reproducible | Verified image digest + update process + resolved dependency set and supported-platform builds; no invented digest |
| COPY before pip install | All source/doc edits invalidate installation cache | Dependency/source layering after packaging/build-input audit; quantify rebuild cost |
| `.env` variants enter context | Actual secret-handling weakness | FIXED narrow ignore policy; static test, actual BuildKit context proof still deferred |
| No image HEALTHCHECK | Not automatically a bug: polling bot is not a listening HTTP server | Per-service readiness contract; do not add a universal localhost:8000 probe for polling mode |
| Shell CMD lacks explicit exec | Signal-forwarding risk, not measured here | Stop/graceful-drain test in both polling and webhook modes before CMD change |

The ignore fix excludes `.env.production`, `.env.local`, and nested `.env` /
`.env.*`. Example templates are retained deliberately. It is not a universal
secret scanner: arbitrary key filenames must still be managed outside build
context. Runtime secrets stay in deployment environment/secret mounts, not ARG,
ENV literals, or copied credentials. No secrets were read or stored by this audit.

## Deep-search evidence by file/decision

Sources consulted 2026-09-25. Recommendations are filtered through the actual
repository, not copied from a blog. Three independently maintained external
sources support each nontrivial policy cluster; local history/CI adds project
specific evidence. Docker/OWASP guidance is not applicable to version arithmetic;
PyPA optional-dependency rules do not by themselves prove a runtime fallback.

- **README + version tests / audit:** repo search and histories (`92dcb47`,
  `c25f7fe`, `71fc9f6`, `c042aff`); existing runbook and version helpers; CI
  full pytest, fast lockstep, release-lineage job. External: [PyPA metadata](https://packaging.python.org/en/latest/specifications/pyproject-toml/),
  [SemVer immutable releases and numeric progression](https://semver.org/),
  [GitHub releases versus tags](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases).
  Decision: manually sync one banner with a test; retain tag gaps, no packaging rewrite.
- **Dependency declarations (reviewed, unchanged):** `rg` imports throughout src,
  lazy loaders and module-level psycopg imports; #33 diff; history `6fd1042`,
  `0d4e9fd`, `c042aff`; `.github/DEPENDENCY_MATRIX.md`, `extras_matrix.py`.
  External: [PyPA optional metadata](https://packaging.python.org/en/latest/specifications/pyproject-toml/),
  [Sentence Transformers installation/backends](https://www.sbert.net/docs/installation.html),
  [canonical llama-cpp Python project and installation examples](https://github.com/abetlen/llama-cpp-python#installation).
  Decision: no backwards-incompatible removal without guards/migration proof.
- **Dockerfile (reviewed) / .dockerignore + new static test:** repo search,
  history `94aa605`, `8e56c6f`, Compose mounts, current CI (no image-build gate).
  External: [Docker build best practices](https://docs.docker.com/build/building/best-practices/),
  [Docker pattern semantics and canonical moby example](https://docs.docker.com/build/concepts/context/#dockerignore-files),
  [OWASP container security](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html),
  [GitHub secret/least-privilege guidance](https://docs.github.com/en/actions/reference/security/secure-use).
  Decision: narrow context exclusion now; ABI/UID changes only with runtime proof.
- **PR/board evidence:** full history/first-parent merges and exact head comparison,
  live PR bodies, repo `AGENTS.md` governance and board tests. External:
  [git-cherry patch-equivalence semantics](https://git-scm.com/docs/git-cherry),
  GitHub release/Actions guidance above, SemVer immutability above. None licenses
  deleting unique work. Board claim initially included leased `docs/README.md`;
  immediately relinquished in `f5453da`, no index edits. Root README evidence
  avoids collision with task-181. No business-logic or PR #83 code was changed.

## TEST_EVIDENCE / COMMIT / BOARD_UPDATE

Diagnostics (not a substitute for full dependency installation or CI):

```sh
.venv/bin/pip install pytest pytest-asyncio ruff==0.16.8 setuptools wheel
.venv/bin/pip install --no-deps --no-build-isolation -e .
.venv/bin/pytest --noconftest -q tests/unit/test_version_lockstep.py \
  tests/unit/test_container_context.py tests/unit/test_agent_board.py \
  tests/unit/test_docs_integrity.py
# 83 passed; no skipped tests
.venv/bin/ruff check .                 # All checks passed
.venv/bin/ruff format --check .        # 496 files already formatted
python scripts/check_version_lockstep.py  # 3.13.0 lockstep OK
python scripts/extras_matrix.py check     # all four existing legs OK
```

`--noconftest` is intentional for these pure file/schema tests: shared conftest
imports runtime dependencies and database fixtures; no runtime success is being
claimed. Editable install with `--no-deps` proves metadata only, not a resolved
core installation. Full gates are **deferred to gates owner** per AGENTS.md.
Baseline CI was green at exact main SHA:
https://github.com/bot523h/nexus-ai-agent/actions/runs/36176954176 .
That is not evidence for this patch's final CI; inspect the exact pushed SHA.
The README fixture checks stale, absent, malformed, duplicate and historical
claims. Container test enforces static exclusions, not BuildKit behavior.

Claim: `release-truth-c-20260925`; claim commits `9e8c2df` and `f5453da`.
Final implementation SHA is recorded in the board follow-up and final handoff
rather than a self-referential hash inside this file. No PR/tag/release closed or
created. Working-tree cleanliness is checked after committing, and lineage is
rerun then (the helper intentionally reports RED on a dirty worktree).

## DEFERRED_ITEMS / completion boundary

1. Packaging owner: coordinated #33 salvage + core-minimal migration/absent-extra
   proof; current declarations retained for compatibility, not declared optimal.
2. Container owner: actual build/benchmark/scan + UID/mount migration + native
   dependency linkage before multi-stage/non-root rollout.
3. Release owner: decide future release cut, draft v2.0.0 disposition; no guessed
   historical intent. Repair API unknown-vs-absent and source-SHA validation in
   release helper with dedicated tests; freeze is not a lockfile.
4. PR owners: per-file salvage analysis for unique patches before closure; current
   evidence cannot establish any open PR as fully redundant.
5. Gates owner: full install, type/runtime suite, exact-head CI verification.

Until these checks are complete this is **not the user's full DONE state**.
The completed deliverable is the evidence audit and two narrow, tested repairs.
