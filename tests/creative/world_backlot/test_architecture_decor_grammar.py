from __future__ import annotations

import math
from dataclasses import FrozenInstanceError, replace

import numpy as np
import pytest

from nexus_ai_agent.creative.world_backlot import (
    ArchitectureSpec,
    ArchitectureStyle,
    CameraCurve,
    CameraKeyframe,
    DepthLayer,
    SceneDNASeal,
    SceneObject,
    ShapeBounds,
    depth_layer,
    generate_architecture,
    generate_camera_curve,
    parallax_project,
    split_shape,
    symmetric_girih_pattern,
)


@pytest.mark.parametrize("axis", [0, 1, 2])
def test_split_grammar_tiles_parent_without_gaps_overlap_or_volume_loss(axis: int) -> None:
    parent = ShapeBounds((-12, 0, 30), (24, 12, 18))
    children = split_shape(parent, axis, (1, 2, 3, 1.5))
    assert len(children) == 4
    assert sum(math.prod(c.dimensions_m) for c in children) == pytest.approx(
        math.prod(parent.dimensions_m)
    )
    assert children[0].origin_m == parent.origin_m
    for left, right in zip(children, children[1:], strict=False):
        assert left.origin_m[axis] + left.dimensions_m[axis] == pytest.approx(right.origin_m[axis])
    assert children[-1].origin_m[axis] + children[-1].dimensions_m[axis] == pytest.approx(
        parent.origin_m[axis] + parent.dimensions_m[axis]
    )
    for child in children:
        for other in set(range(3)) - {axis}:
            assert child.origin_m[other] == parent.origin_m[other]
            assert child.dimensions_m[other] == parent.dimensions_m[other]
    assert split_shape(parent, axis, (1,)) == (parent,)


@pytest.mark.parametrize("order", [4, 5, 8, 12, 16])
def test_girih_is_a_real_rotationally_symmetric_geometric_graph(order: int) -> None:
    pattern = symmetric_girih_pattern("orsi", order, 2)
    assert pattern == symmetric_girih_pattern("orsi", order, 2)
    assert pattern != symmetric_girih_pattern("different", order, 2)
    points = np.asarray(pattern.vertices)
    angle = 2 * math.pi / order
    rotation = np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
    rotated = points @ rotation.T
    np.testing.assert_allclose(rotated, np.roll(points, -2, axis=0), atol=2e-15)
    assert len(points) == 2 * order
    assert all(0 <= a < len(points) and 0 <= b < len(points) and a != b for a, b in pattern.edges)
    assert np.max(np.linalg.norm(points, axis=1)) == pytest.approx(2)


@pytest.mark.parametrize(
    ("style", "features"),
    [
        (
            ArchitectureStyle.IRANIAN_EASTERN,
            {"iwan", "dome", "badgir_windcatcher", "orsi_window", "girih_screen", "courtyard"},
        ),
        (ArchitectureStyle.MODERN_URBAN, {"glass_tower", "street", "bridge", "apartment"}),
        (
            ArchitectureStyle.CLASSICAL_HISTORIC,
            {"castle", "palace", "colonnaded_hall", "vaulted_gate"},
        ),
        (ArchitectureStyle.INDUSTRIAL_CYBERPUNK, {"factory", "warehouse", "tunnel", "neon_sign"}),
        (
            ArchitectureStyle.SPACE_SCIFI,
            {"orbital_station", "planetary_base", "spaceship_corridor", "airlock"},
        ),
    ],
)
def test_five_architecture_grammars_materialize_features_meshes_decor_and_four_layers(
    style: ArchitectureStyle,
    features: set[str],
) -> None:
    scene = generate_architecture("blueprint", ArchitectureSpec(style=style))
    same = generate_architecture("blueprint", ArchitectureSpec(style=style))
    assert scene == same and scene.verify()
    assert (
        scene.seal.sha256
        != generate_architecture("other", ArchitectureSpec(style=style)).seal.sha256
    )
    names = {o.name for o in scene.objects}
    assert features <= names
    assert {"table", "chair", "lamp", "column", "tree", "rock", "stage_prop"} <= names
    assert len([n for n in names if n.startswith("window_")]) == 15
    assert len({o.object_id for o in scene.objects}) == len(scene.objects)
    assert [layer.name for layer in scene.layers] == list(DepthLayer)
    assert all(layer.objects for layer in scene.layers)
    for obj in scene.objects:
        assert obj.layer is depth_layer(obj.position_m[2])
        mesh = obj.mesh(8)
        assert mesh.vertices and mesh.faces
        vertices = np.asarray(mesh.vertices)
        assert np.isfinite(vertices).all()
        assert all(
            len(face) >= 3 and min(face) >= 0 and max(face) < len(vertices) for face in mesh.faces
        )
        center, half_size = np.asarray(obj.position_m), np.asarray(obj.dimensions_m) / 2
        assert np.all(vertices >= center - half_size - 1e-8)
        assert np.all(vertices <= center + half_size + 1e-8)
        assert mesh == obj.mesh(8)


def test_iranian_pointed_arch_is_symmetric_with_a_visible_opening_and_colonnade() -> None:
    scene = generate_architecture(1)
    arch = next(o for o in scene.objects if o.name == "iwan")
    vertices = np.asarray(arch.mesh(16).vertices)
    # Outer and inner arch curves are separate; the central hole is not capped.
    outer, inner = vertices[0::4], vertices[1::4]
    np.testing.assert_allclose(
        outer[:, 0] - arch.position_m[0], -(outer[::-1, 0] - arch.position_m[0]), atol=1e-12
    )
    np.testing.assert_allclose(outer[:, 1], outer[::-1, 1], atol=1e-12)
    assert np.max(inner[:, 1]) < np.max(outer[:, 1])
    assert (
        len([o for o in scene.objects if o.name.startswith("facade_column_")])
        == scene.spec.bays + 1
    )
    interior = generate_architecture(1, ArchitectureSpec(interior=True))
    assert interior.objects[0].material == "interior_plaster"
    assert interior.seal.sha256 != scene.seal.sha256


def test_scene_dna_rejects_dimensions_geometry_order_seed_and_schema_tampering() -> None:
    scene = generate_architecture("film")
    first = scene.objects[0]
    changed = replace(first, dimensions_m=(first.dimensions_m[0] + 1, *first.dimensions_m[1:]))
    assert not replace(scene, objects=(changed, *scene.objects[1:])).verify()
    assert not replace(scene, objects=scene.objects[::-1]).verify()
    assert not replace(scene, seed_sha256="0" * 64).verify()
    assert not replace(scene, spec=replace(scene.spec, height_m=99)).verify()
    assert not replace(scene, seal=SceneDNASeal(scene.seal.sha256, "other")).verify()
    with pytest.raises(FrozenInstanceError):
        scene.spec.width_m = 99  # type: ignore[misc]


def test_four_layer_parallax_is_inverse_depth_in_pixels_and_does_not_change_blueprint() -> None:
    assert [depth_layer(z) for z in (3, 15, 60, 10000)] == list(DepthLayer)
    displacements = [parallax_project(17, 1000, 1, z) - 17 for z in (3, 15, 60, 10000)]
    np.testing.assert_allclose(displacements, [1000 / 3, 1000 / 15, 1000 / 60, 0.1])
    assert all(a > b for a, b in zip(displacements, displacements[1:], strict=False))
    assert parallax_project(3, 900, -2, 30) == -57
    scene = generate_architecture(1)
    curve = generate_camera_curve(1, "tracking")
    seal = scene.seal.sha256
    delta_x = curve.sample(8).position_m[0] - curve.sample(0).position_m[0]
    for obj in scene.objects:
        start, end = curve.project(obj, 0), curve.project(obj, 8)
        assert end[0] - start[0] == pytest.approx(960 * delta_x / obj.position_m[2])
    assert scene.seal.sha256 == seal and scene.verify()


@pytest.mark.parametrize(
    "template",
    [
        "static",
        "dolly",
        "orbit",
        "crane",
        "handheld",
        "tracking",
        "wide",
        "closeup",
        "establishing",
        "low_angle",
    ],
)
def test_camera_templates_are_deterministic_bounded_smooth_curves(template: str) -> None:
    curve = generate_camera_curve("take", template, 10)
    assert curve.sha256 == generate_camera_curve("take", template, 10).sha256
    assert curve.sample(0) == curve.keyframes[0]
    assert curve.sample(5) == curve.keyframes[1]
    assert curve.sample(10) == curve.keyframes[-1]
    before, after = curve.sample(5 - 1e-4), curve.sample(5 + 1e-4)
    assert np.linalg.norm(np.subtract(before.position_m, after.position_m)) < 1e-7
    assert np.isfinite(curve.sample(2.5).position_m).all()
    if template == "static":
        assert curve.sample(0).position_m == curve.sample(10).position_m
    if template in ("wide", "closeup"):
        assert curve.sample(2).focal_length_px == (600 if template == "wide" else 1500)


def test_invalid_geometry_and_camera_inputs_fail_before_materialization() -> None:
    with pytest.raises(ValueError):
        ShapeBounds((0, 0, 0), (1, 0, 1))
    shape = ShapeBounds((0, 0, 0), (1, 1, 1))
    for axis, weights in ((3, (1.0,)), (0, ()), (0, (0.0, 1.0)), (0, (math.nan,))):
        with pytest.raises(ValueError):
            split_shape(shape, axis, weights)
    with pytest.raises(ValueError):
        symmetric_girih_pattern(0, 3)
    with pytest.raises(ValueError):
        ArchitectureSpec(width_m=0)
    with pytest.raises(ValueError):
        ArchitectureSpec(floors=31)
    with pytest.raises(ValueError):
        ArchitectureSpec(interior=1)  # type: ignore[arg-type]
    scene = generate_architecture(0)
    obj = scene.objects[0]
    with pytest.raises(ValueError):
        replace(obj, primitive="unknown")
    with pytest.raises(ValueError):
        replace(obj, dimensions_m=(-1, 1, 1))
    with pytest.raises(ValueError):
        replace(obj, layer=DepthLayer.FOREGROUND)
    assert replace(obj, parameters=(("curve", 0.5),)).parameters == (("curve", 0.5),)
    with pytest.raises(ValueError):
        replace(obj, parameters=(("curve", math.nan),))
    with pytest.raises(ValueError):
        obj.mesh(65)
    with pytest.raises(ValueError):
        parallax_project(0, 900, 1, 0)
    with pytest.raises(ValueError):
        CameraKeyframe(0, (0, 0, 0), 0)
    with pytest.raises(ValueError):
        CameraCurve((CameraKeyframe(1, (0, 0, 0)), CameraKeyframe(2, (1, 0, 0))))
    with pytest.raises(ValueError):
        CameraCurve((CameraKeyframe(0, (0, 0, 0)), CameraKeyframe(0, (1, 0, 0))))
    with pytest.raises(ValueError):
        generate_camera_curve(0, "unknown")
    curve = generate_camera_curve(0)
    with pytest.raises(ValueError):
        curve.sample(8.1)
    with pytest.raises(ValueError):
        curve.project(
            SceneObject("id", "box", "box", (0, 0, 0.1), (1, 1, 1), "m", DepthLayer.FOREGROUND), 8
        )
