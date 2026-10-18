#!/usr/bin/env python3
"""
资源站签到插件的离线测试：本地假站点复刻 jying.top 的接口与鉴权。

接口约定（来自站点前端 bundle）：
  GET  /api/csrf/                     设置 csrftoken
  POST /api/app/login/                {username,password} → data.token
  GET  /api/app/checkin/stats/        X-App-User-Token → data.checked_today/total_points/streak
  POST /api/app/checkin/do/           普通签到 → data.delta/total_points
  GET  /api/app/checkin/luck/         幸运签到状态 → data.can_draw/can_draw_reason
  POST /api/app/checkin/luck/draw/    幸运签到 → data.delta/total_points
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "stubs"))
os.environ.setdefault("MP_CONFIG_PATH", "/tmp/mp-test-config")

PORT = 18092
FLARE_PORT = 18093

CHALLENGE = ('<html><head><title>Just a moment...</title></head><body>'
             'Checking your browser before accessing</body></html>')

STATE = {
    "token": "tok-123",
    "checked_today": False,
    "total_points": 100,
    "streak": 3,
    "require_cf": False,
    "cf_cleared": False,
    "do_calls": 0,
    "luck_calls": 0,
    "login_calls": 0,
    "password": "secret",
    "lock": threading.Lock(),
}
FLARE = {"calls": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload, headers=None):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, code, html):
        data = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return self.headers.get("X-App-User-Token") == STATE["token"]

    def do_GET(self):
        if STATE["require_cf"] and not STATE["cf_cleared"]:
            return self._send_html(403, CHALLENGE)
        if self.path == "/api/csrf/":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "csrftoken=abc123; Path=/")
            data = b'{"status": "success"}'
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if not self._authorized():
            return self._send(401, {"status": "error", "message": "登录已失效，请重新登录"})
        if self.path == "/api/app/checkin/stats/":
            return self._send(200, {"status": "success", "data": {
                "checked_today": STATE["checked_today"],
                "total_points": STATE["total_points"],
                "streak": STATE["streak"],
            }})
        if self.path == "/api/app/checkin/luck/":
            return self._send(200, {"status": "success", "data": {
                "can_draw": not STATE["checked_today"],
                "can_draw_reason": "" if not STATE["checked_today"] else "今日已参与",
                "max_daily_loss": 5,
            }})
        return self._send(404, {"status": "error", "message": "not found"})

    def do_POST(self):
        if STATE["require_cf"] and not STATE["cf_cleared"]:
            return self._send_html(403, CHALLENGE)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body or b"{}")
        except Exception:
            payload = {}

        if self.path == "/api/app/login/":
            STATE["login_calls"] += 1
            if payload.get("username") and payload.get("password") == STATE["password"]:
                return self._send(200, {"status": "success", "data": {"token": STATE["token"]}})
            return self._send(400, {"status": "error", "message": "用户名/邮箱或密码错误"})

        if not self._authorized():
            return self._send(401, {"status": "error", "message": "登录已失效，请重新登录"})

        if self.path == "/api/app/checkin/do/":
            STATE["do_calls"] += 1
            if STATE["checked_today"]:
                return self._send(400, {"status": "error", "message": "今天已经签到"})
            STATE["checked_today"] = True
            STATE["total_points"] += 10
            return self._send(200, {"status": "success", "data": {
                "delta": 10, "total_points": STATE["total_points"]}})

        if self.path == "/api/app/checkin/luck/draw/":
            STATE["luck_calls"] += 1
            if STATE["checked_today"]:
                return self._send(400, {"status": "error", "message": "今天已经签到"})
            STATE["checked_today"] = True
            STATE["total_points"] += 7
            return self._send(200, {"status": "success", "data": {
                "delta": 7, "total_points": STATE["total_points"]}})

        return self._send(404, {"status": "error", "message": "not found"})


class FlareHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        FLARE["calls"] += 1
        STATE["cf_cleared"] = True   # 解盾后放行
        out = {"status": "ok", "solution": {
            "url": "", "status": 200, "response": "<html>ok</html>",
            "userAgent": "Mozilla/5.0 (FlareSolverr)",
            "cookies": [{"name": "cf_clearance", "value": "good", "domain": "127.0.0.1"}],
        }}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(handler, port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def make_plugin(**over):
    from app.plugins.resourcesitecheckin import ResourceSiteCheckin
    p = ResourceSiteCheckin()
    p._enabled = True
    p._notify = True
    p._site_url = f"http://127.0.0.1:{PORT}"
    p._username = "user1"
    p._password = STATE["password"]
    p._token = ""
    p._checkin_mode = "normal"
    p._flare_url = f"http://127.0.0.1:{FLARE_PORT}"
    p._flare_proxy = ""
    p._proxy = ""
    for k, v in over.items():
        setattr(p, k, v)
    return p


def reset(**kw):
    STATE.update({
        "token": "tok-123", "checked_today": False, "total_points": 100, "streak": 3,
        "require_cf": False, "cf_cleared": False, "do_calls": 0, "luck_calls": 0,
        "login_calls": 0, "password": "secret",
    })
    STATE.update(kw)
    FLARE["calls"] = 0


def main():
    serve(Handler, PORT)
    serve(FlareHandler, FLARE_PORT)
    time.sleep(0.3)

    results = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name + (f" — {detail}" if detail else ""))

    # 场景 1：普通签到
    reset()
    p = make_plugin()
    ok, msg = p.checkin()
    check("场景1 普通签到成功", ok and "10" in msg, msg)
    check("场景1 确实调用了 do 接口", STATE["do_calls"] == 1, f"calls={STATE['do_calls']}")
    check("场景1 登录了一次", STATE["login_calls"] == 1)
    check("场景1 发出了通知", len(getattr(p, "_messages", [])) == 1)

    # 场景 2：重复运行 → 已签到，不再调 do
    ok2, msg2 = p.checkin()
    check("场景2 识别今日已签到", ok2 and "已签到" in msg2, msg2)
    check("场景2 没有重复签到", STATE["do_calls"] == 1, f"calls={STATE['do_calls']}")

    # 场景 3：幸运签到
    reset()
    p = make_plugin(_checkin_mode="luck")
    ok3, msg3 = p.checkin()
    check("场景3 幸运签到成功", ok3 and "7" in msg3, msg3)
    check("场景3 调用 luck/draw", STATE["luck_calls"] == 1)

    # 场景 4：token 失效 → 自动重登
    reset()
    p = make_plugin()
    p._token = "old-token"
    ok4, msg4 = p.checkin()
    check("场景4 token 失效后自动重登并签到", ok4 and "10" in msg4, msg4)
    check("场景4 触发了重新登录", STATE["login_calls"] == 1, f"calls={STATE['login_calls']}")

    # 场景 5：密码错误
    reset()
    p = make_plugin(_password="wrong-password")
    ok5, msg5 = p.checkin()
    check("场景5 登录失败有明确提示", (not ok5) and "登录失败" in msg5, msg5)

    # 场景 6：CF 挑战 → FlareSolverr 解盾后成功
    reset(require_cf=True)
    p = make_plugin()
    ok6, msg6 = p.checkin()
    check("场景6 解盾后签到成功", ok6 and "10" in msg6, msg6)
    check("场景6 调用过 FlareSolverr", FLARE["calls"] >= 1, f"calls={FLARE['calls']}")

    ok = sum(1 for _, c in results if c)
    print(f"\n{ok}/{len(results)} 通过")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
