"""NEXUS AI Telegram Bot — Application builder.

v2.1: All engines (Gemini, Image, Speech, Referral, Cloud, Queue)
are initialized here and passed to handlers via bot_data.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from telegram.ext import Application, ApplicationBuilder

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.presence import PresenceStore
from nexus_ai_agent.storage.ai_storage import AIStorageManager, ProviderConfig

from .handlers import (
    build_handlers,
)

logger = get_logger(__name__)
