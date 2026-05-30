from __future__ import annotations

import asyncio
import logging
import os

from celery import Celery

from nexus_ai_agent.config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

celery_app = Celery(
    "nexus_tasks",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)


@celery_app.task(name="process_pdf_task")
def process_pdf_task(user_id: int, file_path: str, file_id: str) -> str:
    """Background task for PDF chunking and embedding."""
    from nexus_ai_agent.features.rag import AdvancedRAGEngine

    async def _run():
        engine = AdvancedRAGEngine()
        # Mocking PDF extraction (should use a real PDF library in production)
        # For now, we assume file_path points to a text file or we just read it as text
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read()
            await engine.add_document(user_id, text, {"file_id": file_id})
            return f"Successfully processed {file_id}"
        except Exception as e:
            return f"Error processing {file_id}: {str(e)}"

    return asyncio.run(_run())


@celery_app.task(name="generate_story_task")
def generate_story_task(user_id: int, text: str, output_path: str) -> str:
    """Background task for Pillow story rendering."""
    from nexus_ai_agent.features.story_gen import AIStoryGenerator

    async def _run():
        gen = AIStoryGenerator()
        try:
            await gen.generate_story_image(text, output_path)
            return output_path
        except Exception as e:
            return f"Error: {str(e)}"

    return asyncio.run(_run())

@celery_app.task(name="nightly_channel_management")
def nightly_channel_management() -> str:
    """Background task for nightly channel management."""
    from telegram import Bot
    from nexus_ai_agent.features.channel_manager import ChannelManager
    
    async def _run():
        bot = Bot(token=settings.telegram_bot_token)
        mgr = ChannelManager(bot)
        await mgr.run_nightly_tasks()
        return "Nightly tasks completed."

    return asyncio.run(_run())
