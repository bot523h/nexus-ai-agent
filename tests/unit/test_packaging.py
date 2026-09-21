"""Packaging contract tests (task-107).

The whole point of splitting the dependencies is that a core install
(``pip install .``) is small **and still boots**. Splitting alone does not
guarantee that: one module-scope ``import chromadb`` in the startup path is
enough to break every core-only deployment, and nothing else in the suite would
notice because CI installs the extras.

So these tests pin the three properties that make the split real:

1. no heavy/native package is declared as a core dependency;
2. no core entry point can reach an extra-provided module through a module-scope
   import (checked statically, over the whole eager import graph);
3. the entry points actually import in a fresh interpreter where every extra
   module is blocked — the same failure mode a core-only install would hit.

Plus the guard contract: a missing extra must fail closed with a typed error
that names the install command.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10 has no stdlib TOML parser
    tomllib = pytest.importorskip("tomli")

from nexus_ai_agent.optional_deps import MODULE_TO_EXTRA, OptionalDependencyMissing

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

# Never allowed in [project.dependencies]: these pull torch, a C++ toolchain or a
# network driver that only some deployments need.
HEAVY_PACKAGES = {
    "sentence-transformers",
    "torch",
    "llama-cpp-python",
    "chromadb",
    "flashrank",
    "sqlite-vec",
    "boto3",
    "psycopg",
    "asyncpg",
    "langgraph-checkpoint-postgres",
    "opentimelineio",
    "gtts",
    "pypdf",
}

# Top-level modules each extra provides. Dotted where only a submodule matters
# (langgraph itself is core; only its Postgres saver is optional).
EXTRA_PROVIDES: dict[str, tuple[str, ...]] = {
    "rag": ("chromadb", "flashrank", "sqlite_vec", "sentence_transformers"),
    "local-llm": ("llama_cpp",),
    "speech": ("gtts",),
    "r2": ("boto3", "botocore"),
    "pdf": ("pypdf",),
    "postgres": ("psycopg", "asyncpg", "langgraph.checkpoint.postgres"),
    "otio": ("opentimelineio",),
    "media": ("imageio_ffmpeg",),
}

# Modules a core-only install must be able to import.
CORE_ENTRY_POINTS = (
    "nexus_ai_agent.bot.app",
    "nexus_ai_agent.cli",
    "nexus_ai_agent.worker",
    "nexus_ai_agent.api.app",
)

SRC_ROOT = REPO_ROOT / "src"


def _core_dependency_names() -> set[str]:
    return {
        dep.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip()
        for dep in PYPROJECT["project"]["dependencies"]
    }


def _eager_imports(path: Path) -> set[str]:
    """Module-scope imports of *path*: top level, plus bodies that run at import.

    Function bodies are lazy (they run when called), ``if TYPE_CHECKING`` blocks
    never run, everything else at module scope executes on import.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()

    def add(node: ast.stmt) -> None:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)

    def visit(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                add(node)
            elif isinstance(node, ast.If):
                if "TYPE_CHECKING" in ast.unparse(node.test):
                    continue
                visit(node.body)
                visit(node.orelse)
            elif isinstance(node, ast.Try):
                visit(node.body)
                for handler in node.handlers:
                    visit(handler.body)
                visit(node.orelse)
                visit(node.finalbody)
            elif isinstance(node, (ast.With, ast.AsyncWith, ast.ClassDef)):
                visit(node.body)

    visit(tree.body)
    return names


def _file_for(module: str) -> Path | None:
    relative = module.replace(".", "/")
    for candidate in (SRC_ROOT / f"{relative}.py", SRC_ROOT / relative / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def _eager_closure(entry_points: tuple[str, ...]) -> tuple[set[str], set[str]]:
    """Follow module-scope imports from *entry_points*; return (internal, external)."""
    internal: set[str] = set()
    external: set[str] = set()
    stack = list(entry_points)
    while stack:
        module = stack.pop()
        if module in internal:
            continue
        internal.add(module)
        path = _file_for(module)
        if path is None:
            external.add(module.split(".")[0])
            continue
        for dependency in _eager_imports(path):
            if dependency.startswith("nexus_ai_agent."):
                stack.append(dependency)
            else:
                external.add(dependency)
    return internal, external


# ── declaration contract ───────────────────────────────────────────────────


def test_core_dependencies_carry_no_heavy_packages() -> None:
    assert not (HEAVY_PACKAGES & _core_dependency_names())


def test_every_capability_extra_is_declared_and_non_empty() -> None:
    extras = PYPROJECT["project"]["optional-dependencies"]
    for extra in EXTRA_PROVIDES:
        assert extras.get(extra), f"extra [{extra}] is missing from pyproject.toml"


def test_extra_provided_modules_are_known_to_the_guard() -> None:
    """``MODULE_TO_EXTRA`` must map every optional module to its extra."""
    for extra, modules in EXTRA_PROVIDES.items():
        for module in modules:
            top_level = module.split(".")[0]
            mapped = MODULE_TO_EXTRA.get(module) or MODULE_TO_EXTRA.get(top_level)
            assert mapped == extra, (
                f"{module} is provided by [{extra}] but optional_deps maps it to "
                f"{mapped!r}: the install hint would be wrong"
            )


def test_dev_extra_mirrors_the_pep735_dev_group() -> None:
    """Both spellings must stay in sync: `.[dev]` (legacy) and `--group dev`."""
    extra = PYPROJECT["project"]["optional-dependencies"]["dev"]
    group = PYPROJECT["dependency-groups"]["dev"]
    assert sorted(extra) == sorted(group)


def test_lint_group_is_a_subset_of_the_dev_group() -> None:
    dev = set(PYPROJECT["dependency-groups"]["dev"])
    assert set(PYPROJECT["dependency-groups"]["lint"]) <= dev


# ── import-graph contract ──────────────────────────────────────────────────


@pytest.mark.parametrize("entry_point", CORE_ENTRY_POINTS)
def test_entry_point_never_imports_an_extra_at_module_scope(entry_point: str) -> None:
    _, external = _eager_closure((entry_point,))
    offenders = {
        module
        for module in external
        for modules in EXTRA_PROVIDES.values()
        if module in modules or any(m.startswith(f"{module}.") for m in modules)
    }
    assert not offenders, (
        f"{entry_point} imports {sorted(offenders)} at module scope, so a core-only "
        "install would crash at startup; move the import inside the function that "
        "needs it and guard it with nexus_ai_agent.optional_deps"
    )


def test_core_import_graph_is_actually_reachable() -> None:
    """Guard against the graph test silently degrading to a no-op.

    The CLI and the bot build their imports inside command bodies (Typer style),
    so the static closure is shallow by design; the subprocess tests below cover
    the rest of the startup path.
    """
    internal, external = _eager_closure(CORE_ENTRY_POINTS)
    assert "nexus_ai_agent.storage.db" in internal
    assert "nexus_ai_agent.api.dashboard" in internal
    assert len(internal) > 15
    assert {"fastapi", "sqlmodel", "telegram.ext"} <= external


# ── runtime contract: core boots with every extra blocked ──────────────────

# The startup path: everything a polling bot, the API and the worker import.
CORE_STARTUP_MODULES = (
    *CORE_ENTRY_POINTS,
    "nexus_ai_agent.bot.handlers",
    "nexus_ai_agent.bot.middleware",
    "nexus_ai_agent.storage.db",
    "nexus_ai_agent.storage.langgraph_checkpoint",
)

_CORE_BOOT_SCRIPT = """
import sys

BLOCKED = {blocked!r}


class _Blocker:
    def find_spec(self, fullname, path=None, target=None):
        for name in BLOCKED:
            if fullname == name or fullname.startswith(name + "."):
                raise ModuleNotFoundError(f"No module named {{fullname!r}}", name=fullname)
        return None


sys.meta_path.insert(0, _Blocker())

import importlib

for name in {modules!r}:
    importlib.import_module(name)

from nexus_ai_agent.storage.langgraph_checkpoint import get_checkpointer

saver = get_checkpointer(":memory:")
print("imported", len({modules!r}), "startup modules")
print("checkpointer", type(saver).__name__)
"""


def test_startup_path_boots_with_every_extra_unavailable(tmp_path: Path) -> None:
    """The real core-only smoke test: fresh interpreter, all extras blocked.

    This is the failure mode a ``pip install .`` deployment hits and that the
    rest of the suite cannot see, because CI installs the extras.
    """
    # Exact (possibly dotted) module names: only the optional submodule is
    # blocked, so `langgraph.checkpoint.postgres` never blocks `langgraph`.
    blocked = sorted({module for modules in EXTRA_PROVIDES.values() for module in modules})
    script = _CORE_BOOT_SCRIPT.format(blocked=blocked, modules=CORE_STARTUP_MODULES)
    env = {
        **os.environ,
        "TELEGRAM_BOT_TOKEN": "123456:core-only-smoke-test",
        "NEXUS_DB_PATH": str(tmp_path / "app.sqlite"),
        "NEXUS_CHECKPOINT_PATH": str(tmp_path / "checkpoint.sqlite"),
        "NEXUS_VECTOR_PATH": str(tmp_path / "vectors.sqlite"),
        "NEXUS_MODEL_PATH": str(tmp_path / "model"),
        "NEXUS_OWNER_TELEGRAM_ID": "1",
    }
    env.pop("NEXUS_DATABASE_URL", None)
    env.pop("DATABASE_URL", None)

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=240,
        check=False,
    )
    assert result.returncode == 0, f"core-only startup failed:\n{result.stderr}"
    assert f"imported {len(CORE_STARTUP_MODULES)} startup modules" in result.stdout
    # SQLite is the default backend and must not need the [postgres] extra.
    assert "checkpointer" in result.stdout
    assert "Postgres" not in result.stdout


# ── guard contract ─────────────────────────────────────────────────────────


def test_missing_extra_error_names_the_install_command() -> None:
    error = OptionalDependencyMissing("chromadb")
    assert error.extra == "rag"
    assert "pip install 'nexus-ai-agent[rag]'" in str(error)


def test_heavy_extras_warn_about_model_downloads() -> None:
    assert "model weights" in str(OptionalDependencyMissing("sentence_transformers"))
    assert "model weights" not in str(OptionalDependencyMissing("pypdf"))


def test_unknown_module_falls_back_to_the_module_name() -> None:
    error = OptionalDependencyMissing("some_random_thing")
    assert error.extra == "some_random_thing"
    assert "pip install 'some_random_thing'" in str(error)


def test_is_installed_does_not_execute_the_module() -> None:
    from nexus_ai_agent.optional_deps import is_installed

    assert is_installed("json") is True
    assert is_installed("definitely_not_a_real_module_xyz") is False
    assert "definitely_not_a_real_module_xyz" not in sys.modules


def test_require_returns_the_module_when_available() -> None:
    from nexus_ai_agent.optional_deps import require

    assert require("json") is sys.modules["json"]


def test_require_raises_the_typed_error_when_missing() -> None:
    from nexus_ai_agent.optional_deps import require

    with pytest.raises(OptionalDependencyMissing, match="pip install"):
        require("definitely_not_a_real_module_xyz")


def test_from_import_error_maps_the_module_to_its_extra() -> None:
    from nexus_ai_agent.optional_deps import from_import_error

    error = from_import_error(ModuleNotFoundError("No module named 'flashrank'", name="flashrank"))
    assert error.module == "flashrank"
    assert error.extra == "rag"


async def test_pdf_job_reports_the_rag_extra_when_it_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the durable job failure carries the install command."""
    from nexus_ai_agent import worker

    async def fake_extract(_: str) -> str:
        return "extracted text"

    monkeypatch.setattr(worker, "extract_pdf_text", fake_extract)
    monkeypatch.delitem(sys.modules, "nexus_ai_agent.features.rag", raising=False)
    monkeypatch.setitem(sys.modules, "chromadb", None)  # makes `import chromadb` fail

    with pytest.raises(RuntimeError, match=r"nexus-ai-agent\[rag\]"):
        await worker.process_pdf_task(42, "/tmp/does-not-matter.pdf", "file-42")


async def test_speech_reports_the_extra_when_gtts_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nexus_ai_agent import optional_deps
    from nexus_ai_agent.features.speech import SpeechEngine

    real_is_installed = optional_deps.is_installed
    monkeypatch.setattr(
        optional_deps,
        "is_installed",
        lambda name: False if name == "gtts" else real_is_installed(name),
    )

    result = await SpeechEngine(output_dir=str(tmp_path)).text_to_speech("سلام")

    assert result["success"] is False
    assert "nexus-ai-agent[speech]" in str(result["error"])


def test_r2_provider_reports_the_extra_when_boto3_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus_ai_agent import optional_deps
    from nexus_ai_agent.storage.providers.r2 import R2Provider

    real_is_installed = optional_deps.is_installed
    monkeypatch.setattr(
        optional_deps,
        "is_installed",
        lambda name: False if name == "boto3" else real_is_installed(name),
    )
    provider = R2Provider(
        account_id="acct", access_key_id="key", secret_access_key="secret", bucket="bkt"
    )

    with pytest.raises(OptionalDependencyMissing, match=r"nexus-ai-agent\[r2\]"):
        _ = provider.client


def test_readme_documents_every_capability_extra() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for extra in EXTRA_PROVIDES:
        assert f"[{extra}]" in readme, f"extra [{extra}] is not documented in README.md"
