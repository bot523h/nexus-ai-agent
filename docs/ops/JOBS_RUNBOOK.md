# Background jobs — operator runbook

The bot is a modular monolith: background work (PDF indexing, story
rendering) runs **inside the bot process** through the in-process
`JobQueuePort` adapter (`adapters/in_process_job_queue.py`). Durable job
state lives in a SQLite **sidecar file** next to the application database:

```
<NEXUS_DB_PATH>.jobs        # job rows (pending / running / retrying / succeeded / failed)
<NEXUS_DB_PATH>.jobs.lock   # advisory ownership lock (flock), see below
```

Nothing here touches the Alembic-managed schema; there is no migration.

## What happens on shutdown

Both run modes (polling and webhook) drain the queue in PTB's `post_stop`
hook: in-flight jobs get up to `JOB_DRAIN_TIMEOUT_SECONDS` (30 s) to finish
while the bot is still initialised, so completion notices are still sent.
Jobs that do not finish in time are cancelled and keep their `running` row —
that is the truthful state ("interrupted"), and it is what `nexus jobs
resume` recovers.

## Nothing is resumed automatically (D1)

The bot never re-dispatches leftover rows at boot. With two live instances
(deploy overlap on Koyeb, a second replica, an operator shell) each would
classify the other's live `running` rows as interrupted and execute them a
second time. Recovery is therefore an explicit operator action.

## `nexus jobs resume`

```bash
nexus jobs resume                    # dry run: list unfinished jobs, change nothing
nexus jobs resume --apply            # re-run them here, wait up to --timeout (300 s)
nexus jobs resume --apply --no-notify
nexus jobs resume --apply --timeout 900
```

* **Dry run is the default** and only reads the store.
* `--apply` requires **store ownership**. The bot holds the advisory lock
  while it runs; if it (or another operator run) does, the command exits
  with code 2 and `refusing to resume: another process owns the job store
  (is the bot running?)`. Stop the bot first, resume, then start it again.
  Enqueue/read never need the lock — a bot that starts while an operator
  holds it keeps serving and picks ownership up when the lock is free.
* `--notify` (default) sends the same completion notice the bot would
  (story PNG delivered, PDF page count, or a user-facing failure reason).
  It needs `TELEGRAM_BOT_TOKEN` and network access; if Telegram is
  unreachable the command exits 2 **before** resuming anything — re-run
  with `--no-notify`.
* Exit codes: `0` every resumed job succeeded (or nothing to do); `1` some
  job failed or did not finish within `--timeout` (details in the log and
  the job rows); `2` refused (store owned elsewhere / Telegram unreachable).

Interrupted `running` rows go through the legal
`running → failed → retrying → running` edges, so the attempt count and the
`interrupted: …` error stay visible in the row.

## Inspecting the store by hand

```bash
sqlite3 "$NEXUS_DB_PATH.jobs" \
  "select id, job_type, status, attempts, error, updated_at from jobs order by created_at"
```

Do not edit rows by hand while the bot is running; the adapter applies every
status change as a compare-and-set update and expects to be the only writer
of `running` rows.
