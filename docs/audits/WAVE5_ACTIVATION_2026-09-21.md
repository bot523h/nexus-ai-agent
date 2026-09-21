# Wave 5 audit — pack runtime activation, six TDD gaps, dependency-free coverage

> **Session:** `arena/01a0c5da-nexus-ai-agent` (self-declared *عامل I*; the branch name is the
> canonical identity per `AGENTS.md`).
> **Claim:** `wave5-activation-and-gap-closure`, zone `nagar-runtime-activation`, written and
> pushed **before** the first line of code.
> **Gates:** *diagnostic only* — `ruff check`, `ruff format --check`, `mypy src`, the full
> `pytest -q -m "not slow"` suite. The authoritative `make lint && make types && make test` gate
> stays with the **gates owner** (AGENTS.md rule 4).
> **Companion documents:** `WAVE5_COORDINATION_01a0c5da.md` (claim, collision record, re-apply
> blob) and `docs/ops/PACK_RUNTIME.md` (operations guide).

---

## 1. Executive summary — the ten steps

| # | Step | Deliverable | Status | Evidence |
|---|---|---|---|---|
| W5-1 | One runtime composition | `src/nexus_ai_agent/creative/packs/runtime.py` — `PackComposition`, `COMPOSITION`, `build_runtime_registry()`, `build_pack_registry()`, `build_pack_runtime()`, `composition_issues()`, `stale_capabilities()` | ✅ | 57 registered operations; `composition_issues() == ()`; `stale_capabilities() == {}` |
| W5-2 | CLI consumes the composition | `cli.py::_packs_registry` → `build_pack_registry()` (one function, hunk-disjoint from PR#33) | ✅ | `nexus packs list --json` → six packs, `pending=0` for all six (was 8/7/8/7 pending) |
| W5-3 | Composition unit tests | `tests/unit/test_pack_runtime_composition.py` | ✅ | 18 tests, green |
| W5-4 | Activation-completeness gate | `tests/architecture/test_pack_activation_completeness.py` (+2 harness-boundary gates) | ✅ | 11 tests, green; fails if a manifest exists without a composition entry, or a tool re-enters the data-only substrate |
| W5-5 | Motion op gap (3) | `motion.apply_mask`, `motion.warp`, `motion.add_particles`; manifest 7 → 10 | ✅ | registered + manifest parity, covered by `test_opgap_wave5.py` |
| W5-6 | Audio op gap (2) | `audio.remove_vocal`, `audio.align_music`; manifest 8 → 10 | ✅ | same |
| W5-7 | Timeline op gap (1) | `timeline.sync_multicam`; manifest 8 → 9 (timeline domain now 10/10 vs the TDD table) | ✅ | same |
| W5-8 | Dependency-free coverage harness | `src/nexus_ai_agent/continuum/pack_coverage.py` + `scripts/pack_coverage.py` (stdlib `trace`/`dis`, no new dependency) | ✅ | 13 harness tests; measurement below; exit code 0/1/2 |
| W5-9 | Operations doc | `docs/ops/PACK_RUNTIME.md` | ✅ | composition contract, addition checklist, troubleshooting table |
| W5-10 | Truth pass + board handoff | this audit, `WAVE5_COORDINATION_01a0c5da.md`, `.agents/board.json` (deliveries, fence, next-10 network) | ✅ | §5–§8 |

**Test accounting:** 1181 → **1258 passed** (+77), 20 skipped, 0 failures; the 77 new tests are
exactly W5-3 (18) + W5-4 (11) + W5-5..7 (35) + W5-8 (13).

---

## 2. Verified evidence (commands and results)

### 2.1 Repository gates (final state, commit `ba76149` + this document)

```bash
/home/user/.venv/bin/python -m pytest -q -m "not slow" -p no:cacheprovider
# 1258 passed, 20 skipped, 4 warnings in 67.71s

/home/user/.venv/bin/ruff check .                # All checks passed!
/home/user/.venv/bin/ruff format --check .       # 397 files already formatted

/home/user/.venv/bin/mypy src
# features/rag.py:8 missing stub for "chromadb.utils"  +  rag.py:46 unused "type: ignore"
# → 2 errors in 1 file, both environment-only (optional dependency not installed in this sandbox;
#   identical on the pre-change baseline). 223 source files checked.
```

### 2.2 Runtime activation — before and after

```bash
/home/user/.venv/bin/python -m nexus_ai_agent.cli packs list --json
```

| package_id | capabilities | pending (before) | pending (now) | active now |
|---|---|---:|---:|---|
| `nexus.slideshow.compose` | 6 | 0 | **0** | ✅ |
| `nexus.language.caption` | 10 | 0 | **0** | ✅ |
| `nexus.edit.timeline` | 9 | 8 | **0** | ✅ |
| `nexus.motion.graphics` | 10 | 7 | **0** | ✅ |
| `nexus.audio.studio` | 10 | 8 | **0** | ✅ |
| `nexus.color.delivery` | 7 | 7 | **0** | ✅ |

```python
build_runtime_registry()  → 57 operations
composition_issues()      → ()
stale_capabilities()      → {}
build_pack_runtime(activate=True).status() → all six ActivePackStatus(active=True)
```

Before Wave 5, five of six packs were composed "one call site at a time"; only slideshow and
caption were reachable, which is why the operator-facing table read `pending=8/7/8/7`.

### 2.3 Coverage of the pack substrate (Wave-5 measurement)

```bash
/home/user/.venv/bin/python scripts/pack_coverage.py --json-out /tmp/cov.json
```

| unit | modules | executed | executable | cover | status |
|---|---:|---:|---:|---:|---|
| core (substrate) | 5 | 669 | 695 | **96.26 %** | OK |
| slideshow | 5 | 841 | 904 | **93.03 %** | OK |
| caption | 4 | 868 | 898 | **96.66 %** | OK |
| edit | 3 | 490 | 511 | **95.89 %** | OK |
| motion | 3 | 679 | 692 | **98.12 %** | OK |
| audio | 3 | 615 | 640 | **96.09 %** | OK |
| delivery | 4 | 497 | 570 | **87.19 %** | OK |
| **TOTAL** | **27** | **4659** | **4910** | **94.89 %** | OK (bar 85 %) |

Weakest modules (the honest gap, not a claim): `delivery/signing.py` 68.89 %,
`slideshow/models.py` 89.83 %, the pack `__init__` re-export blocks ≈ 83–87 %.
Method: the denominator is the compiler's own line table (`dis.findlinestarts`, recursing into
nested code objects); the numerator comes from stdlib `trace.Trace` over 24 pack-focused test
modules. **No third-party coverage dependency was added.**

---

## 3. Zero-interference mandate — corrected, then measured

### 3.1 Correction: the earlier "no merge base" reading was a shallow-clone artifact

The claim-time measurement reported `merge-base = NONE` for PRs #39/#33/#32. That was wrong, and
the cause is now known: this sandbox clone is **shallow** (`.git/shallow` pinned at `b422512`), so
`git merge-base` could not walk to the true common ancestor of branches fetched with their own
shallow boundaries.

```bash
git fetch --unshallow origin '+refs/heads/*:refs/remotes/origin/*'   # 30 MB, 1.3 s
git merge-base main origin/arena/01a0c3aa-nexus-ai-agent   # 978ae16
git merge-base main origin/arena/01a0c34d-nexus-ai-agent   # 5e5009a
git merge-base main origin/arena/01a0c4c1-nexus-ai-agent   # 7573249
```

GitHub's own comparison agrees (`gh api repos/bot523h/nexus-ai-agent/compare/main...<branch>`):
#33 → base `978ae16`, ahead 7 / **behind 43**; #32 → base `5e5009a`, ahead 2 / behind 45;
#39 → base `7573249`, ahead 2 / behind 17. All three are therefore *rebase-before-merge* PRs
(tracked on the board as `task-122`/`task-123`) — they are not "unrelated histories".

**Consequence for this wave:** nothing changed functionally, but the record is now accurate, and
the overlap test below is stronger because it runs on real merge bases.

### 3.2 File-level intersection with every open PR (GitHub compare, authoritative)

| PR | merge base | files changed | intersection with Wave-5 paths |
|---|---|---:|---|
| #45 exposure lane | `b422512` | 11 | `.agents/board.json` only |
| #44 board lease fix | `b422512` | 3 | `.agents/board.json` only |
| #43 storage resilience | `b422512` | 4 | `.agents/board.json` only |
| #39 architecture docs | `7573249` | 26 | `.agents/board.json` only |
| #33 release/interop | `978ae16` | 52 | `.agents/board.json` + `src/nexus_ai_agent/cli.py` |
| #32 feature wiring | `5e5009a` | 26 | `.agents/board.json` only |

### 3.3 Merge-level test — Wave 5 adds **zero** conflicts

`git merge-tree --write-tree <pr-tip> HEAD` (prerequisite: unshallow, §3.1). "Extra" is the
conflict set of PR×Wave-5 minus the conflict set of PR×`main` — the honest measure of whether
*this wave* makes any open PR harder to land.

| PR | conflicts vs `main` | conflicts vs Wave-5 HEAD | **extra caused by Wave 5** |
|---|---:|---:|---:|
| #33 | 11 | 11 | **0** |
| #32 | 7 | 7 | **0** |
| #39 | 1 | 1 | **0** |
| #45 | (clean) | 1 (`board.json`) | coordination file only |
| #44 | (clean) | 1 (`board.json`) | coordination file only |
| #43 | (clean) | 1 (`board.json`) | coordination file only |

For #33 the 11 conflicts are its own rebase debt (behind by 43 commits: `AGENTS.md`,
`CHANGELOG.md`, `pyproject.toml`, …); `cli.py` is **not** among them — PR#33's only `cli.py` hunk
is in `golden_update` (@@ −390), Wave 5's is in `_packs_registry` (@@ ≈ −448): disjoint functions.

The board's own referee agrees:

```bash
python scripts/agent_board.py check --branch arena/01a0c5da-nexus-ai-agent \
  --files "src/nexus_ai_agent/creative/packs/runtime.py,src/nexus_ai_agent/cli.py,\
src/nexus_ai_agent/creative/packs/motion/operations.py"
# no overlap — safe to proceed.
```

### 3.4 Fence (user requirement, recorded on the board)

The user's standing requirement for this wave is explicit: **other agents must not create
interference.** That is recorded in `.agents/board.json` as a `fence` on this claim — see §6.

---

## 4. Honest limits — what this wave does *not* claim

1. **Coverage is 94.89 %, not 95 %.** The `wave4-step7` goal was 95 % per pack. The instrument
   now exists and the residual is visible (`delivery/signing.py` 68.89 %); closing it is a
   follow-up, not a delivered claim.
2. **The harness is diagnostic.** It is not wired into CI: `.github/workflows/ci.yml` is owned by
   PR#33 until the release PR lands. The next-agent network carries the wiring task.
3. **The remaining TDD op gap is 23 operations** — `portrait.*` (10) and `scene.*` (10) are whole
   missing domains, plus `color.white_balance` / `color.hdr_tonemap` / `color.deband_denoise`.
   Wave 5 closed the six *slice* operations (motion 3, audio 2, timeline 1); it deliberately did
   **not** touch `creative/rendering/**`, which PR#45 owns — the three colour ops are the colour
   lane owner's call.
4. **`mypy` shows 2 errors** in `features/rag.py` (missing optional `chromadb` stub + the
   now-unused `type: ignore`). This is environment-only and identical before/after; it is *not*
   fixed here because `features/rag.py` belongs to PR#32's zone.
5. **`CHANGELOG.md` carries no entry** from this session — PR#33 owns the `3.13.0` header. The
   ready-to-paste text is in §7.

---

## 5. Board truth corrections (artefact-verified, ownership untouched)

| Entry | Board said | Reality | Evidence |
|---|---|---|---|
| `task-125` TDD audio/motion slice | available (15 ops) | **6 of the slice's ops delivered** (`motion.apply_mask/warp/add_particles`, `audio.remove_vocal/align_music`, `timeline.sync_multicam`); 23 remain (portrait 10 / scene 10 / colour 3) | `tests/unit/test_opgap_wave5.py` (35 tests), counts re-derived from the TDD table |
| `task-126` unified packs runtime | `available_sequenced_post_33` | **delivered** — six packs, `pending=0`, activation real | `packs list --json`, `composition_issues() == ()` |
| `wave4-step7` coverage | queued (95 % goal) | **instrument delivered** (stdlib harness, 94.89 % baseline); 95 % still open for `delivery/signing.py` | §2.3 |
| `wave4-step10` op gap | queued (6 ops) | **delivered** — motion 3 + audio 2 (+1 timeline op as a bonus) | registry + manifest parity |
| `task-106/112/114/115/118/129/130` | mixed | already delivered on `main` (pre-wave forensic pass) | `WAVE5_COORDINATION_01a0c5da.md` §2 |

Sequencing note: `task-126` was deliberately sequenced after PR#33 *because* that PR touches
`cli.py`. Wave 5 landed the same outcome **before** #33 without making #33 harder: the two `cli.py`
hunks are in different functions and `git merge-tree` confirms #33's conflict set is unchanged by
this branch (§3.3).

---

## 6. Fence / no-interference record (`.agents/board.json`)

The board now carries, on this claim object:

```json
"fence": {
  "rule_fa": "تا زمان release صریح این claim، هیچ عامل دیگری نباید مسیرهای exclusive_paths این موج را ویرایش کند ...",
  "rule_en": "Until this claim is explicitly released, no other agent may edit these exclusive paths ...",
  "verified_zero_interference": {
    "method": "git merge-tree --write-tree <pr-tip> HEAD vs main (unshallow clone)",
    "extra_conflicts_vs_main": {"#33": 0, "#32": 0, "#39": 0},
    "coordination_file_only": ["#45", "#44", "#43"],
    "board_referee": "scripts/agent_board.py check → no overlap — safe to proceed."
  }
}
```

Rules that follow from it:

1. A path in `exclusive_paths` belongs to this session until the claim is released or its TTL
   (24 h) expires and the GC reclaims it.
2. Another agent that *needs* one of those paths must first register a request in
   `.agents/board.json` (a `coordination_log` entry naming the path and the reason) — drafting
   first and asking later is a protocol violation.
3. The only shared file with every open PR remains `.agents/board.json` itself, governed by
   AGENTS.md's *"newest forensic state wins"* rule; the re-apply blob is §4 of the coordination
   record, so no foreign state is lost.
4. Work already delivered is not re-claimable: `task-125/126`, `wave4-step7/step10` carry
   `delivered_by` + `evidence` fields.

---

## 7. Ready-to-paste `CHANGELOG.md` entry (owner: PR#33)

```markdown
### Added
- **Pack runtime composition** (`nexus_ai_agent.creative.packs.runtime`): one module composes the
  Wave-1 catalog and all six builtin packs; `nexus packs list` now reports `pending=0` for every
  builtin pack and each pack can be activated without hand-wiring call sites.
- **Six closed TDD operations**: `motion.apply_mask`, `motion.warp`, `motion.add_particles`,
  `audio.remove_vocal`, `audio.align_music`, `timeline.sync_multicam` (manifests updated to
  10/10/9 capabilities; timeline domain now complete at 10/10).
- **Dependency-free pack coverage harness** (`scripts/pack_coverage.py`, stdlib `trace`/`dis`):
  per-pack and per-module executed-line coverage with a threshold and exit code; measured baseline
  94.89 % over 27 pack modules. No new third-party dependency.

### Documentation
- `docs/ops/PACK_RUNTIME.md` — composition contract, "adding a pack" checklist, troubleshooting.
- `docs/audits/WAVE5_ACTIVATION_2026-09-21.md` — this audit.
```

## 8. Next ten tasks (forwarded to the board as `ten_forward_tasks_wave5`)

| id | title | why now | zone / owner |
|---|---|---|---|
| task-141 | Wire the pack-coverage harness into CI (non-blocking bootstrap, then blocking) | `.github/workflows/ci.yml` frees up when PR#33 lands | ci-quality / gates owner |
| task-142 | Close `delivery/signing.py` to ≥95 % (`tests/unit/test_delivery_signing.py`) | the only module below 90 % in the measured substrate | quality / free |
| task-143 | `portrait` pack — 10 TDD operations (models + pure handlers + manifest) | largest remaining TDD domain gap; composition checklist in `docs/ops/PACK_RUNTIME.md` | nagar-packs / free |
| task-144 | `scene` pack — 10 TDD operations (segmentation/tracking family) | second largest; shares the op-gap template | nagar-packs / free |
| task-145 | Three colour-lane ops (`white_balance`, `hdr_tonemap`, `deband_denoise`) | needs `creative/rendering/**`, owned by PR#45 | colour lane / PR#45 owner |
| task-146 | `nexus packs doctor` — one command combining `verify` + `list` + composition issues for operators | natural CLI follow-on to W5-2 (sequenced after #33) | cli-ops / free after #33 |
| task-147 | Coverage baseline file (`--json-out` accepted as a tracked artefact) + drift check in the bench lane | turns a one-off measurement into a regression signal | quality / free |
| task-148 | Extend the harness to `creative/studio` and `continuum` (out-of-substrate truth tooling) | the same stdlib method applies; keeps "what ships" measured | quality / free |
| task-149 | Ops runbook section for the pack lifecycle (activate/rollback/verify in production) | joins `docs/ops/RUNBOOK_HARDENING.md` work (wave4-step8) | docs-ops / free |
| task-150 | Post-merge hygiene: `README` version, `.nexus/continuum.json` test count, `CHANGELOG` paste | blocked until PR#33 merges | release / PR#33 owner |

---

## Appendix — reproduction recipe

```bash
# environment (system pip is PEP-668 locked; everything goes into a venv)
python3 -m venv /home/user/.venv
/home/user/.venv/bin/pip install -e . --no-deps
/home/user/.venv/bin/pip install -r <curated dev set>       # ~30 packages; heavy extras omitted

# gates
/home/user/.venv/bin/python -m pytest -q -m "not slow" -p no:cacheprovider
/home/user/.venv/bin/ruff check . && /home/user/.venv/bin/ruff format --check .
/home/user/.venv/bin/mypy src

# wave artefacts
/home/user/.venv/bin/python -m nexus_ai_agent.cli packs list --json
/home/user/.venv/bin/python scripts/pack_coverage.py --json-out /tmp/cov.json
/home/user/.venv/bin/python -c "from nexus_ai_agent.creative.packs.runtime import \
composition_issues, stale_capabilities; print(composition_issues(), stale_capabilities())"

# zero-interference proof (needs full history)
git fetch --unshallow origin '+refs/heads/*:refs/remotes/origin/*'
for b in 01a0c58e 01a0c593 01a0c58a 01a0c4c1 01a0c3aa 01a0c34d; do
  git merge-tree --write-tree "origin/arena/$b-nexus-ai-agent" HEAD | grep CONFLICT
done
```
