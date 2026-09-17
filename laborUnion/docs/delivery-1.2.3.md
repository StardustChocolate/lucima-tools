# 工会更新版 1.2.3

移除“工会刷红币”整个页面区块、前端操作逻辑及本地提交/查询/停止接口，工会模块保留“工会管理”和“会长管理”。旧捐献协议函数仅留作内部历史回归验证，没有对用户开放的入口。

“工会管理”新增“领取任务奖励（逐个）”：支持查询任务列表、1～100 轮（默认 1）、开始、停止、进度及逐项结果。按原包证据先查询一次 GuildQuestIDList，再每轮逐个调用 QuestHandler.RewardQuest。登录与游戏地址复用源码的 GameClient，AID/SessionID 使用当前登录会话。

普通业务拒绝会记录后继续下一项；登录失效、断网或异常响应停止且不自动重试。失败提示标明未确认请求的轮次和任务 ID，不计入成功或拒绝。应用重启不会自动重放任务。轮数只表示请求遍历次数，实际奖励与领取条件以游戏服务器为准。

修正手机点击主按钮后背景变暗、文字不清楚的样式问题；静态资源缓存版本为 168。

## 已完成验证

- 63 项 Python 测试、19 项前端测试通过，覆盖现有登录复用、请求参数、真实本地 HTTP 路由、账号隔离、重复提交、停止和异常处理。
- Android 模拟器手机宽度测试使用本地假接口：2 轮共 6 项，4 项成功、2 项拒绝；确认拒绝后继续下一项。停止测试在收到当前项响应后终止后续请求。没有发送真实游戏领奖或扣款请求。
- APK 覆盖安装和启动成功，登录页正常；安装包内 9 个关键后端/前端文件与交付源码逐字节一致。
- 已安装应用返回版本 1.2.3；新领奖接口未登录返回 401，删除的刷红币接口返回 404；新前端脚本可正常加载。
- Android versionCode 为 10203，APK v2 签名验证通过；与本次系列 1.2.2 包使用相同的本地 Android debug 签名。

模拟器使用 16 KB 内存页的新 Android 系统，原项目 Chaquopy 15 运行库仍会触发兼容模式提示；确认后可以运行。本版没有升级该 Python 运行库。真实账号领奖、游戏余额变化及不同实体手机尚未验证。

## 构建与测试

需要 JDK 17/21、Android SDK 34、Python 3.12。通过 ANDROID_HOME 或 android/local.properties 指定 SDK，LUCIMA_BUILD_PYTHON 可指定 Python 3.12。

```sh
cd android
./gradlew :app:assembleDebug
```

在源码根目录、装有 httpx 的 Python 环境执行：

```sh
python -m unittest discover -s tests -p 'test_*.py' -q
node --test tests/*.test.cjs
```

协议证据见 guild-protocol.md。本地界面服务 tests/serve_guild_fixture.py 不会进入 APK。源码包保留原项目 GPLv3 LICENSE，排除构建缓存、日志、账号数据、签名私钥和本机路径配置。
