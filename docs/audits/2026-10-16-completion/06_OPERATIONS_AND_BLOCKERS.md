# 06 — Operations, supply chain, and the blockers that stay external

Nothing in this document is inferred. Every claim carries the run id, the command
or the file it came from — and where a claim could only be made by pretending, the
pretence is named instead.

## Backups — `BLOCKED_BY_EXTERNAL_DEPENDENCY` (re-verified today)

The scheduled `maintenance` workflow ran at 2026-10-16T08:12Z and **failed**:

| Field | Value |
|---|---|
| run | [38872121719](https://github.com/bot523h/nexus-ai-agent/actions/runs/38872121719) |
| workflow | `maintenance` (cron `17 3 * * *` backup job, `23 4 * * 1` housekeeping) |
| failing step | *Preflight — required production backup configuration* |
| annotation | `missing repository secrets: NEXUS_DATABASE_URL R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_BUCKET` |

Two facts follow, and both matter:

1. **The blocker is real and current**: five configuration values are absent from
   the repository's secret store. `gh api .../actions/secrets` is not readable with
   this token (integration tokens cannot list repository secrets), so the
   annotation above is the authoritative evidence available here.
2. **The preflight is fail-closed and works**: the job refuses before touching the
   database or the bucket. A CI-local SQLite backup test is *not* a production
   restore drill, and no such claim is made anywhere in this package.

**Unblocking procedure** (owner, no code change needed): add the five secrets in
Settings → Secrets and variables → Actions, then re-run the workflow:

```bash
gh workflow run maintenance.yml --ref main
gh run watch <new-run-id>            # backup job must reach its success step
```

Acceptance for calling backups complete: a dated R2 object with a checksum, a
restore of that artifact into a disposable database, and `PRAGMA integrity_check`
/ `pg_dump --list` output attached to the run — none of which can be produced from
this environment.

## Scheduled dependency audit — the workflow exists but cannot run yet

`.github/workflows/dependency-audit.yml` (weekly `41 5 * * 2` + `workflow_dispatch`,
`pip-audit` over the declared dependency set, JSON artifact kept 90 days) is on the
work branch only. Verified live:

```
$ gh workflow run dependency-audit.yml --ref arena/26f7d958-nexus-ai-agent
HTTP 404: Not Found (.../actions/workflows/dependency-audit.yml)

$ gh api repos/bot523h/nexus-ai-agent/actions/workflows --jq '.workflows[].path'
.github/workflows/ci.yml
.github/workflows/maintenance.yml
… (dependency-audit.yml absent)
```

GitHub resolves scheduled/dispatched workflows from the default branch, so this
workflow **cannot run until #218 is merged**. That is exactly why the workflow's
existence is not presented as an audit result: the last real audit evidence remains
the local `pip-audit` run recorded in the overnight package (7 advisories, 0 with
a fix available). First action after merge:

```bash
gh workflow run dependency-audit.yml --ref main && gh run list --workflow dependency-audit.yml --limit 1
```

## Supply chain and container

| Item | Decision (with the evidence that supports it) |
|---|---|
| Python runtime | stay on the `>=3.10` matrix that CI actually proves (3.10/3.11/3.12 legs green in run 38810548962). No move to 3.14: nothing in this repository has executed on it, and `datetime.UTC` (3.11+) already forced one compatibility fix |
| Lockfile | **still absent** — `pyproject.toml` declares ranges; a hash-locked install plan (pip-tools/uv-style constraints) is a P1 item with its acceptance criterion below |
| Container | single-stage `python:3.12-slim` build that compiles `llama-cpp-python` from sdist; CI job `container` builds the image, runs it unprivileged and probes the mode-aware healthcheck on every PR (green in both runs above). Multi-stage is a P2: the build tools are not on the runtime path today, and the change must keep the healthcheck probe green |
| Live deployment | **not performed and not claimed.** The CI container job is a probe, not a deployment; no operator smoke test exists for this session |

Acceptance criteria for the two deferred P1s:

* lockfile: `pip install --require-hashes -r requirements.lock` succeeds on the CI
  matrix, and a deliberately tampered hash fails the install (negative control).
* `dependency-audit.yml`: first real run artifact contains a machine-readable JSON
  report and the job fails on a new advisory with an available fix.

## Observability, secrets and PII — status

* Correlation identity: `bot/handlers.py` logs a `correlation_id` for incoming
  updates, and the job/attempt records carry their own identifiers, but there is no
  single propagated correlation id from request → intent → attempt → command run →
  artifact → verification. The decision recorded in the overnight package stands:
  add an optional `correlation_id` job field (NEXT) rather than adopting
  OpenTelemetry, which would add a dependency without a measured gap.
* Log content: the A-12/A-13 sweep replaced f-string logging with parameterised
  `%s`/structlog arguments across the audited modules, which removes the class of
  leak where a formatted message is built (and persisted) even when the level is
  filtered. No prompt/token/identifier is written by the new code paths added here;
  the new tests assert that a 5 000-character argument is truncated to ≤200
  characters and that a HuggingFace token never appears in a URL.

## What ran and what did not (this session)

| Ran | Not run |
|---|---|
| `ruff check`/`format`, `mypy --strict src` (284 files), full local suite 4262 passed / 37 skipped | no production backup or restore (secrets absent) |
| `tests/architecture` inside the suite (complexity ratchet, reachability law, import boundaries) | no live Telegram session, no live LLM/vendor call, no model download |
| PR census against the live API; board claim check; CI runs read per job | no container build locally (no Docker in this environment — the CI `container` job is the evidence) |
| mutation kill for the HuggingFace fix (11 failures on the old parse) | no live deployment smoke test (no deployment access) |
