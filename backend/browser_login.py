"""辅助登录：后端用 Playwright 驱动本机 Edge 打开真实 ero-labs 登录页。

- 自动填好账号密码
- 用户只需点一下 Turnstile 验证码（验证码只能在 ero-labs.com 域名下渲染）
- 验证码过后自动点登录，登录成功后从浏览器读出 erolabsjwt cookie
- 使用持久化用户配置目录：登录过一次后 cookie 留存，下次可免验证码直接进
- 去除自动化指纹，减少 Cloudflare 反复挑战
代理按 config.PORTAL_NEEDS_PROXY 决定（默认门户 game.ero-labs.art 可直连，不套代理）。
返回 erolabsjwt。

**失败要看得见**：免验证码探测属可选优化，失败只降级(记 warning)不阻断登录；
必经步骤(打开登录页)失败则抛 BrowserLoginError 带上 URL/耗时/原因，绝不静默。
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import config
from .logutil import log

# 门户站点在 config 里切换（默认 game.ero-labs.art，可直连；旧 www.ero-labs.com 需代理）。
LOGIN_URL = config.PORTAL_LOGIN_PAGE
HOME_URL = config.PORTAL_HOME_PAGE

# 必经导航预算。源站慢且抖——实测同一个 HTML 1.4s~17.3s，旧址还要多一跳 302，
# 原来的 30s 在 2026-08-10 直接被吃满导致登录崩掉。
_NAV_TIMEOUT_MS = 60_000
# 可选探测（该账号是否已登录）：失败就走完整登录，没必要等久。
_PROBE_TIMEOUT_MS = 20_000
# 表单出现（导航已完成，只等 DOM 里那个 input）。
_FORM_TIMEOUT_MS = 20_000

# 拖慢/挂住页面加载的第三方。门户 index.html 里 livechatbot 那个 <script>
# **没有 async/defer**，是实打实阻塞 domcontentloaded 的；GTM/统计同理。它们对
# 登录毫无用处，不可达时却能把 goto 拖到超时。安卓端早就这么修了（见
# LoginActivity.DEAD_HOSTS），桌面这条路一直没加，所以源站一变慢就先崩。
_DEAD_HOST_RE = re.compile(
    r"^https?://([^/]*\.)?(livechatbot\.net|googletagmanager\.com|google-analytics\.com)/"
)

# 持久化浏览器配置目录（保存登录态，下次免验证码）。
# 用 ARK_DATA_DIR（桌面入口设为 exe 同级；源码运行为项目根），避免 PyInstaller
# 冻结后 __file__ 落在临时 _MEIPASS 导致 profile 每次全新、无法持久化。
# 多账号：每个账号一个独立 profile 子目录（cookie 互相隔离，可同时保持多个账号在线），
# 不再共用一个 profile+切号清 cookie。
_DATA_DIR = Path(os.environ.get("ARK_DATA_DIR", Path(__file__).resolve().parent.parent))
_PROFILE_ROOT = _DATA_DIR / ".browser_profiles"


def _safe_dirname(account: str) -> str:
    """把 email 转成安全的目录名（保留可读性，非法字符替换为下划线）。"""
    return "".join(ch if (ch.isalnum() or ch in "-_.@") else "_" for ch in account.strip()) or "default"


def _profile_dir(account: str) -> Path:
    return _PROFILE_ROOT / _safe_dirname(account)

# 去自动化指纹（Cloudflare 反复挑战多半因为检测到 navigator.webdriver 等）
_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--window-size=520,760",
    "--window-position=80,60",
]

# 在 ero-labs 登录页顶部加一个品牌提示条，引导用户点验证码。
# 注意：不做“祖先链隐藏兄弟”式聚光——那会因页面结构差异误伤登录表单
# （曾导致表单整块消失，页面空白）。这里只加提示条，不隐藏任何原有内容，
# 确保账密行 + Turnstile 验证码始终可见可点。
_BANNER = r"""
(() => {
  if (document.getElementById('__ark_banner')) return;
  const d = document.createElement('div');
  d.id = '__ark_banner';
  d.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;padding:14px 16px;'
    + 'background:linear-gradient(90deg,#ff7fac,#a06bff);color:#fff;font-size:14px;'
    + 'font-weight:700;text-align:center;font-family:sans-serif;box-shadow:0 2px 20px rgba(0,0,0,.4)';
  d.innerHTML = 'LucimaTools · 账号密码已自动填好<br><span style="font-weight:500;font-size:12.5px">'
    + '请点击下方的验证码完成人机验证，随后会自动登录…</span>';
  document.body.appendChild(d);
})();
"""

_STEALTH_INIT = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"


class BrowserLoginError(Exception):
    pass


def _read_jwt(context) -> str | None:
    """从整个 context 里找 erolabsjwt，**不限域名**。

    这个"不限域名"是有意的：上游换过两次址(.com → .tech → .art)，cookie 会落在
    当次实际所在的域下。安卓端曾因为写死查旧域名而静默失效（登录成功却卡在资料页），
    桌面这条路正因为遍历全部 cookie 才扛过了换址。别改成按域名过滤。
    """
    for c in context.cookies():
        if c.get("name") == "erolabsjwt" and c.get("value"):
            return c["value"]
    return None


def _one_line(e: Exception) -> str:
    """异常摘要压成一行（Playwright 的报错带一大段 call log，日志里没必要全铺开）。"""
    return (str(e).splitlines() or [""])[0][:200]


def _block_dead_hosts(context) -> None:
    """短路掉 _DEAD_HOST_RE 命中的第三方请求（它们会阻塞 domcontentloaded）。

    只拦这张写死的小名单，不做"慢就拦"之类的启发式——拦错了会把门户自己的资源
    掐掉，那种故障比慢更难查。命中即 abort，并留 debug 日志便于事后核对拦了什么。
    """
    def _handler(route):
        log.debug("辅助登录：拦掉阻塞加载的第三方 %s", route.request.url[:120])
        try:
            route.abort()
        except Exception:
            # 页面已关/请求已走完时 abort 会抛，这里无所谓——它只是个无用的第三方请求。
            pass

    context.route(_DEAD_HOST_RE, _handler)


def interactive_portal_login(account: str, password: str, timeout: float = 240.0) -> dict:
    """弹出浏览器让用户手点验证码，成功后返回 {jwt, account}。

    每个账号用独立 profile 子目录：若该账号此前登录态仍有效，走免验证码快速通道直接返回。
    """
    profile_dir = _profile_dir(account)
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        # 跟随代理配置：有值才给浏览器设代理；为空（直连/系统 TUN）则不设，
        # 由系统网络层处理（TUN 模式下流量仍会被截走）。
        launch_kwargs = dict(
            user_data_dir=str(profile_dir),
            channel="msedge",
            headless=False,
            viewport={"width": 500, "height": 720},
            args=_STEALTH_ARGS,
        )
        # 新门户(game.ero-labs.tech)实测可直连，就不给浏览器套代理（少一跳，
        # 也不依赖用户代理开着）；切回旧入口(www.ero-labs.com)时才需要代理。
        proxy = config.get_proxy() if config.PORTAL_NEEDS_PROXY else ""
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        try:
            context = p.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:
            raise BrowserLoginError(f"无法启动 Edge 浏览器：{e}")

        try:
            context.add_init_script(_STEALTH_INIT)
            _block_dead_hosts(context)
            page = context.pages[0] if context.pages else context.new_page()

            # 1) 该账号 profile 若已有有效登录态，免验证码快速通道。
            # ⚠️ 这步是**可选优化**，失败不该否决整个登录——2026-08-10 就是它把登录
            # 带崩的（源站变慢 + 旧址 302 多一跳吃满 30s，异常直接冒到 /api/login 成 500，
            # 用户连验证码页都看不到）。所以这里降级继续走完整登录，
            # 但**必须留 warning**：静默吞掉的话，"门户又出事了"只会表现为
            # "怎么每次都要手过验证码"，真正的原因被藏起来。
            try:
                page.goto(HOME_URL, wait_until="domcontentloaded",
                          timeout=_PROBE_TIMEOUT_MS)
                page.wait_for_timeout(1200)
                jwt = _read_jwt(context)
                if jwt:
                    return {"jwt": jwt, "account": account}
            except Exception as e:
                log.warning(
                    "辅助登录：免验证码探测失败，改走完整登录（不影响本次登录）"
                    " url=%s %s: %s", HOME_URL, type(e).__name__, _one_line(e)
                )

            # 2) 需要登录：打开登录页，填账密。这步是**必经**的——失败就明确报错，
            # 带上 URL/耗时/原因，别让 Playwright 的裸异常冒成一个看不懂的 500。
            t0 = time.time()
            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded",
                          timeout=_NAV_TIMEOUT_MS)
                page.wait_for_selector("#loginemail", timeout=_FORM_TIMEOUT_MS)
            except Exception as e:
                raise BrowserLoginError(
                    f"打不开门户登录页（已等 {time.time() - t0:.0f}s）：{LOGIN_URL}"
                    f"｜{type(e).__name__}: {_one_line(e)}"
                    f"｜若门户又换址，改 backend/config.py 的 PORTAL_SITE"
                ) from e
            page.fill("#loginemail", account)
            page.fill("#loginpassword", password)
            try:
                page.evaluate(_BANNER)
            except Exception:
                pass

            deadline = time.time() + timeout
            clicked = False
            while time.time() < deadline:
                jwt = _read_jwt(context)
                if jwt:
                    return {"jwt": jwt, "account": account}
                if not clicked:
                    btn = page.query_selector("#loginSubmit")
                    if btn and btn.is_enabled():
                        try:
                            btn.click()
                            clicked = True
                        except Exception:
                            pass
                    else:
                        # 按钮还没启用（验证码未完成），重贴提示条防丢失
                        try:
                            page.evaluate(_BANNER)
                        except Exception:
                            pass
                page.wait_for_timeout(800)

            raise BrowserLoginError("登录超时：未检测到登录凭证（验证码未完成或账号密码错误）")
        finally:
            try:
                context.close()
            except Exception:
                pass
