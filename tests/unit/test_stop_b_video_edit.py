"""STOP-B: legacy POST /creative/video-edit is a 410 tombstone.

RED on main: the route accepted HMAC + multipart/url and enqueued work.
GREEN here: every shape returns 410 with zero job/registry side effects.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from nexus_ai_agent.api import app as app_module
from nexus_ai_agent.api.app import app, create_video_edit_job


def test_video_edit_post_is_gone() -> None:
    client = TestClient(app)
    response = client.post("/creative/video-edit")
    assert response.status_code == 410
    body = response.json()
    assert "detail" in body
    assert "retired" in str(body.get("detail", "")).lower()


def test_video_edit_multipart_and_url_still_410_without_registry() -> None:
    client = TestClient(app)
    with patch.object(app_module, "get_creative_registry") as reg:
        reg.return_value = AsyncMock()
        reg.return_value.create_job = AsyncMock(return_value="should-not-run")
        r1 = client.post(
            "/creative/video-edit",
            data={"video_url": "https://example.com/x.mp4"},
        )
        r2 = client.post(
            "/creative/video-edit",
            files={"file": ("clip.mp4", b"fake", "video/mp4")},
        )
    assert r1.status_code == 410
    assert r2.status_code == 410
    reg.return_value.create_job.assert_not_called()


def test_tombstone_handler_accepts_no_request_or_background_tasks() -> None:
    sig = inspect.signature(create_video_edit_job)
    assert list(sig.parameters) == []


def test_ast_route_does_not_call_form_or_enqueue() -> None:
    """Mutation: if someone reintroduces form()/create_job in the handler, fail."""
    src = Path(app_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    handler = None
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "create_video_edit_job":
            handler = node
            break
    assert handler is not None
    text = ast.unparse(handler)
    assert "request.form" not in text
    assert "create_job" not in text
    assert "background_tasks" not in text
    assert "_process_video_edit_job" not in text
    assert "require_hmac_signature" not in text
