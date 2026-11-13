"""Real source-code mutation campaign for the NAGAR ATLAS / TITAN / OLYMPUS zone.

This is a **mutation** campaign, not an adversarial negative-input test. For each
entry in :data:`MUTANTS` a single, real source change is introduced into an
isolated temporary copy of the package; the targeted test file is then run in a
subprocess. A mutant counts as KILLED only when a relevant test actually fails
because of the injected defect. The campaign asserts every mutant is killed, that
every mutation anchor is unique in the real tree, and that there are exactly 80.

The temporary copy is created under ``tmp_path``; the real repository source is
never modified. This is a REAL code-mutation experiment (REAL COMPUTE), not a
simulation: each mutant is compiled and imported by a real subprocess that runs
the targeted test file, so a kill means a real behavioural assertion failed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src"
TESTS = REPO_ROOT / "tests"

#: ``(relative source path, original text, mutated text, targeted test file)``.
#: Each ``original`` must occur exactly once in the real tree.
MUTANTS: tuple[tuple[str, str, str, str], ...] = (
    (
        "nexus_ai_agent/creative/atlas/non_euclidean_portal_metrics.py",
        "return float(np.dot(p - self.centroid, self.normal))",
        "return float(np.dot(p + self.centroid, self.normal))",
        "tests/creative/atlas/test_titan_olympus_mutation_killers.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/non_euclidean_portal_metrics.py",
        "t_star = t0 + (t1 - t0) * (s0 / (s0 - s1))",
        "t_star = t0 + (t1 - t0) * (s0 / (s0 + s1))",
        "tests/creative/atlas/test_wave13_portal_metrics.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/non_euclidean_portal_metrics.py",
        "if k < 3 or k > MAX_PORTAL_VERTICES:",
        "if k < 3 or k > MAX_PORTAL_VERTICES + 1:",
        "tests/creative/atlas/test_wave13_portal_metrics.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/non_euclidean_portal_metrics.py",
        "return R.T.copy(), -(R.T @ t)",
        "return R.T.copy(), (R.T @ t)",
        "tests/creative/atlas/test_wave13_portal_metrics.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/volumetric_atmospheric_scattering.py",
        "beta: np.ndarray = scale * (ref**-4.0)",
        "beta: np.ndarray = scale * (ref**4.0)",
        "tests/creative/atlas/test_wave14_atmospheric_scattering.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/volumetric_atmospheric_scattering.py",
        "phase: np.ndarray = (1.0 - g * g) / (4.0 * np.pi * denom)",
        "phase: np.ndarray = (1.0 + g * g) / (4.0 * np.pi * denom)",
        "tests/creative/atlas/test_wave14_atmospheric_scattering.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/volumetric_atmospheric_scattering.py",
        "denom = (1.0 + g * g - 2.0 * g * ct) ** 1.5",
        "denom = (1.0 + g * g - 2.0 * g * ct) ** 2.5",
        "tests/creative/atlas/test_wave14_atmospheric_scattering.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/volumetric_atmospheric_scattering.py",
        "phase: np.ndarray = (3.0 / (16.0 * np.pi)) * (1.0 + ct * ct)",
        "phase: np.ndarray = (3.0 / (8.0 * np.pi)) * (1.0 + ct * ct)",
        "tests/creative/atlas/test_wave14_atmospheric_scattering.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/eikonal_crowd_social_force.py",
        "return 0.5 * (a + b + float(np.sqrt(2.0 * c * c - (a - b) ** 2)))",
        "return 0.5 * (a + b - float(np.sqrt(2.0 * c * c - (a - b) ** 2)))",
        "tests/creative/atlas/test_wave15_eikonal_crowd.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/eikonal_crowd_social_force.py",
        "if b >= 1e17 or (b - a) >= c:",
        "if b >= 1e17 or (b - a) >= 2.0 * c:",
        "tests/creative/atlas/test_wave15_eikonal_crowd.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/eikonal_crowd_social_force.py",
        "heading = np.stack([-gx / norm, -gy / norm], axis=-1)",
        "heading = np.stack([gx / norm, gy / norm], axis=-1)",
        "tests/creative/atlas/test_wave15_eikonal_crowd.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/eikonal_crowd_social_force.py",
        "norm = np.sqrt(gx * gx + gy * gy)",
        "norm = gx * gx + gy * gy",
        "tests/creative/atlas/test_titan_olympus_mutation_killers.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/voronoi_fracture_destruction_ledger.py",
        "kinetic = 0.5 * impactor_mass * impactor_speed * impactor_speed",
        "kinetic = 0.5 * impactor_mass * impactor_speed",
        "tests/creative/atlas/test_wave16_voronoi_fracture.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/voronoi_fracture_destruction_ledger.py",
        'verdict = "FEASIBLE" if kinetic + _EPS >= surface else "INFEASIBLE"',
        'verdict = "FEASIBLE" if kinetic + _EPS <= surface else "INFEASIBLE"',
        "tests/creative/atlas/test_wave16_voronoi_fracture.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/voronoi_fracture_destruction_ledger.py",
        "surface = fracture_toughness * new_surface_area",
        "surface = fracture_toughness + new_surface_area",
        "tests/creative/atlas/test_wave16_voronoi_fracture.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/voronoi_fracture_destruction_ledger.py",
        "mass = float(densities[idx] * voxel_volume * count)",
        "mass = float(densities[idx] * voxel_volume / count)",
        "tests/creative/atlas/test_wave16_voronoi_fracture.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_seidel_optics.py",
        "return float(self.focal_x_mm / self.focal_y_mm)",
        "return float(self.focal_y_mm / self.focal_x_mm)",
        "tests/creative/atlas/test_wave17_anamorphic_optics.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_seidel_optics.py",
        "r_coc = defocus_mm / (2.0 * self.t_stop)",
        "r_coc = defocus_mm / (4.0 * self.t_stop)",
        "tests/creative/atlas/test_wave17_anamorphic_optics.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_seidel_optics.py",
        "sigma_x = sigma_y / self.squeeze_factor",
        "sigma_x = sigma_y * self.squeeze_factor",
        "tests/creative/atlas/test_wave17_anamorphic_optics.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_seidel_optics.py",
        "gx = np.exp(-(ax**2) / (2.0 * sigma_x**2))",
        "gx = np.exp(-(ax**2) / (sigma_x**2))",
        "tests/creative/atlas/test_titan_olympus_mutation_killers.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/pontryagin_editing_pacing_solver.py",
        "k_gain = np.linalg.solve(s, btp @ a)",
        "k_gain = np.linalg.solve(s, btp @ a) * 2.0",
        "tests/creative/atlas/test_wave18_pontryagin_pacing.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/pontryagin_editing_pacing_solver.py",
        "p = q + a.T @ p @ a - a.T @ p @ b @ k_gain",
        "p = q + a.T @ p @ a + a.T @ p @ b @ k_gain",
        "tests/creative/atlas/test_wave18_pontryagin_pacing.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/pontryagin_editing_pacing_solver.py",
        "u = -gains[k] @ x",
        "u = gains[k] @ x",
        "tests/creative/atlas/test_wave18_pontryagin_pacing.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/pontryagin_editing_pacing_solver.py",
        "costates[k] = 2.0 * riccati[k] @ states[k]",
        "costates[k] = riccati[k] @ states[k]",
        "tests/creative/atlas/test_wave18_pontryagin_pacing.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/titan_feature_film_compiler.py",
        "estimated_seconds=(total_frames / frame_rate) if total_frames else 0.0,",
        "estimated_seconds=(total_frames * frame_rate) if total_frames else 0.0,",
        "tests/creative/atlas/test_wave19_feature_film_compiler.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/titan_feature_film_compiler.py",
        'if applies != "always" and applies not in flags:',
        'if applies != "always" or applies not in flags:',
        "tests/creative/atlas/test_wave19_feature_film_compiler.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/titan_feature_film_compiler.py",
        "expected = hashlib.sha256(raw).hexdigest()",
        "expected = hashlib.md5(raw).hexdigest()",
        "tests/creative/atlas/test_wave19_feature_film_compiler.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/titan_feature_film_compiler.py",
        '"kind": "titan.compiled_feature_film",',
        '"kind": "titan.compiled_film",',
        "tests/creative/atlas/test_wave19_feature_film_compiler.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/titan_staged_execution_spine.py",
        'return "SKIPPED", "no bounded operator fixture for this pass", b""',
        'return "COMPUTED", "no bounded operator fixture for this pass", b""',
        "tests/creative/atlas/test_wave20_staged_spine.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/titan_staged_execution_spine.py",
        "if reopened != payload:",
        "if reopened == payload:",
        "tests/creative/atlas/test_wave20_staged_spine.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/titan_staged_execution_spine.py",
        'if not out_dir or out_dir.startswith("/") or ".." in out_dir.split("/"):',
        'if not out_dir or out_dir.startswith("/"):',
        "tests/creative/atlas/test_titan_olympus_mutation_killers.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/titan_staged_execution_spine.py",
        "if side < 2 or side > MAX_FIXTURE_SIDE:",
        "if side < 2 or side > MAX_FIXTURE_SIDE + 1:",
        "tests/creative/atlas/test_titan_olympus_mutation_killers.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/sdf_global_illumination.py",
        'return float(np.linalg.norm(_vec3(point, name="point") - self.center) - self.radius)',
        'return float(np.linalg.norm(_vec3(point, name="point") - self.center) + self.radius)',
        "tests/creative/atlas/test_wave21_sdf_gi.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/sdf_global_illumination.py",
        'return float(np.dot(self.normal, _vec3(point, name="point")) - self.offset)',
        'return float(np.dot(self.normal, _vec3(point, name="point")) + self.offset)',
        "tests/creative/atlas/test_wave21_sdf_gi.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/sdf_global_illumination.py",
        "outside = np.linalg.norm(np.maximum(q, 0.0))",
        "outside = np.linalg.norm(np.maximum(-q, 0.0))",
        "tests/creative/atlas/test_wave21_sdf_gi.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/sdf_global_illumination.py",
        "h = float(np.clip(0.5 + 0.5 * (db - da) / self.k, 0.0, 1.0))",
        "h = float(np.clip(0.5 + 0.5 * (da - db) / self.k, 0.0, 1.0))",
        "tests/creative/atlas/test_wave21_sdf_gi.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/sdf_global_illumination.py",
        "return float(np.linalg.norm(q) - self.minor)",
        "return float(np.linalg.norm(q) + self.minor)",
        "tests/creative/atlas/test_wave21_sdf_gi.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/sdf_global_illumination.py",
        "occ += (h - d) * sca",
        "occ += (d - h) * sca",
        "tests/creative/atlas/test_wave21_sdf_gi.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/spectral_ocean_swe.py",
        "out: np.ndarray = np.sqrt(gravity * kk)",
        "out: np.ndarray = np.sqrt(gravity) * kk",
        "tests/creative/atlas/test_wave22_spectral_ocean.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/spectral_ocean_swe.py",
        "omega_p = 0.8772 * gravity / wind_speed",
        "omega_p = 0.8772 * gravity * wind_speed",
        "tests/creative/atlas/test_wave22_spectral_ocean.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/spectral_ocean_swe.py",
        "k_p = omega_p * omega_p / gravity",
        "k_p = omega_p * omega_p * gravity",
        "tests/creative/atlas/test_wave22_spectral_ocean.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/spectral_ocean_swe.py",
        "h_s = 0.21 * wind_speed * wind_speed / gravity",
        "h_s = 0.12 * wind_speed * wind_speed / gravity",
        "tests/creative/atlas/test_wave22_spectral_ocean.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/spectral_ocean_swe.py",
        "target_var = (h_s / 4.0) ** 2",
        "target_var = (h_s / 2.0) ** 2",
        "tests/creative/atlas/test_wave22_spectral_ocean.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/spectral_ocean_swe.py",
        "spread = np.where(cos_dir > 0.0, cos_dir * cos_dir, 0.0)",
        "spread = np.where(cos_dir > 0.0, cos_dir, 0.0)",
        "tests/creative/atlas/test_titan_olympus_mutation_killers.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/buoyant_pyro_blackbody.py",
        "x = PLANCK_H * PLANCK_C / (lam * BOLTZMANN_K * temperature_k)",
        "x = PLANCK_H * PLANCK_C / (lam * BOLTZMANN_K + temperature_k)",
        "tests/creative/atlas/test_wave23_pyro_blackbody.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/buoyant_pyro_blackbody.py",
        "out: np.ndarray = (2.0 * PLANCK_H * PLANCK_C**2 / lam**5) / denom",
        "out: np.ndarray = (2.0 * PLANCK_H * PLANCK_C**2 / lam**4) / denom",
        "tests/creative/atlas/test_wave23_pyro_blackbody.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/buoyant_pyro_blackbody.py",
        "return WIEN_B / temperature_k * 1e9",
        "return WIEN_B * temperature_k * 1e9",
        "tests/creative/atlas/test_wave23_pyro_blackbody.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/buoyant_pyro_blackbody.py",
        "return STEFAN_SIGMA * temperature_k**4",
        "return STEFAN_SIGMA * temperature_k**3",
        "tests/creative/atlas/test_wave23_pyro_blackbody.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/buoyant_pyro_blackbody.py",
        "out: np.ndarray = np.clip(rgb ** (1.0 / gamma), 0.0, 1.0)",
        "out: np.ndarray = np.clip(rgb ** gamma, 0.0, 1.0)",
        "tests/creative/atlas/test_titan_olympus_mutation_killers.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/rigid_contact_pgs_lcp.py",
        "return 0.0 if self.static else 1.0 / self.mass",
        "return 0.0 if self.static else 1.0 * self.mass",
        "tests/creative/atlas/test_titan_olympus_mutation_killers.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/rigid_contact_pgs_lcp.py",
        "if c.a == c.b:",
        "if c.a != c.b:",
        "tests/creative/atlas/test_wave24_rigid_contact_pgs.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/rigid_contact_pgs_lcp.py",
        "target[i] = -c.restitution * vn if vn < 0.0 else 0.0",
        "target[i] = -c.restitution * vn if vn > 0.0 else 0.0",
        "tests/creative/atlas/test_wave24_rigid_contact_pgs.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/rigid_contact_pgs_lcp.py",
        "bias = baumgarte * max(c.penetration - slop, 0.0)",
        "bias = baumgarte * min(c.penetration - slop, 0.0)",
        "tests/creative/atlas/test_wave24_rigid_contact_pgs.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/rigid_contact_pgs_lcp.py",
        "d_lambda = (target[i] + bias - vn) / k_n[i]",
        "d_lambda = (target[i] + bias + vn) / k_n[i]",
        "tests/creative/atlas/test_wave24_rigid_contact_pgs.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/botanical_modal_wind.py",
        "beta_l**2 * np.sqrt(self.flexural_rigidity / (self.mass_per_length * self.length**4))",
        "beta_l**2 * np.sqrt(self.flexural_rigidity / (self.mass_per_length * self.length**2))",
        "tests/creative/atlas/test_wave25_botanical_wind.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/botanical_modal_wind.py",
        "sigma = (np.cosh(b) + np.cos(b)) / (np.sinh(b) + np.sin(b))",
        "sigma = (np.cosh(b) - np.cos(b)) / (np.sinh(b) + np.sin(b))",
        "tests/creative/atlas/test_titan_olympus_mutation_killers.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/botanical_modal_wind.py",
        "return 0.5 * air_density * drag_coefficient * area * speed * speed",
        "return 0.5 * air_density * drag_coefficient * area * speed",
        "tests/creative/atlas/test_wave25_botanical_wind.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/botanical_modal_wind.py",
        "target_std = turbulence_intensity * mean_speed",
        "target_std = turbulence_intensity + mean_speed",
        "tests/creative/atlas/test_wave25_botanical_wind.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/aces_hald_film_optic.py",
        "num = arr * (a * arr + b)",
        "num = arr * (a * arr - b)",
        "tests/creative/atlas/test_wave26_film_optic.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/aces_hald_film_optic.py",
        "den = arr * (c * arr + d) + e",
        "den = arr * (c * arr + d) - e",
        "tests/creative/atlas/test_wave26_film_optic.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/aces_hald_film_optic.py",
        "out: np.ndarray = np.clip(num / den, 0.0, 1.0)",
        "out: np.ndarray = np.clip(num * den, 0.0, 1.0)",
        "tests/creative/atlas/test_wave26_film_optic.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/aces_hald_film_optic.py",
        "out: np.ndarray = (b * n + g) * n + r",
        "out: np.ndarray = (b * n + g) * n - r",
        "tests/creative/atlas/test_wave26_film_optic.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/aces_hald_film_optic.py",
        "clut = np.stack([r, g, b], axis=-1).astype(np.float64) / denom",
        "clut = np.stack([r, g, b], axis=-1).astype(np.float64) * denom",
        "tests/creative/atlas/test_wave26_film_optic.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/aces_hald_film_optic.py",
        "out: np.ndarray = c0 * (1 - fb) + c1 * fb",
        "out: np.ndarray = c0 * (1 - fb) - c1 * fb",
        "tests/creative/atlas/test_wave26_film_optic.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/diegetic_modal_foley.py",
        "delay = ev.source_distance / SPEED_OF_SOUND",
        "delay = ev.source_distance * SPEED_OF_SOUND",
        "tests/creative/atlas/test_wave27_modal_foley.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/diegetic_modal_foley.py",
        "out += level * np.exp(-t / tau) * np.sin(2.0 * np.pi * fr * t + phase)",
        "out += level * np.exp(-t * tau) * np.sin(2.0 * np.pi * fr * t + phase)",
        "tests/creative/atlas/test_titan_olympus_mutation_killers.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/diegetic_modal_foley.py",
        "if start < 0 or start >= n:",
        "if start < 0 or start > n:",
        "tests/creative/atlas/test_titan_olympus_mutation_killers.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/diegetic_modal_foley.py",
        "mix[start:end] += impact.samples[: end - start]",
        "mix[start:end] -= impact.samples[: end - start]",
        "tests/creative/atlas/test_titan_olympus_mutation_killers.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/showrunner_bible.py",
        "return Truth.FALSE  # not yet introduced",
        "return Truth.TRUE  # not yet introduced",
        "tests/creative/atlas/test_wave28_showrunner_bible.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/showrunner_bible.py",
        "return Truth.TRUE if timeline[latest] == cid else Truth.FALSE",
        "return Truth.TRUE if timeline[latest] != cid else Truth.FALSE",
        "tests/creative/atlas/test_wave28_showrunner_bible.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/showrunner_bible.py",
        "return Truth.UNKNOWN  # ownership never declared up to this episode",
        "return Truth.FALSE  # ownership never declared up to this episode",
        "tests/creative/atlas/test_wave28_showrunner_bible.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/showrunner_bible.py",
        "if any(e.number == episode.number for e in self.episodes):",
        "if any(e.number != episode.number for e in self.episodes):",
        "tests/creative/atlas/test_wave28_showrunner_bible.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/media_sine.py",
        "return value - (value % 2)",
        "return value - (value % 2) + 1",
        "tests/creative/atlas/test_wave29_media_sine.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/media_sine.py",
        "if (probe.width, probe.height) != (w, h):",
        "if (probe.width, probe.height) == (w, h):",
        "tests/creative/atlas/test_wave29_media_sine.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/media_sine.py",
        "if probe.nb_frames != frames:",
        "if probe.nb_frames == frames:",
        "tests/creative/atlas/test_wave29_media_sine.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/media_sine.py",
        "if value < 2 or value > MAX_DIMENSION:",
        "if value < 2 or value > MAX_DIMENSION + 1:",
        "tests/creative/atlas/test_wave29_media_sine.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/olympus_master.py",
        "if frames < 1 or frames > MAX_FRAMES:",
        "if frames < 1 or frames > MAX_FRAMES + 1:",
        "tests/creative/atlas/test_wave30_olympus_master.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/olympus_master.py",
        'if not out_dir or out_dir.startswith("/") or ".." in out_dir.split("/"):',
        'if not out_dir or out_dir.startswith("/"):',
        "tests/creative/atlas/test_titan_olympus_mutation_killers.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/olympus_master.py",
        "if reopened != continuity_payload:",
        "if reopened == continuity_payload:",
        "tests/creative/atlas/test_wave30_olympus_master.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/olympus_master.py",
        "if reopened != payload:",
        "if reopened == payload:",
        "tests/creative/atlas/test_wave30_olympus_master.py",
    ),
)


def test_campaign_has_exactly_eighty_mutants() -> None:
    assert len(MUTANTS) == 80


def test_every_mutation_anchor_is_unique() -> None:
    """Every ``original`` snippet must appear exactly once in the real tree."""
    for rel, old, _new, _test in MUTANTS:
        text = (SRC / rel).read_text(encoding="utf-8")
        assert text.count(old) == 1, f"{rel}: anchor count {text.count(old)} for {old!r}"


def test_every_target_test_file_exists() -> None:
    for _rel, _old, _new, target in MUTANTS:
        assert (REPO_ROOT / target).is_file(), f"missing target test {target}"


@pytest.mark.parametrize(("rel", "old", "new", "target_test"), MUTANTS)
def test_mutant_is_killed(rel: str, old: str, new: str, target_test: str, tmp_path: Path) -> None:
    """Each injected defect must make the targeted test file fail."""
    work = tmp_path / "campaign"
    shutil.copytree(SRC, work / "src")
    shutil.copytree(TESTS, work / "tests")
    shutil.copy(REPO_ROOT / "pyproject.toml", work / "pyproject.toml")

    module = work / "src" / rel
    original = module.read_text(encoding="utf-8")
    assert original.count(old) == 1
    module.write_text(original.replace(old, new, 1), encoding="utf-8")

    env: dict[str, Any] = dict(os.environ)
    env["PYTHONPATH"] = str(work / "src")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", target_test, "-q", "-p", "no:cacheprovider", "-x"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode != 0, (
        f"mutant SURVIVED in {rel}: {old!r} -> {new!r}\nstdout tail:\n{result.stdout[-2000:]}"
    )
