# Test evidence — commands, outputs, and the SHA they belong to

Everything below was executed in this workspace on the fix commit. Nothing is
quoted from an earlier session and nothing is predicted.

| field | value |
|---|---|
| commit | `757dafc74c1900ba1cbc1518d3c19b1b20da2de1` (`fix(character-capsule): close the 30 confirmed PR #326 findings…`) |
| branch | `arena/337c3bc1-nexus-ai-agent` (pushed, PR #326 open, not merged) |
| base | `main` @ `5ed6d4e` |
| environment | Python 3.11.2, pytest 9.2.0, ruff 0.17.4, numpy 2.4.6, PyNaCl 1.6.2, 2 vCPU |
| board | `task-nagar-character-intelligence-02`, generation 2, owner `arena/337c3bc1-nexus-ai-agent` |

## 1. Tests

```bash
. /home/user/.venv/bin/activate
PYTHONPATH=. pytest tests/character_capsule --noconftest -q
```

```
127 passed, 2 warnings in 66.39s
```

The 58 pre-existing tests are unchanged except where a *contract* changed on
purpose (the three evidence tests that asserted a self-signed bundle was `INVALID`
now assert the closed vocabulary; the two benchmark tests now address
`results/<run_id>/`). The 69 new tests are named after the finding they close —
see `FINDINGS_TRIAGE.md` for the mapping.

Why `--noconftest`: the repository conftest imports `httpx`/`typer`/`sqlmodel`,
which this island does not depend on. The island's own `_support.ensure_path()`
keeps its tests independent of the host package.

New suites:

| file | tests | what it proves |
|---|---|---|
| `tests/character_capsule/test_storage_security.py` | 22 | authorization, consent semantics, envelope crypto, import/export containment, blob idempotence, revision CAS |
| `tests/character_capsule/test_identity_scope.py` | 16 | identity-field guards, outfit immutability, scene rules, shot pins, report digests, media binding, module-scope `resource` |
| `tests/character_capsule/test_media_limits.py` | 9 | header-checked decode limits, typed refusals, magic-not-extension |
| `tests/character_capsule/test_benchmark_validity.py` | 23 | split honesty, band-fit labels, independent voice session, report digests, per-run artifacts, measured-not-assumed environment |

## 2. Lint and format

```bash
ruff check .            # → All checks passed!
ruff format --check .   # → 1 file would be reformatted, 1013 files already formatted
```

The one file is `research/character-intelligence/PRIVACY_AND_CONSENT.md`, a
pre-existing markdown formatting difference that also exists on `main`; it is not
touched by this branch (formatted-then-reverted to keep the diff honest).

## 3. Repository guards

```bash
GITHUB_REPOSITORY=bot523h/nexus-ai-agent \
  python scripts/agent_board.py check --files <the files changed here>
```

Result: 47 sources read, no overlap with another agent's claim. The tool reports the
changed paths as claimed by `task-nagar-character-intelligence-02` — **that is this
branch's own claim** (owner `arena/337c3bc1-nexus-ai-agent`, generation 2), which the
tool cannot distinguish from a foreign one by path alone. No other task, and no
Arena-1 zone, is involved.

```bash
git diff --stat 5ed6d4e..HEAD -- src docs pyproject.toml tests/unit tests/architecture
```

→ 0 rows: no protected path was touched.

## 4. Benchmark (real run, this commit)

```bash
PYTHONPATH=. python -m experiments.character_capsule.benchmark \
    --work-dir experiments/character_capsule/.work --run-id final-757dafc
```

```
run_id  final-757dafc   atomic_publish True
summary {'scenarios': 12, 'passed': 12, 'failed': 0, 'accuracy': 1.0,
         'verdicts': ['CANON_CONFLICT', 'IDENTITY_DRIFT', 'INSUFFICIENT_EVIDENCE',
                      'REVIEW_REQUIRED', 'VERIFIED_WITH_LIMITATIONS', 'WARDROBE_DRIFT']}
environment python 3.11.2, peak_rss_mb 55.6, total_wall_ms 5045.5,
            mean_scenario_ms 195.633, cpu_count 2, gpu NOT_MEASURED
```

| artefact | digest |
|---|---|
| corpus (63 frames, per-frame sha256) | `sha256:70e45e5ea91f4bd4fd706b9c32fe462b210752b1180931800e2a7cceaf13c8e4` |
| calibration | `sha256:6a761bee0594760d9a27ba5489b9d62f97bc498dd0ec062ce9d636a6bb30ff36` |
| results | `sha256:4a2ce9400b755c0672c01a3aeacd6b76fea60c6d13a90865ada724bcca24a91f` |
| evidence bundle | `sha256:e47339a51229ba4151502c05adf7ef0fa2c90a6598dd8a04387129b7f4f9f788` |
| bundle id | `EVD-NAGAR-CHAR-000001-final-757dafc` |

Artifacts are published under `experiments/character_capsule/.work/results/final-757dafc/`
(git-ignored, reproducible from the command above):

```
BENCHMARK.md  benchmark_results.json  calibration.json  continuity_reports.json
corpus_manifest.json  evidence_bundle.json
```

## 5. Evidence verification matrix

Executed against the bundle from §4 (`check_evidence.py`, exit codes per the
six-state vocabulary):

| scenario | command | exit | status | checks |
|---|---|---|---|---|
| all artifacts, no anchor | `--asset-dir <run>` | **2** | `UNSIGNED` | `files_match`, `all_artifacts_supplied`, `hard_binding_matches_records`, `lineage_present`, `records_unique` all true |
| anonymous detached signature | `--bundle signed --trust-anchor trusted.pub` | **0** | `VALID` | `signature_valid` + `trust_anchor_matches_signer` true |
| the same signed bundle, no anchor supplied | `--bundle signed` | **4** | `UNTRUSTED_SIGNER` | signature verifies against its own key; trust is absent |
| one byte appended to `BENCHMARK.md` | `--asset-dir tampered` | **1** | `INVALID` | `files_match` false, reason names the file |
| one artifact withheld | `--asset-dir partial` | **3** | `INCOMPLETE` | `missing_records: ['continuity_reports.json']` |
| no Ed25519 backend | unit test | — | `NOT_VERIFIABLE` | `signature_checked` false, reason states why |

Published report digests: **12/12 recompute** (`verify_report_evidence` on
`continuity_reports.json`). Editing a report's verdict breaks its digest
(`test_f25b_editing_a_published_report_breaks_its_digest`).

## 5b. Shot verification CLI (end to end, with pins from a manifest)

```bash
RUN=experiments/character_capsule/.work/results/final-757dafc
PYTHONPATH=. python scripts/character_capsule/verify_shot.py \
    --calibration $RUN/calibration.json \
    --image experiments/character_capsule/.work/frames/000001-ref-front.png \
    --shot-json shot.json --reference <7 portrait references>
```

| input | exit | output |
|---|---|---|
| shot pinned to the capsule in hand | 0 | `VERIFIED_WITH_LIMITATIONS`, `pins_source: shot.json`, per-dimension states published |
| the same shot with `capsule_digest` replaced by `sha256:000…` | **5** | `{"refused": true, "reason": "shot pins do not match the capsule", "details": {pinned, supplied}}` |

The second row is F20 seen from the outside: before the fix this shot was scored
`VERIFIED_WITH_LIMITATIONS`; now it is refused before any measurement, and the refusal
names both digests.

## 6. Pre-fix baseline (what these tests would have caught)

The findings were first reproduced on `5fa6470` before any fix was written; the
table in `FINDINGS_TRIAGE.md` records, per finding, the observed pre-fix behaviour
(e.g. F02: a `VIEWER` exported every asset; F08: 186,696 bytes → 192 MB; F28:
`eye_spacing_ratio` 0.235 → 0.300 inside a revision; F24: voice distance 0.0 between
"two" recordings). That harness lived in a scratch directory outside the repository
and is not committed; the reproducible form is the regression suite, which fails on
`5fa6470` by construction because each test asserts the hardened contract.

What is *not* claimed: these tests do not prove the absence of other defects. They
prove that the 30 reproduced failures no longer reproduce, and that the new
contracts hold under the attacks written down above.
