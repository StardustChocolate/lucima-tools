package com.openrubi.lucimatools

import android.content.Context
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * 进程级一次性引导：启动 Python 运行时、释放 web 资源、在后台线程起标准库 HTTP 服务。
 *
 * 为什么抽出来：后端（HTTP server + 每账号调度线程）是**进程内长期运行**的，跟哪个
 * Android 组件启动它无关。原来放在 MainActivity.onCreate，一旦息屏 / 划掉最近任务，
 * Activity 被回收、进程降级为后台缓存进程，Doze 会冻结 CPU 和网络 → 调度线程停摆。
 * 现在改由前台服务(AutomationService)托管进程，Activity 和 Service 都可能触发引导，
 * 用 AtomicBoolean 保证整个进程只真正引导一次（幂等），避免重复起 server 抢端口。
 */
object AppBootstrap {

    private const val PREFERRED_PORT = 27843
    private val instanceId = UUID.randomUUID().toString()

    private val started = AtomicBoolean(false)
    private val checking = AtomicBoolean(false)
    @Volatile var status = "正在准备启动"
        private set
    @Volatile var failure: String? = null
        private set
    @Volatile var endpoint: LocalServerEndpoint? = null
        private set

    /** web 资源释放目标目录（frontend/ 与 assets/），Python 标准库服务器 open() 从这里读。 */
    fun assetRoot(ctx: Context): File = File(ctx.filesDir, "web")

    /**
     * 幂等引导后端。第一次调用真正执行（Python.start + 资源释放 + 起 server 线程），
     * 之后调用直接返回。可从 Service.onStartCommand 或 Activity.onCreate 调用。
     */
    fun ensureStarted(ctx: Context) {
        EquipmentNotifications.initialize(ctx.applicationContext)
        val app = ctx.applicationContext
        if (!started.compareAndSet(false, true)) {
            val current = endpoint ?: return
            if (!checking.compareAndSet(false, true)) return
            thread(isDaemon = true, name = "ark-server-health") {
                try {
                    if (!current.isReady()) {
                        val runningPort = if (Python.isStarted()) Python.getInstance()
                            .getModule("backend.android_entry").callAttr("running_port").toInt() else 0
                        if (runningPort == 0 && endpoint === current) {
                            endpoint = null
                            started.set(false)
                            ensureStarted(app)
                        }
                    }
                } catch (error: Exception) {
                    Log.e("LucimaStartup", "Unable to check local server", error)
                } finally { checking.set(false) }
            }
            return
        }
        failure = null
        status = "正在初始化运行环境"
        // Both Python.start and first-install extraction can take seconds.
        // Never run either on the Activity/Service main thread.
        thread(isDaemon = true, name = "ark-server") {
            try {
                if (!Python.isStarted()) Python.start(AndroidPlatform(app))
                status = "正在准备界面资源，首次启动可能需要稍等"
                val assetRoot = assetRoot(app)
                AssetSync.syncWeb(app, assetRoot)
                status = "正在启动本地服务"
                val boundPort = Python.getInstance()
                    .getModule("backend.android_entry")
                    .callAttr("start", app.filesDir.absolutePath, assetRoot.absolutePath, "", PREFERRED_PORT,
                        app.applicationInfo.sourceDir, instanceId).toInt()
                endpoint = LocalServerEndpoint(boundPort, instanceId)
                status = "本地服务已就绪"
            } catch (e: Throwable) {
                Log.e("LucimaStartup", "Startup failed at $status", e)
                val detail = "$status\n${Log.getStackTraceString(e)}"
                runCatching {
                    val file = File(app.filesDir, "logs/startup.log")
                    file.parentFile?.mkdirs()
                    file.writeText(detail.take(64 * 1024))
                }
                failure = detail
                endpoint = null
                started.set(false)
            }
        }
    }
}
