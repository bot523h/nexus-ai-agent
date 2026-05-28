from nexus_ai_agent.personality.engine import PersonalityEngine


def test_phi_is_formal():
    assert PersonalityEngine("phi").pv.formality > 0.7


def test_qwen_high_humor():
    assert PersonalityEngine("qwen").pv.humor_level > 0.6


def test_positive_raises_valence():
    pe = PersonalityEngine("gemma")
    before = pe.es.valence
    pe.update("Thank you, that was amazing!")
    assert pe.es.valence > before


def test_negative_lowers_valence():
    pe = PersonalityEngine("gemma")
    pe.es.valence = 0.6
    pe.update("This is terrible and broken")
    assert pe.es.valence < 0.6


def test_prompt_has_persona():
    pe = PersonalityEngine("phi")
    assert "phi" in pe.build_system_prompt("base").lower()


def test_default_is_gemma():
    assert PersonalityEngine().persona == "gemma"

