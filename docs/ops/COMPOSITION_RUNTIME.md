# NAGAR Creative Capability Composition — Install & Run Guide

This guide covers the **composition layer**
(`src/nexus_ai_agent/creative/composition/`), the prototype that proves the
NAGAR thesis: build new capabilities by composing independent media
capabilities, execute them for real, and verify the output independently.

## 1. Prerequisites

| Tool | Version checked | Purpose |
|---|---|---|
| Python | 3.14.8 | runtime |
| pydantic | >= 2.7 | typed contracts |
| Pillow | >= 10 | (transitive, image I/O) |
| FFmpeg | 6.1.1 | video/audio operations |
| ImageMagick | 6.9.12 | image operations |

On Debian/Ubuntu:

```bash
apt-get install -y ffmpeg imagemagick
pip install pydantic pillow
```

The composition layer does **not** require the rest of the NEXUS dependency
tree (langgraph, sqlmodel, etc.) to run -- it is self-contained for the
media-processing path.

## 2. Run the prototype end-to-end

From the repository root:

```bash
# create a test image
convert -size 640x480 gradient:blue-red /tmp/test_input.png

# run the canonical 3-op composition (resize -> grayscale -> image-to-video)
PYTHONPATH=src python3 -c "
from nexus_ai_agent.creative.composition import *

PNG = MediaType(kind='image', codec='png')
VIDEO = MediaType(kind='video', codec='h264', container='mp4')
MIT = LicenseInfo(spdx_id='MIT', source_license='MIT')

registry = {
    'nagar.image.resize': CapabilitySpec(capability_id='nagar.image.resize', version='1.0.0',
        inputs=(CapabilityInput(name='source', media_type=PNG),),
        outputs=(CapabilityOutput(name='resized', media_type=PNG),),
        resources=ResourceRequirements(cpu_threads=1, ram_mb=200, disk_mb=100), license=MIT),
    'nagar.image.grayscale': CapabilitySpec(capability_id='nagar.image.grayscale', version='1.0.0',
        inputs=(CapabilityInput(name='source', media_type=PNG),),
        outputs=(CapabilityOutput(name='grayscale', media_type=PNG),),
        resources=ResourceRequirements(cpu_threads=1, ram_mb=150, disk_mb=100), license=MIT),
    'nagar.video.from_image': CapabilitySpec(capability_id='nagar.video.from_image', version='1.0.0',
        inputs=(CapabilityInput(name='source', media_type=PNG),),
        outputs=(CapabilityOutput(name='video', media_type=VIDEO),),
        resources=ResourceRequirements(cpu_threads=2, ram_mb=500, disk_mb=200), license=MIT),
}

g = CompositionGraph()
g.add_external_input('image', PNG)
g.add_node(CompositionNode('resize', 'nagar.image.resize', params={'width': 1280, 'height': 720},
    bindings={'source': InputBinding('source', external='image')}))
g.add_node(CompositionNode('gray', 'nagar.image.grayscale',
    bindings={'source': InputBinding('source', node_output=('resize','resized'))}))
g.add_node(CompositionNode('video', 'nagar.video.from_image', params={'fps': 24, 'duration_seconds': 2},
    bindings={'source': InputBinding('source', node_output=('gray','grayscale'))}))
g.add_expected_output('clip', 'video', 'video')

quota = ResourceRequirements(cpu_threads=8, ram_mb=4096, disk_mb=4096)
runtime = CompositionRuntime(registry, policy=RuntimePolicy(quota=quota))
result = runtime.execute(g, {'image': '/tmp/test_input.png'})

print('derived capability:', result.derived_spec.capability_id)
print('output:', result.outputs['clip'])
print('verified:', result.artifacts['clip'].verification.verified)
"
```

Expected output: a real `video.mp4` (h264, 1280x720) and `verified: True`.

## 3. Run the tests

```bash
PYTHONPATH=src python3 -m unittest tests.unit.test_composition_runtime -v
```

13 tests, covering real execution, invalid-input refusal, policy refusal,
contract conformance, reproducibility, tamper detection, and path-traversal
guard.

## 4. Run the benchmark

```bash
PYTHONPATH=src python3 scripts/bench_composition.py --iterations 3
```

Reports compile time, per-node execution time, peak RSS, output size, and
independent verification status.

## 5. Verify the output independently

```bash
ffprobe -v error -show_entries stream=codec_name,width,height -of default=nw=1 \
  $(find /tmp -name 'video.mp4' -newer /tmp/test_input.png | head -1)
```

Expect `codec_name=h264`, `width=1280`, `height=720`.
