"""Bounded split grammar, explicit meshes, four depth planes and scene-DNA locks.

Geometry uses metres in camera-local X (right), Y (up), Z (forward). Geometry
and decor never depend on frame/time/weather. Only a sealed camera curve changes
projection; no frame-by-frame resampling of building dimensions is permitted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from ._determinism import Seed, Vec3, digest, finite, integer, seed_bytes, uniform, vector


class ArchitectureStyle(str, Enum):
    IRANIAN_EASTERN = "iranian_eastern"
    MODERN_URBAN = "modern_urban"
    CLASSICAL_HISTORIC = "classical_historic"
    INDUSTRIAL_CYBERPUNK = "industrial_cyberpunk"
    SPACE_SCIFI = "space_scifi"


class DepthLayer(str, Enum):
    FOREGROUND = "Foreground"
    MIDGROUND = "Midground"
    BACKGROUND = "Background"
    FAR_SKY = "FarSky"


def depth_layer(depth_m: float) -> DepthLayer:
    finite(depth_m, "depth", 1e-6, 1e9)
    if depth_m < 8:
        return DepthLayer.FOREGROUND
    if depth_m < 35:
        return DepthLayer.MIDGROUND
    if depth_m < 500:
        return DepthLayer.BACKGROUND
    return DepthLayer.FAR_SKY


@dataclass(frozen=True, slots=True)
class ShapeBounds:
    origin_m: Vec3
    dimensions_m: Vec3

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin_m", vector(self.origin_m, "shape origin"))
        dimensions = vector(self.dimensions_m, "shape dimensions")
        if any(d <= 0 for d in dimensions):
            raise ValueError("shape dimensions must be positive")
        object.__setattr__(self, "dimensions_m", dimensions)


def split_shape(
    shape: ShapeBounds, axis: int, weights: tuple[float, ...]
) -> tuple[ShapeBounds, ...]:
    """Normalized parametric split with the last boundary pinned to the parent."""
    integer(axis, "split axis", 0, 2)
    integer(len(weights), "split count", 1, 64)
    for weight in weights:
        finite(weight, "split weight", 1e-9, 1e9)
    total = math.fsum(weights)
    result: list[ShapeBounds] = []
    cursor = shape.origin_m[axis]
    end = cursor + shape.dimensions_m[axis]
    for index, weight in enumerate(weights):
        extent = (
            end - cursor if index == len(weights) - 1 else shape.dimensions_m[axis] * weight / total
        )
        origin, dimensions = list(shape.origin_m), list(shape.dimensions_m)
        origin[axis], dimensions[axis] = cursor, extent
        result.append(
            ShapeBounds(
                (origin[0], origin[1], origin[2]), (dimensions[0], dimensions[1], dimensions[2])
            )
        )
        cursor += extent
    return tuple(result)


@dataclass(frozen=True, slots=True)
class GirihPattern:
    rotational_order: int
    vertices: tuple[tuple[float, float], ...]
    edges: tuple[tuple[int, int], ...]


def symmetric_girih_pattern(seed: Seed, order: int = 8, radius_m: float = 1.0) -> GirihPattern:
    integer(order, "girih order", 4, 16)
    finite(radius_m, "girih radius", 0.001, 100)
    inner = 0.38 + 0.14 * float(uniform(seed, "girih/inner-radius", 1)[0])
    vertices = tuple(
        (
            radius_m * (1 if i % 2 == 0 else inner) * math.cos(math.pi * i / order),
            radius_m * (1 if i % 2 == 0 else inner) * math.sin(math.pi * i / order),
        )
        for i in range(2 * order)
    )
    edges = tuple((i, (i + 1) % (2 * order)) for i in range(2 * order))
    edges += tuple((2 * i, (2 * i + 4) % (2 * order)) for i in range(order))
    return GirihPattern(order, vertices, edges)


@dataclass(frozen=True, slots=True)
class Mesh:
    vertices: tuple[Vec3, ...]
    faces: tuple[tuple[int, ...], ...]


_PRIMITIVES = frozenset(
    (
        "box",
        "glass_panel",
        "plane",
        "column",
        "tree",
        "rock",
        "dome",
        "pointed_arch",
        "lattice",
        "neon",
        "tunnel",
        "orbital_ring",
    )
)


@dataclass(frozen=True, slots=True)
class SceneObject:
    object_id: str
    name: str
    primitive: str
    position_m: Vec3
    dimensions_m: Vec3
    material: str
    layer: DepthLayer
    parameters: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        if self.primitive not in _PRIMITIVES:
            raise ValueError("unsupported geometric primitive")
        position, dimensions = (
            vector(self.position_m, "object position"),
            vector(self.dimensions_m, "object dimensions"),
        )
        if any(d <= 0 for d in dimensions) or depth_layer(position[2]) is not self.layer:
            raise ValueError("invalid object dimensions or depth assignment")
        for name, value in self.parameters:
            finite(value, name, -1e9, 1e9)
        object.__setattr__(self, "position_m", position)
        object.__setattr__(self, "dimensions_m", dimensions)

    def payload(self) -> tuple[object, ...]:
        return (
            self.object_id,
            self.name,
            self.primitive,
            self.position_m,
            self.dimensions_m,
            self.material,
            self.layer.value,
            self.parameters,
        )

    def mesh(self, segments: int = 16) -> Mesh:
        """Materialize only this bounded mesh, rather than a whole binary archive."""
        integer(segments, "mesh segments", 8, 64)
        vertices: list[Vec3] = []
        faces: list[tuple[int, ...]] = []
        if self.primitive in ("dome", "rock", "tree", "column", "tunnel", "orbital_ring"):
            rings = 6 if self.primitive in ("dome", "rock", "tree", "orbital_ring") else 1
            for j in range(rings + 1):
                angle = (math.pi / 2) * j / rings
                for i in range(segments):
                    theta = 2 * math.pi * i / segments
                    if self.primitive == "orbital_ring":
                        radial, y = (
                            0.4 + 0.1 * math.cos(2 * math.pi * j / rings),
                            0.1 * math.sin(2 * math.pi * j / rings),
                        )
                    elif self.primitive in ("column", "tunnel"):
                        radial, y = 0.5, j - 0.5
                    elif self.primitive == "tree":
                        radial, y = 0.5 * (1 - j / rings), j / rings - 0.5
                    else:
                        radial, y = 0.5 * math.cos(angle), math.sin(angle) - 0.5
                    vertices.append((radial * math.cos(theta), y, radial * math.sin(theta)))
            for j in range(rings):
                for i in range(segments):
                    a, b = j * segments + i, j * segments + (i + 1) % segments
                    faces.append((a, b, b + segments, a + segments))
            faces.extend(
                (
                    tuple(reversed(range(segments))),
                    tuple(rings * segments + i for i in range(segments)),
                )
            )
        elif self.primitive == "pointed_arch":
            for i in range(segments + 1):
                x = i / segments - 0.5
                y = math.sqrt(max(0, 1 - (abs(x) + 0.5) ** 2)) / math.sqrt(0.75)
                vertices.extend(
                    (
                        (x, y - 0.5, -0.5),
                        (0.78 * x, 0.85 * y - 0.5, -0.5),
                        (x, y - 0.5, 0.5),
                        (0.78 * x, 0.85 * y - 0.5, 0.5),
                    )
                )
            for i in range(segments):
                a = 4 * i
                faces.extend(
                    (
                        (a, a + 4, a + 5, a + 1),
                        (a + 2, a + 3, a + 7, a + 6),
                        (a, a + 2, a + 6, a + 4),
                        (a + 1, a + 5, a + 7, a + 3),
                    )
                )
            faces.extend(((0, 1, 3, 2), tuple(4 * segments + i for i in (0, 2, 3, 1))))
        elif self.primitive == "lattice":
            pattern = symmetric_girih_pattern(self.object_id, radius_m=0.48)
            for first, second in pattern.edges:
                start, end = pattern.vertices[first], pattern.vertices[second]
                dx, dy = end[0] - start[0], end[1] - start[1]
                scale = 0.012 / max(math.hypot(dx, dy), 1e-9)
                nx, ny = -dy * scale, dx * scale
                offset = len(vertices)
                vertices.extend(
                    (
                        (start[0] + nx, start[1] + ny, 0),
                        (start[0] - nx, start[1] - ny, 0),
                        (end[0] - nx, end[1] - ny, 0),
                        (end[0] + nx, end[1] + ny, 0),
                    )
                )
                faces.append(tuple(offset + i for i in range(4)))
        else:
            vertices = [(x, y, z) for z in (-0.5, 0.5) for y in (-0.5, 0.5) for x in (-0.5, 0.5)]
            faces = [
                (0, 1, 3, 2),
                (4, 6, 7, 5),
                (0, 4, 5, 1),
                (2, 3, 7, 6),
                (0, 2, 6, 4),
                (1, 5, 7, 3),
            ]
        transformed = tuple(
            (
                v[0] * self.dimensions_m[0] + self.position_m[0],
                v[1] * self.dimensions_m[1] + self.position_m[1],
                v[2] * self.dimensions_m[2] + self.position_m[2],
            )
            for v in vertices
        )
        return Mesh(transformed, tuple(faces))


@dataclass(frozen=True, slots=True)
class ArchitectureSpec:
    style: ArchitectureStyle = ArchitectureStyle.IRANIAN_EASTERN
    width_m: float = 24.0
    height_m: float = 12.0
    depth_m: float = 18.0
    floors: int = 3
    bays: int = 5
    interior: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "style", ArchitectureStyle(self.style))
        for name in ("width_m", "height_m", "depth_m"):
            finite(getattr(self, name), name, 1, 500)
        integer(self.floors, "floors", 1, 30)
        integer(self.bays, "bays", 1, 20)
        if not isinstance(self.interior, bool):
            raise ValueError("interior must be a boolean")

    def payload(self) -> tuple[object, ...]:
        return (
            self.style.value,
            self.width_m,
            self.height_m,
            self.depth_m,
            self.floors,
            self.bays,
            self.interior,
        )


@dataclass(frozen=True, slots=True)
class ParallaxLayer:
    name: DepthLayer
    objects: tuple[SceneObject, ...]


@dataclass(frozen=True, slots=True)
class SceneDNASeal:
    """Content-address integrity seal, not a signature or authorization token."""

    sha256: str
    schema: str = "SceneDNASeal-v1"

    def verify(self, scene: SceneLayout) -> bool:
        return self.schema == "SceneDNASeal-v1" and self.sha256 == digest(scene.geometry_payload())


@dataclass(frozen=True, slots=True)
class SceneLayout:
    spec: ArchitectureSpec
    objects: tuple[SceneObject, ...]
    seed_sha256: str
    seal: SceneDNASeal

    @property
    def layers(self) -> tuple[ParallaxLayer, ...]:
        return tuple(
            ParallaxLayer(layer, tuple(o for o in self.objects if o.layer is layer))
            for layer in DepthLayer
        )

    def geometry_payload(self) -> tuple[object, ...]:
        return self.spec.payload(), self.seed_sha256, tuple(o.payload() for o in self.objects)

    def verify(self) -> bool:
        return self.seal.verify(self)


_STYLE_FEATURES: dict[ArchitectureStyle, tuple[tuple[str, str, str], ...]] = {
    ArchitectureStyle.IRANIAN_EASTERN: (
        ("iwan", "pointed_arch", "terracotta"),
        ("dome", "dome", "turquoise_tile"),
        ("badgir_windcatcher", "box", "adobe"),
        ("orsi_window", "lattice", "colored_glass"),
        ("girih_screen", "lattice", "carved_wood"),
        ("courtyard", "plane", "sandstone"),
    ),
    ArchitectureStyle.MODERN_URBAN: (
        ("glass_tower", "glass_panel", "low_e_glass"),
        ("street", "plane", "asphalt"),
        ("bridge", "box", "steel"),
        ("apartment", "box", "concrete"),
    ),
    ArchitectureStyle.CLASSICAL_HISTORIC: (
        ("castle", "box", "limestone"),
        ("palace", "box", "marble"),
        ("colonnaded_hall", "column", "marble"),
        ("vaulted_gate", "pointed_arch", "granite"),
    ),
    ArchitectureStyle.INDUSTRIAL_CYBERPUNK: (
        ("factory", "box", "corrugated_steel"),
        ("warehouse", "box", "brick"),
        ("tunnel", "tunnel", "steel"),
        ("neon_sign", "neon", "emissive_magenta"),
    ),
    ArchitectureStyle.SPACE_SCIFI: (
        ("orbital_station", "orbital_ring", "titanium"),
        ("planetary_base", "dome", "regolith"),
        ("spaceship_corridor", "box", "ceramic_composite"),
        ("airlock", "tunnel", "aluminium"),
    ),
}


_DEFAULT_ARCHITECTURE_SPEC = ArchitectureSpec()


def generate_architecture(
    seed: Seed, spec: ArchitectureSpec = _DEFAULT_ARCHITECTURE_SPEC
) -> SceneLayout:
    seed_sha256 = seed_bytes(seed, "architecture").hex()
    objects: list[SceneObject] = []

    def add(name: str, primitive: str, position: Vec3, dimensions: Vec3, material: str) -> None:
        object_id = digest((seed_sha256, name, primitive, position, dimensions, material))
        objects.append(
            SceneObject(
                object_id, name, primitive, position, dimensions, material, depth_layer(position[2])
            )
        )

    width, height, depth = spec.width_m, spec.height_m, spec.depth_m
    facade_z = 40.0 + depth / 2
    add(
        "building_shell",
        "box",
        (0, height / 2, facade_z),
        (width, height, depth),
        "interior_plaster" if spec.interior else "structural_masonry",
    )
    parent = ShapeBounds((-width / 2, 0, 40), (width, height, 0.3))
    floors = split_shape(parent, 1, (1.0,) * spec.floors)
    bay_weights = tuple(
        0.85 + 0.3 * float(v) for v in uniform(seed, "architecture/bays", spec.bays)
    )
    for floor_index, floor in enumerate(floors):
        for bay_index, bay in enumerate(split_shape(floor, 0, bay_weights)):
            x, y, z = bay.origin_m
            w, h, _ = bay.dimensions_m
            primitive = (
                "lattice" if spec.style is ArchitectureStyle.IRANIAN_EASTERN else "glass_panel"
            )
            add(
                f"window_{floor_index}_{bay_index}",
                primitive,
                (x + w / 2, y + h * 0.55, z - 0.1),
                (w * 0.65, h * 0.6, 0.12),
                "stained_glass" if primitive == "lattice" else "glass",
            )
    for index, (name, primitive, material) in enumerate(_STYLE_FEATURES[spec.style]):
        x = (index % 3 - 1) * width * 0.28
        y = height * 0.35 if primitive != "dome" else height * 1.1
        feature_width, feature_height = width * 0.25, height * 0.7
        if name in ("street", "courtyard"):
            y, feature_width, feature_height = 0.02, width * 1.4, 0.04
        add(
            name,
            primitive,
            (x, y, 34 + index * 2),
            (feature_width, feature_height, max(0.2, depth * 0.25)),
            material,
        )
    # Actual repeated colonnades, not just an architectural style label.
    if spec.style in (ArchitectureStyle.IRANIAN_EASTERN, ArchitectureStyle.CLASSICAL_HISTORIC):
        for index in range(spec.bays + 1):
            add(
                f"facade_column_{index}",
                "column",
                (-width / 2 + width * index / spec.bays, height * 0.35, 37),
                (0.45, height * 0.7, 0.45),
                "sandstone",
            )
    props = (
        ("table", "box", "wood"),
        ("chair", "box", "wood"),
        ("lamp", "column", "brass"),
        ("column", "column", "stone"),
        ("tree", "tree", "foliage"),
        ("rock", "rock", "granite"),
        ("stage_prop", "box", "painted_wood"),
    )
    offsets = uniform(seed, "decor/offsets", len(props) * 2).reshape(len(props), 2)
    for index, ((name, primitive, material), randoms) in enumerate(
        zip(props, offsets, strict=True)
    ):
        z = (3.0, 5.0, 15.0, 22.0, 60.0, 75.0, 6.5)[index]
        scale = 0.8 + float(randoms[1])
        add(
            name,
            primitive,
            ((float(randoms[0]) - 0.5) * 10, scale / 2, z),
            (scale, scale, scale),
            material,
        )
    add("far_sky", "dome", (0, 0, 10000), (20000, 10000, 20000), "procedural_sky")
    payload = spec.payload(), seed_sha256, tuple(o.payload() for o in objects)
    return SceneLayout(spec, tuple(objects), seed_sha256, SceneDNASeal(digest(payload)))


def parallax_project(u: float, focal_length_px: float, delta_x_m: float, depth_m: float) -> float:
    """The requested convention: u' = u + f * delta_X / Z."""
    finite(u, "image coordinate", -1e12, 1e12)
    finite(focal_length_px, "focal length px", 1e-6, 1e6)
    finite(delta_x_m, "camera delta X", -1e6, 1e6)
    finite(depth_m, "camera depth", 1e-6, 1e9)
    return u + focal_length_px * delta_x_m / depth_m


@dataclass(frozen=True, slots=True)
class CameraKeyframe:
    time_seconds: float
    position_m: Vec3
    focal_length_px: float = 960.0

    def __post_init__(self) -> None:
        finite(self.time_seconds, "keyframe time", 0, 3600)
        object.__setattr__(self, "position_m", vector(self.position_m, "camera position", 10000))
        finite(self.focal_length_px, "focal length px", 1, 100000)


@dataclass(frozen=True, slots=True)
class CameraCurve:
    keyframes: tuple[CameraKeyframe, ...]
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        integer(len(self.keyframes), "camera keyframe count", 2, 64)
        if self.keyframes[0].time_seconds != 0 or any(
            a.time_seconds >= b.time_seconds
            for a, b in zip(self.keyframes, self.keyframes[1:], strict=False)
        ):
            raise ValueError("camera times must start at zero and strictly increase")
        object.__setattr__(
            self,
            "sha256",
            digest(
                tuple((k.time_seconds, k.position_m, k.focal_length_px) for k in self.keyframes)
            ),
        )

    def sample(self, time_seconds: float) -> CameraKeyframe:
        finite(time_seconds, "camera sample time", 0, self.keyframes[-1].time_seconds)
        for a, b in zip(self.keyframes, self.keyframes[1:], strict=False):
            if time_seconds == b.time_seconds:
                return b
            if time_seconds < b.time_seconds:
                t = (time_seconds - a.time_seconds) / (b.time_seconds - a.time_seconds)
                smooth = t * t * (3 - 2 * t)
                p = tuple(
                    x + (y - x) * smooth for x, y in zip(a.position_m, b.position_m, strict=True)
                )
                return CameraKeyframe(
                    time_seconds,
                    (p[0], p[1], p[2]),
                    a.focal_length_px + (b.focal_length_px - a.focal_length_px) * smooth,
                )
        raise AssertionError("validated camera time has no containing segment")

    def project(self, obj: SceneObject, time_seconds: float) -> tuple[float, float]:
        camera = self.sample(time_seconds)
        depth = obj.position_m[2] - camera.position_m[2]
        # Plus sign defines delta_X as scene-relative camera displacement.
        x = parallax_project(
            0, camera.focal_length_px, obj.position_m[0] + camera.position_m[0], depth
        )
        y = parallax_project(
            0, camera.focal_length_px, obj.position_m[1] + camera.position_m[1], depth
        )
        return x, y


def generate_camera_curve(
    seed: Seed, template: str = "dolly", duration_seconds: float = 8
) -> CameraCurve:
    finite(duration_seconds, "camera duration", 0.01, 3600)
    templates = {
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
    }
    if template not in templates:
        raise ValueError("unknown director camera template")
    amplitude = 0.3 + 0.5 * float(uniform(seed, "camera/amplitude", 1)[0])
    positions: dict[str, tuple[Vec3, Vec3, Vec3]] = {
        "static": ((0, 0, 0), (0, 0, 0), (0, 0, 0)),
        "dolly": ((0, 0, 0), (amplitude / 2, 0, 0.1), (amplitude, 0, 0.2)),
        "orbit": ((-amplitude, 0, 0), (0, 0.1, 0.2), (amplitude, 0, 0)),
        "crane": ((0, 0, 0), (0.1, amplitude / 2, 0), (0.2, amplitude, 0)),
        "handheld": ((0, 0, 0), (amplitude * 0.12, -0.04, 0.03), (-0.02, 0.03, 0.06)),
        "tracking": ((-amplitude, 0, 0), (0, 0, 0), (amplitude, 0, 0)),
        "wide": ((0, 0, 0), (0.1, 0, -0.1), (0.2, 0, -0.2)),
        "closeup": ((0, 0, 0), (0, 0, 0.2), (0, 0, 0.4)),
        "establishing": ((-amplitude, amplitude, 0), (0, amplitude / 2, 0.1), (amplitude, 0, 0.2)),
        "low_angle": ((0, -0.3, 0), (amplitude / 2, -0.3, 0), (amplitude, -0.3, 0)),
    }
    focal = 600.0 if template == "wide" else (1500.0 if template == "closeup" else 960.0)
    return CameraCurve(
        tuple(
            CameraKeyframe(duration_seconds * i / 2, p, focal)
            for i, p in enumerate(positions[template])
        )
    )
