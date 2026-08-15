"""离线回归: 虚拟幻境扫荡 / 每日免费招募 / 商店购买 的各条分支。

跑法(项目根): python tests/test_new_tasks.py
关注点:
- 标"应无请求"的分支 calls 必须为空（巡检 5 分钟一轮，多余请求既刷屏又可能扣道具）
- 免费招募买过一次后，同一 client 再跑必须不再发请求（靠 CommodityRecord 回写）
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.game_client import GameClient, GameMessage
from backend import tasks

fails = 0


def check(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(("  ok  " if cond else "  FAIL") + f" {label}" + (f"  {extra}" if extra else ""))


class FakeClient(GameClient):
    """复用真 GameClient 的缓存写回逻辑(_apply_now_items/_apply_commodity_record)，
    只把 call 换成假响应——这样测的就是真实的"买完回写"链路。"""

    def __init__(self, state, responses=None, errors=None):
        self.account_state = state
        self.aid = "aid"
        self.session_id = "sid"
        self.calls = []
        self.responses = responses or {}
        self.errors = errors or {}

    def call(self, route, data=None):
        self.calls.append((route, data))
        if route in self.errors:
            raise GameMessage(self.errors[route])
        body = self.responses.get(route)
        if callable(body):
            body = body(data)
        body = body or {}
        self._apply_now_items(body)
        self._apply_commodity_record(body)
        return body


def scene(sid, stars, oid="o1"):
    return {"_id": {"$oid": oid}, "StaticID": sid, "Stars": stars, "PassCount": 0}


def state_of(scenes=(), items=(), records=()):
    return {
        "SceneDataContainer": {"Scenes": list(scenes)},
        "ItemContainer": {"Items": [{"StaticID": s, "Count": c} for s, c in items]},
        "StoreRecordContainer": {"Records": list(records)},
    }


# ============ 虚拟幻境 ============
print("[虚拟幻境]")

c = FakeClient(state_of([scene("Abyss_1", [1, 0, 0]), scene("Abyss_3", [1, 0, 0])],
                        [("39", 3)]))
r = tasks.task_abyss_sweep(c)
check("未通关 80 层 -> 跳过且应无请求", r["skipped"] and not c.calls, r["detail"])

c = FakeClient(state_of([scene("Abyss_80", [1, 0, 0])], [("39", 0)]))
r = tasks.task_abyss_sweep(c)
check("已通关但无同步器 -> 跳过且应无请求", r["skipped"] and not c.calls, r["detail"])

# 进过第 85 层但没通关(Stars[0]=0) -> 不能算 85，仍按 80 层算
c = FakeClient(state_of([scene("Abyss_80", [1, 0, 0], "o80"),
                         scene("Abyss_85", [0, 0, 0], "o85")], [("39", 3)]),
               responses={tasks.ABYSS_SWEEP_ROUTE: {
                   "Drop": {"Items": [{"Item": {"StaticID": "25", "Count": 315},
                                       "NowItem": {"StaticID": "25", "Count": 999}}]},
                   "CostItems": [{"Item": {"StaticID": "39", "Count": 3},
                                  "NowItem": {"StaticID": "39", "Count": 0}}]}})
r = tasks.task_abyss_sweep(c)
sent = c.calls[0][1] if c.calls else {}
check("未通关的层不计入 -> 扫第 80 层", sent.get("StaticID") == "Abyss_80", str(sent.get("StaticID")))
check("扫荡成功记掉落与消耗", r["ok"] and "315星源粉末" in r["detail"] and "3意识同步器" in r["detail"],
      r["detail"])
check("请求体已转换格式(_id 去 $oid)", sent.get("_id") == "o80", str(sent.get("_id")))
check("消耗写回背包(同步器归 0)", tasks._item_count(c.account_state, "39") == 0)

r2 = tasks.task_abyss_sweep(c)
check("同步器已归 0 -> 再跑不发请求", r2["skipped"] and len(c.calls) == 1, r2["detail"])


# ============ 每日免费招募 ============
print("\n[每日免费招募]")

SUMMON_REC = {"_id": {"$oid": "sum1"}, "StaticID": "NormalSummon", "Store": "Summon",
              "BuyCount": 0, "FreeBuyCount": 0, "GuaranteedCount": 22,
              "LastBuyTime": {"$date": 1785200000000}}


def summon_resp(free_after=1, cost=None):
    rec = {**SUMMON_REC, "FreeBuyCount": free_after, "BuyCount": 1, "GuaranteedCount": 23}
    body = {
        "Drop": {"Items": [
            {"RoleData": {"StaticID": "H081", "Star": 3}},
            {"Item": {"StaticID": "69", "Count": 1}, "NowItem": {"StaticID": "69", "Count": 6}},
        ]},
        "CommodityRecord": rec,
    }
    if cost:
        body["CostItems"] = cost
    return body


c = FakeClient(state_of(records=[dict(SUMMON_REC)]),
               responses={"StoreHandler.BuyCommodity": summon_resp()})
r = tasks.task_free_summon(c)
check("免费额度可用 -> 抽一次", r["ok"] and len(c.calls) == 1, r["detail"])
sent = c.calls[0][1]
check("请求带 SelcetCostItemID 空串", sent.get("SelcetCostItemID") == "", str(sent.get("SelcetCostItemID")))
check("Record 已转格式", sent["Record"]["_id"] == "sum1")
check("日志含团员与保底计数", "3★" in r["detail"] and "保底计数 23" in r["detail"], r["detail"])

r2 = tasks.task_free_summon(c)
check("回写后再跑不发请求(不会扣契约)", r2["skipped"] and len(c.calls) == 1, r2["detail"])

c = FakeClient(state_of(records=[{**SUMMON_REC, "FreeBuyCount": 1}]))
r = tasks.task_free_summon(c)
check("额度已用 -> 跳过且应无请求", r["skipped"] and not c.calls, r["detail"])

c = FakeClient(state_of(records=[]))
r = tasks.task_free_summon(c)
check("查不到招募档 -> 报错不猜、应无请求", (not r["ok"]) and not c.calls, r["detail"][:40])

# 兜底：万一服务器真扣了东西，必须判成失败并显著告警
c = FakeClient(state_of(records=[dict(SUMMON_REC)]),
               responses={"StoreHandler.BuyCommodity": summon_resp(
                   cost=[{"Item": {"StaticID": "5", "Count": 1},
                          "NowItem": {"StaticID": "5", "Count": 9}}])})
r = tasks.task_free_summon(c)
check("响应有 CostItems -> 判失败并告警", (not r["ok"]) and "并非免费" in r["detail"], r["detail"][-40:])


# ============ 商店购买 ============
print("\n[商店购买]")

FS3 = {"_id": {"$oid": "fs3"}, "StaticID": "FriendShip3", "Store": "FriendShip",
       "BuyCount": 0, "FreeBuyCount": 0}
st = state_of(items=[("24", 500)], records=[dict(FS3)])
shelves = tasks.store_shelves(st)
by_id = {s["id"]: s for s in shelves}
check("三个商店都在", set(by_id) == {"VIPGift", "FriendShip", "MedalHonor"}, str(list(by_id)))
check("VIP 只放 4 个战斗应援",
      [g["id"] for g in by_id["VIPGift"]["goods"]] ==
      ["VIPGIFT_VIPQuick1", "VIPGIFT_VIPQuick2", "VIPGIFT_VIPQuick3", "VIPGIFT_VIPQuick4"])
check("友情只放 4 档",
      [g["id"] for g in by_id["FriendShip"]["goods"]] ==
      ["FriendShip3", "FriendShip4", "FriendShip11", "FriendShip10"])
mh = {g["id"]: g for g in by_id["MedalHonor"]["goods"]}
check("荣誉勋章只放技能模块+能源",
      list(mh) == ["MedalHonor1", "MedalHonor2"], str(list(mh)))
check("技能模块 150 勋章 · 周限 1 次",
      mh["MedalHonor1"]["cost"]["count"] == 150 and mh["MedalHonor1"]["limit"] == 1)
check("loc 未收录时用显式标签当名字", mh["MedalHonor1"]["name"] == "技能模块",
      mh["MedalHonor1"]["name"])
fs = {g["id"]: g for g in by_id["FriendShip"]["goods"]}
check("无记录的档也照样列出", not fs["FriendShip10"]["hasRecord"])
check("价格来自实抓表", fs["FriendShip3"]["cost"] == {"item": "24", "name": "友情点数", "count": 100})
check("loc 未收录的档用产出兜底命名", fs["FriendShip4"]["name"] == "5旗帜", fs["FriendShip4"]["name"])
vq = {g["id"]: g for g in by_id["VIPGift"]["goods"]}
check("VIPQuick4 = 25 券 + 30 能源", vq["VIPGIFT_VIPQuick4"]["gainText"] == "25快速战斗券、30能源",
      vq["VIPGIFT_VIPQuick4"]["gainText"])
check("只有 VIP 礼包给内容行（其余档名本身就是产出描述）",
      not fs["FriendShip10"]["gainText"] and not mh["MedalHonor1"]["gainText"])
check("VIP 缺记录给等级不足提示",
      "VIP 等级不足" in vq["VIPGIFT_VIPQuick1"]["hint"], vq["VIPGIFT_VIPQuick1"]["hint"])
check("VIP 限购 1", vq["VIPGIFT_VIPQuick1"]["limit"] == 1)
check("未买过 -> 剩余=上限", vq["VIPGIFT_VIPQuick1"]["remain"] == 1)
check("能源档每日 3 次", mh["MedalHonor2"]["limit"] == 3 and mh["MedalHonor2"]["remain"] == 3)
check("友情四档都是每日 1 次",
      all(fs[k]["limit"] == 1 for k in ("FriendShip3", "FriendShip4", "FriendShip10", "FriendShip11")))

# remain = 剩余可购次数（游戏内"购买 N/M"的 N），买满后为 0 但仍不拦截购买
st2 = state_of(records=[{"_id": {"$oid": "q1"}, "StaticID": "VIPGIFT_VIPQuick1",
                         "Store": "VIPGift", "BuyCount": 1},
                        {"_id": {"$oid": "m2"}, "StaticID": "MedalHonor2",
                         "Store": "MedalHonor", "BuyCount": 1}])
g2 = {g["id"]: g for s in tasks.store_shelves(st2) for g in s["goods"]}
check("买满 -> remain 0", g2["VIPGIFT_VIPQuick1"]["remain"] == 0
      and g2["VIPGIFT_VIPQuick1"]["bought"] == 1)
check("买过 1 次 -> remain 2", g2["MedalHonor2"]["remain"] == 2)
check("不再返回 buyable/reason（改由服务器裁决）",
      "buyable" not in g2["MedalHonor2"] and "reason" not in g2["MedalHonor2"])

c = FakeClient(st, responses={"StoreHandler.BuyCommodity": {
    "Drop": {"Items": [{"Item": {"StaticID": "3", "Count": 40},
                        "NowItem": {"StaticID": "3", "Count": 140}}]},
    "CostItems": [{"Item": {"StaticID": "24", "Count": 100},
                   "NowItem": {"StaticID": "24", "Count": 400}}],
    "CommodityRecord": {**FS3, "BuyCount": 1}}})
r = tasks.store_buy(c, [{"id": "FriendShip3", "count": 1}])
check("购买成功记产出与花费", r["ok"] and "40能源" in r["detail"] and "100友情点数" in r["detail"], r["detail"])
check("已购次数回写",
      {g["id"]: g for g in tasks.store_shelves(c.account_state)[1]["goods"]}
      ["FriendShip3"]["bought"] == 1)

# 快照缺记录：用合成记录照发请求（用户实测 10万金币 在游戏内可买，工具却灰的 bug）
c = FakeClient(state_of(records=[]), responses={"StoreHandler.BuyCommodity": {
    "Drop": {"Items": [{"Item": {"StaticID": "1", "Count": 100000},
                        "NowItem": {"StaticID": "1", "Count": 100000}}]}}})
r = tasks.store_buy(c, [{"id": "FriendShip10", "count": 1}])
sent = c.calls[0][1]["Record"]
check("缺记录时发合成记录", r["ok"] and sent["StaticID"] == "FriendShip10", r["detail"])
check("合成记录 _id 为空串、时间为 MinValue",
      sent["_id"] == "" and sent["LastBuyTime"] == "0001/1/1 0:0:0", str(sent)[:110])

c = FakeClient(st)
r = tasks.store_buy(c, [{"id": "FriendShip1", "count": 1}])
check("白名单外的档不发请求", not c.calls and r["errors"], r["detail"])

c = FakeClient(st, errors={"StoreHandler.BuyCommodity": "Error_NotEnough_Error_NotEnough"})
r = tasks.store_buy(c, [{"id": "FriendShip3", "count": 1}])
check("单档失败要暴露原因", not r["ok"] and "购买失败" in r["detail"], r["detail"])


print(f"\n{'全部通过' if not fails else str(fails) + ' 项失败'}")
sys.exit(1 if fails else 0)
