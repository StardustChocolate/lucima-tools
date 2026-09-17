// LucimaTools 前端逻辑
const $ = (id) => document.getElementById(id);
// 这些接口不绑定单个账号（全局或自带 account），api() 不自动注入 activeAccount
const _GLOBAL_PATHS = new Set(["/api/config", "/api/logs", "/api/tasks", "/api/accounts", "/api/login", "/api/login/saved", "/api/accounts/logout", "/api/accounts/delete", "/api/accounts/restore", "/api/update/status", "/api/update/check", "/api/update/ignore", "/api/update/install"]);
const api = async (path, body) => {
  // POST：向 body 注入 account（未显式给且非全局接口时）
  if (body && typeof body === "object" && !("account" in body) && activeAccount && !_GLOBAL_PATHS.has(path)) {
    body = { ...body, account: activeAccount };
  }
  // GET：非全局接口带上 ?account=
  let url = path;
  if (!body && activeAccount && !_GLOBAL_PATHS.has(path) && !new URLSearchParams(path.split("?")[1] || "").has("account")) {
    url += (path.includes("?") ? "&" : "?") + "account=" + encodeURIComponent(activeAccount);
  }
  const opt = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const res = await fetch(url, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.detail || `HTTP ${res.status}`), { httpStatus: res.status });
  return data;
};

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
    const v = JSON.parse(localStorage.getItem(THEME_LS)) || {};
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
  localStorage.setItem(THEME_LS, JSON.stringify(t));
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
const SECTION_TITLES = { info: "基本信息", auto: "自动任务", manual: "主动任务", toolbox: "工具箱", logs: "运行日志", settings: "系统设置", about: "关于我们" };
let currentSection = "info";
let logUnread = 0;
function showSection(id) {
  currentSection = id;
  if (id === "toolbox" && typeof showToolboxHome === "function") showToolboxHome();
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
      renderAccountSwitcher();
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
    try { localStorage.setItem(BOTTOM_LOG_HEIGHT_LS, String(value)); } catch { /* optional */ }
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
    try { localStorage.setItem(BOTTOM_LOG_LS, enabled ? "1" : "0"); } catch { /* optional */ }
  }
}

function initBottomLogDock() {
  let enabled = false;
  try { enabled = localStorage.getItem(BOTTOM_LOG_LS) === "1"; } catch { /* optional */ }
  try {
    const savedHeight = Number(localStorage.getItem(BOTTOM_LOG_HEIGHT_LS));
    bottomLogHeight = Number.isFinite(savedHeight) && savedHeight > 0 ? savedHeight : defaultBottomLogHeight();
  } catch {
    bottomLogHeight = defaultBottomLogHeight();
  }
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
    await new Promise((r) => setTimeout(r, 500));
    registerAccount(data);
    log(`账号 ${data.account} 已连接`, "ok");
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
    name: (data.status || {}).name,
    avatar: (data.status || {}).avatar,
    logHtml: "",
    logSeq: data.logSeq || 0,
    logUnread: 0,
  };
  (data.autoResults || []).forEach((r) => logResult(r, r.prefix || "自动·", email));
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

// 从登录视图切到应用视图
function enterApp() {
  $("connPill").className = "pill on";
  $("connPill").textContent = "● 已连接";
  $("loginView").classList.add("hidden");
  $("appView").classList.remove("hidden");
}

// 切换当前查看的账号：刷新状态/开关/日志/切换器。
function switchAccount(email) {
  if (!ACCOUNTS[email]) return;
  activeAccount = email;
  try { sessionStorage.setItem(TEST_ACTIVE_ACCOUNT_SS, email); } catch { /* optional */ }
  const a = ACCOUNTS[email];
  a.logUnread = 0;
  NEXT_CLAIMS = a.nextClaims || {};
  renderStatus(a.status || {});
  loadRoster(email);           // 团员/物品清单（按账号缓存，首次拉取）
  loadTasks();                 // 重渲染开关/手动面板（反映该账号的 toggles/params）
  // 恢复该账号日志到完整日志页和底部日志抽屉。
  renderLogViews(a.logHtml || "");
  logUnread = 0; updateLogBadge();
  renderAccountSwitcher();
  renderDevConsole();          // 控制台的 route/data 输入是按账号记的，切号要重载
  showSection("info");
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
    // 掉落行：道具文字 + 装备名，**合成一条**（装备名各自可点击展开详情卡片）
    if (ln && typeof ln === "object" && ln.type === "drop") {
      const links = (ln.equips || []).map((e) => {
        const key = `eqd${++equipDropSeq}`;
        EQUIP_DROPS[key] = e.card;
        return `<a class="equip-link" data-eqd="${key}">${e.text} ▸</a>`;
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
  { key: "stamina", label: "能源", color: "#4ce68a", icon: "/static/assets/icons/stamina.png" },
  { key: "flag", label: "旗帜", color: "#ff6b9d", icon: "/static/assets/icons/flag.png" },
];
// 平铺资产（图标 + 名称 + 数量）
const CUR_META = [
  { key: "gold", label: "金币", icon: "/static/assets/icons/gold.png" },
  { key: "diamond", label: "钻石", icon: "/static/assets/icons/diamond.png" },
  { key: "battery", label: "能源电池", icon: "/static/assets/items/37.png" },
  { key: "recruit", label: "招募契约", icon: "/static/assets/icons/recruit.png" },
  { key: "mystery", label: "神秘契约", icon: "/static/assets/icons/mystery.png" },
  { key: "galaxy", label: "银河契约", icon: "/static/assets/icons/galaxy.png" },
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
const GRADE_ICON = "/static/assets/potential/";
let rosterTab = "heroes";  // 当前 tab
let heroElement = "all";
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

// 拉取某账号的团员/物品清单（按账号缓存，避免重复请求）。
async function loadRoster(email, force = false) {
  const a = ACCOUNTS[email];
  if (!a) return;
  if (a.roster && !force) {
    a.roster = await enrichRosterElements(a.roster);
    if (email === activeAccount) renderRoster(a.roster);
    return;
  }
  try {
    const r = await enrichRosterElements(await api("/api/roster"));
    a.roster = r;
    if (email === activeAccount) renderRoster(r);
  } catch (e) {
    if (email === activeAccount) {
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

function heroCardHTML(h) {
    const init = (h.name || h.id || "?").slice(0, 1);
    const av = h.avatar
      ? `<img src="${h.avatar}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'hero-ph',textContent:'${init}'}))">`
      : `<span class="hero-ph">${init}</span>`;
    const grade = h.grade
      ? `<img class="hero-grade" src="${GRADE_ICON}${h.grade}.png" alt="${h.grade}" title="潜能 ${h.grade}">` : "";
    const lv = h.level != null ? `<span class="hero-lv">${h.level}</span>` : "";
    return `<div class="hero-card">
      <div class="hero-pic">${av}${lv}${grade}</div>
      <div class="hero-name">${h.name || h.id}</div>
    </div>`;
}

function renderRosterHeroes(heroes) {
  const selected = heroElement === "all"
    ? heroes
    : heroes.filter((hero) => heroElementOf(hero) === heroElement);
  const tabs = HERO_ELEMENTS.map(({ id, icon, label }) => `
    <button type="button" class="hero-element-tab ${id === heroElement ? "active" : ""}"
      data-hero-element="${id}" title="${label}" aria-label="${label}" aria-selected="${id === heroElement}" role="tab">
      <img src="${icon}" alt="">
    </button>`).join("");
  const grid = selected.length
    ? selected.map(heroCardHTML).join("")
    : `<div class="roster-empty hero-element-empty">暂无此属性团员</div>`;
  $("rosterHeroes").innerHTML = `
    <div class="hero-element-tabs" role="tablist" aria-label="团员属性">${tabs}</div>
    <div class="hero-grid">${grid}</div>`;
  $("rosterHeroes").querySelectorAll(".hero-element-tab").forEach((button) => {
    button.addEventListener("click", () => {
      heroElement = button.dataset.heroElement;
      renderRosterHeroes(heroes);
    });
  });
  centerActiveHeroElement();
}

function centerActiveHeroElement() {
  const tabBar = $("rosterHeroes").querySelector(".hero-element-tabs");
  if (!tabBar) return;
  const activeTab = tabBar.querySelector(".hero-element-tab.active");
  if (activeTab) tabBar.scrollLeft = activeTab.offsetLeft - (tabBar.clientWidth - activeTab.offsetWidth) / 2;
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
    <div class="item-tile r${it.rank || 0}" title="${it.name || it.id}">
      <div class="item-ic"><img src="/static/assets/items/${it.id}.png" alt="" loading="lazy"
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
      <span>${label}</span><span class="item-category-count">${grouped[id].length}</span>
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
      renderRosterItems(items);
    });
  });
  centerActiveItemCategory();
}

function centerActiveItemCategory() {
  const tabBar = $("rosterItems").querySelector(".item-category-tabs");
  if (!tabBar) return;
  const activeTab = tabBar.querySelector(".item-category-tab.active");
  if (activeTab) tabBar.scrollLeft = activeTab.offsetLeft - (tabBar.clientWidth - activeTab.offsetWidth) / 2;
}

function applyRosterTab() {
  document.querySelectorAll(".rtab").forEach((b) =>
    b.classList.toggle("active", b.dataset.rtab === rosterTab));
  $("rosterHeroes").classList.toggle("active", rosterTab === "heroes");
  $("rosterItems").classList.toggle("active", rosterTab === "items");
  if (rosterTab === "heroes") requestAnimationFrame(centerActiveHeroElement);
  if (rosterTab === "items") requestAnimationFrame(centerActiveItemCategory);
}

// ---------- 任务 ----------
let TASKS = [];
let TOGGLE_TASKS = [];
const TEST_ACTIVE_ACCOUNT_SS = "ark_test_active_account";
const TASK_ICONS = {
  month_signin: "📅", week_signin: "🎁", claim_mail: "✉️", friend_support: "🤝",
  reactor: "⚡", sf_potion: "🧪", sf_starforce: "⭐", sf_tesseract: "🔷",
  dispatch: "🎖️", shop_free: "🛒", shop_diamond: "💎", hunt_sweep: "⚔️", element_sweep: "🔮",
  activity_scene: "🎫", abyss_sweep: "🌌", free_summon: "🎰", store_buy: "🛍️",
  star_source_shop: "🌈", customized_equip: "🔧",
};
// 部分任务改用游戏内物品图标（覆盖上面的 emoji）：动力转换=钻石、成长药剂=传说成长药剂、
// 升星水晶=传说升星水晶、技能模块=技能模块、派遣任务=荣誉勋章、
// 秘密商店刷新(免费)=金币、虚拟幻境=星源粉末、每日免费招募=招募契约、商店购买=荣誉勋章、
// 竞技场NPC=旗帜（挑战就是扣旗帜，与基本信息页旗帜表盘同一个 StaticID 4）
const TASK_ICON_IMG = {
  reactor: "2", sf_potion: "103", sf_starforce: "106",
  sf_tesseract: "34", dispatch: "26", shop_free: "1",
  shop_diamond: "2", abyss_sweep: "25", free_summon: "5", store_buy: "26",
  arena_npc: "4", star_source_shop: "109",
};
const TASK_ICON_UI = {
  claim_mail: "mail.png",
  hunt_sweep: "hunt.png",
  element_sweep: "element.png",
  activity_scene: "activity.png",
  store_buy: "store.png",
  customized_equip: "customized-equip.png",
};
// 生成任务图标 HTML：指定的界面图标优先，其次物品图标，最后回退 emoji/◆。
function taskIconHTML(id, fallback = "◆") {
  const uiIcon = TASK_ICON_UI[id];
  const lightThemeClass = ["claim_mail", "hunt_sweep", "element_sweep", "activity_scene", "store_buy"].includes(id)
    ? " task-icon-light-invert"
    : "";
  if (uiIcon) return `<img class="ticon-img${lightThemeClass}" src="/assets/ui/${uiIcon}" alt="" loading="lazy">`;
  const sid = TASK_ICON_IMG[id];
  if (sid) return `<img class="ticon-img" src="/static/assets/items/${sid}.png" alt="" loading="lazy">`;
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
// blocked（条件不满足，开关置灰）/ note（非计时型任务的状态说明）优先于时间文案
// 免费刷商店说"刷新"而不是"领取"（它不是产出型任务）。档期来自货架
// LastResetTime + 1h，见 backend/tasks.secret_shop_next_free。
const NEXT_WORDS = {
  shop_free: { ready: "可免费刷新", next: "下次免费刷新" },
};
function fmtNextClaim(nc, taskId = "") {
  if (!nc) return "";
  if (nc.blocked) return nc.blocked;
  if (nc.note) return nc.note;
  const w = NEXT_WORDS[taskId] || { ready: "可领取", next: "下次领取时间" };
  if (nc.nextMs == null) return nc.ready ? w.ready : "";
  const diff = nc.nextMs - Date.now();
  if (diff <= 0) return w.ready;
  return `${w.next}: ${fmtClock(nc.nextMs)} (${fmtRemain(diff)})`;
}

// 技能模块的"加速"档期（每生产周期 3 次、间隔 24h）单独一行展示
function fmtCharge(c) {
  const q = `已用 ${c.used}/${c.max}`;
  if (c.exhausted) return `加速已用完 ${c.used}/${c.max}`;
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
    const rows = [`<span class="nc-row${cls}">${fmtNextClaim(nc, el.dataset.id)}</span>`];
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
      if (t.ui === "arena") {
        return `<div class="auto-item has-body ${on ? "on" : ""}" data-id="${t.id}" id="auto-${t.id}">
          <div class="auto-head">
            ${inner}
            <button type="button" class="mini arena-toggle">选项 ▸</button>
            <label class="switch-wrap">
              <input type="checkbox" class="switch-in" ${on ? "checked" : ""}>
              <div class="switch"></div>
            </label>
          </div>
          <div class="auto-body hidden">${renderArenaBody(t)}</div>
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
          <div class="auto-body hidden">${renderShopFreeOptions()}</div>
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
  toggle.forEach((t) => { if (t.wantsBuyTargets) bindShopFreeOptions(t.id); });
  renderNextClaims();
}

function renderShopFreeOptions() {
  const wantedKeys = getWantedKeys();
  const options = BUY_TARGETS.map((target) => `<label class="bt-opt">
    <input type="checkbox" value="${target.key}" ${wantedKeys.includes(target.key) ? "checked" : ""}>
    <span>${target.label}</span>
  </label>`).join("");
  return `<div class="auto-buy-targets">
    <div class="bt-label">自动购买目标</div>
    <div class="bt-opts">${options}</div>
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
    input.addEventListener("change", () => {
      const keys = [...body.querySelectorAll('.bt-opt input[type="checkbox"]:checked')]
        .map((entry) => entry.value);
      setWantedKeys(keys);
    });
  });
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
      <span class="sp-box"></span>
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
        <span class="sp-box"></span>
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
    if (meta && meta.wantsBuyTargets) p[id] = { wanted: wantedIds() };
  });
  return p;
}

// 执行指定 toggle 任务集（ids 为空则不发请求）
// quiet=true 时静默"未到时间/未满"的跳过项（skipped），只输出真正领到的——
// 供每小时巡检用，避免每次刷一堆"还需 N 小时，跳过"的噪音。
async function runToggleTasks(ids, prefix = "自动·", quiet = false) {
  if (!ids || !ids.length) return;
  // 竞技场由后端逐场写日志；运行期间加快拉取，让每场结束后立即出现在界面上。
  const streamsArena = ids.includes("arena_npc");
  const liveLogTimer = streamsArena ? setInterval(pollAutoLogs, 700) : null;
  try {
    const { results } = await api("/api/tasks/auto", { taskIds: ids, params: toggleParams(ids) });
    results.forEach((r) => {
      if (quiet && r.skipped) return;
      if (r.liveLogged) return;
      logResult(r, prefix);
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
    const v = JSON.parse(localStorage.getItem(shopLsKey())) || {};
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
  localStorage.setItem(shopLsKey(), JSON.stringify({ ...getShopCfg(), ...patch }));
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
    (b) => `<label class="bt-opt"><input type="checkbox" data-wkey="${b.key}" ${cfg.wanted.includes(b.key) ? "checked" : ""}><span>${b.label}</span></label>`
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
            <label class="bt-opt bold"><input type="checkbox" class="leg-on" ${cfg.legOn ? "checked" : ""}><span>85级传说装备</span></label>
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
  root.querySelector(".shop-start").addEventListener("click", () => runShopDiamond(id));
  const saved = activeAcc()?.secretShopResult;
  if (saved?.id === id) renderSecretShopState(id, saved);
  if (shopRunning[activeAccount]) {
    setSecretShopStartState(id, activeAccount, activeAcc()?.secretShopPending ? "matched" : "running");
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
  button.disabled = mode !== "idle";
  button.textContent = mode === "matched" ? "等待选择…" : mode === "running" ? "刷新中…" : "▶ 开始刷新";
}

function resolveSecretShopPending(id, email, decision) {
  const account = ACCOUNTS[email];
  const pending = account?.secretShopPending;
  if (!pending || pending.id !== id) return false;
  account.secretShopPending = null;
  setSecretShopStartState(id, email, "running");
  pending.resolve(decision);
  return true;
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
  setSecretShopStartState(id, email, "running");
  const wanted = shopWantedIds();
  const gained = {};   // 本次整轮的目标道具产出累计，结束后并进收尾那一条日志
  let completed = 0;
  let skippedMatches = 0;
  let purchasedMatches = 0;
  let endText = "刷新完成";   // 收尾文案：正常跑完/出错两种，与统计合成一句
  let endLv = "ok";
  let failed = false;
  logTo(email, `${SHOP_TAG} 开始刷新：${cfg.count} 次${scheme ? `，装备方案“${scheme.name}”` : ""}`);
  try {
    for (let i = 1; i <= cfg.count; i++) {
      renderSecretShopState(id, { kind: "running", round: i, total: cfg.count }, email);
      const r = await api("/api/shop/reset", {
        account: email, useGold: true, autoBuy: wanted,
        equipmentScheme: schemeSnapshot,
      });
      completed = i;
      if (r.status) applyStatus(email, r.status, r.nextClaims);  // 每次刷新/购买后即时更新主页资产
      addShopGain(gained, r.bought);   // 失败中止的那次也可能已买到东西，先累加再判 ok
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
        logTo(email, `${SHOP_TAG} 第 ${i} 次命中 ${legendaryMatches.length} 件装备，等待选择`, "ok");
        const decision = await waitForSecretShopDecision(id, {
          cards: legendaryMatches,
          round: i,
          total: cfg.count,
          schemeName: scheme?.name || "",
        }, email);
        purchasedMatches += Number(decision?.count) || 0;
        if (decision?.action === "bought") {
          if (i < cfg.count && !buyErrs.length) logTo(email, `${SHOP_TAG} 本轮命中装备已全部购买，继续刷新`, "ok");
        } else {
          skippedMatches += 1;
          if (i < cfg.count && !buyErrs.length) logTo(email, `${SHOP_TAG} 已跳过本轮剩余命中装备，继续刷新`);
        }
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
    if (!failed) {
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
    const value = JSON.parse(localStorage.getItem(starSourceShopLsKey())) || {};
    const schemeId = schemes.some((scheme) => scheme.id === value.schemeId) ? value.schemeId : fallbackId;
    return { count: clampCount(value.count || 10), schemeId };
  } catch {
    return { count: 10, schemeId: fallbackId };
  }
}

function setStarSourceShopCfg(patch) {
  localStorage.setItem(
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
  const icon = '<img class="star-source-empty-icon" src="/static/assets/items/109.png" alt="">';
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
  button.disabled = mode !== "idle";
  button.textContent = mode === "matched" ? "等待选择…" : mode === "running" ? "刷新中…" : "▶ 开始刷新";
}

function resolveStarSourcePending(id, email, decision) {
  const account = ACCOUNTS[email];
  const pending = account?.starSourcePending;
  if (!pending || pending.id !== id) return false;
  account.starSourcePending = null;
  setStarSourceStartState(id, email, "running");
  pending.resolve(decision);
  return true;
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
    countInput.value = clampCount(countInput.value);
    setStarSourceShopCfg({ count: parseInt(countInput.value) });
    runStarSourceShop(id);
  });
  const saved = activeAcc()?.starSourceResult;
  if (saved?.id === id) renderStarSourceState(id, saved);
  if (starSourceRunning[activeAccount]) {
    setStarSourceStartState(id, activeAccount, activeAcc()?.starSourcePending ? "matched" : "running");
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
  setStarSourceStartState(id, email, "running");
  logTo(email, "[星源商店刷新] 开始刷新：" + cfg.count + " 次，方案“" + scheme.name + "”");
  let completed = 0;
  let skippedMatches = 0;
  let purchasedMatches = 0;
  let failed = false;
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
        logTo(email, "[星源商店刷新] 第 " + round + " 次命中 " + matches.length + " 件装备，等待选择", "ok");
        const decision = await waitForStarSourceSkip(id, {
          cards: matches,
          round,
          total: cfg.count,
          schemeName: scheme.name,
        }, email);
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
    }
    if (!failed) {
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
    if (ACCOUNTS[email]) ACCOUNTS[email].starSourcePending = null;
    setStarSourceStartState(id, email, "idle");
    refreshAccountStatus(email);
  }
}

// ---------- 装备活动刷新 ----------
const CUSTOMIZED_EQUIP_LS = "ark_customized_equip_cfg";
const customizedEquipLsKey = () => `${CUSTOMIZED_EQUIP_LS}::${activeAccount || "_"}`;
const customizedEquipRunning = {};

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
    const saved = JSON.parse(localStorage.getItem(customizedEquipLsKey())) || {};
    return {
      count: clampCount(saved.count || 20),
      schemeId: schemes.some((scheme) => scheme.id === saved.schemeId) ? saved.schemeId : fallbackId,
    };
  } catch {
    return { count: 20, schemeId: fallbackId };
  }
}

function setCustomizedEquipCfg(patch) {
  localStorage.setItem(customizedEquipLsKey(), JSON.stringify({ ...getCustomizedEquipCfg(), ...patch }));
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
        <button type="button" class="customized-reset" data-customized-reset="set" title="重置套装" aria-label="重置套装">↺</button>
      </div>
      <div class="customized-field"><span class="customized-label">部位</span><div class="customized-control customized-part-slot"></div><button type="button" class="customized-reset" data-customized-reset="part" title="重置部位" aria-label="重置部位">↺</button></div>
      <div class="customized-field"><span class="customized-label">主属性</span><div class="customized-control customized-main-slot"></div><button type="button" class="customized-reset" data-customized-reset="main" title="重置主属性" aria-label="重置主属性">↺</button></div>
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
  const actionSelector = ".customized-set-button, .customized-set-popover button, .customized-reset, "
    + ".customized-part-slot button, .customized-main-slot button, .customized-scheme button, "
    + ".customized-equip-start, .customized-equip-count, .customized-choice-btn, .customized-equip-reward";
  root.querySelectorAll(actionSelector).forEach((control) => {
    control.disabled = busy || rewarded;
  });
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
  root.querySelector(".customized-equip-start").disabled =
    stage !== "RefreshSubProp" || !customizedEquipRuleStatus(state).valid;
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
  root.querySelector(".customized-equip-start").addEventListener("click", () => runCustomizedEquip(id));
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
  setCustomizedEquipBusy(root, true);
  const start = root.querySelector(".customized-equip-start");
  start.textContent = "刷新中…";
  const schemeSnapshot = JSON.parse(JSON.stringify(scheme));
  logTo(email, `[装备活动刷新] 开始刷新：${cfg.count} 次，方案“${scheme.name}”`);
  try {
    let completed = 0;
    for (let round = 1; round <= cfg.count; round++) {
      const result = await api("/api/customized-equip/action", {
        account: email, action: "refresh", scheme: schemeSnapshot,
      });
      if (result.status) applyStatus(email, result.status, result.nextClaims);
      if (!result.ok) throw new Error(result.error || "刷新失败");
      completed = round;
      state = result;
      updateCustomizedEquipWidget(id, state, `第 ${round} / ${cfg.count} 次刷新${state.matches ? "，已命中" : ""}`);
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
  } catch (error) {
    updateCustomizedEquipWidget(id, state, error.message);
    logTo(email, `[装备活动刷新] ${error.message}`, "err");
  } finally {
    customizedEquipRunning[email] = false;
    setCustomizedEquipBusy(root, false);
    start.textContent = "▶ 开始刷新";
    refreshAccountStatus(email);
  }
}

// ---------- 扫荡自定义控件（讨伐扫荡 / 元素扫荡共用） ----------
// 配置按 任务id + 账号 双重命名空间（多个扫荡任务各自独立；队伍 ID 各账号不同）
const SWEEP_LS = "ark_sweep_cfg"; // {count, element, sceneId, teamId, quick}
const sweepLsKey = (id) => `${SWEEP_LS}::${id}::${activeAccount || "_"}`;
function getSweepCfg(id) {
  try {
    const v = JSON.parse(localStorage.getItem(sweepLsKey(id))) || {};
    return {
      count: v.count || 5, element: v.element || "", sceneId: v.sceneId || "", teamId: v.teamId || "",
      quick: v.quick !== false,  // 默认勾选：使用快速战斗券
    };
  } catch {
    return { count: 5, element: "", sceneId: "", teamId: "", quick: true };
  }
}
function setSweepCfg(id, patch) {
  localStorage.setItem(sweepLsKey(id), JSON.stringify({ ...getSweepCfg(id), ...patch }));
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
      (o) => `<div class="dd-opt ${o.val === curVal ? "sel" : ""}" data-val="${o.val}">${dot(o.element)}<span>${o.label}</span></div>`
    )
    .join("");
  return `<div class="dropdown ${cls}" data-val="${cur ? cur.val : ""}">
    <button type="button" class="dd-btn" ${disabled ? "disabled" : ""}><span class="dd-cur">${curLabel}</span><span class="dd-caret">▾</span></button>
    <div class="dd-menu hidden">${opts}</div>
  </div>`;
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

function renderSweepWidget(t) {
  const cfg = getSweepCfg(t.id);
  const sceneFields = `<div class="shop-row col">
      <span class="shop-lbl">选择属性</span>
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
      <label class="save-pwd sweep-quick-row">
        <input type="checkbox" class="sweep-quick" ${cfg.quick ? "checked" : ""}>
        <span class="sp-box"></span>
        <span class="sp-txt">使用快速战斗券</span>
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
      root.dataset.loaded = "1";
    }
  });

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

  root.querySelector(".sweep-start").addEventListener("click", () => runSweep(id));
}

async function loadSweepOptions(id) {
  const root = $(`sweep-${id}`);
  const kind = root.dataset.kind || "hunt";
  const elementSlot = root.querySelector(".sweep-element-slot");
  const sceneSlot = root.querySelector(".sweep-scene-slot");
  const teamSlot = root.querySelector(".sweep-team-slot");
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
  } catch (e) {
    if (elementSlot) elementSlot.innerHTML = `<div class="dd-loading err">加载失败</div>`;
    sceneSlot.innerHTML = `<div class="dd-loading err">加载失败</div>`;
    teamSlot.innerHTML = `<div class="dd-loading err">加载失败</div>`;
    log(`加载扫荡选项失败：${e.message}`, "err");
  }
}

// 同商店：账号锁定为发起时的账号，请求显式带 email、日志写该账号缓冲，切账号不串。
const sweepRunning = {};  // email -> bool
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
  startBtn.disabled = true;
  startBtn.textContent = "扫荡中…";
  const sceneName = (root.querySelector(".sweep-scene .dd-cur")?.textContent || cfg.sceneId).trim();
  logTo(email, `开始${taskName}：${sceneName} × ${cfg.count} 次`);
  let done = 0;
  try {
    for (let i = 1; i <= cfg.count; i++) {
      const r = await api("/api/sweep/run", { account: email, sceneId: cfg.sceneId, teamId: cfg.teamId, quick: cfg.quick });
      if (r.status) applyStatus(email, r.status, r.nextClaims);
      logResult({ ...r, name: taskName }, `第${i}次·`, email);
      if (!r.ok) { logTo(email, `第 ${i} 次未获得掉落，停止扫荡`, "err"); break; }
      done++;
    }
    if (summary && email === activeAccount) summary.textContent = `完成 ${done}/${cfg.count} 次`;
  } catch (e) {
    logTo(email, `扫荡出错：${e.message}`, "err");
  } finally {
    sweepRunning[email] = false;
    if (email === activeAccount && startBtn.isConnected) {
      startBtn.disabled = false;
      startBtn.textContent = "▶ 开始扫荡";
    }
    refreshAccountStatus(email);
  }
}

// ---------- 活动关卡自定义控件（含助战好友选择） ----------
// 配置按账号命名空间（关卡/队伍/助战 各账号不同）
const ACT_LS = "ark_activity_cfg"; // {count, sceneId, teamId, supportCuid, quick}
const actLsKey = () => `${ACT_LS}::${activeAccount || "_"}`;
function getActCfg() {
  try {
    const v = JSON.parse(localStorage.getItem(actLsKey())) || {};
    return {
      count: v.count || 5, sceneId: v.sceneId || "", teamId: v.teamId || "",
      supportCuid: v.supportCuid || "", quick: v.quick !== false,
    };
  } catch {
    return { count: 5, sceneId: "", teamId: "", supportCuid: "", quick: true };
  }
}
function setActCfg(patch) {
  localStorage.setItem(actLsKey(), JSON.stringify({ ...getActCfg(), ...patch }));
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
      <div class="shop-row col">
        <div class="act-option-head">
          <span class="shop-lbl">活动关卡</span>
          <button type="button" class="act-options-refresh" title="重新加载活动关卡选项" aria-label="重新加载活动关卡选项">↻</button>
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
      <label class="save-pwd act-quick-row">
        <input type="checkbox" class="act-quick" ${cfg.quick ? "checked" : ""}>
        <span class="sp-box"></span>
        <span class="sp-txt">使用快速战斗券</span>
      </label>
      <button type="button" class="btn primary act-start">▶ 开始扫荡</button>
      <div class="sweep-summary" id="actSummary-${t.id}"></div>
    </div>
  </div>`;
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

  root.querySelector(".act-start").addEventListener("click", () => runActivity(id));
}

async function loadActivityOptions(id, refresh = false) {
  const root = $(`act-${id}`);
  if (!root || root.dataset.loading === "1") return false;
  const sceneSlot = root.querySelector(".act-scene-slot");
  const teamSlot = root.querySelector(".act-team-slot");
  const supSlot = root.querySelector(".act-support-slot");
  const refreshButton = root.querySelector(".act-options-refresh");
  root.dataset.loading = "1";
  refreshButton.disabled = true;
  refreshButton.classList.add("loading");
  if (refresh || !root.dataset.loaded) {
    sceneSlot.innerHTML = `<div class="dd-loading">加载中…</div>`;
    teamSlot.innerHTML = `<div class="dd-loading">加载中…</div>`;
    supSlot.innerHTML = `<div class="dd-loading">加载中…</div>`;
  }
  try {
    const { scenes, teams, supports, favorites } = await api("/api/activity/options");
    const cfg = getActCfg();

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
    delete root.dataset.loaded;
    sceneSlot.innerHTML = `<div class="act-load-error"><span>加载失败</span><button type="button" class="act-options-retry">重新加载</button></div>`;
    teamSlot.innerHTML = `<div class="dd-loading err">等待重新加载</div>`;
    supSlot.innerHTML = `<div class="dd-loading err">等待重新加载</div>`;
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
  startBtn.disabled = true;
  startBtn.textContent = "扫荡中…";
  const sceneName = (root.querySelector(".act-scene .dd-cur")?.textContent || cfg.sceneId).trim();
  const supName = cfg.supportCuid
    ? (root.querySelector(".sup-card.sel .sup-name")?.textContent || "").trim() : "";
  logTo(email, `开始${taskName}：${sceneName} × ${cfg.count} 次${supName ? `（助战 ${supName}）` : "（无助战）"}`);
  let done = 0;
  try {
    for (let i = 1; i <= cfg.count; i++) {
      const r = await api("/api/activity/run", {
        account: email, sceneId: cfg.sceneId, teamId: cfg.teamId,
        supportCuid: cfg.supportCuid || null, quick: cfg.quick,
      });
      if (r.status) applyStatus(email, r.status, r.nextClaims);
      logResult({ ...r, name: taskName }, `第${i}次·`, email);
      if (!r.ok) { logTo(email, `第 ${i} 次未获得掉落，停止扫荡`, "err"); break; }
      done++;
    }
    if (summary && email === activeAccount) summary.textContent = `完成 ${done}/${cfg.count} 次`;
  } catch (e) {
    logTo(email, `活动关卡出错：${e.message}`, "err");
  } finally {
    actRunning[email] = false;
    if (email === activeAccount && startBtn.isConnected) {
      startBtn.disabled = false;
      startBtn.textContent = "▶ 开始扫荡";
    }
    refreshAccountStatus(email);
  }
}

// ---------- 商店购买自定义控件（VIP礼包 / 友情 / 荣誉勋章） ----------
// 选中态按账号命名空间（各账号想买什么互不相干）：{tab, picks:{档位id: 份数}}
const STORE_LS = "ark_store_buy_cfg";
const storeLsKey = () => `${STORE_LS}::${activeAccount || "_"}`;
function getStoreCfg() {
  try {
    const v = JSON.parse(localStorage.getItem(storeLsKey())) || {};
    return { tab: v.tab || "", picks: (v.picks && typeof v.picks === "object") ? v.picks : {} };
  } catch {
    return { tab: "", picks: {} };
  }
}
function setStoreCfg(patch) {
  localStorage.setItem(storeLsKey(), JSON.stringify({ ...getStoreCfg(), ...patch }));
}
// 货架数据按账号缓存（展开时拉一次，切 tab 不重复请求）
const STORE_DATA = {};   // email -> {stores, wallet}
const itemIcon = (sid) => `/static/assets/items/${sid}.png`;
const fmtNum2 = (n) => (n == null ? "—" : Number(n).toLocaleString("en-US"));

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
    STORE_DATA[activeAccount] = await api("/api/store/options");
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
    const state = g.limit != null ? `购买 ${g.remain}/${g.limit}`
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
function applyStatus(email, status, nextClaims, arena) {
  const a = ACCOUNTS[email]; if (!a) return;
  if (status) { a.status = status; a.name = status.name; a.avatar = status.avatar; }
  if (nextClaims) a.nextClaims = nextClaims;
  // 竞技场档期（/api/status 带回）。直接采用后端值：勾选存在后端 params 里、
  // 由 /api/arena/config 单独写入，且 /api/toggles 已改成按 key 合并不会抹掉它，
  // 所以后端就是唯一真源，不需要再在前端做"保留本地勾选"的兜底。
  if (arena) a.arena = arena;
  if (email === activeAccount) {
    if (status) renderStatus(status);
    if (nextClaims) { NEXT_CLAIMS = nextClaims; renderNextClaims(); }
    if (arena) renderArenaList("arena_npc");
  }
  renderAccountSwitcher();
}

async function refreshStatus(quiet = false) {
  const email = activeAccount;
  try {
    const { status, nextClaims, arena } = await api("/api/status");
    applyStatus(email, status, nextClaims, arena);
    if (!quiet) log("状态已刷新", "ok");
  } catch (e) { if (!quiet) log(`刷新失败：${e.message}`, "err"); }
}

// 手动刷新按钮：重新登录拉全量（唯一能拿到游戏客户端内变化 + 体力恢复的权威数据）。
// 只在用户主动点按钮时才重登，不做自动轮询。
async function manualRefresh() {
  const email = activeAccount;
  const btn = $("refreshBtn");
  if (btn) { btn.disabled = true; btn.classList.add("spinning"); }
  try {
    const { status, nextClaims, arena } = await api("/api/status/refresh", {});
    applyStatus(email, status, nextClaims);
    // 刷新会清空竞技场的失败退避（后端 refresh_status 调 reset_task_runtime），
    // 把新状态写回并重渲，让"重试倒计时"立刻变回"可挑战"。
    if (arena && ACCOUNTS[email]) {
      ACCOUNTS[email].arena = arena;
      if (email === activeAccount) renderArenaList("arena_npc");
    }
    loadRoster(email, true);   // 重登后团员/物品也刷新
    log("状态已刷新（重新登录拉取全量）", "ok");
  } catch (e) {
    log(`刷新失败：${e.message}`, "err");
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove("spinning"); }
  }
}

// ---------- 账号切换器 + 后台日志轮询 ----------
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
      <div class="acc-ava">${avatarHtml}</div>
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
        <div class="acc-ava sm">${av}</div>
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
      try {
        const { entries, logSeq } = await api(`/api/logs/auto?account=${encodeURIComponent(email)}&since=${a.logSeq || 0}`);
        if (entries && entries.length) {
          // 用后端给的产生时刻，而不是此刻——窗口冻结期间积压的记录到这里才被拉回
          entries.forEach((e) => logResult(e.result, e.prefix, email, e.ts));
          a.logSeq = logSeq;
          // 有新巡检日志 → 该账号状态多半变了，刷新其状态缓存
          refreshAccountStatus(email);
        }
      } catch (e) { /* 账号可能已下线，忽略 */ }
    }
  } finally {
    autoLogPollPending = false;
  }
}
// 拉某账号最新状态写回缓存（不重登，读后端缓存）
async function refreshAccountStatus(email) {
  try {
    const res = await fetch(`/api/status?account=${encodeURIComponent(email)}`);
    if (!res.ok) return;
    const { status, nextClaims, arena } = await res.json();
    applyStatus(email, status, nextClaims, arena);
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
  loginStatus("");
  renderSavedAccounts();
}

// ---------- 事件绑定 ----------
// 主题：页面一加载就应用（登录前也生效）
renderSwatches();
applyTheme(getTheme());

$("loginBtn").onclick = doLogin;
$("refreshBtn").onclick = manualRefresh;
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

// ---------- 代理设置 ----------
function proxyNotice(msg, cls = "ok") {
  const el = $("proxyNotice");
  if (el) el.innerHTML = msg ? `<div class="notice ${cls}">${msg}</div>` : "";
}
// 手动地址输入框仅在 manual 模式下显示
function syncProxyModeUI() {
  const mode = document.querySelector('input[name="proxyMode"]:checked')?.value || "system";
  const f = $("proxyManualField");
  if (f) f.style.display = mode === "manual" ? "" : "none";
}
// 启动时拉一次 /api/config：代理设置 + 版本号（版本唯一真源在 backend/version.py，
// 前端不写死，免得又出现"关于页 1.0.2 / APK 1.7"各说各话）。
async function loadConfig() {
  try {
    const cfg = await api("/api/config");
    const mode = cfg.proxyMode || "system";
    const radio = document.querySelector(`input[name="proxyMode"][value="${mode}"]`);
    if (radio) radio.checked = true;
    if ($("proxyInput")) $("proxyInput").value = cfg.proxy || "";
    syncProxyModeUI();
    if ($("aboutVer") && cfg.version) $("aboutVer").textContent = `v${cfg.version}`;
  } catch { /* 忽略：服务器未就绪时静默 */ }
}
async function saveProxy() {
  const mode = document.querySelector('input[name="proxyMode"]:checked')?.value || "system";
  const proxy = $("proxyInput").value.trim();
  $("saveProxyBtn").disabled = true;
  try {
    const cfg = await api("/api/config", { proxyMode: mode, proxy });
    const eff = cfg.proxyEffective || "（直连）";
    const modeLabel = { system: "跟随系统代理", manual: "手动指定", direct: "直连" }[cfg.proxyMode];
    proxyNotice(`已保存：${modeLabel}，当前生效代理：${eff || "直连"}（下次登录/请求生效）`, "ok");
    log(`代理模式：${modeLabel}，生效地址：${eff || "直连"}`, "ok");
  } catch (e) {
    proxyNotice(`保存失败：${e.message}`, "err");
  } finally {
    $("saveProxyBtn").disabled = false;
  }
}
if ($("saveProxyBtn")) $("saveProxyBtn").onclick = saveProxy;
document.querySelectorAll('input[name="proxyMode"]').forEach((r) =>
  r.addEventListener("change", syncProxyModeUI)
);
loadConfig();

// ---------- 自助更新 ----------
let updateState = null;
let updateAutoShownVersion = "";
let updateStatusPending = false;
let updateProgressPollToken = 0;
let updateProgressPollTimer = null;

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
  installButton.disabled = !state.artifactAvailable || !state.installSupported || !!state.installing;
  installButton.textContent = state.installing ? "正在准备…" : "立即更新";
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
  if (updateState?.forced) return;
  $("updateModal")?.classList.add("hidden");
  document.body.classList.remove("update-dialog-open");
}

async function refreshUpdateStatus(autoShow = false) {
  if (updateStatusPending) return;
  updateStatusPending = true;
  try {
    const state = await api("/api/update/status");
    updateState = state;
    renderUpdateAvailability(state);
    const shouldShow = state.available && (
      state.forced || (autoShow && !state.ignored && updateAutoShownVersion !== state.latestVersion)
    );
    if (shouldShow) {
      updateAutoShownVersion = state.latestVersion;
      openUpdateDialog(state);
    } else if (!state.available && !$("updateModal")?.classList.contains("hidden")) {
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
  if (!updateState?.available || !updateState.artifactAvailable) return;
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
  refreshUpdateStatus(true);
  setTimeout(() => refreshUpdateStatus(true), 4000);
  setTimeout(() => refreshUpdateStatus(true), 18000);
  setInterval(() => refreshUpdateStatus(true), 60 * 60 * 1000);
}

// ---------- 开发者日志 ----------
async function copyDevLog() {
  const btn = $("copyDevLogBtn");
  if (!btn || btn.disabled) return;
  const original = btn.textContent;
  btn.disabled = true;
  try {
    const r = await api("/api/logs");
    await writeClipboardText(r.content || "（暂无日志）");
    btn.textContent = "已复制";
    btn.classList.add("copy-success");
  } catch (e) {
    btn.textContent = "复制失败";
    btn.classList.add("copy-failed");
  } finally {
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = original;
      btn.classList.remove("copy-success", "copy-failed");
    }, 1200);
  }
}
if ($("copyDevLogBtn")) $("copyDevLogBtn").onclick = copyDevLog;

// ---------- 开发者模式 · 协议控制台 ----------
// 入口靠口令解锁：设置页底部一个无提示输入框（#devGate），输对了控制台就出现，
// 输错了什么都不发生（没有确认按钮、没有报错文案，这是刻意的）。
// 口令**不写在前端**，输入的内容交后端 /api/dev/unlock 校验（真源 config.DEV_PASSPHRASE）。
// ⚠️ 隐藏 UI 不是门禁：真正的门禁是 /api/dev/call 每次都要带口令，由后端验。
const DEV_PRESETS = [
  { v: "", label: "常用 route…" },
  { v: "StoreHandler.ResetRandomStore", label: "刷新秘密商店",
    data: { StoreID: "SecretShop", IsUseGold: 0 } },
  { v: "AccountHandler.Login", label: "登录（拉全量快照）", data: {} },
  // 竞技场档期查询：只要 AID+SessionID，**不扣旗帜**（响应无 CostItems）。
  // 返回 PVPData.NPCPVPInfoList 共 30 条 = 10 个 NPC × 普通/困难/地狱三难度。
  { v: "PVPHandler.QueryPVPData", label: "查竞技场档期（不扣旗帜）", data: {} },
  { v: "SupportFriendHandler.QueryBattleSupportDataList", label: "查助战好友", data: {} },
  { v: "TimingMealHandler.SentMeal", label: "投递定时邮件（月卡/餐食）", data: {} },
  { v: "MailHandler.QueryNewestMails", label: "查邮件", data: { MailID: "" } },
  { v: "ServerStatusHandler.Query", label: "服务器状态/计时器", data: {} },
];

// 记住上次输入（按账号），免得每次重开都要重敲
const devLsKey = () => `ark_dev_console::${activeAccount || "_"}`;
function devSaveInput() {
  try {
    localStorage.setItem(devLsKey(), JSON.stringify({
      route: $("devRoute").value, data: $("devData").value,
      useAuth: $("devUseAuth").checked,
    }));
  } catch { /* localStorage 满/禁用：不影响功能 */ }
}
function devLoadInput() {
  try {
    const v = JSON.parse(localStorage.getItem(devLsKey())) || {};
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

let DEV_LAST = null;   // 供"复制收发"用
let DEV_HISTORY = [];
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
  list.innerHTML = DEV_HISTORY.slice(0, 5).map((item, index) => {
    const fields = Object.keys(item.data || {}).length;
    const meta = [devHistoryTime(item.timestamp), `${fields} 个字段`, item.useAuth ? "自动鉴权" : "无鉴权"]
      .filter(Boolean).join(" · ");
    return `<div class="dev-history-item">
      <div class="dev-history-copy">
        <strong title="${filterEscape(item.route || "")}">${filterEscape(item.route || "未知 route")}</strong>
        <small>${filterEscape(meta)}</small>
      </div>
      <div class="dev-history-actions">
        <button type="button" class="mini dev-history-loop" data-history-loop-index="${index}">加入循环</button>
        <button type="button" class="mini dev-history-import" data-history-import-index="${index}">导入</button>
      </div>
    </div>`;
  }).join("");
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
    DEV_HISTORY = Array.isArray(result.history) ? result.history.slice(0, 5) : [];
    DEV_HISTORY_ACCOUNT = email;
    renderDevHistory();
  } catch { /* 会话切换或退出时静默等待下一轮 */ }
  finally { devHistoryPending = false; }
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

function devCopy() {
  if (!DEV_LAST) { $("devMeta").innerHTML = `<span class="bad">还没有可复制的收发</span>`; return; }
  const txt = [
    `route: ${DEV_LAST.route}`,
    `--- 请求 ---`,
    JSON.stringify(DEV_LAST.request, null, 2),
    `--- 响应 (HTTP ${DEV_LAST.status}, ${DEV_LAST.isJson ? "JSON" : "明文"}`
      + `${DEV_LAST.gzip ? ", gzip" : ""}, ${DEV_LAST.rawLen} 字节) ---`,
    DEV_LAST.rawText,
  ].join("\n");
  navigator.clipboard.writeText(txt).then(
    () => { $("devMeta").innerHTML = `<span class="ok">已复制收发到剪贴板</span>`; },
    () => { $("devMeta").innerHTML = `<span class="bad">复制失败（剪贴板不可用）</span>`; }
  );
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

// 显隐整张卡片：以本次运行有没有输过正确口令为准（进程重启即回到隐藏）
function renderDevConsole() {
  const card = $("devConsoleCard");
  if (!card) return;
  const on = !!devPass();
  card.style.display = on ? "" : "none";
  if (on && !card.dataset.bound) {
    card.dataset.bound = "1";
    $("devPreset").innerHTML = dropdownHTML(
      "", DEV_PRESETS.map((p) => ({ v: p.v, label: p.label })), "", "常用 route…");
    bindDropdown($("devPreset"), (v) => {
      const p = DEV_PRESETS.find((x) => x.v === v);
      if (!p || !p.v) return;
      $("devRoute").value = p.v;
      $("devData").value = JSON.stringify(p.data || {}, null, 2);
      devCheckJson();
      devSaveInput();
    });
    $("devSendBtn").onclick = devSend;
    $("devCopyBtn").onclick = devCopy;
    $("devLoopAddBtn").onclick = devAddCurrentToLoop;
    $("devLoopStartBtn").onclick = devStartLoop;
    $("devLoopStopBtn").onclick = devStopLoop;
    $("devHistoryList").addEventListener("click", (event) => {
      const importButton = event.target.closest("[data-history-import-index]");
      const loopButton = event.target.closest("[data-history-loop-index]");
      if (importButton) devImportHistory(Number(importButton.dataset.historyImportIndex));
      if (loopButton) devAddLoopRequest(DEV_HISTORY[Number(loopButton.dataset.historyLoopIndex)]);
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
      DEV_HISTORY_ACCOUNT = activeAccount || "";
      renderDevHistory();
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
  try { localStorage.removeItem("ark_dev_pass"); } catch { /* 禁用 localStorage 也无妨 */ }
  renderDevConsole();
}

// ---------- 工具箱预览 ----------
function showToolboxHome() {
  const home = $("toolboxHome");
  const page = $("equipmentFilterPage");
  if (home) home.classList.remove("hidden");
  if (page) page.classList.add("hidden");
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
    const saved = JSON.parse(localStorage.getItem(EQUIP_FILTER_LS));
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

function saveEquipmentFilterSchemes() {
  try { localStorage.setItem(EQUIP_FILTER_LS, JSON.stringify(EQUIP_FILTER_SCHEMES_DATA)); } catch { /* 无本地存储时仍可预览 */ }
  const optionSignature = JSON.stringify(EQUIP_FILTER_SCHEMES_DATA.map((scheme) => [scheme.id, scheme.name]));
  if (optionSignature !== equipmentFilterSchemeOptionSignature) {
    equipmentFilterSchemeOptionSignature = optionSignature;
    refreshFilterSchemeDropdowns();
  }
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
      <span class="mix-radio"></span><span>混合池中${filterNumberText(count)}选${filterNumberText(required)}</span>
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
        <button type="button" data-scheme-action="rename" title="重命名" aria-label="重命名"><span class="scheme-action-icon">✎</span></button>
        <button type="button" data-scheme-action="import" title="导入方案" aria-label="导入方案"><span class="scheme-action-icon">↥</span></button>
        <button type="button" data-scheme-action="export" title="导出方案到剪贴板" aria-label="导出方案到剪贴板"><span class="scheme-action-icon">↧</span></button>
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
  beginEquipmentFilterRename(scheme.id);
}

function beginEquipmentFilterRename(schemeId) {
  const scheme = EQUIP_FILTER_SCHEMES_DATA.find((item) => item.id === schemeId);
  const item = [...document.querySelectorAll(".scheme-item")].find((el) => el.dataset.schemeId === schemeId);
  const button = item?.querySelector(".scheme-select");
  if (!scheme || !button) return;
  const input = document.createElement("input");
  input.className = "scheme-rename-input";
  input.value = scheme.name;
  input.maxLength = 32;
  input.setAttribute("aria-label", "方案名称");
  button.replaceWith(input);
  input.focus();
  input.select();
  let finished = false;
  const finish = (save) => {
    if (finished) return;
    finished = true;
    const value = input.value.trim();
    if (save && value) scheme.name = value;
    saveEquipmentFilterSchemes();
    renderEquipmentFilterWorkspace();
  };
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") input.blur();
    if (event.key === "Escape") finish(false);
  });
}

function equipmentFilterExportPayload(scheme) {
  return {
    format: EQUIP_FILTER_EXPORT_FORMAT,
    version: EQUIP_FILTER_EXPORT_VERSION,
    scheme,
  };
}

async function writeClipboardText(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.cssText = "position:fixed;left:-9999px;top:0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("clipboard unavailable");
}

function showSchemeActionFeedback(button, ok, label) {
  if (!button) return;
  clearTimeout(button._feedbackTimer);
  const icon = button.querySelector(".scheme-action-icon");
  const originalIcon = button.dataset.originalIcon || icon?.textContent || "";
  button.dataset.originalIcon = originalIcon;
  button.dataset.originalTitle ||= button.title;
  button.classList.remove("action-success", "action-error");
  void button.offsetWidth;
  button.classList.add(ok ? "action-success" : "action-error");
  button.title = label;
  if (icon) icon.textContent = ok ? "✓" : "!";
  button._feedbackTimer = setTimeout(() => {
    if (!button.isConnected) return;
    button.classList.remove("action-success", "action-error");
    button.title = button.dataset.originalTitle;
    if (icon) icon.textContent = originalIcon;
  }, 1300);
}

async function exportEquipmentFilterScheme(schemeId, button) {
  const scheme = EQUIP_FILTER_SCHEMES_DATA.find((item) => item.id === schemeId);
  if (!scheme) return;
  try {
    const payload = JSON.stringify(equipmentFilterExportPayload(scheme), null, 2);
    await writeClipboardText(payload);
    showSchemeActionFeedback(button, true, "已复制到剪贴板");
  } catch {
    showSchemeActionFeedback(button, false, "复制失败，请检查剪贴板权限");
  }
}

let schemeImportTargetId = null;
let schemeImportReturnFocus = null;
let confirmDialogResolve = null;
let confirmDialogReturnFocus = null;

function openConfirmDialog({ title, message, confirmLabel = "确认删除", returnFocus = document.activeElement }) {
  const modal = $("confirmModal");
  if (!modal) return Promise.resolve(false);
  if (confirmDialogResolve) closeConfirmDialog(false);
  $("confirmTitle").textContent = title;
  $("confirmMessage").textContent = message;
  $("confirmAction").textContent = confirmLabel;
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

function setSchemeImportError(message = "") {
  const output = $("schemeImportError");
  if (output) output.textContent = message;
}

async function openEquipmentFilterImport(schemeId, returnFocus) {
  const scheme = EQUIP_FILTER_SCHEMES_DATA.find((item) => item.id === schemeId);
  const modal = $("schemeImportModal");
  if (!scheme || !modal) return;
  schemeImportTargetId = schemeId;
  schemeImportReturnFocus = returnFocus || null;
  $("schemeImportTarget").textContent = "将覆盖“" + scheme.name + "”的名称和全部规则";
  $("schemeImportText").value = "";
  setSchemeImportError();
  modal.classList.remove("hidden");
  document.body.classList.add("scheme-import-open");
  $("schemeImportText").focus();
  try {
    const clipboardText = await navigator.clipboard?.readText?.();
    if (clipboardText && schemeImportTargetId === schemeId && !$("schemeImportText").value.trim()) {
      $("schemeImportText").value = clipboardText;
      $("schemeImportText").select();
    }
  } catch { /* 浏览器未授权读取时由用户手动粘贴 */ }
}

function closeEquipmentFilterImport() {
  const modal = $("schemeImportModal");
  if (!modal || modal.classList.contains("hidden")) return;
  modal.classList.add("hidden");
  document.body.classList.remove("scheme-import-open");
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

function importEquipmentFilterScheme() {
  const index = EQUIP_FILTER_SCHEMES_DATA.findIndex((item) => item.id === schemeImportTargetId);
  if (index < 0) return;
  const text = $("schemeImportText")?.value.trim();
  if (!text) {
    setSchemeImportError("请先粘贴方案 JSON");
    $("schemeImportText")?.focus();
    return;
  }
  try {
    const source = parseEquipmentFilterImport(text);
    const targetId = EQUIP_FILTER_SCHEMES_DATA[index].id;
    EQUIP_FILTER_SCHEMES_DATA[index] = normalizeEquipmentFilterScheme(source, index, targetId);
    activeFilterSchemeId = targetId;
    activeDetailRuleId = null;
    saveEquipmentFilterSchemes();
    closeEquipmentFilterImport();
    renderEquipmentFilterWorkspace();
    const importedItem = [...document.querySelectorAll(".scheme-item")].find((item) => item.dataset.schemeId === targetId);
    importedItem?.classList.add("scheme-imported");
    setTimeout(() => importedItem?.classList.remove("scheme-imported"), 900);
  } catch (error) {
    setSchemeImportError(error.message || "导入失败，请检查方案内容");
  }
}

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
  const open = $("toolboxHome")?.querySelector('[data-tool="equipment-filter"]');
  if (!home || !page || !open || open.dataset.bound) return;
  open.dataset.bound = "1";
  open.addEventListener("click", () => {
    home.classList.add("hidden");
    page.classList.remove("hidden");
    renderEquipmentFilterWorkspace();
  });
  $("toolboxBack")?.addEventListener("click", showToolboxHome);
  $("schemeAdd")?.addEventListener("click", addEquipmentFilterScheme);
  $("schemeList")?.addEventListener("click", async (event) => {
    const action = event.target.closest("[data-scheme-action]");
    const item = event.target.closest(".scheme-item");
    if (!item) return;
    const schemeId = item.dataset.schemeId;
    if (action?.dataset.schemeAction === "rename") {
      beginEquipmentFilterRename(schemeId);
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
    if (event.target.closest(".scheme-rename-input")) return;
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
  $("schemeImportModal")?.addEventListener("click", (event) => {
    if (event.target.closest("[data-import-close]")) closeEquipmentFilterImport();
  });
  $("schemeImportConfirm")?.addEventListener("click", importEquipmentFilterScheme);
  $("schemeImportText")?.addEventListener("input", () => setSchemeImportError());
  $("confirmModal")?.addEventListener("click", (event) => {
    if (event.target.closest("[data-confirm-cancel]")) closeConfirmDialog(false);
  });
  $("confirmAction")?.addEventListener("click", () => closeConfirmDialog(true));
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!$("confirmModal")?.classList.contains("hidden")) {
      event.preventDefault();
      closeConfirmDialog(false);
    } else if (!$("schemeImportModal")?.classList.contains("hidden")) {
      event.preventDefault();
      closeEquipmentFilterImport();
    }
  });
}

// ---------- 启动：显示登录页，等待用户选择账号 ----------
initBottomLogDock();
initAccounts();
initDevGate();
initToolbox();
initEquipCardShells();
initUpdateUI();

