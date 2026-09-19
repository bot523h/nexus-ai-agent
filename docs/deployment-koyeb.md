# Deploying NEXUS to Koyeb (webhook mode, v3.8.0 — Phase 3)

This guide deploys the bot as a **Koyeb `web` service** that **scales to
zero**: when nobody is talking to the bot, the instance is stopped and costs
nothing. Telegram updates arrive as HTTPS POSTs (`webhook mode`) instead of
the bot pulling them forever (`polling mode`).

> **Context:** the previous deployment shape was a `worker`-type service
> running the always-on polling loop (`NEXUS_RUN_MODE=polling`). That shape
> keeps running — and keeps billing — 24/7. Phase 3 adds the webhook path so
> a web-type service can sleep.

## How it works (30-second tour)

| Piece | File | Role |
|-------|------|------|
| Run-mode resolution | `src/nexus_ai_agent/bot/webhook.py` | CLI `--mode` > `NEXUS_RUN_MODE` > `polling` |
| Webhook server | same + `src/nexus_ai_agent/api/app.py` | uvicorn serves `POST /webhook/telegram` and `GET /healthz` |
| Payload adapter | `src/nexus_ai_agent/bot/app.py` | raw Telegram JSON → PTB `Update` → `application.update_queue` |
| Bind port | `bot/webhook.py` | `PORT` > `DASHBOARD_PORT` > `8000` (Koyeb injects `PORT`) |
| Liveness | `GET /healthz` | no database touch; 200 = process alive |
| Shared secret | `NEXUS_WEBHOOK_SECRET` | Telegram echoes it in `X-Telegram-Bot-Api-Secret-Token`; verified constant-time, mismatches get `403` |

## Prerequisites

- A Telegram bot token (from [@BotFather](https://t.me/BotFather)).
- A public HTTPS URL for the service (Koyeb provides one, e.g.
  `https://<app-name>-<org-name>.koyeb.app`).
- The [Koyeb CLI](https://www.koyeb.com/docs/cli) (optional — you can do all
  of this in the web console) and/or a Koyeb API token if you use
  `koyeb.yaml` / Pulumi-style deploy-from-repo.

## Step-by-step

### 1. Generate a webhook secret

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Keep it; you will paste it into two places (Koyeb env and it is also what
`set_webhook(secret_token=...)` sends to Telegram).

### 2. Push the code

The repository ships a `koyeb.yaml` describing the service. Phase 3 changed
it from `type: worker` to `type: web`, sets `NEXUS_RUN_MODE=webhook`, and
points the health check at `/healthz`. Make sure your deploy branch contains
v3.8.0 or newer.

```bash
git checkout main && git pull
git push origin main   # if you deploy-from-repo on push
```

### 3. Create the web service (console path)

1. Koyeb console → **Create Service** → *GitHub* (or *Docker* — the repo has
   a `Dockerfile`).
2. **Service type: `web`** — this is what enables scale-to-zero.
3. Exposed port: Koyeb injects `PORT`; the app honors it automatically
   (`build_webhook_bind()`). Default fallback is `8000`.
4. Health check path: **`/healthz`** (already set in `koyeb.yaml`).
5. Add the environment variables:

   | Variable | Value |
   |----------|-------|
   | `TELEGRAM_BOT_TOKEN` | your BotFather token |
   | `NEXUS_RUN_MODE` | `webhook` |
   | `NEXUS_WEBHOOK_URL` | `https://<app-name>-<org-name>.koyeb.app/webhook/telegram` |
   | `NEXUS_WEBHOOK_SECRET` | the hex string from step 1 |
   | `GEMINI_API_KEY` | *(optional)* for the Gemini engine |
   | `NEXUS_DATABASE_URL` | *(optional)* Neon/PostgreSQL URL — recommended, because a scale-to-zero instance has **no persistent local disk** between wake-ups |

6. Deploy, then note the public URL Koyeb assigns.

> **Tip (CLI path):** `koyeb deploy . <app-name> --dockerfile Dockerfile
> --type web --port 8000` then `koyeb env set <app-name>
> NEXUS_RUN_MODE=webhook ...` — or just let `koyeb.yaml` drive it.

### 4. Verify

```bash
curl -s https://<app-name>-<org-name>.koyeb.app/healthz
# → {"status":"ok"}
```

Then open a chat with your bot and send a message. The first reply may take
a few seconds — see the trade-off section below. Check the Koyeb logs
(`koyeb logs <app-name>`) for the startup banner; the LLM chain line tells
you which provider answered.

## The scale-to-zero cold-start trade-off (explicitly accepted)

When the service is asleep, the first incoming Telegram update wakes it.
**Waking up takes a few seconds** (image pull/attach, Python boot, LLM chain
init, database connect). During that window Telegram's delivery attempts
fail or time out.

**This is a deliberate, acceptable trade-off — not a bug.** Telegram does
not drop the update: it retries webhook deliveries with a backoff (for
hours), so the user's message is answered a few seconds late instead of
never. In exchange, idle hours cost exactly zero. If a specific bot usage
ever becomes latency-critical, pin the service to at least one always-on
instance (`koyeb scale <app-name> web min=1`) or go back to
`NEXUS_RUN_MODE=polling` on a `worker`-type service — both remain
first-class, backward-compatible modes.

Two mitigations already built in:

- `/healthz` answers without touching the database, so the platform's health
  gate passes as early as possible after wake-up.
- Verified-but-not-yet-processable requests get `503` (not `200`), which
  makes Telegram retry rather than drop, closing the tiny startup race.

## Operational notes

- **Secret check:** deliveries whose `X-Telegram-Bot-Api-Secret-Token`
  header does not match `NEXUS_WEBHOOK_SECRET` are rejected with `403`.
  If you rotate the secret, update it in Koyeb env and the service will
  re-register it with Telegram on next start (`set_webhook`).
- **Shutdown:** on scale-in Koyeb sends `SIGTERM`; uvicorn stops accepting
  requests, drains in-flight ones, then the bot application is stopped and
  shut down cleanly (`run_webhook()`). Updates that were accepted (HTTP 200)
  but not yet processed when the process exits are lost — Telegram will not
  re-deliver those. This is bounded (in-flight seconds) and acceptable for a
  chat bot.
- **State:** use `NEXUS_DATABASE_URL` (Neon/PostgreSQL) for anything you
  want to survive scale-to-zero restarts. Local SQLite under `data/` is
  ephemeral on Koyeb web instances.
- **Backward compatibility:** existing `worker`-type deployments keep
  running `NEXUS_RUN_MODE=polling` (the Dockerfile default). Nothing about
  the polling path changed in Phase 3.
