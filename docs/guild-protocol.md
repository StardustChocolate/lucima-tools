# 佣兵团接口与集成边界

## 当前页面与登录流程（2026-10-16）

- 以登录快照 `PlayerGuildInfo.GID` 是否非空判断是否入团。未入团不发送入团后的查询；新加入后需重新登录取得新快照。
- 每次成功登录、每日重新登录及手动重新登录后，复用账号调度器和账号锁执行一次完整进入流程。页面读取缓存，切换页面不重复发请求；右侧刷新按钮重新执行同一流程。
- 登录后的佣兵团信息同步成功时静默，失败时记录日志；手动刷新保留成功提示。实际报到、领奖和捐赠的结果仍记录日志。
- 信息页显示团徽、佣兵团名称、等级、人数、刷新按钮，以及“自动签到”“自动捐赠”开关。无重复的页面内标题、账号名和更新时间；任务状态放在各自开关描述下方，信息栏不追加同步或错误提示，操作结果记录日志。任务、商店和手动领奖入口暂不展示，旧接口保留待后续重构。
- 团徽由 `GuildData.Info.Icon` 与 `FrameIcon` 组合。徽记读取 `UI80_Guild_Sign_*` UI 图，边框经 `sd_Item.Icon` 映射到完整 UI 图，由 `tools/sync_assets.py` 统一提取到 `assets/guild`；不使用背包中的小尺寸边框缩略图。缺失图层时隐藏该图层。
- “自动签到”按账号保存，默认关闭，只控制 `GuildMemberDayCheckReward` 日奖励领取；每日首次进入时的 `GuildMemberCheckIn` 自动报到独立于开关。
- 切换日奖励开关只读取缓存中的领取条件，不查询佣兵团信息。开启后符合条件时仅发送领奖请求；同步信息与领取奖励使用独立状态，领奖不触发刷新图标旋转。

完整进入顺序来自 [2026-10-15 抓包分析](guild-capture-20261015.md)：

| 顺序 | 路由 | 附加字段 / 条件 |
| --- | --- | --- |
| 1 | `AccountHandler.QueryAccountInfo` | 无 |
| 2 | `GuildHandler.QueryFullGuildData` | 无 |
| 3 | `GuildWarHandler.QueryFullGuildWarData` | `Version_GVG` 开放且团等级达到 `GuildWarLessLV` 时 |
| 4 | `ServerStatusHandler.QueryGuildStatus` | 无 |
| 5 | `GuildInvitationHandler.QueryInvitation` | `IsQueryByGuild: 1` |
| 6 | `GuildHandler.GuildMemberCheckIn` | 当天尚未报到时 |
| 7 | `GuildHandler.GuildMemberDayCheckReward` | 开关已开启且日奖励可领取时 |
| 8 | `GuildHandler.DonateGold` | 自动捐赠已开启、勾选金币且当日有额度和足够资源时 |
| 9 | `GuildHandler.DonateCourage` | 自动捐赠已开启、勾选英勇证明且当日有额度和足够资源时 |

所有请求使用当前账号的 `AID`、`SessionID`。前五步只刷新信息，不重新登录。

2026-10-19 低等级账号抓包 `capture-20261019-085657` 的 cdp-65～68 顺序为
账号资料、完整团资料、团状态、邀请，1 级团跳过团战查询；本人当天已报到，未再发送报到。
客户端 `QueryFullGuildDataWithGuildWar` 的查询回调（RVA `0x218f990`）同时检查
`Version_GVG` 开放状态及 `GuildInfo.GuildLV >= GuildWarInfo.MiniGuildLV`，不满足则直接返回完整团资料。
最低等级由 `sd_PublicVariable.GuildWarLessLV` 提供，当前为 6。
运行时读取随资源同步生成的 `assets/game_config.json`；该文件只保留用到的公共参数与活动静态标志，
活动实际开放时间仍使用登录快照。团战明确返回 `Low GuildLV` 时继续后续普通进入流程，
不触发整团重试；认证失效、网络失败及未知错误仍走原有错误处理。

### 报到与日奖励条件

- `QueryFullGuildData` 中的 `SelfGuildInfo` 在抓包中是默认记录，不能作为本人状态。按 `Info.CUID` / 玩家对象 ID 在 `GuildData.MemberList` 找到本人的 `PlayerGuildInfo`；报到与领奖响应中的 `PlayerGuildData.PlayerGuildInfo` 继续更新该缓存。
- 报到比较 `LastCheckInTime` 与服务器 `Utc` 对应的游戏日。与本项目已有日签一致，以北京时间 08:00 为日界线；同日不重复请求。缺失时间戳时报告状态无法确认，不盲目报到。
- 日奖励要求已完成本次报到、本人 `CanLastDayCheckReward == true`，且 `GuildData.Record.LastCheckInCount > 0`。不能用今日签到人数代替昨日人数；缺失昨日人数时不领取。
- 报到与领奖响应必须确认状态已更新。进入流程中的查询或报到失败后等待 5 分钟重新执行完整查询，确认前次是否已完成后决定是否重发报到。
- 领奖超时或状态未确认时，保留缓存并暂停领取，等待下次登录或手动刷新确认；切换开关不会额外查询或重复领奖。正常同步成功后重新按最新缓存判断是否可领取。
- 后台同步和手动刷新共享账号锁；前端按账号、登录版本和数据版本隔离缓存，避免迟到的响应覆盖当前账号。

### 自动捐赠

- 佣兵团页增加独立的“自动捐赠”开关，默认关闭；选项中可分别勾选金币、英勇证明，默认均勾选。配置按账号保存，与日奖励开关独立。
- 保留资源默认金币 200,000、英勇证明 10，来源为 catalog 115657 的 `sd_Quest.txt` 中 `GuildDonatedGold` / `GuildDonatedCourage` 的 `ConditionCount`（`ConditionID` 分别为道具 `1` / `28`）。缺失或非法配置使用默认值，已设置的非负整数（含 0）优先；零保留值必须显式保存。
- 登录进入流程成功后、开启开关或更改选项后，以及后台正常缓存复查时检查是否可捐赠；不额外查询佣兵团。未入团、当天尚未报到或缺失有效捐赠计数时不捐赠。每日随现有每日重登完成新一天报到，从响应取得重置后的计数，不在本地清零。
- 金币请求为 `GuildHandler.DonateGold {ItemID:"1",Count:N}`，每次 5,000 金币，每日最多 10 次；英勇证明为 `GuildHandler.DonateCourage {ItemID:"28",Count:N}`，每次 1 个，每日最多 3 次。每类合并为一次请求，`N = min(每日上限 − 今日已捐次数, floor(max(0, 当前库存 − 保留数量) / 单次消耗))`；不足一次不请求。后续库存更新时，可按剩余额度继续补捐。
- 成本、道具 ID 与响应结构来自 2026-10-15 抓包 cdp-90、cdp-93。每日上限来自 Ark2 本地客户端 `Game.PlayerGuildInfo..cctor`（RVA `0x2193dcc`），静态字段偏移 `0x10` / `0x14` 为 10 / 3；金币单次成本在 `GuildDonatedPanel.RefreseGoldCount`（RVA `0x2775ed8`）中为 `0x1388`（5,000）。本轮离线证据保存在本地 `build/guild-donation-evidence`，不打包。
- 本人计数从响应 `PlayerGuildInfo` 回写，资源余额沿用 `GameClient` 对 `CostItems[].NowItem` 的统一合并；不扣减推算库存，也不将 `GuildDropResult` 的团内资源写入个人背包。
- 捐赠进行中单独显示状态，不转动信息刷新图标。成功或失败记录日志；响应未确认时暂停该资源，等待下次登录或手动刷新确认后再决定，另一资源仍可独立执行。切换开关不取消确认等待，也不重复发送未确认的捐赠。

本轮额外独立核对了 Ark2 当前本地客户端的 IL2CPP 元数据与 ARM64 代码：

| 客户端方法 | RVA | 已核对的行为 |
| --- | --- | --- |
| `Game.PlayerGuildInfo.get_CanCheckIn` | `0x2192648` | 比较上次报到日期与 `TimeHelper.NowTime` 的日期 |
| `Game.GuildManager.get_PlayerSelfGuildInfo` | `0x218ab5c` | 从成员列表查找本人记录 |
| `GuildHallPanel.OnDayCheckReward` | `0x2778eb8` | 要求 `GuildRecord.LastCheckInCount`（字段偏移 `0x18`）大于 0，以及本人领取标志为 true |

日期比较本身已核对；`TimeHelper` 的完整时区转换本轮未重新追溯，08:00 日界线沿用项目现有日签规则。上述 RVA 仅对应当前本地客户端构建。离线测试覆盖请求顺序、重复进入、已在游戏领取、开关状态、账号切换、失败重查及手机/桌面明暗主题布局；未用真实账号执行新请求。

## 早期集成来源与保留接口

功能来源：[PR #2](https://github.com/StardustChocolate/lucima-tools/pull/2)，提交 `45c0756b35d381e41aeeca72abc8106dd2ffb63d`，作者 `sk <710596173@qq.com>`。

PR 作者记录的协议证据来自第三方参考工具的 ARM64 AOT 反汇编（`libapp.so`、`full.asm`、`pp.txt`）。下述旧接口与地址保留用于核对其记录；本轮没有独立重做该参考工具的反汇编，也没有使用真实账号验证奖励或购买。它们不是当前游戏客户端的通用偏移，当前页面以本文开头的流程为准。

### 早期集成范围

- 纳入佣兵团查询、签到、领取日奖励、逐项领取任务奖励、团长商店。
- 使用现有 `GameClient.call` 和 `_auth_data`，复用当前账号会话；没有独立登录、激活服务或固定凭证。
- 未纳入 PR 的参考工程副本、旧版发布文档、自动入会、退会、捐献及援助流程。
- 登记登录会话后仍由前端显式激活调度器。账号锁保护每次游戏请求，在请求间隔释放，使定时任务可以执行；进度查询和停止不等待该锁。
- 任务与请求 ID 去重保存在当前进程。重启后不恢复、不重发任务；前端保留未确认 ID，需核对游戏结果后解除。
- 领奖后直接读已有账号缓存同步资源。嵌套 `Results` 的余额与任务更新由现有 `GameClient` 统一合并。
- 离开页面、切换账号或隐藏页面时停止界面轮询，后台领取任务继续执行，直到完成或主动停止。

## 路由

所有请求附加当前会话的 `AID`、`SessionID`，表中省略。

| 路由 | 附加字段 / 用途 |
| --- | --- |
| `GuildHandler.QueryGuildDataForSync` | 同步佣兵团及任务 ID |
| `GuildHandler.QueryFullGuildData` | 全信息与商店购买记录 |
| `GuildWarHandler.QueryFullGuildWarData` | 佣兵团战信息 |
| `ServerStatusHandler.QueryGuildStatus` | 服务端佣兵团状态 |
| `GuildHandler.GuildMemberCheckIn` | 签到；先依次查询全信息、团战、服务端状态 |
| `GuildHandler.GuildMemberDayCheckReward` | 日奖励 |
| `QuestHandler.RewardQuest` | `RewardQuestInfos: [{ID: 任务ID, Index: 0}]` |
| `GuildItemHandler.BuyGuildCommodity` | 以下购买记录与平台字段 |

签到顺序证据：`0x55914c`～`0x55916c`。查询解析：`0x540518`；佣兵团名称与 ID：`GuildData.Info.Name`、`GuildData.Info._id`（`0x542208`）。任务列表：`GuildData.Record.GuildQuestIDList`（`0x6ef458`、`0x542100`）。

## 历史手动任务奖励接口

旧 PR 的手动任务接口仍保留兼容，页面不再使用。它先查询并去重任务 ID，随后按照配置轮数逐项请求（默认 30 轮，上限 100）。新周任务自动领取完全不调用此轮询逻辑，规则见下文。

佣兵团「主动任务 → 获取团积分」使用同一组任务接口的 `mode:score`，
替换固定轮数为最高积分奖励档位作为停止目标（当前为 2800）。开始按钮旁有警告标识，
每次开始都须确认「该功能非常规功能，请在明确功能含义的情况下执行，建议使用非正式账号」。
此功能只会手动启动，不随登录启动、不在应用重启后恢复。

开始先轻量同步佣兵团，随后只选服务端本周 `GuildQuestIDList` 中本人已完成且进度可信的任务，
包含已经领取过的已完成任务，排除未完成、未知、未确认的记录。单次积分由资源生成器读取
`sd_Quest.GuildDrops` 关联的 `sd_Drop` 中物品 82 的固定掉落数量，保存在目录的 `points` 字段。
优先选择固定积分最高的一项，按 `ceil((目标积分 - 当前积分) / 单次积分)` 计算领取次数，
连续重复发送 `RewardQuest`。没有已知固定积分的任务不参与批量领取。
从本团、本周积分档位的 `QuestData.Count` 读取实际积分，不将预计积分写入团内缓存。
若中途响应或已有同步确认达标，会提前结束领取。不自动领取积分档位奖励。
已开启普通周任务自动领奖时，执行期间暂停该流程，结束后恢复原开关行为。
重复领取的响应可能只有默认 `PlayerGuildInfo` 与 `Utc`，没有档位进度；不为每次领取补查。
本轮领取结束后统一通过一次 `QueryGuildDataForSync` 校验实际积分，只有达到目标才标记完成。
未达标或校验失败则提示并停止，不自动追加领取。业务拒绝、连接异常、
会话/佣兵团/周编号变化时立即停止后续请求。
页面显示已领取次数与预计次数，积分仍展示最新已知的真实值。
每笔请求沿用设置中的请求延时，支持停止正在等待的后续请求；已发送的请求等待返回后结束。
开始请求通过 UUID 去重，启动响应丢失时前端只查询该 UUID，不重复创建任务。
任务状态保存在进程内，页面仅在可见时向本机接口查询进度，查询不会向游戏发送请求。

请求证据：`0x9f5c70`、`0x9f5e30`、`0x565970`、`0x565aec`。响应入口：`0xccbfd4 -> 0x564c84`。

| 字段 | 类型与空值 |
| --- | --- |
| `Results` | 对象数组；缺失 / null 按空数组处理 |
| 每项 `Drop` | 对象或 null |
| 每项 `Quests` | 对象数组或 null |
| 每项 `GuildQuestDataList` | 对象数组或 null |
| 每项 `HasMail` | 布尔值或 null |

`{}`、`{"Results": null}`、`{"Results": []}`、`{"Results": [{}]}` 均接受，计入“无明细”，不等同于奖励到账。类型错误停止；明确业务拒绝记录后继续下一项。登录失效、重复登录、断网、未知响应停止，不自动重试。设置中的请求间隔与随机偏移应用于相邻领取请求，等待可立即取消。

仅明确的错误标志（`_isErrorResponse`、false 的 success、非空 Error 等）触发业务拒绝。`ErrorCode` 或 `Reason` 单独出现不视为失败。返回详情只展示字段名、类型和数量。

## 团长商店

先查询 `GuildData.GuildStoreRecord.Records`（兼容顶层 `GuildStoreRecord`），按 `StaticID` 与 `Store == "CaptainShop"` 取记录；不存在时使用默认值。商品为 `GuildBox`、`GuildHeroExp`、`GuildMoneyBonus`、`GuildEnergy30`、`GuildEnergy50`。

```json
{
  "Record": {
    "_id": "", "StaticID": "GuildBox", "Store": "CaptainShop",
    "BuyCount": 0, "FreeBuyCount": 0, "GuaranteedCount": 0,
    "SummonRoleID": "", "SummonRoleIDs": [],
    "LastBuyTime": "0001/1/1 0:0:0", "LastResetTime": "0001/1/1 0:0:0"
  },
  "Count": 0, "ItemIndex": -1, "LoginType": "ID_Password",
  "NewErolabs": 0, "Platform": "Android", "SelectRoleID": ""
}
```

请求构造：`0x572508`～`0x5726d0`；记录查找：`0x572728`；序列化：`0x569770`。`Count: 0` 是参考包字段，不能解读为购买零件。普通非空时间字符串保留；空值 / `0001/` 使用默认；毫秒整数 / `$date` 按 UTC 转成不补零的年月日时分秒（`0xbe4e8c`、`0x572de0`）。

购买身份、价格、限购和实际发奖仍由游戏裁定。离线测试仅验证路由、字段、缓存同步、并发、停止和页面行为。

## 自动求助与援助

抓包依据：`capture-20261015-215651` 的 cdp-79/80/81/83。发布求助使用
`GuildHandler.RequestGuildAid`，参数为 `ItemID`；援助使用
`GuildHandler.SupportGuildAid`，参数为 `GuildAidItemInfoID`（求助记录 ID）。

`LastRequestGuildAidTime` 按游戏日期判断每日一次；客户端
`PlayerGuildInfo.get_CanRequestGuildAid`（RVA `0x2193a44`）比较游戏日期。
援助检查 `SupporterList` 中是否有本人 CUID，排除自己的求助和已经满额的记录。
客户端 `GuildAidItemInfoUI.SupportGuildAid`（`0x2770b68`）限制
`DaySupportGuildAidRewardCount + 本次 RewardCount <= 200`，并检查库存是否足够。

`assets/guild_aid.json` 由 `tools/build_guild_aid.py` 根据 `sd_GuildAidItem.txt`、
`sd_Item.txt.Tags` 和官方中文本地化生成，并接入 `assets/sync_full.py`。
运行时只读取这份精简清单与已有 `assets/items/` 图标，不依赖全量资源目录。
求助支持游戏清单中的材料；自动援助支持 Mat1–4（讨伐）、Rune1–2（元素）和
CatalystRare（催化剂）。默认勾选讨伐与元素材料，催化剂默认不勾选。
发布求助由材料选择决定：“不指定”时不发布，选定材料后每日发布一次。
保存时将 `request` 与 `item` 同步，兼容旧配置中已关闭的求助，不影响援助范围。

任务复用登录/手动刷新取得的完整列表；启用时每 60 分钟轻量同步一次，空巡检不记日志。
完整团内查询在没有求助记录时可以省略 `GuildAidItemInfoList`，按客户端 `GuildData`
构造函数的空列表默认值处理；显式非数组值或缺少有效本人状态仍视为未确认。
求助已发布且援助奖励满额后停至下一游戏日；关闭或没有佣兵团时不巡检。
变更参数使用缓存判断，过期时才查询，不重复完整进入流程。

成功响应仅含更新后的 `GuildAidItemInfo`，不含完整本人状态。按照客户端成功回调
`0x2190bac`、`0x2190d18`，分别记录求助时间和累计援助奖励；库存使用 `CostItems.NowItem`
绝对值更新。请求失败/响应未确认后等待 5 分钟先查询，禁止直接重发。
未确认的援助记录在本次会话中跳过；若没有收到绝对余额，暂时扣留对应可用数量，
防止以过高的旧库存继续援助，下一次全量登录后恢复服务器余额。

## 周任务自动领奖与进度

入口：佣兵团「自动任务 → 周任务」，默认关闭，按账号保存。开关只控制自动领奖；
「任务」按钮独立展开缓存中的本周个人任务与六档全团积分奖励，不触发查询。

`assets/guild_quests.json` 由 `tools/build_guild_quests.py` 根据 `sd_Quest.txt`、
`sd_Drop.txt`、`sd_Item.txt` 与中文本地化生成，已接入 `assets/sync_full.py`。
运行时只依赖这份精简目录和本地 `assets/items/` 图标，无需更新服务器或全量资源目录。

数据来源：

- 个人任务集合：`GuildData.Record.GuildQuestIDList`，只展示服务端本周选定的任务。
- 个人进度和领取标记：登录 `QuestContainer.Quests` 与业务响应的增量 `Quests`，
  按 `StaticID` 合并。`Count >= ConditionCount`、`IsFinish == true` 且 `IsRewarded == false`
  才可领取；未知目录或缺失进度只展示，禁止猜测领取。
- 全团积分：本团、本周且属于已知积分档位的 `GuildData.GuildQuestList[].QuestData.Count`
  最大值。各档位记录的是同一份积分，不求和；缺少有效记录时显示未知。
  `Record.WeeklyAssistanceScore` 是贡献相关字段，不作为周任务积分。
- 积分档位达成状态：`GuildData.GuildQuestList[].QuestData`；必须匹配本人 GID 与
  `Record.LastResetQuestWeek`。档位为 500 / 1000 / 1500 / 2000 / 2400 / 2800。
- 本人积分奖励领取记录：`PlayerGuildInfo.PlayerGuildQuestSave` 的
  `LastRewardQuestWeek` 与 `RewardedGuildQuestList`。公共档位的 `QuestData.IsRewarded`
  领奖后仍可能为 false，不能用作个人已领标记。

先逐项 `QuestHandler.RewardQuest`（`RewardQuestInfos: [{ID, Index:0}]`）领取个人奖励，
并从增量响应确认该项 `IsRewarded:true`；再通过一次 `QuestHandler.RewardGuildQuest`
（仅身份字段）领取所有可领积分档位，校验本人已领列表。相邻请求使用设置的请求延时，
关闭开关、退出应用、安装更新或断线会停止后续请求。

领奖响应中的 `GuildQuestDataList`（顶层或 `Results` 内）合并到公共档位列表，
按本团、本周过滤。个人领奖中缺少 GID 的默认 `PlayerGuildInfo` 不覆盖当前成员状态
（抓包 cdp-123/125/126）。`GuildDropResult` 不计入个人库存，也不用于修改
`WeeklyAssistanceScore` 或推算本周积分；以公共档位的实际进度为准。

启用后，登录同步、手动刷新、任务进度更新和开关开启都会检查缓存；此外每小时
`QueryGuildDataForSync` 一次，与自动援助共用查询和失败退避，不重复同步。
游戏日期变化时先同步，再检查奖励；空巡检不记日志，只有确实领取或异常才记录。
没有佣兵团时不开启查询或领奖。未启用周任务与援助时不增加周期请求。

超时/结果不明后等待 5 分钟先查询，再重新判断，禁止盲目重发。积分奖励可由查询中的
本人记录确认。**佣兵团查询不返回个人任务进度**：个人任务未确认时，该项暂时跳过，
等待后续明确的个人任务更新或下次完整登录快照，不借助团内查询猜测个人已领状态。
跨周以服务端周编号为准，个人快照保留登录时的 `NowGuildQuestWeek`；团内同步发现
新周时不把旧的个人进度归入新周。下一次登录（含每日快照刷新）恢复新周个人进度。

## 团员商店

自动任务只包含 `sd_Commodity.Store=MemberShop` 的周、月限购商品；主动任务
中的同名商店提供其余限次和不限购商品，需指定数量手动购买。开关默认关闭，
商品默认不勾选，按账号保存。当前清单共 7 种周期商品、5 种手动商品。

`tools/build_guild_store.py` 从 Commodity / Item / Activity 与中文本地化生成
`assets/guild_store.json`，随 `assets/sync_full.py` 更新。羁绊卡图标从
`assets_full/羁绊/图标` 提取，其余复用 `assets/items`。运行时无需全量数据。
活动开放判断使用该清单的静态规则及登录快照的 ActivityDBSettingContainer，
保留 `IsIgnoreTime`、不限时、周期活动语义。

货架来自登录 `StoreRecordContainer.Records`，缺少单个商品记录时按客户端
构造初始记录；整个容器缺失则禁止购买。周限购在 UTC 周一零点（北京时间
周一 08:00）、月限购在 UTC 月初零点重置；准备发送时在记录副本上归零，
不提前改写已确认缓存。打开商店、检查条件不额外查询服务器。

购买使用 `StoreHandler.BuyCommodity`，复用 `_buy_payload` 发送 Record、Count、
ItemIndex 与会话信息。与旧的团长商店 `GuildItemHandler.BuyGuildCommodity`
分开。每种自动商品购买 `min(剩余额度, 余额 // 单价)`，按清单顺序串行执行，
遵守请求延时，每次发送前重新读取开关、勾选、余额和连接状态。

登录/重新启用/修改勾选/周月重置时检查缓存。货币增加仅唤醒本周期尚未购齐、
使用该货币的已勾选商品；购齐后清除货币监听，等待相应周期刷新。没有独立
商店轮询，没有佣兵团或开关关闭时不执行。正常空检查不写日志。

成功以响应的 CommodityRecord 商品、商店和购买次数为依据，库存仅应用
CostItems / Drop 的 NowItem，不猜测随机箱产物。请求超时或响应不能确认时，
暂停该商品，等待新的完整登录快照；普通佣兵团刷新不包含个人购买记录，
不能解除该暂停。如果返回的货币余额也无法确认，暂停使用该货币购买其它商品；
使用其它货币的已勾选商品仍可按各自有效记录购买。
