#!/usr/bin/env python3
"""Verify an evidence bundle against the artifacts it describes.

A bundle is only worth something if a second party can check it without trusting
the producer. This CLI reads a bundle, re-hashes every file it names, re-checks
the hard binding, and verifies the detached signature against a key the *caller*
supplies.

The point of ``--trust-anchor`` is that the consumer decides whom to trust. A key
embedded in the bundle only proves that whoever wrote the bundle also signed it,
so a self-signed bundle is reported ``UNTRUSTED_SIGNER`` and never ``VALID``.

Exit codes (each one is a distinct outcome, never a silent success)::

    0  VALID              structure, every artifact byte and the trusted signature all check out
    2  UNSIGNED           integrity is checkable, authenticity was never established
    3  INCOMPLETE         the bundle describes artifacts that were not supplied
    4  UNTRUSTED_SIGNER   a signature exists but not from the trusted key
    5  NOT_VERIFIABLE     the signature cannot be checked (bad shape, unusable key)
    1  INVALID            the recorded bytes changed, or the bundle contradicts itself

Example::

    python scripts/character_capsule/check_evidence.py \
        --bundle .work/results/<run>/evidence_bundle.json \
        --asset-dir .work/results/<run>/assets \
        --trust-anchor .work/trusted-signer.pub
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.character_capsule.errors import ValidationError  # noqa: E402
from experiments.character_capsule.evidence import (  # noqa: E402
    BUNDLE_INCOMPLETE,
    BUNDLE_INVALID,
    BUNDLE_NOT_VERIFIABLE,
    BUNDLE_UNSIGNED,
    BUNDLE_UNTRUSTED_SIGNER,
    BUNDLE_VALID,
    EvidenceBundle,
    TrustAnchor,
    verify_bundle,
)

EXIT_CODES = {
    BUNDLE_VALID: 0,
    BUNDLE_INVALID: 1,
    BUNDLE_UNSIGNED: 2,
    BUNDLE_INCOMPLETE: 3,
    BUNDLE_UNTRUSTED_SIGNER: 4,
    BUNDLE_NOT_VERIFIABLE: 5,
}


def _collect_files(bundle: EvidenceBundle, directories: list[Path]) -> dict[str, Path]:
    """Map record name -> file, taking the first match in argument order.

    Later directories never silently replace an earlier match; if two directories
    hold the same name, the first wins and the duplicate is reported, because a
    verifier that picks a different copy than the producer wrote is verifying a
    different claim.
    """
    files: dict[str, Path] = {}
    duplicates: list[str] = []
    for directory in directories:
        if not directory.is_dir():
            raise ValidationError("asset directory does not exist", path=str(directory))
        for record in bundle.records:
            candidate = directory / record.name
            if not candidate.is_file():
                continue
            if record.name in files:
                duplicates.append(f"{record.name}: {directory}")
                continue
            files[record.name] = candidate
    if duplicates:
        print(
            "warning: the same artifact name was found in more than one directory; "
            "the first match was verified: " + "; ".join(duplicates),
            file=sys.stderr,
        )
    return files


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
    parser.add_argument(
        "--trust-anchor",
        type=Path,
        default=None,
        help="file holding the hex Ed25519 public key the consumer trusts",
    )
    parser.add_argument(
        "--trusted-public-key-hex",
        default=None,
        help="the trusted Ed25519 public key, inline",
    )
    parser.add_argument(
        "--expect-key-id",
        default=None,
        help="fail with UNTRUSTED_SIGNER unless the signing key id matches this value",
    )
    args = parser.parse_args(argv)

    if args.trust_anchor and args.trusted_public_key_hex:
        parser.error("pass either --trust-anchor or --trusted-public-key-hex, not both")
    anchor: TrustAnchor | None = None
    if args.trust_anchor is not None:
        if not args.trust_anchor.is_file():
            parser.error(f"trust anchor file does not exist: {args.trust_anchor}")
        anchor = TrustAnchor.from_file(args.trust_anchor)
    elif args.trusted_public_key_hex:
        anchor = TrustAnchor(public_key_hex=args.trusted_public_key_hex)
    if anchor is not None and args.expect_key_id:
        anchor = TrustAnchor(
            public_key_hex=anchor.public_key_hex, key_id=args.expect_key_id, label=anchor.label
        )

    bundle = EvidenceBundle.from_dict(json.loads(args.bundle.read_text(encoding="utf-8")))
    files = _collect_files(bundle, list(args.asset_dir)) if args.asset_dir else {}
    verification = verify_bundle(bundle, files=files or None, trust_anchor=anchor)
    payload = {
        "bundle_id": bundle.bundle_id,
        "bundle_digest": bundle.digest,
        "claim_digest": bundle.claim_digest,
        "signing_key_id": bundle.signature.key_id if bundle.signature else None,
        "trust_anchor": anchor.to_dict() if anchor else None,
        "records": [record.to_dict() for record in bundle.records],
        "checked_files": sorted(files),
        **verification.to_dict(),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    return EXIT_CODES.get(verification.status, 1)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
