"""Regression guards for untrusted filenames entering storage remote keys."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
HANDLERS = ROOT / "src/nexus_ai_agent/bot/handlers.py"


def _function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _has_attribute(node: ast.AST, value: str, attr: str) -> bool:
    return any(
        isinstance(child, ast.Attribute)
        and isinstance(child.value, ast.Name)
        and child.value.id == value
        and child.attr == attr
        for child in ast.walk(node)
    )


def test_cloud_upload_uses_the_sanitized_external_filename_as_its_remote_key() -> None:
    tree = ast.parse(HANDLERS.read_text(encoding="utf-8"), filename=str(HANDLERS))
    cloud_cmd = _function(tree, "cloud_cmd")

    upload_calls = [
        node
        for node in ast.walk(cloud_cmd)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "upload_file"
    ]
    assert len(upload_calls) == 1
    upload = upload_calls[0]
    remote_key = next(keyword.value for keyword in upload.keywords if keyword.arg == "remote_key")
    assert isinstance(remote_key, ast.Name) and remote_key.id == "safe_name", (
        "Telegram's raw document filename must never be used as the remote key"
    )

    sanitizers = [
        node
        for node in ast.walk(cloud_cmd)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "safe_name" for target in node.targets
        )
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "sanitize_file_name"
    ]
    assert len(sanitizers) == 1
    sanitizer = sanitizers[0]
    assert sanitizer.lineno < upload.lineno, "sanitize the external name before remote-key use"
    assert _has_attribute(sanitizer.value, "doc", "file_name")
