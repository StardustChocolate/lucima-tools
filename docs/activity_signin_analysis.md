# 活动签到领取链核对

首次核对日期：2026-10-09；运行实现更新日期：2026-10-14。活动签到属于“每日签到”，登录后跳过已签满七天或当天已签的记录，其余按快照提交，由游戏服务器判断奖励条件。下文的完整客户端条件保留为历史分析依据，软件只复用日期判断，不再依赖逐活动资源规则。本轮仅执行日志分析、资源核对和离线模拟测试，没有发送实际签到请求。

## 客户端自动流程

```text
WeekSignInPanel.ShowCanSignInPanel
→ 读取登录快照 WeekSignInDataContainer.WeekSignInDataList
→ ProcessSignInList(forceShow=false)，逐项处理
→ GetActivityDBSetting
→ NeedCallSignIn
→ WeekSignInManager.SignIn
→ WeekSignInHandler.SignIn
→ 按 ActivityID 替换缓存中的记录
→ QueryNewestMails
```

自动流程遍历全部活动签到记录，包括新手、回归和普通活动。手动菜单的显示分类不会排除自动流程中的这些记录。`UpdateReturning` 虽然存在，但没有发现这条自动流程调用它，因此没有添加额外请求。

### 请求条件

先取得活动设置。`Newbie` 和 `Returning` 可使用客户端构造的默认设置；其它 ID 必须在登录快照的 `ActivityDBSettingContainer.SettingList` 中找到同 `ActivityID` 的设置，包括 `Returning2`。恢复代码中的默认回归 ID 是 `Returning`，不能直接以 `sd_PublicVariable` 中的 `Returning2` 代替。

在有设置的前提下，定义：

| 条件 | 含义 |
|---|---|
| `R` | `sd_SignInReward` 中有 `Type=Week`、同 `ActivityID`、`Day=SignInDay+1` 的奖励 |
| `Dlogin` | 原始 `LastLoginTime` 的日期与服务器校正后 `NowTime` 的日期一致 |
| `Dsign` | 原始 `LastSignInTime` 的日期与服务器校正后 `NowTime` 的日期一致 |
| `L` | 账号等级达到下一天奖励的 `Level` |
| `W` | 设置忽略时间、静态活动不限时，或者记录的 `CreateTime` 在当前有效活动区间内 |

```text
CanSignIn = W && !Dsign && R && L
NeedCallSignIn = CanSignIn || (!Dlogin && R)
```

第二个分支负责记录当天登录，不要求达到领奖等级，也不要求活动窗口匹配。返回成功但 `SignInDay` 没增加可以是正常结果。因此不能只判断“今天未领过”，也不能直接以“活动已过期”或“低等级”排除全部请求。没有下一天奖励时不请求，不硬编码最多 7 天。

### 时间与周期

`TimeHelper.NowTime` 使用 UTC 当前时间、游戏时区偏移和服务器时间校正，恢复代码的默认偏移为 8 小时。这个默认值不能直接当成服务端时间的额外偏移：`SyncServerTime(inTime)` 计算 `inTime - NowTime` 并写入 `ServerTimeOffsetSecond`，偏差至少 5 秒时生效；默认时钟首次同步到原始服务端 UTC 时间会抵消此前的 8 小时。`ProcessSyncServerBaseResult` 将响应的 `ServerTime.Now` 传入此方法。请求日期使用 UTC 的非补零格式，兼容 `.NET` 最小日期 `0001/1/1 0:0:0`。

`$date` 被反序列化成 UTC `DateTime`，签到判断直接比较该字段的 `ToShortDateString()` 与经过同步的 `NowTime.ToShortDateString()`。此前只依据默认时区将本软件的服务端 `Utc` 再加 8 小时，并据此推断客户端在北京时间凌晨可能重复请求，遗漏了上述校正链，该推断已撤回。运行实现直接比较签到记录与服务器 `Utc` 的 UTC 日期，即北京时间 08:00 换日：前一天日间已签到，次日凌晨重登仍跳过；08:00 后进入下一个签到日。

`W` 检查的是记录的 `CreateTime`，而不是简单判断现在是否早于 `EndTime`。没有周期时比较原始起止区间，两端都包含。若 `OffDays>0` 或 `IsRepeat=true`，客户端先计算当前周期：

```text
openHours = truncate((EndTime - StartTime).TotalHours)
cycleHours = openHours + OffDays * 24
cycle = truncate((syncedNowTimeMs - rawStartEpochMs) / cycleMilliseconds)
effectiveStart = StartTime + cycle * cycleHours
effectiveEnd = effectiveStart + openHours
```

设置的起止时间和记录的创建时间互相比较时保持原始字段。参与周期位置计算的 `NowTime` 使用校正后的时钟，不能只依据静态默认值补加 8 小时，也不能给起止时间和创建时间补加偏移。本软件已移除这些窗口检查，不依赖它们决定是否请求。

## 请求、响应与邮件

请求业务字段为整条活动签到记录：

```json
{
  "route": "WeekSignInHandler.SignIn",
  "data": {
    "AID": "<账户标识>",
    "SessionID": "<会话标识>",
    "_id": "<活动记录标识>",
    "SignInDay": 0,
    "ActivityID": "ActivitySignInH189",
    "CreateTime": "<UTC日期字符串>",
    "LastLoginTime": "<UTC日期字符串>",
    "LastSignInTime": "0001/1/1 0:0:0",
    "LastContinuousSignInTime": "0001/1/1 0:0:0"
  }
}
```

成功响应就是更新后的 `WeekSignInData`，带 `_id`、`ActivityID`、`SignInDay`、上述四个 `$date` 时间字段，以及字符串 Unix 秒值 `Utc`。它不是公共 `ServerBaseResult`，没有 `Drop` 和 `HasMail`。客户端先按 `ActivityID` 替换对应缓存记录，保留其它活动，再无条件查询最新邮件。这条流程不自动领取附件。

扫描 26 份已有 HAR，只找到一次实际活动签到领取：

| 证据 | 结果 |
|---|---|
| `capture-20261009-084854.har`，entry 993 | `ActivitySignInH189` 从第 0 天更新到第 1 天 |
| 同名 JSONL，第 2465 / 2491 行 | 请求与响应对应同一活动记录 |
| JSONL，第 2490 / 2493 行 | 成功响应后约 719 毫秒发起邮件查询 |
| 新邮件 `ActivitySignInReward` | `MessageArgs=["1"]`，100 万金币，附件未领取，有效期 7 天 |

该登录快照还含已完成第 7 天的新手与回归记录，客户端没有再发送它们的签到请求。最新月卡账号样本中的三条活动记录也都已完成第 7 天，未捕获活动签到请求。这里只引用业务结果，不保留真实鉴权值和记录 ID。

## 本软件接入

沿用“每日签到”的开关和任务 ID `month_signin`。开启后，每次成功登录、每日快照刷新重登或手动刷新重登，依次检查月历、月卡与活动签到。关闭开关时活动签到也不会在启动任务中强制执行。原 `week_signin` 启动任务已经移除。

每次登录遍历 `WeekSignInDataContainer.WeekSignInDataList`。先判断 `SignInDay >= 7`，满足则标记本次检查完成，不发请求；未签满七天时比较 `LastSignInTime` 与游戏当前日期，当天已签也直接完成。`LastLoginTime` 只作为请求字段，不会令已经签到的记录再次请求。其余活动调用一次 `WeekSignInHandler.SignIn`，不检查活动是否已收录、等级门槛或活动排期；新活动沿用七天签到接口时，无需同步资源表即可处理。

日期判断将记录和当前时间统一在服务端 UTC 日期下，不额外加 8 小时，北京时间 08:00 换日。优先使用本次登录后最近响应的 `Utc` 加经过时间，其次使用登录快照的 `Utc` 和同步时间，缺少有效服务器时间时才使用本机 UTC 时间。旧登录的响应时钟不会沿用到新登录。月历使用同一日期判断，快照显示本签到日已签时也不发送 `MonthSignInHandler.SignIn`。

每项活动独立记录完成结果。一项失败继续检查其它活动，5 分钟后只重试失败项，已成功的月历、月卡和其它活动不再请求。邮件查询失败只补查邮件，不重复签到。正常响应必须对应原活动和记录 ID，天数不能倒退，四个时间字段必须有效；天数未增加仍是成功。已有采集记录确认明文 `Reward is Finish` 表示奖励领完，将这条精确消息作为本次登录检查完成，不再重试，也不修改快照天数；其它未知消息仍作为失败处理，不能把含有 `Already` 或 `Finish` 的任意错误当作成功。

活动成功后先写回缓存，再安排邮件查询；月历、多个活动和月卡产生的查询合并为一次，不领取邮件附件。客户端月历面板的成功回调也会无条件查邮件，此次同步补齐旧月历实现缺失的查询。日志区分“新增签到奖励”和“已记录登录但未新增奖励”。新登录可以再检查一次，失败重登不会释放已完成版本，反复关闭开启开关也不会重复已成功步骤。

请求仍使用快照的原始业务字段和 UTC 日期字符串，不推算或修改签到天数。正常响应按记录位置写回对应快照。当天/满七天判断通过后，与已成功步骤一样记入本次登录的逐项完成标记，避免失败重试时重复检查；没有额外签到轮询或新的活动发现请求。

旧 `backend/signin_ref.json` 及 `tools/sync_assets.py` 中的签到规则生成、验证、写出链已经删除。活动日志直接标识快照提供的 `ActivityID`，不再依赖活动名称静态映射，也无需更新服务器提供规则接口。

本轮触发问题的证据：2026-10-14 16:42 登录公告返回 `catalog_115657`，自动任务提示 `ActivitySignInH610` 未收录，16:47、16:52 重复相同错误，期间尚未发送活动签到请求。独立下载新版官方 catalog 和 2,492,632 字节的静态数据包，只提取 `Activity`、`SignInReward` 两张表，确认规则从 64 项增至 65 项，唯一新增 H610，原 64 项不变；H610 的 7 天奖励均要求 10 级。全表 65 种活动的 455 条奖励均为第 1～7 天，旧 115309 的 64 种也均为七天，用户据此确认采用统一七天上限。该资源核对不构成运行依赖；未来改变签到天数时需调整此上限。

## 证据与验证边界

客户端代码来源为 `E:/Project/ark2/tools/work/il2cpp/global-metadata-decrypted.dat` 和同目录的恢复 `libil2cpp`。关键 RVA：

| 方法 | RVA |
|---|---|
| `ShowCanSignInPanel` / `ProcessSignInList.MoveNext` | `0x204e0b4` / `0x205021c` |
| `ProcessWeekSignInPanel` / `GetActivityDBSetting` | `0x204e728` / `0x204df7c` |
| `NeedCallSignIn` / `CanSignIn` | `0x2231228` / `0x2231278` |
| `IsTodayCalledSignIn` / 下一天奖励查询 | `0x223138c` / `0x2231460` |
| `SignIn` / 成功回调 / 邮件查询 | `0x2231fa8` / `0x2232904` / `0x2232a9c` |
| 缓存 `Refresh` / `ActivityID` 匹配谓词 | `0x2231cb4` / `0x2231f40` |
| 活动设置查询 / 窗口检查 | `0x20fddb0` / `0x20fcff0` |
| 周期起点 / 终点 | `0x20fc788` / `0x20fc8ec` |
| 默认新手/回归 ID 的静态构造方法 | `0x2230c68` |
| `TimeHelper.NowTime` / 静态构造方法 | `0x20a56ec` / `0x20a9f7c` |
| `TimeHelper.SyncServerTime` / 响应同步的调用点 | `0x20a5a94` / `0x20f2aec` |
| `$date` 的 UTC 反序列化 / 直接字段赋值 | `0x22a2770` / `0x22a2ed4` |

`tools/test_activity_signin.py` 覆盖新活动、七天上限、原始请求字段、当天/次日重登、服务器时间校正、旧登录时钟隔离、北京时间午夜不换日及 08:00 前后的日界、未新增奖励、奖励领完、未知错误、响应校验、缓存写回、逐项失败重试及合并邮件查询。`tests/test_daily_signin.py` 保留登录调度、5 分钟退避、并发去重和月历/月卡验证；库存同步测试确认移除规则生成后其它资源仍可同步。

低等级仅记录登录、已过期窗口、跨日分界、多个未完成活动同时存在、返回不增加天数等边界没有真实抓包样本。10 月 9 日两次登录、一次活动签到、两次月历签到、七次月卡查询响应均提供顶层 `Utc`，递归检查也未发现 `TimeZoneUtcOffset`、`UtcOffset` 或 `ServerTime` 字段；原生代码确认了 `ServerTime.Now` 的同步，但仅凭这些样本尚不能还原顶层 `Utc` 到该对象的完整运行转换过程，也不能证明客户端凌晨重复请求。软件的日期实现以原始服务端 `Utc` 和用户确认的北京时间 08:00 日界为准，已有离线边界验证，真实分界前后签到仍待实测。
