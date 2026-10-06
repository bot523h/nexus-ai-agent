from __future__ import annotations

import asyncio
import hmac as hmac_mod
import os
import secrets
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from nexus_ai_agent.api.dashboard import router as dashboard_router
from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.core.ssrf_guard import SafeAsyncTransport, SSRFBlockError, validate_url
from nexus_ai_agent.creative import image_post
from nexus_ai_agent.creative.ffmpeg_executor import execute_ffmpeg_commands
from nexus_ai_agent.creative.job_registry import JobRegistry
from nexus_ai_agent.creative.video_director import analyze_video_with_gemini

CREATIVE_IMAGE_POST_AVAILABLE = hasattr(image_post, "generate_image_post")
_creative_registry: JobRegistry | None = None


def get_creative_registry() -> JobRegistry:
    global _creative_registry
    settings = get_settings()
    registry_path = Path(settings.creative_temp_dir) / "creative_jobs.sqlite3"
    if _creative_registry is None or _creative_registry._db_path != registry_path:
        _creative_registry = JobRegistry(registry_path)
    return _creative_registry


app = FastAPI(title="NEXUS AI Dashboard")


def parse_cors_origins(raw: str) -> list[str]:
    return [origin.strip() for origin in (raw or "").split(",") if origin.strip()]


_cors_origins = parse_cors_origins(get_settings().api_cors_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=bool(_cors_origins),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-NEXUS-Signature", "X-NEXUS-Timestamp"],
)

_HMAC_MAX_AGE_SECONDS = 300.0
_HMAC_TIMESTAMP_HEADER = "X-NEXUS-Timestamp"
_HMAC_SIGNATURE_HEADER = "X-NEXUS-Signature"
_MAX_UPLOAD_BYTES = 500 * 1024 * 1024


async def require_hmac_signature(request: Request) -> None:
    key = get_settings().api_hmac_key
    if not key:
        raise HTTPException(status_code=503, detail="Security configuration incomplete")
    timestamp = request.headers.get(_HMAC_TIMESTAMP_HEADER)
    signature = request.headers.get(_HMAC_SIGNATURE_HEADER)
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="missing signature headers")
    try:
        age = abs(time.time() - float(timestamp))
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid signature timestamp") from None
    if age > _HMAC_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="stale signature timestamp")
    body = await request.body()
    message = f"{timestamp}:".encode() + body
    expected = hmac_mod.new(key.encode(), message, "sha256").hexdigest()
    if not hmac_mod.compare_digest(signature.strip().lower(), expected):
        raise HTTPException(status_code=401, detail="invalid signature")


app.include_router(dashboard_router)


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return "<!DOCTYPE html><html><body>NEXUS AI Dashboard</body></html>"


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def _save_upload_to_temp(upload: StarletteUploadFile) -> str:
    settings = get_settings()
    temp_dir = Path(settings.creative_temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload.filename or "upload.bin").suffix or ".bin"
    fd, temp_path = tempfile.mkstemp(prefix="creative-upload-", suffix=suffix, dir=temp_dir)
    os.close(fd)
    written = 0
    try:
        with Path(temp_path).open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"upload exceeds the {_MAX_UPLOAD_BYTES}-byte limit",
                    )
                handle.write(chunk)
    except Exception:
        Path(temp_path).unlink(missing_ok=True)
        raise
    await upload.close()
    return temp_path


async def _download_video_to_temp(video_url: str) -> str:
    await asyncio.to_thread(validate_url, video_url)
    settings = get_settings()
    temp_dir = Path(settings.creative_temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(urlparse(video_url).path).suffix or ".mp4"
    fd, temp_path = tempfile.mkstemp(prefix="creative-url-", suffix=suffix, dir=temp_dir)
    os.close(fd)
    try:
        async with httpx.AsyncClient(
            timeout=60.0, transport=SafeAsyncTransport(), follow_redirects=True
        ) as client:
            async with client.stream("GET", video_url) as response:
                response.raise_for_status()
                with Path(temp_path).open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
    except Exception:
        Path(temp_path).unlink(missing_ok=True)
        raise
    return temp_path


async def _process_video_edit_job(
    job_id: str,
    source: str,
    cleanup_path: str | None = None,
) -> None:
    """Retained only for historical callers; POST route no longer enqueues this."""
    local_input_path = cleanup_path
    registry = get_creative_registry()
    try:
        await registry.update_job_status(job_id, "processing")
        if source.startswith(("http://", "https://")):
            local_input_path = await _download_video_to_temp(source)
        if local_input_path is None:
            raise RuntimeError("No local video source available for processing")
        settings = get_settings()
        plan = await analyze_video_with_gemini(
            local_input_path,
            settings.creative_gemini_api_key or "",
        )
        output_path = str(Path(settings.creative_temp_dir) / f"{job_id}.mp4")
        result = await execute_ffmpeg_commands(plan, local_input_path, output_path)
        if not result.success:
            raise RuntimeError(result.error_message or "FFmpeg execution failed")
        await registry.update_job_status(job_id, "done", result=result.model_dump())
    except Exception as exc:
        await registry.update_job_status(job_id, "failed", error=str(exc))
    finally:
        if local_input_path:
            Path(local_input_path).unlink(missing_ok=True)


@app.post("/creative/video-edit", deprecated=True, status_code=410)
async def create_video_edit_job() -> JSONResponse:
    # STOP-B tombstone: never parse, authorize, persist, enqueue, or process.
    return JSONResponse(
        status_code=410,
        content={
            "detail": "This legacy video-edit endpoint has been retired.",
            "canonical_execution_lane": (
                "Telegram creative surface → durable job queue → "
                "packs runtime registry → render lane"
            ),
        },
    )


@app.get("/creative/jobs/{job_id}", deprecated=True)
async def get_job_status(job_id: str, request: Request) -> dict[str, object]:
    await require_hmac_signature(request)
    job = await get_creative_registry().get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> JSONResponse:
    from nexus_ai_agent.bot.app import WebhookApplicationAdapter
    from nexus_ai_agent.bot.webhook import TELEGRAM_SECRET_TOKEN_HEADER

    secret = getattr(request.app.state, "webhook_secret", None)
    if secret is None:
        secret = get_settings().webhook_secret or ""
    provided = request.headers.get(TELEGRAM_SECRET_TOKEN_HEADER)
    if not secret or provided is None or not secrets.compare_digest(provided, secret):
        return JSONResponse({"ok": False, "error": "invalid secret"}, status_code=403)

    application = getattr(request.app.state, "webhook_application", None)
    if application is None:
        return JSONResponse(
            {"ok": False, "error": "webhook application not ready"}, status_code=503
        )

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "invalid payload"}, status_code=400)

    adapter = WebhookApplicationAdapter(application)
    try:
        update = adapter.parse_update(payload)
    except Exception:
        return JSONResponse({"ok": False, "error": "unparsable update"}, status_code=400)
    if update is None:
        return JSONResponse({"ok": False, "error": "missing update_id"}, status_code=400)

    adapter.enqueue(update)
    return JSONResponse({"ok": True}, status_code=200)
