# 1.2.8 工会集成交付

已将 laborUnion 中的工会功能合入本项目根目录 backend/ 和 frontend/，根项目的安卓构建会直接包含这些功能。laborUnion 保留为参考源码。

- 工会模块包含工会管理、每日签到和奖励、会长管理；没有“工会刷红币”入口。
- 黄票领取标题为“佣兵团黄票领取（默认30个任务，可点击一键完成）”，默认执行 30 轮，按钮为“一键完成”。
- 按用户提供的文字显示“执行轮数（一轮140分，20轮即可拿满）”，侧边栏工会使用同风格双人线条图标。
- 保留空 Results 时继续逐项执行、返回字段摘要、后台进度和停止、旧 WebView 兼容等修复，移除指定的空奖励说明段落。
- 工会请求使用现有登录产生的 AID/SessionID 和 GameClient，沿用游戏 Router 地址。整合重登关闭旧会话的锁顺序修复，避免后台任务与重新登录相互等待。

版本为 1.2.8，Android versionCode 为 10208，静态资源缓存版本为 173。包名继续使用 com.openrubi.lucimatools，使用本地 Android debug 签名构建，可用于覆盖此前同签名版本。

## 构建

使用 Android SDK 34、JDK 17/21 和 Python 3.12。ANDROID_HOME 指定 SDK，LUCIMA_BUILD_PYTHON 可指定 Python 3.12 可执行文件。项目根目录执行：

```sh
cd android
./gradlew :app:assembleDebug --console=plain
```

交付 APK 位于 debug/1.2.8/LucimaTools-android-debug-1.2.8.apk，集成源码归档位于同目录。

按用户此前要求，本次仅做静态差异阅读、代码集成及构建打包，没有运行测试、模拟器、安装验证或真实账号游戏请求，由用户安装验证。
