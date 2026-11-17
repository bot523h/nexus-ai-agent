import { ApiError, PROJECT_PATTERN, api, isValidJobId } from "./api.js";
import { announce, clear, h } from "./dom.js";
import { t } from "./i18n.js";
import { capabilitiesView, jobView, loginView, settingsView, workspaceView } from "./views.js";

const THEME_KEY = "nagar.theme";
const appRoot = document.getElementById("app");
let overview = null;
let disposers = [];
let renderToken = 0;

function getTheme() {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

function applyTheme(value) {
  if (value === "light" || value === "dark") document.documentElement.dataset.theme = value;
  else delete document.documentElement.dataset.theme;
}

function setTheme(value) {
  try {
    if (value === "system") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, value);
  } catch {
    /* storage unavailable: theme applies for this page view only */
  }
  applyTheme(value);
}

const ctx = {
  get overview() {
    return overview;
  },
  navigate(hash) {
    if (location.hash === hash) route();
    else location.hash = hash;
  },
  onDispose(fn) {
    disposers.push(fn);
  },
  getTheme,
  setTheme,
  async afterLogin() {
    await loadOverview();
    ctx.navigate("#/");
  },
  sessionExpired() {
    overview = null;
    showLogin({ expired: true });
  },
  async logout() {
    try {
      await api.logout();
    } catch {
      /* the cookie may already be gone; the local view is cleared regardless */
    }
    overview = null;
    showLogin({});
  },
};

async function loadOverview() {
  overview = await api.overview();
}

function disposeCurrent() {
  for (const fn of disposers.splice(0)) {
    try {
      fn();
    } catch {
      /* a failing cleanup must not block navigation */
    }
  }
}

function navLink(href, label, current) {
  return h("a", { href, class: "nav-link", "aria-current": current ? "page" : null }, label);
}

function shell(content, active) {
  const banner = h("p", { class: "banner", role: "status", hidden: navigator.onLine }, t("offline_banner"));
  const sync = () => (banner.hidden = navigator.onLine);
  window.addEventListener("online", sync);
  window.addEventListener("offline", sync);
  ctx.onDispose(() => {
    window.removeEventListener("online", sync);
    window.removeEventListener("offline", sync);
  });
  return h(
    "div",
    { class: "shell" },
    h(
      "header",
      { class: "top" },
      h("a", { class: "brand", href: "#/" }, h("img", { src: "/studio/icon.svg", alt: "", width: "28", height: "28" }), h("span", {}, t("brand"))),
    ),
    h(
      "nav",
      { class: "nav", "aria-label": "ناوبری اصلی" },
      navLink("#/", t("nav_workspace"), active === "workspace"),
      navLink("#/capabilities", t("nav_capabilities"), active === "capabilities"),
      navLink("#/settings", t("nav_settings"), active === "settings"),
    ),
    h("main", { id: "main", class: "main" }, banner, content),
  );
}

function mount(node) {
  clear(appRoot);
  appRoot.append(node);
  appRoot.removeAttribute("aria-busy");
  const focus = appRoot.querySelector("[data-focus]");
  if (focus) focus.focus({ preventScroll: false });
}

function showLogin(opts) {
  disposeCurrent();
  mount(h("div", { class: "login-wrap" }, h("main", { id: "main", class: "main" }, loginView(ctx, opts))));
  if (location.hash !== "#/login") history.replaceState(null, "", "#/login");
  announce(t("login_title"));
}

function parseRoute() {
  const path = (location.hash || "#/").replace(/^#/, "");
  const parts = path.split("/").filter(Boolean).map((p) => {
    try {
      return decodeURIComponent(p);
    } catch {
      return "";
    }
  });
  return parts;
}

function route() {
  const token = ++renderToken;
  disposeCurrent();
  if (!overview) return bootstrap();
  const parts = parseRoute();
  let view;
  let active = "workspace";
  if (parts[0] === "capabilities") {
    active = "capabilities";
    view = capabilitiesView(ctx);
  } else if (parts[0] === "settings") {
    active = "settings";
    view = settingsView(ctx);
  } else if (parts[0] === "p" && parts[2] === "j" && PROJECT_PATTERN.test(parts[1] || "") && isValidJobId(parts[3])) {
    view = jobView(ctx, parts[1], parts[3]);
  } else {
    view = workspaceView(ctx);
  }
  if (token !== renderToken) return;
  mount(shell(view, active));
}

async function bootstrap() {
  try {
    const session = await api.session();
    if (!session.configured) return showLogin({ unconfigured: true });
    if (!session.authenticated) return showLogin({});
    await loadOverview();
    route();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return showLogin({ expired: true });
    clear(appRoot);
    const retry = h("button", { type: "button", class: "btn btn-primary" }, t("retry"));
    retry.addEventListener("click", () => bootstrap());
    appRoot.append(
      h("main", { id: "main", class: "main" }, h("div", { class: "state state-error", role: "alert" }, h("p", { class: "state-title" }, t("login_network")), retry)),
    );
    appRoot.removeAttribute("aria-busy");
  }
}

applyTheme(getTheme());
window.addEventListener("hashchange", route);
if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("/studio/sw.js", { scope: "/studio/" }).catch(() => {
    /* the app works without the worker; it only adds an offline shell */
  });
}
bootstrap();
