---
name: nexus-nagar-one-engineering
description: >-
  Engineering playbook for the bot523h/nexus-ai-agent repository (NEXUS /
  NAGAR-ONE). Use when working on this repo: claiming board tasks, landing a
  slice as a PR, running the gates, closing review threads, or resuming a stuck
  campaign. Encodes the hard-won rules (verify live truth, claim before code,
  fail closed, keep CI pins in sync) and the concrete mistakes made in earlier
  sessions together with how to avoid them.
---

# NEXUS / NAGAR-ONE engineering playbook

Distilled from the 2026-11-02 → 2026-11-07 sessions. Read this before touching
this repository. It is a *hard rules + known-failure* list, not a tutorial.

## 0. The one rule that prevents the most damage

**Verify live truth; never trust a prior report, a memory note, or an assumption.**
`main` moves, PRs merge, leases expire. Every session that started from a stale
baseline produced wrong conclusions. Re-derive from GitHub/the tree:

```bash
git fetch origin --prune
curl -s -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://api.github.com/repos/bot523h/nexus-ai-agent/git/ref/heads/main   # live main SHA
```

## 1. Coordination contract (from AGENTS.md — mandatory)

1. **CLAIM BEFORE YOU CODE.** `python scripts/agent_board.py show` →
   `next --branch <you>` → `claim <task> --branch <you>`. Commit **and push** the
   board change immediately; an unpushed claim does not exist.
2. **ONE OWNER PER FILE-ZONE.** Before pushing:
   `python scripts/agent_board.py check --files <a,b,c> --branch <you>`
   (ONE comma-separated arg). Exit 0 = disjoint & readable; **exit 1 = proven
   overlap (do not push)**; **exit 2 = a source was unreadable — NOT a pass**.
3. **Branch name is the canonical identity** (letters A/B/C are labels only).
4. **No global gates owner** — run your own `make lint && make types && make test`
   and record the command + result + SHA.
5. **Leases expire (24h TTL)**; renew by re-running `claim`.

Board invariants enforced by `tests/unit/test_agent_board.py` (a board edit that
violates one of these reds the required `test` job):
- every claimable task needs `acceptance_criteria` + `evidence_required`;
- `exclusive_paths` must sit under ONE declared zone and must not collide with
  another ACTIVE claim;
- `deferred_log` entries must reference an existing claim id;
- `next_work` must contain **exactly 10** entries;
- schema 2 (`protocol`, `zones`, `claims`, `deferred_log`, `takeover_log`,
  `next_work`, `history`) must still validate.

## 2. Golden workflow for a slice

1. Verify live main; read `.agents/board.json`, `AGENTS.md`, the target PR/issue.
2. Claim (board) + push; name the branch after the task.
3. Implement the **minimal** change; keep one authority per responsibility.
4. Add a **mutation anchor** for every new guard and extend the campaign count.
5. Run gates locally: `make lint` (also `ruff format --check .` — CI's
   `lint-fast` runs it and a plain `ruff check` misses formatting), `make types`
   (`mypy src` only — **scripts are not type-gated**), targeted tests, then the
   full suite `pytest -q -m "not slow"`.
6. Push the branch; open ONE PR (base `main`) with the board claim, acceptance
   criteria, exact commands and observed results.
7. Drive CI green; address review threads (reply + resolve — see §6).
8. Release/close the board claim with evidence. **Never write to `main`.**

## 3. Fail-closed is the house style

Every new boundary must refuse on the bad path, not degrade:
- a new trust/verification helper fails closed on network/parse/ancestry failure;
- a missing prerequisite, revision, or allow-list entry is a typed refusal;
- `except` must translate to a **typed** error (e.g. `CreativeRenderError` with a
  code), never let a raw `OSError`/`IntegrityError` escape;
- prefer **one combined guard** over two redundant ones (a split guard leaves an
  unkillable mutant — see the `_github_ancestry_proven` lesson).

## 4. Cross-cutting change checklist (the "silent breakage" set)

A new Alembic migration or a new creative error code silently breaks things far
from the change. Before you finish, grep and fix:

| Change | What it silently couples to |
|---|---|
| New Alembic revision | the head literal is hard-coded in migrate/backup/checkpoint test modules, the `migrate-postgres` CI job, and living docs (`RUNTIME_FLOWS.md`, `DATA_AND_STORAGE.md`) → grep `dpstate`-style head strings |
| Any schema change | the PostgreSQL checkpoint **golden embeds the head** → regenerate with the human-only `nexus checkpoints golden update --backend postgres --yes` |
| New creative ERROR_CODE | `creative.failed.<code>` must exist in **all 15 locales** (`tests/unit/test_creative_notify.py`) |
| New test importing a creative op | `OPERATION_TRUTH.json` drifts → `python -m nexus_ai_agent.nagar --docs` |
| New file under `docs/` | must be indexed in `docs/README.md` (`test_docs_integrity.py`) |
| New boundary rule | must name its enforcing test in `docs/architecture/MODULE_MAP.md` §3 |
| New mutation / guard | the pinned count lives in BOTH `tests/unit/test_mutation_harness_anchors.py` AND the `ci.yml` job/step names — keep them in sync |
| System-behaviour decision | record it in `docs/DECISION_LOG.md` (docs-layer decisions → `docs/architecture/adr/`) |

## 5. CI gotchas that cost red runs

- **CI checkouts have NO `origin/main` ref** (shallow single-branch). Any test that
  shells out to real git for `origin/main` fails in CI for environmental reasons —
  stub the git boundary or use `HEAD`.
- **`trust-mutations` installs a hand-picked minimal dep set with NO alembic.** A
  suite run by a mutation harness must NOT import `nexus_ai_agent.storage.migrations`;
  bootstrap schema with `asyncio.run(create_all_tables(path))`. Symptom: harness
  prints `baseline ... RED — refusing to mutate a suite that is already failing`,
  exit 2.
- **Never `pip install -e ".[dev]"` in a workflow** — torch ships CUDA (~2.3 GB) and
  exhausts the runner (Errno 28). Use `scripts/ci_install.sh` (CPU-only torch,
  PyPI-only remainder) or a minimal `--no-deps` install for mutation jobs.
- **GitHub check-runs API returns both the live and the cancelled superseded run**
  for one head → dedupe by name, prefer the non-cancelled entry.
- `gh api .../actions/jobs/<id>/logs` via the gh tool returns 0 bytes; use
  `curl -sL -H "Authorization: Bearer $TOKEN"` on the REST URL instead.
- `default_create_pr` 403s on this repo → create PRs via
  `POST /repos/bot523h/nexus-ai-agent/pulls` with `GITHUB_PERSONAL_ACCESS_TOKEN`.
- Do NOT `git checkout` another branch while a background pytest suite runs — it
  reads the wrong tree and yields bogus failures.

## 6. Review-thread discipline

`Sourcery review` is a real gate: flag it green only after it re-runs on the new
head. For every thread: fix (or justify), reply with the commit SHA, then resolve.

```bash
# list threads (id + isResolved)
gh api graphql -f query='{ repository(owner:"bot523h",name:"nexus-ai-agent"){ pullRequest(number:N){ reviewThreads(first:50){ nodes{ id isResolved path comments(first:1){nodes{body}} } } } } }'
# reply + resolve
gh api graphql -f query='mutation{ addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:"<id>",body:"Fixed in <sha>"}){comment{id}} }'
gh api graphql -f query='mutation{ resolveReviewThread(input:{threadId:"<id>"}){thread{isResolved}} }'
```

## 7. Errors made in earlier sessions — and how to not repeat them

Each row: the mistake, why it happened, the prevention.

1. **Trusted a stored PR-merge record** (`source=="github_api"` + any 40-hex SHA).
   *Why:* the board consumer read stored text instead of re-verifying.
   *Prevention:* re-verify against the **live** GitHub main ref on every read; bind
   the live main SHA into any cache key so a moved main voids a warm cache; fail
   closed on any outage. (PR #316.)
2. **Counted `completed_released` as satisfying a prerequisite.**
   *Why:* the status set was too broad. *Prevention:* `SATISFIED_STATUSES` is
   `{done, completed_merged}` only — a released epic still blocks. (PR #316.)
3. **Trusted a stale "blocked on #N" note.**
   *Why:* the dependency had already landed via a different PR. *Prevention:*
   `git log origin/main -- <path>` before believing a blocker. (task-282 slice-2.)
4. **Assumed the full suite was environment-blocked.**
   *Why:* copied a prior handoff claim. *Prevention:* it runs here (~10–11 min);
   actually run it before claiming otherwise.
5. **A string assertion satisfied by a comment.**
   *Why:* guarded a shell script by substring. *Prevention:* strip comments and
   assert semantic tokens (`endswith("+cpu")`, `startswith("nvidia-")`).
6. **Opened a duplicate PR** for a fix already delivered on another branch.
   *Why:* did not run `agent_board.py check` / read open PRs first.
   *Prevention:* the board + PR list are mandatory reads before editing.
7. **`git reset --soft HEAD~1` after a conflict cleared `MERGE_HEAD`**, losing merge
   ancestry. *Prevention:* redo the merge from scratch; never "fix" ancestry.
8. **Left a hardened slice with unresolved review threads** (PR #319 shipped CI-green
   but with 15 open Sourcery threads). *Prevention:* a slice is not done until its
   review threads are resolved and its PR body reflects the real head.

## 8. Proof and honesty rules

- Numbers in docs must be reproducible from a named command — never invent one.
- Record NOT_PROVEN explicitly (e.g. the remote backup leg needs `NEXUS_DATABASE_URL`
  + a disposable bucket; the `maintenance` red is fail-closed by design, an owner
  config gap, not a code defect).
- Prefer a mutation-proven guard over a prose claim: "19/19 mutants killed" is
  evidence; "looks correct" is not.

## 9. Collaborating with Jules (Google) — the second engineer

The owner runs a Jules session on the same repo (`sources/github/bot523h/nexus-ai-agent`).
Use it for independent audits or a second implementation lane. API key: `$jules`,
header `X-Goog-Api-Key`, base `https://jules.googleapis.com/v1alpha`.

```bash
# 1. list sources (find the repo source name)
curl -s -H "x-goog-api-key: $jules" https://jules.googleapis.com/v1alpha/sources
# 2. create a session (auto-PR optional)
curl -s -X POST https://jules.googleapis.com/v1alpha/sessions \
  -H "x-goog-api-key: $jules" -H "Content-Type: application/json" \
  -d '{"prompt":"...","sourceContext":{"source":"sources/github/bot523h/nexus-ai-agent","githubRepoContext":{"startingBranch":"main"}},"title":"..."}'
# 3. read activities (progress + agent replies)
curl -s -H "x-goog-api-key: $jules" "https://jules.googleapis.com/v1alpha/sessions/<ID>/activities?pageSize=30"
# 4. send a follow-up
curl -s -X POST "https://jules.googleapis.com/v1alpha/sessions/<ID>:sendMessage" \
  -H "x-goog-api-key: $jules" -H "Content-Type: application/json" -d '{"prompt":"..."}'
```

Rules for co-work: give Jules a **bounded, read-only or single-file task**; never
let two lanes edit the same file-zone; treat its output as a proposal to verify,
not authority. Prefer "audit and report" prompts so Jules cannot collide with a
live claim.

## 10. Daily continuation automation

A cron automation ("NAGAR-ONE Daily Continuation") runs at **12:00 Asia/Tehran**,
resumes from live truth, and continues the oldest unblocked work without writing
to `main`. See the automation prompt for the exact steps; it enforces the same
rules as this skill (claim, gates, review threads, no main writes, Persian final
summary).
