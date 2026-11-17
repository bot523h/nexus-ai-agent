"""Shared fixtures and helpers for the scene-pack tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from nexus_ai_agent.creative.studio.capabilities import OperationContext, OperationOutcome
from nexus_ai_agent.creative.studio.models import Project, Timeline, TypedCommand


def make_project(project_id: str = "p1") -> Project:
    return Project(
        project_id=project_id, name="scene-test", timeline=Timeline(timeline_id="t1", duration_us=0)
    )


def make_context(payload: dict[str, Any]) -> OperationContext:
    return OperationContext(
        command=TypedCommand(command_id="c1", operation="scene.test", input={}),
        input_data=payload,
        history=(),
    )


@dataclass
class RecordingHandler:
    """Call a scene handler and capture its outcome."""

    project: Project
    context: OperationContext

    def __call__(self, handler: Any) -> OperationOutcome:
        return handler(self.project, self.context)


def image(seed: int = 0, size: int = 24) -> np.ndarray:
    return np.random.default_rng(seed).random((size, size, 3)).astype(np.float32)


def mask(size: int = 24, box: tuple[int, int, int, int] = (6, 6, 16, 16)) -> np.ndarray:
    m = np.zeros((size, size), dtype=np.float32)
    m[box[0] : box[2], box[1] : box[3]] = 1.0
    return m


def depth(seed: int = 1, size: int = 24) -> np.ndarray:
    return np.random.default_rng(seed).random((size, size)).astype(np.float32)
