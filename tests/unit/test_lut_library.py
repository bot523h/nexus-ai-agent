"""The LUT library resolves real ``.cube`` files and refuses fake ones.

Session 3: ``color.apply_lut`` executes through ``lut3d`` (no ``colorchannelmixer``
approximation), so the LUT bytes must be validated strictly — a malformed cube
that FFmpeg would reject (or misread) must fail at resolve time with the file
named, never at encode time and never silently.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nexus_ai_agent.creative.luts import (
    BUILTIN_LUTS,
    LutNotFoundError,
    LutValidationError,
    library_path,
    list_luts,
    lut_sha256,
    resolve_lut,
    validate_cube_file,
)


def test_builtin_names_are_stable() -> None:
    assert BUILTIN_LUTS == ("identity", "warm")
    assert list_luts() == ("identity", "warm")


def test_builtin_cubes_exist_and_validate() -> None:
    for name in BUILTIN_LUTS:
        path = library_path(name)
        assert path.is_file(), f"shipped LUT missing: {path}"
        assert validate_cube_file(path) == 16  # 16^3 lattice, strict parse


def test_identity_lut_is_the_identity_map() -> None:
    rows = library_path("identity").read_text().splitlines()
    data = [
        line for line in rows if line and not line.startswith(("#", "TITLE", "LUT_3D", "DOMAIN"))
    ]
    assert len(data) == 16**3
    size = 16
    for index in (0, 1, 100, 1000, 16**3 - 1):
        red, green, blue = (float(v) for v in data[index].split())
        # FAST_RED ordering: index = r + N*g + N^2*b
        expect_r = (index % size) / (size - 1)
        expect_g = ((index // size) % size) / (size - 1)
        expect_b = (index // (size * size)) / (size - 1)
        # 6-decimal lattice values: equality within quantization (1/15 ≈ 0.066667).
        assert (red, green, blue) == pytest.approx((expect_r, expect_g, expect_b), abs=2e-6)


def test_warm_lut_is_not_identity_but_stays_in_gamut() -> None:
    rows = library_path("warm").read_text().splitlines()
    data = [
        line for line in rows if line and not line.startswith(("#", "TITLE", "LUT_3D", "DOMAIN"))
    ]
    assert len(data) == 16**3
    differs = 0
    for line in data:
        red, green, blue = (float(v) for v in line.split())
        assert 0.0 <= red <= 1.0 and 0.0 <= green <= 1.0 and 0.0 <= blue <= 1.0
        if abs(red - blue) > 1e-9 or abs(green - blue) > 1e-9:
            differs += 1
    assert differs > 0  # a "warm" LUT identical to identity would be a lie


def test_lut_sha256_measures_file_bytes() -> None:
    path = library_path("warm")
    expected = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert lut_sha256(path) == expected


def test_resolve_unknown_name_fails_closed() -> None:
    with pytest.raises(LutNotFoundError, match="noir"):
        resolve_lut("noir")


def test_resolve_rejects_relative_paths() -> None:
    with pytest.raises(LutNotFoundError, match="absolute"):
        resolve_lut("../identity")


def test_resolve_accepts_absolute_staged_cubes(tmp_path: Path) -> None:
    staged = tmp_path / "op.cube"
    staged.write_bytes(library_path("warm").read_bytes())
    assert resolve_lut(str(staged)) == staged


def test_resolve_rejects_missing_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(LutNotFoundError, match="ghost.cube"):
        resolve_lut(str(tmp_path / "ghost.cube"))


@pytest.mark.parametrize(
    "body",
    [
        'TITLE "x"\nLUT_3D_SIZE 2\n0 0 0\n',  # short: 1 row, needs 8
        'TITLE "x"\nLUT_3D_SIZE 2\n' + "0 0 0\n" * 9,  # long: 9 rows, needs 8
        'TITLE "x"\nLUT_3D_SIZE 2\n' + "0 0 0\n" * 7 + "1.5 0 0\n",  # out of range
        'TITLE "x"\nLUT_3D_SIZE 2\n' + "0 0 0\n" * 7 + "0 0\n",  # short row
        'TITLE "x"\n',  # no size header
        'TITLE "x"\nLUT_3D_SIZE 0\n',  # degenerate size
    ],
)
def test_malformed_cubes_are_refused(tmp_path: Path, body: str) -> None:
    cube = tmp_path / "bad.cube"
    cube.write_text(body)
    with pytest.raises(LutValidationError):
        validate_cube_file(cube)


def test_missing_file_is_refused_with_path(tmp_path: Path) -> None:
    missing = tmp_path / "ghost.cube"
    with pytest.raises(LutValidationError, match="ghost.cube"):
        validate_cube_file(missing)
