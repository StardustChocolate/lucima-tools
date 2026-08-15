"""标准库 HTTP 服务器（替代 FastAPI）。

纯 stdlib（http.server），无 pydantic/uvicorn 依赖，故可在 Windows(PyInstaller)
与 Android(Chaquopy) 共用同一份服务器代码。每请求一线程（ThreadingHTTPServer），
各账号的独立会话与后台调度由 accounts 模块管理。

对外提供 serve(port, host) 阻塞启动，及 make_server(port, host) 返回 server 对象
（Android 端在后台线程调 serve_forever）。
"""
from __future__ import annotations

import json
import mimetypes
import posixpath
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

from . import appcore
from . import config
from .appcore import ApiError
from .logutil import setup as _setup_log

log = _setup_log()

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

# 静态挂载点：URL 前缀 -> 本地目录
_STATIC_MOUNTS = {
    "/static/": appcore.FRONTEND_DIR,
    "/assets/": appcore.ASSETS_DIR,
}


def _route_api(method: str, path: str, body: dict, query: dict) -> dict:
    """把 (method, path, body, query) 分派到 appcore，返回结果 dict。
    多账号：account 参数 POST 从 body 取、GET 从 query 取。"""
    if method == "POST":
        if path == "/api/login":
            return appcore.login(body["account"], body["password"], body.get("savePwd", False))
        if path == "/api/login/direct":
            return appcore.login_direct(body["account"], body["password"],
                                        body.get("turnstileToken"), body.get("savePwd", False))
        if path == "/api/login/token":
            return appcore.login_token(body["gameToken"], body.get("account"))
        if path == "/api/tasks/auto":
            return appcore.run_auto(body.get("account"), body.get("taskIds", []), body.get("params", {}))
        if path == "/api/tasks/run":
            return appcore.run(body.get("account"), body.get("taskIds", []), body.get("params", {}))
        if path == "/api/shop/reset":
            return appcore.shop_reset_ep(body.get("account"), body.get("useGold", True), body.get("autoBuy", []))
        if path == "/api/shop/buy":
            return appcore.shop_buy_ep(body.get("account"), body["index"])
        if path == "/api/store/buy":
            return appcore.store_buy_ep(body.get("account"), body.get("picks", []))
        if path == "/api/sweep/run":
            return appcore.sweep_run(body.get("account"), body["sceneId"], body["teamId"],
                                     body.get("quick", True))
        if path == "/api/activity/run":
            return appcore.activity_run(body.get("account"), body["sceneId"], body["teamId"],
                                        body.get("supportCuid"), body.get("quick", True))
        if path == "/api/support/favorite":
            return appcore.support_fav_add(body.get("account"), body["cuid"])
        if path == "/api/support/unfavorite":
            return appcore.support_fav_remove(body.get("account"), body["cuid"])
        if path == "/api/arena/config":
            return appcore.arena_set(body.get("account"), body.get("picked"),
                                     body.get("teamId"))
        if path == "/api/status/refresh":
            return appcore.refresh_status(body.get("account"))
        if path == "/api/toggles":
            return appcore.set_toggles(body.get("account"), body.get("toggles", {}), body.get("params"))
        if path == "/api/accounts/logout":
            return appcore.logout(body["account"])
        if path == "/api/accounts/delete":
            return appcore.delete_account(body["account"])
        if path == "/api/config":
            return appcore.set_config(body.get("proxy"), body.get("proxyMode"))
        if path == "/api/dev/unlock":
            # 设置页那个无提示输入框：只问口令对不对，不暴露口令本身。
            return appcore.dev_unlock(body.get("pass"))
        if path == "/api/dev/call":
            # 开发者模式原始协议控制台。门禁在 appcore.dev_call 里验口令
            # （前端隐藏入口不算限制，见 config.DEV_PASSPHRASE 的注释）。
            return appcore.dev_call(body.get("account"), body.get("route"),
                                    body.get("data"), body.get("useAuth", True),
                                    body.get("pass"))
    elif method == "GET":
        if path == "/api/accounts":
            return appcore.list_accounts()
        if path == "/api/status":
            return appcore.status(query.get("account"))
        if path == "/api/roster":
            return appcore.roster(query.get("account"))
        if path == "/api/logs/auto":
            return appcore.auto_logs(query.get("account"), int(query.get("since") or 0))
        if path == "/api/tasks":
            return appcore.tasks()
        if path == "/api/sweep/options":
            return appcore.sweep_options(query.get("account"), query.get("kind") or "hunt")
        if path == "/api/store/options":
            return appcore.store_options(query.get("account"))
        if path == "/api/activity/options":
            return appcore.activity_options(query.get("account"))
        if path == "/api/arena/options":
            return appcore.arena_options(query.get("account"))
        if path == "/api/config":
            return appcore.get_config()
        if path == "/api/logs":
            return appcore.get_logs()
    raise ApiError(404, f"未知接口 {method} {path}")


class Handler(BaseHTTPRequestHandler):
    server_version = f"LucimaTools/{config.APP_VERSION}"

    # 静默默认日志（避免控制台刷屏 / GBK 乱码）
    def log_message(self, fmt, *args):
        pass

    # ---- 响应工具 ----
    def _send_json(self, obj: dict, status: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, ctype: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---- 静态文件 ----
    def _serve_static(self, path: str) -> bool:
        """命中静态资源则发送并返回 True；否则 False。"""
        # 根路径 / -> 前端首页
        if path == "/" or path == "":
            return self._serve_file(appcore.FRONTEND_DIR / "index.html")
        for prefix, base in _STATIC_MOUNTS.items():
            if path.startswith(prefix):
                rel = unquote(path[len(prefix):])
                # 防路径穿越：规范化后必须仍在 base 下
                safe = posixpath.normpath("/" + rel).lstrip("/")
                target = (base / safe).resolve()
                try:
                    target.relative_to(base.resolve())
                except ValueError:
                    self._send_json({"detail": "非法路径"}, 403)
                    return True
                return self._serve_file(target)
        return False

    def _serve_file(self, target: Path) -> bool:
        if not target.is_file():
            self._send_json({"detail": "文件不存在"}, 404)
            return True
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send_bytes(target.read_bytes(), ctype)
        return True

    # ---- 请求分派 ----
    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            body = self._read_body() if method == "POST" else {}
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            try:
                result = _route_api(method, path, body, query)
                self._send_json(result)
                log.info("%s %s -> 200", method, path)
            except ApiError as e:
                self._send_json({"detail": e.message}, e.status)
                log.warning("%s %s -> %d %s", method, path, e.status, e.message)
            except KeyError as e:
                self._send_json({"detail": f"缺少参数 {e}"}, 400)
                log.warning("%s %s -> 400 缺少参数 %s", method, path, e)
            except Exception as e:
                self._send_json({"detail": f"服务器错误：{e}"}, 500)
                # 未预期异常：记完整 traceback 供排查
                log.exception("%s %s -> 500 %s", method, path, e)
            return
        # 非 API：GET 静态资源
        if method == "GET":
            if self._serve_static(path):
                return
        self._send_json({"detail": "Not Found"}, 404)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def make_server(port: int = 8000, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def serve(port: int = 8000, host: str = "127.0.0.1"):
    srv = make_server(port, host)
    srv.serve_forever()


if __name__ == "__main__":
    print("LucimaTools server on http://127.0.0.1:8000")
    serve()
