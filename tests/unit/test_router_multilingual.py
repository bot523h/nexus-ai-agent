"""Unit tests for multilingual (Persian & English) intent classification and persona routing."""

from __future__ import annotations

import pytest

from nexus_ai_agent.orchestration.router import (
    classify_intent,
    normalize_intent_text,
    select_persona,
)


def test_normalize_intent_text() -> None:
    # Arabic Yeh and Kaf conversion
    raw = "يَك كِتاب پاكيزه"
    normalized = normalize_intent_text(raw)
    assert "ی" in normalized
    assert "ک" in normalized
    assert "ي" not in normalized
    assert "ك" not in normalized

    # ZWNJ removal
    zwnj_text = "می\u200cخواهم"
    assert normalize_intent_text(zwnj_text) == "می خواهم"


@pytest.mark.parametrize(
    ("prompt", "expected_intent"),
    [
        # English intents
        ("Please create a plan for tomorrow", "task"),
        ("Remember that my favorite color is blue", "memory"),
        ("Hello, how are you today?", "chat"),
        # Persian task intents
        ("لطفاً برای من یک جدول زمانبندی بساز", "task"),
        ("برنامه روزانه من را ایجاد کن", "task"),
        ("یک اسلایدشو جدید رندر کن", "task"),
        ("این پیام را حذف کن", "task"),
        # Persian memory intents
        ("یادت هست دیروز چی گفتم؟", "memory"),
        ("به خاطر بسپار که من مهندس نرم افزار هستم", "memory"),
        ("اسم من چیه؟", "memory"),
        ("دفعه قبل درباره چه چیزی صحبت کردیم؟", "memory"),
        # Persian general chat
        ("سلام، امروز هوا چطوره؟", "chat"),
        ("خیلی ممنون از همراهیت", "chat"),
    ],
)
def test_classify_intent_multilingual(prompt: str, expected_intent: str) -> None:
    assert classify_intent(prompt) == expected_intent


@pytest.mark.parametrize(
    ("prompt", "expected_persona"),
    [
        # Story persona (qwen)
        ("یک داستان کوتاه درباره سفر به مریخ بنویس", "qwen"),
        ("روایت یک قهرمان در افسانه های کهن", "qwen"),
        ("یک شعر زیبا برایم بگو", "qwen"),
        ("Tell me a fantasy story", "qwen"),
        # Logic/Technical persona (phi)
        ("این مسئله ریاضی را تحلیل کن و دلیل آن را بگو", "phi"),
        ("چرا کدهای ناهمگام سریعتر اجرا می شوند؟ مقایسه کن", "phi"),
        ("یک اسکریپت پایتون بنویس و خطایابی کن", "phi"),
        ("Analyze this logic statement", "phi"),
        # Social/Empathetic persona (gemma)
        ("امروز خیلی حالم گرفته است و احساس تنهایی می کنم", "gemma"),
        ("سلام دوست من، چطوری؟", "gemma"),
        ("به من گوش کن و مشاوره بده", "gemma"),
        ("I feel lonely today, please talk to me", "gemma"),
    ],
)
def test_select_persona_multilingual(prompt: str, expected_persona: str) -> None:
    assert select_persona(prompt) == expected_persona
