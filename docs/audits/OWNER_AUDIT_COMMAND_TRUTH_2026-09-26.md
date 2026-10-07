# Owner audit — command truth and runtime hardening

**Date:** 2026-09-26
**Branch:** `arena/01a0df78-nexus-ai-agent`
**Baseline:** `6624a133329a22f1b66dce463e0b207385ee8416` (`main`)
**Board task:** `task-193-owner-audit-command-truth`, zone `command-truth-and-runtime-hardening`
**Mandate:** review the project line by line as its owner, and convert every
weakness found into an engineered strength rather than a cosmetic patch.

---

## 0. Gate evidence

Both runs use the repository venv (`.venv/bin/python`); the system interpreter
has no dependencies installed and any failure there is environmental.

| Gate | Command | Baseline `6624a13` | After |
|---|---|---|---|
| Lint | `.venv/bin/python -m ruff check --no-cache .` | `All checks passed!` | `All checks passed!` |
| Format | `.venv/bin/python -m ruff format --no-cache --check .` | clean | `508 files already formatted` |
| Types | `.venv/bin/python -m mypy src` | `no issues found in 241 source files` | `no issues found in 245 source files` |
| Tests | `.venv/bin/python -m pytest -q -m "not slow"` | `2323 passed, 30 skipped` | **`2616 passed, 30 skipped`** |

Collected total: **2353 → 2647** (`pytest --collect-only`). Net **+294 tests**,
**0 tests deleted**, **0 tests weakened** — the two existing assertions that
changed are both listed in §6 with the reason each was asserting a defect.

---

## 1. The central finding: eleven commands were fake, and their engines were already built

The repository already had a `README.md` "Real vs. Simulated" honesty table, a
`bot/surface/` package of genuine implementations, and a
`tests/unit/test_surface_registration.py` ratchet holding the retired stub
strings. The pattern was established. It had simply not been finished.

Grep for importers proved the shape of the problem — the engines were not
missing, they were **orphaned**:

```
$ grep -rn "ModerationEngine\|has_profanity\|is_flooding\|is_muted" src
```

The only hit outside `features/moderation.py` itself was a single `set_config`
call. `add_warning`, `mute_user`, `unmute_user`, `get_reputation`,
`has_profanity`, `is_flooding` and `check_message` had **no caller anywhere in
`src/`**. Same story for `GeminiEngine.vision()`.

| Command | Old answer | Why it was worse than "unimplemented" |
|---|---|---|
| `/warn` | `"⚠️ User warned (1/3)."` | The counter never moved. A moderator could warn the same user a hundred times and always read `1/3`. Nothing was persisted, so `max_warnings` could never be reached and the auto-mute was unreachable. |
| `/mute` | `"🔇 User muted for 10 minutes."` | Nobody was muted. The operator believed the group was protected. |
| `/unmute` | `"🔊 User unmuted."` | Reported success for a user who was never muted. |
| `/reputation` | `"👤 User Reputation: 85/100 (Good)."` | **There is no `/100` scale in the schema.** `UserReputation` stores an unbounded integer. The number was not a rounding of anything. |
| `/mod_config` | `"🛡️ Moderation rules updated."` | Claimed success for *every* invocation, including one with no arguments. |
| `/viral_stats` | `"🔥 Viral Engine: 12 posts sent, 450 likes total."` | **No column in `ViralPost` records reactions** (`chat_id`, `text`, `viral_score`, `status`, `posted_at`). The likes figure could not have been produced by any query. |
| `/viral_post` | `"📋 Pending viral posts: 3 in queue."` | Constant `3`, whether the queue held zero rows or nine hundred. |
| `/viral_preview` | `"🔥 Preview: Top AI trends of the week..."` | Not a preview of anything. |
| `/vision` | `"I see a beautiful landscape in this image."` | Returned without downloading — or looking at — the photo. Identical for a chest X-ray and a screenshot. |
| `/storage` | *(nothing — body was `pass`)* | See below. |
| `/model` | *(nothing — body was `pass`)* | See below. |
| `/story_style` | `"استایل فعلی: Motivational / گزینه‌ها: Motivational \| Romantic \| Success"` | `AIStoryGenerator.create_story()` opens with `_ = style`. **No value the user picks changes a single pixel.** |

### Why `/storage` and `/model` were the worst of the set

```text
async def storage_cmd(update, context) -> None:
    pass                      # registered at handlers.py:1304

async def model_cmd(update, context) -> None:
    pass                      # registered at handlers.py:1305
```

Registered means Telegram **advertises them in the command menu**. A user taps
the command and gets no reply, no error, and no log line. From the user's side
that is indistinguishable from the bot being down, and it is undebuggable —
there is nothing to grep for.

This class of defect now has a ratchet of its own:
`test_no_registered_command_answers_with_silence` walks the AST of
`handlers.py`, finds every locally-defined handler reachable from a
`CommandHandler(...)` registration, and fails if any of them degenerates to a
docstring plus `pass`.

### What replaced them

Three new modules on the existing `bot/surface/` pattern — duck-typed through
`._ptb`, so `telegram` is never imported and the frozen import boundary in
`tests/architecture/test_import_boundaries.py` is untouched:

* **`bot/surface/moderation.py`** — real `ModerationEngine`. Owner-gated
  writes, open reads, per-`(user, chat)` scope, all engine calls through
  `asyncio.to_thread` (the engine is synchronous SQLite). Bounds on everything
  an operator can type: warnings `1..20`, mute `1..1440` minutes.
* **`bot/surface/viral.py`** — real `ViralEngine`. Reports the four counts the
  table can answer (`total`/`pending`/`posted`/`failed`) and **states plainly
  that reactions are not measured** instead of inventing a likes counter.
* **`bot/surface/status.py`** — `/model` renders
  `build_routing_chain(settings)`, the very function `build_llm_provider` uses,
  so the reply cannot drift from the chain that actually serves requests;
  `/storage` reports real on-disk bytes and which cloud backends hold
  credentials. Neither prints a secret — only `Deployment.name` and
  `Deployment.litellm_model` are read, never `api_key`. `/story_style` states
  that style selection is not implemented, because that is the truth.

`/vision` stayed in `handlers.py` (it needs the PTB file-download API) and now
downloads the photo, accepts an optional question, and calls
`GeminiEngine.vision()`.

**A bug I introduced and then caught.** The first draft of
`parse_duration_minutes` only read a duration when two numbers were present, so
a reply-based `/mute 45` silently fell back to the 30-minute default — a
moderation command disregarding its own argument, which is the same class of
defect as the stub it replaced. The caller now passes `target_from_reply`
explicitly, and `test_reply_based_mute_honours_its_only_number` pins it.

---

## 2. `ModerationEngine` was not merely unused — it was wrong

Because nothing called it, three defects sat undetected in code whose entire
purpose is punitive. A false positive here costs a real user a warning and, at
`max_warnings`, a mute.

### 2.1 The profanity filter punished ordinary words

The terms were joined into a bare alternation and matched with `re.search`, so
every entry matched as a **substring**:

```python
_PROFANITY_RE = re.compile("|".join(_PROFANITY_PATTERNS), re.IGNORECASE)
```

`خر` therefore fired inside `خرید` ("purchase"), `آخر` ("last"), `خروج`
("exit") and `مخرب` ("destructive"); `رید` fired inside `بگیرید` ("take").

Measured against the corpus now in `tests/unit/test_moderation_engine.py`, old
regex vs new:

```
word                                          OLD flags?   NEW flags?
------------------------------------------------------------------------
خرید                                          FLAGGED      ok
می‌خرید                                       FLAGGED      ok
آخر                                           FLAGGED      ok
خروج                                          FLAGGED      ok
مخرب                                          FLAGGED      ok
بگیرید                                        FLAGGED      ok
گردید                                         ok           ok
مغز                                           FLAGGED      ok
سگال                                          FLAGGED      ok
لطفا برای خروج از برنامه دکمه را بزنید        FLAGGED      ok
این محصول را از فروشگاه خرید کردم             FLAGGED      ok
------------------------------------------------------------------------
innocent corpus (11): OLD false positives = 10, NEW = 0
insult corpus   (8): OLD misses          = 0, NEW = 0
```

**10 of 11 innocent words were punished. Zero are now, with no loss of true
positives.**

The fix anchors each term between non-word boundaries — and `\b` alone is not
sufficient for Persian. U+200C ZERO WIDTH NON-JOINER glues `می‌خرید` into one
word while counting as a non-word character, so it has to be named explicitly:

```python
def _whole_word(term: str) -> str:
    return rf"(?<![\w{_ZWNJ}]){re.escape(term)}(?![\w{_ZWNJ}])"
```

`مغز` ("brain") was also dropped from the list outright: it is not profanity in
any context.

### 2.2 The mute path crashed on its first read

`mute_user` wrote an **aware** datetime; the column and `is_muted` used naive
UTC. The first `is_muted` call after any mute raised
`TypeError: can't compare offset-naive and offset-aware datetimes`. The
moderation path failed exactly when it was used. `is_muted` now normalises
through `as_naive_utc()`, which also handles rows written by an older release
or by PostgreSQL `timestamptz`.

### 2.3 The flood tracker was an unbounded process-lifetime dict

One entry per user, never evicted — a memory leak proportional to the bot's
audience. It is now an `OrderedDict` LRU capped at `FLOOD_TRACKER_MAX_USERS =
10_000`, with `time.monotonic()` instead of `time.time()` (wall-clock jumps and
NTP corrections no longer corrupt the window) and a `reset_flood_tracking()`
that tests can scope.

Also in this pass: the per-call `create_engine()` in both `moderation.py` and
`viral_engine.py` became a process-wide cache keyed by `db_path` under a lock,
and a data-corruption bug in a `viral_engine.py` template — the CJK word `混淆`
spliced into a Persian sentence (`چرا برنامه‌نویس‌ها混淆 میشن؟`) — was corrected
to `گیج`.

---

## 3. The self-update could freeze the bot permanently

`do_update` is an `async def` that called:

```python
subprocess.run(["git", "pull"], check=True)
subprocess.run([sys.executable, "-m", "pip", "install", "."], check=True)
```

Four defects stacked in two lines, and they compound:

1. **The event loop was blocked.** `subprocess.run` is synchronous. A `pip
   install .` routinely takes 30–120 seconds, and for that entire window the
   bot answered *nobody*.
2. **No timeout.** `subprocess.run` without `timeout=` waits forever.
3. **stdin was inherited.** If the remote asks for credentials, `git` blocks on
   a terminal prompt no daemon can answer. Combined with (1) and (2): a
   permanent, unrecoverable freeze with no crash, no restart, no log line.
4. **Output was discarded.** The owner got `returned non-zero exit status 1`
   and nothing else — no "merge conflict", no "permission denied".

The replacement runs every command through `_run()`:
`asyncio.create_subprocess_exec` (never a shell), `cwd` pinned to the
repository root instead of whatever directory the daemon started in,
`stdin=DEVNULL`, `GIT_TERMINAL_PROMPT=0`, a per-command timeout, and captured
output folded into the failure message. Each property has its own test,
including `test_run_does_not_block_the_event_loop`, which runs a heartbeat
coroutine during the subprocess and asserts it keeps ticking.

**A fifth defect, found while fixing the others.** The version check was
`latest != self.current_version`. `running_version()` yields `3.13.0` from
distribution metadata; GitHub release tags conventionally carry a `v`. A bot on
the newest release therefore reported "update available" **on every single
poll, forever**. Both sides are now normalised, and `check_for_update` goes
through `core/http_client.py` so it inherits the project's timeout, retry
policy, per-host circuit breaker and SSRF guard like every other outbound call.

---

## 4. Security and configuration

| Finding | Risk | Fix |
|---|---|---|
| **CORS wildcard + credentials** was reachable by config. `bool(["*"])` is `True`, so `NEXUS_API_CORS_ORIGINS=*` — a plausible thing to try while debugging — made Starlette reflect the caller's `Origin` *and* send `Access-Control-Allow-Credentials: true`. | Hands every website on the internet an authenticated channel to the API; the exact configuration the same-origin policy exists to prevent. | `cors_policy()`: a wildcard is still honoured, but only ever as an **anonymous** one. Credentials are forced off. |
| **`/recent_users?limit=` was an unvalidated `int`.** | One authenticated `?limit=100000000` materialises the whole user table into memory. `?limit=-1` is read by SQLite as *no limit*. | `Query(5, ge=1, le=100)` — out of range is a 422, not a table scan. |
| **Signing keys under 32 bytes were accepted.** The base64 branch took any decodable string, so `NEXUS_SIGNING_KEY="abcd"` yielded a **3-byte** key. | A 2^24 keyspace on the component whose only job is proving a delivery pack was not tampered with. The Ed25519 path would additionally have raised a raw `ValueError` instead of the typed `SigningError`. | `MIN_KEY_BYTES = 32` floor, typed failure. |
| **The container ran as root.** | The process terminates untrusted input from any Telegram user, hands attacker-supplied media to ffmpeg (large C codebase, long CVE history), and can self-update. A parsing bug started from uid 0. | Non-root `nexus` user (uid 10001), `chown -R` so the writable paths still work, guarded by four new contract tests including a red-proof. |
| **Creative scratch defaulted to `/tmp/nexus_creative`.** `get_settings()` `mkdir -p`s it at import time. | A *predictable* path in a world-writable directory, created on every process start: the classic CWE-377/CWE-59 symlink-swap pair. | Default moved to `data/creative_tmp`, inside the trust boundary that already holds the database and vector store. Operators who want a tmpfs now make that an explicit decision. |
| **Seven settings had no `validation_alias`.** The model declares no `env_prefix`, so pydantic-settings fell back to the bare field name. | 72 of 73 settings read `NEXUS_*`; these seven were reachable only as `CREATIVE_TEMP_DIR`, `WORKSPACE_ROOT`, `N_CTX`, `N_GPU_LAYERS`, `MAX_SHORT_TERM_MESSAGES`, `MAX_TOKENS_BEFORE_SUMMARY`, `TOP_K_MEMORIES`. An operator following the documented convention was **silently ignored and got the default**. `workspace_root` is the containment root the file/shell tools resolve against, so someone who set `NEXUS_WORKSPACE_ROOT` to narrow the sandbox believed they had, while it stayed at `"."`. | All seven accept both spellings via `AliasChoices`, prefixed wins. Made mechanical by `tests/unit/test_settings_env_contract.py`, which parametrises over **every** field. |

> The last row is worth dwelling on: that defect was found *by* the test
> written for the first one. I fixed `creative_temp_dir`, wrote a
> parametrised contract to stop it recurring, and the contract immediately
> failed on six more fields — including the sandbox root.

---

## 5. Correctness sweep

* **`datetime.utcnow()` — 13 call sites → 0.** Deprecated since Python 3.12 and
  scheduled for removal; it also returns a *naive* value while newer code
  writes aware ones, which is the precise mechanism behind the `/mute` crash in
  §2.2. New `core/timeutil.py` provides `utcnow()` (naive UTC, the persisted
  spelling), `utcnow_aware()` and `as_naive_utc()`. Pure stdlib, importable
  from any layer. The column semantics are unchanged, so no migration is
  needed.
* **`hashlib.md5` without `usedforsecurity=False` — 3 sites → 0.** All three
  are filename fingerprints, never security primitives, but on a FIPS-enabled
  OpenSSL build the call raises `ValueError` and takes the whole route down.
* **`RUF006` dangling task** in `adapters/langgraph/lifecycle_recording.py`.
  `loop.create_task` keeps only a weak reference, so a task nobody holds can be
  garbage-collected mid-await. The lifecycle recording would vanish with **no
  error and no log line** — and the health gate would count neither a success
  nor a failure for it, so it would never notice. Now held in a self-pruning
  `_inflight` set, with a `drain()` that `flush()` awaits.

---

## 6. The two existing assertions that changed

Per the evidence rule, both are listed with the reason, because neither was
weakened:

1. `tests/unit/test_delivery_signing.py::test_base64_key_fallback_round_trip`
   used a **30-byte** key — i.e. it asserted that an under-length key was
   acceptable. It now uses 32, and `test_short_key_is_rejected` covers the
   rejected range.
2. `tests/unit/test_updater.py` patched `subprocess.run` and asserted
   `calls == [["git", "pull"], …]`. It now patches `updater._run` and asserts
   `[["git", "pull", "--ff-only"], …]`, plus eleven new properties the old
   call could not express.

---

## 7. Stopped at the fence: presence

`install_presence_heartbeat()` had a body of `pass` under the docstring "Mock
presence heartbeat for now.", nothing in `src/` or `tests/` called it, and
`bot/app.py:373` carried a commented-out call noting it was "an unawaited
mock". Net effect: `PresenceStore.mark_online()` had **no caller anywhere in
`src/`**, so every user read as permanently offline.

Deleting the mock is safe standalone and is done. *Replacing* it needs a
catch-all `TypeHandler` registered in `bot/app.py` at a group after the `-1`
access guard — and `bot/app.py` is an exclusive path of
`task-181-gate5-closure` (active, claimed 2026-09-24T23:06:38Z), with PR#94
("bound personality and presence lifecycle") already in that lane.

A working implementation was written, verified against PTB's
one-handler-per-group semantics, and then **reverted**, with the reasoning
recorded in `.agents/board.json` `deferred_log` so it resumes when the zone
frees. `scripts/agent_board.py check` returns `no overlap — safe to proceed`.

---

## 8. Reproducing this audit

```bash
.venv/bin/python -m ruff check --no-cache .
.venv/bin/python -m ruff format --no-cache --check .
.venv/bin/python -m mypy src
.venv/bin/python -m pytest -q -m "not slow"

# the profanity comparison in §2.1, old regex vs new
.venv/bin/python - <<'PY'
import re
OLD = re.compile("|".join(["خرف","احمق","دیوانه","مغز","کثیف","حقیر",
                           "نادان","ابله","رید","خر","گوساله","سگ"]), re.IGNORECASE)
from nexus_ai_agent.features.moderation import ModerationEngine as M
from tests.unit.test_moderation_engine import INNOCENT, PROFANE
print("old false positives:", sum(bool(OLD.search(w)) for w in INNOCENT), "/", len(INNOCENT))
print("new false positives:", sum(M.has_profanity(w) for w in INNOCENT), "/", len(INNOCENT))
print("new misses on insults:", sum(not M.has_profanity(w) for w in PROFANE), "/", len(PROFANE))
PY
```

---

## 9. Known-remaining, deliberately not touched

Recorded so the next reviewer does not mistake silence for absence:

* `ASYNC230` blocking `open()` in async functions — 9 sites across
  `api/app.py`, `bot/app.py`, `bot/handlers.py`, `features/story_gen.py`. Real,
  but small files on already-slow paths; two of the files are in other agents'
  zones.
* `/leaderboard`, `/newchat` (claims to clear history but does not),
  `/companion`, `/analyze` remain simulated and are still listed as such in the
  README.
* No durable retry scheduler; the job queue is in-process only.
* PostgreSQL restore has never been drilled.
* `ShellTool` has no kernel-level sandbox.
* `ssrf_guard.py:173-174` builds an `SSLContext` with `check_hostname=False` /
  `CERT_NONE`. Reviewed this pass — it is confined to the pinned-IP connection
  path where the hostname is verified separately — but it deserves its own
  focused review.
