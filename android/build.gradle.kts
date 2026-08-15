// 顶层构建文件
plugins {
    id("com.android.application") version "8.5.2" apply false
    id("org.jetbrains.kotlin.android") version "1.9.24" apply false
    // Chaquopy：在 APK 里嵌入 CPython，运行 backend/ 的 Python 代码
    id("com.chaquo.python") version "15.0.1" apply false
}
