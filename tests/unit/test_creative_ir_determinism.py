"""Determinism, Merkle locality and the revision property of the Creative IR.

This is the suite that proves the claims the IR exists to make. Three of them
matter enough that a later slice of the plane is impossible without them:

**1. Determinism.** The same creative document built twice -- in this process,
in a fresh process, under a different ``PYTHONHASHSEED`` -- yields byte-identical
identities and byte-identical canonical JSON. Without this, "deterministic
compilation" is a slogan, because the plan would carry ids that churn on every
run and every cache and every diff downstream would be noise.

**2. Merkle locality.** Because a parent's identity commits its children's, a
change confined to one scene changes exactly that scene's subtree and the root.
Every other identity in the document is bit-identical. This is what makes a
semantic diff a tree walk instead of a text diff.

**3. The revision property.** The direct consequence of (2), and the reason the
whole plane is worth building: "make it more emotional but keep the rhythm"
rewrites one scene, and every untouched element *keeps its identity*. A revision
is therefore an edit, not a blind rebuild -- which is what lets a compiler
reuse everything it already decided and lets a user trust that the parts they
did not ask to change did not change.

Also proven here: ``seal`` is idempotent, there is no nondeterministic source in
the identity path (no ``uuid4``, no ``time``, no ``random``), and identity is
sensitive to *meaning* rather than to construction order.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from creative_ir_testkit import ONE_SECOND_US, product_teaser

from nexus_ai_agent.creative.intelligence import (
    Asset,
    AssetKind,
    CreativeBrief,
    CreativeWork,
    Effect,
    EffectFamily,
    EffectParam,
    Layer,
    MediaContent,
    NarrativeRole,
    Scene,
    Segment,
    SemanticRole,
    StyleIntent,
    Timing,
    seal_work,
)

PACKAGE = Path("src/nexus_ai_agent/creative/intelligence")


# ── 1. Determinism ───────────────────────────────────────────────────────────


def test_building_the_same_document_twice_yields_identical_bytes() -> None:
    assert product_teaser().to_canonical_json() == product_teaser().to_canonical_json()


def test_every_identity_is_reproducible_across_independent_builds() -> None:
    a, b = product_teaser(), product_teaser()
    assert a.work_id == b.work_id
    assert [s.scene_id for s in a.scenes] == [s.scene_id for s in b.scenes]
    assert [x.layer_id for x in a.all_layers()] == [x.layer_id for x in b.all_layers()]
    assert [x.asset_id for x in a.assets] == [x.asset_id for x in b.assets]
    assert [c.constraint_id for c in a.constraints] == [c.constraint_id for c in b.constraints]


def test_identity_survives_a_full_serialization_round_trip() -> None:
    work = product_teaser()
    restored = CreativeWork.from_canonical_json(work.to_canonical_json())
    assert restored.work_id == work.work_id
    restored.verify_identity()  # and it is still self-consistent, not just equal


def test_identity_does_not_depend_on_python_hash_randomization() -> None:
    """The strongest form of the claim: a fresh interpreter, a different seed.

    Run as a subprocess because ``PYTHONHASHSEED`` is read once at startup, and
    because a test that only checks the current process cannot detect identity
    leaking from set/dict iteration order in the hashing path.
    """
    program = (
        "import sys; sys.path.insert(0, 'tests/unit');"
        "from creative_ir_testkit import product_teaser;"
        "w = product_teaser();"
        "print(w.work_id);"
        "print(w.to_canonical_json())"
    )
    results: set[str] = set()
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        proc = subprocess.run(
            [sys.executable, "-c", program],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        results.add(proc.stdout)
    assert len(results) == 1, f"identity varied with PYTHONHASHSEED: {results}"


def test_sealing_is_idempotent() -> None:
    work = product_teaser()
    once = seal_work(work)
    twice = seal_work(once)
    assert once.work_id == twice.work_id
    assert once.to_canonical_json() == twice.to_canonical_json()


def test_sealing_an_unsealed_document_reaches_the_same_identity() -> None:
    """Seal is a function of content, not of what the ids happened to be."""
    unsealed = product_teaser(sealed=False)
    assert unsealed.work_id == ""
    assert unsealed.seal().work_id == product_teaser().work_id


# ── No nondeterminism in the identity path ───────────────────────────────────


def test_the_identity_path_has_no_nondeterministic_source() -> None:
    """AST-level proof, not a comment.

    ``uuid4``, ``time``, ``random`` and ``id()`` in the package would each make
    identity unreproducible, and none of them is visible in a test run that
    happens to build the same document twice in one process.
    """
    forbidden = {"uuid4", "uuid1", "time", "monotonic", "perf_counter", "random", "urandom"}
    offenders: list[str] = []
    for path in sorted((Path(__file__).resolve().parents[2] / PACKAGE).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in {"uuid", "random", "time"}:
                        offenders.append(f"{path.name}: imports {alias.name}")
            elif isinstance(node, ast.Attribute) and node.attr in forbidden:
                offenders.append(f"{path.name}:{node.lineno}: uses {node.attr}")
    assert offenders == [], offenders


def test_no_float_appears_in_any_hashed_payload() -> None:
    """Floats in a hash are how 'deterministic' quietly becomes platform-specific."""
    work = product_teaser()
    offenders: list[str] = []

    def walk(node: object, where: str) -> None:
        if isinstance(node, float):
            offenders.append(f"{where} = {node!r}")
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{where}.{key}")
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, f"{where}[{index}]")

    walk(work.semantic_payload(), "work")
    for asset in work.assets:
        walk(asset.semantic_payload(), f"asset:{asset.asset_id}")
    for effect in work.effects:
        walk(effect.semantic_payload(), f"effect:{effect.effect_id}")
    for scene in work.scenes:
        walk(scene.semantic_payload(), f"scene:{scene.scene_id}")
        for layer in scene.layers:
            walk(layer.semantic_payload(), f"layer:{layer.layer_id}")
    assert offenders == [], offenders


# ── 2. Merkle locality ───────────────────────────────────────────────────────


def _rewrite_scene(work: CreativeWork, index: int, **timing: int) -> CreativeWork:
    scene = work.scenes[index]
    return work.model_copy(
        update={
            "scenes": (
                *work.scenes[:index],
                scene.model_copy(update={"timing": Timing(**timing)}),
                *work.scenes[index + 1 :],
            )
        }
    ).seal()


def test_a_change_in_one_scene_changes_only_that_subtree_and_the_root() -> None:
    """The Merkle property, stated as a test rather than as a promise."""
    before = product_teaser()
    after = _rewrite_scene(before, 2, start_us=8 * ONE_SECOND_US, duration_us=3 * ONE_SECOND_US)

    assert after.work_id != before.work_id, "the root must commit to its children"
    assert after.scenes[2].scene_id != before.scenes[2].scene_id, "the edited scene changed"

    untouched = [0, 1, 3]
    for index in untouched:
        assert after.scenes[index].scene_id == before.scenes[index].scene_id, (
            f"scene {index} changed although nothing below it did"
        )
        assert [layer.layer_id for layer in after.scenes[index].layers] == [
            layer.layer_id for layer in before.scenes[index].layers
        ]

    # assets and effects are below nothing that changed, so they are untouched
    assert [a.asset_id for a in after.assets] == [a.asset_id for a in before.assets]
    assert [e.effect_id for e in after.effects] == [e.effect_id for e in before.effects]


def test_an_untouched_scene_keeps_its_identity_even_when_a_sibling_moves() -> None:
    """Locality is about *content*, not about position in the tuple."""
    before = product_teaser()
    after = _rewrite_scene(before, 0, start_us=0, duration_us=2_500_000)
    assert after.scenes[0].scene_id != before.scenes[0].scene_id
    assert after.scenes[3].scene_id == before.scenes[3].scene_id


def test_a_pure_relabel_still_changes_the_scene_identity() -> None:
    """A label is meaning: renaming a beat is a change to that beat."""
    before = product_teaser()
    scene = before.scenes[1]
    after = before.model_copy(
        update={
            "scenes": (
                before.scenes[0],
                scene.model_copy(update={"label": "relabeled"}),
                *before.scenes[2:],
            )
        }
    ).seal()
    assert after.scenes[1].scene_id != scene.scene_id
    assert after.scenes[0].scene_id == before.scenes[0].scene_id


def test_the_root_identity_is_sensitive_to_every_semantic_part() -> None:
    """Each independent part of the document, changed alone, moves the root."""
    base = product_teaser()
    variants = {
        "brief": base.model_copy(
            update={"brief": base.brief.model_copy(update={"goal": "a different goal"})}
        ),
        "output": base.model_copy(
            update={"output": base.output.model_copy(update={"width_px": 1280})}
        ),
        "asset": base.model_copy(
            update={
                "assets": (
                    base.assets[0].model_copy(update={"uri": "asset:other"}),
                    *base.assets[1:],
                )
            }
        ),
        "effect": base.model_copy(
            update={
                "effects": (
                    base.effects[0].model_copy(update={"intensity": 900}),
                    *base.effects[1:],
                )
            }
        ),
        "scene": _rewrite_scene(base, 0, start_us=0, duration_us=2_900_000),
        "transition": base.model_copy(
            update={
                "transitions": (
                    base.transitions[0].model_copy(
                        update={"kind": base.transitions[1].kind, "duration_us": 250_000}
                    ),
                    *base.transitions[1:],
                )
            }
        ),
        "constraint": base.model_copy(update={"constraints": base.constraints[:1]}),
    }
    for name, variant in variants.items():
        sealed = variant.seal()
        assert sealed.work_id != base.work_id, f"{name} did not move the root identity"


def test_two_documents_that_differ_only_in_a_layer_emphasis_differ() -> None:
    """Emphasis is addressed by constraints, so it must be part of identity."""
    base = product_teaser()
    scene = base.scenes[0]
    layer = scene.layers[0]
    changed = base.model_copy(
        update={
            "scenes": (
                scene.model_copy(
                    update={
                        "layers": (layer.model_copy(update={"emphasis": 400}), *scene.layers[1:])
                    }
                ),
                *base.scenes[1:],
            )
        }
    ).seal()
    assert changed.scenes[0].layers[0].layer_id != layer.layer_id
    assert changed.scenes[0].scene_id != scene.scene_id
    assert changed.work_id != base.work_id


# ── 3. The revision property ─────────────────────────────────────────────────


def test_a_semantic_revision_is_an_edit_not_a_rebuild() -> None:
    """The capability the plane exists for, proven at the IR level.

    "Make the reveal more dramatic, keep everything else" raises the climax's
    energy and emphasis. The test asserts what a user is entitled to expect: the
    climax changed, and *nothing else did* -- same identities, therefore the same
    decisions a compiler already made remain valid for it.
    """
    before = product_teaser()
    climax = before.scenes[2]
    revised_layers = tuple(
        layer.model_copy(
            update={
                "emphasis": min(1000, layer.emphasis + 100),
                "style": layer.style.model_copy(update={"energy": 1000, "motion": 1000}),
            }
        )
        for layer in climax.layers
    )
    after = before.model_copy(
        update={
            "scenes": (
                *before.scenes[:2],
                climax.model_copy(update={"layers": revised_layers}),
                before.scenes[3],
            )
        }
    ).seal()

    assert after.work_id != before.work_id
    assert after.scenes[2].scene_id != climax.scene_id
    assert all(
        new.layer_id != old.layer_id
        for new, old in zip(after.scenes[2].layers, climax.layers, strict=True)
    )

    # everything the user did not ask to change kept its identity
    for index in (0, 1, 3):
        assert after.scenes[index].scene_id == before.scenes[index].scene_id
        assert [layer.layer_id for layer in after.scenes[index].layers] == [
            layer.layer_id for layer in before.scenes[index].layers
        ]
    assert [a.asset_id for a in after.assets] == [a.asset_id for a in before.assets]
    assert [e.effect_id for e in after.effects] == [e.effect_id for e in before.effects]


def test_a_revision_preserves_the_constraints_it_was_told_to_preserve() -> None:
    """ "Keep the rhythm" is a constraint, and a revision must be checked against it."""
    before = product_teaser()
    climax = before.scenes[2]
    # a careless revision that also shortens the piece
    too_short = before.model_copy(
        update={
            "scenes": (
                before.scenes[0],
                before.scenes[1].model_copy(
                    update={"timing": Timing(start_us=3 * ONE_SECOND_US, duration_us=2_000_000)}
                ),
                climax.model_copy(
                    update={"timing": Timing(start_us=5 * ONE_SECOND_US, duration_us=2_000_000)}
                ),
                before.scenes[3].model_copy(
                    update={
                        "timing": Timing(start_us=7 * ONE_SECOND_US, duration_us=2_000_000),
                        "layers": tuple(
                            layer.model_copy(
                                update={
                                    "content": MediaContent(
                                        asset_ref=layer.content.asset_ref,  # type: ignore[attr-defined]
                                        segment=Segment(
                                            source_in_us=0,
                                            source_duration_us=2_000_000,
                                            timeline=Timing(
                                                start_us=layer.timeline().start_us,
                                                duration_us=2_000_000,
                                            ),
                                        ),
                                    )
                                    if isinstance(layer.content, MediaContent)
                                    else layer.content.model_copy(
                                        update={
                                            "timeline": Timing(
                                                start_us=layer.timeline().start_us,
                                                duration_us=2_000_000,
                                            )
                                        }
                                    ),
                                }
                            )
                            for layer in before.scenes[3].layers
                        ),
                    }
                ),
            )
        }
    ).seal()
    assert too_short.duration_us == 9 * ONE_SECOND_US
    violations = too_short.check_constraints()
    assert any("below the 10000000us minimum" in v.detail for v in violations), violations
    # ...and the identity of the untouched hook is still stable across the revision
    assert too_short.scenes[0].scene_id == before.scenes[0].scene_id


# ── Order independence ───────────────────────────────────────────────────────


def test_child_order_does_not_change_a_leaf_identity() -> None:
    """``sealed_id`` sorts children: identity depends on *what*, not on order."""
    asset = Asset(asset_id="a", kind=AssetKind.IMAGE, uri="asset:still", role=SemanticRole.SUBJECT)
    layer_a = Layer(
        layer_id="l",
        role=SemanticRole.SUBJECT,
        content=MediaContent(
            asset_ref="a",
            segment=Segment(
                source_in_us=0,
                source_duration_us=ONE_SECOND_US,
                timeline=Timing(start_us=0, duration_us=ONE_SECOND_US),
            ),
        ),
        effects=("e1", "e2"),
    )
    layer_b = layer_a.model_copy(update={"effects": ("e2", "e1")})
    assert (
        seal_work(_minimal(asset, layer_a)).scenes[0].layers[0].layer_id
        == seal_work(_minimal(asset, layer_b)).scenes[0].layers[0].layer_id
    )


def test_effect_parameter_order_is_normalised_at_construction() -> None:
    """Params must be declared sorted, so the rule is enforced, not assumed."""
    with pytest.raises(ValueError, match="name order"):
        Effect(
            family=EffectFamily.COLOR,
            operation="color.adjust_exposure",
            params=(
                EffectParam(name="zulu", value_milli=1),
                EffectParam(name="alpha", value_milli=2),
            ),
        )


def _minimal(asset: Asset, layer: Layer) -> CreativeWork:
    """A one-scene document, for tests that only care about one layer's identity."""
    return CreativeWork(
        brief=CreativeBrief(goal="minimal"),
        assets=(asset,),
        # declared so the layer's effect references resolve: sealing resolves
        # references while assigning identity, and refuses an unresolved one
        effects=(
            Effect(effect_id="e1", family=EffectFamily.COLOR, operation="color.apply_lut"),
            Effect(effect_id="e2", family=EffectFamily.MOTION, operation="motion.add_glow"),
        ),
        scenes=(
            Scene(
                scene_id="s",
                label="s",
                narrative_role=NarrativeRole.HOOK,
                timing=Timing(start_us=0, duration_us=ONE_SECOND_US),
                layers=(layer,),
            ),
        ),
    )


# ── Canonical form ───────────────────────────────────────────────────────────


def test_canonical_json_is_key_order_independent() -> None:
    """Sorted keys at every depth: the same document has one byte string."""
    work = product_teaser()
    payload = json.loads(work.to_canonical_json())
    reshuffled = json.loads(json.dumps(payload))  # json round-trip keeps insertion order
    assert json.dumps(reshuffled, sort_keys=True, separators=(",", ":")) == json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )
    assert work.to_canonical_json() == json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def test_identity_ignores_the_provisional_ids_a_builder_chose() -> None:
    """Two builders that disagree on scratch names still agree on identity."""
    a = product_teaser(sealed=False)
    renamed_layers = tuple(
        scene.model_copy(
            update={
                "layers": tuple(
                    layer.model_copy(update={"layer_id": f"scratch-{i}-{layer.layer_id}"})
                    for i, layer in enumerate(scene.layers)
                )
            }
        )
        for scene in a.scenes
    )
    b = a.model_copy(update={"scenes": renamed_layers})
    assert a.seal().work_id == b.seal().work_id


def test_style_axes_participate_in_identity() -> None:
    """Style is meaning here, so it cannot be identity-invisible."""
    base = product_teaser()
    scene = base.scenes[0]
    layer = scene.layers[0]
    restyled = base.model_copy(
        update={
            "scenes": (
                scene.model_copy(
                    update={
                        "layers": (
                            layer.model_copy(update={"style": StyleIntent(energy=120)}),
                            *scene.layers[1:],
                        )
                    }
                ),
                *base.scenes[1:],
            )
        }
    ).seal()
    assert restyled.scenes[0].layers[0].layer_id != layer.layer_id
