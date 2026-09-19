from __future__ import annotations

import secrets

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from nexus_ai_agent.api.dashboard import router as dashboard_router

app = FastAPI(title="NEXUS AI Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    from nexus_ai_agent.config.settings import get_settings

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
