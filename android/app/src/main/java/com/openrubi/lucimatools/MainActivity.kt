package com.openrubi.lucimatools

import android.Manifest
import android.annotation.SuppressLint
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.PowerManager
import android.provider.DocumentsContract
import android.provider.MediaStore
import android.provider.OpenableColumns
import android.provider.Settings
import android.util.Base64
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.webkit.WebViewClient
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebResourceRequest
import android.webkit.WebResourceError
import android.webkit.WebResourceResponse
import android.view.View
import android.view.Gravity
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.Button
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileOutputStream
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID
import java.util.concurrent.atomic.AtomicReference
import kotlin.concurrent.thread
import org.json.JSONObject

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private val equipmentNotification = AtomicReference<String?>(null)
    private var pendingUpdatePath: String? = null
    private var lastRendererRecovery = 0L
    private lateinit var startupOverlay: LinearLayout
    private lateinit var startupMessage: TextView
    private lateinit var startupRetry: Button
    private lateinit var startupSpinner: ProgressBar
    private var startupGeneration = 0
    private var pageReady = false
    private var startupPermissionsRequested = false
    private val filePreferences by lazy {
        getSharedPreferences(FILE_PREFERENCES, Context.MODE_PRIVATE)
    }
    private val pendingEncyclopediaImages = mutableMapOf<String, PendingEncyclopediaImage>()
    private val pendingEncyclopediaImagesLock = Any()

    private data class PendingEncyclopediaImage(
        val filename: String,
        val file: File,
        var bytesWritten: Int = 0,
    )

    // 通知权限请求器（API 33+）。授不授予都不影响后端运行，只影响常驻通知是否可见。
    private val notifPermLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* 结果无需处理 */ }

    private val legacyStoragePermLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            val message = if (granted) "存储权限已授予，请再次点击保存" else "未授予存储权限"
            Toast.makeText(this, message, Toast.LENGTH_LONG).show()
        }

    private val downloadTreeLauncher =
        registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
            if (uri == null) return@registerForActivityResult
            val flags = Intent.FLAG_GRANT_READ_URI_PERMISSION or
                Intent.FLAG_GRANT_WRITE_URI_PERMISSION
            try {
                contentResolver.takePersistableUriPermission(uri, flags)
                releasePreviousTreePermission(uri)
                filePreferences.edit()
                    .putString(DOWNLOAD_TREE_URI, uri.toString())
                    .putString(DOWNLOAD_TREE_LABEL, treeLocationLabel(uri))
                    .apply()
                notifyDownloadLocation()
            } catch (_: Exception) {
                Toast.makeText(this, "无法保存该文件夹的访问权限", Toast.LENGTH_LONG).show()
            }
        }

    private val equipmentFilterFileLauncher =
        registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
            if (uri == null) return@registerForActivityResult
            thread(isDaemon = true) {
                try {
                    val (filename, contents) = readEquipmentFilterFile(uri)
                    webView.post { notifyEquipmentFilterFileSelected(filename, contents) }
                } catch (error: Exception) {
                    webView.post {
                        notifyEquipmentFilterFileError(error.message ?: "读取方案文件失败")
                    }
                }
            }
        }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        equipmentNotification.set(intent?.getStringExtra(EquipmentNotifications.EVENT_EXTRA))

        // Bootstrap is asynchronous. Clear a prior failure before its new UI
        // starts polling, so a retry cannot consume the previous error.
        AppBootstrap.ensureStarted(this)
        createWebView()

        // 2) 启动前台服务托管进程——让息屏 / 切后台 / 划掉最近任务后后端调度线程仍存活。
        //    持有 Partial WakeLock + WifiLock，配合下面的电池白名单请求对抗 Doze。
        AutomationService.start(this)

    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun createWebView() {
        val generation = ++startupGeneration
        pageReady = false
        webView = WebView(this)
        webView.setBackgroundColor(android.graphics.Color.rgb(11, 16, 32))
        val root = FrameLayout(this)
        root.addView(webView, FrameLayout.LayoutParams(-1, -1))
        startupOverlay = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setPadding(32, 48, 32, 48)
            setBackgroundColor(android.graphics.Color.rgb(11, 16, 32))
        }
        startupMessage = TextView(this).apply {
            text = "正在启动璐茜玛…"
            textSize = 17f
            gravity = Gravity.CENTER
            setTextColor(android.graphics.Color.WHITE)
        }
        startupSpinner = ProgressBar(this).apply {
            isIndeterminate = true
            contentDescription = "正在加载，请稍候"
        }
        startupRetry = Button(this).apply {
            text = "重试"
            visibility = View.GONE
            setOnClickListener {
                isEnabled = false
                val old = webView
                (old.parent as? android.view.ViewGroup)?.removeView(old)
                old.destroy()
                AppBootstrap.ensureStarted(this@MainActivity)
                createWebView()
            }
        }
        startupOverlay.addView(startupSpinner, LinearLayout.LayoutParams(64, 64).apply {
            bottomMargin = 24
        })
        startupOverlay.addView(startupMessage)
        startupOverlay.addView(startupRetry)
        root.addView(startupOverlay, FrameLayout.LayoutParams(-1, -1))
        setContentView(root)
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true          // localStorage（主题/购买目标等）
            // APIs are no-store; local files revalidate their ETag. Reuse
            // image/file bodies instead of reading and copying every visit.
            cacheMode = android.webkit.WebSettings.LOAD_DEFAULT
        }
        webView.addJavascriptInterface(UpdateBridge(), "LucimaUpdate")
        webView.addJavascriptInterface(FileBridge(), "LucimaFiles")
        webView.addJavascriptInterface(TaskRenderingBridge(webView), "LucimaRendering")
        webView.addJavascriptInterface(NotificationBridge(), "LucimaNotifications")
        webView.addJavascriptInterface(StartupBridge(generation), "LucimaStartup")
        webView.webViewClient = object : WebViewClient() {
            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame && generation == startupGeneration) {
                    showStartup("页面加载失败，请点击重试。\n${error.description}", true)
                }
            }
            override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
                if (request.isForMainFrame && generation == startupGeneration) {
                    showStartup("页面加载失败（${response.statusCode}），请点击重试。", true)
                }
            }
            override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
                // Recover the page only. The Python worker retains task state;
                // opening the UI again must never resubmit a battle or sale.
                val now = android.os.SystemClock.elapsedRealtime()
                val repeated = lastRendererRecovery != 0L && now - lastRendererRecovery < 10000
                lastRendererRecovery = now
                (view.parent as? android.view.ViewGroup)?.removeView(view)
                view.destroy()
                if (repeated) {
                    val retry = TextView(this@MainActivity).apply {
                        text = "页面显示暂时中断，已提交的操作不会重复执行。点此恢复页面。"
                        setTextColor(android.graphics.Color.WHITE)
                        setBackgroundColor(android.graphics.Color.rgb(7, 11, 20))
                        setPadding(32, 64, 32, 32)
                        setOnClickListener { isEnabled = false; createWebView() }
                    }
                    setContentView(retry)
                } else createWebView()
                return true
            }
        }
        loadWhenServerReady(generation)
    }

    private fun showStartup(message: String, retry: Boolean = false) {
        startupMessage.text = message
        startupSpinner.visibility = if (retry) View.GONE else View.VISIBLE
        startupRetry.isEnabled = retry
        startupRetry.visibility = if (retry) View.VISIBLE else View.GONE
        startupOverlay.visibility = View.VISIBLE
    }

    private inner class StartupBridge(private val generation: Int) {
        @JavascriptInterface
        fun ready() = runOnUiThread {
            if (generation != startupGeneration || isFinishing || isDestroyed) return@runOnUiThread
            pageReady = true
            startupOverlay.visibility = View.GONE
            if (!startupPermissionsRequested) {
                startupPermissionsRequested = true
                requestNotificationPermission()
                requestIgnoreBatteryOptimizations()
            }
        }
        @JavascriptInterface
        fun failed(message: String) = runOnUiThread {
            if (generation == startupGeneration) showStartup("页面初始化失败，请点击重试。\n${message.take(500)}", true)
        }
    }

    private inner class NotificationBridge {
        @JavascriptInterface
        fun takePendingEquipmentEvent(): String = equipmentNotification.getAndSet(null).orEmpty()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        intent.getStringExtra(EquipmentNotifications.EVENT_EXTRA)?.let { equipmentNotification.set(it) }
        webView.evaluateJavascript("window.dispatchEvent(new Event('lucima:notification-open'))", null)
    }

    private inner class UpdateBridge {
        @JavascriptInterface
        fun installUpdate(path: String) {
            runOnUiThread { requestUpdateInstall(path) }
        }
    }

    private inner class FileBridge {
        @JavascriptInterface
        fun getDownloadLocation(): String = currentDownloadLocation()

        @JavascriptInterface
        fun chooseDownloadDirectory() {
            val initial = customDownloadTreeUri()
            runOnUiThread { downloadTreeLauncher.launch(initial) }
        }

        @JavascriptInterface
        fun resetDownloadDirectory(): String {
            releasePreviousTreePermission(null)
            filePreferences.edit()
                .remove(DOWNLOAD_TREE_URI)
                .remove(DOWNLOAD_TREE_LABEL)
                .apply()
            val location = currentDownloadLocation()
            runOnUiThread { notifyDownloadLocation() }
            return location
        }

        @JavascriptInterface
        fun saveDeveloperLogs(): String {
            if (Build.VERSION.SDK_INT <= Build.VERSION_CODES.P &&
                ContextCompat.checkSelfPermission(
                    this@MainActivity, Manifest.permission.WRITE_EXTERNAL_STORAGE
                ) != PackageManager.PERMISSION_GRANTED
            ) {
                runOnUiThread {
                    legacyStoragePermLauncher.launch(Manifest.permission.WRITE_EXTERNAL_STORAGE)
                }
                return jsonResult(false, detail = "请授予存储权限后再次保存")
            }
            return try {
                val savedTo = saveDeveloperLogFile()
                jsonResult(true, path = savedTo)
            } catch (error: Exception) {
                jsonResult(false, detail = error.message ?: "保存开发者日志失败")
            }
        }

        @JavascriptInterface
        fun saveEquipmentFilterScheme(filename: String, content: String): String {
            if (needsLegacyStoragePermission()) {
                runOnUiThread {
                    legacyStoragePermLauncher.launch(Manifest.permission.WRITE_EXTERNAL_STORAGE)
                }
                return jsonResult(false, detail = "请授予存储权限后再次导出")
            }
            return try {
                val contents = content.toByteArray(Charsets.UTF_8)
                if (contents.size > EQUIPMENT_FILTER_FILE_MAX_BYTES) {
                    throw IllegalArgumentException("方案文件超过 1 MB，无法导出")
                }
                val safeFilename = safeEquipmentFilterFilename(filename)
                val savedTo = saveDownloadFile(
                    safeFilename, contents, "application/json"
                )
                jsonResult(true, path = savedTo)
            } catch (error: Exception) {
                jsonResult(false, detail = error.message ?: "导出装备筛选方案失败")
            }
        }

        @JavascriptInterface
        fun saveEncyclopediaImage(filename: String, pngBase64: String): String {
            if (needsLegacyStoragePermission()) {
                runOnUiThread {
                    legacyStoragePermLauncher.launch(Manifest.permission.WRITE_EXTERNAL_STORAGE)
                }
                return jsonResult(false, detail = "请授予存储权限后再次保存")
            }
            return try {
                val savedTo = saveEncyclopediaPng(filename, pngBase64)
                jsonResult(true, path = savedTo)
            } catch (error: Exception) {
                jsonResult(false, detail = error.message ?: "保存图鉴图片失败")
            }
        }

        @JavascriptInterface
        fun saveEncyclopediaImageStart(filename: String): String {
            if (needsLegacyStoragePermission()) {
                runOnUiThread {
                    legacyStoragePermLauncher.launch(Manifest.permission.WRITE_EXTERNAL_STORAGE)
                }
                return jsonResult(false, detail = "请授予存储权限后再次保存")
            }
            return try {
                val safeFilename = safeEncyclopediaFilename(filename)
                val token = UUID.randomUUID().toString()
                val file = File.createTempFile("lucima-encyclopedia-", ".png", cacheDir)
                synchronized(pendingEncyclopediaImagesLock) {
                    pendingEncyclopediaImages[token] = PendingEncyclopediaImage(safeFilename, file)
                }
                JSONObject().apply {
                    put("ok", true)
                    put("token", token)
                    put("name", safeFilename)
                }.toString()
            } catch (error: Exception) {
                jsonResult(false, detail = error.message ?: "无法开始保存图鉴图片")
            }
        }

        @JavascriptInterface
        fun saveEncyclopediaImageChunk(
            token: String, base64Chunk: String, finalChunk: Boolean
        ): String {
            var pending: PendingEncyclopediaImage? = null
            var completed = false
            return try {
                if (token.isBlank() || base64Chunk.isEmpty() ||
                    base64Chunk.length > ENCYCLOPEDIA_PNG_CHUNK_MAX_CHARS ||
                    !isBase64Chunk(base64Chunk, finalChunk)
                ) {
                    throw IllegalArgumentException("图鉴图片分片无效")
                }
                val decoded = Base64.decode(base64Chunk, Base64.DEFAULT)
                synchronized(pendingEncyclopediaImagesLock) {
                    pending = pendingEncyclopediaImages[token]
                        ?: throw IllegalArgumentException("图鉴图片保存任务已失效")
                    val nextSize = pending!!.bytesWritten.toLong() + decoded.size
                    if (nextSize > ENCYCLOPEDIA_PNG_MAX_BYTES) {
                        throw IllegalArgumentException("图鉴图片超过 32 MB，无法保存")
                    }
                    FileOutputStream(pending!!.file, true).use { output ->
                        output.write(decoded)
                    }
                    pending!!.bytesWritten = nextSize.toInt()
                    if (finalChunk) {
                        pendingEncyclopediaImages.remove(token)
                        completed = true
                    }
                }
                if (!finalChunk) {
                    JSONObject().apply {
                        put("ok", true)
                        put("token", token)
                    }.toString()
                } else {
                    val image = pending!!
                    val contents = image.file.readBytes()
                    if (!isPng(contents)) {
                        throw IllegalArgumentException("图鉴图片不是有效的 PNG")
                    }
                    val savedTo = saveDownloadFile(image.filename, contents, "image/png")
                    image.file.delete()
                    jsonResult(true, path = savedTo)
                }
            } catch (error: Exception) {
                val image = pending ?: synchronized(pendingEncyclopediaImagesLock) {
                    pendingEncyclopediaImages.remove(token)
                }
                if (completed || image != null) {
                    synchronized(pendingEncyclopediaImagesLock) {
                        pendingEncyclopediaImages.remove(token)
                    }
                    image?.file?.delete()
                }
                jsonResult(false, detail = error.message ?: "保存图鉴图片失败")
            }
        }

        @JavascriptInterface
        fun saveEncyclopediaImageCancel(token: String): String {
            val image = synchronized(pendingEncyclopediaImagesLock) {
                pendingEncyclopediaImages.remove(token)
            }
            image?.file?.delete()
            return jsonResult(true)
        }

        @JavascriptInterface
        fun chooseEquipmentFilterFile() {
            runOnUiThread {
                equipmentFilterFileLauncher.launch(arrayOf("application/json", "text/plain"))
            }
        }
    }

    private fun jsonResult(ok: Boolean, path: String = "", detail: String = ""): String =
        JSONObject().apply {
            put("ok", ok)
            if (path.isNotEmpty()) put("path", path)
            if (detail.isNotEmpty()) put("detail", detail)
        }.toString()

    private fun customDownloadTreeUri(): Uri? {
        val raw = filePreferences.getString(DOWNLOAD_TREE_URI, null) ?: return null
        val uri = try { Uri.parse(raw) } catch (_: Exception) { return null }
        val writable = contentResolver.persistedUriPermissions.any {
            it.uri == uri && it.isWritePermission
        }
        return if (writable) uri else null
    }

    private fun currentDownloadLocation(): String {
        val uri = customDownloadTreeUri() ?: return DEFAULT_DOWNLOAD_LABEL
        return filePreferences.getString(DOWNLOAD_TREE_LABEL, null)
            ?: treeLocationLabel(uri)
    }

    private fun treeLocationLabel(uri: Uri): String {
        val documentId = try {
            DocumentsContract.getTreeDocumentId(uri)
        } catch (_: Exception) {
            return "已选择的文件夹"
        }
        val parts = documentId.split(":", limit = 2)
        if (parts.size == 2 && parts[0].equals("primary", ignoreCase = true)) {
            return "内部存储/${parts[1].ifBlank { "根目录" }}"
        }
        return documentId.replace(':', '/')
    }

    private fun releasePreviousTreePermission(keep: Uri?) {
        val raw = filePreferences.getString(DOWNLOAD_TREE_URI, null) ?: return
        val previous = try { Uri.parse(raw) } catch (_: Exception) { return }
        if (previous == keep) return
        try {
            contentResolver.releasePersistableUriPermission(
                previous,
                Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
            )
        } catch (_: Exception) { /* Permission may already have been revoked. */ }
    }

    private fun notifyDownloadLocation() {
        if (!::webView.isInitialized) return
        val quoted = JSONObject.quote(currentDownloadLocation())
        webView.evaluateJavascript(
            "window.onLucimaDownloadDirectoryChanged?.($quoted)", null
        )
    }

    private fun notifyEquipmentFilterFileSelected(filename: String, contents: String) {
        if (!::webView.isInitialized) return
        webView.evaluateJavascript(
            "window.onLucimaEquipmentFilterFileSelected?.(" +
                "${JSONObject.quote(filename)},${JSONObject.quote(contents)})",
            null,
        )
    }

    private fun notifyEquipmentFilterFileError(message: String) {
        if (!::webView.isInitialized) return
        webView.evaluateJavascript(
            "window.onLucimaEquipmentFilterFileError?.(${JSONObject.quote(message)})", null
        )
    }

    private fun needsLegacyStoragePermission(): Boolean =
        Build.VERSION.SDK_INT <= Build.VERSION_CODES.P &&
            ContextCompat.checkSelfPermission(
                this, Manifest.permission.WRITE_EXTERNAL_STORAGE
            ) != PackageManager.PERMISSION_GRANTED

    private fun readEquipmentFilterFile(uri: Uri): Pair<String, String> {
        var filename = "方案文件.json"
        var declaredSize = -1L
        contentResolver.query(
            uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE),
            null, null, null,
        )?.use { cursor ->
            if (cursor.moveToFirst()) {
                val nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                val sizeIndex = cursor.getColumnIndex(OpenableColumns.SIZE)
                if (nameIndex >= 0) filename = cursor.getString(nameIndex) ?: filename
                if (sizeIndex >= 0 && !cursor.isNull(sizeIndex)) declaredSize = cursor.getLong(sizeIndex)
            }
        }
        if (declaredSize > EQUIPMENT_FILTER_FILE_MAX_BYTES) {
            throw IllegalArgumentException("方案文件超过 1 MB，无法导入")
        }
        val bytes = contentResolver.openInputStream(uri)?.use { input ->
            val output = ByteArrayOutputStream()
            val buffer = ByteArray(8192)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                if (output.size() + count > EQUIPMENT_FILTER_FILE_MAX_BYTES) {
                    throw IllegalArgumentException("方案文件超过 1 MB，无法导入")
                }
                output.write(buffer, 0, count)
            }
            output.toByteArray()
        } ?: throw IllegalStateException("无法读取所选方案文件")
        return filename to bytes.toString(Charsets.UTF_8)
    }

    private fun developerLogText(): String {
        val logDirectory = File(filesDir, "logs")
        val sources = (3 downTo 1).map { File(logDirectory, "app.log.$it") } +
            File(logDirectory, "app.log")
        val chunks = sources.filter { it.isFile }.mapNotNull { file ->
            file.readText(Charsets.UTF_8).trimEnd('\n').takeIf { it.isNotEmpty() }
        }
        return if (chunks.isEmpty()) "（暂无日志）\n" else chunks.joinToString("\n") + "\n"
    }

    private fun saveDeveloperLogFile(): String {
        val filename = "LucimaTools-developer-log-${
            SimpleDateFormat("yyyyMMdd-HHmmss-SSS", Locale.US).format(Date())
        }.txt"
        val contents = developerLogText().toByteArray(Charsets.UTF_8)
        return saveDownloadFile(filename, contents, "text/plain")
    }

    private fun saveDownloadFile(filename: String, contents: ByteArray, mimeType: String): String {
        val treeUri = customDownloadTreeUri()
        if (treeUri != null) {
            val root = DocumentsContract.buildDocumentUriUsingTree(
                treeUri, DocumentsContract.getTreeDocumentId(treeUri)
            )
            val target = DocumentsContract.createDocument(
                contentResolver, root, mimeType, filename
            ) ?: throw IllegalStateException("无法在所选文件夹中创建文件")
            contentResolver.openOutputStream(target, "w")?.use { it.write(contents) }
                ?: throw IllegalStateException("无法写入所选文件夹")
            return "${currentDownloadLocation()}/${uriDisplayName(target, filename)}"
        }
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val savedName = saveToMediaStore(filename, contents, mimeType)
            "$DEFAULT_DOWNLOAD_LABEL/$savedName"
        } else {
            @Suppress("DEPRECATION")
            val directory = Environment.getExternalStoragePublicDirectory(
                Environment.DIRECTORY_DOWNLOADS
            )
            directory.mkdirs()
            var target = File(directory, filename)
            var suffix = 1
            while (target.exists()) {
                val dot = filename.lastIndexOf('.')
                val stem = if (dot > 0) filename.substring(0, dot) else filename
                val extension = if (dot > 0) filename.substring(dot) else ""
                target = File(directory, "$stem-$suffix$extension")
                suffix += 1
            }
            target.writeBytes(contents)
            target.absolutePath
        }
    }

    private fun saveToMediaStore(
        filename: String, contents: ByteArray, mimeType: String
    ): String {
        val values = ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, filename)
            put(MediaStore.Downloads.MIME_TYPE, mimeType)
            put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS)
            put(MediaStore.Downloads.IS_PENDING, 1)
        }
        val uri = contentResolver.insert(
            MediaStore.Downloads.EXTERNAL_CONTENT_URI, values
        ) ?: throw IllegalStateException("无法在系统下载文件夹中创建文件")
        try {
            contentResolver.openOutputStream(uri, "w")?.use { it.write(contents) }
                ?: throw IllegalStateException("无法写入系统下载文件夹")
            values.clear()
            values.put(MediaStore.Downloads.IS_PENDING, 0)
            contentResolver.update(uri, values, null, null)
            return uriDisplayName(uri, filename)
        } catch (error: Exception) {
            contentResolver.delete(uri, null, null)
            throw error
        }
    }

    private fun uriDisplayName(uri: Uri, fallback: String): String {
        return try {
            contentResolver.query(
                uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null
            )?.use { cursor ->
                if (cursor.moveToFirst()) {
                    val index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    if (index >= 0) cursor.getString(index) else null
                } else null
            } ?: fallback
        } catch (_: Exception) {
            fallback
        }
    }

    private fun safeEquipmentFilterFilename(raw: String): String {
        val stem = raw.trim().removeSuffix(".json")
            .map { character ->
                if (character.code < 32 || character in "\\/:*?\"<>|") '_' else character
            }
            .joinToString("")
            .trim(' ', '.')
            .take(120)
            .trimEnd(' ', '.')
            .ifBlank { "LucimaTools-equipment-filter-未命名方案" }
        return "$stem.json"
    }

    private fun safeEncyclopediaFilename(raw: String): String {
        val stem = raw.trim().removeSuffix(".png").removeSuffix(".PNG")
            .map { character ->
                if (character.code < 32 || character in "\\/:*?\"<>|") '_' else character
            }
            .joinToString("")
            .trim(' ', '.')
            .take(120)
            .trimEnd(' ', '.')
            .ifBlank { "LucimaTools-encyclopedia" }
        return "$stem.png"
    }

    private fun saveEncyclopediaPng(filename: String, encoded: String): String {
        if (encoded.isBlank() || encoded.length > ENCYCLOPEDIA_PNG_BASE64_MAX_CHARS ||
            !isBase64(encoded)
        ) {
            throw IllegalArgumentException("图鉴图片数据不是有效的 Base64")
        }
        val contents = try {
            Base64.decode(encoded, Base64.DEFAULT)
        } catch (_: IllegalArgumentException) {
            throw IllegalArgumentException("图鉴图片数据不是有效的 Base64")
        }
        if (contents.size > ENCYCLOPEDIA_PNG_MAX_BYTES || !isPng(contents)) {
            throw IllegalArgumentException("图鉴图片不是有效的 PNG 或超过 32 MB")
        }
        return saveDownloadFile(safeEncyclopediaFilename(filename), contents, "image/png")
    }

    private fun isPng(contents: ByteArray): Boolean {
        if (contents.size < PNG_SIGNATURE.size) return false
        return PNG_SIGNATURE.indices.all { index -> contents[index] == PNG_SIGNATURE[index] }
    }

    private fun isBase64(value: String): Boolean {
        if (value.length % 4 != 0) return false
        return value.matches(Regex("[A-Za-z0-9+/]*={0,2}"))
    }

    private fun isBase64Chunk(value: String, finalChunk: Boolean): Boolean {
        if (!isBase64(value)) return false
        return finalChunk || !value.contains('=')
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
        AutomationService.refreshBatteryOptimizationState(this)
        if (::webView.isInitialized) {
            webView.onResume()
            webView.evaluateJavascript("window.dispatchEvent(new Event('lucima:resume'))", null)
        }
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

    override fun onPause() {
        if (::webView.isInitialized) webView.onPause()
        super.onPause()
    }

    override fun onTrimMemory(level: Int) {
        super.onTrimMemory(level)
        if (::webView.isInitialized && level >= android.content.ComponentCallbacks2.TRIM_MEMORY_RUNNING_LOW) {
            webView.post {
                webView.evaluateJavascript("window.dispatchEvent(new Event('lucima:memory-pressure'))", null)
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
     * 每次安装只主动请求一次；允许、拒绝或关闭后，重启和覆盖更新均不再请求。
     */
    private fun requestIgnoreBatteryOptimizations() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return
        val preferences = getSharedPreferences(STARTUP_PREFERENCES, Context.MODE_PRIVATE)
        if (preferences.getBoolean(BATTERY_OPTIMIZATION_PROMPTED, false)) return
        // 打开系统界面前持久化，避免拒绝后重启或 Activity 重建时重复弹出。
        preferences.edit().putBoolean(BATTERY_OPTIMIZATION_PROMPTED, true).commit()
        // 旧版本没有提示记录，覆盖安装时也不补弹。
        val installed = packageManager.getPackageInfo(packageName, 0)
        if (installed.lastUpdateTime > installed.firstInstallTime) return
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
    private fun loadWhenServerReady(generation: Int) {
        thread(isDaemon = true, name = "lucima-startup-probe") {
            val deadline = android.os.SystemClock.elapsedRealtime() + 120_000
            while (android.os.SystemClock.elapsedRealtime() < deadline) {
                if (isDestroyed || generation != startupGeneration) return@thread
                val endpoint = AppBootstrap.endpoint
                if (endpoint?.isReady() == true) {
                    runOnUiThread {
                        if (generation == startupGeneration && !isDestroyed) {
                            showStartup("正在加载界面…")
                            webView.loadUrl(endpoint.url)
                            webView.postDelayed({
                                if (generation == startupGeneration && !pageReady && !isDestroyed) {
                                    showStartup("页面未能完成加载，请点击重试。若多次失败，请更新系统的 Android System WebView。", true)
                                }
                            }, 30_000)
                        }
                    }
                    return@thread
                }
                val error = AppBootstrap.failure
                runOnUiThread {
                    if (generation == startupGeneration && !isDestroyed) {
                        showStartup(if (error == null) AppBootstrap.status else
                            "启动失败，请点击重试。\n${error.lineSequence().take(3).joinToString("\n")}", error != null)
                    }
                }
                if (error != null) return@thread
                Thread.sleep(300)
            }
            runOnUiThread {
                if (generation == startupGeneration && !isDestroyed) {
                    showStartup("启动所需时间较长，请点击重试。\n${AppBootstrap.status}", true)
                }
            }
        }
    }

    override fun onBackPressed() {
        if (webView.parent != null && webView.canGoBack()) webView.goBack() else super.onBackPressed()
    }

    override fun onDestroy() {
        startupGeneration++
        synchronized(pendingEncyclopediaImagesLock) {
            pendingEncyclopediaImages.values.forEach { it.file.delete() }
            pendingEncyclopediaImages.clear()
        }
        super.onDestroy()
    }

    companion object {
        private const val STARTUP_PREFERENCES = "lucima_startup"
        private const val BATTERY_OPTIMIZATION_PROMPTED = "battery_optimization_prompted"
        private const val FILE_PREFERENCES = "lucima_files"
        private const val DOWNLOAD_TREE_URI = "download_tree_uri"
        private const val DOWNLOAD_TREE_LABEL = "download_tree_label"
        private const val DEFAULT_DOWNLOAD_LABEL = "系统下载文件夹（Download）"
        private const val EQUIPMENT_FILTER_FILE_MAX_BYTES = 1024 * 1024
        private const val ENCYCLOPEDIA_PNG_MAX_BYTES = 32 * 1024 * 1024
        private const val ENCYCLOPEDIA_PNG_BASE64_MAX_CHARS =
            ((ENCYCLOPEDIA_PNG_MAX_BYTES + 2) / 3) * 4 + 16
        private const val ENCYCLOPEDIA_PNG_CHUNK_MAX_CHARS = 512 * 1024
        private val PNG_SIGNATURE = byteArrayOf(
            0x89.toByte(), 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A
        )
    }
}
