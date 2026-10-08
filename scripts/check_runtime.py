#!/usr/bin/env python3
"""Fail-closed verification of the canonical Nexus runtime contract."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "pyproject.toml"


def _read_contract() -> dict[str, object]:
    text = CONTRACT.read_text(encoding="utf-8")
    marker = "[tool.nexus.runtime]\n"
    if marker not in text:
        raise SystemExit("runtime contract missing: [tool.nexus.runtime]")
    values: dict[str, object] = {}
    for line in text.split(marker, 1)[1].split("\n\n", 1)[0].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip().strip('"')
        values[key.strip()] = value
    return values


def main() -> int:
    contract = _read_contract()
    expected_py = str(contract["python"])
    expected_os = str(contract["os"])
    actual_py = platform.python_version()
    actual_os = platform.freedesktop_os_release().get("VERSION_ID", "unknown")
    failures: list[str] = []
    if not actual_py.startswith(expected_py + ".") and actual_py != expected_py:
        failures.append(f"Python {actual_py} does not satisfy {expected_py}.x")
    if expected_os not in actual_os and not (expected_os == "26.04" and actual_os == "26.04.1"):
        failures.append(f"OS {actual_os} does not satisfy Ubuntu {expected_os}")
    if shutil.which("ffmpeg") is None:
        failures.append("ffmpeg is not available on PATH")
    report = {
        "contract": contract,
        "actual": {
            "python": actual_py,
            "os_version_id": actual_os,
            "platform": platform.platform(),
            "ffmpeg": shutil.which("ffmpeg"),
            "git_sha": _git_sha(),
        },
        "passed": not failures,
        "failures": failures,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not failures else 1


def _git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
