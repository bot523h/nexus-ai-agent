/* NEXUS Cockpit. No framework, CDN, telemetry, or persisted input/credential.
 * All remote text is inserted as text nodes, never HTML. Only theme and known
 * operation IDs may enter localStorage. A schema verdict is NOT execution proof.
 */
(() => {
  "use strict";
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [
    ...root.querySelectorAll(selector),
  ];
  const fa = new Intl.NumberFormat("fa-IR");
  const number = (value) => (Number.isFinite(value) ? fa.format(value) : "—");
  const routes = {
    overview: "نمای کلی",
    capabilities: "کتابخانهٔ قابلیت‌ها",
    lab: "آزمایشگاه فرمان",
    favorites: "نشان‌شده‌ها",
    trust: "اعتماد و شفافیت",
    guide: "راهنمای اتاق فرمان",
  };
  const packCopy = {
    slideshow: {
      title: "روایت و اسلایدشو",
      english: "SLIDESHOW",
      icon: "slideshow",
      description: "چیدمان تصاویر، انتخاب لحن و برنامه‌ریزی یک روایت تصویری.",
    },
    caption: {
      title: "زبان و زیرنویس",
      english: "LANGUAGE & CAPTIONS",
      icon: "caption",
      description: "رونویسی، همگام‌سازی کلمات و قالب‌بندی زیرنویس فارسی.",
    },
    edit: {
      title: "تدوین و تایم‌لاین",
      english: "TIMELINE EDITING",
      icon: "edit",
      description: "برش، تغییر سرعت و تدوین غیرمخرب با تغییرات بازگشت‌پذیر.",
    },
    motion: {
      title: "موشن و جلوه‌ها",
      english: "MOTION & EFFECTS",
      icon: "motion",
      description: "حرکت، ترنزیشن، عنوان و لایه‌های بصری برای روایت شما.",
    },
    audio: {
      title: "صدا و موسیقی",
      english: "AUDIO STUDIO",
      icon: "audio",
      description: "ریتم، بلندی صدا، کاهش نویز و هماهنگی موسیقی با تصویر.",
    },
    delivery: {
      title: "رنگ و خروجی",
      english: "COLOR & DELIVERY",
      icon: "delivery",
      description: "نور و رنگ، خروجی تایم‌لاین و تعریف نسخه‌های تحویل.",
    },
  };
  const operationTitles = {
    "audio.align_music": "هماهنگ‌سازی موسیقی",
    "audio.beat_sync_cut": "برش روی ضرب موسیقی",
    "audio.deess": "کاهش صدای سوت گفتار",
    "audio.detect_beats": "تشخیص ضرب‌های موسیقی",
    "audio.duck_music": "کاهش موسیقی زیر گفتار",
    "audio.eq_voice": "اکولایزر گفتار",
    "audio.normalize_loudness": "یکنواخت‌سازی بلندی صدا",
    "audio.remove_noise": "کاهش نویز صدا",
    "audio.remove_vocal": "جداسازی صدای خواننده",
    "audio.time_stretch": "تغییر طول صدا",
    "caption.align_words": "همگام‌سازی واژه‌ها",
    "caption.burn_in": "درج زیرنویس روی تصویر",
    "caption.diarize": "تفکیک گویندگان",
    "caption.generate_ass_rtl": "ساخت زیرنویس راست‌به‌چپ",
    "caption.generate_srt": "ساخت زیرنویس SRT",
    "caption.highlight_words": "برجسته‌سازی واژه‌ها",
    "caption.search_transcript": "جست‌وجو در متن گفتار",
    "caption.style_vazirmatn": "استایل فارسی وزیرمتن",
    "caption.transcribe": "رونویسی گفتار",
    "caption.translate_local": "ترجمهٔ محلی زیرنویس",
    "color.adjust_exposure": "تنظیم نوردهی",
    "color.apply_lut": "اعمال جدول رنگ",
    "color.auto_balance": "تعادل خودکار رنگ",
    "color.match_shot": "هماهنگی رنگ نماها",
    "delivery.export_otio": "خروجی تایم‌لاین OTIO",
    "delivery.make_proxy_480p": "ساخت نسخهٔ پروکسی",
    "delivery.render_master_4k": "رندر نسخهٔ اصلی 4K",
    "media.pause": "توقف پخش",
    "media.play": "پخش رسانه",
    "motion.add_glow": "افزودن درخشش",
    "motion.add_motion_blur": "محو‌شدگی حرکت",
    "motion.add_parallax": "حرکت پارالاکس",
    "motion.add_particles": "افزودن ذرات",
    "motion.add_title": "افزودن عنوان",
    "motion.add_transition": "افزودن ترنزیشن",
    "motion.apply_mask": "اعمال ماسک",
    "motion.keyframe_transform": "تبدیل با کی‌فریم",
    "motion.stabilize": "تثبیت حرکت تصویر",
    "motion.warp": "تغییر شکل تصویر",
    "slideshow.compose": "چیدن روایت تصویری",
    "slideshow.render": "رندر اسلایدشو",
    "slideshow.scan_assets": "بررسی دارایی‌های تصویری",
    "slideshow.score_images": "امتیازدهی تصاویر",
    "slideshow.suggest_tone": "پیشنهاد لحن روایت",
    "slideshow.upscale": "افزایش اندازهٔ تصویر",
    "system.undo": "بازگرداندن آخرین تغییر",
    "timeline.attach_b_roll": "افزودن نمای مکمل",
    "timeline.freeze_frame": "ثابت‌کردن یک فریم",
    "timeline.insert_gap": "افزودن فاصله در تایم‌لاین",
    "timeline.mark": "نشانه‌گذاری تایم‌لاین",
    "timeline.retime_to_music": "زمان‌بندی با موسیقی",
    "timeline.reverse_segment": "معکوس‌کردن یک بخش",
    "timeline.ripple_delete": "حذف ریپل",
    "timeline.speed_ramp": "تغییر سرعت نما",
    "timeline.split_at_playhead": "برش در نقطهٔ پخش",
    "timeline.sync_multicam": "همگام‌سازی چند دوربین",
    "timeline.trim": "تنظیم ابتدا و انتهای کلیپ",
  };
  const levelCopy = {
    A: "فوری",
    B: "بازگشت‌پذیر",
    C: "نیازمند تأیید",
    D: "غیرمجاز",
  };
  const apiErrors = {
    authentication_required:
      "توکن معتبر نیست یا نشست شما نیاز به ورود دوباره دارد.",
    authentication_not_configured:
      "توکن دسترسی روی سرور تنظیم نشده است. پنل به‌صورت امن بسته است.",
    invalid_json:
      "JSON معتبر نیست. کلید تکراری، عدد نامتناهی و ساختار ناقص پذیرفته نمی‌شوند.",
    invalid_envelope:
      "درخواست باید فقط شامل شناسهٔ عملیات و یک ورودی JSON از نوع شیء باشد.",
    input_too_large: "ورودی بزرگ‌تر از محدودیت ۳۲ کیلوبایت است.",
    input_too_complex: "ساختار ورودی بیش از حد عمیق یا پیچیده است.",
    unknown_operation:
      "این عملیات در رجیستری ثبت نشده است. کاتالوگ را بازخوانی کنید.",
    json_required: "فقط ورودی با قالب JSON پذیرفته می‌شود.",
    input_timeout: "دریافت ورودی بیش از حد طول کشید. دوباره تلاش کنید.",
    validation_busy:
      "ظرفیت بررسی هم‌زمان پر است. چند لحظه بعد دوباره تلاش کنید.",
    connection_failed:
      "ارتباط با سرور برقرار نشد. اتصال را بررسی و دوباره تلاش کنید.",
    request_timeout: "سرور در مهلت مشخص پاسخ نداد. دوباره تلاش کنید.",
    catalog_changed:
      "نسخهٔ کاتالوگ تغییر کرده است. بازخوانی کنید و دوباره ورودی را بررسی کنید.",
    incompatible_catalog: "پاسخ سرور با قرارداد این پنل سازگار نیست.",
  };
  const schemaErrors = {
    missing: "این فیلد الزامی است.",
    extra_forbidden: "فیلد اضافه در این Schema مجاز نیست.",
    value_error:
      "شرط مدل رعایت نشده؛ مقدارها و ارتباط بین فیلدها را بررسی کنید.",
    greater_than: "مقدار باید از حد پایین بیشتر باشد.",
    greater_than_equal: "مقدار از حداقل مجاز کمتر است.",
    less_than: "مقدار باید از حد بالا کمتر باشد.",
    less_than_equal: "مقدار از حداکثر مجاز بیشتر است.",
    string_too_short: "متن کوتاه‌تر از حد مجاز است.",
    string_too_long: "متن طولانی‌تر از حد مجاز است.",
    string_pattern_mismatch: "قالب متن با الگوی مورد انتظار سازگار نیست.",
    literal_error: "یکی از گزینه‌های تعریف‌شده در Schema را انتخاب کنید.",
    int_parsing: "مقدار باید قابل تبدیل به عدد صحیح باشد.",
    int_type: "نوع مقدار باید عدد صحیح باشد.",
    int_from_float: "عدد اعشاری به‌جای عدد صحیح پذیرفته نیست.",
    float_parsing: "مقدار باید یک عدد معتبر باشد.",
    float_type: "نوع مقدار باید عدد باشد.",
    string_type: "نوع مقدار باید متن باشد.",
    bool_parsing: "مقدار باید درست یا نادرست باشد.",
    bool_type: "نوع مقدار باید boolean باشد.",
    list_type: "این فیلد باید آرایه باشد.",
    tuple_type: "این فیلد باید آرایه باشد.",
    dict_type: "این فیلد باید شیء JSON باشد.",
    too_short: "تعداد اعضا کمتر از حداقل مجاز است.",
    too_long: "تعداد اعضا بیش از حد مجاز است.",
    model_type: "ساختار شیء با مدل مورد انتظار سازگار نیست.",
    finite_number: "فقط عدد متناهی پذیرفته می‌شود.",
    enum: "یکی از گزینه‌های Schema را انتخاب کنید.",
  };
  const samples = {
    "timeline.trim": {
      clip_asset_id: "clip_demo",
      in_point_us: 0,
      out_point_us: 5000000,
    },
    "timeline.mark": { at: "اینجا", label: "شروع روایت", color: "green" },
    "timeline.speed_ramp": {
      clip_asset_id: "clip_demo",
      speed_factor: 1.5,
      maintain_pitch: true,
    },
    "timeline.insert_gap": {
      track_id: "video_main",
      at_us: 2000000,
      duration_us: 1000000,
    },
    "media.play": { start: "شروع" },
    "media.pause": {},
    "system.undo": {},
  };
  function readPreference(key, fallback) {
    try {
      return JSON.parse(localStorage.getItem(key)) ?? fallback;
    } catch {
      return fallback;
    }
  }
  function savePreference(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* storage is optional */
    }
  }
  const storedFavorites = readPreference("nexus.cockpit.favorites", []);
  const state = {
    catalog: null,
    session: null,
    token: "",
    requests: new Set(),
    loadEpoch: 0,
    loading: false,
    favorites: new Set(
      Array.isArray(storedFavorites)
        ? storedFavorites
            .filter((x) => typeof x === "string" && x.length < 129)
            .slice(0, 100)
        : [],
    ),
    filters: { query: "", pack: "all", level: "all", page: 1 },
    selectedOp: "timeline.trim",
    draft: null,
    labResult: null,
    labPending: false,
    labRevision: 0,
    paletteIndex: 0,
    paletteItems: [],
    toastTimer: null,
  };
  function element(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    // Long schemas/snippets must remain scrollable with a keyboard, not only a mouse.
    if (tag === "pre") node.tabIndex = 0;
    for (const [name, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (name.startsWith("on") && typeof value === "function")
        node.addEventListener(name.slice(2), value);
      else if (name === "class") node.className = value;
      else if (name === "value") node.value = value;
      else if (name === "disabled" || name === "hidden")
        node[name] = Boolean(value);
      else node.setAttribute(name, value === true ? "" : String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(
        child instanceof Node ? child : document.createTextNode(String(child)),
      );
    }
    return node;
  }
  function icon(name, size = "") {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", `icon ${size}`);
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `/static/icons.svg#${name}`);
    svg.append(use);
    return svg;
  }
  function button(text, action, style = "secondary", iconName) {
    return element(
      "button",
      { class: `button ${style}`, type: "button", onclick: action },
      iconName ? icon(iconName) : null,
      text,
    );
  }
  function link(text, route, style = "secondary", iconName) {
    return element(
      "a",
      { class: `button ${style}`, href: `#${route}` },
      iconName ? icon(iconName) : null,
      text,
    );
  }
  function badge(text, style = "", iconName) {
    return element(
      "span",
      { class: `badge ${style}` },
      iconName ? icon(iconName) : null,
      text,
    );
  }
  function notice(text, style = "") {
    return element(
      "div",
      { class: `notice ${style}` },
      icon(style === "warn" ? "alert" : "info"),
      element("span", {}, text),
    );
  }
  function normalize(text) {
    return String(text)
      .normalize("NFKC")
      .toLowerCase()
      .replace(/[يى]/g, "ی")
      .replace(/ك/g, "ک")
      .replace(/[\u064B-\u065F\u200C\u200E\u200F]/g, "")
      .replace(/\s+/g, " ")
      .trim();
  }
  function route() {
    return Object.hasOwn(routes, location.hash.slice(1))
      ? location.hash.slice(1)
      : "overview";
  }
  function navigate(next) {
    if (route() === next && location.hash === `#${next}`) render();
    else location.hash = next;
  }
  const titleFor = (op) => operationTitles[op.id] || op.id;
  const opById = (id) => state.catalog?.operations.find((op) => op.id === id);
  const packFor = (id) => state.catalog?.packs.find((pack) => pack.id === id);
  function packLabel(id) {
    const pack = packFor(id);
    return (
      packCopy[pack?.directory]?.title ||
      (id === "nagar.core" ? "هستهٔ نگار" : id)
    );
  }
  function packIcon(id) {
    const pack = packFor(id);
    return packCopy[pack?.directory]?.icon || "grid";
  }
  function matches(op, query) {
    const haystack = normalize(
      [
        op.id,
        titleFor(op),
        op.description,
        op.domain,
        packLabel(op.pack_id),
      ].join(" "),
    );
    return normalize(query)
      .split(" ")
      .every((word) => haystack.includes(word));
  }
  function permission(level) {
    return element(
      "span",
      {
        class: `permission level-${level}`,
        title: `سطح ${level}: ${levelCopy[level] || level}`,
      },
      element("span", { class: "permission-letter", dir: "ltr" }, level),
      element(
        "span",
        { class: "permission-description" },
        levelCopy[level] || level,
      ),
    );
  }
  function toast(message) {
    clearTimeout(state.toastTimer);
    const box = $("#toast");
    box.textContent = message;
    box.hidden = false;
    state.toastTimer = setTimeout(() => {
      box.hidden = true;
    }, 4200);
  }
  function errorText(code) {
    return apiErrors[code] || "درخواست انجام نشد. دوباره تلاش کنید.";
  }
  function setTheme(theme) {
    const selected = theme === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = selected;
    $("#theme-icon").setAttribute(
      "href",
      `/static/icons.svg#${selected === "dark" ? "sun" : "moon"}`,
    );
    $("#theme-toggle").setAttribute(
      "aria-label",
      selected === "dark" ? "فعال‌کردن پوستهٔ روشن" : "فعال‌کردن پوستهٔ تیره",
    );
    savePreference("nexus.cockpit.theme", selected);
  }
  setTheme(readPreference("nexus.cockpit.theme", "dark"));
  async function api(path, init = {}) {
    const controller = new AbortController();
    state.requests.add(controller);
    let timedOut = false;
    const timeout = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 15000);
    const headers = new Headers(init.headers || {});
    if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
    try {
      const response = await fetch(path, {
        ...init,
        headers,
        signal: controller.signal,
        credentials: "omit",
        cache: "no-store",
      });
      const payload = await response.json();
      if (!response.ok)
        throw Object.assign(new Error("Request refused"), {
          code: payload.detail?.code || "request_failed",
          status: response.status,
        });
      return payload;
    } catch (error) {
      if (error.code) throw error;
      throw Object.assign(new Error("Request unavailable"), {
        code: timedOut
          ? "request_timeout"
          : controller.signal.aborted
            ? "aborted"
            : "connection_failed",
      });
    } finally {
      clearTimeout(timeout);
      state.requests.delete(controller);
    }
  }
  function showConnection(message) {
    $("#connection-message").textContent = message;
    $("#connection-banner").hidden = false;
  }
  function updateChrome() {
    $$('[data-count="operations"]').forEach((n) => {
      n.textContent = number(state.catalog?.summary.operations);
    });
    $$('[data-count="favorites"]').forEach((n) => {
      n.textContent = number(state.favorites.size);
    });
    $$("[data-version]").forEach((n) => {
      n.textContent = state.catalog?.version
        ? `v${state.catalog.version}`
        : "SOURCE";
    });
    const current = route();
    $("#breadcrumb-current").textContent = routes[current];
    document.title = `${routes[current]} — NEXUS`;
    $$("[data-nav]").forEach((n) => {
      if (n.dataset.nav === current) n.setAttribute("aria-current", "page");
      else n.removeAttribute("aria-current");
    });
    $("#mode-label").textContent =
      state.session?.mode === "public_preview"
        ? "پیش‌نمایش عمومی"
        : "کاتالوگ محافظت‌شده";
    $("#lock-session").hidden = !state.token;
    $("#refresh").disabled = state.loading;
    $("#refresh").classList.toggle("spin", state.loading);
    if (state.catalog) {
      const time = new Intl.DateTimeFormat("fa-IR", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }).format(new Date(state.catalog.observed_at));
      $("#snapshot-time").textContent = `آخرین خواندن کاتالوگ · ${time}`;
    } else $("#snapshot-time").textContent = "NEXUS COCKPIT";
  }
  function clearSession(message = "") {
    state.loadEpoch++;
    state.labRevision++;
    state.requests.forEach((request) => request.abort());
    state.requests.clear();
    state.token = "";
    state.catalog = null;
    state.draft = null;
    state.labResult = null;
    state.labPending = false;
    state.loading = false;
    ["inspector", "command-palette"].forEach((id) => {
      const dialog = $(`#${id}`);
      if (dialog.open) dialog.close();
    });
    $("#inspector-content").replaceChildren();
    $("#palette-results").replaceChildren();
    state.paletteItems = [];
    $("#connection-banner").hidden = true;
    renderAuth(message);
    updateChrome();
  }
  async function loadCatalog() {
    if (state.loading) return false;
    const epoch = ++state.loadEpoch;
    state.loading = true;
    updateChrome();
    try {
      const data = await api("/api/cockpit/catalog");
      if (epoch !== state.loadEpoch) return false;
      if (
        data.schema_version !== "nexus.cockpit.v1" ||
        data.execution_enabled !== false ||
        data.bot_connected !== false ||
        !Array.isArray(data.operations) ||
        !Array.isArray(data.packs)
      ) {
        throw Object.assign(new Error("Incompatible catalog"), {
          code: "incompatible_catalog",
        });
      }
      if (state.catalog && state.catalog.fingerprint !== data.fingerprint)
        invalidateLab();
      state.catalog = data;
      state.favorites = new Set(
        [...state.favorites].filter((id) => opById(id)),
      );
      savePreference("nexus.cockpit.favorites", [...state.favorites]);
      if (!opById(state.selectedOp)) {
        state.selectedOp = data.operations[0]?.id;
        state.draft = null;
      }
      $("#connection-banner").hidden = true;
      render();
      return true;
    } catch (error) {
      if (epoch !== state.loadEpoch || error.code === "aborted") return false;
      if (
        error.status === 401 ||
        (error.status === 503 && error.code === "authentication_not_configured")
      ) {
        clearSession(errorText(error.code));
      } else if (state.catalog) {
        showConnection(
          `${errorText(error.code)} نسخهٔ قبلی کاتالوگ نمایش داده می‌شود.`,
        );
      } else {
        renderUnavailable(errorText(error.code));
      }
      return false;
    } finally {
      if (epoch === state.loadEpoch) {
        state.loading = false;
        updateChrome();
      }
    }
  }
  async function boot() {
    try {
      state.session = await api("/api/cockpit/session");
      if (state.session.requires_token) renderAuth();
      else await loadCatalog();
      updateChrome();
    } catch (error) {
      if (error.code !== "aborted") renderUnavailable(errorText(error.code));
    }
  }
  function renderUnavailable(message) {
    $("#main").setAttribute("aria-busy", "false");
    $("#main").replaceChildren(
      element(
        "section",
        { class: "loading-state" },
        icon("server", "large"),
        element("h1", {}, "کاتالوگ در دسترس نیست"),
        element("p", {}, message),
        button(
          "تلاش دوباره",
          () => (state.session ? loadCatalog() : boot()),
          "primary",
          "refresh",
        ),
      ),
    );
  }
  function renderAuth(message = "") {
    $("#main").setAttribute("aria-busy", "false");
    const configured = state.session?.configured !== false;
    const section = element(
      "section",
      { class: "auth-panel" },
      icon("lock"),
      element(
        "p",
        { class: "eyebrow", dir: "ltr" },
        "NEXUS / PROTECTED WORKSPACE",
      ),
      element(
        "h1",
        {},
        configured ? "ورود به اتاق فرمان" : "دسترسی، به‌صورت امن بسته است",
      ),
      element(
        "p",
        {},
        configured
          ? "توکن مخصوص پنل را وارد کنید. این محیط فقط برای کاوش کاتالوگ و بررسی ورودی فرمان‌هاست."
          : "متغیر NEXUS_COCKPIT_TOKEN روی سرور تنظیم نشده است. تا پیکربندی مدیر، API در دسترس نیست.",
      ),
    );
    if (configured) {
      const tokenInput = element("input", {
        id: "access-token",
        type: "password",
        autocomplete: "off",
        spellcheck: "false",
        maxlength: 1024,
        required: true,
        placeholder: "NEXUS_COCKPIT_TOKEN",
        "aria-describedby": "auth-note",
      });
      const submit = element(
        "button",
        { type: "submit", class: "button primary full" },
        icon("arrow-left"),
        "باز کردن پنل",
      );
      const errorBox = element(
        "p",
        { class: "auth-error", id: "auth-error", role: "alert" },
        message,
      );
      const form = element(
        "form",
        {
          onsubmit: async (event) => {
            event.preventDefault();
            if (!tokenInput.value || state.loading) return;
            state.token = tokenInput.value;
            tokenInput.value = "";
            submit.disabled = true;
            errorBox.textContent = "";
            const loginEpoch = state.loadEpoch + 1;
            const loaded = await loadCatalog();
            if (!loaded && !state.catalog && state.loadEpoch === loginEpoch) {
              state.token = "";
              renderAuth("ورود انجام نشد؛ توکن و اتصال سرور را بررسی کنید.");
            }
          },
        },
        element(
          "label",
          { class: "field-label", for: "access-token" },
          "توکن دسترسی",
        ),
        tokenInput,
        submit,
        errorBox,
      );
      section.append(
        form,
        element(
          "p",
          { id: "auth-note", class: "auth-note" },
          "توکن فقط در حافظهٔ این تب می‌ماند؛ با قفل‌کردن یا بازخوانی صفحه پاک می‌شود.",
        ),
      );
    } else {
      section.append(
        notice(
          "توکن را در محیط امن سرور تنظیم کنید؛ آن را در URL، کد یا گفت‌وگو وارد نکنید.",
          "warn",
        ),
        element("pre", {}, "python -m nexus_ai_agent.cockpit --port 3000"),
        button("بررسی دوبارهٔ اتصال", () => boot(), "secondary", "refresh"),
      );
    }
    $("#main").replaceChildren(section);
  }
  function heading(title, subtitle, eyebrow, actions = []) {
    return element(
      "div",
      { class: "page-heading" },
      element(
        "div",
        { class: "heading-copy" },
        eyebrow
          ? element("div", { class: "eyebrow", dir: "ltr" }, eyebrow)
          : null,
        element("h1", {}, title),
        element("p", {}, subtitle),
      ),
      actions.length
        ? element("div", { class: "page-heading-actions" }, actions)
        : null,
    );
  }
  function downloadJSON(value, filename) {
    const blob = new Blob([JSON.stringify(value, null, 2) + "\n"], {
      type: "application/json;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const anchor = element("a", { href: url, download: filename });
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function exportCatalog() {
    if (!state.catalog) return;
    downloadJSON(state.catalog, "nexus-catalog.json");
    toast("گزارش کاتالوگ برای دریافت آماده شد؛ نه گزارش سلامت محیط اجرا.");
  }
  async function copyText(text) {
    try {
      if (!navigator.clipboard?.writeText)
        throw new Error("Clipboard not available");
      await navigator.clipboard.writeText(text);
      toast("در حافظهٔ موقت کپی شد.");
    } catch {
      toast("مرورگر اجازهٔ کپی نداد. متن را انتخاب و به‌صورت دستی کپی کنید.");
    }
  }
  function openCatalog(pack = "all", level = "all") {
    state.filters = { query: "", pack, level, page: 1 };
    navigate("capabilities");
  }
  function renderOverview() {
    const summary = state.catalog.summary;
    const view = element(
      "div",
      { class: "view-enter" },
      heading(
        "اتاق فرمان",
        "نمایی روشن از آنچه نکسوس واقعاً در اختیار شما می‌گذارد.",
        "YOUR CREATIVE COMMAND CENTER",
        [button("دریافت گزارش", exportCatalog, "secondary", "download")],
      ),
    );
    const hero = element(
      "section",
      { class: "hero", "aria-labelledby": "hero-title" },
      element(
        "div",
        { class: "hero-copy" },
        element(
          "div",
          { class: "hero-tag" },
          icon("sparkles"),
          "یک فضای آرام برای ایده‌های بزرگ",
        ),
        element(
          "h2",
          { id: "hero-title" },
          "همهٔ توان نکسوس،",
          element("br"),
          element("span", {}, "یک‌جا، پیش روی شما."),
        ),
        element(
          "p",
          {},
          "قابلیت‌ها را کشف کنید، ساختار فرمان‌ها را بشناسید و پیش از اجرا، ورودی‌ها را با دقت بررسی کنید. بدون حدس. بدون اثر جانبی.",
        ),
        element(
          "div",
          { class: "hero-actions" },
          button("کاوش قابلیت‌ها", () => openCatalog(), "primary", "layers"),
          link("ورود به آزمایشگاه", "lab", "subtle", "arrow-left"),
        ),
      ),
      element("img", {
        class: "hero-art",
        src: "/static/orbit.svg",
        alt: "",
        width: 450,
        height: 300,
      }),
    );
    const stats = [
      [
        "عملیات ثبت‌شده",
        summary.operations,
        "terminal",
        "از رجیستری واقعی",
        () => openCatalog(),
      ],
      [
        "پک‌های داخلی",
        summary.packs,
        "layers",
        "بدون فعال‌سازی",
        () => $("#packs-section").scrollIntoView({ block: "start" }),
      ],
      [
        "حوزهٔ تخصصی",
        summary.domains,
        "grid",
        "در کاتالوگ فعلی",
        () => openCatalog(),
      ],
      [
        "نیازمند تأیید",
        summary.confirmation_required,
        "shield",
        "عملیات سطح C",
        () => openCatalog("all", "C"),
      ],
    ];
    view.append(
      hero,
      element(
        "div",
        { class: "stats-grid" },
        stats.map(([label, value, symbol, note, action]) =>
          element(
            "button",
            {
              class: "stat-card",
              type: "button",
              onclick: action,
              "aria-label": `${label}: ${number(value)}`,
            },
            element(
              "div",
              { class: "stat-top" },
              element("span", {}, label),
              element("span", { class: "stat-icon" }, icon(symbol)),
            ),
            element(
              "div",
              { class: "stat-bottom" },
              element("strong", { class: "stat-value" }, number(value)),
              element(
                "span",
                { class: "stat-note" },
                label === "عملیات ثبت‌شده" ? icon("check") : null,
                note,
              ),
            ),
          ),
        ),
      ),
    );
    const packs = element(
      "section",
      { id: "packs-section" },
      element(
        "div",
        { class: "section-heading" },
        element(
          "div",
          {},
          element("h2", {}, "استودیوی نگار"),
          element(
            "p",
            {},
            `${number(summary.packs)} مسیر خلاقیت؛ از اولین تصویر تا آخرین جزئیات.`,
          ),
        ),
        element(
          "a",
          {
            href: "#capabilities",
            onclick: () => {
              state.filters = { query: "", pack: "all", level: "all", page: 1 };
            },
          },
          "همهٔ عملیات",
          icon("arrow-left"),
        ),
      ),
    );
    packs.append(
      element(
        "div",
        { class: "packs-grid" },
        state.catalog.packs.map((pack) => {
          const info = packCopy[pack.directory] || {
            title: pack.display_name,
            english: pack.directory,
            icon: "layers",
            description: pack.display_name,
          };
          return element(
            "button",
            {
              class: `pack-card pack-${pack.directory}`,
              type: "button",
              onclick: () => openCatalog(pack.id),
              "aria-label": `${info.title}؛ ${number(pack.operations.length)} عملیات`,
            },
            element(
              "div",
              { class: "pack-head" },
              element("span", { class: "pack-icon" }, icon(info.icon)),
              icon("arrow-up-left", "pack-arrow"),
            ),
            element("h3", {}, info.title),
            element(
              "span",
              { class: "pack-subtitle", dir: "ltr" },
              info.english,
            ),
            element("p", { class: "pack-description" }, info.description),
            element(
              "div",
              { class: "pack-footer" },
              element(
                "span",
                { class: "pack-counter" },
                element("b", {}, number(pack.operations.length)),
                "عملیات",
              ),
              element(
                "span",
                { class: "registered-label" },
                element("span", { class: "status-dot" }),
                pack.pending_operations.length
                  ? `${number(pack.pending_operations.length)} تعریف ناقص`
                  : "تعاریف ثبت‌شده",
              ),
            ),
          );
        }),
      ),
    );
    view.append(
      packs,
      element(
        "div",
        { class: "overview-bottom" },
        element(
          "section",
          { class: "lab-callout" },
          icon("terminal"),
          element(
            "div",
            {},
            element("h3", {}, "اولین فرمانتان را زیر ذره‌بین ببرید."),
            element(
              "p",
              {},
              "یک عملیات انتخاب کنید، ورودی JSON را تغییر دهید و بازخورد واقعی Schema را ببینید. هیچ فایلی تغییر نمی‌کند.",
            ),
            button(
              "امتحان با یک برش ساده",
              () => openLab("timeline.trim"),
              "link",
              "arrow-left",
            ),
          ),
        ),
        element(
          "section",
          { class: "truth-callout" },
          element(
            "div",
            { class: "truth-callout-header" },
            icon("shield-check"),
            "شفافیت، نه چراغ سبز نمایشی",
          ),
          element(
            "p",
            {},
            "این پنل به ربات یا صف تولید متصل نیست. ثبت یک قابلیت، اثبات اجرای موفق یا اعتبار امضای آن نیست.",
          ),
          element(
            "div",
            { class: "scope-tags" },
            element(
              "a",
              { class: "scope-tag", href: "#trust" },
              icon("info"),
              "مرزهای اعتماد",
            ),
            element(
              "span",
              { class: "scope-tag" },
              icon("server"),
              "کاتالوگ محلی",
            ),
            element("span", { class: "scope-tag" }, icon("lock"), "بدون اجرا"),
          ),
        ),
      ),
    );
    return view;
  }
  function favoriteButton(id) {
    const active = state.favorites.has(id);
    return element(
      "button",
      {
        class: `icon-button${active ? " active" : ""}`,
        type: "button",
        "data-favorite-id": id,
        "aria-label": `${active ? "برداشتن نشان" : "نشان‌کردن"} ${titleFor(opById(id))}`,
        "aria-pressed": String(active),
        title: active ? "برداشتن نشان" : "نشان‌کردن",
        onclick: () => toggleFavorite(id),
      },
      icon("star"),
    );
  }
  function toggleFavorite(id) {
    if (!opById(id)) return;
    if (state.favorites.has(id)) state.favorites.delete(id);
    else state.favorites.add(id);
    savePreference("nexus.cockpit.favorites", [...state.favorites]);
    updateChrome();
    $$("[data-favorite-id]")
      .filter((n) => n.dataset.favoriteId === id)
      .forEach((n) => {
        const active = state.favorites.has(id);
        n.classList.toggle("active", active);
        n.setAttribute("aria-pressed", String(active));
        n.setAttribute(
          "aria-label",
          `${active ? "برداشتن نشان" : "نشان‌کردن"} ${titleFor(opById(id))}`,
        );
        n.title = active ? "برداشتن نشان" : "نشان‌کردن";
      });
    if (route() === "favorites") renderCatalogResults(true);
    toast(
      state.favorites.has(id)
        ? "به نشان‌شده‌ها اضافه شد."
        : "نشان این قابلیت برداشته شد.",
    );
  }
  function renderCatalog(favoritesOnly = false) {
    const view = element(
      "div",
      { class: "view-enter" },
      heading(
        favoritesOnly ? "قابلیت‌های نشان‌شده" : "کتابخانهٔ قابلیت‌ها",
        favoritesOnly
          ? "میان‌بُرهای شما؛ فقط شناسهٔ عملیات در همین مرورگر نگهداری می‌شود."
          : "قرارداد هر عملیات را ببینید؛ از ورودی و نسخه تا سطح دسترسی.",
        favoritesOnly ? "YOUR PERSONAL SHORTLIST" : "EXPLORE THE POSSIBILITIES",
        [
          favoritesOnly
            ? link("همهٔ قابلیت‌ها", "capabilities", "secondary", "layers")
            : link("نشان‌شده‌ها", "favorites", "secondary", "star"),
        ],
      ),
    );
    const search = element("input", {
      type: "search",
      id: "catalog-search",
      value: state.filters.query,
      placeholder: "جست‌وجو در نام، شناسه یا شرح عملیات…",
      "aria-label": "جست‌وجو در قابلیت‌ها",
      autocomplete: "off",
      oninput: (event) => {
        state.filters.query = event.target.value;
        state.filters.page = 1;
        renderCatalogResults(favoritesOnly);
      },
    });
    const select = element(
      "select",
      {
        class: "select-field",
        id: "pack-filter",
        "aria-label": "فیلتر بسته",
        onchange: (event) => {
          state.filters.pack = event.target.value;
          state.filters.page = 1;
          renderCatalogResults(favoritesOnly);
        },
      },
      element("option", { value: "all" }, "همهٔ بسته‌ها"),
      state.catalog.packs.map((pack) =>
        element("option", { value: pack.id }, packLabel(pack.id)),
      ),
      element("option", { value: "nagar.core" }, "هستهٔ نگار"),
    );
    select.value = state.filters.pack;
    view.append(
      element(
        "div",
        { class: "catalog-toolbar" },
        element(
          "div",
          { class: "search-field" },
          icon("search"),
          search,
          element("kbd", { dir: "ltr" }, "/"),
        ),
        select,
      ),
    );
    const chips = element(
      "div",
      { class: "filter-chips", "aria-label": "فیلتر سطح دسترسی" },
      ["all", "A", "B", "C", "D"].map((level) =>
        element(
          "button",
          {
            class: "filter-chip",
            type: "button",
            "data-level": level,
            "aria-pressed": String(state.filters.level === level),
            onclick: () => {
              state.filters.level = level;
              state.filters.page = 1;
              renderCatalogResults(favoritesOnly);
            },
          },
          level === "all" ? "همه" : `${level} · ${levelCopy[level]}`,
          element("span", { "data-level-count": level }),
        ),
      ),
    );
    view.append(
      element(
        "div",
        { class: "filter-bar" },
        chips,
        element("span", {
          id: "results-summary",
          class: "results-summary",
          role: "status",
        }),
      ),
      element("section", {
        class: "panel",
        id: "catalog-results",
        "aria-label": "عملیات ثبت‌شده",
      }),
      notice(
        "قابلیت ثبت‌شده یعنی تعریف آن در رجیستری وجود دارد؛ نه اینکه وابستگی‌ها، مجوزها یا خروجی آن در این محیط تأیید شده‌اند.",
        "bottom-note",
      ),
    );
    return view;
  }
  function renderCatalogResults(favoritesOnly = route() === "favorites") {
    const container = $("#catalog-results");
    if (!container || !state.catalog) return;
    const { query, pack, level } = state.filters;
    const base = state.catalog.operations.filter(
      (op) =>
        (!favoritesOnly || state.favorites.has(op.id)) &&
        (pack === "all" || op.pack_id === pack) &&
        matches(op, query),
    );
    const filtered = base.filter(
      (op) => level === "all" || op.permission_level === level,
    );
    $$("[data-level]").forEach((n) =>
      n.setAttribute("aria-pressed", String(n.dataset.level === level)),
    );
    $$("[data-level-count]").forEach((n) => {
      n.textContent = number(
        n.dataset.levelCount === "all"
          ? base.length
          : base.filter((op) => op.permission_level === n.dataset.levelCount)
              .length,
      );
    });
    $("#results-summary").textContent =
      `${number(filtered.length)} عملیات پیدا شد`;
    const totalPages = Math.max(1, Math.ceil(filtered.length / 10));
    state.filters.page = Math.min(state.filters.page, totalPages);
    if (!filtered.length) {
      container.replaceChildren(
        element(
          "div",
          { class: "empty-state" },
          icon(favoritesOnly ? "star" : "search"),
          element(
            "h2",
            {},
            favoritesOnly && !state.favorites.size
              ? "اینجا جای قابلیت‌های محبوب شماست."
              : "چیزی با این فیلترها پیدا نشد.",
          ),
          element(
            "p",
            {},
            favoritesOnly && !state.favorites.size
              ? "از کتابخانه شروع کنید و ستارهٔ کنار هر عملیات را بزنید."
              : "عبارت کوتاه‌تری بنویسید یا فیلترها را پاک کنید.",
          ),
          favoritesOnly && !state.favorites.size
            ? link("کاوش کتابخانه", "capabilities", "primary", "layers")
            : button(
                "پاک‌کردن فیلترها",
                () => {
                  state.filters = {
                    query: "",
                    pack: "all",
                    level: "all",
                    page: 1,
                  };
                  render();
                },
                "secondary",
                "refresh",
              ),
        ),
      );
      return;
    }
    const table = element(
      "table",
      { class: "operations-table" },
      element(
        "thead",
        {},
        element(
          "tr",
          {},
          element("th", { scope: "col" }, "نام و شناسهٔ عملیات"),
          element("th", { scope: "col", class: "table-pack" }, "بسته"),
          element(
            "th",
            { scope: "col", class: "table-permission" },
            "سطح دسترسی",
          ),
          element(
            "th",
            { scope: "col", class: "table-action-heading" },
            "کاوش",
          ),
        ),
      ),
    );
    const start = (state.filters.page - 1) * 10;
    table.append(
      element(
        "tbody",
        {},
        filtered.slice(start, start + 10).map((op) =>
          element(
            "tr",
            { "data-operation-row": op.id },
            element(
              "td",
              {},
              element(
                "div",
                { class: "op-name-cell" },
                element(
                  "span",
                  { class: "op-symbol" },
                  icon(packIcon(op.pack_id)),
                ),
                element(
                  "button",
                  {
                    type: "button",
                    class: "operation-link",
                    onclick: () => openInspector(op.id),
                    "aria-haspopup": "dialog",
                  },
                  titleFor(op),
                  element("code", { dir: "ltr" }, op.id),
                ),
              ),
            ),
            element("td", { class: "table-pack muted" }, packLabel(op.pack_id)),
            element("td", {}, permission(op.permission_level)),
            element(
              "td",
              {},
              element(
                "div",
                { class: "row-actions" },
                favoriteButton(op.id),
                element(
                  "button",
                  {
                    class: "icon-button",
                    type: "button",
                    "aria-label": `بررسی ${op.id}`,
                    title: "باز کردن جزئیات",
                    onclick: () => openInspector(op.id),
                  },
                  icon("arrow-up-left"),
                ),
              ),
            ),
          ),
        ),
      ),
    );
    const previous = element(
      "button",
      {
        class: "icon-button",
        type: "button",
        disabled: state.filters.page <= 1,
        "aria-label": "صفحهٔ قبل",
        onclick: () => {
          state.filters.page--;
          renderCatalogResults(favoritesOnly);
        },
      },
      icon("arrow-right"),
    );
    const next = element(
      "button",
      {
        class: "icon-button",
        type: "button",
        disabled: state.filters.page >= totalPages,
        "aria-label": "صفحهٔ بعد",
        onclick: () => {
          state.filters.page++;
          renderCatalogResults(favoritesOnly);
        },
      },
      icon("arrow-left"),
    );
    container.replaceChildren(
      table,
      element(
        "div",
        { class: "pagination" },
        element(
          "span",
          {},
          `${number(start + 1)}–${number(Math.min(start + 10, filtered.length))} از ${number(filtered.length)} عملیات`,
        ),
        element(
          "div",
          { class: "pagination-controls" },
          previous,
          element(
            "span",
            {},
            `${number(state.filters.page)} / ${number(totalPages)}`,
          ),
          next,
        ),
      ),
    );
  }
  function resolveSchema(node, root) {
    if (node?.$ref?.startsWith("#/$defs/"))
      return root.$defs?.[node.$ref.slice(8)] || node;
    return node || {};
  }
  function sampleFromSchema(node, root, depth = 0) {
    node = resolveSchema(node, root);
    if (depth > 6) return null;
    if (Object.hasOwn(node, "default")) return node.default;
    if (Object.hasOwn(node, "const")) return node.const;
    if (node.enum?.length) return node.enum[0];
    if (node.anyOf || node.oneOf)
      return sampleFromSchema(
        (node.anyOf || node.oneOf).find((n) => n.type !== "null") || {},
        root,
        depth + 1,
      );
    if (node.type === "object" || node.properties)
      return Object.fromEntries(
        Object.entries(node.properties || {})
          .filter(
            ([key, field]) =>
              (node.required || []).includes(key) ||
              Object.hasOwn(field, "default"),
          )
          .map(([key, field]) => [
            key,
            sampleFromSchema(field, root, depth + 1),
          ]),
      );
    if (node.type === "array")
      return Array.from({ length: Math.min(node.minItems || 1, 3) }, () =>
        sampleFromSchema(node.items || {}, root, depth + 1),
      );
    if (node.type === "boolean") return true;
    if (node.type === "number" || node.type === "integer")
      return (
        node.minimum ??
        (node.exclusiveMinimum !== undefined
          ? node.exclusiveMinimum + 1
          : Math.min(node.maximum ?? 1, 1))
      );
    if (node.type === "null") return null;
    return "demo";
  }
  function exampleFor(op) {
    return JSON.stringify(
      samples[op.id] ?? sampleFromSchema(op.input_schema, op.input_schema),
      null,
      2,
    );
  }
  function invalidateLab() {
    state.labRevision++;
    state.labResult = null;
    state.labPending = false;
  }
  function openLab(id) {
    const op = opById(id);
    if (!op) return;
    state.selectedOp = id;
    state.draft = exampleFor(op);
    invalidateLab();
    const dialog = $("#inspector");
    if (dialog.open) dialog.close();
    navigate("lab");
  }
  function fieldType(node, root) {
    if (node.$ref) return resolveSchema(node, root).title || "object";
    if (node.anyOf || node.oneOf)
      return (node.anyOf || node.oneOf)
        .map((n) => fieldType(n, root))
        .join(" | ");
    return node.type || (node.enum ? "enum" : "value");
  }
  function fieldList(op, limit = Infinity) {
    const schema = op.input_schema;
    const fields = Object.entries(schema.properties || {});
    if (!fields.length)
      return element(
        "p",
        { class: "schema-empty" },
        "این عملیات ورودی الزامی ندارد. شیء خالی {} را امتحان کنید.",
      );
    return element(
      "div",
      { class: "schema-fields" },
      fields.slice(0, limit).map(([name, raw]) => {
        const node = resolveSchema(raw, schema);
        const required = (schema.required || []).includes(name);
        const hints = [];
        if (node.minimum !== undefined)
          hints.push(`حداقل ${number(node.minimum)}`);
        if (node.exclusiveMinimum !== undefined)
          hints.push(`بیشتر از ${number(node.exclusiveMinimum)}`);
        if (node.maximum !== undefined)
          hints.push(`حداکثر ${number(node.maximum)}`);
        if (node.minLength) hints.push(`حداقل ${number(node.minLength)} نویسه`);
        if (node.maxLength)
          hints.push(`حداکثر ${number(node.maxLength)} نویسه`);
        if (node.enum) hints.push(node.enum.join(" / "));
        if (Object.hasOwn(node, "default"))
          hints.push(`پیش‌فرض: ${JSON.stringify(node.default).slice(0, 65)}`);
        return element(
          "div",
          { class: "schema-field" },
          element(
            "div",
            { class: "schema-field-heading" },
            element("code", { dir: "ltr" }, name),
            element(
              "span",
              { class: required ? "required-mark" : "optional-mark" },
              required ? "الزامی" : "اختیاری",
            ),
          ),
          element(
            "div",
            { class: "schema-field-info" },
            element(
              "span",
              { class: "type-label", dir: "ltr" },
              fieldType(raw, schema),
            ),
            hints.map((hint) => element("span", {}, hint)),
          ),
        );
      }),
    );
  }
  function renderLab() {
    const op = opById(state.selectedOp);
    if (!op)
      return element(
        "section",
        { class: "empty-state" },
        icon("terminal"),
        element("h1", {}, "هیچ عملیاتی ثبت نشده است."),
      );
    if (state.draft === null) state.draft = exampleFor(op);
    const view = element(
      "div",
      { class: "view-enter" },
      heading(
        "آزمایشگاه فرمان",
        "فکر کنید، ورودی را شکل بدهید، قرارداد را بررسی کنید. بدون اجرا.",
        "IDEAS IN. CLARITY OUT.",
        [badge("بدون اثر جانبی", "good", "shield-check")],
      ),
    );
    const select = element("select", {
      id: "lab-operation",
      "aria-label": "انتخاب عملیات",
      onchange: (event) => {
        openLab(event.target.value);
        $("#lab-operation")?.focus();
      },
    });
    const groups = [
      ...state.catalog.packs.map((pack) => pack.id),
      "nagar.core",
    ];
    groups.forEach((pack) => {
      const options = state.catalog.operations.filter(
        (item) => item.pack_id === pack,
      );
      if (options.length)
        select.append(
          element(
            "optgroup",
            { label: packLabel(pack) },
            options.map((item) =>
              element("option", { value: item.id }, item.id),
            ),
          ),
        );
    });
    select.value = op.id;
    const textarea = element("textarea", {
      id: "command-input",
      class: "code-editor",
      "aria-label": "ورودی JSON فرمان",
      "aria-describedby": "lab-input-hint",
      dir: "ltr",
      spellcheck: "false",
      autocomplete: "off",
      autocapitalize: "off",
      maxlength: 32768,
      value: state.draft,
      oninput: (event) => {
        state.draft = event.target.value;
        invalidateLab();
        updateLabResult();
        updateInputSize();
      },
    });
    // Native Tab behavior intentionally stays intact: no editor keyboard trap.
    const editor = element(
      "section",
      { class: "panel", "aria-label": "ویرایشگر ورودی فرمان" },
      element(
        "div",
        { class: "operation-select" },
        element(
          "label",
          { class: "field-label", for: "lab-operation" },
          "۱. عملیات مورد نظر را انتخاب کنید",
        ),
        select,
        element(
          "div",
          { class: "lab-operation-meta" },
          element("span", {}, titleFor(op)),
          permission(op.permission_level),
        ),
      ),
      element(
        "div",
        { class: "editor-toolbar" },
        element("span", {}, icon("code", "small"), "input.json"),
        element(
          "div",
          { class: "editor-tools" },
          element(
            "button",
            {
              type: "button",
              id: "load-example",
              onclick: () => {
                state.draft = exampleFor(op);
                invalidateLab();
                textarea.value = state.draft;
                updateLabResult();
                updateInputSize();
                toast(
                  "پیش‌نویس نمونه بارگذاری شد؛ شناسه‌ها نمونه‌اند، نه دارایی واقعی.",
                );
              },
            },
            "پیش‌نویس نمونه",
          ),
          element(
            "button",
            {
              type: "button",
              id: "format-json",
              onclick: () => {
                try {
                  const formatted = JSON.stringify(
                    JSON.parse(textarea.value),
                    null,
                    2,
                  );
                  if (new TextEncoder().encode(formatted).length > 32768)
                    throw new Error("too large");
                  state.draft = formatted;
                  textarea.value = formatted;
                  invalidateLab();
                  updateLabResult();
                  updateInputSize();
                  toast("JSON مرتب شد. برای اعتبارسنجی دوباره بررسی کنید.");
                } catch {
                  toast(
                    "JSON ناقص یا بیش از حد بزرگ است؛ ابتدا ورودی را اصلاح کنید.",
                  );
                }
              },
            },
            "مرتب‌سازی",
          ),
        ),
      ),
      textarea,
      element(
        "div",
        { class: "editor-caption" },
        element("span", {}, "فقط ورودی عملیات؛ نه یک فرمان مجاز برای اجرا"),
        element("span", { id: "input-size", class: "mono", dir: "ltr" }),
      ),
      element(
        "div",
        { class: "editor-actions" },
        element(
          "button",
          {
            class: "button primary",
            type: "button",
            id: "validate-input",
            onclick: validateCurrentInput,
          },
          icon("check"),
          element("span", {}, "بررسی ورودی"),
          element("kbd", { dir: "ltr" }, "Ctrl ↵"),
        ),
        element(
          "button",
          {
            class: "button secondary",
            type: "button",
            id: "export-draft",
            disabled: true,
            onclick: exportDraft,
          },
          icon("download"),
          "دریافت پیش‌نویس",
        ),
      ),
    );
    const primary = element(
      "div",
      { class: "lab-primary" },
      editor,
      element(
        "p",
        { class: "lab-hint", id: "lab-input-hint" },
        "پیش‌نویس‌ها نمونهٔ ساختاری‌اند و ممکن است به اصلاح نیاز داشته باشند. ورودی فقط برای بررسی به همین سرور ارسال می‌شود؛ ذخیره یا به مدل خارجی فرستاده نمی‌شود.",
      ),
      element("div", {
        id: "lab-result",
        "aria-live": "polite",
        "aria-atomic": "true",
      }),
    );
    const secondary = element(
      "aside",
      { class: "lab-secondary", "aria-label": "قرارداد و مرزهای بررسی" },
      element(
        "section",
        { class: "panel" },
        element(
          "div",
          { class: "panel-heading" },
          element(
            "div",
            {},
            element("h2", {}, "قرارداد ورودی"),
            element("p", {}, "از مدل واقعی ثبت‌شده"),
          ),
          badge(`Schema ${op.schema_version}`),
        ),
        fieldList(op, 7),
        element(
          "div",
          { class: "schema-more" },
          element(
            "button",
            { type: "button", onclick: () => openInspector(op.id) },
            "دیدن Schema کامل",
            icon("arrow-left", "small"),
          ),
        ),
      ),
      element(
        "section",
        { class: "panel lab-scope" },
        element(
          "h3",
          {},
          icon("shield-check"),
          "این بررسی چه چیزی را ثابت می‌کند؟",
        ),
        element(
          "ul",
          { class: "scope-list" },
          element(
            "li",
            { class: "checked" },
            icon("check"),
            "سازگاری با مدل ورودی",
          ),
          element("li", {}, "مجوز کاربر و تأیید عملیات: بررسی نمی‌شود"),
          element("li", {}, "وجود فایل و ارجاع‌ها: بررسی نمی‌شود"),
          element("li", {}, "اجرای موفق و خروجی: بررسی نمی‌شود"),
        ),
      ),
    );
    view.append(element("div", { class: "lab-layout" }, primary, secondary));
    return view;
  }
  function updateInputSize() {
    const node = $("#input-size");
    if (node)
      node.textContent = `${new TextEncoder().encode(state.draft || "").length.toLocaleString("en-US")} B / 32 KiB`;
  }
  function updateLabResult() {
    const container = $("#lab-result");
    if (!container) return;
    const valid =
      state.labResult?.valid === true &&
      state.labResult.raw === state.draft &&
      state.labResult.operation === state.selectedOp;
    $("#export-draft").disabled = !valid;
    $("#validate-input").disabled = state.labPending;
    $("#validate-input").setAttribute("aria-busy", String(state.labPending));
    if (state.labPending) {
      container.replaceChildren(
        element(
          "div",
          { class: "validation-idle" },
          element("span", { class: "loader" }),
          "در حال بررسی با مدل ورودی ثبت‌شده…",
        ),
      );
      return;
    }
    const result = state.labResult;
    if (!result) {
      container.replaceChildren(
        element(
          "div",
          { class: "validation-idle" },
          icon("terminal"),
          "آمادهٔ بررسی. نتیجهٔ واقعی اینجا نمایش داده می‌شود.",
        ),
      );
      return;
    }
    const copy = element(
      "div",
      {},
      element(
        "h3",
        {},
        valid ? "ساختار ورودی معتبر است." : "ورودی نیاز به بررسی دارد.",
      ),
      element(
        "p",
        {},
        valid
          ? "هیچ عملیاتی اجرا نشده است. مجوز، ارجاع‌ها، فایل‌ها و وضعیت پروژه هنوز بررسی نشده‌اند."
          : result.requestError
            ? errorText(result.requestError)
            : `${number(result.error_count)} مورد با قرارداد ورودی سازگار نیست. مقدارها و محدودیت‌های Schema را بررسی کنید.`,
      ),
    );
    if (result.errors?.length)
      copy.append(
        element(
          "ul",
          { class: "validation-errors" },
          result.errors.map((error) =>
            element(
              "li",
              {},
              element(
                "code",
                { dir: "ltr" },
                error.path.length ? error.path.join(".") : "$",
              ),
              element(
                "span",
                {},
                schemaErrors[error.code] ||
                  "نوع یا ساختار این مقدار با Schema سازگار نیست.",
              ),
            ),
          ),
        ),
      );
    if (result.error_count > result.errors?.length)
      copy.append(
        element(
          "p",
          {},
          `فقط ${number(result.errors.length)} خطای اول نمایش داده شده است.`,
        ),
      );
    if (valid)
      copy.append(
        element(
          "p",
          { class: "dim" },
          "دامنهٔ نتیجه: فقط Schema ورودی · مجوز اجرا: خیر",
        ),
      );
    container.replaceChildren(
      element(
        "section",
        {
          class: `panel validation-panel ${valid ? "success" : "failure"}`,
          "data-validation": valid ? "valid" : "invalid",
        },
        element(
          "div",
          { class: "validation-body" },
          icon(valid ? "shield-check" : "alert"),
          copy,
        ),
      ),
    );
  }
  async function validateCurrentInput() {
    if (!state.catalog || route() !== "lab" || state.labPending) return;
    const raw = state.draft;
    const operation = state.selectedOp;
    const revision = ++state.labRevision;
    try {
      const value = JSON.parse(raw);
      if (!value || Array.isArray(value) || typeof value !== "object")
        throw new Error("envelope");
    } catch {
      state.labResult = { requestError: "invalid_json", valid: false };
      updateLabResult();
      return;
    }
    // Preserve the original JSON bytes: do not normalize away duplicate keys or
    // non-finite numbers before the server's unambiguous-JSON boundary sees them.
    const body = `{"operation":${JSON.stringify(operation)},"input":${raw}}`;
    if (new TextEncoder().encode(body).length > 32768) {
      state.labResult = { requestError: "input_too_large", valid: false };
      updateLabResult();
      return;
    }
    state.labPending = true;
    state.labResult = null;
    updateLabResult();
    try {
      const result = await api("/api/cockpit/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
      });
      if (
        revision !== state.labRevision ||
        raw !== state.draft ||
        operation !== state.selectedOp
      )
        return;
      if (
        result.scope !== "input_schema_only" ||
        result.executed !== false ||
        result.execution_authorized !== false ||
        result.catalog_fingerprint !== state.catalog.fingerprint
      )
        throw Object.assign(new Error("Catalog changed"), {
          code: "catalog_changed",
        });
      state.labResult = { ...result, raw, operation };
    } catch (error) {
      if (revision !== state.labRevision || error.code === "aborted") return;
      if (error.status === 401) {
        clearSession();
        toast("نشست قفل شد؛ دوباره وارد شوید.");
        return;
      }
      state.labResult = { valid: false, requestError: error.code };
    } finally {
      if (revision === state.labRevision) {
        state.labPending = false;
        updateLabResult();
      }
    }
  }
  function exportDraft() {
    const result = state.labResult;
    if (
      !result?.valid ||
      result.raw !== state.draft ||
      result.operation !== state.selectedOp
    )
      return;
    downloadJSON(
      {
        kind: "nexus.cockpit.input-draft.v1",
        operation: state.selectedOp,
        input: JSON.parse(state.draft),
        catalog_fingerprint: result.catalog_fingerprint,
        validation: { scope: "input_schema_only", valid: true },
        execution_authorized: false,
      },
      `nexus-input-${state.selectedOp}.json`,
    );
    toast("پیش‌نویس دریافت شد. این فایل مجوز یا مدرک اجرای فرمان نیست.");
  }
  function openInspector(id) {
    const op = opById(id);
    if (!op) return;
    const dialog = $("#inspector");
    const body = element(
      "div",
      { class: "inspector-body" },
      element(
        "p",
        { class: "inspector-description", lang: "en", dir: "ltr" },
        op.description,
      ),
      permission(op.permission_level),
    );
    const meta = [
      ["بستهٔ ارائه‌دهنده", packLabel(op.pack_id)],
      ["نسخهٔ قابلیت", element("code", { dir: "ltr" }, op.capability_version)],
      [
        "مجوزهای لازم",
        element(
          "code",
          { dir: "ltr" },
          op.required_permissions.join(", ") || "—",
        ),
      ],
      [
        "حالت‌های ثبت‌شده",
        element("code", { dir: "ltr" }, op.execution_modes.join(", ")),
      ],
      ["قطعیتِ تعریف‌شده", op.deterministic ? "قطعی" : "غیرقطعی"],
      [
        "وضعیت تعریف",
        op.available_in_registry ? "موجود در رجیستری" : "در رجیستری غیرفعال",
      ],
    ];
    body.append(
      element(
        "dl",
        { class: "inspector-meta" },
        meta.map(([label, value]) =>
          element(
            "div",
            {},
            element("dt", {}, label),
            element("dd", {}, value),
          ),
        ),
      ),
      notice(
        "این اطلاعات، قرارداد عملیات است؛ وجود فایل، مجوز شما، فعال‌بودن پک و اجرای واقعی را تأیید نمی‌کند.",
      ),
      element("hr"),
      element("h3", {}, "فیلدهای ورودی"),
      fieldList(op),
      element(
        "details",
        {},
        element("summary", {}, "نمایش JSON Schema اصلی"),
        element(
          "pre",
          { class: "raw-schema", dir: "ltr" },
          JSON.stringify(op.input_schema, null, 2),
        ),
      ),
    );
    $("#inspector-content").replaceChildren(
      element(
        "div",
        { class: "inspector-header" },
        element(
          "div",
          {},
          element(
            "div",
            { class: "eyebrow", dir: "ltr" },
            "CAPABILITY INSPECTOR",
          ),
          element("h2", { id: "inspector-title" }, titleFor(op)),
          element("code", { dir: "ltr" }, op.id),
        ),
        element(
          "button",
          {
            class: "icon-button",
            type: "button",
            "aria-label": "بستن جزئیات",
            onclick: () => dialog.close(),
          },
          icon("close"),
        ),
      ),
      body,
      element(
        "div",
        { class: "inspector-actions" },
        button(
          "باز کردن در آزمایشگاه",
          () => openLab(id),
          "primary",
          "terminal",
        ),
        button("کپی شناسه", () => copyText(id), "secondary", "copy"),
        favoriteButton(id),
      ),
    );
    if (!dialog.open) dialog.showModal();
  }
  function trustStatus(pack) {
    if (pack.signature_verified)
      return badge("امضای تأییدشده", "good", "shield-check");
    return badge(
      pack.signature_state === "placeholder"
        ? "امضای نمونه؛ تأییدنشده"
        : "امضا تأیید نشده",
      "warn",
      "alert",
    );
  }
  function renderTrust() {
    const view = element(
      "div",
      { class: "view-enter" },
      heading(
        "شفافیت، بخشی از طراحی است.",
        "دقیقاً بدانید چه چیزی دیده شده و چه چیزی هنوز ثابت نشده است.",
        "TRUST IS EXPLICIT. NEVER ASSUMED.",
        [button("دریافت گزارش", exportCatalog, "secondary", "download")],
      ),
    );
    const cards = [
      [
        "layers",
        "از رجیستری واقعی",
        `${number(state.catalog.summary.operations)} عملیات مستقیماً از کد نصب‌شده خوانده می‌شود؛ نه از یک فهرست تبلیغاتی.`,
      ],
      [
        "shield",
        "ثبت‌شده ≠ اجراشده",
        "کاتالوگ، مجوز اجرا، سلامت وابستگی‌ها و کیفیت خروجی را تضمین نمی‌کند.",
      ],
      [
        "lock",
        state.session.mode === "public_preview"
          ? "پیش‌نمایش عمومی و صریح"
          : "ورود با توکن اختصاصی",
        state.session.mode === "public_preview"
          ? "این حالت عمداً عمومی است، اما فقط همین کاتالوگ و بررسی Schema را ارائه می‌کند."
          : "API با توکن محافظت می‌شود؛ توکن در حافظهٔ تب است، نه فضای ذخیره‌سازی مرورگر.",
      ],
      [
        "server",
        "مستقل از ربات",
        "این پنل هیچ دیتابیس، صف کاری یا مدل هوش مصنوعی را راه‌اندازی یا پایش نمی‌کند.",
      ],
    ];
    view.append(
      element(
        "div",
        { class: "trust-grid" },
        cards.map(([symbol, title, copy]) =>
          element(
            "section",
            { class: "trust-card" },
            icon(symbol),
            element("h3", {}, title),
            element("p", {}, copy),
          ),
        ),
      ),
    );
    const table = element(
      "table",
      { class: "trust-table" },
      element(
        "thead",
        {},
        element(
          "tr",
          {},
          element("th", { scope: "col" }, "پک داخلی"),
          element("th", { scope: "col" }, "تعریف عملیات"),
          element("th", { scope: "col" }, "وضعیت امضا"),
          element(
            "th",
            { scope: "col", class: "trust-activation" },
            "فعال‌سازی در این کاتالوگ",
          ),
        ),
      ),
      element(
        "tbody",
        {},
        state.catalog.packs.map((pack) =>
          element(
            "tr",
            {},
            element(
              "td",
              {},
              element("strong", {}, packLabel(pack.id)),
              element("code", { dir: "ltr" }, pack.id),
            ),
            element(
              "td",
              {},
              badge(
                `${number(pack.registered_count)} / ${number(pack.operations.length)} ثبت‌شده`,
                pack.pending_operations.length ? "warn" : "",
              ),
            ),
            element("td", {}, trustStatus(pack)),
            element(
              "td",
              { class: "trust-activation dim" },
              pack.active_in_catalog ? "فعال در کاتالوگ" : "فعال نشده",
            ),
          ),
        ),
      ),
    );
    view.append(
      element(
        "section",
        { class: "panel" },
        element(
          "div",
          { class: "panel-heading" },
          element(
            "div",
            {},
            element("h2", {}, "منبع داخلی، جای امضای معتبر را نمی‌گیرد."),
            element("p", {}, "مقادیر زیر گزارش واقعی بررسی مانیفست هستند."),
          ),
          badge("BUILTIN"),
        ),
        table,
      ),
    );
    view.append(
      notice(
        "پک‌های داخلی با لنگر builtin ثبت می‌شوند. امضای placeholder به معنی تأیید رمزنگاری نیست. برای پک خارجی، اعتبارسنجی امضا همچنان در مرز اعتماد موجود پروژه انجام می‌شود.",
        "bottom-note warn",
      ),
    );
    view.append(
      element(
        "section",
        { class: "panel fingerprint-panel" },
        element(
          "div",
          { class: "fingerprint-copy" },
          element(
            "h3",
            { class: "fingerprint-title" },
            icon("fingerprint"),
            "اثر انگشت کاتالوگ",
          ),
          element(
            "p",
            {},
            "SHA-256 از نسخه، تعاریف و قراردادها؛ با بازخوانی ساده عوض نمی‌شود. این هش، مدرک اجرای عملیات نیست.",
          ),
          element(
            "code",
            { class: "fingerprint-value", dir: "ltr" },
            state.catalog.fingerprint,
          ),
        ),
        button(
          "کپی اثر انگشت",
          () => copyText(state.catalog.fingerprint),
          "secondary",
          "copy",
        ),
      ),
    );
    return view;
  }
  function renderGuide() {
    const view = element(
      "div",
      { class: "view-enter" },
      heading(
        "یک اتاق فرمان، با مرزهای روشن.",
        "راهنمای کوتاه استفاده، راه‌اندازی و معنای نتایج.",
        "A LITTLE CONTEXT GOES A LONG WAY.",
      ),
    );
    view.append(
      element(
        "div",
        { class: "guide-grid" },
        element(
          "section",
          { class: "panel guide-section" },
          element("h2", {}, "از کشف تا یک پیش‌نویس دقیق"),
          element(
            "ol",
            {},
            element(
              "li",
              {},
              "در کتابخانه، با نام فارسی یا شناسهٔ فنی جست‌وجو کنید. فیلتر بسته و سطح دسترسی نتیجه‌ها را محدود می‌کند.",
            ),
            element(
              "li",
              {},
              "جزئیات عملیات را باز کنید. Schema از همان مدل ورودی موجود در پروژه می‌آید.",
            ),
            element(
              "li",
              {},
              "در آزمایشگاه، نمونه را تغییر دهید و «بررسی ورودی» را بزنید. فقط قرارداد ورودی بررسی می‌شود.",
            ),
            element(
              "li",
              {},
              "اگر ورودی معتبر بود، پیش‌نویس JSON را دریافت کنید. اجرا باید از مسیر اصلی CommandBus و مجوزهای پروژه عبور کند.",
            ),
          ),
          notice(
            "میان‌بُرها: Ctrl/⌘ + K جست‌وجوی سریع؛ Ctrl/⌘ + Enter بررسی ورودی؛ Escape بستن پنجره.",
          ),
        ),
        element(
          "section",
          { class: "panel guide-section" },
          element("h2", {}, "راه‌اندازی مستقل و اختیاری"),
          element("p", {}, "برای پیش‌نمایش عمومیِ همین کاتالوگ محلی:"),
          element(
            "pre",
            {},
            "python -m nexus_ai_agent.cockpit --preview --port 3000",
          ),
          element(
            "p",
            {},
            "برای حالت محافظت‌شده، یک توکن تصادفیِ حداقل ۳۲ نویسه‌ای را در متغیر محیطی NEXUS_COCKPIT_TOKEN روی سرور قرار دهید و بدون --preview اجرا کنید:",
          ),
          element("pre", {}, "python -m nexus_ai_agent.cockpit --port 3000"),
          element(
            "p",
            {},
            "فایل .env خودکار خوانده نمی‌شود. متغیر باید در محیط فرایند باشد. روی شبکه از پراکسی HTTPS و محدودیت درخواست استفاده کنید.",
          ),
          notice(
            "این برنامه API قدیمی، ربات و CLI اصلی پروژه را جایگزین نمی‌کند. مسیر اختیاری و جداست.",
            "warn",
          ),
        ),
      ),
    );
    const questions = [
      [
        "«معتبر» دقیقاً یعنی چه؟",
        "یعنی مدل Pydantic عملیات، ورودی را پذیرفته است. مجوز کاربر، تأیید عملیات، فعال‌بودن پک، ارجاع به پروژه، وجود فایل، وابستگی‌های اجرا و شواهد خروجی بررسی نشده‌اند. این پاسخ هیچ فرمانی را اجرا نمی‌کند.",
      ],
      [
        "سطح‌های A، B، C و D چه تفاوتی دارند؟",
        "A عملیات فوری و غیرمخرب است. B تغییر بازگشت‌پذیر است. C به تأیید صریح نیاز دارد. D در سیاست پروژه ممنوع است. این پنل فقط سطح تعریف‌شده را نشان می‌دهد؛ هیچ سطحی در اینجا اجرا نمی‌شود.",
      ],
      [
        "چه چیزی روی مرورگر یا سرور می‌ماند؟",
        "در مرورگر فقط پوسته و شناسهٔ نشان‌شده‌ها ذخیره می‌شود. ورودی و توکن در حافظهٔ تب هستند. ورودی برای بررسی به همین سرور می‌رود، نه به ارائه‌دهندهٔ مدل. خود این برنامه ورودی را ذخیره یا لاگ نمی‌کند؛ سیاست لاگ پراکسی را مدیر استقرار باید جداگانه بررسی کند.",
      ],
      [
        "آیا این پنل سلامت ربات یا آماده‌بودن تولید را نشان می‌دهد؟",
        "خیر. این فقط نمای کاتالوگ نصب‌شده است. نه به ربات یا صف کار متصل است و نه به دیتابیس دسترسی دارد. healthz فقط زنده‌بودن فرایند همین پنل را نشان می‌دهد.",
      ],
      [
        "حدود ورودی و اجرای هم‌زمان چیست؟",
        "حداکثر بدنهٔ درخواست ۳۲ کیلوبایت، عمق JSON برابر ۲۴ و تعداد مقدارها ۲۰۴۸ است. هر فرایند حداکثر چهار بررسی هم‌زمان می‌پذیرد و دریافت بدنه ۱۰ ثانیه مهلت دارد. این محدودیت‌ها جایگزین محدودیت ورودی در پراکسی نیستند.",
      ],
      [
        "کاتالوگ چه زمانی تازه می‌شود؟",
        "دکمهٔ بازخوانی، آخرین مشاهده از کاتالوگ همین فرایند را می‌گیرد. بعد از تغییر نسخهٔ نصب‌شده یا قراردادهای رجیستری، برنامه را از نو راه‌اندازی کنید. افزونهٔ دلخواه یا کد خارجی از این رابط بارگذاری نمی‌شود.",
      ],
    ];
    view.append(
      element(
        "section",
        { class: "panel guide-faq" },
        questions.map(([question, answer]) =>
          element(
            "details",
            {},
            element("summary", {}, question),
            element("p", {}, answer),
          ),
        ),
      ),
    );
    return view;
  }
  function render() {
    updateChrome();
    const current = route();
    if (!state.catalog) {
      if (state.session) renderAuth();
      return;
    }
    const views = {
      overview: renderOverview,
      capabilities: () => renderCatalog(false),
      favorites: () => renderCatalog(true),
      lab: renderLab,
      trust: renderTrust,
      guide: renderGuide,
    };
    $("#main").replaceChildren(views[current]());
    $("#main").setAttribute("aria-busy", "false");
    $("#announcer").textContent = routes[current];
    if (current === "capabilities" || current === "favorites")
      renderCatalogResults();
    if (current === "lab") {
      updateInputSize();
      updateLabResult();
    }
  }
  function openPalette() {
    if (!state.catalog) {
      toast("برای جست‌وجو ابتدا کاتالوگ را باز کنید.");
      return;
    }
    $("#palette-input").value = "";
    state.paletteIndex = 0;
    renderPalette();
    const dialog = $("#command-palette");
    if (!dialog.open) dialog.showModal();
    $("#palette-input").focus();
  }
  function renderPalette() {
    const query = $("#palette-input").value;
    const navigation = Object.entries(routes)
      .filter(([id, label]) =>
        normalize(`${label} ${id}`).includes(normalize(query)),
      )
      .map(([id, label]) => ({
        label,
        detail: `WORKSPACE / ${id.toUpperCase()}`,
        icon:
          id === "lab" ? "terminal" : id === "trust" ? "shield" : "dashboard",
        action: () => navigate(id),
      }));
    const operations = state.catalog.operations
      .filter((op) => matches(op, query))
      .map((op) => ({
        label: titleFor(op),
        detail: op.id,
        icon: packIcon(op.pack_id),
        action: () => openLab(op.id),
      }));
    state.paletteItems = [...navigation, ...operations].slice(0, 9);
    state.paletteIndex = Math.min(
      state.paletteIndex,
      Math.max(0, state.paletteItems.length - 1),
    );
    if (!state.paletteItems.length) {
      $("#palette-results").replaceChildren(
        element(
          "div",
          { class: "palette-empty" },
          "فرمانی پیدا نشد؛ عبارت دیگری را امتحان کنید.",
        ),
      );
      $("#palette-input").removeAttribute("aria-activedescendant");
      return;
    }
    $("#palette-results").replaceChildren(
      ...state.paletteItems.map((item, index) =>
        element(
          "button",
          {
            type: "button",
            role: "option",
            tabindex: "-1",
            class: "palette-result",
            id: `palette-option-${index}`,
            "aria-selected": String(index === state.paletteIndex),
            onclick: () => choosePalette(index),
          },
          icon(item.icon),
          element(
            "span",
            {},
            element("strong", {}, item.label),
            element("small", { dir: "ltr" }, item.detail),
          ),
          element("kbd", {}, "↵"),
        ),
      ),
    );
    $("#palette-input").setAttribute(
      "aria-activedescendant",
      `palette-option-${state.paletteIndex}`,
    );
  }
  function choosePalette(index) {
    const item = state.paletteItems[index];
    if (!item) return;
    $("#command-palette").close();
    item.action();
  }
  $("#palette-input").addEventListener("input", () => {
    state.paletteIndex = 0;
    renderPalette();
  });
  $("#palette-input").addEventListener("keydown", (event) => {
    if (event.isComposing) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!state.paletteItems.length) return;
      state.paletteIndex =
        (state.paletteIndex +
          (event.key === "ArrowDown" ? 1 : -1) +
          state.paletteItems.length) %
        state.paletteItems.length;
      renderPalette();
      $(`#palette-option-${state.paletteIndex}`).scrollIntoView({
        block: "nearest",
      });
    } else if (event.key === "Enter") {
      event.preventDefault();
      choosePalette(state.paletteIndex);
    }
  });
  $$("[data-close-dialog]").forEach((node) =>
    node.addEventListener("click", () =>
      $(`#${node.dataset.closeDialog}`).close(),
    ),
  );
  $$("dialog").forEach((dialog) =>
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (
        event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom
      )
        dialog.close();
    }),
  );
  $(".skip-link").addEventListener("click", (event) => {
    // Keep the active hash route; #main must not turn a lab into an overview.
    event.preventDefault();
    $("#main").focus();
    $("#main").scrollIntoView({ block: "start" });
  });
  $("#open-palette").addEventListener("click", openPalette);
  $("#theme-toggle").addEventListener("click", () =>
    setTheme(
      document.documentElement.dataset.theme === "dark" ? "light" : "dark",
    ),
  );
  $("#refresh").addEventListener("click", () => {
    if (!state.session) boot();
    else if (state.session.requires_token && !state.token) renderAuth();
    else loadCatalog();
  });
  $("#retry-connection").addEventListener("click", () =>
    state.session ? loadCatalog() : boot(),
  );
  $("#lock-session").addEventListener("click", () => {
    clearSession();
    toast("پنل قفل شد. توکن و ورودی از حافظهٔ این تب پاک شدند.");
  });
  document.addEventListener("keydown", (event) => {
    if (event.isComposing) return;
    // Search inputs can consume native Escape to clear their value on mobile.
    // Explicitly close the top dialog so our documented shortcut stays reliable.
    if (event.key === "Escape" && $$("dialog[open]").length) {
      event.preventDefault();
      $$("dialog[open]").at(-1).close();
      return;
    }
    const editable =
      event.target instanceof HTMLElement &&
      (event.target.matches("input,textarea,select") ||
        event.target.isContentEditable);
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      if ($("#inspector").open) $("#inspector").close();
      if ($("#command-palette").open) $("#command-palette").close();
      else openPalette();
    } else if (
      (event.ctrlKey || event.metaKey) &&
      event.key === "Enter" &&
      route() === "lab" &&
      !$$("dialog[open]").length
    ) {
      event.preventDefault();
      validateCurrentInput();
    } else if (event.key === "/" && !editable && !$$("dialog[open]").length) {
      event.preventDefault();
      if ($("#catalog-search")) $("#catalog-search").focus();
      else openPalette();
    }
  });
  window.addEventListener("hashchange", () => {
    if (location.hash === "#main") {
      $("#main").focus();
      return;
    }
    $$("dialog[open]").forEach((dialog) => dialog.close());
    render();
    window.scrollTo({ top: 0, behavior: "instant" });
  });
  window.addEventListener("pagehide", () => {
    // bfcache must not revive credentials or input on a protected page.
    if (state.token) clearSession();
  });
  boot();
})();
