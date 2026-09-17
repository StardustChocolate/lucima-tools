package com.openrubi.lucimatools

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat

/**
 * 前台服务：托管长期运行的后端进程（Python HTTP server + 每账号调度线程），
 * 让它在息屏 / 切后台 / 划掉最近任务后依然存活并持续跑自动任务。
 *
 * 三件套：
 *  1. startForeground + 常驻通知 —— 唯一能让进程不被系统回收的官方机制。
 *  2. Partial WakeLock —— 息屏时保持 CPU 不深睡，让 Python 侧 `wait(300s)` 调度
 *     循环准点醒、网络请求发得出去（用户已选"常驻唤醒锁"策略）。
 *  3. WifiLock —— 息屏时保持 Wi-Fi 射频不休眠，避免长时间挂机断网。
 *
 * 电池优化白名单（Doze 豁免）在 MainActivity 里请求——即便有前台服务，未加白名单
 * 的 app 在 Doze 下网络仍可能被掐。
 */
class AutomationService : Service() {

    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // 必须在 onStartCommand 返回前（~5s 内）调 startForeground，否则系统会 ANR/崩溃。
        startForegroundCompat()
        acquireLocks()
        // 幂等引导后端（若 Activity 已引导过则直接返回）
        AppBootstrap.ensureStarted(this)
        // 被系统杀掉后尽量重建服务（intent 可能为 null，onStartCommand 已能处理）
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        releaseLocks()
        super.onDestroy()
    }

    // ---------- 前台通知 ----------
    private fun startForegroundCompat() {
        val tapIntent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val piFlags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M)
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        else PendingIntent.FLAG_UPDATE_CURRENT
        val pi = PendingIntent.getActivity(this, 0, tapIntent, piFlags)

        val notif: Notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.svc_notif_title))
            .setContentText(getString(R.string.svc_notif_text))
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentIntent(pi)
            .setOngoing(true)                     // 不可滑动移除
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIF_ID, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(NOTIF_ID, notif)
        }
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            val ch = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.svc_channel_name),
                NotificationManager.IMPORTANCE_LOW      // 低优先级：不出声、不打扰
            ).apply {
                description = getString(R.string.svc_channel_desc)
                setShowBadge(false)
            }
            nm.createNotificationChannel(ch)
        }
    }

    // ---------- 唤醒锁 ----------
    @Suppress("WakelockTimeout")   // 常驻唤醒锁：挂机跑长任务，故意不设超时（用户已知情选择）
    private fun acquireLocks() {
        if (wakeLock == null) {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, WAKELOCK_TAG)
        }
        wakeLock?.let { if (!it.isHeld) it.acquire() }

        if (wifiLock == null) {
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
                WifiManager.WIFI_MODE_FULL_LOW_LATENCY
            else
                @Suppress("DEPRECATION") WifiManager.WIFI_MODE_FULL_HIGH_PERF
            wifiLock = wm.createWifiLock(mode, WIFILOCK_TAG)
        }
        wifiLock?.let { if (!it.isHeld) it.acquire() }
    }

    private fun releaseLocks() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wifiLock?.let { if (it.isHeld) it.release() }
    }

    companion object {
        private const val CHANNEL_ID = "ark_automation"
        private const val NOTIF_ID = 1001
        private const val WAKELOCK_TAG = "LucimaTools:automation"
        private const val WIFILOCK_TAG = "LucimaTools:automation-wifi"

        /** 启动前台服务（Activity 调用）。O+ 用 startForegroundService。 */
        fun start(ctx: Context) {
            val intent = Intent(ctx, AutomationService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(intent)
            } else {
                ctx.startService(intent)
            }
        }
    }
}
