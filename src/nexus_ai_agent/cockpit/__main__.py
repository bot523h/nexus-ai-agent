"""Opt-in entry point; see --help for setup, security and the evidence boundary."""

from __future__ import annotations

import argparse
import os

from nexus_ai_agent.cockpit.app import CockpitConfig, create_app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NEXUS Cockpit: Persian-first catalog explorer and effect-free input lab.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # Explicit public preview: installed catalog only, no bot/DB/provider access.
  python -m nexus_ai_agent.cockpit --preview --port 3000

  # Protected deployment: inject a random >=32-character token via environment.
  # Never put a secret in a command-line flag, URL, source file, or browser storage.
  python -m nexus_ai_agent.cockpit --port 3000

NEXUS_COCKPIT_TOKEN is the only environment variable this entry point reads.
The .env file is NOT loaded. Use your platform's secret environment and HTTPS
reverse proxy. All assets, fonts, and API requests are same-origin; no CDN,
model download, npm build, database migration, or Telegram token is required.

The validation result covers INPUT SCHEMA ONLY. It does not authorize or run a
command, resolve references, check installed binaries, or verify an artifact.
Registered is not activated; a builtin manifest is not a verified signature.
The catalog fingerprint is a metadata digest, NOT execution evidence.

Resource limits: 32 KiB JSON, depth 24, 2048 values, 4 simultaneous validations
per process, 10-second body deadline. Public ingress rate limits / TLS belong
at your proxy. Preview is intentionally public; never mistake it for auth.

Browser smoke (optional dev dependency, not needed to run the UI):
  pip install playwright && python -m playwright install chromium
  python scripts/test_cockpit_browser.py --url http://127.0.0.1:3000
""",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=3000, help="Listen port (default: 3000)")
    parser.add_argument(
        "--preview", action="store_true", help="Explicitly expose only the safe local catalog/lab"
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    try:
        config = CockpitConfig(
            public_preview=args.preview, token=os.environ.get("NEXUS_COCKPIT_TOKEN")
        )
    except ValueError as exc:
        parser.error(str(exc))
    if not config.public_preview and not config.token:
        parser.error("Set NEXUS_COCKPIT_TOKEN in the environment, or explicitly choose --preview.")
    import uvicorn

    print("NEXUS Cockpit · local catalog only · command execution disabled", flush=True)
    # Disable access logs: they could contain a mistakenly pasted token in a URL.
    # This application has no reason to log request content or authentication.
    uvicorn.run(create_app(config=config), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
