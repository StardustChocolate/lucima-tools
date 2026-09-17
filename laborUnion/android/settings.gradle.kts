pluginManagement {
    repositories {
        // 国内镜像优先
        // Chaquopy 插件仓库
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.PREFER_SETTINGS)
    repositories {
        google()
        mavenCentral()
    }
}
rootProject.name = "LucimaTools"
include(":app")
