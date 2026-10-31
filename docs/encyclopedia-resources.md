# 图鉴资料与自动更新

LucimaTools 在本地保留卡片渲染、筛选、缩放、导出、动态播放器及其运行库。角色索引、详情、公共文案映射和图鉴图片从独立云端入口获取；安装包不再包含 `assets/encyclopedia/`。源码中原目录是此前版本的生成快照，当前运行逻辑不读取它，新的生成结果位于 `build/encyclopedia-bundle/`。

## 云端契约

新版通过 `/encyclopedia/latest.json` 获取资料版本，当前版本为 `2026.10.28-data-1`，包含 227 名角色。入口实际为 454 字节，只含 `schema`、`kind`、`renderer`、`version`、`index` 和 `ui`。两个资料引用都采用 `{path, sha256, size}`；`path` 是同目录下的 `objects/<sha256>.json`。

- 索引保存名字、别名、属性、职业、星级、状态等列表与筛选字段，每名角色分别引用头像和详情。当前未压缩索引为 144,780 字节。
- 公共资料保存渲染所需的文案与图标映射，以及共用图片的哈希引用；当前为 62,283 字节。映射的原游戏资源键保留，值使用对象相对路径。
- 单人详情使用 `{schema: 1, character: {...}, media: {...}}`。`character` 保留 Ark2 处理版正式字段及本地渲染器需要的标签；`media` 只列该角色的立绘和技能资源，以逻辑路径映射到对象引用。
- 图片、骨骼和图集复用现有对象池。新增资料不重复上传已有媒体；新版不从云端加载 JavaScript。
- 入口 `schema: 1`、`renderer: 1` 标识当前数据与渲染契约。超出客户端能力的版本保留旧缓存并提示需要更新软件。

```text
encyclopedia/
  latest.json                    # 新版自动更新入口
  versions/<数据版本>.json         # 不可变入口快照
  objects/<sha256>.json           # 索引、公共资料、单人详情
  objects/<sha256>.png            # 头像、公共图标、立绘、技能图
  objects/<sha256>.skel
  objects/<sha256>.atlas
  manifest.json                  # 旧客户端媒体入口，继续兼容
  manifests/<媒体版本>.json
  cards/                         # 对外提供的成品 PNG 及查询表
```

图片、描述变化时按新内容生成哈希对象。版本不可原地改写；发布时先上传并核验对象与快照，最后切换 `latest.json` 并刷新入口缓存。回退时将入口切回已有快照。旧对象保留，供正在查看的旧卡片和旧版软件使用。旧媒体与成品卡片入口独立维护，本次发布没有改变它们。

## 检查与按需下载

首次使用图鉴时优先显示上次有效缓存，并在后台检查轻量入口；无缓存时先取得入口、索引和公共资料。进入图鉴、恢复前台时按全应用共享的一小时间隔检查；图鉴保持可见时自动安排下一次检查。页面的“同步”按钮支持立即检查，重复请求合并并短暂防抖，不按账号分别检查。

入口使用 `Cache-Control: no-cache`，保存 ETag 并在后续请求发送 `If-None-Match`。版本不变时不重新下载角色资料和媒体；未返回 304 的服务器也只需重传小入口。失败按 2、4、8、16、30 分钟退避，图鉴可见时自动重试，手动检查可提前重试。

列表头像沿用浏览器懒加载。筛选只使用索引，不获取全部详情。打开卡片才取得该角色 JSON、静态立绘和技能图标；启用动态模式后才取骨骼、atlas 及其纹理。共用图片也由实际渲染需求触发。所有对象首次下载先校验大小与 SHA-256，校验失败不会覆盖有效缓存。

## 缓存与版本切换

`backend/encyclopedia.py` 在应用数据目录的 `encyclopedia-cache/` 中保存对象、入口快照、当前版本和检查状态；不写入安装目录，也不依赖浏览器端口或单个账号。Windows 和 Android 共用该实现，并使用应用的系统代理设置。

索引和公共资料全部下载并校验成功后，才原子替换当前版本入口。前端正在查看卡片、导出图片或打开状态选择框时，保留当前视图的数据版本，关闭后再应用等待中的新版。详情内存缓存使用内容哈希作为键；相同角色 ID 的新资料不会命中旧数据。

已有缓存可离线使用。首次安装离线时提示“暂时无法获取图鉴资料”；没有下载过的详情或图片不会伪造占位资料。清单、数据格式、网络或哈希异常继续使用此前有效版本。缺少官方立绘的 16 位角色继续保留空图槽。

本地 API：

- `GET /api/encyclopedia/catalog`：只读取本机已激活版本，不主动联网。
- `POST /api/encyclopedia/check`：执行按间隔检查，`force: true` 表示手动检查。
- `GET /api/encyclopedia/detail?version=...&id=Hxxx`：按指定快照下载或读取单人详情。
- `GET /api/encyclopedia/objects/<hash.ext>`：只允许访问已验证资料引用的对象，命中本机缓存时不联网。

## 来源与构建

源资料继续来自当前 Ark2 项目的官方表和处理版 `out/assets/团员/资料`。不依赖旧 OpenRubi 项目，也不自行编写或缩写游戏文案。

日常使用统一构建入口，资源版本独立于软件版本。默认从同级 `ark2/` 读取当前提取资料，可用 `--ark2-root` 指定其它位置：

```powershell
.\build-encyclopedia.bat --version <新资源版本> --cards
```

此命令依次整理资料、头像和媒体，生成云端资料，并使用当前前端渲染器生成整套成品 PNG 和查询表。头像直接来自本次 Ark2 提取源，不依赖安装包内的旧头像。构建需要 Pillow；生成卡片还需要 Playwright（Chromium）和 pypinyin。

- 省略 `--cards`：只更新云端资料与媒体，已有成品卡片入口不参与此次发布。
- 增加 `--reuse-media`：复用现有媒体清单，适合没有图片变动的资料更新；新资料引用的媒体仍必须完整存在。
- `--from-existing --cards`：只校验当前已有产物并生成发布计划，不重新提取或渲染，可省略 `--version`。

中间结果写入 `build/encyclopedia/<资源版本>/`，最终文件仍位于 `releases/encyclopedia/`。全部引用文件、角色集合和版本快照校验通过后，才生成该目录的 `.release-plan.json`；它记录此次要发布的文件与入口，不上传云端。构建命令不联网、不上传文件。

发布工具在本机 `deploy/encyclopedia/` 维护，不随软件源码仓库分发。先预演，再执行上传：

```powershell
.\deploy\encyclopedia\publish.bat --dry-run
.\deploy\encyclopedia\publish.bat
```

预演重新校验本地发布计划及全部文件，不联网、不读取云凭据。实际发布只处理计划引用的文件，先上传并校验不可变对象及其公开下载内容，再切换媒体、可选卡片和新版资料入口；`latest.json` 最后切换。中断状态与入口备份保存在 `deploy/encyclopedia/state/`，重跑命令继续处理缓存刷新。图鉴上传复用本机私有配置，独立于软件发布通道。

底层 `build_encyclopedia.py`、`stage_encyclopedia_cdn.py`、`build_cloud_encyclopedia.py` 和 `build_encyclopedia_cards.py` 继续供单步构建及排查使用。角色数量由生成索引决定，无需为新角色修改发布脚本。不同内容仍须使用新资源版本，不覆盖已发布快照。

本地服务地址沿用忽略提交的 `encyclopedia-endpoint.json` 或环境变量 `LUCIMA_ENCYCLOPEDIA_URL`。配置可以指向 `latest.json`；已有 `manifest.json` 配置会解析为同目录下的 `latest.json`。软件更新检查与图鉴检查彼此独立，`start.bat` 也支持图鉴更新。

Windows spec 与 Android `copyWebAssets` 均排除 `assets/encyclopedia/**`。其它业务所需的角色头像、属性和职业小索引仍由公共游戏资源提供；基本信息页职业筛选读取独立的 `assets/role_jobs.json`，不再读取图鉴索引或等待图鉴网络请求。

## 验证

`tools/test_encyclopedia_cloud.py` 覆盖轻量检查、ETag、退避、并发、损坏/不兼容资料、原子切换、历史版本及离线重启。`tools/test_encyclopedia_cloud_ui.py` 覆盖详情哈希缓存、失败重试与卡片版本固定；`tools/test_cloud_encyclopedia_build.py` 核对云端资料保真、对象复用及版本不可改写。原有图鉴集合、媒体池和移动视口测试继续适用。

本轮用真实前端、HTTP 服务和本地云镜像检查了 227 人列表、H806 静态/动态立绘、PNG 导出、打开卡片期间更新和重启后离线使用；脚本错误为 0。公开云入口、索引、公共资料、H806 详情及媒体均已再次下载验证。没有构建或发布软件安装包。
