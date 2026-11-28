"""The CI maintenance lane must be *structurally* unable to reach a cloud tier.

``test_maintenance_ci_preflight.py`` proves the lane is local on a runner with no
secrets. That is necessary but not sufficient: the audit's objection was that the
guarantee rested on the caller remembering to pass a local provider. This module
pins the stronger invariant.

* ``build_blob_provider`` **raises** while ``forbid_remote_provider_factory()`` is
  active, so a future refactor that drops the injection fails closed instead of
  quietly writing a synthetic SQLite artifact to a real bucket.
* ``run_housekeeping`` accepts an explicit provider, so the prune path can be
  exercised without consulting the environment.
* across a six-cell environment matrix (nothing / complete B2 / complete R2 /
  both / partial B2 / partial B2 + complete R2) the self-test calls
  ``B2Provider``, ``R2Provider`` and ``build_blob_provider`` **zero times**.
* the bare ``python -m nexus_ai_agent.maintenance.ci_selftest`` works with no
  arguments, which is what the ``lint`` CI job runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.maintenance import backup, ci_selftest
from nexus_ai_agent.storage.providers.local_cache import LocalCacheProvider

#: The two complete tiers, as a real runner would hold them in its environment.
COMPLETE_B2 = {
    "NEXUS_B2_KEY_ID": "b2-key-id",
    "NEXUS_B2_APPLICATION_KEY": "b2-application-key",
    "NEXUS_B2_BUCKET": "b2-bucket",
    "NEXUS_B2_ENDPOINT": "https://s3.eu-central-1.backblazeb2.com",
    "NEXUS_B2_REGION": "eu-central-1",
}
COMPLETE_R2 = {
    "R2_ACCOUNT_ID": "r2-account",
    "R2_ACCESS_KEY_ID": "r2-key",
    "R2_SECRET_ACCESS_KEY": "r2-secret",
    "R2_BUCKET": "r2-bucket",
}
REMOTE_ENV = (*COMPLETE_B2, *COMPLETE_R2, "NEXUS_DATABASE_URL", "NEXUS_MAINTENANCE_LOCAL_TIER")

CELLS = {
    "nothing": {},
    "b2-complete": COMPLETE_B2,
    "r2-complete": COMPLETE_R2,
    "both-complete": {**COMPLETE_B2, **COMPLETE_R2},
    "b2-partial": {"NEXUS_B2_KEY_ID": "half", "NEXUS_B2_BUCKET": "half-bucket"},
    "b2-partial-r2-complete": {
        "NEXUS_B2_KEY_ID": "half",
        "NEXUS_B2_BUCKET": "half-bucket",
        **COMPLETE_R2,
    },
}


def _clean_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name in (*REMOTE_ENV, backup.SELFTEST_FLAG_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _settings(**env: str) -> Settings:
    """Settings built from the ambient environment (the real factory input)."""
    return Settings.model_validate({})


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------
def test_the_factory_refuses_while_the_guard_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_env(monkeypatch, **{**COMPLETE_B2, **COMPLETE_R2})
    with backup.forbid_remote_provider_factory():
        assert backup.selftest_active() is True
        with pytest.raises(RuntimeError) as excinfo:
            backup.build_blob_provider(_settings())
    assert str(excinfo.value) == backup.SELFTEST_FACTORY_FORBIDDEN
    assert "forbidden during CI local SQLite self-test" in str(excinfo.value)


def test_the_factory_refuses_even_when_the_tier_is_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal is unconditional; it is not a remote-tier special case.

    This case also keeps the guard's kill deterministic: with the tier local the
    factory would succeed without importing an optional extra, so a mutant that
    drops the guard fails an assertion instead of raising an import error whose
    classification depends on what the job happens to have installed.
    """
    _clean_env(monkeypatch)
    with backup.forbid_remote_provider_factory():
        with pytest.raises(RuntimeError) as excinfo:
            backup.build_blob_provider(_settings())
    assert str(excinfo.value) == backup.SELFTEST_FACTORY_FORBIDDEN


def test_the_guard_lifts_on_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch, **COMPLETE_R2)
    with backup.forbid_remote_provider_factory():
        pass
    assert backup.selftest_active() is False
    import os

    assert backup.SELFTEST_FLAG_ENV not in os.environ
    provider = backup.build_blob_provider(_settings())
    assert provider.name == "r2", "the guard must not change ordinary selection after it exits"


def test_the_guard_is_inherited_through_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child process (the self-test spawns some) must inherit the refusal."""
    _clean_env(monkeypatch, **COMPLETE_B2)
    monkeypatch.setenv(backup.SELFTEST_FLAG_ENV, "1")
    assert backup.selftest_active() is True
    with pytest.raises(RuntimeError):
        backup.build_blob_provider(_settings())


def test_the_guard_survives_an_exception_inside_the_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_env(monkeypatch)
    with pytest.raises(ValueError):
        with backup.forbid_remote_provider_factory():
            raise ValueError("boom")
    assert backup.selftest_active() is False


def test_a_complete_b2_tier_is_selected_outside_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must be the *only* difference: the factory still works normally."""
    _clean_env(monkeypatch, **COMPLETE_B2)
    assert backup.build_blob_provider(_settings()).name == "b2"


def test_the_legacy_alias_delegates_to_the_guarded_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_build_provider`` must not be a second, unguarded binding."""
    _clean_env(monkeypatch, **COMPLETE_R2)
    with backup.forbid_remote_provider_factory():
        with pytest.raises(RuntimeError):
            backup._build_provider(_settings())


# ---------------------------------------------------------------------------
# housekeeping takes a provider instead of consulting the environment
# ---------------------------------------------------------------------------
def test_housekeeping_without_a_provider_fails_closed_inside_the_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nexus_ai_agent.maintenance.housekeeping import run_housekeeping

    _clean_env(monkeypatch, **COMPLETE_R2)
    settings = _settings().model_copy(update={"creative_temp_dir": str(tmp_path / "temp")})
    with backup.forbid_remote_provider_factory():
        with pytest.raises(RuntimeError):
            run_housekeeping(settings=settings)


def test_housekeeping_prunes_through_an_injected_local_tier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nexus_ai_agent.maintenance.housekeeping import run_housekeeping

    _clean_env(monkeypatch, **COMPLETE_R2)
    provider = LocalCacheProvider(tmp_path / "tier")
    stale = provider.path_for_key("backups/db/20000101-000000/nexus.sqlite3")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"old")
    settings = _settings().model_copy(update={"creative_temp_dir": str(tmp_path / "temp")})

    with backup.forbid_remote_provider_factory():
        result = run_housekeeping(settings=settings, backup_retention_days=0, provider=provider)

    assert result["backups_deleted"] == ["backups/db/20000101-000000/nexus.sqlite3"]
    assert not stale.exists()


# ---------------------------------------------------------------------------
# The six-cell environment matrix
# ---------------------------------------------------------------------------
def _forbid_every_remote_constructor(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the factory and both cloud constructors with recording refusals."""
    calls: list[str] = []

    class _Refusal:
        name = "must_not_be_reached"

        def __init__(self, *_: Any, **__: Any) -> None:
            calls.append("constructed")

        @classmethod
        def from_settings(cls, *_: Any, **__: Any) -> Any:
            calls.append("from_settings")
            raise AssertionError("a cloud provider was constructed during the CI self-test")

    def _factory(*_: Any, **__: Any) -> Any:
        calls.append("build_blob_provider")
        raise AssertionError("build_blob_provider was consulted during the CI self-test")

    monkeypatch.setattr(backup, "B2Provider", _Refusal)
    monkeypatch.setattr(backup, "R2Provider", _Refusal)
    monkeypatch.setattr(backup, "build_blob_provider", _factory)
    monkeypatch.setattr(backup, "_build_provider", _factory)
    return calls


@pytest.mark.parametrize("cell", sorted(CELLS))
def test_self_test_never_constructs_a_cloud_tier(
    cell: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clean_env(monkeypatch, **CELLS[cell])
    calls = _forbid_every_remote_constructor(monkeypatch)

    evidence = ci_selftest.run(
        only="backup",
        database=tmp_path / "db" / "nexus.sqlite3",
        tier=tmp_path / "tier",
        workspace_root=tmp_path,
    )

    assert calls == [], f"{cell}: the self-test reached a remote provider ({calls})"
    assert evidence["mode"] == "ci_local_sqlite_selftest"
    assert evidence["production_postgres_dr_verified"] is False
    assert evidence["storage_tier"] == "local_cache"
    assert evidence["backup"]["tier"] == "local_cache"
    assert evidence["backup"]["verified"] is True
    assert sorted(evidence["remote_tier_env_suppressed"]) == sorted(CELLS[cell])


@pytest.mark.parametrize("cell", sorted(CELLS))
def test_local_only_settings_are_local_in_every_cell(
    cell: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clean_env(monkeypatch, **CELLS[cell])
    settings = ci_selftest.local_only_settings(tmp_path / "db.sqlite3", tmp_path / "tier")
    for field in ci_selftest.REMOTE_TIER_FIELDS:
        assert getattr(settings, field) is None, f"{cell}: {field} leaked from the environment"
    assert settings.database_url is None
    assert settings.maintenance_local_tier == str(tmp_path / "tier")


def test_the_self_test_does_not_leave_the_guard_or_the_environment_dirty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    _clean_env(monkeypatch, **COMPLETE_B2)
    ci_selftest.run(
        only="backup",
        database=tmp_path / "db" / "nexus.sqlite3",
        tier=tmp_path / "tier",
        workspace_root=tmp_path,
    )
    assert backup.selftest_active() is False
    assert backup.SELFTEST_FLAG_ENV not in os.environ
    assert os.environ["NEXUS_B2_BUCKET"] == "b2-bucket", "ambient env is restored"


def test_a_configured_database_url_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clean_env(monkeypatch, NEXUS_DATABASE_URL="postgresql://pg:pg@localhost:5432/nexus")
    with pytest.raises(ci_selftest.SelfTestError):
        ci_selftest.run(
            only="backup",
            database=tmp_path / "db.sqlite3",
            tier=tmp_path / "tier",
            workspace_root=tmp_path,
        )


# ---------------------------------------------------------------------------
# The bare invocation the lint job runs
# ---------------------------------------------------------------------------
def test_the_bare_command_runs_every_leg_with_no_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert ci_selftest.main([]) == 0

    out = capsys.readouterr().out
    assert '"mode": "ci_local_sqlite_selftest"' in out
    assert '"production_postgres_dr_verified": false' in out
    assert '"storage_tier": "local_cache"' in out
    assert '"housekeeping"' in out
    assert "✅ maintenance self-test (all)" in out


def test_the_bare_command_is_rerunnable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A second run must not inherit the first run's database or tier."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert ci_selftest.main(["--only", "backup"]) == 0
    capsys.readouterr()
    assert ci_selftest.main([]) == 0
    evidence = json.loads(capsys.readouterr().out.split("\n✅")[0])
    assert evidence["ok"] is True


def test_the_json_out_option_writes_the_evidence_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "evidence.json"
    assert ci_selftest.main(["--only", "backup", "--json-out", str(target)]) == 0
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["mode"] == "ci_local_sqlite_selftest"
    assert document["backup"]["verified"] is True
    assert "evidence:" in capsys.readouterr().out


def test_the_default_workdir_lives_under_runner_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    assert ci_selftest.default_workdir() == tmp_path / "nexus-maintenance-selftest"
    monkeypatch.delenv("RUNNER_TEMP")
    assert ci_selftest.default_workdir().name == "nexus-maintenance-selftest"


def test_the_run_holds_the_guard_for_its_whole_duration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The guard must be armed *while a leg runs*, not merely around the process.

    ``run`` opens the guard once and every leg executes inside it; a leg that
    escaped the block would be free to select a cloud tier again.
    """
    _clean_env(monkeypatch)
    observed: list[bool] = []
    real_prepare = ci_selftest.prepare_database

    def _spy(path: Path):
        observed.append(backup.selftest_active())
        return real_prepare(path)

    monkeypatch.setattr(ci_selftest, "prepare_database", _spy)
    ci_selftest.run(
        only="backup",
        database=tmp_path / "db.sqlite3",
        tier=tmp_path / "tier",
        workspace_root=tmp_path,
    )
    assert observed == [True], observed
