"""离线回归: 用假 client 跑 task_tesseract(技能模块 领取+加速) 的各条分支。

跑法(项目根): python tests/test_tesseract_charge.py
关注点: 标"应无请求"的分支 calls 必须为空——巡检 5 分钟一轮，多余请求会刷屏。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime, timezone

from backend.game_client import GameMessage
from backend import tasks

HOUR = 3600_000
DAY = 24 * HOUR


def now_ms():
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


class FakeClient:
    """只实现 tasks 用到的三件: account_state / _auth_data / call。"""

    def __init__(self, lab, fail=None):
        self.account_state = {"ArkStarForceLabData": dict(lab)}
        self.calls = []
        self.fail = fail or {}

    def _auth_data(self, extra=None):
        return {"AID": "a", "SessionID": "s", **(extra or {})}

    def call(self, route, data):
        self.calls.append(route)
        if route in self.fail:
            raise GameMessage(self.fail[route])
        lab = self.account_state["ArkStarForceLabData"]
        if route.endswith("ChargeTesseract"):
            lab = dict(lab)
            lab["TesseractChargeCount"] = int(lab.get("TesseractChargeCount") or 0) + 1
            lab["NextCanChargeTime"] = {"$date": now_ms() + DAY}
            done = tasks._ms(lab.get("NextCanReceiveTesseractTime")) or now_ms()
            lab["NextCanReceiveTesseractTime"] = {"$date": done - DAY}
            return {"ArkStarForceLabData": lab}
        if route.endswith("RewardTesseract"):
            lab = dict(lab)
            lab["TesseractChargeCount"] = 0
            lab["NextCanReceiveTesseractTime"] = {"$date": now_ms() + 7 * DAY}
            lab["NextCanChargeTime"] = {"$date": now_ms() + DAY}
            return {"ArkStarForceLabData": lab,
                    "Drop": {"Items": [{"Item": {"StaticID": "34", "Count": 1}}]}}
        raise AssertionError("unexpected route " + route)


def show(title, c, r):
    print(f"\n=== {title}")
    print("  calls   :", c.calls)
    print("  skipped :", r.get("skipped", False), "ok:", r.get("ok"))
    for ln in r.get("lines") or []:
        print("  line    :", ln)
    print("  detail  :", r.get("detail"))
    print("  count   :", c.account_state["ArkStarForceLabData"].get("TesseractChargeCount"))


# ① 已到领取时间 → 直接领，不加速
c = FakeClient({"NextCanReceiveTesseractTime": {"$date": now_ms() - HOUR},
                "NextCanChargeTime": {"$date": now_ms() - HOUR},
                "TesseractChargeCount": 1})
show("到点即领(不该加速)", c, tasks.task_tesseract(c))

# ② 未到点 + 加速冷却已过 + 额度未满 → 加速一次；剩余仍 >24h 故领不到
c = FakeClient({"NextCanReceiveTesseractTime": {"$date": now_ms() + 5 * DAY},
                "NextCanChargeTime": {"$date": now_ms() - HOUR},
                "TesseractChargeCount": 0})
show("生产中→加速1次(仍未完成)", c, tasks.task_tesseract(c))

# ③ 未到点但只剩 3 小时 → 加速后完成时间落到过去 → 立刻再领
c = FakeClient({"NextCanReceiveTesseractTime": {"$date": now_ms() + 3 * HOUR},
                "NextCanChargeTime": {"$date": now_ms() - HOUR},
                "TesseractChargeCount": 2})
show("加速后立刻可领", c, tasks.task_tesseract(c))

# ④ 加速额度用尽 → 不发任何请求(skipped)
c = FakeClient({"NextCanReceiveTesseractTime": {"$date": now_ms() + 2 * DAY},
                "NextCanChargeTime": {"$date": now_ms() - HOUR},
                "TesseractChargeCount": 3})
show("额度用完(应无请求)", c, tasks.task_tesseract(c))

# ⑤ 加速冷却中 → 不发任何请求(skipped)
c = FakeClient({"NextCanReceiveTesseractTime": {"$date": now_ms() + 2 * DAY},
                "NextCanChargeTime": {"$date": now_ms() + 6 * HOUR},
                "TesseractChargeCount": 1})
show("冷却中(应无请求)", c, tasks.task_tesseract(c))

# ⑥ 服务器拒绝加速 → 不炸、要可见(非 skipped)、且不再重试
c = FakeClient({"NextCanReceiveTesseractTime": {"$date": now_ms() + 2 * DAY},
                "NextCanChargeTime": {"$date": now_ms() - HOUR},
                "TesseractChargeCount": 0},
               fail={"ArkStarForceLabHandler.ChargeTesseract": "Already_Charged"})
show("服务器拒绝加速", c, tasks.task_tesseract(c))
show("拒绝后重跑(应退避、无新请求)", c, tasks.task_tesseract(c))

# ⑦ 全新周期(字段缺失, 老号快照可能没有 charge 字段)
c = FakeClient({"NextCanReceiveTesseractTime": {"$date": now_ms() + 6 * DAY}})
show("字段缺失", c, tasks.task_tesseract(c))
