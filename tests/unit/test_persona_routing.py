from nexus_ai_agent.orchestration.router import select_persona


def test_story_to_qwen():
    assert select_persona("Tell me a story") == "qwen"


def test_logic_to_phi():
    assert select_persona("Analyze this problem") == "phi"


def test_social_to_gemma():
    assert select_persona("I feel so lonely") == "gemma"


def test_default_gemma():
    assert select_persona("hello") == "gemma"


def test_roleplay_to_qwen():
    assert select_persona("Let's roleplay a fantasy") == "qwen"
