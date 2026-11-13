import { ApiError, api, isValidJobId } from "./api.js";
import { announce, clear, formatBytes, h, shortJson } from "./dom.js";
import { gapLabel, lineageGapLabel, statusLabel, t } from "./i18n.js";

const TERMINAL = new Set(["completed", "failed_terminal", "failed_retryable"]);
const STEPS = ["pending", "processing", "verifying", "completed"];

// ---- small shared pieces -------------------------------------------------------------
function pill(text, tone) {
  return h("span", { class: "pill pill-" + tone }, text);
}

function section(titleText, ...body) {
  const id = "s-" + Math.random().toString(36).slice(2, 8);
  return h("section", { class: "card", "aria-labelledby": id }, h("h2", { id }, titleText), ...body);
}

function kv(rows) {
  const dl = h("dl", { class: "kv" });
  for (const [label, value, opts] of rows) {
    if (value === null || value === undefined || value === "") continue;
    dl.append(h("dt", {}, label), h("dd", { class: opts && opts.mono ? "mono" : null }, value));
  }
  return dl;
}

function chips(items) {
  return h("ul", { class: "chips" }, items.map((item) => h("li", {}, item)));
}

function copyButton(value) {
  const btn = h("button", { type: "button", class: "btn btn-quiet" }, t("copy"));
  btn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(value);
      btn.textContent = t("copied");
      announce(t("copied"));
    } catch {
      btn.textContent = t("copy");
    }
  });
  return btn;
}

function stateBlock(kind, title, body, action) {
  return h(
    "div",
    { class: "state state-" + kind, role: kind === "error" ? "alert" : "status" },
    h("p", { class: "state-title" }, title),
    body ? h("p", {}, body) : null,
    action || null,
  );
}

function evidenceTree(value, depth = 0) {
  if (value === null || typeof value !== "object") {
    return h("span", { class: "mono" }, shortJson(value, 300));
  }
  const entries = Array.isArray(value) ? value.map((v, i) => [String(i), v]) : Object.entries(value);
  if (depth >= 3) return h("span", { class: "mono" }, shortJson(value, 300));
  const list = h("ul", { class: "tree" });
  for (const [key, item] of entries.slice(0, 50)) {
    const isObject = item !== null && typeof item === "object";
    list.append(
      h(
        "li",
        {},
        h("span", { class: "tree-key mono" }, key),
        isObject ? evidenceTree(item, depth + 1) : h("span", { class: "mono" }, shortJson(item, 300)),
      ),
    );
  }
  if (entries.length > 50) list.append(h("li", {}, "…"));
  return list;
}

// ---- login ---------------------------------------------------------------------------
export function loginView(ctx, { expired = false, unconfigured = false } = {}) {
  const input = h("input", {
    id: "token",
    name: "token",
    type: "password",
    autocomplete: "current-password",
    spellcheck: "false",
    autocapitalize: "off",
    required: true,
    class: "input mono",
    dir: "ltr",
  });
  const error = h("p", { class: "field-error", role: "alert", hidden: true });
  const submit = h("button", { type: "submit", class: "btn btn-primary" }, t("login_submit"));
  const form = h(
    "form",
    { class: "stack", novalidate: true },
    h("label", { for: "token", class: "label" }, t("login_token")),
    input,
    error,
    submit,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = input.value;
    if (!value) return;
    submit.disabled = true;
    submit.textContent = t("login_busy");
    error.hidden = true;
    try {
      await api.login(value);
      input.value = ""; // never keep the secret in the DOM after use
      await ctx.afterLogin();
    } catch (e) {
      input.value = "";
      const code = e instanceof ApiError ? e.code : "network";
      error.textContent =
        code === "unconfigured"
          ? t("login_unconfigured")
          : code === "throttled"
            ? t("login_throttled")
            : code === "network"
              ? t("login_network")
              : t("login_bad");
      error.hidden = false;
      submit.disabled = false;
      submit.textContent = t("login_submit");
      input.focus();
    }
  });
  return h(
    "div",
    { class: "narrow" },
    h("h1", { tabindex: "-1", dataset: { focus: "1" } }, t("login_title")),
    expired ? stateBlock("warn", t("session_expired")) : null,
    unconfigured ? stateBlock("error", t("login_unconfigured")) : null,
    h("p", { class: "lead" }, t("login_lead")),
    form,
  );
}

// ---- recent job ids (per-browser convenience; ids only, never content) ---------------
const RECENT_KEY = "nagar.recent.v1";

function readRecent() {
  try {
    const data = JSON.parse(localStorage.getItem(RECENT_KEY) || "{}");
    return data && typeof data === "object" ? data : {};
  } catch {
    return {};
  }
}

export function rememberJob(projectId, jobId) {
  try {
    const data = readRecent();
    const list = [jobId, ...(Array.isArray(data[projectId]) ? data[projectId] : []).filter((x) => x !== jobId)];
    data[projectId] = list.slice(0, 8);
    localStorage.setItem(RECENT_KEY, JSON.stringify(data));
  } catch {
    /* storage unavailable: the feature simply does not remember */
  }
}

// ---- workspace -----------------------------------------------------------------------
export function workspaceView(ctx) {
  const projects = ctx.overview.projects;
  const root = h("div", {}, h("h1", { tabindex: "-1", dataset: { focus: "1" } }, t("workspace_title")));
  if (projects.length === 0) {
    root.append(stateBlock("empty", t("workspace_empty_title"), t("workspace_empty_body")));
    return root;
  }
  root.append(h("p", { class: "lead" }, t("workspace_lead")), h("p", { class: "note" }, t("no_listing")));
  const recent = readRecent();
  const grid = h("div", { class: "grid" });
  for (const projectId of projects) {
    const input = h("input", {
      id: "job-" + projectId,
      class: "input mono",
      dir: "ltr",
      placeholder: "job-…",
      autocomplete: "off",
      spellcheck: "false",
      "aria-describedby": "hint-" + projectId,
    });
    const error = h("p", { class: "field-error", role: "alert", hidden: true }, t("job_id_invalid"));
    const form = h(
      "form",
      { class: "stack", novalidate: true },
      h("label", { class: "label", for: "job-" + projectId }, t("job_id")),
      input,
      h("p", { class: "note", id: "hint-" + projectId }, t("job_id_hint")),
      error,
      h("button", { type: "submit", class: "btn btn-primary" }, t("open_job")),
    );
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const jobId = input.value.trim();
      if (!isValidJobId(jobId)) {
        error.hidden = false;
        input.focus();
        return;
      }
      ctx.navigate(`#/p/${encodeURIComponent(projectId)}/j/${encodeURIComponent(jobId)}`);
    });
    const ids = Array.isArray(recent[projectId]) ? recent[projectId] : [];
    const recentList = ids.length
      ? h(
          "div",
          {},
          h("h3", { class: "mini" }, t("recent_jobs")),
          h(
            "ul",
            { class: "links" },
            ids.map((jobId) =>
              h(
                "li",
                {},
                h(
                  "a",
                  { class: "mono", dir: "ltr", href: `#/p/${encodeURIComponent(projectId)}/j/${encodeURIComponent(jobId)}` },
                  jobId,
                ),
              ),
            ),
          ),
          h("p", { class: "note" }, t("recent_note")),
        )
      : null;
    grid.append(h("article", { class: "card" }, h("h2", { class: "mono", dir: "ltr" }, projectId), form, recentList));
  }
  root.append(grid);
  return root;
}

// ---- job view ------------------------------------------------------------------------
function statusStepper(status) {
  const failed = status === "failed_terminal" || status === "failed_retryable";
  const index = STEPS.indexOf(status);
  return h(
    "ol",
    { class: "steps", "aria-label": t("section_status") },
    STEPS.map((step, i) => {
      const state = failed ? (i === 0 ? "done" : "idle") : index < 0 ? "idle" : i < index ? "done" : i === index ? "current" : "idle";
      return h("li", { class: "step step-" + state, "aria-current": state === "current" ? "step" : null }, statusLabel(step));
    }),
  );
}

function renderIntent(intent) {
  const entries = Object.entries(intent || {});
  if (entries.length === 0) return h("p", { class: "note" }, t("no_intent"));
  return kv(entries.map(([k, v]) => [k, typeof v === "string" ? v : shortJson(v), { mono: typeof v !== "string" }]));
}

function renderPlan(plan) {
  if (!plan) return h("p", { class: "note" }, t("no_plan"));
  const parts = [];
  if (plan.scenes && plan.scenes.length) {
    parts.push(
      h("h3", { class: "mini" }, t("scenes")),
      h(
        "ol",
        { class: "scenes" },
        plan.scenes.map((scene) =>
          h(
            "li",
            {},
            h("strong", {}, scene.label),
            scene.start_seconds !== null && scene.end_seconds !== null
              ? h("span", { class: "mono", dir: "ltr" }, ` ${scene.start_seconds}s–${scene.end_seconds}s`)
              : null,
            scene.operations && scene.operations.length ? chips(scene.operations) : null,
          ),
        ),
      ),
    );
  }
  if (plan.operations && plan.operations.length) parts.push(h("h3", { class: "mini" }, t("operations")), chips(plan.operations));
  if (plan.constraints && plan.constraints.length) parts.push(h("h3", { class: "mini" }, t("constraints")), chips(plan.constraints));
  if (plan.risks && plan.risks.length) parts.push(h("h3", { class: "mini" }, t("risks")), chips(plan.risks));
  if (plan.verification_points && plan.verification_points.length) {
    parts.push(h("h3", { class: "mini" }, t("verification_points")), chips(plan.verification_points));
  }
  if (plan.estimated_work) parts.push(kv([[t("estimated_work"), plan.estimated_work]]));
  if (plan.contract_gap) parts.push(h("p", { class: "note" }, gapLabel(plan.contract_gap)));
  return parts.length ? h("div", { class: "stack" }, parts) : h("p", { class: "note" }, t("no_plan"));
}

function renderArtifact(artifact) {
  if (!artifact || (!artifact.artifact_id && !artifact.sha256 && artifact.verification_status === "not_available")) {
    return h("p", { class: "note" }, t("no_artifact"));
  }
  const tone = artifact.verification_status === "verified" ? "ok" : artifact.verification_status === "unverified" ? "warn" : "muted";
  const sha = artifact.sha256
    ? h("span", { class: "row-inline" }, h("span", { class: "mono", dir: "ltr" }, artifact.sha256), copyButton(artifact.sha256))
    : null;
  // The server-side file path is deliberately NOT rendered: it is host-internal.
  return h(
    "div",
    { class: "stack" },
    h("p", {}, pill(t("verify_" + artifact.verification_status, artifact.verification_status), tone)),
    kv([
      [t("field_artifact"), artifact.artifact_id, { mono: true }],
      [t("field_kind"), artifact.kind],
      [t("field_size"), artifact.size_bytes === null ? null : h("span", { class: "mono", dir: "ltr" }, formatBytes(artifact.size_bytes))],
      [t("field_sha"), sha],
    ]),
  );
}

function renderLineage(lineage) {
  const links = [
    ["lineage_intent", lineage.intent_id],
    ["lineage_plan", lineage.plan_reference],
    ["lineage_job", lineage.job_id],
    ["lineage_artifact", lineage.artifact_id],
    ["lineage_revision", lineage.revision === null || lineage.revision === undefined ? null : String(lineage.revision)],
  ];
  return h(
    "ol",
    { class: "lineage" },
    links.map(([key, value]) =>
      h(
        "li",
        { class: value ? "has" : "gap" },
        h("span", { class: "lineage-name" }, t(key)),
        value ? h("span", { class: "mono", dir: "ltr" }, value) : h("span", { class: "note" }, t("missing")),
      ),
    ),
  );
}

function jobBody(view) {
  const exec = view.execution || { status: "unknown" };
  const tone = exec.status === "completed" ? "ok" : exec.status.startsWith("failed") ? "bad" : "info";
  const gaps = view.contract_gaps || [];
  return h(
    "div",
    { class: "stack" },
    section(
      t("section_status"),
      h("p", {}, pill(statusLabel(exec.status), tone)),
      statusStepper(exec.status),
      kv([
        [t("field_operation"), exec.operation_id, { mono: true }],
        [t("field_failure"), exec.failure_reason, { mono: true }],
      ]),
    ),
    section(t("section_intent"), renderIntent(view.intent)),
    section(t("section_plan"), renderPlan(view.plan)),
    section(t("section_artifact"), renderArtifact(view.artifact)),
    section(
      t("section_evidence"),
      view.evidence && Object.keys(view.evidence).length
        ? h("details", { open: true }, h("summary", {}, t("section_evidence")), evidenceTree(view.evidence))
        : h("p", { class: "note" }, t("no_evidence")),
    ),
    section(t("section_lineage"), renderLineage(view.lineage)),
    section(
      t("section_gaps"),
      gaps.length
        ? h("div", { class: "stack" }, chips(gaps.map(gapLabel)), h("p", { class: "note" }, t("gaps_note")))
        : h("p", { class: "note" }, t("gaps_none")),
    ),
  );
}

export function jobView(ctx, projectId, jobId) {
  const body = h("div", { class: "stack", "aria-live": "off" }, stateBlock("loading", t("job_loading")));
  const stamp = h("p", { class: "note", dir: "ltr" });
  const root = h(
    "div",
    {},
    h("p", {}, h("a", { class: "back", href: "#/" }, "→ " + t("back"))),
    h("h1", { tabindex: "-1", dataset: { focus: "1" } }, t("job_title")),
    h("p", { class: "mono id-line", dir: "ltr" }, jobId),
    h("p", { class: "note mono", dir: "ltr" }, projectId),
    body,
    stamp,
  );

  let controller = null;
  let timer = null;
  let lastStatus = null;
  let disposed = false;
  let failures = 0;

  const schedule = (ms) => {
    clearTimeout(timer);
    if (!disposed) timer = setTimeout(load, ms);
  };

  async function load() {
    if (disposed) return;
    controller = new AbortController();
    try {
      const view = await api.jobState(projectId, jobId, controller.signal);
      failures = 0;
      rememberJob(projectId, jobId);
      clear(body);
      body.append(jobBody(view));
      stamp.textContent = `${t("refreshed")}: ${new Date().toLocaleTimeString("fa-IR")}`;
      const status = view.execution ? view.execution.status : "unknown";
      if (status !== lastStatus) announce(statusLabel(status));
      lastStatus = status;
      if (!TERMINAL.has(status)) schedule(4000);
    } catch (error) {
      if (error && error.name === "AbortError") return;
      if (error instanceof ApiError && error.status === 401) return ctx.sessionExpired();
      failures += 1;
      clear(body);
      const retry = h("button", { type: "button", class: "btn" }, t("retry"));
      retry.addEventListener("click", () => {
        clear(body);
        body.append(stateBlock("loading", t("job_loading")));
        load();
      });
      if (error instanceof ApiError && error.status === 404) {
        body.append(stateBlock("error", t("job_not_found")));
      } else if (error instanceof ApiError && error.status === 0) {
        body.append(stateBlock("error", t("job_offline"), null, retry));
        schedule(Math.min(30000, 3000 * 2 ** Math.min(failures, 4)));
      } else {
        body.append(stateBlock("error", t("job_error"), null, retry));
      }
    }
  }

  const onVisible = () => {
    if (!document.hidden && lastStatus !== null && !TERMINAL.has(lastStatus)) load();
  };
  document.addEventListener("visibilitychange", onVisible);
  ctx.onDispose(() => {
    disposed = true;
    clearTimeout(timer);
    if (controller) controller.abort();
    document.removeEventListener("visibilitychange", onVisible);
  });
  load();
  return root;
}

// ---- capabilities --------------------------------------------------------------------
export function capabilitiesView(ctx) {
  const tone = { VERIFIED: "ok", VERIFIED_WITH_LIMITATIONS: "warn", HARDENED_BUT_NOT_COMPLETE: "warn", BLOCKED: "bad", DEFERRED: "muted" };
  const list = h("ul", { class: "caps" });
  for (const cap of ctx.overview.capabilities) {
    list.append(
      h(
        "li",
        { class: "card" },
        h("div", { class: "row-between" }, h("h2", {}, t("cap_" + cap.id, cap.id)), pill(t("cap_" + cap.status, cap.status), tone[cap.status] || "muted")),
        kv([
          [t("caps_reason"), cap.reason],
          [t("caps_evidence"), cap.evidence, { mono: true }],
        ]),
      ),
    );
  }
  return h("div", {}, h("h1", { tabindex: "-1", dataset: { focus: "1" } }, t("caps_title")), h("p", { class: "lead" }, t("caps_lead")), list);
}

// ---- settings ------------------------------------------------------------------------
export function settingsView(ctx) {
  const select = h(
    "select",
    { id: "theme", class: "input" },
    ["system", "light", "dark"].map((value) =>
      h("option", { value, selected: ctx.getTheme() === value }, t("theme_" + value)),
    ),
  );
  select.addEventListener("change", () => ctx.setTheme(select.value));
  const out = h("button", { type: "button", class: "btn" }, t("logout"));
  out.addEventListener("click", () => ctx.logout());
  return h(
    "div",
    { class: "stack" },
    h("h1", { tabindex: "-1", dataset: { focus: "1" } }, t("settings_title")),
    section(t("theme"), h("label", { class: "label", for: "theme" }, t("theme")), select),
    section(t("logout"), out, h("p", { class: "note" }, t("logout_note"))),
    section(t("about"), kv([[t("build"), ctx.overview.build, { mono: true }]])),
  );
}
