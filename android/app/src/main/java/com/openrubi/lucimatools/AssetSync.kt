package com.openrubi.lucimatools

import android.content.Context
import java.io.File
import java.io.InputStream
import java.security.MessageDigest

/**
 * 把 APK assets/web 下的 frontend/ 与 assets/ 释放到私有目录（真实文件系统），
 * 供 Python 的标准库服务器 open() 读取。做一次性释放，戳记未变则跳过
 * （避免每次启动都重复复制静态图片）。
 *
 * 同版本覆盖安装也必须刷新界面：安装戳记加上 APK 内的核心前端文件指纹，
 * 再核对实际释放文件。即使模拟器安装时间未变或旧文件残留，也不能跳过同步。
 */
object AssetSync {

    private val frontendFiles = listOf("frontend/index.html", "frontend/app.js", "frontend/style.css")

    fun syncWeb(ctx: Context, target: File) {
        val installation = try {
            val pi = ctx.packageManager.getPackageInfo(ctx.packageName, 0)
            "${pi.versionCode}:${pi.lastUpdateTime}"
        } catch (e: Exception) {
            "1:0"
        }
        val expected = frontendFiles.associateWith { path ->
            ctx.assets.open("web/$path").use { sha256(it) }
        }
        val token = installation + ":" + expected.values.joinToString(":")
        val stamp = File(target, ".version")
        if (target.exists() && runCatching { stamp.readText().trim() }.getOrNull() == token &&
            frontendMatches(target, expected)
        ) {
            return // 与当前安装的包一致，跳过
        }
        target.deleteRecursively()
        target.mkdirs()
        copyAssetDir(ctx, "web", target)
        check(frontendMatches(target, expected)) { "前端资源同步不完整" }
        stamp.writeText(token)
    }

    private fun frontendMatches(target: File, expected: Map<String, String>): Boolean =
        expected.all { (path, digest) ->
            runCatching { File(target, path).inputStream().use { sha256(it) } == digest }.getOrDefault(false)
        }

    private fun sha256(input: InputStream): String {
        val digest = MessageDigest.getInstance("SHA-256")
        val buffer = ByteArray(8192)
        while (true) {
            val count = input.read(buffer)
            if (count < 0) break
            digest.update(buffer, 0, count)
        }
        return digest.digest().joinToString("") { "%02x".format(it.toInt() and 0xff) }
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
                // An I/O error must fail startup and leave the stamp absent.
                // Treating it as an empty directory permanently hid bad files.
                am.open(childAsset).use { input ->
                    File(outDir, name).outputStream().use { input.copyTo(it) }
                }
            }
        }
    }
}
