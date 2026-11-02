# NEXUS — Master Forensic Revalidation, Security Hardening & Repair Report

*Mission:* `NEXUS-MASTER-CLOSURE-2026-11-03` · *Date:* 2026-11-03 · *Status:* fixes implemented, gated and pushed on a branch (human review pending)

This is a dated audit record. It is not a source of truth: the living views are
`docs/architecture/*` and `docs/DECISION_LOG.md`; when this file disagrees with
them, they win.

## 1. Baseline (exact)

| Item | Value |
|---|---|
| Repository | `bot523h/nexus-ai-agent` |
| Base `main` | `a2a847a6ed70f16e1f0364d36169836f6d2d5a74` |
| `main` contains | merge of PR #302 (creative redelivery P0) and PR #298 (IBPC live editing + security closure) |
| Working branch | `arena/nexus-master-closure-20261103` |
| Branch head | `ded9d0b5a51c26a0c23abda077d5ee0c285e6366` |
| Work tree | `/workspace/project/work/nexus/wtmain2` |

The branch adds six commits on top of `main` (one board claim, two fix packages,
one CI wiring, one decision-log record, one board evidence update). It touches 24
files, +272 / −6 lines.

## 2. What was audited

The mission was an adversarial re-validation of the two boundaries that `main`
had just landed (PR #302 / PR #298), plus a repository-wide sweep:

1. The creative redelivery boundary (decision **D-0036**): a Telegram creative
   request's media must be immutable once admitted, the surface stages privately,
   and the worker re-measures before any engine.
2. The IBPC live-editing security closure (PR #298): approval single-use,
   sequence/session/turn binding, replay refusal, staged-input digest binding.
3. General hygiene: shell/eval/pickle/YAML sinks, TLS verification, tool
   enablement defaults, and i18n key coverage for every user-visible code.

## 3. Findings

Severity: **P0** = exploitable cross-job data-integrity / information-disclosure
on a normal path; **P1** = user-visible defect on a normal path; **P2** =
defence-in-depth.

### NEW-1 (P0) — workspace-node symlink substitution laundered into the victim

* **Where:** `src/nexus_ai_agent/creative/render_jobs.py::_guarded_workspace`.
* **Root cause:** the workspace path was `resolve()`d *before* it was inspected.
  `resolve()` follows a symlink, so the guard could only ever see the target.
* **Reachability / impact:** the queue row is durable and the worker runs in a
  different process from the surface. A workspace symlink planted after admission
  (`creative_job` → `creative_victim`, both inside the trusted root and both
  carrying the reserved `creative_` prefix) passes both the containment check and
  the reserved-prefix check. The worker then resolves the link and writes
  `output.mp4` into the **victim** directory — a cross-job workspace substitution
  (integrity) and a write into a directory the request never owned.
* **Existing defence / why insufficient:** the surface refuses a symlinked
  workspace, and `_guarded_input` already `lstat`s the *input* link before
  resolving it. The workspace node was simply not covered, and the surface's check
  cannot be inherited across the durable queue.
* **Fix:** `lstat` the raw workspace node and refuse `S_ISLNK` before `resolve()`,
  mirroring `_guarded_input`. A missing node is still allowed (the worker may be
  the first to create it).
* **Test:** `tests/integration/test_creative_surface_redelivery.py::test_worker_refuses_a_workspace_that_is_a_symlink_inside_the_root`
  (fails iff the guard is removed). Mutation probe `M11` in
  `scripts/creative_redelivery_mutations.py`.

### NEW-3 (P2) — private-directory mode was never repaired for an existing node

* **Where:** `src/nexus_ai_agent/bot/creative_surface.py::_ensure_private_directory`.
* **Root cause:** the code relied on `mkdir(mode=0o700, exist_ok=True)`, which
  applies the mode **only when it creates** the directory. An existing directory
  (older deployment, wider umask at first creation, a volume restored from a
  backup, a deliberately loosened scratch root) keeps its mode.
* **Reachability / impact:** the owner-only staging/workspace contract is silently
  unenforced; another local principal could read or tamper with in-flight media.
  Not a network-exploitable bug, hence P2.
* **Fix:** new `_harden_private_directory` `lstat`s the node, refuses a symlink /
  non-directory / foreign owner, `chmod`s an owned directory to `0o700`, and
  re-`stat`s to confirm the mode actually applied. Called for both the root and the
  workspace. The sibling write path (IBPC `session.py`) already forced permissions
  with `os.fchmod`; this brings the surface to parity.
* **Test:** `tests/unit/test_creative_surface.py` (private-dir symlink refusal +
  mode repair). Mutation probe `M12`.

### FINDING-2 (P1) — a P0-redelivery failure leaked a raw i18n key to the user

* **Where:** `src/nexus_ai_agent/bot/app.py::_notify_creative_completion` maps
  every code in `render_jobs.ERROR_CODES` to `creative.failed.{code}`.
* **Root cause:** `input_integrity` was added to `ERROR_CODES` by
  task-nagar-workspace-redelivery-01, but the corresponding locale key was never
  added to **any** of the 15 locales. `I18n.t` returns the key itself when a
  translation is missing, so a digest-mismatch / symlinked-input refusal rendered
  the literal string `creative.failed.input_integrity` in the Telegram chat.
* **Impact:** user-visible, on the very path the P0 boundary is meant to protect.
  Not an information leak (the raw key contains no path), but a broken message and
  a failed i18n contract.
* **Fix:** added `creative.failed.input_integrity` to all 15 locales with a
  **path-free** message (every `input_integrity` detail embeds a filesystem path,
  so the message must not interpolate `{detail}`). Added a lockstep test over
  `ERROR_CODES × SUPPORTED_LANGUAGES` that fails if any code resolves to a raw key.
* **Test:** `tests/unit/test_creative_notify.py::test_every_worker_error_code_has_a_translated_message_in_every_language`
  and `::test_input_integrity_failure_is_translated_not_a_raw_key` (the first was
  proven RED by deleting the key from `en.json`).

### NEW-2 (accepted limitation, not a defect) — digest → engine TOCTOU

The worker re-measures the input (`_verify_input_digest`) and *then* hands the
path to FFmpeg. Between the two, a local principal could in principle swap the
file. This is **accepted**, not silently ignored: the input lives in an
owner-only directory, the path is never redirected through a link, and the
content-identity witness is not claimed to be encryption. Recorded in
`docs/DECISION_LOG.md` D-0036.

### NEW-4 (policy confirmed) — legacy media rows without a digest

A media-bearing payload written before D-0036 carries no `input_sha256`. The
worker refuses it fail-closed with `input_integrity` (never executes an
unverified input) and it now reaches the user as a real translated message
(FINDING-2). Handling requires operator reconciliation; the code makes no claim
that existing rows were migrated.

### Sweep results (no new defects)

* `shell=True` / `os.system` / `os.popen`: none in `src/`. `ShellTool` is
  registered only when `NEXUS_ENABLE_SHELL` is set, and the setting defaults to
  `False`.
* `eval` / `exec` / `pickle.loads` / `marshal.loads`: none (matches are SQLAlchemy
  `session.exec`).
* `yaml.load` (unsafe): none.
* TLS: the only `check_hostname = False` is inside `_build_ssl_context(verify=False)`,
  which is **unreachable from the codebase** — every construction site
  (`http_client.py`, `summarizer.py`, `api/app.py`) uses the default `verify=True`.
* i18n: an AST scan of every literal `i18n.t` key (and every `"creative.*"`
  literal) finds zero keys missing from the catalog after FINDING-2.

## 4. Evidence (exact commands and results on the branch head)

All run against `ded9d0b5` (tree `arena/nexus-master-closure-20261103`) unless
noted; Python from the project venv with `PYTHONPATH=src`.

| Gate | Command | Result |
|---|---|---|
| Full suite | `pytest -q -m "not slow" tests/unit tests/architecture tests/integration` | **6166 passed, 39 skipped** (617 s) |
| Lint | `ruff check src tests scripts` | clean |
| Format | `ruff format --check src tests scripts` | 759 files already formatted |
| Types | `mypy src` | success, 355 source files |
| Deps | `deptry src` | no dependency issues |
| Board / docs / i18n | `pytest -q tests/unit/test_agent_board.py tests/unit/test_docs_integrity.py tests/unit/test_i18n_parity.py` | **212 passed** |
| Creative redelivery mutations | `python scripts/creative_redelivery_mutations.py` | **12/12 killed**, tree restored byte-identical |
| IBPC security mutations | `python scripts/ibpc_security_mutations.py` | **23/23 killed**, 0 blocked |
| Pack-trust mutations | `PYTHONPATH=src python scripts/pack_trust_mutations.py` | **12/12 killed** |
| Board overlap referee | `agent_board.py check --files <changed> --repo bot523h/nexus-ai-agent` | no overlap, exit 0 (45 sources read) |

Note on the mutation harnesses: they mutate the working tree and run focused
tests. Run them with `PYTHONPATH=src` in an isolated worktree; otherwise an
editable install of the package shadows the mutated file and the harness
false-greens (observed as `1/12` for pack-trust without the pin). CI installs the
package in-lane, so it does not see this.

### CI integration

The creative redelivery mutation campaign is now wired into the existing
`trust-mutations` job in `.github/workflows/ci.yml`, with `imageio-ffmpeg`
added to that lane's minimal install set (verified sufficient in a scratch venv:
12/12 killed). The lane already runs `git diff --exit-code` after the IBPC
campaign; the same "tree unchanged" assertion now follows the creative campaign.

## 5. LangSmith and Jules (connectivity checks requested)

* **LangSmith** — the deployed key **authenticates and accepts a real trace**:
  * local `scripts/langsmith_smoke.py` → `RESULT=OK DETAIL=project=nexus-ai-agent-smoke`;
  * GitHub Actions run `41026231669` (workflow `langsmith-smoke`, `main`,
    2026-10-30) → job `langsmith-smoke` success, log line
    `RESULT=OK DETAIL=project=nexus-ai-agent-smoke`.
  * The key is a write/ingest tracing key: it can create runs but cannot read
    projects (HTTP 403 on the read API). That is sufficient for the tracing path,
    which is opt-in and off by default (`docs/architecture/TRACING.md`, D-0031).
  * The GitHub Actions secret list is not readable with the integration token
    (`Resource not accessible by integration`, HTTP 403), so the deployment is
    proven by the successful CI run rather than by listing the secret.
* **Jules** — the provided key's Google Cloud project returns
  `403 SERVICE_DISABLED` (`stitch.googleapis.com has not been used in project
  196187850568`). The Jules API is therefore **not usable** from here, and the
  independent read-only review was performed adversarially in-repo instead.

## 6. Independent review

An adversarial self-review of the candidate diff against `main` was performed
(author ≠ reviewer pass over every changed line, plus the sweep in §3). A
separate agent (Jules) review was attempted and is blocked by the API being
disabled for the provided key — recorded here rather than claimed.

## 7. Limitations (recorded, not hidden)

* The digest → engine TOCTOU window is accepted (§3, NEW-2).
* A crash between publication and enqueue can leave one orphaned `staging_*` /
  immutable input directory; no reaper is added (D-0036).
* Legacy rows without a digest require operator reconciliation (NEW-4).
* The `friendly_render_error` map in `src/nexus_ai_agent/bot/slideshow.py` (used
  only by the **legacy slideshow** notifier, which routes through
  `worker_adapter.ERROR_CODES`) degrades unknown codes to a generic internal
  message. The creative path is fully covered by FINDING-2; this legacy map is
  left as-is (out of the claimed zone) and is not a crash or a leak.

## 8. Change inventory

| File | Change |
|---|---|
| `src/nexus_ai_agent/creative/render_jobs.py` | NEW-1: refuse a symlinked workspace node before resolving |
| `src/nexus_ai_agent/bot/creative_surface.py` | NEW-3: `_harden_private_directory` repairs mode, refuses foreign/symlink nodes |
| `src/nexus_ai_agent/i18n/locales/*.json` (15) | FINDING-2: `creative.failed.input_integrity` |
| `tests/unit/test_creative_surface.py` | NEW-3 regressions |
| `tests/integration/test_creative_surface_redelivery.py` | NEW-1 regression |
| `tests/unit/test_creative_notify.py` | FINDING-2 lockstep + end-to-end regressions |
| `scripts/creative_redelivery_mutations.py` | probes M11, M12 |
| `.github/workflows/ci.yml` | run the creative campaign in `trust-mutations` |
| `docs/DECISION_LOG.md` | D-0036 post-acceptance hardening + revision r16 |
| `.agents/board.json` | claim note with the evidence above |

## 9. Verdict

Three real defects (one P0, one P1, one P2) were found by adversarial
re-validation of the freshly landed boundaries, closed as code with
failing-first regressions and mutation proof, and pushed on
`arena/nexus-master-closure-20261103` for human review. No merge, no
force-push, and no branch-protection change was performed. The recorded
limitations are deliberate and bounded.
