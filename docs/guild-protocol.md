# 工会接口对照与验证边界

本文件记录本次对本地参考包的静态核对结果。证据来自 `app-release/lib/arm64-v8a/libapp.so` 的 ARM64 AOT 反汇编，以及 `app-release-repacked/analysis/full.asm`、`analysis/pp.txt`。下表地址均为参考包的虚拟地址，不能当作其他版本的通用偏移。

本次文档不包含任何账号、密码、令牌或会话值。静态核对不等于已使用真实账号完成交易验证。

## 连接与登录

- 参考包游戏入口：`https://game-arkre-labs.ecchi.xxx/Router/RouterHandler.ashx`（对象池 `pp+0x123f8`）。
- 新模块使用源码已有的 `GameClient.call` 及 `_auth_data`，登录流程、会话获取、HTTP 请求封装和服务器地址配置由原源码统一处理。
- 每个工会请求的 `data` 内都带现有会话的 `AID`、`SessionID`；下表省略这两个公共字段。参考包任务上下文称它们为 `rtaAid`、`sessionId`，但线上 JSON 键是 `AID`、`SessionID`。
- 不从浏览器复制固定会话到源码，不引入参考工具的激活服务。

## 已核实的请求

| 游戏路由 | 附加字段 | 参考证据 |
| --- | --- | --- |
| `GuildHandler.DonateGold` | `ItemID: "1", Count: 10` | `0x5758f8` 构建；`0x57597c` 读取对象池字符串 `"1"`；`0x575988` 写入整数 10；`0x575300` 序列化 |
| `GuildHandler.DonateCourage` | `ItemID: "28", Count: 1` | `0x575200` 构建；`0x575288`、`0x575290` 默认值；同一 `0x575300` 序列化 |
| `GuildHandler.SearchGuild` | `Name: 名称, AdmitType: "OpenAutomatic", SearchWithType: 0` | `0x6d19a8` 构建；`0x6d1a70`、`0x6d1a78` 默认值；`0x6d1b04` 序列化 |
| `GuildInvitationHandler.JoinGuild` | `RecipientID: 工会ID字符串, RecipientCUID: 0` | `0x6d137c` 构建；`0x6d143c`、`0x6d1440` 赋值；`0x6d14cc` 序列化 |
| `GuildHandler.GuildMemberCheckIn` | 无 | `0x577a94` 构建；公共序列化 `0x544f8c` |
| `GuildItemHandler.BuyGuildCommodity` | 见后文 | `0x572508`～`0x5726d0` |

参考包把上述金币请求描述为“一次 5 万金币”，因此 `Count` 的单位是捐献档位，十个档位对应 50,000 金币，不能直接发送 `Count: 50000`。此次修正保留固定 50,000 金币操作，对应 `Count: 10`。服务器仍可根据余额、成员状态、每日次数等条件拒绝。

普通金币捐献和参考包的 `guild-donate-gold-once` 都复用 `0x5758c8` 请求构造。后者响应处理函数 `0xc6d8a0` 会在检测到 `OverCount` 后返回空结果（`0xc6da6c`、`0xc6daf8`、`0xc6db10`），这不是服务器成功证据。新模块显示该错误并停止任务，避免把达到上限误报成成功。

## 工会查询结果

`QueryGuildDataForSync` 与 `QueryFullGuildData` 使用同一响应解析函数 `0x540518`（全信息查询调用点 `0x5785b8`）。结构如下；空值及字段缺失需分别处理：

```text
GuildData
  Info
    _id          工会 ID，可以是字符串或 {"$oid": "..."}
    Name         工会名称
    GuildLV, Exp, MemberCount, AdmitType
  Record
    LastCheckInCount, NowCheckInCount, LastCheckInDate
    TotalDonatedGuildGold, TotalDonatedGuildCourage
    GuildMemo, GuildWarMemo, GuildQuestIDList
  MemberList
  GuildAidItemInfoList
  ItemContainer
  GuildQuestList
  GuildStoreRecord
    Records      商品购买记录列表
SelfGuildInfo
  NextCanJoinGuildTime
  Authority
  DayDonateGoldCount, DayDonateCourageCount
  LastCheckInTime, CanLastDayCheckReward
IsAlreadySignUp
Utc
```

`GuildData.Info` 解析位于 `0x542208`；`_id` 读取位于 `0x542238`，`Name` 读取位于 `0x542344`。`GuildData.Record` 是工会记录，不应拿它的 `_id` 代替 `Info._id`。

搜索结果是 `GuildDatas` 列表，每一项含 `Info`。参考包读取第一条结果的 `Info._id`（`0x6d1674` → `0x6d1754` → `0x6d179c`），兼容字符串和 `$oid` 对象。新模块应按 `Info.Name` 精确匹配目标，并在不唯一或结构不完整时停止，避免误入同名近似工会。

金币捐献响应解析函数 `0x5754ac` 读取 `PlayerGuildInfo`、`Drop`、`CostItems`、`Quests`、`HasMail`、`Utc`。结果需要展示并刷新状态，不能仅凭 HTTP 200 认定已成功捐献。

## 签到流程

参考包同时提供直接签到与“出席全流程”。后者在 `0x55914c`～`0x55916c` 按以下顺序组装：

1. `GuildHandler.QueryFullGuildData`
2. `GuildWarHandler.QueryFullGuildWarData`
3. `ServerStatusHandler.QueryGuildStatus`
4. `GuildHandler.GuildMemberCheckIn`
5. `GuildHandler.GuildMemberDayCheckReward`

这些调用本身不能保证满足游戏的签到或领奖条件。`CantCheckIn` 是游戏业务响应，不能仅据此推断登录失败或网络地址错误。

## 会长商店

参考包购买请求包含：

```json
{
  "Record": {
    "_id": "",
    "StaticID": "GuildBox",
    "Store": "CaptainShop",
    "BuyCount": 0,
    "FreeBuyCount": 0,
    "GuaranteedCount": 0,
    "SummonRoleID": "",
    "SummonRoleIDs": [],
    "LastBuyTime": "0001/1/1 0:0:0",
    "LastResetTime": "0001/1/1 0:0:0"
  },
  "Count": 0,
  "ItemIndex": -1,
  "LoginType": "ID_Password",
  "NewErolabs": 0,
  "Platform": "Android",
  "SelectRoleID": ""
}
```

以上示例省略 `AID`、`SessionID`，展示首次购买默认记录。请求外层的零与负一是参考包直接设置值（`ItemIndex` 的 Dart Smi `-2` 代表 JSON 整数 `-1`），不是购买零个商品。

参考包先从 `GuildData.GuildStoreRecord.Records` 查找对应 `StaticID` 的记录（`0x572728`），复制其十个字段。不存在匹配记录时才使用上述默认值（`0x572d7c`～`0x572dc0`）。五个会长商品 ID 是 `GuildBox`、`GuildHeroExp`、`GuildMoneyBonus`、`GuildEnergy30`、`GuildEnergy50`。

记录序列化函数是 `0x569770`。它支持可选 `DropResult`、`GiftInfo`，但工会购买记录构造函数未给这两个字段赋值，所以该工会请求实际省略二者。不要把查询响应整个原样作为请求记录传回。

时间字段通过 `0xbe4e8c` 转换：已有非空普通字符串原样保留；空值、空字符串、`0001/` 前缀使用 `0001/1/1 0:0:0`；`$date` 对象或毫秒整数经 `0x572de0` 按 UTC 转为 `年/月/日 时:分:秒`，不补零，负值使用默认时间。

## 实测界限

单元测试只能验证字段、路由、任务停止、会话复用和结果处理。安装测试只能验证 APK 启动及前后端连接。真实金币扣除、工会资产增加和游戏奖励发放，必须以当前游戏服务器的真实响应及前后余额为准，不能用原工具的“成功”提示替代验证。

## 1.2.3：领取任务奖励（逐个）

用户要求移除“工会刷红币”模块。1.2.3 删除该页面区域及其 HTTP 提交、查询、停止接口。保留工会管理与会长管理，在工会管理内新增逐个领奖。

原包首先通过 `GuildHandler.QueryGuildDataForSync` 读取 `GuildData.Record.GuildQuestIDList`（`0x6ef458`、`0x6ef70c`～`0x6ef73c`、`0x542100`）。该快照在一次执行中保持不变。执行轮数默认为 1，每轮逐个发送以下请求，每个请求只含一个任务 ID：

```json
{
  "route": "QuestHandler.RewardQuest",
  "data": {
    "AID": "使用当前登录会话",
    "SessionID": "使用当前登录会话",
    "RewardQuestInfos": [{"ID": "工会任务 ID", "Index": 0}]
  }
}
```

请求构造证据：路由 `0x9f5c70`；单任务信息 `0x9f5e30`；外层序列化 `0x565970`；ID/Index 序列化 `0x565aec`。轮数遍历见 `0x9f5b08`～`0x9f5d68`，UI 说明见对象池 `pp+0x37728`、`pp+0x37730`。

该接口与积分领取 `QuestHandler.RewardGuildQuest` 不同。返回结构由 `0xccbfd4 → 0x564c84` 解析：外层 `Results[]`、`PlayerGuildInfo`、`Utc`；每项 `Drop`、`Quests`、`GuildQuestDataList`。新代码逐项维护嵌套 Drop 中的绝对 NowItem 余额，空 Results 不计成功。

一般业务拒绝（已领取、条件未满足等）记录后继续下一个任务；登录失效、断网或无法识别的响应停止任务，不自动重试。静态证据只能证明原包循环请求，不证明可以无限重复领奖，或固定 30 轮即可获得特定积分/奖励。

## 1.2.5：纠正逐个领奖响应的必填假设

用户在 1.2.4 中已成功读取 6 个工会任务 ID，但首项 `GuildCheckIn` 的响应被本地逻辑判为“缺少 Results”，随即停止。重新核对原包后，确认 1.2.3 引入的两项限制与参考包不符：强制响应包含非空 `Results`，以及强制每项至少具有非空 `Drop`、`Quests` 或 `GuildQuestDataList`。这些字段允许缺失或为空，不能仅凭缺失判断请求执行失败。

响应入口仍是 `0x9f5db8 → 0xccbfd4 → 0x564c84`。`0xccbfd4` 先通过 `0xbb95b0` 检查 HTTP/错误标志，再通过 `0xbb91f0` 解码 JSON 对象；没有在此处解开其他 `data`、`result` 等包装层。顶层 `Results` 在 `0x564ca4` 读取，接受 `List?`；缺失或 `null` 经 `0x564d14`、`0x564d8c` 分支，在 `0x564da0` 创建空列表。列表中的每项必须是 JSON 对象（`0x564e84 → 0x564eb0`），然后由 `0x564ed4` 解析。

| 每项字段 | 原包接受的 JSON 类型及缺省行为 | 地址证据 |
| --- | --- | --- |
| `Drop` | 对象或 `null`；缺失保持 `null`。对象交给掉落解析器。 | `0x564f04` 读取；`0x564f14` 判空；`0x564f50`、`0x564f5c` 要求 `Map<String, dynamic>`；`0x564f68 → 0x560130` 解析；`0x564f74` 缺省 `null` |
| `Quests` | 对象数组或 `null`，空数组有效；缺失保持 `null`。不是以任务 ID 为键的对象。 | `0x564f90` 读取；`0x564fcc`、`0x564fd8` 要求 `List?`；`0x564fe0`～`0x564fec` 处理 `null`；`0x565010` 声明映射元素类型 `Map<String, dynamic>`；映射回调 `0x5653f8`～`0x565428` 要求每项为对象并原样返回 |
| `GuildQuestDataList` | 对象数组或 `null`；缺失/`null` 转为空数组。 | `0x565070` 读取；`0x5650ac`、`0x5650b8` 要求 `List?`；`0x565138`～`0x56514c` 创建缺省空数组；元素回调 `0x5651dc` 要求对象后交给 `0x56522c` |
| `HasMail` | 布尔值；缺失/`null` 转为 `false`。 | `0x565160` 读取；`0x565170`～`0x565178` 设置缺省值；`0x5651a4`、`0x5651b0` 要求 `bool` |

因此，`{}`、`{"Results": null}`、`{"Results": []}`、`{"Results": [{}]}` 在该原包解析器中均不会因为“缺少奖励字段”而抛错。类型错误仍然不同，例如 `Results` 是对象、列表项不是对象、`Quests` 是对象而非数组，均不符合解析器类型要求。没有找到该领奖路径把顶层 `Drop` 转换为 `Results` 的证据，不据此添加猜测的协议兼容层。

`0x9f5b08`～`0x9f5d68` 构建的是轮数乘任务 ID 的请求计划。实际执行时，通用执行器在 `0xbbc044`～`0xbbc04c` 调用响应解析，随后等待步骤间隔（`0xbbc074`），再输出“完成…步骤”（`0xbbc0a4`、`0xbbc114`）；这个路径没有检查 `Results` 是否非空。原包的步骤完成表示请求及解析流程完成，并不证明奖励已到账。修正时应允许无奖励明细的正常响应继续下一项，单独显示“未返回奖励明细”，不将其计为已确认发奖。

原包对 `guild-quest-reward-individual-` 前缀的步骤设置默认间隔：`0xbbeae0` 识别前缀，`0xbbeb34`～`0xbbeb84` 读取 `defaultStepIntervalMillis`，未配置时为 500 毫秒；步骤自身指定的正间隔优先。异常继续逻辑在 `0xbbd4b4`～`0xbbd524` 先检查 `Duplicate Login`、`DuplicateLogin`、`重复登录`，这些错误中断流程；其余异常根据继续标志处理（`0xbbd52c`、`0xbbd560`、`0xbbd5d0`）。新模块继续区分业务拒绝、缺少奖励明细和网络结果未知，网络异常不自动重发。

错误信息的大小写也必须与原包区分。`0xbb95b0` 在 HTTP 2xx 时检查 `_isErrorResponse == true`（`0xbb96b8`～`0xbb96f8`）；非 2xx 分支还检查小写 `success == false`（`0xbb9a5c`～`0xbb9a98`）。失败消息提取器 `0xbbebd4` 按 `_formattedMessage`、`message`、`originalError` 的顺序取首个非空信息，再尝试 `Error`、`error`、`ErrorCode`、`errorCode`、`Reason`（`0xbbee64`～`0xbbee90`）。这段代码是错误信息提取规则，不是“任一字段存在就算失败”的判定依据。网络适配器还会把 HTTP 2xx 的非 JSON 明文错误包装成 `_isErrorResponse` 对象，并将内部状态置为 501（`0xbbb1bc`～`0xbbb2ec`）；源码直接以 `GameMessage` 表示明文业务拒绝，无需复制这个内部包装。

本次纠正依据为离线静态分析及用户截图。按用户要求没有执行游戏请求或运行验证测试；实际返回内容和奖励变化由用户安装后核对。
