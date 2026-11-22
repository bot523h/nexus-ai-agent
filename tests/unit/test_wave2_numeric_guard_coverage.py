"""Exercise generated numerical entry guards before downstream DSP geometry.

Cases are extracted from explicit guard calls (not inferred from function names).
The witness requires the invalid argument's own name in the exception, so a
failure of a different shape/parameter cannot masquerade as a guard hit.
"""

import ast
import importlib
import inspect

import numpy as np
import pytest

MODULES = [
    "creative.audio_engine." + name
    for name in (
        "cinema_kernels",
        "stereo_hpss",
        "psychoacoustic_bark_mastering",
        "spectral_voice_restoration",
        "ambisonics_binaural_fdn",
        "av_subframe_foley_sync",
        "forensic_qc",
        "spine_lineage",
    )
] + [
    "creative.atlas.aces_hald_film_optic",
    "creative.photoreal_intrinsic.screened_poisson_skin_transport",
    "creative.storyboard_compiler.cinematography_solver",
]


def _cases():
    result = []
    for path in MODULES:
        module = importlib.import_module("nexus_ai_agent." + path)
        for name, fn in vars(module).items():
            if not inspect.isfunction(fn) or fn.__module__ != module.__name__:
                continue
            tree = ast.parse(inspect.getsource(fn))
            for call in ast.walk(tree):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                    continue
                if call.func.id not in ("_require_finite_scalar", "_require_finite_array"):
                    continue
                if not call.args or not isinstance(call.args[0], ast.Name):
                    continue
                parameter = call.args[0].id
                label = next(
                    (
                        kw.value.value
                        for kw in call.keywords
                        if kw.arg == "name" and isinstance(kw.value, ast.Constant)
                    ),
                    None,
                )
                if parameter not in inspect.signature(fn).parameters or label != parameter:
                    continue
                case = (path, name, parameter, call.func.id.endswith("array"))
                if case not in result:
                    result.append(case)
    return result


@pytest.mark.parametrize("path,name,parameter,is_array", _cases())
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), True])
def test_numeric_parameter_guard(path, name, parameter, is_array, bad):
    module = importlib.import_module("nexus_ai_agent." + path)
    fn = getattr(module, name)
    kwargs = {}
    for key, spec in inspect.signature(fn).parameters.items():
        if spec.default is not inspect.Parameter.empty:
            continue
        annotation = str(spec.annotation)
        if "ndarray" in annotation or "Array" in annotation or "Sequence" in annotation:
            kwargs[key] = np.ones((4, 4))
        elif "tuple" in annotation:
            kwargs[key] = (1.0, 1.0, 1.0)
        elif annotation == "int":
            kwargs[key] = 1
        elif annotation == "float":
            kwargs[key] = 0.5
        elif annotation == "str":
            kwargs[key] = "test"
        else:
            kwargs[key] = None
    if name == "_validate_so3":
        kwargs["tol"] = 1e-5
    if name == "build_rule_of_thirds_pose":
        kwargs.update(
            camera_pos=(0.0, -2.0, 1.0), subject_pos=(0.0, 0.0, 1.0), target_uv=(1 / 3, 1 / 3)
        )
    kwargs[parameter] = np.array([bad]) if is_array else bad
    with pytest.raises((ValueError, TypeError)) as error:
        fn(**kwargs)
    assert parameter in str(error.value)


@pytest.mark.parametrize("path", MODULES)
def test_guard_nonnumeric_scalars_and_boolean_object_arrays(path):
    module = importlib.import_module("nexus_ai_agent." + path)
    for bad in ("not numeric", None, 1j):
        with pytest.raises((ValueError, TypeError)):
            module._require_finite_scalar(bad, name="input")
    for bad in (np.array([True], dtype=object), np.array([float("nan")], dtype=object)):
        with pytest.raises((ValueError, TypeError)):
            module._require_finite_array(bad, name="input")
