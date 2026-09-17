package com.openrubi.lucimatools

import android.content.Context
import java.io.File

/**
 * 把 APK assets/web 下的 frontend/ 与 assets/ 释放到私有目录（真实文件系统），
 * 供 Python 的标准库服务器 open() 读取。做一次性释放，戳记未变则跳过
 * （避免每次启动都重复复制静态图片）。
 *
 * 戳记 = `versionCode:lastUpdateTime`。**不能只用 versionCode**：versionCode 现在
 * 由 backend/version.py 的 APP_VERSION 派生（见 build.gradle.kts），同一版本内改了
 * frontend/ 重新构建安装时 versionCode 不变 → 旧逻辑会跳过释放，设备上继续跑旧前端，
 * 看起来像"改了没生效"。`lastUpdateTime` 每次安装/覆盖安装都会变，正好补上这一档，
 * 顺带干掉了过去"同步 Android 必须手工 bump versionCode"的坑。
 */
object AssetSync {

    fun syncWeb(ctx: Context, target: File) {
        val token = try {
            val pi = ctx.packageManager.getPackageInfo(ctx.packageName, 0)
            "${pi.longVersionCode}:${pi.lastUpdateTime}"
        } catch (e: Exception) {
            "1:0"
        }
        val stamp = File(target, ".version")
        if (target.exists() && stamp.exists() && stamp.readText().trim() == token) {
            return // 与当前安装的包一致，跳过
        }
        target.deleteRecursively()
        target.mkdirs()
        copyAssetDir(ctx, "web", target)
        stamp.writeText(token)
    }

    private fun copyAssetDir(ctx: Context, assetPath: String, outDir: File) {
        val am = ctx.assets
        val entries = am.list(assetPath) ?: return
        if (entries.isEmpty()) {
            // 可能是文件（list 返回空数组）——由调用方保证 assetPath 是目录
            return
        }
        outDir.mkdirs()
        for (name in entries) {
            val childAsset = "$assetPath/$name"
            val sub = am.list(childAsset)
            if (sub != null && sub.isNotEmpty()) {
                copyAssetDir(ctx, childAsset, File(outDir, name))
            } else {
                // 叶子：尝试当文件复制；失败则说明是空目录
                try {
                    am.open(childAsset).use { input ->
                        File(outDir, name).outputStream().use { input.copyTo(it) }
                    }
                } catch (e: Exception) {
                    File(outDir, name).mkdirs()
                }
            }
        }
    }
}
