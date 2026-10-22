# 游戏运行数据刷新

`assets/sync_full.py` 在补齐运行图片之前，先从同一份 `assets_full` 构建技能静态索引、自动施放规则、深渊战斗定义及装备引用，并完成商品表和全部图片映射预检。源数据或必需资源不一致时，此入口在写入运行资源前停止。通过预检后，先保存新装备引用，再按新清单同步图片；已有头像按当前源文件更新，内容相同的不改写，以免上线前的小熊占位头像长期留在软件中。其它类别的已有压缩图及源库缺失的旧图继续保留。

独立构建入口为 `tools/sync_game_metadata.py`。在项目根可以先生成隔离结果：

```powershell
python tools/sync_game_metadata.py --output-root build/asset-refresh-audit --report build/asset-refresh-audit/report.json
```

`--source` 指定完整资产树，默认 `assets_full`；`--data-root` 可指定 Ark2 处理版资料，默认 `<source>/团员/资料`。其同级 `资料_原始/manifest.json` 必须和 source 中的原始表哈希一致。`--previous-root` 指定需要保留历史信息的现有运行资源根目录，默认项目根。省略 `--output-root` 时写入正式 `assets/` 和 `backend/`。

## 产物及来源

| 产物 | 直接来源 | 保持的语义 |
| --- | --- | --- |
| `assets/ark2_skill_static.json` | `sd_Skill.txt`、`sd_Role.txt` | 技能原字段、数值字符串、完整 Role 及 `SkillIDs` 的 Hide 标志；普通技能 ID 去除展示后缀 |
| `assets/auto_cast_rules.json` | 正式角色 `资料/Hxxx.json`、`sd_Role.AIs` | 使用处理版 `AutoCast` 的条件、目标、优先级、技能索引和顺序；轻量技能读取最终冷却/星源范围及 `Targeting`，缺源技能保持 null |
| `assets/abyss_battle_static.json` | 15 张原始表：Role、Skill、Status、Fantasy、AIAction、Scene、Activity、RoleProperty、RoleAwaken、RoleImprint、SkillLevel、Equipment、EquipmentSet、Artifact、PublicVariable | 原始决策与属性定义、解析后的 Dynamic DSL；保留重复 AI/Fantasy 行顺序及等级复合键，不使用图鉴翻译反推规则 |
| `backend/equip_ref.json` | `sd_Equipment.Part/EquipLV`、`sd_Item.Name`、官方简中文本、实际装备图片 | 精确部位和等级；官方名称只移除显示标签与多余空白；没有源图的新增装备保留空图片字段 |

`rolesByStatic` 按正式 `Role.ID` 建立，不再让共享技能的 NPC 行遮住同 ID 英雄的属性、职业及 AI。全量原始 Role 索引仍单独保存在 `roles`；`rolesByStatic` 不用战斗数值倍率冒充角色的当前攻击力。

装备引用保留原表已不包含的历史 ID（当前为 12 个 V ID），及现有各项的额外人工字段；当前表已提供的等级、部位、名称和图片按游戏来源更新。无法本地化的新名称保持空白，不根据 ID 或图标补写。如果既有图片缺少新源但仍在运行目录中，保留其明确的既有引用并记录。

## 来源校验与重复执行

生成前核对原始资料 manifest 中全部源表哈希、文本提取报告的表/简中哈希、原表正式角色集合、轻量角色索引和处理版文件集合。处理版 schema 必须为 `2.0-processed`；技能集合、目标字段和每条自动施放的技能索引还须与原表一致。

技能索引的 `version` 默认从 `文本/文本提取报告.json` 的 catalog 文件名推导，例如当前的 `ark2-115309`，同时记录真实的表哈希、catalog 构建哈希及 staticdata 内容哈希。没有提取报告时使用源表哈希标签，不继续标注旧 `0513` 来源。可以用 `--source-version` 指定标签；它不能绕过任何哈希校验。

输出先全部构建和序列化，再逐文件原子替换。内容相同时不改写文件。独立入口按旧 SHA256 保存覆盖前的四份 metadata 到输出根的 `build/game-metadata-backups/`；报告默认写入 `build/game-metadata-refresh.json`，含来源、缺图/缺名、历史保留项、逐索引差异及输出哈希。`battle` 部分另含各源表哈希、规则类型统计和未解析 DynamicField 清单。出现未解析属性时保留显式标记；这不表示对应战斗语义已经支持。

深渊执行器读取新的原始战斗定义；`auto_cast_rules.json` 继续作为图鉴等展示资料。完整的解析器仅在构建时使用，运行包只读取生成的 JSON。Android 通过 `ARK_ASSET_ROOT` 定位随应用解包的文件。实现与验收边界见 [深渊迁移记录](../docs/abyss-implementation.md)。

正式刷新命令仍为：

```powershell
python assets/sync_full.py
```

角色图鉴的应用文本和公共小图标继续由 `tools/build_encyclopedia.py` 单独生成；远程图鉴素材暂存及高清立绘收藏不属于本入口。刷新后重启现有应用或测试后端即可重新读取缓存的运行索引。

2026-10-08 已从 catalog 115309 正式同步 1,926 条技能、1,873 条 Role 索引、226 份自动施放规则、694 条装备引用和 4,498 条商品配置。694 条装备引用包含 682 条当前源记录及 12 条保留的历史 V ID；装备源表本轮没有变化，17 个旧引用补齐、E0391–E0396 等级及四个 `rolesByStatic` 身份修正属于同步缺口修复。来源、备份、差异及资源发布记录见 [刷新报告](../docs/asset-refresh-115309.md)。
