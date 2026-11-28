/*
 * NAGAR Studio — authenticated product client.
 *
 * Pure presentation/mapping helpers live in the factory and are exported for
 * Node so the mapping from API payloads to what a human sees is unit-tested
 * without a browser. DOM wiring runs only when a document exists.
 *
 * Security model: the principal token is posted once to
 * `/studio/app/api/login` and never kept by this script. The server returns an
 * opaque handle in an HttpOnly cookie, so nothing here reads, stores or
 * forwards a credential — no localStorage, no sessionStorage, no query string.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.StudioSurface = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const API = "/studio/app/api";
  const MARKER = { "x-nagar-studio": "1" };

  const GATES = {
    PASS: { label: "PASS", tone: "ok" },
    FAIL: { label: "FAIL", tone: "bad" },
    ABSTAIN_INSUFFICIENT_EVIDENCE: { label: "ABSTAIN", tone: "warn" },
  };
  const PERMISSIONS = {
    granted: { label: "مجاز", tone: "ok" },
    allow: { label: "مجاز", tone: "ok" },
    requires_approval: { label: "نیازمند تأیید", tone: "warn" },
    ask: { label: "نیازمند تأیید", tone: "warn" },
    denied: { label: "ممنوع", tone: "bad" },
    forbidden: { label: "ممنوع", tone: "bad" },
  };
  const UNKNOWN_BADGE = { label: "نامشخص", tone: "" };

  function gateBadge(verdict) {
    const key = typeof verdict === "string" ? verdict.trim().toUpperCase() : "";
    return GATES[key] ? { ...GATES[key] } : { ...UNKNOWN_BADGE };
  }

  function permissionBadge(level) {
    const key = typeof level === "string" ? level.trim().toLowerCase() : "";
    return PERMISSIONS[key] ? { ...PERMISSIONS[key] } : { ...UNKNOWN_BADGE };
  }

  function uncertaintyLabel(value) {
    if (typeof value === "number" && Number.isFinite(value)) {
      return "±" + Math.abs(value).toFixed(3);
    }
    if (value && typeof value === "object") {
      const expanded = Number(value.expanded);
      if (Number.isFinite(expanded)) {
        const coverage = Number(value.coverage);
        const suffix = Number.isFinite(coverage) ? " (" + (coverage * 100).toFixed(0) + "%)" : "";
        return "±" + Math.abs(expanded).toFixed(3) + suffix;
      }
    }
    return "—";
  }

  function truncate(text, limit) {
    const value = typeof text === "string" ? text.trim() : "";
    if (!value) return "بدون متن";
    const max = Number.isFinite(limit) ? limit : 80;
    return value.length <= max ? value : value.slice(0, max - 1) + "…";
  }

  function turnView(turn) {
    const t = turn && typeof turn === "object" ? turn : {};
    const state =
      typeof t.state === "string" ? t.state : typeof t.status === "string" ? t.status : "unknown";
    const sequence = Number.isFinite(Number(t.sequence_number)) ? Number(t.sequence_number) : null;
    const badges = [
      { label: state, tone: state === "failed" ? "bad" : state === "completed" ? "ok" : "" },
      gateBadge(t.gate || t.verdict),
      permissionBadge(t.permission || t.permission_level),
      { label: "u " + uncertaintyLabel(t.uncertainty), tone: "" },
    ];
    return {
      title: truncate(t.text || t.intent),
      subtitle: sequence === null ? "نوبت" : "نوبت " + sequence,
      badges,
      body: typeof t.plan_summary === "string" ? t.plan_summary : "",
    };
  }

  /* The sessions panel shows what the durable store really holds. */
  function sessionItems(payload) {
    const body = payload && typeof payload === "object" ? payload : {};
    const rows = Array.isArray(body.sessions) ? body.sessions : [];
    return rows.map(function (row) {
      const session = row && typeof row === "object" ? row : {};
      const id = typeof session.session_id === "string" ? session.session_id : "";
      const status = typeof session.status === "string" ? session.status : "unknown";
      const turns = Number.isFinite(Number(session.max_sequence)) ? Number(session.max_sequence) : 0;
      return {
        id: id,
        title: id ? truncate(id, 18) : "بدون شناسه",
        detail: status + " · " + turns + " نوبت · epoch " + (session.session_epoch || "?"),
        status: status,
        maxSequence: turns,
      };
    });
  }

  /*
   * The next sequence number is derived from the session the server returned,
   * never hardcoded: a constant would collide with the store's ordering rules
   * on the second turn of any session.
   */
  function nextSequence(sessionDetail) {
    const detail = sessionDetail && typeof sessionDetail === "object" ? sessionDetail : {};
    const turns = Array.isArray(detail.turns) ? detail.turns : [];
    let max = Number.isFinite(Number(detail.max_sequence)) ? Number(detail.max_sequence) : 0;
    turns.forEach(function (turn) {
      const value = Number(turn && turn.sequence_number);
      if (Number.isFinite(value) && value > max) max = value;
    });
    return max + 1;
  }

  /* One idempotency key per intent; retrying the same submit reuses it. */
  function idempotencyKey(projectId, sessionId, turnId) {
    return ["studio", projectId, sessionId, turnId].join(":");
  }

  function newId() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
      return globalThis.crypto.randomUUID();
    }
    return "id-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
  }

  function statusFromError(error) {
    if (!error) return "error";
    const status = Number(error.status);
    if (status === 401 || status === 403) return "unauthorized";
    if (status === 429) return "throttled";
    if (status === 0 || status === 502 || status === 503 || status === 504) return "unavailable";
    if (error.name === "TypeError") return "unavailable";
    return "error";
  }

  function statusMessage(kind) {
    if (kind === "unauthorized") return "برای دیدن استودیو باید وارد شوید.";
    if (kind === "throttled") return "تلاش‌های ناموفق زیاد بود؛ یک دقیقه بعد دوباره تلاش کنید.";
    if (kind === "unavailable") return "پشتیبان استودیو در دسترس نیست؛ بعداً تلاش کنید.";
    if (kind === "ok") return "متصل.";
    return "خطای غیرمنتظره رخ داد.";
  }

  return {
    API: API,
    MARKER: MARKER,
    gateBadge: gateBadge,
    permissionBadge: permissionBadge,
    uncertaintyLabel: uncertaintyLabel,
    turnView: turnView,
    sessionItems: sessionItems,
    nextSequence: nextSequence,
    idempotencyKey: idempotencyKey,
    newId: newId,
    statusFromError: statusFromError,
    statusMessage: statusMessage,
  };
});

if (typeof document !== "undefined") {
  (function wire(api) {
    const $ = (id) => document.getElementById(id);
    const state = { me: null, projectId: "", session: null, turn: null };

    function setStatus(text, tone) {
      const el = $("status");
      el.textContent = text;
      if (tone) el.dataset.tone = tone;
    }

    async function call(path, options) {
      const settings = Object.assign({ method: "GET", credentials: "same-origin" }, options || {});
      const headers = Object.assign({ Accept: "application/json" }, api.MARKER);
      if (settings.body !== undefined) headers["Content-Type"] = "application/json";
      settings.headers = headers;
      const response = await fetch(api.API + path, settings);
      if (!response.ok) {
        const error = new Error("HTTP " + response.status);
        error.status = response.status;
        throw error;
      }
      return response.status === 204 ? null : response.json();
    }

    function showLogin(visible) {
      $("login").hidden = !visible;
      $("workspace").hidden = visible;
      $("logout").hidden = visible;
    }

    function badge(text, tone) {
      const span = document.createElement("span");
      span.className = "badge";
      span.textContent = text;
      if (tone) span.dataset.tone = tone;
      return span;
    }

    function renderTurns(items) {
      const turns = $("turn-list");
      turns.textContent = "";
      $("turns-empty").hidden = items.length > 0;
      items.forEach(function (item) {
        const view = api.turnView(item);
        const li = document.createElement("li");
        const title = document.createElement("p");
        title.className = "turn-title";
        title.textContent = view.title;
        const sub = document.createElement("p");
        sub.className = "turn-sub";
        sub.textContent = view.subtitle;
        const badges = document.createElement("div");
        badges.className = "badges";
        view.badges.forEach(function (entry) {
          badges.appendChild(badge(entry.label, entry.tone));
        });
        li.appendChild(title);
        li.appendChild(sub);
        li.appendChild(badges);
        turns.appendChild(li);
      });
    }

    function renderSessions(items) {
      const list = $("session-list");
      list.textContent = "";
      $("sessions-empty").hidden = items.length > 0;
      items.forEach(function (item) {
        const li = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "link";
        button.textContent = item.title;
        button.addEventListener("click", function () {
          selectSession(item.id);
        });
        const sub = document.createElement("p");
        sub.className = "turn-sub";
        sub.textContent = item.detail;
        li.appendChild(button);
        li.appendChild(sub);
        list.appendChild(li);
      });
    }

    function renderDigests(turn) {
      $("digests").hidden = false;
      $("digest-intent").textContent = turn.intent_digest || "—";
      $("digest-plan").textContent = turn.plan_digest || "—";
      $("digest-preview").textContent = turn.preview_digest || "—";
      $("digest-approval").textContent = "—";
      $("execute-state").textContent = "—";
      $("approval-actions").hidden = !turn.approval_id;
    }

    async function loadMe() {
      try {
        state.me = await call("/me");
        showLogin(false);
        setStatus("متصل به‌عنوان " + state.me.principal_id, "ok");
        return true;
      } catch (error) {
        showLogin(true);
        setStatus(api.statusMessage(api.statusFromError(error)), "warn");
        return false;
      }
    }

    async function loadSessions() {
      const projectId = String(new FormData($("sessions-form")).get("project_id") || "").trim();
      if (!projectId) return;
      state.projectId = projectId;
      try {
        const payload = await call("/sessions?project_id=" + encodeURIComponent(projectId));
        renderSessions(api.sessionItems(payload));
      } catch (error) {
        $("sessions-empty").hidden = false;
        $("sessions-empty").textContent = api.statusMessage(api.statusFromError(error));
      }
    }

    async function openSession() {
      if (!state.projectId) return;
      try {
        const opened = await call(
          "/projects/" + encodeURIComponent(state.projectId) + "/ibpc/sessions",
          { method: "POST", body: JSON.stringify({ privacy_mode: "LOCAL_VAULT" }) },
        );
        await selectSession(opened.session_id);
        await loadSessions();
      } catch (error) {
        $("session-state").textContent = api.statusMessage(api.statusFromError(error));
      }
    }

    async function selectSession(sessionId) {
      try {
        const detail = await call(
          "/projects/" + encodeURIComponent(state.projectId) + "/ibpc/sessions/" +
            encodeURIComponent(sessionId),
        );
        state.session = detail;
        $("session-state").textContent =
          "نشست " + detail.session_id + " · " + detail.status + " · epoch " + detail.session_epoch;
        renderTurns(Array.isArray(detail.turns) ? detail.turns : []);
      } catch (error) {
        $("session-state").textContent = api.statusMessage(api.statusFromError(error));
      }
    }

    async function submitTurn(event) {
      event.preventDefault();
      if (!state.session) {
        $("composer-hint").textContent = "نخست یک نشست باز یا انتخاب کنید.";
        return;
      }
      const data = new FormData($("composer-form"));
      const text = String(data.get("text") || "").trim();
      const assetId = String(data.get("asset_id") || "").trim();
      const sourcePath = String(data.get("source_path") || "").trim();
      if (!text || !assetId || !sourcePath) return;
      const turnId = api.newId();
      const body = {
        turn_id: turnId,
        sequence_number: api.nextSequence(state.session),
        text: text,
        asset_id: assetId,
        source_path: sourcePath,
        idempotency_key: api.idempotencyKey(state.projectId, state.session.session_id, turnId),
        expected_epoch: state.session.session_epoch,
      };
      const button = $("submit-turn");
      button.disabled = true;
      $("composer-hint").textContent = "در حال ارسال…";
      try {
        state.turn = await call(
          "/projects/" + encodeURIComponent(state.projectId) + "/ibpc/sessions/" +
            encodeURIComponent(state.session.session_id) + "/turns",
          { method: "POST", body: JSON.stringify(body) },
        );
        renderDigests(state.turn);
        $("composer-hint").textContent = "در انتظار تأیید.";
      } catch (error) {
        $("composer-hint").textContent = api.statusMessage(api.statusFromError(error));
      } finally {
        button.disabled = false;
      }
    }

    async function decide(action) {
      if (!state.turn || !state.turn.approval_id) return;
      const path =
        "/projects/" + encodeURIComponent(state.projectId) + "/ibpc/sessions/" +
        encodeURIComponent(state.session.session_id) + "/turns/" +
        encodeURIComponent(state.turn.turn_id) + "/" + action;
      try {
        const result = await call(path, {
          method: "POST",
          body: JSON.stringify({ approval_id: state.turn.approval_id }),
        });
        if (action === "approve") {
          $("digest-approval").textContent = result.approval_hash || result.state || "—";
        } else {
          $("execute-state").textContent = (result.status || "—") + " · " + (result.job_id || "—");
        }
      } catch (error) {
        $("composer-hint").textContent = api.statusMessage(api.statusFromError(error));
      }
    }

    async function login(event) {
      event.preventDefault();
      const input = $("principal-token");
      const token = input.value;
      if (!token) return;
      $("login-hint").textContent = "در حال ورود…";
      try {
        await call("/login", { method: "POST", body: JSON.stringify({ principal_token: token }) });
        input.value = "";
        $("login-hint").textContent = "";
        await loadMe();
      } catch (error) {
        $("login-hint").textContent = api.statusMessage(api.statusFromError(error));
      }
    }

    async function logout() {
      try {
        await call("/logout", { method: "POST", body: JSON.stringify({}) });
      } catch (error) {
        /* a failed logout still clears the local view */
      }
      state.me = null;
      state.session = null;
      showLogin(true);
      setStatus("خارج شدید.", "warn");
    }

    $("login-form").addEventListener("submit", login);
    $("logout").addEventListener("click", logout);
    $("sessions-form").addEventListener("submit", function (event) {
      event.preventDefault();
      loadSessions();
    });
    $("open-session").addEventListener("click", openSession);
    $("composer-form").addEventListener("submit", submitTurn);
    $("approve-turn").addEventListener("click", function () {
      decide("approve");
    });
    $("execute-turn").addEventListener("click", function () {
      decide("execute");
    });

    loadMe();
  })(globalThis.StudioSurface);
}
