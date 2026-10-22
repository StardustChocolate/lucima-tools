# 未加入佣兵团账号的抓包梳理（2026-10-16）

## 范围

- 来源：`capture/records/capture-20261016-093906.jsonl`。
- 抓包时间：北京时间 09:39:07—09:40:58。
- 客户端版本：`4.0.1.113419`，平台：`WebGLPlayer`。
- 按 `request_id` 配对请求、响应及正文，共 26 组游戏路由请求，全部 HTTP 200，全部具有响应正文；另有 9 组平台接口请求，采集摘要 `capture_error=0`。
- 只分析已有抓包，没有向游戏服务器重放请求。下文省略身份和会话参数；页面操作名称根据协议顺序推断。
- 同日上午另两份抓包中，09:31 那份主要停留在登录；09:33 那份出现了已入团账号的团内详情和签到。本报告聚焦最新的未入团账号，避免混合不同账号的行为。

## 1. 登录：已经能够识别未入团

| 时间 | 抓包 ID | 请求 | 业务参数 | 响应 |
| --- | --- | --- | --- | --- |
| 09:39:14.161 | cdp-10 | `GameServerDBSettingHandler.QueryBulletinInfoResult` | `{}` | 返回公告、服务器时间等信息。 |
| 09:39:27.077 | cdp-11 | `ErolabsHandler.QueryUserID` | 平台登录信息 | 返回登录身份信息。 |
| 09:39:27.659 | cdp-12 | `AccountHandler.Login` | 平台登录参数 | 返回登录快照。`GuildData` 和 `Info.GuildSubInfo` 字段均缺失；`PlayerGuildInfo` 仍存在，但 `GID` 为空字符串。 |

登录快照里与归属判断有关的结构：

```text
GuildData                     字段缺失
Info.GuildSubInfo              字段缺失
PlayerGuildInfo.GID            ""
PlayerGuildInfo.Authority      3
PlayerGuildInfo.NowGuildQuestWeek  661
PlayerGuildInfo.PlayerGuildQuestSave.RewardedGuildQuestList
                              仍保留旧的六档周任务领取记录
```

因此，存在 `PlayerGuildInfo`、权限数字或旧奖励列表，都不代表账号当前属于某个团。该账号已有历史佣兵团记录，本次没有抓到从一个从未入团的新账号首次进入的行为。

## 2. 进入大厅后的后台查询与助战奖励请求

| 时间 | 抓包 ID | 请求 | 响应 |
| --- | --- | --- | --- |
| 09:39:51.625 | cdp-13 | `ServerStatusHandler.Query` | 状态同步；`Drop` 无道具，`HasMail=false`。 |
| 09:39:51.629 | cdp-15 | `MailHandler.QueryNewestMails`，带最近邮件 `MailID` | 返回 89 条 `Mails`。这是查询，没有领取邮件的请求。 |
| 09:39:52.428 | cdp-16 | `SupportFriendHandler.QuerySupportRewardTime` | 返回 `NextRewardTime`。 |
| 09:39:53.434 | cdp-17 | `SupportFriendHandler.QuerySupFriendContainer` | 返回 7 条 `SupportInfos`。 |
| 09:39:54.025 | cdp-18 | `SupportFriendHandler.GetReward` | 返回 `SupportInfo` 等，本次 `Drop` 没有道具，因此只能确认请求返回，不能说领到了道具。 |

这些属于大厅和好友助战流程，与佣兵团加入流程分开。

与已入团账号不同，本次没有 `GuildChatHandler.QueryGuildChatContainer` 或聊天增量轮询。

## 3. 打开佣兵团入口：走搜索与邀请流程

### 3.1 刷新本人资料

**09:40:44.442，cdp-30：`AccountHandler.QueryAccountInfo`。**

业务参数：`{}`。

响应返回本人基础资料，等级 70，仍然不包含 `GuildSubInfo`。这是进入佣兵团入口前刷新归属状态的直接证据；具体页面跳转由客户端进行。

### 3.2 查询发给自己的邀请

**09:40:45.398，cdp-31：`GuildInvitationHandler.QueryInvitation`。**

```json
{
  "IsQueryByGuild": 0
}
```

响应：

```text
InviterList: 10 条
  9 条包含 InvitationID 和 GuildData
  1 条只有 InvitationID，缺少 GuildData
Utc
```

- 这是向本人发出的邀请列表，与已入团账号请求的 `IsQueryByGuild=1` 不同。
- 有资料的邀请项包含目标团的 `Info`、`Record`、`MemberList` 等，足以展示团名、等级、人数、最低等级、加入类型及成员。
- 其中也有当前成员数为 30 的团；收到邀请不代表一定能够加入，仍需以实际加入时的服务器结果为准。
- 缺少 `GuildData` 的那条应容错处理；不能从本次抓包断言是解散、失效还是其他原因。
- 这些 `GuildData` 都是邀请目标团的资料，不能写成本人的当前佣兵团。

### 3.3 同时请求可浏览的佣兵团列表

**09:40:45.400，cdp-32：`GuildHandler.SearchGuild`。**

```json
{
  "AdmitType": "OpenAutomatic",
  "SearchWithType": 0
}
```

响应：

```text
GuildDatas: 10 条
Utc
```

- 10 个结果中，7 个 `Info.AdmitType=OpenAutomatic`，3 个为 `OpenSelective`。
- 因此本次 `SearchWithType=0` 的请求没有将结果限制为 `OpenAutomatic`。不能只根据请求里的 `AdmitType`，把所有结果都展示为直接加入。
- 每个结果包含 `Info`、`Record`、`MemberList`、`ItemContainer`、`GuildStoreRecord`；资源和商店对象为空，没有完整的援助列表或全团周任务列表。
- 列表本身已经带有成员数据。本次没有随后点开某个团的请求，不能据此确定所有团详情页是否都不需要补充请求。
- 这 10 个搜索结果与有资料的邀请目标没有重合，邀请与搜索是两个独立集合。

邀请与搜索请求只相隔约 2 毫秒，属于并行获取。邀请响应于 09:40:46.298 返回，搜索响应于 09:40:46.751 返回；不需要等邀请结束才开始搜索。

## 4. 本次实际做到哪一步

操作链可以概括为：

```text
登录游戏
  → 大厅查询和好友助战奖励请求
  → 打开佣兵团入口
  → 刷新本人基础资料
  → 并行获取邀请列表、搜索佣兵团列表
  → 抓包结束
```

没有捕获到：

- 申请加入、直接加入或接受邀请。
- 拒绝邀请、取消申请、创建佣兵团。
- 点开搜索结果中的某个佣兵团。
- 团内完整查询、团战查询、签到、捐赠、援助、购买及任务领奖。

因此这份抓包补齐了未入团入口的识别与列表协议，还没有提供实际入团、申请或接受邀请的请求体及响应。

## 5. 穿插的后台轮询

`RTAHandler.QueryRTAInvitation` 共 15 次，09:39:51.626—09:40:55.841，约每 4.6 秒一次，响应均只有 `Utc`。这是实时竞技场邀请轮询，不属于佣兵团操作。

## 6. 对 LucimaTools 的实现建议

1. **区分三种情况：已入团、确认未入团、查询失败或信息不完整。** 本次未入团状态由最新本人资料缺少 `GuildSubInfo`，并结合登录快照 `PlayerGuildInfo.GID=""` 得到确认；不要仅根据 `PlayerGuildInfo` 是否存在判断归属，也不要把任意一次响应字段缺失都当成未入团。
2. **未入团应显示正常空状态。** 无需继续请求本团团战、签到、捐赠等；当前通用查询仍会调用多种团内接口，后续重构时需要按归属分流。若只实现已有成员的管理功能，先显示“尚未加入佣兵团”即可。
3. **邀请和搜索结果只作为候选资料。** 不能将其 `GuildData` 混入本人的团内状态，也不能跨账号沿用之前缓存的团内数据。
4. **邀请列表需要容错，加入类型按每个结果判断。** 本次已证实缺少 `GuildData` 的邀请项，以及搜索结果混合加入类型的情况。
5. **保留入团冷却字段。** 本人 `PlayerGuildInfo.NextCanJoinGuildTime` 可用于后续解释冷却，但实际加入资格、满员和审核结果由服务端裁定。

本轮仅补充协议分析文档，没有修改佣兵团业务逻辑。
