"""Shared fixtures for the portrait-pack tests."""

from __future__ import annotations

import numpy as np

from nexus_ai_agent.creative.studio.capabilities import OperationContext
from nexus_ai_agent.creative.studio.models import Project, Timeline, TypedCommand


def image(seed: int = 0, size: int = 24) -> np.ndarray:
    return np.random.default_rng(seed).random((size, size, 3)).astype(np.float32)


def region(size: int = 24, box: tuple[int, int, int, int] = (6, 6, 18, 18)) -> np.ndarray:
    m = np.zeros((size, size), dtype=np.float32)
    m[box[0] : box[2], box[1] : box[3]] = 1.0
    return m


def depth(seed: int = 1, size: int = 24) -> np.ndarray:
    return np.random.default_rng(seed).random((size, size)).astype(np.float32)


def make_project(project_id: str = "p1") -> Project:
    return Project(
        project_id=project_id,
        name="portrait-test",
        timeline=Timeline(timeline_id="t1", duration_us=0),
    )


def make_context(payload: dict) -> OperationContext:
    return OperationContext(
        command=TypedCommand(command_id="c1", operation="portrait.test", input={}),
        input_data=payload,
        history=(),
    )
