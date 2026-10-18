#!/usr/bin/env python3
"""
离线测试：用桩模块替换 MoviePilot 运行时，验证签到的 CF 三层降级 / 失败分类 / 重试 / 并发抖动。

运行：python3 tests/test_signin.py
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

SITE_PORT = 18081
FLARE_PORT = 18191

# ---------------- 假站点状态 ----------------
STATE = {
    "mode": "cf",          # cf | login | ok
    "in_flight": 0,
    "max_in_flight": 0,
    "hits": [],
    "lock": threading.Lock(),
}

CHALLENGE = """<!DOCTYPE html><html><head><title>Just a moment...</title></head>
<body><div id="cf-challenge">Checking your browser before accessing</div>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1"></script></body></html>"""

LOGIN_PAGE = """<html><body><form action="takelogin.php"><input name="username"></form>
<div>请先登录</div></body></html>"""

OK_PAGE = """<html><body><div>欢迎回来</div><div>签到已得42 个魔力值</div>
<a href="logout.php">logout</a></body></html>"""


class SiteHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        with STATE["lock"]:
            STATE["in_flight"] += 1
            STATE["max_in_flight"] = max(STATE["max_in_flight"], STATE["in_flight"])
            STATE["hits"].append((time.time(), self.path, self.headers.get("Cookie", "")))
        try:
            time.sleep(0.15)  # 模拟站点响应时间，便于观察并发
            mode = STATE["mode"]
            if mode == "login":
                body, code = LOGIN_PAGE, 200
            elif mode == "ok":
                body, code = OK_PAGE, 200
            else:
                cookie = self.headers.get("Cookie", "")
                body = OK_PAGE if "cf_clearance=good" in cookie else CHALLENGE
                code = 200
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        finally:
            with STATE["lock"]:
                STATE["in_flight"] -= 1


# ---------------- 假 FlareSolverr ----------------
FLARE = {"calls": 0, "solve_ok": True}


class FlareHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        FLARE["calls"] += 1
        if not FLARE["solve_ok"]:
            out = {"status": "error", "message": "Challenge not solved"}
        else:
            out = {
                "status": "ok",
                "message": "Challenge solved!",
                "solution": {
                    "url": payload.get("url"),
                    "status": 200,
                    "response": OK_PAGE,
                    "userAgent": "Mozilla/5.0 (FlareSolverr)",
                    "cookies": [
                        {"name": "cf_clearance", "value": "good", "domain": ".127.0.0.1"},
                        {"name": "uid", "value": "1", "domain": ".127.0.0.1"},
                    ],
                },
            }
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
    from app.plugins.autosigninenhanced import AutoSignInEnhanced
    p = AutoSignInEnhanced()
    p._site_schema = []
    p._jitter = 0
    p._retry_times = 2
    p._flare_url = f"http://127.0.0.1:{FLARE_PORT}"
    p._flare_timeout = 20
    p._flare_proxy = ""
    p._concurrency = 2
    p._clearance_ttl = 1800
    for k, v in over.items():
        setattr(p, k, v)
    return p


def site_info(**over):
    info = {
        "name": "FakePT",
        "url": f"http://127.0.0.1:{SITE_PORT}/",
        "cookie": "uid=1; pass=abc",
        "ua": "UA/1.0",
        "render": False,
        "proxy": False,
        "timeout": 10,
    }
    info.update(over)
    return info


def reset(**kw):
    STATE.update({"mode": "cf", "in_flight": 0, "max_in_flight": 0, "hits": []})
    STATE.update(kw)
    FLARE.update({"calls": 0, "solve_ok": True})
    FLARE.update(kw.get("flare", {}))


def main():
    serve(SiteHandler, SITE_PORT)
    serve(FlareHandler, FLARE_PORT)
    time.sleep(0.3)

    from app.db.site_oper import SiteOper
    from app.utils.http import RequestUtils
    from app.helper.browser import PlaywrightHelper

    results = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))
        print(("✅ " if cond else "❌ ") + name + (f" — {detail}" if detail else ""))

    # ---------- 场景 1：直连被盾 → FlareSolverr 解盾 → 缓存 clearance ----------
    reset()
    p = make_plugin()
    cache_file = os.path.join(p.get_data_path(), "cf_clearance.json")
    if os.path.exists(cache_file):
        os.remove(cache_file)
    SiteOper.calls.clear()
    name, msg = p.signin_site(site_info())
    check("场景1 签到成功", "签到已得" in msg, msg)
    check("场景1 走了 FlareSolverr", FLARE["calls"] == 1, f"calls={FLARE['calls']}")
    check("场景1 缓存了 cf_clearance",
          os.path.exists(cache_file) and "cf_clearance=good" in open(cache_file).read())
    check("场景1 直连先失败一次再解盾", len(STATE["hits"]) >= 1, f"hits={len(STATE['hits'])}")

    # ---------- 场景 2：第二次直连用缓存，不再解盾 ----------
    before = FLARE["calls"]
    hits_before = len(STATE["hits"])
    name, msg = p.signin_site(site_info())
    check("场景2 第二次签到成功", "签到已得" in msg, msg)
    check("场景2 没有再次解盾", FLARE["calls"] == before, f"calls={FLARE['calls']}")
    check("场景2 只发了一次直连请求", len(STATE["hits"]) - hits_before == 1,
          f"新增 {len(STATE['hits']) - hits_before} 次")

    # ---------- 场景 3：Cookie 失效 → 分类为登录失效，不重试 ----------
    reset(mode="login")
    os.remove(cache_file)
    p = make_plugin()
    hits_before = len(STATE["hits"])
    name, msg = p.signin_site(site_info())
    check("场景3 判定登录失效", "登录失效" in msg, msg)
    check("场景3 登录类失败不重试", len(STATE["hits"]) - hits_before == 1,
          f"请求 {len(STATE['hits']) - hits_before} 次")
    check("场景3 不调用 FlareSolverr", FLARE["calls"] == 0, f"calls={FLARE['calls']}")

    # ---------- 场景 4：网络不通 → 分类网络超时 + 重试 2 次（共 3 次） ----------
    reset()
    p = make_plugin()
    bad = site_info(url="http://127.0.0.1:9/", timeout=2)
    t0 = time.time()
    name, msg = p.signin_site(bad)
    elapsed = time.time() - t0
    check("场景4 判定网络失败", "网络超时" in msg, msg)
    check("场景4 有退避重试", elapsed >= 5 + 10, f"耗时 {elapsed:.1f}s（5s+10s 退避）")

    # ---------- 场景 5：FlareSolverr 不可用 → 仿真兜底 ----------
    reset()
    FLARE["solve_ok"] = False
    p = make_plugin()
    os.remove(cache_file) if os.path.exists(cache_file) else None
    PlaywrightHelper.canned_html = OK_PAGE
    PlaywrightHelper.calls.clear()
    name, msg = p.signin_site(site_info(render=True))
    check("场景5 解盾失败后仿真成功", "签到已得" in msg, msg)
    check("场景5 确实调用了仿真", len(PlaywrightHelper.calls) == 1)

    # ---------- 场景 6：并发上限 2 + 抖动 ----------
    reset(mode="ok")
    p = make_plugin(_jitter=2, _concurrency=2)
    sites = [site_info(name=f"S{i}", url=f"http://127.0.0.1:{SITE_PORT}/?i={i}") for i in range(6)]
    t0 = time.time()
    from multiprocessing.dummy import Pool as ThreadPool
    with ThreadPool(min(len(sites), p._concurrency)) as pool:
        out = pool.map(p.signin_site, sites)
    elapsed = time.time() - t0
    check("场景6 6 站全部成功", all("签到已得" in m for _, m in out), f"{len(out)} 个")
    check("场景6 并发不超过 2", STATE["max_in_flight"] <= 2, f"峰值 {STATE['max_in_flight']}")
    check("场景6 抖动生效（耗时>3s）", elapsed > 3, f"耗时 {elapsed:.1f}s")

    # ---------- 汇总 ----------
    ok = sum(1 for _, c, _ in results if c)
    print(f"\n{ok}/{len(results)} 通过")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
