/*
 * NAGAR Studio — reference product client.
 *
 * Pure presentation helpers live in the factory and are exported for Node so
 * the mapping from API payloads to what a human sees is unit-tested without a
 * browser. DOM wiring runs only when a document exists.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.StudioSurface = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

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
    const state = typeof t.state === "string" ? t.state : typeof t.status === "string" ? t.status : "unknown";
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

  function sessionItems(overview) {
    const payload = overview && typeof overview === "object" ? overview : {};
    const projects = Array.isArray(payload.projects) ? payload.projects : [];
    return projects.map(function (project) {
      const id = typeof project === "string" ? project : String((project && project.id) || "");
      const jobs = project && Array.isArray(project.jobs) ? project.jobs.length : 0;
      return { id: id, title: id || "بدون شناسه", detail: jobs + " کار" };
    });
  }

  function statusFromError(error) {
    if (!error) return "error";
    const status = Number(error.status);
    if (status === 401 || status === 403) return "unauthorized";
    if (status === 0 || status === 502 || status === 503 || status === 504) return "unavailable";
    if (error.name === "TypeError") return "unavailable";
    return "error";
  }

  function statusMessage(kind) {
    if (kind === "unauthorized") return "برای دیدن استودیو باید وارد شوید.";
    if (kind === "unavailable") return "پشتیبان استودیو در دسترس نیست؛ بعداً تلاش کنید.";
    if (kind === "ok") return "متصل.";
    return "خطای غیرمنتظره رخ داد.";
  }

  return {
    gateBadge: gateBadge,
    permissionBadge: permissionBadge,
    uncertaintyLabel: uncertaintyLabel,
    turnView: turnView,
    sessionItems: sessionItems,
    statusFromError: statusFromError,
    statusMessage: statusMessage,
  };
});

if (typeof document !== "undefined") {
  (function wire(api) {
    const $ = (id) => document.getElementById(id);
    const status = $("status");
    const list = $("session-list");
    const empty = $("sessions-empty");
    const turns = $("turn-list");
    const turnsEmpty = $("turns-empty");
    const hint = $("composer-hint");
    const form = $("composer-form");
    let session = null;

    function setStatus(text, tone) {
      status.textContent = text;
      if (tone) status.dataset.tone = tone;
    }

    async function call(path, options) {
      const response = await fetch(path, {
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      if (!response.ok) {
        const error = new Error("HTTP " + response.status);
        error.status = response.status;
        throw error;
      }
      return response.status === 204 ? null : response.json();
    }

    function badge(text, tone) {
      const span = document.createElement("span");
      span.className = "badge";
      span.textContent = text;
      if (tone) span.dataset.tone = tone;
      return span;
    }

    function renderTurns(items) {
      turns.textContent = "";
      turnsEmpty.hidden = items.length > 0;
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
        view.badges.forEach(function (entry) { badges.appendChild(badge(entry.label, entry.tone)); });
        li.appendChild(title);
        li.appendChild(sub);
        li.appendChild(badges);
        turns.appendChild(li);
      });
    }

    async function loadOverview() {
      try {
        const overview = await call("/api/studio/overview");
        const items = api.sessionItems(overview);
        list.textContent = "";
        empty.hidden = items.length > 0;
        items.forEach(function (item) {
          const li = document.createElement("li");
          const title = document.createElement("p");
          title.className = "turn-title";
          title.textContent = item.title;
          const sub = document.createElement("p");
          sub.className = "turn-sub";
          sub.textContent = item.detail;
          li.appendChild(title);
          li.appendChild(sub);
          list.appendChild(li);
        });
        setStatus(api.statusMessage("ok"), "ok");
      } catch (error) {
        const kind = api.statusFromError(error);
        setStatus(api.statusMessage(kind), kind === "error" ? "bad" : "warn");
      }
    }

    async function submit(event) {
      event.preventDefault();
      const data = new FormData(form);
      const projectId = String(data.get("project_id") || "").trim();
      const text = String(data.get("text") || "").trim();
      const assetId = String(data.get("asset_id") || "").trim();
      const sourcePath = String(data.get("source_path") || "").trim();
      if (!projectId || !text || !assetId || !sourcePath) return;
      const button = $("submit-turn");
      button.disabled = true;
      hint.textContent = "در حال ارسال…";
      try {
        if (!session || session.project !== projectId) {
          session = await call("/api/studio/projects/" + encodeURIComponent(projectId) + "/ibpc/sessions", {
            method: "POST",
            body: JSON.stringify({}),
          });
          session.project = projectId;
        }
        const turnId = (globalThis.crypto && globalThis.crypto.randomUUID)
          ? globalThis.crypto.randomUUID()
          : "turn-" + Date.now();
        const turn = await call(
          "/api/studio/projects/" + encodeURIComponent(projectId) + "/ibpc/sessions/" +
            encodeURIComponent(session.session_id || session.id) + "/turns",
          {
            method: "POST",
            body: JSON.stringify({
              turn_id: turnId,
              sequence_number: 1,
              text: text,
              asset_id: assetId,
              source_path: sourcePath,
            }),
          },
        );
        renderTurns([turn]);
        hint.textContent = "ارسال شد.";
      } catch (error) {
        const kind = api.statusFromError(error);
        hint.textContent = api.statusMessage(kind);
      } finally {
        button.disabled = false;
      }
    }

    form.addEventListener("submit", submit);
    $("reload").addEventListener("click", loadOverview);
    loadOverview();
  })(globalThis.StudioSurface);
}
