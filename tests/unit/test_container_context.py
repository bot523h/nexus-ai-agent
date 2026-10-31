"""Static build-context policy; not a substitute for a real Docker build."""

from pathlib import Path


def test_environment_secrets_are_excluded_from_container_context() -> None:
    root = Path(__file__).parents[2]
    rules = (root / ".dockerignore").read_text().splitlines()
    # Require explicit root and nested exclusions, with the template exception last.
    for pattern in (".env", ".env.*", "**/.env", "**/.env.*"):
        assert pattern in rules, f"COPY . . could include secrets without {pattern}"
        assert rules.index(pattern) < rules.index("!.env.example")
    assert rules[-2:] == ["!.env.example", "!**/.env.example"]
    assert (root / ".env.example").is_file()
