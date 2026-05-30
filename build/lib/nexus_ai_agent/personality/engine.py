from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PersonalityVector:
    openness: float = 0.7
    conscientiousness: float = 0.8
    extraversion: float = 0.6
    agreeableness: float = 0.9
    neuroticism: float = 0.2
    humor_level: float = 0.5
    formality: float = 0.4
    verbosity: float = 0.6


@dataclass
class EmotionalState:
    valence: float = 0.5
    arousal: float = 0.5
    dominance: float = 0.5
    trust: float = 0.7
    engagement: float = 0.6


PERSONAS: dict[str, PersonalityVector] = {
    "phi": PersonalityVector(
        conscientiousness=0.95,
        formality=0.9,
        humor_level=0.2,
        verbosity=0.4,
        extraversion=0.3,
    ),
    "qwen": PersonalityVector(
        openness=0.95,
        humor_level=0.8,
        formality=0.2,
        verbosity=0.9,
        extraversion=0.8,
    ),
    "gemma": PersonalityVector(
        agreeableness=0.95,
        extraversion=0.9,
        humor_level=0.7,
        formality=0.3,
        verbosity=0.75,
    ),
}

_POS = [
    "thank",
    "great",
    "love",
    "amazing",
    "awesome",
    "good",
    "nice",
    "please",
    "wonderful",
    "helpful",
]
_NEG = [
    "bad",
    "wrong",
    "hate",
    "terrible",
    "stupid",
    "useless",
    "broken",
    "fail",
    "horrible",
    "awful",
]
_EXC = ["!", "wow", "omg", "incredible", "fantastic"]


class PersonalityEngine:
    def __init__(
        self,
        persona: str = "gemma",
        state_path: str | None = None,
    ) -> None:
        self.persona = persona
        self.pv = PERSONAS.get(persona, PERSONAS["gemma"])
        self.es = EmotionalState()
        self._state_path = state_path
        if state_path:
            self._load()

    def build_system_prompt(
        self,
        base: str,
        memory_context: str = "",
    ) -> str:
        p, e = self.pv, self.es
        tone: list[str] = []
        if p.formality > 0.7:
            tone.append("formal and precise")
        elif p.formality < 0.3:
            tone.append("casual and friendly")
        if p.humor_level > 0.6:
            tone.append("occasionally witty")
        if p.verbosity < 0.3:
            tone.append("very concise")
        elif p.verbosity > 0.8:
            tone.append("thorough with examples")
        if e.valence > 0.7:
            tone.append("upbeat and positive")
        elif e.valence < 0.3:
            tone.append("calm and measured")
        mood = ", ".join(tone) if tone else "helpful"
        mem = f"\nMemory:\n{memory_context}" if memory_context else ""
        return f"You are NEXUS ({self.persona} core). You are {mood}. {base}{mem}"

    def update(self, text: str) -> None:
        t = text.lower()
        if any(w in t for w in _POS):
            self.es.valence = min(1.0, self.es.valence + 0.08)
            self.es.trust = min(1.0, self.es.trust + 0.04)
        if any(w in t for w in _NEG):
            self.es.valence = max(-1.0, self.es.valence - 0.08)
        if any(w in t for w in _EXC):
            self.es.arousal = min(1.0, self.es.arousal + 0.08)
        if len(text) > 150:
            self.es.engagement = min(1.0, self.es.engagement + 0.05)
        self._save()

    def style_hint(self) -> str:
        h: list[str] = []
        if self.pv.verbosity < 0.3:
            h.append("Reply in 1-2 sentences.")
        elif self.pv.verbosity > 0.8:
            h.append("Be thorough.")
        if self.pv.humor_level > 0.7:
            h.append("Light wit welcome.")
        if self.pv.formality > 0.8:
            h.append("Professional tone only.")
        return " ".join(h)

    def status(self) -> str:
        e = self.es
        return (
            f"Core: {self.persona} | "
            f"Mood: {e.valence:.2f} | "
            f"Trust: {e.trust:.2f} | "
            f"Engaged: {e.engagement:.2f}"
        )

    def _save(self) -> None:
        if not self._state_path:
            return
        Path(self._state_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self._state_path).write_text(json.dumps(self.es.__dict__, indent=2))

    def _load(self) -> None:
        if self._state_path is None:
            return
        p = Path(self._state_path)
        if not p.exists():
            return
        try:
            self.es = EmotionalState(**json.loads(p.read_text()))
        except Exception:
            pass
