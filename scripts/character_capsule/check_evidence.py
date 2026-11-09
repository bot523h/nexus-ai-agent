#!/usr/bin/env python3
"""Verify an evidence bundle against the artifacts it describes.

A bundle is only worth something if a second party can check it without trusting
the producer. This CLI reads a bundle, re-hashes the files it names, re-checks the
hard binding, and verifies the detached signature when one exists.

Exit codes: ``0`` VALID, ``2`` UNSIGNED (integrity checkable, authenticity not
established), ``1`` INVALID.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.character_capsule.evidence import (  # noqa: E402
    BUNDLE_INVALID,
    BUNDLE_UNSIGNED,
    BUNDLE_VALID,
    EvidenceBundle,
    verify_bundle,
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="verify a character-capsule evidence bundle")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument(
        "--asset-dir",
        type=Path,
        action="append",
        default=[],
        help="directory that may hold the recorded artifacts (repeatable)",
    )
    parser.add_argument("--public-key-hex", default=None)
    args = parser.parse_args(argv)

    bundle = EvidenceBundle.from_dict(json.loads(args.bundle.read_text(encoding="utf-8")))
    files: dict[str, Path] = {}
    for directory in args.asset_dir:
        for record in bundle.records:
            candidate = directory / record.name
            if candidate.is_file() and record.name not in files:
                files[record.name] = candidate
    verification = verify_bundle(bundle, files=files or None, public_key_hex=args.public_key_hex)
    print(
        json.dumps(
            {
                "bundle_id": bundle.bundle_id,
                "bundle_digest": bundle.digest,
                "claim_digest": bundle.claim_digest,
                "records": [record.to_dict() for record in bundle.records],
                "checked_files": sorted(files),
                **verification.to_dict(),
            },
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if verification.status == BUNDLE_VALID:
        return 0
    if verification.status == BUNDLE_UNSIGNED:
        return 2
    if verification.status == BUNDLE_INVALID:
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
