// LucimaTools 前端逻辑
const $ = (id) => document.getElementById(id);

// Shared visual mark for semantic multi-select checkboxes. The native input
// remains in the DOM for keyboard/accessibility semantics while this inline
// SVG supplies the themed check indicator.
const checkboxUIHTML = () => `
  <span class="check-box" aria-hidden="true">
    <svg viewBox="0 0 24 24" focusable="false"><path d="M4 12 10 18 20 6"></path></svg>
  </span>`;
// 这些接口不绑定单个账号（全局或自带 account），api() 不自动注入 activeAccount
const _GLOBAL_PATHS = new Set(["/api/config", "/api/preferences", "/api/logs", "/api/logs/save", "/api/equipment-filter/export", "/api/diagnostics/runtime", "/api/tasks", "/api/accounts", "/api/login", "/api/login/saved", "/api/accounts/logout", "/api/accounts/delete", "/api/accounts/restore", "/api/update/status", "/api/update/check", "/api/update/ignore", "/api/update/install", "/api/update/channel"]);
_GLOBAL_PATHS.add("/api/encyclopedia/resources");
_GLOBAL_PATHS.add("/api/encyclopedia/save-image");
const _CONNECTION_BLOCKED_PATHS = new Set([
  "/api/tasks/auto", "/api/tasks/run", "/api/shop/reset", "/api/shop/buy",
  "/api/star-source/query", "/api/star-source/refresh", "/api/star-source/buy",
  "/api/customized-equip/action", "/api/store/options", "/api/store/buy",
  "/api/sweep/options", "/api/sweep/run", "/api/activity/options", "/api/activity/run",
  "/api/activity/progress/run",
  "/api/activity/urgent/clear", "/api/support/favorite", "/api/support/unfavorite",
  "/api/arena/options", "/api/arena/config", "/api/dev/call",
  "/api/abyss/options", "/api/abyss/config", "/api/abyss/run", "/api/abyss/cancel", "/api/abyss/rewards",
  "/api/guild/overview", "/api/guild/action", "/api/guild/quests/query", "/api/guild/quests/start",
]);

// Request pacing is deliberately owned by repeated-task loops. Keeping this
// state in the UI lets login, one-off actions, and developer-console calls
// stay immediate while each batch waits only between its own iterations.
const REQUEST_DELAY_DEFAULTS = Object.freeze({ enabled: true, ms: 300, jitter: 20 });
let requestDelayRuntime = { ...REQUEST_DELAY_DEFAULTS };

function waitRequestDelayBetweenIterations(iteration, shouldStop = null) {
  if (Number(iteration) <= 1) return Promise.resolve();
  const cfg = requestDelayRuntime || REQUEST_DELAY_DEFAULTS;
  if (cfg.enabled === false) return Promise.resolve();
  const ms = Math.max(0, Math.min(60000, Number(cfg.ms) || 0));
  const jitter = Math.max(0, Math.min(100, Number(cfg.jitter) || 0)) / 100;
  if (!ms) return Promise.resolve();
  const target = ms * (1 - jitter + Math.random() * jitter * 2);
  if (typeof shouldStop === "function" && shouldStop()) return Promise.resolve();
  return new Promise((resolve) => {
    let settled = false;
    let timer = null;
    let poll = null;
    const finish = () => {
      if (settled) return;
      settled = true;
      if (timer != null) window.clearTimeout(timer);
      if (poll != null) window.clearInterval(poll);
      resolve();
    };
    timer = window.setTimeout(finish, Math.round(target));
    if (typeof shouldStop === "function") {
      poll = window.setInterval(() => {
        if (shouldStop()) finish();
      }, 100);
    }
  });
}

const api = async (path, body, { signal } = {}) => {
  const [apiPath, queryString = ""] = String(path || "").split("?", 2);
  const queryAccount = new URLSearchParams(queryString).get("account");
  const requestAccount = body?.account || queryAccount || (typeof activeAccount !== "undefined" ? activeAccount : null);
  const localAccount = requestAccount && typeof ACCOUNTS !== "undefined" ? ACCOUNTS[requestAccount] : null;
  if (_CONNECTION_BLOCKED_PATHS.has(apiPath) && localAccount?.connection?.connected === false) {
    applyConnectionState(requestAccount, localAccount.connection, { notify: true });
    throw new Error("[重复登录] 当前连接已断开");
  }
  // POST：向 body 注入 account（未显式给且非全局接口时）
  if (body && typeof body === "object" && !("account" in body) && activeAccount && !_GLOBAL_PATHS.has(apiPath)) {
    body = { ...body, account: activeAccount };
  }
  // GET：非全局接口带上 ?account=
  let url = path;
  if (!body && activeAccount && !_GLOBAL_PATHS.has(apiPath) && !queryAccount) {
    url += (path.includes("?") ? "&" : "?") + "account=" + encodeURIComponent(activeAccount);
  }
  const opt = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const res = await fetch(url, { ...opt, signal });
  const data = await res.json().catch(() => ({}));
  const responseAccount = data?.account || requestAccount;
  if (data?.connection && responseAccount && typeof applyConnectionState === "function") {
    // The first duplicate-login response is logged and reflected in the
    // status pill. A task click after that is what shows the one-time toast.
    applyConnectionState(responseAccount, data.connection, { notify: false });
  }
  // Every account response can advertise a new inventory version, including
  // the background poll when a task batch has not produced a log yet.
  if (localAccount && ACCOUNTS[responseAccount] === localAccount
      && apiPath !== "/api/roster" && apiPath !== "/api/inventory") {
    observeInventoryVersion(responseAccount, data);
  }
  if (!res.ok) throw Object.assign(new Error(data.detail || `HTTP ${res.status}`), { httpStatus: res.status });
  if (data.guild && localAccount && ACCOUNTS[responseAccount] === localAccount
      && typeof applyGuildSnapshot === "function") {
    applyGuildSnapshot(responseAccount, data.guild);
  }
  return data;
};

const LEGACY_FRONTEND_SETTINGS_PREFIX = "ark_";
let FRONTEND_SETTINGS = readLegacyFrontendSettings();
let frontendSettingsSaveTimer = null;
let frontendSettingsSavePending = false;
let frontendSettingsSaveInFlight = false;
let frontendSettingsDirty = false;

function readLegacyFrontendSettings() {
  const output = {};
  try {
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index);
      if (!key || !key.startsWith(LEGACY_FRONTEND_SETTINGS_PREFIX) || key === "ark_dev_pass") continue;
      const value = localStorage.getItem(key);
      if (value != null) output[key] = value;
    }
    localStorage.removeItem("ark_dev_pass");
  } catch { /* localStorage 不可用时由后端设置或默认值接管 */ }
  return output;
}

function frontendStorageGet(key) {
  const value = FRONTEND_SETTINGS[key];
  return typeof value === "string" ? value : null;
}

function frontendStorageSet(key, value) {
  FRONTEND_SETTINGS[key] = String(value);
  frontendSettingsDirty = true;
  scheduleFrontendSettingsSave();
}

function frontendStorageRemove(key) {
  if (!Object.prototype.hasOwnProperty.call(FRONTEND_SETTINGS, key)) return;
  delete FRONTEND_SETTINGS[key];
  frontendSettingsDirty = true;
  scheduleFrontendSettingsSave();
}

function scheduleFrontendSettingsSave(delay = 350) {
  frontendSettingsSavePending = true;
  if (frontendSettingsSaveTimer) clearTimeout(frontendSettingsSaveTimer);
  frontendSettingsSaveTimer = setTimeout(() => {
    frontendSettingsSaveTimer = null;
    flushFrontendSettings();
  }, delay);
}

async function flushFrontendSettings() {
  if (frontendSettingsSaveInFlight || !frontendSettingsSavePending) return;
  frontendSettingsSavePending = false;
  frontendSettingsSaveInFlight = true;
  try {
    await api("/api/preferences", { settings: { ...FRONTEND_SETTINGS } });
    frontendSettingsDirty = false;
  } catch (error) {
    frontendSettingsSavePending = true;
    console.warn("保存前端设置失败：", error);
    if (!frontendSettingsSaveTimer) {
      frontendSettingsSaveTimer = setTimeout(() => {
        frontendSettingsSaveTimer = null;
        flushFrontendSettings();
      }, 1500);
    }
  } finally {
    frontendSettingsSaveInFlight = false;
  }
}

async function loadFrontendSettings() {
  const legacy = { ...FRONTEND_SETTINGS };
  let remote = {};
  try {
    const result = await api("/api/preferences");
    if (result.settings && typeof result.settings === "object" && !Array.isArray(result.settings)) {
      remote = Object.fromEntries(
        Object.entries(result.settings).filter(([, value]) => typeof value === "string"),
      );
    }
  } catch (error) {
    console.warn("读取前端设置失败：", error);
  }

  const merged = { ...legacy, ...remote };
  const needsMigration = Object.keys(legacy).some((key) => !Object.prototype.hasOwnProperty.call(remote, key));
  FRONTEND_SETTINGS = merged;
  if (needsMigration || (!Object.keys(remote).length && Object.keys(legacy).length)) {
    frontendSettingsDirty = true;
    frontendSettingsSavePending = true;
    await flushFrontendSettings();
  }
  reloadEquipmentFilterSchemesFromSettings();
}

window.addEventListener("pagehide", () => {
  if (!frontendSettingsDirty) return;
  try {
    fetch("/api/preferences", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: { ...FRONTEND_SETTINGS } }),
      keepalive: true,
    });
  } catch { /* 关闭窗口时尽力保存 */ }
});

// ---------- 主题引擎 ----------
const THEME_LS = "ark_theme";
const PRESETS = [
  { name: "cyber", accent: "#00e5ff", accent2: "#7c5cff" },
  { name: "aurora", accent: "#2ee6a0", accent2: "#00b8d4" },
  { name: "sakura", accent: "#ff7fac", accent2: "#a06bff" },
  { name: "ember", accent: "#ff7a45", accent2: "#ffb454" },
  { name: "ocean", accent: "#4aa8ff", accent2: "#6b5cff" },
  { name: "violet", accent: "#a06bff", accent2: "#ff6bd6" },
];
function hexToRgb(hex) {
  const h = hex.replace("#", "");
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
// 默认主题：sakura（樱粉）
const DEFAULT_THEME = { accent: "#ff7fac", accent2: "#a06bff", mode: "dark" };
function getTheme() {
  try {
    const v = JSON.parse(frontendStorageGet(THEME_LS)) || {};
    return {
      accent: v.accent || DEFAULT_THEME.accent,
      accent2: v.accent2 || DEFAULT_THEME.accent2,
      mode: v.mode || DEFAULT_THEME.mode,
    };
  } catch { return { ...DEFAULT_THEME }; }
}
function applyTheme(t) {
  const root = document.documentElement;
  root.style.setProperty("--accent", t.accent);
  root.style.setProperty("--accent-2", t.accent2);
  root.style.setProperty("--accent-rgb", hexToRgb(t.accent).join(", "));
  root.style.setProperty("--accent2-rgb", hexToRgb(t.accent2).join(", "));
  root.setAttribute("data-theme", t.mode);
  frontendStorageSet(THEME_LS, JSON.stringify(t));
  syncThemeUI(t);
}
function setTheme(patch) { applyTheme({ ...getTheme(), ...patch }); }
function toggleMode() { setTheme({ mode: getTheme().mode === "dark" ? "light" : "dark" }); }
// 把当前主题反映到设置面板控件 + 登录页明暗按钮
function syncThemeUI(t) {
  const nextModeLabel = t.mode === "dark" ? "切换到亮色" : "切换到深色";
  const themeIconHref = t.mode === "dark" ? "#nav-sun" : "#nav-moon";
  document.querySelectorAll("[data-theme-icon-use]").forEach((icon) => icon.setAttribute("href", themeIconHref));
  const mini = $("loginThemeBtn");
  if (mini) {
    mini.title = nextModeLabel;
    mini.setAttribute("aria-label", nextModeLabel);
  }
  const sideToggle = $("themeToggle");
  if (sideToggle) sideToggle.title = nextModeLabel;
  const ap = $("accentPick"); if (ap) ap.value = t.accent;
  const ap2 = $("accent2Pick"); if (ap2) ap2.value = t.accent2;
  document.querySelectorAll("#modeSeg button").forEach((b) => b.classList.toggle("active", b.dataset.mode === t.mode));
  document.querySelectorAll("#swatchRow .swatch").forEach((s) =>
    s.classList.toggle("active", s.dataset.accent === t.accent && s.dataset.accent2 === t.accent2)
  );
}
function renderSwatches() {
  const row = $("swatchRow"); if (!row) return;
  row.innerHTML = PRESETS.map(
    (p) => `<div class="swatch" data-accent="${p.accent}" data-accent2="${p.accent2}"
      style="background:linear-gradient(135deg, ${p.accent}, ${p.accent2})" title="${p.name}"></div>`
  ).join("");
  row.querySelectorAll(".swatch").forEach((s) =>
    s.addEventListener("click", () => setTheme({ accent: s.dataset.accent, accent2: s.dataset.accent2 }))
  );
}

// ---------- 板块导航 ----------
const SECTION_TITLES = { info: "基本信息", auto: "自动任务", manual: "主动任务", guild: "佣兵团", toolbox: "工具箱", logs: "运行日志", settings: "系统设置", about: "关于我们" };
let currentSection = "info";
let logUnread = 0;
function showSection(id) {
  const leavingToolbox = currentSection === "toolbox" && id !== "toolbox";
  currentSection = id;
  if ((id === "toolbox" || leavingToolbox) && typeof showToolboxHome === "function") showToolboxHome();
  if (id !== "toolbox" && typeof stopBackgroundTestPolling === "function") stopBackgroundTestPolling();
  document.querySelectorAll(".section").forEach((s) => s.classList.toggle("active", s.id === `section-${id}`));
  document.querySelectorAll("#sideNav .nav-item").forEach((n) => n.classList.toggle("active", n.dataset.section === id));
  $("crumb").textContent = SECTION_TITLES[id] || "";
  if (id === "logs") {
    logUnread = 0;
    updateLogBadge();
    setBottomLogExpanded(false);
  }
  window.dispatchEvent(new CustomEvent("lucima:section-change", { detail: { section: id } }));
  // 移动端选完自动收起侧栏 + 隐藏遮罩
  if (window.innerWidth <= 820) {
    $("sidebar").classList.add("collapsed");
    $("sidebarBackdrop").classList.remove("show");
  }
}
function updateLogBadge() {
  const value = logUnread > 99 ? "99+" : String(logUnread);
  [$("logBadge"), $("bottomLogBadge")].forEach((badge) => {
    if (!badge) return;
    if (logUnread > 0) {
      badge.textContent = value;
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
  });
}

// ---------- 日志（每账号独立缓冲） ----------
// 每个账号的日志存 ACCOUNTS[email].logHtml；只有"当前查看账号"的日志渲染进 #log。
// 后台账号的任务日志累加到各自缓冲 + 切换器上显示未读小红点。
// ts = 该条日志的产生时刻(epoch ms)，缺省用当前时刻。
// 后端巡检日志带自己的 ts（见 accounts.push_log）：窗口隐藏/冻结期间轮询停摆，
// 恢复后积压记录会一次性拉回，用渲染时刻打戳会让它们全变成"刚刚"。
// 非当天的记录补上"MM-DD"，否则挂机过夜后看不出是昨天的。
function _logLine(msg, cls, ts) {
  const d = ts ? new Date(ts) : new Date();
  const hms = d.toLocaleTimeString("zh-CN", { hour12: false });
  const sameDay = d.toDateString() === new Date().toDateString();
  const p = (n) => String(n).padStart(2, "0");
  const stamp = sameDay ? hms : `${p(d.getMonth() + 1)}-${p(d.getDate())} ${hms}`;
  return `<span class="l-time">[${stamp}]</span> <span class="l-${cls}">${msg}</span>\n`;
}

function renderLogViews(html) {
  [$("log"), $("bottomLog")].forEach((view) => {
    if (!view) return;
    view.innerHTML = html || "";
    view.scrollTop = view.scrollHeight;
  });
}

function isBottomLogExpanded() {
  return !!$("bottomLogDock")?.classList.contains("expanded");
}

// 写日志到指定账号（默认当前账号）。email 无对应账号时退化为直接写 DOM（登录前）。
function logTo(email, msg, cls = "info", ts = null) {
  const a = ACCOUNTS[email];
  const line = _logLine(msg, cls, ts);
  if (a) {
    a.logHtml = (a.logHtml || "") + line;
    if (email === activeAccount) {
      renderLogViews(a.logHtml);
      if (currentSection !== "logs" && !isBottomLogExpanded()) { logUnread++; updateLogBadge(); }
    } else {
      a.logUnread = (a.logUnread || 0) + 1;
      updateAccountSwitcherUnread(email);
    }
  } else {
    renderLogViews(($("log")?.innerHTML || "") + line);
  }
}
function log(msg, cls = "info") { logTo(activeAccount, msg, cls); }

const BOTTOM_LOG_LS = "ark_bottom_log_enabled";
const BOTTOM_LOG_HEIGHT_LS = "ark_bottom_log_height";
let bottomLogHeight = null;

function bottomLogHeightLimits() {
  const main = document.querySelector(".main");
  const total = main?.clientHeight || window.innerHeight || 640;
  const topbarHeight = main?.querySelector(".topbar")?.offsetHeight || 0;
  const min = Math.min(140, Math.max(108, total - topbarHeight - 100));
  const reservedContent = Math.max(100, Math.min(180, total * .24));
  const max = Math.max(min, total - topbarHeight - reservedContent);
  return { min: Math.round(min), max: Math.round(max) };
}

function defaultBottomLogHeight() {
  const total = window.innerHeight || 640;
  return window.innerWidth <= 820 ? Math.min(240, total * .3) : Math.min(300, total * .38);
}

function setBottomLogHeight(height, persist = false) {
  const dock = $("bottomLogDock");
  if (!dock) return;
  const limits = bottomLogHeightLimits();
  const value = Math.round(Math.max(limits.min, Math.min(limits.max, Number(height) || defaultBottomLogHeight())));
  bottomLogHeight = value;
  dock.style.setProperty("--bottom-log-height", `${value}px`);
  const resizer = $("bottomLogResizer");
  resizer?.setAttribute("aria-valuemin", String(limits.min));
  resizer?.setAttribute("aria-valuemax", String(limits.max));
  resizer?.setAttribute("aria-valuenow", String(value));
  if (persist) {
    frontendStorageSet(BOTTOM_LOG_HEIGHT_LS, String(value));
  }
}

function setBottomLogExpanded(expanded) {
  const dock = $("bottomLogDock");
  const main = document.querySelector(".main");
  const enabled = $("bottomLogEnabled")?.checked;
  if (!dock) return;
  const open = !!expanded && !!enabled;
  dock.classList.toggle("expanded", open);
  main?.classList.toggle("bottom-log-expanded", open);
  $("bottomLogExpand")?.setAttribute("aria-expanded", String(open));
  if (open) {
    setBottomLogHeight(bottomLogHeight ?? defaultBottomLogHeight());
    logUnread = 0;
    updateLogBadge();
    const html = activeAcc()?.logHtml || $("log")?.innerHTML || "";
    renderLogViews(html);
  }
}

function setBottomLogEnabled(enabled, persist = true) {
  const checkbox = $("bottomLogEnabled");
  const dock = $("bottomLogDock");
  const main = document.querySelector(".main");
  if (checkbox) checkbox.checked = !!enabled;
  dock?.classList.toggle("hidden", !enabled);
  main?.classList.toggle("bottom-log-enabled", !!enabled);
  if (!enabled) setBottomLogExpanded(false);
  if (persist) {
    frontendStorageSet(BOTTOM_LOG_LS, enabled ? "1" : "0");
  }
}

function initBottomLogDock() {
  let enabled = false;
  enabled = frontendStorageGet(BOTTOM_LOG_LS) === "1";
  const savedHeight = Number(frontendStorageGet(BOTTOM_LOG_HEIGHT_LS));
  bottomLogHeight = Number.isFinite(savedHeight) && savedHeight > 0 ? savedHeight : defaultBottomLogHeight();
  setBottomLogHeight(bottomLogHeight);
  setBottomLogEnabled(enabled, false);
  $("bottomLogEnabled")?.addEventListener("change", (event) => setBottomLogEnabled(event.target.checked));
  $("bottomLogExpand")?.addEventListener("click", () => setBottomLogExpanded(true));
  $("bottomLogCollapse")?.addEventListener("click", () => setBottomLogExpanded(false));

  const resizer = $("bottomLogResizer");
  const dock = $("bottomLogDock");
  let drag = null;
  const finishDrag = (pointerId) => {
    if (!drag || (pointerId != null && drag.pointerId !== pointerId)) return;
    drag = null;
    dock?.classList.remove("resizing");
    setBottomLogHeight(bottomLogHeight, true);
  };
  resizer?.addEventListener("pointerdown", (event) => {
    if (!isBottomLogExpanded()) return;
    drag = {
      pointerId: event.pointerId,
      startY: event.clientY,
      startHeight: dock.getBoundingClientRect().height,
    };
    dock.classList.add("resizing");
    resizer.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  resizer?.addEventListener("pointermove", (event) => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    setBottomLogHeight(drag.startHeight + drag.startY - event.clientY);
    event.preventDefault();
  });
  resizer?.addEventListener("pointerup", (event) => finishDrag(event.pointerId));
  resizer?.addEventListener("pointercancel", (event) => finishDrag(event.pointerId));
  resizer?.addEventListener("lostpointercapture", (event) => finishDrag(event.pointerId));
  resizer?.addEventListener("keydown", (event) => {
    const limits = bottomLogHeightLimits();
    let next = bottomLogHeight;
    if (event.key === "ArrowUp") next += 20;
    else if (event.key === "ArrowDown") next -= 20;
    else if (event.key === "Home") next = limits.min;
    else if (event.key === "End") next = limits.max;
    else return;
    event.preventDefault();
    setBottomLogHeight(next, true);
  });
  window.addEventListener("resize", () => setBottomLogHeight(bottomLogHeight));
}

// ---------- 登录 ----------
// 登录卡片的单一状态框。kind: hint / pending / ok / err。
const LOGIN_HINT =
  '不再保存密码；登录成功后由系统安全存储登录token。';
function loginStatus(html, kind = "hint") {
  const box = $("loginStatus");
  if (!box) return;
  // 不传内容 → 回到默认引导态
  box.innerHTML = html || LOGIN_HINT;
  box.className = "login-status " + (html ? kind : "hint");
}

// 有密码时换取新 Token；从下拉框选中账号后留空密码即可恢复会话。
async function doLogin() {
  if (!updateStartupReady || updateStartupBlocked) {
    loginStatus("正在完成版本检查，请稍候…", "pending");
    return;
  }
  const account = $("account").value.trim();
  const password = $("password").value;
  if (!account) return loginStatus("请填写账号", "err");
  if (!password && !hasSavedToken(account)) return loginStatus("请填写密码", "err");
  const restoring = !password;
  loginStatus(restoring ? "正在恢复登录…" : "正在登录…", "pending");
  $("loginBtn").disabled = true;
  $("loginBtn").textContent = "登录中…";
  try {
    log(`${restoring ? "恢复" : "门户"}登录 ${account}...`);
    const data = restoring
      ? await api("/api/login/saved", { account })
      : await api("/api/login", { account, password });
    loginStatus("登录成功，正在加载账号信息…", "ok");
    registerAccount(data);
    logTo(data.account, `账号 ${data.account} 已连接`, "ok");
    ensureAccountStarted(data.account);
  } catch (e) {
    loginStatus(`登录失败：${e.message}`, "err");
  } finally {
    $("loginBtn").disabled = false;
    $("loginBtn").textContent = "登录";
    $("password").value = "";
  }
}

// ---------- 账号登记 / 切换 / 移除 ----------
// 把后端登录负载写入前端账号表。
function rememberAccount(data) {
  const email = data.account;
  ACCOUNTS[email] = {
    status: data.status || {},
    nextClaims: data.nextClaims || {},
    arena: null,                 // 展开竞技场面板时才拉（/api/arena/options）
    toggles: data.toggles || {},
    params: data.params || {},
    guild: data.guild || null,
    name: (data.status || {}).name,
    avatar: (data.status || {}).avatar,
    logHtml: "",
    logSeq: 0,
    logUnread: 0,
    stateRevision: data.stateRevision || 1,
    stateSyncedAt: data.stateSyncedAt || null,
    stateSyncError: data.stateSyncError || null,
    inventoryTargetEpoch: data.inventoryEpoch || null,
    inventoryTargetRevision: data.inventoryRevision || 0,
    inventoryEpoch: null,
    inventoryRevision: 0,
    inventoryRetiredEpochs: new Set(),
    redeemCodes: data.redeemCodes || { codes: [], lastUpdated: null, refreshError: null },
    connection: data.connection || { connected: true, reason: null, changedAt: null },
    duplicateLoginLogged: false,
    duplicateLoginNotified: false,
    activationStarted: false,
    activationPending: false,
    activationRetryAt: 0,
    activationError: "",
  };
}

// 用户主动登录成功后登记账号并切换过去。
function registerAccount(data) {
  rememberAccount(data);
  const email = data.account;
  enterApp();
  switchAccount(email);
  ensureAutoLogPoll();
  // 刷新存档列表（下次添加账号时该账号出现在"已保存账号"里）
  api("/api/accounts").then((r) => { SAVED_ACCTS = r.accounts || []; }).catch(() => {});
}

async function ensureAccountStarted(email) {
  const account = ACCOUNTS[email];
  if (!account || account.activationStarted || account.activationPending
      || account.connection?.connected === false || Date.now() < account.activationRetryAt) return;
  account.activationPending = true;
  try {
    // Let the main view paint before background startup can produce task logs.
    await new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0)));
    if (ACCOUNTS[email] !== account || account.connection?.connected === false) return;
    await api("/api/accounts/start", { account: email });
    if (ACCOUNTS[email] !== account) return;
    account.activationStarted = true;
    account.activationError = "";
  } catch (error) {
    if (ACCOUNTS[email] !== account) return;
    account.activationRetryAt = Date.now() + 5000;
    const message = error?.message || "未知错误";
    if (account.activationError !== message) {
      account.activationError = message;
      logTo(email, `后台任务启动失败，将自动重试：${message}`, "err");
    }
  } finally {
    account.activationPending = false;
  }
}

function renderConnectionState(email = activeAccount) {
  const pill = $("connPill");
  if (!pill) return;
  const account = ACCOUNTS[email];
  const state = account?.connection || { connected: true };
  const connected = state.connected !== false;
  pill.classList.toggle("on", !!account && connected);
  pill.classList.toggle("off", !!account && !connected);
  $("connStatus").textContent = account ? (connected ? "已连接" : "已断开") : "未连接";
  pill.title = !account ? "尚未登录" : connected ? "游戏会话正常"
    : state.reason === "duplicate_login" ? "重复登录，当前会话已断开" : "游戏会话已断开";
  const refreshing = !!account?.refreshPending;
  const retry = $("reconnectBtn");
  if (retry) {
    retry.disabled = !account || refreshing;
    retry.classList.toggle("loading", refreshing);
    retry.setAttribute("aria-busy", String(refreshing));
  }
  const refresh = $("refreshBtn");
  if (refresh) {
    refresh.disabled = !account || refreshing;
    refresh.classList.toggle("spinning", refreshing);
  }
}

function applyConnectionState(email, state, { notify = false } = {}) {
  const account = ACCOUNTS[email];
  if (!account || !state) return;
  const rawChangedAt = Number(state.changedAt);
  const next = {
    connected: state.connected !== false,
    reason: state.reason || null,
    changedAt: Number.isFinite(rawChangedAt) && rawChangedAt > 0 ? rawChangedAt : null,
  };
  const previous = account.connection || { connected: true };
  account.connection = next;
  updateAccountConnectionIndicator(email);
  if (email === activeAccount) renderConnectionState(email);
  const duplicate = !next.connected && next.reason === "duplicate_login";
  if (duplicate) {
    if (previous.connected !== false && !account.duplicateLoginLogged) {
      account.duplicateLoginLogged = true;
      // Android WebView may remain frozen in the background for a long time.
      // Use the backend transition time instead of the foreground/poll time.
      logTo(email, "[重复登录] 当前连接已断开", "err", next.changedAt);
    }
    if (notify && !account.duplicateLoginNotified) {
      account.duplicateLoginNotified = true;
      showMessageToast("[重复登录] 当前连接已断开", "err", 5200);
    }
  } else if (next.connected) {
    account.duplicateLoginLogged = false;
    account.duplicateLoginNotified = false;
  }
}

// 从登录视图切到应用视图
function enterApp() {
  $("loginView").classList.add("hidden");
  $("appView").classList.remove("hidden");
  renderConnectionState();
}

// 切换当前查看的账号：刷新状态/开关/日志/切换器。
function switchAccount(email) {
  if (!ACCOUNTS[email]) return;
  closeAbyssPicker(false);
  const preservePage = !!ACCOUNTS[activeAccount];
  activeAccount = email;
  try { sessionStorage.setItem(TEST_ACTIVE_ACCOUNT_SS, email); } catch { /* optional */ }
  const a = ACCOUNTS[email];
  a.logUnread = 0;
  renderConnectionState(email);
  NEXT_CLAIMS = a.nextClaims || {};
  renderStatus(a.status || {});
  loadRoster(email);           // 团员/物品清单（按账号缓存，首次拉取）
  loadTasks();                 // 重渲染开关/手动面板（反映该账号的 toggles/params）
  // 恢复该账号日志到完整日志页和底部日志抽屉。
  renderLogViews(a.logHtml || "");
  logUnread = 0; updateLogBadge();
  renderAccountSwitcher();
  renderDevConsole();          // 控制台的 route/data 输入是按账号记的，切号要重载
  // 已登录账号间切换只更新账号数据，保留当前板块和工具箱子页面。
  if (!preservePage) showSection("info");
  else if (window.innerWidth <= 820) setDrawer(false);
  window.dispatchEvent(new CustomEvent("lucima:account-change", { detail: { account: email } }));
}

// 下线（保留存档，可再连接）
async function accLogout(email) {
  try { await api("/api/accounts/logout", { account: email }); } catch (e) {}
  delete ACCOUNTS[email];
  afterAccountGone(email);
}

// 删除账号（下线 + 清存档）
async function accDelete(email) {
  try { await api("/api/accounts/delete", { account: email }); } catch (e) {}
  SAVED_ACCTS = SAVED_ACCTS.filter((a) => a.account !== email);
  delete ACCOUNTS[email];
  afterAccountGone(email);
}

// 某账号从在线列表移除后：切到另一个在线账号，或回到登录页
function afterAccountGone(email) {
  if (activeAccount === email) activeAccount = null;
  const rest = Object.keys(ACCOUNTS);
  if (rest.length) {
    switchAccount(rest[0]);
  } else {
    activeAccount = null;
    window.dispatchEvent(new CustomEvent("lucima:account-change", { detail: { account: null } }));
    $("appView").classList.add("hidden");
    $("loginView").classList.remove("hidden");
    $("loginCancel") && $("loginCancel").classList.add("hidden");
    const el = $("log"); if (el) el.innerHTML = "";
    renderSavedAccounts();
    // Returning to the login page starts a fresh update gate as well.
    runStartupUpdateCheck();
  }
  renderAccountSwitcher();
}

// 装备掉落卡片缓存（日志里的可点击链接 → 弹窗查看）。key 递增。
const EQUIP_DROPS = {};
let equipDropSeq = 0;

// 统一按结果输出日志：有 lines 就逐条，否则单条 detail。
// line 可以是字符串，也可以是 {type:"drop", text, equips:[{text,card}]}（掉落：道具文字
// + 装备名合成一条，每个装备名渲染成可点击链接 → 弹详情卡片）。
// email 缺省=当前账号；后台轮询把别的账号的结果写进各自缓冲。
function logResult(r, prefix = "", email = activeAccount, ts = null) {
  const cls = r.ok ? "ok" : "err";
  const lines = r.lines && r.lines.length ? r.lines : [r.detail];
  lines.forEach((ln) => {
    if (ln == null || (typeof ln === "string" && !ln.trim())) return;
    // 掉落行：道具文字 + 装备名，**合成一条**（装备名各自可点击展开详情卡片）
    if (ln && typeof ln === "object" && ln.type === "drop") {
      const links = (ln.equips || []).map((e) => {
        const key = `eqd${++equipDropSeq}`;
        EQUIP_DROPS[key] = e.card;
        const rarityClass = e.card?.is85Legendary === true ? " equip-link-legendary" : "";
        return `<a class="equip-link${rarityClass}" data-eqd="${key}">${e.text} ▸</a>`;
      });
      // 道具和装备并成同一个"、"列表：获得 30500金币、18星源粉末、85级传说烈焰晶石爪 ▸
      const parts = ln.text ? [ln.text, ...links] : links;
      logTo(email, `[${prefix}${r.name}] 获得 ${parts.join("、")}`, cls, ts);
    } else {
      logTo(email, `[${prefix}${r.name}] ${ln}`, cls, ts);
    }
  });
}

function fmtNum(n) {
  if (n == null) return "—";
  return Number(n).toLocaleString("en-US");
}

// 表盘资源（随时间恢复型）：能源 / 旗帜（能源=游戏官方名，功能即旧称"体力"）
const GAUGE_META = [
  { key: "stamina", label: "能源", color: "#4ce68a", icon: "/assets/icons/stamina.png" },
  { key: "flag", label: "旗帜", color: "#ff6b9d", icon: "/assets/icons/flag.png" },
];
// 平铺资产（图标 + 名称 + 数量）
const CUR_META = [
  { key: "gold", label: "金币", icon: "/assets/icons/gold.png" },
  { key: "diamond", label: "钻石", icon: "/assets/icons/diamond.png" },
  { key: "battery", label: "能源电池", icon: "/assets/items/37.png" },
  { key: "recruit", label: "招募契约", icon: "/assets/icons/recruit.png" },
  { key: "mystery", label: "神秘契约", icon: "/assets/icons/mystery.png" },
  { key: "galaxy", label: "银河契约", icon: "/assets/icons/galaxy.png" },
];

// 单个表盘：细环 + 刻度 + 进度端点光珠，中心放物品图标与数值
function gaugeHTML({ key, label, color, icon }, g) {
  const cur = g.current ?? 0;
  const max = g.max || 0;
  // 溢出（能源可超上限）按 100% 画满，另标记溢出色
  const pct = max > 0 ? Math.min(100, Math.round((cur / max) * 100)) : 0;
  const over = max > 0 && cur > max;
  const dc = over ? "var(--warn)" : color;
  return `<div class="dial">
    <div class="dial-ring" style="--pct:${pct}; --dc:${dc}">
      <i class="dial-tip"></i>
      <div class="dial-core">
        <img class="dial-icon" src="${icon}" alt="${label}">
        <div class="dial-val">${fmtNum(cur)}${max ? `<span>/${fmtNum(max)}</span>` : ""}</div>
      </div>
    </div>
    <div class="dial-label">${label}</div>
  </div>`;
}

function renderStatus(s) {
  $("avatar").innerHTML = s.avatar
    ? `<img src="${s.avatar}" alt="${s.name || ""}">`
    : (s.name || "?").slice(0, 1);
  $("pName").textContent = s.name || "—";
  $("pUid").textContent = `UID ${s.cuid ?? "—"}`;
  // 等级：名字下方一行纯文字（无框）
  $("pLv").textContent = s.level != null ? `LV ${s.level}` : "—";

  // 能源 / 旗帜：表盘
  $("gaugeRow").innerHTML = GAUGE_META.map((m) => gaugeHTML(m, s[m.key] || {})).join("");

  // 资源概览：左列金币/钻石/能源电池，右列三种契约
  const cur = s.currencies || {};
  $("curBar").innerHTML = CUR_META.map(({ key, label, icon }) => `<div class="asset" title="${label}">
    <div class="asset-ic"><img src="${icon}" alt="${label}"></div>
    <div class="asset-val">${fmtNum(cur[key])}</div>
  </div>`).join("");
}

// ---------- 团员 / 物品清单 ----------
// 潜能等级图标（ImprintLV→字形：D/C/B/A/S/SS/SSS）。素材已复制进本项目 assets。
const GRADE_ICON = "/assets/potential/";
let rosterTab = "heroes";  // 当前 tab
let heroElement = "all";
let heroJob = "ALL";
const HERO_ELEMENTS = [
  { id: "all", icon: "/assets/ui/hero-all.png", label: "全部" },
  { id: "Fire", icon: "/assets/attributes/fire.png", label: "火属性" },
  { id: "Ice", icon: "/assets/attributes/water.png", label: "水属性" },
  { id: "Earth", icon: "/assets/attributes/wood.png", label: "木属性" },
  { id: "Light", icon: "/assets/attributes/light.png", label: "光属性" },
  { id: "Dark", icon: "/assets/attributes/dark.png", label: "暗属性" },
];
const HERO_ELEMENT_IDS = new Set(HERO_ELEMENTS.map((element) => element.id));
let itemCategory = "wealth";
const ITEM_CATEGORIES = [
  { id: "enhancement", label: "强化芯片" },
  { id: "element", label: "元素" },
  { id: "crystal", label: "晶石" },
  { id: "activity", label: "活动道具" },
  { id: "catalyst", label: "催化剂" },
  { id: "wealth", label: "财物" },
  { id: "other", label: "其他" },
];
const ITEM_CATEGORY_IDS = new Set(ITEM_CATEGORIES.map((category) => category.id));
let ROLE_ELEMENTS_DATA = null;

async function enrichRosterElements(roster) {
  if (!roster?.heroes?.length || roster.heroes.every((hero) => hero.element)) return roster;
  try {
    if (!ROLE_ELEMENTS_DATA) {
      const response = await fetch("/assets/role_elements.json", { cache: "no-store" });
      if (!response.ok) return roster;
      ROLE_ELEMENTS_DATA = await response.json();
    }
    return {
      ...roster,
      heroes: roster.heroes.map((hero) => ({
        ...hero,
        element: hero.element || ROLE_ELEMENTS_DATA[hero.id],
      })),
    };
  } catch {
    return roster;
  }
}

// Inventory versions are separate from full account-state refreshes. Only
// settled item balances cross this channel; mail previews never enter it.
function observeInventoryVersion(email, data, schedule = true) {
  const a = ACCOUNTS[email];
  const epoch = data?.inventoryEpoch, revision = data?.inventoryRevision;
  if (!a || typeof epoch !== "string" || !epoch || !Number.isSafeInteger(revision) || revision < 0) return false;
  a.inventoryRetiredEpochs ||= new Set();
  if (a.inventoryRetiredEpochs.has(epoch)) return false;
  if (a.inventoryTargetEpoch && a.inventoryTargetEpoch !== epoch) {
    a.inventoryRetiredEpochs.add(a.inventoryTargetEpoch);
    a.inventoryTargetRevision = 0;
  }
  a.inventoryTargetEpoch = epoch;
  a.inventoryTargetRevision = Math.max(a.inventoryTargetRevision || 0, revision);
  if (schedule) syncInventory(email);
  return true;
}

function applyInventorySnapshot(email, snapshot) {
  const a = ACCOUNTS[email];
  if (!a?.roster || !snapshot || !Array.isArray(snapshot.items)
      || !observeInventoryVersion(email, {
        inventoryEpoch: snapshot.epoch, inventoryRevision: snapshot.revision,
      }, false)) return false;
  const sameEpoch = a.inventoryEpoch === snapshot.epoch;
  if ((sameEpoch && snapshot.revision < a.inventoryRevision) || (!sameEpoch && !snapshot.full)) return false;
  const byId = new Map((snapshot.full ? [] : a.roster.items || []).map(item => [String(item.id), item]));
  for (const id of snapshot.removed || []) byId.delete(String(id));
  for (const item of snapshot.items) {
    if (item.count > 0) byId.set(String(item.id), item);
    else byId.delete(String(item.id));
  }
  a.roster.items = [...byId.values()].sort((left, right) =>
    (right.rank || 0) - (left.rank || 0)
    || (String(left.id) < String(right.id) ? -1 : String(left.id) > String(right.id) ? 1 : 0));
  a.inventoryEpoch = snapshot.epoch;
  a.inventoryRevision = snapshot.revision;
  if (email === activeAccount) updateRosterItems(a.roster.items);
  return true;
}

function syncInventory(email) {
  const a = ACCOUNTS[email];
  if (!a?.roster || !a.inventoryTargetEpoch) return Promise.resolve();
  if (a.inventoryPending) return a.inventoryPending;
  if (a.inventoryEpoch === a.inventoryTargetEpoch && a.inventoryRevision >= a.inventoryTargetRevision) return Promise.resolve();
  a.inventoryPending = (async () => {
    try {
      while (ACCOUNTS[email] === a && (a.inventoryEpoch !== a.inventoryTargetEpoch
          || a.inventoryRevision < a.inventoryTargetRevision)) {
        const query = new URLSearchParams({ account: email });
        if (a.inventoryEpoch) {
          query.set("epoch", a.inventoryEpoch);
          query.set("since", String(a.inventoryRevision));
        }
        const data = await api(`/api/inventory?${query}`);
        if (ACCOUNTS[email] !== a) return;
        if (!applyInventorySnapshot(email, data.inventory)) return;
        if (data.status) applyStatus(email, data.status);
      }
    } catch { /* Retry from the last applied version on the next poll. */ }
    finally { a.inventoryPending = null; }
  })();
  return a.inventoryPending;
}

// 团员按需加载；物品从这次完整列表的版本继续同步。
async function loadRoster(email, force = false) {
  const a = ACCOUNTS[email];
  if (!a) return;
  if (a.roster && !force) {
    const cachedRoster = a.roster;
    const enriched = await enrichRosterElements(cachedRoster);
    if (ACCOUNTS[email] !== a) return;
    if (a.roster === cachedRoster) a.roster = { ...enriched, items: a.roster.items };
    if (email === activeAccount) renderRoster(a.roster);
    syncInventory(email);
    return;
  }
  const loadSeq = a.rosterLoadSeq = (a.rosterLoadSeq || 0) + 1;
  try {
    const r = await enrichRosterElements(await api(`/api/roster?account=${encodeURIComponent(email)}`));
    if (ACCOUNTS[email] !== a || a.rosterLoadSeq !== loadSeq) return;
    const previousItems = a.roster?.items || [];
    a.roster = { ...r, items: previousItems };
    if (r.inventoryEpoch) {
      applyInventorySnapshot(email, {
        epoch: r.inventoryEpoch, revision: r.inventoryRevision,
        full: true, items: r.items || [], removed: [],
      });
    } else a.roster.items = r.items || [];
    if (email === activeAccount) renderRoster(a.roster);
    syncInventory(email);
  } catch (e) {
    if (ACCOUNTS[email] === a && a.rosterLoadSeq === loadSeq && email === activeAccount) {
      $("rosterHeroes").innerHTML = `<div class="roster-empty">加载失败：${e.message}</div>`;
      $("rosterItems").innerHTML = `<div class="roster-empty">加载失败：${e.message}</div>`;
    }
  }
}

function renderRoster(r) {
  const heroes = r.heroes || [], items = r.items || [];
  $("heroCount").textContent = heroes.length;
  $("itemCount").textContent = items.length;

  renderRosterHeroes(heroes);
  renderRosterItems(items);

  // 应用当前 tab 可见性
  applyRosterTab();
}

function heroElementOf(hero) {
  return HERO_ELEMENT_IDS.has(hero.element) ? hero.element : "other";
}

function heroPortraitHTML(h) {
  const initial = filterEscape(String(h.name || h.id || "?").slice(0, 1));
  const avatar = h.avatar
    ? `<img src="${filterEscape(h.avatar)}" alt="" loading="lazy" data-initial="${initial}" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'hero-ph',textContent:this.dataset.initial}))">`
    : `<span class="hero-ph">${initial}</span>`;
  const grade = h.grade
    ? `<img class="hero-grade" src="${GRADE_ICON}${filterEscape(h.grade)}.png" alt="${filterEscape(h.grade)}" title="潜能 ${filterEscape(h.grade)}">` : "";
  const level = h.level != null ? `<span class="hero-lv">${filterEscape(h.level)}</span>` : "";
  return `<span class="hero-pic">${avatar}${level}${grade}</span>`;
}

function heroCardHTML(h) {
  return `<div class="hero-card">${heroPortraitHTML(h)}<div class="hero-name">${filterEscape(h.name || h.id)}</div></div>`;
}

function renderRosterHeroes(heroes) {
  const selected = heroes.filter((hero) =>
    (heroElement === "all" || heroElementOf(hero) === heroElement)
    && (heroJob === "ALL" || hero.job === heroJob));
  const tabs = HERO_ELEMENTS.map(({ id, icon, label }) => `
    <button type="button" class="hero-element-tab ${id === heroElement ? "active" : ""}"
      data-hero-element="${id}" title="${label}" aria-label="${label}" aria-selected="${id === heroElement}" role="tab">
      <img src="${icon}" alt="">
    </button>`).join("");
  const jobs = ENCYCLOPEDIA_JOBS.map(({ id, icon, label }) => `
    <button type="button" class="hero-job-tab ${id === heroJob ? "active" : ""}"
      data-hero-job="${id}" title="${id === "ALL" ? "全部职业" : label}" aria-label="${id === "ALL" ? "全部职业" : label}" aria-selected="${id === heroJob}" role="tab">
      <img class="task-icon-light-invert" src="${icon}" alt="">
    </button>`).join("");
  const grid = selected.length
    ? selected.map(heroCardHTML).join("")
    : `<div class="roster-empty hero-element-empty">暂无符合筛选条件的团员</div>`;
  $("rosterHeroes").innerHTML = `
    <div class="hero-filters">
      <div class="hero-filter-row"><span class="hero-filter-label">属性</span><div class="hero-element-tabs" role="tablist" aria-label="团员属性">${tabs}</div></div>
      <div class="hero-filter-row"><span class="hero-filter-label">职业</span><div class="hero-job-tabs" role="tablist" aria-label="团员职业">${jobs}</div></div>
    </div>
    <div class="hero-grid">${grid}</div>`;
  $("rosterHeroes").querySelectorAll(".hero-element-tab").forEach((button) => {
    button.addEventListener("click", () => {
      heroElement = button.dataset.heroElement;
      renderRosterHeroes(heroes);
    });
  });
  $("rosterHeroes").querySelectorAll(".hero-job-tab").forEach((button) => {
    button.addEventListener("click", () => {
      heroJob = button.dataset.heroJob;
      renderRosterHeroes(heroes);
    });
  });
}

function itemCategoryOf(item) {
  if (ITEM_CATEGORY_IDS.has(item.category)) return item.category;
  const id = String(item.id || "");
  if (id === "38" || /^(?:EC|AC|UEC|UAC)/.test(id)) return "enhancement";
  if (id === "74" || /^R\d+$/.test(id)) return "element";
  if (/^CR/.test(id)) return "crystal";
  if (/^AH/.test(id)) return "activity";
  if (/^C\d+$/.test(id)) return "catalyst";
  return /^\d+$/.test(id) ? "wealth" : "other";
}

function itemTileHTML(it) {
  return `
    <div class="item-tile r${it.rank || 0}" data-item-id="${it.id}" title="${it.name || it.id}">
      <div class="item-ic"><img src="/assets/items/${it.id}.png" alt="" loading="lazy"
        onerror="this.style.display='none';this.parentNode.classList.add('noimg');this.parentNode.dataset.t=('${(it.name || it.id)}').slice(0,2)"></div>
      <div class="item-cnt">${fmtNum(it.count)}</div>
    </div>`;
}

function renderRosterItems(items) {
  const grouped = Object.fromEntries(ITEM_CATEGORIES.map(({ id }) => [id, []]));
  items.forEach((item) => grouped[itemCategoryOf(item)].push(item));
  const selectedItems = grouped[itemCategory] || [];
  const tabs = ITEM_CATEGORIES.map(({ id, label }) => `
    <button type="button" class="item-category-tab ${id === itemCategory ? "active" : ""}"
      data-item-category="${id}" role="tab" aria-selected="${id === itemCategory}">
      <span>${label}</span>
    </button>`).join("");
  const grid = selectedItems.length
    ? selectedItems.map(itemTileHTML).join("")
    : `<div class="roster-empty item-category-empty">暂无此类物品</div>`;
  $("rosterItems").innerHTML = `
    <div class="item-category-tabs" role="tablist" aria-label="物品分类">${tabs}</div>
    <div class="item-category-grid">${grid}</div>`;
  $("rosterItems").querySelectorAll(".item-category-tab").forEach((button) => {
    button.addEventListener("click", () => {
      itemCategory = button.dataset.itemCategory;
      renderRosterItems(ACCOUNTS[activeAccount]?.roster?.items || items);
    });
  });
}

// Keep category controls, scroll position and unchanged image nodes in place
// when a balance changes. A gain/loss of an item only rebuilds the item grid.
function updateRosterItems(items) {
  $("itemCount").textContent = items.length;
  const root = $("rosterItems");
  const grid = root.querySelector(".item-category-grid");
  if (!grid) return renderRosterItems(items);
  const grouped = Object.fromEntries(ITEM_CATEGORIES.map(({ id }) => [id, []]));
  items.forEach(item => grouped[itemCategoryOf(item)].push(item));
  const visible = grouped[itemCategory] || [];
  const tiles = [...grid.querySelectorAll(".item-tile")];
  if (tiles.length !== visible.length || tiles.some((tile, index) => tile.dataset.itemId !== String(visible[index].id))) {
    const scrollTop = grid.scrollTop;
    grid.innerHTML = visible.length ? visible.map(itemTileHTML).join("")
      : `<div class="roster-empty item-category-empty">暂无此类物品</div>`;
    grid.scrollTop = scrollTop;
  } else {
    tiles.forEach((tile, index) => {
      const item = visible[index], count = tile.querySelector(".item-cnt");
      const label = fmtNum(item.count);
      if (count.textContent !== label) count.textContent = label;
      tile.title = item.name || item.id;
      tile.className = `item-tile r${item.rank || 0}`;
    });
  }
}

function applyRosterTab() {
  document.querySelectorAll(".rtab").forEach((b) =>
    b.classList.toggle("active", b.dataset.rtab === rosterTab));
  $("rosterHeroes").classList.toggle("active", rosterTab === "heroes");
  $("rosterItems").classList.toggle("active", rosterTab === "items");
}

// ---------- 任务 ----------
let TASKS = [];
let TOGGLE_TASKS = [];
const TEST_ACTIVE_ACCOUNT_SS = "ark_test_active_account";
const TASK_ICONS = {
  month_signin: "📅", daily_achievement: "🎯", claim_mail: "✉️", friend_support: "🤝",
  reactor: "⚡", sf_potion: "🧪", sf_starforce: "⭐", sf_tesseract: "🔷",
  dispatch: "🎖️", shop_free: "🛒", shop_diamond: "💎", hunt_sweep: "⚔️", element_sweep: "🔮",
  activity_scene: "🎫", abyss_sweep: "🌌", free_summon: "🎰", store_buy: "🛍️",
  star_source_shop: "🌈", customized_equip: "🔧",
};
// 部分任务改用游戏内物品图标（覆盖上面的 emoji）：动力转换=钻石、成长药剂=传说成长药剂、
// 升星水晶=传说升星水晶、技能模块=技能模块、派遣任务=荣誉勋章、
// 秘密商店刷新(免费)=金币、虚拟幻境=星源粉末、每日免费招募=招募契约、商店购买=荣誉勋章、
// 竞技场NPC=旗帜（挑战就是扣旗帜，与基本信息页旗帜表盘同一个 StaticID 4）、每日体力=体力
const TASK_ICON_IMG = {
  send_meal: "3", reactor: "2", sf_potion: "103", sf_starforce: "106",
  sf_tesseract: "34", dispatch: "26", shop_free: "1",
  shop_diamond: "2", abyss_sweep: "25", free_summon: "5", store_buy: "26",
  arena_npc: "4", star_source_shop: "109",
};
const TASK_ICON_UI = {
  month_signin: "UI05_Icon_Monthly.png",
  daily_achievement: "DailyPoint.png",
  redeem_code: "GiftPack.png",
  claim_mail: "mail.png",
  hunt_sweep: "hunt.png",
  element_sweep: "element.png",
  activity_scene: "activity.png",
  store_buy: "store.png",
  customized_equip: "customized-equip.png",
};
// 生成任务图标 HTML：指定的 SVG 和界面图标优先，其次物品图标，最后回退 emoji/◆。
function taskIconHTML(id, fallback = "◆") {
  if (id === "abyss_auto") return '<svg class="ticon-svg" aria-hidden="true"><use href="#ui-mysterious-realm"></use></svg>';
  const uiIcon = TASK_ICON_UI[id];
  const lightThemeClass = ["month_signin", "claim_mail", "hunt_sweep", "element_sweep", "activity_scene", "store_buy"].includes(id)
    ? " task-icon-light-invert"
    : "";
  if (uiIcon) return `<img class="ticon-img${lightThemeClass}" src="/assets/ui/${uiIcon}" alt="" loading="lazy">`;
  const sid = TASK_ICON_IMG[id];
  if (sid) return `<img class="ticon-img" src="/assets/items/${sid}.png" alt="" loading="lazy">`;
  return TASK_ICONS[id] || fallback;
}
// 自动购买目标可选项
// 购买目标：一个选项(key)可对应一个或多个物品 StaticID
// stat = 统计汇总用的短名（label 是勾选框文案，芯片那项带部位说明，汇总里不合适）
// 自动购买目标：**由后端 /api/tasks 下发**（backend/tasks.py BUY_TARGETS 是唯一真源）。
// ⚠️ 不要在这里写死一份。曾经前后端各存一份默认值——前端缺省全选、后端缺省只有
// 招募/神秘契约——界面上"强化芯片"明明打着勾，后端却从不买芯片。
// loadTasks() 之前为空数组：此时购买目标 UI 还没渲染，也不会有任务在跑。
let BUY_TARGETS = [];
// ===== 多账号状态 =====
// 后端持有每个账号的会话/开关/调度线程；前端只维护"当前查看哪个账号"+各账号的展示态。
// ACCOUNTS[email] = {status, nextClaims, toggles, params, logHtml, logSeq, logUnread, name, avatar}
let ACCOUNTS = {};
let activeAccount = null;      // 当前查看的账号 email
let countdownTimer = null;     // 倒计时文案就地重算（每 30s，不发请求）
let autoLogTimer = null;       // 后台日志轮询（每 1s，拉所有在线账号的新巡检日志）
const AUTO_LOG_POLL_MS = 1000;
let autoLogPollPending = false;
// NEXT_CLAIMS 是"当前账号"nextClaims 的镜像，供 renderNextClaims 复用
let NEXT_CLAIMS = {};

function acc(email) { return ACCOUNTS[email || activeAccount]; }
function activeAcc() { return ACCOUNTS[activeAccount]; }

// 绝对时刻 -> "MM-DD H:MM"（本地时区，月日补零、小时不补）
function fmtClock(ms) {
  const t = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(t.getMonth() + 1)}-${p(t.getDate())} ${t.getHours()}:${p(t.getMinutes())}`;
}

// 剩余毫秒 -> "1天10小时20分"。超过 24h 进位成天（技能模块间隔 7 天，168小时没法读），
// 为 0 的单位省略；不足 1 分钟按 1 分显示（免得写成"0分"）。
// 先按分钟四舍五入再拆：直接 floor 会把差几毫秒的整点砍掉一档（25h 渲染成"1天59分"）
function fmtRemain(diff) {
  const total = Math.max(1, Math.round(diff / 60000));
  const d = Math.floor(total / 1440);
  const h = Math.floor((total % 1440) / 60);
  const m = total % 60;
  if (!d && !h) return `${m}分`;
  return (d ? `${d}天` : "") + (h ? `${h}小时` : "") + (m ? `${m}分` : "");
}

// 下次产出档期文案：可领取 / 下次领取时间: 07-29 8:00 (10小时20分)
// blocked（条件不满足，开关置灰）优先；失败重试展示倒计时，其余 note 仍优先于时间文案
// 免费刷商店说"刷新"而不是"领取"（它不是产出型任务）。档期来自货架
// LastResetTime + 1h1s，见 backend/tasks.secret_shop_next_free。
const NEXT_WORDS = {
  month_signin: { ready: "可签到", next: "下次签到时间" },
  send_meal: { ready: "可发放至邮箱", next: "下次发放时间" },
  shop_free: { ready: "可免费刷新", next: "下次免费刷新" },
};
function fmtNextClaim(nc, taskId = "") {
  if (!nc) return "";
  if (nc.blocked) return nc.blocked;
  if (nc.note && !nc.retrying) return nc.note;
  let w = NEXT_WORDS[taskId] || { ready: "可领取", next: "下次领取时间" };
  if (taskId === "send_meal" && nc.confirming) {
    w = { ready: "待确认体力邮件", next: "下次邮件检查" };
  } else if (taskId === "send_meal" && nc.retrying) {
    w = { ...w, next: "下次检查" };
  } else if (nc.retrying) {
    w = { ready: "可重试", next: "下次重试" };
  }
  if (nc.nextMs == null) return nc.ready ? w.ready : "";
  const diff = nc.nextMs - Date.now();
  if (diff <= 0) return w.ready;
  return `${w.next}: ${fmtClock(nc.nextMs)} (${fmtRemain(diff)})`;
}

// 技能模块的"加速"档期（每生产周期 3 次、间隔 24h）单独一行展示
function fmtCharge(c) {
  const q = `已用 ${c.used}/${c.max}`;
  if (c.exhausted) return `加速已用完 ${c.used}/${c.max}`;
  if (c.note) return `${c.note} · ${q}`;
  if (c.retrying) return `加速等待重试 · ${q}`;
  const diff = c.nextMs == null ? 0 : c.nextMs - Date.now();
  if (diff <= 0) return `可加速 · ${q}`;
  return `下次加速时间: ${fmtClock(c.nextMs)} (${fmtRemain(diff)}) · ${q}`;
}

// 把每个开关旁的"下次可领"文案刷新（读 NEXT_CLAIMS 内存态，不发请求）
// 同时处理 blocked：条件不满足的任务把整行置灰、开关禁用（原因显示在文案里）
function renderNextClaims() {
  document.querySelectorAll("#autoList .auto-item").forEach((el) => {
    const nc = NEXT_CLAIMS[el.dataset.id];
    const slot = el.querySelector(".tnext");
    const box = el.querySelector(".switch-in");
    const blocked = !!(nc && nc.blocked);
    el.classList.toggle("blocked", blocked);
    if (box) box.disabled = blocked;
    if (!slot) return;
    if (!nc) { slot.innerHTML = ""; return; }
    const cls = blocked ? " warn" : (nc.ready ? " ready" : "");
    const note = el.dataset.id === "daily_achievement" && !blocked && !nc.retrying && Array.isArray(nc.noteParts)
      ? nc.noteParts.map((part) => `<span class="achievement-note-part">${filterEscape(part)}</span>`).join("")
      : fmtNextClaim(nc, el.dataset.id);
    const rows = [`<span class="nc-row${cls}">${note}</span>`];
    if (nc.charge) {
      const on = !nc.charge.exhausted && nc.charge.ready;
      rows.push(`<span class="nc-row${on ? " ready" : ""}">${fmtCharge(nc.charge)}</span>`);
    }
    slot.innerHTML = rows.join("");
  });
  // 竞技场展开页里每个 NPC 的倒计时也要跟着走（同样只重算文案、不发请求）
  document.querySelectorAll("#autoList .auto-item.has-body").forEach((el) => {
    if (el.querySelector(".arena-row")) renderArenaList(el.dataset.id);
  });
}

// 开关/参数改由后端按账号持有。前端读当前账号缓存，改动即 POST /api/toggles 同步。
function getToggles() { const a = activeAcc(); return (a && a.toggles) || {}; }
function getAccParams() { const a = activeAcc(); return (a && a.params) || {}; }

// 把当前账号的开关+参数同步到后端（持久化 + 后台调度据此执行）
async function syncToggles() {
  const a = activeAcc(); if (!a) return;
  try { await api("/api/toggles", { toggles: a.toggles, params: a.params }); }
  catch (e) { log(`保存开关失败：${e.message}`, "err"); }
}

// 选中的自动购买目标 key（存当前账号 params.shop_free.wanted 里，转成 key 展示）
function getWantedKeys() {
  const p = getAccParams();
  const ids = (p.shop_free && p.shop_free.wanted) || null;
  if (!Array.isArray(ids)) return BUY_TARGETS.map((b) => b.key);  // 默认全选
  return BUY_TARGETS.filter((b) => b.ids.some((x) => ids.includes(x))).map((b) => b.key);
}
function setWantedKeys(keys) {
  const a = activeAcc(); if (!a) return;
  const ids = [];
  BUY_TARGETS.forEach((b) => { if (keys.includes(b.key)) ids.push(...b.ids); });
  a.params = { ...a.params, shop_free: { ...(a.params.shop_free || {}), wanted: ids } };
  syncToggles();
}
function wantedIds() {
  const keys = getWantedKeys();
  const ids = [];
  BUY_TARGETS.forEach((b) => { if (keys.includes(b.key)) ids.push(...b.ids); });
  return ids;
}

async function loadTasks() {
  const { toggle, manual, buyTargets } = await api("/api/tasks");
  TOGGLE_TASKS = toggle;
  TASKS = manual;
  // 购买目标表以后端为准；缺失就报错，不本地兜一份默认值（那正是漏买芯片的根因）
  if (!Array.isArray(buyTargets) || !buyTargets.length) {
    log("购买目标表加载失败（后端未返回 buyTargets），自动购买目标不可用", "err");
  } else {
    BUY_TARGETS = buyTargets;
  }
  renderAutoPanel(toggle);
  renderManualPanel(manual);
}

// 自动功能面板：开关；带额外配置的任务在自身选项中展示。
function renderAutoPanel(toggle) {
  const saved = getToggles();
  $("autoList").innerHTML = toggle
    .map((t) => {
      const on = saved[t.id] ?? false;
      const inner = `<div class="ticon">${taskIconHTML(t.id)}</div>
        <div class="tinfo">
          <div class="tname">${t.name}</div>
          <div class="tdesc">${t.desc}</div>
          <div class="tnext"></div>
        </div>`;
      // 带展开页的开关：外层必须是 div 而不是 label
      // ——<button>（"选项 ▸"）不能放在 <label> 里，点它会连带触发开关。
      // 开关本体单独包一层 <label> 保持点击区。
      if (t.ui === "arena" || t.ui === "achievement" || t.ui === "redeem_code") {
        const achievement = t.ui === "achievement";
        const redeemCode = t.ui === "redeem_code";
        return `<div class="auto-item has-body ${on ? "on" : ""}" data-id="${t.id}" id="auto-${t.id}">
          <div class="auto-head">
            ${inner}
            <button type="button" class="mini ${achievement ? "achievement-toggle" : (redeemCode ? "redeem-code-toggle" : "arena-toggle")}">选项 ▸</button>
            <label class="switch-wrap">
              <input type="checkbox" class="switch-in" ${on ? "checked" : ""}>
              <div class="switch"></div>
            </label>
          </div>
          <div class="auto-body hidden">${achievement ? renderAchievementOptions() : (redeemCode ? renderRedeemCodeOptions() : renderArenaBody(t))}</div>
        </div>`;
      }
      if (t.wantsBuyTargets) {
        return `<div class="auto-item has-body ${on ? "on" : ""}" data-id="${t.id}" id="auto-${t.id}">
          <div class="auto-head">
            ${inner}
            <button type="button" class="mini shop-free-toggle">选项 ▸</button>
            <label class="switch-wrap">
              <input type="checkbox" class="switch-in" ${on ? "checked" : ""}>
              <div class="switch"></div>
            </label>
          </div>
          <div class="auto-body hidden">${renderShopFreeOptions(t.id)}</div>
        </div>`;
      }
      return `<label class="auto-item ${on ? "on" : ""}" data-id="${t.id}">
        ${inner}
        <input type="checkbox" class="switch-in" ${on ? "checked" : ""}>
        <div class="switch"></div>
      </label>`;
    })
    .join("");

  // 开关事件：更新当前账号缓存 + 同步后端 + 开启时立即跑一次
  document.querySelectorAll("#autoList .auto-item").forEach((el) => {
    const box = el.querySelector(".switch-in");
    box.addEventListener("change", () => {
      el.classList.toggle("on", box.checked);
      onToggleChange(el.dataset.id, box.checked);
    });
  });
  // 带展开页的开关：绑定各自的"选项"按钮和内部交互。
  toggle.forEach((t) => { if (t.ui === "arena") bindArenaWidget(t.id); });
  toggle.forEach((t) => { if (t.ui === "achievement") bindAchievementOptions(t.id); });
  toggle.forEach((t) => { if (t.ui === "redeem_code") bindRedeemCodeOptions(t.id); });
  toggle.forEach((t) => { if (t.wantsBuyTargets) bindShopFreeOptions(t.id); });
  renderNextClaims();
}

function redeemCodeState(email = activeAccount) {
  return ACCOUNTS[email]?.redeemCodes || { codes: [], lastUpdated: null, refreshError: null };
}

function redeemCodeStatus(status) {
  if (status === "redeemed" || status === "already_redeemed") return "redeemed";
  if (status === "invalid") return "invalid";
  return status === "expired" ? "expired" : "pending";
}

function renderRedeemCodeOptions() {
  const state = redeemCodeState();
  const lastUpdated = Date.parse(state.lastUpdated || "");
  const updatedText = Number.isFinite(lastUpdated) ? fmtClock(lastUpdated) : "尚未更新";
  const rows = (state.codes || []).map((entry) => {
    const accountState = ACCOUNTS[activeAccount]?.params?.redeem_code?.codes?.[entry.code] || {};
    const status = redeemCodeStatus(entry.status ?? accountState.status);
    const statusText = { pending: "待兑换", redeemed: "已兑换", expired: "已过期", invalid: "无效" }[status];
    const record = entry.status != null ? entry : accountState;
    const content = status === "redeemed" ? record.detail || "" : "";
    return `<div class="redeem-code-row"><code>${filterEscape(entry.code || "")}</code><span>${filterEscape(entry.validUntil || "未注明")}</span><strong class="redeem-${status}">${statusText}</strong><span>${filterEscape(content)}</span></div>`;
  }).join("");
  return `<div class="redeem-code-wrap"><div class="tdesc">最后更新日期：${filterEscape(updatedText)}</div>${state.refreshError ? `<div class="notice err">同步失败：${filterEscape(state.refreshError)}</div>` : ""}${state.sourceError ? `<div class="notice err">来源网页更新失败：${filterEscape(state.sourceError)}</div>` : ""}<div class="redeem-code-table"><div class="redeem-code-head"><span>兑换码</span><span>有效期</span><span>状态</span><span>内容</span></div>${rows || `<div class="tdesc">当前没有可用兑换码</div>`}</div></div>`;
}

function bindRedeemCodeOptions(id) {
  const root = $(`auto-${id}`);
  if (!root) return;
  const body = root.querySelector(".auto-body");
  const toggle = root.querySelector(".redeem-code-toggle");
  toggle.addEventListener("click", () => {
    body.classList.toggle("hidden");
    toggle.textContent = body.classList.contains("hidden") ? "选项 ▸" : "选项 ▾";
  });
}

function achievementCfg(email = activeAccount) {
  const cfg = ACCOUNTS[email]?.params?.daily_achievement || {};
  return { daily: cfg.daily ?? true, weekly: cfg.weekly ?? true };
}

function renderAchievementOptions() {
  const cfg = achievementCfg();
  return `<div class="arena-wrap">
    <div class="bt-opts">
      <label class="bt-opt"><input type="checkbox" value="daily" ${cfg.daily ? "checked" : ""}>${checkboxUIHTML()}<span>每日成就</span></label>
      <label class="bt-opt"><input type="checkbox" value="weekly" ${cfg.weekly ? "checked" : ""}>${checkboxUIHTML()}<span>每周成就</span></label>
    </div>
    <div class="tdesc">可单选或同时勾选；均不勾选时不领取。仅在任务状态更新或更改选项时检测，不定时查询。</div>
  </div>`;
}

function bindAchievementOptions(id) {
  const root = $(`auto-${id}`);
  if (!root) return;
  const email = activeAccount;
  const body = root.querySelector(".auto-body");
  const toggle = root.querySelector(".achievement-toggle");
  toggle.addEventListener("click", () => {
    body.classList.toggle("hidden");
    toggle.textContent = body.classList.contains("hidden") ? "选项 ▸" : "选项 ▾";
  });
  const inputs = [...body.querySelectorAll('input[type="checkbox"]')];
  inputs.forEach((input) => input.addEventListener("change", async () => {
    const account = ACCOUNTS[email];
    if (!account) return;
    const previous = { ...(account.params?.[id] || {}) };
    const selected = Object.fromEntries(inputs.map((entry) => [entry.value, entry.checked]));
    account.params = { ...account.params, [id]: { ...previous, ...selected } };
    inputs.forEach((entry) => { entry.disabled = true; });
    try {
      await api("/api/toggles", {
        account: email, toggles: account.toggles, params: { [id]: account.params[id] },
      });
      await refreshAccountStatus(email);
    } catch (error) {
      account.params = { ...account.params, [id]: previous };
      const restored = achievementCfg(email);
      inputs.forEach((entry) => { entry.checked = restored[entry.value]; });
      logTo(email, `保存成就选项失败：${error.message}`, "err");
    } finally {
      inputs.forEach((entry) => { entry.disabled = false; });
    }
  }));
}

function shopFreeEquipCfg(email = activeAccount) {
  const schemes = EQUIP_FILTER_SCHEMES_DATA;
  const fallbackId = schemes.find((scheme) => scheme.id === "plan-star-shop")?.id || schemes[0]?.id || "";
  const cfg = ACCOUNTS[email]?.params?.shop_free || {};
  const schemeId = schemes.some((scheme) => scheme.id === cfg.equipmentSchemeId)
    ? cfg.equipmentSchemeId : fallbackId;
  const storedScheme = cfg.equipmentScheme && cfg.equipmentScheme.id === schemeId
    ? cfg.equipmentScheme : null;
  return {
    legOn: !!cfg.equipmentEnabled,
    mode: cfg.equipmentMode === "auto" ? "auto" : "display",
    schemeId,
    scheme: storedScheme || schemes.find((scheme) => scheme.id === schemeId) || null,
  };
}

function setShopFreeEquipCfg(patch) {
  const a = activeAcc(); if (!a) return;
  const current = shopFreeEquipCfg();
  const nextSchemeId = patch.equipmentSchemeId || current.schemeId;
  const nextScheme = EQUIP_FILTER_SCHEMES_DATA.find((scheme) => scheme.id === nextSchemeId);
  a.params = {
    ...a.params,
    shop_free: {
      ...(a.params.shop_free || {}), ...patch,
      equipmentSchemeId: nextSchemeId,
      equipmentScheme: nextScheme ? JSON.parse(JSON.stringify(nextScheme)) : null,
    },
  };
  syncToggles();
}

function renderShopFreeOptions(id) {
  const wantedKeys = getWantedKeys();
  const equipCfg = shopFreeEquipCfg();
  const options = BUY_TARGETS.map((target) => `<label class="bt-opt">
    <input type="checkbox" value="${target.key}" ${wantedKeys.includes(target.key) ? "checked" : ""}>
    ${checkboxUIHTML()}
    <span>${target.label}</span>
  </label>`).join("");
  return `<div class="shop-grid shop-free-grid">
    <div class="shop-left">
      <div class="shop-row col">
        <span class="shop-lbl">自动购买目标</span>
        <div class="bt-opts col">${options}</div>
      </div>
      <div class="shop-row col shop-free-equip-options">
        <label class="bt-opt shop-free-equip-toggle">
          <input type="checkbox" class="free-equip-on" ${equipCfg.legOn ? "checked" : ""}>
          ${checkboxUIHTML()}
          <span>85级传说装备</span>
        </label>
        <div class="shop-free-equip-controls ${equipCfg.legOn ? "" : "hidden"}">
          <div class="shop-equip-scheme-wrap">
            ${dropdownHTML("shop-free-equip-scheme", starSourceSchemeOptions(), equipCfg.schemeId, "暂无筛选方案")}
          </div>
          <div class="shop-free-equip-modes" role="radiogroup" aria-label="85级传说装备处理方式">
            <label class="bt-opt"><input type="radio" name="shop-free-equip-mode-${id}" value="display" ${equipCfg.mode === "display" ? "checked" : ""}><span class="radio-box" aria-hidden="true"></span><span>仅显示</span></label>
            <label class="bt-opt"><input type="radio" name="shop-free-equip-mode-${id}" value="auto" ${equipCfg.mode === "auto" ? "checked" : ""}><span class="radio-box" aria-hidden="true"></span><span>自动购买</span></label>
          </div>
        </div>
      </div>
    </div>
    <div class="shop-right">
      <div class="card-area star-source-card-area shop-free-results" id="shop-free-results-${id}">
        ${freeShopResultHTML()}
      </div>
    </div>
  </div>`;
}

function bindShopFreeOptions(id) {
  const root = $(`auto-${id}`);
  if (!root) return;
  const body = root.querySelector(".auto-body");
  const toggle = root.querySelector(".shop-free-toggle");
  toggle.addEventListener("click", () => {
    body.classList.toggle("hidden");
    toggle.textContent = body.classList.contains("hidden") ? "选项 ▸" : "选项 ▾";
  });
  body.querySelectorAll('.bt-opt input[type="checkbox"]').forEach((input) => {
    if (input.classList.contains("free-equip-on")) return;
    input.addEventListener("change", () => {
      const keys = [...body.querySelectorAll('.bt-opt input[type="checkbox"]:checked')]
        .map((entry) => entry.value);
      setWantedKeys(keys);
    });
  });
  const equipToggle = body.querySelector(".free-equip-on");
  const equipControls = body.querySelector(".shop-free-equip-controls");
  equipToggle?.addEventListener("change", () => {
    equipControls?.classList.toggle("hidden", !equipToggle.checked);
    setShopFreeEquipCfg({ equipmentEnabled: equipToggle.checked });
  });
  const schemeSlot = body.querySelector(".shop-free-equip-scheme");
  if (schemeSlot) bindDropdown(schemeSlot, (schemeId) => setShopFreeEquipCfg({ equipmentSchemeId: schemeId }));
  body.querySelectorAll('input[name^="shop-free-equip-mode-"]').forEach((input) => {
    input.addEventListener("change", () => {
      if (input.checked) setShopFreeEquipCfg({ equipmentMode: input.value });
    });
  });
  const previous = activeAcc()?.freeShopResult;
  if (previous?.id === id) renderFreeShopResult(id, previous);
}

const SHOP_FREE_TAG = "[秘密商店刷新(免费)]";

function freeShopResultHTML(state) {
  const cards = state?.legendaryMatches || [];
  if (!cards.length) {
    return `<div class="card-empty">
      <img class="star-source-empty-icon" src="/assets/ui/equipment.png" alt="">
      <div class="ce-text">命中筛选方案的<b>85级传说装备</b><br>会显示在这里</div>
    </div>`;
  }
  const cardStates = state.cardStates || {};
  return `<div class="shop-free-result-head"><span>命中筛选方案的85级传说装备</span><b>${cards.length} 件</b></div>
    <div class="shop-free-card-grid">${cards.map((card, cardIndex) => {
      const index = Number.isInteger(card.index) ? card.index : cardIndex;
      const status = cardStates[index] || "";
      const error = (state.cardErrors || {})[index];
      const label = status === "buying" ? "购买中…" : status === "bought" ? "已购买" : "购买";
      const tag = status === "bought" ? '<span class="eq-flag buy">已购买</span>' : "";
      return `<div class="shop-free-card-item">${equipCardHTML(card, tag)}
        <div class="shop-free-card-actions"><button type="button" class="btn primary shop-free-buy" data-shelf-index="${index}"${status === "buying" || status === "bought" ? " disabled" : ""}>${label}</button></div>
        ${error ? `<div class="shop-free-card-error">${filterEscape(error)}</div>` : ""}</div>`;
    }).join("")}</div>`;
}

function renderFreeShopResult(id, state, email = activeAccount) {
  const account = ACCOUNTS[email];
  if (account) account.freeShopResult = { id, ...state };
  if (email !== activeAccount) return;
  const area = $(`shop-free-results-${id}`);
  if (!area) return;
  area.classList.toggle("has-results", !!state?.legendaryMatches?.length);
  area.innerHTML = freeShopResultHTML(state);
  area.querySelectorAll(".shop-free-buy").forEach((button) => button.addEventListener("click", async () => {
    const shelfIndex = Number(button.dataset.shelfIndex);
    const current = account?.freeShopResult;
    if (!account || !Number.isInteger(shelfIndex) || !current?.legendaryMatches?.length) return;
    const cardStates = { ...(current.cardStates || {}), [shelfIndex]: "buying" };
    const cardErrors = { ...(current.cardErrors || {}) };
    delete cardErrors[shelfIndex];
    renderFreeShopResult(id, { ...current, cardStates, cardErrors }, email);
    try {
      const result = await api("/api/shop/buy", { account: email, index: shelfIndex });
      if (result.status) applyStatus(email, result.status, result.nextClaims);
      const latest = account.freeShopResult || current;
      const nextStates = { ...(latest.cardStates || {}) };
      const nextErrors = { ...(latest.cardErrors || {}) };
      if (result.ok) {
        nextStates[shelfIndex] = "bought";
        delete nextErrors[shelfIndex];
        logTo(email, `${SHOP_FREE_TAG} 已购买目标装备`, "ok");
      } else {
        delete nextStates[shelfIndex];
        nextErrors[shelfIndex] = result.detail || "购买失败";
        logTo(email, `${SHOP_FREE_TAG} ${nextErrors[shelfIndex]}`, "err");
      }
      renderFreeShopResult(id, { ...latest, cardStates: nextStates, cardErrors: nextErrors }, email);
    } catch (error) {
      const latest = account.freeShopResult || current;
      const nextStates = { ...(latest.cardStates || {}) };
      const nextErrors = { ...(latest.cardErrors || {}), [shelfIndex]: error.message };
      delete nextStates[shelfIndex];
      renderFreeShopResult(id, { ...latest, cardStates: nextStates, cardErrors: nextErrors }, email);
      logTo(email, `${SHOP_FREE_TAG} 购买出错：${error.message}`, "err");
    }
  }));
}

function handleAutoTaskResult(result, prefix = "自动·", email = activeAccount, ts = null) {
  if (result?.id === "shop_free" && result.equipmentMode
      && Array.isArray(result.legendaryMatches)) {
    renderFreeShopResult("shop_free", result, email);
  }
  logResult(result, prefix, email, ts);
}

// ---------- 竞技场 NPC 展开页 ----------
// 10 个地狱级 NPC：名称 + 下次挑战时间/倒计时 + 勾选框，外加队伍下拉。
// 勾选与队伍存后端（ACCOUNTS[email].params.arena_npc），因为后台调度要用。
// 冷却只有"本工具扫荡过"的才知道（游戏快照区分不出难度，见后端 arena_state 注释），
// 未知的显示"可挑战"直接尝试。
function renderArenaBody(t) {
  return `<div class="arena-wrap">
    <div class="shop-row col">
      <span class="shop-lbl">出战队伍</span>
      <div class="dd-slot arena-team-slot"><div class="dd-loading">加载中…</div></div>
    </div>
    <label class="arena-row arena-all" id="arenaAll-${t.id}">
      <input type="checkbox" class="arena-pick-all">
      ${checkboxUIHTML()}
      <span class="arena-name">全选</span>
      <span class="arena-when arena-count"></span>
    </label>
    <div class="arena-list" id="arenaList-${t.id}">
      <div class="dd-loading">加载中…</div>
    </div>
  </div>`;
}

// 单个 NPC 行的档期文案（沿用自动任务面板的时间格式：绝对时刻在前、倒计时在括号内）
function arenaWhen(npc) {
  if (npc.waitingForLogin) return { txt: "等待登录同步刷新", cls: "" };
  const gate = Math.max(npc.nextMs || 0, npc.retryMs || 0);
  if (!gate) return { txt: "可挑战", cls: "ready" };
  const diff = gate - Date.now();
  if (diff <= 0) return { txt: "可挑战", cls: "ready" };
  const label = npc.retryMs && npc.retryMs >= (npc.nextMs || 0) ? "重试" : "下次挑战";
  return { txt: `${label}: ${fmtClock(gate)} (${fmtRemain(diff)})`, cls: "" };
}

function renderArenaList(taskId) {
  const box = $(`arenaList-${taskId}`);
  if (!box) return;
  const a = activeAcc();
  const st = (a && a.arena) || null;
  if (!st) return;
  box.innerHTML = st.npcs
    .map((n) => {
      const w = arenaWhen(n);
      return `<label class="arena-row ${n.picked ? "on" : ""}" data-n="${n.n}">
        <input type="checkbox" class="arena-pick" ${n.picked ? "checked" : ""}>
        ${checkboxUIHTML()}
        <span class="arena-name"><span class="arena-tier">地狱级 · </span>${n.name}</span>
        <span class="arena-when ${w.cls}">${w.txt}</span>
      </label>`;
    })
    .join("");
  box.querySelectorAll(".arena-pick").forEach((cb) =>
    cb.addEventListener("change", () => {
      const row = cb.closest(".arena-row");
      row.classList.toggle("on", cb.checked);
      saveArenaPicks(taskId);
    })
  );
  syncArenaAll(taskId);
}

// 全选行：勾选数 = 0/部分/全部 三态（部分用 indeterminate 表达）
function syncArenaAll(taskId) {
  const all = $(`arenaAll-${taskId}`);
  if (!all) return;
  const boxes = [...document.querySelectorAll(`#arenaList-${taskId} .arena-pick`)];
  const on = boxes.filter((b) => b.checked).length;
  const cb = all.querySelector(".arena-pick-all");
  cb.checked = boxes.length > 0 && on === boxes.length;
  cb.indeterminate = on > 0 && on < boxes.length;
  all.classList.toggle("on", cb.checked);
  all.classList.toggle("part", cb.indeterminate);
  const cnt = all.querySelector(".arena-count");
  if (cnt) cnt.textContent = boxes.length ? `已选 ${on}/${boxes.length}` : "";
}

function bindArenaAll(taskId) {
  const all = $(`arenaAll-${taskId}`);
  if (!all) return;
  const cb = all.querySelector(".arena-pick-all");
  cb.addEventListener("change", () => {
    // indeterminate（部分选中）时点一下当作"全选"，符合直觉
    const target = cb.indeterminate ? true : cb.checked;
    document.querySelectorAll(`#arenaList-${taskId} .arena-row`).forEach((row) => {
      const box = row.querySelector(".arena-pick");
      box.checked = target;
      row.classList.toggle("on", target);
    });
    syncArenaAll(taskId);
    saveArenaPicks(taskId);
  });
}

async function saveArenaPicks(taskId) {
  const email = activeAccount;
  const a = ACCOUNTS[email];
  if (!a) return;
  const picked = [...document.querySelectorAll(`#arenaList-${taskId} .arena-pick:checked`)]
    .map((cb) => parseInt(cb.closest(".arena-row").dataset.n));
  const teamId = (a.arena && a.arena.teamId) || "0";
  try {
    const st = await api("/api/arena/config", { account: email, picked, teamId });
    applyArenaConfig(email, st);
    // 后端保存勾选时会唤醒调度线程立刻开打（notify_config_changed），但日志轮询是
    // 每 8s 一次。开关已打开且确实勾了东西时，提前拉一次日志，让结果马上可见。
    if (picked.length && getToggles()[taskId]) setTimeout(pollAutoLogs, 700);
  } catch (e) {
    logTo(email, `保存竞技场勾选失败：${e.message}`, "err");
  }
}

// 把 /api/arena/config 的返回写回内存态。
// ⚠️ 必须同时更新 params 副本：勾选是存在 params.arena_npc 里的，而 syncToggles()
// 会把 a.params 整份 POST 给 /api/toggles。以前不更新，用户一拨开关就把刚存的勾选
// 用陈旧副本盖掉了（后端也已改成按 key 合并，两侧都堵上）。
function applyArenaConfig(email, st) {
  const a = ACCOUNTS[email];
  if (!a || !st) return;
  a.arena = { npcs: st.npcs, teamId: st.teamId, teams: st.teams };
  if (st.params) a.params = st.params;
}

// 拉 NPC 清单 + 队伍 + 冷却。返回是否成功（供调用方决定要不要盖"已加载"戳）。
async function loadArenaOptions(taskId) {
  const email = activeAccount;
  const root = $(`auto-${taskId}`);
  if (!root || !email) return false;
  const teamSlot = root.querySelector(".arena-team-slot");
  try {
    const st = await api(`/api/arena/options`);
    if (!ACCOUNTS[email]) return;
    ACCOUNTS[email].arena = st;
    const teamOpts = (st.teams || []).length
      ? st.teams.map((x) => ({ val: x.id, label: x.name }))
      : [{ val: "", label: "（无已保存的队伍）" }];
    const cur = st.teamId && st.teams.some((x) => x.id === st.teamId) ? st.teamId : teamOpts[0].val;
    teamSlot.innerHTML = dropdownHTML("arena-team", teamOpts, cur);
    bindArenaAll(taskId);
    bindDropdown(teamSlot.querySelector(".dropdown"), async (v) => {
      const a = ACCOUNTS[email];
      if (a && a.arena) a.arena.teamId = v;
      try {
        const s2 = await api("/api/arena/config", { account: email, teamId: v });
        applyArenaConfig(email, s2);
      } catch (e) { logTo(email, `保存竞技场队伍失败：${e.message}`, "err"); }
    });
    renderArenaList(taskId);
    return true;
  } catch (e) {
    teamSlot.innerHTML = `<div class="dd-loading err">加载失败</div>`;
    const list = $(`arenaList-${taskId}`);
    if (list) list.innerHTML = `<div class="dd-loading err">加载失败：${e.message}</div>`;
    return false;
  }
}

function bindArenaWidget(id) {
  const root = $(`auto-${id}`);
  if (!root) return;
  const body = root.querySelector(".auto-body");
  const btn = root.querySelector(".arena-toggle");
  btn.addEventListener("click", async (e) => {
    e.preventDefault();
    e.stopPropagation();
    body.classList.toggle("hidden");
    btn.textContent = body.classList.contains("hidden") ? "选项 ▸" : "选项 ▾";
    if (!body.classList.contains("hidden") && !root.dataset.loaded) {
      // 只有加载成功才盖"已加载"戳——否则一次失败(未登录/网络抖动)会把面板永久
      // 钉在"加载失败"上，用户重新展开也不再重试。
      if (await loadArenaOptions(id)) root.dataset.loaded = "1";
    }
  });
}

// 组装 params：给需要购买目标的任务塞 wanted
function toggleParams(ids) {
  const p = {};
  ids.forEach((id) => {
    const meta = TOGGLE_TASKS.find((x) => x.id === id);
    if (meta?.ui === "achievement") p[id] = achievementCfg();
    if (meta && meta.wantsBuyTargets) {
      const equipCfg = shopFreeEquipCfg();
      const scheme = equipCfg.scheme;
      p[id] = {
        wanted: wantedIds(),
        equipmentScheme: equipCfg.legOn ? (scheme ? JSON.parse(JSON.stringify(scheme)) : null) : null,
        equipmentMode: equipCfg.mode,
      };
    }
  });
  return p;
}

// 执行指定 toggle 任务集（ids 为空则不发请求）
// quiet=true 时静默"未到时间/未满"的跳过项（skipped），只输出真正领到的——
// 供每小时巡检用，避免每次刷一堆"还需 N 小时，跳过"的噪音。
async function runToggleTasks(ids, prefix = "自动·", quiet = false) {
  if (!ids || !ids.length) return;
  // 竞技场和兑换码由后端逐项写日志，运行期间持续拉取已完成的结果。
  const streamsResults = ids.includes("arena_npc") || ids.includes("redeem_code");
  const liveLogTimer = streamsResults ? setInterval(pollAutoLogs, 700) : null;
  try {
    const { results } = await api("/api/tasks/auto", { taskIds: ids, params: toggleParams(ids) });
    results.forEach((r) => {
      if (quiet && r.skipped) return;
      if (r.liveLogged) return;
      handleAutoTaskResult(r, prefix);
    });
  } catch (e) {
    log(`自动任务执行失败：${e.message}`, "err");
  } finally {
    if (liveLogTimer) {
      clearInterval(liveLogTimer);
      await pollAutoLogs();
    }
  }
}

// 开关变化：更新当前账号缓存 → 同步后端（后台调度据此执行）；开启时立即跑一次该任务。
// quiet=true：未到点而跳过的不记日志（只有真发了请求领到东西才记）。
function onToggleChange(id, on) {
  const a = activeAcc(); if (!a) return;
  a.toggles = { ...a.toggles, [id]: on };
  syncToggles();
  if (on) {
    runToggleTasks([id], "开关·", true).then(() => refreshStatus(true));
  }
}

// 已开启的 toggle 任务 id（当前账号）
function enabledToggleIds() {
  const t = getToggles();
  return Object.keys(t).filter((k) => t[k]);
}

const clampCount = (v) => Math.min(999, Math.max(1, parseInt(v) || 1));

// 手动任务面板：普通任务=自带启动按钮的行；自定义 UI 任务(shop/sweep)=各自控件
function renderManualPanel(tasks) {
  $("taskList").innerHTML = tasks
    .map((t) => {
      if (t.ui === "shop") return renderShopWidget(t);
      if (t.ui === "starshop") return renderStarSourceShopWidget(t);
      if (t.ui === "customizedequip") return renderCustomizedEquipWidget(t);
      if (t.ui === "sweep") return renderSweepWidget(t);
      if (t.ui === "activity") return renderActivityWidget(t);
      if (t.ui === "abyss") return renderAbyssWidget(t);
      if (t.ui === "storebuy") return renderStoreWidget(t);
      // 普通任务：名称+描述在左，启动按钮在右（不再多选+统一执行）
      return `<div class="task-row">
        <div class="ticon">${taskIconHTML(t.id)}</div>
        <div class="tinfo"><div class="tname">${t.name}</div><div class="tdesc">${t.desc}</div></div>
        <div class="tstate" id="ts-${t.id}"></div>
        <button type="button" class="mini task-run" data-id="${t.id}">启动</button>
      </div>`;
    })
    .join("");
  document.querySelectorAll("#taskList .task-run").forEach((b) =>
    b.addEventListener("click", () => runSingleTask(b.dataset.id, b))
  );
  tasks.forEach((t) => {
    if (t.ui === "shop") bindShopWidget(t.id);
    if (t.ui === "starshop") bindStarSourceShopWidget(t.id);
    if (t.ui === "customizedequip") bindCustomizedEquipWidget(t.id);
    if (t.ui === "sweep") bindSweepWidget(t.id);
    if (t.ui === "activity") bindActivityWidget(t.id);
    if (t.ui === "abyss") bindAbyssWidget(t.id);
    if (t.ui === "storebuy") bindStoreWidget(t.id);
  });
}

// 单个普通任务：点自己的按钮直接执行
async function runSingleTask(id, btn) {
  const meta = TASKS.find((x) => x.id === id) || {};
  const params = {};
  if (meta.wantsBuyTargets) params.wanted = wantedIds();
  const state = $(`ts-${id}`);
  btn.disabled = true; btn.textContent = "运行中…";
  if (state) { state.textContent = "运行中"; state.className = "tstate run"; }
  try {
    const { results } = await api("/api/tasks/run", { taskIds: [id], params: { [id]: params } });
    const r = results[0] || { ok: false, detail: "无返回" };
    if (state) { state.textContent = r.ok ? "✓ 完成" : "✗ 失败"; state.className = "tstate " + (r.ok ? "ok" : "err"); }
    logResult(r);
    refreshStatus();
  } catch (e) {
    log(`执行出错：${e.message}`, "err");
    if (state) { state.textContent = "✗ 失败"; state.className = "tstate err"; }
  } finally {
    btn.disabled = false; btn.textContent = "启动";
  }
}

// ---------- 秘密商店刷新(钻石) 自定义控件 ----------
// 配置按账号命名空间（不同账号刷商店偏好独立）
const SHOP_LS = "ark_shop_diamond_cfg";  // {count, wanted:[key], legOn, schemeId}
const shopLsKey = () => `${SHOP_LS}::${activeAccount || "_"}`;
// 缺省全选：从后端下发的目标表推导，别再写死 key 列表
// （后端加/改目标项时这里会自动跟上，不会悄悄漏掉新项）
const allTargetKeys = () => BUY_TARGETS.map((b) => b.key);
function getShopCfg() {
  const schemes = EQUIP_FILTER_SCHEMES_DATA;
  const fallbackId = schemes.find((scheme) => scheme.id === "plan-star-shop")?.id || schemes[0]?.id || "";
  try {
    const v = JSON.parse(frontendStorageGet(shopLsKey())) || {};
    return {
      count: v.count || 10,
      wanted: Array.isArray(v.wanted) ? v.wanted : allTargetKeys(),
      legOn: !!v.legOn,
      schemeId: schemes.some((scheme) => scheme.id === v.schemeId) ? v.schemeId : fallbackId,
    };
  } catch {
    return { count: 10, wanted: allTargetKeys(), legOn: false, schemeId: fallbackId };
  }
}
function setShopCfg(patch) {
  frontendStorageSet(shopLsKey(), JSON.stringify({ ...getShopCfg(), ...patch }));
}
function shopWantedIds() {
  const keys = getShopCfg().wanted;
  const ids = [];
  BUY_TARGETS.forEach((b) => { if (keys.includes(b.key)) ids.push(...b.ids); });
  return ids;
}

function renderShopWidget(t) {
  const cfg = getShopCfg();
  const opts = BUY_TARGETS.map(
    (b) => `<label class="bt-opt"><input type="checkbox" data-wkey="${b.key}" ${cfg.wanted.includes(b.key) ? "checked" : ""}>${checkboxUIHTML()}<span>${b.label}</span></label>`
  ).join("");
  return `<div class="shop-widget" id="shop-${t.id}">
    <div class="shop-head">
      <div class="ticon">${taskIconHTML(t.id, "💎")}</div>
      <div class="tinfo"><div class="tname">${t.name}</div><div class="tdesc">${t.desc}</div></div>
      <button type="button" class="mini shop-toggle">选项 ▸</button>
    </div>
    <div class="shop-body hidden">
      <div class="shop-grid">
        <div class="shop-left">
          <div class="shop-row">
            <span class="shop-lbl">刷新次数</span>
            <div class="count-box">
              <button type="button" class="count-btn" data-d="-1">−</button>
              <input type="text" inputmode="numeric" class="count-in shop-count" value="${cfg.count}">
              <span class="count-unit">次</span>
              <button type="button" class="count-btn" data-d="1">+</button>
            </div>
          </div>
          <div class="shop-row col">
            <span class="shop-lbl">自动购买目标</span>
            <div class="bt-opts col">${opts}</div>
          </div>
          <div class="shop-row col">
            <label class="bt-opt"><input type="checkbox" class="leg-on" ${cfg.legOn ? "checked" : ""}>${checkboxUIHTML()}<span>85级传说装备</span></label>
            <div class="shop-equip-scheme-wrap ${cfg.legOn ? "" : "hidden"}">
              ${dropdownHTML("shop-equip-scheme", starSourceSchemeOptions(), cfg.schemeId, "暂无筛选方案")}
            </div>
          </div>
          <button type="button" class="btn primary shop-start">▶ 开始刷新</button>
        </div>
        <div class="shop-right">
          <div class="card-area star-source-card-area" id="cardArea-${t.id}">
            ${secretShopStateHTML()}
          </div>
        </div>
      </div>
    </div>
  </div>`;
}

function bindShopWidget(id) {
  const root = $(`shop-${id}`);
  if (!root) return;
  const body = root.querySelector(".shop-body");
  const toggle = root.querySelector(".shop-toggle");
  toggle.addEventListener("click", () => {
    body.classList.toggle("hidden");
    toggle.textContent = body.classList.contains("hidden") ? "选项 ▸" : "选项 ▾";
  });
  const cntIn = root.querySelector(".shop-count");
  cntIn.addEventListener("input", () => { cntIn.value = cntIn.value.replace(/[^0-9]/g, "").slice(0, 3); });
  cntIn.addEventListener("blur", () => { cntIn.value = clampCount(cntIn.value); setShopCfg({ count: parseInt(cntIn.value) }); });
  root.querySelectorAll(".count-btn").forEach((btn) =>
    btn.addEventListener("click", () => {
      cntIn.value = clampCount((parseInt(cntIn.value) || 10) + parseInt(btn.dataset.d));
      setShopCfg({ count: parseInt(cntIn.value) });
    })
  );
  root.querySelectorAll("[data-wkey]").forEach((cb) =>
    cb.addEventListener("change", () =>
      setShopCfg({ wanted: [...root.querySelectorAll("[data-wkey]:checked")].map((x) => x.dataset.wkey) })
    )
  );
  const legOn = root.querySelector(".leg-on");
  const schemeWrap = root.querySelector(".shop-equip-scheme-wrap");
  legOn.addEventListener("change", () => {
    schemeWrap.classList.toggle("hidden", !legOn.checked);
    setShopCfg({ legOn: legOn.checked });
  });
  bindDropdown(root.querySelector(".shop-equip-scheme"), (schemeId) => setShopCfg({ schemeId }));
  root.querySelector(".shop-start").addEventListener("click", () => {
    if (shopRunning[activeAccount]) requestSecretShopStop(id, activeAccount);
    else runShopDiamond(id);
  });
  const saved = activeAcc()?.secretShopResult;
  if (saved?.id === id) renderSecretShopState(id, saved);
  if (shopRunning[activeAccount]) {
    setSecretShopStartState(id, activeAccount,
      shopStopRequested[activeAccount] ? "stopping" : (activeAcc()?.secretShopPending ? "matched" : "running"));
  }
}

// 一轮刷新的产出统计：把后端 bought({StaticID:数量}) 按购买目标分组累加。
// 六个部位的下级强化芯片合并成一项（用户只关心总数）。
function addShopGain(total, bought) {
  Object.entries(bought || {}).forEach(([sid, cnt]) => {
    const t = BUY_TARGETS.find((b) => b.ids.includes(String(sid)));
    const key = t ? t.key : `sid:${sid}`;   // 不在目标表里的照样计，不静默丢
    total[key] = (total[key] || 0) + (parseInt(cnt) || 0);
  });
}

// "共获取 3招募契约、1下级强化芯片"；数量为 0 的项不出现，全为 0 返回空串（收尾日志只留前半句）
function shopGainSummary(total) {
  const parts = [];
  BUY_TARGETS.forEach((b) => {
    const n = total[b.key] || 0;
    if (n > 0) parts.push(`${n}${b.stat || b.label}`);
  });
  Object.entries(total).forEach(([k, n]) => {
    if (k.startsWith("sid:") && n > 0) parts.push(`${n}道具#${k.slice(4)}`);
  });
  return parts.length ? `共获取 ${parts.join("、")}` : "";
}

const SHOP_TAG = "[秘密商店刷新(钻石)]";   // 该功能所有日志的统一前缀

// 编排循环：严格"上一次买完再刷下一次"。
// ⚠️ 账号在循环期间锁定为发起时的账号：所有请求显式带 email、日志写该账号缓冲，
// 即使用户中途切到别的账号，请求与日志也不会串（之前读全局 activeAccount 会串账号）。
const shopRunning = {};  // email -> bool，改为按账号，切账号后仍可各自刷
const shopStopRequested = {};

function secretShopStateHTML(state) {
  if (state?.cards?.length) {
    const cardStates = state.cardStates || {};
    const buying = Object.values(cardStates).includes("buying");
    return `<div class="star-source-result-head"><span>第 ${state.round} 次刷新命中</span>
      <div class="star-source-result-actions"><b>${state.cards.length} 件装备</b>
        ${state.kind === "matched" ? `<button type="button" class="btn ghost secret-shop-skip"${buying ? " disabled" : ""}>跳过</button>` : ""}
      </div></div>
      <div class="star-source-card-grid">${state.cards.map((card, cardIndex) => {
        const shelfIndex = Number.isInteger(card.index) ? card.index : cardIndex;
        const cardState = cardStates[shelfIndex] || "";
        const stateTag = cardState === "bought" ? '<span class="eq-flag buy">已购买</span>' : "";
        const buttonText = cardState === "buying" ? "购买中…" : cardState === "bought" ? "已购买" : "购买";
        const error = (state.cardErrors || {})[shelfIndex];
        return `<div class="star-source-card-item">${equipCardHTML(card, stateTag)}
          <div class="star-source-card-actions"><button type="button" class="btn primary secret-shop-buy" data-shelf-index="${shelfIndex}"${cardState === "buying" || cardState === "bought" ? " disabled" : ""}>${buttonText}</button></div>
          ${error ? `<div class="star-source-card-error">${filterEscape(error)}</div>` : ""}
        </div>`;
      }).join("")}</div>`;
  }
  const icon = '<img class="star-source-empty-icon" src="/assets/ui/equipment.png" alt="">';
  if (state?.kind === "running") {
    return `<div class="card-empty">${icon}<div class="ce-text">正在刷新 ${state.round} / ${state.total}</div></div>`;
  }
  if (state?.kind === "error") {
    return `<div class="card-empty">${icon}<div class="ce-text">${filterEscape(state.message || "刷新失败")}</div></div>`;
  }
  if (state?.kind === "empty") {
    if (state.equipmentEnabled === false) {
      return `<div class="card-empty">${icon}<div class="ce-text">已完成 ${state.total} 次刷新</div></div>`;
    }
    const summaries = [];
    if (state.purchasedMatches) summaries.push(`已购买 ${state.purchasedMatches} 件命中装备`);
    if (state.skippedMatches) summaries.push(`已跳过 ${state.skippedMatches} 轮命中装备`);
    return `<div class="card-empty">${icon}<div class="ce-text">已完成 ${state.total} 次刷新<br>${summaries.length ? summaries.join("，") : "没有符合方案的装备"}</div></div>`;
  }
  return `<div class="card-empty">${icon}<div class="ce-text">命中筛选方案的<b>传说装备</b><br>会在这里等待购买或跳过</div></div>`;
}

function renderSecretShopState(id, state, email = activeAccount) {
  const account = ACCOUNTS[email];
  if (account) account.secretShopResult = { id, ...state };
  if (email !== activeAccount) return;
  const area = $(`cardArea-${id}`);
  if (!area) return;
  area.classList.toggle("has-results", !!state?.cards?.length);
  area.innerHTML = secretShopStateHTML(state);
  bindSecretShopResultActions(id, email);
}

function setSecretShopStartState(id, email, mode) {
  if (email !== activeAccount) return;
  const button = $(`shop-${id}`)?.querySelector(".shop-start");
  if (!button) return;
  const stopping = mode === "stopping";
  const active = mode === "running" || mode === "matched" || stopping;
  button.disabled = stopping;
  button.classList.toggle("task-stop-button", active);
  button.textContent = stopping ? "停止中…" : active ? "停止" : "▶ 开始刷新";
}

function resolveSecretShopPending(id, email, decision) {
  const account = ACCOUNTS[email];
  const pending = account?.secretShopPending;
  if (!pending || pending.id !== id) return false;
  account.secretShopPending = null;
  setSecretShopStartState(id, email, shopStopRequested[email] ? "stopping" : "running");
  pending.resolve(decision);
  return true;
}

function requestSecretShopStop(id, email) {
  if (!email || !shopRunning[email]) return;
  shopStopRequested[email] = true;
  const account = ACCOUNTS[email];
  const buying = Object.values(account?.secretShopResult?.cardStates || {}).includes("buying");
  if (!buying && account?.secretShopPending) {
    resolveSecretShopPending(id, email, { action: "stop", count: 0 });
  } else {
    setSecretShopStartState(id, email, "stopping");
  }
}

function bindSecretShopResultActions(id, email) {
  const account = ACCOUNTS[email];
  const state = account?.secretShopResult;
  if (!account || state?.id !== id || email !== activeAccount) return;
  const area = $(`cardArea-${id}`);
  if (!area) return;

  area.querySelector(".secret-shop-skip")?.addEventListener("click", () => {
    const current = account.secretShopResult;
    if (Object.values(current?.cardStates || {}).includes("buying")) return;
    const boughtCount = current.cards.reduce((count, card, cardIndex) => {
      const shelfIndex = Number.isInteger(card.index) ? card.index : cardIndex;
      return count + (current.cardStates?.[shelfIndex] === "bought" ? 1 : 0);
    }, 0);
    resolveSecretShopPending(id, email, { action: "skip", count: boughtCount });
  });

  area.querySelectorAll(".secret-shop-buy").forEach((button) => button.addEventListener("click", async () => {
    const shelfIndex = Number(button.dataset.shelfIndex);
    let current = account.secretShopResult;
    if (!Number.isInteger(shelfIndex) || current?.kind !== "matched") return;
    const card = current.cards.find((item, cardIndex) =>
      (Number.isInteger(item.index) ? item.index : cardIndex) === shelfIndex);
    const cardStates = { ...(current.cardStates || {}), [shelfIndex]: "buying" };
    const cardErrors = { ...(current.cardErrors || {}) };
    delete cardErrors[shelfIndex];
    renderSecretShopState(id, { ...current, cardStates, cardErrors }, email);
    try {
      const result = await api("/api/shop/buy", { account: email, index: shelfIndex });
      if (result.status) applyStatus(email, result.status, result.nextClaims);
      current = account.secretShopResult;
      const nextStates = { ...(current.cardStates || {}) };
      const nextErrors = { ...(current.cardErrors || {}) };
      if (result.ok) {
        nextStates[shelfIndex] = "bought";
        delete nextErrors[shelfIndex];
        const equipmentName = [card?.setCN, card?.slotCN].filter(Boolean).join("·") || "目标装备";
        logTo(email, `${SHOP_TAG} 已购买 ${equipmentName}`, "ok");
      } else {
        delete nextStates[shelfIndex];
        nextErrors[shelfIndex] = result.detail || "购买失败";
        logTo(email, `${SHOP_TAG} ${nextErrors[shelfIndex]}`, "err");
      }
      renderSecretShopState(id, { ...current, cardStates: nextStates, cardErrors: nextErrors }, email);
      const allBought = result.ok && current.cards.every((item, cardIndex) => {
        const itemIndex = Number.isInteger(item.index) ? item.index : cardIndex;
        return nextStates[itemIndex] === "bought";
      });
      if (allBought) {
        resolveSecretShopPending(id, email, { action: "bought", count: current.cards.length });
      }
    } catch (error) {
      current = account.secretShopResult;
      const nextStates = { ...(current.cardStates || {}) };
      const nextErrors = { ...(current.cardErrors || {}), [shelfIndex]: error.message };
      delete nextStates[shelfIndex];
      renderSecretShopState(id, { ...current, cardStates: nextStates, cardErrors: nextErrors }, email);
      logTo(email, `${SHOP_TAG} 购买出错：${error.message}`, "err");
    }
    if (shopStopRequested[email] && account.secretShopPending) {
      resolveSecretShopPending(id, email, { action: "stop", count: 0 });
    }
  }));
}

function waitForSecretShopDecision(id, state, email) {
  return new Promise((resolve) => {
    const account = ACCOUNTS[email];
    if (!account) {
      resolve({ action: "skip", count: 0 });
      return;
    }
    account.secretShopPending = { id, resolve };
    renderSecretShopState(id, { ...state, kind: "matched", cardStates: {}, cardErrors: {} }, email);
    setSecretShopStartState(id, email, "matched");
  });
}

async function runShopDiamond(id) {
  const email = activeAccount;
  if (shopRunning[email]) return;
  const cfg = getShopCfg();
  const scheme = cfg.legOn
    ? EQUIP_FILTER_SCHEMES_DATA.find((item) => item.id === cfg.schemeId)
    : null;
  if (cfg.legOn && (!scheme || !starSourceSchemeCanMatch(scheme))) {
    const message = !scheme ? "请选择有效的装备筛选方案" : "当前装备筛选方案尚未配置任何适用套装";
    logTo(email, `${SHOP_TAG} ${message}`, "warn");
    return;
  }
  const schemeSnapshot = scheme ? JSON.parse(JSON.stringify(scheme)) : null;
  shopRunning[email] = true;
  shopStopRequested[email] = false;
  setSecretShopStartState(id, email, "running");
  const wanted = shopWantedIds();
  const gained = {};   // 本次整轮的目标道具产出累计，结束后并进收尾那一条日志
  let completed = 0;
  let skippedMatches = 0;
  let purchasedMatches = 0;
  let endText = "刷新完成";   // 收尾文案：正常跑完/出错两种，与统计合成一句
  let endLv = "ok";
  let failed = false;
  let stopped = false;
  logTo(email, `${SHOP_TAG} 开始刷新：${cfg.count} 次${scheme ? `，装备方案“${scheme.name}”` : ""}`);
  try {
    for (let i = 1; i <= cfg.count; i++) {
      await waitRequestDelayBetweenIterations(i, () => shopStopRequested[email]);
      if (shopStopRequested[email]) {
        endText = "已停止";
        endLv = "warn";
        stopped = true;
        break;
      }
      renderSecretShopState(id, { kind: "running", round: i, total: cfg.count }, email);
      const r = await api("/api/shop/reset", {
        account: email, useGold: true, autoBuy: wanted,
        equipmentScheme: schemeSnapshot,
      });
      completed = i;
      if (r.status) applyStatus(email, r.status, r.nextClaims);  // 每次刷新/购买后即时更新主页资产
      addShopGain(gained, r.bought);   // 失败中止的那次也可能已买到东西，先累加再判 ok
      const stopAfterRequest = !!shopStopRequested[email];
      if (!r.ok) {
        const message = r.error || "未知错误";
        renderSecretShopState(id, { kind: "error", message }, email);
        logTo(email, `${SHOP_TAG} 第 ${i} 次刷新失败：${message}，停止`, "err");
        endText = "刷新中止";
        endLv = "err";
        failed = true;
        break;
      }
      (r.boughtLines || []).forEach((ln) => logTo(email, `${SHOP_TAG} 第 ${i} 次：${ln}`, "ok"));
      const buyErrs = r.buyErrors || [];
      buyErrs.forEach((ln) => logTo(email, `${SHOP_TAG} 第 ${i} 次：${ln}`, "err"));
      const legendaryMatches = r.legendaryMatches || [];
      if (legendaryMatches.length) {
        if (stopAfterRequest) {
          renderSecretShopState(id, {
            kind: "matched", cards: legendaryMatches, round: i, total: cfg.count,
            schemeName: scheme?.name || "",
          }, email);
          endText = "已停止";
          endLv = "warn";
          stopped = true;
          break;
        }
        logTo(email, `${SHOP_TAG} 第 ${i} 次命中 ${legendaryMatches.length} 件装备，等待选择`, "ok");
        const decision = await waitForSecretShopDecision(id, {
          cards: legendaryMatches,
          round: i,
          total: cfg.count,
          schemeName: scheme?.name || "",
        }, email);
        purchasedMatches += Number(decision?.count) || 0;
        if (shopStopRequested[email]) {
          endText = "已停止";
          endLv = "warn";
          stopped = true;
          break;
        }
        if (decision?.action === "bought") {
          if (i < cfg.count && !buyErrs.length) logTo(email, `${SHOP_TAG} 本轮命中装备已全部购买，继续刷新`, "ok");
        } else {
          skippedMatches += 1;
          if (i < cfg.count && !buyErrs.length) logTo(email, `${SHOP_TAG} 已跳过本轮剩余命中装备，继续刷新`);
        }
      }
      if (shopStopRequested[email]) {
        endText = "已停止";
        endLv = "warn";
        stopped = true;
        break;
      }
      // 普通自动购买目标没买成时继续刷新只会白耗钻石，因此仍按原逻辑停止；若同轮
      // 也命中装备，先让用户看完并处理卡片，再执行停止逻辑。
      if (buyErrs.length) {
        const message = "有自动购买目标未购成，已停止刷新";
        renderSecretShopState(id, { kind: "error", message }, email);
        logTo(email, `${SHOP_TAG} 第 ${i} 次有目标未购成，停止刷新（避免白耗钻石）`, "warn");
        endText = "刷新中止";
        endLv = "warn";
        failed = true;
        break;
      }
      if (!(r.boughtLines || []).length && !legendaryMatches.length) {
        logTo(email, `${SHOP_TAG} 第 ${i} 次：无目标道具`);
      }
    }
    if (!failed && !stopped) {
      renderSecretShopState(id, {
        kind: "empty",
        total: completed,
        skippedMatches,
        purchasedMatches,
        equipmentEnabled: !!scheme,
      }, email);
    }
  } catch (e) {
    endText = `刷新出错：${e.message}`;
    endLv = "err";
    renderSecretShopState(id, { kind: "error", message: e.message }, email);
  } finally {
    // 收尾一条日志 = 完成/出错 + 本轮统计。放 finally 里，提前中止(缺钱停止/出错)
    // 也能看到已获取的东西；一件没买到时只留前半句。
    const summary = shopGainSummary(gained);
    logTo(email, `${SHOP_TAG} ${endText}${summary ? "，" + summary : ""}`, endLv);
    shopRunning[email] = false;
    delete shopStopRequested[email];
    if (ACCOUNTS[email]) ACCOUNTS[email].secretShopPending = null;
    setSecretShopStartState(id, email, "idle");
    refreshAccountStatus(email);
  }
}

const CARD_EMPTY = `<div class="card-empty">
  <img class="ce-icon-img" src="/assets/ui/equipment.png" alt="">
  <div class="ce-text">命中筛选方案的<b>传说装备</b><br>会在这里等待购买或跳过</div>
</div>`;

const EQUIP_STAT_ICONS = Object.freeze({
  AttackValue: "Attack.png",
  AttackRate: "Attack.png",
  DefenceValue: "Defense.png",
  DefenceRate: "Defense.png",
  HPValue: "Health.png",
  HPRate: "Health.png",
  CriticalRate: "Critical.png",
  CriticalDamageRate: "CriticalDamage.png",
  SpeedValue: "Speed.png",
  EffectHitRate: "EffectHit.png",
  ResistanceRate: "Resistance.png",
});

const EQUIP_STAT_ICONS_BY_LABEL = Object.freeze({
  "攻击力": "Attack.png",
  "防御力": "Defense.png",
  "生命值": "Health.png",
  "暴击率": "Critical.png",
  "暴击伤害": "CriticalDamage.png",
  "速度": "Speed.png",
  "效果命中": "EffectHit.png",
  "效果抵抗": "Resistance.png",
});

function equipStatIcon(prop = {}) {
  const file = EQUIP_STAT_ICONS[prop.type] || EQUIP_STAT_ICONS_BY_LABEL[prop.label] || "EffectHit.png";
  return `/assets/stats/${file}`;
}

function equipStatHTML(prop = {}, className) {
  const label = prop.label || "属性";
  const value = prop.text != null ? prop.text : "—";
  return `<div class="${className}" title="${label}">
    <img class="eq-stat-icon" src="${equipStatIcon(prop)}" alt="${label}">
    <strong>${value}</strong>
  </div>`;
}

// 装备详情卡片 HTML（秘密商店手选 + 讨伐掉落弹窗共用同一套模组）
function equipCardHTML(c, stateTag = "") {
  const tier = Math.max(0, Math.min(4, Number(c.classLV) || 0));
  const enhance = Math.max(0, Math.round(Number(c.enhance) || 0));
  const main = equipStatHTML(c.mainProp, "eq-main");
  const subs = (c.subProps || []).slice(0, 4)
    .map((prop) => equipStatHTML(prop, "eq-sub"))
    .join("");
  const name = c.name || "未知装备";

  return `<div class="eq-card-shell">
    <div class="eq-card" data-tier="${tier}">
    <div class="eq-visual">
      <div class="eq-icon-frame">
        <img class="eq-img${c.img ? "" : " eq-img-placeholder"}" src="${c.img || "/assets/ui/equipment.png"}" alt="${c.img ? name : ""}">
        ${c.level != null ? `<span class="eq-level">${c.level}</span>` : ""}
        ${enhance > 0 ? `<span class="eq-enhance">+${enhance}</span>` : ""}
        ${c.setIcon ? `<span class="eq-set-mark" title="${c.setCN || ""}套装"><img src="${c.setIcon}" alt="${c.setCN || ""}套装"></span>` : ""}
      </div>
    </div>
    <div class="eq-detail">
      <div class="eq-name">${name}</div>
      <div class="eq-kind"><span>${c.tierName || ""}</span>${c.slotCN ? `<i>·</i><b>${c.slotCN}</b>` : ""}</div>
      ${stateTag}
      ${main}
      <div class="eq-score"><strong>${c.auxScore != null ? c.auxScore : "—"}</strong></div>
      <div class="eq-divider"></div>
      <div class="eq-subs">${subs}</div>
    </div>
    </div>
  </div>`;
}

// 装备卡片按固定 360x180 设计，外层容器根据可用宽度等比例缩放。
const equipCardResizeObserver = typeof ResizeObserver !== "undefined"
  ? new ResizeObserver((entries) => entries.forEach((entry) => fitEquipCardShell(entry.target)))
  : null;
const equipCardMutationObserver = typeof MutationObserver !== "undefined"
  ? new MutationObserver((records) => records.forEach((record) => record.addedNodes.forEach((node) => {
    if (node.nodeType !== 1) return;
    if (node.matches?.(".eq-card-shell")) observeEquipCardShell(node);
    node.querySelectorAll?.(".eq-card-shell").forEach(observeEquipCardShell);
  })))
  : null;

function fitEquipCardShell(shell) {
  if (!shell?.isConnected) return;
  const width = Math.min(360, Math.max(0, shell.getBoundingClientRect().width));
  if (!width) return;
  const scale = width / 360;
  shell.style.setProperty("--eq-card-scale", scale.toFixed(5));
  shell.style.height = `${(180 * scale).toFixed(2)}px`;
}

function observeEquipCardShell(shell) {
  if (!shell || shell.dataset.eqObserved) return;
  shell.dataset.eqObserved = "1";
  equipCardResizeObserver?.observe(shell);
  fitEquipCardShell(shell);
}

function initEquipCardShells() {
  document.querySelectorAll(".eq-card-shell").forEach(observeEquipCardShell);
  equipCardMutationObserver?.observe(document.body, { childList: true, subtree: true });
  window.addEventListener("resize", () => {
    document.querySelectorAll(".eq-card-shell").forEach(fitEquipCardShell);
  }, { passive: true });
}

// 传说装备手选：状态存 ACCOUNTS[email].pick（按账号），不绑定 DOM——
// 切走账号则卡片保留、循环挂起(await 未 resolve)；切回来 renderPickArea 恢复继续选。
// 每个账号页面只显示自己的商店卡片。
function pickLegendaries(id, cards, round, email = activeAccount) {
  return new Promise((resolve) => {
    const a = ACCOUNTS[email];
    if (!a) { resolve(); return; }
    a.pick = {
      id, cards, round,
      cur: 0,
      decided: new Array(cards.length).fill(null),
      resolve,
    };
    renderPickArea(email);
  });
}

// 把某账号的手选状态渲染到其商店卡片区（仅当前查看该账号时才画进 DOM）。
function renderPickArea(email) {
  const a = ACCOUNTS[email];
  const pk = a && a.pick;
  if (!pk) return;
  if (email !== activeAccount) return;           // 不是当前账号：不渲染（切回来时再画）
  const area = $(`cardArea-${pk.id}`);
  if (!area) return;
  const { cards, decided, cur, round } = pk;
  const c = cards[cur];
  const st = decided[cur];
  const remain = decided.filter((x) => x === null).length;
  const stateTag = st === "buy" ? `<span class="eq-flag buy">已购买</span>`
    : st === "skip" ? `<span class="eq-flag skip">已跳过</span>` : "";
  area.innerHTML = `
    ${equipCardHTML(c, stateTag)}
    <div class="card-foot">
      <div class="card-nav">
        <button type="button" class="cnav" data-nav="-1" ${cur === 0 ? "disabled" : ""}>◀</button>
        <span class="cidx">第 ${round} 屏 · 传说 ${cur + 1}/${cards.length}</span>
        <button type="button" class="cnav" data-nav="1" ${cur === cards.length - 1 ? "disabled" : ""}>▶</button>
      </div>
      <div class="card-btns">
        <button type="button" class="btn ghost card-skip">${st === "skip" ? "已跳过" : "跳过"}</button>
        <button type="button" class="btn primary card-buy" ${st ? "disabled" : ""}>${st === "buy" ? "已购买" : "购买"}</button>
      </div>
      <div class="card-hint">全部处理完后继续下一次刷新（剩余 ${remain} 张未决策）</div>
    </div>`;
  area.querySelectorAll(".cnav").forEach((b) =>
    b.addEventListener("click", () => { pk.cur += parseInt(b.dataset.nav); renderPickArea(email); })
  );
  area.querySelector(".card-skip").addEventListener("click", () => { pk.decided[pk.cur] = "skip"; pickNext(email); });
  area.querySelector(".card-buy").addEventListener("click", async () => {
    const buyBtn = area.querySelector(".card-buy");
    buyBtn.disabled = true; buyBtn.textContent = "购买中…";
    const c2 = pk.cards[pk.cur];
    try {
      const res = await api("/api/shop/buy", { account: email, index: c2.index });
      if (res.status) applyStatus(email, res.status, res.nextClaims);
      if (res.ok) { logTo(email, `${SHOP_TAG} ${res.detail}`, "ok"); pk.decided[pk.cur] = "buy"; }
      else { logTo(email, `${SHOP_TAG} ${res.detail}`, "err"); pk.decided[pk.cur] = "skip"; }
    } catch (e) { logTo(email, `购买出错：${e.message}`, "err"); pk.decided[pk.cur] = "skip"; }
    pickNext(email);
  });
}

// 手选推进到下一张未决策；全部决策完 → 清状态 + 恢复空占位 + resolve（放行刷新循环）。
function pickNext(email) {
  const a = ACCOUNTS[email];
  const pk = a && a.pick;
  if (!pk) return;
  if (pk.decided.every((x) => x !== null)) {
    const resolve = pk.resolve;
    a.pick = null;
    if (email === activeAccount) {
      const area = $(`cardArea-${pk.id}`);
      if (area) area.innerHTML = CARD_EMPTY;
    }
    resolve();
    return;
  }
  const after = pk.decided.findIndex((x, i) => x === null && i > pk.cur);
  pk.cur = after >= 0 ? after : pk.decided.findIndex((x) => x === null);
  renderPickArea(email);
}

// ---------- 星源商店刷新 ----------
const STAR_SOURCE_SHOP_LS = "ark_star_source_shop_cfg";
const starSourceShopLsKey = () => STAR_SOURCE_SHOP_LS + "::" + (activeAccount || "_");
const starSourceRunning = {};
const starSourceStopRequested = {};

function starSourceSchemeOptions() {
  return EQUIP_FILTER_SCHEMES_DATA.map((scheme) => ({
    val: scheme.id,
    label: filterEscape(scheme.name),
  }));
}

function getStarSourceShopCfg() {
  const schemes = EQUIP_FILTER_SCHEMES_DATA;
  const fallbackId = schemes.find((scheme) => scheme.id === "plan-star-shop")?.id || schemes[0]?.id || "";
  try {
    const value = JSON.parse(frontendStorageGet(starSourceShopLsKey())) || {};
    const schemeId = schemes.some((scheme) => scheme.id === value.schemeId) ? value.schemeId : fallbackId;
    return { count: clampCount(value.count || 10), schemeId };
  } catch {
    return { count: 10, schemeId: fallbackId };
  }
}

function setStarSourceShopCfg(patch) {
  frontendStorageSet(
    starSourceShopLsKey(),
    JSON.stringify({ ...getStarSourceShopCfg(), ...patch }),
  );
}

function starSourceStateHTML(state) {
  if (state?.cards?.length) {
    const title = "第 " + state.round + " 次刷新命中";
    const cardStates = state.cardStates || {};
    const buying = Object.values(cardStates).includes("buying");
    return '<div class="star-source-result-head"><span>' + title
      + '</span><div class="star-source-result-actions"><b>' + state.cards.length + ' 件装备</b>'
      + (state.kind === "matched" ? '<button type="button" class="btn ghost star-source-skip"'
        + (buying ? ' disabled' : '') + '>跳过</button>' : '') + '</div></div>'
      + '<div class="star-source-card-grid">'
      + state.cards.map((card, cardIndex) => {
        const shopIndex = Number.isInteger(card.shopIndex) ? card.shopIndex : cardIndex;
        const cardState = cardStates[shopIndex] || "";
        const stateTag = cardState === "bought" ? '<span class="eq-flag buy">已购买</span>' : "";
        const buttonText = cardState === "buying" ? "购买中…" : cardState === "bought" ? "已购买" : "购买";
        const error = (state.cardErrors || {})[shopIndex];
        return '<div class="star-source-card-item">' + equipCardHTML(card, stateTag)
          + '<div class="star-source-card-actions"><button type="button" class="btn primary star-source-buy" data-shop-index="'
          + shopIndex + '"' + (cardState === "buying" || cardState === "bought" ? ' disabled' : '')
          + '>' + buttonText + '</button></div>'
          + (error ? '<div class="star-source-card-error">' + filterEscape(error) + '</div>' : '')
          + '</div>';
      }).join("")
      + '</div>';
  }
  const icon = '<img class="star-source-empty-icon" src="/assets/items/109.png" alt="">';
  if (state?.kind === "querying") {
    return '<div class="card-empty">' + icon + '<div class="ce-text">正在同步商店状态</div></div>';
  }
  if (state?.kind === "running") {
    return '<div class="card-empty">' + icon + '<div class="ce-text">正在刷新 '
      + state.round + ' / ' + state.total + '</div></div>';
  }
  if (state?.kind === "error") {
    return '<div class="card-empty">' + icon + '<div class="ce-text">'
      + filterEscape(state.message || "刷新失败") + '</div></div>';
  }
  if (state?.kind === "empty") {
    const summaries = [];
    if (state.purchasedMatches) summaries.push("已购买 " + state.purchasedMatches + " 件命中装备");
    if (state.skippedMatches) summaries.push("已跳过 " + state.skippedMatches + " 轮命中装备");
    return '<div class="card-empty">' + icon + '<div class="ce-text">已完成 '
      + state.total + ' 次刷新<br>' + (summaries.length
        ? summaries.join("，")
        : '没有符合方案的装备') + '</div></div>';
  }
  return '<div class="card-empty">' + icon + '<div class="ce-text">等待刷新结果</div></div>';
}

function renderStarSourceState(id, state, email = activeAccount) {
  const account = ACCOUNTS[email];
  if (account) account.starSourceResult = { id, ...state };
  if (email !== activeAccount) return;
  const area = $("starSourceArea-" + id);
  if (!area) return;
  area.classList.toggle("has-results", !!state?.cards?.length);
  area.innerHTML = starSourceStateHTML(state);
  bindStarSourceResultActions(id, email);
}

function setStarSourceStartState(id, email, mode) {
  if (email !== activeAccount) return;
  const button = $("starshop-" + id)?.querySelector(".star-source-start");
  if (!button) return;
  const stopping = mode === "stopping";
  const active = mode === "running" || mode === "matched" || stopping;
  button.disabled = stopping;
  button.classList.toggle("task-stop-button", active);
  button.textContent = stopping ? "停止中…" : active ? "停止" : "▶ 开始刷新";
}

function resolveStarSourcePending(id, email, decision) {
  const account = ACCOUNTS[email];
  const pending = account?.starSourcePending;
  if (!pending || pending.id !== id) return false;
  account.starSourcePending = null;
  setStarSourceStartState(id, email, starSourceStopRequested[email] ? "stopping" : "running");
  pending.resolve(decision);
  return true;
}

function requestStarSourceStop(id, email) {
  if (!email || !starSourceRunning[email]) return;
  starSourceStopRequested[email] = true;
  const account = ACCOUNTS[email];
  const buying = Object.values(account?.starSourceResult?.cardStates || {}).includes("buying");
  if (!buying && account?.starSourcePending) {
    resolveStarSourcePending(id, email, { action: "stop" });
  } else {
    setStarSourceStartState(id, email, "stopping");
  }
}

function bindStarSourceResultActions(id, email) {
  const account = ACCOUNTS[email];
  const state = account?.starSourceResult;
  if (!account || state?.id !== id || email !== activeAccount) return;
  const area = $("starSourceArea-" + id);
  if (!area) return;

  area.querySelector(".star-source-skip")?.addEventListener("click", () => {
    const current = account.starSourceResult;
    if (Object.values(current?.cardStates || {}).includes("buying")) return;
    resolveStarSourcePending(id, email, { action: "skip" });
  });

  area.querySelectorAll(".star-source-buy").forEach((button) => button.addEventListener("click", async () => {
    const shopIndex = Number(button.dataset.shopIndex);
    let current = account.starSourceResult;
    if (!Number.isInteger(shopIndex) || current?.kind !== "matched") return;
    const card = current.cards.find((item, index) =>
      (Number.isInteger(item.shopIndex) ? item.shopIndex : index) === shopIndex);
    const cardStates = { ...(current.cardStates || {}), [shopIndex]: "buying" };
    const cardErrors = { ...(current.cardErrors || {}) };
    delete cardErrors[shopIndex];
    renderStarSourceState(id, { ...current, cardStates, cardErrors }, email);
    try {
      const result = await api("/api/star-source/buy", { account: email, index: shopIndex });
      if (result.status) applyStatus(email, result.status, result.nextClaims);
      current = account.starSourceResult;
      const nextStates = { ...(current.cardStates || {}) };
      const nextErrors = { ...(current.cardErrors || {}) };
      if (result.ok) {
        nextStates[shopIndex] = "bought";
        delete nextErrors[shopIndex];
        const equipmentName = [card?.setCN, card?.slotCN].filter(Boolean).join("·") || "目标装备";
        logTo(email, "[星源商店刷新] 已购买 " + equipmentName, "ok");
      } else {
        delete nextStates[shopIndex];
        nextErrors[shopIndex] = result.detail || "购买失败";
        logTo(email, "[星源商店刷新] " + nextErrors[shopIndex], "err");
      }
      renderStarSourceState(id, { ...current, cardStates: nextStates, cardErrors: nextErrors }, email);
      const allBought = result.ok && current.cards.every((item, index) => {
        const itemIndex = Number.isInteger(item.shopIndex) ? item.shopIndex : index;
        return nextStates[itemIndex] === "bought";
      });
      if (allBought) {
        resolveStarSourcePending(id, email, {
          action: "bought",
          count: current.cards.length,
        });
      }
    } catch (error) {
      current = account.starSourceResult;
      const nextStates = { ...(current.cardStates || {}) };
      const nextErrors = { ...(current.cardErrors || {}), [shopIndex]: error.message };
      delete nextStates[shopIndex];
      renderStarSourceState(id, { ...current, cardStates: nextStates, cardErrors: nextErrors }, email);
      logTo(email, "[星源商店刷新] 购买出错：" + error.message, "err");
    }
    if (starSourceStopRequested[email] && account.starSourcePending) {
      resolveStarSourcePending(id, email, { action: "stop" });
    }
  }));
}

function waitForStarSourceSkip(id, state, email) {
  return new Promise((resolve) => {
    const account = ACCOUNTS[email];
    if (!account) {
      resolve();
      return;
    }
    account.starSourcePending = { id, resolve };
    renderStarSourceState(id, {
      ...state,
      kind: "matched",
      cardStates: {},
      cardErrors: {},
    }, email);
    setStarSourceStartState(id, email, "matched");
  });
}

function renderStarSourceShopWidget(task) {
  const cfg = getStarSourceShopCfg();
  const options = starSourceSchemeOptions();
  return '<div class="shop-widget star-source-widget" id="starshop-' + task.id + '">'
    + '<div class="shop-head">'
    + '<div class="ticon">' + taskIconHTML(task.id, "🌈") + '</div>'
    + '<div class="tinfo"><div class="tname">' + task.name + '</div><div class="tdesc">' + task.desc + '</div></div>'
    + '<button type="button" class="mini shop-toggle">选项 ▸</button></div>'
    + '<div class="shop-body hidden"><div class="shop-grid"><div class="shop-left">'
    + '<div class="shop-row"><span class="shop-lbl">刷新次数</span><div class="count-box">'
    + '<button type="button" class="count-btn" data-d="-1">−</button>'
    + '<input type="text" inputmode="numeric" class="count-in star-source-count" value="' + cfg.count + '">'
    + '<span class="count-unit">次</span><button type="button" class="count-btn" data-d="1">+</button>'
    + '</div></div><div class="shop-row col"><span class="shop-lbl">筛选方案</span>'
    + dropdownHTML("star-source-scheme", options, cfg.schemeId, "暂无筛选方案")
    + '</div><button type="button" class="btn primary star-source-start">▶ 开始刷新</button></div>'
    + '<div class="shop-right"><div class="card-area star-source-card-area" id="starSourceArea-' + task.id + '">'
    + starSourceStateHTML() + '</div></div></div></div></div>';
}

function bindStarSourceShopWidget(id) {
  const root = $("starshop-" + id);
  if (!root) return;
  const body = root.querySelector(".shop-body");
  const toggle = root.querySelector(".shop-toggle");
  toggle.addEventListener("click", () => {
    body.classList.toggle("hidden");
    toggle.textContent = body.classList.contains("hidden") ? "选项 ▸" : "选项 ▾";
  });
  const countInput = root.querySelector(".star-source-count");
  countInput.addEventListener("input", () => {
    countInput.value = countInput.value.replace(/[^0-9]/g, "").slice(0, 3);
  });
  countInput.addEventListener("blur", () => {
    countInput.value = clampCount(countInput.value);
    setStarSourceShopCfg({ count: parseInt(countInput.value) });
  });
  root.querySelectorAll(".count-btn").forEach((button) => {
    button.addEventListener("click", () => {
      countInput.value = clampCount((parseInt(countInput.value) || 10) + parseInt(button.dataset.d));
      setStarSourceShopCfg({ count: parseInt(countInput.value) });
    });
  });
  bindDropdown(root.querySelector(".star-source-scheme"), (schemeId) => {
    setStarSourceShopCfg({ schemeId });
  });
  root.querySelector(".star-source-start").addEventListener("click", () => {
    if (starSourceRunning[activeAccount]) {
      requestStarSourceStop(id, activeAccount);
      return;
    }
    countInput.value = clampCount(countInput.value);
    setStarSourceShopCfg({ count: parseInt(countInput.value) });
    runStarSourceShop(id);
  });
  const saved = activeAcc()?.starSourceResult;
  if (saved?.id === id) renderStarSourceState(id, saved);
  if (starSourceRunning[activeAccount]) {
    setStarSourceStartState(id, activeAccount,
      starSourceStopRequested[activeAccount] ? "stopping" : (activeAcc()?.starSourcePending ? "matched" : "running"));
  }
}

function starSourceSchemeCanMatch(scheme) {
  return EQUIP_FILTER_SLOTS.some((slot) =>
    (scheme?.slots?.[slot.id] || []).some((rule) => Array.isArray(rule.sets) && rule.sets.length),
  );
}

async function runStarSourceShop(id) {
  const email = activeAccount;
  if (!email || starSourceRunning[email]) return;
  const cfg = getStarSourceShopCfg();
  const scheme = EQUIP_FILTER_SCHEMES_DATA.find((item) => item.id === cfg.schemeId);
  const root = $("starshop-" + id);
  if (!scheme || !starSourceSchemeCanMatch(scheme)) {
    const message = !scheme ? "请选择有效的筛选方案" : "当前方案尚未配置任何适用套装";
    renderStarSourceState(id, { kind: "error", message }, email);
    logTo(email, "[星源商店刷新] " + message, "warn");
    return;
  }

  const schemeSnapshot = JSON.parse(JSON.stringify(scheme));
  starSourceRunning[email] = true;
  starSourceStopRequested[email] = false;
  setStarSourceStartState(id, email, "running");
  logTo(email, "[星源商店刷新] 开始刷新：" + cfg.count + " 次，方案“" + scheme.name + "”");
  let completed = 0;
  let skippedMatches = 0;
  let purchasedMatches = 0;
  let failed = false;
  let stopped = false;
  try {
    renderStarSourceState(id, { kind: "querying" }, email);
    const initial = await api("/api/star-source/query", {
      account: email,
    });
    if (initial.status) applyStatus(email, initial.status, initial.nextClaims);
    if (!initial.ok) {
      const message = initial.error || "读取商店状态失败";
      renderStarSourceState(id, { kind: "error", message }, email);
      logTo(email, "[星源商店刷新] 同步商店状态失败：" + message, "err");
      failed = true;
    }
    for (let round = 1; round <= cfg.count; round++) {
      if (failed) break;
      await waitRequestDelayBetweenIterations(round, () => starSourceStopRequested[email]);
      if (starSourceStopRequested[email]) {
        stopped = true;
        break;
      }
      renderStarSourceState(id, { kind: "running", round, total: cfg.count }, email);
      const result = await api("/api/star-source/refresh", {
        account: email,
        scheme: schemeSnapshot,
      });
      completed = round;
      if (result.status) applyStatus(email, result.status, result.nextClaims);
      if (!result.ok) {
        const message = result.error || "未知错误";
        renderStarSourceState(id, { kind: "error", message }, email);
        logTo(email, "[星源商店刷新] 第 " + round + " 次刷新失败：" + message, "err");
        failed = true;
        break;
      }
      const matches = result.matches || [];
      if (matches.length) {
        if (starSourceStopRequested[email]) {
          renderStarSourceState(id, {
            kind: "matched", cards: matches, round, total: cfg.count,
            schemeName: scheme.name,
          }, email);
          stopped = true;
          break;
        }
        logTo(email, "[星源商店刷新] 第 " + round + " 次命中 " + matches.length + " 件装备，等待选择", "ok");
        const decision = await waitForStarSourceSkip(id, {
          cards: matches,
          round,
          total: cfg.count,
          schemeName: scheme.name,
        }, email);
        if (starSourceStopRequested[email]) {
          stopped = true;
          break;
        }
        if (decision?.action === "bought") {
          purchasedMatches += Number(decision.count) || matches.length;
          if (round < cfg.count) {
            logTo(email, "[星源商店刷新] 本轮命中装备已全部购买，继续刷新", "ok");
          }
        } else {
          skippedMatches += 1;
        }
        if (decision?.action !== "bought" && round < cfg.count) {
          logTo(email, "[星源商店刷新] 已跳过本轮命中，继续刷新");
        }
      }
      if (starSourceStopRequested[email]) {
        stopped = true;
        break;
      }
    }
    if (!failed && !stopped) {
      renderStarSourceState(id, {
        kind: "empty",
        total: completed,
        skippedMatches,
        purchasedMatches,
      }, email);
      logTo(email, "[星源商店刷新] 已完成 " + completed + " 次刷新"
        + (purchasedMatches ? "，共购买 " + purchasedMatches + " 件命中装备" : "")
        + (skippedMatches ? "，共跳过 " + skippedMatches + " 轮命中装备" : "")
        + (!purchasedMatches && !skippedMatches ? "，未发现符合方案的装备" : ""));
    }
  } catch (error) {
    renderStarSourceState(id, { kind: "error", message: error.message }, email);
    logTo(email, "[星源商店刷新] 刷新出错：" + error.message, "err");
  } finally {
    starSourceRunning[email] = false;
    delete starSourceStopRequested[email];
    if (ACCOUNTS[email]) ACCOUNTS[email].starSourcePending = null;
    setStarSourceStartState(id, email, "idle");
    refreshAccountStatus(email);
  }
}

// ---------- 装备活动刷新 ----------
const CUSTOMIZED_EQUIP_LS = "ark_customized_equip_cfg";
const customizedEquipLsKey = () => `${CUSTOMIZED_EQUIP_LS}::${activeAccount || "_"}`;
const customizedEquipRunning = {};
const customizedEquipStopRequested = {};

const CUSTOMIZED_EQUIP_PARTS = Object.freeze([
  { val: "Weapon", slot: "weapon", label: "武器" },
  { val: "Helmet", slot: "helmet", label: "头盔" },
  { val: "Armor", slot: "armor", label: "铠甲" },
  { val: "Necklace", slot: "necklace", label: "项链" },
  { val: "Ring", slot: "ring", label: "戒指" },
  { val: "Shoes", slot: "boots", label: "鞋子" },
]);

function getCustomizedEquipCfg() {
  const schemes = EQUIP_FILTER_SCHEMES_DATA;
  const fallbackId = schemes.find((scheme) => scheme.id === "plan-star-shop")?.id || schemes[0]?.id || "";
  try {
    const saved = JSON.parse(frontendStorageGet(customizedEquipLsKey())) || {};
    return {
      count: clampCount(saved.count || 20),
      schemeId: schemes.some((scheme) => scheme.id === saved.schemeId) ? saved.schemeId : fallbackId,
    };
  } catch {
    return { count: 20, schemeId: fallbackId };
  }
}

function setCustomizedEquipCfg(patch) {
  frontendStorageSet(customizedEquipLsKey(), JSON.stringify({ ...getCustomizedEquipCfg(), ...patch }));
}

function customizedEquipSchemeOptions() {
  return EQUIP_FILTER_SCHEMES_DATA.map((scheme) => ({ val: scheme.id, label: filterEscape(scheme.name) }));
}

// 过滤器方案变更后，主动任务卡片可能仍在 DOM 中（用户从工具箱返回时不会重建整页）。
// 这里只替换下拉框本身，保留任务状态与当前已选方案，并重新绑定自定义下拉交互。
function refreshFilterSchemeDropdowns() {
  const replaceDropdown = (slot, html) => {
    const template = document.createElement("template");
    template.innerHTML = html.trim();
    const replacement = template.content.firstElementChild;
    if (!replacement) return null;
    slot.replaceWith(replacement);
    return replacement;
  };

  const customizedRoots = document.querySelectorAll('[id^="customizedequip-"]');
  customizedRoots.forEach((root) => {
    const slot = root.querySelector(".customized-scheme");
    if (!slot) return;
    const cfg = getCustomizedEquipCfg();
    const replacement = replaceDropdown(
      slot,
      dropdownHTML("customized-scheme", customizedEquipSchemeOptions(), cfg.schemeId, "暂无筛选方案"),
    );
    if (!replacement) return;
    const id = root.id.replace(/^customizedequip-/, "");
    bindDropdown(replacement, (schemeId) => {
      setCustomizedEquipCfg({ schemeId });
      customizedEquipAction(id, "query", { scheme: customizedEquipSchemeSnapshot() });
    });
  });

  const starRoots = document.querySelectorAll('[id^="starshop-"]');
  starRoots.forEach((root) => {
    const slot = root.querySelector(".star-source-scheme");
    if (!slot) return;
    const cfg = getStarSourceShopCfg();
    const replacement = replaceDropdown(
      slot,
      dropdownHTML("star-source-scheme", starSourceSchemeOptions(), cfg.schemeId, "暂无筛选方案"),
    );
    if (!replacement) return;
    bindDropdown(replacement, (schemeId) => {
      setStarSourceShopCfg({ schemeId });
    });
  });

  document.querySelectorAll('[id^="shop-"]').forEach((root) => {
    const slot = root.querySelector(".shop-equip-scheme");
    if (!slot) return;
    const cfg = getShopCfg();
    const replacement = replaceDropdown(
      slot,
      dropdownHTML("shop-equip-scheme", starSourceSchemeOptions(), cfg.schemeId, "暂无筛选方案"),
    );
    if (!replacement) return;
    bindDropdown(replacement, (schemeId) => setShopCfg({ schemeId }));
  });

  document.querySelectorAll('[id^="auto-"]').forEach((root) => {
    const slot = root.querySelector(".shop-free-equip-scheme");
    if (!slot) return;
    const cfg = shopFreeEquipCfg();
    const replacement = replaceDropdown(
      slot,
      dropdownHTML("shop-free-equip-scheme", starSourceSchemeOptions(), cfg.schemeId, "暂无筛选方案"),
    );
    if (!replacement) return;
    bindDropdown(replacement, (schemeId) => setShopFreeEquipCfg({ equipmentSchemeId: schemeId }));
  });
}

function customizedEquipMainOptions(slot) {
  return filterMainStatsForSlot(slot).map((stat) => ({
    val: stat.id,
    label: `${filterStatIconHTML(stat)}<span>${filterEscape(stat.label)}</span>`,
  }));
}

function customizedEquipSchemeSnapshot() {
  const schemeId = getCustomizedEquipCfg().schemeId;
  const scheme = EQUIP_FILTER_SCHEMES_DATA.find((item) => item.id === schemeId);
  return scheme ? JSON.parse(JSON.stringify(scheme)) : null;
}

function customizedEquipRuleStatus(state, schemeId = getCustomizedEquipCfg().schemeId) {
  if (!state?.slot || !state?.set) return { valid: false, message: "请先完成套装与部位选择" };
  if (!state?.main) return { valid: false, message: "请先选择主属性" };
  const scheme = EQUIP_FILTER_SCHEMES_DATA.find((item) => item.id === schemeId);
  if (!scheme) return { valid: false, message: "请选择有效的副属性方案" };
  const rules = scheme.slots?.[state.slot] || [];
  const valid = rules.some((rule) =>
    rule.sets?.includes(state.set) && (!rule.main || rule.main === state.main || rule.forceScoreEnabled),
  );
  return valid
    ? { valid: true, message: `当前组合可使用“${scheme.name}”` }
    : { valid: false, message: "当前套装、部位与主属性在该方案中没有适用细则" };
}

// 活动装备的系列图标兜底：同一晶石系列在六个部位使用同一组基础资源。
const CUSTOMIZED_EQUIP_CRYSTAL_IMAGES = Object.freeze({
  Critical: { weapon: "E0061", helmet: "E0062", armor: "E0063", necklace: "E0064", ring: "E0065", boots: "E0066" },
  Hit: { weapon: "E0061", helmet: "E0062", armor: "E0063", necklace: "E0064", ring: "E0065", boots: "E0066" },
  Speed: { weapon: "E0061", helmet: "E0062", armor: "E0063", necklace: "E0064", ring: "E0065", boots: "E0066" },
  Attack: { weapon: "E0121", helmet: "E0122", armor: "E0123", necklace: "E0124", ring: "E0125", boots: "E0126" },
  Health: { weapon: "E0121", helmet: "E0122", armor: "E0123", necklace: "E0124", ring: "E0125", boots: "E0126" },
  Defense: { weapon: "E0121", helmet: "E0122", armor: "E0123", necklace: "E0124", ring: "E0125", boots: "E0126" },
  Resist: { weapon: "E0181", helmet: "E0182", armor: "E0183", necklace: "E0184", ring: "E0185", boots: "E0186" },
  Destruction: { weapon: "E0181", helmet: "E0182", armor: "E0183", necklace: "E0184", ring: "E0185", boots: "E0186" },
  Lifesteal: { weapon: "E0181", helmet: "E0182", armor: "E0183", necklace: "E0184", ring: "E0185", boots: "E0186" },
  Counter: { weapon: "E0181", helmet: "E0182", armor: "E0183", necklace: "E0184", ring: "E0185", boots: "E0186" },
  Pinch: { weapon: "E0241", helmet: "E0242", armor: "E0243", necklace: "E0244", ring: "E0245", boots: "E0246" },
  Immunity: { weapon: "E0241", helmet: "E0242", armor: "E0243", necklace: "E0244", ring: "E0245", boots: "E0246" },
  Rage: { weapon: "E0241", helmet: "E0242", armor: "E0243", necklace: "E0244", ring: "E0245", boots: "E0246" },
  Revenge: { weapon: "E0301", helmet: "E0302", armor: "E0303", necklace: "E0304", ring: "E0305", boots: "E0306" },
  Injury: { weapon: "E0301", helmet: "E0302", armor: "E0303", necklace: "E0304", ring: "E0305", boots: "E0306" },
  Penetration: { weapon: "E0301", helmet: "E0302", armor: "E0303", necklace: "E0304", ring: "E0305", boots: "E0306" },
  Torrent: { weapon: "E0301", helmet: "E0302", armor: "E0303", necklace: "E0304", ring: "E0305", boots: "E0306" },
});

function customizedEquipCardWithImage(card, state) {
  if (!card) return null;
  if (card.img) return card;
  const imageId = CUSTOMIZED_EQUIP_CRYSTAL_IMAGES[state?.set]?.[state?.slot];
  return imageId ? { ...card, img: `/assets/equip/${imageId}.png` } : card;
}

function customizedEquipCardHTML(state) {
  const current = customizedEquipCardWithImage(state?.currentCard, state);
  const candidate = customizedEquipCardWithImage(state?.candidateCard, state);
  if (current && candidate) {
    return `<div class="customized-equip-choices">
      <div class="customized-equip-choice">
        <div class="customized-choice-title">当前装备</div>
        <div class="customized-card-shell">${equipCardHTML(current, '<span class="eq-flag current">当前</span>')}</div>
        <button type="button" class="btn ghost customized-choice-btn" data-customized-choice="current">保留当前</button>
      </div>
      <div class="customized-equip-choice">
        <div class="customized-choice-title">候选装备</div>
        <div class="customized-card-shell">${equipCardHTML(candidate, '<span class="eq-flag buy">候选</span>')}</div>
        <button type="button" class="btn primary customized-choice-btn" data-customized-choice="candidate">采用候选</button>
      </div>
    </div>`;
  }
  const card = current || customizedEquipCardWithImage(state?.card, state);
  if (card) {
    return `<div class="customized-card-single"><div class="customized-card-shell">${equipCardHTML(card, state?.hasCandidate ? '<span class="eq-flag buy">候选</span>' : "")}</div></div>`;
  }
  return `<div class="card-empty"><img class="ce-icon-img" src="/assets/ui/equipment.png" alt=""><div class="ce-text">完成套装、部位和主属性选择后<br>当前装备会显示在这里</div></div>`;
}

function customizedEquipSetButtonHTML(state) {
  const selected = EQUIP_FILTER_SETS.find((set) => set.id === state?.set);
  return `<button type="button" class="customized-set-button" aria-expanded="false">
    ${selected ? `<img src="/assets/sets/${selected.icon}.png" alt=""><span>${selected.label}套装</span>` : '<span>选择套装</span>'}
  </button>`;
}

function renderCustomizedEquipWidget(task) {
  const cfg = getCustomizedEquipCfg();
  return `<div class="shop-widget customized-equip-widget" id="customizedequip-${task.id}">
    <div class="shop-head">
      <div class="ticon">${taskIconHTML(task.id, "🔧")}</div>
      <div class="tinfo"><div class="tname">${task.name}</div><div class="tdesc">${task.desc}</div></div>
      <button type="button" class="mini customized-equip-toggle">选项 ▸</button>
    </div>
    <div class="shop-body hidden"><div class="shop-grid customized-equip-grid"><div class="shop-left customized-equip-left">
      <div class="customized-field customized-set-field">
        <div class="customized-set-anchor">${customizedEquipSetButtonHTML()}
          <div class="customized-set-popover hidden">${CUSTOMIZED_EQUIP_SETS.map((set) => `<button type="button" data-customized-set="${set.id}" title="${set.label}"><img src="/assets/sets/${set.icon}.png" alt="${set.label}"></button>`).join("")}</div>
        </div>
        <button type="button" class="customized-reset" data-customized-reset="set" title="重置套装" aria-label="重置套装"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><use href="#ui-rotate-ccw"></use></svg></button>
      </div>
      <div class="customized-field"><span class="customized-label">部位</span><div class="customized-control customized-part-slot"></div><button type="button" class="customized-reset" data-customized-reset="part" title="重置部位" aria-label="重置部位"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><use href="#ui-rotate-ccw"></use></svg></button></div>
      <div class="customized-field"><span class="customized-label">主属性</span><div class="customized-control customized-main-slot"></div><button type="button" class="customized-reset" data-customized-reset="main" title="重置主属性" aria-label="重置主属性"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><use href="#ui-rotate-ccw"></use></svg></button></div>
      <div class="customized-field customized-scheme-field"><span class="customized-label">副属性方案</span><div class="customized-control">${dropdownHTML("customized-scheme", customizedEquipSchemeOptions(), cfg.schemeId, "暂无筛选方案")}</div></div>
      <div class="customized-rule-hint"></div>
      <div class="customized-start-row"><button type="button" class="btn primary customized-equip-start">▶ 开始刷新</button><div class="count-box"><input type="text" inputmode="numeric" class="count-in customized-equip-count" value="${cfg.count}"><span class="count-unit">次</span></div><button type="button" class="btn ghost customized-equip-reward">领取</button></div>
    </div><div class="shop-right"><div class="card-area customized-equip-card-area">${customizedEquipCardHTML()}</div></div></div></div>
  </div>`;
}

function customizedEquipIsRewarded(state) {
  return String(state?.stage || "").toLowerCase() === "rewarded";
}

function syncCustomizedEquipControls(root) {
  const state = root._customizedState || {};
  const stage = String(state?.stage || "");
  const busy = root.classList.contains("busy");
  const rewarded = customizedEquipIsRewarded(state);
  const running = !!customizedEquipRunning[activeAccount];
  const stopping = !!customizedEquipStopRequested[activeAccount];
  const actionSelector = ".customized-set-button, .customized-set-popover button, .customized-reset, "
    + ".customized-part-slot button, .customized-main-slot button, .customized-scheme button, "
    + ".customized-equip-count, .customized-choice-btn, .customized-equip-reward";
  root.querySelectorAll(actionSelector).forEach((control) => {
    control.disabled = busy || rewarded;
  });
  const start = root.querySelector(".customized-equip-start");
  if (start) {
    start.classList.toggle("task-stop-button", running);
    start.textContent = running ? (stopping ? "停止中…" : "停止") : "▶ 开始刷新";
    start.disabled = running ? stopping : busy || rewarded;
  }
  const reward = root.querySelector(".customized-equip-reward");
  if (reward) {
    reward.textContent = rewarded ? "已领取" : "领取";
    reward.classList.toggle("is-rewarded", rewarded);
    reward.disabled = busy || rewarded || stage !== "RefreshSubProp" || !state.currentCard;
    reward.setAttribute("aria-label", rewarded ? "装备已领取" : "领取当前装备");
  }
  if (busy || rewarded) return;
  root.querySelector(".customized-set-button").disabled = stage !== "ChooseSet";
  root.querySelectorAll(".customized-set-popover button").forEach((control) => {
    control.disabled = stage !== "ChooseSet";
  });
  root.querySelector(".customized-part .dd-btn").disabled = stage !== "ChoosePart";
  root.querySelector(".customized-main .dd-btn").disabled = stage !== "ChooseProp";
  if (start) start.disabled = stage !== "RefreshSubProp" || !customizedEquipRuleStatus(state).valid;
  root.querySelectorAll(".customized-choice-btn").forEach((control) => {
    control.disabled = stage !== "ChooseSubProp";
  });
  root.querySelector('[data-customized-reset="set"]').disabled = !state.set;
  root.querySelector('[data-customized-reset="part"]').disabled = !state.slot;
  root.querySelector('[data-customized-reset="main"]').disabled = !state.main;
}

function setCustomizedEquipBusy(root, busy) {
  root.classList.toggle("busy", busy);
  syncCustomizedEquipControls(root);
}

function updateCustomizedEquipWidget(id, state, message = "") {
  const root = $(`customizedequip-${id}`);
  if (!root) return;
  root._customizedState = state || {};
  const setAnchor = root.querySelector(".customized-set-anchor");
  const oldPopover = root.querySelector(".customized-set-popover");
  setAnchor.querySelector(".customized-set-button").outerHTML = customizedEquipSetButtonHTML(state);
  if (oldPopover) oldPopover.classList.add("hidden");
  const partOptions = [{ val: "", label: "选择部位" }, ...CUSTOMIZED_EQUIP_PARTS.map(({ val, label }) => ({ val, label }))];
  const partSlot = root.querySelector(".customized-part-slot");
  partSlot.innerHTML = dropdownHTML("customized-part", partOptions, state?.part || "", "选择部位");
  const mainSlot = root.querySelector(".customized-main-slot");
  const mainOptions = [{ val: "", label: "选择主属性" }, ...customizedEquipMainOptions(state?.slot)];
  mainSlot.innerHTML = dropdownHTML("customized-main", mainOptions, state?.main || "", "选择主属性");
  root.querySelector(".customized-equip-card-area").innerHTML = customizedEquipCardHTML(state);
  bindDropdown(partSlot.querySelector(".customized-part"), (part) => {
    if (part) customizedEquipAction(id, "choosePart", { part });
  });
  bindDropdown(mainSlot.querySelector(".customized-main"), (main) => {
    if (main) customizedEquipAction(id, "chooseMain", { part: state?.part, main });
  });
  bindCustomizedSetButton(id);
  root.querySelectorAll(".customized-choice-btn").forEach((button) => {
    button.addEventListener("click", () => customizedEquipAction(id, "chooseSub", {
      accept: button.dataset.customizedChoice === "candidate",
    }));
  });
  const rewarded = customizedEquipIsRewarded(state);
  const status = rewarded ? { valid: true, message: "装备已领取" } : customizedEquipRuleStatus(state);
  const hint = root.querySelector(".customized-rule-hint");
  hint.className = `customized-rule-hint ${status.valid ? "ok" : "warn"}`;
  hint.innerHTML = `<span>${status.valid ? "✓" : "!"}</span>${filterEscape(message || status.message)}`;
  root.classList.toggle("rewarded", rewarded);
  syncCustomizedEquipControls(root);
}

function bindCustomizedSetButton(id) {
  const root = $(`customizedequip-${id}`);
  const button = root?.querySelector(".customized-set-button");
  const popover = root?.querySelector(".customized-set-popover");
  if (!button || !popover) return;
  button.addEventListener("click", () => {
    const open = popover.classList.toggle("hidden");
    button.setAttribute("aria-expanded", String(!open));
  });
}

async function customizedEquipAction(id, action, data = {}) {
  const root = $(`customizedequip-${id}`);
  if (!root || root._customizedAction) return null;
  root._customizedAction = true;
  setCustomizedEquipBusy(root, true);
  try {
    const result = await api("/api/customized-equip/action", { action, ...data });
    if (result.status) applyStatus(activeAccount, result.status, result.nextClaims);
    if (!result.ok) throw new Error(result.error || "操作失败");
    updateCustomizedEquipWidget(id, result);
    return result;
  } catch (error) {
    updateCustomizedEquipWidget(id, root._customizedState, error.message);
    return null;
  } finally {
    root._customizedAction = false;
    setCustomizedEquipBusy(root, false);
  }
}

function bindCustomizedEquipWidget(id) {
  const root = $(`customizedequip-${id}`);
  if (!root) return;
  const body = root.querySelector(".shop-body");
  const toggle = root.querySelector(".customized-equip-toggle");
  toggle.addEventListener("click", async () => {
    body.classList.toggle("hidden");
    toggle.textContent = body.classList.contains("hidden") ? "选项 ▸" : "选项 ▾";
    if (!body.classList.contains("hidden") && !root._customizedLoaded) {
      root._customizedLoaded = true;
      await customizedEquipAction(id, "query", { scheme: customizedEquipSchemeSnapshot() });
    }
  });
  bindCustomizedSetButton(id);
  root.querySelector(".customized-set-popover").addEventListener("click", (event) => {
    const option = event.target.closest("[data-customized-set]");
    if (option) customizedEquipAction(id, "chooseSet", { set: option.dataset.customizedSet });
  });
  root.querySelectorAll("[data-customized-reset]").forEach((button) => button.addEventListener("click", () => {
    customizedEquipAction(id, "reset", { level: button.dataset.customizedReset });
  }));
  bindDropdown(root.querySelector(".customized-scheme"), (schemeId) => {
    setCustomizedEquipCfg({ schemeId });
    customizedEquipAction(id, "query", { scheme: customizedEquipSchemeSnapshot() });
  });
  const count = root.querySelector(".customized-equip-count");
  count.addEventListener("input", () => { count.value = count.value.replace(/[^0-9]/g, "").slice(0, 3); });
  count.addEventListener("blur", () => {
    count.value = clampCount(count.value);
    setCustomizedEquipCfg({ count: Number(count.value) });
  });
  root.querySelector(".customized-equip-start").addEventListener("click", () => {
    if (customizedEquipRunning[activeAccount]) requestCustomizedEquipStop(id, activeAccount);
    else runCustomizedEquip(id);
  });
  root.querySelector(".customized-equip-reward").addEventListener("click", () => customizedEquipAction(id, "reward"));
  updateCustomizedEquipWidget(id, {});
}

async function runCustomizedEquip(id) {
  const email = activeAccount;
  const root = $(`customizedequip-${id}`);
  if (!email || !root || customizedEquipRunning[email]) return;
  const cfg = getCustomizedEquipCfg();
  const scheme = EQUIP_FILTER_SCHEMES_DATA.find((item) => item.id === cfg.schemeId);
  let state = root._customizedState || {};
  const validation = customizedEquipRuleStatus(state, cfg.schemeId);
  if (!scheme || String(state?.stage || "") !== "RefreshSubProp" || !validation.valid) {
    updateCustomizedEquipWidget(id, state, validation.valid ? "请先完成套装、部位和主属性选择" : validation.message);
    return;
  }
  customizedEquipRunning[email] = true;
  customizedEquipStopRequested[email] = false;
  setCustomizedEquipBusy(root, true);
  const schemeSnapshot = JSON.parse(JSON.stringify(scheme));
  logTo(email, `[装备活动刷新] 开始刷新：${cfg.count} 次，方案“${scheme.name}”`);
  try {
    let completed = 0;
    let stopped = false;
    for (let round = 1; round <= cfg.count; round++) {
      await waitRequestDelayBetweenIterations(round, () => customizedEquipStopRequested[email]);
      if (customizedEquipStopRequested[email]) {
        stopped = true;
        break;
      }
      const result = await api("/api/customized-equip/action", {
        account: email, action: "refresh", scheme: schemeSnapshot,
      });
      if (result.status) applyStatus(email, result.status, result.nextClaims);
      if (!result.ok) throw new Error(result.error || "刷新失败");
      completed = round;
      state = result;
      updateCustomizedEquipWidget(id, state, `第 ${round} / ${cfg.count} 次刷新${state.matches ? "，已命中" : ""}`);
      if (customizedEquipStopRequested[email]) {
        stopped = true;
        updateCustomizedEquipWidget(id, state, state.hasCandidate ? "已停止，保留当前与候选装备" : "已停止，保留当前副属性");
        break;
      }
      const last = round === cfg.count;
      if (!state.hasCandidate) {
        if (state.matches) {
          updateCustomizedEquipWidget(id, state, `第 ${round} 次生成的副属性已命中`);
          logTo(email, `[装备活动刷新] 第 ${round} 次生成的副属性命中方案`, "ok");
          break;
        }
        if (last) {
          updateCustomizedEquipWidget(id, state, `已完成 ${completed} 次刷新，保留当前副属性`);
          logTo(email, `[装备活动刷新] 已完成 ${completed} 次，保留当前副属性`, "warn");
          break;
        }
        continue;
      }
      if (state.matches || last) {
        updateCustomizedEquipWidget(id, state, state.matches
          ? `第 ${round} 次刷新已命中，请选择当前或候选装备`
          : `已完成 ${completed} 次刷新，请选择当前或候选装备`);
        logTo(email, state.matches
          ? `[装备活动刷新] 第 ${round} 次命中方案，等待选择当前或候选装备`
          : `[装备活动刷新] 已完成 ${completed} 次，等待选择当前或候选装备`, state.matches ? "ok" : "warn");
        break;
      }
      const rejected = await api("/api/customized-equip/action", {
        account: email, action: "chooseSub", accept: false,
      });
      if (!rejected.ok) throw new Error(rejected.error || "放弃副属性失败");
      state = rejected;
    }
    if (stopped) logTo(email, "[装备活动刷新] 已停止，未继续后续请求", "warn");
  } catch (error) {
    updateCustomizedEquipWidget(id, state, error.message);
    logTo(email, `[装备活动刷新] ${error.message}`, "err");
  } finally {
    customizedEquipRunning[email] = false;
    delete customizedEquipStopRequested[email];
    setCustomizedEquipBusy(root, false);
    refreshAccountStatus(email);
  }
}

function requestCustomizedEquipStop(id, email) {
  if (!email || !customizedEquipRunning[email]) return;
  customizedEquipStopRequested[email] = true;
  const root = $(`customizedequip-${id}`);
  if (root) syncCustomizedEquipControls(root);
}

// ---------- 扫荡自定义控件（讨伐扫荡 / 元素扫荡共用） ----------
// 配置按 任务id + 账号 双重命名空间（多个扫荡任务各自独立；队伍 ID 各账号不同）
const SWEEP_LS = "ark_sweep_cfg"; // {count, element, sceneId, teamId, quick}
const sweepLsKey = (id) => `${SWEEP_LS}::${id}::${activeAccount || "_"}`;
function getSweepCfg(id) {
  try {
    const v = JSON.parse(frontendStorageGet(sweepLsKey(id))) || {};
    return {
      count: v.count || 5, element: v.element || "", sceneId: v.sceneId || "", teamId: v.teamId || "",
      quick: v.quick !== false,  // 默认勾选：使用快速战斗券
    };
  } catch {
    return { count: 5, element: "", sceneId: "", teamId: "", quick: true };
  }
}
function setSweepCfg(id, patch) {
  frontendStorageSet(sweepLsKey(id), JSON.stringify({ ...getSweepCfg(id), ...patch }));
}
// 元素 -> 主题色（讨伐与元素探索属性下拉的色点）
const ELEMENT_COLOR = {
  fire: "#ff6b4a", water: "#5cd0ff", wood: "#4ce68a", dark: "#b98cff", light: "#ffd84a",
};

// 自定义下拉：options=[{val,label,element?}]。返回 HTML 串。
function dropdownHTML(cls, options, curVal, placeholder = "请选择") {
  const cur = options.find((o) => o.val === curVal) || options[0];
  const disabled = !options.length;
  const dot = (el) => (el ? `<span class="dd-dot" style="background:${ELEMENT_COLOR[el] || "var(--accent)"}"></span>` : "");
  const curLabel = cur ? `${dot(cur.element)}${cur.label}` : placeholder;
  const opts = options
    .map(
      (o) => `<div class="dd-opt ${o.val === curVal ? "sel" : ""}" data-val="${filterEscape(o.val)}">${dot(o.element)}<span>${o.label}</span></div>`
    )
    .join("");
  return `<div class="dropdown ${cls}" data-val="${filterEscape(cur ? cur.val : "")}">
    <button type="button" class="dd-btn" ${disabled ? "disabled" : ""}><span class="dd-cur">${curLabel}</span><span class="dd-caret">▾</span></button>
    <div class="dd-menu hidden">${opts}</div>
  </div>`;
}

// 根据视口和祖先裁剪区域选择展开方向，避免菜单超出选项卡片或滚动页。
function positionDropdown(root) {
  const btn = root.querySelector(".dd-btn"), menu = root.querySelector(".dd-menu");
  if (!btn || !menu || menu.classList.contains("hidden")) return;
  const viewport = window.visualViewport;
  let top = (viewport?.offsetTop || 0) + 8;
  let bottom = (viewport?.offsetTop || 0) + (viewport?.height || window.innerHeight) - 8;
  for (let parent = root.parentElement; parent; parent = parent.parentElement) {
    if (!/^(auto|scroll|hidden|clip)$/.test(getComputedStyle(parent).overflowY)) continue;
    const rect = parent.getBoundingClientRect();
    top = Math.max(top, rect.top + parent.clientTop + 8);
    bottom = Math.min(bottom, rect.top + parent.clientTop + parent.clientHeight - 8);
  }
  const rect = btn.getBoundingClientRect();
  if (rect.bottom <= top || rect.top >= bottom) {
    menu.classList.add("hidden");
    root.classList.remove("open");
    btn.setAttribute("aria-expanded", "false");
    return;
  }
  const above = Math.max(0, rect.top - top - 6), below = Math.max(0, bottom - rect.bottom - 6);
  menu.style.maxHeight = "260px";
  const desired = Math.min(260, menu.scrollHeight + 2);
  const upward = below < desired && above > below;
  menu.style.top = upward ? "auto" : "";
  menu.style.bottom = upward ? "calc(100% + 6px)" : "";
  menu.style.maxHeight = `${Math.floor(Math.min(260, upward ? above : below))}px`;
}

// 绑定一个下拉的交互：onPick(val) 回调
function bindDropdown(root, onPick) {
  const btn = root.querySelector(".dd-btn");
  const menu = root.querySelector(".dd-menu");
  if (!btn || !menu) return;
  // Some controls (such as the equipment filter) keep the root node while
  // replacing its menu markup. Remove the previous button handler first.
  if (root._ddButtonRef && root._ddButtonHandler) {
    root._ddButtonRef.removeEventListener("click", root._ddButtonHandler);
  }
  root._ddButtonRef = btn;
  root._ddOnPick = onPick;
  root._ddButtonHandler = (e) => {
    e.stopPropagation();
    document.querySelectorAll(".dd-menu").forEach((m) => { if (m !== menu) m.classList.add("hidden"); });
    document.querySelectorAll(".dropdown.open").forEach((d) => {
      if (d !== root) {
        d.classList.remove("open");
        d.querySelector(".dd-btn")?.setAttribute("aria-expanded", "false");
      }
    });
    const open = menu.classList.toggle("hidden");
    root.classList.toggle("open", !open);
    btn.setAttribute("aria-expanded", String(!open));
    if (!open) {
      positionDropdown(root);
      const selected = menu.querySelector(".dd-opt.sel");
      // 只滚动菜单本身，避免 scrollIntoView 将整个任务页带走。
      if (selected) menu.scrollTop = selected.offsetTop - (menu.clientHeight - selected.offsetHeight) / 2;
    }
  };
  btn.addEventListener("click", root._ddButtonHandler);
  btn.setAttribute("aria-expanded", String(!menu.classList.contains("hidden")));
  menu.querySelectorAll(".dd-opt").forEach((opt) =>
    opt.addEventListener("click", () => {
      const val = opt.dataset.val;
      root.dataset.val = val;
      root.querySelector(".dd-cur").innerHTML = opt.innerHTML;
      menu.querySelectorAll(".dd-opt").forEach((o) => {
        const selected = o === opt;
        o.classList.toggle("sel", selected);
        o.setAttribute("aria-selected", String(selected));
      });
      menu.classList.add("hidden");
      root.classList.remove("open");
      btn.setAttribute("aria-expanded", "false");
      root._ddOnPick?.(val);
    })
  );
}

function repositionDropdowns(event) {
  document.querySelectorAll(".dropdown.open").forEach((root) => {
    // 菜单内部滚动不影响它的展开位置。
    if (!(event.target instanceof Node && root.querySelector(".dd-menu")?.contains(event.target))) positionDropdown(root);
  });
}
document.addEventListener("scroll", repositionDropdowns, { capture: true, passive: true });
window.addEventListener("resize", repositionDropdowns);
window.visualViewport?.addEventListener("resize", repositionDropdowns);
window.visualViewport?.addEventListener("scroll", repositionDropdowns);

function renderSweepWidget(t) {
  const cfg = getSweepCfg(t.id);
  const sceneFields = `<div class="shop-row col">
      <div class="act-option-head"><span class="shop-lbl">选择属性</span><button type="button" class="act-options-refresh sweep-options-refresh" title="重新加载${t.name}选项" aria-label="重新加载${t.name}选项"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><use href="#ui-rotate-cw"></use></svg></button></div>
      <div class="dd-slot sweep-element-slot"><div class="dd-loading">加载中…</div></div>
    </div>
    <div class="shop-row col">
      <span class="shop-lbl">选择关卡</span>
      <div class="dd-slot sweep-scene-slot"><div class="dd-loading">加载中…</div></div>
    </div>`;
  return `<div class="shop-widget sweep-host" id="sweep-${t.id}" data-kind="${t.sweepKind || "hunt"}">
    <div class="shop-head">
      <div class="ticon">${taskIconHTML(t.id, "⚔️")}</div>
      <div class="tinfo"><div class="tname">${t.name}</div><div class="tdesc">${t.desc}</div></div>
      <button type="button" class="mini sweep-toggle">选项 ▸</button>
    </div>
    <div class="shop-body hidden">
      ${sceneFields}
      <div class="shop-row col">
        <span class="shop-lbl">队伍</span>
        <div class="dd-slot sweep-team-slot"><div class="dd-loading">加载中…</div></div>
      </div>
      <div class="shop-row">
        <span class="shop-lbl">扫荡次数</span>
        <div class="count-box">
          <button type="button" class="count-btn" data-d="-1">−</button>
          <input type="text" inputmode="numeric" class="count-in sweep-count" value="${cfg.count}">
          <span class="count-unit">次</span>
          <button type="button" class="count-btn" data-d="1">+</button>
        </div>
      </div>
      <label class="quick-ticket-toggle target-score-toggle sweep-quick-row">
        <input type="checkbox" role="switch" class="sweep-quick" ${cfg.quick ? "checked" : ""}>
        <span class="target-score-switch" aria-hidden="true"></span>
        <span>使用快速战斗券</span>
      </label>
      <button type="button" class="btn primary sweep-start">▶ 开始扫荡</button>
      <div class="sweep-summary" id="sweepSummary-${t.id}"></div>
    </div>
  </div>`;
}

async function bindSweepWidget(id) {
  const root = $(`sweep-${id}`);
  if (!root) return;
  const body = root.querySelector(".shop-body");
  const toggle = root.querySelector(".sweep-toggle");
  toggle.addEventListener("click", async () => {
    body.classList.toggle("hidden");
    toggle.textContent = body.classList.contains("hidden") ? "选项 ▸" : "选项 ▾";
    if (!body.classList.contains("hidden") && !root.dataset.loaded) {
      await loadSweepOptions(id);
    }
  });
  root.querySelector(".sweep-options-refresh").addEventListener("click", () => loadSweepOptions(id));

  const cntIn = root.querySelector(".sweep-count");
  cntIn.addEventListener("input", () => { cntIn.value = cntIn.value.replace(/[^0-9]/g, "").slice(0, 3); });
  cntIn.addEventListener("blur", () => { cntIn.value = clampCount(cntIn.value); setSweepCfg(id, { count: parseInt(cntIn.value) }); });
  root.querySelectorAll(".count-btn").forEach((btn) =>
    btn.addEventListener("click", () => {
      cntIn.value = clampCount((parseInt(cntIn.value) || 5) + parseInt(btn.dataset.d));
      setSweepCfg(id, { count: parseInt(cntIn.value) });
    })
  );

  const quick = root.querySelector(".sweep-quick");
  if (quick) quick.addEventListener("change", () => setSweepCfg(id, { quick: quick.checked }));

  root.querySelector(".sweep-start").addEventListener("click", () => {
    if (sweepRunning[activeAccount]) {
      if (sweepRunningTask[activeAccount] === id) requestSweepStop(id, activeAccount);
      return;
    }
    else runSweep(id);
  });
  if (sweepRunning[activeAccount] && sweepRunningTask[activeAccount] === id) {
    const button = root.querySelector(".sweep-start");
    const stopping = !!sweepStopRequested[activeAccount];
    button.disabled = stopping;
    button.classList.add("task-stop-button");
    button.textContent = stopping ? "停止中…" : "停止";
  }
}

async function loadSweepOptions(id) {
  const root = $(`sweep-${id}`);
  if (!root || root.dataset.loading === "1") return;
  const kind = root.dataset.kind || "hunt";
  const elementSlot = root.querySelector(".sweep-element-slot");
  const sceneSlot = root.querySelector(".sweep-scene-slot");
  const teamSlot = root.querySelector(".sweep-team-slot");
  const refreshButton = root.querySelector(".sweep-options-refresh");
  root.dataset.loading = "1";
  refreshButton.disabled = true;
  refreshButton.classList.add("loading");
  try {
    const { elements = [], scenes, teams } = await api(`/api/sweep/options?kind=${encodeURIComponent(kind)}`);
    const cfg = getSweepCfg(id);

    const teamOpts = teams.length
      ? teams.map((t) => ({ val: t.id, label: t.name }))
      : [{ val: "", label: "（无已保存的队伍）" }];

    const teamVal = cfg.teamId && teams.some((t) => t.id === cfg.teamId) ? cfg.teamId : teamOpts[0]?.val || "";
    if (elementSlot && elements.length) {
      const elementOpts = elements.map((e) => ({ val: e.id, label: e.name, element: e.id }));
      const savedScene = scenes.find((scene) => scene.id === cfg.sceneId);
      const elementVal = elements.some((e) => e.id === cfg.element)
        ? cfg.element
        : savedScene?.element || elementOpts[0]?.val || "";

      const renderSceneSelect = (element, preferredScene = "") => {
        const matching = scenes.filter((scene) => scene.element === element);
        const sceneOpts = matching.map((scene) => ({ val: scene.id, label: scene.name }));
        const sceneVal = matching.some((scene) => scene.id === preferredScene)
          ? preferredScene
          : sceneOpts[0]?.val || "";
        sceneSlot.innerHTML = dropdownHTML("sweep-scene", sceneOpts, sceneVal, "暂无已解锁关卡");
        bindDropdown(sceneSlot.querySelector(".dropdown"), (value) => setSweepCfg(id, { sceneId: value }));
        setSweepCfg(id, { element, sceneId: sceneVal });
      };

      elementSlot.innerHTML = dropdownHTML("sweep-element", elementOpts, elementVal, "请选择属性");
      bindDropdown(elementSlot.querySelector(".dropdown"), (value) => renderSceneSelect(value));
      renderSceneSelect(elementVal, cfg.sceneId);
    } else {
      const sceneOpts = scenes.map((s) => ({ val: s.id, label: s.name, element: s.element }));
      const sceneVal = cfg.sceneId && scenes.some((s) => s.id === cfg.sceneId) ? cfg.sceneId : sceneOpts[0]?.val || "";
      sceneSlot.innerHTML = dropdownHTML("sweep-scene", sceneOpts, sceneVal);
      bindDropdown(sceneSlot.querySelector(".dropdown"), (v) => setSweepCfg(id, { sceneId: v }));
      setSweepCfg(id, { sceneId: sceneVal });
    }
    setSweepCfg(id, { teamId: teamVal });
    teamSlot.innerHTML = dropdownHTML("sweep-team", teamOpts, teamVal);
    bindDropdown(teamSlot.querySelector(".dropdown"), (v) => setSweepCfg(id, { teamId: v }));
    root.dataset.loaded = "1";
  } catch (e) {
    delete root.dataset.loaded;
    if (elementSlot) elementSlot.innerHTML = `<div class="dd-loading err">加载失败</div>`;
    sceneSlot.innerHTML = `<div class="dd-loading err">加载失败</div>`;
    teamSlot.innerHTML = `<div class="dd-loading err">加载失败</div>`;
    log(`加载扫荡选项失败：${e.message}`, "err");
  } finally {
    delete root.dataset.loading;
    refreshButton.disabled = false;
    refreshButton.classList.remove("loading");
  }
}

// 同商店：账号锁定为发起时的账号，请求显式带 email、日志写该账号缓冲，切账号不串。
const sweepRunning = {};  // email -> bool
const sweepRunningTask = {};  // email -> 当前运行的扫荡任务 id
const sweepStopRequested = {};
async function runSweep(id) {
  const email = activeAccount;
  if (sweepRunning[email]) return;
  const cfg = getSweepCfg(id);
  if (!cfg.sceneId || !cfg.teamId) return log("请先选择关卡和队伍", "err");
  const root = $(`sweep-${id}`);
  const taskName = (TASKS.find((x) => x.id === id) || {}).name || "扫荡";
  const startBtn = root.querySelector(".sweep-start");
  const summary = $(`sweepSummary-${id}`);
  sweepRunning[email] = true;
  sweepRunningTask[email] = id;
  sweepStopRequested[email] = false;
  startBtn.disabled = false;
  startBtn.classList.add("task-stop-button");
  startBtn.textContent = "停止";
  const sceneName = (root.querySelector(".sweep-scene .dd-cur")?.textContent || cfg.sceneId).trim();
  logTo(email, `开始${taskName}：${sceneName} × ${cfg.count} 次`);
  let done = 0;
  let stopped = false;
  try {
    for (let i = 1; i <= cfg.count; i++) {
      await waitRequestDelayBetweenIterations(i, () => sweepStopRequested[email]);
      if (sweepStopRequested[email]) {
        stopped = true;
        break;
      }
      const r = await api("/api/sweep/run", { account: email, sceneId: cfg.sceneId, teamId: cfg.teamId, quick: cfg.quick });
      if (r.status) applyStatus(email, r.status, r.nextClaims);
      logResult({ ...r, name: taskName }, `第${i}次·`, email);
      if (sweepStopRequested[email]) {
        stopped = true;
        break;
      }
      if (!r.ok) { logTo(email, `第 ${i} 次未获得掉落，停止扫荡`, "err"); break; }
      done++;
    }
    if (stopped) logTo(email, `[${taskName}] 已停止，未继续后续请求`, "warn");
    if (summary && email === activeAccount) summary.textContent = `完成 ${done}/${cfg.count} 次`;
  } catch (e) {
    logTo(email, `扫荡出错：${e.message}`, "err");
  } finally {
    sweepRunning[email] = false;
    delete sweepRunningTask[email];
    delete sweepStopRequested[email];
    if (email === activeAccount && startBtn.isConnected) {
      startBtn.disabled = false;
      startBtn.classList.remove("task-stop-button");
      startBtn.textContent = "▶ 开始扫荡";
    }
    refreshAccountStatus(email);
  }
}

function requestSweepStop(id, email) {
  if (!email || !sweepRunning[email]) return;
  if (sweepRunningTask[email] && sweepRunningTask[email] !== id) return;
  sweepStopRequested[email] = true;
  const root = $(`sweep-${id}`);
  const button = root?.querySelector(".sweep-start");
  if (button) {
    button.disabled = true;
    button.classList.add("task-stop-button");
    button.textContent = "停止中…";
  }
}

// ---------- 虚构深渊：两波自由配队 ----------
// 这里只保存角色引用；开始时由后端按当前登录快照补齐装备/神器等战斗字段。
const ABYSS_LS = "ark_abyss_cfg";
const abyssLsKey = () => `${ABYSS_LS}::${activeAccount || "_"}`;
const abyssRunning = {};
const abyssCancelling = {};
const abyssBattleState = {};
const abyssRequests = {};
const abyssRewarding = {};
const abyssState = {};
const abyssPickerFilters = new Map();
let abyssPickerState = null;

function getAbyssCfg() {
  try {
    const value = JSON.parse(frontendStorageGet(abyssLsKey())) || {};
    const wave = (key) => {
      const input = value[key];
      if (Array.isArray(input)) return Array.from({ length: 4 }, (_, i) => input[i] || null);
      if (input && typeof input === "object") return Array.from({ length: 4 }, (_, i) => input[String(i)] || null);
      return [null, null, null, null];
    };
    return { sceneId: value.sceneId || "", upper: wave("upper"), lower: wave("lower"), autoAdvance: value.autoAdvance === true };
  } catch {
    return { sceneId: "", upper: [null, null, null, null], lower: [null, null, null, null], autoAdvance: false };
  }
}
function setAbyssCfg(patch) {
  const current = getAbyssCfg();
  frontendStorageSet(abyssLsKey(), JSON.stringify({ ...current, ...patch }));
}
function abyssRoleId(role) {
  const raw = role?.roleId ?? role?._id ?? role?.id ?? role?.RoleID ?? "";
  if (raw && typeof raw === "object") return String(raw.$oid ?? raw.oid ?? raw.id ?? "");
  return String(raw);
}
function abyssRoleName(role) { return role?.name || role?.Name || role?.id || role?._id || "未知角色"; }
function abyssRoleAvatar(role) { return role?.avatar || role?.Avatar || role?.icon || role?.Icon || ""; }
function abyssRoleList(data) {
  const source = data?.roles || data?.heroes || data?.characters || data?.roster?.heroes || [];
  return Array.isArray(source) ? source.filter((role) => abyssRoleId(role)) : [];
}
function abyssRoleCard(role, selected, disabled, position = "") {
  const id = abyssRoleId(role), name = abyssRoleName(role), avatar = abyssRoleAvatar(role);
  const wave = position ? (position.startsWith("上") ? "upper" : "lower") : "";
  const label = [name, role.level != null ? `等级 ${role.level}` : "", role.grade ? `潜能 ${role.grade}` : "", position ? `已编入${position}，再次点击移除` : "", disabled ? "暂不可选择" : ""].filter(Boolean).join("，");
  return `<button type="button" class="hero-card abyss-role-option${selected ? " selected" : ""}" data-role-id="${filterEscape(id)}" data-abyss-wave="${wave}" aria-label="${filterEscape(label)}" title="${filterEscape(label)}" aria-pressed="${selected}" ${disabled ? "disabled" : ""}>
    ${heroPortraitHTML({ ...role, id, name, avatar })}${selected ? `<span class="abyss-role-check${position ? " has-position" : ""}" aria-hidden="true">${filterEscape(position || "✓")}</span>` : ''}
  </button>`;
}
function renderAbyssWidget(t) {
  const cfg = getAbyssCfg();
  const running = abyssRunning[activeAccount], cancelling = abyssCancelling[activeAccount], rewarding = abyssRewarding[activeAccount];
  return `<div class="shop-widget abyss-widget" id="abyss-${t.id}">
    <div class="shop-head"><div class="ticon">${taskIconHTML(t.id, "🌌")}</div>
      <div class="tinfo"><div class="tname">${t.name}</div><div class="tdesc">${t.desc}</div></div>
      <button type="button" class="mini abyss-toggle">选项 ▸</button></div>
    <div class="shop-body hidden">
      <div class="shop-row col"><div class="act-option-head"><span class="shop-lbl">深渊关卡</span><button type="button" class="act-options-refresh abyss-options-refresh" title="重新加载深渊选项" aria-label="重新加载深渊选项"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><use href="#ui-rotate-cw"></use></svg></button></div>
        <div class="dd-slot abyss-scene-slot"><div class="dd-loading">展开时加载…</div></div></div>
      <details class="abyss-effects hidden" ${window.innerWidth > 680 ? "open" : ""}>
        <summary><span>关卡特殊效果</span><span class="abyss-effects-scene"></span><svg class="abyss-effects-chevron" viewBox="0 0 24 24" aria-hidden="true"><use href="#ui-chevron-down"></use></svg></summary>
        <div class="abyss-effects-content" aria-live="polite"></div>
      </details>
      <div class="abyss-team-grid">
        ${abyssWaveHTML(t.id, "upper", "上半", cfg.upper)}
        ${abyssWaveHTML(t.id, "lower", "下半", cfg.lower)}
      </div>
      <label class="quick-ticket-toggle target-score-toggle abyss-auto-row">
        <input type="checkbox" role="switch" class="abyss-auto-advance" ${cfg.autoAdvance ? "checked" : ""} ${running || rewarding ? "disabled" : ""}>
        <span class="target-score-switch" aria-hidden="true"></span><span>自动推关</span>
      </label>
      <div class="abyss-actions">
        <button type="button" class="btn abyss-rewards" ${running || rewarding ? "disabled" : ""}><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#ui-gift"></use></svg><span>${rewarding ? "领取中…" : "奖励领取"}</span></button>
        <button type="button" class="btn primary abyss-start${running ? " task-stop-button" : ""}" ${cancelling || rewarding ? "disabled" : ""}>${cancelling ? "中止中…" : running ? "中止挑战" : "▶ 开始挑战"}</button>
      </div>
      <div class="sweep-summary abyss-summary" id="abyssSummary-${t.id}"></div>
      <div class="abyss-battle-progress${abyssBattleState[activeAccount] ? "" : " hidden"}" aria-label="战斗进度">${abyssBattleHTML(abyssBattleState[activeAccount])}</div>
    </div>
  </div>`;
}
function renderAbyssEffects(id) {
  const root = $(`abyss-${id}`);
  const host = root?.querySelector(".abyss-effects");
  if (!host) return;
  const state = abyssState[id];
  const scene = state?.account === activeAccount
    ? state.scenes?.find((item) => item.id === getAbyssCfg().sceneId) : null;
  host.classList.toggle("hidden", !scene);
  const content = host.querySelector(".abyss-effects-content");
  host.querySelector(".abyss-effects-scene").textContent = scene?.name || "";
  if (!scene) { content.innerHTML = ""; return; }
  if (!scene.effects) {
    content.innerHTML = '<p class="abyss-effect-empty">关卡效果暂未提供</p>';
    return;
  }
  content.innerHTML = [ ["upper", "上半"], ["lower", "下半"] ].map(([wave, label]) => {
    const groups = scene.effects[wave] || {};
    return `<section class="abyss-effect-wave" aria-label="${label}特殊效果"><h4>${label}</h4>
      ${[ ["bonuses", "增益"], ["restrictions", "限制"] ].map(([group, title]) => {
        const effects = groups[group] || [];
        return `<div class="abyss-effect-group ${group}" role="group" aria-label="${title}">
          ${effects.length ? `<ul>${effects.map((effect) => `<li${effect.descriptionMissing ? ` class="abyss-effect-missing" title="效果编号：${filterEscape(effect.id)}"` : ""}>${filterEscape(effect.description)}</li>`).join("")}</ul>` : '<p class="abyss-effect-empty">无</p>'}
        </div>`;
      }).join("")}</section>`;
  }).join("");
}
function abyssBattleHTML(progress, labels = {}) {
  if (!progress) return "";
  const cards = (members) => (members || []).map((member) => {
    const value = Number(member.hpPercent);
    const percent = member.dead || !Number.isFinite(value) ? 0 : Math.max(0, Math.min(100, value));
    const name = member.name || member.staticId || "未知角色";
    const label = `${name}，生命 ${percent}%${member.dead ? "，已死亡" : ""}`;
    return `<div class="abyss-battle-member${member.dead ? " dead" : ""}" data-unit-id="${filterEscape(member.id)}" title="${filterEscape(label)}">
      ${heroPortraitHTML({ name: member.avatar ? name : "?", avatar: member.avatar })}
      <div class="abyss-hp-bar" role="progressbar" aria-label="${filterEscape(label)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><span style="width:${percent}%"></span></div>
      <span class="abyss-hp-percent">${percent}%</span>
    </div>`;
  }).join("");
  const stage = /^MysteriousRealm_(\d+)$/.exec(String(progress.sceneId || ""));
  const sceneLabel = labels.sceneLabel ?? (stage ? `第 ${Number(stage[1])} 关` : "");
  const waveLabel = labels.waveLabel ?? (Number(progress.wave) === 1 ? "下半" : "上半");
  return `${sceneLabel ? `<div class="abyss-battle-scene">${filterEscape(sceneLabel)}</div>` : ""}<div class="abyss-battle-wave" aria-live="polite">${filterEscape(waveLabel)}</div>
    <div class="abyss-battle-side enemies"><span class="abyss-battle-label">敌方</span><div class="abyss-battle-members">${cards(progress.enemies)}</div></div>
    <div class="abyss-battle-side allies"><span class="abyss-battle-label">我方</span><div class="abyss-battle-members">${cards(progress.allies)}</div></div>`;
}
function renderAbyssBattle(id) {
  const root = $(`abyss-${id}`);
  if (!root) return;
  const progress = abyssBattleState[activeAccount];
  const host = root.querySelector(".abyss-battle-progress");
  const html = abyssBattleHTML(progress);
  if (host && host._battleHTML !== html) {
    host.innerHTML = html;
    host._battleHTML = html;
    host.classList.toggle("hidden", !progress);
  }
  const button = root.querySelector(".abyss-start");
  if (button) {
    button.disabled = !!(abyssCancelling[activeAccount] || abyssRewarding[activeAccount]);
    button.classList.toggle("task-stop-button", !!abyssRunning[activeAccount]);
    button.textContent = abyssCancelling[activeAccount] ? "中止中…" : abyssRunning[activeAccount] ? "中止挑战" : "▶ 开始挑战";
  }
  const rewards = root.querySelector(".abyss-rewards");
  if (rewards) {
    rewards.disabled = !!(abyssRunning[activeAccount] || abyssRewarding[activeAccount]);
    rewards.querySelector("span").textContent = abyssRewarding[activeAccount] ? "领取中…" : "奖励领取";
  }
  const autoAdvance = root.querySelector(".abyss-auto-advance");
  if (autoAdvance) {
    autoAdvance.checked = getAbyssCfg().autoAdvance;
    autoAdvance.disabled = !!(abyssRunning[activeAccount] || abyssRewarding[activeAccount]);
  }
}
function applyAbyssBattle(email, battle) {
  if (!battle || typeof battle.running !== "boolean") return;
  abyssRunning[email] = !!(battle.running || abyssRequests[email]);
  if (!abyssRunning[email]) abyssCancelling[email] = false;
  if (battle.running && battle.progress) abyssBattleState[email] = battle.progress;
  else delete abyssBattleState[email];
  if (email === activeAccount) document.querySelectorAll(".abyss-widget").forEach((root) => renderAbyssBattle(root.id.slice(6)));
}
function abyssSceneStarsHTML(scene) {
  const stars = Array.isArray(scene?.stars) ? scene.stars : [];
  const earned = Array.from({ length: 4 }, (_, index) => stars[index] === true || Number(stars[index]) > 0);
  const count = earned.filter(Boolean).length;
  return `<span class="abyss-scene-stars" aria-label="通关星级 ${count} / 4">${earned.map((hasStar) => `<span class="${hasStar ? "earned" : "unearned"}" aria-hidden="true">${hasStar ? "★" : "☆"}</span>`).join("")}</span>`;
}
function abyssSlotHTML(id, wave, index, roleId) {
  const role = roleId ? (abyssState[id]?.roleMap?.[roleId]
    || activeAcc()?.roster?.heroes?.find((item) => abyssRoleId(item) === roleId)) : null;
  const name = role ? abyssRoleName(role) : roleId || "空槽";
  const roleMarkup = roleId
    ? heroPortraitHTML({ ...role, id: roleId, name: role ? name : "?", avatar: abyssRoleAvatar(role) })
    : '<span class="abyss-slot-plus">＋</span>';
  const detail = role ? [role.level != null ? `等级 ${role.level}` : "", role.grade ? `潜能 ${role.grade}` : ""].filter(Boolean).join("，") : "";
  const label = `${wave === "upper" ? "上半" : "下半"}位置 ${index + 1}：${name}${detail ? `，${detail}` : ""}`;
  return `<button type="button" class="abyss-slot${roleId ? " filled" : ""}" data-wave="${wave}" data-index="${index}" data-role-id="${filterEscape(roleId || "")}" aria-label="${filterEscape(label)}" title="${filterEscape(label)}">${roleMarkup}</button>`;
}
function abyssWaveHTML(id, wave, label, picks) {
  return `<section class="abyss-wave" data-wave="${wave}"><div class="abyss-wave-title"><span>${label}</span></div><div class="abyss-slots">${picks.map((roleId, index) => abyssSlotHTML(id, wave, index, roleId)).join("")}</div></section>`;
}
function abyssPicks(cfg) { return [...cfg.upper, ...cfg.lower].filter(Boolean); }
function abyssPayload(cfg) {
  return { sceneId: cfg.sceneId, scene_id: cfg.sceneId, upper: cfg.upper, lower: cfg.lower,
    waves: { upper: cfg.upper, lower: cfg.lower }, autoAdvance: cfg.autoAdvance };
}
function abyssEnsurePicker() {
  let modal = $("abyssRolePicker");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = "abyssRolePicker";
  modal.className = "abyss-picker-modal hidden";
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-modal", "true");
  modal.setAttribute("aria-labelledby", "abyssPickerTitle");
  modal.innerHTML = `<button type="button" class="abyss-picker-backdrop" data-abyss-close tabindex="-1" aria-label="关闭角色选择"></button>
    <div class="abyss-picker-dialog">
      <div class="abyss-picker-head"><h3 id="abyssPickerTitle">编辑深渊队伍</h3><button type="button" class="scheme-import-close" data-abyss-close title="关闭" aria-label="关闭角色选择"><svg aria-hidden="true"><use href="#ui-x"></use></svg></button></div>
      <div class="abyss-picker-top">
      <div class="abyss-picker-teams" aria-label="上下半队伍"></div>
      <div class="encyclopedia-filter-panel abyss-picker-filters" aria-label="角色筛选器">
        <div class="encyclopedia-filter-group"><span class="encyclopedia-filter-label">属性</span><div class="encyclopedia-filter-options" data-abyss-filter-options="element"></div></div>
        <div class="encyclopedia-filter-group"><span class="encyclopedia-filter-label">职业</span><div class="encyclopedia-filter-options" data-abyss-filter-options="job"></div></div>
        <div class="encyclopedia-filter-group encyclopedia-status-filter-group"><span class="encyclopedia-filter-label">状态</span><div class="encyclopedia-status-filter-controls">
          <button type="button" class="encyclopedia-status-filter-button" data-abyss-status-filter aria-haspopup="dialog" aria-expanded="false" aria-controls="encyclopediaStatusModal"><span>选择状态</span><svg aria-hidden="true"><use href="#ui-chevron-down"></use></svg></button>
          <div class="encyclopedia-status-selected-icons" data-abyss-status-icons role="list" aria-label="已选状态"></div>
        </div></div>
        <button type="button" class="mini hidden" data-abyss-filter-retry>重试加载筛选</button>
      </div>
      </div>
      <div class="abyss-picker-list"></div>
      <div class="abyss-picker-footer"><p class="abyss-picker-hint" role="status" aria-live="polite"></p><div class="abyss-picker-actions"><button type="button" class="btn primary" data-abyss-close>完成</button></div></div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener("click", (event) => {
    if (Date.now() < (abyssPickerState?.suppressClickUntil || 0)) { event.preventDefault(); return; }
    if (event.target.closest("[data-abyss-close]")) return closeAbyssPicker();
    const context = abyssPickerState;
    if (!context || context.account !== activeAccount) return closeAbyssPicker(false);
    const remove = event.target.closest("[data-abyss-remove-slot]");
    if (remove) {
      const cfg = getAbyssCfg();
      cfg[remove.dataset.wave][Number(remove.dataset.index)] = null;
      context.wave = remove.dataset.wave;
      context.index = Number(remove.dataset.index);
      commitAbyssPicker(cfg);
      return;
    }
    const slot = event.target.closest("[data-abyss-team-slot]");
    if (slot) {
      const target = { wave: slot.dataset.wave, index: Number(slot.dataset.index) };
      context.wave = target.wave;
      context.index = target.index;
      renderAbyssPicker();
      modal.querySelector(`[data-abyss-team-slot][data-wave="${target.wave}"][data-index="${target.index}"]`)?.focus();
      return;
    }
    const filter = event.target.closest("[data-abyss-filter]");
    if (filter && !filter.disabled) {
      context.filters[filter.dataset.abyssFilter] = filter.dataset.value || "ALL";
      renderAbyssPicker();
      modal.querySelector(`[data-abyss-filter="${filter.dataset.abyssFilter}"][data-value="${filter.dataset.value}"]`)?.focus();
      return;
    }
    if (event.target.closest("[data-abyss-filter-retry]")) return void loadAbyssPickerFilters(context);
    const status = event.target.closest("[data-abyss-status-filter]");
    if (status && !status.disabled) {
      openCharacterStatusFilter({ trigger: status, statuses: context.filters.statuses, onApply: (statuses) => {
        if (abyssPickerState !== context || context.account !== activeAccount) return;
        context.filters.statuses = statuses;
        renderAbyssPicker();
      } });
      return;
    }
    const button = event.target.closest(".abyss-role-option[data-role-id]");
    if (!button || button.disabled || !$(`abyss-${context.id}`)?.isConnected) return;
    const next = button.dataset.roleId;
    if (!(abyssState[context.id]?.roles || []).some((role) => abyssRoleId(role) === next)) return;
    const updated = getAbyssCfg();
    const occupied = ["upper", "lower"].find((wave) => updated[wave].includes(next));
    if (occupied) {
      context.wave = occupied;
      context.index = updated[occupied].indexOf(next);
      updated[context.wave][context.index] = null;
    } else {
      if (context.index == null) return;
      updated[context.wave][context.index] = next;
      advanceAbyssPickerSlot(context, updated);
    }
    commitAbyssPicker(updated);
    modal.querySelector(`.abyss-role-option[data-role-id="${next}"]`)?.focus({ preventScroll: true });
  });
  modal.addEventListener("dragstart", (event) => event.preventDefault());
  modal.addEventListener("pointerdown", (event) => {
    const context = abyssPickerState;
    if (!context || context.account !== activeAccount || !event.isPrimary || event.button !== 0 || context.drag
      || event.target.closest("[data-abyss-remove-slot]")) return;
    const slot = event.target.closest("[data-abyss-team-slot]");
    if (!slot || !getAbyssCfg()[slot.dataset.wave][Number(slot.dataset.index)]) return;
    context.drag = { source: { wave: slot.dataset.wave, index: Number(slot.dataset.index) }, slot,
      pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, rect: slot.getBoundingClientRect() };
    slot.setPointerCapture(event.pointerId);
  });
  modal.addEventListener("pointermove", (event) => {
    const context = abyssPickerState, drag = context?.drag;
    if (!drag || event.pointerId !== drag.pointerId) return;
    if (!drag.preview && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 8) return;
    event.preventDefault();
    if (!drag.preview) {
      const preview = drag.preview = drag.slot.cloneNode(true);
      preview.classList.add("abyss-drag-preview");
      preview.removeAttribute("data-abyss-team-slot");
      preview.removeAttribute("aria-pressed");
      preview.setAttribute("aria-hidden", "true");
      preview.tabIndex = -1;
      preview.style.width = `${drag.rect.width}px`;
      preview.style.height = `${drag.rect.height}px`;
      modal.appendChild(preview);
      drag.slot.classList.add("dragging");
    }
    drag.preview.style.left = `${event.clientX - (drag.startX - drag.rect.left)}px`;
    drag.preview.style.top = `${event.clientY - (drag.startY - drag.rect.top)}px`;
    const hit = document.elementFromPoint(event.clientX, event.clientY);
    const slot = hit?.closest(".abyss-editor-cell")?.querySelector("[data-abyss-team-slot]");
    modal.querySelectorAll(".drag-over").forEach((el) => el.classList.remove("drag-over"));
    drag.target = slot && modal.contains(slot) ? { wave: slot.dataset.wave, index: Number(slot.dataset.index) } : null;
    slot?.classList.add("drag-over");
  });
  modal.addEventListener("pointerup", (event) => {
    const context = abyssPickerState, drag = context?.drag;
    if (!drag || event.pointerId !== drag.pointerId) return;
    const moved = !!drag.preview, target = drag.target;
    cancelAbyssPickerDrag(context);
    if (!moved) return;
    event.preventDefault();
    context.suppressClickUntil = Date.now() + 250;
    if (target) moveAbyssPickerSlot(drag.source, target);
  });
  for (const type of ["pointercancel", "lostpointercapture"]) modal.addEventListener(type, (event) => {
    if (abyssPickerState?.drag?.pointerId === event.pointerId) cancelAbyssPickerDrag();
  });
  document.addEventListener("keydown", (event) => {
    if (modal.classList.contains("hidden") || !$("encyclopediaStatusModal")?.classList.contains("hidden")) return;
    if (event.key === "Escape") { event.preventDefault(); closeAbyssPicker(); }
    else if (event.key === "Tab") {
      const controls = [...modal.querySelectorAll(".abyss-picker-dialog button:not(:disabled)")].filter((el) => el.getClientRects().length);
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if ((!event.shiftKey && document.activeElement === last) || !modal.contains(document.activeElement)) { event.preventDefault(); first?.focus(); }
    }
  });
  return modal;
}
function openAbyssPicker(id, wave, index) {
  const modal = abyssEnsurePicker();
  if (!abyssPickerFilters.has(activeAccount)) abyssPickerFilters.set(activeAccount, { element: "ALL", job: "ALL", statuses: [] });
  const context = abyssPickerState = { id, wave, index, originWave: wave, originIndex: index, account: activeAccount, filters: abyssPickerFilters.get(activeAccount), returnFocus: document.activeElement, loading: !ENCYCLOPEDIA_STATE.index, metadata: new Map((ENCYCLOPEDIA_STATE.index || []).map((entry) => [entry.id, entry])) };
  renderAbyssPicker();
  modal.classList.remove("hidden");
  document.body.classList.add("abyss-picker-open");
  modal.querySelector(".scheme-import-close")?.focus();
  void loadAbyssPickerFilters(context);
}
function closeAbyssPicker(restoreFocus = true) {
  const context = abyssPickerState;
  cancelAbyssPickerDrag(context);
  if (ENCYCLOPEDIA_STATE.statusContext?.trigger?.closest("#abyssRolePicker")) closeEncyclopediaStatusFilter(false);
  $("abyssRolePicker")?.classList.add("hidden");
  document.body.classList.remove("abyss-picker-open");
  abyssPickerState = null;
  if (restoreFocus && context) {
    const slot = $(`abyss-${context.id}`)?.querySelector(`.abyss-slot[data-wave="${context.originWave}"][data-index="${context.originIndex}"]`);
    (slot || (context.returnFocus?.isConnected ? context.returnFocus : null))?.focus();
  }
}
async function loadAbyssPickerFilters(context) {
  context.loading = true;
  context.error = false;
  renderAbyssPicker();
  try {
    const index = await loadEncyclopediaIndex({ resources: false });
    context.metadata = new Map(index.map((entry) => [entry.id, entry]));
  } catch (error) {
    context.error = true;
    console.warn("读取深渊角色筛选资料失败：", error);
  } finally {
    context.loading = false;
    if (abyssPickerState === context && context.account === activeAccount) renderAbyssPicker();
  }
}
function renderAbyssPicker() {
  const context = abyssPickerState, modal = $("abyssRolePicker");
  if (!context || !modal || context.account !== activeAccount) return;
  cancelAbyssPickerDrag(context);
  const { id, filters, metadata } = context;
  const roles = abyssState[id]?.roles || [];
  const cfg = getAbyssCfg(), positions = new Map();
  for (const wave of ["upper", "lower"]) cfg[wave].forEach((roleId, index) => {
    if (roleId) positions.set(roleId, `${wave === "upper" ? "上" : "下"}${index + 1}`);
  });
  renderAbyssPickerTeams();
  for (const [type, options] of [["element", ENCYCLOPEDIA_ELEMENTS], ["job", ENCYCLOPEDIA_JOBS]]) {
    const host = modal.querySelector(`[data-abyss-filter-options="${type}"]`);
    host.innerHTML = options.map((option) => encyclopediaFilterButtonHTML(option, type, filters[type] === option.id, "abyss")).join("");
    host.querySelectorAll("button").forEach((button) => { button.disabled = context.loading || context.error; });
  }
  const statusButton = modal.querySelector("[data-abyss-status-filter]"), statusOptions = encyclopediaStatusOptions();
  statusButton.disabled = context.loading || context.error || !(statusOptions.buff.length + statusOptions.debuff.length);
  statusButton.classList.toggle("active", filters.statuses.length > 0);
  statusButton.setAttribute("aria-label", filters.statuses.length ? `状态筛选，已选 ${filters.statuses.length} 种状态` : "选择状态，不限状态");
  modal.querySelector("[data-abyss-status-icons]").innerHTML = characterStatusIconsHTML(filters.statuses);
  const filtered = context.loading || context.error ? roles : roles.filter((role) => characterMatchesFilters(metadata.get(role.staticId || role.StaticID || role.id) || role, filters));
  modal.querySelector(".abyss-picker-list").innerHTML = filtered.length
    ? filtered.map((role) => abyssRoleCard(role, positions.has(abyssRoleId(role)), context.index == null && !positions.has(abyssRoleId(role)), positions.get(abyssRoleId(role)))).join("")
    : `<div class="roster-empty abyss-picker-empty">${roles.length ? "暂无符合筛选条件的角色" : "暂无可用角色，请先刷新账号数据"}</div>`;
  modal.querySelector("[data-abyss-filter-retry]").classList.toggle("hidden", !context.error);
}

function advanceAbyssPickerSlot(context, cfg) {
  const next = Array.from({ length: 4 }, (_, offset) => (context.index + 1 + offset) % 4).find((index) => !cfg[context.wave][index]);
  if (next != null) { context.index = next; return; }
  const other = context.wave === "upper" ? "lower" : "upper";
  const otherIndex = cfg[other].findIndex((roleId) => !roleId);
  if (otherIndex >= 0) { context.wave = other; context.index = otherIndex; }
  else context.index = null;
}

function commitAbyssPicker(cfg) {
  const context = abyssPickerState;
  if (!context || context.account !== activeAccount) return;
  setAbyssCfg({ upper: cfg.upper, lower: cfg.lower });
  renderAbyssSlots(context.id);
  renderAbyssPicker();
}

function moveAbyssPickerSlot(source, target) {
  const context = abyssPickerState;
  if (!context || context.account !== activeAccount) return;
  const cfg = getAbyssCfg();
  const roleId = cfg[source.wave][source.index];
  if (!roleId) return;
  cfg[source.wave][source.index] = cfg[target.wave][target.index];
  cfg[target.wave][target.index] = roleId;
  context.wave = target.wave;
  context.index = target.index;
  commitAbyssPicker(cfg);
}

function cancelAbyssPickerDrag(context = abyssPickerState) {
  const drag = context?.drag;
  if (!drag) return;
  context.drag = null;
  drag.preview?.remove();
  drag.slot.classList.remove("dragging");
  $("abyssRolePicker")?.querySelectorAll(".drag-over").forEach((el) => el.classList.remove("drag-over"));
  if (drag.slot.hasPointerCapture(drag.pointerId)) drag.slot.releasePointerCapture(drag.pointerId);
}

function renderAbyssPickerTeams() {
  const context = abyssPickerState, modal = $("abyssRolePicker");
  if (!context || !modal || context.account !== activeAccount) return;
  const cfg = getAbyssCfg();
  modal.querySelector(".abyss-picker-teams").innerHTML = ["upper", "lower"].map((wave) => {
    const label = wave === "upper" ? "上半" : "下半";
    return `<section class="abyss-editor-wave" data-abyss-wave="${wave}"><div class="abyss-editor-wave-title"><strong>${label}</strong><small>${cfg[wave].filter(Boolean).length} / 4</small></div><div class="abyss-editor-slots">${cfg[wave].map((roleId, index) => {
      const role = abyssState[context.id]?.roleMap?.[roleId];
      const active = context.wave === wave && context.index === index;
      const name = role ? abyssRoleName(role) : roleId || "空槽";
      return `<div class="abyss-editor-cell"><button type="button" class="abyss-editor-slot${roleId ? " filled" : ""}${active ? " active" : ""}" data-abyss-team-slot data-wave="${wave}" data-index="${index}" aria-pressed="${active}" aria-label="${filterEscape(`${label}位置 ${index + 1}，${name}`)}" title="${filterEscape(name)}">${role ? heroPortraitHTML({ ...role, name, avatar: abyssRoleAvatar(role) }) : roleId ? `<span class="abyss-slot-label">${filterEscape(roleId)}</span>` : '<span class="abyss-slot-plus">＋</span>'}<span class="abyss-editor-position" aria-hidden="true">${index + 1}</span></button>${roleId ? `<button type="button" class="abyss-editor-remove" data-abyss-remove-slot data-wave="${wave}" data-index="${index}" aria-label="${filterEscape(`移除${label}位置 ${index + 1}的${name}`)}" title="移除角色"><svg aria-hidden="true"><use href="#ui-x"></use></svg></button>` : ""}</div>`;
    }).join("")}</div></section>`;
  }).join("");
  const hint = modal.querySelector(".abyss-picker-hint");
  hint.textContent = context.index == null ? "两队已满。可拖动调整位置，或点击已选角色移除。"
    : `${context.wave === "upper" ? "上半" : "下半"} ${context.index + 1}：${cfg[context.wave][context.index] ? "点击角色替换" : "点击角色加入"}；拖动队伍头像调整位置。`;
}
function renderAbyssSlots(id) {
  const root = $(`abyss-${id}`); if (!root) return;
  const cfg = getAbyssCfg();
  root.querySelectorAll(".abyss-wave").forEach((waveEl) => {
    const wave = waveEl.dataset.wave;
    waveEl.querySelector(".abyss-slots").innerHTML = cfg[wave].map((roleId, index) => abyssSlotHTML(id, wave, index, roleId)).join("");
  });
  root.querySelectorAll(".abyss-slot").forEach((slot) => slot.addEventListener("click", () => openAbyssPicker(id, slot.dataset.wave, Number(slot.dataset.index))));
}
async function loadAbyssOptions(id, refresh = false) {
  const root = $(`abyss-${id}`); if (!root || (root.dataset.loading === "1" && !refresh)) return;
  const account = activeAccount;
  root.dataset.loading = "1";
  const sceneSlot = root.querySelector(".abyss-scene-slot"), refreshBtn = root.querySelector(".abyss-options-refresh");
  if (refreshBtn) { refreshBtn.disabled = true; refreshBtn.classList.add("loading"); }
  try {
    const data = await api("/api/abyss/options");
    if (activeAccount !== account || !root.isConnected) return;
    const scenes = Array.isArray(data.scenes) ? data.scenes : (Array.isArray(data.levels) ? data.levels : []);
    let cfg = getAbyssCfg();
    // 账号配置也保存在后端，首次在新设备打开时用后端快照恢复本地配队。
    // 已存在本地配置时优先保留用户当前浏览器里的编辑内容。
    const saved = data?.config;
    if (frontendStorageGet(abyssLsKey()) == null) cfg.autoAdvance = saved?.autoAdvance === true;
    if (!cfg.sceneId && saved?.sceneId) cfg.sceneId = saved.sceneId;
    if (!cfg.upper.some(Boolean) && Array.isArray(saved?.waves?.upper)) {
      cfg.upper = Array.from({ length: 4 }, (_, i) => saved.waves.upper[i] || null);
    }
    if (!cfg.lower.some(Boolean) && Array.isArray(saved?.waves?.lower)) {
      cfg.lower = Array.from({ length: 4 }, (_, i) => saved.waves.lower[i] || null);
    }
    setAbyssCfg(cfg);
    const autoAdvance = root.querySelector(".abyss-auto-advance");
    if (autoAdvance) autoAdvance.checked = cfg.autoAdvance;
    const sceneOpts = scenes.map((scene) => ({
      val: scene.id || scene.sceneId || scene.StaticID,
      label: `<span class="abyss-scene-label"><span class="abyss-scene-name">${filterEscape(scene.name || scene.label || scene.id)}</span>${abyssSceneStarsHTML(scene)}</span>`,
    }));
    const sceneVal = cfg.sceneId && sceneOpts.some((s) => s.val === cfg.sceneId) ? cfg.sceneId : sceneOpts[0]?.val || "";
    setAbyssCfg({ sceneId: sceneVal });
    sceneSlot.innerHTML = dropdownHTML("abyss-scene", sceneOpts, sceneVal, "暂无已解锁的虚构深渊关卡");
    bindDropdown(sceneSlot.querySelector(".dropdown"), (value) => {
      setAbyssCfg({ sceneId: value });
      renderAbyssEffects(id);
    });
    const roles = abyssRoleList(data).length ? abyssRoleList(data) : (activeAcc()?.roster?.heroes || []);
    const roleMap = Object.fromEntries(roles.map((role) => [abyssRoleId(role), role]));
    abyssState[id] = { roles, roleMap, scenes, account };
    renderAbyssEffects(id);
    renderAbyssSlots(id);
    root.dataset.loaded = "1";
  } catch (error) {
    if (activeAccount !== account || !root.isConnected) return;
    sceneSlot.innerHTML = `<div class="dd-loading err">加载失败：${filterEscape(error.message)}</div>`;
    const roles = activeAcc()?.roster?.heroes || [];
    abyssState[id] = { roles, roleMap: Object.fromEntries(roles.map((role) => [abyssRoleId(role), role])), scenes: [], account };
    renderAbyssEffects(id);
    renderAbyssSlots(id);
    log(`加载虚构深渊选项失败：${error.message}`, "err");
  } finally {
    root.dataset.loading = "0";
    if (refreshBtn) { refreshBtn.disabled = false; refreshBtn.classList.remove("loading"); }
  }
}
function bindAbyssWidget(id) {
  const root = $(`abyss-${id}`); if (!root) return;
  const body = root.querySelector(".shop-body"), toggle = root.querySelector(".abyss-toggle");
  toggle.addEventListener("click", async () => { body.classList.toggle("hidden"); toggle.textContent = body.classList.contains("hidden") ? "选项 ▸" : "选项 ▾"; if (!body.classList.contains("hidden") && !root.dataset.loaded) await loadAbyssOptions(id); });
  root.querySelector(".abyss-options-refresh")?.addEventListener("click", () => loadAbyssOptions(id, true));
  root.querySelector(".abyss-start")?.addEventListener("click", () => runAbyss(id));
  root.querySelector(".abyss-rewards")?.addEventListener("click", () => claimAbyssRewards(id));
  root.querySelector(".abyss-auto-advance")?.addEventListener("change", (event) => setAbyssCfg({ autoAdvance: event.target.checked }));
  renderAbyssBattle(id);
}
async function claimAbyssRewards(id) {
  const email = activeAccount;
  if (!email || abyssRunning[email] || abyssRewarding[email]) return;
  abyssRewarding[email] = true;
  renderAbyssBattle(id);
  try {
    const result = await api("/api/abyss/rewards", { account: email });
    if (result.status) applyStatus(email, result.status, result.nextClaims);
    logResult({ ...result, name: "深渊奖励" }, "", email);
    if (activeAccount === email) {
      const summary = $(`abyss-${id}`)?.querySelector(".abyss-summary");
      if (summary) summary.textContent = result.detail || (result.ok ? "奖励领取完成" : "奖励领取失败");
    }
  } catch (error) {
    logTo(email, `深渊奖励领取失败：${error.message}`, "err");
    if (activeAccount === email) {
      const summary = $(`abyss-${id}`)?.querySelector(".abyss-summary");
      if (summary) summary.textContent = "奖励领取失败";
    }
  } finally {
    delete abyssRewarding[email];
    if (activeAccount === email) renderAbyssBattle(id);
  }
}
async function runAbyss(id) {
  const email = activeAccount; if (!email) return;
  if (abyssRewarding[email]) return;
  if (abyssRunning[email]) {
    if (abyssCancelling[email]) return;
    abyssCancelling[email] = true;
    const button = $(`abyss-${id}`)?.querySelector(".abyss-start");
    if (button) { button.disabled = true; button.textContent = "中止中…"; }
    try {
      const result = await api("/api/abyss/cancel", { account: email });
      if (!result.running) {
        abyssCancelling[email] = false;
        logTo(email, "挑战正在启动或已结束，请稍后确认运行状态", "info");
      }
    }
    catch (error) {
      abyssCancelling[email] = false;
      logTo(email, `中止深渊失败：${error.message}`, "err");
    } finally {
      if (activeAccount === email && !abyssCancelling[email]) {
        const current = $(`abyss-${id}`)?.querySelector(".abyss-start");
        if (current) { current.disabled = false; current.textContent = abyssRunning[email] ? "中止挑战" : "▶ 开始挑战"; }
      }
    }
    return;
  }
  const cfg = getAbyssCfg();
  if (!cfg.sceneId) return logTo(email, "请先选择深渊关卡", "err");
  if (!cfg.upper.some(Boolean) || !cfg.lower.some(Boolean)) return logTo(email, "上半和下半都至少需要 1 名角色", "err");
  const picks = abyssPicks(cfg); if (new Set(picks).size !== picks.length) return logTo(email, "上下半不能使用重复角色", "err");
  const root = $(`abyss-${id}`), button = root?.querySelector(".abyss-start"), summary = root?.querySelector(".abyss-summary");
  const request = {};
  abyssRequests[email] = request;
  delete abyssBattleState[email];
  if (summary) summary.textContent = "";
  abyssRunning[email] = true; if (button) { button.disabled = false; button.textContent = "中止挑战"; button.classList.add("task-stop-button"); }
  renderAbyssBattle(id);
  try {
    const result = await api("/api/abyss/run", { ...abyssPayload(cfg), account: email });
    if (result.status) applyStatus(email, result.status, result.nextClaims);
    logResult({ ...result, name: (TASKS.find((task) => task.id === id) || {}).name || "虚构深渊" }, "", email);
    if (summary?.isConnected && activeAccount === email) summary.textContent = result.detail || (result.ok ? "挑战完成" : "挑战失败");
  } catch (error) {
    logTo(email, `虚构深渊执行失败：${error.message}`, "err");
    if (summary?.isConnected && activeAccount === email) summary.textContent = "执行失败";
  } finally {
    if (abyssRequests[email] === request) delete abyssRequests[email];
    abyssRunning[email] = false;
    abyssCancelling[email] = false;
    delete abyssBattleState[email];
    if (activeAccount === email) {
      renderAbyssBattle(id);
      if ($(`abyss-${id}`)?.dataset.loaded === "1") await loadAbyssOptions(id, true);
    }
  }
}

// ---------- 活动关卡自定义控件（含助战好友选择） ----------
// 配置按账号命名空间（关卡/队伍/助战 各账号不同）
const ACT_LS = "ark_activity_cfg"; // {count, sceneId, teamId, supportCuid, quick, urgentEnabled, urgentSpecializations}
const actLsKey = () => `${ACT_LS}::${activeAccount || "_"}`;
function getActCfg() {
  try {
    const v = JSON.parse(frontendStorageGet(actLsKey())) || {};
    const legacyUrgent = ["gold", "catalyst"].includes(v.urgentSpecialization)
      ? [v.urgentSpecialization] : ["gold", "catalyst"];
    const urgentSpecializations = Array.isArray(v.urgentSpecializations)
      ? [...new Set(v.urgentSpecializations.filter((mode) => ["gold", "catalyst"].includes(mode)))]
      : legacyUrgent;
    return {
      count: v.count || 5, sceneId: v.sceneId || "", teamId: v.teamId || "",
      mode: v.mode === "progress" ? "progress" : "sweep", family: v.family || "",
      supportCuid: v.supportCuid || "", quick: v.quick !== false,
      urgentEnabled: v.urgentEnabled !== false,
      urgentSpecializations,
    };
  } catch {
    return {
      count: 5, sceneId: "", teamId: "", supportCuid: "", quick: true,
      mode: "sweep", family: "",
      urgentEnabled: true, urgentSpecializations: ["gold", "catalyst"],
    };
  }
}
function setActCfg(patch) {
  frontendStorageSet(actLsKey(), JSON.stringify({ ...getActCfg(), ...patch }));
}

function closeActivityUrgentHelp(scope = document) {
  scope.querySelectorAll(".act-urgent-option.help-open").forEach((item) => item.classList.remove("help-open"));
  scope.querySelectorAll("[data-urgent-help-text]").forEach((item) => item.classList.add("hidden"));
  scope.querySelectorAll("[data-urgent-help]").forEach((item) => item.setAttribute("aria-expanded", "false"));
}

function renderActivityWidget(t) {
  const cfg = getActCfg();
  return `<div class="shop-widget sweep-host" id="act-${t.id}">
    <div class="shop-head">
      <div class="ticon">${taskIconHTML(t.id, "🎫")}</div>
      <div class="tinfo"><div class="tname">${t.name}</div><div class="tdesc">${t.desc}</div></div>
      <button type="button" class="mini act-toggle">选项 ▸</button>
    </div>
    <div class="shop-body hidden">
      <div class="act-option-head"><div class="b-tabs act-tabs" role="tablist" aria-label="活动关卡模式">
        <button type="button" class="b-tab${cfg.mode !== "progress" ? " active" : ""}" role="tab" aria-selected="${cfg.mode !== "progress"}" data-act-mode="sweep">扫荡</button>
        <button type="button" class="b-tab${cfg.mode === "progress" ? " active" : ""}" role="tab" aria-selected="${cfg.mode === "progress"}" data-act-mode="progress">自动推图</button>
      </div><button type="button" class="act-options-refresh" title="重新加载活动关卡选项" aria-label="重新加载活动关卡选项"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><use href="#ui-rotate-cw"></use></svg></button></div>
      <div class="shop-row col act-progress-only${cfg.mode === "progress" ? "" : " hidden"}">
        <span class="shop-lbl">活动</span>
        <div class="dd-slot act-family-slot"><div class="dd-loading">加载中…</div></div>
        <p class="act-progress-hint">从首个未通关关卡继续，直至当前已开放的最后一关。剧情关自动使用指定队伍。</p>
      </div>
      <div class="shop-row col">
        <div class="act-option-head">
          <span class="shop-lbl">活动关卡</span>
        </div>
        <div class="dd-slot act-scene-slot"><div class="dd-loading">加载中…</div></div>
      </div>
      <div class="shop-row col">
        <span class="shop-lbl">队伍</span>
        <div class="dd-slot act-team-slot"><div class="dd-loading">加载中…</div></div>
      </div>
      <div class="shop-row col">
        <span class="shop-lbl">助战好友</span>
        <div class="act-support-slot"><div class="dd-loading">加载中…</div></div>
      </div>
      <div class="shop-row">
        <span class="shop-lbl">扫荡次数</span>
        <div class="count-box">
          <button type="button" class="count-btn" data-d="-1">−</button>
          <input type="text" inputmode="numeric" class="count-in act-count" value="${cfg.count}">
          <span class="count-unit">次</span>
          <button type="button" class="count-btn" data-d="1">+</button>
        </div>
      </div>
      <label class="quick-ticket-toggle target-score-toggle act-quick-row">
        <input type="checkbox" role="switch" class="act-quick" ${cfg.quick ? "checked" : ""}>
        <span class="target-score-switch" aria-hidden="true"></span>
        <span>使用快速战斗券</span>
      </label>
      <div class="act-urgent-control${cfg.urgentEnabled ? " enabled" : ""}">
        <label class="act-urgent-toggle target-score-toggle">
          <input type="checkbox" role="switch" class="act-urgent-enabled" ${cfg.urgentEnabled ? "checked" : ""}>
          <span class="target-score-switch" aria-hidden="true"></span>
          <span>自动清理紧急任务</span>
        </label>
        <div class="act-urgent-modes${cfg.urgentEnabled ? "" : " hidden"}" role="group" aria-label="紧急任务特化类型，可多选">
          <div class="act-urgent-option${cfg.urgentSpecializations.includes("gold") ? " selected" : ""}">
            <label class="act-urgent-choice">
              <input type="checkbox" data-urgent-mode="gold" ${cfg.urgentSpecializations.includes("gold") ? "checked" : ""}>
              ${checkboxUIHTML()}
              <span>金币特化</span>
            </label>
            <button type="button" class="act-urgent-help" data-urgent-help="gold" aria-label="查看金币特化说明" aria-expanded="false" title="查看说明">
              <svg aria-hidden="true"><use href="#ui-circle-help"></use></svg>
            </button>
            <p class="act-urgent-help-text hidden" data-urgent-help-text="gold">发现赏金任务时立即完成；其它任务最多暂存两个以提高目标出现概率，本轮活动结束后统一清理。</p>
          </div>
          <div class="act-urgent-option${cfg.urgentSpecializations.includes("catalyst") ? " selected" : ""}">
            <label class="act-urgent-choice">
              <input type="checkbox" data-urgent-mode="catalyst" ${cfg.urgentSpecializations.includes("catalyst") ? "checked" : ""}>
              ${checkboxUIHTML()}
              <span>催化剂特化</span>
            </label>
            <button type="button" class="act-urgent-help" data-urgent-help="catalyst" aria-label="查看催化剂特化说明" aria-expanded="false" title="查看说明">
              <svg aria-hidden="true"><use href="#ui-circle-help"></use></svg>
            </button>
            <p class="act-urgent-help-text hidden" data-urgent-help-text="catalyst">发现催化剂任务时立即完成；其它任务最多暂存两个以提高目标出现概率，本轮活动结束后统一清理。</p>
          </div>
        </div>
      </div>
      <button type="button" class="btn primary act-start">▶ 开始扫荡</button>
      <div class="sweep-summary" id="actSummary-${t.id}"></div>
      <div class="act-progress-status hidden" aria-live="polite"></div>
      <div class="act-battle-progress abyss-battle-progress hidden" aria-label="活动战斗进度"></div>
    </div>
  </div>`;
}

const actProgressRunning = {};
const actProgressRequests = {};
const actProgressState = {};
const actProgressStopping = {};

function updateActivityMode(id) {
  const root = $(`act-${id}`);
  if (!root) return;
  const running = !!actProgressRunning[activeAccount];
  const progress = running || getActCfg().mode === "progress";
  root.querySelectorAll(".act-sweep-only").forEach((node) => node.classList.toggle("hidden", progress));
  root.querySelectorAll(".act-progress-only").forEach((node) => node.classList.toggle("hidden", !progress));
  root.querySelectorAll("[data-act-mode]").forEach((button) => {
    const selected = button.dataset.actMode === (progress ? "progress" : "sweep");
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
    button.disabled = !!(running || actRunning[activeAccount]);
  });
  const button = root.querySelector(".act-start");
  if (progress) {
    button.textContent = running ? (actProgressStopping[activeAccount] ? "停止中…" : "停止推图") : "▶ 开始推图";
    button.disabled = !!actProgressStopping[activeAccount];
    button.classList.toggle("task-stop-button", running);
  } else if (!actRunning[activeAccount]) {
    button.textContent = "▶ 开始扫荡";
    button.disabled = false;
    button.classList.remove("task-stop-button");
  }
  root.querySelector(".act-options-refresh").disabled = !!(running || actRunning[activeAccount] || root.dataset.loading === "1");
  root.querySelectorAll(".dd-slot, .act-support-slot").forEach((node) => {
    node.inert = !!(running || actRunning[activeAccount]);
  });
  const status = root.querySelector(".act-progress-status");
  const state = actProgressState[activeAccount];
  status.classList.toggle("hidden", !progress || !state);
  if (state) {
    const wave = state.battle ? ` · 第 ${Number(state.battle.wave) + 1} 波` : "";
    status.textContent = `已通关 ${state.completed}/${state.total}${state.stageName ? ` · ${state.stageName}` : ""}${wave} · ${state.detail || ""}`;
  }
  const battle = progress && running ? state?.battle : null;
  const host = root.querySelector(".act-battle-progress");
  const html = battle ? abyssBattleHTML(battle, { sceneLabel: state.stageName || "",
    waveLabel: `第 ${Number(battle.wave) + 1} 波` }) : "";
  if (host._battleHTML !== html) {
    host.innerHTML = html;
    host._battleHTML = html;
  }
  host.classList.toggle("hidden", !battle);
}

function applyActivityProgress(email, snapshot) {
  if (!snapshot) return;
  actProgressRunning[email] = !!(snapshot.running || actProgressRequests[email]);
  if (!actProgressRunning[email]) delete actProgressStopping[email];
  if (snapshot.progress) actProgressState[email] = snapshot.progress;
  if (activeAccount === email) updateActivityMode("activity_scene");
}

async function runActivityProgress(id) {
  const email = activeAccount;
  if (actProgressRunning[email]) {
    actProgressStopping[email] = true;
    updateActivityMode(id);
    try { await api("/api/activity/progress/cancel", { account: email }); }
    catch (error) { delete actProgressStopping[email]; logTo(email, error.message, "err"); updateActivityMode(id); }
    return;
  }
  if (actRunning[email]) return;
  const cfg = getActCfg();
  if (!cfg.family || !cfg.teamId) return log("请先选择活动和队伍", "err");
  const request = {};
  actProgressRequests[email] = request;
  actProgressRunning[email] = true;
  delete actProgressState[email];
  updateActivityMode(id);
  logTo(email, "开始活动自动推图", "info");
  try {
    const result = await api("/api/activity/progress/run", { account: email, family: cfg.family,
      teamId: cfg.teamId, supportCuid: cfg.supportCuid || null });
    applyActivityProgress(email, result.activityProgress);
    if (result.status) applyStatus(email, result.status, result.nextClaims);
    logResult({ ...result, name: "活动自动推图" }, "", email);
    if (result.lines?.length && result.detail) logTo(email, `[活动自动推图] ${result.detail}`, result.ok ? "ok" : "warn");
    if (actProgressState[email]) actProgressState[email] = { ...actProgressState[email], battle: null, detail: result.detail };
  } catch (error) {
    logTo(email, `活动自动推图：${error.message}`, "err");
    actProgressState[email] = { ...(actProgressState[email] || {completed: 0, total: "—"}), battle: null, detail: error.message };
  } finally {
    if (actProgressRequests[email] === request) delete actProgressRequests[email];
    actProgressRunning[email] = false;
    delete actProgressStopping[email];
    if (email === activeAccount) updateActivityMode(id);
    refreshAccountStatus(email);
  }
}

// 助战好友卡片（信息页 hero-card 风格：圆头像+名，附 UID）。选中高亮。
// fav=已收藏（星标实心）；offline=收藏夹里但当前不在助战列表（用存档快照打）。
function supportCardHTML(f, selected, fav = false, offline = false) {
  const h = f.hero || {};
  const init = (h.name || h.id || "?").slice(0, 1);
  const av = h.avatar
    ? `<img src="${h.avatar}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'hero-ph',textContent:'${init}'}))">`
    : `<span class="hero-ph">${init}</span>`;
  const lv = h.level != null ? `<span class="hero-lv">${h.level}</span>` : "";
  // 潜能字形（同信息页团员卡：右下角角标）。imprint=0 的不挂。
  const grade = h.grade
    ? `<img class="hero-grade" src="${GRADE_ICON}${h.grade}.png" alt="${h.grade}" title="潜能 ${h.grade}">` : "";
  const star = `<button type="button" class="sup-star${fav ? " on" : ""}" data-cuid="${f.cuid}"
    title="${fav ? "取消收藏" : "加入收藏夹"}">${fav ? "★" : "☆"}</button>`;
  const off = offline ? `<span class="sup-off" title="当前不在助战列表，将使用收藏时保存的数据">存档</span>` : "";
  return `<div class="sup-card${selected ? " sel" : ""}" data-cuid="${f.cuid}">
    <div class="hero-pic">${av}${lv}${grade}</div>
    <div class="sup-meta">
      <div class="sup-name"><span class="sup-nm">${f.name || "(无名)"}</span>${off}</div>
      <div class="sup-uid">UID ${f.cuid}</div>
      <div class="sup-hero">${h.name || h.id || ""}</div>
    </div>
    ${star}
  </div>`;
}

async function bindActivityWidget(id) {
  const root = $(`act-${id}`);
  if (!root) return;
  const body = root.querySelector(".shop-body");
  const toggle = root.querySelector(".act-toggle");
  const sceneRow = root.querySelector(".act-scene-slot").closest(".shop-row");
  const countRow = root.querySelector(".act-count").closest(".shop-row");
  [sceneRow, countRow, root.querySelector(".act-quick-row"), root.querySelector(".act-urgent-control")]
    .forEach((node) => node.classList.add("act-sweep-only"));
  root.querySelectorAll("[data-act-mode]").forEach((button) => button.addEventListener("click", () => {
    if (actRunning[activeAccount] || actProgressRunning[activeAccount]) return;
    setActCfg({ mode: button.dataset.actMode });
    updateActivityMode(id);
  }));
  updateActivityMode(id);
  toggle.addEventListener("click", async () => {
    body.classList.toggle("hidden");
    toggle.textContent = body.classList.contains("hidden") ? "选项 ▸" : "选项 ▾";
    if (!body.classList.contains("hidden") && !root.dataset.loaded) {
      await loadActivityOptions(id);
    }
  });

  root.querySelector(".act-options-refresh").addEventListener("click", () => loadActivityOptions(id, true));

  const cntIn = root.querySelector(".act-count");
  cntIn.addEventListener("input", () => { cntIn.value = cntIn.value.replace(/[^0-9]/g, "").slice(0, 3); });
  cntIn.addEventListener("blur", () => { cntIn.value = clampCount(cntIn.value); setActCfg({ count: parseInt(cntIn.value) }); });
  root.querySelectorAll(".count-btn").forEach((btn) =>
    btn.addEventListener("click", () => {
      cntIn.value = clampCount((parseInt(cntIn.value) || 5) + parseInt(btn.dataset.d));
      setActCfg({ count: parseInt(cntIn.value) });
    })
  );

  const quick = root.querySelector(".act-quick");
  if (quick) quick.addEventListener("change", () => setActCfg({ quick: quick.checked }));

  const urgentControl = root.querySelector(".act-urgent-control");
  const urgentToggle = root.querySelector(".act-urgent-enabled");
  const urgentModes = root.querySelector(".act-urgent-modes");
  urgentToggle.addEventListener("change", () => {
    urgentControl.classList.toggle("enabled", urgentToggle.checked);
    urgentModes.classList.toggle("hidden", !urgentToggle.checked);
    if (!urgentToggle.checked) closeActivityUrgentHelp(root);
    setActCfg({ urgentEnabled: urgentToggle.checked });
  });
  root.querySelectorAll("[data-urgent-mode]").forEach((input) => {
    input.addEventListener("change", () => {
      input.closest(".act-urgent-option")?.classList.toggle("selected", input.checked);
      const modes = [...root.querySelectorAll("[data-urgent-mode]:checked")]
        .map((item) => item.dataset.urgentMode);
      setActCfg({ urgentSpecializations: modes });
    });
  });
  root.querySelectorAll("[data-urgent-help]").forEach((button) => {
    button.addEventListener("click", () => {
      const text = root.querySelector(`[data-urgent-help-text="${button.dataset.urgentHelp}"]`);
      const willOpen = text?.classList.contains("hidden");
      closeActivityUrgentHelp(root);
      if (willOpen && text) {
        button.closest(".act-urgent-option")?.classList.add("help-open");
        text.classList.remove("hidden");
        button.setAttribute("aria-expanded", "true");
      }
    });
  });

  root.querySelector(".act-start").addEventListener("click", () => {
    if (actProgressRunning[activeAccount] || getActCfg().mode === "progress") runActivityProgress(id);
    else if (actRunning[activeAccount]) requestActivityStop(id, activeAccount);
    else runActivity(id);
  });
  if (actRunning[activeAccount]) {
    const button = root.querySelector(".act-start");
    const stopping = !!actStopRequested[activeAccount];
    button.disabled = stopping;
    button.classList.add("task-stop-button");
    button.textContent = stopping ? "停止中…" : "停止";
  }
}

async function loadActivityOptions(id, refresh = false) {
  const root = $(`act-${id}`);
  if (!root || root.dataset.loading === "1") return false;
  const sceneSlot = root.querySelector(".act-scene-slot");
  const teamSlot = root.querySelector(".act-team-slot");
  const supSlot = root.querySelector(".act-support-slot");
  const refreshButton = root.querySelector(".act-options-refresh");
  const email = activeAccount;
  root.dataset.loading = "1";
  refreshButton.disabled = true;
  refreshButton.classList.add("loading");
  if (refresh || !root.dataset.loaded) {
    root.querySelector(".act-family-slot").innerHTML = `<div class="dd-loading">加载中…</div>`;
    sceneSlot.innerHTML = `<div class="dd-loading">加载中…</div>`;
    teamSlot.innerHTML = `<div class="dd-loading">加载中…</div>`;
    supSlot.innerHTML = `<div class="dd-loading">加载中…</div>`;
  }
  try {
    const { scenes, teams, supports, favorites, activities = [] } = await api("/api/activity/options");
    if (activeAccount !== email || !root.isConnected) return false;
    const cfg = getActCfg();
    const familySlot = root.querySelector(".act-family-slot");
    const family = activities.some((a) => a.id === cfg.family) ? cfg.family : activities[0]?.id || "";
    setActCfg({ family });
    familySlot.innerHTML = dropdownHTML("act-family", activities.length
      ? activities.map((a) => ({ val: a.id, label: `${a.name} · 已通关 ${a.completed}/${a.total}` }))
      : [{ val: "", label: "（当前没有开放的活动）" }], family);
    bindDropdown(familySlot.querySelector(".dropdown"), (v) => setActCfg({ family: v }));

    // 后端只回当期活动的全部已解锁关卡（已结束的按排期表过滤掉），可能一个都没有
    const sceneOpts = scenes.length
      ? scenes.map((s) => ({ val: s.id, label: s.name }))
      : [{ val: "", label: "（当前没有已解锁的活动关卡）" }];
    const teamOpts = teams.length
      ? teams.map((t) => ({ val: t.id, label: t.name }))
      : [{ val: "", label: "（无已保存的队伍）" }];

    const sceneVal = cfg.sceneId && scenes.some((s) => s.id === cfg.sceneId) ? cfg.sceneId : sceneOpts[0]?.val || "";
    const teamVal = cfg.teamId && teams.some((t) => t.id === cfg.teamId) ? cfg.teamId : teamOpts[0]?.val || "";
    setActCfg({ sceneId: sceneVal, teamId: teamVal });

    sceneSlot.innerHTML = dropdownHTML("act-scene", sceneOpts, sceneVal);
    teamSlot.innerHTML = dropdownHTML("act-team", teamOpts, teamVal);
    bindDropdown(sceneSlot.querySelector(".dropdown"), (v) => setActCfg({ sceneId: v }));
    bindDropdown(teamSlot.querySelector(".dropdown"), (v) => setActCfg({ teamId: v }));

    renderSupportPicker(id, supports || [], favorites || []);
    root.dataset.loaded = "1";
    return true;
  } catch (e) {
    if (activeAccount !== email || !root.isConnected) return false;
    delete root.dataset.loaded;
    sceneSlot.innerHTML = `<div class="act-load-error"><span>加载失败</span><button type="button" class="act-options-retry">重新加载</button></div>`;
    teamSlot.innerHTML = `<div class="dd-loading err">等待重新加载</div>`;
    supSlot.innerHTML = `<div class="dd-loading err">等待重新加载</div>`;
    const familySlot = root.querySelector(".act-family-slot");
    familySlot.innerHTML = `<div class="act-load-error"><span>加载失败</span><button type="button" class="act-options-retry">重新加载</button></div>`;
    familySlot.querySelector(".act-options-retry").addEventListener("click", () => loadActivityOptions(id, true));
    sceneSlot.querySelector(".act-options-retry")?.addEventListener("click", () => loadActivityOptions(id, true));
    log(`加载活动关卡选项失败：${e.message}`, "err");
    return false;
  } finally {
    delete root.dataset.loading;
    refreshButton.disabled = false;
    refreshButton.classList.remove("loading");
  }
}

// 助战选择区：收藏夹 + 助战列表两组 + "不使用助战"。点选即存 supportCuid。
// 收藏夹按账号存（后端 settings.json 的 accounts[email].support_favs），收藏过的人
// 只出现在收藏组、不在下面重复。最近一次拉到的选项缓存下来，星标增删后可就地重渲染
// 而不用重发请求；缓存按账号分键，换号不会串用上一个号的收藏夹。
const ACT_SUPPORTS = {};   // `${taskId}::${email}` -> {supports, favorites}
const actSupKey = (id) => `${id}::${activeAccount || "_"}`;

function renderSupportPicker(id, supports, favorites) {
  const root = $(`act-${id}`);
  if (!root) return;
  const supSlot = root.querySelector(".act-support-slot");
  ACT_SUPPORTS[actSupKey(id)] = { supports, favorites };
  const favs = favorites || [];
  const favIds = new Set(favs.map((f) => String(f.cuid)));
  const rest = supports.filter((f) => !favIds.has(String(f.cuid)));
  const cfg = getActCfg();
  // 存的 cuid 既不在收藏夹也不在助战列表则清空（好友变动/取消收藏）
  const pickable = [...favs, ...rest].map((f) => String(f.cuid));
  const cur = cfg.supportCuid && pickable.includes(String(cfg.supportCuid))
    ? String(cfg.supportCuid) : "";
  setActCfg({ supportCuid: cur });

  const none = `<div class="sup-card sup-none${cur ? "" : " sel"}" data-cuid=""><div class="sup-none-txt">不使用助战</div></div>`;
  let html = "";
  if (favs.length) {
    html += `<div class="sup-group">★ 收藏夹</div><div class="sup-grid">${
      favs.map((f) => supportCardHTML(f, String(f.cuid) === cur, true, !f.online)).join("")}</div>`;
  }
  html += `<div class="sup-group">助战列表${favs.length ? "" : "（点 ☆ 加入收藏夹）"}</div>`;
  html += rest.length || !favs.length
    ? `<div class="sup-grid">${none}${rest.map((f) => supportCardHTML(f, String(f.cuid) === cur)).join("")}</div>`
    : `<div class="sup-grid">${none}</div><div class="sup-empty">助战列表里的好友都已收藏</div>`;
  if (!favs.length && !rest.length) {
    html = `<div class="sup-empty">暂无可选助战好友（需先在游戏内加好友）</div>`;
  }
  supSlot.innerHTML = html;

  supSlot.querySelectorAll(".sup-card").forEach((card) =>
    card.addEventListener("click", () => {
      supSlot.querySelectorAll(".sup-card").forEach((c) => c.classList.toggle("sel", c === card));
      setActCfg({ supportCuid: card.dataset.cuid || "" });
    })
  );
  // 星标：阻止冒泡（否则会连带选中该卡）
  supSlot.querySelectorAll(".sup-star").forEach((btn) =>
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      toggleSupportFav(id, btn.dataset.cuid, btn.classList.contains("on"), btn);
    })
  );
}

// 星标增删。成功后就地更新缓存并重渲染（不重发 options 请求）。
async function toggleSupportFav(id, cuid, isFav, btn) {
  const email = activeAccount;
  const cache = ACT_SUPPORTS[actSupKey(id)] || { supports: [], favorites: [] };
  btn.disabled = true;
  try {
    if (isFav) {
      await api("/api/support/unfavorite", { account: email, cuid });
      cache.favorites = (cache.favorites || []).filter((f) => String(f.cuid) !== String(cuid));
      logTo(email, `已从收藏夹移除助战 UID ${cuid}`);
    } else {
      const r = await api("/api/support/favorite", { account: email, cuid });
      cache.favorites = [...(cache.favorites || []), r.favorite]
        .sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")));
      logTo(email, `已收藏助战 ${r.favorite.name || cuid}（${r.favorite.hero?.name || ""}）`);
    }
    renderSupportPicker(id, cache.supports, cache.favorites);
  } catch (e) {
    btn.disabled = false;
    logTo(email, `收藏助战失败：${e.message}`, "err");
  }
}

// 运行编排：同扫荡（锁定发起账号、显式带 email、无掉落即停）。
const actRunning = {};  // email -> bool
const actStopRequested = {};
function logActivityUrgentResults(cleanup, prefix, email) {
  (cleanup?.results || []).forEach((result) => {
    const reason = result.reason ? `·${result.reason}` : "";
    logResult({ ...result, name: `${result.urgentName || "紧急任务"}${reason}` }, prefix, email);
  });
}

async function runActivity(id) {
  const email = activeAccount;
  if (actRunning[email]) return;
  const cfg = getActCfg();
  if (!cfg.sceneId || !cfg.teamId) return log("请先选择关卡和队伍", "err");
  const root = $(`act-${id}`);
  const taskName = (TASKS.find((x) => x.id === id) || {}).name || "活动关卡";
  const startBtn = root.querySelector(".act-start");
  const summary = $(`actSummary-${id}`);
  actRunning[email] = true;
  updateActivityMode(id);
  actStopRequested[email] = false;
  startBtn.disabled = false;
  startBtn.classList.add("task-stop-button");
  startBtn.textContent = "停止";
  const sceneName = (root.querySelector(".act-scene .dd-cur")?.textContent || cfg.sceneId).trim();
  const supName = cfg.supportCuid
    ? (root.querySelector(".sup-card.sel .sup-name")?.textContent || "").trim() : "";
  const urgentModeNames = cfg.urgentSpecializations.map((mode) => mode === "catalyst" ? "催化剂特化" : "金币特化");
  const urgentModeText = urgentModeNames.length ? urgentModeNames.join("、") : "仅保留两个非目标任务";
  logTo(email, `开始${taskName}：${sceneName} × ${cfg.count} 次${supName ? `（助战 ${supName}）` : "（无助战）"}${cfg.urgentEnabled ? `，自动清理紧急任务（${urgentModeText}）` : ""}`);
  let done = 0;
  let stopped = false;
  let urgentSpecializations = cfg.urgentEnabled ? [...cfg.urgentSpecializations] : null;
  try {
    for (let i = 1; i <= cfg.count; i++) {
      await waitRequestDelayBetweenIterations(i, () => actStopRequested[email]);
      if (actStopRequested[email]) {
        stopped = true;
        break;
      }
      const r = await api("/api/activity/run", {
        account: email, sceneId: cfg.sceneId, teamId: cfg.teamId,
        supportCuid: cfg.supportCuid || null, quick: cfg.quick,
        urgentSpecializations,
      });
      if (r.status) applyStatus(email, r.status, r.nextClaims);
      logResult({ ...r, name: taskName }, `第${i}次·`, email);
      if (actStopRequested[email]) {
        stopped = true;
        break;
      }
      if (r.urgentCleanup) {
        logActivityUrgentResults(r.urgentCleanup, `第${i}次·紧急任务·`, email);
        if (!r.urgentCleanup.ok) {
          urgentSpecializations = null;
          logTo(email, "本轮紧急任务自动清理失败，后续活动次数不再重复尝试", "warn");
        }
      }
      if (!r.ok) { logTo(email, `第 ${i} 次未获得掉落，停止扫荡`, "err"); break; }
      done++;
    }
    if (stopped) logTo(email, `[${taskName}] 已停止，未继续后续请求`, "warn");
    if (summary && email === activeAccount) summary.textContent = `完成 ${done}/${cfg.count} 次`;
  } catch (e) {
    logTo(email, `活动关卡出错：${e.message}`, "err");
  } finally {
    if (cfg.urgentEnabled && !stopped) {
      try {
        const cleanup = await api("/api/activity/urgent/clear", {
          account: email, teamId: cfg.teamId, supportCuid: cfg.supportCuid || null,
        });
        if (cleanup.status) applyStatus(email, cleanup.status, cleanup.nextClaims);
        logActivityUrgentResults(cleanup, "紧急任务·", email);
        if (cleanup.ok && !(cleanup.results || []).length) {
          logTo(email, "[紧急任务] 最终清理完成，当前没有待处理任务", "ok");
        }
      } catch (error) {
        logTo(email, `紧急任务最终清理失败：${error.message}`, "err");
      }
    }
    actRunning[email] = false;
    delete actStopRequested[email];
    if (email === activeAccount && startBtn.isConnected) {
      startBtn.disabled = false;
      startBtn.classList.remove("task-stop-button");
      startBtn.textContent = "▶ 开始扫荡";
      updateActivityMode(id);
    }
    refreshAccountStatus(email);
  }
}

function requestActivityStop(id, email) {
  if (!email || !actRunning[email]) return;
  actStopRequested[email] = true;
  const root = $(`act-${id}`);
  const button = root?.querySelector(".act-start");
  if (button) {
    button.disabled = true;
    button.classList.add("task-stop-button");
    button.textContent = "停止中…";
  }
}

// ---------- 商店购买自定义控件（静态货架） ----------
// 选中态按账号命名空间（各账号想买什么互不相干）：{tab, picks:{档位id: 份数}}
const STORE_LS = "ark_store_buy_cfg";
const storeLsKey = () => `${STORE_LS}::${activeAccount || "_"}`;
function getStoreCfg() {
  try {
    const v = JSON.parse(frontendStorageGet(storeLsKey())) || {};
    return { tab: v.tab || "", picks: (v.picks && typeof v.picks === "object") ? v.picks : {} };
  } catch {
    return { tab: "", picks: {} };
  }
}
function setStoreCfg(patch) {
  frontendStorageSet(storeLsKey(), JSON.stringify({ ...getStoreCfg(), ...patch }));
}
// 货架数据按账号缓存（展开时拉一次，切 tab 不重复请求）
const STORE_DATA = {};   // email -> {stores, wallet}
const itemIcon = (sid) => `/assets/items/${sid}.png`;
const fmtNum2 = (n) => (n == null ? "—" : Number(n).toLocaleString("en-US"));
const STORE_RESET_LABEL = { Day: "每日", Week: "每周", Month: "每月", Hour: "每小时" };

function applyStateSyncMeta(email, data) {
  const a = ACCOUNTS[email];
  if (!a || !data) return false;
  const oldRevision = a.stateRevision;
  const oldError = a.stateSyncError;
  if (data.stateRevision != null) a.stateRevision = data.stateRevision;
  if (data.stateSyncedAt != null) a.stateSyncedAt = data.stateSyncedAt;
  a.stateSyncError = data.stateSyncError || null;
  return (data.stateRevision != null && data.stateRevision !== oldRevision)
    || a.stateSyncError !== oldError;
}

async function reloadStoreAfterStateSync(email) {
  delete STORE_DATA[email];
  if (email !== activeAccount) return;
  const root = $("store-store_buy");
  if (!root || !root.dataset.loaded) return;
  const body = root.querySelector(".shop-body");
  if (!body || body.classList.contains("hidden")) {
    delete root.dataset.loaded;
    return;
  }
  await loadStoreOptions("store_buy");
}

function renderStoreWidget(t) {
  return `<div class="shop-widget store-host" id="store-${t.id}">
    <div class="shop-head">
      <div class="ticon">${taskIconHTML(t.id, "🛍️")}</div>
      <div class="tinfo"><div class="tname">${t.name}</div><div class="tdesc">${t.desc}</div></div>
      <button type="button" class="mini store-toggle">选项 ▸</button>
    </div>
    <div class="shop-body hidden">
      <div class="store-panes"><div class="dd-loading">加载中…</div></div>
    </div>
  </div>`;
}

async function bindStoreWidget(id) {
  const root = $(`store-${id}`);
  if (!root) return;
  const body = root.querySelector(".shop-body");
  const toggle = root.querySelector(".store-toggle");
  toggle.addEventListener("click", async () => {
    body.classList.toggle("hidden");
    toggle.textContent = body.classList.contains("hidden") ? "选项 ▸" : "选项 ▾";
    if (!body.classList.contains("hidden") && !root.dataset.loaded) {
      await loadStoreOptions(id);
      root.dataset.loaded = "1";
    }
  });
}

async function loadStoreOptions(id) {
  const root = $(`store-${id}`);
  const pane = root.querySelector(".store-panes");
  try {
    const data = await api("/api/store/options");
    STORE_DATA[activeAccount] = data;
    applyStateSyncMeta(activeAccount, data);
    renderStorePanes(id);
  } catch (e) {
    pane.innerHTML = `<div class="dd-loading err">加载失败</div>`;
    log(`加载商店货架失败：${e.message}`, "err");
  }
}

// 勾选一档时的默认份数：能买几次就买几次（剩余额度），限购未知时按 1
const storeDefaultQty = (g) => (g.remain != null ? Math.max(1, g.remain) : 1);

// 已选商品的花费合计 {货币id: 总额}
function storeTotals() {
  const data = STORE_DATA[activeAccount];
  const { picks } = getStoreCfg();
  const sum = {};
  let n = 0;
  (data ? data.stores : []).forEach((s) =>
    s.goods.forEach((g) => {
      const q = picks[g.id] || 0;
      if (!q) return;
      n += q;
      sum[g.cost.item] = (sum[g.cost.item] || 0) + g.cost.count * q;
    })
  );
  return { sum, n };
}

function renderStorePanes(id) {
  const root = $(`store-${id}`);
  const pane = root.querySelector(".store-panes");
  const data = STORE_DATA[activeAccount];
  if (!data) { pane.innerHTML = `<div class="dd-loading">加载中…</div>`; return; }

  const cfg = getStoreCfg();
  const cur = data.stores.find((s) => s.id === cfg.tab) || data.stores[0];
  const picks = cfg.picks;

  const tabs = data.stores.map((s) => {
    const c = s.goods.filter((g) => picks[g.id]).length;
    return `<button type="button" class="b-tab ${s.id === cur.id ? "active" : ""}" data-tab="${s.id}">
      <img src="${itemIcon(s.currency)}" alt="" loading="lazy"><span>${s.name}</span>
      ${c ? `<span class="n">${c}</span>` : ""}</button>`;
  }).join("");

  // 同一 tab 内是否有需要"内容行 / 步进器"的卡片。有才占位，保证同排卡片里
  // 价格药丸与状态行齐平；整 tab 都不需要时就不留空行（免得卡片凭空变高）。
  const anyGain = cur.goods.some((g) => g.gainText);
  const anyStep = cur.goods.some((g) => g.limit == null || g.limit > 1);
  const cards = cur.goods.map((g) => {
    const art = `<img class="b-art" src="${itemIcon(g.icon)}" alt="" loading="lazy">`;
    const cost = `<span class="b-cost"><img src="${itemIcon(g.cost.item)}" alt="">${fmtNum2(g.cost.count)}</span>`;
    // 购买状态 = 游戏内的"剩余可购次数/上限"（"购买 1/1"=还能买 1 次，"购买 0/1"=已售罄）。
    // 售罄不置灰：照发请求，服务器拒绝时把它的原话写进日志（用户定）。
    const cycle = STORE_RESET_LABEL[g.resetType] || "";
    const state = g.limit != null ? `${cycle ? `${cycle}限购 · ` : "限购 · "}剩余 ${g.remain}/${g.limit}`
      : (g.bought ? `已购 ${g.bought}` : "");
    // 可多次购买的档给次数步进器，默认买满剩余额度；限购 1 次的不给（没得选）
    const q = picks[g.id];
    const multi = g.limit == null || g.limit > 1;
    const stepper = multi ? `<div class="b-step" data-gid="${g.id}">
        <button type="button" data-d="-1">−</button>
        <span class="v">${q || storeDefaultQty(g)}</span>
        <button type="button" data-d="1">+</button>
      </div>` : (anyStep ? `<div class="b-step-gap"></div>` : "");
    return `<div class="b-card ${q ? "sel" : ""}" data-gid="${g.id}" title="${g.name}">
      <span class="b-pick">✓</span>${art}
      <div class="b-name">${g.name}</div>
      ${anyGain ? `<div class="b-gain">${g.gainText}</div>` : ""}
      ${cost}${stepper}
      <div class="b-lim">${state}</div>
    </div>`;
  }).join("");

  const { sum, n } = storeTotals();
  const spend = Object.keys(sum).map((sid) => {
    const short = (data.wallet[sid] || 0) < sum[sid];
    return `<span class="w-item ${short ? "short" : ""}">
      <img src="${itemIcon(sid)}" alt=""><b>${fmtNum2(sum[sid])}</b>
      <span class="w-have">/ ${fmtNum2(data.wallet[sid] || 0)}</span></span>`;
  }).join("");
  pane.innerHTML = `<div class="b-tabs">${tabs}</div>
    <div class="b-grid">${cards || '<div class="dd-loading">该商店暂无可显示的货架</div>'}</div>
    <div class="b-cart">
      <span class="cnt">已选 ${n} 件</span>
      <div class="wallet">${spend || '<span class="a-sum">未选择商品</span>'}</div>
      <button type="button" class="btn primary store-start" ${n ? "" : "disabled"}>▶ 开始购买</button>
    </div>
    <div class="sweep-summary" id="storeSummary-${id}"></div>`;

  if (cur.id !== cfg.tab) setStoreCfg({ tab: cur.id });
  pane.querySelectorAll(".b-tab").forEach((b) =>
    b.addEventListener("click", () => { setStoreCfg({ tab: b.dataset.tab }); renderStorePanes(id); })
  );
  const goodsById = {};
  data.stores.forEach((s) => s.goods.forEach((g) => { goodsById[g.id] = g; }));
  // 点卡片：勾选/取消。勾选时份数默认买满剩余额度
  pane.querySelectorAll(".b-card").forEach((c) =>
    c.addEventListener("click", () => {
      const gid = c.dataset.gid;
      const p = { ...getStoreCfg().picks };
      if (p[gid]) delete p[gid]; else p[gid] = storeDefaultQty(goodsById[gid]);
      setStoreCfg({ picks: p });
      renderStorePanes(id);
    })
  );
  // 步进器：改份数（同时把该档勾上）；stopPropagation 免得冒泡成"点卡片=取消勾选"
  pane.querySelectorAll(".b-step button").forEach((b) =>
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      const gid = b.closest(".b-step").dataset.gid;
      const p = { ...getStoreCfg().picks };
      const cur = p[gid] || storeDefaultQty(goodsById[gid]);
      p[gid] = Math.min(99, Math.max(1, cur + parseInt(b.dataset.d)));
      setStoreCfg({ picks: p });
      renderStorePanes(id);
    })
  );
  const start = pane.querySelector(".store-start");
  if (start) start.addEventListener("click", () => runStoreBuy(id));
}

// 购买：一次请求把所选各档交给后端顺序买（后端逐档独立处理，单档失败不影响其余）
const storeRunning = {};  // email -> bool
async function runStoreBuy(id) {
  const email = activeAccount;
  if (storeRunning[email]) return;
  const { picks } = getStoreCfg();
  const list = Object.keys(picks).map((gid) => ({ id: gid, count: picks[gid] }));
  if (!list.length) return log("请先选择要购买的商品", "err");
  const root = $(`store-${id}`);
  const taskName = (TASKS.find((x) => x.id === id) || {}).name || "商店购买";
  const btn = root.querySelector(".store-start");
  storeRunning[email] = true;
  if (btn) { btn.disabled = true; btn.textContent = "购买中…"; }
  logTo(email, `开始${taskName}：${list.length} 档`);
  try {
    const r = await api("/api/store/buy", { account: email, picks: list });
    if (r.status) applyStatus(email, r.status, r.nextClaims);
    logResult({ ...r, name: taskName }, "", email);
    // 买完货架的已购次数变了，重新拉一次让卡片状态跟服务器一致
    if (email === activeAccount) {
      await loadStoreOptions(id);
      const sum = $(`storeSummary-${id}`);
      if (sum) sum.textContent = r.detail || "";
    }
  } catch (e) {
    logTo(email, `商店购买出错：${e.message}`, "err");
  } finally {
    storeRunning[email] = false;
    if (email === activeAccount && btn && btn.isConnected) {
      btn.disabled = false; btn.textContent = "▶ 开始购买";
    }
    refreshAccountStatus(email);
  }
}

// 读缓存状态（不重登）：工具自己的领取/购买已由后端 NowItem 增量写回，读缓存即最新。
// 巡检、任务执行后都走这个，零重登。
// 把 status/nextClaims 写回当前账号缓存并渲染（供刷新/巡检后调用）
function applyStatus(email, status, nextClaims, arena, connection, redeemCodes) {
  const a = ACCOUNTS[email]; if (!a) return;
  if (connection) applyConnectionState(email, connection);
  if (status) { a.status = status; a.name = status.name; a.avatar = status.avatar; }
  if (nextClaims) a.nextClaims = nextClaims;
  // 竞技场档期（/api/status 带回）。直接采用后端值：勾选存在后端 params 里、
  // 由 /api/arena/config 单独写入，且 /api/toggles 已改成按 key 合并不会抹掉它，
  // 所以后端就是唯一真源，不需要再在前端做"保留本地勾选"的兜底。
  if (arena) a.arena = arena;
  if (redeemCodes) {
    a.redeemCodes = redeemCodes;
    if (email === activeAccount) {
      const root = $("auto-redeem_code");
      if (root) { const body = root.querySelector(".auto-body"); if (body) body.innerHTML = renderRedeemCodeOptions(); }
    }
  }
  if (email === activeAccount) {
    if (status) renderStatus(status);
    if (nextClaims) { NEXT_CLAIMS = nextClaims; renderNextClaims(); }
    if (arena) renderArenaList("arena_npc");
  }
  updateAccountSwitcher(email);
}

async function refreshStatus(quiet = false) {
  const email = activeAccount;
  try {
    const data = await api("/api/status");
    const { status, nextClaims, arena, connection, redeemCodes } = data;
    applyStateSyncMeta(email, data);
    applyStatus(email, status, nextClaims, arena, connection, redeemCodes);
    if (!quiet) log("状态已刷新", "ok");
  } catch (e) { if (!quiet) log(`刷新失败：${e.message}`, "err"); }
}

// 手动刷新按钮：重新登录拉全量（唯一能拿到游戏客户端内变化 + 体力恢复的权威数据）。
// 只在用户主动点按钮时才重登，不做自动轮询。
async function manualRefresh() {
  const email = activeAccount;
  const account = ACCOUNTS[email];
  if (!account || account.refreshPending) return;
  account.refreshPending = true;
  renderConnectionState(email);
  try {
    const data = await api("/api/status/refresh", { account: email });
    if (ACCOUNTS[email] !== account) return;
    const { status, nextClaims, arena, connection, redeemCodes } = data;
    const snapshotChanged = applyStateSyncMeta(email, data);
    applyStatus(email, status, nextClaims, undefined, connection, redeemCodes);
    // 刷新会清空竞技场的失败退避（后端 refresh_status 调 reset_task_runtime），
    // 把新状态写回并重渲，让"重试倒计时"立刻变回"可挑战"。
    if (arena && ACCOUNTS[email]) {
      ACCOUNTS[email].arena = arena;
      if (email === activeAccount) renderArenaList("arena_npc");
    }
    if (snapshotChanged) await reloadStoreAfterStateSync(email);
    loadRoster(email, true);   // 重登后团员/物品也刷新
    logTo(email, "状态已刷新（重新登录）", "ok");
  } catch (e) {
    if (ACCOUNTS[email] === account) logTo(email, `重新登录失败：${e.message}`, "err");
  } finally {
    account.refreshPending = false;
    if (email === activeAccount && ACCOUNTS[email] === account) renderConnectionState(email);
  }
}

// ---------- 账号切换器 + 后台日志轮询 ----------
function updateAccountSwitcherUnread(email) {
  const account = ACCOUNTS[email];
  if (!account) return;
  const item = [...document.querySelectorAll("#accSwitcher .acc-item")]
    .find((element) => element.dataset.email === email);
  if (!item) return;

  const oldBadge = item.querySelector(".acc-unread");
  if (!account.logUnread) {
    oldBadge?.remove();
    return;
  }

  const text = account.logUnread > 99 ? "99+" : String(account.logUnread);
  if (oldBadge) {
    oldBadge.textContent = text;
    return;
  }

  const badge = document.createElement("span");
  badge.className = "acc-unread";
  badge.textContent = text;
  item.insertBefore(badge, item.querySelector(".acc-x"));
}

function updateAccountConnectionIndicator(email) {
  const account = ACCOUNTS[email];
  const box = $("accSwitcher");
  if (!account || !box) return;
  const connected = account.connection?.connected !== false;
  const label = connected ? "已连接：游戏会话正常"
    : account.connection?.reason === "duplicate_login" ? "已断开：账号在其它地方登录"
    : "已断开：游戏会话已断开";
  const item = [...box.querySelectorAll(".acc-item")]
    .find((element) => element.dataset.email === email);
  const indicators = [item?.querySelector(".acc-connection")];
  if (email === activeAccount) indicators.push($("accCurrentBtn")?.querySelector(".acc-connection"));
  indicators.forEach((indicator) => {
    if (!indicator) return;
    indicator.className = `acc-connection ${connected ? "is-connected" : "is-disconnected"}`;
    indicator.title = label;
    indicator.setAttribute("aria-label", label);
  });
}

function updateAccountSwitcher(email) {
  const account = ACCOUNTS[email];
  if (!account) return;

  const item = [...document.querySelectorAll("#accSwitcher .acc-item")]
    .find((element) => element.dataset.email === email);
  if (item) {
    const avatar = item.querySelector(".acc-ava");
    const name = item.querySelector(".acc-name");
    const sub = item.querySelector(".acc-sub");
    if (avatar) {
      avatar.innerHTML = account.avatar
        ? `<img src="${account.avatar}" alt="">`
        : `<span>${(account.name || email).slice(0, 1)}</span>`;
    }
    if (name) name.textContent = account.name || email;
    if (sub) sub.textContent = email;
  }

  if (email === activeAccount) {
    const current = $("accCurrentBtn");
    if (current) {
      const avatar = current.querySelector(".acc-ava");
      const name = current.querySelector(".acc-name");
      const sub = current.querySelector(".acc-sub");
      if (avatar) {
        avatar.innerHTML = account.avatar
          ? `<img src="${account.avatar}" alt="">`
          : `<span>${(account.name || email || "?").slice(0, 1)}</span>`;
      }
      if (name) name.textContent = account.name || email || "未登录";
      if (sub) sub.textContent = email;
    }
  }

  updateAccountSwitcherUnread(email);
  updateAccountConnectionIndicator(email);
}

// 渲染左下角账号切换器（在线账号列表 + 添加账号）。
function renderAccountSwitcher() {
  const box = $("accSwitcher");
  if (!box) return;
  const emails = Object.keys(ACCOUNTS);
  const cur = ACCOUNTS[activeAccount] || {};
  const initial = (cur.name || activeAccount || "?").slice(0, 1);
  const avatarHtml = cur.avatar
    ? `<img src="${cur.avatar}" alt="">` : `<span>${initial}</span>`;
  // 顶部：当前账号胶囊（点击展开列表）
  let html = `<button class="acc-current" id="accCurrentBtn">
      <div class="acc-portrait"><div class="acc-ava">${avatarHtml}</div>
        <span class="acc-connection hidden" role="img"></span></div>
      <div class="acc-meta"><div class="acc-name">${cur.name || activeAccount || "未登录"}</div>
        <div class="acc-sub">${activeAccount || ""}</div></div>
      <span class="acc-caret">▴</span>
    </button>`;
  // 弹出列表
  const items = emails.map((email) => {
    const a = ACCOUNTS[email];
    const av = a.avatar ? `<img src="${a.avatar}" alt="">` : `<span>${(a.name || email).slice(0, 1)}</span>`;
    const badge = a.logUnread ? `<span class="acc-unread">${a.logUnread > 99 ? "99+" : a.logUnread}</span>` : "";
    return `<div class="acc-item ${email === activeAccount ? "on" : ""}" data-email="${email}">
        <div class="acc-portrait"><div class="acc-ava sm">${av}</div>
          <span class="acc-connection hidden" role="img"></span></div>
        <div class="acc-meta"><div class="acc-name">${a.name || email}</div><div class="acc-sub">${email}</div></div>
        ${badge}
        <button class="acc-x" data-del="${email}" title="退出并删除该账号">✕</button>
      </div>`;
  }).join("");
  html += `<div class="acc-pop hidden" id="accPop">
      ${items}
      <button class="acc-add" id="accAddBtn"><span>＋</span> 添加账号</button>
    </div>`;
  box.innerHTML = html;
  emails.forEach(updateAccountConnectionIndicator);

  $("accCurrentBtn").onclick = (e) => { e.stopPropagation(); $("accPop").classList.toggle("hidden"); };
  box.querySelectorAll(".acc-item").forEach((el) => {
    el.addEventListener("click", (e) => {
      if (e.target.closest(".acc-x")) return;
      $("accPop").classList.add("hidden");
      switchAccount(el.dataset.email);
    });
  });
  box.querySelectorAll(".acc-x").forEach((b) =>
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      const email = b.dataset.del;
      if (confirm(`确定删除账号 ${email}？将下线并清除其登录令牌与开关设置。`)) accDelete(email);
    })
  );
  $("accAddBtn").onclick = () => { $("accPop").classList.add("hidden"); openAddAccount(); };
}

// 打开"添加账号"（复用登录视图，作为叠加层；有在线账号时显示"取消"）
function openAddAccount() {
  setDrawer(false);   // 窄屏收起侧栏抽屉，否则登录页会被抽屉盖住
  $("account").value = "";
  $("password").value = "";
  $("password").placeholder = "密码";
  loginStatus("");   // 回到默认引导态
  $("loginView").classList.remove("hidden");
  const cancel = $("loginCancel");
  if (cancel) cancel.classList.toggle("hidden", Object.keys(ACCOUNTS).length === 0);
  renderSavedAccounts();
}

// 登录页"已保存账号"快捷选择：有 Token 时留空密码即可恢复登录。
let SAVED_ACCTS = [];   // 来自 /api/accounts 的存档列表
function savedAccount(account) {
  return SAVED_ACCTS.find((a) => a.account === account);
}
function hasSavedToken(account) {
  const item = savedAccount(account);
  return !!(item && item.hasToken && !item.tokenError);
}
function savedAcctItems() {
  return SAVED_ACCTS.filter((a) => !ACCOUNTS[a.account]);  // 排除已在线
}
function renderSavedAccounts() {
  const box = $("savedAccts");
  const caret = $("acctCaret");
  if (!box) return;
  const items = savedAcctItems();
  if (!items.length) {
    box.classList.add("hidden"); box.innerHTML = "";
    if (caret) caret.classList.add("hidden");
    return;
  }
  if (caret) caret.classList.remove("hidden");
  box.innerHTML = items.map((a) =>
    `<div class="sa-opt" data-email="${a.account}" data-hastoken="${a.hasToken && !a.tokenError ? 1 : 0}" data-tokenerror="${a.tokenError ? 1 : 0}">
       <span class="sa-em">${a.account}</span>
       <button type="button" class="sa-del" data-del="${a.account}" title="删除已保存的账号">✕</button>
     </div>`).join("");
  box.querySelectorAll(".sa-opt").forEach((row) =>
    row.addEventListener("click", (e) => {
      if (e.target.closest(".sa-del")) return;  // 点叉是删除，不填充
      $("account").value = row.dataset.email;
      $("password").value = "";
      if (row.dataset.hastoken === "1") {
        $("password").placeholder = "使用临时token登录";
        $("loginBtn").focus();
      } else {
        $("password").placeholder = row.dataset.tokenerror === "1" ? "令牌不可用，请输入密码" : "密码";
        $("password").focus();
      }
      $("savedAccts").classList.add("hidden");
    })
  );
  box.querySelectorAll(".sa-del").forEach((b) =>
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteSavedAccount(b.dataset.del);
    })
  );
}

// 登录页主动删除一个已保存账号（离线存档）：清后端存档 + 本地列表，重渲下拉。
async function deleteSavedAccount(email) {
  if (!confirm(`删除已保存的账号 ${email}？将清除其登录令牌与开关设置。`)) return;
  try { await api("/api/accounts/delete", { account: email }); } catch (e) {}
  SAVED_ACCTS = SAVED_ACCTS.filter((a) => a.account !== email);
  renderSavedAccounts();
}
function toggleSavedMenu() {
  const box = $("savedAccts");
  if (!box || box.innerHTML.trim() === "") return;
  box.classList.toggle("hidden");
}

// 后台日志轮询：每 1s 拉所有在线账号自后端新产生的巡检日志，写进各自缓冲。
function ensureAutoLogPoll() {
  if (autoLogTimer) return;
  autoLogTimer = setInterval(pollAutoLogs, AUTO_LOG_POLL_MS);
  if (!countdownTimer) countdownTimer = setInterval(renderNextClaims, 30 * 1000);
}
async function pollAutoLogs() {
  if (autoLogPollPending) return;
  autoLogPollPending = true;
  const emails = Object.keys(ACCOUNTS);
  try {
    for (const email of emails) {
      const a = ACCOUNTS[email];
      if (!a) continue;
      ensureAccountStarted(email);
      const abyssRequest = abyssRequests[email];
      const activityRequest = actProgressRequests[email];
      try {
        const data = await api(`/api/logs/auto?account=${encodeURIComponent(email)}&since=${a.logSeq || 0}`);
        if (ACCOUNTS[email] !== a) continue;
        if (abyssRequest === abyssRequests[email]) applyAbyssBattle(email, data.abyssBattle);
        if (activityRequest === actProgressRequests[email]) applyActivityProgress(email, data.activityProgress);
        const { entries, logSeq } = data;
        const snapshotChanged = applyStateSyncMeta(email, data);
        if (entries && entries.length) {
          // 用后端给的产生时刻，而不是此刻——窗口冻结期间积压的记录到这里才被拉回
          entries.forEach((e) => handleAutoTaskResult(e.result, e.prefix, email, e.ts));
          a.logSeq = logSeq;
          // 有新巡检日志 → 该账号状态多半变了，刷新其状态缓存
          refreshAccountStatus(email);
        }
        if (snapshotChanged) {
          refreshAccountStatus(email);
          await reloadStoreAfterStateSync(email);
        }
      } catch (e) { /* 账号可能已下线，忽略 */ }
    }
  } finally {
    autoLogPollPending = false;
  }
}
// 拉某账号最新状态写回缓存（不重登，读后端缓存）
async function refreshAccountStatus(email) {
  const a = ACCOUNTS[email];
  try {
    const data = await api(`/api/status?account=${encodeURIComponent(email)}`);
    if (ACCOUNTS[email] !== a) return;
    const { status, nextClaims, arena, connection, redeemCodes } = data;
    applyStateSyncMeta(email, data);
    applyStatus(email, status, nextClaims, arena, connection, redeemCodes);
  } catch (e) {}
}

// ---------- 装备掉落详情弹窗 ----------
function ensureEquipPopup() {
  let pop = $("equipPopup");
  if (!pop) {
    pop = document.createElement("div");
    pop.id = "equipPopup";
    pop.className = "equip-popup hidden";
    document.body.appendChild(pop);
  }
  return pop;
}

function showEquipPopup(key, anchor) {
  const card = EQUIP_DROPS[key];
  if (!card) return;
  const pop = ensureEquipPopup();
  pop.innerHTML = `<button type="button" class="ep-close">✕</button>${equipCardHTML(card)}`;
  pop.classList.remove("hidden");
  pop.querySelector(".ep-close").addEventListener("click", hideEquipPopup);

  // 定位在链接附近，超出视口则回退
  const r = anchor.getBoundingClientRect();
  const pw = pop.offsetWidth, ph = pop.offsetHeight;
  let left = r.left + window.scrollX;
  let top = r.bottom + window.scrollY + 8;
  const vw = window.innerWidth, vh = window.innerHeight;
  if (left + pw > vw + window.scrollX - 12) left = vw + window.scrollX - pw - 12;
  if (left < window.scrollX + 12) left = window.scrollX + 12;
  // 下方放不下就翻到链接上方
  if (r.bottom + ph + 12 > vh) top = r.top + window.scrollY - ph - 8;
  pop.style.left = `${Math.max(window.scrollX + 8, left)}px`;
  pop.style.top = `${Math.max(window.scrollY + 8, top)}px`;
}

function hideEquipPopup() {
  const pop = $("equipPopup");
  if (pop) pop.classList.add("hidden");
}

// 全局点击：装备链接 → 弹窗；点其它地方 → 关闭弹窗 + 收起所有下拉
document.addEventListener("click", (e) => {
  const link = e.target.closest(".equip-link");
  if (link) {
    e.preventDefault();
    showEquipPopup(link.dataset.eqd, link);
    return;
  }
  if (!e.target.closest("#equipPopup")) hideEquipPopup();
  // 点在下拉之外就收起（下拉内部的点击已 stopPropagation / 自行处理）
  if (!e.target.closest(".dropdown")) {
    document.querySelectorAll(".dd-menu").forEach((m) => m.classList.add("hidden"));
    document.querySelectorAll(".dropdown.open").forEach((d) => {
      d.classList.remove("open");
      d.querySelector(".dd-btn")?.setAttribute("aria-expanded", "false");
    });
  }
  // 登录页账号下拉：点在 combo 之外收起
  if (!e.target.closest("#acctCombo")) {
    const sa = $("savedAccts"); if (sa) sa.classList.add("hidden");
  }
  // 左下角账号切换器：点在其之外收起展开框
  if (!e.target.closest("#accSwitcher")) {
    const pop = $("accPop"); if (pop) pop.classList.add("hidden");
  }
  if (!e.target.closest("[data-urgent-help], [data-urgent-help-text]")) {
    closeActivityUrgentHelp();
  }
});

if ($("acctCaret")) $("acctCaret").onclick = (e) => { e.stopPropagation(); toggleSavedMenu(); };

// 退出登录 = 下线当前账号（保留存档，下次可再连接）
function logout() {
  if (activeAccount) accLogout(activeAccount);
}

// ---------- 启动：只拉账号列表，登录由用户在登录页主动触发 ----------
async function initAccounts() {
  let accountPayload = {};
  try { accountPayload = await api("/api/accounts"); } catch (e) {}
  const list = accountPayload.accounts || [];
  // start.bat test mode keeps the Python account registry alive across a page
  // refresh. Restore every online account without a new login/bootstrap, then
  // return to the account this tab was showing before the refresh.
  if (accountPayload.autoRestore) {
    const online = list.filter((item) => item.online);
    if (online.length) {
      const restored = await Promise.allSettled(
        online.map((item) => api("/api/accounts/restore", { account: item.account })),
      );
      restored.forEach((result) => {
        if (result.status === "fulfilled") rememberAccount(result.value);
      });
      const restoredEmails = Object.keys(ACCOUNTS);
      if (restoredEmails.length) {
        SAVED_ACCTS = list;
        enterApp();
        let preferred = "";
        try { preferred = sessionStorage.getItem(TEST_ACTIVE_ACCOUNT_SS) || ""; } catch { /* optional */ }
        switchAccount(ACCOUNTS[preferred] ? preferred : restoredEmails[0]);
        ensureAutoLogPoll();
        restoredEmails.forEach((email) => ensureAccountStarted(email));
        return;
      }
    }
  }
  // 启动不恢复任何会话：即使后端已有在线账号，用户也要先在下拉框选择账号，
  // 再主动点击“登录”触发 /api/login/saved。这样多账号场景不会误连或立即跑任务。
  SAVED_ACCTS = list;
  ACCOUNTS = {};
  activeAccount = null;
  $("appView").classList.add("hidden");
  $("loginView").classList.remove("hidden");
  $("loginCancel") && $("loginCancel").classList.add("hidden");
  if (updateStartupReady) loginStatus("");
  else loginStatus("正在检查最新版本，请稍候…", "pending");
  renderSavedAccounts();
}

// ---------- 事件绑定 ----------
$("loginBtn").onclick = doLogin;
$("refreshBtn").onclick = manualRefresh;
$("reconnectBtn").onclick = manualRefresh;
$("logoutBtn").onclick = logout;
$("password").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
$("account").addEventListener("input", () => {
  const item = savedAccount($("account").value.trim());
  $("password").placeholder = item?.tokenError
    ? "令牌不可用，请输入密码"
    : (item?.hasToken ? "使用临时token登录" : "密码");
});
if ($("loginCancel")) $("loginCancel").onclick = () => {
  $("loginView").classList.add("hidden");
  if (!activeAccount && Object.keys(ACCOUNTS).length) switchAccount(Object.keys(ACCOUNTS)[0]);
};

// 板块导航
document.querySelectorAll("#sideNav .nav-item").forEach((n) =>
  n.addEventListener("click", () => showSection(n.dataset.section))
);
// 团员/物品 tab 切换
document.querySelectorAll(".roster-tabs .rtab").forEach((b) =>
  b.addEventListener("click", () => { rosterTab = b.dataset.rtab; applyRosterTab(); })
);
// 明暗切换
$("themeToggle").onclick = toggleMode;
$("loginThemeBtn").onclick = toggleMode;
// 移动端侧栏
// 抽屉展开/收起：同步遮罩显隐（窄屏用遮罩点击收起，比冒泡判断可靠）
function setDrawer(open) {
  $("sidebar").classList.toggle("collapsed", !open);
  $("sidebarBackdrop").classList.toggle("show", open);
}
$("menuBtn").onclick = () => setDrawer($("sidebar").classList.contains("collapsed"));
$("sidebarBackdrop").onclick = () => setDrawer(false);
// 清空当前账号日志，同时更新完整日志页和底部日志抽屉。
$("clearLogBtn").onclick = () => {
  const a = activeAcc();
  if (a) a.logHtml = "";
  renderLogViews("");
  logUnread = 0;
  updateLogBadge();
};
// 设置：自定义颜色 + 明暗分段
$("accentPick").addEventListener("input", (e) => setTheme({ accent: e.target.value }));
$("accent2Pick").addEventListener("input", (e) => setTheme({ accent2: e.target.value }));
document.querySelectorAll("#modeSeg button").forEach((b) =>
  b.addEventListener("click", () => setTheme({ mode: b.dataset.mode }))
);

// ---------- 系统设置 ----------
let runtimePlatform = "windows";

function requestDelayNotice(message, cls = "ok") {
  const el = $("requestDelayNotice");
  if (!el) return;
  el.textContent = message || "";
  el.className = `request-delay-notice${message ? ` ${cls}` : ""}`;
}

let autoReloginSaving = false;
let autoReloginConfig = { enabled: false, delaySeconds: 300 };

function autoReloginNotice(message, cls = "ok") {
  const el = $("autoReloginNotice");
  if (!el) return;
  el.textContent = message || "";
  el.className = `request-delay-notice${message ? ` ${cls}` : ""}`;
}

function syncAutoReloginUI(value = {}) {
  const enabledInput = $("autoReloginEnabled");
  const delayInput = $("autoReloginDelay");
  if (!enabledInput || !delayInput) return;
  const enabled = value.enabled === true;
  const minutes = Math.max(1, Math.min(1440, Math.round(Number(value.delaySeconds ?? 300) / 60) || 5));
  enabledInput.checked = enabled;
  enabledInput.disabled = autoReloginSaving;
  delayInput.value = String(minutes);
  delayInput.disabled = !enabled || autoReloginSaving;
  $("autoReloginFields")?.classList.toggle("request-delay-row-disabled", !enabled);
}

async function saveAutoRelogin() {
  const enabledInput = $("autoReloginEnabled");
  const delayInput = $("autoReloginDelay");
  if (!enabledInput || !delayInput || autoReloginSaving) return;
  const minutes = Math.max(1, Math.min(1440, Math.round(Number(delayInput.value) || 5)));
  const enabled = enabledInput.checked;
  delayInput.value = String(minutes);
  autoReloginSaving = true;
  enabledInput.disabled = true;
  delayInput.disabled = true;
  try {
    const cfg = await api("/api/config", {
      autoReloginEnabled: enabled,
      autoReloginDelaySeconds: minutes * 60,
    });
    autoReloginConfig = cfg.autoRelogin;
    autoReloginNotice("");
  } catch (error) {
    autoReloginNotice(`保存失败：${error.message}`, "err");
  } finally {
    autoReloginSaving = false;
    syncAutoReloginUI(autoReloginConfig);
  }
}

$("autoReloginEnabled")?.addEventListener("change", () => {
  syncAutoReloginUI({
    enabled: $("autoReloginEnabled").checked,
    delaySeconds: Number($("autoReloginDelay").value || 5) * 60,
  });
  saveAutoRelogin();
});
$("autoReloginDelay")?.addEventListener("change", saveAutoRelogin);

function syncRequestDelayUI(value = {}) {
  const enabledInput = $("requestDelayEnabled");
  const msInput = $("requestDelayMs");
  const jitterInput = $("requestDelayJitter");
  const enabled = value.enabled !== false;
  const ms = Math.max(0, Math.min(60000, Number(value.ms ?? REQUEST_DELAY_DEFAULTS.ms) || 0));
  const jitter = Math.max(0, Math.min(100, Number(value.jitter ?? 20) || 0));
  requestDelayRuntime = { enabled, ms, jitter };
  if (!enabledInput || !msInput || !jitterInput) return;
  enabledInput.checked = enabled;
  msInput.value = String(Math.round(ms));
  jitterInput.value = String(Math.round(jitter));
  msInput.disabled = !enabled;
  jitterInput.disabled = !enabled;
  $("requestDelayControls")?.classList.toggle("request-delay-row-disabled", !enabled);
}

async function saveRequestDelay() {
  const enabledInput = $("requestDelayEnabled");
  const msInput = $("requestDelayMs");
  const jitterInput = $("requestDelayJitter");
  if (!enabledInput || !msInput || !jitterInput) return;
  const ms = Math.max(0, Math.min(60000, Math.round(Number(msInput.value) || 0)));
  const jitter = Math.max(0, Math.min(100, Math.round(Number(jitterInput.value) || 0)));
  msInput.value = String(ms);
  jitterInput.value = String(jitter);
  try {
    const cfg = await api("/api/config", {
      requestDelayEnabled: enabledInput.checked,
      requestDelayMs: ms,
      requestDelayJitter: jitter,
    });
    syncRequestDelayUI(cfg.requestDelay);
    requestDelayNotice("");
  } catch (error) {
    requestDelayNotice(`保存失败：${error.message}`, "err");
  }
}

$("requestDelayEnabled")?.addEventListener("change", () => {
  syncRequestDelayUI({
    enabled: $("requestDelayEnabled").checked,
    ms: $("requestDelayMs").value,
    jitter: $("requestDelayJitter").value,
  });
  saveRequestDelay();
});
$("requestDelayMs")?.addEventListener("change", saveRequestDelay);
$("requestDelayJitter")?.addEventListener("change", saveRequestDelay);
// 启动时拉一次 /api/config：系统设置 + 版本号（版本唯一真源在 backend/version.py，
// 前端不写死，免得又出现"关于页 1.0.2 / APK 1.7"各说各话）。
async function loadConfig() {
  try {
    const cfg = await api("/api/config");
    runtimePlatform = cfg.platform || "windows";
    syncRequestDelayUI(cfg.requestDelay);
    autoReloginConfig = cfg.autoRelogin || autoReloginConfig;
    syncAutoReloginUI(autoReloginConfig);
    syncDownloadPathUI(cfg);
    if ($("aboutVer") && cfg.version) $("aboutVer").textContent = `v${cfg.version}`;
  } catch { /* 忽略：服务器未就绪时静默 */ }
}
loadConfig();

// ---------- 下载路径 ----------
function downloadPathNotice(message, cls = "ok") {
  const element = $("downloadPathNotice");
  if (!element) return;
  element.textContent = message || "";
  element.className = `download-path-notice${message ? ` ${cls}` : ""}`;
}

function androidDownloadLocation() {
  try {
    return window.LucimaFiles?.getDownloadLocation?.() || "";
  } catch {
    return "";
  }
}

function syncDownloadPathUI(config = {}) {
  const input = $("downloadPathInput");
  if (!input) return;
  const nativeAndroid = runtimePlatform === "android" && window.LucimaFiles;
  input.readOnly = Boolean(nativeAndroid);
  input.value = nativeAndroid
    ? (androidDownloadLocation() || config.downloadPath || "系统下载文件夹（Download）")
    : (config.downloadPath || "");
}

async function persistDownloadPath(path) {
  const normalized = String(path || "").trim();
  if (!normalized || runtimePlatform === "android") return;
  try {
    const config = await api("/api/config", { downloadPath: normalized });
    syncDownloadPathUI(config);
    downloadPathNotice("");
  } catch (error) {
    downloadPathNotice(error.message, "err");
  }
}

async function browseDownloadPath() {
  downloadPathNotice("");
  if (runtimePlatform === "android" && window.LucimaFiles?.chooseDownloadDirectory) {
    window.LucimaFiles.chooseDownloadDirectory();
    return;
  }
  if (window.pywebview?.api?.chooseDownloadDirectory) {
    try {
      const selected = await window.pywebview.api.chooseDownloadDirectory(
        $("downloadPathInput")?.value || ""
      );
      if (selected) await persistDownloadPath(selected);
    } catch (error) {
      downloadPathNotice(`无法打开文件夹选择器：${error.message || error}`, "err");
    }
    return;
  }
  try {
    const config = await api("/api/config", { resetDownloadPath: true });
    syncDownloadPathUI(config);
    downloadPathNotice("浏览器测试模式使用 Windows 默认下载路径", "ok");
  } catch (error) {
    downloadPathNotice(error.message || "无法读取 Windows 默认下载路径", "err");
  }
}

async function resetDownloadPath() {
  downloadPathNotice("");
  if (runtimePlatform === "android" && window.LucimaFiles?.resetDownloadDirectory) {
    try {
      $("downloadPathInput").value = window.LucimaFiles.resetDownloadDirectory();
    } catch (error) {
      downloadPathNotice(error.message || "恢复默认路径失败", "err");
    }
    return;
  }
  try {
    const config = await api("/api/config", { resetDownloadPath: true });
    syncDownloadPathUI(config);
  } catch (error) {
    downloadPathNotice(error.message, "err");
  }
}

window.onLucimaDownloadDirectoryChanged = (location) => {
  if ($("downloadPathInput")) $("downloadPathInput").value = location || "";
  downloadPathNotice("");
};

$("browseDownloadPathBtn")?.addEventListener("click", browseDownloadPath);
$("resetDownloadPathBtn")?.addEventListener("click", resetDownloadPath);
$("downloadPathInput")?.addEventListener("change", (event) => {
  persistDownloadPath(event.target.value);
});
$("downloadPathInput")?.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    event.target.blur();
  }
});

// ---------- 自助更新 ----------
let updateState = null;
let updateAutoShownVersion = "";
let updateStatusPending = false;
let updateProgressPollToken = 0;
let updateProgressPollTimer = null;
let updateDialogMode = "update";
let updateStartupReady = false;
let updateStartupBlocked = true;
let updateStartupCheckPending = false;
let updateStartupCheckPromise = null;
let updateLastSuccessfulCheckAt = 0;
const UPDATE_STALE_AFTER_MS = 60 * 60 * 1000;

function formatUpdateBytes(value) {
  const bytes = Math.max(0, Number(value) || 0);
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function resetUpdateProgress() {
  const progress = $("updateProgress");
  if (!progress) return;
  progress.classList.add("hidden");
  progress.classList.remove("indeterminate");
  $("updateProgressBar").style.width = "0%";
  $("updateProgressTrack").setAttribute("aria-valuenow", "0");
}

function renderUpdateProgress(progressState) {
  const progress = $("updateProgress");
  if (!progress) return;
  const phase = String(progressState?.phase || "idle");
  if (phase === "idle") {
    resetUpdateProgress();
    return;
  }
  const downloaded = Math.max(0, Number(progressState?.downloadedBytes) || 0);
  const total = Math.max(0, Number(progressState?.totalBytes) || 0);
  const percent = total ? Math.min(100, Math.max(0, Number(progressState?.percent) || 0)) : 0;
  const labels = {
    preparing: "正在准备下载",
    downloading: "正在下载更新",
    verifying: "正在校验更新",
    "applying-delta": "正在合成增量更新",
    waiting: "正在等待当前任务结束",
    "preparing-install": "正在准备安装",
    ready: "更新包已准备完成",
    restarting: "正在准备重启",
  };
  const determinate = phase === "downloading" && total > 0;
  const downloadComplete = phase !== "downloading" && total > 0 && downloaded >= total;
  progress.classList.remove("hidden");
  progress.classList.toggle("indeterminate", !determinate && !downloadComplete);
  $("updateProgressLabel").textContent = labels[phase] || "正在处理更新";
  $("updateProgressValue").textContent = determinate
    ? `${formatUpdateBytes(downloaded)} / ${formatUpdateBytes(total)}  ${percent.toFixed(1)}%`
    : (downloadComplete ? "下载完成" : "请稍候");
  $("updateProgressBar").style.width = determinate ? `${percent}%` : (downloadComplete ? "100%" : "");
  const track = $("updateProgressTrack");
  if (determinate || downloadComplete) {
    track.setAttribute("aria-valuenow", String(downloadComplete ? 100 : percent));
  } else {
    track.removeAttribute("aria-valuenow");
  }
}

function stopUpdateProgressPolling() {
  updateProgressPollToken += 1;
  clearTimeout(updateProgressPollTimer);
  updateProgressPollTimer = null;
}

function startUpdateProgressPolling() {
  stopUpdateProgressPolling();
  const token = updateProgressPollToken;
  const poll = async () => {
    try {
      const state = await api("/api/update/status");
      if (token !== updateProgressPollToken) return;
      updateState = state;
      if (state.installing || state.progress?.phase !== "idle") renderUpdateProgress(state.progress);
    } catch { /* 安装请求仍在运行时，短暂的状态轮询失败不终止更新 */ }
    if (token === updateProgressPollToken) updateProgressPollTimer = setTimeout(poll, 400);
  };
  poll();
}

function renderUpdateAvailability(state) {
  const button = $("aboutUpdateBtn");
  if (button) button.classList.toggle("hidden", !state?.available);
}

function openUpdateDialog(state = updateState) {
  if (!state?.available) return;
  updateDialogMode = "update";
  updateState = state;
  const forced = !!state.forced;
  $("updateEyebrow").textContent = forced ? "需要更新后继续使用" : "发现新版本";
  $("updateTitle").textContent = `LucimaTools ${state.latestVersion}`;
  $("updateSummary").textContent = forced
    ? `当前版本 ${state.currentVersion} 已停止支持。`
    : `当前版本 ${state.currentVersion}，更新准备就绪。`;
  const notes = Array.isArray(state.notes) ? state.notes.filter(Boolean) : [];
  const notesEl = $("updateNotes");
  notesEl.classList.toggle("empty", !notes.length);
  notesEl.innerHTML = notes.length
    ? notes.map((note) => `<li>${filterEscape(note)}</li>`).join("")
    : "此版本暂无更新说明";
  $("updateIgnoreBtn").classList.toggle("hidden", forced);
  $("updateModal").querySelector(".update-actions")?.classList.toggle("forced", forced);
  const installButton = $("updateInstallBtn");
  // A forced manifest without a package must remain blocking, but the only
  // available action is a retry instead of a permanently disabled dialog.
  const canRetryManifest = forced && (!state.artifactAvailable || !state.installSupported);
  installButton.disabled = canRetryManifest ? false : (!state.artifactAvailable || !state.installSupported || !!state.installing);
  installButton.textContent = canRetryManifest
    ? "重新检查"
    : (state.installing ? "正在准备…" : "立即更新");
  if (state.installing) renderUpdateProgress(state.progress);
  else resetUpdateProgress();
  $("updateStatus").className = "update-status";
  $("updateStatus").textContent = !state.artifactAvailable
    ? "服务器尚未提供当前平台的安装包。"
    : (!state.installSupported ? "源码调试模式不会覆盖本地工程，请在打包版本中测试安装。" : "");
  $("updateModal").classList.remove("hidden");
  document.body.classList.add("update-dialog-open");
  requestAnimationFrame(() => (forced ? installButton : $("updateIgnoreBtn"))?.focus());
}

function closeUpdateDialog() {
  if (updateState?.forced || updateStartupBlocked) return;
  $("updateModal")?.classList.add("hidden");
  document.body.classList.remove("update-dialog-open");
}

function openUpdateCheckFailureDialog(message = "无法获取最新版本，请检查网络后重试。") {
  updateDialogMode = "check-error";
  updateState = {
    ...(updateState || {}),
    available: false,
    forced: false,
    startupError: true,
    artifactAvailable: false,
    installSupported: false,
    installing: false,
    notes: [],
  };
  $("updateEyebrow").textContent = "无法继续使用";
  $("updateTitle").textContent = "无法获取最新版本";
  $("updateSummary").textContent = "请确认网络连接正常，完成版本检查后才能进入主界面。";
  const notesEl = $("updateNotes");
  notesEl.classList.add("empty");
  notesEl.textContent = "更新服务暂时不可用。";
  $("updateIgnoreBtn").classList.add("hidden");
  $("updateModal").querySelector(".update-actions")?.classList.add("forced");
  const retry = $("updateInstallBtn");
  retry.disabled = false;
  retry.textContent = "重新检查";
  resetUpdateProgress();
  $("updateStatus").className = "update-status err";
  $("updateStatus").textContent = message;
  $("updateModal").classList.remove("hidden");
  document.body.classList.add("update-dialog-open");
  requestAnimationFrame(() => retry.focus());
}

function sleepForUpdateCheck(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function fetchFreshUpdateStatus() {
  let state = await api("/api/update/check", {});
  const deadline = Date.now() + 20000;
  // request_check is intentionally non-blocking when the background checker
  // already owns the lock. Wait for that check to settle before deciding.
  while (!state?.testServer && (state?.checking || (!state?.checked && !state?.lastError))
         && Date.now() < deadline) {
    await sleepForUpdateCheck(250);
    state = await api("/api/update/status");
  }
  if (!state?.testServer && !state?.checked && !state?.lastError) {
    throw new Error("版本检查超时");
  }
  return state;
}

function setLoginUpdateGate(ready, message = "") {
  updateStartupReady = !!ready;
  const button = $("loginBtn");
  if (button) button.disabled = !ready;
  if (!ready && message) loginStatus(message, "pending");
  else if (ready) loginStatus("");
}

async function runStartupUpdateCheck() {
  if (updateStartupCheckPromise) return updateStartupCheckPromise;
  updateStartupCheckPending = true;
  setLoginUpdateGate(false, "正在检查最新版本，请稍候…");
  updateStartupCheckPromise = (async () => {
    try {
      const state = await fetchFreshUpdateStatus();
      updateState = state;
      if (state.testServer) {
        updateLastSuccessfulCheckAt = Date.now();
        updateStartupBlocked = false;
        setLoginUpdateGate(true);
        closeUpdateDialog();
        return state;
      }
      if (state.lastError) throw new Error(state.lastError);
      updateLastSuccessfulCheckAt = Number(state.lastCheckedAt) || Date.now();
      if (state.forced) {
        updateStartupBlocked = true;
        setLoginUpdateGate(false, "当前版本需要更新后才能继续使用。");
        openUpdateDialog(state);
      } else {
        updateStartupBlocked = false;
        setLoginUpdateGate(true);
        renderUpdateAvailability(state);
        if (state.available && !state.ignored) openUpdateDialog(state);
        else closeUpdateDialog();
      }
      return state;
    } catch (error) {
      updateStartupBlocked = true;
      setLoginUpdateGate(false, "无法获取最新版本，请完成检查后再登录。");
      openUpdateCheckFailureDialog(error?.message || "更新检查失败");
      return null;
    } finally {
      updateStartupCheckPending = false;
      updateStartupCheckPromise = null;
    }
  })();
  return updateStartupCheckPromise;
}

async function refreshUpdateStatus(autoShow = false, forceCheck = false) {
  if (updateStatusPending || updateStartupCheckPending) return;
  updateStatusPending = true;
  try {
    const state = forceCheck ? await fetchFreshUpdateStatus() : await api("/api/update/status");
    updateState = state;
    if (!state.testServer && !state.lastError && state.checked && state.lastCheckedAt) {
      updateLastSuccessfulCheckAt = Number(state.lastCheckedAt);
    }
    renderUpdateAvailability(state);
    const shouldShow = state.available && (
      state.forced || (autoShow && !state.ignored && updateAutoShownVersion !== state.latestVersion)
    );
    if (shouldShow) {
      updateAutoShownVersion = state.latestVersion;
      openUpdateDialog(state);
    } else if (!state.available && !$("updateModal")?.classList.contains("hidden") && !updateStartupBlocked) {
      closeUpdateDialog();
    }
  } catch { /* 更新服务不可达不影响主功能 */ }
  finally { updateStatusPending = false; }
}

async function ignoreCurrentUpdate() {
  if (!updateState?.latestVersion || updateState.forced) return;
  const button = $("updateIgnoreBtn");
  button.disabled = true;
  try {
    updateState = await api("/api/update/ignore", { version: updateState.latestVersion });
    closeUpdateDialog();
    renderUpdateAvailability(updateState);
  } catch (error) {
    $("updateStatus").className = "update-status err";
    $("updateStatus").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function installCurrentUpdate() {
  if (updateDialogMode === "check-error") {
    await runStartupUpdateCheck();
    return;
  }
  if (!updateState?.available) return;
  if (!updateState.artifactAvailable || !updateState.installSupported) {
    // Keep a forced dialog actionable even if the server published its
    // manifest before the platform artifact finished uploading.
    if (updateState.forced) {
      await refreshUpdateStatus(true, true);
    }
    return;
  }
  const installButton = $("updateInstallBtn");
  const ignoreButton = $("updateIgnoreBtn");
  installButton.disabled = true;
  ignoreButton.disabled = true;
  installButton.textContent = "正在准备…";
  $("updateStatus").className = "update-status";
  $("updateStatus").textContent = "正在下载并校验更新，请保持软件运行。";
  renderUpdateProgress({ phase: "preparing" });
  startUpdateProgressPolling();
  try {
    const result = await api("/api/update/install", {});
    stopUpdateProgressPolling();
    if (result.platform === "android") {
      if (!window.LucimaUpdate?.installUpdate) throw new Error("Android 安装桥不可用");
      $("updateStatus").className = "update-status ok";
      $("updateStatus").textContent = "校验完成，正在打开系统安装界面。";
      renderUpdateProgress({ phase: "ready", downloadedBytes: 1, totalBytes: 1 });
      window.LucimaUpdate.installUpdate(result.installerPath);
      installButton.textContent = "重新打开安装器";
      installButton.disabled = false;
    } else {
      $("updateStatus").className = "update-status ok";
      $("updateStatus").textContent = "校验完成，软件即将重启并完成更新。";
      renderUpdateProgress({ phase: "restarting", downloadedBytes: 1, totalBytes: 1 });
      installButton.textContent = "正在重启…";
    }
  } catch (error) {
    stopUpdateProgressPolling();
    $("updateStatus").className = "update-status err";
    $("updateStatus").textContent = error.message;
    installButton.textContent = "重试更新";
    installButton.disabled = false;
    ignoreButton.disabled = false;
  }
}

function initUpdateUI() {
  $("aboutUpdateBtn")?.addEventListener("click", () => openUpdateDialog());
  $("updateIgnoreBtn")?.addEventListener("click", ignoreCurrentUpdate);
  $("updateInstallBtn")?.addEventListener("click", installCurrentUpdate);
  runStartupUpdateCheck();
  setTimeout(() => refreshUpdateStatus(true), 4000);
  setTimeout(() => refreshUpdateStatus(true), 18000);
  setInterval(() => refreshUpdateStatus(true), 60 * 60 * 1000);
  const checkOnForeground = () => {
    if (document.visibilityState !== "visible" || !updateStartupReady) return;
    const last = Number(updateLastSuccessfulCheckAt) || 0;
    if (!last || Date.now() - last >= UPDATE_STALE_AFTER_MS) {
      refreshUpdateStatus(true, true);
    }
  };
  document.addEventListener("visibilitychange", checkOnForeground);
  window.addEventListener("focus", checkOnForeground);
}

// ---------- 非阻塞消息弹窗 ----------
function showMessageToast(message, type = "ok", duration = 4200) {
  const region = $("messageToastRegion");
  if (!region || !message) return;
  while (region.children.length >= 3) region.firstElementChild?.remove();

  const toast = document.createElement("div");
  toast.className = `message-toast ${type === "err" ? "err" : "ok"}`;
  toast.setAttribute("role", type === "err" ? "alert" : "status");
  toast.innerHTML = `<svg class="message-toast-icon" aria-hidden="true"><use href="${type === "err" ? "#ui-triangle-alert" : "#ui-circle-check"}"></use></svg>
    <span class="message-toast-text"></span>
    <button class="message-toast-close" type="button" title="关闭" aria-label="关闭消息"><svg aria-hidden="true"><use href="#ui-x"></use></svg></button>`;
  toast.querySelector(".message-toast-text").textContent = message;
  region.appendChild(toast);

  let timer;
  const dismiss = () => {
    clearTimeout(timer);
    if (!toast.isConnected || toast.classList.contains("leaving")) return;
    toast.classList.add("leaving");
    setTimeout(() => toast.remove(), 180);
  };
  toast.querySelector(".message-toast-close").addEventListener("click", dismiss);
  timer = setTimeout(dismiss, duration);
}

// ---------- 开发者日志 ----------
async function saveDevLog() {
  const btn = $("saveDevLogBtn");
  if (!btn || btn.disabled) return;
  const label = btn.querySelector("span");
  const original = label?.textContent || "保存";
  btn.disabled = true;
  try {
    let result;
    if (runtimePlatform === "android" && window.LucimaFiles?.saveDeveloperLogs) {
      const raw = window.LucimaFiles.saveDeveloperLogs();
      result = typeof raw === "string" ? JSON.parse(raw) : raw;
      if (!result?.ok) throw new Error(result?.detail || "保存开发者日志失败");
    } else {
      result = await api("/api/logs/save", {});
    }
    if (label) label.textContent = "已保存";
    btn.classList.add("save-success");
    showMessageToast(`开发者日志已保存至 ${result.path}`, "ok");
  } catch (e) {
    if (label) label.textContent = "保存失败";
    btn.classList.add("save-failed");
    showMessageToast(e.message || "保存开发者日志失败", "err", 5200);
  } finally {
    setTimeout(() => {
      btn.disabled = false;
      if (label) label.textContent = original;
      btn.classList.remove("save-success", "save-failed");
    }, 1600);
  }
}
if ($("saveDevLogBtn")) $("saveDevLogBtn").onclick = saveDevLog;

// ---------- 开发者模式 · 协议控制台 ----------
// 入口靠口令解锁：设置页底部一个无提示输入框（#devGate），输对了控制台就出现，
// 输错了什么都不发生（没有确认按钮、没有报错文案，这是刻意的）。
// 口令**不写在前端**，输入的内容交后端 /api/dev/unlock 校验（真源 config.DEV_PASSPHRASE）。
// ⚠️ 隐藏 UI 不是门禁：真正的门禁是 /api/dev/call 每次都要带口令，由后端验。
// 记住上次输入（按账号），免得每次重开都要重敲
const devLsKey = () => `ark_dev_console::${activeAccount || "_"}`;
function devSaveInput() {
  try {
    frontendStorageSet(devLsKey(), JSON.stringify({
      route: $("devRoute").value, data: $("devData").value,
      useAuth: $("devUseAuth").checked,
    }));
  } catch { /* localStorage 满/禁用：不影响功能 */ }
}
function devLoadInput() {
  try {
    const v = JSON.parse(frontendStorageGet(devLsKey())) || {};
    if (v.route) $("devRoute").value = v.route;
    if (v.data) $("devData").value = v.data;
    if (v.useAuth === false) $("devUseAuth").checked = false;
  } catch { /* 忽略坏数据 */ }
}

// data 输入框的 JSON 实时校验：语法错误当场提示，不等发出去才报
function devCheckJson() {
  const raw = $("devData").value.trim();
  const hint = $("devHint");
  if (!raw) { hint.textContent = "留空 = 只发鉴权字段"; hint.className = "dev-hint"; return {}; }
  try {
    const o = JSON.parse(raw);
    if (o === null || typeof o !== "object" || Array.isArray(o)) {
      hint.textContent = "data 必须是一个 JSON 对象（不是数组/字面量）";
      hint.className = "dev-hint bad";
      return null;
    }
    hint.textContent = `JSON 有效 · ${Object.keys(o).length} 个字段`;
    hint.className = "dev-hint ok";
    return o;
  } catch (e) {
    hint.textContent = `JSON 语法错误：${e.message}`;
    hint.className = "dev-hint bad";
    return null;
  }
}

let DEV_LAST = null;   // 当前响应，用于发送区展示
let DEV_HISTORY = [];
let DEV_RECENT_ROUTES = [];
let DEV_HISTORY_ACCOUNT = "";
let devHistoryTimer = null;
let devHistoryPending = false;

function devHistoryTime(timestamp) {
  if (!Number.isFinite(Number(timestamp))) return "";
  return new Date(Number(timestamp)).toLocaleTimeString("zh-CN", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
}

function renderDevHistory() {
  const list = $("devHistoryList");
  if (!list) return;
  if (!DEV_HISTORY.length) {
    list.innerHTML = '<div class="dev-history-empty">暂无请求记录</div>';
    return;
  }
  list.innerHTML = DEV_HISTORY.map((item, index) => {
    const fields = Object.keys(item.data || {}).length;
    const source = item.source === "console" ? "控制台" : "后台任务";
    const status = item.error ? "请求失败" : item.status == null ? "等待响应" : `HTTP ${item.status}`;
    const meta = [source, devHistoryTime(item.timestamp), status, `${fields} 个字段`, item.useAuth ? "自动鉴权" : "无鉴权"]
      .filter(Boolean).join(" · ");
    return `<div class="dev-history-item">
      <div class="dev-history-copy">
        <strong title="${filterEscape(item.route || "")}">${filterEscape(item.route || "未知 route")}</strong>
        <small>${filterEscape(meta)}</small>
      </div>
      <div class="dev-history-actions">
        <button type="button" class="mini dev-history-copy-request" data-history-copy-request-index="${index}">复制请求</button>
        <button type="button" class="mini dev-history-copy-response" data-history-copy-response-index="${index}">复制响应</button>
        <button type="button" class="mini dev-history-loop" data-history-loop-index="${index}">加入循环</button>
        <button type="button" class="mini dev-history-import" data-history-import-index="${index}">导入</button>
      </div>
    </div>`;
  }).join("");
}

function showDevHistoryCopyFeedback(button, ok) {
  if (!button) return;
  clearTimeout(button._copyFeedbackTimer);
  const original = button.dataset.copyLabel || button.textContent;
  button.dataset.copyLabel = original;
  button.classList.remove("copy-feedback-success", "copy-feedback-error");
  void button.offsetWidth;
  button.classList.add(ok ? "copy-feedback-success" : "copy-feedback-error");
  button.textContent = ok ? "✓" : "!";
  button._copyFeedbackTimer = setTimeout(() => {
    if (!button.isConnected) return;
    button.classList.remove("copy-feedback-success", "copy-feedback-error");
    button.textContent = original;
  }, 1200);
}

async function copyDevHistoryPart(index, kind, button) {
  const item = DEV_HISTORY[index];
  if (!item) return;
  let text = "";
  if (kind === "request") {
    const request = item.request || { route: item.route || "", data: item.data || {} };
    text = JSON.stringify(request, null, 2);
  } else {
    if (item.error) text = `请求失败：${item.error}`;
    else if (typeof item.responseText === "string" && item.responseText.length) text = item.responseText;
    else if (item.isJson) text = JSON.stringify(item.response, null, 2);
    else text = "未收到响应";
  }
  try {
    if (!navigator.clipboard?.writeText) throw new Error("剪贴板不可用");
    await navigator.clipboard.writeText(text);
    showDevHistoryCopyFeedback(button, true);
  } catch {
    showDevHistoryCopyFeedback(button, false);
  }
}

function devImportHistory(index) {
  const item = DEV_HISTORY[index];
  if (!item) return;
  $("devRoute").value = item.route || "";
  $("devData").value = JSON.stringify(item.data || {}, null, 2);
  $("devUseAuth").checked = item.useAuth !== false;
  devCheckJson();
  devSaveInput();
  const card = $("devConsoleCard");
  card?.classList.remove("dev-history-applied");
  void card?.offsetWidth;
  card?.classList.add("dev-history-applied");
  $("devMeta").innerHTML = `<span class="ok">已导入 ${filterEscape(item.route || "请求")}</span>`;
}

async function devRefreshHistory() {
  if (!devPass() || !activeAccount || devHistoryPending) return;
  const email = activeAccount;
  devHistoryPending = true;
  try {
    const result = await api("/api/dev/history", {
      account: email,
      pass: devPass(),
    });
    if (email !== activeAccount) return;
    DEV_HISTORY = Array.isArray(result.history) ? result.history.slice(0, 30) : [];
    const routes = Array.isArray(result.routes)
      ? result.routes.map((route) => String(route || "").trim()).filter(Boolean).slice(0, 30)
      : [];
    if (JSON.stringify(routes) !== JSON.stringify(DEV_RECENT_ROUTES)) {
      DEV_RECENT_ROUTES = routes;
      renderDevRecentRoutes();
    }
    DEV_HISTORY_ACCOUNT = email;
    renderDevHistory();
  } catch { /* 会话切换或退出时静默等待下一轮 */ }
  finally { devHistoryPending = false; }
}

function renderDevRecentRoutes() {
  const holder = $("devPreset");
  if (!holder) return;
  const selected = holder.dataset.val || "";
  const options = DEV_RECENT_ROUTES.map((route) => ({ val: route, label: filterEscape(route) }));
  const current = options.some((option) => option.val === selected) ? selected : "";
  holder.innerHTML = dropdownHTML(
    "dev-preset",
    options,
    current,
    options.length ? "最近运行的 route…" : "暂无运行 route",
  );
  bindDropdown(holder, (value) => {
    if (!value) return;
    $("devRoute").value = value;
    devSaveInput();
  });
}

function ensureDevHistoryPoll() {
  if (!devHistoryTimer) devHistoryTimer = setInterval(devRefreshHistory, 1200);
  devRefreshHistory();
}

const DEV_LOOP_STATES = new Map();
let devLoopItemSeq = 0;

function devLoopState(email = activeAccount) {
  if (!DEV_LOOP_STATES.has(email)) {
    DEV_LOOP_STATES.set(email, {
      items: [], rounds: 1, interval: 1000, running: false, stopRequested: false,
      status: "", statusType: "", wake: null,
    });
  }
  return DEV_LOOP_STATES.get(email);
}

const devClampLoopRounds = (value) => Math.min(9999, Math.max(1, parseInt(value) || 1));
const devClampLoopInterval = (value) => Math.min(60000, Math.max(0, parseInt(value) || 0));

function devRequestSnapshot(item) {
  return {
    id: ++devLoopItemSeq,
    route: String(item?.route || "").trim(),
    data: JSON.parse(JSON.stringify(item?.data || {})),
    useAuth: item?.useAuth !== false,
  };
}

function renderDevLoop() {
  const list = $("devLoopList");
  if (!list || !activeAccount) return;
  const state = devLoopState();
  $("devLoopCountLabel").textContent = `${state.items.length} 条请求`;
  list.innerHTML = state.items.length ? state.items.map((item, index) => {
    const meta = `${Object.keys(item.data || {}).length} 个字段 · ${item.useAuth ? "自动鉴权" : "无鉴权"}`;
    const disabled = state.running ? " disabled" : "";
    return `<div class="dev-loop-item" data-loop-id="${item.id}">
      <span class="dev-loop-index">${index + 1}</span>
      <div class="dev-loop-copy">
        <strong title="${filterEscape(item.route)}">${filterEscape(item.route)}</strong>
        <small>${meta}</small>
      </div>
      <div class="dev-loop-order">
        <button type="button" data-loop-action="up" title="上移" aria-label="上移"${disabled}${index === 0 ? " disabled" : ""}><svg aria-hidden="true"><use href="#ui-chevron-up"></use></svg></button>
        <button type="button" data-loop-action="down" title="下移" aria-label="下移"${disabled}${index === state.items.length - 1 ? " disabled" : ""}><svg aria-hidden="true"><use href="#ui-chevron-down"></use></svg></button>
        <button type="button" data-loop-action="remove" title="移除" aria-label="移除"${disabled}>−</button>
      </div>
    </div>`;
  }).join("") : '<div class="dev-loop-empty">从历史记录或当前发送区加入请求</div>';

  const rounds = $("devLoopRounds");
  const interval = $("devLoopInterval");
  rounds.value = state.rounds;
  interval.value = state.interval;
  rounds.disabled = state.running;
  interval.disabled = state.running;
  $("devLoopAddBtn").disabled = state.running;
  $("devLoopStartBtn").disabled = state.running || !state.items.length;
  $("devLoopStartBtn").classList.toggle("hidden", state.running);
  $("devLoopStopBtn").classList.toggle("hidden", !state.running);
  const status = $("devLoopStatus");
  status.textContent = state.status || (state.items.length ? "等待开始" : "");
  status.className = `dev-loop-status${state.statusType ? ` ${state.statusType}` : ""}`;
}

function devAddLoopRequest(item) {
  const snapshot = devRequestSnapshot(item);
  if (!snapshot.route) return;
  const state = devLoopState();
  if (state.running) return;
  if (state.items.length >= 30) {
    state.status = "循环队列最多添加 30 条请求";
    state.statusType = "bad";
    renderDevLoop();
    return;
  }
  state.items.push(snapshot);
  state.status = `已加入 ${snapshot.route}`;
  state.statusType = "ok";
  renderDevLoop();
}

function devAddCurrentToLoop() {
  const route = $("devRoute").value.trim();
  if (!route) {
    $("devMeta").innerHTML = '<span class="bad">route 不能为空</span>';
    return;
  }
  const data = devCheckJson();
  if (data === null) return;
  devAddLoopRequest({ route, data, useAuth: $("devUseAuth").checked });
}

function devMoveLoopItem(id, action) {
  const state = devLoopState();
  if (state.running) return;
  const index = state.items.findIndex((item) => item.id === id);
  if (index < 0) return;
  if (action === "remove") state.items.splice(index, 1);
  if (action === "up" && index > 0) {
    [state.items[index - 1], state.items[index]] = [state.items[index], state.items[index - 1]];
  }
  if (action === "down" && index < state.items.length - 1) {
    [state.items[index + 1], state.items[index]] = [state.items[index], state.items[index + 1]];
  }
  state.status = "";
  state.statusType = "";
  renderDevLoop();
}

function devShowResponse(result) {
  DEV_LAST = result;
  $("devReq").textContent = JSON.stringify(result.request, null, 2);
  $("devResp").textContent = result.isJson
    ? JSON.stringify(result.json, null, 2)
    : result.rawText;
  const kind = result.isJson ? "JSON" : "明文（非 JSON）";
  const trunc = result.rawLen > (result.rawText || "").length ? "，已截断显示" : "";
  $("devMeta").innerHTML =
    `<span class="${result.status === 200 ? "ok" : "bad"}">HTTP ${result.status}</span>`
    + ` · ${kind}${result.gzip ? " · gzip" : ""} · ${result.rawLen} 字节${trunc} · ${result.elapsedMs}ms`;
}

function devLoopDelay(milliseconds, state) {
  if (!milliseconds) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      state.wake = null;
      resolve();
    }, milliseconds);
    state.wake = () => {
      clearTimeout(timer);
      state.wake = null;
      resolve();
    };
  });
}

function devStopLoop() {
  const state = devLoopState();
  if (!state.running) return;
  state.stopRequested = true;
  state.status = "正在停止，等待当前请求完成…";
  state.statusType = "warn";
  state.wake?.();
  renderDevLoop();
}

async function devStartLoop() {
  const email = activeAccount;
  const state = devLoopState(email);
  if (!email || state.running || !state.items.length) return;
  state.rounds = devClampLoopRounds($("devLoopRounds").value);
  state.interval = devClampLoopInterval($("devLoopInterval").value);
  const queue = state.items.map(devRequestSnapshot);
  const total = state.rounds * queue.length;
  let sent = 0;
  let failures = 0;
  let stopReason = "";
  state.running = true;
  state.stopRequested = false;
  state.statusType = "run";
  renderDevLoop();
  try {
    outer: for (let round = 1; round <= state.rounds; round++) {
      for (let index = 0; index < queue.length; index++) {
        if (state.stopRequested) { stopReason = "已手动停止"; break outer; }
        if (activeAccount !== email) { stopReason = "账号已切换，循环已停止"; break outer; }
        const item = queue[index];
        state.status = `第 ${round} / ${state.rounds} 轮 · ${index + 1} / ${queue.length} · ${item.route}`;
        renderDevLoop();
        try {
          const result = await api("/api/dev/call", {
            account: email, route: item.route, data: item.data,
            useAuth: item.useAuth, pass: devPass(),
          });
          sent += 1;
          if (result.status !== 200) failures += 1;
          if (email === activeAccount) devShowResponse(result);
        } catch (error) {
          sent += 1;
          failures += 1;
          if (email === activeAccount) {
            $("devMeta").innerHTML = `<span class="bad">请求失败：${filterEscape(error.message)}</span>`;
            $("devResp").textContent = "";
          }
        }
        if (sent < total && !state.stopRequested) await devLoopDelay(state.interval, state);
      }
    }
  } finally {
    state.running = false;
    state.stopRequested = false;
    state.wake = null;
    state.status = stopReason || `已完成 ${state.rounds} 轮，共发送 ${sent} 条请求${failures ? `，${failures} 条异常` : ""}`;
    state.statusType = stopReason ? "warn" : (failures ? "bad" : "ok");
    if (email === activeAccount) renderDevLoop();
  }
}

async function devSend() {
  const route = $("devRoute").value.trim();
  if (!route) { $("devMeta").innerHTML = `<span class="bad">route 不能为空</span>`; return; }
  const data = devCheckJson();
  if (data === null) { $("devMeta").innerHTML = `<span class="bad">data 不是合法 JSON，未发送</span>`; return; }
  devSaveInput();
  const btn = $("devSendBtn");
  btn.disabled = true; btn.textContent = "发送中…";
  $("devMeta").innerHTML = "";
  try {
    const r = await api("/api/dev/call", {
      account: activeAccount, route, data, useAuth: $("devUseAuth").checked,
      pass: devPass(),
    });
    devShowResponse(r);
    devRefreshHistory();
  } catch (e) {
    $("devMeta").innerHTML = `<span class="bad">请求失败：${e.message}</span>`;
    $("devResp").textContent = "";
  } finally {
    btn.disabled = false; btn.textContent = "发送请求";
  }
}

// 解锁态：口令存在本机，随每次 /api/dev/call 带上（后端每次都验）。
// ⚠️ **只存内存，不落盘**：每次重启（以及刷新页面）都要重新输口令才显控制台。
// 故意不用 localStorage/sessionStorage —— 否则口令会留在 WebView 的 profile 里，
// 既违背"每次重启要重输"的要求，也等于把口令写在用户磁盘上。
let DEV_PASS = "";
const devPass = () => DEV_PASS;

// 输入框防抖校验：停手 250ms 才问后端一次，别每敲一个字母发一个请求。
let devGateTimer = null;
function bindDevGate() {
  const box = $("devGate");
  if (!box || box.dataset.bound) return;
  box.dataset.bound = "1";
  box.addEventListener("input", () => {
    clearTimeout(devGateTimer);
    const v = box.value;
    if (!v.trim()) return;
    devGateTimer = setTimeout(async () => {
      let ok = false;
      try { ok = !!(await api("/api/dev/unlock", { pass: v })).ok; } catch { ok = false; }
      // 口令错：静默，什么都不做（不清空、不提示）——用户只当这是个普通空框。
      if (!ok) return;
      DEV_PASS = v.trim();
      box.value = "";       // 别把口令留在框里
      box.blur();
      renderDevConsole();
    }, 250);
  });
}

let devUpdateChannelState = null;
let devUpdateChannelPending = false;
let devUpdateChannelError = "";

function renderDevUpdateChannel(selectedChannel = null) {
  const holder = $("devUpdateChannel");
  if (!holder) return;
  const state = devUpdateChannelState || updateState || {};
  holder.innerHTML = dropdownHTML("", [
    { val: "stable", label: "稳定版" },
    { val: "preview", label: "预览版" },
  ], selectedChannel || state.channel || "stable");
  const dropdown = holder.querySelector(".dropdown");
  const button = dropdown.querySelector(".dd-btn");
  button.disabled = devUpdateChannelPending;
  button.setAttribute("aria-labelledby", "devUpdateChannelLabel");
  bindDropdown(dropdown, channel => checkDevUpdateChannel(channel));
  const refresh = $("devUpdateCheckBtn");
  refresh.disabled = devUpdateChannelPending;
  refresh.classList.toggle("loading", devUpdateChannelPending);
  const status = $("devUpdateChannelStatus");
  const error = devUpdateChannelError || state.lastError;
  status.className = `dev-hint dev-update-status${!devUpdateChannelPending && error ? " bad" : ""}`;
  status.textContent = devUpdateChannelPending ? "正在检查更新…"
    : error ? `检查失败：${error}`
    : state.testServer ? "网页测试版不检查更新"
    : state.checking ? "正在检查更新…"
    : !state.checked ? "尚未检查"
    : state.available ? `发现新版本 ${state.latestVersion}`
    : `当前 ${state.currentVersion} · 通道最新 ${state.latestVersion}`;
}

async function checkDevUpdateChannel(channel = null) {
  if (devUpdateChannelPending) return;
  devUpdateChannelPending = true;
  devUpdateChannelError = "";
  renderDevUpdateChannel(channel);
  try {
    devUpdateChannelState = channel
      ? await api("/api/update/channel", { channel, pass: devPass() })
      : await api("/api/update/check", {});
    if (!devUpdateChannelState.testServer && devUpdateChannelState.checking) {
      devUpdateChannelState = await fetchFreshUpdateStatus();
    }
    updateState = devUpdateChannelState;
    if (!updateState.lastError && updateState.checked) {
      updateLastSuccessfulCheckAt = Number(updateState.lastCheckedAt) || Date.now();
    }
    updateAutoShownVersion = "";
    renderUpdateAvailability(updateState);
    if (!updateState.testServer && !updateState.lastError && updateState.available && !updateState.ignored) {
      updateAutoShownVersion = updateState.latestVersion;
      openUpdateDialog(updateState);
    }
  } catch (error) {
    devUpdateChannelError = error.message || "请求失败";
    const state = await api("/api/update/status").catch(() => null);
    if (state) {
      devUpdateChannelState = updateState = state;
      renderUpdateAvailability(state);
    }
  } finally {
    devUpdateChannelPending = false;
    renderDevUpdateChannel();
  }
}

// 显隐整张卡片：以本次运行有没有输过正确口令为准（进程重启即回到隐藏）
function renderDevConsole() {
  const card = $("devConsoleCard");
  if (!card) return;
  const on = !!devPass();
  card.style.display = on ? "" : "none";
  if (on && !card.dataset.bound) {
    card.dataset.bound = "1";
    renderDevUpdateChannel();
    api("/api/update/status").then(state => {
      if (!devUpdateChannelPending && !devUpdateChannelState) {
        devUpdateChannelState = state;
        renderDevUpdateChannel();
      }
    }).catch(() => {});
    $("devUpdateCheckBtn").onclick = () => checkDevUpdateChannel();
    renderDevRecentRoutes();
    $("devSendBtn").onclick = devSend;
    $("devLoopAddBtn").onclick = devAddCurrentToLoop;
    $("devLoopStartBtn").onclick = devStartLoop;
    $("devLoopStopBtn").onclick = devStopLoop;
    $("devHistoryList").addEventListener("click", (event) => {
      const importButton = event.target.closest("[data-history-import-index]");
      const loopButton = event.target.closest("[data-history-loop-index]");
      const copyRequestButton = event.target.closest("[data-history-copy-request-index]");
      const copyResponseButton = event.target.closest("[data-history-copy-response-index]");
      if (importButton) devImportHistory(Number(importButton.dataset.historyImportIndex));
      if (loopButton) devAddLoopRequest(DEV_HISTORY[Number(loopButton.dataset.historyLoopIndex)]);
      if (copyRequestButton) copyDevHistoryPart(Number(copyRequestButton.dataset.historyCopyRequestIndex), "request", copyRequestButton);
      if (copyResponseButton) copyDevHistoryPart(Number(copyResponseButton.dataset.historyCopyResponseIndex), "response", copyResponseButton);
    });
    $("devLoopList").addEventListener("click", (event) => {
      const button = event.target.closest("[data-loop-action]");
      const row = event.target.closest("[data-loop-id]");
      if (button && row) devMoveLoopItem(Number(row.dataset.loopId), button.dataset.loopAction);
    });
    $("devLoopRounds").addEventListener("input", (event) => {
      event.target.value = event.target.value.replace(/[^0-9]/g, "").slice(0, 4);
    });
    $("devLoopRounds").addEventListener("change", (event) => {
      const state = devLoopState();
      state.rounds = devClampLoopRounds(event.target.value);
      renderDevLoop();
    });
    $("devLoopInterval").addEventListener("input", (event) => {
      event.target.value = event.target.value.replace(/[^0-9]/g, "").slice(0, 5);
    });
    $("devLoopInterval").addEventListener("change", (event) => {
      const state = devLoopState();
      state.interval = devClampLoopInterval(event.target.value);
      renderDevLoop();
    });
    $("devData").addEventListener("input", devCheckJson);
    $("devRoute").addEventListener("change", devSaveInput);
  }
  if (on) {
    if (DEV_HISTORY_ACCOUNT !== activeAccount) {
      DEV_HISTORY = [];
      DEV_RECENT_ROUTES = [];
      DEV_HISTORY_ACCOUNT = activeAccount || "";
      renderDevHistory();
      renderDevRecentRoutes();
    }
    devLoadInput();
    devCheckJson();
    renderDevLoop();
    ensureDevHistoryPoll();
  }
}

// 启动：控制台一律从隐藏开始（DEV_PASS 是内存态，每次启动都是空的），
// 只把那个无提示口令框接上事件。
function initDevGate() {
  bindDevGate();
  // 清掉旧版本(v73)遗留在磁盘上的口令：那时是存 localStorage 的，现在不再持久化，
  // 留着既没用又是把口令写在用户机器上。
  frontendStorageRemove("ark_dev_pass");
  renderDevConsole();
}

// ---------- 工具箱预览 ----------
function showToolboxHome() {
  stopBackgroundTestPolling();
  closeEncyclopediaStatusFilter(false);
  closeEncyclopediaCard();
  $("encyclopediaPage")?.classList.add("hidden");
  $("encyclopediaDetailPage")?.classList.add("hidden");
  const home = $("toolboxHome");
  if (home) home.classList.remove("hidden");
  document.querySelectorAll("#section-toolbox .tool-page").forEach((page) => page.classList.add("hidden"));
}

// ---------- 图鉴查询 ----------
// The index stays small enough to ship with the app. Character dossiers and
// large portrait assets are loaded only after a preview is selected.
const ENCYCLOPEDIA_ALL_ICON = "/assets/ui/hero-all.png";
const ENCYCLOPEDIA_ELEMENTS = [
  { id: "ALL", label: "ALL", icon: ENCYCLOPEDIA_ALL_ICON },
  { id: "Fire", label: "火", icon: "/assets/attributes/fire.png" },
  { id: "Ice", label: "水", icon: "/assets/attributes/water.png" },
  { id: "Earth", label: "木", icon: "/assets/attributes/wood.png" },
  { id: "Light", label: "光", icon: "/assets/attributes/light.png" },
  { id: "Dark", label: "暗", icon: "/assets/attributes/dark.png" },
];
const ENCYCLOPEDIA_JOBS = [
  { id: "ALL", label: "ALL", icon: ENCYCLOPEDIA_ALL_ICON },
  { id: "Warrior", label: "战士", icon: "/assets/jobs/Warrior.png" },
  { id: "Caster", label: "术师", icon: "/assets/jobs/Caster.png" },
  { id: "Sniper", label: "狙击", icon: "/assets/jobs/Sniper.png" },
  { id: "Vanguard", label: "先锋", icon: "/assets/jobs/Vanguard.png" },
  { id: "Defender", label: "重装", icon: "/assets/jobs/Defender.png" },
  { id: "Medic", label: "医疗", icon: "/assets/jobs/Medic.png" },
];
const ENCYCLOPEDIA_STARS = [
  { id: "ALL", label: "ALL", icon: ENCYCLOPEDIA_ALL_ICON },
  { id: "5", label: "5星" },
  { id: "4", label: "4星" },
  { id: "3", label: "3星" },
  { id: "2", label: "2星" },
];
const ENCYCLOPEDIA_STATE = {
  index: null,
  ui: null,
  resourceConfig: null,
  details: new Map(),
  filters: { element: "ALL", job: "ALL", star: "ALL", statuses: [] },
  statusDraft: null,
  statusReturnFocus: null,
  statusContext: null,
  mode: "static",
  initialized: false,
  loading: false,
  activeId: null,
  detailGeneration: 0,
  savingImage: false,
  archiveView: {
    zoomFactor: 1,
    scale: 1,
    fitScale: 1,
    panX: 0,
    panY: 0,
    viewport: null,
    stage: null,
    abortController: null,
    resizeObserver: null,
    dragging: false,
    pointerId: null,
    startX: 0,
    startY: 0,
    startPanX: 0,
    startPanY: 0,
    moved: false,
    suppressClickUntil: 0,
  },
};
const ENCYCLOPEDIA_INDEX_URL = "/assets/encyclopedia/index.json";
const ENCYCLOPEDIA_UI_URL = "/assets/encyclopedia/ui.json";
let encyclopediaIndexPending = null;

function encyclopediaEscape(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[character]));
}

function encyclopediaJoinUrl(base, path) {
  if (!path) return "";
  const raw = String(path);
  if (/^(?:https?:)?\/\//i.test(raw) || raw.startsWith("/")) return raw;
  if (!base) return "";
  try {
    const result = new URL(raw.replace(/^\/+/, ""), String(base).replace(/\/?$/, "/"));
    return ["http:", "https:"].includes(result.protocol) ? result.href : "";
  } catch { return ""; }
}

function encyclopediaConfigUrl(path, fallback = "") {
  const config = ENCYCLOPEDIA_STATE.resourceConfig || {};
  if (!path) return fallback;
  const raw = String(path);
  if (/^(?:https?:)?\/\//i.test(raw) || raw.startsWith("/")) {
    try {
      const result = new URL(raw, location.href);
      return ["http:", "https:"].includes(result.protocol) ? result.href : fallback;
    } catch { return fallback; }
  }
  return encyclopediaJoinUrl(config.baseUrl || "", raw) || fallback;
}

function encyclopediaCacheName(url = "") {
  if (/\/objects\/[a-f0-9]{64}\.(png|jpg|jpeg|webp|skel|atlas|js)$/.test(new URL(url || location.href, location.href).pathname)) {
    return "lucima-encyclopedia-objects-v1";
  }
  const version = String(ENCYCLOPEDIA_STATE.resourceConfig?.version || "local").replace(/[^a-z0-9._-]/gi, "_");
  return `lucima-encyclopedia-resources-${version}`;
}

const encyclopediaPendingRequests = new Map();
async function encyclopediaFetch(url) {
  if (!url) throw new Error("图鉴资源地址为空");
  let cache = null;
  try {
    if (typeof caches !== "undefined") {
      cache = await caches.open(encyclopediaCacheName(url));
      const cached = await cache.match(url);
      if (cached) return cached;
    }
  } catch { /* Cache Storage can be unavailable in embedded browsers. */ }
  if (!encyclopediaPendingRequests.has(url)) {
    const pending = (async () => {
      const objectHash = new URL(url, location.href).pathname.match(/\/objects\/([a-f0-9]{64})\.(?:png|jpg|jpeg|webp|skel|atlas|js)$/)?.[1];
      const response = await fetch(url, { cache: objectHash ? "default" : "no-store", credentials: "omit" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      if (objectHash) {
        const digest = await crypto.subtle.digest("SHA-256", await response.clone().arrayBuffer());
        const actual = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
        if (actual !== objectHash) throw new Error("图鉴资源校验失败");
      }
      try { await cache?.put(url, response.clone()); } catch { /* Still display the download. */ }
      return response;
    })();
    encyclopediaPendingRequests.set(url, pending);
  }
  try {
    return (await encyclopediaPendingRequests.get(url)).clone();
  } finally {
    encyclopediaPendingRequests.delete(url);
  }
}
window.fetchEncyclopediaResource = encyclopediaFetch;

async function encyclopediaJson(url) {
  const parsed = new URL(url, location.href);
  const isBundledDetail = parsed.origin === location.origin
    && parsed.pathname.startsWith("/assets/encyclopedia/");
  const response = isBundledDetail
    ? await fetch(parsed.href, { cache: "no-store" })
    : await encyclopediaFetch(url);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

let encyclopediaRuntimePromise = null;
async function ensureEncyclopediaSpineRuntime() {
  if (window.spine) return window.spine;
  if (encyclopediaRuntimePromise) return encyclopediaRuntimePromise;
  const runtimeUrl = ENCYCLOPEDIA_STATE.resourceConfig?.spineRuntimeUrl;
  if (!runtimeUrl) throw new Error("未配置 Spine 运行时");
  encyclopediaRuntimePromise = (async () => {
    const config = ENCYCLOPEDIA_STATE.resourceConfig;
    const resolved = window.LucimaEncyclopediaMedia.resourceUrl(config, runtimeUrl);
    if (!resolved) throw new Error("Spine 运行库未列入资源清单");
    const response = await encyclopediaFetch(resolved);
    const code = await response.text();
    const script = document.createElement("script");
    script.textContent = `${code}\n//# sourceURL=${encyclopediaConfigUrl(runtimeUrl)}`;
    document.head.appendChild(script);
    if (!window.spine) throw new Error("Spine 运行时初始化失败");
    return window.spine;
  })();
  try {
    return await encyclopediaRuntimePromise;
  } catch (error) {
    encyclopediaRuntimePromise = null;
    throw error;
  }
}

async function loadEncyclopediaResourceConfig() {
  if (ENCYCLOPEDIA_STATE.resourceConfig) return ENCYCLOPEDIA_STATE.resourceConfig;
  if (ENCYCLOPEDIA_STATE.resourceConfigPromise) return ENCYCLOPEDIA_STATE.resourceConfigPromise;
  ENCYCLOPEDIA_STATE.resourceConfigPromise = readEncyclopediaResourceConfig();
  return ENCYCLOPEDIA_STATE.resourceConfigPromise;
}

async function readEncyclopediaResourceConfig() {
  let config = {};
  try {
    config = await api("/api/encyclopedia/resources");
  } catch (error) {
    console.warn("读取图鉴资源配置失败：", error);
  }
  if (config.manifestUrl) {
    let cache = null;
    const resolveManifest = async (response) => {
      if (!response?.ok) throw new Error("图鉴资源清单不可用");
      const manifest = await response.json();
      if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)
          || typeof manifest.version !== "string" || !manifest.version.trim()
          || (manifest.baseUrl !== undefined && typeof manifest.baseUrl !== "string")) {
        throw new Error("图鉴资源清单格式无效");
      }
      if (manifest.schema !== undefined && ![1, 2].includes(manifest.schema)) {
        throw new Error("不支持的图鉴资源清单版本");
      }
      if (manifest.schema === 2) {
        const files = manifest.files;
        if (!files || typeof files !== "object" || Array.isArray(files) || !Object.keys(files).length
            || Object.entries(files).some(([path, file]) =>
              path.startsWith("/") || /[\\:%?#\u0000-\u001f]/.test(path)
              || path.split("/").some((part) => !part || part === "." || part === "..")
              || !/\.(png|jpg|jpeg|webp|skel|atlas|js)$/i.test(path)
              || !file || typeof file !== "object" || !/^[a-f0-9]{64}$/.test(file.sha256)
              || !Number.isSafeInteger(file.size) || file.size <= 0)) {
          throw new Error("图鉴资源文件表无效");
        }
      }
      const baseUrl = new URL(manifest.baseUrl || ".", config.manifestUrl);
      if (!["http:", "https:"].includes(baseUrl.protocol) || baseUrl.username || baseUrl.password) {
        throw new Error("图鉴资源地址无效");
      }
      return { ...config, ...manifest, baseUrl: baseUrl.href };
    };
    try {
      try { cache = await caches.open("lucima-encyclopedia-config"); } catch { /* Optional cache. */ }
      try {
        const response = await fetch(config.manifestUrl, {
          cache: "no-cache", credentials: "omit",
          ...(typeof AbortSignal.timeout === "function" ? { signal: AbortSignal.timeout(10000) } : {}),
        });
        const copy = response.clone();
        const resolved = await resolveManifest(response);
        try { await cache?.put(config.manifestUrl, copy); } catch { /* Optional cache. */ }
        config = resolved;
      } catch (error) {
        const response = await cache?.match(config.manifestUrl);
        if (!response) throw error;
        config = await resolveManifest(response);
      }
    } catch {
      // A missing manifest leaves the complete text card available.
      config = {};
    }
  }
  ENCYCLOPEDIA_STATE.resourceConfig = config;
  return config;
}

function encyclopediaLocalIndexUrl() {
  return new URL(ENCYCLOPEDIA_INDEX_URL, location.href).href;
}

async function loadEncyclopediaIndex({ resources = true } = {}) {
  if (resources) void loadEncyclopediaResourceConfig();
  if (ENCYCLOPEDIA_STATE.index && ENCYCLOPEDIA_STATE.ui) return ENCYCLOPEDIA_STATE.index;
  if (!encyclopediaIndexPending) {
    encyclopediaIndexPending = (async () => {
      if (!ENCYCLOPEDIA_STATE.ui) {
        ENCYCLOPEDIA_STATE.ui = await encyclopediaJson(new URL(ENCYCLOPEDIA_UI_URL, location.origin).href);
      }
      const payload = await encyclopediaJson(encyclopediaLocalIndexUrl());
      const characters = Array.isArray(payload) ? payload : payload?.characters;
      if (!Array.isArray(characters)) throw new Error("图鉴索引格式无效");
      ENCYCLOPEDIA_STATE.index = characters.map((entry) => ({
        ...entry,
        id: String(entry.id || ""),
        number: Number(entry.number) || Number(String(entry.id || "").slice(1)) || 9999,
        statuses: Array.isArray(entry.statuses) ? entry.statuses : [],
      })).filter((entry) => entry.id).sort((left, right) => left.number - right.number || left.id.localeCompare(right.id));
      return ENCYCLOPEDIA_STATE.index;
    })().finally(() => { encyclopediaIndexPending = null; });
  }
  return encyclopediaIndexPending;
}

function characterMatchesFilters(entry, filters) {
  return (filters.element === "ALL" || entry.element === filters.element)
    && (filters.job === "ALL" || entry.job === filters.job)
    && (!filters.star || filters.star === "ALL" || Number(entry.star) === Number(filters.star))
    && (!filters.statuses.length || (entry.statuses || []).some((id) => filters.statuses.includes(id)));
}

function characterStatusIconsHTML(statuses) {
  const options = encyclopediaStatusOptions();
  const names = new Map([...options.buff, ...options.debuff].map((option) => [option.id, option.name]));
  return statuses.map((id) => {
    const icon = ENCYCLOPEDIA_STATE.ui?.status_icons?.[id];
    if (!icon) return "";
    const name = names.get(id) || id;
    return `<span class="encyclopedia-status-selected-icon" role="listitem" data-encyclopedia-selected-status="${encyclopediaEscape(id)}" title="${encyclopediaEscape(name)}"><img src="${encyclopediaEscape(icon)}" alt="${encyclopediaEscape(name)}"></span>`;
  }).join("");
}

function encyclopediaFilterButtonHTML(option, type, active, scope = "encyclopedia") {
  const themeIconClass = type === "job" || option.id === "ALL" ? " task-icon-light-invert" : "";
  const content = type === "star" && option.id !== "ALL"
    ? `<span class="encyclopedia-filter-rating" aria-hidden="true"><span>${encyclopediaEscape(option.id)}</span><svg><use href="#ui-star"></use></svg></span>`
    : `<img class="encyclopedia-filter-icon${themeIconClass}" src="${encyclopediaEscape(option.icon)}" alt="">`;
  return `<button type="button" class="encyclopedia-filter-option ${active ? "active" : ""} ${option.id === "ALL" ? "is-all" : ""}" data-${scope}-filter="${type}" data-value="${encyclopediaEscape(option.id)}" aria-label="${encyclopediaEscape(option.label)}" title="${encyclopediaEscape(option.label)}" aria-pressed="${active}">
    ${content}
  </button>`;
}

function renderEncyclopediaFilters() {
  const elementHost = $("encyclopediaElementFilters");
  const jobHost = $("encyclopediaJobFilters");
  const starHost = $("encyclopediaStarFilters");
  if (elementHost) elementHost.innerHTML = ENCYCLOPEDIA_ELEMENTS.map((option) => encyclopediaFilterButtonHTML(option, "element", ENCYCLOPEDIA_STATE.filters.element === option.id)).join("");
  if (jobHost) jobHost.innerHTML = ENCYCLOPEDIA_JOBS.map((option) => encyclopediaFilterButtonHTML(option, "job", ENCYCLOPEDIA_STATE.filters.job === option.id)).join("");
  if (starHost) starHost.innerHTML = ENCYCLOPEDIA_STARS.map((option) => encyclopediaFilterButtonHTML(option, "star", ENCYCLOPEDIA_STATE.filters.star === option.id)).join("");
  updateEncyclopediaStatusFilterButton();
}

function encyclopediaStatusOptions() {
  const options = ENCYCLOPEDIA_STATE.ui?.status_filters || {};
  return {
    buff: Array.isArray(options.buff) ? options.buff : [],
    debuff: Array.isArray(options.debuff) ? options.debuff : [],
  };
}

function updateEncyclopediaStatusFilterButton() {
  const button = $("encyclopediaStatusFilterButton");
  if (!button) return;
  const options = encyclopediaStatusOptions();
  const count = ENCYCLOPEDIA_STATE.filters.statuses.length;
  button.disabled = !ENCYCLOPEDIA_STATE.index || !(options.buff.length + options.debuff.length);
  button.classList.toggle("active", count > 0);
  button.setAttribute("aria-label", count ? `状态筛选，已选 ${count} 种状态` : "选择状态，不限状态");
  const icons = $("encyclopediaStatusSelectedIcons");
  if (icons) icons.innerHTML = characterStatusIconsHTML(ENCYCLOPEDIA_STATE.filters.statuses);
}

function renderEncyclopediaStatusOptions() {
  const options = encyclopediaStatusOptions();
  const draft = ENCYCLOPEDIA_STATE.statusDraft;
  for (const [tone, hostId] of [["buff", "encyclopediaStatusBuffOptions"], ["debuff", "encyclopediaStatusDebuffOptions"]]) {
    const host = $(hostId);
    if (!host) continue;
    host.innerHTML = options[tone].map((option) => {
      const checked = draft?.has(option.id) || false;
      const icon = ENCYCLOPEDIA_STATE.ui?.status_icons?.[option.id];
      return `<button type="button" class="encyclopedia-status-option${checked ? " selected" : ""}" data-encyclopedia-status="${encyclopediaEscape(option.id)}" title="${encyclopediaEscape(option.name)}" aria-label="${encyclopediaEscape(option.name)}" aria-pressed="${checked}">
        <img src="${encyclopediaEscape(icon || "")}" alt="">
      </button>`;
    }).join("");
  }
  updateEncyclopediaStatusSelection();
}

function updateEncyclopediaStatusSelection() {
  const count = ENCYCLOPEDIA_STATE.statusDraft?.size || 0;
  const output = $("encyclopediaStatusSelectionCount");
  if (output) output.textContent = `已选 ${count} 种状态`;
  const clear = $("encyclopediaStatusClear");
  if (clear) clear.disabled = count === 0;
}

function openEncyclopediaStatusFilter() {
  const button = $("encyclopediaStatusFilterButton");
  if (!button || button.disabled) return;
  openCharacterStatusFilter({ trigger: button, statuses: ENCYCLOPEDIA_STATE.filters.statuses, onApply: (statuses) => {
    ENCYCLOPEDIA_STATE.filters.statuses = statuses;
    updateEncyclopediaStatusFilterButton();
    renderEncyclopediaGrid();
  } });
}

function openCharacterStatusFilter(context) {
  const modal = $("encyclopediaStatusModal");
  if (!modal) return;
  bindCharacterStatusFilter();
  ENCYCLOPEDIA_STATE.statusContext = context;
  ENCYCLOPEDIA_STATE.statusDraft = new Set(context.statuses);
  ENCYCLOPEDIA_STATE.statusReturnFocus = context.trigger || document.activeElement;
  renderEncyclopediaStatusOptions();
  modal.classList.toggle("abyss-status-modal", !!context.trigger?.closest("#abyssRolePicker"));
  modal.classList.remove("hidden");
  context.trigger?.setAttribute("aria-expanded", "true");
  modal.querySelector(".encyclopedia-status-body").scrollTop = 0;
  modal.querySelector(".scheme-import-close")?.focus();
}

function closeEncyclopediaStatusFilter(restoreFocus = true) {
  const modal = $("encyclopediaStatusModal");
  if (!modal || modal.classList.contains("hidden")) return;
  modal.classList.add("hidden");
  modal.classList.remove("abyss-status-modal");
  ENCYCLOPEDIA_STATE.statusContext?.trigger?.setAttribute("aria-expanded", "false");
  ENCYCLOPEDIA_STATE.statusContext = null;
  ENCYCLOPEDIA_STATE.statusDraft = null;
  const returnFocus = ENCYCLOPEDIA_STATE.statusReturnFocus;
  ENCYCLOPEDIA_STATE.statusReturnFocus = null;
  if (restoreFocus && returnFocus?.isConnected) returnFocus.focus();
}

function applyEncyclopediaStatusFilter() {
  if (!ENCYCLOPEDIA_STATE.statusDraft) return;
  const statuses = [...ENCYCLOPEDIA_STATE.statusDraft], context = ENCYCLOPEDIA_STATE.statusContext;
  closeEncyclopediaStatusFilter();
  context?.onApply(statuses);
}

function encyclopediaFilteredIndex() {
  return (ENCYCLOPEDIA_STATE.index || []).filter((entry) => characterMatchesFilters(entry, ENCYCLOPEDIA_STATE.filters));
}

function renderEncyclopediaGrid() {
  const grid = $("encyclopediaGrid");
  const empty = $("encyclopediaEmpty");
  if (!grid) return;
  const entries = encyclopediaFilteredIndex();
  grid.innerHTML = entries.map((entry) => `<button class="encyclopedia-preview" type="button" data-encyclopedia-character="${encyclopediaEscape(entry.id)}" aria-label="查看 ${encyclopediaEscape(entry.name || entry.id)} 图鉴">
    <span class="encyclopedia-preview-avatar"><img src="${encyclopediaEscape(entry.avatar || "/assets/ui/hero-all.png")}" alt="" loading="lazy"></span>
    <span class="encyclopedia-preview-name">${encyclopediaEscape(entry.name || entry.id)}</span>
  </button>`).join("");
  empty?.classList.toggle("hidden", entries.length > 0);
  const count = $("encyclopediaResultCount");
  if (count) count.textContent = String(entries.length);
  const total = $("encyclopediaTotalCount");
  if (total) total.textContent = String((ENCYCLOPEDIA_STATE.index || []).length);
}

function setEncyclopediaMode(mode, rerender = true) {
  ENCYCLOPEDIA_STATE.mode = mode === "dynamic" ? "dynamic" : "static";
  frontendStorageSet("encyclopedia_portrait_mode", ENCYCLOPEDIA_STATE.mode);
  document.querySelectorAll("[data-encyclopedia-mode]").forEach((button) => {
    const active = button.dataset.encyclopediaMode === ENCYCLOPEDIA_STATE.mode;
    button.setAttribute("aria-pressed", String(active));
  });
  if (rerender && ENCYCLOPEDIA_STATE.activeId && !$("encyclopediaDetailPage")?.classList.contains("hidden")) {
    $("encyclopediaCard").dataset.portraitMode = ENCYCLOPEDIA_STATE.mode;
    encyclopediaArchiveViewState().renderer?.setStaticMode(ENCYCLOPEDIA_STATE.mode === "static");
    void encyclopediaArchiveViewState().media?.setMode(ENCYCLOPEDIA_STATE.mode);
  }
}

function encyclopediaDetailUrl(entry) {
  return new URL(`/assets/encyclopedia/${entry.detail || `characters/${entry.id}.json`}`, location.origin).href;
}

async function loadEncyclopediaDetail(entry) {
  if (ENCYCLOPEDIA_STATE.details.has(entry.id)) return ENCYCLOPEDIA_STATE.details.get(entry.id);
  const detail = await encyclopediaJson(encyclopediaDetailUrl(entry));
  ENCYCLOPEDIA_STATE.details.set(entry.id, detail);
  return detail;
}

function encyclopediaArchiveViewState() {
  return ENCYCLOPEDIA_STATE.archiveView;
}

function disposeEncyclopediaArchiveViewport() {
  const view = encyclopediaArchiveViewState();
  view.media?.dispose?.();
  view.media = null;
  view.renderer?.dispose?.();
  view.abortController?.abort?.();
  view.resizeObserver?.disconnect?.();
  view.stopGestures?.();
  view.stopGestures = null;
  view.viewport?.classList.remove("is-dragging");
  view.viewport = null;
  view.stage = null;
  view.shell = null;
  view.scaleNode = null;
  view.card = null;
  view.renderer = null;
  view.abortController = null;
  view.resizeObserver = null;
  view.dragging = false;
  view.pointerId = null;
}

function encyclopediaArchiveClamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

function updateEncyclopediaZoomControls() {
  const view = encyclopediaArchiveViewState();
  const label = $("encyclopediaZoomValue");
  if (label) label.textContent = `${Math.round((view.scale || view.fitScale || 1) * 100)}%`;
  document.querySelectorAll("[data-encyclopedia-zoom]").forEach((button) => {
    const action = button.dataset.encyclopediaZoom;
    if (action === "out") button.disabled = (view.zoomFactor || 1) <= 0.8 + 0.001;
    if (action === "in") button.disabled = (view.zoomFactor || 1) >= 3 - 0.001;
  });
}

function positionEncyclopediaArchiveViewport(preserveAnchor = false, anchor = null) {
  const view = encyclopediaArchiveViewState();
  const { viewport, stage, shell, scaleNode, card } = view;
  if (!viewport || !stage || !shell || !scaleNode || !card) return;
  const oldStageWidth = stage.offsetWidth || viewport.clientWidth;
  const oldStageHeight = stage.offsetHeight || viewport.clientHeight;
  const oldCenterX = viewport.scrollLeft + viewport.clientWidth / 2;
  const oldCenterY = viewport.scrollTop + viewport.clientHeight / 2;
  const oldAnchorX = preserveAnchor && oldStageWidth > viewport.clientWidth
    ? oldCenterX / oldStageWidth : 0.5;
  const oldAnchorY = preserveAnchor && oldStageHeight > viewport.clientHeight
    ? oldCenterY / oldStageHeight : 0;

  const naturalHeight = Math.max(1200, card.offsetHeight || 0, card.scrollHeight || 0);
  const availableWidth = Math.max(1, viewport.clientWidth);
  const fitScale = Math.min(1, availableWidth / 1000);
  const zoomFactor = encyclopediaArchiveClamp(Number(view.zoomFactor) || 1, 0.8, 3);
  const scale = fitScale * zoomFactor;
  const scaledWidth = Math.max(1, Math.round(1000 * scale));
  const scaledHeight = Math.max(1, Math.round(naturalHeight * scale));
  view.fitScale = fitScale;
  view.scale = scale;
  view.zoomFactor = zoomFactor;

  scaleNode.style.width = "1000px";
  scaleNode.style.height = `${naturalHeight}px`;
  scaleNode.style.setProperty("--archive-scale", String(scale));
  scaleNode.style.setProperty("--archive-card-height", `${naturalHeight}px`);
  shell.style.width = `${scaledWidth}px`;
  shell.style.height = `${scaledHeight}px`;
  shell.style.setProperty("--archive-scaled-width", `${scaledWidth}px`);
  shell.style.setProperty("--archive-scaled-height", `${scaledHeight}px`);
  stage.style.width = `${Math.max(viewport.clientWidth, scaledWidth)}px`;
  stage.style.height = `${Math.max(viewport.clientHeight, scaledHeight)}px`;

  const maxScrollX = Math.max(0, viewport.scrollWidth - viewport.clientWidth);
  const maxScrollY = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
  const rect = anchor ? card.getBoundingClientRect() : null;
  const nextX = anchor ? viewport.scrollLeft + rect.left + anchor.x * scale - anchor.clientX
    : preserveAnchor ? oldAnchorX * stage.offsetWidth - viewport.clientWidth / 2 : maxScrollX / 2;
  const nextY = anchor ? viewport.scrollTop + rect.top + anchor.y * scale - anchor.clientY
    : preserveAnchor ? oldAnchorY * stage.offsetHeight - viewport.clientHeight / 2 : 0;
  viewport.scrollLeft = encyclopediaArchiveClamp(nextX, 0, maxScrollX);
  viewport.scrollTop = encyclopediaArchiveClamp(nextY, 0, maxScrollY);
  updateEncyclopediaZoomControls();
}

function initEncyclopediaArchiveViewport(viewport) {
  if (!viewport) return;
  disposeEncyclopediaArchiveViewport();
  const stage = viewport.querySelector(".encyclopedia-archive-stage");
  const shell = viewport.querySelector(".encyclopedia-archive-shell");
  const scaleNode = viewport.querySelector(".encyclopedia-archive-scale");
  const card = viewport.querySelector(".archive-card");
  if (!stage || !shell || !scaleNode || !card) return;
  const view = encyclopediaArchiveViewState();
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const options = controller ? { signal: controller.signal } : undefined;
  view.viewport = viewport;
  view.stage = stage;
  view.shell = shell;
  view.scaleNode = scaleNode;
  view.card = card;
  view.abortController = controller;
  view.dragging = false;
  view.moved = false;
  view.suppressClickUntil = 0;

  const pointers = new Map();
  let pinch = null;
  let gestureFrame = 0;
  const pairPosition = () => {
    const [first, second] = [...pointers.values()];
    return { clientX: (first.x + second.x) / 2, clientY: (first.y + second.y) / 2,
      distance: Math.hypot(first.x - second.x, first.y - second.y) };
  };
  const capturePointer = (id) => viewport.setPointerCapture?.(id);
  const releasePointer = (id) => {
    if (viewport.hasPointerCapture?.(id)) viewport.releasePointerCapture(id);
  };
  const rebaseGesture = () => {
    pinch = null;
    view.dragging = false;
    view.pointerId = null;
    if (pointers.size >= 2) {
      const pair = pairPosition();
      const rect = card.getBoundingClientRect();
      pinch = { distance: Math.max(1, pair.distance), zoomFactor: view.zoomFactor,
        x: (pair.clientX - rect.left) / view.scale, y: (pair.clientY - rect.top) / view.scale };
      view.dragging = true;
      view.moved = true;
      for (const id of pointers.keys()) capturePointer(id);
    } else if (pointers.size === 1) {
      const [id, point] = [...pointers][0];
      if (!point.interactive || view.moved) {
        view.dragging = true;
        view.pointerId = id;
        view.startX = point.x;
        view.startY = point.y;
        view.startPanX = viewport.scrollLeft;
        view.startPanY = viewport.scrollTop;
        capturePointer(id);
      }
    }
    viewport.classList.toggle("is-dragging", view.dragging);
  };
  const applyGesture = () => {
    gestureFrame = 0;
    if (view.viewport !== viewport) return;
    if (pinch) {
      const pair = pairPosition();
      view.zoomFactor = encyclopediaArchiveClamp(pinch.zoomFactor * pair.distance / pinch.distance, 0.8, 3);
      positionEncyclopediaArchiveViewport(false, { ...pinch, ...pair });
    } else if (view.dragging && view.moved) {
      const point = pointers.get(view.pointerId);
      viewport.scrollLeft = view.startPanX - (point.x - view.startX);
      viewport.scrollTop = view.startPanY - (point.y - view.startY);
    }
  };
  const flushGesture = () => {
    if (!gestureFrame) return;
    cancelAnimationFrame(gestureFrame);
    applyGesture();
  };
  const stopGestures = () => {
    if (gestureFrame) cancelAnimationFrame(gestureFrame);
    gestureFrame = 0;
    const ids = [...pointers.keys()];
    pointers.clear();
    pinch = null;
    view.dragging = false;
    view.pointerId = null;
    viewport.classList.remove("is-dragging");
    for (const id of ids) releasePointer(id);
  };
  view.stopGestures = stopGestures;

  const onPointerDown = (event) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    if ([...pointers.values()].some((point) => point.type !== event.pointerType)
        || (pointers.size && event.pointerType !== "touch")) return;
    const interactive = Boolean(event.target.closest?.("summary,button,a,input,select,textarea"));
    // A control tap stays native; a second finger can still start a pinch there.
    if (interactive && event.pointerType !== "touch") return;
    flushGesture();
    if (!pointers.size) view.moved = false;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY, type: event.pointerType, interactive });
    rebaseGesture();
    if (pinch) event.preventDefault();
  };
  const onPointerMove = (event) => {
    const point = pointers.get(event.pointerId);
    if (!point) return;
    point.x = event.clientX;
    point.y = event.clientY;
    if (!view.dragging) return;
    const deltaX = event.clientX - view.startX;
    const deltaY = event.clientY - view.startY;
    if (pinch || Math.hypot(deltaX, deltaY) > 4) view.moved = true;
    if (!view.moved) return;
    event.preventDefault();
    if (!gestureFrame) gestureFrame = requestAnimationFrame(applyGesture);
  };
  const onPointerUp = (event) => {
    if (!pointers.has(event.pointerId)) return;
    flushGesture();
    pointers.delete(event.pointerId);
    if (view.moved) view.suppressClickUntil = performance.now() + (event.pointerType === "touch" ? 300 : 80);
    rebaseGesture();
    releasePointer(event.pointerId);
  };
  const onClick = (event) => {
    if (view.suppressClickUntil && performance.now() < view.suppressClickUntil) {
      event.preventDefault();
      event.stopPropagation();
      view.suppressClickUntil = 0;
    }
  };
  viewport.addEventListener("pointerdown", onPointerDown, options);
  viewport.addEventListener("pointermove", onPointerMove, options);
  window.addEventListener("pointerup", onPointerUp, options);
  window.addEventListener("pointercancel", onPointerUp, options);
  viewport.addEventListener("lostpointercapture", onPointerUp, options);
  viewport.addEventListener("click", onClick, { ...options, capture: true });
  window.addEventListener("blur", stopGestures, options);
  const resize = () => positionEncyclopediaArchiveViewport(true, pinch ? { ...pinch, ...pairPosition() } : null);
  window.addEventListener("resize", resize, options);
  if (typeof ResizeObserver === "function") {
    view.resizeObserver = new ResizeObserver(resize);
    view.resizeObserver.observe(viewport);
    view.resizeObserver.observe(card);
  }
  positionEncyclopediaArchiveViewport(false);
}

function setEncyclopediaZoom(action) {
  const view = encyclopediaArchiveViewState();
  if (!view.viewport) return;
  view.stopGestures?.();
  if (action === "reset") view.zoomFactor = 1;
  else if (action === "in") view.zoomFactor = encyclopediaArchiveClamp((view.zoomFactor || 1) * 1.15, 0.8, 3);
  else if (action === "out") view.zoomFactor = encyclopediaArchiveClamp((view.zoomFactor || 1) / 1.15, 0.8, 3);
  positionEncyclopediaArchiveViewport(true);
}

function renderEncyclopediaCard(entry, detail) {
  const card = $("encyclopediaCard");
  const body = $("encyclopediaCardBody");
  if (!card || !body) return;
  disposeEncyclopediaArchiveViewport();
  card.dataset.portraitMode = ENCYCLOPEDIA_STATE.mode;
  $("encyclopediaCardId").textContent = entry.id;
  $("encyclopediaCardTitle").textContent = detail.NAME || entry.name || entry.id;
  $("encyclopediaCardNameEn").textContent = detail.NAME_EN || entry.nameEn || "";
  body.innerHTML = `<div class="encyclopedia-archive-viewport"><div class="encyclopedia-archive-stage"><div class="encyclopedia-archive-shell"><div class="encyclopedia-archive-scale"></div></div></div></div>`;
  const renderer = window.LucimaEncyclopediaCard.mount({
    host: body.querySelector(".encyclopedia-archive-scale"),
    data: detail,
    translations: ENCYCLOPEDIA_STATE.ui || {},
    staticMode: ENCYCLOPEDIA_STATE.mode === "static",
  });
  initEncyclopediaArchiveViewport(body.querySelector(".encyclopedia-archive-viewport"));
  encyclopediaArchiveViewState().renderer = renderer;
  updateEncyclopediaSaveButton();
  void loadEncyclopediaResourceConfig().then((config) => {
    const view = encyclopediaArchiveViewState();
    if (view.renderer !== renderer) return;
    view.media = window.LucimaEncyclopediaMedia.mount({
      renderer, entry, data: detail, config,
      fetchResource: encyclopediaFetch, loadRuntime: ensureEncyclopediaSpineRuntime,
    });
    void view.media.setMode(ENCYCLOPEDIA_STATE.mode);
  });
}

async function openEncyclopediaCard(id) {
  const entry = ENCYCLOPEDIA_STATE.index?.find((item) => item.id === id);
  const detailPage = $("encyclopediaDetailPage");
  const listPage = $("encyclopediaPage");
  const body = $("encyclopediaCardBody");
  if (!entry || !detailPage || !body) return;
  if (ENCYCLOPEDIA_STATE.activeId !== id) {
    encyclopediaArchiveViewState().zoomFactor = 1;
    encyclopediaArchiveViewState().scale = 1;
  }
  disposeEncyclopediaArchiveViewport();
  const generation = ++ENCYCLOPEDIA_STATE.detailGeneration;
  ENCYCLOPEDIA_STATE.activeId = id;
  $("toolboxHome")?.classList.add("hidden");
  listPage?.classList.add("hidden");
  detailPage.classList.remove("hidden");
  $("content")?.scrollTo({ top: 0, behavior: "auto" });
  document.body.classList.add("encyclopedia-detail-open");
  body.innerHTML = `<div class="encyclopedia-card-loading">加载 ${encyclopediaEscape(entry.name || entry.id)}…</div>`;
  updateEncyclopediaSaveButton();
  try {
    const detail = await loadEncyclopediaDetail(entry);
    if (ENCYCLOPEDIA_STATE.detailGeneration === generation && ENCYCLOPEDIA_STATE.activeId === id) renderEncyclopediaCard(entry, detail);
  } catch (error) {
    if (ENCYCLOPEDIA_STATE.detailGeneration === generation && ENCYCLOPEDIA_STATE.activeId === id) {
      body.innerHTML = `<div class="encyclopedia-card-loading">图鉴卡片加载失败：${encyclopediaEscape(error.message || "未知错误")}</div>`;
    }
  }
}

function showEncyclopediaList() {
  $("encyclopediaDetailPage")?.classList.add("hidden");
  $("encyclopediaPage")?.classList.remove("hidden");
}

function closeEncyclopediaCard() {
  ENCYCLOPEDIA_STATE.detailGeneration += 1;
  ENCYCLOPEDIA_STATE.activeId = null;
  showEncyclopediaList();
  document.body.classList.remove("encyclopedia-detail-open");
  disposeEncyclopediaArchiveViewport();
  $("encyclopediaCardBody")?.replaceChildren();
  updateEncyclopediaSaveButton();
}

function updateEncyclopediaSaveButton() {
  const button = $("encyclopediaSaveImage");
  if (!button) return;
  button.disabled = ENCYCLOPEDIA_STATE.savingImage || !ENCYCLOPEDIA_STATE.activeId
    || !$("encyclopediaCardBody")?.querySelector(".encyclopedia-ark2");
  button.setAttribute("aria-busy", String(ENCYCLOPEDIA_STATE.savingImage));
  button.querySelector("span").textContent = ENCYCLOPEDIA_STATE.savingImage ? "保存中…" : "保存图片";
}

async function saveEncyclopediaImageAndroid(filename, pngBase64) {
  const bridge = window.LucimaFiles;
  if (!bridge?.saveEncyclopediaImageStart || !bridge?.saveEncyclopediaImageChunk) {
    throw new Error("当前 Android 版本不支持保存图鉴图片，请更新应用");
  }
  const read = (raw) => {
    const result = typeof raw === "string" ? JSON.parse(raw) : raw;
    if (!result?.ok) throw new Error(result?.detail || "保存图鉴图片失败");
    return result;
  };
  const started = read(bridge.saveEncyclopediaImageStart(filename));
  let complete = false;
  try {
    const chunkSize = 512 * 1024;
    for (let offset = 0; offset < pngBase64.length; offset += chunkSize) {
      // Yield between synchronous native bridge calls so progress can repaint.
      await new Promise((resolve) => setTimeout(resolve, 0));
      const finalChunk = offset + chunkSize >= pngBase64.length;
      const result = read(bridge.saveEncyclopediaImageChunk(started.token, pngBase64.slice(offset, offset + chunkSize), finalChunk));
      if (finalChunk) {
        complete = true;
        return result;
      }
    }
    throw new Error("图鉴图片数据为空");
  } finally {
    if (!complete) {
      try { bridge.saveEncyclopediaImageCancel?.(started.token); } catch { /* Native cleanup also runs on Activity destruction. */ }
    }
  }
}

async function saveEncyclopediaImage() {
  if ($("encyclopediaSaveImage")?.disabled) return;
  const entry = ENCYCLOPEDIA_STATE.index?.find((item) => item.id === ENCYCLOPEDIA_STATE.activeId);
  if (!entry) return;
  ENCYCLOPEDIA_STATE.savingImage = true;
  updateEncyclopediaSaveButton();
  try {
    const [data, config] = await Promise.all([loadEncyclopediaDetail(entry), loadEncyclopediaResourceConfig()]);
    const { pngBase64 } = await window.LucimaEncyclopediaExport.render({
      entry, data, translations: ENCYCLOPEDIA_STATE.ui || {}, config, fetchResource: encyclopediaFetch,
    });
    const name = data.NAME || entry.name || entry.id;
    const result = runtimePlatform === "android"
      ? await saveEncyclopediaImageAndroid(`LucimaTools-encyclopedia-${entry.id}-${name}.png`, pngBase64)
      : await api("/api/encyclopedia/save-image", { characterId: entry.id, name, pngBase64 });
    if (!result?.ok) throw new Error(result?.detail || "保存图鉴图片失败");
    showMessageToast(`图鉴图片已保存：${result.path}`, "ok", 5200);
  } catch (error) {
    showMessageToast(`保存图鉴图片失败：${error.message || "未知错误"}`, "err", 5200);
  } finally {
    ENCYCLOPEDIA_STATE.savingImage = false;
    updateEncyclopediaSaveButton();
  }
}

function bindCharacterStatusFilter() {
  const modal = $("encyclopediaStatusModal");
  if (!modal || modal.dataset.bound) return;
  modal.dataset.bound = "1";
  $("encyclopediaStatusModal")?.addEventListener("click", (event) => {
    if (event.target.closest("[data-encyclopedia-status-apply]")) applyEncyclopediaStatusFilter();
    else if (event.target.closest("[data-encyclopedia-status-close]")) closeEncyclopediaStatusFilter();
    else {
      const button = event.target.closest("[data-encyclopedia-status]");
      const draft = ENCYCLOPEDIA_STATE.statusDraft;
      if (!button || !draft) return;
      const id = button.dataset.encyclopediaStatus;
      const selected = !draft.has(id);
      if (selected) draft.add(id);
      else draft.delete(id);
      button.classList.toggle("selected", selected);
      button.setAttribute("aria-pressed", String(selected));
      updateEncyclopediaStatusSelection();
    }
  });
  $("encyclopediaStatusClear")?.addEventListener("click", () => {
    ENCYCLOPEDIA_STATE.statusDraft?.clear();
    $("encyclopediaStatusModal")?.querySelectorAll("[data-encyclopedia-status]").forEach((button) => {
      button.setAttribute("aria-pressed", "false");
      button.classList.remove("selected");
    });
    updateEncyclopediaStatusSelection();
  });
  $("encyclopediaStatusApply")?.addEventListener("click", applyEncyclopediaStatusFilter);
  document.addEventListener("keydown", (event) => {
    const modal = $("encyclopediaStatusModal");
    if (modal && !modal.classList.contains("hidden")) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeEncyclopediaStatusFilter();
      } else if (event.key === "Tab") {
        const controls = [...modal.querySelector(".encyclopedia-status-dialog").querySelectorAll("button:not(:disabled), input:not(:disabled)")];
        const first = controls[0], last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if ((!event.shiftKey && document.activeElement === last) || !modal.contains(document.activeElement)) {
          event.preventDefault();
          first?.focus();
        }
      }
      return;
    }
  });
}

async function initEncyclopedia() {
  if (ENCYCLOPEDIA_STATE.initialized) return;
  ENCYCLOPEDIA_STATE.initialized = true;
  ENCYCLOPEDIA_STATE.mode = frontendStorageGet("encyclopedia_portrait_mode") === "dynamic" ? "dynamic" : "static";
  renderEncyclopediaFilters();
  setEncyclopediaMode(ENCYCLOPEDIA_STATE.mode, false);
  $("encyclopediaElementFilters")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-encyclopedia-filter]");
    if (!button) return;
    ENCYCLOPEDIA_STATE.filters.element = button.dataset.value || "ALL";
    renderEncyclopediaFilters();
    renderEncyclopediaGrid();
  });
  $("encyclopediaJobFilters")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-encyclopedia-filter]");
    if (!button) return;
    ENCYCLOPEDIA_STATE.filters.job = button.dataset.value || "ALL";
    renderEncyclopediaFilters();
    renderEncyclopediaGrid();
  });
  $("encyclopediaStarFilters")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-encyclopedia-filter]");
    if (!button) return;
    ENCYCLOPEDIA_STATE.filters.star = button.dataset.value || "ALL";
    renderEncyclopediaFilters();
    renderEncyclopediaGrid();
  });
  $("encyclopediaStatusFilterButton")?.addEventListener("click", openEncyclopediaStatusFilter);
  bindCharacterStatusFilter();
  $("encyclopediaGrid")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-encyclopedia-character]");
    if (button) openEncyclopediaCard(button.dataset.encyclopediaCharacter);
  });
  document.querySelectorAll("[data-encyclopedia-zoom]").forEach((button) => button.addEventListener("click", () => setEncyclopediaZoom(button.dataset.encyclopediaZoom)));
  document.querySelectorAll("[data-encyclopedia-mode]").forEach((button) => button.addEventListener("click", () => setEncyclopediaMode(button.dataset.encyclopediaMode)));
  $("encyclopediaBack")?.addEventListener("click", showToolboxHome);
  $("encyclopediaDetailBack")?.addEventListener("click", closeEncyclopediaCard);
  $("encyclopediaCardClose")?.addEventListener("click", closeEncyclopediaCard);
  $("encyclopediaSaveImage")?.addEventListener("click", saveEncyclopediaImage);
  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || !$("encyclopediaStatusModal")?.classList.contains("hidden")) return;
    if (event.key === "Escape" && !$("encyclopediaDetailPage")?.classList.contains("hidden")) closeEncyclopediaCard();
  });
  try {
    await loadEncyclopediaIndex();
    updateEncyclopediaStatusFilterButton();
    renderEncyclopediaGrid();
  } catch (error) {
    console.error("图鉴索引加载失败：", error);
    const empty = $("encyclopediaEmpty");
    if (empty) {
      empty.textContent = "本地图鉴资料加载失败";
      empty.classList.remove("hidden");
    }
  }
}

function showToolboxPage(tool) {
  const pages = {
    "equipment-filter": "equipmentFilterPage",
    "background-limit-test": "backgroundLimitTestPage",
    encyclopedia: "encyclopediaPage",
  };
  const page = $(pages[tool]);
  if (!page) return;
  if (tool !== "background-limit-test") stopBackgroundTestPolling();
  $("toolboxHome")?.classList.add("hidden");
  document.querySelectorAll("#section-toolbox .tool-page").forEach((entry) => entry.classList.add("hidden"));
  page.classList.remove("hidden");
  if (tool === "equipment-filter") renderEquipmentFilterWorkspace();
  if (tool === "background-limit-test") enterBackgroundLimitTestPage();
  if (tool === "encyclopedia") initEncyclopedia();
}

const BACKGROUND_TEST_POLL_MS = 1000;
let backgroundLimitTestTimer = null;
let backgroundLimitTestPollBusy = false;
let backgroundTestRequestSerial = 0;
let backgroundLimitTest = {
  state: "idle",
  startedAtMs: null,
  stoppedAtMs: null,
  capturedAtMs: null,
  appElapsedMs: 0,
  realElapsedMs: 0,
  error: "",
};

function formatBackgroundDuration(value) {
  const ms = Math.max(0, Number(value) || 0);
  const totalTenths = Math.floor(ms / 100);
  const tenths = totalTenths % 10;
  const seconds = Math.floor(totalTenths / 10) % 60;
  const minutes = Math.floor(totalTenths / 600) % 60;
  const hours = Math.floor(totalTenths / 36000);
  return String(hours).padStart(2, "0") + ":" + String(minutes).padStart(2, "0") + ":" + String(seconds).padStart(2, "0") + "." + tenths;
}

function backgroundTestPageOpen() {
  const section = $("section-toolbox");
  return currentSection === "toolbox"
    && !!section?.classList.contains("active")
    && !$("backgroundLimitTestPage")?.classList.contains("hidden");
}

function backgroundTestTimestamp(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function backgroundTestDurations() {
  let real = Math.max(0, Number(backgroundLimitTest.realElapsedMs) || 0);
  const app = Math.max(0, Number(backgroundLimitTest.appElapsedMs) || 0);
  if (backgroundLimitTest.state === "running" && Number.isFinite(Number(backgroundLimitTest.startedAtMs))) {
    real = Math.max(real, Date.now() - Number(backgroundLimitTest.startedAtMs));
  }
  return { app, real };
}

function setBackgroundTestError(message = "") {
  backgroundLimitTest.error = String(message || "");
  const output = $("backgroundTestError");
  if (output) output.textContent = backgroundLimitTest.error;
}

function applyBackgroundTestSnapshot(snapshot) {
  if (!snapshot || typeof snapshot !== "object") throw new Error("后台计时响应格式无效");
  const allowed = new Set(["idle", "running", "completed"]);
  const state = allowed.has(snapshot.state) ? snapshot.state : "idle";
  backgroundLimitTest = {
    ...backgroundLimitTest,
    state,
    startedAtMs: backgroundTestTimestamp(snapshot.startedAtMs),
    stoppedAtMs: backgroundTestTimestamp(snapshot.stoppedAtMs),
    capturedAtMs: backgroundTestTimestamp(snapshot.capturedAtMs),
    appElapsedMs: Math.max(0, Number(snapshot.appElapsedMs) || 0),
    realElapsedMs: Math.max(0, Number(snapshot.realElapsedMs) || 0),
  };
  if (state !== "running") stopBackgroundTestPolling();
  renderBackgroundLimitTest();
}

function renderBackgroundLimitTest() {
  const running = backgroundLimitTest.state === "running";
  const starting = backgroundLimitTest.state === "starting";
  const stopping = backgroundLimitTest.state === "stopping";
  const durations = backgroundTestDurations();
  const status = $("backgroundTestStatus");
  if (!status) return;
  $("backgroundTestActive").textContent = formatBackgroundDuration(durations.app);
  $("backgroundTestReal").textContent = formatBackgroundDuration(durations.real);
  status.className = "background-test-status" + (running ? " running" : (backgroundLimitTest.state === "completed" ? " completed" : (backgroundLimitTest.error ? " error" : "")));
  status.querySelector("span").textContent = starting ? "正在开始" : (stopping ? "正在停止" : (running ? "计时中" : (backgroundLimitTest.state === "completed" ? "已停止" : "未开始")));
  $("backgroundTestStart").disabled = running || starting || stopping;
  $("backgroundTestStart").textContent = backgroundLimitTest.state === "completed" ? "重新开始" : "开始";
  $("backgroundTestStop").disabled = !running;
  const error = $("backgroundTestError");
  if (error) error.textContent = backgroundLimitTest.error || "";
}

function stopBackgroundTestPolling() {
  if (backgroundLimitTestTimer != null) {
    clearInterval(backgroundLimitTestTimer);
    backgroundLimitTestTimer = null;
  }
}

function startBackgroundTestPolling() {
  stopBackgroundTestPolling();
  if (!backgroundTestPageOpen() || document.hidden || backgroundLimitTest.state !== "running") return;
  backgroundLimitTestTimer = setInterval(pollBackgroundTest, BACKGROUND_TEST_POLL_MS);
}

async function pollBackgroundTest() {
  if (backgroundLimitTestPollBusy || backgroundLimitTest.state !== "running" || !backgroundTestPageOpen()) return;
  const requestSerial = backgroundTestRequestSerial;
  backgroundLimitTestPollBusy = true;
  try {
    const snapshot = await api("/api/diagnostics/runtime", { phase: "snapshot" });
    if (requestSerial !== backgroundTestRequestSerial || backgroundLimitTest.state !== "running"
        || !backgroundTestPageOpen()) return;
    setBackgroundTestError("");
    applyBackgroundTestSnapshot(snapshot);
    if (backgroundLimitTest.state === "running") startBackgroundTestPolling();
  } catch (error) {
    if (requestSerial !== backgroundTestRequestSerial || !backgroundTestPageOpen()) return;
    setBackgroundTestError("无法读取后台计时：" + (error.message || "未知错误"));
    renderBackgroundLimitTest();
  } finally {
    backgroundLimitTestPollBusy = false;
  }
}

async function syncBackgroundTest() {
  const requestSerial = ++backgroundTestRequestSerial;
  try {
    const snapshot = await api("/api/diagnostics/runtime", { phase: "snapshot" });
    if (requestSerial !== backgroundTestRequestSerial || !backgroundTestPageOpen()) return;
    setBackgroundTestError("");
    applyBackgroundTestSnapshot(snapshot);
    if (backgroundLimitTest.state === "running") startBackgroundTestPolling();
  } catch (error) {
    if (requestSerial !== backgroundTestRequestSerial || !backgroundTestPageOpen()) return;
    setBackgroundTestError("无法读取后台计时：" + (error.message || "未知错误"));
    renderBackgroundLimitTest();
  }
}

async function startBackgroundLimitTest() {
  if (backgroundLimitTest.state === "running" || backgroundLimitTest.state === "starting" || backgroundLimitTest.state === "stopping") return;
  const requestSerial = ++backgroundTestRequestSerial;
  const previous = { ...backgroundLimitTest };
  stopBackgroundTestPolling();
  backgroundLimitTest = {
    ...previous,
    state: "starting",
    error: "",
  };
  renderBackgroundLimitTest();
  try {
    const snapshot = await api("/api/diagnostics/runtime", { phase: "start" });
    if (requestSerial !== backgroundTestRequestSerial) return;
    setBackgroundTestError("");
    applyBackgroundTestSnapshot(snapshot);
    if (backgroundLimitTest.state === "running") startBackgroundTestPolling();
  } catch (error) {
    if (requestSerial !== backgroundTestRequestSerial) return;
    backgroundLimitTest.state = previous.state;
    setBackgroundTestError("无法开始后台计时：" + (error.message || "未知错误"));
    renderBackgroundLimitTest();
  }
}

async function stopBackgroundLimitTest() {
  if (backgroundLimitTest.state !== "running") return;
  const requestSerial = ++backgroundTestRequestSerial;
  const previous = { ...backgroundLimitTest };
  stopBackgroundTestPolling();
  backgroundLimitTest.state = "stopping";
  setBackgroundTestError("");
  renderBackgroundLimitTest();
  try {
    const snapshot = await api("/api/diagnostics/runtime", { phase: "stop" });
    if (requestSerial !== backgroundTestRequestSerial) return;
    setBackgroundTestError("");
    applyBackgroundTestSnapshot(snapshot);
  } catch (error) {
    if (requestSerial !== backgroundTestRequestSerial) return;
    backgroundLimitTest.state = previous.state;
    setBackgroundTestError("无法停止后台计时：" + (error.message || "未知错误"));
    renderBackgroundLimitTest();
    startBackgroundTestPolling();
  }
}

function handleBackgroundTestVisibilityChange() {
  if (document.hidden) {
    stopBackgroundTestPolling();
    return;
  }
  if (backgroundTestPageOpen()) void syncBackgroundTest();
}

function enterBackgroundLimitTestPage() {
  renderBackgroundLimitTest();
  void syncBackgroundTest();
}

const EQUIP_FILTER_LS = "ark_equipment_filter_schemes_v2";
const EQUIP_FILTER_EXPORT_FORMAT = "ark-equipment-filter-scheme";
const EQUIP_FILTER_EXPORT_VERSION = 2;
const EQUIP_FILTER_SLOTS = Object.freeze([
  { id: "weapon", label: "武器", icon: "weapon.png" },
  { id: "helmet", label: "头盔", icon: "helmet.png" },
  { id: "armor", label: "铠甲", icon: "armor.png" },
  { id: "necklace", label: "项链", icon: "necklace.png" },
  { id: "ring", label: "戒指", icon: "ring.png" },
  { id: "boots", label: "鞋子", icon: "boots.png" },
]);
const EQUIP_FILTER_SETS = Object.freeze([
  { id: "Attack", label: "攻击", icon: "Attack" },
  { id: "Defense", label: "防御", icon: "Defense" },
  { id: "Critical", label: "暴击", icon: "Critical" },
  { id: "Health", label: "生命", icon: "Health" },
  { id: "Speed", label: "速度", icon: "Speed" },
  { id: "Hit", label: "命中", icon: "Hit" },
  { id: "Resist", label: "抗性", icon: "Resist" },
  { id: "Lifesteal", label: "吸血", icon: "Lifesteal" },
  { id: "Counter", label: "反击", icon: "Counter" },
  { id: "Destruction", label: "暴伤", icon: "Destruction" },
  { id: "Immunity", label: "免疫", icon: "Immunity" },
  { id: "Penetration", label: "贯穿", icon: "Penetration" },
  { id: "Revenge", label: "复仇", icon: "Revenge" },
  { id: "Injury", label: "创伤", icon: "Injury" },
  { id: "Pinch", label: "追击", icon: "Pinch" },
  { id: "Rage", label: "暴怒", icon: "Rage" },
  { id: "Torrent", label: "飞流", icon: "Riptide" },
]);
// 装备活动的可选套装不包含飞流；装备过滤器仍保留完整套装列表。
const CUSTOMIZED_EQUIP_SETS = Object.freeze(
  EQUIP_FILTER_SETS.filter((set) => set.id !== "Torrent"),
);
const EQUIP_FILTER_STATS = Object.freeze([
  { id: "AttackValue", label: "攻击力", unit: "", icon: "Attack.png" },
  { id: "AttackRate", label: "攻击力%", unit: "%", icon: "Attack.png" },
  { id: "DefenceValue", label: "防御力", unit: "", icon: "Defense.png" },
  { id: "DefenceRate", label: "防御力%", unit: "%", icon: "Defense.png" },
  { id: "HPValue", label: "生命值", unit: "", icon: "Health.png" },
  { id: "HPRate", label: "生命值%", unit: "%", icon: "Health.png" },
  { id: "SpeedValue", label: "速度", unit: "", icon: "Speed.png" },
  { id: "CriticalRate", label: "暴击率", unit: "%", icon: "Critical.png" },
  { id: "CriticalDamageRate", label: "暴击伤害", unit: "%", icon: "CriticalDamage.png" },
  { id: "EffectHitRate", label: "效果命中", unit: "%", icon: "EffectHit.png" },
  { id: "ResistanceRate", label: "效果抵抗", unit: "%", icon: "Resistance.png" },
]);

// 装备过滤器只处理 85 级传说装备。主属性取 +0 的确定值；副属性取
// 85 级传说装备的一次初始 roll 范围，速度忽略带 * 的极低概率上限 5。
const EQUIP_FILTER_MAIN_STATS_BY_SLOT = Object.freeze({
  weapon: ["AttackValue"],
  helmet: ["HPValue"],
  armor: ["DefenceValue"],
  necklace: ["AttackValue", "AttackRate", "DefenceValue", "DefenceRate", "HPValue", "HPRate", "CriticalRate", "CriticalDamageRate"],
  ring: ["AttackValue", "AttackRate", "DefenceValue", "DefenceRate", "HPValue", "HPRate", "EffectHitRate", "ResistanceRate"],
  boots: ["AttackValue", "AttackRate", "DefenceValue", "DefenceRate", "HPValue", "HPRate", "SpeedValue"],
});
const EQUIP_FILTER_SUB_STATS_BY_SLOT = Object.freeze({
  weapon: ["AttackRate", "HPValue", "HPRate", "SpeedValue", "CriticalRate", "CriticalDamageRate", "EffectHitRate", "ResistanceRate"],
  helmet: ["AttackValue", "AttackRate", "DefenceValue", "DefenceRate", "HPRate", "SpeedValue", "CriticalRate", "CriticalDamageRate", "EffectHitRate", "ResistanceRate"],
  armor: ["DefenceRate", "HPValue", "HPRate", "SpeedValue", "CriticalRate", "CriticalDamageRate", "EffectHitRate", "ResistanceRate"],
  necklace: EQUIP_FILTER_STATS.map((stat) => stat.id),
  ring: EQUIP_FILTER_STATS.map((stat) => stat.id),
  boots: EQUIP_FILTER_STATS.map((stat) => stat.id),
});
const EQUIP_FILTER_MAIN_VALUES = Object.freeze({
  AttackValue: 100,
  AttackRate: 12,
  DefenceValue: 60,
  DefenceRate: 12,
  HPValue: 540,
  HPRate: 12,
  SpeedValue: 8,
  CriticalRate: 11,
  CriticalDamageRate: 13,
  EffectHitRate: 12,
  ResistanceRate: 12,
});
const EQUIP_FILTER_SUB_RANGES = Object.freeze({
  AttackValue: { min: 33, max: 46 },
  AttackRate: { min: 4, max: 8 },
  DefenceValue: { min: 28, max: 35 },
  DefenceRate: { min: 4, max: 8 },
  HPValue: { min: 157, max: 202 },
  HPRate: { min: 4, max: 8 },
  SpeedValue: { min: 2, max: 4 },
  CriticalRate: { min: 3, max: 5 },
  CriticalDamageRate: { min: 4, max: 7 },
  EffectHitRate: { min: 4, max: 8 },
  ResistanceRate: { min: 4, max: 8 },
});
const EQUIP_FILTER_MAX_SUBS = 4;
const EQUIP_FILTER_MAX_SCORE = 55;
const EQUIP_FILTER_REFERENCE_SUB_SCORE = 9;
// 与 backend/tasks.py 的 AUX_FACTOR 保持一致。
const EQUIP_FILTER_AUX_FACTORS = Object.freeze({
  AttackValue: 0.17,
  AttackRate: 1.8,
  DefenceValue: 0.2,
  DefenceRate: 1.8,
  HPValue: 0.05,
  HPRate: 1.5,
  SpeedValue: 3.5,
  CriticalRate: 2.0,
  CriticalDamageRate: 1.8,
  EffectHitRate: 1.25,
  ResistanceRate: 1.25,
});

const EQUIP_FILTER_RULE_COLORS = Object.freeze([
  { hex: "#ff7fac", rgb: "255, 127, 172" },
  { hex: "#42c8d7", rgb: "66, 200, 215" },
  { hex: "#f1b44c", rgb: "241, 180, 76" },
  { hex: "#52cf87", rgb: "82, 207, 135" },
  { hex: "#a685ff", rgb: "166, 133, 255" },
  { hex: "#ff6f61", rgb: "255, 111, 97" },
  { hex: "#5e9cff", rgb: "94, 156, 255" },
  { hex: "#d5dbe8", rgb: "213, 219, 232" },
]);

const emptyEquipmentDetailRule = (slot, color = 0) => ({
  id: `detail-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
  color,
  main: ({ weapon: "AttackValue", helmet: "HPValue", armor: "DefenceValue" })[slot] || "",
  subs: [],
  minScore: 30,
  scoreEnabled: false,
  forceScore: 45,
  forceScoreEnabled: false,
  sets: [],
});

function defaultEquipmentFilterRule(
  id,
  color,
  main,
  subs,
  sets,
  { mixRequired = 0, minScore = 35, forceScore = 47, forceScoreEnabled = true } = {},
) {
  return {
    id: `example-${id}`,
    color,
    main,
    subs: subs.map(([stat, value, mode = "fixed"]) => ({ stat, value: String(value), mode })),
    mixRequired,
    minScore,
    scoreEnabled: true,
    forceScore,
    forceScoreEnabled,
    sets,
  };
}

// First-time users get one useful example instead of several named presets.
const defaultEquipmentFilterSchemes = () => ([
  {
    id: "plan-example-1",
    name: "新方案 1",
    slots: {
      weapon: [
        defaultEquipmentFilterRule(
          "weapon-output",
          0,
          "AttackValue",
          [
            ["SpeedValue", 2],
            ["AttackRate", 4],
            ["CriticalRate", 3],
            ["CriticalDamageRate", 4],
          ],
          ["Destruction", "Torrent", "Penetration"],
          { minScore: 40 },
        ),
      ],
      helmet: [
        defaultEquipmentFilterRule(
          "helmet-output",
          0,
          "HPValue",
          [
            ["SpeedValue", 2],
            ["AttackRate", 4],
            ["CriticalRate", 3],
            ["CriticalDamageRate", 4],
          ],
          ["Destruction", "Torrent", "Penetration"],
          { minScore: 40 },
        ),
        defaultEquipmentFilterRule(
          "helmet-survival",
          1,
          "HPValue",
          [
            ["SpeedValue", 2],
            ["HPRate", 4],
            ["DefenceRate", 4],
            ["ResistanceRate", 4],
          ],
          ["Defense", "Health", "Immunity", "Resist"],
          { minScore: 40 },
        ),
      ],
      armor: [
        defaultEquipmentFilterRule(
          "armor-output",
          0,
          "DefenceValue",
          [
            ["SpeedValue", 2],
            ["CriticalRate", 3],
            ["CriticalDamageRate", 4],
          ],
          ["Destruction", "Penetration", "Torrent"],
          { minScore: 40 },
        ),
      ],
      necklace: [
        defaultEquipmentFilterRule(
          "necklace-output",
          0,
          "CriticalDamageRate",
          [
            ["SpeedValue", 2],
            ["AttackRate", 4],
            ["CriticalRate", 3],
          ],
          ["Destruction", "Penetration", "Torrent", "Critical", "Rage", "Immunity"],
        ),
        defaultEquipmentFilterRule(
          "necklace-hp",
          1,
          "HPRate",
          [
            ["DefenceRate", 4],
            ["SpeedValue", 2],
            ["ResistanceRate", 4],
          ],
          ["Defense", "Health", "Resist", "Injury", "Immunity"],
        ),
        defaultEquipmentFilterRule(
          "necklace-defense",
          2,
          "DefenceRate",
          [
            ["HPRate", 4],
            ["SpeedValue", 2],
            ["ResistanceRate", 4],
          ],
          ["Defense", "Health", "Resist", "Injury", "Immunity"],
        ),
        defaultEquipmentFilterRule(
          "necklace-counter",
          3,
          "HPRate",
          [
            ["DefenceRate", 4],
            ["HPValue", 157, "mixed"],
            ["DefenceValue", 28, "mixed"],
            ["ResistanceRate", 4, "mixed"],
          ],
          ["Counter", "Pinch"],
          { mixRequired: 2 },
        ),
        defaultEquipmentFilterRule(
          "necklace-bruiser",
          4,
          "CriticalDamageRate",
          [
            ["CriticalRate", 3],
            ["HPRate", 4],
            ["DefenceRate", 4],
          ],
          ["Counter", "Lifesteal"],
        ),
        defaultEquipmentFilterRule(
          "necklace-speed",
          5,
          "",
          [["SpeedValue", 4]],
          ["Speed"],
          { minScore: 45, forceScore: 45, forceScoreEnabled: false },
        ),
      ],
      ring: [
        defaultEquipmentFilterRule(
          "ring-output",
          0,
          "AttackRate",
          [
            ["CriticalRate", 3],
            ["CriticalDamageRate", 4],
            ["SpeedValue", 2],
          ],
          ["Destruction", "Penetration", "Torrent", "Critical", "Rage", "Immunity"],
        ),
        defaultEquipmentFilterRule(
          "ring-bruiser",
          1,
          "AttackRate",
          [
            ["CriticalRate", 3],
            ["CriticalDamageRate", 4],
            ["HPRate", 4, "mixed"],
            ["DefenceRate", 4, "mixed"],
          ],
          ["Counter", "Pinch", "Lifesteal"],
          { mixRequired: 1 },
        ),
        defaultEquipmentFilterRule(
          "ring-hp",
          2,
          "HPRate",
          [
            ["SpeedValue", 2],
            ["DefenceRate", 4],
            ["ResistanceRate", 4],
          ],
          ["Defense", "Health", "Immunity", "Injury"],
        ),
        defaultEquipmentFilterRule(
          "ring-resist",
          3,
          "ResistanceRate",
          [
            ["HPRate", 4],
            ["DefenceRate", 4],
            ["SpeedValue", 2],
          ],
          ["Resist"],
        ),
        defaultEquipmentFilterRule(
          "ring-speed",
          4,
          "",
          [["SpeedValue", 4]],
          ["Speed"],
          { minScore: 45, forceScoreEnabled: false },
        ),
      ],
      boots: [
        defaultEquipmentFilterRule(
          "boots-speed-output",
          0,
          "SpeedValue",
          [
            ["AttackRate", 4],
            ["CriticalRate", 3],
            ["CriticalDamageRate", 4],
          ],
          ["Destruction", "Penetration", "Torrent", "Lifesteal", "Rage", "Critical", "Immunity", "Speed"],
        ),
        defaultEquipmentFilterRule(
          "boots-attack-output",
          1,
          "AttackRate",
          [
            ["CriticalRate", 3],
            ["CriticalDamageRate", 4],
          ],
          ["Destruction", "Critical", "Lifesteal", "Counter", "Rage", "Pinch"],
        ),
        defaultEquipmentFilterRule(
          "boots-survival",
          2,
          "SpeedValue",
          [
            ["HPRate", 4],
            ["DefenceRate", 4],
            ["ResistanceRate", 4],
          ],
          ["Defense", "Health", "Resist", "Immunity"],
        ),
      ],
    },
  },
]);

function normalizeEquipmentDetailRule(rule, slot, index) {
  const validSetIds = new Set(EQUIP_FILTER_SETS.map((set) => set.id));
  const mainIds = EQUIP_FILTER_MAIN_STATS_BY_SLOT[slot] || [];
  const requestedMain = String(rule?.main || "");
  const main = mainIds.length === 1 ? mainIds[0] : (mainIds.includes(requestedMain) ? requestedMain : "");
  const validSubIds = new Set((EQUIP_FILTER_SUB_STATS_BY_SLOT[slot] || []).filter((id) => id !== main));
  const usedSubIds = new Set();
  const rawSubs = Array.isArray(rule?.subs) ? rule.subs : [];
  const normalizedSubs = rawSubs.flatMap((sub) => {
    const stat = String(sub?.stat || "");
    if (stat && (!validSubIds.has(stat) || usedSubIds.has(stat))) return [];
    if (stat) usedSubIds.add(stat);
    const range = EQUIP_FILTER_SUB_RANGES[stat];
    return [{
      stat,
      value: stat ? clampFilterValue(sub?.value, range.min, range.max, range.min) : "",
      mode: sub?.mode === "mixed" ? "mixed" : "fixed",
    }];
  }).slice(0, validSubIds.size);
  // Old rules always contained four rows. Drop an entirely blank legacy list,
  // while preserving intentional blank rows alongside configured attributes.
  const subs = normalizedSubs.some((sub) => sub.stat || sub.value) ? normalizedSubs : [];
  const minScore = !rule?.scoreEnabled && Number(rule?.minScore) === 0
    ? 30
    : clampFilterScore(rule?.minScore, 30);
  const forceScore = clampFilterScore(rule?.forceScore, 45);
  return {
    id: String(rule?.id || `detail-${Date.now()}-${index}`),
    color: Number.isFinite(Number(rule?.color)) ? Number(rule.color) % EQUIP_FILTER_RULE_COLORS.length : index % EQUIP_FILTER_RULE_COLORS.length,
    main,
    subs,
    mixRequired: Number.isFinite(Number(rule?.mixRequired)) ? Number(rule.mixRequired) : 0,
    minScore,
    scoreEnabled: rule?.scoreEnabled == null ? minScore > 0 : Boolean(rule.scoreEnabled),
    forceScore,
    forceScoreEnabled: Boolean(rule?.forceScoreEnabled),
    sets: Array.isArray(rule?.sets) ? [...new Set(rule.sets.filter((id) => validSetIds.has(id)))] : [],
  };
}

function normalizeEquipmentFilterScheme(scheme, schemeIndex = 0, preservedId = "") {
  const slots = {};
  EQUIP_FILTER_SLOTS.forEach((slot) => {
    const rules = Array.isArray(scheme?.slots?.[slot.id]) ? scheme.slots[slot.id] : [];
    if (rules.length) slots[slot.id] = rules.map((rule, index) => normalizeEquipmentDetailRule(rule, slot.id, index));
  });
  return {
    id: String(preservedId || scheme?.id || ("plan-" + Date.now() + "-" + schemeIndex)),
    name: String(scheme?.name || "未命名方案").slice(0, 32),
    slots,
  };
}

function loadEquipmentFilterSchemes() {
  try {
    const saved = JSON.parse(frontendStorageGet(EQUIP_FILTER_LS));
    if (Array.isArray(saved) && saved.length) {
      return saved.map((scheme, schemeIndex) => {
        const slots = {};
        EQUIP_FILTER_SLOTS.forEach((slot) => {
          const rules = Array.isArray(scheme?.slots?.[slot.id]) ? scheme.slots[slot.id] : [];
          if (rules.length) slots[slot.id] = rules.map((rule, index) => normalizeEquipmentDetailRule(rule, slot.id, index));
        });
        return {
          id: String(scheme.id || `plan-${Date.now()}-${schemeIndex}`),
          name: String(scheme.name || "未命名方案").slice(0, 32),
          slots,
        };
      });
    }
  } catch { /* 使用内置方案 */ }
  return defaultEquipmentFilterSchemes();
}

let EQUIP_FILTER_SCHEMES_DATA = loadEquipmentFilterSchemes();
let activeFilterSchemeId = EQUIP_FILTER_SCHEMES_DATA[0].id;
let activeFilterSlot = "weapon";
let activeDetailRuleId = EQUIP_FILTER_SCHEMES_DATA[0].slots.weapon?.[0]?.id || null;
let equipmentFilterSchemeOptionSignature = JSON.stringify(
  EQUIP_FILTER_SCHEMES_DATA.map((scheme) => [scheme.id, scheme.name]),
);

function reloadEquipmentFilterSchemesFromSettings() {
  const schemes = loadEquipmentFilterSchemes();
  if (!Array.isArray(schemes) || !schemes.length) return;
  EQUIP_FILTER_SCHEMES_DATA = schemes;
  activeFilterSchemeId = schemes[0].id;
  activeFilterSlot = "weapon";
  activeDetailRuleId = schemes[0].slots.weapon?.[0]?.id || null;
  equipmentFilterSchemeOptionSignature = JSON.stringify(
    schemes.map((scheme) => [scheme.id, scheme.name]),
  );
}

function saveEquipmentFilterSchemes() {
  frontendStorageSet(EQUIP_FILTER_LS, JSON.stringify(EQUIP_FILTER_SCHEMES_DATA));
  syncShopFreeSchemeSnapshot();
  const optionSignature = JSON.stringify(EQUIP_FILTER_SCHEMES_DATA.map((scheme) => [scheme.id, scheme.name]));
  if (optionSignature !== equipmentFilterSchemeOptionSignature) {
    equipmentFilterSchemeOptionSignature = optionSignature;
    refreshFilterSchemeDropdowns();
  }
}

let shopFreeSchemeSyncTimer = null;
function syncShopFreeSchemeSnapshot() {
  const a = activeAcc();
  const cfg = a?.params?.shop_free;
  if (!a || !cfg?.equipmentEnabled) return;
  const schemeId = cfg.equipmentSchemeId || EQUIP_FILTER_SCHEMES_DATA[0]?.id;
  const scheme = EQUIP_FILTER_SCHEMES_DATA.find((item) => item.id === schemeId);
  if (!scheme) return;
  a.params = {
    ...a.params,
    shop_free: { ...cfg, equipmentSchemeId: scheme.id, equipmentScheme: JSON.parse(JSON.stringify(scheme)) },
  };
  if (shopFreeSchemeSyncTimer) clearTimeout(shopFreeSchemeSyncTimer);
  shopFreeSchemeSyncTimer = setTimeout(() => { shopFreeSchemeSyncTimer = null; syncToggles(); }, 350);
}

function filterEscape(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function filterRuleColorStyle(rule) {
  const color = EQUIP_FILTER_RULE_COLORS[rule?.color % EQUIP_FILTER_RULE_COLORS.length] || EQUIP_FILTER_RULE_COLORS[0];
  return `--rule-color:${color.hex};--rule-rgb:${color.rgb}`;
}

function activeFilterScheme() {
  return EQUIP_FILTER_SCHEMES_DATA.find((scheme) => scheme.id === activeFilterSchemeId) || EQUIP_FILTER_SCHEMES_DATA[0];
}

function activeSlotRules() {
  const scheme = activeFilterScheme();
  return scheme?.slots?.[activeFilterSlot] || [];
}

function syncActiveDetailRule() {
  const rules = activeSlotRules();
  if (!rules.some((rule) => rule.id === activeDetailRuleId)) activeDetailRuleId = rules[0]?.id || null;
}

function activeDetailRule() {
  syncActiveDetailRule();
  return activeSlotRules().find((rule) => rule.id === activeDetailRuleId) || null;
}

function configuredFilterRuleCount(scheme) {
  return EQUIP_FILTER_SLOTS.reduce((count, slot) => count + (scheme?.slots?.[slot.id]?.length || 0), 0);
}

function filterStat(id) { return EQUIP_FILTER_STATS.find((stat) => stat.id === id); }

function filterStatsByIds(ids) {
  const allowed = new Set(ids);
  return EQUIP_FILTER_STATS.filter((stat) => allowed.has(stat.id));
}

function filterMainStatsForSlot(slot) {
  return filterStatsByIds(EQUIP_FILTER_MAIN_STATS_BY_SLOT[slot] || []);
}

function filterSubStatIdsForRule(rule, slot = activeFilterSlot) {
  return (EQUIP_FILTER_SUB_STATS_BY_SLOT[slot] || []).filter((id) => id !== rule?.main);
}

function filterSubStatsForRow(rule, rowIndex) {
  const selectedElsewhere = new Set((rule?.subs || [])
    .filter((_, index) => index !== rowIndex)
    .map((sub) => sub.stat)
    .filter(Boolean));
  return filterStatsByIds(filterSubStatIdsForRule(rule).filter((id) => !selectedElsewhere.has(id)));
}

function filterStatRange(statId) {
  return EQUIP_FILTER_SUB_RANGES[statId] || null;
}

function clampFilterValue(value, min, max, fallback = "") {
  if (value === "" || value == null) return String(fallback);
  const number = Number(value);
  if (!Number.isFinite(number)) return String(fallback);
  return String(Math.max(min, Math.min(max, Math.round(number))));
}

function clampFilterScore(value, fallback = 30) {
  if (value === "" || value == null) return fallback;
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.max(0, Math.min(EQUIP_FILTER_MAX_SCORE, Math.round(number)));
}

function fixedSubRules(rule) {
  return (rule?.subs || []).filter((sub) => sub.stat && sub.mode !== "mixed");
}

function mixedSubRules(rule) {
  return (rule?.subs || []).filter((sub) => sub.stat && sub.mode === "mixed");
}

function maxMixedRequired(rule) {
  const mixedCount = mixedSubRules(rule).length;
  const remainingSlots = Math.max(0, EQUIP_FILTER_MAX_SUBS - fixedSubRules(rule).length);
  return Math.max(0, Math.min(mixedCount - 1, remainingSlots));
}

function syncMixedRule(rule) {
  const count = mixedSubRules(rule).length;
  const max = maxMixedRequired(rule);
  if (count < 2 || max < 1) {
    rule.mixRequired = 0;
    return;
  }
  const current = Number(rule.mixRequired);
  rule.mixRequired = Number.isFinite(current) && current >= 1 && current <= max ? Math.round(current) : max;
}

function scoreSubValue(sub) {
  const factor = EQUIP_FILTER_AUX_FACTORS[sub?.stat];
  const value = Number(sub?.value);
  return Number.isFinite(factor) && Number.isFinite(value) ? value * factor : 0;
}

function roundAuxScore(total) {
  return Math.ceil(total - 0.5);
}

function combinationSums(values, count, start = 0, total = 0, output = []) {
  if (count === 0) {
    output.push(total);
    return output;
  }
  for (let index = start; index <= values.length - count; index++) {
    combinationSums(values, count - 1, index + 1, total + values[index], output);
  }
  return output;
}

function mixedRequiredForScore(rule) {
  const mixedCount = mixedSubRules(rule).length;
  const remainingSlots = Math.max(0, EQUIP_FILTER_MAX_SUBS - fixedSubRules(rule).length);
  if (!mixedCount || !remainingSlots) return 0;
  if (mixedCount === 1) return 1;
  syncMixedRule(rule);
  return rule.mixRequired;
}

function subScorePreview(rule) {
  const fixed = fixedSubRules(rule);
  const mixed = mixedSubRules(rule);
  if (!fixed.length && !mixed.length) return null;
  const fixedTotal = fixed.reduce((total, sub) => total + scoreSubValue(sub), 0);
  const required = mixedRequiredForScore(rule);
  const referenceCount = Math.max(0, EQUIP_FILTER_MAX_SUBS - fixed.length - required);
  const referenceTotal = referenceCount * EQUIP_FILTER_REFERENCE_SUB_SCORE;
  const mixedTotals = required ? combinationSums(mixed.map(scoreSubValue), required) : [0];
  const scores = mixedTotals.map((total) => roundAuxScore(fixedTotal + total + referenceTotal));
  return { min: Math.min(...scores), max: Math.max(...scores), referenceCount };
}

function subScorePreviewText(rule) {
  const score = subScorePreview(rule);
  if (!score) return "—";
  return score.min === score.max ? String(score.min) : `${score.min}–${score.max}`;
}

function subScorePreviewNote(rule) {
  const score = subScorePreview(rule);
  if (!score?.referenceCount) return "";
  const count = score.referenceCount;
  return `（含 ${count} 条任意属性，参考叠加 ${count * EQUIP_FILTER_REFERENCE_SUB_SCORE} 分）`;
}

function updateSubScorePreview(rule) {
  const output = $("subScorePreviewValue");
  if (output) output.textContent = subScorePreviewText(rule);
  const note = $("subScorePreviewNote");
  if (note) note.textContent = subScorePreviewNote(rule);
}

function filterNumberText(value) {
  const labels = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十"];
  return labels[value] || String(value);
}

function mixedPoolOptionsHTML(rule) {
  const count = mixedSubRules(rule).length;
  syncMixedRule(rule);
  const max = maxMixedRequired(rule);
  if (count < 2 || max < 1) return "";
  const options = Array.from({ length: max }, (_, index) => max - index);
  return `<div class="mixed-pool-options" role="radiogroup" aria-label="混合属性命中数量">
    ${options.map((required) => `<label class="mix-option">
      <input type="radio" name="mix-required-${filterEscape(rule.id)}" value="${required}" data-mix-required ${rule.mixRequired === required ? "checked" : ""}>
      <span class="radio-box" aria-hidden="true"></span><span>混合池中${filterNumberText(count)}选${filterNumberText(required)}</span>
    </label>`).join("")}
  </div>`;
}

function addSubRuleButtonHTML(rule) {
  const disabled = rule.subs.length >= filterSubStatIdsForRule(rule).length;
  return `<div class="sub-rule-add-row">
    <div class="sub-score-preview" title="按当前最低值计算"><span>副属性分预览</span><strong id="subScorePreviewValue">${subScorePreviewText(rule)}</strong><small id="subScorePreviewNote">${subScorePreviewNote(rule)}</small></div>
    <button type="button" class="sub-rule-add" data-add-sub-rule title="添加副属性" aria-label="添加副属性"${disabled ? " disabled" : ""}>+</button>
  </div>`;
}

function filterStatIconHTML(stat) {
  return stat ? `<img class="dd-stat-icon" src="/assets/stats/${stat.icon}" alt="">` : "";
}

function filterStatValueHTML(statId, value) {
  const stat = filterStat(statId);
  if (!stat || value === "" || value == null) return `<span class="stat-value-number">—</span>`;
  return `<span class="stat-value-number">${filterEscape(value)}</span><span class="stat-value-unit${stat.unit ? "" : " empty"}">${stat.unit || ""}</span>`;
}

function filterStatDropdownHTML(cls, selected, placeholder, extraAttrs = "", availableStats = EQUIP_FILTER_STATS) {
  const stat = filterStat(selected);
  const options = [{ id: "", label: placeholder }, ...availableStats];
  const opts = options.map((item) => `<div class="dd-opt${item.id === selected ? " sel" : ""}" data-val="${filterEscape(item.id)}" role="option" aria-selected="${item.id === selected}">${filterStatIconHTML(item.id ? item : null)}<span>${filterEscape(item.label)}</span></div>`).join("");
  return `<div class="dropdown filter-stat-dropdown ${cls}" data-val="${filterEscape(selected || "")}"${extraAttrs}>
    <button type="button" class="dd-btn" aria-haspopup="listbox" aria-expanded="false">
      <span class="dd-cur">${filterStatIconHTML(stat)}<span class="dd-label">${filterEscape(stat?.label || placeholder)}</span></span>
      <span class="dd-caret">▾</span>
    </button>
    <div class="dd-menu hidden" role="listbox">${opts}</div>
  </div>`;
}

function renderFilterSchemeList() {
  const list = $("schemeList");
  if (!list) return;
  list.innerHTML = EQUIP_FILTER_SCHEMES_DATA.map((scheme) => {
    const count = configuredFilterRuleCount(scheme);
    const active = scheme.id === activeFilterSchemeId;
    return `<div class="scheme-item${active ? " active" : ""}" data-scheme-id="${filterEscape(scheme.id)}">
      <button class="scheme-select" type="button">
        <span class="scheme-mark"></span>
        <span class="scheme-copy"><strong>${filterEscape(scheme.name)}</strong><small>${count} 条细则</small></span>
      </button>
      <div class="scheme-actions">
        <button type="button" data-scheme-action="rename" title="重命名" aria-label="重命名"><svg class="scheme-action-icon" aria-hidden="true"><use href="#ui-square-pen"></use></svg></button>
        <button type="button" data-scheme-action="import" title="导入方案" aria-label="导入方案"><svg class="scheme-action-icon" aria-hidden="true"><use href="#ui-upload"></use></svg></button>
        <button type="button" data-scheme-action="export" title="导出方案文件" aria-label="导出方案文件"><svg class="scheme-action-icon" aria-hidden="true"><use href="#ui-download"></use></svg></button>
        <button type="button" data-scheme-action="delete" title="删除" aria-label="删除"${EQUIP_FILTER_SCHEMES_DATA.length === 1 ? " disabled" : ""}><svg class="scheme-action-icon" aria-hidden="true"><use href="#ui-trash-2"></use></svg></button>
      </div>
    </div>`;
  }).join("");
  $("schemeCount").textContent = `${EQUIP_FILTER_SCHEMES_DATA.length} 个方案`;
  $("activeSchemeName").textContent = activeFilterScheme()?.name || "—";
}

function renderFilterSlots() {
  const scheme = activeFilterScheme();
  const configuredSlots = new Set(EQUIP_FILTER_SLOTS.filter((slot) => scheme?.slots?.[slot.id]?.length).map((slot) => slot.id));
  $("equipSlotTabs").innerHTML = EQUIP_FILTER_SLOTS.map((slot) => {
    const rules = scheme?.slots?.[slot.id] || [];
    const lights = rules.map((rule, index) => `<i style="${filterRuleColorStyle(rule)}" title="细分规则 ${index + 1}"></i>`).join("");
    return `<button class="slot-tab${slot.id === activeFilterSlot ? " active" : ""}${configuredSlots.has(slot.id) ? " configured" : ""}"
      type="button" data-slot="${slot.id}"><span class="slot-glyph" style="--slot-icon:url('/assets/slots/${slot.icon}')" aria-hidden="true"></span><b>${slot.label}</b><span class="slot-rule-lights">${lights}</span></button>`;
  }).join("");
}

function renderDetailRuleTabs() {
  const rules = activeSlotRules();
  syncActiveDetailRule();
  $("detailRuleCircles").innerHTML = rules.map((rule, index) =>
    `<button class="detail-rule-circle${rule.id === activeDetailRuleId ? " active" : ""}" type="button"
      data-detail-rule="${filterEscape(rule.id)}" style="${filterRuleColorStyle(rule)}" title="细分规则 ${index + 1}">${index + 1}</button>`
  ).join("");
  const index = rules.findIndex((rule) => rule.id === activeDetailRuleId);
  $("detailRuleDuplicate").classList.toggle("hidden", index < 0);
  $("detailRuleDelete").classList.toggle("hidden", index < 0);
}

function updateApplySetButton(rule) {
  const sets = rule?.sets || [];
  $("applySetButton").classList.toggle("has-selection", sets.length > 0);
  $("applySelectedSets").innerHTML = sets.map((setId) => {
    const set = EQUIP_FILTER_SETS.find((item) => item.id === setId);
    return set ? `<img src="/assets/sets/${set.icon}.png" alt="${set.label}" title="${set.label}">` : "";
  }).join("");
}

function renderApplySetGrid(rule) {
  $("applySetGrid").innerHTML = EQUIP_FILTER_SETS.map((set) => {
    const selected = rule?.sets?.includes(set.id);
    return `<button class="apply-set-option${selected ? " selected" : ""}" type="button" data-apply-set="${set.id}">
      <img src="/assets/sets/${set.icon}.png" alt="${set.label}" title="${set.label}"></button>`;
  }).join("");
}

function renderDetailRuleEditor() {
  const rule = activeDetailRule();
  $("detailRuleApplyRow").classList.toggle("hidden", !rule);
  $("detailRuleEmpty").classList.toggle("hidden", !!rule);
  $("detailRuleEditor").classList.toggle("hidden", !rule);
  setApplyPopoverOpen(false);
  if (!rule) return;
  syncMixedRule(rule);
  const targetScore = $("targetSubScore");
  const targetScoreEnabled = $("targetScoreEnabled");
  const targetForceScore = $("targetForceSubScore");
  const targetForceScoreEnabled = $("targetForceScoreEnabled");
  targetScore.value = clampFilterScore(rule.minScore);
  targetScore.disabled = !rule.scoreEnabled;
  targetScoreEnabled.checked = Boolean(rule.scoreEnabled);
  targetScore.closest(".target-score-row").classList.toggle("enabled", Boolean(rule.scoreEnabled));
  targetForceScore.value = clampFilterScore(rule.forceScore, 45);
  targetForceScore.disabled = !rule.forceScoreEnabled;
  targetForceScoreEnabled.checked = Boolean(rule.forceScoreEnabled);
  targetForceScore.closest(".target-score-row").classList.toggle("enabled", Boolean(rule.forceScoreEnabled));
  const mainStats = filterMainStatsForSlot(activeFilterSlot);
  const mainStat = filterStat(rule.main);
  const fixedMain = mainStats.length === 1;
  const mainControl = $("mainStatControl");
  mainControl.classList.toggle("is-fixed", fixedMain);
  mainControl.title = fixedMain ? "85级 +0 固定主属性" : "";
  mainControl.innerHTML = fixedMain
    ? `<div class="main-stat-fixed">${filterStatIconHTML(mainStat)}<span>${filterEscape(mainStat?.label || "—")}</span></div>`
    : filterStatDropdownHTML("main-stat-dropdown", rule.main, "选择主属性", " id=\"mainStatSelect\"", mainStats);
  const mainValue = $("mainStatValue");
  mainValue.innerHTML = filterStatValueHTML(rule.main, EQUIP_FILTER_MAIN_VALUES[rule.main]);
  mainValue.classList.toggle("empty", !mainStat);
  mainValue.title = mainStat ? "85级 +0 主属性值" : "";
  $("subRuleList").innerHTML = rule.subs.map((sub, index) => {
    const stat = filterStat(sub.stat);
    const range = filterStatRange(sub.stat) || { min: 0, max: 0 };
    const value = stat ? clampFilterValue(sub.value, range.min, range.max, range.min) : "";
    return `<div class="sub-rule-row" data-index="${index}">
      <div class="stat-control">
        ${filterStatDropdownHTML("sub-stat-dropdown", sub.stat, "选择副属性", " data-sub-stat", filterSubStatsForRow(rule, index))}
      </div>
      <div class="range-control">
        <input class="sub-value-range" type="range" min="${range.min}" max="${range.max}" step="1" value="${value || range.min}" data-sub-range aria-label="${filterEscape(stat?.label || "副属性")}最低值"${stat ? "" : " disabled"}>
        <label class="range-number"><span class="range-number-content"><input class="sub-value-number" type="number" min="${range.min}" max="${range.max}" step="1" value="${filterEscape(value)}" data-sub-value aria-label="${filterEscape(stat?.label || "副属性")}最低值"${stat ? "" : " disabled"}><b>${stat?.unit || ""}</b></span></label>
      </div>
      <div class="sub-mode" role="group" aria-label="属性匹配方式">
        <button type="button" class="mode-option${sub.mode !== "mixed" ? " active" : ""}" data-sub-mode="fixed">固定</button>
        <button type="button" class="mode-option${sub.mode === "mixed" ? " active" : ""}" data-sub-mode="mixed">混合</button>
      </div>
      <button type="button" class="sub-rule-remove" data-remove-sub-rule title="删除这条副属性" aria-label="删除这条副属性">−</button>
    </div>`;
  }).join("") + addSubRuleButtonHTML(rule) + mixedPoolOptionsHTML(rule);
  if (!fixedMain) {
    bindDropdown($("mainStatSelect"), (value) => {
      const current = activeDetailRule();
      if (!current) return;
      current.main = value;
      const allowed = new Set(filterSubStatIdsForRule(current));
      current.subs = current.subs.filter((sub) => !sub.stat || allowed.has(sub.stat));
      syncMixedRule(current);
      markEquipmentFilterChanged();
    });
  }
  $("subRuleList").querySelectorAll("[data-sub-stat]").forEach((root) => {
    bindDropdown(root, (value) => {
      const index = Number(root.closest(".sub-rule-row")?.dataset.index);
      const current = activeDetailRule();
      if (!current || !current.subs[index]) return;
      current.subs[index].stat = value;
      const range = filterStatRange(value);
      current.subs[index].value = range ? String(range.min) : "";
      markEquipmentFilterChanged();
    });
  });
  updateApplySetButton(rule);
  renderApplySetGrid(rule);
}

function renderFilterCoverage() {
  const rules = activeSlotRules();
  let covered = 0;
  $("coverageGrid").innerHTML = EQUIP_FILTER_SETS.map((set) => {
    const matches = rules.map((rule, index) => ({ rule, index })).filter(({ rule }) => rule.sets.includes(set.id));
    if (matches.length) covered++;
    const lights = matches.map(({ rule, index }) =>
      `<i style="${filterRuleColorStyle(rule)}" title="细分规则 ${index + 1}"></i>`
    ).join("");
    return `<div class="coverage-item${matches.length ? " covered" : ""}" data-coverage-set="${set.id}">
      <img src="/assets/sets/${set.icon}.png" alt="${set.label}" title="${set.label}"><div class="coverage-lights">${lights}</div></div>`;
  }).join("");
  $("coverageCount").textContent = `${covered} / ${EQUIP_FILTER_SETS.length}`;
}

function renderEquipmentFilterWorkspace() {
  syncActiveDetailRule();
  renderFilterSchemeList();
  renderFilterSlots();
  renderDetailRuleTabs();
  renderDetailRuleEditor();
  renderFilterCoverage();
}

function markEquipmentFilterChanged() {
  saveEquipmentFilterSchemes();
  renderEquipmentFilterWorkspace();
}

function addEquipmentFilterScheme() {
  const number = EQUIP_FILTER_SCHEMES_DATA.length + 1;
  const scheme = { id: `plan-${Date.now()}`, name: `新方案 ${number}`, slots: {} };
  EQUIP_FILTER_SCHEMES_DATA.push(scheme);
  activeFilterSchemeId = scheme.id;
  activeDetailRuleId = null;
  saveEquipmentFilterSchemes();
  renderEquipmentFilterWorkspace();
  openEquipmentFilterRename(scheme.id);
}

let schemeRenameTargetId = null;
let schemeRenameReturnFocus = null;

function setSchemeRenameError(message = "") {
  const output = $("schemeRenameError");
  if (output) output.textContent = message;
}

function openEquipmentFilterRename(schemeId, returnFocus) {
  const scheme = EQUIP_FILTER_SCHEMES_DATA.find((item) => item.id === schemeId);
  const modal = $("schemeRenameModal");
  const input = $("schemeRenameInput");
  if (!scheme || !modal || !input) return;
  schemeRenameTargetId = schemeId;
  schemeRenameReturnFocus = returnFocus || null;
  input.value = scheme.name;
  setSchemeRenameError();
  modal.classList.remove("hidden");
  document.body.classList.add("scheme-rename-open");
  input.focus();
  try { input.setSelectionRange(input.value.length, input.value.length); } catch { /* unsupported input selection */ }
}

function closeEquipmentFilterRename() {
  const modal = $("schemeRenameModal");
  if (!modal || modal.classList.contains("hidden")) return;
  modal.classList.add("hidden");
  document.body.classList.remove("scheme-rename-open");
  schemeRenameTargetId = null;
  const returnFocus = schemeRenameReturnFocus;
  schemeRenameReturnFocus = null;
  returnFocus?.focus?.();
}

function saveEquipmentFilterRename() {
  const scheme = EQUIP_FILTER_SCHEMES_DATA.find((item) => item.id === schemeRenameTargetId);
  const input = $("schemeRenameInput");
  if (!scheme || !input) return;
  const value = input.value.trim();
  if (!value) {
    setSchemeRenameError("方案名称不能为空");
    input.focus();
    return;
  }
  scheme.name = value;
  saveEquipmentFilterSchemes();
  closeEquipmentFilterRename();
  renderEquipmentFilterWorkspace();
}

function equipmentFilterExportPayload(scheme) {
  return {
    format: EQUIP_FILTER_EXPORT_FORMAT,
    version: EQUIP_FILTER_EXPORT_VERSION,
    scheme,
  };
}

function showSchemeActionFeedback(button, ok, label) {
  if (!button) return;
  clearTimeout(button._feedbackTimer);
  const icon = button.querySelector(".scheme-action-icon");
  const use = icon?.querySelector("use");
  const originalIconHref = button.dataset.originalIconHref || use?.getAttribute("href") || "";
  button.dataset.originalIconHref = originalIconHref;
  button.dataset.originalTitle ||= button.title;
  button.classList.remove("action-success", "action-error");
  void button.offsetWidth;
  button.classList.add(ok ? "action-success" : "action-error");
  button.title = label;
  use?.setAttribute("href", ok ? "#ui-check" : "#ui-triangle-alert");
  button._feedbackTimer = setTimeout(() => {
    if (!button.isConnected) return;
    button.classList.remove("action-success", "action-error");
    button.title = button.dataset.originalTitle;
    if (originalIconHref) use?.setAttribute("href", originalIconHref);
  }, 1300);
}

async function exportEquipmentFilterScheme(schemeId, button) {
  const scheme = EQUIP_FILTER_SCHEMES_DATA.find((item) => item.id === schemeId);
  if (!scheme) return;
  try {
    const exportData = equipmentFilterExportPayload(scheme);
    const contents = JSON.stringify(exportData, null, 2) + "\n";
    let result;
    if (runtimePlatform === "android" && window.LucimaFiles?.saveEquipmentFilterScheme) {
      const filename = `LucimaTools-equipment-filter-${scheme.name}.json`;
      const raw = window.LucimaFiles.saveEquipmentFilterScheme(filename, contents);
      result = typeof raw === "string" ? JSON.parse(raw) : raw;
      if (!result?.ok) throw new Error(result?.detail || "导出装备筛选方案失败");
    } else {
      result = await api("/api/equipment-filter/export", exportData);
    }
    showSchemeActionFeedback(button, true, "导出成功");
    showMessageToast(`方案“${scheme.name}”已导出至 ${result.path}`, "ok", 5200);
  } catch (error) {
    showSchemeActionFeedback(button, false, "导出失败");
    showMessageToast(error.message || "导出装备筛选方案失败", "err", 5200);
  }
}

let schemeImportTargetId = null;
let schemeImportReturnFocus = null;
let confirmDialogResolve = null;
let confirmDialogReturnFocus = null;

function openConfirmDialog({ title, message, confirmLabel = "确认删除", icon = "ui-trash-2", tone = "danger", returnFocus = document.activeElement }) {
  const modal = $("confirmModal");
  if (!modal) return Promise.resolve(false);
  if (confirmDialogResolve) closeConfirmDialog(false);
  $("confirmTitle").textContent = title;
  $("confirmMessage").textContent = message;
  $("confirmAction").textContent = confirmLabel;
  modal.dataset.tone = tone === "warning" ? "warning" : "danger";
  modal.querySelector('.confirm-icon use').setAttribute('href', `#${icon}`);
  confirmDialogReturnFocus = returnFocus || null;
  modal.classList.remove("hidden");
  document.body.classList.add("confirm-dialog-open");
  return new Promise((resolve) => {
    confirmDialogResolve = resolve;
    requestAnimationFrame(() => $("confirmAction")?.focus());
  });
}

function closeConfirmDialog(confirmed = false) {
  const modal = $("confirmModal");
  if (!modal || modal.classList.contains("hidden")) return;
  modal.classList.add("hidden");
  document.body.classList.remove("confirm-dialog-open");
  const resolve = confirmDialogResolve;
  const returnFocus = confirmDialogReturnFocus;
  confirmDialogResolve = null;
  confirmDialogReturnFocus = null;
  returnFocus?.focus?.();
  resolve?.(confirmed);
}

let desktopCloseBusy = false;
let desktopCloseReturnFocus = null;
let desktopCloseMinimizeToTray = false;

function setDesktopCloseError(message = "") {
  const error = $("desktopCloseError");
  if (error) error.textContent = message;
}

function showDesktopCloseDialog() {
  const modal = $("desktopCloseModal");
  const tray = $("desktopCloseTray");
  const confirm = $("desktopCloseConfirm");
  const cancel = $("desktopCloseCancel");
  if (!modal) return false;
  desktopCloseBusy = false;
  desktopCloseReturnFocus = document.activeElement;
  if (tray) tray.checked = desktopCloseMinimizeToTray;
  if (confirm) confirm.disabled = false;
  if (cancel) cancel.disabled = false;
  setDesktopCloseError();
  modal.classList.remove("hidden");
  document.body.classList.add("desktop-close-open");
  requestAnimationFrame(() => cancel?.focus());
  return true;
}

window.showDesktopCloseDialog = showDesktopCloseDialog;

function hideDesktopCloseDialog() {
  const modal = $("desktopCloseModal");
  if (!modal) return;
  modal.classList.add("hidden");
  document.body.classList.remove("desktop-close-open");
  const returnFocus = desktopCloseReturnFocus;
  desktopCloseReturnFocus = null;
  returnFocus?.focus?.();
}

async function cancelDesktopClose() {
  if (desktopCloseBusy) return;
  const bridge = window.pywebview?.api;
  try {
    await bridge?.cancelClose?.();
  } catch { /* 关闭请求已被原生窗口拦截，页面只需恢复可操作状态 */ }
  hideDesktopCloseDialog();
}

async function confirmDesktopClose() {
  if (desktopCloseBusy) return;
  const modal = $("desktopCloseModal");
  const tray = $("desktopCloseTray");
  const confirm = $("desktopCloseConfirm");
  const cancel = $("desktopCloseCancel");
  const bridge = window.pywebview?.api;
  if (!modal || !bridge?.confirmClose) {
    setDesktopCloseError("桌面窗口桥接尚未就绪，请稍后重试。");
    return;
  }
  desktopCloseBusy = true;
  setDesktopCloseError();
  if (confirm) confirm.disabled = true;
  if (cancel) cancel.disabled = true;
  const minimizeToTray = !!tray?.checked;
  desktopCloseMinimizeToTray = minimizeToTray;
  try {
    const result = await bridge.confirmClose(minimizeToTray);
    if (!result?.ok) throw new Error(result?.detail || "无法完成关闭操作");
    hideDesktopCloseDialog();
  } catch (error) {
    desktopCloseBusy = false;
    if (confirm) confirm.disabled = false;
    if (cancel) cancel.disabled = false;
    setDesktopCloseError(error.message || "无法完成关闭操作");
  }
}

async function openEquipmentFilterImport(schemeId, returnFocus) {
  const scheme = EQUIP_FILTER_SCHEMES_DATA.find((item) => item.id === schemeId);
  if (!scheme) return;
  schemeImportTargetId = schemeId;
  schemeImportReturnFocus = returnFocus || null;
  try {
    if (runtimePlatform === "android" && window.LucimaFiles?.chooseEquipmentFilterFile) {
      window.LucimaFiles.chooseEquipmentFilterFile();
    } else {
      const input = $("schemeImportFile");
      if (!input) throw new Error("系统文件选择器不可用");
      input.value = "";
      input.click();
    }
  } catch (error) {
    finishEquipmentFilterImport();
    showMessageToast(error.message || "无法打开方案文件选择器", "err", 5200);
  }
}

function finishEquipmentFilterImport() {
  schemeImportTargetId = null;
  const returnFocus = schemeImportReturnFocus;
  schemeImportReturnFocus = null;
  returnFocus?.focus?.();
}

function parseEquipmentFilterImport(text) {
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new Error("无法解析 JSON，请确认内容完整且格式正确");
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("导入内容不是有效的筛选方案");
  }
  if (payload.format && payload.format !== EQUIP_FILTER_EXPORT_FORMAT) {
    throw new Error("这不是装备过滤器的方案文件");
  }
  const version = Number(payload.version == null ? 1 : payload.version);
  if (!Number.isInteger(version) || version < 1) {
    throw new Error("方案版本无效");
  }
  if (version > EQUIP_FILTER_EXPORT_VERSION) {
    throw new Error("方案版本高于当前支持版本，请先更新工具");
  }
  const scheme = payload.scheme || payload;
  if (!scheme || typeof scheme !== "object" || Array.isArray(scheme)) {
    throw new Error("方案数据缺失");
  }
  if (typeof scheme.name !== "string" || !scheme.name.trim()) {
    throw new Error("方案名称缺失");
  }
  if (!scheme.slots || typeof scheme.slots !== "object" || Array.isArray(scheme.slots)) {
    throw new Error("方案的部位规则缺失");
  }
  return scheme;
}

function importEquipmentFilterScheme(text, filename = "方案文件") {
  const index = EQUIP_FILTER_SCHEMES_DATA.findIndex((item) => item.id === schemeImportTargetId);
  if (index < 0) throw new Error("导入目标方案已失效，请重新选择导入");
  if (!text?.trim()) throw new Error("方案文件为空");
  if (new TextEncoder().encode(text).length > 1024 * 1024) throw new Error("方案文件超过 1 MB，无法导入");
  const source = parseEquipmentFilterImport(text);
  const targetId = EQUIP_FILTER_SCHEMES_DATA[index].id;
  EQUIP_FILTER_SCHEMES_DATA[index] = normalizeEquipmentFilterScheme(source, index, targetId);
  activeFilterSchemeId = targetId;
  activeDetailRuleId = null;
  saveEquipmentFilterSchemes();
  renderEquipmentFilterWorkspace();
  const importedItem = [...document.querySelectorAll(".scheme-item")].find((item) => item.dataset.schemeId === targetId);
  importedItem?.classList.add("scheme-imported");
  setTimeout(() => importedItem?.classList.remove("scheme-imported"), 900);
  showMessageToast(`已从 ${filename} 导入方案`, "ok");
}

function handleEquipmentFilterImport(filename, text) {
  try {
    importEquipmentFilterScheme(text, filename || "方案文件");
  } catch (error) {
    showMessageToast(error.message || "导入失败，请检查方案文件", "err", 5200);
  } finally {
    finishEquipmentFilterImport();
  }
}

window.onLucimaEquipmentFilterFileSelected = (filename, contents) => {
  handleEquipmentFilterImport(filename, contents);
};

window.onLucimaEquipmentFilterFileError = (message) => {
  showMessageToast(message || "读取方案文件失败", "err", 5200);
  finishEquipmentFilterImport();
};

function addEquipmentDetailRule() {
  const scheme = activeFilterScheme();
  if (!scheme) return;
  if (!scheme.slots[activeFilterSlot]) scheme.slots[activeFilterSlot] = [];
  const rules = scheme.slots[activeFilterSlot];
  const used = new Set(rules.map((rule) => rule.color));
  let color = EQUIP_FILTER_RULE_COLORS.findIndex((_, index) => !used.has(index));
  if (color < 0) color = rules.length % EQUIP_FILTER_RULE_COLORS.length;
  const rule = emptyEquipmentDetailRule(activeFilterSlot, color);
  rules.push(rule);
  activeDetailRuleId = rule.id;
  markEquipmentFilterChanged();
}

function duplicateActiveEquipmentDetailRule() {
  const rules = activeSlotRules();
  const sourceIndex = rules.findIndex((rule) => rule.id === activeDetailRuleId);
  if (sourceIndex < 0) return;
  const source = rules[sourceIndex];
  const usedColors = new Set(rules.map((rule) => rule.color));
  let color = EQUIP_FILTER_RULE_COLORS.findIndex((_, index) => !usedColors.has(index));
  if (color < 0) color = (Number(source.color) + 1) % EQUIP_FILTER_RULE_COLORS.length;
  const duplicate = {
    ...source,
    id: "detail-" + Date.now() + "-" + Math.random().toString(36).slice(2, 7),
    color,
    subs: (source.subs || []).map((sub) => ({ ...sub })),
    sets: [...(source.sets || [])],
  };
  rules.splice(sourceIndex + 1, 0, duplicate);
  activeDetailRuleId = duplicate.id;
  markEquipmentFilterChanged();
}

function addEquipmentSubRule() {
  const rule = activeDetailRule();
  if (!rule) return;
  if (rule.subs.length >= filterSubStatIdsForRule(rule).length) return;
  rule.subs.push({ stat: "", value: "", mode: "fixed" });
  markEquipmentFilterChanged();
}

function removeEquipmentSubRule(index) {
  const rule = activeDetailRule();
  if (!rule || !rule.subs[index]) return;
  rule.subs.splice(index, 1);
  syncMixedRule(rule);
  markEquipmentFilterChanged();
}

async function deleteActiveEquipmentDetailRule() {
  const rules = activeSlotRules();
  const index = rules.findIndex((rule) => rule.id === activeDetailRuleId);
  if (index < 0) return;
  const confirmed = await openConfirmDialog({
    title: `删除细分规则 ${index + 1}？`,
    message: "该细则的属性与套装配置将一并删除，此操作无法撤销。",
  });
  if (!confirmed) return;
  rules.splice(index, 1);
  activeDetailRuleId = rules[Math.min(index, rules.length - 1)]?.id || null;
  markEquipmentFilterChanged();
}

function setApplyPopoverOpen(open) {
  const popover = $("applySetPopover");
  const button = $("applySetButton");
  if (!popover || !button) return;
  popover.classList.toggle("hidden", !open);
  button.setAttribute("aria-expanded", String(open));
}

function initToolbox() {
  const home = $("toolboxHome");
  const page = $("equipmentFilterPage");
  if (!home || !page || home.dataset.bound) return;
  home.dataset.bound = "1";
  home.addEventListener("click", (event) => {
    const tile = event.target.closest("[data-tool]");
    if (tile) showToolboxPage(tile.dataset.tool);
  });
  $("toolboxBack")?.addEventListener("click", showToolboxHome);
  $("backgroundTestBack")?.addEventListener("click", showToolboxHome);
  $("backgroundTestStart")?.addEventListener("click", startBackgroundLimitTest);
  $("backgroundTestStop")?.addEventListener("click", stopBackgroundLimitTest);
  document.addEventListener("visibilitychange", handleBackgroundTestVisibilityChange);
  $("schemeAdd")?.addEventListener("click", addEquipmentFilterScheme);
  $("schemeList")?.addEventListener("click", async (event) => {
    const action = event.target.closest("[data-scheme-action]");
    const item = event.target.closest(".scheme-item");
    if (!item) return;
    const schemeId = item.dataset.schemeId;
    if (action?.dataset.schemeAction === "rename") {
      openEquipmentFilterRename(schemeId, action);
      return;
    }
    if (action?.dataset.schemeAction === "import") {
      await openEquipmentFilterImport(schemeId, action);
      return;
    }
    if (action?.dataset.schemeAction === "export") {
      await exportEquipmentFilterScheme(schemeId, action);
      return;
    }
    if (action?.dataset.schemeAction === "delete") {
      const scheme = EQUIP_FILTER_SCHEMES_DATA.find((entry) => entry.id === schemeId);
      const confirmed = scheme && await openConfirmDialog({
        title: `删除“${scheme.name}”？`,
        message: "方案中的全部部位与筛选细则都会被删除，此操作无法撤销。",
        returnFocus: action,
      });
      if (confirmed) {
        EQUIP_FILTER_SCHEMES_DATA = EQUIP_FILTER_SCHEMES_DATA.filter((entry) => entry.id !== schemeId);
        if (activeFilterSchemeId === schemeId) activeFilterSchemeId = EQUIP_FILTER_SCHEMES_DATA[0].id;
        activeDetailRuleId = null;
        markEquipmentFilterChanged();
      }
      return;
    }
    activeFilterSchemeId = schemeId;
    activeDetailRuleId = null;
    renderEquipmentFilterWorkspace();
  });
  $("equipSlotTabs")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-slot]");
    if (!button) return;
    activeFilterSlot = button.dataset.slot;
    activeDetailRuleId = null;
    renderEquipmentFilterWorkspace();
  });
  $("detailRuleCircles")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-detail-rule]");
    if (!button) return;
    activeDetailRuleId = button.dataset.detailRule;
    renderEquipmentFilterWorkspace();
  });
  $("detailRuleAdd")?.addEventListener("click", addEquipmentDetailRule);
  $("detailRuleDuplicate")?.addEventListener("click", duplicateActiveEquipmentDetailRule);
  $("detailRuleEmptyAdd")?.addEventListener("click", addEquipmentDetailRule);
  $("detailRuleDelete")?.addEventListener("click", deleteActiveEquipmentDetailRule);
  $("subRuleList")?.addEventListener("click", (event) => {
    if (event.target.closest("[data-add-sub-rule]")) {
      addEquipmentSubRule();
      return;
    }
    const removeButton = event.target.closest("[data-remove-sub-rule]");
    if (removeButton) {
      const index = Number(removeButton.closest(".sub-rule-row")?.dataset.index);
      removeEquipmentSubRule(index);
      return;
    }
    const modeButton = event.target.closest("[data-sub-mode]");
    if (!modeButton) return;
    const index = Number(modeButton.closest(".sub-rule-row")?.dataset.index);
    const rule = activeDetailRule();
    if (!rule || !rule.subs[index]) return;
    rule.subs[index].mode = modeButton.dataset.subMode === "mixed" ? "mixed" : "fixed";
    syncMixedRule(rule);
    markEquipmentFilterChanged();
  });
  $("subRuleList")?.addEventListener("change", (event) => {
    const valueInput = event.target.closest("[data-sub-value], [data-sub-range]");
    if (valueInput) {
      const row = valueInput.closest(".sub-rule-row");
      const index = Number(row?.dataset.index);
      const rule = activeDetailRule();
      const rangeConfig = filterStatRange(rule?.subs?.[index]?.stat);
      if (!rule || !rule.subs[index] || !rangeConfig) return;
      const value = clampFilterValue(valueInput.value, rangeConfig.min, rangeConfig.max, rangeConfig.min);
      rule.subs[index].value = value;
      row.querySelector("[data-sub-range]").value = value;
      row.querySelector("[data-sub-value]").value = value;
      updateSubScorePreview(rule);
      saveEquipmentFilterSchemes();
      return;
    }
    const mixInput = event.target.closest("[data-mix-required]");
    if (!mixInput) return;
    const rule = activeDetailRule();
    if (!rule) return;
    rule.mixRequired = Number(mixInput.value);
    updateSubScorePreview(rule);
    saveEquipmentFilterSchemes();
  });
  $("subRuleList")?.addEventListener("input", (event) => {
    const input = event.target.closest("[data-sub-value], [data-sub-range]");
    if (!input) return;
    const row = input.closest(".sub-rule-row");
    const index = Number(row?.dataset.index);
    const rule = activeDetailRule();
    if (!rule || !rule.subs[index]) return;
    const rangeConfig = filterStatRange(rule.subs[index].stat);
    if (!rangeConfig) return;
    const value = clampFilterValue(input.value, rangeConfig.min, rangeConfig.max);
    rule.subs[index].value = value;
    const range = row.querySelector("[data-sub-range]");
    const number = row.querySelector("[data-sub-value]");
    if (input !== range) range.value = value || rangeConfig.min;
    if (input !== number) number.value = value;
    updateSubScorePreview(rule);
    saveEquipmentFilterSchemes();
  });
  $("targetSubScore")?.addEventListener("input", (event) => {
    const rule = activeDetailRule();
    if (!rule) return;
    rule.minScore = clampFilterScore(event.target.value);
    saveEquipmentFilterSchemes();
  });
  $("targetSubScore")?.addEventListener("change", (event) => {
    const rule = activeDetailRule();
    if (!rule) return;
    rule.minScore = clampFilterScore(event.target.value);
    event.target.value = rule.minScore;
    saveEquipmentFilterSchemes();
  });
  $("targetScoreEnabled")?.addEventListener("change", (event) => {
    const rule = activeDetailRule();
    if (!rule) return;
    rule.scoreEnabled = event.target.checked;
    $("targetSubScore").disabled = !rule.scoreEnabled;
    event.target.closest(".target-score-row").classList.toggle("enabled", rule.scoreEnabled);
    saveEquipmentFilterSchemes();
  });
  $("targetForceSubScore")?.addEventListener("input", (event) => {
    const rule = activeDetailRule();
    if (!rule) return;
    rule.forceScore = clampFilterScore(event.target.value, 45);
    saveEquipmentFilterSchemes();
  });
  $("targetForceSubScore")?.addEventListener("change", (event) => {
    const rule = activeDetailRule();
    if (!rule) return;
    rule.forceScore = clampFilterScore(event.target.value, 45);
    event.target.value = rule.forceScore;
    saveEquipmentFilterSchemes();
  });
  $("targetForceScoreEnabled")?.addEventListener("change", (event) => {
    const rule = activeDetailRule();
    if (!rule) return;
    rule.forceScoreEnabled = event.target.checked;
    $("targetForceSubScore").disabled = !rule.forceScoreEnabled;
    event.target.closest(".target-score-row").classList.toggle("enabled", rule.forceScoreEnabled);
    saveEquipmentFilterSchemes();
  });
  $("targetScoreApplyAll")?.addEventListener("click", (event) => {
    const source = activeDetailRule();
    if (!source) return;
    EQUIP_FILTER_SCHEMES_DATA.forEach((scheme) => {
      (scheme?.slots?.[activeFilterSlot] || []).forEach((rule) => {
        rule.minScore = source.minScore;
        rule.scoreEnabled = source.scoreEnabled;
        rule.forceScore = source.forceScore;
        rule.forceScoreEnabled = source.forceScoreEnabled;
      });
    });
    saveEquipmentFilterSchemes();
    const button = event.currentTarget;
    clearTimeout(button._feedbackTimer);
    button.classList.remove("applied");
    void button.offsetWidth;
    button.classList.add("applied");
    button.textContent = "✓";
    button._feedbackTimer = setTimeout(() => {
      if (!button.isConnected) return;
      button.classList.remove("applied");
      button.textContent = "一键复制";
    }, 1200);
  });
  $("applySetButton")?.addEventListener("click", () => {
    setApplyPopoverOpen($("applySetPopover").classList.contains("hidden"));
  });
  $("applySetGrid")?.addEventListener("click", (event) => {
    const option = event.target.closest("[data-apply-set]");
    const rule = activeDetailRule();
    if (!option || !rule) return;
    event.stopPropagation();
    const setId = option.dataset.applySet;
    const index = rule.sets.indexOf(setId);
    if (index >= 0) rule.sets.splice(index, 1);
    else rule.sets.push(setId);
    saveEquipmentFilterSchemes();
    renderApplySetGrid(rule);
    updateApplySetButton(rule);
    renderFilterCoverage();
  });
  document.addEventListener("click", (event) => {
    const picker = event.target.closest(".apply-picker");
    if (!picker && !$("applySetPopover")?.classList.contains("hidden")) setApplyPopoverOpen(false);
  });
  $("schemeRenameModal")?.addEventListener("click", (event) => {
    if (event.target.closest("[data-rename-close]")) closeEquipmentFilterRename();
  });
  $("schemeRenameConfirm")?.addEventListener("click", saveEquipmentFilterRename);
  $("schemeRenameInput")?.addEventListener("input", () => setSchemeRenameError());
  $("schemeImportFile")?.addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (file.size > 1024 * 1024) {
      showMessageToast("方案文件超过 1 MB，无法导入", "err", 5200);
      finishEquipmentFilterImport();
      return;
    }
    try {
      handleEquipmentFilterImport(file.name, await file.text());
    } catch (error) {
      showMessageToast(error.message || "读取方案文件失败", "err", 5200);
      finishEquipmentFilterImport();
    }
  });
  $("confirmModal")?.addEventListener("click", (event) => {
    if (event.target.closest("[data-confirm-cancel]")) closeConfirmDialog(false);
  });
  $("confirmAction")?.addEventListener("click", () => closeConfirmDialog(true));
  $("desktopCloseModal")?.addEventListener("click", (event) => {
    if (event.target.closest("[data-desktop-close-cancel]")) cancelDesktopClose();
  });
  $("desktopCloseCancel")?.addEventListener("click", cancelDesktopClose);
  $("desktopCloseConfirm")?.addEventListener("click", confirmDesktopClose);
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!$("desktopCloseModal")?.classList.contains("hidden")) {
      event.preventDefault();
      cancelDesktopClose();
    } else if (!$("confirmModal")?.classList.contains("hidden")) {
      event.preventDefault();
      closeConfirmDialog(false);
    } else if (!$("schemeRenameModal")?.classList.contains("hidden")) {
      event.preventDefault();
      closeEquipmentFilterRename();
    }
  });
}

// ---------- 启动：显示登录页，等待用户选择账号 ----------
async function initApp() {
  await loadFrontendSettings();
  renderSwatches();
  applyTheme(getTheme());
  initBottomLogDock();
  initAccounts();
  initDevGate();
  initToolbox();
  initEquipCardShells();
  initUpdateUI();
}

initApp();
