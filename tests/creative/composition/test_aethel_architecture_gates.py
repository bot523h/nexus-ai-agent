"""Wave 8 — AETHEL architecture gates (mechanical, AST-based).

These gates make the AETHEL doctrine enforceable rather than aspirational:

1. Determinism — the same input yields byte-identical findings and fingerprints.
2. No heavy/optional dependencies in the reasoning cores (stdlib + pydantic only).
3. No code-execution, no ambient ``Path.cwd()``, no unsafe deserialization.
4. No network egress and no cloud-AI SDKs (offline guarantee).
5. No re-creation of shared authority (CommandBus, AssetVersion, passport, DB).
6. Bounded input — the parsers carry explicit ``MAX_*`` limits and refuse oversize.

Scans use the AST (not raw text), so documentation prose that merely *mentions*
``eval``/``Path.cwd()`` cannot produce a false positive.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src" / "nexus_ai_agent" / "creative"

#: The AETHEL reasoning cores owned by this agent.
ZONES = (
    SRC / "composition",
    SRC / "director",
    SRC / "episode_causality_lab",
)

#: Heavy or optional third-party modules the offline cores must never import.
BANNED_IMPORTS = {
    "scipy",
    "cv2",
    "torch",
    "tensorflow",
    "moviepy",
    "PIL",
    "requests",
    "httpx",
    "urllib",
    "socket",
    "subprocess",
    "ctypes",
    "pickle",
    "openai",
    "anthropic",
    "google",
    "telegram",
    "sqlalchemy",
}

#: Names that would indicate a re-created shared authority.
BANNED_DEFINED = {"CommandBus", "AssetVersion", "Passport", "Revision"}


def _source_files() -> list[Path]:
    return sorted(p for zone in ZONES for p in zone.rglob("*.py"))


def _iter_imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield node.module.split(".")[0]


def test_no_banned_imports_in_reasoning_cores() -> None:
    offenders: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for top in _iter_imports(tree):
            if top in BANNED_IMPORTS:
                offenders.append(f"{path.name}: imports {top!r}")
    assert offenders == []


def test_no_code_execution_or_ambient_cwd() -> None:
    offenders: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"eval", "exec", "__import__", "compile"}:
                    offenders.append(f"{path.name}:{node.lineno} calls {node.func.id}()")
            if isinstance(node, ast.Attribute) and node.attr == "cwd":
                offenders.append(f"{path.name}:{node.lineno} reads .cwd()")
    assert offenders == []


def test_no_shared_authority_is_redefined() -> None:
    offenders: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in BANNED_DEFINED:
                offenders.append(f"{path.name}: defines {node.name}")
    assert offenders == []


def test_cores_are_offline_and_deterministic_on_repeat() -> None:
    from nexus_ai_agent.creative.director import (
        compile_fountain_to_story_document,
        parse_fountain,
    )
    from nexus_ai_agent.creative.episode_causality_lab import analyze_story

    script = "INT. ROOM - DAY\n\nAlice waits.\n\nALICE\nHello.\n"
    doc = compile_fountain_to_story_document(parse_fountain(script), "ep1")
    first = analyze_story(doc)
    second = analyze_story(doc)
    assert first.fingerprint == second.fingerprint
    assert first.causal.findings == second.causal.findings
    assert first.status is second.status


def test_parsers_are_bounded() -> None:
    from nexus_ai_agent.creative.director import InputBoundError, parse_fountain
    from nexus_ai_agent.creative.director.contracts import MAX_SCREENPLAY_LINES

    oversize = "\n".join("INT. ROOM - DAY" for _ in range(MAX_SCREENPLAY_LINES + 5))
    with pytest.raises(InputBoundError):
        parse_fountain(oversize)


def _minimal_raw(**overrides: object) -> dict:  # type: ignore[type-arg]
    base: dict = {  # type: ignore[type-arg]
        "schema_version": 1,
        "characters": [{"id": "alice"}, {"id": "bob"}],
        "props": [{"id": "gun"}],
        "locations": [{"id": "room"}],
        "episodes": [{"id": "ep1", "order_index": 0}],
        "scenes": [
            {
                "id": "sc1",
                "episode_id": "ep1",
                "location_id": "room",
                "order_index": 0,
                "time_of_day": "DAY",
            }
        ],
        "initial_facts": [],
        "events": [],
        "temporal_constraints": [],
        "beats": [],
        "shot_candidates": [],
    }
    base.update(overrides)
    return base


def test_story_parser_is_bounded() -> None:
    import json

    from nexus_ai_agent.creative.episode_causality_lab import LimitExceededError, loads_story

    events = [
        {
            "id": f"e{i}",
            "episode_id": "ep1",
            "scene_id": "sc1",
            "order_index": 0,
            "kind": "enter_location",
            "actor_id": "alice",
            "source": {"path": "ep1/sc1", "line": 0, "note": ""},
        }
        for i in range(200_000)
    ]
    with pytest.raises(LimitExceededError):
        loads_story(json.dumps(_minimal_raw(events=events)))


def test_story_document_round_trips_deterministically() -> None:
    import json

    from nexus_ai_agent.creative.episode_causality_lab import loads_story

    payload = json.dumps(_minimal_raw())
    doc = loads_story(payload)
    assert isinstance(doc.fingerprint(), str) and len(doc.fingerprint()) == 64
    assert loads_story(payload).fingerprint() == doc.fingerprint()
