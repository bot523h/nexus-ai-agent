from __future__ import annotations

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
from nexus_ai_agent.core.ssrf_guard import (
    SafeAsyncTransport,
    SSRFBlockError,
    validate_url,
)
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

#: Resource caps for the deprecated legacy lane (owner directive 2026-09-24 §3).
#: The canonical path (Telegram surface → queue → render lane) never touches
#: these; they exist so the legacy HTTP lane cannot be used as an amplifier.
_MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MiB
_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # 200 MiB
_DOWNLOAD_TIMEOUT_SECONDS = 60.0
_MAX_REDIRECTS = 5
#: Only media-ish bodies are accepted from a caller-supplied URL.
_ALLOWED_DOWNLOAD_PREFIXES = (
    "video/",
    "application/octet-stream",
    "binary/octet-stream",
)

#: Keys of a legacy job row that may leave the process.  Everything else —
#: ``input_data`` (local paths, source URLs), the raw ``error`` text (paths and
#: internal exceptions) — stays inside (owner directive §5).
_PUBLIC_JOB_RESULT_KEYS = (
    "success",
    "output_sha256",
    "size_bytes",
    "duration_us",
    "width",
    "height",
    "has_audio",
)


async def require_hmac_signature(request: Request) -> None:
    """HMAC-SHA256 request signing for state-changing dashboard endpoints.

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
        # A rejected or failed upload never leaves a partial file behind.
        Path(temp_path).unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    if written == 0:
        Path(temp_path).unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    return temp_path


def _download_suffix(video_url: str) -> str:
    return Path(urlparse(video_url).path).suffix or ".mp4"


async def _download_video_to_temp(video_url: str) -> str:
    """Fetch a caller-supplied URL through the repository's canonical guard.

    Owner directive §4: the download path uses ``core/ssrf_guard`` — there is
    no second downloader in this codebase.  ``validate_url`` runs *before* the
    fetch (https-only, and every address the host resolves to must be public)
    and :class:`SafeAsyncTransport` re-resolves and re-checks the address at
    every TCP connect, so a redirect or a DNS rebind towards loopback / RFC1918
    / link-local / cloud-metadata space fails at connect time.  The read is
    bounded and a rejected fetch leaves no partial file behind.
    """
    try:
        validate_url(video_url)
    except SSRFBlockError as exc:
        raise HTTPException(
            status_code=400, detail="video_url is not an allowed remote source"
        ) from exc

    settings = get_settings()
    temp_dir = Path(settings.creative_temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix="creative-url-", suffix=_download_suffix(video_url), dir=temp_dir
    )
    os.close(fd)
    written = 0
    try:
        async with httpx.AsyncClient(
            timeout=_DOWNLOAD_TIMEOUT_SECONDS,
            follow_redirects=True,
            max_redirects=_MAX_REDIRECTS,
            transport=SafeAsyncTransport(),
        ) as client:
            async with client.stream("GET", video_url) as response:
                if response.status_code >= 400:
                    raise HTTPException(
                        status_code=502,
                        detail=f"remote source answered {response.status_code}",
                    )
                content_type = str(response.headers.get("content-type", "")).lower()
                if content_type and not content_type.startswith(_ALLOWED_DOWNLOAD_PREFIXES):
                    raise HTTPException(
                        status_code=415, detail="remote source is not media content"
                    )
                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        too_big = int(declared) > _MAX_DOWNLOAD_BYTES
                    except ValueError:
                        too_big = False
                    if too_big:
                        raise HTTPException(
                            status_code=413,
                            detail=f"remote source exceeds {_MAX_DOWNLOAD_BYTES} bytes",
                        )
                with Path(temp_path).open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > _MAX_DOWNLOAD_BYTES:
                            raise HTTPException(
                                status_code=413,
                                detail=f"remote source exceeds {_MAX_DOWNLOAD_BYTES} bytes",
                            )
                        handle.write(chunk)
        if written == 0:
            raise HTTPException(status_code=502, detail="remote source returned no bytes")
    except HTTPException:
        Path(temp_path).unlink(missing_ok=True)
        raise
    except (httpx.HTTPError, SSRFBlockError) as exc:
        Path(temp_path).unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail="could not fetch the remote source") from exc
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


@app.post("/creative/video-edit", deprecated=True)
async def create_video_edit_job(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Legacy one-shot edit lane — deprecated, fail-closed, SSRF-safe.

    Architecture decision (owner directive §3): the canonical production path
    for creative work is Telegram surface → durable queue → ``creative_render``
    → render lane; this HTTP route is *not* wired to the bot and is kept only
    for external callers that already hold the HMAC key.  It is therefore
    fail-closed (503 without ``NEXUS_API_HMAC_KEY``), authenticated, capped
    (500 MiB uploads / 200 MiB URL fetches), and its URL branch goes through
    the canonical SSRF guard.  New integrations must use the canonical path.
    """
    # Fail-closed HMAC gate runs BEFORE any form parsing so the raw body is
    # read exactly once here (starlette caches it in request._body, and the
    # multipart parser below reuses that cache).
    await require_hmac_signature(request)

    form = await request.form()
    upload = form.get("file")
    # request.form() yields starlette UploadFile instances (fastapi's class
    # only subclasses it), so the isinstance target is the starlette one.
    file = upload if isinstance(upload, StarletteUploadFile) else None
    raw_video_url = form.get("video_url")
    video_url = str(raw_video_url) if isinstance(raw_video_url, str) and raw_video_url else None

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
        # Validate the URL *in the request path*, before a job row exists: an
        # internal/loopback/unsupported target must be a visible 400, never a
        # background job that quietly fails after the caller got a job id.
        # (_download_video_to_temp validates again at fetch time, and the
        # transport re-checks every connect — including redirect hops.)
        try:
            validate_url(normalized_url)
        except SSRFBlockError as exc:
            raise HTTPException(
                status_code=400, detail="video_url is not an allowed remote source"
            ) from exc
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


def _public_job_view(job: dict[str, object]) -> dict[str, object]:
    """Minimal, safe projection of a legacy job row (owner directive §5).

    Never returned to a caller: ``input_data`` (staged local paths, source
    URLs, filenames), the raw ``error`` string (may embed paths or internal
    exception text) and anything else the row happens to carry.
    """
    result = job.get("result")
    safe_result: dict[str, object] | None = None
    if isinstance(result, dict):
        safe_result = {key: result[key] for key in _PUBLIC_JOB_RESULT_KEYS if key in result}
    status = str(job.get("status", ""))
    return {
        "job_id": str(job.get("id", "")),
        "job_type": str(job.get("job_type", "")),
        "status": status,
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "result": safe_result,
        # A closed vocabulary: the raw exception text stays server-side.
        "failure_code": "job_failed" if status == "failed" else None,
    }


@app.get("/creative/jobs/{job_id}", deprecated=True)
async def get_job_status(job_id: str, request: Request) -> dict[str, object]:
    """Legacy job read — deprecated, authenticated and minimized.

    Authentication/authorization uses the same fail-closed HMAC gate as the
    POST: without ``NEXUS_API_HMAC_KEY`` the route is disabled (503), an
    unsigned or stale request is rejected (401), and a caller holding a
    different key cannot read jobs it did not create.  The legacy registry has
    no per-user column (and no migration is allowed), so the *principal* of
    this lane is the API-key holder; the response is minimized as well, so a
    future gate regression still cannot leak paths or internals.
    """
    await require_hmac_signature(request)
    job = await get_creative_registry().get_job(job_id)
    if job is None:
        # Unknown and unauthorized-looking ids answer identically (no oracle).
        raise HTTPException(status_code=404, detail="Job not found")
    return _public_job_view(job)


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
