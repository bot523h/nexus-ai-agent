from __future__ import annotations

import json
import logging
import time
from datetime import datetime

from sqlmodel import select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.llm.errors import LLMError
from nexus_ai_agent.llm.gateway.contract import (
    Caller,
    CallerCategory,
    GenerationParams,
    LLMOperation,
    LLMRequest,
)
from nexus_ai_agent.llm.gateway.facade import json_object_validator
from nexus_ai_agent.llm.gemini_provider import GeminiProvider
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import UserMemory

logger = logging.getLogger(__name__)


def _parse_json_object(text: str) -> dict:
    """Parse a model answer into a JSON object — strictly, or raise.

    Accepts a bare object and a fenced code block (models wrap JSON in ```
    often enough that refusing them would discard valid answers). Rejects
    anything that is not an object: returning ``{}`` for nonsense would make an
    unparseable answer indistinguishable from "no new information found".
    """

    candidate = (text or "").strip()
    if candidate.startswith("```"):
        candidate = _strip_code_fences(candidate)
    payload = json.loads(candidate)
    if not isinstance(payload, dict):
        raise TypeError("expected a JSON object from the extractor")
    return payload


def _strip_code_fences(text: str) -> str:
    """Remove a leading/trailing ``` fence and an optional language tag."""

    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline == -1:
            return ""
        stripped = stripped[first_newline + 1 :]
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    return stripped.strip()


# Consent states for ``UserMemory.ai_memory_consent`` (tri-state).
CONSENT_UNSET = "unset"
CONSENT_GRANTED = "granted"
CONSENT_DENIED = "denied"

# Reasons ``update_from_message`` may skip the LLM egress.  Anything other
# than ``EGRESSED`` is a hard no-egress outcome.
EGRESSED = "egressed"
SKIP_DISABLED = "disabled"
SKIP_NOT_CONSENTED = "not_consented"
SKIP_RATE_LIMITED = "rate_limited"


class AIMemoryEngine:
    """Engine for analyzing user interactions and maintaining long-term memory.

    **P0-7 — LLM egress consent gate.** Every call that would send user
    message text to the external LLM (:meth:`update_from_message`) passes a
    three-stage gate, in order:

    1. *Global switch* — ``settings.ai_memory_enabled`` (env kill switch).
    2. *Per-user consent* — ``UserMemory.ai_memory_consent`` must be exactly
       ``"granted"``; unset or denied users never egress (default-deny).
    3. *Rate limit* — at most one egress per user per
       ``settings.ai_memory_min_egress_seconds`` (in-process, per user).

    The gate is enforced *inside* the engine, not at the call sites, so no
    future caller can bypass it. Local-only operations (``get_context``,
    ``forget_user``) never touch the network.
    """

    def __init__(
        self,
        gemini_provider: GeminiProvider | None = None,
        *,
        min_egress_seconds: float | None = None,
    ) -> None:
        settings = get_settings()
        self.gemini = gemini_provider or GeminiProvider(api_key=settings.gemini_api_key or "")
        self.min_egress_seconds = (
            float(settings.ai_memory_min_egress_seconds)
            if min_egress_seconds is None
            else float(min_egress_seconds)
        )
        # In-process per-user egress timestamps (rate limit).  Lost on restart,
        # which only relaxes the limit — it can never admit a non-consenting user.
        self._last_egress: dict[int, float] = {}

    # ── Consent state (local DB only — never egresses) ───────────────

    async def get_consent(self, user_id: int) -> str:
        """Return ``"unset"`` | ``"granted"`` | ``"denied"`` for *user_id*."""
        async with get_session() as session:
            stmt = select(UserMemory).where(UserMemory.user_id == user_id)
            memory = (await session.execute(stmt)).scalar_one_or_none()
            if memory is None or memory.ai_memory_consent is None:
                return CONSENT_UNSET
            return memory.ai_memory_consent

    async def has_been_prompted(self, user_id: int) -> bool:
        """True if the one-time consent question was already shown."""
        async with get_session() as session:
            stmt = select(UserMemory).where(UserMemory.user_id == user_id)
            memory = (await session.execute(stmt)).scalar_one_or_none()
            return bool(memory is not None and memory.ai_memory_prompted)

    async def set_consent(self, user_id: int, granted: bool) -> str:
        """Persist the user's vote.  Returns the stored state string."""
        state = CONSENT_GRANTED if granted else CONSENT_DENIED
        async with get_session() as session:
            stmt = select(UserMemory).where(UserMemory.user_id == user_id)
            memory = (await session.execute(stmt)).scalar_one_or_none()
            if memory is None:
                memory = UserMemory(
                    user_id=user_id,
                    last_updated=datetime.utcnow(),
                    ai_memory_consent=state,
                    ai_memory_consent_at=datetime.utcnow(),
                    ai_memory_prompted=True,
                )
            else:
                memory.ai_memory_consent = state
                memory.ai_memory_consent_at = datetime.utcnow()
                memory.ai_memory_prompted = True
            session.add(memory)
            await session.commit()
        return state

    async def mark_prompted(self, user_id: int) -> None:
        """Record that the consent question was shown (idempotent)."""
        async with get_session() as session:
            stmt = select(UserMemory).where(UserMemory.user_id == user_id)
            memory = (await session.execute(stmt)).scalar_one_or_none()
            if memory is None:
                memory = UserMemory(
                    user_id=user_id,
                    last_updated=datetime.utcnow(),
                    ai_memory_prompted=True,
                )
            elif not memory.ai_memory_prompted:
                memory.ai_memory_prompted = True
            session.add(memory)
            await session.commit()

    # ── The gated egress path ─────────────────────────────────────────

    async def update_from_message(self, user_id: int, message: str) -> str:
        """Extract important user information from a message.

        **Never** sends *message* to the LLM unless the P0-7 gate passes.
        Returns the outcome: :data:`EGRESSED` or one of the skip reasons.
        """
        settings = get_settings()
        if not settings.ai_memory_enabled:
            return SKIP_DISABLED

        consent = await self.get_consent(user_id)
        if consent != CONSENT_GRANTED:
            return SKIP_NOT_CONSENTED

        now = time.monotonic()
        last = self._last_egress.get(user_id)
        if last is not None and (now - last) < self.min_egress_seconds:
            return SKIP_RATE_LIMITED
        self._last_egress[user_id] = now

        prompt = f"""
        Analyze the following message from a user and extract key personal information.
        Information to look for: Name, Interests, Occupation, Personality traits.

        User Message: "{message}"

        Return ONLY a JSON object with these keys:
        name, interests (list), occupation, personality_tags (list).
        If no new information is found, return an empty JSON object {{}}.
        """

        system = "You are a personal information extractor."
        try:
            data = await self._extract(prompt, system, user_id=user_id)
        except LLMError as exc:
            # Typed: the gateway already classified this from the status code and
            # the provider's structured error body. Log the kind, not a message
            # dump that could carry prompt text (this prompt contains user data).
            logger.warning(
                "ai_memory_llm_failure",
                extra={
                    "error_kind": exc.kind.value,
                    "status": exc.status_code,
                    "request_id": exc.request_id,
                },
            )
            return EGRESSED
        except (ValueError, TypeError) as exc:
            # The model answered, but not with the JSON object we asked for.
            # That is a contract failure, not a reason to guess: nothing is
            # written to the database from a half-parsed answer.
            logger.warning(
                "ai_memory_unparseable_response",
                extra={"error": type(exc).__name__, "user_id": user_id},
            )
            return EGRESSED
        except Exception:
            logger.exception("ai_memory_unexpected_failure", extra={"user_id": user_id})
            return EGRESSED

        if data:
            await self._save_memory(user_id, data)
        return EGRESSED

    async def _extract(self, prompt: str, system: str, *, user_id: int) -> dict:
        """Run the extraction and return the validated JSON object (or ``{}``).

        Two paths, one contract:

        * **typed** — when the provider exposes its gateway (the real
          ``GeminiProvider`` does), the request goes through it with an
          ``output_validator`` and ``responseMimeType=application/json``. The
          gateway validates the answer inside the authority, so a malformed
          reply becomes a typed ``STRUCTURED_OUTPUT_INVALID`` instead of a
          half-parsed dict, and the attempt is observable like every other.
        * **compatibility** — an injected double with only ``generate()``
          (used by the consent-gate tests) still works. Its answer is parsed by
          the same strict helper, so both paths reject the same nonsense.

        What is gone from both paths is ``text.find("{")``/``rfind("}")``: that
        pattern accepts any answer containing a brace *anywhere* — including a
        model that apologises in prose and quotes a stray ``{`` — and then hands
        a truncated slice to ``json.loads``.
        """

        gateway = getattr(self.gemini, "gateway", None)
        if gateway is not None:
            request = LLMRequest(
                caller=Caller(category=CallerCategory.MEMORY, name="features.ai_memory"),
                purpose="memory-extract",
                operation=LLMOperation.CHAT,
                prompt=prompt,
                system=system,
                provider="gemini",
                # Personal data extraction must not be answered by a locally
                # faked provider: a fabricated "name" would be written to the
                # user's permanent memory record (LAW 8, LAW 11).
                allow_fallback=False,
                generation=GenerationParams(
                    response_mime_type="application/json",
                    temperature=0.0,
                ),
                output_validator=json_object_validator(),
                metadata={"user_id": str(user_id)},
            )
            response = await gateway.execute(request)
            structured = response.structured
            if isinstance(structured, dict):
                return structured
            return _parse_json_object(response.text)

        response_text = await self.gemini.generate(prompt=prompt, system=system)
        return _parse_json_object(response_text)

    async def _save_memory(self, user_id: int, data: dict) -> None:
        """Merge new data into persistent UserMemory."""
        async with get_session() as session:
            stmt = select(UserMemory).where(UserMemory.user_id == user_id)
            memory = (await session.execute(stmt)).scalar_one_or_none()

            if not memory:
                memory = UserMemory(user_id=user_id, last_updated=datetime.utcnow())

            if data.get("name"):
                memory.name = data["name"]
            if data.get("occupation"):
                memory.occupation = data["occupation"]

            if data.get("interests"):
                existing_interests = json.loads(memory.interests)
                new_interests = list(set(existing_interests + data["interests"]))
                memory.interests = json.dumps(new_interests)

            if data.get("personality_tags"):
                existing_tags = json.loads(memory.personality_tags)
                new_tags = list(set(existing_tags + data["personality_tags"]))
                memory.personality_tags = json.dumps(new_tags)

            memory.last_updated = datetime.utcnow()
            session.add(memory)
            await session.commit()

    async def get_context(self, user_id: int) -> str:
        """Generate a context string for system prompt injection.

        Local read-only; never egresses.
        """
        async with get_session() as session:
            stmt = select(UserMemory).where(UserMemory.user_id == user_id)
            memory = (await session.execute(stmt)).scalar_one_or_none()

            if not memory:
                return ""

            parts = []
            if memory.name:
                parts.append(f"User Name: {memory.name}")
            if memory.occupation:
                parts.append(f"Occupation: {memory.occupation}")

            interests = json.loads(memory.interests)
            if interests:
                parts.append(f"Interests: {', '.join(interests)}")

            tags = json.loads(memory.personality_tags)
            if tags:
                parts.append(f"Personality: {', '.join(tags)}")

            return " | ".join(parts)

    async def forget_user(self, user_id: int) -> None:
        """Wipe all memory **and the consent record** for a user.

        Forgetting implies revoking: the row (including ``ai_memory_consent``)
        is deleted, so any future egress requires a fresh explicit vote.
        """
        self._last_egress.pop(user_id, None)
        async with get_session() as session:
            stmt = select(UserMemory).where(UserMemory.user_id == user_id)
            memory = (await session.execute(stmt)).scalar_one_or_none()
            if memory:
                await session.delete(memory)
                await session.commit()
