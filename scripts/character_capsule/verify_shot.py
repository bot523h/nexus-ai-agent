#!/usr/bin/env python3
"""Evaluate one frame against a capsule and print the continuity report.

Everything that decides a verdict lives in ``experiments/character_capsule``
(``continuity`` for the engine, ``storage`` for capsule loading, ``media`` for
image I/O). This wrapper only wires arguments to those calls.

Example::

    python scripts/character_capsule/verify_shot.py \
        --calibration experiments/character_capsule/.work/results/calibration.json \
        --image experiments/character_capsule/.work/frames/000001-shot-angle.png \
        --reference experiments/character_capsule/.work/frames/000001-ref-front.png \
        --shot-id SH-102

Exit codes: ``0`` for VERIFIED / VERIFIED_WITH_LIMITATIONS, ``3`` for any state
that needs a human (REVIEW_REQUIRED, INSUFFICIENT_EVIDENCE), ``4`` for a drift or
conflict state. A wrapper that always exited ``0`` would be useless in CI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.character_capsule.continuity import (  # noqa: E402
    ContinuityEvaluator,
    VerificationState,
    load_calibration,
)
from experiments.character_capsule.media import load_rgb  # noqa: E402
from experiments.character_capsule.scene import (  # noqa: E402
    DeclaredShot,
    ShotReference,
    shot_from_dict,
)

NEEDS_HUMAN = {
    VerificationState.REVIEW_REQUIRED,
    VerificationState.INSUFFICIENT_EVIDENCE,
}
DRIFTED = {
    VerificationState.IDENTITY_DRIFT,
    VerificationState.WARDROBE_DRIFT,
    VerificationState.BODY_DRIFT,
    VerificationState.CANON_CONFLICT,
}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="evaluate one frame against a capsule")
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--reference", type=Path, action="append", default=[])
    parser.add_argument("--shot-id", default="SH-UNKNOWN")
    parser.add_argument("--character", default="NAGAR-CHAR-000001")
    parser.add_argument("--outfit", default="OUT-0001")
    parser.add_argument("--expression", default="neutral")
    parser.add_argument("--scene", default="SC-001")
    parser.add_argument("--sequence", default="SEQ-EP1-A")
    parser.add_argument("--shot-index", type=int, default=0)
    parser.add_argument(
        "--shot-json",
        type=Path,
        default=None,
        help=(
            "shot manifest to evaluate against. Pins come from this record, so a shot "
            "authored against an older capsule is refused instead of silently re-pinned "
            "to the capsule under test"
        ),
    )
    args = parser.parse_args(argv)

    references = _expand(args.reference)
    if not references:
        parser.error("at least one --reference frame is required")
    if len(references) < 5:
        print(
            f"warning: {len(references)} reference frame(s); the calibrated band was fitted "
            "on a 7-frame gallery, so a smaller gallery gives a stricter comparison "
            "than the calibration describes",
            file=sys.stderr,
        )

    calibration = load_calibration(json.loads(args.calibration.read_text(encoding="utf-8")))
    capsule = _capsule(args.character)
    pins_source = "derived"
    if args.shot_json is not None:
        shot = shot_from_dict(json.loads(args.shot_json.read_text(encoding="utf-8")))
        pins_source = str(args.shot_json)
        if shot.character_id != capsule.character_id:
            parser.error(
                f"shot pins character {shot.character_id}, capsule is {capsule.character_id}"
            )
    else:
        # Pins are derived from the capsule in hand. That is self-consistent by
        # construction, so the report records ``pins_source`` to make the weaker
        # guarantee visible instead of implying the shot was authored independently.
        shot = ShotReference(
            shot_id=args.shot_id,
            character_id=args.character,
            capsule_revision=capsule.revision_no,
            capsule_digest=capsule.digest,
            module_digests={
                name: module.digest for name, module in sorted(capsule.modules.items())
            },
            scene_id=args.scene,
            sequence_id=args.sequence,
            shot_index=args.shot_index,
            image_asset_id=f"AST-{args.shot_id}",
            declared=DeclaredShot(outfit_id=args.outfit, expression=args.expression),
        )
    evaluator = ContinuityEvaluator(calibration)
    report = evaluator.evaluate(
        shot=shot,
        capsule=capsule,
        image=load_rgb(args.image),
        reference_images=[load_rgb(path) for path in references],
    )
    payload = report.to_dict()
    payload["pins_source"] = pins_source
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    if report.verdict in DRIFTED:
        return 4
    if report.verdict in NEEDS_HUMAN:
        return 3
    return 0


def _expand(patterns: list[str]) -> list[Path]:
    """Expand shell-style globs so a whole gallery can be passed in one argument."""
    paths: list[Path] = []
    for pattern in map(str, patterns):
        candidate = Path(pattern)
        if any(char in pattern for char in "*?["):
            paths.extend(sorted(candidate.parent.glob(candidate.name)))
        else:
            paths.append(candidate)
    return paths


def _capsule(character_id: str):
    """Build the demonstrator capsule for a character id.

    A production CLI would load a *stored* capsule (``storage.CapsuleRepository``);
    this prototype evaluates against the deterministic demonstrator capsule so the
    command works on a clean checkout with no database.
    """
    from experiments.character_capsule.dataset import CHARACTERS, build_capsule

    for spec in CHARACTERS:
        if spec.character_id == character_id:
            return build_capsule(spec)
    raise SystemExit(f"unknown character id: {character_id}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
