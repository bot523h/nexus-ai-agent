"""AST reachability for advertisements. Does not import production modules."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _tuple_strings(node: ast.AST | None) -> set[str]:
    if not isinstance(node, ast.Tuple):
        return set()
    return {
        elt.value
        for elt in node.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    }


def _assign_strings(relative: str, name: str) -> set[str]:
    tree = ast.parse(_source(relative))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                found = _tuple_strings(node.value)
                if found:
                    return found
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    found = _tuple_strings(node.value)
                    if found:
                        return found
    return set()


def _return_keys(relative: str, function: str) -> set[str]:
    tree = ast.parse(_source(relative))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != function:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Return) and isinstance(child.value, ast.Dict):
                return {
                    key.value
                    for key in child.value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
    return set()


def test_ad_commands_are_registered_before_stubs() -> None:
    commands = _assign_strings("src/nexus_ai_agent/bot/feature_handlers.py", "OPS_COMMANDS")
    assert {"ad_create", "ad_list", "ad_pause", "ad_resume", "ad_delete", "ad_stats"} <= commands
    keys = _return_keys(
        "src/nexus_ai_agent/bot/feature_handlers.py", "build_feature_command_handlers"
    )
    assert {"ad_create", "ad_list", "ad_pause", "ad_resume", "ad_delete", "ad_stats"} <= keys


def test_real_ad_replies_do_not_reuse_simulated_success() -> None:
    handlers = _source("src/nexus_ai_agent/bot/feature_handlers.py")
    ads = _source("src/nexus_ai_agent/features/ads.py")
    for banned in ("5k impressions", "simulated", "Campaign #1 created"):
        assert banned not in handlers
        assert banned not in ads


def test_delivery_claims_before_send() -> None:
    ads = _source("src/nexus_ai_agent/features/ads.py")
    deliver = ads.split("async def deliver_due", 1)[1].split("async def", 1)[0]
    claim_at = deliver.index("claim_due")
    send_at = deliver.index("send_message")
    assert claim_at < send_at
