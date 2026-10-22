# 深渊客户端自动决策迁移记录

日期：2026-10-12。对应 [架构分析](abyss-client-auto-decision-architecture.md)。已接入属性回调、当前深渊开局资料及共用条件/选人模块，并建立离线验收。首次解锁判断已补齐；烧魂界面与连续挑战暂缓。

## 已接入运行链

- 关卡列表只接受打包 Scene/Fantasy 表中的 `MysteriousRealm_*`，排除虚拟幻境 `Abyss_*`。结合当前活动、已保存场景与 `PreScene` 通关记录判断可挑战关卡；第一关的前置为 `1Ext_3_10`。没有场景记录的首次挑战按客户端抓包使用 `{StaticID, Stars:[0,0,0], PassCount:0}`，保存/开战共用判断，只有服务器结算后才写入真实场景记录并解锁下一关。最新抓包的十个首次开战均使用此默认结构。
- `backend/abyss_state.py` 从原始 Fantasy 配置按当前活动选择第一条开放记录，用真实怪物 ID、位置、Boss 标志及覆盖 AI 初始化两波阵容。Scene.LV 覆盖 Fantasy 中的怪物 LV；默认 Pos=1、LV=1，支持历史 `Pos:5`。带入 InitStar、ArtifactID/ArtifactLV、满级技能、波次 Bs/DBs 和初始冷却。
- 先应用 RoundResult，再应用 ActionResult。仅 `IsCanSyncRole != 0` 的事件更新角色；可信事件省略的状态、冷却及基本标量恢复客户端默认值，不再沿用旧状态。同步行动条但不凭空生成缺少身份的角色。
- 状态能力从 Status 的属性定义读取。只有 `IsMergeProperty` 打开时才按 `NowStack/Value` 乘算，乘数使用原生 float32 路径。保留 `AI/AllSkillReplace` 等字符串列表的全部条目。
- AI 使用原始表的运行时覆写、TauntAI、优先级及稳定顺序；负技能索引取最后一项。技能替换选择原技能容器中的记录，保留等级，普攻门槛与回退同样使用替换后的首个技能。
- 修正比较边界、Any/All 与空集合、友方目标默认包含隐身和无敌、三次独立目标回退、ConquerOK 的两层候选及普攻评分常量。烧魂执行参数与阵营魂值已接入执行器；前端目前仍使用默认关闭设置。
- 认证不再因客户端 Index 为 0 而重复。认证推进期限 30 秒，开战后 120 秒；心跳不延长期限。必须收到 BattleEndData 才确认结束。
- 收到 SceneRewardData 后合并物品绝对余额、任务、账号、角色、场景、通行证及 LineRole 更新，保留未返回字段。接口随结算返回最新资产状态。
- 新增中止挑战按钮和 `/api/abyss/cancel`。同账号战斗互斥；中止请求不等待账号任务锁，排队中已取消的任务不会建房。运行、异常、配置写入失败均在 finally 清理注册记录。切页或切换账号后按钮按对应账号恢复运行状态。
- WSS 显式使用 HTTPX 的证书配置（certifi 与 `SSL_CERT_FILE/SSL_CERT_DIR`），保留主机名和证书链验证。2026-10-12 实测 RoomServer 在 Python 默认 Windows 证书库下报 `unable to get local issuer certificate`，使用登录 HTTP 请求的证书配置验证通过。证书验证失败会转换为可读的任务错误，不重新建房或关闭验证重试。

中止关闭本地战斗连接，不自动重开；服务器的最终战斗状态需通过后续刷新确认。此轮没有添加未经验证的重连或补发策略。

## 数据构建与属性层

`tools/sync_game_metadata.py` 新增第四份产物 `assets/abyss_battle_static.json`，从 15 张原始表构建，运行时不依赖 Ark2 文件路径、原始解包目录或本地化图鉴文案。

当前版本为 `ark2-115309`，文件 2,886,026 bytes，SHA256 `b66a9fd2803eaaf84112a80843f649c19ca3f57017048ccf3e7190a4859e4596`。重复的 AI/Fantasy 行和等级复合键均保留；来源表与提取报告校验哈希。构建报告列出 selector 类型与未解析字段。构建器支持顶层双分隔符 `~~`，并保留分隔符原样；字符串内部不做替换。FH108/FH108_CanSleep 的 DirectAwakenSkill 与 CriticalRate 属性函数现已正确解析。剩余一处 H086S2 的 AfterEvent 字段缺少结束括号，保留原文标记，不补造内容；这类技能事件由服务器执行。Scene 构建保留 RoleStatusInfo1/2，当前十个深渊关卡均为空。

`backend/abyss_properties.py` 保留数值基础合成与独立审计，并在执行器中加入完整的属性回调阶段：

1. RoleProperty 等级面板 × Role 倍率 × 星级倍率；倍率转 float32，累积属性保留 double。
2. 装备副属性、主属性、可重复凑齐的套装、神器等级插值与每 3 级属性增量。
3. 自身烙印、逐级累计觉醒、按位置生效且排除同一角色实例的队伍烙印。
4. HP/Attack/Defence/Speed 的倍率、固定值、最终倍率顺序，以及原生取整与伤痕乘算。

已核对的常量：最大星级 6；神器最大等级 30；RoleStarRate 缺省值为原生 float32 `0.0560000017285347`。PublicVariable 若提供 RoleStarRate 则优先读取。依据分别是 `RoleStaticData..cctor 0x21fb484`、`ArtifactData..cctor 0x21178b8`（ELF `0xd89824`）及 `PublicVariableStaticData.get_RoleStarRate 0x217e3a4`（ELF `0xd89d38`）。

运行流程：`initialize_units → refresh_abilities(initialize=True) → sync_units → refresh_abilities(round_role) → AI`。只有开局初始化填充 HP/SP；每次己方决策前根据可信实时状态重建当前波次双方能力，保留服务器已经同步的当前 HP、状态和冷却。伤痕参与 MaxHP，Explicit MaxHP 响应优先；观测生命上限只保留为诊断信息，不用于已初始化角色的正常决策。

属性来源包括原始技能容器中被动技能的主属性、所有技能的 PassiveProp、逐级技能属性与增量、ReferanceSkillID/LevelSkillID、技能觉醒增强和 DirectAwakenSkill；SealPassive 会停止被动。装备套装、神器、烙印、觉醒与实时状态的属性函数共同参与合并。ID 按 LV、转换倍率或 Value、光环来源位置选择；AddValue 只增强已存在的函数，GID 保留最高 Value。单例神器保留等级最高的来源，同级保持阵容顺序，数值面板继续生效。

光环仅来自同波次仍存活的其它角色，尊重 AuraToAlly、NoSelf、AuraSource 和当前主回合。属性函数忽略普通技能选人 Target/TargetCamp，按 Property 回调计算 Condition/Conditions 与 ValueConvert。转换器的缺省 Who=Source、Rate=0；包括条件倍率、Base、嵌套值、额外倍率和上下限，逐步使用 float32。原生 SkillValueConvert.AddValue 中 MaxValue 自加（`s3+s3`）的行为保留为经核对的客户端规则。

11 场的 88 个己方槽位中，仅数值基础合成仍有 76 个生命值与首次可信 LastHP 一致；加入属性回调后，88 个己方和 22 个敌方的开局 MaxHP 全部与首次 LastHP 一致。12 个约 30% 的差异来自关卡职业 HPFinalRate，已消除。首次 LastHP 仍是观察值，这项核对不代表所有角色和所有战中状态已通过实战验证。

`backend/abyss_rules.py` 统一服务属性回调与 AI 条件、选人。包含 Who/TargetInfo、Any/All、ComparedValue、嵌套条件、状态组/叠层、角色职业/属性/站位、主回合和冷却判断；NextCondition/Conditions 位于当前节点 Invert 之外。前排/后排按固定位置 0/3；支持复活候选、克制、极值、随机候选和目标合法性。通用 SkillSelectInfo 的隐身/无敌默认值与 AI 独立三次重试的覆盖值分别按客户端处理。

两个名称容易误读：`CheckSkillCooldown` 为 CD<1，`CheckSkillNoUse` 为 CD>0；`LastHPRate` 实际为损失生命的百分数（100 倍），`LostHPRate` 为损失生命比例。未支持或缺少上下文的条件不会因 Invert 变成通过；执行器跳过该规则，并按每场唯一原因记录 `[abyss] rule_unavailable`。

## 离线验收

最新样本：`capture/records/capture-20261010-194908.jsonl`，11 场、216 个己方行动窗口。应用子进程只能接收开战前资料及逐条已下发消息，禁止真实网络；原客户端行动留在父进程，比较结果不反馈给决策。测试随机源固定为 seed 0。

| 项目 | 结果 |
|---|---:|
| 原始主动行动 / 生成主动行动 | 203 / 204 |
| 全字段完全相同 | 193 |
| 同一窗口内容不同 | 10 |
| 双方均跳过 | 12 |
| 漏发 | 0 |
| 额外行动 | 1 |

内容差异涉及 5 次技能、5 次烧魂及 1 次目标，字段统计可重叠。额外行动对应第八关首次录制中客户端主动投降的窗口。第 2–7、9、10 关的主动请求全部一致。开战快照、结束回执、行动窗口、连接关闭和子进程退出检查全部通过。

**固定响应不会根据新行动重新计算战果，录制 Win 不能证明本实现实战获胜。** 手动选择、烧魂设置、随机候选与实现缺口还需逐项区分。

复现命令：

```powershell
python -m unittest tools.test_abyss_rules tools.test_abyss_battle capture.test_ark2_ai capture.test_replay_abyss -q
python tools/test_sync_game_metadata.py -q
python capture/review_abyss_implementation.py --output capture/records/abyss-rules-latest.json
node --check frontend/app.js
```

回放及基础属性、完整开局属性逐项结果输出到 `capture/records/abyss-rules-latest.json`，不包含账号标识或会话令牌。原始抓包和 native 证据不进入运行包。

本轮 Python 回归共 85 项通过（76 项运行/规则/抓包隔离测试 + 9 项元数据构建测试）。本轮没有修改前端或生成 APK。构建只更新 abyss_battle_static.json，重复构建的 changed_files 为空。

## 当前边界

伤害结算、反击、技能事件和状态增加/移除由战斗服务器执行，本地仅从可信事件同步，并复刻自动决策需要的属性与选人。没有将固定录制结果当作本地战斗模拟器。

烧魂界面、首次挑战解锁及连续关卡留待后续。当前深渊没有剧情角色 RoleStatusInfo；未来若添加这类账号存档条件，会在建房前明确报错，避免带着错误的开局状态开始战斗。事件专用的 TriggerSource/TriggerTarget 等字段没有相应事件上下文时明确失败，不虚构主体。原始代码来自 Ark2 保存的较早 APK，静态表是当前 catalog 115309；新客户端若修改这些规则，需重新核验。

本轮只运行离线脚本与短时测试子进程；未启动测试服务、未发起真实战斗、未生成安卓安装包。

## LucimaTools 实测入口与旧逻辑清理

2026-10-12，主动任务「虚构深渊」统一通过 `/api/abyss/run → appcore.abyss_run → tasks.task_abyss_auto → abyss_battle.run_abyss_battle` 执行当前客户端规则实现。任务入口不再把执行器当作可选组件，也不再转换导入错误为旧的「尚未安装」提示；资源缺失或执行失败会直接报告错误并清理运行登记。

清除执行器中旧图鉴导出结构的 `SkillID/id/SkillSP/targetCamp/noSelf` 兼容读取，只使用原始战斗表字段和实时技能容器。状态同步直接调用 `abyss_state.sync_units`，移除旧的兼容转发函数。离线回放的目标审计同样改读 `abyss_battle_static.json`，不再依赖 `auto_cast_rules.json`。客户端本身的普攻回退仍属于新规则执行流程。

开发者日志新增 `[abyss]` 记录：开战关卡、数据版本、烧魂设置；每次已发送行动的来源位置、技能、等级、目标、烧魂及 AI/普攻分支；无法行动、结算和中止原因。日志不记录房间鉴权对象、会话或完整 WebSocket 报文。

接入检查共 65 项通过，覆盖 HTTP 入口通过实际任务注册抵达新执行器、失败释放运行登记、取消、状态同步、结算及资源构建。最新 11 场抓包重新回放，仍为 216 个己方窗口、193 个完整相同行动、10 个内容差异、12 个双方跳过、0 漏发和 1 个额外行动，协议及连接清理检查通过。结果保存在 `capture/records/abyss-integration-latest.json`。

源码测试需要重启已有 `start.bat` 后端以加载修改，再从「主动任务 → 虚构深渊」选择关卡和上下半队伍进行实测。此轮没有启动常驻测试后台或生成安装包；本轮已接入属性回调与共用条件/目标模块，剩余边界见上节。
