"""开发者模式协议控制台的门禁 + 报文原样返回 回归。

门禁是**口令**（设置页无提示输入框），且**在后端校验**：前端隐藏卡片不算限制
（工具会打包成 exe/APK 分发，任何能访问 localhost 的人都能直接 POST /api/dev/call）。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend import config, appcore, accounts

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


DEV = "tester@example.com"      # 口令门禁后，哪个账号都行
PASS = config.DEV_PASSPHRASE

print("===== ① 口令判定 =====")
check_true("正确口令通过", config.check_dev_pass(PASS))
check_true("错误口令不通过", not config.check_dev_pass("wrong"))
check_true("None 不通过", not config.check_dev_pass(None))
check_true("空串不通过", not config.check_dev_pass(""))
check_true("纯空格不通过", not config.check_dev_pass("   "))
check_true("首尾空格容错", config.check_dev_pass(f"  {PASS}  "))
check_true("大小写敏感（口令不是账号名）", not config.check_dev_pass(PASS.upper()))
check_true("前缀不算（防截断绕过）", not config.check_dev_pass(PASS[:-1]))
check_true("加后缀不算", not config.check_dev_pass(PASS + "x"))
# 解锁接口只回 ok，不回显口令/提示（前端也不显示任何反馈）
check("解锁接口对：ok=True", appcore.dev_unlock(PASS), {"ok": True})
check("解锁接口错：ok=False", appcore.dev_unlock("nope"), {"ok": False})

print("\n===== ② 接口门禁（不依赖前端隐藏）=====")


class FakeResp:
    def __init__(self, content, status=200):
        self.content = content
        self.status_code = status


class FakeHttp:
    def __init__(self, resp):
        self.resp = resp
        self.sent = []

    def request(self, method, url, content=None):
        self.sent.append((method, url, json.loads(content.decode())))
        return self.resp


class FakeClient:
    def __init__(self, resp):
        self.aid = "AID123"
        self.session_id = "SESS456"
        self.account_state = {"ItemContainer": {"Items": [{"StaticID": "1", "Count": 100}]}}
        self._client = FakeHttp(resp)
        self._proxy = ""

    def _sync_proxy(self):
        pass

    def _auth_data(self, extra=None):
        d = {"AID": self.aid, "SessionID": self.session_id}
        if extra:
            d.update(extra)
        return d

    def close(self):
        pass


def install(email, resp):
    """把一个假账号塞进注册表，返回它。"""
    acc = accounts.Account.__new__(accounts.Account)
    acc.email = email
    acc.client = FakeClient(resp)
    import threading
    acc.lock = threading.Lock()
    with accounts._reg_lock:
        accounts._accounts[email] = acc
    return acc


def uninstall(email):
    with accounts._reg_lock:
        accounts._accounts.pop(email, None)


# 口令不对/不给：即使账号已登录，也必须 403 且一个字节都不发出去
acc = install(DEV, FakeResp(b'{"ok":1}'))
for bad, why in ((None, "不带口令"), ("", "空口令"), ("wrong", "错口令"),
                 (PASS.upper(), "大小写不对")):
    try:
        appcore.dev_call(DEV, "AnyHandler.Any", {}, passphrase=bad)
        check_true(f"{why} 被拒", False, "居然放行了")
    except appcore.ApiError as e:
        check(f"{why} 返回 403", e.status, 403)
check("口令不对时一个请求都没发", len(acc.client._client.sent), 0)
uninstall(DEV)

print("\n===== ③ 明文响应必须原样返回（不吞 GameMessage）=====")
# 这正是当初丢失 CantReset 原始报文的原因：GameClient.call 遇非 JSON 会抛异常，
# 只剩消息文本，原始 body 没了。dev_call 必须把它原样带回。
acc = install(DEV, FakeResp("CantReset_CantReset".encode()))
r = appcore.dev_call(DEV, "StoreHandler.ResetRandomStore",
                 {"StoreID": "SecretShop", "IsUseGold": 0}, passphrase=PASS)
check("明文原样返回", r["rawText"], "CantReset_CantReset")
check_true("标记为非 JSON", r["isJson"] is False)
check_true("没有 json 字段", "json" not in r)
check("HTTP 状态带回", r["status"], 200)
check_true("有耗时", isinstance(r["elapsedMs"], int))
sent = acc.client._client.sent[0]
check("用 PUT", sent[0], "PUT")
check("route 正确", sent[2]["route"], "StoreHandler.ResetRandomStore")
check("自动补 AID", sent[2]["data"]["AID"], "AID123")
check("自动补 SessionID", sent[2]["data"]["SessionID"], "SESS456")
check("用户字段保留", sent[2]["data"]["StoreID"], "SecretShop")
check("IsUseGold 保留", sent[2]["data"]["IsUseGold"], 0)
check_true("request 回显与实发一致", r["request"] == sent[2])
uninstall(DEV)

print("\n===== ④ JSON 响应两种形态都给 =====")
acc = install(DEV, FakeResp(json.dumps({"Records": [{"Store": "SecretShop"}], "Utc": "1"}).encode()))
r = appcore.dev_call(DEV, "X.Y", {}, passphrase=PASS)
check_true("标记为 JSON", r["isJson"] is True)
check("解析后的 json 可用", r["json"]["Utc"], "1")
check_true("同时保留 rawText", "Records" in r["rawText"])
uninstall(DEV)

print("\n===== ⑤ gzip 自动解压 =====")
import gzip as _gz
acc = install(DEV, FakeResp(_gz.compress(b'{"a":1}')))
r = appcore.dev_call(DEV, "X.Y", {}, passphrase=PASS)
check_true("标记 gzip", r["gzip"] is True)
check("解压后可解析", r["json"], {"a": 1})
uninstall(DEV)

print("\n===== ⑥ useAuth=False 不补鉴权字段 =====")
acc = install(DEV, FakeResp(b'{}'))
appcore.dev_call(DEV, "X.Y", {"foo": 1}, use_auth=False, passphrase=PASS)
sent = acc.client._client.sent[0]
check("不补 AID", "AID" not in sent[2]["data"], True)
check("只有用户字段", sent[2]["data"], {"foo": 1})
uninstall(DEV)

print("\n===== ⑦ 参数校验 =====")
acc = install(DEV, FakeResp(b'{}'))
for bad, why in ((None, "None"), ("", "空串"), ("   ", "纯空格")):
    try:
        appcore.dev_call(DEV, bad, {}, passphrase=PASS)
        check_true(f"route={why} 被拒", False, "居然放行")
    except appcore.ApiError as e:
        check(f"route={why} 返回 400", e.status, 400)
check("非法 route 时没发请求", len(acc.client._client.sent), 0)
# data 传非 dict → 当空 dict，不炸
appcore.dev_call(DEV, "X.Y", "not a dict", passphrase=PASS)
check("data 非 dict 时退化为空", acc.client._client.sent[-1][2]["data"],
      {"AID": "AID123", "SessionID": "SESS456"})
uninstall(DEV)

print("\n===== ⑧ 不污染账号状态快照 =====")
# 手工试探不应改动缓存（否则倒计时/余额判定会被试探结果带偏）
payload = {"Drop": {"Items": [{"Item": {"StaticID": "1", "Count": 5},
                               "NowItem": {"StaticID": "1", "Count": 99999}}]},
           "Records": [{"_id": "r0", "Store": "SecretShop",
                        "LastResetTime": {"$date": 1}}]}
acc = install(DEV, FakeResp(json.dumps(payload).encode()))
before = json.dumps(acc.client.account_state, sort_keys=True)
appcore.dev_call(DEV, "StoreHandler.ResetRandomStore", {}, passphrase=PASS)
after = json.dumps(acc.client.account_state, sort_keys=True)
check_true("account_state 未被改写", before == after,
           "dev_call 走了写回钩子，会污染快照")
check_true("没有凭空长出货架容器", "StoreRecordContainer" not in acc.client.account_state)
uninstall(DEV)

print("\n===== ⑨ 接线检查（口令真源只在后端）=====")
srv = open(os.path.join(ROOT, "backend", "server.py"), encoding="utf-8").read()
check_true("路由已挂 /api/dev/call", "/api/dev/call" in srv)
check_true("路由已挂 /api/dev/unlock", "/api/dev/unlock" in srv)
check_true("server 把口令透传给 dev_call", 'body.get("pass")' in srv)
src = open(os.path.join(ROOT, "backend", "appcore.py"), encoding="utf-8").read()
check_true("账号列表不再下发 dev 标志（口令与账号解耦）", '"dev"' not in src)
check_true("dev_call 验口令", "check_dev_pass" in src)
js = open(os.path.join(ROOT, "frontend", "app.js"), encoding="utf-8").read()
check_true("前端不写死口令（真源在 config.DEV_PASSPHRASE）",
           PASS not in js,
           "口令出现在前端 JS 里 → 打包后任何人都能翻出来")
check_true("前端不再按账号判 dev", "rec.dev" not in js)
check_true("前端发 call 时带口令", "pass: devPass()" in js)
check_true("前端有无提示口令框的绑定", "devGate" in js)
# 每次重启都要重输口令 → 解锁态只能在内存里，不许落盘
check_true("解锁态不持久化（不 setItem 口令）",
           "setItem(DEV_PASS_LS" not in js and 'setItem("ark_dev_pass"' not in js,
           "口令被写进 storage → 重启还开着，且口令留在用户磁盘上")
check_true("解锁态是内存变量", "let DEV_PASS " in js)
check_true("启动会清掉旧版遗留的磁盘口令",
           'removeItem("ark_dev_pass")' in js)
html = open(os.path.join(ROOT, "frontend", "index.html"), encoding="utf-8").read()
check_true("口令框存在", 'id="devGate"' in html)
# 只看 #devGate 这一个 <input> 标签本身（别把后面 #devRoute 的 placeholder 算进来）
import re as _re
_tag = _re.search(r"<input[^>]*id=\"devGate\"[^>]*>", html)
check_true("找到口令框 input 标签", bool(_tag))
check_true("口令框无 placeholder（无任何提示）",
           bool(_tag) and "placeholder" not in _tag.group(0),
           "输入框带了提示文案，与'没有任何提示'的要求冲突")
# 也不该有伴随的 label/说明文字：整个 .dev-gate 容器里只有 input
_gate = _re.search(r"<div class=\"dev-gate\">(.*?)</div>", html, _re.S)
check_true("dev-gate 容器里没有其它文字/按钮",
           bool(_gate) and not _re.search(r"<(label|button|p|span)\b", _gate.group(1)),
           "容器里多了文案或按钮")
check_true("口令不写在 HTML 里", PASS not in html)
css = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
check_true("口令框有样式", ".dev-gate" in css)

print("\n" + (f"有 {len(FAIL)} 项失败: {FAIL}" if FAIL else "全部通过"))
sys.exit(1 if FAIL else 0)
