"""Deterministic bilingual (Persian/English) screenplay parser and timing estimator.

Parses standard Fountain/Hollywood and Persian cinema screenplays into immutable,
typed scene graphs with zero external API calls. Extracts:

* Scene headings (``INT``/``EXT``/``INT_EXT`` in Persian and English), location,
  time of day (``DAY``, ``NIGHT``, ``DAWN``, ``DUSK``, ``GOLDEN_HOUR``), and
  weather condition (``CLEAR``, ``RAIN``, ``FOG``, ``SNOW``, ``DUST``).
* Character dialogue turns (both standard multi-line cue blocks and inline colon
  cues), parenthetical emotional directives (e.g. ``(با خشم)``, ``(زمزمه)``,
  ``(whispering)``), delivery mode (``ON_SCREEN``, ``VOICE_OVER``, ``OFF_SCREEN``),
  and intentional 180-degree axis-jump cues.
* Action/description beats with participating focus characters and tension scores.
* Prosodic duration and pause estimates grounded in Persian and English syllable
  and word counts modulated by emotion-specific speech-rate multipliers.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final

__all__ = [
    "ActionBeat",
    "BASE_EN_SYLLABLES_PER_SEC",
    "BASE_FA_SYLLABLES_PER_SEC",
    "DeliveryMode",
    "DialogueLine",
    "DialogueTiming",
    "EMOTION_PAUSE_MULTIPLIER",
    "EMOTION_SPEECH_RATE",
    "EMOTION_TENSION",
    "EmotionTag",
    "ParsedScene",
    "ParsedScript",
    "SceneElement",
    "SceneHeading",
    "SceneSetting",
    "ScriptLanguage",
    "ScriptParseError",
    "TimeOfDay",
    "WeatherCondition",
    "classify_emotion",
    "count_english_syllables",
    "count_persian_syllables",
    "detect_language",
    "estimate_action_duration",
    "estimate_dialogue_timing",
    "normalize_script_text",
    "parse_bilingual_script",
    "parse_scene_heading",
    "verify_script_provenance",
]


class ScriptParseError(ValueError):
    """Raised when a screenplay input violates structural or domain constraints."""


#: Lowercase-hex SHA-256 spelling, validated before any comparison against untrusted input.
_SHA256_HEX_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class SceneSetting(str, Enum):
    """Interior/exterior camera environment classification."""

    INT = "INT"
    EXT = "EXT"
    INT_EXT = "INT_EXT"


class TimeOfDay(str, Enum):
    """Canonical lighting period extracted from a scene slugline."""

    DAY = "DAY"
    NIGHT = "NIGHT"
    DAWN = "DAWN"
    DUSK = "DUSK"
    GOLDEN_HOUR = "GOLDEN_HOUR"


class WeatherCondition(str, Enum):
    """Canonical atmospheric condition for a scene."""

    CLEAR = "CLEAR"
    RAIN = "RAIN"
    FOG = "FOG"
    SNOW = "SNOW"
    DUST = "DUST"


class ScriptLanguage(str, Enum):
    """Detected script or utterance language."""

    FA = "FA"
    EN = "EN"
    MIXED = "MIXED"


class EmotionTag(str, Enum):
    """Canonical acting emotion inferred from parenthetical directives."""

    NEUTRAL = "NEUTRAL"
    ANGRY = "ANGRY"
    WHISPER = "WHISPER"
    FEARFUL = "FEARFUL"
    SAD = "SAD"
    JOYFUL = "JOYFUL"
    URGENT = "URGENT"
    HESITANT = "HESITANT"
    SARCASTIC = "SARCASTIC"
    RESOLUTE = "RESOLUTE"


class DeliveryMode(str, Enum):
    """Vocal perspective of a dialogue line."""

    ON_SCREEN = "ON_SCREEN"
    VOICE_OVER = "VOICE_OVER"
    OFF_SCREEN = "OFF_SCREEN"


#: Deterministic heuristic estimator (4.4 syl/s) for Persian pre-production speech timing.
BASE_FA_SYLLABLES_PER_SEC: Final[float] = 4.4
"""Deterministic heuristic estimator (4.4 syl/s) for Persian pre-production timing.

Calibrated for zero-API offline storyboard and animatic duration planning rather
than speaker-specific acoustic measurement or corpus-trained prosody prediction.
"""

#: Deterministic heuristic estimator (4.2 syl/s) for English pre-production speech timing.
BASE_EN_SYLLABLES_PER_SEC: Final[float] = 4.2
"""Deterministic heuristic estimator (4.2 syl/s) for English pre-production timing.

Calibrated for zero-API offline storyboard and animatic duration planning rather
than speaker-specific acoustic measurement or corpus-trained prosody prediction.
"""

#: Auxiliary deterministic heuristic word-rate priors (words / second) for pre-production.
_BASE_FA_WORDS_PER_SEC: Final[float] = 2.25
_BASE_EN_WORDS_PER_SEC: Final[float] = 2.50

#: Deterministic heuristic speech-rate multipliers (>1 faster, <1 slower) for pre-production.
EMOTION_SPEECH_RATE: Final[dict[EmotionTag, float]] = {
    EmotionTag.URGENT: 1.28,
    EmotionTag.ANGRY: 1.18,
    EmotionTag.FEARFUL: 1.15,
    EmotionTag.JOYFUL: 1.12,
    EmotionTag.NEUTRAL: 1.00,
    EmotionTag.RESOLUTE: 0.96,
    EmotionTag.SARCASTIC: 0.90,
    EmotionTag.WHISPER: 0.82,
    EmotionTag.SAD: 0.78,
    EmotionTag.HESITANT: 0.74,
}
"""Deterministic heuristic speech-rate multipliers for pre-production storyboard timing.

Scales ``BASE_FA_SYLLABLES_PER_SEC`` and ``BASE_EN_SYLLABLES_PER_SEC`` by fixed,
reproducible relative factors per ``EmotionTag`` to approximate dramatic tempo
shifts during pre-visualization without external TTS/ASR inference.
"""

#: Emotion-conditioned pause duration multipliers.
EMOTION_PAUSE_MULTIPLIER: Final[dict[EmotionTag, float]] = {
    EmotionTag.URGENT: 0.70,
    EmotionTag.ANGRY: 0.80,
    EmotionTag.JOYFUL: 0.90,
    EmotionTag.NEUTRAL: 1.00,
    EmotionTag.RESOLUTE: 1.05,
    EmotionTag.FEARFUL: 1.15,
    EmotionTag.SARCASTIC: 1.18,
    EmotionTag.WHISPER: 1.25,
    EmotionTag.SAD: 1.35,
    EmotionTag.HESITANT: 1.45,
}

#: Baseline dramatic tension in [0.0, 1.0] per acting emotion.
EMOTION_TENSION: Final[dict[EmotionTag, float]] = {
    EmotionTag.ANGRY: 0.92,
    EmotionTag.FEARFUL: 0.88,
    EmotionTag.URGENT: 0.85,
    EmotionTag.WHISPER: 0.72,
    EmotionTag.RESOLUTE: 0.68,
    EmotionTag.HESITANT: 0.62,
    EmotionTag.SAD: 0.58,
    EmotionTag.SARCASTIC: 0.55,
    EmotionTag.JOYFUL: 0.35,
    EmotionTag.NEUTRAL: 0.30,
}


@dataclass(frozen=True)
class DialogueTiming:
    """Deterministic prosodic timing decomposition of a dialogue line."""

    persian_word_count: int
    english_word_count: int
    total_word_count: int
    persian_syllable_count: int
    english_syllable_count: int
    total_syllable_count: int
    speech_rate_multiplier: float
    effective_syllables_per_sec: float
    spoken_duration_sec: float
    pause_duration_sec: float
    total_duration_sec: float


@dataclass(frozen=True)
class DialogueLine:
    """A single parsed character dialogue turn within a scene."""

    element_index: int
    character: str
    raw_text: str
    spoken_text: str
    language: ScriptLanguage
    parentheticals: tuple[str, ...]
    emotion: EmotionTag
    delivery_mode: DeliveryMode
    timing: DialogueTiming
    dramatic_tension: float
    intentional_axis_jump: bool = False


@dataclass(frozen=True)
class ActionBeat:
    """A single parsed narrative action or staging beat within a scene."""

    element_index: int
    text: str
    language: ScriptLanguage
    duration_sec: float
    dramatic_tension: float
    focus_characters: tuple[str, ...] = ()
    intentional_axis_jump: bool = False
    ambiguous_colon_action: bool = False


SceneElement = DialogueLine | ActionBeat


@dataclass(frozen=True)
class SceneHeading:
    """Normalized bilingual screenplay slugline."""

    raw: str
    scene_number: int | None
    setting: SceneSetting
    location: str
    time_of_day: TimeOfDay
    weather: WeatherCondition
    language: ScriptLanguage


@dataclass(frozen=True)
class ParsedScene:
    """A complete parsed scene with chronologically ordered elements."""

    scene_index: int
    heading: SceneHeading
    elements: tuple[SceneElement, ...]
    characters: tuple[str, ...]
    total_duration_sec: float
    peak_tension: float
    mean_tension: float


@dataclass(frozen=True)
class ParsedScript:
    """Root AST of a compiled bilingual screenplay.

    ``normalized_source`` retains the exact normalization fixed point that ``source_sha256``
    digests, so an auditor can replay the deterministic parser on the sealed text and prove the
    AST below really is the parse of that screenplay (and not a hand-forged scene graph).
    """

    scenes: tuple[ParsedScene, ...]
    language: ScriptLanguage
    characters: tuple[str, ...]
    total_duration_sec: float
    source_sha256: str
    normalized_source: str = ""


# --------------------------------------------------------------------------- #
# Normalization & Language Detection Tables
# --------------------------------------------------------------------------- #

_CHAR_TRANSLATION: Final[dict[int, str | None]] = {
    0x064A: "\u06cc",  # Arabic Yeh -> Persian Yeh
    0x0649: "\u06cc",  # Alef Maksura -> Persian Yeh
    0x0643: "\u06a9",  # Arabic Kaf -> Persian Kaf
    0x06C0: "\u0647\u0654",  # Heh with Yeh above
    0x0640: None,  # Tatweel
    0x200E: None,  # LRM
    0x200F: None,  # RLM
    0x202A: None,  # LRE
    0x202B: None,  # RLE
    0x202C: None,  # PDF
    0x202D: None,  # LRO
    0x202E: None,  # RLO
    0xFEFF: None,  # BOM
}

_DIGIT_TRANSLATION: Final[dict[int, str]] = {
    **{0x06F0 + i: str(i) for i in range(10)},  # Persian digits ۰-۹
    **{0x0660 + i: str(i) for i in range(10)},  # Arabic-Indic digits ٠-٩
}

_PERSIAN_CHAR_RE: Final[re.Pattern[str]] = re.compile(r"[\u0600-\u06FF]")
_LATIN_CHAR_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z]")
_DIACRITIC_RE: Final[re.Pattern[str]] = re.compile(r"[\u064B-\u065F\u0670]")
_WORD_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"[\u0600-\u06FF]+(?:\u200c[\u0600-\u06FF]+)*|[A-Za-z0-9]+(?:'[A-Za-z]+)*"
)

#: Valid Persian CVCC coda clusters following a long vowel.
_PERSIAN_LONG_VOWEL_CODAS: Final[frozenset[str]] = frozenset(
    {
        "ست",
        "شت",
        "فت",
        "خت",
        "ند",
        "رد",
        "زد",
        "نگ",
        "نک",
        "رگ",
        "رم",
        "رن",
        "رس",
        "رش",
        "رف",
        "رق",
        "رک",
        "رخ",
        "رز",
        "سم",
        "شم",
        "شک",
        "سک",
        "سپ",
        "لب",
        "لک",
        "لم",
        "لف",
        "نس",
        "نش",
        "نج",
        "نت",
        "کس",
        "کش",
        "کت",
    }
)

#: Ordered (longest-first) time-of-day phrases in canonical lookup space.
_TIME_OF_DAY_PHRASES: Final[tuple[tuple[str, TimeOfDay], ...]] = (
    # GOLDEN_HOUR (before DUSK/DAY so "ساعت طلایی" and "دم غروب" win over "غروب")
    ("golden hour", TimeOfDay.GOLDEN_HOUR),
    ("magic hour", TimeOfDay.GOLDEN_HOUR),
    ("late afternoon", TimeOfDay.GOLDEN_HOUR),
    ("ساعت طلایی", TimeOfDay.GOLDEN_HOUR),
    ("نور طلایی", TimeOfDay.GOLDEN_HOUR),
    ("آفتاب طلایی", TimeOfDay.GOLDEN_HOUR),
    ("دم غروب", TimeOfDay.GOLDEN_HOUR),
    ("طلایی", TimeOfDay.GOLDEN_HOUR),
    # DAWN (before DAY/DUSK so "early morning" and "گرگ و میش صبح" win)
    ("early morning", TimeOfDay.DAWN),
    ("first light", TimeOfDay.DAWN),
    ("گرگ و میش صبح", TimeOfDay.DAWN),
    ("طلوع آفتاب", TimeOfDay.DAWN),
    ("طلوع خورشید", TimeOfDay.DAWN),
    ("سپیده دم", TimeOfDay.DAWN),
    ("سپیده", TimeOfDay.DAWN),
    ("سحرگاه", TimeOfDay.DAWN),
    ("بامداد", TimeOfDay.DAWN),
    ("پگاه", TimeOfDay.DAWN),
    ("طلوع", TimeOfDay.DAWN),
    ("سحر", TimeOfDay.DAWN),
    ("daybreak", TimeOfDay.DAWN),
    ("sunrise", TimeOfDay.DAWN),
    ("dawn", TimeOfDay.DAWN),
    # DUSK
    ("گرگ و میش", TimeOfDay.DUSK),
    ("تاریک روشن", TimeOfDay.DUSK),
    ("سر شب", TimeOfDay.DUSK),
    ("شامگاه", TimeOfDay.DUSK),
    ("غروب", TimeOfDay.DUSK),
    ("عصر", TimeOfDay.DUSK),
    ("twilight", TimeOfDay.DUSK),
    ("sundown", TimeOfDay.DUSK),
    ("sunset", TimeOfDay.DUSK),
    ("evening", TimeOfDay.DUSK),
    ("dusk", TimeOfDay.DUSK),
    # NIGHT
    ("late night", TimeOfDay.NIGHT),
    ("نیمه شب", TimeOfDay.NIGHT),
    ("آخر شب", TimeOfDay.NIGHT),
    ("دل شب", TimeOfDay.NIGHT),
    ("شبانگاه", TimeOfDay.NIGHT),
    ("نیمشب", TimeOfDay.NIGHT),
    ("nighttime", TimeOfDay.NIGHT),
    ("midnight", TimeOfDay.NIGHT),
    ("night", TimeOfDay.NIGHT),
    ("شب", TimeOfDay.NIGHT),
    # DAY
    ("بعد از ظهر", TimeOfDay.DAY),
    ("بعدازظهر", TimeOfDay.DAY),
    ("نیمروز", TimeOfDay.DAY),
    ("daytime", TimeOfDay.DAY),
    ("afternoon", TimeOfDay.DAY),
    ("morning", TimeOfDay.DAY),
    ("midday", TimeOfDay.DAY),
    ("noon", TimeOfDay.DAY),
    ("day", TimeOfDay.DAY),
    ("روز", TimeOfDay.DAY),
    ("صبح", TimeOfDay.DAY),
    ("ظهر", TimeOfDay.DAY),
)

_CONTINUITY_TIME_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "continuous",
        "cont",
        "later",
        "moments later",
        "same time",
        "same",
        "ادامه",
        "پیوسته",
        "کمی بعد",
        "لحظاتی بعد",
        "همان زمان",
        "همزمان",
    }
)

#: Ordered weather phrases (longest-first so "طوفان شن" -> DUST before "طوفان" -> RAIN).
_WEATHER_PHRASES: Final[tuple[tuple[str, WeatherCondition], ...]] = (
    # DUST (before FOG/RAIN so "غبار" and "طوفان شن" win cleanly)
    ("dust storm", WeatherCondition.DUST),
    ("sand storm", WeatherCondition.DUST),
    ("sandstorm", WeatherCondition.DUST),
    ("طوفان شن", WeatherCondition.DUST),
    ("توفان شن", WeatherCondition.DUST),
    ("گرد و غبار", WeatherCondition.DUST),
    ("گردوغبار", WeatherCondition.DUST),
    ("غبار آلود", WeatherCondition.DUST),
    ("غبارآلود", WeatherCondition.DUST),
    ("خاک آلود", WeatherCondition.DUST),
    ("خاک‌آلود", WeatherCondition.DUST),
    ("ریزگرد", WeatherCondition.DUST),
    ("غبار", WeatherCondition.DUST),
    ("haboob", WeatherCondition.DUST),
    ("dusty", WeatherCondition.DUST),
    ("dust", WeatherCondition.DUST),
    # FOG
    ("مه آلود", WeatherCondition.FOG),
    ("مه‌آلود", WeatherCondition.FOG),
    ("مه گرفته", WeatherCondition.FOG),
    ("overcast", WeatherCondition.FOG),
    ("foggy", WeatherCondition.FOG),
    ("misty", WeatherCondition.FOG),
    ("hazy", WeatherCondition.FOG),
    ("smog", WeatherCondition.FOG),
    ("mist", WeatherCondition.FOG),
    ("haze", WeatherCondition.FOG),
    ("fog", WeatherCondition.FOG),
    ("مه", WeatherCondition.FOG),
    # SNOW
    ("snowstorm", WeatherCondition.SNOW),
    ("blizzard", WeatherCondition.SNOW),
    ("snowing", WeatherCondition.SNOW),
    ("snowy", WeatherCondition.SNOW),
    ("frost", WeatherCondition.SNOW),
    ("sleet", WeatherCondition.SNOW),
    ("snow", WeatherCondition.SNOW),
    ("hail", WeatherCondition.SNOW),
    ("یخبندان", WeatherCondition.SNOW),
    ("کولاک", WeatherCondition.SNOW),
    ("بوران", WeatherCondition.SNOW),
    ("تگرگ", WeatherCondition.SNOW),
    ("برفی", WeatherCondition.SNOW),
    ("برف", WeatherCondition.SNOW),
    # RAIN
    ("thunderstorm", WeatherCondition.RAIN),
    ("نم نم باران", WeatherCondition.RAIN),
    ("شرشر باران", WeatherCondition.RAIN),
    ("downpour", WeatherCondition.RAIN),
    ("raining", WeatherCondition.RAIN),
    ("drizzle", WeatherCondition.RAIN),
    ("pouring", WeatherCondition.RAIN),
    ("stormy", WeatherCondition.RAIN),
    ("storm", WeatherCondition.RAIN),
    ("rainy", WeatherCondition.RAIN),
    ("rain", WeatherCondition.RAIN),
    ("بارانی", WeatherCondition.RAIN),
    ("باران", WeatherCondition.RAIN),
    ("طوفانی", WeatherCondition.RAIN),
    ("توفانی", WeatherCondition.RAIN),
    ("طوفان", WeatherCondition.RAIN),
    ("توفان", WeatherCondition.RAIN),
    ("رگبار", WeatherCondition.RAIN),
    # CLEAR
    ("آسمان صاف", WeatherCondition.CLEAR),
    ("آفتابی", WeatherCondition.CLEAR),
    ("starry", WeatherCondition.CLEAR),
    ("sunny", WeatherCondition.CLEAR),
    ("clear", WeatherCondition.CLEAR),
    ("صاف", WeatherCondition.CLEAR),
)

_EMOTION_LEXICON: Final[tuple[tuple[EmotionTag, tuple[str, ...]], ...]] = (
    (
        EmotionTag.WHISPER,
        (
            "whispering",
            "whispers",
            "whisper",
            "under breath",
            "sotto voce",
            "murmuring",
            "murmur",
            "hushed",
            "softly",
            "quietly",
            "با زمزمه",
            "زمزمه کنان",
            "زمزمه",
            "زیر لب",
            "در گوشی",
            "پچ پچ",
            "نجوا",
            "آهسته",
            "آرام",
        ),
    ),
    (
        EmotionTag.ANGRY,
        (
            "screaming",
            "shouting",
            "furious",
            "angrily",
            "yelling",
            "snapping",
            "barking",
            "enraged",
            "angry",
            "rage",
            "با عصبانیت",
            "عصبانی",
            "با خشم",
            "خشمگین",
            "با فریاد",
            "داد زنان",
            "فریاد",
            "پرخاش",
            "غران",
            "خشم",
        ),
    ),
    (
        EmotionTag.FEARFUL,
        (
            "terrified",
            "trembling",
            "panicked",
            "nervously",
            "fearfully",
            "fearful",
            "shaking",
            "anxious",
            "nervous",
            "scared",
            "afraid",
            "وحشت زده",
            "با وحشت",
            "با ترس",
            "با لرزش",
            "ترسیده",
            "ترسان",
            "هراسان",
            "لرزان",
            "مضطرب",
            "نگران",
        ),
    ),
    (
        EmotionTag.URGENT,
        (
            "breathlessly",
            "breathless",
            "desperately",
            "desperate",
            "urgently",
            "pleading",
            "hurried",
            "frantic",
            "quickly",
            "rapidly",
            "urgent",
            "rushed",
            "نفس زنان",
            "با عجله",
            "ملتمسانه",
            "شتابان",
            "بی تاب",
            "فوری",
            "سریع",
            "تند",
        ),
    ),
    (
        EmotionTag.SAD,
        (
            "heartbroken",
            "sorrowful",
            "melancholy",
            "weeping",
            "tearful",
            "sobbing",
            "sighing",
            "crying",
            "sadly",
            "sad",
            "اندوهگین",
            "آه کشان",
            "با حسرت",
            "با گریه",
            "با بغض",
            "با غم",
            "غمگین",
            "گریان",
            "افسرده",
            "شکسته",
            "بغض",
        ),
    ),
    (
        EmotionTag.HESITANT,
        (
            "hesitantly",
            "hesitating",
            "stammering",
            "stuttering",
            "reluctantly",
            "reluctant",
            "uncertain",
            "haltingly",
            "hesitant",
            "بریده بریده",
            "با تردید",
            "با لکنت",
            "من و من",
            "محتاط",
            "تردید",
            "مردد",
            "دودل",
        ),
    ),
    (
        EmotionTag.SARCASTIC,
        (
            "sarcastically",
            "sarcastic",
            "mockingly",
            "bitterly",
            "smirking",
            "scoffing",
            "mocking",
            "cynical",
            "ironic",
            "dryly",
            "طعنه آمیز",
            "با طعنه",
            "با کنایه",
            "با پوزخند",
            "پوزخند",
            "ریشخند",
            "تمسخر",
            "طعنه",
            "تلخ",
        ),
    ),
    (
        EmotionTag.JOYFUL,
        (
            "cheerfully",
            "excitedly",
            "laughing",
            "chuckling",
            "grinning",
            "cheerful",
            "happily",
            "smiling",
            "excited",
            "joyful",
            "warmly",
            "happy",
            "ذوق زده",
            "با خنده",
            "با لبخند",
            "با شوق",
            "خوشحال",
            "خندان",
            "سرخوش",
            "لبخند",
            "شاد",
        ),
    ),
    (
        EmotionTag.RESOLUTE,
        (
            "determined",
            "commanding",
            "resolute",
            "sternly",
            "deadpan",
            "serious",
            "firmly",
            "coldly",
            "steady",
            "stern",
            "firm",
            "با اراده",
            "خونسرد",
            "آمرانه",
            "مطمئن",
            "قاطع",
            "محکم",
            "مصمم",
            "جدی",
            "سرد",
        ),
    ),
)

_HIGH_TENSION_ACTION_CUES: Final[tuple[str, ...]] = (
    "gun",
    "weapon",
    "knife",
    "blood",
    "slam",
    "slams",
    "punch",
    "strike",
    "explosion",
    "scream",
    "chase",
    "run",
    "runs",
    "shatter",
    "crash",
    "lock",
    "shadow",
    "trembl",
    "اسلحه",
    "تفنگ",
    "چاقو",
    "خون",
    "مشت",
    "کوب",
    "می کوبد",
    "انفجار",
    "فریاد",
    "فرار",
    "می دود",
    "شکس",
    "قفل",
    "سایه",
    "لرز",
    "ضربه",
    "شعله",
    "تهدید",
)

_AXIS_JUMP_RE: Final[re.Pattern[str]] = re.compile(
    r"[\[(]\s*(?:"
    r"axis[\s_-]*jump|cross(?:ing)?[\s_-]*(?:the[\s_-]*)?axis|180[\s_-]*jump|"
    r"پرش\s*محوری|عبور\s*از\s*خط(?:\s*فرضی|\s*کنش|\s*180\s*درجه|\s*۱۸۰\s*درجه)?|"
    r"پرش\s*خط\s*(?:180|۱۸۰)\s*درجه|شکست\s*خط\s*(?:فرضی|180\s*درجه|۱۸۰\s*درجه)"
    r")\s*[\])]",
    re.IGNORECASE,
)

_SCENE_NUM_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:"
    r"(?:scene|سکانس|صحنه)\s*#?\s*(\d+)\s*(?:[.:)\-–—]+\s*|\s+)|"
    r"#?\s*(\d+)\s*[.:)\-–—]+\s*"
    r")",
    re.IGNORECASE,
)

_SETTING_PATTERNS: Final[tuple[tuple[re.Pattern[str], SceneSetting, bool], ...]] = (
    # INT_EXT combined prefixes (unambiguous)
    (
        re.compile(
            r"^(?:INT\.?\s*/\s*EXT\.?|EXT\.?\s*/\s*INT\.?|I\s*/\s*E\.?)(?:\s*[.:\-–—]\s*|\s+)",
            re.IGNORECASE,
        ),
        SceneSetting.INT_EXT,
        True,
    ),
    (
        re.compile(
            r"^(?:داخلی\s*/\s*خارجی|خارجی\s*/\s*داخلی|داخلی\s+و\s+خارجی)(?:\s*[.:\-–—]\s*|\s+)"
        ),
        SceneSetting.INT_EXT,
        True,
    ),
    # Punctuated single prefixes (unambiguous)
    (
        re.compile(r"^(?:INTERIOR|INT)\s*[.:]\s*", re.IGNORECASE),
        SceneSetting.INT,
        True,
    ),
    (
        re.compile(r"^(?:EXTERIOR|EXT)\s*[.:]\s*", re.IGNORECASE),
        SceneSetting.EXT,
        True,
    ),
    (
        re.compile(r"^داخلی\s*[.:]\s*"),
        SceneSetting.INT,
        True,
    ),
    (
        re.compile(r"^خارجی\s*[.:]\s*"),
        SceneSetting.EXT,
        True,
    ),
    # Unpunctuated single prefixes (require explicit slug dash separator)
    (
        re.compile(r"^(?:INTERIOR|INT)\s+(?:[-–—]\s*)?"),
        SceneSetting.INT,
        False,
    ),
    (
        re.compile(r"^(?:EXTERIOR|EXT)\s+(?:[-–—]\s*)?"),
        SceneSetting.EXT,
        False,
    ),
    (
        re.compile(r"^داخلی\s+(?:[-–—]\s*)?"),
        SceneSetting.INT,
        False,
    ),
    (
        re.compile(r"^خارجی\s+(?:[-–—]\s*)?"),
        SceneSetting.EXT,
        False,
    ),
)

_TRANSITION_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:"
    r"(?:SMASH\s+|MATCH\s+|JUMP\s+|HARD\s+|FINAL\s+)?CUT\s+TO:?|"
    r"DISSOLVE\s+TO:?|FADE\s+IN:?|FADE\s+OUT\.?:?|FADE\s+TO\s+BLACK\.?:?|IRIS\s+(?:IN|OUT)\.?|"
    r"(?:کات|برش|دیزالو)\s+به\s*:?|فید\s*(?:این|اوت|به\s*سیاهی)\s*[.:]?|سیاهی\s*\."
    r")$",
    re.IGNORECASE,
)

_VO_TOKENS: Final[tuple[str, ...]] = (
    "v.o.",
    "v.o",
    "vo",
    "voice over",
    "voiceover",
    "نریشن",
    "صدای راوی",
    "صدای ذهن",
    "گفتار متن",
)

_OS_TOKENS: Final[tuple[str, ...]] = (
    "o.s.",
    "o.s",
    "o.c.",
    "o.c",
    "off screen",
    "off camera",
    "صدای خارج از قاب",
    "خارج از قاب",
    "خارج از صحنه",
    "از پشت در",
)

_CONTD_TOKENS: Final[frozenset[str]] = frozenset(
    {"cont'd", "contd", "cont.", "cont", "continuing", "ادامه"}
)

_PERSIAN_VERB_ENDINGS: Final[tuple[str, ...]] = (
    "می کند",
    "می کنند",
    "می شود",
    "می شوند",
    "می رود",
    "می آید",
    "می گوید",
    "می بیند",
    "می کوبد",
    "می نشیند",
    "می ایستد",
    "می افتد",
    "می زند",
    "می گیرد",
    "می گذارد",
    "می بندد",
    "می کشد",
    "است",
    "هستند",
    "بود",
    "شد",
    "کرد",
)

_QUOTED_SPEECH_RE: Final[re.Pattern[str]] = re.compile(r'[«»"\u201c\u201d]')

_DIALOGUE_PRONOUNS: Final[frozenset[str]] = frozenset(
    {
        "من",
        "تو",
        "شما",
        "ما",
        "منم",
        "مرا",
        "i",
        "i'm",
        "i'll",
        "i've",
        "me",
        "my",
        "you",
        "you're",
        "your",
        "we",
        "we're",
        "our",
        "let's",
    }
)

#: Third-person Persian stage-direction verb endings (canonical space-separated form).
_PERSIAN_COLON_ACTION_ENDINGS: Final[tuple[str, ...]] = (
    "نگاه می کند",
    "خیره می شود",
    "وارد می شود",
    "خارج می شود",
    "می نشیند",
    "برمی خیزد",
    "بر می خیزد",
    "بلند می شود",
    "می ایستد",
    "سکوت می کند",
    "مکث می کند",
    "می رود",
    "برمی گردد",
    "بر می گردد",
    "لبخند می زند",
    "پوزخند می زند",
    "آه می کشد",
    "نفس می کشد",
    "تکان می دهد",
    "گریه می کند",
    "می خندد",
    "زمزمه می کند",
    "قدم می زند",
    "می دود",
    "نزدیک می شود",
    "دور می شود",
    "می کوبد",
    "می بندد",
    "باز می کند",
    "برمی دارد",
    "بر می دارد",
    "می گذارد",
    "می افتد",
    "می چرخد",
    "تکیه می دهد",
    "دست می کشد",
)

#: Third-person singular English stage-direction lead verbs for unquoted colon actions.
_ENGLISH_COLON_ACTION_LEADS: Final[frozenset[str]] = frozenset(
    {
        "approaches",
        "backs",
        "blinks",
        "breathes",
        "closes",
        "crosses",
        "drops",
        "enters",
        "exhales",
        "exits",
        "flinches",
        "freezes",
        "frowns",
        "gazes",
        "gestures",
        "glances",
        "grabs",
        "hesitates",
        "inhales",
        "kneels",
        "leans",
        "leaves",
        "looks",
        "moves",
        "murmurs",
        "mutters",
        "nods",
        "opens",
        "pauses",
        "picks",
        "points",
        "puts",
        "reaches",
        "remains",
        "retreats",
        "rises",
        "runs",
        "rushes",
        "sets",
        "shakes",
        "shrugs",
        "sighs",
        "sits",
        "slams",
        "smiles",
        "stands",
        "stares",
        "steps",
        "turns",
        "waits",
        "walks",
        "watches",
        "weeps",
        "whispers",
    }
)

#: Common non-``-ly`` English manner/aspect adverbs preceding 3rd-person stage verbs.
_ENGLISH_STAGE_ADVERBS: Final[frozenset[str]] = frozenset(
    {
        "again",
        "almost",
        "already",
        "always",
        "back",
        "barely",
        "ever",
        "fast",
        "hard",
        "hardly",
        "just",
        "merely",
        "nearly",
        "never",
        "now",
        "often",
        "right",
        "simply",
        "soon",
        "still",
        "straight",
        "then",
    }
)

_PAREN_GROUP_RE: Final[re.Pattern[str]] = re.compile(r"\(([^()]+)\)")
_NUMERIC_PAUSE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:(?:pause|beat|silence|مکث|سکوت)\s*(?:of\s*)?(\d+(?:\.\d+)?)\s*(?:s|sec|seconds?|ثانیه)?|"
    r"(\d+(?:\.\d+)?)\s*(?:s|sec|seconds?|ثانیه)\s*(?:pause|beat|silence|مکث|سکوت)?)",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Public Utility Functions
# --------------------------------------------------------------------------- #


def normalize_script_text(text: str, *, normalize_digits: bool = False) -> str:
    """Normalize bilingual screenplay text while preserving ZWNJ and line breaks.

    When ``normalize_digits=True``, Persian (``۰-۹``) and Arabic-Indic (``٠-٩``)
    numerals are also converted into ASCII digits (``0-9``). By default
    (``normalize_digits=False``), native numerals in prose and dialogue are kept.
    """
    if not isinstance(text, str):
        raise ScriptParseError(f"script text must be a str, got {type(text).__name__}")
    unified = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    cleaned = unified.translate(_CHAR_TRANSLATION)
    if normalize_digits:
        return cleaned.translate(_DIGIT_TRANSLATION)
    return cleaned


def _canonical_key(text: str) -> str:
    """Map Persian/English phrase into a punctuation-stripped lookup key."""
    ascii_digits = normalize_script_text(text, normalize_digits=True).lower()
    no_diacritics = _DIACRITIC_RE.sub("", ascii_digits)
    spaced = re.sub(r"[\u200c_\-–—/]+", " ", no_diacritics)
    cleaned = re.sub(r"[^\w\s\u0600-\u06FF']", " ", spaced)
    return re.sub(r"\s+", " ", cleaned).strip()


def _contains_phrase(haystack_key: str, phrase: str) -> bool:
    """Boundary-aware membership test on canonicalized lookup strings."""
    return f" {phrase} " in f" {haystack_key} "


def detect_language(text: str) -> ScriptLanguage:
    """Classify text as ``FA``, ``EN``, or ``MIXED`` from script character counts."""
    normalized = normalize_script_text(text)
    fa_count = len(_PERSIAN_CHAR_RE.findall(normalized))
    en_count = len(_LATIN_CHAR_RE.findall(normalized))
    if fa_count > 0 and en_count > 0:
        return ScriptLanguage.MIXED
    if fa_count > 0:
        return ScriptLanguage.FA
    return ScriptLanguage.EN


def count_english_syllables(word: str) -> int:
    """Deterministically estimate syllable count of an English word."""
    cleaned = re.sub(r"[^a-z]", "", word.lower())
    if not cleaned:
        digits = re.sub(r"[^0-9]", "", word.translate(_DIGIT_TRANSLATION))
        return len(digits) if digits else 0
    if len(cleaned) <= 3:
        return 1

    # Count contiguous vowel groups as candidate nuclei.
    groups = re.findall(r"[aeiouy]+", cleaned)
    count = len(groups)

    # Silent word-final '-e' (excluding syllabic '-le' after a consonant, e.g. 'table').
    if cleaned.endswith("e") and not cleaned.endswith(("ee", "oe", "ye")):
        if not (cleaned.endswith("le") and len(cleaned) > 2 and cleaned[-3] not in "aeiouy"):
            count -= 1

    # Silent regular past '-ed' when not preceded by 't' or 'd' (e.g. 'walked' vs 'wanted').
    if (
        cleaned.endswith("ed")
        and len(cleaned) > 4
        and cleaned[-3] not in "td"
        and not cleaned.endswith("ied")
    ):
        count -= 1

    # Silent plural/verb '-es' when not preceded by a sibilant (e.g. 'makes' vs 'voices').
    if (
        cleaned.endswith("es")
        and len(cleaned) > 4
        and not cleaned.endswith(("ses", "zes", "xes", "ches", "shes", "ges", "ces", "ies"))
    ):
        count -= 1

    # Common disyllabic vowel hiatus pairs (e.g. 'radio', 'quiet', 'chaos').
    count += len(re.findall(r"(?:ia|io|eo|ua|uo|iu|ui(?=[aeiou]))", cleaned))
    # Subtract over-counted '-tion' / '-sion' / '-cial' / '-tial' where 'io'/'ia' is one syllable.
    count -= len(re.findall(r"(?:[ts]ion|[ct]ial|[cg]ious|[st]ious)", cleaned))

    return max(1, count)


def _count_persian_morpheme_syllables(morpheme: str) -> int:
    """Estimate syllable count of a single ZWNJ-free Persian morpheme."""
    explicit_short = len(re.findall(r"[\u064E\u064F\u0650]", morpheme))
    letters = re.sub(r"[^\u0621-\u064A\u067E\u0686\u0698\u06A9\u06AF\u06CC]", "", morpheme)
    n = len(letters)
    if n == 0:
        return 0
    if n <= 2:
        return 1

    # Persian present-tense verb stems where medial 'و' is consonant /v/ (e.g. روم = ra-vam = 2).
    verb_v_stems = frozenset({"روم", "رود", "روند", "شوم", "شود", "شوند", "دوم", "دود", "دوند"})
    if letters in verb_v_stems:
        return max(2, explicit_short)

    # Classify each letter as 'V' (vowel nucleus) or 'C' (consonant).
    kinds: list[str] = []
    for idx, ch in enumerate(letters):
        prev_ch = letters[idx - 1] if idx > 0 else ""
        next_ch = letters[idx + 1] if idx + 1 < n else ""
        if ch == "\u0622":  # آ is always a long vowel nucleus
            kinds.append("V")
        elif ch == "\u0627":  # ا: initial carries a short vowel; medial/final is long ā
            kinds.append("V")
        elif ch == "\u0648":  # و: onset consonant initially or after آ/ا, else vowel u/o
            if idx == 0 or prev_ch in ("\u0622", "\u0627"):
                kinds.append("C")
            else:
                kinds.append("V")
        elif ch == "\u06cc":  # ی: glide initially or between vowel and vowel/final-ه
            if idx == 0 or (
                prev_ch in ("\u0622", "\u0627", "\u0648")
                and next_ch in ("\u0622", "\u0627", "\u0648", "\u06cc", "\u0647")
            ):
                kinds.append("C")
            else:
                kinds.append("V")
        elif ch == "\u0647" and idx == n - 1 and kinds and kinds[-1] == "C":
            # Word-final silent/vowel ه (-e), e.g. خانه (xā-ne), زمزمه (zem-ze-me).
            kinds.append("V")
        else:
            kinds.append("C")

    vowel_indices = [i for i, k in enumerate(kinds) if k == "V"]
    if not vowel_indices:
        # Pure-consonant spelling (only unwritten short vowels): CV/CVC/CVCC rules.
        base = 1 if n <= 3 else n // 2
        return max(base, explicit_short)

    syllables = len(vowel_indices)

    # Consonants before first explicit vowel: 1 attaches as onset, rest require short vowels.
    lead_c = vowel_indices[0]
    if lead_c >= 2:
        syllables += (lead_c) // 2

    # Consonant runs between consecutive explicit vowels: up to 2 fit as coda + next onset.
    for left_v, right_v in zip(vowel_indices, vowel_indices[1:], strict=False):
        between_c = right_v - left_v - 1
        if between_c >= 3:
            syllables += (between_c - 1) // 2

    # Consonants after final explicit vowel.
    last_v = vowel_indices[-1]
    tail_c = n - 1 - last_v
    if tail_c == 2:
        # Initial 'ا' represents CV (e.g. اسم = esm, 1 syllable); long vowels only keep
        # true Persian CVCC codas (e.g. راست = rāst, 1) and split CVCVC (e.g. مادر = mā-dar, 2).
        tail_pair = letters[-2:]
        if letters[last_v] != "\u0627" or last_v > 0:
            if tail_pair not in _PERSIAN_LONG_VOWEL_CODAS:
                syllables += 1
    elif tail_c >= 3:
        syllables += tail_c // 2

    return max(1, syllables, explicit_short)


def count_persian_syllables(word: str) -> int:
    """Deterministically estimate syllable count of a Persian word (ZWNJ-aware)."""
    normalized = normalize_script_text(word)
    parts = normalized.split("\u200c")
    total = sum(_count_persian_morpheme_syllables(part) for part in parts)
    if total > 0:
        return total
    digits = re.sub(r"[^0-9]", "", normalized.translate(_DIGIT_TRANSLATION))
    return len(digits) if digits else 0


def classify_emotion(parentheticals: Sequence[str], spoken_text: str = "") -> EmotionTag:
    """Classify dominant acting emotion from parenthetical cues and punctuation."""
    for raw_paren in parentheticals:
        key = _canonical_key(raw_paren)
        if not key:
            continue
        for emotion, cues in _EMOTION_LEXICON:
            for cue in cues:
                if _contains_phrase(key, cue):
                    return emotion

    # Subtle punctuation fallback when no explicit emotional parenthetical was supplied.
    stripped = spoken_text.strip()
    if "!" in stripped and ("?" in stripped or "؟" in stripped or stripped.count("!") >= 2):
        return EmotionTag.ANGRY
    if "..." in stripped or "…" in stripped:
        return EmotionTag.HESITANT
    return EmotionTag.NEUTRAL


def _extract_parenthetical_pause(parentheticals: Sequence[str]) -> float:
    """Compute explicit dramatic pause duration (seconds) declared in parentheticals."""
    pause_sec = 0.0
    for raw in parentheticals:
        normalized = normalize_script_text(raw).translate(_DIGIT_TRANSLATION)
        numeric_match = _NUMERIC_PAUSE_RE.search(normalized)
        if numeric_match is not None:
            raw_val = numeric_match.group(1) or numeric_match.group(2)
            value = float(raw_val)
            pause_sec += min(max(value, 0.10), 15.0)
            continue

        key = _canonical_key(raw)
        if any(
            _contains_phrase(key, p)
            for p in ("long pause", "long beat", "silence", "مکث طولانی", "سکوت طولانی", "سکوت")
        ):
            pause_sec += 1.20
        elif any(_contains_phrase(key, p) for p in ("short pause", "مکث کوتاه")):
            pause_sec += 0.35
        elif any(_contains_phrase(key, p) for p in ("pause", "beat", "pausing", "مکث")):
            pause_sec += 0.60
    return pause_sec


def _compute_punctuation_pause(spoken_text: str, emotion: EmotionTag) -> float:
    """Compute prosodic pause time from ellipses, commas, dashes, and mid-line stops."""
    # Collapse multi-dot ellipses into single marker so '.' counting doesn't double-count.
    ellipsis_count = len(re.findall(r"\.{3,}|…", spoken_text))
    without_ellipses = re.sub(r"\.{3,}|…", " ", spoken_text)

    comma_count = without_ellipses.count(",") + without_ellipses.count("،")
    semi_count = (
        without_ellipses.count(";") + without_ellipses.count("؛") + without_ellipses.count(":")
    )
    dash_count = len(re.findall(r"—|–|--", without_ellipses))

    # Count sentence boundaries strictly inside the utterance (exclude final terminator).
    trimmed = re.sub(r"[.!?؟]+\s*$", "", without_ellipses.strip())
    mid_sentence_stops = len(re.findall(r"[.!?؟]+", trimmed))

    raw_pause = (
        0.55 * ellipsis_count
        + 0.22 * comma_count
        + 0.30 * semi_count
        + 0.35 * dash_count
        + 0.38 * mid_sentence_stops
        + 0.20  # natural post-utterance breath cadence
    )
    return raw_pause * EMOTION_PAUSE_MULTIPLIER[emotion]


def estimate_dialogue_timing(
    spoken_text: str,
    *,
    emotion: EmotionTag = EmotionTag.NEUTRAL,
    parentheticals: Sequence[str] = (),
) -> DialogueTiming:
    """Estimate dialogue spoken duration, pause duration, and total duration.

    Blends syllable-based duration (75% weight) with word-count duration (25% weight)
    separately for Persian and English tokens, then scales by the acting emotion's
    speech-rate multiplier and adds punctuation + parenthetical pauses.
    """
    normalized = normalize_script_text(spoken_text)
    # Strip any residual inline parentheticals from spoken token counting.
    clean_spoken = _PAREN_GROUP_RE.sub(" ", normalized)
    clean_spoken = _AXIS_JUMP_RE.sub(" ", clean_spoken).strip()

    fa_words = 0
    en_words = 0
    fa_syllables = 0
    en_syllables = 0

    for match in _WORD_TOKEN_RE.finditer(clean_spoken):
        token = match.group(0)
        if _PERSIAN_CHAR_RE.search(token):
            syl = count_persian_syllables(token)
            fa_words += 1
            fa_syllables += syl
        else:
            en_words += 1
            en_syllables += count_english_syllables(token)

    total_words = fa_words + en_words
    total_syllables = fa_syllables + en_syllables
    rate_mult = EMOTION_SPEECH_RATE[emotion]

    fa_base_sec = 0.0
    if fa_words > 0:
        fa_base_sec = 0.75 * (fa_syllables / BASE_FA_SYLLABLES_PER_SEC) + 0.25 * (
            fa_words / _BASE_FA_WORDS_PER_SEC
        )

    en_base_sec = 0.0
    if en_words > 0:
        en_base_sec = 0.75 * (en_syllables / BASE_EN_SYLLABLES_PER_SEC) + 0.25 * (
            en_words / _BASE_EN_WORDS_PER_SEC
        )

    raw_spoken_sec = (fa_base_sec + en_base_sec) / rate_mult
    spoken_sec = max(0.25, raw_spoken_sec) if total_syllables > 0 else 0.0

    punct_pause = _compute_punctuation_pause(clean_spoken, emotion) if total_syllables > 0 else 0.0
    paren_pause = _extract_parenthetical_pause(parentheticals)
    pause_sec = punct_pause + paren_pause

    total_sec = max(0.35, spoken_sec + pause_sec)
    if spoken_sec > 0.0:
        eff_rate = total_syllables / spoken_sec
    else:
        eff_rate = BASE_EN_SYLLABLES_PER_SEC * rate_mult

    return DialogueTiming(
        persian_word_count=fa_words,
        english_word_count=en_words,
        total_word_count=total_words,
        persian_syllable_count=fa_syllables,
        english_syllable_count=en_syllables,
        total_syllable_count=total_syllables,
        speech_rate_multiplier=round(rate_mult, 6),
        effective_syllables_per_sec=round(eff_rate, 6),
        spoken_duration_sec=round(spoken_sec, 6),
        pause_duration_sec=round(pause_sec, 6),
        total_duration_sec=round(total_sec, 6),
    )


def estimate_action_duration(action_text: str) -> float:
    """Estimate screen time (seconds) required to stage and read a narrative action beat."""
    normalized = _AXIS_JUMP_RE.sub(" ", normalize_script_text(action_text)).strip()
    tokens = _WORD_TOKEN_RE.findall(normalized)
    word_count = len(tokens)
    if word_count == 0:
        return 1.20
    clause_breaks = len(re.findall(r"[.!?؟،,;؛—–]+", normalized))
    duration = 1.00 + 0.24 * word_count + 0.20 * max(0, clause_breaks - 1)
    return round(min(max(duration, 1.20), 18.0), 6)


# --------------------------------------------------------------------------- #
# Scene Heading Parsing
# --------------------------------------------------------------------------- #


def _match_time_of_day(
    segment_key: str, previous_time: TimeOfDay | None
) -> tuple[TimeOfDay | None, str]:
    """Extract time of day from a slug segment and return remaining unmatched key."""
    if segment_key in _CONTINUITY_TIME_TOKENS:
        return (previous_time or TimeOfDay.DAY), ""
    for phrase, tod in _TIME_OF_DAY_PHRASES:
        if _contains_phrase(segment_key, phrase):
            remainder = re.sub(rf"(?:^|\s){re.escape(phrase)}(?=\s|$)", " ", segment_key).strip()
            remainder = re.sub(r"\s+", " ", remainder)
            return tod, remainder
    return None, segment_key


def _match_weather(segment_key: str) -> tuple[WeatherCondition | None, str]:
    """Extract weather condition from a slug segment and return remaining unmatched key."""
    for phrase, weather in _WEATHER_PHRASES:
        if _contains_phrase(segment_key, phrase):
            remainder = re.sub(rf"(?:^|\s){re.escape(phrase)}(?=\s|$)", " ", segment_key).strip()
            remainder = re.sub(r"\s+", " ", remainder)
            return weather, remainder
    return None, segment_key


def parse_scene_heading(
    line: str,
    *,
    previous_time_of_day: TimeOfDay | None = None,
) -> SceneHeading | None:
    """Parse a single Persian or English scene slugline, or return ``None`` if not a heading."""
    cleaned = normalize_script_text(line).strip()
    if not cleaned:
        return None

    # Extract optional leading scene number (e.g. "۱. داخلی..." or "SCENE 2 - INT...").
    ascii_line = cleaned.translate(_DIGIT_TRANSLATION)
    scene_number: int | None = None
    body = cleaned
    num_match = _SCENE_NUM_PREFIX_RE.match(ascii_line)
    if num_match is not None:
        raw_num = num_match.group(1) or num_match.group(2)
        scene_number = int(raw_num)
        body = cleaned[num_match.end() :].strip()

    # Strip optional trailing duplicated scene number (Fountain "INT. ROOM - NIGHT #12#").
    body = re.sub(r"\s+#[\d\u06F0-\u06F9\u0660-\u0669]+#\s*$", "", body).strip()

    matched_setting: SceneSetting | None = None
    unambiguous = False
    remainder = ""
    for pattern, setting, is_unambiguous in _SETTING_PATTERNS:
        m = pattern.match(body)
        if m is not None:
            matched_setting = setting
            unambiguous = is_unambiguous
            remainder = body[m.end() :].strip()
            break

    if matched_setting is None or not remainder:
        return None
    # Reject bare dash without a location before it (e.g. "INT. - NIGHT").
    if remainder[0] in "-–—":
        return None

    # Extract any trailing parenthetical qualifiers like "(RAIN)" or "(بارانی)".
    trailing_parens: list[str] = []
    while True:
        tail_match = re.search(r"\s*\(([^()]+)\)\s*$", remainder)
        if tail_match is None:
            break
        trailing_parens.insert(0, tail_match.group(1).strip())
        remainder = remainder[: tail_match.start()].strip()

    # Split remainder on slug dash separators.
    raw_segments = [seg.strip() for seg in re.split(r"\s+[-–—]+\s+|\s*—\s*", remainder)]
    if len(raw_segments) == 1 and "-" in remainder:
        raw_segments = [seg.strip() for seg in remainder.split("-") if seg.strip()]
    raw_segments = [seg for seg in raw_segments if seg]

    if not unambiguous and len(raw_segments) < 2:
        return None

    time_of_day: TimeOfDay | None = None
    weather: WeatherCondition | None = None

    # First inspect trailing parenthetical qualifiers for weather or time of day.
    for paren in trailing_parens:
        pkey = _canonical_key(paren)
        if weather is None:
            w_found, pkey = _match_weather(pkey)
            weather = w_found
        if time_of_day is None and pkey:
            t_found, _ = _match_time_of_day(pkey, previous_time_of_day)
            time_of_day = t_found

    # Inspect trailing dash-separated segments from right to left (keeping segment 0 as location).
    location_end = len(raw_segments)
    for idx in range(len(raw_segments) - 1, 0, -1):
        seg_key = _canonical_key(raw_segments[idx])
        consumed = False
        # Match weather first so compound segments like "شب بارانی" / "FOGGY DAWN" yield both.
        if weather is None:
            w_found, residual = _match_weather(seg_key)
            if w_found is not None:
                weather = w_found
                seg_key = residual
                consumed = True
        if time_of_day is None and seg_key:
            t_found, residual = _match_time_of_day(seg_key, previous_time_of_day)
            if t_found is not None:
                time_of_day = t_found
                seg_key = residual
                consumed = True

        if consumed:
            # Segment carried time-of-day and/or weather (with optional modifier like "شدید").
            location_end = idx
            continue
        break

    if not unambiguous and time_of_day is None and weather is None:
        return None

    location = " - ".join(raw_segments[:location_end]).strip(" .:-–—")
    if not location:
        return None

    return SceneHeading(
        raw=cleaned,
        scene_number=scene_number,
        setting=matched_setting,
        location=location,
        time_of_day=time_of_day if time_of_day is not None else TimeOfDay.DAY,
        weather=weather if weather is not None else WeatherCondition.CLEAR,
        language=detect_language(cleaned),
    )


# --------------------------------------------------------------------------- #
# Character Cue, Dialogue & Action Parsing Helpers
# --------------------------------------------------------------------------- #


def _parse_character_header(raw_header: str) -> tuple[str, DeliveryMode, list[str]]:
    """Split a character cue into canonical name, delivery mode, and inline parentheticals."""
    delivery = DeliveryMode.ON_SCREEN
    extra_parens: list[str] = []

    def _consume_paren(match: re.Match[str]) -> str:
        nonlocal delivery
        content = match.group(1).strip()
        lower = normalize_script_text(content).lower().strip()
        canon = _canonical_key(content)
        if lower in _CONTD_TOKENS or canon in _CONTD_TOKENS:
            return " "
        if any(lower == tok or _contains_phrase(canon, _canonical_key(tok)) for tok in _VO_TOKENS):
            delivery = DeliveryMode.VOICE_OVER
            return " "
        if any(lower == tok or _contains_phrase(canon, _canonical_key(tok)) for tok in _OS_TOKENS):
            delivery = DeliveryMode.OFF_SCREEN
            return " "
        extra_parens.append(content)
        return " "

    name = _PAREN_GROUP_RE.sub(_consume_paren, raw_header)
    name = re.sub(r"^@\s*", "", name)  # Fountain forced-character '@' prefix.
    name = re.sub(r"\s*\^\s*$", "", name)  # Fountain dual-dialogue '^' suffix.
    name = re.sub(r"\s+", " ", name).strip(" :：-–—")
    return name, delivery, extra_parens


def _is_valid_character_name(candidate: str, *, require_uppercase_for_en: bool) -> bool:
    """Return True if ``candidate`` is syntactically a plausible screenplay character cue."""
    base = _PAREN_GROUP_RE.sub(" ", candidate).strip()
    base = re.sub(r"^@\s*|\s*\^\s*$", "", base).strip()
    if not base or len(base) > 38:
        return False
    # Disallow sentence-ending or narrative punctuation in character names.
    if re.search(r"[!?؟،,;؛\"«»…]", base) or base.endswith("."):
        return False
    # Disallow pure digits/timestamps.
    if not (_PERSIAN_CHAR_RE.search(base) or _LATIN_CHAR_RE.search(base)):
        return False

    words = base.split()
    if len(words) > 4:
        return False

    has_fa = bool(_PERSIAN_CHAR_RE.search(base))
    has_en = bool(_LATIN_CHAR_RE.search(base))

    if has_en and not has_fa and require_uppercase_for_en:
        letters_only = "".join(_LATIN_CHAR_RE.findall(base))
        if not letters_only.isupper():
            return False

    if has_fa:
        canon = _canonical_key(base)
        if any(canon.endswith(ending) for ending in _PERSIAN_VERB_ENDINGS):
            return False

    return True


def _try_parse_colon_dialogue(line: str) -> tuple[str, DeliveryMode, list[str], str] | None:
    """Parse ``CHARACTER (emotion): dialogue`` in Persian or English, or return ``None``."""
    colon_idx = line.find(":")
    if colon_idx <= 0:
        return None

    left = line[:colon_idx].strip()
    right = line[colon_idx + 1 :].strip()
    if not right:
        return None

    # Avoid time expressions such as "12:30".
    if left[-1].isdigit() and right[0].isdigit():
        return None

    forced = left.startswith("@")
    if not _is_valid_character_name(left, require_uppercase_for_en=False) and not forced:
        return None

    # For English colon cues that aren't uppercase, require <=3 title-cased words.
    base_left = _PAREN_GROUP_RE.sub(" ", left).strip()
    if _LATIN_CHAR_RE.search(base_left) and not _PERSIAN_CHAR_RE.search(base_left) and not forced:
        words = base_left.split()
        if not base_left.isupper() and not all(w[0].isupper() for w in words if w[0].isalpha()):
            return None

    char_name, delivery, parens = _parse_character_header(left)
    if not char_name:
        return None
    return char_name, delivery, parens, right


def _is_english_stage_adverb(token: str) -> bool:
    """Return True if ``token`` is an ``-ly`` manner adverb or common stage adverb."""
    return token in _ENGLISH_STAGE_ADVERBS or (len(token) >= 4 and token.endswith("ly"))


def _is_unquoted_colon_stage_action(
    raw_body: str,
    header_parens: Sequence[str],
    delivery: DeliveryMode,
    *,
    forced_cue: bool,
) -> bool:
    """Return True when an unquoted ``CHAR: ...`` line is 3rd-person stage action."""
    if (
        forced_cue
        or delivery != DeliveryMode.ON_SCREEN
        or bool(header_parens)
        or bool(_QUOTED_SPEECH_RE.search(raw_body))
    ):
        return False

    clause, _, _ = _extract_inline_directives(raw_body, ())
    if not clause or clause.endswith(("?", "؟", "!")):
        return False

    canon = _canonical_key(clause)
    tokens = canon.split()
    if not tokens or any(tok in _DIALOGUE_PRONOUNS for tok in tokens):
        return False

    if _PERSIAN_CHAR_RE.search(clause):
        return any(
            canon == ending or canon.endswith(f" {ending}")
            for ending in _PERSIAN_COLON_ACTION_ENDINGS
        )
    return tokens[0] in _ENGLISH_COLON_ACTION_LEADS or (
        len(tokens) >= 2
        and _is_english_stage_adverb(tokens[0])
        and tokens[1] in _ENGLISH_COLON_ACTION_LEADS
    )


def _is_standalone_character_cue(line: str, next_line: str | None) -> bool:
    """Determine whether ``line`` begins a standard multi-line dialogue block."""
    if next_line is None or not next_line.strip() or ":" in line:
        return False
    if parse_scene_heading(next_line) is not None or _TRANSITION_RE.match(next_line):
        return False
    if line.startswith("@"):
        name, _, _ = _parse_character_header(line)
        return bool(name)
    return _is_valid_character_name(line, require_uppercase_for_en=True)


def _extract_inline_directives(
    raw_dialogue: str, initial_parens: Sequence[str]
) -> tuple[str, tuple[str, ...], bool]:
    """Separate parenthetical directives and axis-jump markers from spoken dialogue."""
    axis_jump = bool(_AXIS_JUMP_RE.search(raw_dialogue)) or any(
        bool(_AXIS_JUMP_RE.search(f"({p})")) for p in initial_parens
    )
    cleaned = _AXIS_JUMP_RE.sub(" ", raw_dialogue)

    parens: list[str] = [
        p.strip() for p in initial_parens if p.strip() and not _AXIS_JUMP_RE.fullmatch(f"({p})")
    ]

    def _collect(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if inner:
            parens.append(inner)
        return " "

    spoken = _PAREN_GROUP_RE.sub(_collect, cleaned)
    spoken = re.sub(r"\s+", " ", spoken).strip()
    # Normalize spacing before punctuation created when an inline parenthetical was removed.
    spoken = re.sub(r"\s+([,،.;؛!?؟])", r"\1", spoken)
    return spoken, tuple(parens), axis_jump


def _compute_dialogue_tension(
    emotion: EmotionTag,
    spoken_text: str,
    timing: DialogueTiming,
    time_of_day: TimeOfDay,
    weather: WeatherCondition,
) -> float:
    """Compute bounded dramatic tension score in [0.0, 1.0] for a dialogue line."""
    tension = EMOTION_TENSION[emotion]
    if "!" in spoken_text:
        tension += 0.05
    if ("?" in spoken_text or "؟" in spoken_text) and "!" in spoken_text:
        tension += 0.04
    if time_of_day == TimeOfDay.NIGHT:
        tension += 0.03
    if weather in (WeatherCondition.RAIN, WeatherCondition.FOG, WeatherCondition.DUST):
        tension += 0.02
    # Terse, punchy 1-3 word lines carry slightly elevated dramatic sharpness.
    if 1 <= timing.total_word_count <= 3 and emotion != EmotionTag.JOYFUL:
        tension += 0.02
    return round(min(max(tension, 0.0), 1.0), 6)


def _compute_action_tension(
    text: str,
    time_of_day: TimeOfDay,
    weather: WeatherCondition,
    *,
    is_opening_beat: bool,
) -> float:
    """Compute bounded dramatic tension score in [0.0, 1.0] for a narrative action beat."""
    canon = _canonical_key(text)
    cue_hits = sum(1 for cue in _HIGH_TENSION_ACTION_CUES if cue in canon)
    base = 0.20 if is_opening_beat and cue_hits == 0 else 0.32
    tension = base + min(0.50, 0.16 * cue_hits)
    if "!" in text:
        tension += 0.08
    if time_of_day == TimeOfDay.NIGHT:
        tension += 0.04
    if weather != WeatherCondition.CLEAR:
        tension += 0.03
    return round(min(max(tension, 0.0), 1.0), 6)


def _infer_weather_from_opening_action(text: str) -> WeatherCondition | None:
    """Detect explicit atmospheric weather mentions in a scene's opening action line."""
    key = _canonical_key(text)
    # Require explicit meteorological nouns/adjectives (avoid matching lone 'صاف' in dialogue).
    atmospheric_cues: tuple[tuple[str, WeatherCondition], ...] = (
        ("طوفان شن", WeatherCondition.DUST),
        ("توفان شن", WeatherCondition.DUST),
        ("گرد و غبار", WeatherCondition.DUST),
        ("گردوغبار", WeatherCondition.DUST),
        ("ریزگرد", WeatherCondition.DUST),
        ("sandstorm", WeatherCondition.DUST),
        ("dust storm", WeatherCondition.DUST),
        ("مه آلود", WeatherCondition.FOG),
        ("مه غلیظ", WeatherCondition.FOG),
        ("thick fog", WeatherCondition.FOG),
        ("dense fog", WeatherCondition.FOG),
        ("heavy mist", WeatherCondition.FOG),
        ("کولاک", WeatherCondition.SNOW),
        ("برف", WeatherCondition.SNOW),
        ("blizzard", WeatherCondition.SNOW),
        ("snowflakes", WeatherCondition.SNOW),
        ("snow falls", WeatherCondition.SNOW),
        ("باران", WeatherCondition.RAIN),
        ("رگبار", WeatherCondition.RAIN),
        ("downpour", WeatherCondition.RAIN),
        ("thunderstorm", WeatherCondition.RAIN),
        ("rain", WeatherCondition.RAIN),
    )
    for phrase, condition in atmospheric_cues:
        if _contains_phrase(key, phrase):
            return condition
    return None


def _find_focus_characters(action_text: str, scene_characters: Sequence[str]) -> tuple[str, ...]:
    """Identify scene characters referenced inside an action beat, in order of mention."""
    canon_action = _canonical_key(action_text)
    matched: list[tuple[int, str]] = []
    padded = f" {canon_action} "
    for char in scene_characters:
        char_key = _canonical_key(char)
        candidates = [char_key]
        # Match final given/family name token for titled cues (e.g. "سرگرد رضا" -> "رضا").
        parts = char_key.split()
        if len(parts) >= 2 and len(parts[-1]) >= 3:
            candidates.append(parts[-1])
        for cand in candidates:
            pos = padded.find(f" {cand} ")
            if pos >= 0:
                matched.append((pos, char))
                break
    matched.sort(key=lambda item: item[0])
    return tuple(char for _, char in matched)


def _finalize_scene(
    scene_index: int,
    heading: SceneHeading,
    heading_had_explicit_weather: bool,
    raw_elements: list[SceneElement],
) -> ParsedScene:
    """Attach focus characters, optional atmospheric weather, and scene-level aggregates."""
    seen_chars: list[str] = []
    for elem in raw_elements:
        if isinstance(elem, DialogueLine):
            if elem.character not in seen_chars:
                seen_chars.append(elem.character)
        else:
            for fc in elem.focus_characters:
                if fc not in seen_chars:
                    seen_chars.append(fc)

    effective_heading = heading
    if (
        not heading_had_explicit_weather
        and heading.weather == WeatherCondition.CLEAR
        and raw_elements
        and isinstance(raw_elements[0], ActionBeat)
    ):
        inferred = _infer_weather_from_opening_action(raw_elements[0].text)
        if inferred is not None:
            effective_heading = SceneHeading(
                raw=heading.raw,
                scene_number=heading.scene_number,
                setting=heading.setting,
                location=heading.location,
                time_of_day=heading.time_of_day,
                weather=inferred,
                language=heading.language,
            )

    resolved_elements: list[SceneElement] = []
    for idx, elem in enumerate(raw_elements):
        if isinstance(elem, ActionBeat):
            detected_focus = _find_focus_characters(elem.text, seen_chars)
            merged_focus: list[str] = list(elem.focus_characters)
            for fc in detected_focus:
                if fc not in merged_focus:
                    merged_focus.append(fc)
            tension = _compute_action_tension(
                elem.text,
                effective_heading.time_of_day,
                effective_heading.weather,
                is_opening_beat=(idx == 0),
            )
            resolved_elements.append(
                ActionBeat(
                    element_index=idx,
                    text=elem.text,
                    language=elem.language,
                    duration_sec=elem.duration_sec,
                    dramatic_tension=tension,
                    focus_characters=tuple(merged_focus),
                    intentional_axis_jump=elem.intentional_axis_jump,
                    ambiguous_colon_action=elem.ambiguous_colon_action,
                )
            )
        else:
            resolved_elements.append(
                DialogueLine(
                    element_index=idx,
                    character=elem.character,
                    raw_text=elem.raw_text,
                    spoken_text=elem.spoken_text,
                    language=elem.language,
                    parentheticals=elem.parentheticals,
                    emotion=elem.emotion,
                    delivery_mode=elem.delivery_mode,
                    timing=elem.timing,
                    dramatic_tension=elem.dramatic_tension,
                    intentional_axis_jump=elem.intentional_axis_jump,
                )
            )

    if not resolved_elements:
        # An establishing heading-only scene still requires a minimal visual beat.
        establishing_tension = _compute_action_tension(
            effective_heading.location,
            effective_heading.time_of_day,
            effective_heading.weather,
            is_opening_beat=True,
        )
        resolved_elements.append(
            ActionBeat(
                element_index=0,
                text=effective_heading.location,
                language=effective_heading.language,
                duration_sec=2.0,
                dramatic_tension=establishing_tension,
                focus_characters=(),
                intentional_axis_jump=False,
            )
        )

    durations = [
        e.timing.total_duration_sec if isinstance(e, DialogueLine) else e.duration_sec
        for e in resolved_elements
    ]
    tensions = [e.dramatic_tension for e in resolved_elements]
    total_dur = round(sum(durations), 6)
    peak_tension = round(max(tensions), 6)
    mean_tension = round(sum(tensions) / len(tensions), 6)

    return ParsedScene(
        scene_index=scene_index,
        heading=effective_heading,
        elements=tuple(resolved_elements),
        characters=tuple(seen_chars),
        total_duration_sec=total_dur,
        peak_tension=peak_tension,
        mean_tension=mean_tension,
    )


def _heading_explicitly_names_weather(raw_heading: str) -> bool:
    """Return True if the raw slugline itself explicitly contained a weather keyword."""
    key = _canonical_key(raw_heading)
    return any(_contains_phrase(key, phrase) for phrase, _ in _WEATHER_PHRASES)


def parse_bilingual_script(script_text: str) -> ParsedScript:
    """Deterministically compile a Persian/English screenplay into a ``ParsedScript``.

    Raises :class:`ScriptParseError` if ``script_text`` is empty, not a string, or
    contains no recognizable scene heading.
    """
    normalized = normalize_script_text(script_text)
    if not normalized.strip():
        raise ScriptParseError("screenplay text is empty")

    source_sha256 = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    lines = [ln.strip() for ln in normalized.split("\n")]

    scenes: list[ParsedScene] = []
    current_heading: SceneHeading | None = None
    current_explicit_weather = False
    current_elements: list[SceneElement] = []
    pending_action: list[str] = []
    pending_axis_jump = False
    last_time_of_day: TimeOfDay | None = None

    def _flush_action() -> None:
        nonlocal pending_axis_jump
        if not pending_action or current_heading is None:
            pending_action.clear()
            return
        joined = " ".join(pending_action).strip()
        pending_action.clear()
        has_jump = pending_axis_jump or bool(_AXIS_JUMP_RE.search(joined))
        clean_text = re.sub(r"\s+", " ", _AXIS_JUMP_RE.sub(" ", joined)).strip()
        if not clean_text:
            # Standalone `[AXIS JUMP]` marker forwards to the next shot cleanly.
            pending_axis_jump = has_jump
            return
        pending_axis_jump = False
        current_elements.append(
            ActionBeat(
                element_index=len(current_elements),
                text=clean_text,
                language=detect_language(clean_text),
                duration_sec=estimate_action_duration(clean_text),
                dramatic_tension=0.30,
                focus_characters=(),
                intentional_axis_jump=has_jump,
            )
        )

    def _append_dialogue(
        char_name: str,
        delivery: DeliveryMode,
        header_parens: Sequence[str],
        raw_body: str,
    ) -> None:
        nonlocal pending_axis_jump
        assert current_heading is not None
        spoken, parens, inline_jump = _extract_inline_directives(raw_body, header_parens)
        axis_jump = pending_axis_jump or inline_jump
        pending_axis_jump = False
        emotion = classify_emotion(parens, spoken)
        timing = estimate_dialogue_timing(spoken, emotion=emotion, parentheticals=parens)
        tension = _compute_dialogue_tension(
            emotion,
            spoken,
            timing,
            current_heading.time_of_day,
            current_heading.weather,
        )
        lang_probe = f"{spoken} {' '.join(parens)}".strip() or char_name
        current_elements.append(
            DialogueLine(
                element_index=len(current_elements),
                character=char_name,
                raw_text=raw_body.strip(),
                spoken_text=spoken,
                language=detect_language(lang_probe),
                parentheticals=parens,
                emotion=emotion,
                delivery_mode=delivery,
                timing=timing,
                dramatic_tension=tension,
                intentional_axis_jump=axis_jump,
            )
        )

    idx = 0
    total_lines = len(lines)
    while idx < total_lines:
        line = lines[idx]

        # Blank line terminates any pending action paragraph.
        if not line:
            _flush_action()
            idx += 1
            continue

        # Editorial transition markers (FADE IN:, CUT TO:, کات به:, ...).
        if _TRANSITION_RE.match(line):
            _flush_action()
            idx += 1
            continue

        # Scene heading check.
        heading = parse_scene_heading(line, previous_time_of_day=last_time_of_day)
        if heading is not None:
            _flush_action()
            if current_heading is not None:
                scenes.append(
                    _finalize_scene(
                        len(scenes),
                        current_heading,
                        current_explicit_weather,
                        current_elements,
                    )
                )
            current_heading = heading
            current_explicit_weather = _heading_explicitly_names_weather(line)
            current_elements = []
            pending_axis_jump = False
            last_time_of_day = heading.time_of_day
            idx += 1
            continue

        if current_heading is None:
            raise ScriptParseError(
                f"content encountered before first scene heading at line {idx + 1}: {line!r}"
            )

        # Inline colon line: disambiguate unquoted 3rd-person stage action vs spoken dialogue.
        colon_parsed = _try_parse_colon_dialogue(line)
        if colon_parsed is not None:
            _flush_action()
            char_name, delivery, header_parens, body = colon_parsed
            if _is_unquoted_colon_stage_action(
                body,
                header_parens,
                delivery,
                forced_cue=line.startswith("@"),
            ):
                has_jump = pending_axis_jump or bool(_AXIS_JUMP_RE.search(body))
                pending_axis_jump = False
                clean_body = re.sub(r"\s+", " ", _AXIS_JUMP_RE.sub(" ", body)).strip()
                action_text = f"{char_name}: {clean_body}"
                current_elements.append(
                    ActionBeat(
                        element_index=len(current_elements),
                        text=action_text,
                        language=detect_language(action_text),
                        duration_sec=estimate_action_duration(clean_body),
                        dramatic_tension=0.30,
                        focus_characters=(char_name,),
                        intentional_axis_jump=has_jump,
                        ambiguous_colon_action=True,
                    )
                )
            else:
                _append_dialogue(char_name, delivery, header_parens, body)
            idx += 1
            continue

        # Standard multi-line dialogue cue block. Unambiguous ALL-CAPS English cues and
        # '@'-forced cues flush preceding action even without a blank line; Persian cues
        # require a preceding blank line so wrapped action prose is never split mid-sentence.
        next_line = lines[idx + 1] if idx + 1 < total_lines else None
        is_unambiguous_cue = line.startswith("@") or (
            bool(_LATIN_CHAR_RE.search(line)) and not bool(_PERSIAN_CHAR_RE.search(line))
        )
        if (not pending_action or is_unambiguous_cue) and _is_standalone_character_cue(
            line, next_line
        ):
            _flush_action()
            char_name, delivery, block_parens = _parse_character_header(line)
            dialogue_parts: list[str] = []
            idx += 1
            while idx < total_lines and lines[idx]:
                sub = lines[idx]
                if parse_scene_heading(sub, previous_time_of_day=last_time_of_day) is not None:
                    break
                if _TRANSITION_RE.match(sub):
                    break
                if sub.startswith("(") and sub.endswith(")") and sub.count("(") == 1:
                    block_parens.append(sub[1:-1].strip())
                else:
                    dialogue_parts.append(sub)
                idx += 1
            _append_dialogue(char_name, delivery, block_parens, " ".join(dialogue_parts))
            continue

        # Otherwise accumulate narrative action beat.
        pending_action.append(line)
        idx += 1

    _flush_action()
    if current_heading is not None:
        scenes.append(
            _finalize_scene(
                len(scenes),
                current_heading,
                current_explicit_weather,
                current_elements,
            )
        )

    if not scenes:
        raise ScriptParseError("screenplay contains no valid scene headings")

    all_characters: list[str] = []
    for sc in scenes:
        for ch in sc.characters:
            if ch not in all_characters:
                all_characters.append(ch)

    total_duration = round(sum(sc.total_duration_sec for sc in scenes), 6)
    script_lang = detect_language(normalized)

    return ParsedScript(
        scenes=tuple(scenes),
        language=script_lang,
        characters=tuple(all_characters),
        total_duration_sec=total_duration,
        source_sha256=source_sha256,
        normalized_source=normalized,
    )


def verify_script_provenance(
    script: ParsedScript | None,
    *,
    expected_source_sha256: str | None = None,
) -> bool:
    """Prove a sealed AST really is the deterministic parse of its own hashed source text.

    Every check is fail-closed *and* total: this is an untrusted-input boundary (a manifest may be
    deserialized), so a missing, ``None``, or wrongly typed field -- or anything raised while
    replaying the parser or comparing the AST -- yields ``False`` and never an exception.

    1. ``script.normalized_source`` must be a non-empty ``str`` whose SHA-256 equals
       ``script.source_sha256`` (also a ``str``), so the claimed provenance digest cannot be
       detached from, or forged independently of, the retained text.
    2. When ``expected_source_sha256`` is supplied it must itself be a well-formed 64-hex digest
       and equal ``script.source_sha256``, letting a deployment pin the screenplay it agreed to
       compile without a malformed pin silently passing.
    3. Re-running :func:`parse_bilingual_script` on that text must reproduce the AST *verbatim*
       (scenes, headings, every element, language, cast, and both durations). A forged or partially
       edited scene graph is caught even after all four manifest SHA-256 digests have been
       recomputed, because the replay never looks at the attacker's AST.
    """
    if script is None:
        return False
    source = getattr(script, "normalized_source", None)
    claimed = getattr(script, "source_sha256", None)
    if not isinstance(source, str) or not source or not isinstance(claimed, str):
        return False
    if hashlib.sha256(source.encode("utf-8")).hexdigest() != claimed:
        return False
    if expected_source_sha256 is not None:
        if (
            not isinstance(expected_source_sha256, str)
            or not _SHA256_HEX_RE.match(expected_source_sha256)
            or expected_source_sha256 != claimed
        ):
            return False
    try:
        replayed = parse_bilingual_script(source)
        # The comparison is between two independently built ``ParsedScript`` values, so a genuine
        # AST matches exactly and nothing else can. It is still inside the guard, because a hostile
        # AST may own an ``__eq__`` (or a field whose ``__eq__`` explodes) that raises on contact.
        return bool(replayed == script)
    except Exception:
        # Anything at all -- a parser refusal, a mistyped field, an exploding ``__eq__`` -- means
        # provenance cannot be established, so the answer is "not verified". Deliberately not
        # ``BaseException``: KeyboardInterrupt and SystemExit must still propagate.
        return False
