"""Numerical postcondition helpers; UNKNOWN is always a failed verification."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from .contracts import Continuity, KernelError


def continuity_gate(result: Continuity) -> bool:
    return result is Continuity.OK


def gradient_correlation(reference: NDArray[np.float64], candidate: NDArray[np.float64]) -> float:
    if reference.shape != candidate.shape or reference.size == 0:
        raise KernelError("frames must have identical non-empty shape")
    a, b = np.gradient(reference.astype(float)), np.gradient(candidate.astype(float))
    av, bv = np.concatenate([v.ravel() for v in a]), np.concatenate([v.ravel() for v in b])
    denom = float(np.linalg.norm(av - av.mean()) * np.linalg.norm(bv - bv.mean()))
    if denom == 0:
        return 1.0 if np.array_equal(reference, candidate) else 0.0
    return float(np.dot(av - av.mean(), bv - bv.mean()) / denom)


def verify_structure(
    reference: NDArray[np.float64], candidate: NDArray[np.float64], threshold: float = 0.985
) -> None:
    score = gradient_correlation(reference, candidate)
    if score < threshold:
        raise KernelError(f"structural verification failed: {score:.6f} < {threshold}")


def _sobel_magnitude(rgb: NDArray[np.float64]) -> NDArray[np.float64]:
    luminance = rgb @ np.array([0.2126, 0.7152, 0.0722])
    padded = np.pad(luminance, 1, mode="edge")
    gx = (
        padded[:-2, 2:]
        + 2 * padded[1:-1, 2:]
        + padded[2:, 2:]
        - padded[:-2, :-2]
        - 2 * padded[1:-1, :-2]
        - padded[2:, :-2]
    )
    gy = (
        padded[2:, :-2]
        + 2 * padded[2:, 1:-1]
        + padded[2:, 2:]
        - padded[:-2, :-2]
        - 2 * padded[:-2, 1:-1]
        - padded[:-2, 2:]
    )
    magnitude: NDArray[np.float64] = np.asarray(np.hypot(gx, gy), dtype=np.float64)
    return magnitude


def verify_persisted_relighting(
    source_rgb: NDArray[np.float64],
    relit_rgb: NDArray[np.float64],
    target_ref_rgb: NDArray[np.float64],
    output_path: str | Path,
    threshold: float = 0.985,
) -> tuple[str, float, float, float]:
    """Persist, independently decode, then enforce color and structure gates."""
    from .color_science import delta_e_2000, read_rgb8_png, rgb_to_lab, write_rgb8_png

    destination = Path(output_path)
    staged = destination.with_name(destination.name + ".staged")
    try:
        quantized = np.clip(np.rint(relit_rgb * 255), 0, 255).astype(np.uint8)
        write_rgb8_png(staged, quantized)
        os.replace(staged, destination)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        decoded = read_rgb8_png(destination).astype(np.float64) / 255.0
        if source_rgb.shape != decoded.shape or target_ref_rgb.shape != decoded.shape:
            raise KernelError("source, target, and persisted frames must have matching shapes")
        before = float(np.mean(delta_e_2000(rgb_to_lab(source_rgb), rgb_to_lab(target_ref_rgb))))
        after = float(np.mean(delta_e_2000(rgb_to_lab(decoded), rgb_to_lab(target_ref_rgb))))
        if not after < before:
            raise KernelError("persisted frame did not converge perceptually toward target")
        source_gradient = _sobel_magnitude(source_rgb).ravel()
        decoded_gradient = _sobel_magnitude(decoded).ravel()
        centered_source = source_gradient - source_gradient.mean()
        centered_decoded = decoded_gradient - decoded_gradient.mean()
        denominator = np.sqrt(
            float(np.sum(centered_source**2) * np.sum(centered_decoded**2)) + 1e-12
        )
        score = float(np.sum(centered_source * centered_decoded) / denominator)
        if score < threshold:
            raise KernelError(f"persisted structure lock failed: {score:.6f} < {threshold}")
        return digest, before, after, score
    except Exception:
        staged.unlink(missing_ok=True)
        raise
