# Deploy Runbook — NEXUS AI on Koyeb (webhook, scale-to-zero)

**Scope:** production deploys of the `bot` web service described by
`koyeb.yaml`. Companion docs: `docs/deployment-koyeb.md` (first-time setup),
`docs/ops/NEON_LIFECYCLE_RUNBOOK.md` (database lifecycle).

**Golden rule:** every deploy ends with the smoke script green. If smoke is
red, roll back first and debug second.

```bash
python scripts/deploy_smoke.py --strict                       # offline: manifest + repo contract
python scripts/deploy_smoke.py --strict --url https://<app>.koyeb.app   # + live probes
```

Exit status: `0` = ship it, `1` = a check failed, `2` = bad CLI usage.
Unit coverage lives in `tests/unit/test_deploy_smoke.py` (21 tests).

---

## 1. Preflight (before every deploy)

1. Branch is green on `main` (CI: ruff, format, mypy, pytest).
2. Offline smoke passes from the exact commit you will deploy:
   `python scripts/deploy_smoke.py --strict`.
3. Secrets exist in the Koyeb console (never in git): `TELEGRAM_BOT_TOKEN`,
   `NEXUS_WEBHOOK_URL`, `NEXUS_WEBHOOK_SECRET`, plus `GEMINI_API_KEY` and
   `NEXUS_DATABASE_URL` when those features are wanted.
4. If the deploy includes a database migration, apply it **before**
   switching traffic (see the Neon runbook), and keep the migration
   backward-compatible with the currently running revision.

## 2. Deploy

Deploy-from-repo (push to the tracked branch) or console **Redeploy**.
Koyeb builds from `Dockerfile`, starts the `web` service, and gates traffic
on `GET /healthz` (200 + `{"status": "ok"}`, no database touch).

Watch the first boot for the startup banner and the LLM-chain line:

```bash
koyeb logs <app-name> --type build   # build phase
koyeb logs <app-name>                # runtime: banner + provider line
```

## 3. Smoke (after every deploy)

```bash
BASE=https://<app-name>-<org-name>.koyeb.app
python scripts/deploy_smoke.py --strict --url "$BASE"
```

Expected: 10 checks green — manifest shape, repo contract, `healthz`
liveness, and the `webhook-gate` probe (a bogus secret must get `403`,
proving the secret gate is live; rejections happen before any processing,
so the probe is side-effect free).

Then send the bot one real message. On a cold start the first reply lags a
few seconds (accepted trade-off — Telegram retries; see
`docs/deployment-koyeb.md`).

## 4. Rollback

Koyeb keeps previous revisions. Roll back from the console (**Service →
Revisions → Rollback**) or the CLI, then re-run live smoke:

```bash
koyeb redeploy <app-name> --revision <previous>   # or console rollback
python scripts/deploy_smoke.py --strict --url "$BASE"
```

If the bad revision ran a migration, check the Neon runbook before rolling
back: code rollback is safe only while the migration stays
backward-compatible.

## 5. Wiring smoke into CI (post-#33)

> `.github/workflows/` is owned by the in-flight PR #33. Do **not** edit
> workflows until it merges; then add this job verbatim:

```yaml
deploy-smoke:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-python@v5
      with:
        python-version: "3.12"
    - run: pip install pyyaml
    - run: python scripts/deploy_smoke.py --strict
```

Live probes (`--url`) stay a manual post-deploy step: CI has no production
URL and must never touch production.

## 6. Incident quick-reference

| Symptom | First check | Likely cause |
|---|---|---|
| `/healthz` non-200 | `koyeb logs`, `koyeb describe` | crashed boot / bad env (smoke `manifest` catches shape drift) |
| Bot silent, health green | bot chat + Koyeb logs | cold start (wait, Telegram retries) or wrong `NEXUS_WEBHOOK_URL` |
| All webhooks `403` | secret rotation history | `NEXUS_WEBHOOK_SECRET` mismatch between console and Telegram |
| State lost after idle | `NEXUS_DATABASE_URL` set? | scale-to-zero wiped local disk — use Neon (see Neon runbook) |
| Smoke `manifest` red | `git diff koyeb.yaml` | deploy shape drifted from the documented contract |

Secret rotation: generate (`python -c "import secrets;
print(secrets.token_hex(32))"`), update Koyeb env, redeploy — the service
re-registers the secret with Telegram on next start — then re-run live
smoke.
