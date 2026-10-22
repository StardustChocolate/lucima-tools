# 付费月卡领取链核对

核对日期：2026-10-09。通过已有资源、采集记录与恢复的 ARM64 客户端代码核对；分析和测试没有向游戏发送请求。`capture-20261009-142412` 已验证三种月卡的直接奖励、额外邮件与状态更新；现已将账户每日奖励查询并入“每日签到”，与月历、活动签到共用登录后的调度时机。领取实现通过离线模拟请求验证；用户另通过本软件协议控制台验证了同日重复月历签到，首次发奖尚未在本软件实测。活动签到的客户端规则和采集依据见 `docs/activity_signin_analysis.md`。

## 已确认的客户端链

```text
登录取得 AccountData.StatusContainer
→ 进入/驻留大厅：LobbyMainUIControl.Update
→ ProcessSystemPopupPanel 的系统弹窗队列
→ AccountStatusDayPanel.ShowPanel
→ StatusContainer.IsNeedQuery，且账户 UpdateTimer.CanUpdate
→ UpdateTimer.ResetNextUpdateTime(60.0)
→ AccountManager.QueryAccountStatus
→ ServerStatusHandler.Query
→ 处理 Drop、更新 AccountStatusList、执行公共响应同步
→ HasMail=true 时 MailManager.QueryNewestMails
→ 根据 NeedDisplay 显示每日账户奖励提示
```

这不是 `AccountHandler.Login` 内部的月卡专用请求。客户端在大厅系统弹窗流程中查询账户状态；进入大厅后和继续停留在大厅时都可能运行，购买后的 `OnBuy` 回调也会调用同一提示流程。请求同时处理其它账户状态，不只处理月卡。邮件查询与领取邮件分属不同方法，这条链没有自动调用 `ReceivedMail` 或 `ReceiceAllMail`。

离线代码来源为 [global-metadata-decrypted.dat](/E:/Project/ark2/tools/work/il2cpp/global-metadata-decrypted.dat)、同目录的 `libil2cpp.so` 与 `libil2cpp_rebuilt.so`。使用项目现有 `il2cpp_meta.py`、`analyze_damage_metadata.py`、`damage_binary_probe.py`、`extract_damage_disassembly.py` 在内存中映射元数据与恢复的函数体。关键证据位置如下，RVA 是客户端 ELF 虚拟地址，不是文本行号：

| 方法 | RVA / 关键指令 | 已验证行为 |
|---|---|---|
| `LobbyMainUIControl.Update` | `0x1f7b624`，调用 `0x1f7b648` | 计时到期进入系统弹窗流程 |
| `ProcessSystemPopupPanel` / 闭包 `b__3` | `0x1f7b658` / `0x1f7c060` | 队列回调在 `0x1f7c104` 调用 `AccountStatusDayPanel.ShowPanel` |
| `AccountStatusDayPanel.ShowPanel` | `0x1f0cb8c` | 游戏就绪、没有教程提示、状态需要查询、请求计时允许后，重置 60 秒限流并查询 |
| `AccountManager.QueryAccountStatus` | `0x20f3200`，末尾 `0x20f333c` | 新建空 `JsonObject` 并发送查询；路由元数据字面量编号 `13006` 为 `ServerStatusHandler.Query` |
| 查询成功回调 `<QueryAccountStatus>b__0` | `0x20f4944` | 调用 `ProcessDropResult`、`StatusContainer.OnUpdate`、`ProcessSyncServerBaseResult` |
| `ProcessSyncServerBaseResult` | `0x20f2854`，`0x20f2a20` / `0x20f2abc` | 检查响应 `HasMail`；为真时调用 `QueryNewestMails` |

## 请求与响应

已有采集证明请求顶层为 `route`、`data`，`data` 仅含账户鉴权字段 `AID`、`SessionID`；没有月卡 ID、状态 ID、日期或手动选择奖励的参数。客户端创建的空业务对象由 HTTP 层补齐鉴权。安全示意：

```json
{"route":"ServerStatusHandler.Query","data":{"AID":"<账户标识>","SessionID":"<会话标识>"}}
```

采集证据：[2026-09-14 记录](/E:/Project/ark_auto/capture/records/capture-20260914-003317.jsonl:1124)、[2026-10-09 早期记录](/E:/Project/ark_auto/capture/records/capture-20261009-084854.jsonl:2353)、[有效月卡请求](/E:/Project/ark_auto/capture/records/capture-20261009-142412.jsonl:1442)。这里只列字段名，未记录任何实际账户标识或会话值。

该路由走公共 `ServerBaseResult` 响应。客户端处理 `Drop`、`AccountStatusList` 和公共同步数据；`HasMail` 决定是否补查邮箱。有效月卡样本实际返回 `Event`、`MultiplierList`、`Drop`、`VirtualDrop`、`GuildDropResult`、`AccountStatusList`、`HasMail`、`Utc`；成功发奖时 `Event` 仍是 `"None"`，不能据此认定未领取。后续无奖励的查询仍返回 `AccountStatusList`，更早的无月卡样本则会省略该字段。

登录快照的 `StatusContainer.List` 中，月卡状态含 `_id`、`StaticID`、`TriggerTime`、`EndTime`、`NeedDisplay`；其它类别的状态不一定有 `_id`。时间格式为 `{"$date": epochMs}`。`DisplayList` 是另一份显示分类列表，不是可领取状态的筛选依据。

## 时间判断与提示标志

`AccountStatusData.IsTriggerOn`（RVA `0x20f671c`）只是读取 `TriggerTime` 并调用 `IsNowTimeMoreThanCheckTime`，不发送请求。`StatusContainer.IsNeedQuery`（`0x20f6724`）对状态列表执行同类时间判断；列表中有任一满足条件的状态即可进入查询闸门。

`IsNowTimeMoreThanCheckTime`（`0x20f6628`）读取的是静态状态的 **`LifeTimeType`**，字段偏移经元数据/二进制核对为 `0x44`。在 `Day` 分支，判断等价于：

```text
NowTime > TriggerTime
且 TriggerTime < TimeHelper.NowTime0000
```

`NowTime0000`（`0x20a57a4`）由 `NowTime` 截取当天零时；`NowTime`（`0x20a56ec`）来自 UTC 当前时间加游戏时区偏移与服务器时间校正，恢复代码的默认时区偏移为 8 小时（`TimeHelper` 静态构造方法 `0x20a9f7c`）。但 `SyncServerTime`（`0x20a5a94`）还会将服务端传入时间与当前时钟的差写入校正量，不能根据默认偏移直接认定同步后时钟仍是 UTC+8，详见活动签到分析中的后续核对。新样本中 `TriggerTime` 从昨天推进到今天，间隔约 20 小时 2 分 21 秒，已排除“必须距上次触发满 24 小时”。采集未覆盖日分界前后连续查询；当前月卡仍在登录后查询一次，由服务器判断奖励，本地不按时区推算领奖资格。

`NeedDisplay` 只参与奖励提示：`AccountStatusDayPanel` 的查询回调取 `GetItem` 类型状态，筛选 `NeedDisplay && !IsExpired()` 后显示列表（`0x1f0d3c4`、`0x1f0d3f4`）。**它不是是否请求领取的条件。** 新样本首次发奖后四个奖励状态的 `NeedDisplay` 仍为 `true`，随后无奖励的三次查询才变为 `false`。成功发奖时 `TriggerTime` 推进到服务器当天的发放时间，`EndTime` 不变；持续加成状态 `TIME07/09` 的时间不变。

## 三种月卡与直接奖励、额外邮件

原始资源链为 `sd_Commodity.Drops → sd_Drop.TagString → sd_ServerStatus`。物品名称对照本项目 [item_names.json](/E:/Project/ark_auto/backend/item_names.json)，数量及关联以原始表为准：

| 商品 | 购买时的一次性掉落 | 加入的账户状态 | 每日 `GetItem` 奖励 | 额外邮件附件 |
|---|---|---|---|---|
| `GIFTMonth01` | 钻石 `2 × 200` | `TIME06`、`TIME07` | `TIME06`：钻石 `2 × 50` | `GIFTMonth01Quick`：快速战斗券 `110 × 10` |
| `GIFTMonth02` | 钻石 `2 × 400` | `TIME08`、`TIME09` | `TIME08`：能源电池 `37 × 1` | `GIFTMonth02Quick`：快速战斗券 `110 × 10` |
| `GIFTMonth03` | 钻石 `2 × 400`、心愿币 `83 × 100` | `TIME23`、`TIME24` | `TIME23`：钻石 `2 × 40`；`TIME24`：心愿币 `83 × 10` | `GIFTMonth03Quick`：能源 `3 × 40` |

四个每日奖励状态均为 `Account / GetItem / LifeTimeType=Day / TriggerType=Day / TriggerValue=1`。`TIME06`、`TIME08`、`TIME23` 分别配置三种月卡邮件；`TIME24.Mail` 为空，不能遗漏第三种月卡的心愿币直接奖励。`TIME07` 是团员经验加成，`TIME09` 是金币掉落加成，属于持续效果。

表中直接奖励与邮件附件是两条数据定义，新样本已分别验证。客户端查询成功后处理 `Drop`，并在 `HasMail` 为真时补查邮箱；不能把“邮件没出现”直接等同于“所有月卡奖励都没领取”。三种邮件均配置 `RetentionHours=168`（7 天）。

原始文本证据：

- [sd_Commodity.txt](/E:/Project/ark2/out/assets/文本/数据表/sd_Commodity.txt:874)，三种商品的 `Drops`。
- [sd_Drop.txt 购买掉落](/E:/Project/ark2/out/assets/文本/数据表/sd_Drop.txt:5664)，购买掉落及加入的状态，包含 `TIME24`。
- [sd_ServerStatus.txt](/E:/Project/ark2/out/assets/文本/数据表/sd_ServerStatus.txt:7)，账户状态的奖励与邮件配置；`TIME24` 在第 12 行。
- [sd_Mail.txt](/E:/Project/ark2/out/assets/文本/数据表/sd_Mail.txt:34)，三种邮件附件组与 168 小时保留期。
- [sd_Drop.txt 邮件附件](/E:/Project/ark2/out/assets/文本/数据表/sd_Drop.txt:9490)，三个邮件附件组。
- [loc_CHS_FINAL.json](/E:/Project/ark2/tools/work/loc_live_v2/loc_CHS_FINAL.json:2114)，游戏提示当天奖励错过后无法补领；第 82131 行起为三种月卡邮件正文。

## 本次成功样本

证据文件：[HAR](/E:/Project/ark_auto/capture/records/capture-20261009-142412.har)、[JSONL](/E:/Project/ark_auto/capture/records/capture-20261009-142412.jsonl)。下表时间为采集机的北京时间；`Utc`、`TriggerTime` 则来自服务器，两种时钟不能混用。

| 操作 | 请求正文 / 响应正文 JSONL 行号 | 采集请求时间 | 验证结果 |
|---|---|---|---|
| `AccountHandler.Login` | 698 / 828 | 14:26:29.388 | `StatusContainer.List` 共 19 项，包含 6 项月卡状态 |
| 首次 `ServerStatusHandler.Query` | 1442 / 1455 | 14:27:44.081 | 直接奖励 4 条、月卡状态 6 项、`HasMail=true` |
| `MailHandler.QueryNewestMails` | 1446 / 1469 | 14:27:44.204 | 查询到今天新增的 3 封月卡邮件 |
| `MonthSignInHandler.SignIn` | 1620 / 1628 | 14:28:22.230 | 月历签到 `NowDay` 从 25 变为 26，与付费月卡分开 |
| 第二次状态查询 | 1737 / 1743 | 14:28:44.303 | 无 `Drop.Items`，`HasMail=false` |
| `TimingMealHandler.SentMeal` | 1761 / 1765 | 14:29:03.119 | 体力投喂使用独立接口 |
| 第三次状态查询 | 1801 / 1803 | 14:29:44.339 | 无 `Drop.Items`，`HasMail=false` |
| 第四次状态查询 | 1858 / 1861 | 14:30:45.082 | 无 `Drop.Items`，`HasMail=false` |

### 直接奖励与余额

首次响应的 `Drop.Items` 按顺序为：

| `Item.StaticID` | `Item.Count` 增量 | 对应状态 |
|---|---|---|
| `2` | 50 钻石 | `TIME06` |
| `37` | 1 能源电池 | `TIME08` |
| `2` | 40 钻石 | `TIME23` |
| `83` | 10 心愿币 | `TIME24` |

合计 90 钻石、1 能源电池、10 心愿币，与登录库存到最后 `NowItem.Count` 的余额差完全吻合。每条 `Item.Count` 是本次增量，`NowItem.Count` 是发放后的绝对余额。同一个钻石 ID 出现两次，库存要按响应顺序取最后的 `NowItem`，不能累加绝对余额，也不能先更新 `NowItem` 再重复加增量。现有 `GameClient._apply_now_items()` 已按这一规则处理。

### 状态是部分更新

首次和随后三次响应均只返回 `TIME06/07/08/09/23/24` 六项。六项的 `_id` 均为非空，与登录快照中同 `StaticID` 的状态逐一匹配且保持不变；其余 13 项账户状态没有返回，包括 8 项 VIP、4 项 Activity、1 项 `GuildMoneyBonus`。12 项 VIP/Activity 状态没有 `_id`，合并时不能将这些无 ID 状态压成一项。接入时按非空 `_id` 合并响应的六项，保留其它状态及 `DisplayList`，不以缺席推断删除。`GameClient.call()` 已通过 `_apply_account_status_updates()` 完成这部分缓存合并；全量登录仍由 `_replace_account_state()` 权威替换快照。

| 状态 | 登录时 `TriggerTime`（北京时间） | 首次查询后 `TriggerTime`（北京时间） |
|---|---|---|
| `TIME06` | 10 月 8 日 18:25:52.533 | 10 月 9 日 14:28:13.184 |
| `TIME08` | 10 月 8 日 18:25:52.577 | 10 月 9 日 14:28:13.213 |
| `TIME23` | 10 月 8 日 18:25:52.637 | 10 月 9 日 14:28:13.247 |
| `TIME24` | 10 月 8 日 18:25:52.645 | 10 月 9 日 14:28:13.255 |

四项 `EndTime` 均不变，随后三次查询的 `TriggerTime` 也不再推进。四次请求鉴权属于同一会话，间隔约 60 秒；已验证本会话重复查询不重复发奖，尚不能扩展为重新登录、请求超时后重试的完整保证。

### 邮件和时间边界

三封今天的月卡邮件 `_id` 均不在登录邮箱中，发放时间与对应状态的推进时间相差数毫秒：`GIFTMonth01/02` 各 10 张快速战斗券，`GIFTMonth03` 为 40 体力。三封邮件 `IsReceived=false`，`ExpiryTime-SentTime` 均为 7 天。本次没有捕获邮件领取接口；已验证直接奖励到账与附件投递，邮件附件仍待领取。第 1469、1475、1643 行返回的今天月卡邮件是同三个 ID，属于重叠查询，不能累计为发放六封或九封。

首次邮件请求早于状态网络响应：请求在 14:27:44.204，状态网络响应第 1449 行为 14:27:46.768。第二次邮件请求第 1453/1454 行为 14:27:49.058/059，发生在网络响应之后，与响应后补查流程吻合。状态正文第 1455 行直到 14:27:52.533 才异步记录，不能把正文记录时间当作客户端实际收到响应的时刻。首次并行邮件查询不能归因为 `HasMail` 回调；该回调关系由前述客户端代码确认，抓包不能单独证明第二次邮件查询的调用者。两次邮件查询均返回了状态发放时产生的新邮件。

`Utc` 是字符串形式的 Unix 秒值，状态与邮件的 `$date` 是毫秒值。首次响应 `Utc="1791527293"` 与奖励状态同秒，对应北京时间 14:28:13；采集网络响应为 14:27:46.768，正文记录为 14:27:52.533。服务端时间与采集机事件时间存在秒级差异，异步记录又引入额外延迟，不能直接以正文保存时间计算精确时钟偏移。若实现本地跨日判断，应使用服务器时间校正，不能把抓包机时间直接当作服务器时间。

## 历史样本与剩余边界

本次成功样本之前，7 个完整会话中的 13 次 `ServerStatusHandler.Query` 响应均为 `Event="None"`、`HasMail=false`，奖励对象只有 `IsReactorCritical`，没有实发奖励。7 个登录快照共 50 项状态均无 `TIME06/08/23/24`，且 `NeedDisplay` 全为 `false`。这些旧样本用于补充无有效月卡时的返回结构；本次已补足有效月卡实际发放的证据。

旧会话 `capture-20260914-003317.jsonl` 的首次状态请求在第 1124 行，邮件请求在第 1128 行，状态响应在第 1163 行。该邮件请求早于状态响应，属于并行初始化，不能归因为此响应的 `HasMail`。第二次状态请求/响应在第 1293/1297 行，间隔约 60.139 秒。`capture-20261009-084854.jsonl` 第 2353/2361、3024/3028、3101/3105 行的三次查询间隔约 60.701/60.223 秒，与恢复代码的 60 秒限流吻合。

当前尚未实测的边界为：

- 精确的日分界时刻；本次覆盖昨天到今天的触发记录，没有在分界前后连续查询。
- 重新登录后的同日查询、服务端已处理但客户端未收到响应时的重试。
- 漏过多天是否补发；资源文本明确提示错过当天奖励不能补领，但本次没有缺勤样本。
- 月卡到期、续费和状态移除，以及其它账户每日奖励的实际响应。

本次只有一次完整登录，次新 `capture-20261009-142331` 没有完整可解析业务登录，不能作为同日重登的对照。这些边界不阻碍“登录/快照刷新后调用一次，由服务器决定是否发放”的初版接入，也不需要先复刻客户端的本地跨日判断或 60 秒大厅轮询。

## 月历同日重复调用实测

用户在已完成签到的账号上，通过本软件协议控制台再次调用 `MonthSignInHandler.SignIn`，业务参数为空并自动补鉴权。接口返回 HTTP 200 和正常月历记录，没有返回“已领取”错误消息。省略真实记录 ID 后，关键字段为：

```json
{
  "NowDay": 26,
  "LastSignInTime": {"$date": 1791527331167},
  "Utc": "1791539573"
}
```

原游戏客户端领取响应见 `capture-20261009-142412.jsonl` 第 1628 行，`NowDay=26`、`LastSignInTime=1791527331167`，与重复调用完全一致。只有 `Utc` 从 `1791527331` 推进到 `1791539573`。原签到时间为北京时间 14:28:51.167，重复调用响应的服务器时间为 17:52:53，间隔 3 小时 24 分 1.833 秒。由此确认本次重复调用返回既有签到记录，不需要签到天数递增才算完成。

现有成功分支已经会标记本次登录的月历步骤完成，不会将这份响应判为失败。新增回归覆盖该响应和 5 分钟后不再请求，并将记录未变化时的日志改为“今日已完成月历签到”。邮箱查询失败时仍只补查邮箱。这一实测只确认月历接口的本次同日重复调用，不能直接用作活动签到重复调用、精确跨日或超时后重试的证据。

## 当前接入

“每日签到”沿用任务 ID `month_signin` 与现有开关，在每次成功登录、每日快照重登或手动刷新重登后执行：

1. 月历先比较快照中的 `LastSignInTime` 与游戏日期，当天已签则跳过；否则调用 `MonthSignInHandler.SignIn`，成功后写回 `MonthSignInData`。
2. `ServerStatusHandler.Query` 处理账户每日奖励，由公共响应同步库存和账户状态。
3. 活动先跳过已签满七天或当天已签的快照记录，其余提交 `WeekSignInHandler.SignIn`，由服务器判断是否发奖并写回对应记录。没有逐活动规则表、等级或排期依赖，详见活动签到分析。
4. 月历或任一活动签到成功、月卡响应 `HasMail=true` 时，通过 `_query_mail()` 查询并合并邮箱。多项成功合并为一次查询，不自动领取附件。

三项领取分别用 `calendarCheckedRevision`、`statusCheckedRevision`、`activityCheckedRevision` 记录本次 `loginRevision` 的成功结果；活动内部另按记录 ID 保存已完成项。邮件未查完时用 `pendingMailRevision` 记录待处理版本。全部完成后才设置总 `checkedRevision`。一个领取步骤失败不阻止其它步骤，失败按既有 5 分钟退避重试，仅补未完成步骤；发奖已成功而邮件查询失败时只补查邮件。成功重新登录允许新一轮执行，失败重登不释放已完成版本，关闭再打开开关也不会重复已成功的请求。

月历签到的管理器成功回调 `0x2230270` 只写回记录；上层 `MonthSignInPanel.ProcessMonthSignIn` 的成功回调 `0x204d254` 在 `0x204d3e0` 无条件查询最新邮件。此次重构同步补齐这条旧实现缺失的邮箱查询，即使没有月卡或待处理活动，月历签到成功后也会拉回奖励邮件缓存。

无 `Drop.Items` 是已领过或本次无奖励的正常结果，不依据 `Event="None"`、`NeedDisplay` 或月历签到的 `NowDay` 判为失败。任务日志展示本次直接奖励汇总，成功邮件查询不额外输出日志；月卡原有邮件附件领取仍由“一键领邮件”功能处理。每个登录版本执行一次账户状态查询，月历和活动按日期及完成状态决定是否请求，不新增大厅 60 秒轮询。

离线验证文件为 `tests/test_daily_signin.py`、`tests/test_account_status_state.py` 和 `tools/test_activity_signin.py`，覆盖一次性执行、并发去重、新登录、分步失败重试、三种月卡奖励与余额、无奖励响应、邮件仅查询、部分状态合并，以及活动记录提交和已完成响应。
