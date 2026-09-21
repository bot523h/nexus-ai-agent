"""Koyeb deploy smoke checks for NEXUS AI (task-130).

Offline checks (always run, no network, no side effects):

* ``manifest`` — ``koyeb.yaml`` declares the documented deploy shape: a
  ``web``-type ``bot`` service, health path ``/healthz``, and the required
  webhook env vars (needs PyYAML; ``SKIP`` without it, ``FAIL`` under
  ``--strict``).
* ``contract`` — the repo carries what the manifest promises: a
  ``Dockerfile``, the ``/healthz`` + ``/webhook/telegram`` routes, and 15
  parseable i18n locale files.

Live checks (only with ``--url https://<app>.koyeb.app``):

* ``healthz`` — ``GET /healthz`` answers 200 + ``{"status": "ok"}``.
* ``webhook-gate`` — ``POST /webhook/telegram`` with a bogus secret token
  answers 403, proving the secret gate is live (rejected before any
  processing, so this is side-effect free).

Exit status: 0 when every executed check passes (skips allowed unless
``--strict``), 1 on any failure, 2 on bad CLI usage.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - runner without PyYAML
    yaml = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parents[1]

SERVICE_NAME = "bot"
EXPECTED_SERVICE_TYPE = "web"
EXPECTED_HEALTH_PATH = "/healthz"
EXPECTED_WEBHOOK_PATH = "/webhook/telegram"

REQUIRED_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "NEXUS_RUN_MODE",
    "NEXUS_WEBHOOK_URL",
    "NEXUS_WEBHOOK_SECRET",
)
RECOMMENDED_ENV_VARS = ("GEMINI_API_KEY", "NEXUS_DATABASE_URL")
EXPECTED_LOCALE_COUNT = 15

Status = Literal["PASS", "FAIL", "SKIP"]


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single smoke check."""

    name: str
    status: Status
    detail: str


def check_manifest(manifest_path: Path) -> list[CheckResult]:
    """Validate the Koyeb manifest against the documented deploy shape."""
    results: list[CheckResult] = []
    if yaml is None:
        return [
            CheckResult(
                "manifest",
                "SKIP",
                "PyYAML not installed; install pyyaml to validate koyeb.yaml",
            )
        ]
    if not manifest_path.is_file():
        return [CheckResult("manifest", "FAIL", f"missing file: {manifest_path}")]
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surface any parse failure
        return [CheckResult("manifest", "FAIL", f"unparseable YAML: {exc}")]

    services = manifest.get("services", []) if isinstance(manifest, dict) else []
    matches = [s for s in services if s.get("name") == SERVICE_NAME]
    if not matches:
        return [CheckResult("manifest", "FAIL", f"no service named {SERVICE_NAME!r} declared")]
    service = matches[0]

    if service.get("type") != EXPECTED_SERVICE_TYPE:
        results.append(
            CheckResult(
                "manifest",
                "FAIL",
                f"service {SERVICE_NAME!r} must be type {EXPECTED_SERVICE_TYPE!r} "
                f"(scale-to-zero), got {service.get('type')!r}",
            )
        )
    else:
        results.append(CheckResult("manifest", "PASS", f"service {SERVICE_NAME!r} is type 'web'"))

    health_path = (service.get("health_check") or {}).get("path")
    if health_path != EXPECTED_HEALTH_PATH:
        results.append(
            CheckResult(
                "manifest",
                "FAIL",
                f"health_check.path must be {EXPECTED_HEALTH_PATH!r}, got {health_path!r}",
            )
        )
    else:
        results.append(CheckResult("manifest", "PASS", "health_check.path is /healthz"))

    env_entries = service.get("env", []) or []
    env_names = {entry.get("name") for entry in env_entries if isinstance(entry, dict)}
    missing = [name for name in REQUIRED_ENV_VARS if name not in env_names]
    if missing:
        results.append(
            CheckResult("manifest", "FAIL", f"missing required env vars: {', '.join(missing)}")
        )
    else:
        results.append(CheckResult("manifest", "PASS", "required webhook env vars present"))
    run_mode = next(
        (
            entry.get("value")
            for entry in env_entries
            if isinstance(entry, dict) and entry.get("name") == "NEXUS_RUN_MODE"
        ),
        None,
    )
    if run_mode is not None and run_mode != "webhook":
        results.append(
            CheckResult(
                "manifest",
                "FAIL",
                f"NEXUS_RUN_MODE must be 'webhook' for a web service, got {run_mode!r}",
            )
        )

    absent_recommended = [name for name in RECOMMENDED_ENV_VARS if name not in env_names]
    if absent_recommended:
        # Advisory, not a skip: these are optional by contract (see
        # docs/deployment-koyeb.md), so their absence never blocks a deploy.
        results.append(
            CheckResult(
                "manifest",
                "PASS",
                "optional env vars not in manifest "
                f"(set in console if needed): {', '.join(absent_recommended)}",
            )
        )
    return results


def check_contract(repo_root: Path) -> list[CheckResult]:
    """Validate the repo carries what the manifest promises."""
    results: list[CheckResult] = []

    dockerfile = repo_root / "Dockerfile"
    if dockerfile.is_file():
        results.append(CheckResult("contract", "PASS", "Dockerfile present"))
    else:
        results.append(CheckResult("contract", "FAIL", "Dockerfile missing"))

    app_py = repo_root / "src" / "nexus_ai_agent" / "api" / "app.py"
    if not app_py.is_file():
        results.append(CheckResult("contract", "FAIL", "web app module (api/app.py) missing"))
    else:
        source = app_py.read_text(encoding="utf-8")
        for route in (EXPECTED_HEALTH_PATH, EXPECTED_WEBHOOK_PATH):
            if route in source:
                results.append(CheckResult("contract", "PASS", f"route {route} present"))
            else:
                results.append(CheckResult("contract", "FAIL", f"route {route} missing"))

    locales_dir = repo_root / "src" / "nexus_ai_agent" / "i18n" / "locales"
    locale_files = sorted(locales_dir.glob("*.json")) if locales_dir.is_dir() else []
    if len(locale_files) != EXPECTED_LOCALE_COUNT:
        results.append(
            CheckResult(
                "contract",
                "FAIL",
                f"expected {EXPECTED_LOCALE_COUNT} locale files, found {len(locale_files)}",
            )
        )
    else:
        broken = []
        for path in locale_files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or not payload:
                    broken.append(f"{path.stem} (empty)")
            except Exception:  # noqa: BLE001 - any corruption fails the check
                broken.append(f"{path.stem} (unparseable)")
        if broken:
            results.append(
                CheckResult("contract", "FAIL", f"broken locale files: {', '.join(broken)}")
            )
        else:
            results.append(
                CheckResult(
                    "contract",
                    "PASS",
                    f"{EXPECTED_LOCALE_COUNT} locale files parse",
                )
            )
    return results


def _read_json_response(url: str, timeout: float) -> tuple[int, Any]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, None


def check_healthz(base_url: str, timeout: float) -> CheckResult:
    """Probe the liveness endpoint of a deployed instance."""
    url = base_url.rstrip("/") + EXPECTED_HEALTH_PATH
    try:
        status, payload = _read_json_response(url, timeout)
    except Exception as exc:  # noqa: BLE001 - network failures fail the check
        return CheckResult("healthz", "FAIL", f"{url} unreachable: {exc}")
    if status != 200 or payload != {"status": "ok"}:
        return CheckResult("healthz", "FAIL", f"{url} answered {status} {payload!r}")
    return CheckResult("healthz", "PASS", f"{url} -> 200 {payload!r}")


def check_webhook_gate(base_url: str, timeout: float) -> CheckResult:
    """Prove the webhook secret gate answers 403 before any processing."""
    url = base_url.rstrip("/") + EXPECTED_WEBHOOK_PATH
    body = json.dumps({"update_id": 0}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": "smoke-probe-bogus-secret",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception as exc:  # noqa: BLE001 - network failures fail the check
        return CheckResult("webhook-gate", "FAIL", f"{url} unreachable: {exc}")
    if status != 403:
        return CheckResult(
            "webhook-gate",
            "FAIL",
            f"bogus secret answered {status}, expected 403",
        )
    return CheckResult("webhook-gate", "PASS", "bogus secret rejected with 403")


def run_smoke(
    *,
    repo_root: Path = REPO_ROOT,
    base_url: str | None = None,
    timeout: float = 15.0,
) -> list[CheckResult]:
    """Run the offline checks plus the live probes when ``base_url`` is set."""
    results = [
        *check_manifest(repo_root / "koyeb.yaml"),
        *check_contract(repo_root),
    ]
    if base_url:
        results.append(check_healthz(base_url, timeout))
        results.append(check_webhook_gate(base_url, timeout))
    return results


def summarize(results: list[CheckResult], *, strict: bool) -> int:
    """Print results; return the process exit status (0/1)."""
    for result in results:
        print(f"[{result.status}] {result.name}: {result.detail}")
    failed = [r for r in results if r.status == "FAIL"]
    blocking_skips = [r for r in results if strict and r.status == "SKIP"]
    if failed or blocking_skips:
        print(f"smoke: {len(failed)} failed, {len(blocking_skips)} strict-skips")
        return 1
    print(f"smoke: {len(results)} checks green")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NEXUS AI Koyeb deploy smoke.")
    parser.add_argument(
        "--url",
        default=None,
        help="Deployed base URL for live probes "
        "(e.g. https://myapp-org.koyeb.app). Without it, offline only.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat SKIP results as failures.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Seconds per live probe (default: 15).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint (returns exit status; never raises SystemExit itself)."""
    args = build_parser().parse_args(argv)
    if args.timeout <= 0:
        print("smoke: --timeout must be positive", file=sys.stderr)
        return 2
    results = run_smoke(base_url=args.url, timeout=args.timeout)
    return summarize(results, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
