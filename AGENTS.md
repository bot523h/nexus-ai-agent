# AGENTS.md — Multi-Agent Coordination Contract

**Every agent (Arena session, Claude Code instance, human contributor) MUST read this
file before writing a single line of code in this repository.**

Multiple agents work on this repo **in parallel, from separate sandboxes**. The only
shared medium between sandboxes is **this git repository itself**. Coordination is
therefore file-based: a claim board committed and pushed through git, backed by a
small CLI. This mirrors the standard multi-agent pattern (one branch per agent +
worktree-style isolation + a shared task board with leases and stale-lease takeover).

## The 5 rules

1. **CLAIM BEFORE YOU CODE.** Run `python scripts/agent_board.py show`, then
   `... next --branch <your-branch>`, then `... claim <task> --branch <your-branch>`.
   Commit and push the board change **immediately** — an unpushed claim does not exist.
2. **STOP if the zone is taken.** If the task/zone you need is ACTIVELY claimed
   (fresh lease, not expired), do NOT start. Pick another task, or record your
   deferral:
   ```bash
   python scripts/agent_board.py defer <task> --branch <your-branch> \
     --fa "چون عامل دیگری روی این محدوده کار می‌کرد متوقف شدم؛ این کار پس از آزاد شدن ناحیه انجام خواهد شد تا فراموش نشود." \
     --resume-when "P0-security-batch merged to main"
   ```
   The deferral note is persisted in `.agents/board.json → deferred_log` so nothing
   is forgotten.
3. **ONE OWNER PER FILE-ZONE.** Claims carry `exclusive_paths`. Before pushing, run
   `python scripts/agent_board.py check --files <comma-separated changed files> --branch <you>`.
   Exit 1 = overlap with another agent's active lease = do not push that work; defer it.
4. **ONE GATES OWNER.** Only the agent holding `gates_owner: true` runs
   `make lint && make types && make test` against main-bound work. Other agents
   write "deferred to gates owner" in their PR body instead of racing the same gates.
5. **LEASES EXPIRE.** Every claim has a TTL (default 24h). A stale lease is
   auto-released by `show`/`next`/`gc`. Renew by re-running `claim` (heartbeat).
   Finish early → `release`. Blocked → `defer`.

## Current board state (summary — always verify with `show`)

| Task | Zone | Status | Owner |
|---|---|---|---|
| `P0-security-batch` | core-security (auth, api, core, docs) | **done** — PR#30 merged (`5e5009a`); shipped protocol files + audit report only, the code half of its scope never landed | released |
| `P0-security-code-batch` | core-security (auth, api, core, handlers) | **done** — v3.13.0 delivered the code half (`delivered` list in the board) | `arena/01a0c3aa-nexus-ai-agent` |
| `feature-wiring-batch` | feature-wiring (features/, handlers wiring) | **done** — tools/games/referral/gamification/anon-chat wired | `arena/01a0c3aa-nexus-ai-agent` |
| `P0-memory-consent-batch` | feature-wiring | queued | unclaimed |
| `stub-command-truth-batch` | feature-wiring | queued | unclaimed |
| `handlers-decomposition-batch` | core-security | queued | unclaimed |

**Closing a claim.** `release` is not enough on its own: write what was actually
delivered into the claim's `delivered` list and point `evidence` at the tests that
prove it. PR#30 merged with an active lease whose declared scope was never
committed, and the next agent had to rediscover that by reading the code.

**Orphaned-lease rule (learned 2026-09-21).** A claim whose branch has already been
merged to `main` is finished even if its lease has not expired — the owning sandbox
is gone and nobody will ever run `release`. If `show` reports an `active` lease whose
`agent_branch` is merged, the next agent may reclaim it, but MUST record the takeover
in `.agents/board.json → takeover_log` with the merge commit as evidence. `gc_expired()`
cannot catch these because TTL is measured from `claimed_at`, not from the merge.

Full details: `.agents/board.json` · Protocol: `docs/MULTI_AGENT_PROTOCOL.md`
(فارسی: `docs/MULTI_AGENT_PROTOCOL.fa.md`)

## Merge order & shared files

`src/nexus_ai_agent/bot/handlers.py` is the single highest-conflict file in the repo.
It is listed in **both** zones' `exclusive_paths` on purpose: whoever holds the active
lease owns it; the other agent must not touch it until the lease is released and the
PR is merged. After a merge to `main`, rebase your branch on `main` before continuing.
