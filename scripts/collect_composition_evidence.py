#!/usr/bin/env python3
"""Collect the composition runtime's evidence into one JSON document.

The evidence document is the machine-readable counterpart of the final
engineering report: it states the exact git facts, the environment, the
measured FFmpeg build, the licence ledger, a real plan fingerprint and a real
artifact digest — everything a reviewer needs to re-run the claim.

Usage::

    python scripts/collect_composition_evidence.py --output evidence/composition_evidence.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()
    except OSError:  # pragma: no cover - git is always present in the repo
        return ""


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _environment() -> dict[str, object]:
    from nexus_ai_agent.creative.slideshow.ffmpeg import resolve_ffmpeg_bin

    try:
        binary = resolve_ffmpeg_bin()
    except Exception:  # pragma: no cover - environment dependent
        binary = ""
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "ffmpeg_binary": binary,
        "gpu": "none",
    }


def _ffmpeg_facts() -> dict[str, object]:
    from nexus_ai_agent.creative.composition.license import ffmpeg_build_facts

    return ffmpeg_build_facts()


def _license_ledger() -> dict[str, object]:
    from nexus_ai_agent.creative.composition.license import ffmpeg_ledger, ledger_report

    return ledger_report(ffmpeg_ledger())


def _live_registry() -> dict[str, object]:
    from nexus_ai_agent.creative.composition.compiler import registry_fingerprint
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

    registry = build_runtime_registry()
    operations = registry.list_operations()
    return {
        "operation_count": len(operations),
        "media_operations": [op for op in operations if op.startswith("media.")],
        "registry_fingerprint": registry_fingerprint(registry),
    }


def _a_real_composition(media_out: Path | None = None) -> dict[str, object]:
    """Run one real composition and record what it produced.

    When ``media_out`` is given, the **real** artefact and its source image
    are copied next to the evidence, so the delivery contains an actual media
    file and not only a description of one.
    """

    from nexus_ai_agent.continuum.composition_bench import _plan, _source_image
    from nexus_ai_agent.creative.composition.compiler import CompositionCompiler
    from nexus_ai_agent.creative.composition.executor import CompositionExecutor

    with tempfile.TemporaryDirectory(prefix="nagar-evidence-") as raw:
        root = Path(raw)
        source = _source_image(root / "source.png")
        plan = _plan(width=640, height=480, duration_us=1_000_000, fps=25)
        compiled = CompositionCompiler().compile(plan)
        result = CompositionExecutor().execute(
            compiled, external_inputs={"src": source}, workspace=root / "work"
        )
        artifact = result.artifacts["final"]
        if media_out is not None:
            media_out.mkdir(parents=True, exist_ok=True)
            (media_out / "composition_image_to_video.mp4").write_bytes(
                Path(artifact.output_path).read_bytes()
            )
            (media_out / "composition_source.png").write_bytes(source.read_bytes())
        return {
            "plan_fingerprint": compiled.fingerprint,
            "registry_fingerprint": compiled.registry_fingerprint,
            "artifact_path": artifact.output_path,
            "artifact_sha256": artifact.output_hash,
            "artifact_size_bytes": artifact.output_size_bytes,
            "integrity": artifact.integrity.as_dict(),
            "semantic": artifact.semantic.as_dict(),
            "lineage": list(artifact.lineage),
            "receipts": [receipt.as_dict() for receipt in artifact.receipts],
            "tool_versions": result.tool_versions,
            "state_revision": result.state_revision,
            "state_hash": result.state_hash,
            "duration_seconds": result.duration_seconds,
            "resource_estimate": result.estimate.model_dump(mode="json"),
            "source_sha256": _sha256(source),
            "source_bytes": source.stat().st_size,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "evidence" / "composition_evidence.json",
    )
    parser.add_argument(
        "--media-out",
        type=Path,
        default=None,
        help="also copy the real artefact and its source image into this directory",
    )
    args = parser.parse_args()

    document = {
        "generated_by": "scripts/collect_composition_evidence.py",
        "git": {
            "branch": _git("branch", "--show-current"),
            "head_sha": _git("rev-parse", "HEAD"),
            "base_sha": _git("rev-parse", "HEAD"),
            "origin_main": _git("rev-parse", "origin/main"),
            "status": _git("status", "--porcelain"),
            "commit_count_since_base": _git("rev-list", "--count", "origin/main..HEAD"),
        },
        "environment": _environment(),
        "ffmpeg": _ffmpeg_facts(),
        "license_ledger": _license_ledger(),
        "live_registry": _live_registry(),
        "real_composition": _a_real_composition(args.media_out),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
