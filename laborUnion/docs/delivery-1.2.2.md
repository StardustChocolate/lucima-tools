# 工会修复版 1.2.2

修复金币捐献请求：游戏原生 DonateGold 携带 ItemID="1"、Count=10，表示一次 50,000 金币。
登录、AID/SessionID 获取、游戏 Router 地址均复用原源码。不会复制浏览器凭证或接入参考工具的激活服务。

捐献任务改为后台运行：立即显示进度，按当前账号防重复提交，支持停止、部分成功记录、请求结果不明时查询。
目标留空使用当前工会；填写名称后精确核对，只对未入会账号尝试加入。不同工会不会自动退出。
只有明确勾选且全部捐献成功后才自动退会。服务器次数上限、权限与冷却保持生效。
应用重启不会重放任务；状态未知时需先核对游戏结果，再解除待确认状态。
修复会长商品 Record 请求、相关搜索/入会参数，以及同账号重登的锁顺序。

## 已完成验证
- Python 后端 37 项测试、前端 14 项测试通过；包含真实本地 HTTP 的提交/查询/取消及模拟游戏传输。
- Android 模拟器安装和启动成功；APK 内后端、页面资源可访问，版本为 1.2.2，未登录操作返回 401。
- 手机宽度界面模拟测试：提交后反馈、完成状态、停止后不再继续请求。
- 游戏公告路由连通探测返回 HTTP 200，仅证明基础网络可达。
- 测试模拟器为 Android 37，首次启动显示旧 Chaquopy 运行时的 16 KB 兼容模式提示；确认后登录页及后端正常。未改动原源码的 Python 运行时版本。
- APK v2 签名验证通过，使用此前安装包相同的本地 Android debug 签名，可覆盖本次系列旧包。

没有执行真实账号捐献、扣款或奖励发放测试。Chrome 扩展未完成安装，因此未抓取网页登录会话；请求协议依据原包静态证据，详见 guild-protocol.md。

## 构建与测试
需要 JDK 17/21、Android SDK 34、Python 3.12。设置 ANDROID_HOME 或 android/local.properties。

```sh
cd android
./gradlew :app:assembleDebug
# 可通过 LUCIMA_BUILD_PYTHON 指定 Python 3.12 路径
```

在装有 httpx 的 Python 环境执行：

```sh
python -m unittest discover -s tests -p 'test_*.py' -q
node --test tests/guild_frontend.test.cjs
```

测试界面服务位于 tests/serve_guild_fixture.py，不会包含在 APK。源代码保留原项目 GPLv3 LICENSE。
