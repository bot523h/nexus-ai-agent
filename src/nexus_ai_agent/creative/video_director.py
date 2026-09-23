from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import BaseModel, Field

from nexus_ai_agent.config.settings import get_settings


class Cut(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)


class Zoom(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    scale: float = Field(gt=0)


class Caption(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str


class VideoEditPlan(BaseModel):
    cuts: list[Cut]
    zooms: list[Zoom]
    captions: list[Caption]
    reasoning: str


def _build_prompt(video_path_or_url: str) -> str:
    return (
        "You are a video editing planner. Analyze the provided video reference and "
        "return JSON only that matches this schema exactly:\n"
        "{\n"
        '  "cuts": [{"start": 0.0, "end": 1.0}],\n'
        '  "zooms": [{"start": 0.0, "end": 1.0, "scale": 1.0}],\n'
        '  "captions": [{"start": 0.0, "end": 1.0, "text": "caption"}],\n'
        '  "reasoning": "short explanation"\n'
        "}\n"
        "Rules:\n"
        "- Use seconds as floats.\n"
        "- Keep cuts non-overlapping and ordered.\n"
        "- Keep captions concise.\n"
        "- If no zooms or captions are needed, return empty arrays.\n"
        f"Video reference: {video_path_or_url}"
    )


def _strip_code_fences(payload: str) -> str:
    text = payload.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
    return text


def _extract_response_text(body: dict[str, Any]) -> str:
    candidates = body.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            parts = content.get("parts")
            if not isinstance(parts, list):
                continue
            for part in parts:
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    return text
    raise ValueError("Gemini response did not contain JSON text")


async def analyze_video_with_gemini(video_path_or_url: str, api_key: str) -> VideoEditPlan:
    if not api_key:
        raise ValueError("Gemini API key is required for creative video analysis")

    settings = get_settings()
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent"
    )
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": _build_prompt(video_path_or_url)}],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, json=payload, headers={"x-goog-api-key": api_key})
        if response.is_error:
            response.raise_for_status()
        body = response.json()

    try:
        return VideoEditPlan.model_validate(body)
    except Exception:
        text = _extract_response_text(body)
        parsed = json.loads(_strip_code_fences(text))
        return VideoEditPlan.model_validate(parsed)
