package com.openrubi.lucimatools

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.webkit.CookieManager
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity
import androidx.webkit.ProxyConfig
import androidx.webkit.ProxyController
import androidx.webkit.WebViewFeature
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.util.concurrent.Executors
import kotlin.concurrent.thread

/**
 * 登录页：WebView 经代理打开 ero-labs 登录页，自动填账密，用户手过验证码，
 * 通过后轮询 CookieManager 拿 erolabsjwt，返回给 MainActivity 做游戏 bootstrap。
 */
class LoginActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private var done = false
    private var account: String = ""
    private var password: String = ""
    /** 账密是否已确认填进表单（读回校验通过）。没填成前轮询会一直重试。 */
    private var filled = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val account = intent.getStringExtra("account") ?: ""
        val password = intent.getStringExtra("password") ?: ""
        val proxy = intent.getStringExtra("proxy") ?: ""

        // 经代理访问 ero-labs（游戏站点有地区限制）；localhost 直连不走代理
        if (proxy.isNotEmpty() && WebViewFeature.isFeatureSupported(WebViewFeature.PROXY_OVERRIDE)) {
            applyProxy(proxy)
        }

        this.account = account
        this.password = password

        webView = WebView(this)
        setContentView(webView)
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
        }
        val cm = CookieManager.getInstance()
        cm.setAcceptCookie(true)
        cm.setAcceptThirdPartyCookies(webView, true)

        webView.webViewClient = object : WebViewClient() {
            // ⚠️ 不要只在 onPageFinished 里填表——它等的是 load 事件，而 load 会等
            // 页面里那个 async 的 googletagmanager 脚本。门户换到 game.ero-labs.tech
            // 后不再套代理，GTM 在国内直连不通 → load 被拖到 40s 后甚至不来，
            // 于是"页面早就显示了却迟迟不自动填账密"。实测(Playwright 模拟同样网络)：
            // 表单 3.7s 就可填，load 要 42.9s，空窗 39s。改为首屏可见即填 + 轮询重试。
            override fun onPageCommitVisible(view: WebView, url: String) {
                tryFill(view, url)
            }

            override fun onPageFinished(view: WebView, url: String) {
                tryFill(view, url)
                // 首页出现即可能已登录，尝试读 cookie
                pollCookie()
            }

            // 把不可达的第三方统统短路成空响应：GTM/统计/客服挂在那儿会拖住整页
            // 加载(load 事件)，对登录毫无用处；顺带也不把登录页行为漏给它们。
            override fun shouldInterceptRequest(
                view: WebView, request: WebResourceRequest
            ): WebResourceResponse? {
                val host = request.url.host ?: return null
                if (DEAD_HOSTS.any { host == it || host.endsWith(".$it") }) {
                    return WebResourceResponse(
                        "text/plain", "utf-8", ByteArrayInputStream(ByteArray(0))
                    )
                }
                return null
            }
        }
        // 关键：先清掉上一个账号残留的 erolabsjwt cookie，再加载登录页。
        // Android CookieManager 是全局持久的——不清的话切换账号时会立刻读到旧
        // 账号的 cookie 直接返回，导致"输了新账密却登进旧账号"。清完(异步)才 loadUrl。
        cm.removeAllCookies { cm.flush(); webView.loadUrl(LOGIN_URL) }
        startCookiePolling()
    }

    private fun applyProxy(proxy: String) {
        // proxy 形如 http://127.0.0.1:7890 -> ProxyConfig 需 host:port
        val hostPort = proxy.removePrefix("http://").removePrefix("https://").trimEnd('/')
        val cfg = ProxyConfig.Builder()
            .addProxyRule(hostPort)
            .addDirect()               // 规则未命中时直连
            .bypassSimpleHostnames()
            .addBypassRule("127.0.0.1")
            .addBypassRule("localhost")
            .build()
        ProxyController.getInstance().setProxyOverride(cfg, Executors.newSingleThreadExecutor()) {}
    }

    /**
     * 注入自动填表。幂等 + 自校验：脚本读回输入框的值，确认填住了才返回 'ok'，
     * 没填住（表单还没渲染 / 被页面脚本重置）返回 'retry'，由轮询继续试。
     */
    private fun tryFill(view: WebView, url: String) {
        if (filled || done) return
        if (!url.contains("login")) return
        view.evaluateJavascript(fillScript(account, password)) { r ->
            if (r != null && r.contains("ok")) filled = true
        }
    }

    /** 定时轮询：拿到 cookie 即返回；没填上账密继续补填；验证码过了自动点登录。 */
    private fun startCookiePolling() {
        thread(isDaemon = true) {
            repeat(400) { // 最多 ~200s
                if (done) return@thread
                Thread.sleep(500)
                runOnUiThread {
                    pollCookie()
                    if (!done) {
                        // 页面可能晚渲染或被自身脚本重置，没确认填住就一直补填
                        if (!filled) tryFill(webView, webView.url ?: "")
                        autoClickSubmit()
                    }
                }
            }
        }
    }

    private var submitClicked = false

    /**
     * 验证码通过后 #loginSubmit 会启用；启用即自动点一次，无需用户手点登录。
     * 但必须等账密真的填进去了才点——否则会拿空表单提交，触发页面自己的
     * "请输入邮箱"校验，白跑一次。
     */
    private fun autoClickSubmit() {
        if (submitClicked || !filled) return
        webView.evaluateJavascript(
            """(function(){
                var e=document.querySelector('#loginemail');
                var p=document.querySelector('#loginpassword');
                if(!e||!p||!e.value||!p.value) return 'nofill';
                var b=document.querySelector('#loginSubmit');
                if(b && !b.disabled && b.offsetParent!==null){ b.click(); return 'clicked'; }
                return 'wait';
            })();"""
        ) { r -> if (r != null && r.contains("clicked")) submitClicked = true }
    }

    /**
     * cookie 要按**当前实际所在的域**去查，不能写死常量。
     *
     * 上游换过两次址(.com → .tech → .art)，且旧址是用 302 跳到新址的：WebView 跟着
     * 跳到新域、cookie 落在新域下，而写死的老域名查出来永远是 null —— 2026-08-10
     * 就这么坏过一次(登录其实成功了，卡在资料页回不来，因为 jwt 在 .art 而我们查 .tech)。
     * 不同顶级域之间 cookie 不可能共享，所以只有跟着当前 URL 走才是对的。
     * webView.url 在首次加载前可能为 null/空，那时退回 LOGIN_URL（初始值，非猜测）。
     */
    private fun cookieDomain(): String {
        val cur = webView.url
        return if (cur.isNullOrBlank()) LOGIN_URL else cur
    }

    private fun pollCookie() {
        if (done) return
        val cookies = CookieManager.getInstance().getCookie(cookieDomain()) ?: return
        val jwt = cookies.split(";")
            .map { it.trim() }
            .firstOrNull { it.startsWith("erolabsjwt=") }
            ?.substringAfter("erolabsjwt=")
        if (!jwt.isNullOrEmpty()) {
            done = true
            finishWith(jwt)
        }
    }

    private fun finishWith(token: String) {
        setResult(Activity.RESULT_OK, Intent()
            .putExtra("token", token)
            .putExtra("account", account))   // 回传真实账号邮箱作注册表 key
        finish()
    }

    override fun onBackPressed() {
        setResult(Activity.RESULT_CANCELED, Intent().putExtra("error", "登录已取消"))
        super.onBackPressed()
    }

    /**
     * 自动填账密 + 顶部品牌提示条。返回 'ok'(已确认填住) / 'retry'(表单还没好)。
     *
     * 不做“祖先链隐藏兄弟”式聚光——那会因页面结构差异误伤登录表单（导致表单整块
     * 消失、页面空白）。保持表单+验证码可见。
     */
    private fun fillScript(account: String, password: String): String {
        // JSONObject.quote 输出带双引号的合法 JS 字符串字面量，反斜杠/引号/换行
        // /控制字符全部转义好——比手写 replace 靠得住（密码里啥字符都可能有）。
        val acc = JSONObject.quote(account)
        val pwd = JSONObject.quote(password)
        return """
        (function(){
          var acc = $acc, pwd = $pwd;
          var e = document.querySelector('#loginemail');
          var p = document.querySelector('#loginpassword');
          if (!e || !p) return 'retry';
          // 幂等：值已对就不重复写，避免每 500ms 打断用户自己的输入
          if (e.value !== acc) { e.value = acc; e.dispatchEvent(new Event('input',{bubbles:true})); }
          if (pwd && p.value !== pwd) { p.value = pwd; p.dispatchEvent(new Event('input',{bubbles:true})); }
          if(!document.getElementById('__ark_banner')){
            var d=document.createElement('div'); d.id='__ark_banner';
            d.style.cssText='position:fixed;top:0;left:0;right:0;z-index:2147483647;padding:14px 16px;'
              +'background:linear-gradient(90deg,#ff7fac,#a06bff);color:#fff;font-size:15px;'
              +'font-weight:700;text-align:center;font-family:sans-serif';
            d.innerHTML='LucimaTools · 账号密码已自动填好<br><span style="font-weight:500;font-size:13px">'
              +'请完成下方的人机验证，随后会自动登录…</span>';
            document.body.appendChild(d);
          }
          // 读回校验：确实填住了才算成功（没有密码可填时只看账号）
          var okAcc = e.value === acc, okPwd = !pwd || p.value === pwd;
          return (okAcc && okPwd) ? 'ok' : 'retry';
        })();
        """.trimIndent()
    }

    companion object {
        // 门户入口：与 backend/config.py 的 PORTAL_SITE 保持一致（改一处必改另一处）。
        // 历代入口都是同一后端（Turnstile key 相同、表单字段 ID 相同）：
        //   game.ero-labs.art  —— 当前（可直连，不依赖代理）
        //   game.ero-labs.tech —— 2026-08-10 起 302 → .art
        //   www.ero-labs.com   —— 最早，需代理
        // 直接指向当前入口，不靠 302：多一跳只是白等，且 cookie 会落到跳转后的域。
        const val LOGIN_URL = "https://game.ero-labs.art/cn/login.html"
        // 注意：cookie 域名**不再写常量**，见 cookieDomain() —— 写死会在换址时静默失效。

        // 登录页引用的第三方，国内直连不通（新门户不套代理）。不拦的话它们的
        // 请求会挂住整页 load 事件几十秒。对登录流程无用，一律短路成空响应。
        private val DEAD_HOSTS = listOf(
            "googletagmanager.com",
            "google-analytics.com",
            "livechatbot.net",
        )
    }
}
