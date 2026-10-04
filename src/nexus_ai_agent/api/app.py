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
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from nexus_ai_agent.api.dashboard import router as dashboard_router
from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.core.ssrf_guard import SafeAsyncTransport, validate_url
from nexus_ai_agent.creative import image_post
from nexus_ai_agent.creative.job_registry import JobRegistry

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

#: Resource cap used by the retained upload helper (task-165, ADR 0006).
#: The retired ``POST /creative/video-edit`` route does not call that helper;
#: the canonical creative surface caps media by duration (30 s) at validation.
_MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MiB


async def require_hmac_signature(request: Request) -> None:
    """HMAC-SHA256 request authentication for the legacy job-status endpoint.

    **Fail-closed**: without a configured ``NEXUS_API_HMAC_KEY`` the
    endpoint is disabled outright and answers ``503 Security configuration
    incomplete``. With a key, the request must carry ``X-NEXUS-Timestamp``
    (unix seconds) and
    ``X-NEXUS-Signature: hex(HMAC-SHA256(key, "{timestamp}:{raw_body}"))``;
    the timestamp must be within ±300 s and the comparison is constant-time.
    """
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
        # A rejected (or failed) upload never leaves a partial file behind.
        Path(temp_path).unlink(missing_ok=True)
        raise
    await upload.close()
    return temp_path


async def _download_video_to_temp(video_url: str) -> str:
    """Download a user-supplied video URL to a temp file (SSRF-hardened).

    Defence in depth (the URL is attacker-controlled input on a
    deprecated-but-live route):

    1. ``validate_url`` fail-fast *before* any temp file is created —
       https-only and every resolved address must be public (blocks
       loopback, RFC1918, cloud metadata, IPv6 loopback, IPv4-mapped
       forms and ``user@host`` tricks).
    2. The fetch itself runs through :class:`SafeAsyncTransport`, whose
       httpcore backend re-resolves and re-checks the address at every
       TCP connection — including every redirect hop — and pins the
       connection to the validated IP (closes the DNS-rebinding TOCTOU
       that a preflight-only check would leave open).
    """
    # Sync DNS resolution: keep it off the event loop.
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
        # A refused/blocked/failed download never leaves a partial file.
        Path(temp_path).unlink(missing_ok=True)
        raise
    return temp_path


# STOP-B tombstone: keep the historical URL discoverable, but never parse,
# authorize, persist, enqueue, download, or process its request body.
@app.post("/creative/video-edit", deprecated=True, status_code=410)
async def create_video_edit_job() -> JSONResponse:
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
    # Legacy read gate (task-165, ADR 0006): job rows carry local paths and
    # source URLs, so this endpoint remains behind the fail-closed HMAC gate.
    # A GET signs "{timestamp}:" + empty body; unsigned callers cannot read
    # the old job registry. The POST that used to create these jobs is retired.
    await require_hmac_signature(request)
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
