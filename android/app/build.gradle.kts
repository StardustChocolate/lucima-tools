import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// 项目根（android/ 的上一级），backend / frontend / assets 都在这里
val projectRoot = rootProject.projectDir.parentFile

// 更新地址由本地构建环境注入，不能写进源码或仓库。
val updateManifestUrl = System.getenv("LUCIMA_UPDATE_URL")?.trim().orEmpty()
val updateEndpointFile = File(projectRoot, "build/update-endpoint.json")
val writeUpdateEndpoint = tasks.register("writeUpdateEndpoint") {
    doLast {
        updateEndpointFile.parentFile.mkdirs()
        if (updateManifestUrl.isBlank()) {
            updateEndpointFile.delete()
        } else {
            val escaped = updateManifestUrl.replace("\\", "\\\\").replace("\"", "\\\"")
            updateEndpointFile.writeText("{\"manifestUrl\":\"$escaped\"}", Charsets.UTF_8)
        }
    }
}

// ---------- 版本号：唯一真源是 backend/version.py，构建时正则读出 ----------
// 以前 versionName 手工写在这里、关于页写在 index.html、后端又写在 server.py，
// 三处各说各话（1.7 / 1.0.2 / 1.0）。现在只改 version.py 一处。
// versionCode 由版本号派生（主*10000+次*100+修订），保证单调递增且不用手工 bump。
val appVersion: String = run {
    val f = File(projectRoot, "backend/version.py")
    val m = Regex("""APP_VERSION\s*=\s*["']([^"']+)["']""").find(f.readText())
        ?: throw GradleException("未能从 ${f.path} 解析 APP_VERSION")
    m.groupValues[1]
}
val appVersionCode: Int = run {
    val p = appVersion.substringBefore("-").split(".")
    fun seg(i: Int) = p.getOrNull(i)?.toIntOrNull() ?: 0
    seg(0) * 10000 + seg(1) * 100 + seg(2)
}

// 正式签名只从被 Git 忽略的 keystore.properties 读取。
// Release 构建缺少任一项时直接失败，禁止静默产生未签名或 Debug 签名的发布包。
val keystorePropsFile = rootProject.file("keystore.properties")
val keystoreProps = Properties().apply {
    if (keystorePropsFile.exists()) keystorePropsFile.inputStream().use { load(it) }
}
val signingPropertyNames = listOf("storeFile", "storePassword", "keyAlias", "keyPassword")
val hasReleaseSigning = keystorePropsFile.exists() &&
    signingPropertyNames.all { !keystoreProps.getProperty(it).isNullOrBlank() }
val releaseRequested = gradle.startParameter.taskNames.any {
    it.contains("release", ignoreCase = true)
}
if (releaseRequested && !hasReleaseSigning) {
    throw GradleException(
        "Release signing is not configured. Generate android/signing/lucima.keystore " +
            "and android/keystore.properties first."
    )
}
if (releaseRequested && hasReleaseSigning) {
    val configuredStore = rootProject.file(keystoreProps.getProperty("storeFile"))
    if (!configuredStore.isFile) {
        throw GradleException("Release keystore is missing: ${configuredStore.path}")
    }
}

android {
    namespace = "com.openrubi.lucimatools"
    compileSdk = 34

    signingConfigs {
        if (hasReleaseSigning) {
            create("release") {
                storeFile = rootProject.file(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
        }
    }

    defaultConfig {
        applicationId = "com.openrubi.lucimatools"
        minSdk = 24
        targetSdk = 34
        versionCode = appVersionCode      // 1.1.0 → 10100（旧手工值最大 8，可安全覆盖安装）
        versionName = appVersion
        ndk {
            // 真机 arm64；x86_64 供模拟器
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    buildTypes {
        debug { }
        release {
            isMinifyEnabled = false
            if (hasReleaseSigning) signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        viewBinding = true
    }
}

chaquopy {
    defaultConfig {
        version = "3.12"
        pip {
            // httpx 及其纯 Python 依赖；游戏协议客户端用它走代理
            install("httpx")
            // 虚构深渊实时战斗使用的 RoomServer WebSocket 客户端
            install("websockets==13.1")
        }
        // 让 backend 包被提取到真实文件系统，使 open(equip_ref.json) 可用
        extractPackages("backend")
    }
}

// ---------- 构建前把共享源复制进来（单一真源在项目根，不重复维护） ----------
val copyBackend = tasks.register<Sync>("copyBackendPython") {
    from(File(projectRoot, "backend")) {
        exclude("__pycache__/**")
    }
    into(layout.projectDirectory.dir("src/main/python/backend"))
}

val copyWeb = tasks.register<Sync>("copyWebAssets") {
    dependsOn(writeUpdateEndpoint)
    into(layout.projectDirectory.dir("src/main/assets/web"))
    from(File(projectRoot, "frontend")) { into("frontend") }
    from(File(projectRoot, "assets")) { into("assets") }
    from(updateEndpointFile)
    from(File(projectRoot, "encyclopedia-endpoint.json"))
    from(File(projectRoot, "redeem-codes-endpoint.json"))
}

tasks.named("preBuild") {
    dependsOn(copyBackend, copyWeb)
}

// Chaquopy 的 mergePythonSources / AGP 的 mergeAssets 会读取被 Sync 写入的目录，
// 必须显式声明依赖，否则 Gradle 报隐式依赖错误（且可能顺序错乱）。
tasks.matching { it.name.matches(Regex("merge.*PythonSources")) }
    .configureEach { dependsOn(copyBackend) }
tasks.matching { it.name.matches(Regex("merge.*Assets")) }
    .configureEach { dependsOn(copyWeb) }

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("com.google.android.material:material:1.11.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")
}
