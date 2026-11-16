"""Deterministic local DNA seed hydration (pre-bake on the user's device)."""

from __future__ import annotations

import hashlib
import os
import struct
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
from resource_pack_testkit import ATLAS_RECIPE, FORMANT_RECIPE, LUT_RECIPE, core_seed_entries

from nexus_ai_agent.storage.resource_packs import (
    RECIPE_NAMES,
    IntegrityError,
    LocalContentStore,
    ResourceBlobEntry,
    SeedRecipeError,
    bake_lut3d_cube,
    bake_skin_pore_normal_atlas,
    bake_vocal_formant_table,
    build_manifest,
    describe_seed_entry,
    hydrate_entry_to_store,
    hydrate_seed,
    verify_installed_pack,
)
from nexus_ai_agent.storage.resource_packs import seed_hydrator as sh

#: Pinned digests.  The bakers use integer arithmetic only, so these must hold
#: on every OS, CPU and CPython version of the CI matrix — a change here is a
#: breaking change of every published core manifest.
GOLDEN = {
    "lut3d_cube": (19853, "bd579aac6230602a833c6e80795ecca088d0aa97d08fa4eb4db45f103cd1d72d"),
    "skin_pore_normal_atlas": (
        3096,
        "99c28c2172cdc4bc88172d77802d386a78c0ecbcc4db9fd318cadf4a0277eba7",
    ),
    "vocal_formant_table": (
        678,
        "d30223cc150efca2a72ba4b3b909ddaaae8262d248c4752a8e76c4361ddc481d",
    ),
}
RECIPES: dict[str, Mapping[str, object]] = {
    "lut3d_cube": LUT_RECIPE,
    "skin_pore_normal_atlas": ATLAS_RECIPE,
    "vocal_formant_table": FORMANT_RECIPE,
}


@pytest.mark.parametrize("name", RECIPE_NAMES)
def test_hydration_matches_pinned_golden_digest(name: str) -> None:
    data = hydrate_seed(RECIPES[name])
    assert (len(data), hashlib.sha256(data).hexdigest()) == GOLDEN[name]
    assert hydrate_seed(RECIPES[name]) == data  # repeatable in-process


def test_hydration_is_identical_in_a_fresh_interpreter() -> None:
    """Another process, another hash seed: byte-identical output."""
    code = (
        "import hashlib,json,sys;"
        "from nexus_ai_agent.storage.resource_packs import hydrate_seed;"
        "r=json.loads(sys.argv[1]);print(hashlib.sha256(hydrate_seed(r)).hexdigest())"
    )
    import json

    env = {**os.environ, "PYTHONHASHSEED": "12345"}
    src = str(Path(sh.__file__).resolve().parents[3])
    env["PYTHONPATH"] = os.pathsep.join([src, env.get("PYTHONPATH", "")])
    out = subprocess.run(
        [sys.executable, "-c", code, json.dumps(LUT_RECIPE)],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    assert out.stdout.strip() == GOLDEN["lut3d_cube"][1]


def test_core_pack_hydrates_on_disk_to_the_manifest_digests(tmp_path: Path) -> None:
    entries = core_seed_entries()
    manifest = build_manifest(
        pack_id="core", title_fa="هسته", tier="core", version="1.0.0", entries=entries
    )
    assert manifest.total_network_download_bytes == 0  # zero server bandwidth
    store = LocalContentStore(tmp_path / "cas")
    install = tmp_path / "install"
    for entry in manifest.entries:
        path = hydrate_entry_to_store(entry, store)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry.sha256
        store.materialize(entry.sha256, install / entry.relative_path, prefer_hardlink=False)
    report = verify_installed_pack(manifest, install)
    assert report.verified_files == 3
    assert set(store.index()) == {e.sha256 for e in entries}


def test_tampered_recipe_is_rejected_before_entering_the_cas(tmp_path: Path) -> None:
    honest = describe_seed_entry("luts/a.cube", LUT_RECIPE)
    params = dict(LUT_RECIPE["params"])  # type: ignore[call-overload]
    params["contrast_milli"] = 3501
    forged = ResourceBlobEntry(
        honest.relative_path,
        honest.sha256,
        honest.size_bytes,
        "local_dna_seed",
        {**LUT_RECIPE, "params": params},
    )
    store = LocalContentStore(tmp_path / "cas")
    with pytest.raises(IntegrityError, match="do not match the manifest"):
        hydrate_entry_to_store(forged, store)
    assert store.index() == {}


def test_hydrating_a_remote_entry_is_refused(tmp_path: Path) -> None:
    remote = ResourceBlobEntry("x.bin", "a" * 64, 1, "remote_blob")
    with pytest.raises(SeedRecipeError, match="not a local_dna_seed"):
        hydrate_entry_to_store(remote, LocalContentStore(tmp_path))


def test_lut_is_a_well_formed_cube_file() -> None:
    text = bake_lut3d_cube({"size": 3, "title": "Neutral"}).decode("ascii")
    lines = text.splitlines()
    assert 'TITLE "Neutral"' in lines and "LUT_3D_SIZE 3" in lines
    rows = [line for line in lines if line[:1].isdigit()]
    assert len(rows) == 27
    values = [[float(v) for v in row.split()] for row in rows]
    assert all(0.0 <= v <= 1.0 for row in values for v in row)
    assert values[0] == [0.0, 0.0, 0.0]
    # neutral grade: red varies fastest and the grey axis stays (almost) grey
    assert values[1][0] > values[0][0] and values[1][1] == values[0][1]
    assert values[26] == pytest.approx([1.0, 1.0, 1.0], abs=1e-3)
    assert values[13] == pytest.approx([0.5, 0.5, 0.5], abs=2e-3)


def test_lut_parameters_change_the_grade() -> None:
    neutral = bake_lut3d_cube({"size": 5})
    warm = bake_lut3d_cube({"size": 5, "temperature_k": 3200})
    cool = bake_lut3d_cube({"size": 5, "temperature_k": 9000})
    punchy = bake_lut3d_cube({"size": 5, "contrast_milli": 8000, "midpoint_milli": 400})
    assert len({neutral, warm, cool, punchy}) == 4
    mid = [float(v) for v in warm.decode().splitlines()[5 + 62].split()]  # (2,2,2)
    assert mid[0] > mid[2]  # warm white balance: red above blue


def test_atlas_layout_and_seamless_determinism() -> None:
    data = bake_skin_pore_normal_atlas({"width": 16, "height": 8, "seed": 7, "pore_cell_px": 4})
    magic, version, width, height, channels, seed = struct.unpack(">8sHHHHQ", data[:24])
    assert (magic, version, width, height, channels, seed) == (sh.ATLAS_MAGIC, 1, 16, 8, 2, 7)
    assert len(data) == 24 + 16 * 8 * 2 + 16 * 8
    other = bake_skin_pore_normal_atlas({"width": 16, "height": 8, "seed": 8, "pore_cell_px": 4})
    assert other != data
    flat = bake_skin_pore_normal_atlas(
        {
            "width": 8,
            "height": 8,
            "pore_depth_milli": 0,
            "wrinkle_depth_milli": 0,
            "roughness_milli": 0,
        }
    )
    normals, wrinkle = flat[24 : 24 + 128], flat[24 + 128 :]
    assert set(normals) == {128} and set(wrinkle) == {128}  # flat surface: +Z normal


def test_formant_table_layout() -> None:
    data = bake_vocal_formant_table(
        {"languages": ["fa"], "voice_scale_milli": 1170, "window_sizes": [16]}
    )
    magic, version, mask, count, windows = struct.unpack(">8sHBBH", data[:14])
    assert (magic, version, mask, count, windows) == (sh.FORMANT_MAGIC, 1, 1, 6, 1)
    record = struct.unpack(">4sBHHHHHH", data[14:31])
    assert record[0].rstrip(b"\x00") == b"a" and record[1] == 1
    assert record[2:5] == (819, 1931, 2984)  # 700/1650/2550 Hz scaled by 1.17
    offset = 14 + 6 * 17
    assert struct.unpack(">I", data[offset : offset + 4]) == (16,)
    hann = struct.unpack(">16H", data[offset + 4 :])
    assert hann[0] == hann[-1] == 0 and hann == hann[::-1]
    assert max(hann) < 65535 and min(hann[1:-1]) > 0


def test_fixed_point_primitives_are_accurate() -> None:
    one = 1 << 32
    assert abs(sh._PI / one - 3.141592653589793) < 1e-9
    assert abs(sh._exp_fx(one) / one - 2.718281828459045) < 1e-8
    assert abs(sh._exp_fx(-3 * one) / one - 0.049787068367863944) < 1e-8
    sin, cos = sh._sincos_fx(sh._PI // 6)
    assert abs(sin / one - 0.5) < 1e-8 and abs(cos / one - 0.8660254037844386) < 1e-8
    assert sh._icbrt(0) == 0 and sh._icbrt(26) == 2 and sh._icbrt(27) == 3
    assert sh._icbrt(10**30) == 10**10
    probes = list(range(1, 20_000)) + [k**3 + d for k in range(2, 3000, 7) for d in (-1, 0, 1)]
    probes += [(1 << bits) + d for bits in range(1, 200) for d in (-1, 0, 1)]
    for n in probes:
        root = sh._icbrt(n)
        assert root**3 <= n < (root + 1) ** 3, n


@pytest.mark.parametrize(
    "recipe",
    [
        "not a mapping",
        {"recipe": "lut3d_cube", "version": 1, "extra": 1},
        {"recipe": "fractal", "version": 1},
        {"recipe": "lut3d_cube", "version": 2},
        {"recipe": "lut3d_cube", "version": True},
        {"recipe": "lut3d_cube", "version": 1, "params": []},
        {"recipe": "lut3d_cube", "version": 1, "params": {"size": 1}},
        {"recipe": "lut3d_cube", "version": 1, "params": {"size": "9"}},
        {"recipe": "lut3d_cube", "version": 1, "params": {"size": True}},
        {"recipe": "lut3d_cube", "version": 1, "params": {"title": 'bad"quote'}},
        {"recipe": "lut3d_cube", "version": 1, "params": {"gamma": 2}},
        {"recipe": "skin_pore_normal_atlas", "version": 1, "params": {"width": 20}},
        {"recipe": "vocal_formant_table", "version": 1, "params": {"languages": []}},
        {"recipe": "vocal_formant_table", "version": 1, "params": {"languages": ["de"]}},
        {"recipe": "vocal_formant_table", "version": 1, "params": {"languages": ["fa", "fa"]}},
        {"recipe": "vocal_formant_table", "version": 1, "params": {"window_sizes": [64, 32]}},
        {"recipe": "vocal_formant_table", "version": 1, "params": {"window_sizes": [8]}},
        {"recipe": "vocal_formant_table", "version": 1, "params": {"window_sizes": ["64"]}},
        {
            "recipe": "vocal_formant_table",
            "version": 1,
            "params": {"window_sizes": list(range(16, 26))},
        },
    ],
)
def test_invalid_recipes_are_rejected(recipe: object) -> None:
    with pytest.raises(SeedRecipeError):
        hydrate_seed(recipe)  # type: ignore[arg-type]
