"""Image analysis above the command bus: measurements, labels, optional scoring.

Two providers behind one contract:

``local_heuristic`` (default)
    Pure local measurement with Pillow + numpy: Laplacian-variance sharpness,
    luma balance, orientation and resolution.  No network, no upload, works
    offline and costs nothing.

``gemini``
    Hosted multimodal scoring.  **Fail-closed**: it only runs when the caller
    explicitly allows image upload (``NEXUS_SLIDESHOW_ALLOW_IMAGE_UPLOAD=1``),
    and it uploads downscaled copies (``<= 768 px``), never the originals.
    This matches the pack manifest's declared ``egress_media_optin``
    permission — the pack cannot upload anything on its own.

Whatever the provider returns, the *order and roles* are decided by the pack's
pure ``slideshow.score_images`` operation: a hosted model proposes, the pack
disposes.
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
import numpy as np
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nexus_ai_agent.creative.packs.slideshow.models import (
    AssetEvidence,
    ImageScore,
    SlideshowAnalysis,
)

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_ANALYSIS_EDGE_PX = 768
MAX_IMAGES_PER_REQUEST = 20
DEFAULT_TIMEOUT_SECONDS = 60.0

#: Sharpness normalisation: Laplacian variance of ~900 is a very crisp frame.
SHARPNESS_SCALE = 900.0


class AnalysisError(ValueError):
    """Image analysis failed (unreadable file, provider error, bad payload)."""


class EgressNotAllowedError(AnalysisError):
    """A hosted provider was requested without the explicit media-egress opt-in."""


@dataclass(frozen=True)
class GeminiConfig:
    """Everything the hosted provider needs (no global state)."""

    api_key: str
    model: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_images: int = MAX_IMAGES_PER_REQUEST
    transport: httpx.BaseTransport | None = field(default=None, repr=False)


class _GeminiImageScore(BaseModel):
    model_config = ConfigDict(extra="ignore")

    evidence_id: str
    labels: list[str] = Field(default_factory=list)
    sharpness: float = Field(default=0.5, ge=0.0, le=1.0)
    aesthetic: float = Field(default=0.5, ge=0.0, le=1.0)
    subject: str = ""
    suggested_role: Literal["opener", "body", "climax", "closer"] = "body"


class _GeminiPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    images: list[_GeminiImageScore] = Field(default_factory=list)
    recommended_template_id: str | None = None
    reasoning: str = ""


# ---------------------------------------------------------------------------
# local heuristics
# ---------------------------------------------------------------------------


def _grayscale_array(path: Path | str, *, max_edge: int = 512) -> np.ndarray:
    with Image.open(path) as image:
        gray = image.convert("L")
        if max(gray.size) > max_edge:
            gray = gray.copy()
            gray.thumbnail((max_edge, max_edge))
        return np.asarray(gray, dtype=np.float32)


def sharpness_of(path: Path | str) -> float:
    """Normalised Laplacian variance in ``[0, 1]`` — higher is crisper."""
    array = _grayscale_array(path)
    if array.size < 9:
        return 0.0
    laplacian = (
        -4.0 * array
        + np.roll(array, 1, axis=0)
        + np.roll(array, -1, axis=0)
        + np.roll(array, 1, axis=1)
        + np.roll(array, -1, axis=1)
    )
    variance = float(np.var(laplacian))
    return float(np.clip(variance / SHARPNESS_SCALE, 0.0, 1.0))


def _luma_balance(mean_luma: float) -> float:
    """1.0 at a well-exposed mid-tone, dropping off toward black/white clipping."""
    return float(np.clip(1.0 - abs(mean_luma - 0.5) * 2.0 * 0.9, 0.0, 1.0))


def _aspect_score(width: int | None, height: int | None) -> float:
    if not width or not height:
        return 0.5
    ratio = width / float(height)
    for target in (16 / 9, 3 / 2, 4 / 3, 4 / 5, 9 / 16):
        if abs(ratio - target) <= 0.05:
            return 1.0
    return 0.7 if 0.5 <= ratio <= 2.2 else 0.4


def _labels_for(evidence: AssetEvidence, sharpness: float) -> tuple[str, ...]:
    labels: list[str] = []
    if evidence.mean_luma >= 0.62:
        labels.append("bright")
    elif evidence.mean_luma <= 0.35:
        labels.append("dark")
    else:
        labels.append("balanced_light")
    labels.append("sharp" if sharpness >= 0.55 else "soft")
    if evidence.width and evidence.height:
        labels.append("landscape" if evidence.width >= evidence.height else "portrait")
        if evidence.width / max(evidence.height, 1) >= 2.0:
            labels.append("panorama")
    return tuple(labels)


def local_score_sheet(evidences: tuple[AssetEvidence, ...]) -> tuple[ImageScore, ...]:
    """Measure every image locally and return the score sheet."""
    scores: list[ImageScore] = []
    for evidence in evidences:
        if evidence.media_kind != "image":
            continue
        try:
            sharpness = sharpness_of(evidence.path)
        except OSError as exc:
            raise AnalysisError(f"cannot analyse {evidence.path}: {exc}") from exc
        aesthetic = float(
            np.clip(
                0.45 * sharpness
                + 0.35 * _luma_balance(evidence.mean_luma)
                + 0.20 * _aspect_score(evidence.width, evidence.height),
                0.0,
                1.0,
            )
        )
        scores.append(
            ImageScore(
                evidence_id=evidence.evidence_id,
                labels=_labels_for(evidence, sharpness),
                sharpness=round(sharpness, 4),
                aesthetic=round(aesthetic, 4),
                subject="",
                suggested_role="body",
            )
        )
    return tuple(scores)


def local_analysis(evidences: tuple[AssetEvidence, ...]) -> SlideshowAnalysis:
    """Local-only analysis: scores plus the pack-computed order (see service)."""
    scores = local_score_sheet(evidences)
    return SlideshowAnalysis(
        ordered_evidence_ids=tuple(score.evidence_id for score in scores),
        scores=scores,
        reasoning="local heuristics: Laplacian sharpness + luma balance + aspect",
        source="local_heuristic",
    )


# ---------------------------------------------------------------------------
# hosted provider (explicit opt-in)
# ---------------------------------------------------------------------------


def _inline_image(path: Path | str) -> dict[str, Any] | None:
    try:
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            if max(rgb.size) > MAX_ANALYSIS_EDGE_PX:
                rgb = rgb.copy()
                rgb.thumbnail((MAX_ANALYSIS_EDGE_PX, MAX_ANALYSIS_EDGE_PX))
            buffer = io.BytesIO()
            rgb.save(buffer, format="JPEG", quality=85)
    except OSError:
        return None
    return {
        "inline_data": {
            "mime_type": "image/jpeg",
            "data": base64.b64encode(buffer.getvalue()).decode("ascii"),
        }
    }


def _prompt(evidences: tuple[AssetEvidence, ...]) -> str:
    ids = ", ".join(f'"{evidence.evidence_id}"' for evidence in evidences)
    return (
        "You are the image curator for a slideshow editor. For every attached photo, "
        f"return one JSON object with the exact evidence_id from this list: {ids}.\n"
        "Fields per image: evidence_id (string, must match the list), labels (array of short "
        "english tags), sharpness (0-1, technical quality), aesthetic (0-1, visual appeal), "
        "subject (short english phrase), suggested_role (one of opener|body|climax|closer).\n"
        "Also return recommended_template_id (one of the tone templates you judge fitting, or "
        "null), reasoning (one short english sentence), and order the images array in the "
        "sequence you would cut them.\n"
        'Answer with JSON only: {"images": [...], "recommended_template_id": ..., '
        '"reasoning": ...}'
    )


def analyze_with_gemini(
    evidences: tuple[AssetEvidence, ...],
    *,
    config: GeminiConfig,
    allow_upload: bool,
) -> SlideshowAnalysis:
    """Ask Gemini for a score sheet (requires the explicit egress opt-in)."""
    if not allow_upload:
        raise EgressNotAllowedError(
            "hosted image analysis is disabled: set NEXUS_SLIDESHOW_ALLOW_IMAGE_UPLOAD=1 "
            "(or pass --allow-image-upload) to send downscaled copies to the provider"
        )
    images = [evidence for evidence in evidences if evidence.media_kind == "image"]
    if not images:
        raise AnalysisError("no images to analyse")
    selected = images[: config.max_images]
    parts: list[dict[str, Any]] = [{"text": _prompt(tuple(selected))}]
    for evidence in selected:
        inline = _inline_image(evidence.path)
        if inline is not None:
            parts.append(inline)
    if len(parts) == 1:
        raise AnalysisError("none of the selected images could be encoded for upload")

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    url = GEMINI_ENDPOINT.format(model=config.model)
    try:
        with httpx.Client(timeout=config.timeout_seconds, transport=config.transport) as client:
            response = client.post(url, json=payload, headers={"x-goog-api-key": config.api_key})
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPError as exc:
        raise AnalysisError(f"gemini request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AnalysisError(f"gemini returned a non-JSON envelope: {exc}") from exc

    text = _extract_text(body)
    if text is None:
        raise AnalysisError("gemini response contained no text part")
    try:
        parsed = _GeminiPayload.model_validate_json(text)
    except ValidationError as exc:
        raise AnalysisError(f"gemini score sheet failed validation: {exc}") from exc

    known = {evidence.evidence_id for evidence in selected}
    scores: list[ImageScore] = []
    ordered: list[str] = []
    for item in parsed.images:
        if item.evidence_id not in known:
            continue
        scores.append(
            ImageScore(
                evidence_id=item.evidence_id,
                labels=tuple(item.labels),
                sharpness=item.sharpness,
                aesthetic=item.aesthetic,
                subject=item.subject,
                suggested_role=item.suggested_role,
            )
        )
        ordered.append(item.evidence_id)
    if not scores:
        raise AnalysisError("gemini score sheet did not reference any known image")
    missing = [evidence_id for evidence_id in known if evidence_id not in set(ordered)]
    ordered.extend(sorted(missing))
    return SlideshowAnalysis(
        ordered_evidence_ids=tuple(ordered),
        scores=tuple(scores),
        recommended_template_id=parsed.recommended_template_id,
        reasoning=parsed.reasoning,
        source="gemini",
    )


def _extract_text(body: dict[str, Any]) -> str | None:
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    parts = candidates[0].get("content", {}).get("parts", [])
    for part in parts:
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return None


def analyze(
    evidences: tuple[AssetEvidence, ...],
    *,
    provider: Literal["local", "gemini"] = "local",
    api_key: str | None = None,
    model: str = "gemini-2.0-flash",
    allow_upload: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> SlideshowAnalysis:
    """Run the requested provider (``gemini`` falls back to local only on request)."""
    if provider == "gemini":
        if not allow_upload:
            raise EgressNotAllowedError(
                "hosted image analysis is disabled: set NEXUS_SLIDESHOW_ALLOW_IMAGE_UPLOAD=1 "
                "(or pass --allow-image-upload) to send downscaled copies to the provider"
            )
        if not api_key:
            raise AnalysisError("gemini provider selected but no API key is configured")
        return analyze_with_gemini(
            evidences,
            config=GeminiConfig(api_key=api_key, model=model, transport=transport),
            allow_upload=allow_upload,
        )
    return local_analysis(evidences)
