"""Deterministic local "DNA seed" hydration — bake pack files on the device.

A ``local_dna_seed`` entry ships a few-byte *recipe* instead of a blob.  The
device bakes the recipe into the standard binary/text file and checks the
result against the SHA-256 published in the manifest.  For that check to be
meaningful the output must be **bit-identical on every machine in the world**,
so this module obeys one rule:

    No floating point.  Every computation is exact integer arithmetic
    (Python ints, Q32 fixed point), every transcendental is a fixed-length
    integer series, and every constant (π, ln 2) is derived at import time
    from an integer series — never from ``math`` (libm results differ across
    platforms) and never from ``float`` parsing.

No compression is applied either: zlib output is not guaranteed stable across
zlib/zlib-ng builds, which would make the published hash device-dependent.

Recipes (``{"recipe": <name>, "version": 1, "params": {...}}``):

* ``lut3d_cube`` — a standard Adobe/Resolve ``.cube`` 3D LUT: mired-shift
  white balance, OKLab chroma/hue grading (Ottosson's matrices) and an
  ImageMagick-style sigmoidal contrast curve.
* ``skin_pore_normal_atlas`` — a seamless tangent-space normal atlas of
  micro skin pores (jittered cellular dimples) over a wrinkle base, packed as
  two-channel RG8 normals (BC5-style, Z is reconstructed) plus an 8-bit
  wrinkle-base plane.
* ``vocal_formant_table`` — Persian/English vowel formant table (F1–F3 and
  bandwidths, vocal-tract scaled) followed by quantised symmetric Hann windows.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Callable, Mapping
from math import isqrt
from pathlib import Path

from .content_store import LocalContentStore
from .errors import IntegrityError, SeedRecipeError
from .pack_manifest import ResourceBlobEntry

__all__ = [
    "ATLAS_MAGIC",
    "FORMANT_MAGIC",
    "RECIPE_NAMES",
    "bake_lut3d_cube",
    "bake_skin_pore_normal_atlas",
    "bake_vocal_formant_table",
    "describe_seed_entry",
    "hydrate_entry_to_store",
    "hydrate_seed",
]

# --------------------------------------------------------------------------
# exact fixed-point core (Q32)
# --------------------------------------------------------------------------

_FRAC = 32
_ONE = 1 << _FRAC
_HALF = 1 << (_FRAC - 1)
_GUARD = 16  # extra bits carried through series evaluation
_HI = 128  # precision of derived constants before rounding to Q32


def _rshift_round(value: int, bits: int) -> int:
    return (value + (1 << (bits - 1))) >> bits


def _rdiv(numerator: int, denominator: int) -> int:
    """Round-half-up integer division for a positive denominator."""
    return (2 * numerator + denominator) // (2 * denominator)


def _ln2_hi() -> int:
    """ln 2 = Σ_{k≥1} 1 / (k·2^k), at 2^-_HI resolution."""
    total, k = 0, 1
    while term := (1 << _HI) // (k << k):
        total += term
        k += 1
    return total


def _arctan_inv_hi(x: int) -> int:
    """arctan(1/x) by its alternating series, at 2^-_HI resolution."""
    total, n, power, x2 = 0, 0, (1 << _HI) // x, x * x
    while power:
        term = power // (2 * n + 1)
        total += -term if n % 2 else term
        power //= x2
        n += 1
    return total


_LN2 = _rshift_round(_ln2_hi(), _HI - _FRAC)
_PI = _rshift_round(16 * _arctan_inv_hi(5) - 4 * _arctan_inv_hi(239), _HI - _FRAC)  # Machin
_TWO_PI = 2 * _PI


def _fmul(a: int, b: int) -> int:
    return _rshift_round(a * b, _FRAC)


def _fdiv(a: int, b: int) -> int:
    return _rdiv(a << _FRAC, b)


def _clamp(value: int, lo: int = 0, hi: int = _ONE) -> int:
    return lo if value < lo else hi if value > hi else value


def _exp_fx(x: int) -> int:
    """e^x for Q32 *x*: ln2 range reduction + a fixed 24-term Taylor series."""
    x = _clamp(x, -64 * _ONE, 64 * _ONE)
    k = (x + _LN2 // 2) // _LN2
    r = (x - k * _LN2) << _GUARD  # |r| <= ln2/2
    one = _ONE << _GUARD
    term = total = one
    for n in range(1, 25):
        term = term * r // (one * n)
        total += term
    shift = k - _GUARD
    return total << shift if shift >= 0 else _rshift_round(total, -shift)


def _sincos_fx(angle: int) -> tuple[int, int]:
    """(sin, cos) of a Q32 angle in radians, reduced to [-π, π], 30-term series."""
    a = angle % _TWO_PI
    if a > _PI:
        a -= _TWO_PI
    one = _ONE << _GUARD
    x = a << _GUARD
    x2 = x * x // one
    sin_term = sin_total = x
    cos_term = cos_total = one
    for n in range(1, 30):
        sin_term = -sin_term * x2 // (one * (2 * n) * (2 * n + 1))
        cos_term = -cos_term * x2 // (one * (2 * n - 1) * (2 * n))
        sin_total += sin_term
        cos_total += cos_term
    return _rshift_round(sin_total, _GUARD), _rshift_round(cos_total, _GUARD)


def _icbrt(n: int) -> int:
    """⌊∛n⌋ for n ≥ 0 by integer Newton iteration."""
    if n <= 0:
        return 0
    x = 1 << ((n.bit_length() + 2) // 3)
    while True:
        y = (2 * x + n // (x * x)) // 3
        if y >= x:
            break
        x = y
    return x  # Newton from above stops exactly at the floor (proved by the tests)


def _cbrt_fx(value: int) -> int:
    return _icbrt(max(value, 0) << (2 * _FRAC))


def _sqrt_fx(value: int) -> int:
    return isqrt(max(value, 0) << _FRAC)


def _format_unit(value: int) -> str:
    micro = _rshift_round(_clamp(value) * 1_000_000, _FRAC)
    return f"{micro // 1_000_000}.{micro % 1_000_000:06d}"


def _q(decimal_e10: int) -> int:
    """Convert a coefficient given in units of 1e-10 to Q32, exactly rounded."""
    return _rdiv(decimal_e10 * _ONE, 10**10)


def _matrix(rows: tuple[tuple[int, int, int], ...]) -> tuple[tuple[int, int, int], ...]:
    return tuple((_q(a), _q(b), _q(c)) for a, b, c in rows)


def _apply(
    matrix: tuple[tuple[int, int, int], ...], v: tuple[int, int, int]
) -> tuple[int, int, int]:
    (a0, a1, a2), (b0, b1, b2), (c0, c1, c2) = matrix
    x, y, z = v
    return (
        _rshift_round(a0 * x + a1 * y + a2 * z, _FRAC),
        _rshift_round(b0 * x + b1 * y + b2 * z, _FRAC),
        _rshift_round(c0 * x + c1 * y + c2 * z, _FRAC),
    )


# Björn Ottosson, "A perceptual color space for image processing" (2020).
_RGB_TO_LMS = _matrix(
    (
        (4122214708, 5363325363, 514459929),
        (2119034982, 6806995451, 1073969566),
        (883024619, 2817188376, 6299787005),
    )
)
_LMS_TO_LAB = _matrix(
    (
        (2104542553, 7936177850, -40720468),
        (19779984951, -24285922050, 4505937099),
        (259040371, 7827717662, -8086757660),
    )
)
_LAB_TO_LMS = _matrix(
    (
        (10000000000, 3963377774, 2158037573),
        (10000000000, -1055613458, -638541728),
        (10000000000, -894841775, -12914855480),
    )
)
_LMS_TO_RGB = _matrix(
    (
        (40767416621, -33077115913, 2309699292),
        (-12684380046, 26097574011, -3413193965),
        (-41960863, -7034186147, 17076147010),
    )
)

# --------------------------------------------------------------------------
# recipe parsing
# --------------------------------------------------------------------------

RECIPE_NAMES: tuple[str, ...] = ("lut3d_cube", "skin_pore_normal_atlas", "vocal_formant_table")
_RECIPE_KEYS = frozenset({"recipe", "version", "params"})
_RECIPE_VERSION = 1


class _Params:
    """Strict, typed accessor that also rejects unknown parameter names."""

    def __init__(self, recipe: str, raw: Mapping[str, object], allowed: frozenset[str]) -> None:
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise SeedRecipeError(f"{recipe}: unknown parameter(s) {unknown}")
        self._recipe = recipe
        self._raw = raw

    def integer(self, name: str, default: int, lo: int, hi: int) -> int:
        value = self._raw.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool):
            raise SeedRecipeError(f"{self._recipe}.{name} must be an integer")
        if not lo <= value <= hi:
            raise SeedRecipeError(f"{self._recipe}.{name}={value} outside [{lo}, {hi}]")
        return value

    def sequence(self, name: str, default: tuple[object, ...]) -> tuple[object, ...]:
        value = self._raw.get(name, default)
        if not isinstance(value, (list, tuple)) or not value:
            raise SeedRecipeError(f"{self._recipe}.{name} must be a non-empty list")
        return tuple(value)

    def text(self, name: str, default: str) -> str:
        value = self._raw.get(name, default)
        if (
            not isinstance(value, str)
            or not 0 < len(value) <= 64
            or not all(" " <= ch <= "~" and ch != '"' for ch in value)
        ):
            raise SeedRecipeError(f"{self._recipe}.{name} must be 1-64 printable ASCII chars")
        return value


def _parse_recipe(recipe: object) -> tuple[str, Mapping[str, object]]:
    if not isinstance(recipe, Mapping) or not set(recipe) <= _RECIPE_KEYS:
        raise SeedRecipeError("seed recipe must be a mapping with recipe/version/params")
    name = recipe.get("recipe")
    if name not in RECIPE_NAMES:
        raise SeedRecipeError(f"unknown seed recipe {name!r}")
    version = recipe.get("version")
    if version != _RECIPE_VERSION or isinstance(version, bool):
        raise SeedRecipeError(f"{name}: unsupported recipe version {version!r}")
    params = recipe.get("params", {})
    if not isinstance(params, Mapping):
        raise SeedRecipeError(f"{name}: params must be a mapping")
    return str(name), params


# --------------------------------------------------------------------------
# recipe 1: lut3d_cube
# --------------------------------------------------------------------------

_LUT_PARAMS = frozenset(
    {
        "title",
        "size",
        "temperature_k",
        "contrast_milli",
        "midpoint_milli",
        "chroma_milli",
        "hue_shift_mdeg",
    }
)


def bake_lut3d_cube(params: Mapping[str, object]) -> bytes:
    """Bake a ``.cube`` 3D LUT (red varies fastest, as the format requires)."""
    p = _Params("lut3d_cube", params, _LUT_PARAMS)
    title = p.text("title", "NEXUS Cinematic")
    size = p.integer("size", 17, 2, 65)
    temperature = p.integer("temperature_k", 6500, 1500, 15000)
    contrast = p.integer("contrast_milli", 0, 0, 20000)
    midpoint = p.integer("midpoint_milli", 500, 1, 999)
    chroma = p.integer("chroma_milli", 1000, 0, 4000)
    hue_mdeg = p.integer("hue_shift_mdeg", 0, -180000, 180000)

    # white balance: linear gains in mired space around D65 (6500 K)
    mired = _rdiv(10**6 * _ONE, temperature) - _rdiv(10**6 * _ONE, 6500)
    gains = (
        max(0, _ONE + mired * 12 // 10_000),
        _ONE,
        max(0, _ONE - mired * 15 // 10_000),
    )
    chroma_q = _rdiv(chroma * _ONE, 1000)
    hue_sin, hue_cos = _sincos_fx(_rdiv(hue_mdeg * _PI, 180_000))
    rot_sin, rot_cos = _fmul(hue_sin, chroma_q), _fmul(hue_cos, chroma_q)

    beta, alpha = _rdiv(contrast * _ONE, 1000), _rdiv(midpoint * _ONE, 1000)

    def logistic(x: int) -> int:
        return _fdiv(_ONE, _ONE + _exp_fx(_fmul(beta, alpha - x)))

    sig0, sig1 = (logistic(0), logistic(_ONE)) if contrast else (0, _ONE)

    def grade(r: int, g: int, b: int) -> tuple[int, int, int]:
        lin = tuple(
            _clamp(_fmul(_fmul(c, c), gain)) for c, gain in zip((r, g, b), gains, strict=True)
        )
        lms = _apply(_RGB_TO_LMS, (lin[0], lin[1], lin[2]))
        lab = _apply(_LMS_TO_LAB, (_cbrt_fx(lms[0]), _cbrt_fx(lms[1]), _cbrt_fx(lms[2])))
        big_l, a, bb = lab
        a, bb = _fmul(a, rot_cos) - _fmul(bb, rot_sin), _fmul(a, rot_sin) + _fmul(bb, rot_cos)
        l_, m_, s_ = _apply(_LAB_TO_LMS, (big_l, a, bb))
        cubes = tuple(_fmul(_fmul(v, v), v) for v in (l_, m_, s_))
        out = _apply(_LMS_TO_RGB, (cubes[0], cubes[1], cubes[2]))
        encoded = [_sqrt_fx(_clamp(v)) for v in out]
        if contrast:
            encoded = [_fdiv(logistic(v) - sig0, sig1 - sig0) for v in encoded]
        return encoded[0], encoded[1], encoded[2]

    axis = [_rdiv(i * _ONE, size - 1) for i in range(size)]
    lines = [
        "# NEXUS local DNA seed: lut3d_cube v1 (integer-exact)",
        f'TITLE "{title}"',
        f"LUT_3D_SIZE {size}",
        "DOMAIN_MIN 0.000000 0.000000 0.000000",
        "DOMAIN_MAX 1.000000 1.000000 1.000000",
    ]
    for b in axis:
        for g in axis:
            for r in axis:
                lines.append(" ".join(_format_unit(c) for c in grade(r, g, b)))
    return ("\n".join(lines) + "\n").encode("ascii")


# --------------------------------------------------------------------------
# recipe 2: skin_pore_normal_atlas
# --------------------------------------------------------------------------

ATLAS_MAGIC = b"NXSPNA01"
_ATLAS_PARAMS = frozenset(
    {
        "width",
        "height",
        "seed",
        "pore_cell_px",
        "pore_radius_milli",
        "pore_depth_milli",
        "wrinkle_cycles_x",
        "wrinkle_cycles_y",
        "wrinkle_depth_milli",
        "roughness_milli",
        "normal_strength_milli",
    }
)
_MASK64 = (1 << 64) - 1
_H_ONE = 1 << 16  # height-field unit


def _splitmix64(value: int) -> int:
    z = (value + 0x9E3779B97F4A7C15) & _MASK64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
    return z ^ (z >> 31)


def _encode_unit_byte(component: int, length: int) -> int:
    return ((component + length) * 255 + length) // (2 * length)


def bake_skin_pore_normal_atlas(params: Mapping[str, object]) -> bytes:
    """Bake a seamless micro-pore normal atlas (RG8 normals + wrinkle plane)."""
    p = _Params("skin_pore_normal_atlas", params, _ATLAS_PARAMS)
    cell = p.integer("pore_cell_px", 8, 4, 64)
    width = p.integer("width", 64, 8, 512)
    height = p.integer("height", 64, 8, 512)
    if width % cell or height % cell:
        raise SeedRecipeError("skin_pore_normal_atlas: width/height must be multiples of cell")
    seed = p.integer("seed", 1, 0, (1 << 63) - 1)
    radius_milli = p.integer("pore_radius_milli", 450, 100, 1000)
    depth_milli = p.integer("pore_depth_milli", 600, 0, 1000)
    cycles_x = p.integer("wrinkle_cycles_x", 3, 0, 64)
    cycles_y = p.integer("wrinkle_cycles_y", 1, 0, 64)
    wrinkle_milli = p.integer("wrinkle_depth_milli", 350, 0, 1000)
    rough_milli = p.integer("roughness_milli", 120, 0, 1000)
    strength_milli = p.integer("normal_strength_milli", 2000, 100, 8000)

    sub = 1024  # sub-pixel resolution of feature points
    grid_w, grid_h = width // cell, height // cell
    span = cell * sub
    base_radius = span * radius_milli // 1000
    base_depth = _H_ONE * depth_milli // 1000

    features: dict[tuple[int, int], tuple[int, int, int, int]] = {}
    for gy in range(grid_h):
        for gx in range(grid_w):
            h = _splitmix64(seed ^ _splitmix64((gy << 20) | gx))
            jx = (h & 0xFFFF) * span >> 16
            jy = ((h >> 16) & 0xFFFF) * span >> 16
            radius = base_radius * (768 + ((h >> 32) & 0x1FF)) // 1024
            depth = base_depth * (512 + ((h >> 41) & 0x1FF)) // 1024
            features[(gx, gy)] = (jx, jy, radius, depth)

    # wrinkle base: sin(2π(x·cx/W + y·cy/H)) via the angle-addition identity
    wrinkle_amp = _H_ONE * wrinkle_milli // 1000
    col = [_sincos_fx(_TWO_PI * x * cycles_x // width) for x in range(width)]
    row = [_sincos_fx(_TWO_PI * y * cycles_y // height) for y in range(height)]

    heights = [[0] * width for _ in range(height)]
    wrinkle_plane = bytearray(width * height)
    rough_amp = _H_ONE * rough_milli // 1000
    for y in range(height):
        sin_y, cos_y = row[y]
        gy = y // cell
        py = y * sub + sub // 2
        for x in range(width):
            sin_x, cos_x = col[x]
            wave = _rshift_round(sin_x * cos_y + cos_x * sin_y, _FRAC)  # Q32 sin
            wrinkle = wrinkle_amp * wave >> _FRAC
            gx = x // cell
            px = x * sub + sub // 2
            pore = 0
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    cx, cy = gx + dx, gy + dy
                    jx, jy, radius, depth = features[(cx % grid_w, cy % grid_h)]
                    fx, fy = cx * span + jx, cy * span + jy
                    d2 = (px - fx) ** 2 + (py - fy) ** 2
                    r2 = radius * radius
                    if d2 < r2:
                        t = _H_ONE - d2 * _H_ONE // r2
                        pore -= depth * t * t >> 32
            noise = (_splitmix64(seed ^ 0x5EED ^ ((y << 20) | x)) & 0xFFFF) - 0x8000
            heights[y][x] = wrinkle + pore + (rough_amp * noise >> 16)
            if wrinkle_amp:
                wrinkle_plane[y * width + x] = _encode_unit_byte(wrinkle, wrinkle_amp)
            else:
                wrinkle_plane[y * width + x] = 128

    normals = bytearray(width * height * 2)
    nz = 2 * _H_ONE
    for y in range(height):
        up, down = heights[(y - 1) % height], heights[(y + 1) % height]
        line = heights[y]
        for x in range(width):
            nx = -(line[(x + 1) % width] - line[(x - 1) % width]) * strength_milli // 1000
            ny = -(down[x] - up[x]) * strength_milli // 1000
            length = isqrt(nx * nx + ny * ny + nz * nz)
            offset = 2 * (y * width + x)
            normals[offset] = _encode_unit_byte(nx, length)
            normals[offset + 1] = _encode_unit_byte(ny, length)

    header = struct.pack(">8sHHHHQ", ATLAS_MAGIC, 1, width, height, 2, seed)
    return header + bytes(normals) + bytes(wrinkle_plane)


# --------------------------------------------------------------------------
# recipe 3: vocal_formant_table
# --------------------------------------------------------------------------

FORMANT_MAGIC = b"NXVFT001"
_FORMANT_PARAMS = frozenset({"languages", "voice_scale_milli", "window_sizes"})
_LANG_CODES = {"fa": 1, "en": 2}

#: (language, phoneme id, F1, F2, F3) — adult male reference values in Hz.
#: Persian: Tehrani vowel system; English: Peterson & Barney (1952) means.
_VOWELS: tuple[tuple[str, str, int, int, int], ...] = (
    ("fa", "a", 700, 1650, 2550),
    ("fa", "A", 650, 1050, 2500),
    ("fa", "e", 480, 1900, 2550),
    ("fa", "i", 290, 2250, 2900),
    ("fa", "o", 450, 900, 2450),
    ("fa", "u", 320, 800, 2350),
    ("en", "iy", 270, 2290, 3010),
    ("en", "ih", 390, 1990, 2550),
    ("en", "eh", 530, 1840, 2480),
    ("en", "ae", 660, 1720, 2410),
    ("en", "ah", 520, 1190, 2390),
    ("en", "aa", 730, 1090, 2440),
    ("en", "ao", 570, 840, 2410),
    ("en", "uh", 440, 1020, 2240),
    ("en", "uw", 300, 870, 2240),
    ("en", "er", 490, 1350, 1690),
)


def _hann_window(size: int) -> bytes:
    """Symmetric Hann window quantised to u16 (0..65535)."""
    samples = []
    for n in range(size):
        _, cos = _sincos_fx(_TWO_PI * n // (size - 1))
        weight = (_ONE - cos) // 2
        samples.append(_rshift_round(_clamp(weight) * 65535, _FRAC))
    return struct.pack(f">{size}H", *samples)


def bake_vocal_formant_table(params: Mapping[str, object]) -> bytes:
    p = _Params("vocal_formant_table", params, _FORMANT_PARAMS)
    languages = p.sequence("languages", ("fa", "en"))
    if any(lang not in _LANG_CODES for lang in languages) or len(set(languages)) != len(languages):
        raise SeedRecipeError("vocal_formant_table.languages must be unique values of fa/en")
    scale = p.integer("voice_scale_milli", 1000, 700, 1500)
    raw_windows = p.sequence("window_sizes", (256, 512, 1024))
    sizes = [w for w in raw_windows if isinstance(w, int) and not isinstance(w, bool)]
    if (
        len(sizes) != len(raw_windows)
        or len(sizes) > 8
        or sizes != sorted(set(sizes))
        or not all(16 <= w <= 8192 for w in sizes)
    ):
        raise SeedRecipeError(
            "vocal_formant_table.window_sizes must be <= 8 strictly ascending ints in [16, 8192]"
        )
    mask = sum(_LANG_CODES[str(lang)] for lang in languages)
    records = []
    for lang, phoneme, f1, f2, f3 in _VOWELS:
        if lang not in languages:
            continue
        s1, s2, s3 = (_rdiv(f * scale, 1000) for f in (f1, f2, f3))
        records.append(
            struct.pack(
                ">4sBHHHHHH",
                phoneme.encode("ascii"),
                _LANG_CODES[lang],
                s1,
                s2,
                s3,
                50 + s1 // 10,
                70 + s2 // 20,
                110 + s3 // 25,
            )
        )
    body = b"".join(records)
    body += b"".join(struct.pack(">I", size) + _hann_window(size) for size in sizes)
    header = struct.pack(">8sHBBH", FORMANT_MAGIC, 1, mask, len(records), len(sizes))
    return header + body


# --------------------------------------------------------------------------
# public entry points
# --------------------------------------------------------------------------

_BAKERS: dict[str, Callable[[Mapping[str, object]], bytes]] = {
    "lut3d_cube": bake_lut3d_cube,
    "skin_pore_normal_atlas": bake_skin_pore_normal_atlas,
    "vocal_formant_table": bake_vocal_formant_table,
}


def hydrate_seed(recipe: Mapping[str, object]) -> bytes:
    """Bake *recipe* into its exact, device-independent bytes."""
    name, params = _parse_recipe(recipe)
    return _BAKERS[name](params)


def describe_seed_entry(relative_path: str, recipe: Mapping[str, object]) -> ResourceBlobEntry:
    """Publisher helper: bake once and record the hash/size the device must reproduce."""
    data = hydrate_seed(recipe)
    return ResourceBlobEntry(
        relative_path=relative_path,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        acquisition_mode="local_dna_seed",
        seed_recipe=recipe,
    )


def hydrate_entry_to_store(entry: ResourceBlobEntry, store: LocalContentStore) -> Path:
    """Bake a ``local_dna_seed`` entry on this device and adopt it into the CAS.

    The SHA-256 is checked before anything becomes addressable; a mismatch
    (tampered recipe, incompatible baker) raises :class:`IntegrityError`.
    """
    if entry.acquisition_mode != "local_dna_seed" or entry.seed_recipe is None:
        raise SeedRecipeError(f"{entry.relative_path} is not a local_dna_seed entry")
    data = hydrate_seed(entry.seed_recipe)
    digest = hashlib.sha256(data).hexdigest()
    if digest != entry.sha256 or len(data) != entry.size_bytes:
        raise IntegrityError(
            f"{entry.relative_path}: hydrated bytes sha256={digest} size={len(data)} do not "
            f"match the manifest (sha256={entry.sha256} size={entry.size_bytes})"
        )
    return store.put_bytes(data, entry.sha256)
