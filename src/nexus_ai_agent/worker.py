"""Application-owned background job implementations.

The functions in this module are ordinary async jobs. They are executed by
``InProcessJobQueue`` on the bot process event loop; no broker or worker
process is required.
"""

from __future__ import annotations


async def process_pdf_task(user_id: int, file_path: str, file_id: str) -> str:
    """Process a PDF job and return a durable success message.

    PDF extraction remains the existing text-file-compatible implementation;
    replacing that parser is outside the queue-migration scope. Failures are
    raised so the queue can persist ``failed`` rather than reporting success.
    """
    from nexus_ai_agent.features.rag import AdvancedRAGEngine

    try:
        with open(file_path, encoding="utf-8") as handle:
            text = handle.read()
        engine = AdvancedRAGEngine()
        await engine.add_document(user_id, text, {"file_id": file_id})
    except Exception as exc:  # noqa: BLE001 - queue owns durable failure mapping
        raise RuntimeError(f"Error processing {file_id}: {exc}") from exc
    return f"Successfully processed {file_id}"


async def generate_story_task(user_id: int, text: str, output_path: str) -> str:
    """Generate a story image locally and return its output path."""
    from nexus_ai_agent.features.story_gen import AIStoryGenerator

    try:
        generator = AIStoryGenerator()
        await generator.generate_story_image(text, output_path)
    except Exception as exc:  # noqa: BLE001 - queue owns durable failure mapping
        raise RuntimeError(f"Error generating story for user {user_id}: {exc}") from exc
    return output_path


async def process_pdf_job(payload: dict[str, object]) -> dict[str, object]:
    result = await process_pdf_task(
        int(str(payload["user_id"])),
        str(payload["file_path"]),
        str(payload["file_id"]),
    )
    return {"message": result}


async def generate_story_job(payload: dict[str, object]) -> dict[str, object]:
    result = await generate_story_task(
        int(str(payload["user_id"])),
        str(payload["text"]),
        str(payload["output_path"]),
    )
    return {"output_path": result}


async def nightly_channel_management() -> str:
    """Run the existing nightly channel operation in-process."""
    from telegram import Bot

    from nexus_ai_agent.config.settings import get_settings
    from nexus_ai_agent.features.channel_manager import ChannelManager

    settings = get_settings()
    bot = Bot(token=settings.telegram_bot_token)
    manager = ChannelManager(bot)
    await manager.run_nightly_tasks()
    return "Nightly tasks completed."


__all__ = [
    "generate_story_job",
    "generate_story_task",
    "nightly_channel_management",
    "process_pdf_job",
    "process_pdf_task",
]
