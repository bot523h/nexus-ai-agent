"""Vision remains a pure, registry-only planning pack."""

from pathlib import Path

ROOT = Path(__file__).parents[2] / "src/nexus_ai_agent/creative/packs"


def test_vision_has_no_direct_io_or_execution_escape_hatches():
    source = "\n".join(
        p.read_text() for d in ("vision", "portrait", "scene") for p in (ROOT / d).glob("*.py")
    )
    forbidden = (
        "subprocess",
        "os.system",
        "nexus_ai_agent.storage",
        "nexus_ai_agent.llm",
        "eval(",
        "exec(",
        "shader_code",
        "filtergraph",
    )
    assert not [token for token in forbidden if token in source]


def test_manifests_are_data_and_pixel_boundary_is_honest():
    for directory in ("portrait", "scene"):
        raw = (ROOT / directory / "pack.manifest.json").read_text()
        assert "entrypoint" not in raw and "post_install" not in raw and "shell" not in raw
    operations = (ROOT / "vision" / "operations.py").read_text()
    assert '"pixel_execution": False' in operations
    assert '"execution_boundary": "deterministic_plan_only"' in operations
