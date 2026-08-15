// LucimaTools 前端逻辑
const $ = (id) => document.getElementById(id);
// 这些接口不绑定单个账号（全局或自带 account），api() 不自动注入 activeAccount
const _GLOBAL_PATHS = new Set(["/api/config", "/api/logs", "/api/tasks", "/api/accounts", "/api/login", "/api/login/direct", "/api/login/token", "/api/accounts/logout", "/api/accounts/delete"]);
const api = async (path, body) => {
  // POST：向 body 注入 account（未显式给且非全局接口时）
  if (body && typeof body === "object" && !("account" in body) && activeAccount && !_GLOBAL_PATHS.has(path)) {
    body = { ...body, account: activeAccount };
  }
  // GET：非全局接口带上 ?account=
  let url = path;
  if (!body && activeAccount && !_GLOBAL_PATHS.has(path)) {
    url += (path.includes("?") ? "&" : "?") + "account=" + encodeURIComponent(activeAccount);
  }
  const opt = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const res = await fetch(url, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
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
  const mini = $("loginThemeBtn"); if (mini) mini.textContent = t.mode === "dark" ? "🌙" : "☀️";
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
const SECTION_TITLES = { info: "基本信息", auto: "自动任务", manual: "主动任务", logs: "运行日志", settings: "系统设置", about: "关于我们" };
let currentSection = "info";
let logUnread = 0;
function showSection(id) {
  currentSection = id;
  document.querySelectorAll(".section").forEach((s) => s.classList.toggle("active", s.id === `section-${id}`));
  document.querySelectorAll("#sideNav .nav-item").forEach((n) => n.classList.toggle("active", n.dataset.section === id));
  $("crumb").textContent = SECTION_TITLES[id] || "";
  if (id === "logs") { logUnread = 0; updateLogBadge(); }
  // 移动端选完自动收起侧栏 + 隐藏遮罩
  if (window.innerWidth <= 820) {
    $("sidebar").classList.add("collapsed");
    $("sidebarBackdrop").classList.remove("show");
  }
}
function updateLogBadge() {
  const b = $("logBadge"); if (!b) return;
  if (logUnread > 0) { b.textContent = logUnread > 99 ? "99+" : logUnread; b.classList.remove("hidden"); }
  else b.classList.add("hidden");
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
// 写日志到指定账号（默认当前账号）。email 无对应账号时退化为直接写 DOM（登录前）。
function logTo(email, msg, cls = "info", ts = null) {
  const a = ACCOUNTS[email];
  const line = _logLine(msg, cls, ts);
  if (a) {
    a.logHtml = (a.logHtml || "") + line;
    if (email === activeAccount) {
      const el = $("log");
      if (el) { el.innerHTML = a.logHtml; el.scrollTop = el.scrollHeight; }
      if (currentSection !== "logs") { logUnread++; updateLogBadge(); }
    } else {
      a.logUnread = (a.logUnread || 0) + 1;
      renderAccountSwitcher();
    }
  } else {
    const el = $("log");
    if (el) { el.innerHTML += line; el.scrollTop = el.scrollHeight; }
  }
}
function log(msg, cls = "info") { logTo(activeAccount, msg, cls); }

// ---------- 登录 ----------
// 登录卡片的单一状态框：合并了原"报错提示"和"验证码说明"两个框。
// kind: hint(默认引导) / pending(登录中) / ok(成功) / err(报错)。
const LOGIN_HINT =
  '点击登录后会弹出一个小窗口，<b>你只需点一下里面的验证码</b>，随后自动完成登录。';
function loginStatus(html, kind = "hint") {
  const box = $("loginStatus");
  if (!box) return;
  // 不传内容 → 回到默认引导态
  box.innerHTML = html || LOGIN_HINT;
  box.className = "login-status " + (html ? kind : "hint");
}

// Android 端由 Kotlin 注入 window.ArkBridge；桌面端为 undefined。
const isAndroid = () => !!window.ArkBridge;

// Android 登录：Kotlin 在原生 WebView 里完成验证码 → 拿 token → 回调本函数。
// 用 Promise 包裹，由 window.__androidLoginDone(resultJson) resolve/reject。
function androidLogin(account, password, savePwd) {
  return new Promise((resolve, reject) => {
    window.__androidLoginDone = (payload) => {
      try {
        const data = typeof payload === "string" ? JSON.parse(payload) : payload;
        if (data && data.ok === false) reject(new Error(data.detail || "登录失败"));
        else resolve(data);
      } catch (e) { reject(e); }
    };
    try {
      // 新桥签名带 savePwd；旧版 Kotlin 只收 2 参也能兼容（多余实参被忽略）
      window.ArkBridge.startLogin(account, password, savePwd ? 1 : 0);
    } catch (e) { reject(e); }
  });
}

// 登录/添加账号：提交账密（+是否保存密码）→ 弹验证码 → 建立会话 → 登记账号。
async function doLogin() {
  const account = $("account").value.trim();
  // 密码框还是圆点占位（用户没改过）→ 当作空密码，后端回退用存档密码
  let password = $("password").value;
  if (password === PWD_MASK) password = "";
  const savePwd = $("savePwd") ? $("savePwd").checked : false;
  if (!account) return loginStatus("请填写账号", "err");
  // 密码可空：仅当该账号在后端已保存密码时（后端会回退用存档密码）
  if (!password && !hasSavedPwd(account)) return loginStatus("请填写密码", "err");
  loginStatus("即将弹出验证窗口，请在窗口中点击验证码…", "pending");
  $("loginBtn").disabled = true;
  $("loginBtn").textContent = "等待验证码…";
  try {
    log(`门户登录 ${account}（等待验证码）...`);
    const data = isAndroid()
      ? await androidLogin(account, password, savePwd)
      : await api("/api/login", { account, password, savePwd });
    // 抓到验证码、后端连上：先亮成功态并让出一帧，用户看得到再切进应用
    loginStatus("登录成功，正在加载账号信息…", "ok");
    await new Promise((r) => setTimeout(r, 500));
    registerAccount(data);
    log(`账号 ${data.account} 已连接`, "ok");
  } catch (e) {
    loginStatus(`登录失败：${e.message}`, "err");
  } finally {
    $("loginBtn").disabled = false;
    $("loginBtn").textContent = "登录";
    // 登录成功时视图已切走无所谓；失败时若该账号有存档密码，恢复圆点占位便于重试
    $("password").value = hasSavedPwd(account) ? PWD_MASK : "";
  }
}

// ---------- 账号登记 / 切换 / 移除 ----------
// 登录成功后把返回负载登记进 ACCOUNTS，并切到该账号。
function registerAccount(data) {
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
  enterApp();
  switchAccount(email);
  // 登录时跑的任务结果写进该账号日志。两类混在一起，各带自己的前缀（后端下发）：
  // "自动·"=必跑项（月签/活动签/助战/月卡），"登录·"=已开启的自动任务开关到点跑的那些。
  (data.autoResults || []).forEach((r) => logResult(r, r.prefix || "自动·", email));
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
  const a = ACCOUNTS[email];
  a.logUnread = 0;
  NEXT_CLAIMS = a.nextClaims || {};
  renderStatus(a.status || {});
  loadRoster(email);           // 团员/物品清单（按账号缓存，首次拉取）
  loadTasks();                 // 重渲染开关/手动面板（反映该账号的 toggles/params）
  // 恢复该账号日志到 #log
  const el = $("log"); if (el) { el.innerHTML = a.logHtml || ""; el.scrollTop = el.scrollHeight; }
  logUnread = 0; updateLogBadge();
  renderAccountSwitcher();
  renderDevConsole();          // 控制台的 route/data 输入是按账号记的，切号要重载
  showSection("info");
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
    $("appView").classList.add("hidden");
    $("loginView").classList.remove("hidden");
    $("loginCancel") && $("loginCancel").classList.add("hidden");
    const el = $("log"); if (el) el.innerHTML = "";
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

  // 货币列：金币 / 钻石 / 三种契约（图标 + 数量，一列）
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

// 拉取某账号的团员/物品清单（按账号缓存，避免重复请求）。
async function loadRoster(email, force = false) {
  const a = ACCOUNTS[email];
  if (!a) return;
  if (a.roster && !force) { if (email === activeAccount) renderRoster(a.roster); return; }
  try {
    const r = await api("/api/roster");
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

  // 团员小卡片：头像为主，左上角等级，右下角潜能等级，下方名字
  $("rosterHeroes").innerHTML = heroes.length ? heroes.map((h) => {
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
  }).join("") : `<div class="roster-empty">暂无团员</div>`;

  // 物品紧凑格：小图标 + 数量（名字作 hover 提示）
  $("rosterItems").innerHTML = items.length ? items.map((it) => `
    <div class="item-tile r${it.rank || 0}" title="${it.name || it.id}">
      <div class="item-ic"><img src="/static/assets/items/${it.id}.png" alt="" loading="lazy"
        onerror="this.style.display='none';this.parentNode.classList.add('noimg');this.parentNode.dataset.t=('${(it.name || it.id)}').slice(0,2)"></div>
      <div class="item-cnt">${fmtNum(it.count)}</div>
    </div>`).join("") : `<div class="roster-empty">暂无物品</div>`;

  // 应用当前 tab 可见性
  applyRosterTab();
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
const TASK_ICONS = {
  month_signin: "📅", week_signin: "🎁", claim_mail: "✉️", friend_support: "🤝",
  reactor: "⚡", sf_potion: "🧪", sf_starforce: "⭐", sf_tesseract: "🔷",
  dispatch: "🎖️", shop_free: "🛒", shop_diamond: "💎", hunt_sweep: "⚔️", element_sweep: "🔮",
  activity_scene: "🎫", abyss_sweep: "🌌", free_summon: "🎰", store_buy: "🛍️",
};
// 部分任务改用游戏内物品图标（覆盖上面的 emoji）：动力转换=钻石、成长药剂=传说成长药剂、
// 升星水晶=传说升星水晶、技能模块=技能模块、派遣任务=荣誉勋章、
// 秘密商店刷新(免费)=金币、虚拟幻境=星源粉末、每日免费招募=招募契约、商店购买=荣誉勋章、
// 竞技场NPC=旗帜（挑战就是扣旗帜，与基本信息页旗帜表盘同一个 StaticID 4）
const TASK_ICON_IMG = {
  reactor: "2", sf_potion: "103", sf_starforce: "106",
  sf_tesseract: "34", dispatch: "26", shop_free: "1",
  abyss_sweep: "25", free_summon: "5", store_buy: "26",
  arena_npc: "4",
};
// 生成任务图标 HTML：有物品图标用 <img>，否则回退 emoji/◆
function taskIconHTML(id, fallback = "◆") {
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
let autoLogTimer = null;       // 后台日志轮询（每 8s，拉所有在线账号的新巡检日志）
const AUTO_LOG_POLL_MS = 8 * 1000;
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

// 自动功能面板：开关 + 购买目标
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
      // 带展开页的开关（目前只有竞技场 ui:"arena"）：外层必须是 div 而不是 label
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
      return `<label class="auto-item ${on ? "on" : ""}" data-id="${t.id}">
        ${inner}
        <input type="checkbox" class="switch-in" ${on ? "checked" : ""}>
        <div class="switch"></div>
      </label>`;
    })
    .join("");

  // 购买目标勾选
  const wantedKeys = getWantedKeys();
  $("btOpts").innerHTML = BUY_TARGETS.map(
    (b) => `<label class="bt-opt">
      <input type="checkbox" value="${b.key}" ${wantedKeys.includes(b.key) ? "checked" : ""}>
      <span>${b.label}</span>
    </label>`
  ).join("");

  // 开关事件：更新当前账号缓存 + 同步后端 + 开启时立即跑一次
  document.querySelectorAll("#autoList .auto-item").forEach((el) => {
    const box = el.querySelector(".switch-in");
    box.addEventListener("change", () => {
      el.classList.toggle("on", box.checked);
      onToggleChange(el.dataset.id, box.checked);
    });
  });
  // 带展开页的开关（竞技场）：绑定"选项"按钮 + 展开页内的交互
  toggle.forEach((t) => { if (t.ui === "arena") bindArenaWidget(t.id); });
  // 购买目标事件
  document.querySelectorAll("#btOpts input").forEach((i) =>
    i.addEventListener("change", () => {
      setWantedKeys([...document.querySelectorAll("#btOpts input:checked")].map((x) => x.value));
    })
  );

  // 只显示购买目标区（当有需要目标的开关存在时）
  const needTargets = toggle.some((t) => t.wantsBuyTargets);
  $("buyTargets").style.display = needTargets ? "" : "none";
  renderNextClaims();
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
  try {
    const { results } = await api("/api/tasks/auto", { taskIds: ids, params: toggleParams(ids) });
    results.forEach((r) => {
      if (quiet && r.skipped) return;
      logResult(r, prefix);
    });
  } catch (e) {
    log(`自动任务执行失败：${e.message}`, "err");
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
      if (t.ui === "sweep") return renderSweepWidget(t);
      if (t.ui === "activity") return renderActivityWidget(t);
      if (t.ui === "storebuy") return renderStoreWidget(t);
      // 普通任务：名称+描述在左，启动按钮在右（不再多选+统一执行）
      return `<div class="task-row">
        <div class="ticon">${TASK_ICONS[t.id] || "◆"}</div>
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
const SHOP_LS = "ark_shop_diamond_cfg";  // {count, wanted:[key], legOn, legMode}
const shopLsKey = () => `${SHOP_LS}::${activeAccount || "_"}`;
// 缺省全选：从后端下发的目标表推导，别再写死 key 列表
// （后端加/改目标项时这里会自动跟上，不会悄悄漏掉新项）
const allTargetKeys = () => BUY_TARGETS.map((b) => b.key);
function getShopCfg() {
  try {
    const v = JSON.parse(localStorage.getItem(shopLsKey())) || {};
    return {
      count: v.count || 10,
      wanted: Array.isArray(v.wanted) ? v.wanted : allTargetKeys(),
      legOn: !!v.legOn,
      legMode: v.legMode === "auto" ? "auto" : "pause",
    };
  } catch {
    return { count: 10, wanted: allTargetKeys(), legOn: false, legMode: "pause" };
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
      <div class="ticon">${TASK_ICONS[t.id] || "💎"}</div>
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
            <div class="leg-mode ${cfg.legOn ? "" : "hidden"}">
              <label class="radio"><input type="radio" name="legmode-${t.id}" value="auto" ${cfg.legMode === "auto" ? "checked" : ""}><span>按规则自动购买（规则待补充）</span></label>
              <label class="radio"><input type="radio" name="legmode-${t.id}" value="pause" ${cfg.legMode === "pause" ? "checked" : ""}><span>暂停刷新，手动逐张选</span></label>
            </div>
          </div>
          <button type="button" class="btn primary shop-start">▶ 开始刷新</button>
        </div>
        <div class="shop-right">
          <div class="card-area" id="cardArea-${t.id}">
            <div class="card-empty">
              <div class="ce-icon">🗃️</div>
              <div class="ce-text">刷到的<b>传说装备</b>会在这里逐张展示<br>用下方 ◀ ▶ 切换，选择购买或跳过</div>
            </div>
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
  const legMode = root.querySelector(".leg-mode");
  legOn.addEventListener("change", () => {
    legMode.classList.toggle("hidden", !legOn.checked);
    setShopCfg({ legOn: legOn.checked });
  });
  root.querySelectorAll(`input[name="legmode-${id}"]`).forEach((r) =>
    r.addEventListener("change", () => setShopCfg({ legMode: r.value }))
  );
  root.querySelector(".shop-start").addEventListener("click", () => runShopDiamond(id));
  // 若当前账号有未完成的传说手选，切回来时恢复卡片（否则显示空占位）
  const a = activeAcc();
  if (a && a.pick && a.pick.id === id) {
    root.querySelector(".shop-body").classList.remove("hidden");
    root.querySelector(".shop-toggle").textContent = "选项 ▾";
    renderPickArea(activeAccount);
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
async function runShopDiamond(id) {
  const email = activeAccount;
  if (shopRunning[email]) return;
  const cfg = getShopCfg();
  const startBtn = $(`shop-${id}`).querySelector(".shop-start");
  shopRunning[email] = true;
  startBtn.disabled = true;
  startBtn.textContent = "刷新中…";
  const wanted = shopWantedIds();
  const gained = {};   // 本次整轮的目标道具产出累计，结束后并进收尾那一条日志
  let endText = "刷新完成";   // 收尾文案：正常跑完/出错两种，与统计合成一句
  let endLv = "ok";
  logTo(email, `${SHOP_TAG} 开始刷新：${cfg.count} 次`);
  try {
    for (let i = 1; i <= cfg.count; i++) {
      const r = await api("/api/shop/reset", { account: email, useGold: true, autoBuy: wanted });
      if (r.status) applyStatus(email, r.status, r.nextClaims);  // 每次刷新/购买后即时更新主页资产
      addShopGain(gained, r.bought);   // 失败中止的那次也可能已买到东西，先累加再判 ok
      if (!r.ok) { logTo(email, `${SHOP_TAG} 第 ${i} 次刷新失败：${r.error || "未知错误"}，停止`, "err"); break; }
      (r.boughtLines || []).forEach((ln) => logTo(email, `${SHOP_TAG} 第 ${i} 次：${ln}`, "ok"));
      const buyErrs = r.buyErrors || [];
      buyErrs.forEach((ln) => logTo(email, `${SHOP_TAG} 第 ${i} 次：${ln}`, "err"));
      // 匹配到目标却没买成(金币不足/售罄等)：停止。继续刷只会白烧钻石——金币不足是
      // 持续约束，下次遇到目标照样买不起；网络错误路径(HTTP 500)本就会 throw 跳出循环。
      if (buyErrs.length) { logTo(email, `${SHOP_TAG} 第 ${i} 次有目标未购成，停止刷新（避免白耗钻石）`, "warn"); break; }
      const legs = r.legendaries || [];
      if (cfg.legOn && legs.length) {
        if (cfg.legMode === "auto") {
          logTo(email, `${SHOP_TAG} 第 ${i} 次：发现 ${legs.length} 件传说装备，自动购买规则待补充，本屏跳过`, "warn");
        } else {
          // 暂停手选：状态存该账号。若此刻不在该账号页面，卡片保留、循环挂起，
          // 切回该账号即可继续逐张选（renderPickArea 恢复）。
          const hint = email === activeAccount
            ? "已暂停 → 在下方卡片逐张选择"
            : "已暂停 → 切回该账号继续在卡片区选择";
          logTo(email, `${SHOP_TAG} 第 ${i} 次：发现 ${legs.length} 件传说装备，${hint}`, "warn");
          await pickLegendaries(id, legs, i, email);
        }
      } else if (!(r.boughtLines || []).length) {
        logTo(email, `${SHOP_TAG} 第 ${i} 次：无目标道具`);
      }
    }
  } catch (e) {
    endText = `刷新出错：${e.message}`;
    endLv = "err";
  } finally {
    // 收尾一条日志 = 完成/出错 + 本轮统计。放 finally 里，提前中止(缺钱停止/出错)
    // 也能看到已获取的东西；一件没买到时只留前半句。
    const summary = shopGainSummary(gained);
    logTo(email, `${SHOP_TAG} ${endText}${summary ? "，" + summary : ""}`, endLv);
    shopRunning[email] = false;
    // 按钮可能已因切账号被重渲染，仅当仍是同一账号面板时复位
    if (email === activeAccount && startBtn.isConnected) {
      startBtn.disabled = false;
      startBtn.textContent = "▶ 开始刷新";
    }
    refreshAccountStatus(email);
  }
}

const CARD_EMPTY = `<div class="card-empty">
  <div class="ce-icon">🗃️</div>
  <div class="ce-text">刷到的<b>传说装备</b>会在这里逐张展示<br>用下方 ◀ ▶ 切换，选择购买或跳过</div>
</div>`;

// 装备详情卡片 HTML（秘密商店手选 + 讨伐掉落弹窗共用同一套模组）
function equipCardHTML(c, stateTag = "") {
  const subs = (c.subProps || [])
    .map((s) => `<div class="pv"><span>${s.label}</span><b>${s.text}</b></div>`)
    .join("");
  return `<div class="eq-card">
    <div class="eq-head">
      <div class="eq-head-left">
        ${c.level ? `<span class="eq-lv">${c.level}</span>` : ""}
        <div class="eq-imgbox">
          ${c.img ? `<img class="eq-img" src="${c.img}" alt="${c.name}">` : ""}
        </div>
      </div>
      <div class="eq-head-right">
        <div class="eq-aux"><span>副属分数</span><b>${c.auxScore != null ? c.auxScore : "—"}</b></div>
        <span class="eq-tier">${c.tierName || ""}${c.slotCN || ""}</span>
        <span class="eq-name">${c.name}</span>
        ${stateTag}
      </div>
    </div>
    <div class="eq-main"><span>${c.mainProp.label}</span><b>${c.mainProp.text}</b></div>
    <div class="eq-subs">${subs}</div>
    <div class="eq-set">
      ${c.setIcon ? `<img class="eq-set-icon" src="${c.setIcon}" alt="">` : ""}
      <span>${c.setCN}套装</span>
    </div>
  </div>`;
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

// ---------- 扫荡自定义控件（讨伐扫荡 / 元素扫荡共用） ----------
// 配置按 任务id + 账号 双重命名空间（多个扫荡任务各自独立；队伍 ID 各账号不同）
const SWEEP_LS = "ark_sweep_cfg"; // {count, sceneId, teamId, quick}
const sweepLsKey = (id) => `${SWEEP_LS}::${id}::${activeAccount || "_"}`;
function getSweepCfg(id) {
  try {
    const v = JSON.parse(localStorage.getItem(sweepLsKey(id))) || {};
    return {
      count: v.count || 5, sceneId: v.sceneId || "", teamId: v.teamId || "",
      quick: v.quick !== false,  // 默认勾选：使用快速战斗券
    };
  } catch {
    return { count: 5, sceneId: "", teamId: "", quick: true };
  }
}
function setSweepCfg(id, patch) {
  localStorage.setItem(sweepLsKey(id), JSON.stringify({ ...getSweepCfg(id), ...patch }));
}
// 关卡下拉标签按扫荡类型区分
const SWEEP_SCENE_LABEL = { hunt: "讨伐关卡", elf: "元素关卡" };

// 元素 -> 主题色（讨伐关卡下拉的色点）
const ELEMENT_COLOR = {
  fire: "#ff6b4a", water: "#5cd0ff", wood: "#4ce68a", dark: "#b98cff", light: "#ffd84a",
};

// 自定义下拉：options=[{val,label,element?}]。返回 HTML 串。
function dropdownHTML(cls, options, curVal, placeholder = "请选择") {
  const cur = options.find((o) => o.val === curVal) || options[0];
  const dot = (el) => (el ? `<span class="dd-dot" style="background:${ELEMENT_COLOR[el] || "var(--accent)"}"></span>` : "");
  const curLabel = cur ? `${dot(cur.element)}${cur.label}` : placeholder;
  const opts = options
    .map(
      (o) => `<div class="dd-opt ${o.val === curVal ? "sel" : ""}" data-val="${o.val}">${dot(o.element)}<span>${o.label}</span></div>`
    )
    .join("");
  return `<div class="dropdown ${cls}" data-val="${cur ? cur.val : ""}">
    <button type="button" class="dd-btn"><span class="dd-cur">${curLabel}</span><span class="dd-caret">▾</span></button>
    <div class="dd-menu hidden">${opts}</div>
  </div>`;
}

// 绑定一个下拉的交互：onPick(val) 回调
function bindDropdown(root, onPick) {
  const btn = root.querySelector(".dd-btn");
  const menu = root.querySelector(".dd-menu");
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    // 关掉其它已打开的下拉
    document.querySelectorAll(".dd-menu").forEach((m) => { if (m !== menu) m.classList.add("hidden"); });
    menu.classList.toggle("hidden");
    root.classList.toggle("open", !menu.classList.contains("hidden"));
  });
  menu.querySelectorAll(".dd-opt").forEach((opt) =>
    opt.addEventListener("click", () => {
      const val = opt.dataset.val;
      root.dataset.val = val;
      root.querySelector(".dd-cur").innerHTML = opt.innerHTML;
      menu.querySelectorAll(".dd-opt").forEach((o) => o.classList.toggle("sel", o === opt));
      menu.classList.add("hidden");
      root.classList.remove("open");
      onPick(val);
    })
  );
}

function renderSweepWidget(t) {
  const cfg = getSweepCfg(t.id);
  const sceneLabel = SWEEP_SCENE_LABEL[t.sweepKind] || "关卡";
  return `<div class="shop-widget sweep-host" id="sweep-${t.id}" data-kind="${t.sweepKind || "hunt"}">
    <div class="shop-head">
      <div class="ticon">${TASK_ICONS[t.id] || "⚔️"}</div>
      <div class="tinfo"><div class="tname">${t.name}</div><div class="tdesc">${t.desc}</div></div>
      <button type="button" class="mini sweep-toggle">选项 ▸</button>
    </div>
    <div class="shop-body hidden">
      <div class="shop-row col">
        <span class="shop-lbl">${sceneLabel}</span>
        <div class="dd-slot sweep-scene-slot"><div class="dd-loading">加载中…</div></div>
      </div>
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
  const sceneSlot = root.querySelector(".sweep-scene-slot");
  const teamSlot = root.querySelector(".sweep-team-slot");
  try {
    const { scenes, teams } = await api(`/api/sweep/options?kind=${encodeURIComponent(kind)}`);
    const cfg = getSweepCfg(id);

    const sceneOpts = scenes.map((s) => ({ val: s.id, label: s.name, element: s.element }));
    const teamOpts = teams.length
      ? teams.map((t) => ({ val: t.id, label: t.name }))
      : [{ val: "", label: "（无已保存的队伍）" }];

    const sceneVal = cfg.sceneId && scenes.some((s) => s.id === cfg.sceneId) ? cfg.sceneId : sceneOpts[0]?.val || "";
    const teamVal = cfg.teamId && teams.some((t) => t.id === cfg.teamId) ? cfg.teamId : teamOpts[0]?.val || "";
    setSweepCfg(id, { sceneId: sceneVal, teamId: teamVal });

    sceneSlot.innerHTML = dropdownHTML("sweep-scene", sceneOpts, sceneVal);
    teamSlot.innerHTML = dropdownHTML("sweep-team", teamOpts, teamVal);
    bindDropdown(sceneSlot.querySelector(".dropdown"), (v) => setSweepCfg(id, { sceneId: v }));
    bindDropdown(teamSlot.querySelector(".dropdown"), (v) => setSweepCfg(id, { teamId: v }));
  } catch (e) {
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
      <div class="ticon">${TASK_ICONS[t.id] || "🎫"}</div>
      <div class="tinfo"><div class="tname">${t.name}</div><div class="tdesc">${t.desc}</div></div>
      <button type="button" class="mini act-toggle">选项 ▸</button>
    </div>
    <div class="shop-body hidden">
      <div class="shop-row col">
        <span class="shop-lbl">活动关卡</span>
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
      root.dataset.loaded = "1";
    }
  });

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

async function loadActivityOptions(id) {
  const root = $(`act-${id}`);
  const sceneSlot = root.querySelector(".act-scene-slot");
  const teamSlot = root.querySelector(".act-team-slot");
  const supSlot = root.querySelector(".act-support-slot");
  try {
    const { scenes, teams, supports, favorites } = await api("/api/activity/options");
    const cfg = getActCfg();

    // 后端只回当期活动（已结束的按排期表过滤掉），可能一个都没有
    const sceneOpts = scenes.length
      ? scenes.map((s) => ({ val: s.id, label: s.name }))
      : [{ val: "", label: "（当前没有进行中的活动）" }];
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
  } catch (e) {
    sceneSlot.innerHTML = `<div class="dd-loading err">加载失败</div>`;
    teamSlot.innerHTML = `<div class="dd-loading err">加载失败</div>`;
    supSlot.innerHTML = `<div class="dd-loading err">加载失败</div>`;
    log(`加载活动关卡选项失败：${e.message}`, "err");
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
      if (confirm(`确定删除账号 ${email}？将下线并清除其保存的密码与开关设置。`)) accDelete(email);
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
  if ($("savePwd")) $("savePwd").checked = false;
  loginStatus("");   // 回到默认引导态
  $("loginView").classList.remove("hidden");
  const cancel = $("loginCancel");
  if (cancel) cancel.classList.toggle("hidden", Object.keys(ACCOUNTS).length === 0);
  renderSavedAccounts();
}

// 登录页"已保存账号"快捷选择：挂在账号输入框上的下拉（有存档才显示 ▾ 按钮），
// 选一个自动填充邮箱（有存密码则留空密码+勾上保存）。比铺一排标签省空间。
let SAVED_ACCTS = [];   // 来自 /api/accounts 的存档列表
// 已保存密码的占位圆点：真实密码不下发前端（后端登录时按账号回退用存档密码），
// 这里往密码框填一段定长占位串，type=password 会把它渲染成小圆点，观感等同"密码已填"。
// 提交/编辑时按是否等于它来区分"用存档密码"还是"用户重新输入的密码"。
const PWD_MASK = "••••••••";
// 该账号在后端是否存了密码（存档列表里 hasPwd 为真）。
function hasSavedPwd(account) {
  return SAVED_ACCTS.some((a) => a.account === account && a.hasPwd);
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
    `<div class="sa-opt" data-email="${a.account}" data-haspwd="${a.hasPwd ? 1 : 0}">
       <span class="sa-em">${a.account}</span>
       <button type="button" class="sa-del" data-del="${a.account}" title="删除已保存的账号">✕</button>
     </div>`).join("");
  box.querySelectorAll(".sa-opt").forEach((row) =>
    row.addEventListener("click", (e) => {
      if (e.target.closest(".sa-del")) return;  // 点叉是删除，不填充
      $("account").value = row.dataset.email;
      if (row.dataset.haspwd === "1") {
        // 显示圆点占位（观感=已填密码），聚焦按钮而非密码框，避免立刻清空
        $("password").value = PWD_MASK;
        $("password").placeholder = "密码";
        if ($("savePwd")) $("savePwd").checked = true;
        $("loginBtn").focus();
      } else {
        $("password").value = "";
        $("password").placeholder = "密码";
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
  if (!confirm(`删除已保存的账号 ${email}？将清除其保存的密码与开关设置。`)) return;
  try { await api("/api/accounts/delete", { account: email }); } catch (e) {}
  SAVED_ACCTS = SAVED_ACCTS.filter((a) => a.account !== email);
  renderSavedAccounts();
}
function toggleSavedMenu() {
  const box = $("savedAccts");
  if (!box || box.innerHTML.trim() === "") return;
  box.classList.toggle("hidden");
}

// 后台日志轮询：每 8s 拉所有在线账号自后端新产生的巡检日志，写进各自缓冲。
function ensureAutoLogPoll() {
  if (autoLogTimer) return;
  autoLogTimer = setInterval(pollAutoLogs, AUTO_LOG_POLL_MS);
  if (!countdownTimer) countdownTimer = setInterval(renderNextClaims, 30 * 1000);
}
async function pollAutoLogs() {
  const emails = Object.keys(ACCOUNTS);
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
    document.querySelectorAll(".dropdown.open").forEach((d) => d.classList.remove("open"));
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

// ---------- 启动：拉账号列表，恢复在线账号 ----------
async function initAccounts() {
  let list = [];
  try { list = (await api("/api/accounts")).accounts || []; } catch (e) {}
  SAVED_ACCTS = list;   // 供登录页"已保存账号"快捷填充
  const online = list.filter((x) => x.online);
  if (online.length) {
    // 恢复所有在线账号的会话到前端（拉各自状态；开关/参数由 /api/accounts 一并带回）
    for (const item of online) {
      try {
        const st = await fetch(`/api/status?account=${encodeURIComponent(item.account)}`).then((r) => r.json());
        ACCOUNTS[item.account] = {
          status: st.status || {}, nextClaims: st.nextClaims || {},
          arena: st.arena || null,
          toggles: item.toggles || {}, params: item.params || {},
          name: item.name, avatar: item.avatar,
          logHtml: "", logSeq: item.logSeq || 0, logUnread: 0,
        };
      } catch (e) {}
    }
    enterApp();
    switchAccount(online[0].account);
    ensureAutoLogPoll();
  } else {
    $("loginView").classList.remove("hidden");
    $("loginCancel") && $("loginCancel").classList.add("hidden");
    renderSavedAccounts();
  }
  renderAccountSwitcher();
}

// ---------- 事件绑定 ----------
// 主题：页面一加载就应用（登录前也生效）
renderSwatches();
applyTheme(getTheme());

$("loginBtn").onclick = doLogin;
$("refreshBtn").onclick = manualRefresh;
$("logoutBtn").onclick = logout;
$("password").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
// 聚焦密码框且仍是圆点占位 → 清空，让用户直接输入新密码
$("password").addEventListener("focus", () => {
  if ($("password").value === PWD_MASK) $("password").value = "";
});
// 离开密码框，若留空且当前账号有存档密码 → 恢复圆点占位（观感=密码仍在）
$("password").addEventListener("blur", () => {
  if (!$("password").value && hasSavedPwd($("account").value.trim())) {
    $("password").value = PWD_MASK;
  }
});
// 改动账号：若密码框还是圆点占位、但新账号并无存档密码 → 清空，避免误导
$("account").addEventListener("input", () => {
  if ($("password").value === PWD_MASK && !hasSavedPwd($("account").value.trim())) {
    $("password").value = "";
  }
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
// 清空日志
$("clearLogBtn").onclick = () => { $("log").innerHTML = ""; logUnread = 0; updateLogBadge(); };
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

// ---------- 开发者日志 ----------
async function loadDevLog() {
  const btn = $("loadDevLogBtn");
  btn.disabled = true;
  try {
    const r = await api("/api/logs");
    $("devLogPath").textContent = r.path || "";
    const pre = $("devLog");
    pre.textContent = r.content || "（暂无日志）";
    pre.style.display = "";
  } catch (e) {
    $("devLogPath").textContent = `读取失败：${e.message}`;
  } finally {
    btn.disabled = false;
  }
}
if ($("loadDevLogBtn")) $("loadDevLogBtn").onclick = loadDevLog;

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
    DEV_LAST = r;
    $("devReq").textContent = JSON.stringify(r.request, null, 2);
    // 响应：是 JSON 就美化，不是就**原样**显示（抓明文报文正是要看原始形态）
    $("devResp").textContent = r.isJson
      ? JSON.stringify(r.json, null, 2)
      : r.rawText;
    const kind = r.isJson ? "JSON" : "明文（非 JSON）";
    const trunc = r.rawLen > (r.rawText || "").length ? `，已截断显示` : "";
    $("devMeta").innerHTML =
      `<span class="${r.status === 200 ? "ok" : "bad"}">HTTP ${r.status}</span>`
      + ` · ${kind}${r.gzip ? " · gzip" : ""} · ${r.rawLen} 字节${trunc} · ${r.elapsedMs}ms`;
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
    $("devData").addEventListener("input", devCheckJson);
    $("devRoute").addEventListener("change", devSaveInput);
  }
  if (on) { devLoadInput(); devCheckJson(); }
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

// ---------- 启动：恢复已在线账号 / 显示登录页 ----------
initAccounts();
initDevGate();
