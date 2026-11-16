"""Comprehensive tests for the deterministic bilingual screenplay parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.creative.storyboard_compiler.bilingual_script_parser import (
    ActionBeat,
    DeliveryMode,
    DialogueLine,
    EmotionTag,
    SceneSetting,
    ScriptLanguage,
    ScriptParseError,
    TimeOfDay,
    WeatherCondition,
    classify_emotion,
    count_english_syllables,
    count_persian_syllables,
    detect_language,
    estimate_action_duration,
    estimate_dialogue_timing,
    normalize_script_text,
    parse_bilingual_script,
    parse_scene_heading,
)

FULL_PERSIAN_SCRIPT = """\
فید این:

۱. داخلی. اتاق بازجویی - شب - بارانی

لامپ کم‌نوری بالای میز فلزی نوسان می‌کند. سرگرد رضا پرونده را روی میز می‌کوبد!

سرگرد رضا
(با خشم)
نیمه‌شب کجا بودی؟! راستش را بگو!

سارا
(زمزمه)
(مکث کوتاه)
همه‌چیز را قبلاً گفتم... من آنجا نبودم، باور کن.

سرگرد رضا (با طعنه): فکر می‌کنی با این حرف‌ها پرونده بسته می‌شود؟

سارا (با ترس) (صدای خارج از قاب): صدای پا از راهرو می‌آید!

کات به:

سکانس ۲ - خارجی. پشت‌بام ساختمان قدیمی - سپیده‌دم - مه‌آلود

مه غلیظ شهر را پوشانده است. سارا و رضا کنار لبه می‌ایستند.

سارا
(غمگین)
دیگر راه برگشتی نمانده؛ همهٔ پل‌ها خراب شد.

[پرش محوری]

سرگرد رضا
(قاطع)
هنوز تمام نشده. تا آخرش می‌ایستیم.

خارجی. جاده کویری - ساعت طلایی - طوفان شن

باد شن‌های سرخ را روی آسفالت می‌پاشد.

سارا (نریشن): (با لبخند) خورشید دوباره طلوع کرد.

داخلی/خارجی. ایستگاه قطار - غروب - برفی

سرگرد رضا
(با عجله)
سریع سوار شو، قطار حرکت می‌کند!

خارجی. میدان شهر - ادامه

سارا
(با تردید)
نمی‌دانم... (مکث ۲ ثانیه) شاید حق با تو باشد.

فید اوت.
"""

FULL_ENGLISH_SCRIPT = """\
FADE IN:

1. INT. INTERROGATION ROOM - NIGHT - RAIN #1#

A bare tungsten bulb swings above a scarred steel table. REZA slams a folder down!

DETECTIVE REZA
(angrily)
Where were you at midnight?! Answer me!

SARA
(whispering)
(short pause)
I already told you everything... I wasn't there, believe me.

DETECTIVE REZA (CONT'D)
(sarcastically)
You really think that alibi holds water?

SMASH CUT TO:

SCENE 2 - EXT. OLD ROOFTOP - DAWN - FOG

SARA (V.O.)
(sadly)
There was no turning back; every bridge had burned.

[AXIS JUMP]

DETECTIVE REZA (O.S.)
(firmly)
It is not over yet. Stay steady.

EXT. DESERT HIGHWAY - GOLDEN_HOUR - DUST

SARA (happily): We finally made it past the border!

INT./EXT. MOUNTAIN STATION - DUSK - SNOW

DETECTIVE REZA
(urgently)
Get on the train right now—hurry!

EXT. HARBOR PIER - DAY - CLEAR

@McCLANE ^
(fearfully)
(pause 1.5s)
Somebody locked the gate behind us.

EXT. HARBOR ALLEY - CONTINUOUS

SARA
(hesitantly)
(long pause)
Wait... did you hear that?

FADE OUT.
"""

MIXED_BILINGUAL_SCRIPT = """\
داخلی. اتاق فرماندهی / CONTROL ROOM - شب (FOG)

مانیتورها تصویر خیابان را نشان می‌دهند.

SARA
(whispering)
رضا، سیگنال قطع شد—they found our frequency!

سرگرد رضا (urgently): Switch to channel 4 همین الان!

EXT. TEHRAN ALLEY - DUSK

باران تندی روی سنگفرش کوچه می‌بارد.

سارا
(مکث)
All clear... فعلاً امن است.

داخلی. راهرو تاریک - شب
"""


# --------------------------------------------------------------------------- #
# Normalization & Language Detection
# --------------------------------------------------------------------------- #


def test_normalize_script_text_handles_arabic_variants_crlf_and_bidi() -> None:
    raw = "\ufeff\u200fداخلي. كتابخانه\u0640 بزرگ - شب\r\nسلام دنيا\rپايان\u06c0"
    norm = normalize_script_text(raw)
    assert "\ufeff" not in norm
    assert "\u200f" not in norm
    assert "\r" not in norm
    assert "\u0640" not in norm
    assert norm.startswith("داخلی. کتابخانه بزرگ - شب\nسلام دنیا\nپایان")


def test_normalize_script_text_preserves_zwnj_and_rejects_non_string() -> None:
    assert "\u200c" in normalize_script_text("می\u200cروم")
    # Default normalize_digits=False preserves Persian and Arabic-Indic numerals in dialogue/prose.
    assert normalize_script_text("ساعت ۱۲:۳۰ و کد ٤٥") == "ساعت ۱۲:۳۰ و کد ٤٥"
    # Optional normalize_digits=True converts Persian (۰-۹) and Arabic-Indic (٠-٩) digits to ASCII.
    assert (
        normalize_script_text("ساعت ۱۲:۳۰ و کد ٠١٢٣٤٥٦٧٨٩", normalize_digits=True)
        == "ساعت 12:30 و کد 0123456789"
    )
    with pytest.raises(ScriptParseError, match="str"):
        normalize_script_text(123)  # type: ignore[arg-type]


def test_detect_language_fa_en_mixed_and_punctuation_only() -> None:
    assert detect_language("داخلی. اتاق بازجویی - شب") == ScriptLanguage.FA
    assert detect_language("INT. INTERROGATION ROOM - NIGHT") == ScriptLanguage.EN
    assert detect_language("سلام Hello دنیا") == ScriptLanguage.MIXED
    assert detect_language("12345 ...") == ScriptLanguage.EN


# --------------------------------------------------------------------------- #
# Scene Heading Extraction (Settings, All Times of Day, All Weather States)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw_line", "expected_setting", "expected_loc", "expected_tod", "expected_weather"),
    [
        (
            "داخلی. اتاق بازجویی - شب",
            SceneSetting.INT,
            "اتاق بازجویی",
            TimeOfDay.NIGHT,
            WeatherCondition.CLEAR,
        ),
        (
            "INT. INTERROGATION ROOM - NIGHT",
            SceneSetting.INT,
            "INTERROGATION ROOM",
            TimeOfDay.NIGHT,
            WeatherCondition.CLEAR,
        ),
        (
            "خارجی. خیابان ولیعصر - روز - آفتابی",
            SceneSetting.EXT,
            "خیابان ولیعصر",
            TimeOfDay.DAY,
            WeatherCondition.CLEAR,
        ),
        (
            "EXT. ROOFTOP - DAWN - FOG",
            SceneSetting.EXT,
            "ROOFTOP",
            TimeOfDay.DAWN,
            WeatherCondition.FOG,
        ),
        (
            "خارجی. ساحل شمال - غروب - باران شدید",
            SceneSetting.EXT,
            "ساحل شمال",
            TimeOfDay.DUSK,
            WeatherCondition.RAIN,
        ),
        (
            "INT./EXT. PATROL CAR - GOLDEN HOUR - DUST",
            SceneSetting.INT_EXT,
            "PATROL CAR",
            TimeOfDay.GOLDEN_HOUR,
            WeatherCondition.DUST,
        ),
        (
            "داخلی/خارجی. کلبه کوهستانی - سپیده‌دم - برفی",
            SceneSetting.INT_EXT,
            "کلبه کوهستانی",
            TimeOfDay.DAWN,
            WeatherCondition.SNOW,
        ),
        (
            "I/E. BRIDGE - NIGHT (RAIN)",
            SceneSetting.INT_EXT,
            "BRIDGE",
            TimeOfDay.NIGHT,
            WeatherCondition.RAIN,
        ),
        (
            "خارجی. کوچه بن‌بست (شب) (مه‌آلود)",
            SceneSetting.EXT,
            "کوچه بن‌بست",
            TimeOfDay.NIGHT,
            WeatherCondition.FOG,
        ),
        (
            "داخلی اتاق کار - شب بارانی",
            SceneSetting.INT,
            "اتاق کار",
            TimeOfDay.NIGHT,
            WeatherCondition.RAIN,
        ),
        (
            "INT KITCHEN - MORNING - RAIN",
            SceneSetting.INT,
            "KITCHEN",
            TimeOfDay.DAY,
            WeatherCondition.RAIN,
        ),
        (
            "EXT FOREST-DUSK",
            SceneSetting.EXT,
            "FOREST",
            TimeOfDay.DUSK,
            WeatherCondition.CLEAR,
        ),
        (
            "خارجی. بیابان - روز و گردوغبار",
            SceneSetting.EXT,
            "بیابان",
            TimeOfDay.DAY,
            WeatherCondition.DUST,
        ),
        (
            "INT. BUNKER - RAIN - NIGHT",
            SceneSetting.INT,
            "BUNKER",
            TimeOfDay.NIGHT,
            WeatherCondition.RAIN,
        ),
        (
            "INT. BASEMENT",
            SceneSetting.INT,
            "BASEMENT",
            TimeOfDay.DAY,
            WeatherCondition.CLEAR,
        ),
    ],
)
def test_parse_scene_heading_bilingual_variants(
    raw_line: str,
    expected_setting: SceneSetting,
    expected_loc: str,
    expected_tod: TimeOfDay,
    expected_weather: WeatherCondition,
) -> None:
    heading = parse_scene_heading(raw_line)
    assert heading is not None
    assert heading.setting == expected_setting
    assert heading.location == expected_loc
    assert heading.time_of_day == expected_tod
    assert heading.weather == expected_weather


def test_parse_scene_heading_rejects_non_headings_and_ambiguous_lines() -> None:
    assert parse_scene_heading("") is None
    assert parse_scene_heading("   ") is None
    assert parse_scene_heading("INT.") is None
    assert parse_scene_heading("INT. - NIGHT") is None
    assert parse_scene_heading("INT. ... - NIGHT") is None
    dual_paren = parse_scene_heading("INT. CAVE (RAIN) (FOG)")
    assert dual_paren is not None and dual_paren.weather == WeatherCondition.RAIN
    # Unpunctuated "داخلی" inside ordinary narrative prose must NOT match as a heading.
    assert parse_scene_heading("داخلی ساختمان بسیار تاریک بود.") is None
    assert parse_scene_heading("داخلی ساختمان - طبقه دوم") is None
    assert parse_scene_heading("SARA walks across the room.") is None


def test_parse_scene_heading_continuity_inherits_or_defaults_to_day() -> None:
    inherited = parse_scene_heading(
        "INT. HALLWAY - CONTINUOUS", previous_time_of_day=TimeOfDay.NIGHT
    )
    assert inherited is not None and inherited.time_of_day == TimeOfDay.NIGHT

    fallback = parse_scene_heading("داخلی. راهرو - ادامه", previous_time_of_day=None)
    assert fallback is not None and fallback.time_of_day == TimeOfDay.DAY

    sublocation = parse_scene_heading("INT. HOSPITAL - WARD B - NIGHT")
    assert sublocation is not None
    assert sublocation.location == "HOSPITAL - WARD B"
    assert sublocation.time_of_day == TimeOfDay.NIGHT


# --------------------------------------------------------------------------- #
# Syllable Counting & Emotion-Aware Timing Estimation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("the", 1),
        ("night", 1),
        ("table", 2),
        ("walked", 1),
        ("wanted", 2),
        ("makes", 1),
        ("voices", 2),
        ("whispering", 3),
        ("radio", 3),
        ("interrogation", 5),
        ("42", 2),
        ("...", 0),
    ],
)
def test_count_english_syllables(word: str, expected: int) -> None:
    assert count_english_syllables(word) == expected


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("شب", 1),
        ("مرد", 1),
        ("راست", 1),
        ("اسم", 1),
        ("سلام", 2),
        ("مادر", 2),
        ("آواز", 2),
        ("اتاق", 2),
        ("خانه", 2),
        ("سایه", 2),
        ("سرگرد", 2),
        ("یک", 1),
        ("می\u200cروم", 3),
        ("نیمه\u200cشب", 3),
        ("زمزمه", 3),
        ("بازجویی", 3),
        ("خانواده", 4),
        ("آسمانخراش", 4),
        ("مُتَشَکِّر", 4),
        ("۱۲۳", 3),
        ("؟!", 0),
    ],
)
def test_count_persian_syllables(word: str, expected: int) -> None:
    assert count_persian_syllables(word) == expected


def test_emotion_modulates_speech_rate_and_spoken_duration_monotonically() -> None:
    fa_line = "من تمام حقیقت را دربارهٔ آن شب بارانی به تو گفتم."
    en_line = "I already told you the entire truth about what happened that night."

    for line in (fa_line, en_line):
        urgent = estimate_dialogue_timing(line, emotion=EmotionTag.URGENT)
        angry = estimate_dialogue_timing(line, emotion=EmotionTag.ANGRY)
        neutral = estimate_dialogue_timing(line, emotion=EmotionTag.NEUTRAL)
        whisper = estimate_dialogue_timing(line, emotion=EmotionTag.WHISPER)
        sad = estimate_dialogue_timing(line, emotion=EmotionTag.SAD)
        hesitant = estimate_dialogue_timing(line, emotion=EmotionTag.HESITANT)

        assert urgent.spoken_duration_sec < angry.spoken_duration_sec
        assert angry.spoken_duration_sec < neutral.spoken_duration_sec
        assert neutral.spoken_duration_sec < whisper.spoken_duration_sec
        assert whisper.spoken_duration_sec < sad.spoken_duration_sec
        assert sad.spoken_duration_sec < hesitant.spoken_duration_sec
        assert urgent.effective_syllables_per_sec > hesitant.effective_syllables_per_sec


def test_pauses_and_silence_only_dialogue_timing() -> None:
    base = estimate_dialogue_timing("نه", emotion=EmotionTag.NEUTRAL)
    with_beat = estimate_dialogue_timing("نه", emotion=EmotionTag.NEUTRAL, parentheticals=("مکث",))
    with_numeric = estimate_dialogue_timing(
        "نه", emotion=EmotionTag.NEUTRAL, parentheticals=("مکث ۳ ثانیه",)
    )
    assert with_beat.pause_duration_sec == pytest.approx(base.pause_duration_sec + 0.60, abs=1e-5)
    assert with_numeric.pause_duration_sec == pytest.approx(base.pause_duration_sec + 3.0, abs=1e-5)

    silent = estimate_dialogue_timing("...", emotion=EmotionTag.NEUTRAL, parentheticals=("سکوت",))
    assert silent.total_word_count == 0
    assert silent.spoken_duration_sec == 0.0
    assert silent.pause_duration_sec == pytest.approx(1.20, abs=1e-5)
    assert silent.total_duration_sec == pytest.approx(1.20, abs=1e-5)

    assert estimate_action_duration("...") == pytest.approx(1.20, abs=1e-5)
    assert estimate_action_duration("رضا آرام به سمت پنجره می‌رود و بیرون را نگاه می‌کند.") > 2.0


def test_classify_emotion_covers_lexicon_and_punctuation_fallbacks() -> None:
    assert classify_emotion(("با خشم",)) == EmotionTag.ANGRY
    assert classify_emotion(("زمزمه",)) == EmotionTag.WHISPER
    assert classify_emotion(("whispering",)) == EmotionTag.WHISPER
    assert classify_emotion(("با ترس",)) == EmotionTag.FEARFUL
    assert classify_emotion(("غمگین",)) == EmotionTag.SAD
    assert classify_emotion(("با خنده",)) == EmotionTag.JOYFUL
    assert classify_emotion(("با عجله",)) == EmotionTag.URGENT
    assert classify_emotion(("با تردید",)) == EmotionTag.HESITANT
    assert classify_emotion(("با طعنه",)) == EmotionTag.SARCASTIC
    assert classify_emotion(("قاطع",)) == EmotionTag.RESOLUTE
    assert classify_emotion(("   ",), "چرا جواب نمی‌دهی؟!") == EmotionTag.ANGRY
    assert classify_emotion((), "شاید... فردا.") == EmotionTag.HESITANT
    assert classify_emotion((), "بله.") == EmotionTag.NEUTRAL


# --------------------------------------------------------------------------- #
# Full Persian, English, and Mixed Bilingual Screenplay Parsing
# --------------------------------------------------------------------------- #


def test_parse_full_persian_screenplay() -> None:
    parsed = parse_bilingual_script(FULL_PERSIAN_SCRIPT)
    assert parsed.language == ScriptLanguage.FA
    assert len(parsed.scenes) == 5
    assert parsed.characters == ("سرگرد رضا", "سارا")
    assert len(parsed.source_sha256) == 64
    assert parsed.total_duration_sec > 15.0

    s0, s1, s2, s3, s4 = parsed.scenes
    assert s0.heading.scene_number == 1
    assert s0.heading.setting == SceneSetting.INT
    assert s0.heading.location == "اتاق بازجویی"
    assert s0.heading.time_of_day == TimeOfDay.NIGHT
    assert s0.heading.weather == WeatherCondition.RAIN

    # Verify action beat focus characters + emotions + delivery modes in scene 0.
    assert isinstance(s0.elements[0], ActionBeat)
    assert s0.elements[0].focus_characters == ("سرگرد رضا",)
    d0 = s0.elements[1]
    d1 = s0.elements[2]
    d2 = s0.elements[3]
    d3 = s0.elements[4]
    assert isinstance(d0, DialogueLine) and d0.emotion == EmotionTag.ANGRY
    assert isinstance(d1, DialogueLine) and d1.emotion == EmotionTag.WHISPER
    assert isinstance(d2, DialogueLine) and d2.emotion == EmotionTag.SARCASTIC
    assert isinstance(d3, DialogueLine) and d3.emotion == EmotionTag.FEARFUL
    assert d3.delivery_mode == DeliveryMode.OFF_SCREEN

    # Scene 1: DAWN + FOG + standalone [پرش محوری] forwarded onto Reza's line.
    assert s1.heading.scene_number == 2
    assert s1.heading.setting == SceneSetting.EXT
    assert s1.heading.time_of_day == TimeOfDay.DAWN
    assert s1.heading.weather == WeatherCondition.FOG
    assert isinstance(s1.elements[0], ActionBeat)
    assert set(s1.elements[0].focus_characters) == {"سارا", "سرگرد رضا"}
    assert isinstance(s1.elements[1], DialogueLine) and s1.elements[1].emotion == EmotionTag.SAD
    assert isinstance(s1.elements[2], DialogueLine)
    assert s1.elements[2].emotion == EmotionTag.RESOLUTE
    assert s1.elements[2].intentional_axis_jump is True

    # Scene 2: GOLDEN_HOUR + DUST + VOICE_OVER + JOYFUL.
    assert s2.heading.time_of_day == TimeOfDay.GOLDEN_HOUR
    assert s2.heading.weather == WeatherCondition.DUST
    assert isinstance(s2.elements[1], DialogueLine)
    assert s2.elements[1].delivery_mode == DeliveryMode.VOICE_OVER
    assert s2.elements[1].emotion == EmotionTag.JOYFUL

    # Scene 3: INT_EXT + DUSK + SNOW + URGENT.
    assert s3.heading.setting == SceneSetting.INT_EXT
    assert s3.heading.time_of_day == TimeOfDay.DUSK
    assert s3.heading.weather == WeatherCondition.SNOW
    assert isinstance(s3.elements[0], DialogueLine) and s3.elements[0].emotion == EmotionTag.URGENT

    # Scene 4: CONTINUOUS ("ادامه") inherits DUSK from Scene 3; inline 2s pause parsed.
    assert s4.heading.time_of_day == TimeOfDay.DUSK
    assert isinstance(s4.elements[0], DialogueLine)
    assert s4.elements[0].emotion == EmotionTag.HESITANT
    assert s4.elements[0].timing.pause_duration_sec > 2.5


def test_parse_full_english_screenplay() -> None:
    parsed = parse_bilingual_script(FULL_ENGLISH_SCRIPT)
    assert parsed.language == ScriptLanguage.EN
    assert len(parsed.scenes) == 6
    assert parsed.characters == ("DETECTIVE REZA", "SARA", "McCLANE")

    assert parsed.scenes[0].heading.setting == SceneSetting.INT
    assert parsed.scenes[0].heading.time_of_day == TimeOfDay.NIGHT
    assert parsed.scenes[0].heading.weather == WeatherCondition.RAIN

    assert parsed.scenes[1].heading.time_of_day == TimeOfDay.DAWN
    assert parsed.scenes[1].heading.weather == WeatherCondition.FOG
    vo_line = parsed.scenes[1].elements[0]
    os_line = parsed.scenes[1].elements[1]
    assert isinstance(vo_line, DialogueLine) and vo_line.delivery_mode == DeliveryMode.VOICE_OVER
    assert isinstance(os_line, DialogueLine) and os_line.delivery_mode == DeliveryMode.OFF_SCREEN
    assert os_line.intentional_axis_jump is True

    assert parsed.scenes[2].heading.time_of_day == TimeOfDay.GOLDEN_HOUR
    assert parsed.scenes[2].heading.weather == WeatherCondition.DUST
    assert parsed.scenes[3].heading.time_of_day == TimeOfDay.DUSK
    assert parsed.scenes[3].heading.weather == WeatherCondition.SNOW
    assert parsed.scenes[4].heading.time_of_day == TimeOfDay.DAY
    assert parsed.scenes[4].heading.weather == WeatherCondition.CLEAR
    assert parsed.scenes[5].heading.time_of_day == TimeOfDay.DAY


def test_parse_mixed_bilingual_screenplay_and_edge_cases() -> None:
    parsed = parse_bilingual_script(MIXED_BILINGUAL_SCRIPT)
    assert parsed.language == ScriptLanguage.MIXED
    assert len(parsed.scenes) == 3

    # Scene 0 has mixed heading and code-switched lines with both FA and EN tokens.
    s0 = parsed.scenes[0]
    assert s0.heading.language == ScriptLanguage.MIXED
    assert s0.heading.weather == WeatherCondition.FOG
    d0 = s0.elements[1]
    d1 = s0.elements[2]
    assert isinstance(d0, DialogueLine)
    assert d0.language == ScriptLanguage.MIXED
    assert d0.timing.persian_word_count >= 3
    assert d0.timing.english_word_count >= 4
    assert isinstance(d1, DialogueLine) and d1.emotion == EmotionTag.URGENT

    # Scene 1 had no weather in slugline, so opening action inferred RAIN automatically.
    s1 = parsed.scenes[1]
    assert s1.heading.weather == WeatherCondition.RAIN

    # Scene 2 is a heading-only establishing scene -> receives a 2.0s establishing ActionBeat.
    s2 = parsed.scenes[2]
    assert len(s2.elements) == 1
    assert isinstance(s2.elements[0], ActionBeat)
    assert s2.elements[0].duration_sec == pytest.approx(2.0)


def test_colon_false_positives_and_block_terminators() -> None:
    script = """\
INT. STATION - NIGHT
The clock reads 12:30 on the wall.
a note: stay quiet tonight.
One two three four five: not a character name.
Notice this very long descriptive prefix before a colon: nothing speaks.
    1234: numeric label only.
    STOP!: punctuated prefix.
    SARA:
    @: empty forced name.

    she pauses quietly
    and looks back.

    SARA
    Hello (   ) there!

    SILENCE
    INT. HALLWAY - NIGHT
    SARA
    Goodbye!
    CUT TO:
    EXT. ROOF - NIGHT
    SARA
    Look up!
    EXT. STREET - DAY
The morning sun warms the pavement.

رضا در را می‌بندد
و آرام بیرون می‌رود.

@ripley: Copy that.
"""
    parsed = parse_bilingual_script(script)
    assert len(parsed.scenes) == 4
    # First scene: action beats + SARA dialogue + trailing SILENCE action before next heading.
    assert isinstance(parsed.scenes[0].elements[0], ActionBeat)
    assert isinstance(parsed.scenes[0].elements[1], ActionBeat)
    assert isinstance(parsed.scenes[0].elements[2], DialogueLine)
    assert parsed.scenes[0].elements[2].character == "SARA"
    assert parsed.scenes[0].elements[2].spoken_text == "Hello there!"
    assert isinstance(parsed.scenes[0].elements[3], ActionBeat)
    # Second scene: SARA dialogue cleanly stopped before CUT TO:.
    assert len(parsed.scenes[1].elements) == 1
    assert isinstance(parsed.scenes[1].elements[0], DialogueLine)
    # Third scene: SARA dialogue cleanly stopped directly before next scene heading.
    assert len(parsed.scenes[2].elements) == 1
    assert isinstance(parsed.scenes[2].elements[0], DialogueLine)
    # Fourth scene: Persian two-line action with verb ending NOT misread as character cue,
    # plus forced @ripley colon dialogue.
    assert isinstance(parsed.scenes[3].elements[0], ActionBeat)
    assert isinstance(parsed.scenes[3].elements[1], ActionBeat)
    assert isinstance(parsed.scenes[3].elements[2], DialogueLine)
    assert parsed.scenes[3].elements[2].character == "ripley"


def test_colon_action_disambiguation_vs_spoken_dialogue_and_heuristic_docs() -> None:
    import nexus_ai_agent.creative.storyboard_compiler.bilingual_script_parser as parser_mod

    # F4: Unquoted 3rd-person stage-direction colon lines are classified as ActionBeat
    # with ambiguous_colon_action=True and character focus, NOT as spoken DialogueLine.
    script = """\
داخلی. اتاق نشیمن - شب

علی: با دلخوری نگاه می‌کند.
علی: «من دیگر برنمی‌گردم.»
سارا: آرام کنار پنجره می‌نشیند.
سارا: به علی خیره می‌شود.
علی: من دیگر خسته شده‌ام.
سارا: کجا می‌رود؟
سارا: فردا دیر است.
سارا: (با مکث)
سارا: ...
ALI: stares quietly at the door.
ALI: quietly stares at the door.
SARA: slowly walks toward the window.
REZA: still stands by the table.
ALI: quietly, nothing happens.
ALI: Tomorrow will be fine.
"""
    parsed = parse_bilingual_script(script)
    assert len(parsed.scenes) == 1
    elems = parsed.scenes[0].elements

    # 0 -> "علی: با دلخوری نگاه می‌کند." -> ActionBeat with character focus & flag.
    e0 = elems[0]
    assert isinstance(e0, ActionBeat)
    assert e0.ambiguous_colon_action is True
    assert e0.focus_characters == ("علی",)

    # 1 -> "علی: «من دیگر برنمی‌گردم.»" -> genuine quoted DialogueLine.
    e1 = elems[1]
    assert isinstance(e1, DialogueLine)
    assert e1.character == "علی"
    assert "برنمی‌گردم" in e1.spoken_text
    assert e1.timing.persian_syllable_count >= 6

    # 2 & 3 -> unquoted 3rd-person actions ("می‌نشیند", "به علی خیره می‌شود").
    e2 = elems[2]
    e3 = elems[3]
    assert isinstance(e2, ActionBeat) and e2.ambiguous_colon_action is True
    assert e2.focus_characters == ("سارا",)
    assert isinstance(e3, ActionBeat) and e3.ambiguous_colon_action is True
    assert e3.focus_characters == ("سارا", "علی")

    # 4..8 -> first-person speech, interrogative, declarative speech, silent beat -> DialogueLine.
    assert isinstance(elems[4], DialogueLine) and elems[4].character == "علی"
    assert isinstance(elems[5], DialogueLine) and elems[5].character == "سارا"
    assert isinstance(elems[6], DialogueLine) and elems[6].character == "سارا"
    assert isinstance(elems[7], DialogueLine) and elems[7].character == "سارا"
    assert isinstance(elems[8], DialogueLine) and elems[8].character == "سارا"

    # 9..12 -> English unquoted 3rd-person colon stage directions (direct & adverb-led).
    for action_idx, expected_char in ((9, "ALI"), (10, "ALI"), (11, "SARA"), (12, "REZA")):
        act_elem = elems[action_idx]
        assert isinstance(act_elem, ActionBeat)
        assert act_elem.ambiguous_colon_action is True
        assert act_elem.focus_characters == (expected_char,)

    # 13 & 14 -> English spoken dialogue (adverb + non-stage-verb & declarative sentence).
    assert isinstance(elems[13], DialogueLine) and elems[13].character == "ALI"
    assert isinstance(elems[14], DialogueLine) and elems[14].character == "ALI"

    # Also verify a scene containing ONLY an ambiguous colon action registers the character
    # and handles duplicate focus character entries cleanly.
    solo = parse_bilingual_script("داخلی. راهرو - شب\nعلی: آرام وارد می‌شود و سکوت می‌کند.")
    assert solo.characters == ("علی",)
    assert isinstance(solo.scenes[0].elements[0], ActionBeat)
    assert solo.scenes[0].elements[0].ambiguous_colon_action is True
    assert solo.scenes[0].elements[0].focus_characters == ("علی",)

    # F6: Verify speech-rate priors carry both `#: Deterministic heuristic` comments & docstrings.
    src_text = Path(parser_mod.__file__).read_text(encoding="utf-8")
    assert src_text.count("#: Deterministic heuristic") >= 3
    assert parser_mod.BASE_FA_SYLLABLES_PER_SEC == pytest.approx(4.4)
    assert parser_mod.BASE_EN_SYLLABLES_PER_SEC == pytest.approx(4.2)
    assert parser_mod.EMOTION_SPEECH_RATE[EmotionTag.NEUTRAL] == pytest.approx(1.0)


def test_parser_refusals_on_empty_or_headingless_scripts() -> None:
    with pytest.raises(ScriptParseError, match="empty"):
        parse_bilingual_script("   \n\t  ")
    with pytest.raises(ScriptParseError, match="before first scene heading"):
        parse_bilingual_script("SARA\nHello without a scene heading.")
    with pytest.raises(ScriptParseError, match="no valid scene headings"):
        parse_bilingual_script("FADE IN:\n\nCUT TO:\n\nFADE OUT.")
