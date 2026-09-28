#!/usr/bin/env python3
"""Fresh-process W2 repetitions with explicit exit status and content provenance.

No daemon, model call, dependency, source mutation, cache deletion or CI claim.
Full means the repository's CI selection: pytest -m 'not slow'. Service-dependent
skips remain skips. Stop on the first failure; preserve its complete log.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELECTIONS = {
    "race": [
        "tests/unit/test_llm_gateway_registry_race.py",
        "tests/unit/test_llm_gateway_golden.py",
    ],
    "engine": ["tests/unit/test_llm_gateway_engine.py"],
    "load": ["tests/unit/test_llm_gateway_load.py"],
    "transport": ["tests/unit/test_gemini_key_transport.py"],
    "full": ["-m", "not slow"],
}


def fingerprint() -> str:
    digest = hashlib.sha256()
    # Include untracked proof tests as well as tracked files; don't fingerprint
    # generated reports, caches, or the diagnostic logs themselves.
    paths = [ROOT / "pyproject.toml", ROOT / ".agents/board.json"]
    for directory in ("src", "tests", "scripts", "docs"):
        paths.extend(
            p for p in (ROOT / directory).rglob("*") if p.suffix in {".py", ".md", ".json", ".toml"}
        )
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=SELECTIONS)
    parser.add_argument("--runs", type=int, default=40)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("runs must be positive")
    output = ROOT / "ci-artifacts" / "w2" / f"repeat-{args.kind}"
    output.mkdir(parents=True, exist_ok=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    content = fingerprint()
    summary: dict = {
        "head": head,
        "fingerprint": content,
        "python": sys.version,
        "kind": args.kind,
        "requested": args.runs,
        "runs": [],
    }
    for index in range(1, args.runs + 1):
        if fingerprint() != content:
            raise RuntimeError(
                "source/test/docs changed during repetition; refusing mixed-tree proof"
            )
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-rs",
            "-p",
            "no:cacheprovider",
            *SELECTIONS[args.kind],
        ]
        env = dict(os.environ, PYTHONHASHSEED=str(index))
        started = time.monotonic()
        log = output / f"{index:02d}.log"
        try:
            with log.open("w") as stream:
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=env,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    timeout=args.timeout,
                    check=False,
                )
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = 124
        elapsed = round(time.monotonic() - started, 3)
        summary["runs"].append(
            {
                "index": index,
                "exit_code": code,
                "seconds": elapsed,
                "hash_seed": index,
                "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
            }
        )
        summary["unchanged"] = fingerprint() == content
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(f"{args.kind} {index}/{args.runs}: exit={code}, {elapsed}s, {log}", flush=True)
        if code or not summary["unchanged"]:
            print(log.read_text()[-12000:], flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
