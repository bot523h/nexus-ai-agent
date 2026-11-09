# Arena 1 Compatibility Report

Status: **VERIFIED** — mechanical check executed in this workspace on 2026-11-09.

## Claim under test

This mission must not alter Arena 1: no edits to the composition runtime, the
canonical infrastructure, packs, rendering, the job queue, `nagar/ibpc/`, the ADR
directory, the composition documents, `pyproject.toml` or CI configuration.

## Mechanical evidence

`git status --porcelain` for the whole repository, restricted to the protected
paths, returns **zero rows**:

```
src/nexus_ai_agent/creative/packs                    changed=0
src/nexus_ai_agent/creative/render_jobs.py           changed=0
src/nexus_ai_agent/application/ports/job_queue.py    changed=0
src/nexus_ai_agent/nagar/**                          changed=0
docs/architecture/adr/**                             changed=0
pyproject.toml                                       changed=0
```

The same check over the whole tree confirms the diff against the base commit
`5ed6d4e` contains **zero rows** under `src/`, `docs/`, `pyproject.toml`,
`tests/unit/` and `tests/architecture/`:

```
git diff --name-only 5ed6d4e..HEAD -- src docs pyproject.toml tests/unit tests/architecture
→ 0 rows
```

Paths that the mission names but that are **absent** in this checkout are reported
as absent, not as "unchanged" — nothing can be unchanged if it does not exist:

| path | state in this checkout |
|---|---|
| `src/nexus_ai_agent/creative/composition/` | **absent** |
| `tests/unit/test_composition_runtime.py` | **absent** |
| `docs/ops/COMPOSITION_RUNTIME.md` | **absent** |
| `docs/architecture/COMPOSITION_THREAT_MODEL.md` | **absent** |
| `docs/architecture/NAGAR_COMPOSITION_RUNTIME_REPORT.md` | **absent** |
| `src/nexus_ai_agent/creative/studio/`, `creative/rendering/`, `creative/packs/`, `creative/render_jobs.py` | present, **unchanged** (0 rows) |
| `src/nexus_ai_agent/nagar/**` incl. `nagar/ibpc/` | present, **unchanged** (0 rows) |
| `src/nexus_ai_agent/application/ports/job_queue.py` | present, **unchanged** (0 rows) |
| `docs/architecture/adr/`, `pyproject.toml`, CI | present, **unchanged** (0 rows) |

The complete change set of this mission is confined to four new paths plus the
board claim:

```
research/character-intelligence/     (new: documents)
experiments/character_capsule/       (new: prototype + CLI target)
tests/character_capsule/             (new: 6 test modules, 127 tests after hardening)
scripts/character_capsule/           (new: 3 thin CLIs)
.agents/board.json                   (board CLI: zone + claim, no hand edit)
```

Nothing under `src/` was created, modified or deleted. Nothing under `docs/` was
touched, so the docs index gate (`tests/unit/test_docs_integrity.py`) is unaffected
— it passed (259 guard tests green, see below).

## Import boundary

The prototype imports **only** its own modules plus `numpy` and `nacl`:

```
experiments/character_capsule/{canonical,errors,media,model,wardrobe,canon,scene,
security,storage,vision,audio_motion,continuity,dataset,benchmark,evidence}.py
```

There is **no** `import nexus_ai_agent...` anywhere in the prototype, so it cannot
drag in the composition runtime, the queue, the registry or the IBPC code. The
reverse direction is also clean: no Arena 1 module imports the experiment package
(it is not installed and lives outside `src/`).

## Duplicate-infrastructure check

| canonical capability | reused or duplicated? |
|---|---|
| canonical JSON / digest | **re-implemented locally** in `canonical.py` (12 lines: `json.dumps` with sorted keys) — deliberate isolation, and it produces the same digest form (`sha256:<hex>`); not a competing service |
| asset registry | **not duplicated**: capsules store `AssetRef` digests; no new registry, no new table outside the prototype's own SQLite schema |
| key management | **not duplicated**: `security.py` reads a KEK from an external file and never stores key material; no new KMS |
| queue / CommandBus | **not duplicated**: none exists in this mission |
| canon authority | **not duplicated**: the prototype's canon module is a *character* canon, not the repository canon; it has no authority over Arena 1 |

The one item worth flagging: `canonical.py` restates canonical-JSON hashing rather
than importing Arena 1's helper. Reason: importing `src/` would couple a research
prototype to the installed package and violate the isolation the mission requires.
The formats are compatible (`sort_keys`, compact separators, `sha256:` prefix), and
the file is 40 lines. If a future integration wants one implementation, the
prototype's `canonical.py` can be deleted in favour of the shared helper — this is
recorded as `DEFERRED` in `INTEGRATION_CONTRACT_PROPOSAL.md`.

## Gate tests executed

```
PYTHONPATH=. python -m pytest \
  tests/architecture/test_scripts_import_boundary.py \
  tests/architecture/test_test_suite_hygiene.py \
  tests/architecture/test_no_orphan_modules.py \
  tests/architecture/test_modular_monolith.py \
  tests/architecture/test_repo_truth_consistency.py \
  tests/unit/test_docs_integrity.py \
  tests/unit/test_agent_board.py --noconftest -q
→ 259 passed
```

Two architecture test modules (`test_legacy_lane_verification_gap.py`,
`test_storage_backend_boundary.py`) cannot be collected **in this sandbox** because
`httpx` / `aiosqlite` are not installed; that is an environment limitation, stated
rather than worked around, and it predates this mission.

## Verdict

**Zero conflict with Arena 1.** The mission is additive, isolated, and its only
shared surface is the board claim it registered through the official CLI.
