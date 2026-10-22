# 佣兵团操作抓包梳理（2026-10-15）

## 范围与读取方式

- 来源：`capture/records/capture-20261015-215651.jsonl`，采集时间为北京时间 2026-10-15 21:56:52—22:04:48。
- 将 `request`、`request_body`、`response`、`response_body` 按 `request_id` 配对，共解析到 147 组游戏路由请求和响应，均为 HTTP 200，均捕获到响应正文。另有 9 组平台接口请求。
- 以下时间使用抓包机记录的北京时间，按请求发送顺序排列；响应中的 `Utc` 是另一套服务端时间，不混用来排序。
- 请求共用 `PUT /Router/RouterHandler.ashx`，封装为 `{"route":"处理器.方法","data":{...}}`。下文只写业务参数，省略身份及会话字段；`{}` 表示没有额外业务参数。
- 请求、参数和奖励是直接抓包证据；“打开某页面”“点击某入口”等操作名称由协议顺序推断。抓包没有录屏，不能逐项证明具体点击位置，也不能仅凭请求时间认定签到是手动触发还是入页自动触发。
- 数量和道具名称根据响应中的 `Item.StaticID`、`Item.Count` 及本地 `backend/item_names.json` 核对。购买次数与实际获得的道具数量分别列出。

## 1. 登录游戏与后台初始化

| 时间 | 抓包 ID | 请求 | 业务参数 | 响应与含义 |
| --- | --- | --- | --- | --- |
| 21:56:59.896 | cdp-10 | `GameServerDBSettingHandler.QueryBulletinInfoResult` | `{}` | 返回 `Info`、`ServerTime`、`IsCheckTime`、`Utc`，查询公告与服务器时间信息。 |
| 21:57:14.981 | cdp-11 | `ErolabsHandler.QueryUserID` | 平台 `WebGLPlayer`，版本 `4.0.1.113419`，登录类型 `Erolabs` | 返回平台登录身份信息。 |
| 21:57:15.595 | cdp-12 | `AccountHandler.Login` | 平台登录参数 | 返回完整登录快照，包括 `GuildData`、`PlayerGuildInfo`、`QuestContainer`、`StoreRecordContainer` 等。 |
| 21:57:39.120 | cdp-14 | `MailHandler.QueryNewestMails` | 最近邮件的 `MailID` | 返回 `Mails`、`Utc`。这是查询，未捕获领取邮件的请求。 |
| 21:57:39.122 | cdp-15 | `GuildChatHandler.QueryGuildChatContainer` | `{}` | 返回佣兵团聊天 `Chats`，这是后台聊天初始化。 |
| 21:58:30.084 | cdp-23 | `ServerStatusHandler.Query` | `{}` | 返回账号状态效果等；本次 `Drop` 没有道具，`HasMail=false`。 |
| 21:58:30.979 | cdp-24 | `SupportFriendHandler.QuerySupportRewardTime` | `{}` | 返回 `NextRewardTime`，查询好友助战奖励时间。 |

这些请求出现在佣兵团详情查询前，不应全部算成“点击佣兵团后新增的请求”。

## 2. 进入佣兵团，随后发生签到

| 时间 | 抓包 ID | 请求 | 业务参数 | 响应与含义 |
| --- | --- | --- | --- | --- |
| 21:58:51.095 | cdp-31 | `AccountHandler.QueryAccountInfo` | `{}` | 返回玩家基础资料及 `GuildSubInfo`，确认当前所属佣兵团。 |
| 21:58:51.662 | cdp-32 | `GuildHandler.QueryFullGuildData` | `{}` | 返回 `GuildData`、`SelfGuildInfo`、`AidSupporterList`、`IsAlreadySignUp`。本团等级 18、成员 30 人；包含 27 条援助记录、6 条全团周任务、5 条会长商店购买记录；本团今日签到数为 22。 |
| 21:58:52.572 | cdp-33 | `GuildWarHandler.QueryFullGuildWarData` | `{}` | 返回本方团战数据及赛季信息；第 11 赛季，当前战次 `666~5`，上场 `666~3`，`IsWarNow=false`；本次无奖励道具。 |
| 21:58:54.351 | cdp-35 | `ServerStatusHandler.QueryGuildStatus` | `{}` | 返回佣兵团状态效果；有 `GuildMoneyBonus`，本次无奖励道具。 |
| 21:58:54.353 | cdp-36 | `GuildInvitationHandler.QueryInvitation` | `IsQueryByGuild=1` | 响应仅有 `Utc`，未返回邀请列表。 |
| 21:58:54.512 | cdp-37 | `GuildHandler.GuildMemberCheckIn` | `{}` | 签到成功：勇气纹章（`30`）+10；返回 `PlayerGuildData`，本人 `LastCheckInTime` 更新，`CanLastDayCheckReward=true`；任务 `GuildCheckIn` 的计数更新为 4。 |

签到返回的本人状态同时将本日捐赠计数、援助奖励计数重置为 0。入页时成员列表中保存的是此前的记录，不能直接把旧计数当成本日已执行次数。

## 3. 领取日奖励

**21:59:02.028，cdp-40：`GuildHandler.GuildMemberDayCheckReward`，业务参数 `{}`。**

响应：

- 勇气纹章（`30`）+30。
- 金币（`1`）+15,000。
- `PlayerGuildData.PlayerGuildInfo.CanLastDayCheckReward` 从签到后的 `true` 变为 `false`。
- 奖励通过 `Drop.Items` 直接到账，本次 `HasMail=false`。

因此签到与日奖励是两个请求，应分别处理成功状态。

## 4. 进入团战相关页面，查看战况与排行

| 时间 | 抓包 ID | 推断操作 | 请求与参数 | 响应 |
| --- | --- | --- | --- | --- |
| 21:59:22.713 | cdp-45 | 进入团战，结算上场奖励 | `GuildWarHandler.QueryGuildWarDataForSync`，`{}` | 返回团战同步数据、本人防守队伍及 `WarEndReward`。上场 `666~3`，`IsWin=false`，奖励倍率 5；神秘契约（`6`）+62，出现在 `WarEndReward.Drop` 和顶层 `Drop`，两处是同一份奖励，不能重复累加。 |
| 21:59:52.344 | cdp-54 | 查看团战战斗记录 | `GuildWarHandler.QueryGuildWarBattleLogByGIDandWarID`，`{}` | 仅 `Utc`，未返回战斗记录列表。 |
| 22:00:10.013 | cdp-58 | 查看当前团战排行 | `GuildWarHandler.QueryNowGuildWarRank`，`{}` | `GuildWarCampaignInfoList` 20 条，另有 `SelfCampaignInfo`；本团该记录 `NowCampaignRank=113`、`NowRankScore=11227`。 |
| 22:00:21.248 | cdp-62 | 点开排行中的另一佣兵团 | `GuildHandler.QueryPartialGuildDataForGuildWar`，`GuildID=<所选佣兵团ID>` | 返回目标团的部分资料、30 人成员列表及 `GuildWarCampaignInfo`；没有完整援助、商店内容。 |
| 22:00:40.830 | cdp-68 | 查看历史团战战绩 | `GuildWarHandler.QueryGuildWarCampaignLog`，`{}` | 返回 14 条 `GuildWarCampaignLogList`，包括双方佣兵团、得分与 MVP。 |
| 22:01:02.180 | cdp-74 | 查看历届赛季前三 | `GuildWarHandler.QuerySeasonTop3List`，`{}` | 返回 10 条赛季记录，每条有赛季起止日期与 `GuildSubInfoList`。 |

本段没有战斗开始、攻击、结算战斗等请求，实际进行了查询和上场奖励结算，没有新打一场团战。

## 5. 援助三位团员，再发布自己的求助

三次援助均发送 `GuildHandler.SupportGuildAid`，业务参数为 `{"GuildAidItemInfoID":"<该条援助记录ID>"}`。这是援助记录 ID，不是道具 ID 或玩家 ID。

| 时间 | 抓包 ID | 消耗 | 直接获得 | 响应中的援助与任务变化 |
| --- | --- | --- | --- | --- |
| 22:01:19.045 | cdp-79 | 灿烂晶石原石（`CR54`）×2 | 勇气纹章 ×30 | 返回更新后的 `GuildAidItemInfo`，`NowCount=8`；`HelpMember30.Count=3035`。 |
| 22:01:20.728 | cdp-80 | 灿烂晶石原石（`CR54`）×2 | 勇气纹章 ×30 | 另一条援助记录 `NowCount=8`；`HelpMember30.Count=3036`。 |
| 22:01:22.028 | cdp-81 | 中级木元素（`R008`）×1 | 勇气纹章 ×20 | 第三条援助记录 `NowCount=2`；`HelpMember30.Count=3037`。 |

本人的三次援助合计消耗原石 ×4、中级木元素 ×1，获得勇气纹章 ×80。第三条的最终进度 2 不代表本人消耗了 2 个元素，实际消耗必须以 `CostItems` 为准。

**22:01:25.096，cdp-83：发布自己的求助。**

```json
{
  "route": "GuildHandler.RequestGuildAid",
  "data": {"ItemID": "CR54"}
}
```

返回本人的 `GuildAidItemInfo`，道具为 `CR54`，`NowCount=0`。这是发布求助，不是立即获得道具；响应没有 `Drop`。

## 6. 捐赠金币与英勇证明

### 金币捐赠

**22:01:47.912，cdp-90：`GuildHandler.DonateGold`。**

业务参数：`{"ItemID":"1","Count":10}`。

- `CostItems`：金币 -50,000。
- 本人 `Drop`：勇气纹章 +50。
- `GuildDropResult`：佣兵团荣誉金币（`80`）+50,000。
- `PlayerGuildInfo.DayDonateGoldCount=10`。
- **`Count=10` 是捐赠份数，这次实际扣 50,000 金币。**

随后 **22:01:48.647，cdp-91** 调用 `GuildHandler.QueryGuildDataForSync`，参数 `{}`，重新同步团内资料、资源及本人状态。

### 英勇证明捐赠

**22:01:51.929，cdp-93：`GuildHandler.DonateCourage`。**

业务参数：`{"ItemID":"28","Count":3}`。

- `CostItems`：本人英勇证明（`28`）-3。
- 本人 `Drop`：同盟徽章（`29`）+15。
- `GuildDropResult`：佣兵团英勇证明（`81`）+3。
- `PlayerGuildInfo.DayDonateCourageCount=3`。
- 返回 `GuildDonatedCourage` 任务进度 12，`IsFinish=true`、`IsRewarded=false`。

随后 **22:01:52.664，cdp-94** 再次调用 `GuildHandler.QueryGuildDataForSync` 同步。

注意：本人道具 `28` 和团内资源 `81` 名称相同，但它们的所属容器、ID、用途不同。

## 7. 在成员商店购买八次

这一组全部是 **`StoreHandler.BuyCommodity`**，请求中的 `Record.Store` 都是 **`MemberShop`**。

共同参数结构如下；`Record` 来自本人商品购买记录，日期按客户端字符串格式发送，不是只传商品 ID：

```json
{
  "Record": {
    "_id": "<该商品的购买记录ID>",
    "StaticID": "GuildCertificateEterna",
    "Store": "MemberShop",
    "BuyCount": 0,
    "FreeBuyCount": 0,
    "GuaranteedCount": 0,
    "SummonRoleID": "",
    "SummonRoleIDs": [],
    "LastBuyTime": "2026/9/30 17:56:20",
    "LastResetTime": "2026/10/1 0:20:21"
  },
  "Count": 2,
  "ItemIndex": -1,
  "LoginType": "ID_Password",
  "NewErolabs": 0,
  "Platform": "WebGLPlayer",
  "SelectRoleID": ""
}
```

| 时间 | 抓包 ID | 商品 `StaticID` | 请求 `Count` | 实际消耗 | 实际获得 |
| --- | --- | --- | --- | --- | --- |
| 22:02:10.119 | cdp-99 | `GuildCertificateEterna` | 2 | 同盟徽章 ×600 | 永恒之证 ×2。 |
| 22:02:26.598 | cdp-103 | `GuildTeraStarForce` | 1 | 勇气纹章 ×900 | 传说升星水晶 ×1。 |
| 22:02:28.879 | cdp-105 | `GuildTesseract` | 1 | 勇气纹章 ×250 | 技能模块 ×1。 |
| 22:02:30.496 | cdp-106 | `GuildContractMystic` | 1 | 同盟徽章 ×200 | 神秘契约 ×50。 |
| 22:02:33.812 | cdp-108 | `GuildB015` | 5 | 同盟徽章 ×150 | 直接获得五种催化剂各 1：狂热剑刃、神臂弓弦、复合式枪刃、感知抑制器、犄角头盔。 |
| 22:02:45.496 | cdp-112 | `GuildB047` | 3 | 勇气纹章 ×540 | 直接获得感知抑制器、传说佣兵回忆录、犄角头盔各 1。 |
| 22:02:54.363 | cdp-115 | `GuildB021` | 2 | 同盟徽章 ×300 | 直接获得超时空之钥、星源萃取精华各 1。 |
| 22:03:03.246 | cdp-118 | `GuildAC01` | 10 | 勇气纹章 ×1,800 | 下级羁绊强化芯片 ×10；该商品 `BuyCount` 从 559 增至 569。 |

每次响应都返回 `Drop`、`CostItems`、`CommodityRecord` 等；应使用响应更新库存、货币和购买次数。三个箱子商品直接返回随机材料，本段没有额外的开箱请求。

八次购买共消耗勇气纹章 ×3,490、同盟徽章 ×1,250。

这里没有调用 `GuildItemHandler.BuyGuildCommodity`，也没有购买当前软件会长商店中的 `GuildBox`、`GuildHeroExp` 等。因此不能用这段抓包直接断言现有会长商店接口或 `Count=0` 正误。

## 8. 领取三个个人佣兵团任务

三次调用 **`QuestHandler.RewardQuest`**，结构为：

```json
{
  "RewardQuestInfos": [{"ID": "GuildDonatedCourage", "Index": 0}],
  "CommodityID": ""
}
```

| 时间 | 抓包 ID | 任务 ID | 本人奖励 | 团内资源变化 |
| --- | --- | --- | --- | --- |
| 22:03:21.819 | cdp-123 | `GuildDonatedCourage` | 勇气纹章 ×20、同盟徽章 ×15 | 英勇证明（`81`）+1、公会积分（`82`）+20。 |
| 22:03:23.363 | cdp-125 | `GuildGetAmulet` | 勇气纹章 ×20、下级万能饰品强化芯片（`UAC01`）×1 | 英勇证明 +1、公会积分 +25。 |
| 22:03:24.713 | cdp-126 | `GuildGetRing` | 勇气纹章 ×20、下级万能饰品强化芯片 ×1 | 英勇证明 +1、公会积分 +25。 |

响应的奖励位于 `Results[].Drop`，并在 `Results[].Quests` 中将对应任务标记为 `IsRewarded=true`。同一次响应还有更新后的全团周任务数据。

本次只看到三个不同任务各领取一次，没有看到相同任务循环领取多轮的客户端行为。团里的 `GuildQuestIDList` 一共有六个 ID，但列出 ID 不等于六个任务都已达标、都可领取。

## 9. 一次领取六档全团周任务奖励

**22:03:26.613，cdp-127：`QuestHandler.RewardGuildQuest`，业务参数 `{}`。**

与上面的 `RewardQuest` 是两个不同接口。此次响应的 `Results` 有六项，合计：

- 下级万能装备强化芯片（`UEC01`）×3。
- 勇气纹章 ×200。
- 招募契约（`5`）×5。
- 同盟徽章 ×80。
- 永恒之证 ×1。
- 神秘契约 ×50。

本人 `PlayerGuildInfo.PlayerGuildQuestSave.RewardedGuildQuestList` 新增：

```text
WeeklyGuildScore500
WeeklyGuildScore1000
WeeklyGuildScore1500
WeeklyGuildScore2000
WeeklyGuildScore2400
WeeklyGuildScore2800
```

这六档公共 `GuildQuestDataList[].QuestData.IsRewarded` 在领取后仍为 `false`。公共任务达成情况与“本人是否领过”分开保存；不能只读公共任务的 `IsRewarded` 判断本人是否可领。

## 10. 再次进入佣兵团，查看日志和成员资料

| 时间 | 抓包 ID | 请求与参数 | 响应 |
| --- | --- | --- | --- |
| 22:03:38.623 | cdp-131 | `ServerStatusHandler.Query`，`{}` | 账号状态同步，无奖励道具。 |
| 22:03:40.096 | cdp-133 | `AccountHandler.QueryAccountInfo`，`{}` | 再次查询本人基础信息与所属佣兵团。 |
| 22:03:40.680 | cdp-134 | `GuildHandler.QueryFullGuildData`，`{}` | 全团详情；今日签到人数变为 23，成员列表里的本人状态包含签到、捐赠及六档周奖励已领记录。 |
| 22:03:41.603 | cdp-136 | `GuildWarHandler.QueryFullGuildWarData`，`{}` | 再次查询完整团战数据，本次没有奖励。 |
| 22:03:43.568 | cdp-137 | `ServerStatusHandler.QueryGuildStatus`，`{}` | 查询佣兵团状态效果，本次没有奖励。 |
| 22:03:43.570 | cdp-138 | `GuildInvitationHandler.QueryInvitation`，`IsQueryByGuild=1` | 仅 `Utc`，没有邀请列表；没有再次发送签到。 |
| 22:04:03.001 | cdp-143 | `GuildHandler.QueryGuildLogList`，`GuildID=<本人佣兵团ID>` | 返回 30 条 `GuildLogs`，含成员、日志日期、道具和操作类型。 |
| 22:04:28.230 | cdp-150 | `AccountHandler.QueryPlayerCardData`，`CUID=<被点开的团员编号>` | 该编号在本团成员列表中；返回 `BattleSupportData`（玩家及角色资料）、`PVPInfo`、好友数量等。 |
| 22:04:43.138 | cdp-155 | `ServerStatusHandler.Query`，`{}` | 再次同步账号状态，本次没有奖励。 |

第二次进佣兵团时没有 `GuildMemberCheckIn` 请求是本次直接观察到的事实；至于由客户端哪段判断决定跳过，应另行对照客户端实现。

## 穿插在操作之间的后台轮询

- `RTAHandler.QueryRTAInvitation` 共 91 次，21:57:39.118—22:04:45.295，约每 4.6 秒查询实时竞技场邀请；不是 91 次佣兵团操作。
- `GuildChatHandler.QueryUpdateGuildChatContainer` 共 7 次，在 21:58:39、21:59:39、22:00:39、22:01:39、22:02:39、22:03:39、22:04:39 附近，约每分钟一次。请求携带最后一条聊天记录 `LastChatData`，响应为 `Chats`、`Utc`；没有发送聊天消息的请求。
- 上表已经保留三次 `ServerStatusHandler.Query` 的位置。

## 对当前 LucimaTools 实现的直接启示

1. **签到状态需要正确回写。** `GuildMemberCheckIn`、`GuildMemberDayCheckReward` 返回的是 `PlayerGuildData.PlayerGuildInfo`；当前 `GuildService._call` 仅直接回写顶层 `PlayerGuildInfo`。此外，这次 `QueryFullGuildData.SelfGuildInfo` 出现默认式的旧周数/零计数，与成员列表中本人的 `PlayerGuildInfo` 不一致，不能无条件以它覆盖后者。
2. **个人任务与全团周奖励需要分开。** 已有循环领取功能只调用 `RewardQuest`，未覆盖本次证实的 `RewardGuildQuest` 六档周奖励；固定多轮调用也不是这段客户端操作所显示的领取流程。后续应根据完成和本人领取记录决定调用。
3. **成员商店需要独立设计。** 这次证实的是 `StoreHandler.BuyCommodity + MemberShop + 实际购买份数`；现有功能只支持会长商店的另一套接口。
4. **援助与捐赠已取得直接参数和收益证据。** 可以分别设计援助、求助、金币捐赠、英勇证明捐赠；库存变化以 `CostItems` 与 `Drop` 为准。
5. **查询响应也可能带结算。** 团战同步查询此次直接发放神秘契约 ×62；嵌套 `WarEndReward.Drop` 与顶层 `Drop` 要避免重复记账。
6. **公共进度不等于本人领取状态。** 全团周任务领取后，仍需读本人 `RewardedGuildQuestList`，不能以公共 `IsRewarded` 推断。

本轮仅恢复导航入口并记录协议证据；入口已通过离线前端点击检查，没有向游戏服务器重放这些操作。
