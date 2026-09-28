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
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def clean_full_runs(args: argparse.Namespace) -> int:
    """Independent fresh clones; share only immutable objects and installed deps.

    Keep every clone on the session's existing branch name. No reset, checkout,
    remote write, or source edit is performed. Clones live in disposable .cache.
    """
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    branch = subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=ROOT, text=True
    ).strip()
    if not branch or subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT):
        raise RuntimeError("clean-clone proof requires a named branch and clean working tree")
    output = ROOT / "ci-artifacts" / "w2" / "clean-full"
    output.mkdir(parents=True, exist_ok=True)
    cache = Path.home() / ".cache"
    cache.mkdir(exist_ok=True)
    summary = {
        "head": head,
        "branch": branch,
        "python": sys.version,
        "requested": args.runs,
        "workers": args.workers,
        "isolation": "fresh shared-object clone per run; shared installed dependencies",
        "runs": [],
    }
    with tempfile.TemporaryDirectory(prefix="w2-clean-", dir=cache) as temporary:
        seed = Path(temporary) / "seed"
        clone = ["git", "clone", "--shared", "--quiet", "--single-branch", "--branch", branch]
        subprocess.run([*clone, str(ROOT), str(seed)], check=True)
        seed_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=seed, text=True
        ).strip()
        if seed_head != head:
            raise RuntimeError("HEAD changed while the proof snapshot was captured")

        def run(index: int) -> dict:
            work = Path(temporary) / f"run-{index:02d}"
            subprocess.run([*clone, str(seed), str(work)], check=True)
            env = dict(os.environ, PYTHONHASHSEED=str(index), PYTHONPATH=str(work / "src"))
            module = subprocess.check_output(
                [sys.executable, "-c", "import nexus_ai_agent; print(nexus_ai_agent.__file__)"],
                cwd=work,
                env=env,
                text=True,
            ).strip()
            if not module.startswith(str(work / "src")):
                raise RuntimeError("pytest would import a different source tree")
            log = output / f"{index:02d}.log"
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-rs",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(work / ".pytest-tmp"),
                "-m",
                "not slow",
            ]
            started = time.monotonic()
            try:
                with log.open("w") as stream:
                    result = subprocess.run(
                        command,
                        cwd=work,
                        env=env,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        timeout=args.timeout,
                        check=False,
                    )
                code = result.returncode
            except subprocess.TimeoutExpired:
                code = 124
            result = {
                "index": index,
                "exit_code": code,
                "hash_seed": index,
                "seconds": round(time.monotonic() - started, 3),
                "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
            }
            print(f"clean full {index}/{args.runs}: exit={code}, {result['seconds']}s", flush=True)
            if code:
                print(log.read_text()[-10000:], flush=True)
            return result

        # At most one finite batch is outstanding; after a failure no new batch
        # starts. Other workers in that batch finish and their evidence is kept.
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for first in range(1, args.runs + 1, args.workers):
                jobs = [
                    pool.submit(run, i)
                    for i in range(first, min(first + args.workers, args.runs + 1))
                ]
                failed = False
                for job in as_completed(jobs):
                    result = job.result()
                    summary["runs"].append(result)
                    failed |= bool(result["exit_code"])
                    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
                if failed:
                    return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=SELECTIONS)
    parser.add_argument("--runs", type=int, default=40)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--clean-full", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("runs must be positive")
    if args.workers < 1 or args.workers > 4:
        parser.error("workers must be between 1 and 4")
    if args.clean_full:
        if args.kind != "full":
            parser.error("--clean-full applies only to full")
        return clean_full_runs(args)
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
