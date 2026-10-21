"""Fail-safe boot for composition roots.

Unavailability degrades (the bot keeps running without self-state); an
integrity or charter failure is reported distinctly and NEVER yields a usable
state — features that need self-state (initiative, autonomy) must require
``status is OK``.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from nexus_ai_agent.selfhood.boot import CharterDriftError, SelfIntegrityError, SelfState, boot_self
from nexus_ai_agent.selfhood.charter import CHARTER, Charter

_LOGGER = logging.getLogger(__name__)


class SelfBootStatus(str, Enum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    TAMPERED = "tampered"
    CHARTER_DRIFT = "charter_drift"


@dataclass(frozen=True)
class SelfBoot:
    status: SelfBootStatus
    state: SelfState | None
    detail: str = ""


def self_ledger_db_path(db_path: Path | str) -> Path:
    """The Self-Ledger sidecar that belongs to one main database path."""
    return Path(f"{db_path}.self.sqlite3")


def try_boot_self(db_path: Path | str, *, charter: Charter = CHARTER) -> SelfBoot:
    try:
        return SelfBoot(SelfBootStatus.OK, boot_self(db_path, charter=charter))
    except SelfIntegrityError as exc:
        _LOGGER.error("self_ledger_tampered path=%s detail=%s", db_path, exc)
        return SelfBoot(SelfBootStatus.TAMPERED, None, str(exc))
    except CharterDriftError as exc:
        _LOGGER.error("self_charter_drift path=%s detail=%s", db_path, exc)
        return SelfBoot(SelfBootStatus.CHARTER_DRIFT, None, str(exc))
    except (sqlite3.Error, OSError) as exc:
        _LOGGER.warning("self_ledger_unavailable path=%s", db_path, exc_info=True)
        return SelfBoot(SelfBootStatus.UNAVAILABLE, None, str(exc))


__all__ = ["SelfBoot", "SelfBootStatus", "self_ledger_db_path", "try_boot_self"]
