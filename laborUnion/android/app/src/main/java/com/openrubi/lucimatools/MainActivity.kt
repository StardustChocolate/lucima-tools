package com.openrubi.lucimatools

import android.Manifest
import android.annotation.SuppressLint
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
import androidx.core.content.FileProvider
import java.io.File
import kotlin.concurrent.thread

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private val port = AppBootstrap.PORT
    private var pendingUpdatePath: String? = null

    // 通知权限请求器（API 33+）。授不授予都不影响后端运行，只影响常驻通知是否可见。
    private val notifPermLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* 结果无需处理 */ }

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
        webView.addJavascriptInterface(UpdateBridge(), "LucimaUpdate")
        loadWhenServerReady()
    }

    private inner class UpdateBridge {
        @JavascriptInterface
        fun installUpdate(path: String) {
            runOnUiThread { requestUpdateInstall(path) }
        }
    }

    private fun checkedUpdateApk(rawPath: String): File? {
        return try {
            val base = File(filesDir, "updates").canonicalFile
            val apk = File(rawPath).canonicalFile
            val inside = apk.path.startsWith(base.path + File.separator)
            if (inside && apk.isFile && apk.extension.equals("apk", ignoreCase = true)) apk else null
        } catch (_: Exception) {
            null
        }
    }

    private fun requestUpdateInstall(path: String) {
        val apk = checkedUpdateApk(path)
        if (apk == null) {
            Toast.makeText(this, "更新安装包路径无效", Toast.LENGTH_LONG).show()
            return
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
            !packageManager.canRequestPackageInstalls()
        ) {
            pendingUpdatePath = apk.absolutePath
            try {
                startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES).apply {
                    data = Uri.parse("package:$packageName")
                })
            } catch (_: Exception) {
                Toast.makeText(this, "请允许本应用安装更新", Toast.LENGTH_LONG).show()
            }
            return
        }
        openUpdateInstaller(apk)
    }

    private fun openUpdateInstaller(apk: File) {
        try {
            val uri = FileProvider.getUriForFile(this, "$packageName.updates", apk)
            startActivity(Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, "application/vnd.android.package-archive")
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            })
        } catch (_: Exception) {
            Toast.makeText(this, "无法打开系统安装器", Toast.LENGTH_LONG).show()
        }
    }

    override fun onResume() {
        super.onResume()
        val pending = pendingUpdatePath ?: return
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O ||
            packageManager.canRequestPackageInstalls()
        ) {
            pendingUpdatePath = null
            checkedUpdateApk(pending)?.let { apk ->
                webView.post { openUpdateInstaller(apk) }
            }
        }
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

    override fun onBackPressed() {
        if (webView.canGoBack()) webView.goBack() else super.onBackPressed()
    }
}
