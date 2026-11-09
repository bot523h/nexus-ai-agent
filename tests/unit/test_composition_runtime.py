"""Tests for the NAGAR Creative Capability Composition layer.

These tests use the stdlib ``unittest`` so they run without the project's
full dev dependency tree (the composition layer depends only on pydantic +
the FFmpeg/ImageMagick binaries).

Coverage map (matches the prototype acceptance criteria):

* real execution       -- 3 composed capabilities produce a real media file
* invalid input        -- type mismatch, missing binding, cycle all refused
* unauthorized         -- network policy, non-commercial commercial-use, quota
* contract conformance -- output media type matches the derived spec; proof verifies
* reproducibility      -- same inputs + params yield the same output hash
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from nexus_ai_agent.creative.composition import (
    BuiltinMediaWorker,
    CapabilityInput,
    CapabilityOutput,
    CapabilitySpec,
    CompositionCompiler,
    CompositionError,
    CompositionGraph,
    CompositionNode,
    CompositionRuntime,
    InputBinding,
    LicenseInfo,
    MediaType,
    ResourceRequirements,
    RuntimeError,
    RuntimePolicy,
    VerificationContract,
    sha256_file,
    verify_proof,
)

PNG = MediaType(kind="image", codec="png")
JPEG = MediaType(kind="image", codec="jpeg")
VIDEO = MediaType(kind="video", codec="h264", container="mp4")
MIT = LicenseInfo(spdx_id="MIT", source_license="MIT", copyleft=False, allow_commercial=True)
NC = LicenseInfo(
    spdx_id="CC-BY-NC-4.0",
    source_license="MIT",
    model_license="CC-BY-NC-4.0",
    copyleft=False,
    allow_commercial=False,
)


def _registry() -> dict[str, CapabilitySpec]:
    return {
        "nagar.image.resize": CapabilitySpec(
            capability_id="nagar.image.resize",
            version="1.0.0",
            inputs=(CapabilityInput(name="source", media_type=PNG),),
            outputs=(CapabilityOutput(name="resized", media_type=PNG),),
            resources=ResourceRequirements(cpu_threads=1, ram_mb=200, disk_mb=100),
            license=MIT,
        ),
        "nagar.image.grayscale": CapabilitySpec(
            capability_id="nagar.image.grayscale",
            version="1.0.0",
            inputs=(CapabilityInput(name="source", media_type=PNG),),
            outputs=(CapabilityOutput(name="grayscale", media_type=PNG),),
            resources=ResourceRequirements(cpu_threads=1, ram_mb=150, disk_mb=100),
            license=MIT,
        ),
        "nagar.video.from_image": CapabilitySpec(
            capability_id="nagar.video.from_image",
            version="1.0.0",
            inputs=(CapabilityInput(name="source", media_type=PNG),),
            outputs=(CapabilityOutput(name="video", media_type=VIDEO),),
            resources=ResourceRequirements(cpu_threads=2, ram_mb=500, disk_mb=200),
            license=MIT,
        ),
        "nagar.image.convert": CapabilitySpec(
            capability_id="nagar.image.convert",
            version="1.0.0",
            inputs=(CapabilityInput(name="source", media_type=PNG),),
            outputs=(CapabilityOutput(name="converted", media_type=JPEG),),
            resources=ResourceRequirements(cpu_threads=1, ram_mb=100, disk_mb=50),
            license=MIT,
        ),
        # non-commercial capability (e.g. a model under CC-BY-NC)
        "nagar.image.restyle_nc": CapabilitySpec(
            capability_id="nagar.image.restyle_nc",
            version="1.0.0",
            inputs=(CapabilityInput(name="source", media_type=PNG),),
            outputs=(CapabilityOutput(name="restyled", media_type=PNG),),
            resources=ResourceRequirements(cpu_threads=1, ram_mb=256, disk_mb=100),
            license=NC,
        ),
        # network-required capability (e.g. a cloud API adapter)
        "nagar.image.cloud_enhance": CapabilitySpec(
            capability_id="nagar.image.cloud_enhance",
            version="1.0.0",
            inputs=(CapabilityInput(name="source", media_type=PNG),),
            outputs=(CapabilityOutput(name="enhanced", media_type=PNG),),
            resources=ResourceRequirements(cpu_threads=1, ram_mb=128, disk_mb=50, network=True),
            license=MIT,
            network_required=True,
        ),
    }


def _make_image(path: Path, *, width: int = 640, height: int = 480) -> Path:
    subprocess.run(
        ["convert", "-size", f"{width}x{height}", "gradient:blue-red", str(path)],
        check=True,
        capture_output=True,
    )
    return path


class CompositionCompilerTests(unittest.TestCase):
    """The compiler validates structure, types, resources, licences, determinism."""

    def test_composes_three_capabilities(self) -> None:
        reg = _registry()
        g = CompositionGraph()
        g.add_external_input("image", PNG)
        g.add_node(CompositionNode("r", "nagar.image.resize",
            params={"width": 320, "height": 240},
            bindings={"source": InputBinding("source", external="image")}))
        g.add_node(CompositionNode("g", "nagar.image.grayscale",
            bindings={"source": InputBinding("source", node_output=("r", "resized"))}))
        g.add_node(CompositionNode("v", "nagar.video.from_image",
            params={"fps": 10, "duration_seconds": 1},
            bindings={"source": InputBinding("source", node_output=("g", "grayscale"))}))
        g.add_expected_output("clip", "v", "video")

        derived = CompositionCompiler(reg).compile(g)
        self.assertEqual(derived.capability_id, "nagar.composition.grayscale.resize.from_image")
        self.assertTrue(derived.deterministic)
        self.assertEqual(derived.resources.cpu_threads, 4)
        self.assertEqual(derived.resources.ram_mb, 850)
        # output media type is the final node's output
        self.assertEqual(derived.outputs[0].media_type, VIDEO)
        self.assertEqual(derived.inputs[0].media_type, PNG)

    def test_refuses_type_mismatch(self) -> None:
        reg = _registry()
        g = CompositionGraph()
        g.add_external_input("image", JPEG)  # wrong: resize requires PNG
        g.add_node(CompositionNode("r", "nagar.image.resize",
            bindings={"source": InputBinding("source", external="image")}))
        g.add_expected_output("out", "r", "resized")
        with self.assertRaises(CompositionError):
            CompositionCompiler(reg).compile(g)

    def test_refuses_cycle(self) -> None:
        reg = _registry()
        g = CompositionGraph()
        g.add_node(CompositionNode("a", "nagar.image.grayscale",
            bindings={"source": InputBinding("source", node_output=("b", "grayscale"))}))
        g.add_node(CompositionNode("b", "nagar.image.grayscale",
            bindings={"source": InputBinding("source", node_output=("a", "grayscale"))}))
        g.add_expected_output("out", "a", "grayscale")
        with self.assertRaises(CompositionError):
            CompositionCompiler(reg).compile(g)

    def test_refuses_missing_required_binding(self) -> None:
        reg = _registry()
        g = CompositionGraph()
        g.add_node(CompositionNode("r", "nagar.image.resize"))
        g.add_expected_output("out", "r", "resized")
        with self.assertRaises(CompositionError):
            CompositionCompiler(reg).compile(g)

    def test_refuses_quota_exceeded(self) -> None:
        reg = _registry()
        g = CompositionGraph()
        g.add_external_input("image", PNG)
        g.add_node(CompositionNode("r", "nagar.image.resize",
            bindings={"source": InputBinding("source", external="image")}))
        g.add_expected_output("out", "r", "resized")
        quota = ResourceRequirements(cpu_threads=1, ram_mb=1, disk_mb=1, network=False)
        with self.assertRaises(CompositionError):
            CompositionCompiler(reg).compile(g, quota=quota)

    def test_copyleft_propagates(self) -> None:
        reg = _registry()
        g = CompositionGraph()
        g.add_external_input("image", PNG)
        g.add_node(CompositionNode("r", "nagar.image.restyle_nc",
            bindings={"source": InputBinding("source", external="image")}))
        g.add_node(CompositionNode("v", "nagar.video.from_image",
            params={"fps": 10, "duration_seconds": 1},
            bindings={"source": InputBinding("source", node_output=("r", "restyled"))}))
        g.add_expected_output("clip", "v", "video")
        derived = CompositionCompiler(reg).compile(g)
        self.assertFalse(derived.license.effective_allow_commercial)

    def test_stochastic_node_demotes_determinism(self) -> None:
        reg = _registry()
        # make restyle_nc non-deterministic
        reg["nagar.image.restyle_nc"] = reg["nagar.image.restyle_nc"].model_copy(
            update={"deterministic": False}
        )
        g = CompositionGraph()
        g.add_external_input("image", PNG)
        g.add_node(CompositionNode("r", "nagar.image.restyle_nc",
            bindings={"source": InputBinding("source", external="image")}))
        g.add_expected_output("out", "r", "restyled")
        derived = CompositionCompiler(reg).compile(g)
        self.assertFalse(derived.deterministic)


class RuntimePolicyTests(unittest.TestCase):
    """The runtime enforces policy before any worker runs."""

    def test_refuses_network_when_policy_forbids(self) -> None:
        reg = _registry()
        g = CompositionGraph()
        g.add_external_input("image", PNG)
        g.add_node(CompositionNode("c", "nagar.image.cloud_enhance",
            bindings={"source": InputBinding("source", external="image")}))
        g.add_expected_output("out", "c", "enhanced")
        # quota allows network (so the compiler passes), but policy forbids it
        quota = ResourceRequirements(cpu_threads=8, ram_mb=4096, disk_mb=4096, network=True)
        runtime = CompositionRuntime(reg, policy=RuntimePolicy(allow_network=False, quota=quota))
        with self.assertRaises(RuntimeError):
            runtime.execute(g, {"image": "/tmp/nonexistent.png"})

    def test_refuses_commercial_use_of_noncommercial(self) -> None:
        reg = _registry()
        g = CompositionGraph()
        g.add_external_input("image", PNG)
        g.add_node(CompositionNode("r", "nagar.image.restyle_nc",
            bindings={"source": InputBinding("source", external="image")}))
        g.add_expected_output("out", "r", "restyled")
        quota = ResourceRequirements(cpu_threads=8, ram_mb=4096, disk_mb=4096)
        runtime = CompositionRuntime(reg, policy=RuntimePolicy(allow_commercial=False, quota=quota))
        with self.assertRaises(RuntimeError):
            runtime.execute(g, {"image": "/tmp/nonexistent.png"}, commercial_use=True)


class RuntimeExecutionTests(unittest.TestCase):
    """Real execution end-to-end: 3 operations, real output, proof-carrying artifact."""

    def test_real_execution_produces_verified_output(self) -> None:
        reg = _registry()
        g = CompositionGraph()
        g.add_external_input("image", PNG)
        g.add_node(CompositionNode("r", "nagar.image.resize",
            params={"width": 320, "height": 240},
            bindings={"source": InputBinding("source", external="image")}))
        g.add_node(CompositionNode("gr", "nagar.image.grayscale",
            bindings={"source": InputBinding("source", node_output=("r", "resized"))}))
        g.add_node(CompositionNode("v", "nagar.video.from_image",
            params={"fps": 10, "duration_seconds": 1},
            bindings={"source": InputBinding("source", node_output=("gr", "grayscale"))}))
        g.add_expected_output("clip", "v", "video")

        with tempfile.TemporaryDirectory() as tmp:
            img = _make_image(Path(tmp) / "in.png")
            quota = ResourceRequirements(cpu_threads=8, ram_mb=4096, disk_mb=4096)
            runtime = CompositionRuntime(reg, policy=RuntimePolicy(quota=quota))
            result = runtime.execute(g, {"image": str(img)})

            out = Path(result.outputs["clip"])
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 0)

            # contract conformance: output media type matches derived spec
            self.assertEqual(result.derived_spec.outputs[0].media_type, VIDEO)

            # proof-carrying artifact is independently verified
            art = result.artifacts["clip"]
            self.assertTrue(art.verification.verified)
            self.assertEqual(art.verification.method, "independent_rehash")
            self.assertEqual(len(art.execution_receipts), 3)

            # lineage records all three capabilities in order
            self.assertEqual(len(art.lineage), 3)

            # re-verify independently (simulate a downstream verifier)
            report = verify_proof(art)
            self.assertTrue(report.verified)

    def test_reproducibility_same_input_same_hash(self) -> None:
        reg = _registry()
        g = CompositionGraph()
        g.add_external_input("image", PNG)
        g.add_node(CompositionNode("r", "nagar.image.resize",
            params={"width": 320, "height": 240},
            bindings={"source": InputBinding("source", external="image")}))
        g.add_node(CompositionNode("c", "nagar.image.convert",
            params={"format": "jpeg", "quality": 90},
            bindings={"source": InputBinding("source", node_output=("r", "resized"))}))
        g.add_expected_output("img", "c", "converted")

        with tempfile.TemporaryDirectory() as tmp:
            img = _make_image(Path(tmp) / "in.png")
            quota = ResourceRequirements(cpu_threads=8, ram_mb=4096, disk_mb=4096)
            runtime = CompositionRuntime(reg, policy=RuntimePolicy(quota=quota))
            r1 = runtime.execute(g, {"image": str(img)}, output_dir=Path(tmp) / "run1")
            r2 = runtime.execute(g, {"image": str(img)}, output_dir=Path(tmp) / "run2")
            h1 = r1.artifacts["img"].output_hash
            h2 = r2.artifacts["img"].output_hash
            self.assertEqual(h1, h2, "deterministic composition must reproduce byte-for-byte")

    def test_tampered_artifact_fails_verification(self) -> None:
        reg = _registry()
        g = CompositionGraph()
        g.add_external_input("image", PNG)
        g.add_node(CompositionNode("r", "nagar.image.resize",
            params={"width": 160, "height": 120},
            bindings={"source": InputBinding("source", external="image")}))
        g.add_expected_output("out", "r", "resized")

        with tempfile.TemporaryDirectory() as tmp:
            img = _make_image(Path(tmp) / "in.png")
            quota = ResourceRequirements(cpu_threads=8, ram_mb=4096, disk_mb=4096)
            runtime = CompositionRuntime(reg, policy=RuntimePolicy(quota=quota))
            result = runtime.execute(g, {"image": str(img)})
            art = result.artifacts["out"]
            # tamper with the output bytes
            with open(art.output_path, "ab") as f:
                f.write(b"tampered")
            report = verify_proof(art)
            self.assertFalse(report.verified)
            self.assertTrue(any("mismatch" in f for f in report.findings))

    def test_worker_path_traversal_guard(self) -> None:
        """A worker must refuse an input path that escapes its scratch dir."""
        from nexus_ai_agent.creative.composition.workers import _safe_resolve, WorkerError
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self.assertRaises(WorkerError):
                _safe_resolve(base, "../../etc/passwd")


if __name__ == "__main__":
    unittest.main(verbosity=2)
