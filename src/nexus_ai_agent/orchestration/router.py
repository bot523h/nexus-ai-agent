from __future__ import annotations

import re

# Arabic to Persian character mapping and diacritics removal
_ARABIC_TO_PERSIAN = str.maketrans(
    {
        "ي": "ی",
        "ى": "ی",
        "ك": "ک",
        "ة": "ه",
        "ؤ": "و",
        "إ": "ا",
        "أ": "ا",
        "ء": "",
    }
)

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


# ── memory-read policy ────────────────────────────────────────────────────
#
# Root cause this makes explicit: ``orchestration/graph.py::route_intent`` sends
# ``task`` → memory_reader_task and ``memory`` → memory_reader_chat, and every
# other intent straight to ``route_persona``. Ordinary ``chat`` — the common
# case — therefore never reads long-term memory, while ``_memory_writer`` stores
# a turn on *every* path. Memory was write-always / read-sometimes, and the
# decision to read was encoded as an *intent* classification.
#
# That is the wrong axis. Whether a turn should consult memory is a question
# about relevance and cost, not about whether the user happened to type one of
# the ~20 literals in ``MEMORY_KEYWORDS``. Asking "what was my project called?"
# contains no memory keyword, so it was answered with an empty memory context
# even though the answer was stored.
#
# The policy below is the corrected rule, stated once and tested here so the
# graph wiring is a one-line change rather than a design change. It lives in the
# router (not the graph) because the router already owns intent semantics, and
# because ``graph.py`` is under another agent's lease.

#: Every intent a turn can carry. ``classify_intent`` returns the first three;
#: ``"unknown"`` is declared by ``NexusState`` but currently unreachable — see
#: the value-engineering audit for that finding.
ALL_INTENTS: tuple[str, ...] = ("chat", "task", "memory", "unknown")


def should_read_memory(intent: str, text: str = "") -> bool:
    """Whether a turn must consult long-term memory before answering.

    Unconditionally ``True``, and total over every input — including intents
    nobody declared. "We could not classify this turn" is not evidence that
    memory is irrelevant, so an unrecognised intent must not silently opt out of
    recall; that is precisely the failure this policy replaces.

    The cost of always looking is one indexed ``COUNT(*)`` plus a BM25 pass, and
    :meth:`LongTermMemory.recall` reports ``EMPTY`` immediately when the thread
    has no rows. Reading is cheap enough that gating it bought nothing and lost
    context.

    Both parameters are accepted and deliberately unused. ``intent`` stays in the
    signature because the defect *was* an intent gate — keeping the axis visible
    at the call site is what stops it being silently reintroduced — and ``text``
    gives a future cost-aware policy (skip a pure command turn, say) somewhere to
    live without another signature change.
    """
    _ = intent, text
    return True
