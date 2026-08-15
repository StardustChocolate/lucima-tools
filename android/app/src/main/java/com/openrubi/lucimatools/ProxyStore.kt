package com.openrubi.lucimatools

import android.content.Context

/**
 * 代理地址的本地持久化。与 Python 端 settings.json 分开存一份（Kotlin 侧 WebView
 * 也要用它配 ProxyController），前端设置面板保存后经 /api/config 同步到 Python。
 * 这里存的是给 WebView 登录页用的默认值。
 */
object ProxyStore {
    private const val PREF = "ark_prefs"
    private const val KEY = "proxy"
    private const val DEFAULT = "http://127.0.0.1:7890"

    fun get(ctx: Context): String =
        ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE).getString(KEY, DEFAULT) ?: DEFAULT

    fun set(ctx: Context, proxy: String) {
        ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE).edit().putString(KEY, proxy).apply()
    }
}
