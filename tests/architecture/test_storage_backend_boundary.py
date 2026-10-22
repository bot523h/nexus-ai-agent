"""Boundary laws for the storage-backend layer (task-272).

Law S1 — the contract is dependency-free: ``storage/object_store.py`` imports
    only the standard library, so every layer can depend on it.
Law S2 — the Neon adapter never imports its SDK at module import time: an
    installation without ``boto3`` or without credentials must still start.
Law S3 — no new storage-backend module imports a web framework, the ORM, the
    Telegram SDK or the legacy fake-capacity routing (``storage/unified_cloud``).
Law S4 — the layering is one-way: ``creative.assets`` may be composed *by* the
    local compatibility adapter, but the asset library must not reach into the
    new backends (no second, competing wiring path).
Law S5 — credentials stay out of the dataclass ``repr``.

Each law is a real import-graph assertion over the tree, not a comment.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nexus_ai_agent.storage.local_object_store import LocalObjectStore
from nexus_ai_agent.storage.neon_object_storage import NeonObjectStorage, NeonStorageConfig
from nexus_ai_agent.storage.object_store import ObjectStore

ROOT = Path(__file__).parents[2]
STORAGE = ROOT / "src/nexus_ai_agent/storage"

CONTRACT = STORAGE / "object_store.py"
NEON = STORAGE / "neon_object_storage.py"
LOCAL = STORAGE / "local_object_store.py"
NEW_MODULES = (CONTRACT, NEON, LOCAL)

FORBIDDEN_ROOTS = {"telegram", "sqlmodel", "fastapi", "uvicorn", "pydantic", "httpx", "typer"}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(path: Path, *, top_level_only: bool = False) -> set[str]:
    """Every imported dotted name, including ``from x import y`` leaf segments."""
    tree = _tree(path)
    nodes = tree.body if top_level_only else list(ast.walk(tree))
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _roots(names: set[str]) -> set[str]:
    return {name.split(".")[0] for name in names}


def test_law_s1_contract_imports_only_the_standard_library() -> None:
    imports = _imports(CONTRACT)
    third_party = {name for name in imports if name.startswith("nexus_ai_agent")}
    assert not third_party, f"the contract must not depend on the tree: {sorted(third_party)}"
    assert not _roots(imports) & FORBIDDEN_ROOTS
    assert "boto3" not in _roots(imports)


def test_law_s2_neon_sdk_is_imported_lazily() -> None:
    top_level = _roots(_imports(NEON, top_level_only=True))
    assert "boto3" not in top_level
    assert "botocore" not in top_level
    # ... and it is genuinely imported somewhere (a lazy import, not a dead one).
    assert {"boto3", "botocore"} & _roots(_imports(NEON)), (
        "the adapter must import boto3/botocore inside its client method; removing "
        "them entirely would mean the adapter cannot build a real client"
    )


def test_law_s3_new_modules_do_not_import_frameworks_or_the_fake_capacity_router() -> None:
    problems: list[str] = []
    for path in NEW_MODULES:
        imports = _imports(path)
        offending = _roots(imports) & FORBIDDEN_ROOTS
        if offending:
            problems.append(f"{path.name}: forbidden imports {sorted(offending)}")
        if "nexus_ai_agent.storage.unified_cloud" in imports:
            problems.append(
                f"{path.name}: imports unified_cloud — its PROVIDER_LIMITS are advertised "
                "free-tier numbers, not measurements"
            )
        if "nexus_ai_agent.storage.providers" in {name.rsplit(".", 1)[0] for name in imports}:
            problems.append(
                f"{path.name}: imports the legacy maintenance tier; the two storage "
                "seams stay separate (R2 keeps its backup/RAG role)"
            )
    assert not problems, "\n".join(problems)


def test_law_s4_asset_library_does_not_reach_into_the_new_backends() -> None:
    asset_modules = (ROOT / "src/nexus_ai_agent/creative/assets").rglob("*.py")
    offenders = [
        str(path.relative_to(ROOT))
        for path in asset_modules
        if any(
            name.startswith(
                (
                    "nexus_ai_agent.storage.object_store",
                    "nexus_ai_agent.storage.neon_object_storage",
                    "nexus_ai_agent.storage.local_object_store",
                )
            )
            for name in _imports(path)
        )
    ]
    assert not offenders, (
        "the Asset Library must be wired to the new backend through an explicit "
        f"composition step, not import it directly: {offenders}"
    )


def test_local_adapter_is_the_only_composition_point_for_the_asset_store() -> None:
    """One compatibility adapter, not a second content-addressed implementation."""
    importers = {
        path.relative_to(ROOT).as_posix()
        for path in STORAGE.rglob("*.py")
        if any("nexus_ai_agent.creative.assets" in name for name in _imports(path))
    }
    assert importers == {LOCAL.relative_to(ROOT).as_posix()}, (
        "exactly the local compatibility adapter may compose the existing "
        f"content-addressed store; found {sorted(importers)}"
    )


def test_law_s5_credentials_are_excluded_from_repr() -> None:
    config = NeonStorageConfig(
        endpoint_url="https://br-example.storage.c-2.us-east-2.aws.neon.tech",
        region="us-east-2",
        access_key_id="nak_live_should_not_leak",
        secret_access_key="nsk_live_should_not_leak",
        bucket="nagar-assets",
    )
    rendered = repr(config)
    assert "nak_live_should_not_leak" not in rendered
    assert "nsk_live_should_not_leak" not in rendered
    assert config.bucket in rendered, "non-secret configuration should stay visible for debugging"


def test_both_backends_satisfy_the_contract_protocol(tmp_path) -> None:
    config = NeonStorageConfig(
        endpoint_url="https://br-example.storage.c-2.us-east-2.aws.neon.tech",
        region="us-east-2",
        access_key_id="nak_live",
        secret_access_key="nsk_live",
        bucket="nagar-assets",
    )
    assert isinstance(LocalObjectStore(tmp_path / "objects"), ObjectStore)
    assert isinstance(NeonObjectStorage(config, client=object()), ObjectStore)


@pytest.mark.parametrize("path", NEW_MODULES, ids=lambda p: p.name)
def test_new_modules_declare_a_docstring(path: Path) -> None:
    assert ast.get_docstring(_tree(path)), f"{path.name} has no module docstring"
