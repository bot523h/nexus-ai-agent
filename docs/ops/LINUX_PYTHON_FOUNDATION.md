# Linux + Python Foundation Contract

## Canonical baseline

Nexus uses **Ubuntu 26.04 LTS (Resolute Raccoon)** as its canonical Linux substrate and **CPython 3.14.x** as its application runtime. The selected patch baseline is **Python 3.14.8**. The contract is declared in `[tool.nexus.runtime]` in `pyproject.toml` and checked by `scripts/check_runtime.py`.

This is an application-runtime contract, not a language migration: Python remains the core runtime. Java, Go and Rust are not introduced.

## One canonical path

| Stage | Command / definition | Proof boundary |
|---|---|---|
| Developer bootstrap | `make dev-bootstrap` | `scripts/bootstrap_dev.sh` → `scripts/bootstrap_env.py` |
| Runtime contract | `make runtime-check` | `scripts/check_runtime.py` |
| Receipt | `make runtime-receipt` | SHA, branch, OS, Python, package manager, application version |
| Build | `docker build -t nexus:foundation .` | `Dockerfile` |
| Boot / health | `docker compose up` and `GET /healthz` | `docker-compose.yml`, `src/nexus_ai_agent/api/app.py` |
| Tests | `make lint && make types && make test` | repository gates |

## System dependencies

The production image declares Python 3.14, pip/venv tooling, a compiler toolchain for packages without wheels, FFmpeg for the slideshow lane, `libmagic1`, `libgl1` and Liberation fonts. Node, Java, Go and Rust are not runtime dependencies.

## CI parity

CI's standard lint/test/extras legs run on Python 3.14. The compatibility matrix continues to exercise the declared `requires-python >=3.10` floor on Python 3.10–3.12. The `runtime-foundation` job uses an Ubuntu 26.04 container and runs the contract, installation, import and health smoke checks. A green documentation claim must refer to the exact commit's CI run; local success alone is not a CI proof.

## Security posture

The image creates and runs as an unprivileged `nexus` user, installs packages without a persistent pip cache, keeps writable application state under `/app/data`, and does not copy `.env` into the image. Secrets remain runtime environment inputs.

## Known limitations

- The current sandbox used for local verification may be Ubuntu 24.04 and Python 3.12; that is an environment limitation, not evidence of the canonical baseline.
- Dependency resolution is recorded in the committed `uv.lock` for the canonical Python 3.14 resolution. CI still publishes an exact-SHA `pip freeze` artifact because the compatibility matrix intentionally exercises Python 3.10–3.14 and optional extras separately.
- External Telegram, LLM, database and cloud-provider credentials are not required for the local import/health proof and are not fabricated.

## External references

- [Ubuntu 26.04 LTS release notes](https://documentation.ubuntu.com/release-notes/26.04/) — released 23 April 2026; standard support through April 2031.
- [Python 3.14 documentation](https://docs.python.org/3/whatsnew/3.14.html) — Python 3.14 released 7 October 2025; patch-level compatibility is verified in CI.
