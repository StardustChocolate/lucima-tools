"""购买目标前后端一致性 + 免费刷商店买芯片的离线回归。

起因：前端曾自带一份 BUY_TARGETS（缺省全选三项），后端 DEFAULT_SHOP_WANTED 只有
招募/神秘契约 —— 界面显示会买芯片、巡检却从不买，用户报"漏买强化芯片"。
现在后端 tasks.BUY_TARGETS 是唯一真源，前端从 /api/tasks 取。本测试锁住这个约束：
前端源码里不得再出现第二份目标表，且空 params 必须买全部目标。
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.tasks import BUY_TARGETS, DEFAULT_SHOP_WANTED, task_shop_free, run_toggle_tasks
from backend import appcore

FAIL = []


def check(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + (f" => {got!r}" if ok else f"\n      实际 {got!r}\n      期望 {want!r}"))
    if not ok:
        FAIL.append(name)


def check_true(name, cond, hint=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  {hint}"))
    if not cond:
        FAIL.append(name)


CHIPS = ["EC11", "EC21", "EC31", "EC41", "EC51", "EC61"]


class FakeClient:
    """记录所有请求；ResetRandomStore 返回给定货架，BuyCommodity 记账。"""

    def __init__(self, shelf, free_ok=True, state=None, fail_msg="CantReset"):
        self.shelf = shelf
        self.free_ok = free_ok
        self.fail_msg = fail_msg
        self.calls = []
        self.bought = []
        self.account_state = state or {}

    def _auth_data(self, d):
        return {"AID": "a", "SessionID": "s", **d}

    def call(self, route, payload):
        self.calls.append((route, payload))
        if route == "StoreHandler.ResetRandomStore":
            if not self.free_ok:
                from backend.game_client import GameMessage
                # 真实拒绝消息：2026-07-30 在真账号上连发 3 次免费刷新实测，
                # 第 1 次成功、第 2/3 次均回明文 `CantReset`（不扣钻/不扣金币）
                raise GameMessage(self.fail_msg)
            return {"Records": self.shelf}
        if route == "StoreHandler.BuyCommodity":
            sid = ((payload["Record"].get("DropResult") or {}).get("Items") or [{}])[0].get("Item", {}).get("StaticID")
            self.bought.append(sid)
            return {"Drop": {"Items": [{"Item": {"StaticID": sid, "Count": 1}}]}}
        raise AssertionError("意外 route " + route)


def shelf_of(*sids):
    return [
        {"_id": f"r{i}", "StaticID": f"SecretShop{i+1}", "Store": "SecretShop", "BuyCount": 0,
         "DropResult": {"Items": [{"Item": {"StaticID": s, "Count": 1}}]}}
        for i, s in enumerate(sids)
    ]


print("===== ① 前后端一致性（结构约束）=====")
keys = [t["key"] for t in BUY_TARGETS]
check("后端目标 key", keys, ["recruit", "mystery", "chip"])
check("chip 展开六部位", [t for t in BUY_TARGETS if t["key"] == "chip"][0]["ids"], CHIPS)
check("默认白名单=全部目标展开", DEFAULT_SHOP_WANTED, ["5", "6"] + CHIPS)
check_true("默认白名单含芯片", all(c in DEFAULT_SHOP_WANTED for c in CHIPS),
           "这正是漏买芯片的根因")
for t in BUY_TARGETS:
    check_true(f"目标 {t['key']} 字段齐全", all(k in t for k in ("key", "label", "stat", "ids")))

# /api/tasks 必须把表下发给前端
payload = appcore.tasks()
check_true("/api/tasks 带 buyTargets", "buyTargets" in payload)
check("buyTargets 与真源同一份", payload["buyTargets"], BUY_TARGETS)

print("\n===== ② 前端不得自带第二份目标表 =====")
app_js = open(os.path.join(ROOT, "frontend", "app.js"), encoding="utf-8").read()
check_true("前端 BUY_TARGETS 是空数组声明（不写死内容）",
           re.search(r"let BUY_TARGETS = \[\];", app_js) is not None,
           "前端又写死目标表了 → 会再次与后端漂移")
for sid in CHIPS + ["5", "6"]:
    check_true(f"前端源码不含硬编码 sid {sid}", f'"{sid}"' not in app_js.split("let BUY_TARGETS")[1][:400],
               "目标 sid 应只在后端定义")
check_true("前端从响应里取 buyTargets", "buyTargets" in app_js)
check_true("前端不再硬编码 key 列表",
           '["recruit", "mystery", "chip"]' not in app_js,
           "getShopCfg 的缺省应由 allTargetKeys() 推导")

print("\n===== ③ 空 params 时必须买全部目标（原 bug 复现点）=====")
c = FakeClient(shelf_of("EC41", "1", "3"))
r = task_shop_free(c, {})            # 巡检在 params 为空时就是这样调的
check("空 params 买到芯片", c.bought, ["EC41"])
check_true("结果 ok", r["ok"])

c = FakeClient(shelf_of(*CHIPS))
task_shop_free(c, {})
check("空 params 六部位芯片全买", c.bought, CHIPS)

c = FakeClient(shelf_of("5", "6", "EC11"))
task_shop_free(c, {})
check("三类同屏全买", sorted(c.bought), sorted(["5", "6", "EC11"]))

print("\n===== ④ 经 run_toggle_tasks（巡检真实调用路径）=====")
c = FakeClient(shelf_of("EC31"))
res = run_toggle_tasks(c, ["shop_free"], {})      # accounts.py 传的就是 self.params
check("巡检路径买到芯片", c.bought, ["EC31"])
check_true("巡检结果 ok", res and res[0].get("ok"))

c = FakeClient(shelf_of("EC31"))
run_toggle_tasks(c, ["shop_free"], {"shop_free": {}})   # params 有键但为空 dict
check("params 有空 dict 也买芯片", c.bought, ["EC31"])

print("\n===== ⑤ 显式配置仍然生效（不被默认值覆盖）=====")
c = FakeClient(shelf_of("5", "EC11"))
task_shop_free(c, {"wanted": ["5"]})
check("只选招募时不买芯片", c.bought, ["5"])

print("\n===== ⑥ 其它既有约束不回归 =====")
c = FakeClient(shelf_of("EC11"))
task_shop_free(c, {})
reset = [p for rt, p in c.calls if rt == "StoreHandler.ResetRandomStore"]
check("免费刷 IsUseGold=0", reset[0]["IsUseGold"], 0)

# 已购档位不重复买
c = FakeClient(shelf_of("EC11"))
c.shelf[0]["BuyCount"] = 1
task_shop_free(c, {})
check("BuyCount>0 跳过", c.bought, [])

# 冷却中(CantReset) → 跳过本轮，一件不买、不拿过期缓存货架顶替
c = FakeClient([], free_ok=False,
               state={"StoreRecordContainer": {"Records": shelf_of("EC21")}})
r = task_shop_free(c, {})
check("CantReset 不买任何东西", c.bought, [])
check_true("CantReset 标记 skipped", r.get("skipped") is True)
check_true("CantReset 不算失败", r.get("ok") is True)
check("CantReset 只发了 1 个请求", [rt for rt, _ in c.calls], ["StoreHandler.ResetRandomStore"])

# 其它未知错误必须抛出，不能被当成"冷却中"吞掉
c = FakeClient([], free_ok=False, state={}, fail_msg="SomethingElseWrong")
try:
    task_shop_free(c, {})
    check_true("未知 GameMessage 应抛出", False, "被静默吞掉了")
except Exception as e:
    check_true("未知 GameMessage 抛出", "SomethingElseWrong" in str(e))

print("\n===== ⑦ 免费刷新档期（LastResetTime + 1h）=====")
import time as _time
from backend.tasks import secret_shop_next_free, SHOP_FREE_COOLDOWN_MS, next_claim_times

NEVER = -62135596800000
HOUR = 60 * 60 * 1000


def state_with(last_reset_ms, n=6):
    """构造带 n 档秘密商店货架的账号快照。"""
    return {"StoreRecordContainer": {"Records": [
        {"_id": f"r{i}", "StaticID": f"SecretShop{i+1}", "Store": SECRET_ID,
         "BuyCount": 0, "LastResetTime": {"$date": last_reset_ms}}
        for i in range(n)
    ]}}


SECRET_ID = "SecretShop"
now = int(_time.time() * 1000)

check("冷却常量=1小时", SHOP_FREE_COOLDOWN_MS, HOUR)
check("刚刷完 → 下次=+1h", secret_shop_next_free(state_with(now)), now + HOUR)
check_true("刚刷完 → 未到点", secret_shop_next_free(state_with(now)) > now)
check_true("1h 前刷的 → 已到点", secret_shop_next_free(state_with(now - HOUR - 1000)) <= now)
check("无货架记录 → None（视为可刷）", secret_shop_next_free({}), None)
check("LastReset 是 C# MinValue → None", secret_shop_next_free(state_with(NEVER)), None)
# 6 档理应同值；某档滞后时取 max，避免把档期算早、每轮空发换回 CantReset
mixed = state_with(now - 2 * HOUR)
mixed["StoreRecordContainer"]["Records"][3]["LastResetTime"] = {"$date": now}
check("各档不一致时取最新", secret_shop_next_free(mixed), now + HOUR)

nc = next_claim_times(state_with(now))["shop_free"]
check("next_claim_times 给出 nextMs", nc["nextMs"], now + HOUR)
check_true("刚刷完 ready=False", nc["ready"] is False)
nc2 = next_claim_times(state_with(now - HOUR - 1000))["shop_free"]
check_true("到点 ready=True", nc2["ready"] is True)

print("\n===== ⑧ 调度闸门：按档期而非固定 55 分钟 =====")
# 原 bug：真实冷却 60 分钟、节流 55 分钟 → 55 分钟时必被拒，而 _shop_free_last 按"尝试"
# 更新，于是要再等 55 分钟 → 实际 110 分钟才成功一次，白丢近一半免费刷新。
from backend.accounts import Account


class StubClient:
    def __init__(self, state):
        self.account_state = state

    def close(self):
        pass


def due_of(state, shop_free_last=0.0):
    a = Account("tester@example.com", StubClient(state), None)
    a.toggles = {"shop_free": True, "reactor": True}
    a._shop_free_last = shop_free_last
    return a._due_ids()


check("刚刷完 → 不发请求", due_of(state_with(now)), ["reactor"])
check_true("55 分钟时仍不发（真实冷却 60 分钟）",
           "shop_free" not in due_of(state_with(now - 55 * 60 * 1000)),
           "这正是原来白丢一半次数的地方")
check_true("60 分钟后立刻发", "shop_free" in due_of(state_with(now - HOUR - 1000)))
check_true("拿不到档期时退回固定间隔(刚跑过→不发)",
           "shop_free" not in due_of({}, shop_free_last=_time.time()))
check_true("拿不到档期且久未跑 → 发",
           "shop_free" in due_of({}, shop_free_last=0.0))

print("\n===== ⑨ 刷新后把新货架写回缓存（否则倒计时不走）=====")
from backend.game_client import GameClient

gc = GameClient.__new__(GameClient)          # 不走 __init__，只测写回逻辑
gc.account_state = state_with(now - 3 * HOUR)
old_next = secret_shop_next_free(gc.account_state)
gc._apply_store_records({"Records": [
    {"_id": f"r{i}", "StaticID": f"SecretShop{i+1}", "Store": SECRET_ID,
     "BuyCount": 0, "LastResetTime": {"$date": now}} for i in range(6)
]})
check("写回后档期推进到 +1h", secret_shop_next_free(gc.account_state), now + HOUR)
check_true("确实变了（不是没写进去）", secret_shop_next_free(gc.account_state) != old_next)
check("档数没重复膨胀", len(gc.account_state["StoreRecordContainer"]["Records"]), 6)

# 新档位应追加而不是覆盖
gc._apply_store_records({"Records": [
    {"_id": "brand_new", "StaticID": "SecretShop7", "Store": SECRET_ID,
     "BuyCount": 0, "LastResetTime": {"$date": now}}]})
check("新档追加", len(gc.account_state["StoreRecordContainer"]["Records"]), 7)
# 无 Store 字段的响应不该动货架（如 BuyCommodity 的 Records 语义不同）
before = len(gc.account_state["StoreRecordContainer"]["Records"])
gc._apply_store_records({"Records": [{"_id": "x", "NoStoreField": 1}]})
check("无 Store 字段不写入", len(gc.account_state["StoreRecordContainer"]["Records"]), before)
gc._apply_store_records({})                   # 没有 Records 键不炸
check_true("响应无 Records 键安全", True)

# ⚠️ 上面测的是方法本身。还必须锁住"call() 真的调了它"——否则把 call() 里那行删掉，
# 方法测试依旧全过，而实际货架永远不写回、倒计时永不推进（这个漏洞真发生过）。
import inspect
call_src = inspect.getsource(GameClient.call)
check_true("call() 里挂了 _apply_store_records",
           "_apply_store_records" in call_src,
           "写回没接进 call()，倒计时不会走")

# 端到端：伪造一次 ResetRandomStore 走完 call()，档期必须推进
gc2 = GameClient.__new__(GameClient)
gc2.account_state = state_with(now - 3 * HOUR)
new_records = [
    {"_id": f"r{i}", "StaticID": f"SecretShop{i+1}", "Store": SECRET_ID,
     "BuyCount": 0, "LastResetTime": {"$date": now}} for i in range(6)
]
# 只跑 call() 尾部那三个写回钩子（不碰网络）
for hook in (gc2._apply_now_items, gc2._apply_commodity_record, gc2._apply_store_records):
    hook({"Records": new_records})
check("经写回钩子后档期推进", secret_shop_next_free(gc2.account_state), now + HOUR)

print("\n" + (f"有 {len(FAIL)} 项失败: {FAIL}" if FAIL else "全部通过"))
sys.exit(1 if FAIL else 0)
