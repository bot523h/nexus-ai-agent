"""Procedural character rendering + measurable visual descriptors.

Two halves, and the split matters
--------------------------------
**Renderer** (``render_portrait`` / ``render_full_body``): draws a synthetic
character from capsule parameters. Deterministic — the same capsule, view,
lighting, expression and outfit always produce byte-identical pixels. Synthetic
fixtures are used because no licensed face model, no GPU and no consent to use a
real person's likeness exist in this environment (see the Live Truth document).

**Descriptors** (``observe``): classical, hand-built measurements taken *from the
pixels*, never from the parameters that produced them:

* skin-region localisation (YCbCr chroma heuristic) and a normalised face crop;
* facial geometry proxied from dark-structure analysis (inter-ocular span, eye
  aspect, nose width, mouth width/height, jaw width) — the same *class* of cue a
  learned embedder uses, at a fraction of the range;
* appearance blocks and a gradient-orientation histogram (HOG-lite);
* body silhouette proportions from a foreground mask estimated from the border;
* hair region statistics; garment colour/pattern statistics.

Honesty boundary: these are **not** ArcFace/SFace embeddings. They are
reproducible classical measurements that let the verifier's *machinery*
(quality gating, calibration, three-band decisions, FAR/FRR) be measured for
real. ``tests/character_capsule`` record which dimensions are measured and
``research/character-intelligence/VISUAL_CONTINUITY_EVALUATION.md`` states the
transfer limits: swapping in a licensed embedder is an adapter change, not a
rewrite, but the calibrated thresholds do **not** transfer.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .media import resize_bilinear, to_gray
from .model import BodyModule, FaceModule, HairModule
from .scene import Environment, ShotReference
from .wardrobe import Outfit

# ---------------------------------------------------------------------------
# Small deterministic raster toolkit
# ---------------------------------------------------------------------------


def hex_to_rgb(value: str) -> np.ndarray:
    """Public alias for the palette helper (``#rrggbb`` → 0..1 RGB)."""
    return _hex_to_rgb(value)


def _hex_to_rgb(value: str) -> np.ndarray:
    value = value.lstrip("#")
    return np.array([int(value[i : i + 2], 16) / 255.0 for i in (0, 2, 4)], dtype=np.float64)


def _rng(seed_material: str) -> np.random.Generator:
    digest = hashlib.sha256(seed_material.encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "big"))


def _grid(height: int, width: int) -> tuple[np.ndarray, np.ndarray]:
    ys = (np.arange(height, dtype=np.float64) + 0.5) / height
    xs = (np.arange(width, dtype=np.float64) + 0.5) / width
    return np.meshgrid(xs, ys)


def _ellipse(
    xx: np.ndarray, yy: np.ndarray, cx: float, cy: float, rx: float, ry: float
) -> np.ndarray:
    safe_rx = max(rx, 1e-6)
    safe_ry = max(ry, 1e-6)
    return ((xx - cx) / safe_rx) ** 2 + ((yy - cy) / safe_ry) ** 2 <= 1.0


def _blend(image: np.ndarray, mask: np.ndarray, colour: np.ndarray, alpha: float = 1.0) -> None:
    """Paint ``colour`` (a triple or a full-frame array) inside ``mask``."""
    if not mask.any():
        return
    array = np.asarray(colour, dtype=np.float64)
    target = array[mask] if array.ndim == 3 else array
    if alpha >= 1.0:
        image[mask] = target
        return
    image[mask] = (1.0 - alpha) * image[mask] + alpha * target


def _lambert(
    xx: np.ndarray, yy: np.ndarray, cx: float, cy: float, rx: float, ry: float, light: np.ndarray
) -> np.ndarray:
    """Cheap sphere-ish shading for a face ellipse (0.55..1.25 multiplier)."""
    nx = (xx - cx) / max(rx, 1e-6)
    ny = (yy - cy) / max(ry, 1e-6)
    inside = np.clip(1.0 - nx**2 - ny**2, 0.0, 1.0)
    nz = np.sqrt(inside)
    dot = np.clip(nx * light[0] + ny * light[1] + nz * light[2], 0.0, 1.0)
    return 0.55 + 0.70 * dot


def kelvin_to_rgb(kelvin: int) -> np.ndarray:
    """Approximate black-body tint normalised to unit mean (Tanner Helland fit)."""
    temperature = kelvin / 100.0
    if temperature <= 66:
        red = 255.0
        green = 99.4708025861 * np.log(max(temperature, 1e-6)) - 161.1195681661
        blue = (
            0.0 if temperature <= 19 else 138.5177312231 * np.log(temperature - 10) - 305.0447927307
        )
    else:
        red = 329.698727446 * ((temperature - 60) ** -0.1332047592)
        green = 288.1221695283 * ((temperature - 60) ** -0.0755148492)
        blue = 255.0
    rgb = np.clip(np.array([red, green, blue]) / 255.0, 0.05, 1.0)
    return rgb / rgb.mean()


# ---------------------------------------------------------------------------
# Render specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Lighting:
    """One declared lighting condition (scene environment → render parameters)."""

    key_kelvin: int = 5600
    intensity: float = 1.0
    azimuth: float = -0.55  # light from camera-left
    elevation: float = 0.45

    def vector(self) -> np.ndarray:
        vec = np.array([self.azimuth, -self.elevation, 0.85])
        return vec / np.linalg.norm(vec)

    def tint(self) -> np.ndarray:
        return kelvin_to_rgb(self.key_kelvin)


@dataclass(frozen=True)
class RenderSpec:
    """Everything the renderer needs for one frame (all from declared models)."""

    face: FaceModule
    body: BodyModule
    hair: HairModule
    outfit: Outfit
    expression: str = "neutral"
    view: str = "front"
    lighting: Lighting = field(default_factory=Lighting)
    environment: Environment = field(default_factory=Environment)
    scar: bool = False

    def key(self) -> str:
        return "|".join(
            [
                self.face.digest,
                self.body.digest,
                self.hair.digest,
                self.outfit.outfit_id,
                str(self.outfit.version),
                self.expression,
                self.view,
                str(self.lighting.key_kelvin),
                f"{self.lighting.intensity:.3f}",
                self.environment.background_colour,
                str(self.scar),
            ]
        )


VIEW_YAW = {"front": 0.0, "three_quarter": 32.0, "profile": 72.0}


def _face_shapes(
    xx: np.ndarray, yy: np.ndarray, spec: RenderSpec, yaw_deg: float
) -> dict[str, np.ndarray]:
    """Face-region masks for the requested view, in normalised image coordinates."""
    yaw = np.deg2rad(yaw_deg)
    compress = float(np.cos(yaw))  # horizontal foreshortening of facial features
    shift = float(np.sin(yaw)) * 0.06  # nose/eye drift toward the profile edge
    face = spec.face
    cx = 0.5 - shift * 0.35
    cy = 0.47
    rx = 0.235 * (0.55 + 0.45 * compress)
    ry = 0.285
    masks: dict[str, np.ndarray] = {}
    head = _ellipse(xx, yy, cx, cy, rx, ry)
    # jaw: narrower ellipse in the lower half produces a chin
    jaw_width = rx * (0.55 + 0.75 * (face.jaw_width_ratio - 0.3))
    jaw = _ellipse(xx, yy, cx, cy + ry * 0.55, jaw_width, ry * 0.42)
    masks["head"] = head
    masks["jaw"] = jaw & head
    face_mask = head | jaw
    masks["face"] = face_mask
    eye_span = face.eye_spacing_ratio * compress
    eye_rx = face.eye_size_ratio * (0.6 + 0.4 * compress)
    eye_ry = eye_rx * 0.55
    openness = {"smile": 0.85, "sad": 0.7, "angry": 0.75, "surprised": 1.35}.get(
        spec.expression, 1.0
    )
    eye_y = cy - ry * 0.12
    left_eye_x = cx - eye_span
    right_eye_x = cx + eye_span
    masks["eye_left"] = _ellipse(xx, yy, left_eye_x, eye_y, eye_rx, eye_ry * openness) & face_mask
    if yaw_deg < 55:
        masks["eye_right"] = (
            _ellipse(xx, yy, right_eye_x, eye_y, eye_rx, eye_ry * openness) & face_mask
        )
    else:  # near-profile: the far eye is hidden by the nose bridge
        masks["eye_right"] = np.zeros_like(face_mask, dtype=bool)
    masks["iris_left"] = (
        _ellipse(xx, yy, left_eye_x + shift * 0.3, eye_y, eye_rx * 0.55, eye_ry * 0.55 * openness)
        & face_mask
    )
    masks["iris_right"] = (
        _ellipse(xx, yy, right_eye_x + shift * 0.3, eye_y, eye_rx * 0.55, eye_ry * 0.55 * openness)
        & face_mask
    )
    masks["pupil"] = (
        _ellipse(xx, yy, left_eye_x + shift * 0.3, eye_y, eye_rx * 0.22, eye_ry * 0.22 * openness)
        | _ellipse(
            xx, yy, right_eye_x + shift * 0.3, eye_y, eye_rx * 0.22, eye_ry * 0.22 * openness
        )
    ) & face_mask
    brow_y = eye_y - eye_ry * 2.4
    brow_ry = max(face.brow_thickness_ratio * 0.09, 0.004)
    masks["brow_left"] = _ellipse(xx, yy, left_eye_x, brow_y, eye_rx * 1.25, brow_ry) & face_mask
    masks["brow_right"] = _ellipse(xx, yy, right_eye_x, brow_y, eye_rx * 1.25, brow_ry) & face_mask
    nose_w = face.nose_width_ratio * 0.5
    nose_top = cy - ry * 0.05
    nose_bottom = cy + ry * (0.10 + 0.25 * face.nose_length_ratio)
    nose = np.abs(xx - (cx + shift * 0.5)) <= nose_w * (1.0 - 0.5 * compress)
    nose_mask = nose & (yy >= nose_top) & (yy <= nose_bottom) & face_mask
    masks["nose"] = nose_mask
    mouth_w = face.mouth_width_ratio * 0.5
    mouth_y = cy + ry * 0.55
    if spec.expression == "smile":
        mouth_curve = (xx - cx) ** 2 * 1.9
        mouth = (np.abs(xx - cx) <= mouth_w) & (
            np.abs(yy - (mouth_y - mouth_curve + 0.02)) <= 0.012
        )
    elif spec.expression == "sad":
        mouth_curve = (xx - cx) ** 2 * 1.9
        mouth = (np.abs(xx - cx) <= mouth_w * 0.9) & (np.abs(yy - (mouth_y + mouth_curve)) <= 0.010)
    elif spec.expression == "angry":
        mouth = (np.abs(xx - cx) <= mouth_w) & (np.abs(yy - mouth_y) <= 0.012)
    elif spec.expression == "surprised":
        mouth = _ellipse(xx, yy, cx, mouth_y, mouth_w * 0.45, 0.028)
    else:
        mouth = (np.abs(xx - cx) <= mouth_w) & (np.abs(yy - mouth_y) <= 0.008)
    masks["mouth"] = mouth & face_mask
    if spec.scar:
        scar = (
            (np.abs(xx - (cx - rx * 0.55)) <= 0.006)
            & (yy >= cy - ry * 0.1)
            & (yy <= cy + ry * 0.35)
        )
        masks["scar"] = scar & face_mask
    return masks


def render_portrait(spec: RenderSpec, *, size: int = 256) -> np.ndarray:
    """Render a portrait (``size`` × ``size`` × 3 uint8). Deterministic."""
    height = width = int(size)
    xx, yy = _grid(height, width)
    background = _hex_to_rgb(spec.environment.background_colour)
    image = np.broadcast_to(background, (height, width, 3)).astype(np.float64).copy()
    # subtle environment gradient so background statistics are not degenerate
    image *= (0.92 + 0.16 * (1.0 - yy))[..., None]

    yaw = VIEW_YAW[spec.view]
    masks = _face_shapes(xx, yy, spec, yaw)
    light = spec.lighting.vector()
    tint = spec.lighting.tint()
    skin = _hex_to_rgb(spec.face.skin_colour)
    hair = _hex_to_rgb(spec.hair.colour)

    # hair behind the head
    cx, cy = 0.5, 0.47
    rx, ry = 0.235, 0.285
    back = _ellipse(xx, yy, cx, cy - ry * 0.08, rx * 1.22, ry * 1.18)
    if spec.hair.style == "long":
        back = back | _ellipse(xx, yy, cx, cy + ry * 0.5, rx * 1.15, ry * 0.9)
    if spec.hair.style == "bun":
        back = back | _ellipse(xx, yy, cx, cy - ry * 1.25, rx * 0.45, ry * 0.35)
    if spec.hair.style == "braid":
        back = back | _ellipse(xx, yy, cx + rx * 0.9, cy + ry * 0.4, rx * 0.35, ry * 0.6)
    back = back & ~masks["face"]
    shade = _lambert(xx, yy, cx, cy - ry * 0.1, rx * 1.2, ry * 1.2, light)
    _blend(image, back, hair * shade[..., None] * tint)

    # face
    face_shade = _lambert(xx, yy, cx, cy, rx, ry, light)
    _blend(image, masks["face"], skin * face_shade[..., None] * tint)
    if spec.face.skin_texture == "weathered":
        rng = _rng(spec.key() + "|texture")
        noise = rng.normal(0.0, 0.035, size=(height, width, 1))
        skin_noised = np.clip(skin * face_shade[..., None] * tint + noise, 0.0, 1.0)
        _blend(image, masks["face"], skin_noised)
    if spec.face.freckles:
        rng = _rng(spec.key() + "|freckles")
        dots_y = rng.integers(
            int(cy * height - ry * height * 0.4), int(cy * height + ry * height * 0.2)
        )
        dots_x = rng.integers(int((cx - rx) * width), int((cx + rx) * width))
        for dy, dx in zip(dots_y, dots_x, strict=False):
            if 0 <= dy < height and 0 <= dx < width and masks["face"][dy, dx]:
                image[max(dy - 1, 0) : dy + 1, max(dx - 1, 0) : dx + 1] *= 0.82

    # eyebrows
    brow_colour = hair * 0.75
    _blend(image, masks["brow_left"] | masks["brow_right"], brow_colour)
    # eyes
    white = np.array([0.93, 0.93, 0.95])
    _blend(image, masks["eye_left"] | masks["eye_right"], white)
    iris = _hex_to_rgb(spec.face.eye_colour)
    _blend(image, masks["iris_left"] | masks["iris_right"], iris)
    _blend(image, masks["pupil"], iris * 0.25)
    # shoulders and upper torso (portraits must show the garment to be verifiable)
    top_colour = _hex_to_rgb(spec.outfit.top_colour)
    torso = (yy >= 0.80) & (np.abs(xx - 0.5) <= 0.26)
    shoulder_band = (yy >= 0.80) & (yy <= 0.86) & (np.abs(xx - 0.5) <= 0.30)
    torso_shade = _lambert(xx, yy, 0.5, 0.95, 0.45, 0.30, light)
    _blend(image, torso | shoulder_band, top_colour * torso_shade[..., None] * tint)
    collar = (yy >= 0.795) & (yy <= 0.825) & (np.abs(xx - 0.5) <= 0.12)
    _blend(image, collar, _hex_to_rgb(spec.outfit.accent_colour) * tint)
    # nose shadow and mouth
    _blend(image, masks["nose"], np.clip(skin * face_shade[..., None] * 0.86, 0.0, 1.0))
    _blend(image, masks["mouth"], np.array([0.45, 0.22, 0.24]))
    if "scar" in masks:
        _blend(image, masks["scar"], np.array([0.62, 0.30, 0.28]))
    return (np.clip(image, 0.0, 1.0) * 255.0).round().astype(np.uint8)


def render_full_body(spec: RenderSpec) -> np.ndarray:
    """Render a full-body figure (192 × 384 × 3 uint8). Deterministic."""
    height, width = 384, 192
    xx, yy = _grid(height, width)
    background = _hex_to_rgb(spec.environment.background_colour)
    image = np.broadcast_to(background, (height, width, 3)).astype(np.float64).copy()
    image *= (0.9 + 0.2 * (1.0 - yy))[..., None]
    light = spec.lighting.vector()
    tint = spec.lighting.tint()
    top = _hex_to_rgb(spec.outfit.top_colour)
    bottom = _hex_to_rgb(spec.outfit.bottom_colour)
    accent = _hex_to_rgb(spec.outfit.accent_colour)
    skin = _hex_to_rgb(spec.face.skin_colour)

    scale = 0.82 + (spec.body.height_cm - 170.0) / 400.0
    shoulder = spec.body.shoulder_width_ratio * 0.55
    hip = spec.body.hip_width_ratio * 0.55
    limb = spec.body.limb_thickness_ratio * 0.5
    cx = 0.5
    head_top = 0.10
    head_bottom = 0.10 + 0.16 * scale
    torso_top = head_bottom
    torso_bottom = 0.55
    leg_bottom = 0.94

    head = _ellipse(xx, yy, cx, (head_top + head_bottom) / 2, 0.085 * scale, 0.085 * scale)
    neck = (np.abs(xx - cx) <= 0.035) & (yy >= head_bottom - 0.01) & (yy <= torso_top + 0.01)
    torso_width = shoulder * (1.0 - 0.35 * (yy - torso_top) / max(torso_bottom - torso_top, 1e-6))
    torso = (np.abs(xx - cx) <= torso_width) & (yy >= torso_top) & (yy <= torso_bottom)
    # widen toward the hips
    hip_band = (yy >= torso_bottom - 0.06) & (yy <= torso_bottom)
    torso = torso | ((np.abs(xx - cx) <= hip) & hip_band)
    arms = np.zeros_like(torso)
    for side in (-1.0, 1.0):
        arm_x = cx + side * (shoulder + limb * 0.5)
        arms = arms | (
            (np.abs(xx - arm_x) <= limb) & (yy >= torso_top + 0.02) & (yy <= torso_bottom + 0.10)
        )
    legs = np.zeros_like(torso)
    for side in (-1.0, 1.0):
        leg_x = cx + side * (hip * 0.5)
        legs = legs | (
            (np.abs(xx - leg_x) <= limb * 1.05) & (yy >= torso_bottom) & (yy <= leg_bottom)
        )
    shoes = legs & (yy >= leg_bottom - 0.045)
    shade = _lambert(xx, yy, cx, 0.45, 0.5, 0.55, light)

    _blend(image, legs, bottom * shade[..., None] * tint)
    _blend(image, shoes, np.array([0.12, 0.12, 0.14]))
    _blend(image, torso, top * shade[..., None] * tint)
    _blend(image, arms, top * shade[..., None] * tint * 0.95)
    belt = torso & (np.abs(yy - (torso_bottom - 0.05)) <= 0.012)
    _blend(image, belt, accent)
    _blend(image, neck, skin * shade[..., None] * tint)
    _blend(image, head, skin * shade[..., None] * tint)
    hair = _rng(spec.key() + "|hair")
    hair_mask = _ellipse(
        xx, yy, cx, (head_top + head_bottom) / 2 - 0.015, 0.09 * scale, 0.085 * scale
    )
    _blend(image, hair_mask & head, _hex_to_rgb(spec.hair.colour) * (0.9 + 0.2 * hair.random()))
    # face marks so the full-body frame still carries identity cues
    eye_y = (head_top + head_bottom) / 2 - 0.012
    for side in (-1.0, 1.0):
        eye = _ellipse(xx, yy, cx + side * 0.032 * scale, eye_y, 0.010, 0.007)
        _blend(image, eye, _hex_to_rgb(spec.face.eye_colour))
    mouth = (np.abs(xx - cx) <= spec.face.mouth_width_ratio * 0.09) & (
        np.abs(yy - (eye_y + 0.035)) <= 0.005
    )
    _blend(image, mouth, np.array([0.45, 0.22, 0.24]))
    return (np.clip(image, 0.0, 1.0) * 255.0).round().astype(np.uint8)


def spec_from_shot(
    capsule_face: FaceModule,
    capsule_body: BodyModule,
    capsule_hair: HairModule,
    outfit: Outfit,
    shot: ShotReference | None,
    *,
    expression: str = "neutral",
    view: str = "front",
    lighting: Lighting | None = None,
    environment: Environment | None = None,
    scar: bool = False,
) -> RenderSpec:
    """Convenience: compose a render spec for a shot declaration."""
    chosen_expression = shot.declared.expression if shot is not None else expression
    return RenderSpec(
        face=capsule_face,
        body=capsule_body,
        hair=capsule_hair,
        outfit=outfit,
        expression=chosen_expression,
        view=view,
        lighting=lighting or Lighting(),
        environment=environment or Environment(),
        scar=scar,
    )


# ---------------------------------------------------------------------------
# Descriptors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """Measured descriptors of one frame, plus the quality that gates them."""

    face: dict[str, np.ndarray]
    hair: dict[str, np.ndarray]
    body: dict[str, np.ndarray]
    wardrobe: dict[str, np.ndarray]
    scene: dict[str, np.ndarray]
    face_quality: float
    face_reason: str = ""
    body_quality: float = 0.0
    framing: str = "portrait"

    def measurable(self, dimension: str) -> bool:
        """Whether a dimension was actually measured on this frame.

        Hair and wardrobe are measured inside the face-relative crop, so they are
        only meaningful when the face itself was localised. A full-body frame has
        no measurable wardrobe or hair here — measuring garments from a whole-body
        frame needs a dedicated segmenter, which this prototype does not have and
        does not pretend to have (see the report's limitations).
        """
        if dimension == "face":
            return self.face_quality > 0.0
        if dimension == "body":
            return self.body_quality > 0.0
        if dimension in {"hair", "wardrobe"}:
            return self.face_quality > 0.0
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "face_quality": round(float(self.face_quality), 6),
            "face_reason": self.face_reason,
            "body_quality": round(float(self.body_quality), 6),
            "framing": self.framing,
            "dimensions": {
                name: {key: [round(float(v), 6) for v in value] for key, value in part.items()}
                for name, part in (
                    ("face", self.face),
                    ("hair", self.hair),
                    ("body", self.body),
                    ("wardrobe", self.wardrobe),
                    ("scene", self.scene),
                )
            },
        }


def _skin_mask(rgb: np.ndarray) -> np.ndarray:
    array = rgb.astype(np.float64) / 255.0
    r, g, b = array[..., 0], array[..., 1], array[..., 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 0.564 * (b - y)
    cr = 0.713 * (r - y)
    # Chroma alone accepts maroon garments (their Cb/Cr land in the skin box), so
    # the standard ordering constraint of skin reflectance is added: green above
    # blue, red above green, and enough luminance to be skin rather than shadow.
    mask = (
        (cb > -0.16)
        & (cb < -0.02)
        & (cr > 0.05)
        & (cr < 0.22)
        & (y > 0.25)
        & (y < 0.95)
        & (g >= b)
        & (r > g)
    )
    return mask


#: Bounds for grey-world gains; wide enough to neutralise a 3000 K key, narrow
#: enough that a genuinely warm-lit scene is not forced to neutral grey.
WB_GAIN_CLAMP: tuple[float, float] = (0.85, 1.20)


def _white_balance(rgb: np.ndarray) -> np.ndarray:
    """Grey-world white balance with bounded gains.

    A warm (3000 K) or cool (7800 K) key light shifts every chromaticity in the
    frame; without this step skin segmentation and garment colour both drift with
    the light. Gains are clamped so a genuinely warm-coloured scene is not
    neutralised into grey.
    """
    values = rgb.astype(np.float64)
    means = values.reshape(-1, 3).mean(axis=0)
    target = float(means.mean())
    gains = np.clip(target / np.maximum(means, 1e-6), WB_GAIN_CLAMP[0], WB_GAIN_CLAMP[1])
    return np.clip(values * gains, 0.0, 255.0)


def _background_map(image: np.ndarray) -> np.ndarray:
    """Per-row backdrop colour taken from the left/right border columns.

    The renderer applies a vertical background gradient, so a single border
    median is wrong near the top of the frame and the whole upper band was being
    classified as subject under a bright key light.
    """
    height = image.shape[0]
    edges = np.concatenate([image[:, :3, :], image[:, -3:, :]], axis=1).astype(np.float64)
    rows = np.median(edges, axis=1)
    if height >= 9:
        padded = np.pad(rows, ((4, 4), (0, 0)), mode="edge")
        rows = np.stack([padded[offset : offset + height] for offset in range(9)], axis=0).mean(
            axis=0
        )
    return rows


def _foreground_mask(rgb: np.ndarray, background: np.ndarray) -> np.ndarray:
    """Pixels that differ from the backdrop in *chromaticity* or relative luminance.

    Chromaticity dominates the test so a global exposure change (a brighter key
    light, a different stop) cannot flip the mask, which is what made a naive
    RGB-distance threshold fail under a 3000 K key.
    """
    chroma = _chromaticity(rgb)[..., :2]
    bg_map = _background_map(rgb)
    bg_chroma = _chromaticity(bg_map)[:, :2].reshape(-1, 1, 2)
    chroma_distance = np.linalg.norm(chroma - bg_chroma, axis=2)
    bg_luma = _chromaticity(bg_map)[:, 2].reshape(-1, 1)
    luma = _chromaticity(rgb)[..., 2]
    relative_luma = np.abs(luma - bg_luma) / np.maximum(bg_luma, 0.05)
    return (chroma_distance > 0.025) | (relative_luma > 0.18)


def _bbox(mask: np.ndarray, *, low: float = 2.0, high: float = 98.0) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return (0, 0, 0, 0)
    y0, y1 = np.percentile(ys, [low, high])
    x0, x1 = np.percentile(xs, [low, high])
    return int(y0), int(y1) + 1, int(x0), int(x1) + 1


def _gradient_histogram(gray: np.ndarray, *, cells: int = 4, bins: int = 8) -> np.ndarray:
    gy, gx = np.gradient(gray)
    magnitude = np.hypot(gx, gy)
    angle = (np.arctan2(gy, gx) % np.pi) / np.pi  # unsigned orientation
    h, w = gray.shape
    hist = np.zeros((cells, cells, bins), dtype=np.float64)
    for cy in range(cells):
        for cx in range(cells):
            ys = slice(cy * h // cells, (cy + 1) * h // cells)
            xs = slice(cx * w // cells, (cx + 1) * w // cells)
            mag = magnitude[ys, xs].ravel()
            ang = angle[ys, xs].ravel()
            idx = np.clip((ang * bins).astype(int), 0, bins - 1)
            np.add.at(hist[cy, cx], idx, mag)
    # per-cell L2 normalisation: a cell's *pattern* of edge orientations is kept,
    # its contrast (which follows the key light) is divided out
    norms = np.linalg.norm(hist.reshape(cells, cells, bins), axis=2, keepdims=True)
    hist = hist.reshape(cells, cells, bins) / np.where(norms > 0, norms, 1.0)
    return hist.reshape(-1)


def _dark_structure(gray: np.ndarray) -> dict[str, float]:
    """Geometry proxies measured from dark regions inside a normalised crop."""
    median = float(np.median(gray))
    spread = float(np.std(gray)) or 1e-6
    dark = gray < median - 0.55 * spread
    h, w = gray.shape
    out: dict[str, float] = {}
    eye_band = dark[int(0.28 * h) : int(0.50 * h), :]
    cols = eye_band.sum(axis=0).astype(np.float64)
    if cols.sum() > 0:
        half = w // 2
        left = cols[:half]
        right = cols[half:]
        left_peak = int(np.argmax(left)) if left.size else 0
        right_peak = half + (int(np.argmax(right)) if right.size else 0)
        out["interocular_ratio"] = abs(right_peak - left_peak) / w
        out["eye_energy"] = float(cols.sum() / (eye_band.size or 1))
    else:
        out["interocular_ratio"] = 0.0
        out["eye_energy"] = 0.0
    nose_band = dark[int(0.42 * h) : int(0.72 * h), :]
    if nose_band.size:
        row_widths = nose_band.sum(axis=1).astype(np.float64)
        out["nose_width_ratio"] = float(row_widths.max() / w) if row_widths.size else 0.0
        out["nose_vertical_ratio"] = float(nose_band.any(axis=1).sum() / h)
    mouth_band = dark[int(0.70 * h) : int(0.95 * h), :]
    if mouth_band.size:
        rows = mouth_band.sum(axis=1).astype(np.float64)
        out["mouth_width_ratio"] = float(rows.max() / w) if rows.size else 0.0
        ys = np.nonzero(mouth_band.any(axis=1))[0]
        out["mouth_vertical_center"] = float(ys.mean() / mouth_band.shape[0]) if ys.size else 0.0
    else:
        out["mouth_width_ratio"] = 0.0
        out["mouth_vertical_center"] = 0.0
    return out


def _jaw_ratio(mask: np.ndarray, y0: int, y1: int) -> float:
    """Face-width at 85% of the face height relative to the widest row."""
    band = mask[y0:y1]
    widths = band.sum(axis=1).astype(np.float64)
    if widths.size < 5:
        return 0.0
    widest = float(widths.max())
    if widest <= 0:
        return 0.0
    index = min(int(0.85 * widths.size), widths.size - 1)
    return float(widths[index] / widest)


def framing_of(image: np.ndarray, face_bbox: tuple[int, int, int, int]) -> str:
    """Classify the frame as ``portrait`` or ``full_body`` from measured geometry.

    Applicability of a dimension depends on framing, and pretending otherwise is
    how body drift gets "verified" from a headshot. The rule is measured, not
    declared: the skin region must be a large part of the frame for a portrait.
    """
    height, width = image.shape[0], image.shape[1]
    y0, y1, x0, x1 = face_bbox
    face_area = max(y1 - y0, 0) * max(x1 - x0, 0)
    return "portrait" if face_area / float(height * width) > 0.05 else "full_body"


def _chromaticity(rgb: np.ndarray) -> np.ndarray:
    """Lighting-robust colour: r/(r+g+b), g/(r+g+b), luminance."""
    array = np.asarray(rgb, dtype=np.float64) / 255.0
    total = array.sum(axis=-1, keepdims=True)
    safe = np.where(total < 1e-6, 1e-6, total)
    chroma = array / safe
    luma = (0.299 * array[..., 0] + 0.587 * array[..., 1] + 0.114 * array[..., 2])[..., None]
    return np.concatenate([chroma[..., :2], luma], axis=-1)


def _mouth_curvature(gray: np.ndarray) -> float:
    """Signed curvature of the mouth line, measured inside a normalised crop.

    Dark pixels in the lower third are fitted with ``y = a·(x−x̄)² + c``; the sign
    of ``a`` (in image coordinates, y down) is positive for a smile. This is a
    proxy and is labelled as one everywhere it is reported.
    """
    h, w = gray.shape
    band = gray[int(0.68 * h) : int(0.95 * h), :]
    if band.size == 0:
        return 0.0
    threshold = float(np.median(band)) - 0.55 * float(np.std(band) or 1e-6)
    ys, xs = np.nonzero(band < threshold)
    if xs.size < 20:
        return 0.0
    x = (xs - xs.mean()) / max(w, 1)
    design = np.vstack([x * x, np.ones_like(x)]).T
    try:
        coefficient, *_ = np.linalg.lstsq(design, ys.astype(np.float64), rcond=None)
    except np.linalg.LinAlgError:
        return 0.0
    return float(coefficient[0] / max(band.shape[0], 1))


def _hair_measurements(
    rgb: np.ndarray, face_bbox: tuple[int, int, int, int], background: np.ndarray
) -> dict[str, np.ndarray]:
    """Hair identified as: not background, not skin, above the face's lower third."""
    height = rgb.shape[0]
    y0, y1, x0, x1 = face_bbox
    top = rgb[: max(int(max(y0, height * 0.55)), 1)]
    skin = _skin_mask(top)
    mask = _foreground_mask(top, background) & ~skin
    if not mask.any():
        return {
            "colour": np.zeros(3),
            "shape": np.zeros(3),
        }
    chroma = _chromaticity(top)
    colour = chroma[mask].mean(axis=0)
    ys, xs = np.nonzero(mask)
    extent = np.array(
        [
            float(mask.mean()),
            float((ys.max() - ys.min()) / max(top.shape[0], 1)),
            float((xs.max() - xs.min()) / max(top.shape[1], 1)),
        ],
        dtype=np.float64,
    )
    return {"colour": colour, "shape": extent}


def _eye_anchor(
    rgb: np.ndarray, face_bbox: tuple[int, int, int, int]
) -> tuple[float, float, float] | None:
    """Locate the eye line and return ``(centre_x, eye_y, interocular_px)``.

    Anchoring the crop on the eyes (a landmark) instead of on the skin-mask
    bounding box is what makes the descriptor comparable across key lights: the
    mask grows and shrinks with illumination, the eyes do not move.

    The best *pair* of dark column peaks is chosen under an anatomical constraint
    (interocular distance between 0.22 and 0.60 of the face width); picking a
    single peak per half was unstable under a warm key light, where one eyebrow
    can out-darken an eye.
    """
    y0, y1, x0, x1 = face_bbox
    gray = to_gray(rgb[y0:y1, x0:x1]) / 255.0
    if gray.ndim != 2 or gray.size == 0:
        return None
    height, width = gray.shape
    if width < 20 or height < 20:
        return None
    top, bottom = int(0.20 * height), int(0.58 * height)
    band = gray[top:bottom, :]
    if band.shape[0] < 3:
        return None
    profile = 1.0 - band.min(axis=0)
    profile = profile - float(profile.min())
    if float(profile.max()) <= 1e-3:
        return None
    if width >= 5:
        kernel = np.ones(5, dtype=np.float64) / 5.0
        profile = np.convolve(np.pad(profile, 2, mode="edge"), kernel, mode="valid")
    low = int(0.22 * width)
    high = int(0.60 * width)
    best: tuple[float, int, int] | None = None
    for left_peak in range(1, width - 1):
        for right_peak in range(left_peak + low, min(left_peak + high, width - 2) + 1):
            if left_peak >= right_peak:
                continue
            if (
                profile[left_peak] < profile[left_peak - 1]
                or profile[left_peak] < profile[left_peak + 1]
            ):
                continue
            if (
                profile[right_peak] < profile[right_peak - 1]
                or profile[right_peak] < profile[right_peak + 1]
            ):
                continue
            if min(profile[left_peak], profile[right_peak]) < 0.45 * float(profile.max()):
                continue
            value = float(profile[left_peak] + profile[right_peak])
            # prefer a balanced pair centred on the face, not two peaks on one side
            centre_penalty = abs(((left_peak + right_peak) / 2.0) - width / 2.0) / width
            value -= 1.2 * centre_penalty * float(profile.max())
            ratio = (right_peak - left_peak) / float(width)
            if not 0.30 <= ratio <= 0.62:
                continue
            if best is None or value > best[0]:
                best = (value, left_peak, right_peak)
    if best is None:
        return None
    _, left_peak, right_peak = best
    interocular = float(right_peak - left_peak)
    centre_x = x0 + (left_peak + right_peak) / 2.0
    row = gray[top:bottom, left_peak : right_peak + 1]
    eye_y = y0 + top + (int(np.argmax(1.0 - row.min(axis=1))) if row.size else band.shape[0] // 2)
    return centre_x, float(eye_y), interocular


def _face_measurements(
    rgb: np.ndarray, face_bbox: tuple[int, int, int, int], skin: np.ndarray
) -> dict[str, np.ndarray]:
    """Face groups: spatial chroma structure, gradients, geometry, skin/eye tone."""
    y0, y1, x0, x1 = face_bbox
    # Canonical *square* crop anchored on the eye line (see ``_eye_anchor``).
    anchor = _eye_anchor(rgb, face_bbox)
    if anchor is not None:
        centre_x, eye_y, interocular = anchor
        side = 2.55 * interocular
        top = int(round(eye_y - 0.40 * side))
        left = int(round(centre_x - side / 2.0))
    else:
        side = max(y1 - y0, x1 - x0) * 1.12
        top = int(round((y0 + y1) / 2.0 - side / 2.0))
        left = int(round((x0 + x1) / 2.0 - side / 2.0))
    side_px = max(int(round(side)), 8)
    top = max(0, min(top, max(rgb.shape[0] - side_px, 0)))
    left = max(0, min(left, max(rgb.shape[1] - side_px, 0)))
    crop_rgb = rgb[top : top + side_px, left : left + side_px]
    if crop_rgb.size == 0:
        crop_rgb = rgb[y0:y1, x0:x1]
    crop_gray = resize_bilinear(to_gray(crop_rgb) / 255.0, 64, 64)
    blocks = 4
    h, w = crop_rgb.shape[0], crop_rgb.shape[1]
    chroma = _chromaticity(crop_rgb)[..., :2]
    mean_chroma = chroma.reshape(-1, 2).mean(axis=0)
    # relative chroma: subtracting the crop mean cancels a global tint (key light)
    # while keeping the *spatial* pattern that carries identity.
    block_means = np.zeros((blocks, blocks, 2), dtype=np.float64)
    for by in range(blocks):
        for bx in range(blocks):
            patch = chroma[
                by * h // blocks : (by + 1) * h // blocks,
                bx * w // blocks : (bx + 1) * w // blocks,
            ]
            block_means[by, bx] = patch.reshape(-1, 2).mean(axis=0)
    # Illumination estimate: the 4x4 block means, upsampled. Subtracting it is a
    # high-pass filter on colour, which removes the smooth gradient a directional
    # key light produces while keeping local colour transitions (lips, lids, brows).
    illumination = resize_bilinear(block_means, h, w)
    high_pass = chroma - illumination
    appearance = np.zeros((blocks, blocks, 2), dtype=np.float64)
    for by in range(blocks):
        for bx in range(blocks):
            patch = high_pass[
                by * h // blocks : (by + 1) * h // blocks,
                bx * w // blocks : (bx + 1) * w // blocks,
            ]
            appearance[by, bx] = patch.reshape(-1, 2).mean(axis=0)
    geometry = _dark_structure(crop_gray)
    eye_patch = chroma[int(h * 0.30) : int(h * 0.44), int(w * 0.28) : int(w * 0.72)]
    eye_chroma = eye_patch.reshape(-1, 2).mean(axis=0) if eye_patch.size else np.zeros(2)
    normalised_gray = (crop_gray - float(crop_gray.mean())) / max(float(crop_gray.std()), 1e-6)
    return {
        "appearance": appearance.reshape(-1),
        "gradient": _gradient_histogram(normalised_gray),
        "geometry": np.array(
            [
                geometry.get("interocular_ratio", 0.0),
                geometry.get("eye_energy", 0.0),
                geometry.get("nose_width_ratio", 0.0),
                geometry.get("nose_vertical_ratio", 0.0),
                geometry.get("mouth_width_ratio", 0.0),
                geometry.get("mouth_vertical_center", 0.0),
                _jaw_ratio(skin, y0, y1),
            ],
            dtype=np.float64,
        ),
        "skin": np.concatenate([mean_chroma, eye_chroma]),
    }


def _hair_measurements(
    rgb: np.ndarray, face_bbox: tuple[int, int, int, int], background: np.ndarray
) -> dict[str, np.ndarray]:
    """Hair identified as: not background, not skin, above the face's lower third."""
    height = rgb.shape[0]
    y0, y1, x0, x1 = face_bbox
    top = rgb[: max(int(max(y0, height * 0.55)), 1)]
    skin = _skin_mask(top)
    mask = _foreground_mask(top, background) & ~skin
    if not mask.any():
        return {"colour": np.zeros(2), "shape": np.zeros(3)}
    chroma = _chromaticity(top)[..., :2]
    # Hair tone relative to *skin*: skin and hair are lit by the same key, so the
    # difference survives a lighting change — including a change of backdrop,
    # which a background-relative measure did not survive.
    skin_chroma = chroma[skin].mean(axis=0) if skin.any() else chroma.reshape(-1, 2).mean(axis=0)
    ys, xs = np.nonzero(mask)
    return {
        "colour": chroma[mask].mean(axis=0) - skin_chroma,
        "shape": np.array(
            [
                float(mask.mean()),
                float((ys.max() - ys.min()) / max(top.shape[0], 1)),
                float((xs.max() - xs.min()) / max(top.shape[1], 1)),
            ],
            dtype=np.float64,
        ),
    }


def _body_measurements(
    rgb: np.ndarray, background: np.ndarray, face_bbox: tuple[int, int, int, int]
) -> tuple[dict[str, np.ndarray], float, str]:
    fg = _foreground_mask(rgb, background)
    coverage = float(fg.mean())
    by0, by1, bx0, bx1 = _bbox(fg, low=1.0, high=99.0)
    height = max(by1 - by0, 1)
    width = max(bx1 - bx0, 1)
    y0, y1, _, _ = face_bbox
    face_ratio = max(y1 - y0, 1) / float(rgb.shape[0])
    framing = "portrait" if face_ratio > 0.18 else "full_body"
    quality = 0.0
    if framing == "full_body" and coverage > 0.05 and height / width > 1.5:
        quality = float(min(1.0, coverage * 6.0))
    row_widths = fg[by0:by1, bx0:bx1].sum(axis=1).astype(np.float64)
    normalised = row_widths / width if width else row_widths
    shoulder = (
        float(normalised[int(0.10 * height) : int(0.28 * height)].max()) if height > 8 else 0.0
    )
    hip = float(normalised[int(0.40 * height) : int(0.60 * height)].max()) if height > 8 else 0.0
    lower = float(normalised[int(0.75 * height) : int(0.95 * height)].mean()) if height > 8 else 0.0
    return (
        {
            "silhouette": np.array(
                [
                    height / max(width, 1),
                    shoulder,
                    hip,
                    shoulder / max(hip, 1e-6),
                    lower,
                    coverage,
                ],
                dtype=np.float64,
            ),
            "profile": np.asarray(
                np.interp(
                    np.linspace(0.0, 1.0, 16),
                    np.linspace(0.0, 1.0, normalised.size),
                    normalised,
                ),
                dtype=np.float64,
            ),
        },
        quality,
        framing,
    )


def _wardrobe_measurements(
    rgb: np.ndarray, background: np.ndarray, face_bbox: tuple[int, int, int, int]
) -> dict[str, np.ndarray]:
    """Garment statistics measured below the face — colour, bands, pattern energy."""
    y0, y1, x0, x1 = face_bbox
    start = int(min(max(y1, rgb.shape[0] * 0.55), rgb.shape[0] - 8))
    garment = rgb[start:]
    if garment.shape[0] < 4:
        return {"colour": np.zeros(3), "bands": np.zeros(9), "pattern": np.zeros(2)}
    fg = _foreground_mask(garment, background)
    skin = _skin_mask(garment)
    garment_mask = fg & ~skin
    if not garment_mask.any():
        return {"colour": np.zeros(3), "bands": np.zeros(9), "pattern": np.zeros(2)}
    chroma = _chromaticity(garment)
    bands = np.zeros((3, 3), dtype=np.float64)
    rows = garment.shape[0]
    for index in range(3):
        band = slice(index * rows // 3, (index + 1) * rows // 3)
        mask = garment_mask[band]
        if mask.any():
            bands[index] = chroma[band][mask].mean(axis=0)
    garment_gray = to_gray(garment) / 255.0
    gradient = np.abs(np.gradient(garment_gray, axis=0))[garment_mask]
    return {
        "colour": chroma[garment_mask].mean(axis=0),
        "bands": bands.reshape(-1),
        "pattern": np.array(
            [float(gradient.mean()), float(np.std(garment_gray[garment_mask]))], dtype=np.float64
        ),
    }


def observe(image: np.ndarray, *, subject_expected: bool = True) -> Observation:
    """Measure face/hair/body/wardrobe/scene descriptor groups from one RGB frame."""
    rgb = np.asarray(image, dtype=np.uint8)
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError(f"observe expects (H, W, 3) RGB, got {rgb.shape}")
    balanced = _white_balance(rgb)
    gray_full = to_gray(balanced) / 255.0
    mask = _skin_mask(balanced)
    coverage = float(mask.mean())
    y0, y1, x0, x1 = _bbox(mask)
    face_reason = ""
    face_quality = 0.0
    if coverage < 0.008:
        face_reason = "no skin-tone region found (face not localisable)"
    elif coverage > 0.55:
        face_reason = "skin-tone region covers the frame (background or segmentation failure)"
    elif (y1 - y0) < 24 or (x1 - x0) < 18:
        face_reason = "face region too small to measure"
    else:
        aspect = (x1 - x0) / max(y1 - y0, 1)
        if not 0.35 <= aspect <= 2.2:
            face_reason = "face region aspect ratio is implausible"
        else:
            face_quality = float(
                min(1.0, (y1 - y0) * (x1 - x0) / (rgb.shape[0] * rgb.shape[1]) * 25.0)
            )
            crop = rgb[y0:y1, x0:x1].astype(np.float64)
            clipped = float(((crop >= 254.0) | (crop <= 1.0)).mean())
            face_quality *= max(0.0, 1.0 - 4.0 * clipped)
            if face_quality < 0.05:
                face_quality = 0.0
                face_reason = "highlights or shadows clipped over too much of the face"

    def _border(image: np.ndarray) -> np.ndarray:
        return np.median(
            np.concatenate(
                [
                    image[:3].reshape(-1, 3),
                    image[-3:].reshape(-1, 3),
                    image[:, :3].reshape(-1, 3),
                    image[:, -3:].reshape(-1, 3),
                ]
            ),
            axis=0,
        )

    background = _border(balanced)
    scene_background = _border(rgb.astype(np.float64))
    if face_quality > 0.0:
        face = _face_measurements(balanced, (y0, y1, x0, x1), mask)
        hair = _hair_measurements(balanced, (y0, y1, x0, x1), background)
    else:
        face = {
            "appearance": np.zeros(32),
            "gradient": np.zeros(128),
            "geometry": np.zeros(7),
            "skin": np.zeros(4),
        }
        hair = {"colour": np.zeros(2), "shape": np.zeros(3)}
    body, body_quality, framing = _body_measurements(balanced, background, (y0, y1, x0, x1))
    wardrobe = _wardrobe_measurements(balanced, background, (y0, y1, x0, x1))
    scene = {
        "background": scene_background / 255.0,
        "intensity": np.array([float(gray_full.mean())], dtype=np.float64),
    }
    return Observation(
        face=face,
        hair=hair,
        body=body,
        wardrobe=wardrobe,
        scene=scene,
        face_quality=face_quality,
        face_reason=face_reason,
        body_quality=body_quality,
        framing=framing,
    )


# ---------------------------------------------------------------------------
# Group-weighted distances
# ---------------------------------------------------------------------------

#: Per-group weights, fixed by design (not tuned against the benchmark):
#: geometry is the most identity-specific and lighting-robust cue, so it is
#: weighted double; absolute skin tone is the least reliable under a coloured key
#: light, so it is weighted half. Groups are averaged, so a 128-dimension gradient
#: group cannot outvote a 9-dimension geometry group.
GROUP_WEIGHTS: dict[str, dict[str, float]] = {
    "face": {"appearance": 1.0, "gradient": 1.0, "geometry": 2.0, "skin": 0.3},
    "hair": {"colour": 1.0, "shape": 2.0},
    "body": {"silhouette": 1.0, "profile": 1.0},
    "wardrobe": {"colour": 1.0, "bands": 1.0, "pattern": 0.5},
    "scene": {"background": 1.0, "intensity": 0.25},
}

#: Minimal relative spread used when a group is constant across the gallery.
_STD_FLOOR_FRACTION = 0.05


def feature_groups(observation: Observation, dimension: str) -> dict[str, np.ndarray]:
    return dict(getattr(observation, dimension))


def group_stats(
    observations: Sequence[Observation], dimension: str
) -> dict[str, dict[str, list[float]]]:
    """Per-group mean/std over a gallery (std floored relative to the mean scale)."""
    groups = feature_groups(observations[0], dimension)
    out: dict[str, dict[str, list[float]]] = {}
    for name in groups:
        matrix = np.vstack(
            [np.ravel(feature_groups(item, dimension)[name]) for item in observations]
        )
        mean = matrix.mean(axis=0)
        std = matrix.std(axis=0)
        floor = _STD_FLOOR_FRACTION * (np.abs(mean) + 0.05)
        std = np.maximum(std, floor)
        out[name] = {
            "mean": [round(float(value), 8) for value in mean],
            "std": [round(float(value), 8) for value in std],
        }
    return out


def distance(
    reference: Observation,
    candidate: Observation,
    dimension: str,
    *,
    stats: Mapping[str, Mapping[str, list[float]]] | None = None,
) -> float:
    """Weighted mean of per-group z-scored RMS distances.

    With ``stats`` the features are standardised per group; without it the raw
    RMS is used (only meaningful for debugging).
    """
    reference_groups = feature_groups(reference, dimension)
    candidate_groups = feature_groups(candidate, dimension)
    weights = GROUP_WEIGHTS.get(dimension, {name: 1.0 for name in reference_groups})
    total_weight = 0.0
    accumulated = 0.0
    for name in sorted(reference_groups):
        a = np.ravel(reference_groups[name]).astype(np.float64)
        b = np.ravel(candidate_groups[name]).astype(np.float64)
        if a.shape != b.shape:
            raise ValueError(
                f"feature shape mismatch in {dimension}.{name}: {a.shape} vs {b.shape}"
            )
        if stats is not None:
            entry = stats.get(name)
            if entry is None:
                raise ValueError(f"calibration stats missing for {dimension}.{name}")
            mean = np.asarray(entry["mean"], dtype=np.float64)
            std = np.asarray(entry["std"], dtype=np.float64)
            if mean.shape != a.shape or std.shape != a.shape:
                raise ValueError(f"calibration stats shape mismatch for {dimension}.{name}")
            a = (a - mean) / np.where(std < 1e-9, 1.0, std)
            b = (b - mean) / np.where(std < 1e-9, 1.0, std)
        diff = a - b
        group_distance = float(np.sqrt(float(np.mean(diff * diff))))
        weight = float(weights.get(name, 1.0))
        accumulated += weight * group_distance
        total_weight += weight
    return accumulated / total_weight if total_weight else 0.0


def score_from_distance(value: float) -> float:
    """Map a non-negative distance to a 0..1 similarity (monotone, documented)."""
    return 1.0 / (1.0 + max(value, 0.0))
