#!/usr/bin/env python3
"""Emit a JSON runtime receipt bound to the current repository state."""

from __future__ import annotations

import importlib.metadata
import json
import platform
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str) -> str:
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def main() -> int:
    try:
        package = importlib.metadata.version("nexus-ai-agent")
    except importlib.metadata.PackageNotFoundError:
        match = re.search(
            r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.MULTILINE
        )
        package = match.group(1) if match else "unknown"
    receipt = {
        "application": package,
        "commit_sha": _run("git", "rev-parse", "HEAD"),
        "branch": _run("git", "branch", "--show-current"),
        "os": platform.freedesktop_os_release(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "package_manager": _run("python", "-m", "pip", "--version"),
        "working_tree": _run("git", "status", "--porcelain") or "clean",
    }
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
