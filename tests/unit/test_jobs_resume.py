"""D1 — resuming interrupted jobs is an explicit operator action (``nexus jobs resume``).

No auto-resume at boot: with two bot instances (deploy overlap, a second
replica) each would reclassify the other's live ``running`` rows as
interrupted and execute them again.  The operator command is dry-run by
default, refuses to run while another process owns the job store, and reports
outcomes so the exit code is meaningful.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from nexus_ai_agent import jobs
from nexus_ai_agent.adapters.in_process_job_queue import (
    InProcessJobQueue,
    JobStatus,
    JobStoreBusyError,
)
from nexus_ai_agent.config.settings import Settings, get_settings

runner = CliRunner()


async def _leave_interrupted_rows(settings: Settings) -> tuple[str, str]:
    """Simulate a bot that died mid-job: one ``running`` row, one ``pending`` row."""
    release = asyncio.Event()

    async def hang(payload: dict[str, object]) -> None:
        await release.wait()

    queue = InProcessJobQueue(jobs.job_store_path(settings))  # same store the bot uses
    queue.register(jobs.STORY_JOB, hang)  # real job types, hanging bodies
    queue.register(jobs.PDF_JOB, hang)
    running = await queue.enqueue(
        job_type=jobs.STORY_JOB, idempotency_key="s", payload={"chat_id": 1}
    )
    deadline = asyncio.get_running_loop().time() + 5
    while (
        record := await queue.get_job(running)
    ) is None or record.status is not JobStatus.RUNNING:
        assert asyncio.get_running_loop().time() < deadline, "job never reached running"
        await asyncio.sleep(0.01)
    # A row that never got dispatched (e.g. the loop died between insert and task start).
    pending = await queue.enqueue(
        job_type=jobs.PDF_JOB, idempotency_key="p", payload={"chat_id": 2}
    )
    async with queue._connect() as db:  # test-only: force the second row back to pending
        await db.execute(
            "UPDATE jobs SET status = 'pending' WHERE id = ?",
            (pending,),
        )
        await db.commit()
    await queue.close(timeout=0)
    return running, pending


@pytest.fixture()
def fast_jobs(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, object]]]:
    """Replace the heavy job bodies with instant ones for orchestration tests."""
    calls: dict[str, list[dict[str, object]]] = {"story": [], "pdf": []}

    async def story(payload: dict[str, object]) -> dict[str, object]:
        calls["story"].append(payload)
        return {"output_path": "x.png"}

    async def pdf(payload: dict[str, object]) -> dict[str, object]:
        calls["pdf"].append(payload)
        raise jobs.PdfExtractionError("the PDF has no extractable text")

    monkeypatch.setattr(jobs, "generate_story_job", story)
    monkeypatch.setattr(jobs, "process_pdf_job", pdf)
    return calls


async def test_dry_run_reports_without_touching_rows(
    settings_override: Settings, fast_jobs: dict[str, list[dict[str, object]]]
) -> None:
    settings = get_settings()
    running, pending = await _leave_interrupted_rows(settings)

    report = await jobs.run_resume(settings, apply=False, timeout=5)

    assert report.applied is False
    assert [(r.job_id, r.status) for r in report.unfinished] == [
        (running, JobStatus.RUNNING),
        (pending, JobStatus.PENDING),
    ]
    assert report.resumed == [] and report.outcomes == {}
    assert fast_jobs == {"story": [], "pdf": []}
    check = InProcessJobQueue(jobs.job_store_path(settings))
    statuses = {r.job_id: r.status for r in await check.list_unfinished()}
    assert statuses == {running: JobStatus.RUNNING, pending: JobStatus.PENDING}
    await check.close()


async def test_apply_resumes_and_reports_outcomes(
    settings_override: Settings, fast_jobs: dict[str, list[dict[str, object]]]
) -> None:
    settings = get_settings()
    running, pending = await _leave_interrupted_rows(settings)

    report = await jobs.run_resume(settings, apply=True, timeout=10)

    assert report.applied is True
    assert set(report.resumed) == {running, pending}
    assert report.outcomes == {running: JobStatus.SUCCEEDED, pending: JobStatus.FAILED}
    assert report.ok is False  # one job failed → operator should look
    assert fast_jobs["story"] == [{"chat_id": 1}] and fast_jobs["pdf"] == [{"chat_id": 2}]
    check = InProcessJobQueue(jobs.job_store_path(settings))
    assert await check.list_unfinished() == []
    record = await check.get_job(running)
    assert record is not None and record.attempts == 2
    await check.close()


async def test_apply_opens_the_notifier_around_the_run(
    settings_override: Settings,
    fast_jobs: dict[str, list[dict[str, object]]],
) -> None:
    """The injected notifier is opened before anything is resumed and closed after."""
    from contextlib import asynccontextmanager

    settings = get_settings()
    await _leave_interrupted_rows(settings)
    seen: list[tuple[str, int]] = []
    events: list[str] = []

    async def hook(record: Any) -> None:
        seen.append((record.job_type, int(record.payload["chat_id"])))

    @asynccontextmanager
    async def notifier() -> AsyncIterator[Any]:
        events.append("open")
        try:
            yield hook
        finally:
            events.append("close")

    report = await jobs.run_resume(settings, apply=True, timeout=10, notifier=notifier)

    assert report.notified is True
    assert sorted(seen) == sorted([(jobs.STORY_JOB, 1), (jobs.PDF_JOB, 2)])
    assert events == ["open", "close"]


async def test_bot_layer_notifier_refuses_before_resuming_when_telegram_is_unreachable(
    settings_override: Settings, fast_jobs: dict[str, list[dict[str, object]]], monkeypatch: Any
) -> None:
    """``job_notifier_for_token`` maps PTB errors to ``NotifierUnavailableError``; nothing runs."""
    import functools

    from telegram import Bot
    from telegram.error import InvalidToken

    from nexus_ai_agent.bot.app import job_notifier_for_token

    async def failing_initialize(self: Any) -> None:
        raise InvalidToken("bad token")

    monkeypatch.setattr(Bot, "initialize", failing_initialize)
    settings = get_settings()
    running, pending = await _leave_interrupted_rows(settings)
    notifier = functools.partial(job_notifier_for_token, settings.telegram_bot_token)
    with pytest.raises(jobs.NotifierUnavailableError, match="InvalidToken"):
        await jobs.run_resume(settings, apply=True, timeout=5, notifier=notifier)
    assert fast_jobs == {"story": [], "pdf": []}
    check = InProcessJobQueue(jobs.job_store_path(settings))
    assert {r.job_id for r in await check.list_unfinished()} == {running, pending}
    await check.close()


async def test_bot_layer_notifier_wraps_an_initialised_client(monkeypatch: Any) -> None:
    from telegram import Bot

    from nexus_ai_agent.bot.app import job_notifier_for_token
    from nexus_ai_agent.bot.job_notifications import TelegramJobNotifier

    calls: list[str] = []

    async def fake_initialize(self: Any) -> None:
        calls.append("initialize")

    async def fake_shutdown(self: Any) -> None:
        calls.append("shutdown")

    monkeypatch.setattr(Bot, "initialize", fake_initialize)
    monkeypatch.setattr(Bot, "shutdown", fake_shutdown)
    async with job_notifier_for_token("123456:token") as hook:
        assert isinstance(hook, TelegramJobNotifier) and isinstance(hook.bot, Bot)
        assert calls == ["initialize"]
    assert calls == ["initialize", "shutdown"]


async def test_resume_refuses_while_another_process_owns_the_store(
    settings_override: Settings, fast_jobs: dict[str, list[dict[str, object]]]
) -> None:
    settings = get_settings()
    await _leave_interrupted_rows(settings)
    bot_instance = jobs.build_job_queue(settings)
    await bot_instance.initialize()  # "the bot is running"
    try:
        with pytest.raises(JobStoreBusyError):
            await jobs.run_resume(settings, apply=True, timeout=5)
        # dry-run is a read: still allowed
        report = await jobs.run_resume(settings, apply=False, timeout=5)
        assert len(report.unfinished) == 2
    finally:
        await bot_instance.close()


def test_cli_jobs_resume_is_dry_run_by_default(
    settings_override: Settings, fast_jobs: dict[str, list[dict[str, object]]]
) -> None:
    from nexus_ai_agent.cli import app

    running, pending = asyncio.run(_leave_interrupted_rows(get_settings()))
    result = runner.invoke(app, ["jobs", "resume"])
    assert result.exit_code == 0, result.output
    assert running in result.output and pending in result.output
    assert "dry run" in result.output.lower() and "--apply" in result.output
    assert fast_jobs == {"story": [], "pdf": []}


def test_cli_jobs_resume_apply_exit_code_reflects_outcomes(
    settings_override: Settings, fast_jobs: dict[str, list[dict[str, object]]]
) -> None:
    from nexus_ai_agent.cli import app

    running, pending = asyncio.run(_leave_interrupted_rows(get_settings()))
    result = runner.invoke(app, ["jobs", "resume", "--apply", "--no-notify", "--timeout", "10"])
    assert result.exit_code == 1, result.output  # the pdf job failed
    assert "succeeded" in result.output and "failed" in result.output
    assert len(fast_jobs["story"]) == 1 and len(fast_jobs["pdf"]) == 1

    again = runner.invoke(app, ["jobs", "resume", "--apply", "--no-notify"])
    assert again.exit_code == 0, again.output
    assert "nothing to resume" in again.output.lower()


def test_cli_notify_composes_the_bot_layer_notifier(
    settings_override: Settings,
    fast_jobs: dict[str, list[dict[str, object]]],
    monkeypatch: Any,
) -> None:
    """``--notify`` (default) goes through ``bot.app.job_notifier_for_token`` with the token."""
    from contextlib import asynccontextmanager

    from nexus_ai_agent.bot import app as bot_app
    from nexus_ai_agent.cli import app

    tokens: list[str] = []

    @asynccontextmanager
    async def fake_notifier(token: str) -> AsyncIterator[Any]:
        tokens.append(token)

        async def hook(record: Any) -> None:
            return None

        yield hook

    monkeypatch.setattr(bot_app, "job_notifier_for_token", fake_notifier)
    asyncio.run(_leave_interrupted_rows(get_settings()))
    result = runner.invoke(app, ["jobs", "resume", "--apply", "--timeout", "10"])
    assert result.exit_code == 1, result.output  # pdf job fails by design of the fixture
    assert tokens == ["test-token"] and "notices: on" in result.output


def test_cli_jobs_resume_fails_fast_when_the_bot_owns_the_store(
    settings_override: Settings, fast_jobs: dict[str, list[dict[str, object]]]
) -> None:
    from nexus_ai_agent.cli import app

    async def scenario() -> tuple[int, str]:
        await _leave_interrupted_rows(get_settings())
        bot_instance = jobs.build_job_queue(get_settings())
        await bot_instance.initialize()
        try:
            # run the CLI in a worker thread: it owns its own event loop
            result = await asyncio.to_thread(
                runner.invoke, app, ["jobs", "resume", "--apply", "--no-notify"]
            )
        finally:
            await bot_instance.close()
        return result.exit_code, result.output

    code, output = asyncio.run(scenario())
    assert code == 2, output
    assert "another process owns the job store" in output and "is the bot running" in output
    assert fast_jobs == {"story": [], "pdf": []}


def test_runbook_documents_the_command() -> None:
    runbook = Path(__file__).parents[2] / "docs" / "ops" / "JOBS_RUNBOOK.md"
    text = runbook.read_text(encoding="utf-8")
    for needle in ("nexus jobs resume", "--apply", "dry run", "owns the job store", "post_stop"):
        assert needle in text, needle
