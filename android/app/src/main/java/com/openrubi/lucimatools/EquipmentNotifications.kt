package com.openrubi.lucimatools

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import org.json.JSONObject

/** Called directly by the Python scheduler, including with no Activity alive. */
object EquipmentNotifications {
    private const val CHANNEL = "lucima_equipment_matches"
    const val EVENT_EXTRA = "lucima_equipment_event"
    @Volatile private var context: Context? = null

    fun initialize(value: Context) { context = value.applicationContext }

    @JvmStatic
    fun cancel(id: String) {
        context?.let { NotificationManagerCompat.from(it).cancel("equipment-$id", 2002) }
    }

    @JvmStatic
    fun show(payload: String): Boolean {
        val ctx = context ?: return false
        if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(ctx,
                Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return false
        val manager = NotificationManagerCompat.from(ctx)
        if (!manager.areNotificationsEnabled()) return false
        val data = JSONObject(payload)
        val id = data.optString("id")
        if (!id.matches(Regex("[a-f0-9]{32}"))) return false
        if (Build.VERSION.SDK_INT >= 26) {
            val channel = NotificationChannel(CHANNEL, "装备命中提醒", NotificationManager.IMPORTANCE_HIGH)
            channel.description = "商店出现符合筛选规则的装备时提醒，点击查看当前货架并购买"
            (ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).createNotificationChannel(channel)
        }
        val intent = Intent(ctx, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP
            this.data = Uri.parse("lucimatools://equipment/$id")
            putExtra(EVENT_EXTRA, id)
        }
        val pending = PendingIntent.getActivity(ctx, id.hashCode(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val text = data.optString("text").take(8000)
        val builder = NotificationCompat.Builder(ctx, CHANNEL)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(data.optString("title").take(160))
            .setContentText(text.lineSequence().firstOrNull().orEmpty())
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setContentIntent(pending).setAutoCancel(true).setOnlyAlertOnce(true)
        val image = data.optString("image")
        if (image.matches(Regex("/assets/(equip|sets)/[A-Za-z0-9_-]+\\.png"))) {
            try {
                ctx.assets.open("web" + image).use { builder.setLargeIcon(BitmapFactory.decodeStream(it)) }
            } catch (_: Exception) { /* The complete text remains available. */ }
        }
        return try { manager.notify("equipment-$id", 2002, builder.build()); true }
        catch (_: SecurityException) { false }
    }
}
