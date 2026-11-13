"""M72--M81 source/operator mutants, isolated without unloading C extensions.

Each oracle is first run against the real implementation, then the mutated
source. A mutant is killed only when the SAME independent oracle rejects it.
Existing fault-injection probes and handler-disable tests remain unchanged.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import numpy.typing as npt
import pytest

from nexus_ai_agent.creative.veritas import (
    continuum_and_quantum_mechanics as quantum,
)
from nexus_ai_agent.creative.veritas import (
    omnia_cinema_physics as omnia,
)
from nexus_ai_agent.creative.veritas import (
    real_media_veritas_compiler as compiler,
)
from nexus_ai_agent.creative.veritas import (
    reality_auditor as auditor,
)
from nexus_ai_agent.creative.veritas import (
    veritas_spine_adapter as spine,
)

MUTATIONS = [
    ("M72", quantum, "(drho_dt + divergence) ** 2", "divergence ** 2"),
    ("M73", quantum, "np.exp(-potential / scale)", "np.exp(potential / scale)"),
    ("M74", omnia, "cauchy_b_coefficient / lambda_r**2", "-cauchy_b_coefficient / lambda_r**2"),
    (
        "M75",
        omnia,
        "sound_speed_m_s - float(radial_velocity_m_s)",
        "sound_speed_m_s + float(radial_velocity_m_s)",
    ),
    ("M76", omnia, "if n_cols % 2 == 0:", "if True:"),
    ("M77", omnia, "-0.5 * blur_arc, 0.5 * blur_arc", "0.0 * blur_arc, blur_arc"),
    ("M78", auditor, "after, _ = self._check_eyes(healed)", "after = ()"),
    (
        "M79",
        compiler,
        "output_frames_content_sha256 = _digest_array(decoded_frames)",
        "output_frames_content_sha256 = _digest_files(png_paths)",
    ),
    (
        "M80",
        spine,
        (
            "if self.active_fencing_token_getter() != request.fencing_token:\n"
            '                raise VeritasStaleFencingTokenError("stale before promotion")'
        ),
        'if False:\n                raise VeritasStaleFencingTokenError("stale before promotion")',
    ),
    ("M81", spine, "if not verify_compiled_veritas_bundle_on_disk(bundle):", "if False:"),
]


def oracle(code: str, module: ModuleType, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if code == "M72":
        value = module.madelung_continuity_residual(
            np.ones((5, 5)) * 2, np.zeros((5, 5)), previous_density=np.ones((5, 5)), dt=0.25
        )
        assert value == pytest.approx(4.0)
    elif code == "M73":
        density = np.ones((5, 5))
        density[2, 2] = 5
        weight = module.bohmian_edge_weight(density)
        assert np.all(weight > 0) and np.all(weight <= 1) and np.any(weight < 1)
    elif code == "M74":
        result = module.rectify_anamorphic_dioptric_optics(np.zeros((8, 8, 3)))
        expected = ((1.5168 + 0.0042 / 0.650**2) - (1.5168 + 0.0042 / 0.532**2)) / (
            1.5168 + 0.0042 / 0.532**2
        )
        assert result.delta_red == pytest.approx(expected)
    elif code == "M75":
        result = module.apply_acoustic_doppler_shift(np.ones(100), 48000, 10)
        assert result.doppler_factor == pytest.approx(343 / 333)
    elif code == "M76":
        kx, _ = module._wavenumbers(7, 7)
        assert kx[-1] == pytest.approx(2 * np.pi * 3 / 7)
    elif code == "M77":
        angles: list[float] = []

        def capture(patch: npt.NDArray[np.float64], angle: float) -> npt.NDArray[np.float64]:
            angles.append(angle)
            return patch

        with monkeypatch.context() as patcher:
            patcher.setattr(module, "_rotate", capture)
            module.rectify_stroboscopic_wagon_wheel(
                np.ones((8, 8)), angular_velocity_rad_s=200, fps=24
            )
        assert np.mean(angles) == pytest.approx(0, abs=1e-12)
    elif code == "M78":
        with monkeypatch.context() as patcher:
            patcher.setattr(
                module,
                "solve_binocular_vergence",
                lambda *a: SimpleNamespace(
                    violations_before=("operator_fault",), is_consistent=False
                ),
            )
            shot = module.CinemaShot(
                eyes=(
                    np.array([-31.5, 0.0, 0.0]),
                    np.array([31.5, 0.0, 0.0]),
                    np.array([0.0, 0.0, 500.0]),
                )
            )
            report = module.VeritasRealityAuditor(laws=(2,)).audit_and_heal_cinema_shot(shot)
        assert "operator_fault" in report.details_after[2]
    elif code == "M79":
        result = module.compile_and_heal_real_media_shot(
            root, input_frames=np.zeros((2, 8, 8, 3)), input_audio_pcm=np.zeros(100), mux_mp4=False
        )
        decoded = np.stack([module._read_png_rgb(Path(p)) for p in result.png_paths])
        assert result.output_frames_content_sha256 == hashlib.sha256(decoded.tobytes()).hexdigest()
    else:
        token = [7]

        def fault(stage: Path) -> None:
            if code == "M80":
                token[0] = 8
            else:
                path = stage / "dialogue.wav"
                path.write_bytes(path.read_bytes()[:-1])

        req = module.VeritasSpinePublishRequest(
            "p",
            "r",
            "s",
            "a",
            7,
            root,
            root / "published",
            input_frames_rgb=np.zeros((2, 8, 8, 3)),
            input_audio_pcm=np.zeros(100),
        )
        refused = False
        try:
            module.VeritasStagedSpineAdapter(lambda: token[0], fault).execute(req)
        except module.VeritasSpineVerificationError:
            refused = True
        assert refused and not (root / "published").exists()


@pytest.mark.parametrize("code,original,old,new", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_internal_source_mutant_killed(
    code: str,
    original: ModuleType,
    old: str,
    new: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle(code, original, tmp_path / "baseline", monkeypatch)
    assert original.__file__ is not None
    source = Path(original.__file__).read_text()
    assert source.count(old) == 1, "mutation site must be unique"
    name = f"nexus_ai_agent.creative.veritas._isolated_{code}"
    spec = importlib.util.spec_from_file_location(name, original.__file__)
    assert spec is not None
    mutated = importlib.util.module_from_spec(spec)
    # Register only this new Python module. Never purge numpy/PIL/scipy or
    # reload their C extensions during a coverage run.
    monkeypatch.setitem(sys.modules, name, mutated)
    exec(compile(source.replace(old, new), original.__file__, "exec"), mutated.__dict__)
    with pytest.raises(AssertionError):
        oracle(code, mutated, tmp_path / "mutant", monkeypatch)
