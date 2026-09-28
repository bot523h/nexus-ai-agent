"""Distribution contract: no editable checkout can supply missing runtime data.

Builds both wheel and sdist->wheel offline using the current test toolchain,
installs with --no-deps to an isolated target, and invokes Python -I from a
foreign cwd. Dependencies come from the test environment; project code/data
MUST come from the installed target. The CI leg uses a clean venv, not -e.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.fixture(scope="module", params=["wheel", "sdist-wheel"])
def installed(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    work = tmp_path_factory.mktemp(request.param)
    source = work / "source"
    source.mkdir()
    for filename in (
        "pyproject.toml",
        "setup.py",
        "MANIFEST.in",
        "README.md",
        "LICENSE",
        "alembic.ini",
        "VERSION",
    ):
        path = ROOT / filename
        if path.exists():
            shutil.copy2(path, source / filename)
    for directory in ("src", "migrations"):
        shutil.copytree(
            ROOT / directory,
            source / directory,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
        )
    if request.param == "sdist-wheel":
        run("-m", "build", "--sdist", "--no-isolation", cwd=source)
        archive = next((source / "dist").glob("*.tar.gz"))
        extracted = work / "extracted"
        extracted.mkdir()
        # Locally built archive, not user input; keep Python 3.10 compatible.
        with tarfile.open(archive) as tar:
            tar.extractall(extracted)  # noqa: S202
        source = next(extracted.iterdir())
    run("-m", "build", "--wheel", "--no-isolation", cwd=source)
    wheel = next((source / "dist").glob("*.whl"))
    target = work / "installed"
    run(
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--no-index",
        "--target",
        str(target),
        str(wheel),
        cwd=work,
    )
    cwd = work / "outside-checkout"
    cwd.mkdir()
    # The build source cannot accidentally satisfy checkout-relative discovery.
    shutil.rmtree(source)
    return target, cwd


def isolated(installed: tuple, script: str) -> str:
    target, cwd = installed
    env = {k: v for k, v in os.environ.items() if not k.startswith("NEXUS_")}
    env.pop("DATABASE_URL", None)
    env.pop("PYTHONPATH", None)
    env["NEXUS_DB_PATH"] = str(cwd / "runtime.sqlite")
    env["TELEGRAM_BOT_TOKEN"] = "test-token"
    prefix = (
        f"import sys, pathlib; root = pathlib.Path({str(target)!r})\n"
        "sys.path.insert(0, str(root))\n"
    )
    suffix = """
import nexus_ai_agent
assert pathlib.Path(nexus_ai_agent.__file__).is_relative_to(root)
for name, module in list(sys.modules.items()):
    if name.startswith('nexus_ai_agent.') and getattr(module, '__file__', None):
        assert pathlib.Path(module.__file__).is_relative_to(root), (name, module.__file__)
"""
    return run("-I", "-c", prefix + script + suffix, cwd=cwd, env=env)


def test_all_source_json_resources_ship(installed: tuple) -> None:
    target, _ = installed
    resources = list((ROOT / "src" / "nexus_ai_agent").rglob("*.json"))
    assert resources
    for source in resources:
        relative = source.relative_to(ROOT / "src")
        shipped = target / relative
        assert shipped.is_file(), f"wheel omits {relative}"
        assert shipped.read_bytes() == source.read_bytes(), relative


def test_packaged_migrations_match_canonical_source(installed: tuple) -> None:
    target, _ = installed
    packaged = target / "nexus_ai_agent" / "storage" / "_alembic"
    for source in (ROOT / "migrations").rglob("*"):
        if source.suffix not in {".py", ".mako"}:
            continue
        shipped = packaged / source.relative_to(ROOT / "migrations")
        assert shipped.is_file(), f"wheel omits {source.name}"
        assert shipped.read_bytes() == source.read_bytes()
    assert (packaged / "alembic.ini").read_bytes() == (ROOT / "alembic.ini").read_bytes()


def test_installed_resources_can_be_used(installed: tuple) -> None:
    isolated(
        installed,
        """
from nexus_ai_agent.i18n import I18n, SUPPORTED_LANGUAGES
from nexus_ai_agent.creative.packs.slideshow.templates import load_tone_templates
from nexus_ai_agent.creative.packs.trust import TrustRoot
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry, composition_issues
import json
translations = I18n()
assert set(translations.get_available_languages()) == set(SUPPORTED_LANGUAGES)
assert translations.t('onboarding.welcome', lang='fa') != 'onboarding.welcome'
assert load_tone_templates().ids()
TrustRoot.load()
registry = build_runtime_registry()
assert registry.list_operations()
assert composition_issues() == ()
packs = root / 'nexus_ai_agent' / 'creative' / 'packs'
assert len(list(packs.rglob('pack.manifest.json'))) == 6
for path in packs.rglob('pack.manifest.json'):
    assert json.loads(path.read_text())['package_id']
for path in (root / 'nexus_ai_agent' / 'storage').rglob('*.json'):
    assert json.loads(path.read_text())
""",
    )


def test_installed_migrations_and_real_session_roundtrip(installed: tuple) -> None:
    isolated(
        installed,
        """
import asyncio
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.storage import db
from nexus_ai_agent.storage.migrations import build_alembic_config, run_migrations
from nexus_ai_agent.bot.handlers import _upsert_user, _upsert_chat
from types import SimpleNamespace
config = build_alembic_config()
assert pathlib.Path(config.get_main_option('script_location')).is_relative_to(root)
assert ScriptDirectory.from_config(config).get_heads() == ['7c2f9d41e8a3']
run_migrations()
run_migrations()
command.check(config)
async def roundtrip():
    try:
        user = SimpleNamespace(id=321, username='wheel-user')
        async with db.get_session() as session:
            assert pathlib.Path(session.bind.url.database) == pathlib.Path(get_settings().db_path)
        first = await _upsert_user(db.get_session, user)
        assert (await _upsert_user(db.get_session, user)).id == first.id
        assert (await _upsert_chat(db.get_session, 321, 'tg:321')).chat_id == 321
    finally:
        if db._engine is not None:
            await db._engine.dispose()
asyncio.run(roundtrip())
command.downgrade(config, 'base')
engine = create_engine('sqlite:///' + get_settings().db_path)
assert set(inspect(engine).get_table_names()) <= {'alembic_version'}
engine.dispose()
run_migrations()
command.check(config)
""",
    )


def test_config_resolution_is_not_controlled_by_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.config import settings as settings_module
    from nexus_ai_agent.storage.migrations import build_alembic_config

    monkeypatch.chdir(tmp_path)
    (tmp_path / "alembic.ini").write_text("not an ini file")
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "100% ready.sqlite"))
    settings_module.get_settings.cache_clear()
    try:
        config = build_alembic_config()
        scripts = Path(config.get_main_option("script_location"))
        assert scripts != tmp_path / "migrations"
        assert (scripts / "env.py").is_file()
        assert "100% ready.sqlite" in config.get_main_option("sqlalchemy.url")
    finally:
        settings_module.get_settings.cache_clear()


def test_broken_installed_migrations_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.storage import migrations

    fake_module = tmp_path / "src" / "nexus_ai_agent" / "storage" / "migrations.py"
    monkeypatch.setattr(migrations, "__file__", str(fake_module))
    with pytest.raises(RuntimeError, match="Alembic resources are missing"):
        migrations.build_alembic_config()
