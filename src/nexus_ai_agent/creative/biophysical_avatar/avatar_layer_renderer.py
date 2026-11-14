"""Memory-bounded RGBA renderer with perspective, depth tests, and PNG/FFmpeg IO."""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import numpy as np
from numpy.typing import NDArray

from nexus_ai_agent.creative.biophysical_avatar.craniofacial_body_manifold import (
    PARENT_INDICES,
    SkeletonPose,
)

__all__ = [
    "MAX_RENDER_PIXELS",
    "AvatarLayerRenderer",
    "AvatarScene",
    "Camera",
    "LayeredFrame",
    "RGBAFrame",
    "encode_sequence",
    "estimate_frame_memory_bytes",
    "face_grid_triangles",
]

UInt8Array = NDArray[np.uint8]
FloatArray = NDArray[np.float64]
DepthArray = NDArray[np.float32]
MAX_RENDER_PIXELS = 1_000_000


def _points3(name: str, values: Sequence[Sequence[float]] | NDArray[np.float64]) -> FloatArray:
    try:
        points = np.asarray(values, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a numeric (n, 3) array") from exc
    if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
        raise ValueError(f"{name} must be a finite (n, 3) array")
    return points


def _triangles(
    name: str, values: Sequence[Sequence[int]], vertex_count: int
) -> tuple[tuple[int, int, int], ...]:
    result: list[tuple[int, int, int]] = []
    for triangle in values:
        if len(triangle) != 3:
            raise ValueError(f"{name} triangles must have three indices")
        indices = (int(triangle[0]), int(triangle[1]), int(triangle[2]))
        if any(index < 0 or index >= vertex_count for index in indices):
            raise ValueError(f"{name} triangle index is out of bounds")
        result.append(indices)
    return tuple(result)


def estimate_frame_memory_bytes(width: int, height: int, *, layers: int = 3) -> int:
    """Conservative high-water estimate including tiled raster scratch space."""
    if width < 1 or height < 1 or layers < 1:
        raise ValueError("width, height, and layers must be positive")
    return width * height * (layers * (4 + 4) + 8 + 16)


@dataclass(frozen=True, slots=True)
class Camera:
    """Perspective camera looking along its local +Z axis; world Z is up."""

    width: int = 640
    height: int = 480
    position: tuple[float, float, float] = (0.0, -4.0, 1.35)
    yaw_radians: float = 0.0
    pitch_radians: float = -0.04
    vertical_fov_degrees: float = 42.0
    near_clip_m: float = 0.05

    def __post_init__(self) -> None:
        if self.width < 1 or self.height < 1:
            raise ValueError("camera dimensions must be positive")
        if len(self.position) != 3 or not all(math.isfinite(value) for value in self.position):
            raise ValueError("camera position must be a finite 3-vector")
        values = (self.yaw_radians, self.pitch_radians, self.vertical_fov_degrees, self.near_clip_m)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("camera parameters must be finite")
        if not 1.0 <= self.vertical_fov_degrees < 179.0 or self.near_clip_m <= 0.0:
            raise ValueError("camera field of view or near clip is invalid")

    @property
    def focal_length_pixels(self) -> float:
        return self.height / (2.0 * math.tan(math.radians(self.vertical_fov_degrees) * 0.5))

    def camera_coordinates(
        self, points: Sequence[Sequence[float]] | NDArray[np.float64]
    ) -> FloatArray:
        world = _points3("points", points)
        yaw = self.yaw_radians
        pitch = self.pitch_radians
        forward = np.asarray(
            (math.sin(yaw) * math.cos(pitch), math.cos(yaw) * math.cos(pitch), math.sin(pitch)),
            dtype=np.float64,
        )
        right = np.asarray((math.cos(yaw), -math.sin(yaw), 0.0), dtype=np.float64)
        up = np.cross(right, forward)
        up /= np.linalg.norm(up)
        relative = world - np.asarray(self.position, dtype=np.float64)
        return np.column_stack((relative @ right, relative @ up, relative @ forward))

    def project(
        self,
        points: Sequence[Sequence[float]] | NDArray[np.float64],
    ) -> tuple[FloatArray, FloatArray]:
        """Project world points to pixel coordinates and positive camera depth."""
        camera_points = self.camera_coordinates(points)
        depth = camera_points[:, 2].copy()
        safe_depth = np.maximum(depth, self.near_clip_m)
        focal = self.focal_length_pixels
        pixels = np.column_stack(
            (
                self.width * 0.5 + focal * camera_points[:, 0] / safe_depth,
                self.height * 0.5 - focal * camera_points[:, 1] / safe_depth,
            )
        )
        pixels[depth <= self.near_clip_m] = np.nan
        return pixels, depth


@dataclass(frozen=True, slots=True)
class AvatarScene:
    """One actor's world-space skeleton, face mesh, and garment mesh."""

    skeleton: SkeletonPose | None = None
    face_vertices: FloatArray | None = None
    face_triangles: tuple[tuple[int, int, int], ...] = ()
    cloth_vertices: FloatArray | None = None
    cloth_triangles: tuple[tuple[int, int, int], ...] = ()
    face_color: tuple[int, int, int, int] = (194, 144, 111, 255)
    garment_color: tuple[int, int, int, int] = (78, 88, 106, 255)

    def __post_init__(self) -> None:
        if self.face_vertices is not None:
            face = _points3("face_vertices", self.face_vertices).copy()
            face.flags.writeable = False
            object.__setattr__(self, "face_vertices", face)
            object.__setattr__(
                self,
                "face_triangles",
                _triangles("face", self.face_triangles, face.shape[0]),
            )
        elif self.face_triangles:
            raise ValueError("face_triangles require face_vertices")
        if self.cloth_vertices is not None:
            cloth = _points3("cloth_vertices", self.cloth_vertices).copy()
            cloth.flags.writeable = False
            object.__setattr__(self, "cloth_vertices", cloth)
            object.__setattr__(
                self,
                "cloth_triangles",
                _triangles("cloth", self.cloth_triangles, cloth.shape[0]),
            )
        elif self.cloth_triangles:
            raise ValueError("cloth_triangles require cloth_vertices")
        for name, color in (("face_color", self.face_color), ("garment_color", self.garment_color)):
            if len(color) != 4 or any(channel < 0 or channel > 255 for channel in color):
                raise ValueError(f"{name} must contain four byte channels")


def face_grid_triangles() -> tuple[tuple[int, int, int], ...]:
    """Triangles for the 8x8 procedural face manifold landmark grid."""
    result: list[tuple[int, int, int]] = []
    width = 8
    for row in range(width - 1):
        for column in range(width - 1):
            upper_left = row * width + column
            upper_right = upper_left + 1
            lower_left = upper_left + width
            lower_right = lower_left + 1
            result.extend(
                ((upper_left, upper_right, lower_right), (upper_left, lower_right, lower_left))
            )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class RGBAFrame:
    """A transparent RGBA layer (or final composite) with its depth buffer."""

    rgba: UInt8Array
    depth: DepthArray

    def __post_init__(self) -> None:
        if self.rgba.ndim != 3 or self.rgba.shape[2] != 4:
            raise ValueError("rgba must have shape (height, width, 4)")
        if self.depth.shape != self.rgba.shape[:2]:
            raise ValueError("depth dimensions must match rgba")
        if self.rgba.dtype != np.uint8 or self.depth.dtype != np.float32:
            raise ValueError("RGBAFrame requires uint8 color and float32 depth")
        self.rgba.flags.writeable = False
        self.depth.flags.writeable = False

    @property
    def width(self) -> int:
        return int(self.rgba.shape[1])

    @property
    def height(self) -> int:
        return int(self.rgba.shape[0])

    def write_png(self, path: str | Path) -> Path:
        """Write an 8-bit RGBA PNG using only the Python standard library."""
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        height, width, _ = self.rgba.shape
        raw = bytearray()
        for row in self.rgba:
            raw.append(0)
            raw.extend(row.tobytes())
        ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
        payload = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
        payload += _png_chunk(b"IDAT", zlib.compress(bytes(raw), level=6))
        payload += _png_chunk(b"IEND", b"")
        destination.write_bytes(payload)
        return destination


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


@dataclass(frozen=True, slots=True)
class LayeredFrame:
    """Named RGBA passes plus painter's-Z composite for one timestamp."""

    layers: Mapping[str, RGBAFrame]
    composite: RGBAFrame
    time_seconds: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.time_seconds) or self.time_seconds < 0.0:
            raise ValueError("frame time must be finite and non-negative")
        object.__setattr__(self, "layers", MappingProxyType(dict(self.layers)))

    @property
    def estimated_memory_bytes(self) -> int:
        return estimate_frame_memory_bytes(self.composite.width, self.composite.height)


def _transparent_layer(width: int, height: int) -> tuple[UInt8Array, DepthArray]:
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    depth = np.full((height, width), np.inf, dtype=np.float32)
    return rgba, depth


def _put_fragment(
    rgba: UInt8Array,
    depth_buffer: DepthArray,
    x: int,
    y: int,
    z: float,
    color: tuple[int, int, int, int],
) -> None:
    height, width, _ = rgba.shape
    if 0 <= x < width and 0 <= y < height and z < float(depth_buffer[y, x]):
        depth_buffer[y, x] = z
        rgba[y, x] = color


def _draw_line(
    rgba: UInt8Array,
    depth_buffer: DepthArray,
    first: FloatArray,
    second: FloatArray,
    camera: Camera,
    color: tuple[int, int, int, int],
    radius: int = 3,
) -> None:
    projected, depths = camera.project(np.vstack((first, second)))
    if not np.isfinite(projected).all() or np.any(depths <= camera.near_clip_m):
        return
    start, end = projected
    steps = max(1, int(math.ceil(float(np.max(np.abs(end - start))))))
    offsets = tuple(
        (dx, dy)
        for dy in range(-radius, radius + 1)
        for dx in range(-radius, radius + 1)
        if dx * dx + dy * dy <= radius * radius
    )
    for step in range(steps + 1):
        amount = step / steps
        point = start + (end - start) * amount
        depth = float(depths[0] + (depths[1] - depths[0]) * amount)
        center_x, center_y = int(round(point[0])), int(round(point[1]))
        for offset_x, offset_y in offsets:
            _put_fragment(
                rgba,
                depth_buffer,
                center_x + offset_x,
                center_y + offset_y,
                depth,
                color,
            )


def _rasterize_triangle(
    rgba: UInt8Array,
    depth_buffer: DepthArray,
    vertices: FloatArray,
    camera: Camera,
    color: tuple[int, int, int, int],
) -> None:
    pixels, depths = camera.project(vertices)
    if not np.isfinite(pixels).all() or np.any(depths <= camera.near_clip_m):
        return
    x_min = max(0, int(math.floor(float(np.min(pixels[:, 0])))))
    x_max = min(rgba.shape[1] - 1, int(math.ceil(float(np.max(pixels[:, 0])))))
    y_min = max(0, int(math.floor(float(np.min(pixels[:, 1])))))
    y_max = min(rgba.shape[0] - 1, int(math.ceil(float(np.max(pixels[:, 1])))))
    if x_min > x_max or y_min > y_max:
        return
    x0, y0 = pixels[0]
    x1, y1 = pixels[1]
    x2, y2 = pixels[2]
    denominator = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
    if abs(float(denominator)) < 1e-12:
        return
    edge_a = vertices[1] - vertices[0]
    edge_b = vertices[2] - vertices[0]
    normal = np.cross(edge_a, edge_b)
    normal_length = float(np.linalg.norm(normal))
    if normal_length <= 1e-12:
        return
    normal /= normal_length
    light = np.asarray((0.36, -0.42, 0.83), dtype=np.float64)
    light /= np.linalg.norm(light)
    view = np.asarray(camera.position, dtype=np.float64) - np.mean(vertices, axis=0)
    view /= max(float(np.linalg.norm(view)), 1e-12)
    lambert = abs(float(np.dot(normal, light)))
    facing = min(max(abs(float(np.dot(normal, view))), 0.0), 1.0)
    fresnel = 0.04 + 0.96 * (1.0 - facing) ** 5
    illumination = min(1.0, max(0.12, 0.20 + 0.72 * lambert + 0.12 * fresnel))
    shaded = tuple(
        max(0, min(255, int(round(channel * illumination)))) for channel in color[:3]
    ) + (color[3],)
    x_coordinates = np.arange(x_min, x_max + 1, dtype=np.float64)
    for tile_start in range(y_min, y_max + 1, 64):
        tile_end = min(tile_start + 63, y_max)
        grid_x, grid_y = np.meshgrid(
            x_coordinates,
            np.arange(tile_start, tile_end + 1, dtype=np.float64),
        )
        weight0 = ((y1 - y2) * (grid_x - x2) + (x2 - x1) * (grid_y - y2)) / denominator
        weight1 = ((y2 - y0) * (grid_x - x2) + (x0 - x2) * (grid_y - y2)) / denominator
        weight2 = 1.0 - weight0 - weight1
        inside = (weight0 >= -1e-8) & (weight1 >= -1e-8) & (weight2 >= -1e-8)
        reciprocal_depth = weight0 / depths[0] + weight1 / depths[1] + weight2 / depths[2]
        triangle_depth = np.divide(
            1.0,
            reciprocal_depth,
            out=np.full_like(reciprocal_depth, np.inf),
            where=reciprocal_depth > 0.0,
        )
        target_depth = depth_buffer[tile_start : tile_end + 1, x_min : x_max + 1]
        visible = inside & (triangle_depth < target_depth)
        if visible.any():
            target_depth[visible] = triangle_depth[visible].astype(np.float32)
            target_rgba = rgba[tile_start : tile_end + 1, x_min : x_max + 1]
            target_rgba[visible] = shaded


def _draw_triangles(
    rgba: UInt8Array,
    depth_buffer: DepthArray,
    vertices: FloatArray,
    triangles: Sequence[tuple[int, int, int]],
    camera: Camera,
    color: tuple[int, int, int, int],
) -> None:
    for triangle in triangles:
        _rasterize_triangle(rgba, depth_buffer, vertices[list(triangle)], camera, color)


def _composite(layers: Mapping[str, tuple[UInt8Array, DepthArray]]) -> RGBAFrame:
    names = tuple(layers)
    first_rgba, first_depth = layers[names[0]]
    closest_depth = first_depth.copy()
    closest_color = first_rgba.copy()
    for name in names[1:]:
        layer_rgba, layer_depth = layers[name]
        visible = (layer_rgba[:, :, 3] > 0) & (layer_depth < closest_depth)
        closest_depth[visible] = layer_depth[visible]
        closest_color[visible] = layer_rgba[visible]
    closest_depth[closest_color[:, :, 3] == 0] = np.inf
    return RGBAFrame(closest_color, closest_depth)


class AvatarLayerRenderer:
    """Project and shade actor parts while keeping a predictable RAM ceiling."""

    def render(
        self,
        scene: AvatarScene,
        camera: Camera,
        *,
        time_seconds: float = 0.0,
    ) -> LayeredFrame:
        if camera.width * camera.height > MAX_RENDER_PIXELS:
            raise ValueError(f"frame exceeds the {MAX_RENDER_PIXELS:,}-pixel memory budget")
        if not math.isfinite(time_seconds) or time_seconds < 0.0:
            raise ValueError("time_seconds must be finite and non-negative")
        layer_buffers = {
            name: _transparent_layer(camera.width, camera.height)
            for name in ("body", "face", "garment")
        }
        if scene.skeleton is not None:
            body_rgba, body_depth = layer_buffers["body"]
            positions = scene.skeleton.positions
            for joint, parent in enumerate(PARENT_INDICES):
                if parent >= 0:
                    _draw_line(
                        body_rgba,
                        body_depth,
                        positions[parent],
                        positions[joint],
                        camera,
                        (57, 99, 157, 255),
                    )
            for point in positions:
                self._draw_point(body_rgba, body_depth, point, camera, (76, 123, 183, 255), 4)
        if scene.face_vertices is not None:
            face_rgba, face_depth = layer_buffers["face"]
            triangles = scene.face_triangles or (
                face_grid_triangles() if scene.face_vertices.shape[0] == 64 else ()
            )
            if triangles:
                _draw_triangles(
                    face_rgba,
                    face_depth,
                    scene.face_vertices,
                    triangles,
                    camera,
                    scene.face_color,
                )
            else:
                for point in scene.face_vertices:
                    self._draw_point(face_rgba, face_depth, point, camera, scene.face_color, 2)
        if scene.cloth_vertices is not None and scene.cloth_triangles:
            cloth_rgba, cloth_depth = layer_buffers["garment"]
            _draw_triangles(
                cloth_rgba,
                cloth_depth,
                scene.cloth_vertices,
                scene.cloth_triangles,
                camera,
                scene.garment_color,
            )
        frames = {name: RGBAFrame(rgba, depth) for name, (rgba, depth) in layer_buffers.items()}
        composite = _composite(layer_buffers)
        return LayeredFrame(frames, composite, time_seconds)

    @staticmethod
    def _draw_point(
        rgba: UInt8Array,
        depth: DepthArray,
        point: Sequence[float],
        camera: Camera,
        color: tuple[int, int, int, int],
        radius: int,
    ) -> None:
        projected, camera_depth = camera.project(np.asarray(point, dtype=np.float64).reshape(1, 3))
        if not np.isfinite(projected[0]).all() or camera_depth[0] <= camera.near_clip_m:
            return
        center_x, center_y = (int(round(value)) for value in projected[0])
        offsets = (
            (dx, dy)
            for dy in range(-radius, radius + 1)
            for dx in range(-radius, radius + 1)
            if dx * dx + dy * dy <= radius * radius
        )
        for dx, dy in offsets:
            _put_fragment(rgba, depth, center_x + dx, center_y + dy, float(camera_depth[0]), color)


def encode_sequence(
    frame_pattern: str | Path,
    output_path: str | Path,
    *,
    fps: float,
    audio_path: str | Path | None = None,
    ffmpeg_binary: str = "ffmpeg",
) -> Path:
    """Mux an RGBA PNG sequence and optional synchronized WAV using FFmpeg."""
    if not math.isfinite(fps) or fps <= 0.0:
        raise ValueError("fps must be finite and positive")
    executable = shutil.which(ffmpeg_binary)
    if executable is None:
        raise RuntimeError(f"FFmpeg executable not found: {ffmpeg_binary}")
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        executable,
        "-y",
        "-framerate",
        str(fps),
        "-i",
        str(frame_pattern),
    ]
    if audio_path is not None:
        command.extend(("-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0", "-shortest"))
    command.extend(("-c:v", "libx264", "-pix_fmt", "yuv420p"))
    if audio_path is not None:
        command.extend(("-c:a", "aac"))
    else:
        command.append("-an")
    command.append(str(destination))
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("FFmpeg failed to encode the avatar sequence") from exc
    return destination
