"""STOP-B: legacy POST /creative/video-edit is a 410 tombstone."""

from __future__ import annotations

from fastapi.testclient import TestClient

from nexus_ai_agent.api.app import app


def test_video_edit_post_is_gone() -> None:
    client = TestClient(app)
    response = client.post("/creative/video-edit")
    assert response.status_code == 410
    body = response.json()
    assert "detail" in body
    assert "retired" in str(body.get("detail", "")).lower()
