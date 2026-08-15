package com.openrubi.lucimatools

import android.content.Context
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
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

    const val PORT = 8000

    private val started = AtomicBoolean(false)

    /** web 资源释放目标目录（frontend/assets），Python 标准库服务器 open() 从这里读。 */
    fun assetRoot(ctx: Context): File = File(ctx.filesDir, "web")

    /**
     * 幂等引导后端。第一次调用真正执行（Python.start + 资源释放 + 起 server 线程），
     * 之后调用直接返回。可从 Service.onStartCommand 或 Activity.onCreate 调用。
     */
    fun ensureStarted(ctx: Context) {
        if (!started.compareAndSet(false, true)) return

        val app = ctx.applicationContext

        // 1) 启动 Python 运行时（进程内单例）
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(app))
        }

        // 2) 释放打包前端与静态资源到私有目录（Python open() 需要真实文件系统）
        val assetRoot = assetRoot(app)
        AssetSync.syncWeb(app, assetRoot)

        // 3) 后台线程起标准库 HTTP 服务（serve_forever 永久阻塞）。
        //    不强制传代理（传空）——由 Python 配置决定（默认"跟随系统"）。
        thread(isDaemon = true, name = "ark-server") {
            try {
                Python.getInstance()
                    .getModule("backend.android_entry")
                    .callAttr("start", app.filesDir.absolutePath, assetRoot.absolutePath, "", PORT)
            } catch (e: Throwable) {
                // 起服务失败：复位标志，允许后续重试引导
                started.set(false)
            }
        }
    }
}
