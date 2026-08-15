package com.openrubi.lucimatools

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.chaquo.python.Python
import kotlin.concurrent.thread

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private val port = AppBootstrap.PORT

    // 通知权限请求器（API 33+）。授不授予都不影响后端运行，只影响常驻通知是否可见。
    private val notifPermLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* 结果无需处理 */ }

    // 本次登录待保存的账密（startLogin 时记下，onActivityResult 拿 token 后传给 Python 保存）
    private var pendingPassword: String = ""
    private var pendingSavePwd: Boolean = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // 1) 引导后端（Python 运行时 + 资源释放 + 起 HTTP 服务）。幂等：若前台服务已引导过
        //    则直接返回。这里先引导，保证 WebView 一起来就能连上本地服务。
        AppBootstrap.ensureStarted(this)

        // 2) 启动前台服务托管进程——让息屏 / 切后台 / 划掉最近任务后后端调度线程仍存活。
        //    持有 Partial WakeLock + WifiLock，配合下面的电池白名单请求对抗 Doze。
        AutomationService.start(this)

        // 3) 请求通知权限（API 33+，用于常驻通知）+ 引导用户加电池优化白名单（Doze 豁免）。
        requestNotificationPermission()
        requestIgnoreBatteryOptimizations()

        // 4) WebView 加载本地前端
        webView = WebView(this)
        setContentView(webView)
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true          // localStorage（主题/购买目标等）
            cacheMode = android.webkit.WebSettings.LOAD_NO_CACHE
        }
        webView.addJavascriptInterface(ArkBridge(), "ArkBridge")
        loadWhenServerReady()
    }

    /** API 33+ 需运行时申请 POST_NOTIFICATIONS，否则前台服务的常驻通知不显示。 */
    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.POST_NOTIFICATIONS
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            notifPermLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    /**
     * 引导用户把本 app 加入电池优化白名单（Doze 豁免）。关键：即便有前台服务，未加白名单的
     * app 在 Doze 下网络仍可能被切断、定时任务被延迟批处理 → 挂机时长任务照样停摆。
     * 已在白名单则不弹。用户拒绝也不影响启动，只是息屏可靠性下降。
     */
    private fun requestIgnoreBatteryOptimizations() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) return
        try {
            @SuppressLint("BatteryLife")
            val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                data = Uri.parse("package:$packageName")
            }
            startActivity(intent)
        } catch (e: Exception) {
            // 个别 ROM 无此 Intent——退回应用详情页让用户手动设置
            try {
                startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                    data = Uri.parse("package:$packageName")
                })
            } catch (_: Exception) { /* 忽略：不阻断启动 */ }
        }
    }

    /** 轮询本地端口，服务器就绪后再加载页面（Python 启动需要一点时间）。 */
    private fun loadWhenServerReady() {
        thread(isDaemon = true) {
            val url = "http://127.0.0.1:$port/"
            repeat(60) {
                if (probe(url)) {
                    runOnUiThread { webView.loadUrl(url) }
                    return@thread
                }
                Thread.sleep(300)
            }
            runOnUiThread {
                Toast.makeText(this, "本地服务未能就绪", Toast.LENGTH_LONG).show()
            }
        }
    }

    private fun probe(url: String): Boolean {
        return try {
            val c = (java.net.URL(url + "api/config").openConnection() as java.net.HttpURLConnection)
            c.connectTimeout = 500
            c.readTimeout = 500
            c.requestMethod = "GET"
            val ok = c.responseCode in 200..499
            c.disconnect()
            ok
        } catch (e: Exception) {
            false
        }
    }

    // ---------- JS 桥：前端 window.ArkBridge ----------
    inner class ArkBridge {
        /** 前端点“登录”时调用：启动 LoginActivity 打开 ero-labs 登录页。
         *  savePwd: 1=用户勾了"保存密码"。默认 0，兼容旧前端只传 2 参的情况。 */
        @JavascriptInterface
        fun startLogin(account: String, password: String, savePwd: Int) {
            val py = Python.getInstance().getModule("backend.android_entry")
            // 登录 WebView 用的代理以 Python 配置为准（跟随系统/手动/直连）
            val proxy = try { py.callAttr("effective_proxy").toString() } catch (e: Throwable) { "" }
            // 选"已保存账号"时前端密码框为空——向 Python 取回存档明文密码去填 ero-labs 表单
            var pwd = password
            if (pwd.isEmpty() && account.isNotEmpty()) {
                pwd = try { py.callAttr("saved_password", account).toString() } catch (e: Throwable) { "" }
            }
            pendingPassword = pwd
            pendingSavePwd = savePwd != 0
            val intent = Intent(this@MainActivity, LoginActivity::class.java).apply {
                putExtra("account", account)
                putExtra("password", pwd)
                putExtra("proxy", proxy)
            }
            startActivityForResult(intent, REQ_LOGIN)
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_LOGIN) return
        if (resultCode == Activity.RESULT_OK && data != null) {
            val token = data.getStringExtra("token")
            val account = data.getStringExtra("account") ?: ""
            if (token.isNullOrEmpty()) {
                dispatchLoginResult("""{"ok":false,"detail":"未获取到登录凭证"}""")
                return
            }
            // 用 token 完成游戏 bootstrap（Python 侧，走代理）。带上真实账号邮箱
            // 作注册表 key，否则会兜底成 "android"（左下角账号名显示错误）。
            val pwd = pendingPassword
            val savePwd = pendingSavePwd
            thread(isDaemon = true) {
                val result = try {
                    Python.getInstance()
                        .getModule("backend.android_entry")
                        .callAttr("login_from_token", token, account, pwd, savePwd)
                        .toString()
                } catch (e: Throwable) {
                    """{"ok":false,"detail":"登录处理失败: ${e.message}"}"""
                }
                dispatchLoginResult(result)
            }
        } else {
            val msg = data?.getStringExtra("error") ?: "登录已取消"
            dispatchLoginResult("""{"ok":false,"detail":"$msg"}""")
        }
    }

    /** 把登录结果回传给前端的 window.__androidLoginDone。 */
    private fun dispatchLoginResult(json: String) {
        val escaped = json.replace("\\", "\\\\").replace("'", "\\'")
        runOnUiThread {
            webView.evaluateJavascript(
                "window.__androidLoginDone && window.__androidLoginDone('$escaped');",
                null
            )
        }
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) webView.goBack() else super.onBackPressed()
    }

    companion object {
        const val REQ_LOGIN = 1001
    }
}
