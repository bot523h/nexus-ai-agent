from __future__ import annotations

import hmac as hmac_mod
import logging
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import httpx
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from nexus_ai_agent.api.dashboard import router as dashboard_router
from nexus_ai_agent.config.settings import get_settings
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
    """Parse the comma-separated ``NEXUS_API_CORS_ORIGINS`` allowlist."""
    return [origin.strip() for origin in (raw or "").split(",") if origin.strip()]


# CORS is an explicit allowlist (NEXUS_API_CORS_ORIGINS, comma-separated).
# Default is empty → no cross-origin browser access at all; the served
# dashboard is same-origin and needs none. The previous default
# (allow_origins=["*"] + allow_credentials=True) reflected *any* origin —
# effectively disabling the browser same-origin policy for this API.
_cors_origins = parse_cors_origins(get_settings().api_cors_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=bool(_cors_origins),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-NEXUS-Signature", "X-NEXUS-Timestamp"],
)

#: Signature freshness window for HMAC-authenticated mutating endpoints.
_HMAC_MAX_AGE_SECONDS = 300.0
_HMAC_TIMESTAMP_HEADER = "X-NEXUS-Timestamp"
_HMAC_SIGNATURE_HEADER = "X-NEXUS-Signature"
_hmac_warning_emitted = False
_logger = logging.getLogger(__name__)


async def require_hmac_signature(request: Request) -> None:
    """HMAC-SHA256 request signing for state-changing dashboard endpoints.

    When ``NEXUS_API_HMAC_KEY`` is set, the request must carry
    ``X-NEXUS-Timestamp`` (unix seconds) and
    ``X-NEXUS-Signature: hex(HMAC-SHA256(key, "{timestamp}:{raw_body}"))``;
    the timestamp must be within ±300 s and the comparison is constant-time.
    With no key configured the dependency keeps the legacy open behaviour
    and logs a one-time warning — deployments must set the key in production.
    """
    global _hmac_warning_emitted
    key = get_settings().api_hmac_key
    if not key:
        if not _hmac_warning_emitted:
            _hmac_warning_emitted = True
            _logger.warning(
                "NEXUS_API_HMAC_KEY is not set: mutating dashboard endpoints "
                "accept unauthenticated requests (legacy behaviour)."
            )
        return

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
    return """
    <!DOCTYPE html>
    <html lang="fa" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>NEXUS AI Dashboard</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@100;400;700&display=swap');
            body {
                font-family: 'Vazirmatn', sans-serif;
                background-color: #0f172a;
                color: #f8fafc;
            }
        </style>
    </head>
    <body class="p-8">
        <div class="max-w-4xl mx-auto">
            <header class="mb-12 text-center">
                <h1 class="text-4xl font-bold text-blue-400 mb-2">NEXUS AI Dashboard</h1>
                <p class="text-slate-400">پنل مدیریت هوشمند نسخه v3.2.0</p>
            </header>
            
            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-12">
                <div class="bg-slate-800 p-6 rounded-xl border border-slate-700">
                    <p class="text-slate-400 text-sm mb-1">کل کاربران</p>
                    <h2 id="total_users" class="text-3xl font-bold">-</h2>
                </div>
                <div class="bg-slate-800 p-6 rounded-xl border border-slate-700">
                    <p class="text-slate-400 text-sm mb-1">کل چت‌ها</p>
                    <h2 id="total_chats" class="text-3xl font-bold">-</h2>
                </div>
                <div class="bg-slate-800 p-6 rounded-xl border border-slate-700">
                    <p class="text-slate-400 text-sm mb-1">فایل‌های ابری</p>
                    <h2 id="total_files" class="text-3xl font-bold">-</h2>
                </div>
                <div class="bg-slate-800 p-6 rounded-xl border border-slate-700">
                    <p class="text-slate-400 text-sm mb-1">Agentهای فعال</p>
                    <h2 id="active_agents" class="text-3xl font-bold">-</h2>
                </div>
            </div>

            <div class="bg-slate-800 rounded-xl border border-slate-700 overflow-hidden">
                <div class="p-6 border-b border-slate-700">
                    <h3 class="text-xl font-bold">آخرین کاربران پیوسته</h3>
                </div>
                <div id="recent_users" class="p-6">
                    <p class="text-slate-400">در حال بارگذاری...</p>
                </div>
            </div>
        </div>

        <script>
            async function loadStats() {
                try {
                    const res = await fetch('/api/dashboard/stats');
                    const data = await res.json();
                    document.getElementById('total_users').innerText = data.total_users;
                    document.getElementById('total_chats').innerText = data.total_chats;
                    document.getElementById('total_files').innerText = data.total_files;
                    const activeAgents = data.active_specialized_agents;
                    document.getElementById('active_agents').innerText = activeAgents;
                } catch (e) { console.error(e); }
            }

            async function loadRecentUsers() {
                try {
                    const res = await fetch('/api/dashboard/recent_users');
                    const data = await res.json();
                    const container = document.getElementById('recent_users');
                    if (data.length === 0) {
                        container.innerHTML = '<p class="text-slate-400">هیچ کاربری یافت نشد.</p>';
                        return;
                    }
                    let html = '<ul class="divide-y divide-slate-700">';
                    data.forEach(u => {
                        const name = u.username || 'بدون نام';
                        html += `
                            <li class="py-3 flex justify-between items-center">
                                <div>
                                    <span class="font-bold text-blue-300">${name}</span>
                                    <span class="text-slate-500 text-sm ml-2">
                             ID: ${u.telegram_id}
                         </span>
                                </div>
                                <span class="bg-slate-700 px-2 py-1 rounded text-xs text-slate-300">
                                    User #${u.id}
                                </span>
                            </li>
                        `;
                    });
                    html += '</ul>';
                    container.innerHTML = html;
                } catch (e) { console.error(e); }
            }

            loadStats();
            loadRecentUsers();
            setInterval(loadStats, 30000);
        </script>
    </body>
    </html>
    """


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe for webhook/scale-to-zero platforms (v3.8.0).

    Deliberately touches no database and no engine: a 200 here means only
    "the process is up and accepting requests", which is exactly what the
    platform health gate should ask before routing traffic.
    """
    return {"status": "ok"}


async def _save_upload_to_temp(upload: UploadFile) -> str:
    settings = get_settings()
    temp_dir = Path(settings.creative_temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload.filename or "upload.bin").suffix or ".bin"
    fd, temp_path = tempfile.mkstemp(prefix="creative-upload-", suffix=suffix, dir=temp_dir)
    os.close(fd)
    with Path(temp_path).open("wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    await upload.close()
    return temp_path


async def _download_video_to_temp(video_url: str) -> str:
    settings = get_settings()
    temp_dir = Path(settings.creative_temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(urlparse(video_url).path).suffix or ".mp4"
    fd, temp_path = tempfile.mkstemp(prefix="creative-url-", suffix=suffix, dir=temp_dir)
    os.close(fd)
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
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
        await registry.update_job_status(
            job_id,
            "done",
            result=result.model_dump(),
        )
    except Exception as exc:
        await registry.update_job_status(job_id, "failed", error=str(exc))
    finally:
        if local_input_path:
            Path(local_input_path).unlink(missing_ok=True)


@app.post("/creative/video-edit", dependencies=[Depends(require_hmac_signature)])
async def create_video_edit_job(
    background_tasks: BackgroundTasks,
    file: Annotated[UploadFile | None, File()] = None,
    video_url: Annotated[str | None, Form()] = None,
) -> dict[str, str]:
    if file is None and not video_url:
        raise HTTPException(status_code=400, detail="Provide either file or video_url")
    if file is not None and video_url:
        raise HTTPException(status_code=400, detail="Provide only one video source")

    source: str
    cleanup_path: str | None = None
    input_data: dict[str, str | None]

    if file is not None:
        cleanup_path = await _save_upload_to_temp(file)
        source = cleanup_path
        input_data = {
            "source_type": "upload",
            "path": cleanup_path,
            "filename": file.filename,
            "content_type": file.content_type,
        }
    else:
        normalized_url = (video_url or "").strip()
        if not normalized_url:
            raise HTTPException(status_code=400, detail="video_url must not be empty")
        source = normalized_url
        input_data = {
            "source_type": "url",
            "video_url": normalized_url,
            "filename": None,
            "content_type": None,
        }

    job_id = await get_creative_registry().create_job("video_edit", input_data)
    background_tasks.add_task(_process_video_edit_job, job_id, source, cleanup_path)
    return {"job_id": job_id, "status": "pending"}


@app.get("/creative/jobs/{job_id}")
async def get_job_status(job_id: str) -> dict[str, object]:
    job = await get_creative_registry().get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> JSONResponse:
    """Receive Telegram webhook deliveries (webhook run-mode, v3.8.0).

    The shared secret (``NEXUS_WEBHOOK_SECRET``) is compared constant-time
    against the header Telegram echoes from ``set_webhook(secret_token=...)``.
    Verified payloads are converted into a PTB ``Update`` (via the
    ``WebhookApplicationAdapter`` in ``bot/app.py`` — the sanctioned telegram
    import boundary) and pushed to the application's update queue; actual
    processing happens on the application's own loop.
    """
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
        # run_webhook() always publishes the application before serving;
        # reaching this means a cold-start race or a misconfiguration.
        # 503 makes Telegram retry the delivery instead of dropping it.
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
        # Telegram always includes update_id; anything else is malformed.
        return JSONResponse({"ok": False, "error": "missing update_id"}, status_code=400)

    adapter.enqueue(update)
    return JSONResponse({"ok": True}, status_code=200)
