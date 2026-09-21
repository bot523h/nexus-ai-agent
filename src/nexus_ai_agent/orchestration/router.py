from __future__ import annotations

import re

# Arabic to Persian character mapping and diacritics removal
_ARABIC_TO_PERSIAN = str.maketrans({
    "ي": "ی",
    "ى": "ی",
    "ك": "ک",
    "ة": "ه",
    "ؤ": "و",
    "إ": "ا",
    "أ": "ا",
    "ء": "",
})

_DIACRITICS_RE = re.compile(r"[\u064B-\u0652\u0658]")


def normalize_intent_text(text: str) -> str:
    """Normalize English and Persian text for robust keyword and intent classification."""
    t = text.lower().strip()
    t = t.translate(_ARABIC_TO_PERSIAN)
    t = _DIACRITICS_RE.sub("", t)
    # Replace ZWNJ (\u200c) with space to cleanly match compound words
    t = t.replace("\u200c", " ")
    return t


TASK_KEYWORDS = [
    # English
    "create",
    "make",
    "build",
    "plan",
    "schedule",
    "write",
    "delete",
    "run",
    "execute",
    "save",
    "generate",
    "render",
    "compose",
    # Persian
    "بساز",
    "ایجاد",
    "تولید",
    "برنامه",
    "زمانبندی",
    "بنویس",
    "حذف",
    "پاک کن",
    "اجرا",
    "ذخیره",
    "ثبت",
    "طراحی",
    "انجام بده",
    "رندر",
]

MEMORY_KEYWORDS = [
    # English
    "remember",
    "recall",
    "what did",
    "last time",
    "my name is",
    "who am i",
    # Persian
    "یادت",
    "یادته",
    "خاطرت",
    "به خاطر بسپار",
    "چی گفتم",
    "چی گفتی",
    "دفعه قبل",
    "قبلا",
    "به یاد داری",
    "اسم من",
    "اسمم چیه",
    "یادت هست",
]


def classify_intent(text: str) -> str:
    t = normalize_intent_text(text)

    for kw in MEMORY_KEYWORDS:
        if kw in t:
            return "memory"

    for kw in TASK_KEYWORDS:
        if kw in t:
            return "task"

    return "chat"


_STORY = [
    # English
    "story",
    "tale",
    "once upon",
    "narrate",
    "fiction",
    "character",
    "adventure",
    "imagine",
    "roleplay",
    "continue",
    "chapter",
    "plot",
    "write a",
    "poem",
    # Persian
    "داستان",
    "قصه",
    "روایت",
    "شعر",
    "ماجرا",
    "رمان",
    "شخصیت",
    "افسانه",
    "تخیل",
    "نمایشنامه",
]
_LOGIC = [
    # English
    "analyze",
    "explain why",
    "how does",
    "compare",
    "reason",
    "calculate",
    "proof",
    "evidence",
    "fact check",
    "moderate",
    "logic",
    "code",
    "debug",
    "python",
    # Persian
    "تحلیل",
    "چرا",
    "چگونه",
    "مقایسه",
    "منطق",
    "محاسبه",
    "استدلال",
    "اثبات",
    "دلیل",
    "کدنویسی",
    "برنامه نویسی",
    "فرمول",
    "خطایابی",
]
_SOCIAL = [
    # English
    "feel",
    "sad",
    "happy",
    "lonely",
    "friend",
    "talk to me",
    "listen",
    "support",
    "advice",
    "how are you",
    "miss you",
    "love",
    "care",
    # Persian
    "حالم",
    "غمگین",
    "خوشحال",
    "تنها",
    "دوست",
    "گوش کن",
    "صحبت",
    "احساس",
    "سلام",
    "درود",
    "چطوری",
    "دلم",
    "مشاوره",
    "حرف بزن",
]


def select_persona(text: str) -> str:
    t = normalize_intent_text(text)
    for kw in _STORY:
        if kw in t:
            return "qwen"
    for kw in _LOGIC:
        if kw in t:
            return "phi"
    for kw in _SOCIAL:
        if kw in t:
            return "gemma"
    return "gemma"
