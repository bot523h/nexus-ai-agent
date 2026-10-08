"""Execution-Fabric boundary laws (task-255) — enforced, not requested.

The Execution Fabric is a seam, so its boundaries are laws:

1. the provider-neutral contract imports **no** provider SDK and **no** Nexus
   authority module (queue / bus / verifier / passport / provenance);
2. importing the fabric or the Hatchet package never imports ``hatchet_sdk``
   (the optional extra stays optional — only ``_sdk.py`` may touch it);
3. no product code outside the fabric imports the fabric or a provider adapter
   (Hatchet is opt-in, never wired into an entrypoint by accident);
4. embedded Hatchet is refused unless an explicit env flag is set.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "nexus_ai_agent"
FABRIC = SRC / "integrations" / "execution"
HATCHET = SRC / "integrations" / "hatchet"

AUTHORITY_MODULES = {
    "nexus_ai_agent.adapters.in_process_job_queue",
    "nexus_ai_agent.application.ports.job_queue",
    "nexus_ai_agent.creative.studio.bus",
    "nexus_ai_agent.jobs.verification",
    "nexus_ai_agent.jobs.creative_passport",
    "nexus_ai_agent.jobs.lifecycle",
    "nexus_ai_agent.provenance",
    "nexus_ai_agent.creative.render_jobs",
}
PROVIDER_SDKS = {"hatchet_sdk"}


def _module_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_contract_imports_no_provider_and_no_authority() -> None:
    violations: list[str] = []
    for path in FABRIC.rglob("*.py"):
        for name in _module_imports(path):
            if name.split(".")[0] in PROVIDER_SDKS:
                violations.append(f"{path.name}: provider import {name}")
            if any(name == a or name.startswith(a + ".") for a in AUTHORITY_MODULES):
                violations.append(f"{path.name}: authority import {name}")
    assert not violations, "fabric contract boundary violations:\n" + "\n".join(violations)


def test_hatchet_package_does_not_import_sdk_eagerly() -> None:
    for path in HATCHET.rglob("*.py"):
        if path.name == "_sdk.py":
            continue  # the single, deliberate lazy-import site
        for name in _module_imports(path):
            assert name.split(".")[0] not in PROVIDER_SDKS, (
                f"{path.name} imports the optional provider SDK eagerly: {name}"
            )


def test_importing_the_fabric_does_not_import_the_sdk() -> None:
    assert "hatchet_sdk" not in sys.modules
    importlib.import_module("nexus_ai_agent.integrations.execution")
    importlib.import_module("nexus_ai_agent.integrations.hatchet")
    assert "hatchet_sdk" not in sys.modules, "importing the fabric pulled in hatchet_sdk"


def test_no_product_code_imports_the_fabric_or_a_provider() -> None:
    """Only the fabric packages themselves may reference the fabric/provider."""
    allowed_roots = (FABRIC, HATCHET)
    violations: list[str] = []
    for path in SRC.rglob("*.py"):
        if any(path.is_relative_to(root) for root in allowed_roots):
            continue
        for name in _module_imports(path):
            if name.startswith("nexus_ai_agent.integrations.execution") or name.startswith(
                "nexus_ai_agent.integrations.hatchet"
            ):
                violations.append(f"{path.relative_to(SRC)} imports {name}")
            if name.split(".")[0] in PROVIDER_SDKS:
                violations.append(f"{path.relative_to(SRC)} imports {name}")
    assert not violations, "product code reached the fabric/provider:\n" + "\n".join(violations)


def test_embedded_mode_is_refused_without_explicit_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_ai_agent.integrations.hatchet import _sdk

    monkeypatch.delenv(_sdk.EMBEDDED_ENV_FLAG, raising=False)
    assert _sdk.embedded_allowed() is False
    with pytest.raises(_sdk.HatchetEmbeddedForbidden):
        _sdk.new_client(embedded=True)
    monkeypatch.setenv(_sdk.EMBEDDED_ENV_FLAG, "1")
    assert _sdk.embedded_allowed() is True
