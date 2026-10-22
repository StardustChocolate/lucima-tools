# 图鉴资源与加载

全量角色索引、文字资料、列表头像和公共小图标随应用提供。卡片中的静态立绘、动态立绘和技能图标通过独立资源入口的 `/encyclopedia/` 区域按需下载到浏览器缓存。图鉴资源配置独立于软件版本检查，`start.bat` 测试版也能加载图片，并继续禁用软件更新检查。

## 状态筛选

图鉴列表的“选择状态”按钮打开多选弹窗，上方为 Buff、下方为 Debuff，每项显示本地状态图标、官方名称和勾选框。点击“应用筛选”或弹窗外的遮罩后，角色只要关联任意一种已选状态即可入选，并同时满足属性与职业条件；未选择状态时不限状态。已应用的状态图标排列在按钮后，空间不足时自动换行，悬停可查看状态名称。弹窗的“清空”修改当前选择，应用后恢复不限状态；取消、关闭按钮和 Escape 丢弃尚未应用的选择。弹窗支持键盘焦点切换、手机滚动及明暗主题，滚动条沿用软件全局的主题色和圆角样式。

`tools/build_encyclopedia.py` 从最终角色资料的全部 `Skill[].Status` 生成角色索引中的 `statuses` 去重 ID 数组，包含隐藏技能及子技能关联状态。状态名称直接取正式 `Status.Name`；公共 `ui.status_filters` 仅保存分组的 `{id,name}`，复用原有 `status_icons` 和 `status_tones`。机制说明卡不作为 Buff/Debuff，已退役的 H168S2_3 重复阶段也不作为独立选项；H168 星链标记通过仍在资料中的 H168S2_1 筛选。当前可选 42 种 Buff、25 种 Debuff。

筛选在轻量索引上完成，打开弹窗或修改条件均无需加载全部角色详情或远程立绘。本次角色索引增加 11,373 字节，公共选项增加 2,742 字节；226 份角色详情和公共小图标保持原文件。定向数据与交互测试：`python -B -m unittest discover -s tools -p test_encyclopedia_catalog.py -v`、`python -B -m unittest discover -s tests -p test_encyclopedia_status_filters.py -v`。来源及文件核验见 [状态筛选数据报告](../build/encyclopedia-status-filter-verification.json)。

## 当前发布范围

`/encyclopedia/manifest.json` 是独立的完整资源入口。当前资源版本为 `2026.10.14-catalog-115657`，覆盖 226 个角色。1,516 个逻辑资源共 848,785,371 字节；按 SHA-256 和扩展名去重后为 1,489 个实际对象、848,717,858 字节（约 809.4 MiB）。清单另计，每份 228,881 字节。来源刷新记录见 [catalog 115657 更新](asset-refresh-115657.md)。

| 类别 | 数量 | 清单中的逻辑路径 |
| --- | ---: | --- |
| 静态立绘 | 210 | `portraits/static/Hxxx.png` |
| 技能图标 | 675 | `skills/<IconSprite>.png` |
| 动态立绘 | 210 组，共 630 个文件 | `portraits/spine/Hxxx/{Hxxx.skel,Hxxx.atlas,<atlas texture pages>}` |
| 共用 Spine 运行库 | 1 | `runtime/spine-webgl.js` |

210 个角色各有静态和动态立绘。其余 16 个角色的源资源没有立绘，文字资料照常提供，立绘保持空缺。

资源池清单使用 `schema: 2`。每个逻辑路径只记录原文件的哈希和长度，实际地址由 `objects/<sha256><原扩展名>` 推导，不存逐角色绝对地址：

```json
{
  "schema": 2,
  "version": "2026.10.14-catalog-115657",
  "baseUrl": "/encyclopedia/",
  "staticPortraitBaseUrl": "portraits/static/",
  "dynamicBaseUrl": "portraits/spine/",
  "skillIconBaseUrl": "skills/",
  "spineRuntimeUrl": "runtime/spine-webgl.js",
  "files": {
    "portraits/static/H001.png": {"sha256": "<64 位小写 SHA-256>", "size": 123456}
  }
}
```

角色立绘按 ID 构造逻辑路径，技能图标按 `Skill[].IconSprite` 构造逻辑路径。加载器查清单后只下载对应哈希对象；动态播放器继续使用 atlas 原始相对纹理名称，由媒体层将虚拟路径映射到资源池，不重写 atlas 或骨骼。缺失映射保持空缺，不回退到未列入清单的网络路径。旧版无 `schema` 或 `schema: 1` 的版本目录清单仍兼容。

### 资源路径契约

```text
encyclopedia/
  manifest.json
  manifests/<资源版本>.json
  objects/<sha256>.png
  objects/<sha256>.skel
  objects/<sha256>.atlas
  objects/<sha256>.js
```

当前入口清单和版本快照均引用共享对象路径，未变化的内容可跨资源版本复用。
客户端依据清单中的逻辑路径、大小和 SHA-256 定位并验证资源，不依赖具体托管平台。
远程响应应允许应用来源的跨域读取；无效响应与缺失资源不会写入成功缓存。

## 独立资源配置

`GET /api/encyclopedia/resources` 返回本地配置的 `manifestUrl` 和 `baseUrl`。`backend/encyclopedia_resources.py` 只读取本地配置，不请求服务器、不读取软件更新状态。配置优先级如下：

1. 环境变量 `LUCIMA_ENCYCLOPEDIA_URL`，值为图鉴入口清单 URL。
2. `encyclopedia-endpoint.json`，依次检查 `ARK_ASSET_ROOT`、`backend` 和项目根目录。
3. 环境变量 `LUCIMA_UPDATE_URL` 或同样位置的 `update-endpoint.json`，从已有更新地址的同 host 推导 `/encyclopedia/manifest.json`。

没有有效配置时接口返回 `{}`，图鉴继续显示本地文字资料和空图片槽位。测试版使用项目根目录的私有 `encyclopedia-endpoint.json`；该文件已加入 `.gitignore`。以下地址仅为格式示例，不是实际服务器地址：

```json
{
  "manifestUrl": "https://media.example/encyclopedia/manifest.json"
}
```

Windows/Android 打包流程会注入私有 `encyclopedia-endpoint.json`，明确设置图鉴资源入口。软件更新域名与图鉴域名可以不同；只有没有独立配置时才从更新入口推导图鉴地址。真实地址不写入可提交代码，图鉴加载不依赖软件更新清单的 `resources` 字段。

## 卡片、图片与缓存

`frontend/encyclopedia-card.js` 和 `frontend/encyclopedia-card.css` 迁移 Ark2 样板的原始卡片结构、渲染函数和样式。`frontend/app.js` 管理页面切换、缩放和平移；`frontend/encyclopedia-media.js` 在文字卡片出现后填入原有媒体槽位，不阻塞文字渲染。

五属性和三种状态的衍生色由 `frontend/encyclopedia-palette.js` 提供，保留 Ark2 在新版 Chromium 中的 OKLCH 混色结果及透明度，运行时只使用 RGB/RGBA。这避免旧 Android WebView 的近中性色相插值差异；属性徽章圆环同样使用兼容的 RGBA。调整样板配色后运行 `python tools/build_encyclopedia_palette.py` 重生成；也可通过 `--source` 指定 Ark2 的原始样板 CSS。生成器会逐色核对可见颜色，应用端不需要混色库。

边框点光使用独立路径，随卡片高度同步原始几何，避免旧 WebView 的 `<use>` 路径样式覆盖高亮颜色和线宽。端点、徽章及点光淡出均使用显式透明度蒙版，保留原渐变范围、模糊与叠加效果；静态导出也使用同一套色板和图层。

静态模式只加载该角色的 PNG 立绘和技能图标，不下载 Spine 运行库、骨骼、atlas 或纹理页，并暂停卡片的星海、圆环与 SVG 点光效果。切换动态模式后，才由随应用发布的 `frontend/encyclopedia-player.js` 下载并播放 `.skel`、`.atlas` 及 atlas 声明的纹理页；`spineRuntimeUrl` 只指向运行库，无需额外的远程播放器文件。

静态与动态立绘共用取景算法 `frontend/encyclopedia-framing.js`，它与 Ark2 的 `out/assets/spine/portrait-framing.js` 保持同字节。算法采样完整 `00_Idle` 动画的可见附件边界，在固定 400×520 逻辑画布内计算中心和视口，不加额外边距。静态 PNG 使用该镜头绘制 `LV1 / 00_Idle / t=0`，输出 800×1040 并保留透明画布；动态首次绘制同样从时间 0 开始。图片和画布切换直接显示已完成的帧，避免淡入时叠加。

缺图、下载失败或图片不能解码时，静态立绘区域和原有 68×68 技能图标槽位保持空缺，不显示占位图，也不把列表头像当作卡片立绘。动态立绘加载期间或失败时，可以继续显示已经加载的静态立绘。离开卡片会停止播放器、销毁媒体实例并回收图片的 object URL；稍后完成的旧请求不会填入新卡片。

哈希对象优先读取共享的 `lucima-encyclopedia-objects-v1` Cache Storage，缓存键为完整对象 URL，与整体资源版本无关。清单升级但文件哈希不变时，刷新页面也不会重新下载；只改变某个技能图标时只下载该图标。首次下载校验 SHA-256，成功后才写入缓存，损坏响应与 404 不写入；缓存命中不重复计算哈希。同 URL 的同时下载合并，缓存不可用时仍可使用已校验的下载。旧格式资源继续使用 `lucima-encyclopedia-resources-<version>` 缓存。本地角色 JSON 不放入远程缓存。更换域名属于 URL 变化，首次资源地址迁移不会自动复用旧服务器域名下的缓存。

入口清单单独存入 `lucima-encyclopedia-config` 缓存。正常访问时检查入口清单，网络失败或新清单格式无效时使用此前的有效清单。对象 URL 按内容永久稳定；资源版本更新不更换未变化对象的地址。

公共资料、能力、潜能、阵营、状态和星源小图标仍来自应用 assets。`assets/encyclopedia/ui.json` 的 `small_icons` 将 Ark2 样板的公共图标路径映射为本地 URL；`job_icons`、`faction_icons`、`status_icons` 直接提供本地 URL，不经过远程图片加载。

## 来源与暂存

Ark2 的 `tools/build_character_archive.py` 先从 Unity 解包的 `sd_*.txt` 源表生成 `out/assets/团员/资料_原始/Hxxx.json`，再结合游戏本地化生成处理版 `out/assets/团员/资料/Hxxx.json`。内存恢复的客户端代码用于核对 DSL、倍率和 AI 规则，生成器不直接读取内存镜像；完整证据边界见 Ark2 的 `tools/character_json_pipeline.md`。软件只导入处理版资料。

```powershell
python tools/build_encyclopedia.py --ark2-root E:\Project\ark2
```

构建器和资源暂存器以正式 `资料/character_index.json` 的 `characters` 数组为清单，核对角色 ID 集合、同名字段和资源标志；资料必须为 `schema_version: 2.0-processed`。全部角色文字与机制字段写入应用的 `assets/encyclopedia/characters/Hxxx.json`，本地索引按编号排序。`translation_map.json` 只提供公共图标和文案映射。

前端读取原字段 `NAME`、`Description`、`Description_Markup`、`Skill`、`Mechanics`、`Scaling` 等，不补写翻译字段或 `skills` 副本。技能 `Description` 保存本地化原文，卡片正文使用 Ark2 处理后的 `Description_Markup`；`Description_Final` 不再生成。`HiddenSkillIDs` 控制卡片隐藏技能，完整技能数据仍保留。技能 `IconSprite` 为图片 basename，缺图时可为空串；动态立绘元数据使用规范化 `Base=Hxxx`、`Skin=LV1`、`Animation=00_Idle`。

全量暂存命令如下，输出必须是新的空目录：

```powershell
python tools/stage_encyclopedia_resources.py --ark2-root E:\Project\ark2 --output build/encyclopedia-resources/2026.10.08-catalog-115309
```

省略 `--character` 时暂存全量角色资源和归集后的全部技能图标；局部发布时可重复指定 `--character Hxxx`，只收集指定角色引用的技能图标。脚本从 Ark2 的 `out/assets/团员/立绘`、`动态立绘`、`技能图标` 及 `out/assets/spine/spine-webgl.js` 读取文件，并检查技能引用、骨骼、atlas 和纹理页是否齐全。它只准备本地目录，上传资源和切换服务器入口清单是独立发布步骤。

重建立绘使用 Ark2 的 `python tools/spine_webgl/batch.py`，可以通过 `--only H001,H002` 指定角色或 `--output` 先生成到隔离目录。批量、单图和资源刷新入口均调用同一个渲染页面。生成目录的 `_render_profile.json` 记录渲染器、共享取景脚本、运行库、参数和每个角色源资源与 PNG 的哈希；旧图缺少记录或任一指纹改变时自动重新生成，避免仅因游戏 bundle 未变就复用旧镜头。详细规则见 Ark2 的 `tools/portrait_framing.md`。

## 旧清单兼容

无 `schema` 或 `schema: 1` 的旧清单继续通过 `baseUrl` 解析版本目录中的资源。
新版 `schema: 2` 通过哈希对象映射读取共享资源池；缺失、损坏或无法解码的资源保持空缺，
不会回退到未列入清单的网络路径。
