"""Video edit planning with Gemini — creative studio's LLM call site.

W2 (Global LLM Gateway): this module used to own an ``httpx.AsyncClient``, a
hand-built payload, its own error path (``raise_for_status``) and two private
helpers for digging JSON out of a response envelope. All of that now lives in one
place — the gateway and its ``GeminiHttpAdapter`` — and this module keeps only
what is genuinely its own: the prompt, the pydantic schema and the contract that
the answer must satisfy it.

Two details worth naming, because they are the reason the change is worth making:

* The old code called ``VideoEditPlan.model_validate(body)`` on the **raw Gemini
  envelope** first. That could never succeed — the envelope is
  ``{"candidates": [...]}``, not an edit plan — so every call paid for a
  guaranteed-failing validation before falling through to the real parse. The
  gateway validates the *model text* once, through ``output_validator``.
* A malformed answer is now a typed ``STRUCTURED_OUTPUT_INVALID`` failure with a
  request id, instead of a bare ``ValueError``/``JSONDecodeError`` that told the
  caller nothing about which provider, model or attempt produced it.

Pinned contract (``tests/unit/test_creative_studio.py``,
``tests/unit/test_gemini_key_transport.py``): the wire payload keeps
``generationConfig.temperature == 0`` and
``generationConfig.responseMimeType == "application/json"``, the URL keeps
``generateContent``, and the API key keeps riding in the ``x-goog-api-key``
header rather than the query string.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.llm.gateway.contract import (
    Caller,
    CallerCategory,
    GenerationParams,
    LLMOperation,
    LLMRequest,
)
from nexus_ai_agent.llm.gateway.facade import pydantic_validator
from nexus_ai_agent.llm.gateway.registry import gateway_for_credentials


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


#: Deterministic output: an edit plan is executed by ffmpeg, so a creative
#: temperature here means non-reproducible cuts. Kept at exactly 0 (pre-W2 value).
_PLAN_GENERATION = GenerationParams(temperature=0.0, response_mime_type="application/json")


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


async def analyze_video_with_gemini(video_path_or_url: str, api_key: str) -> VideoEditPlan:
    """Plan edits for *video_path_or_url* through the gateway.

    Raises ``ValueError`` when no key is supplied (a configuration error the
    caller can act on) and a typed
    :class:`~nexus_ai_agent.llm.errors.LLMError` for every provider-side
    failure — including ``STRUCTURED_OUTPUT_INVALID`` when the model's answer
    does not satisfy :class:`VideoEditPlan`.
    """

    if not api_key:
        raise ValueError("Gemini API key is required for creative video analysis")

    settings = get_settings()
    model = settings.gemini_model
    gateway = gateway_for_credentials(api_key, model)
    request = LLMRequest(
        caller=Caller(category=CallerCategory.CREATIVE, name="creative.video_director"),
        purpose="video-edit-plan",
        operation=LLMOperation.CHAT,
        prompt=_build_prompt(video_path_or_url),
        provider="gemini",
        model=model,
        # A creative job must not be silently planned by a different provider or
        # by a locally faked answer: the plan is executed against real media.
        allow_fallback=False,
        generation=_PLAN_GENERATION,
        output_validator=pydantic_validator(VideoEditPlan),
    )
    response = await gateway.execute(request)
    structured = response.structured
    if isinstance(structured, VideoEditPlan):
        return structured
    # Defensive: a validator that returned something else is a programming error
    # in the validator, not a provider condition. Say so precisely.
    raise TypeError(
        f"video edit plan validator returned {type(structured).__name__} instead of VideoEditPlan"
    )
