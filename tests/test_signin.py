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
    "norez_count": 0,
}

CHALLENGE = """<!DOCTYPE html><html><head><title>Just a moment...</title></head>
<body><div id="cf-challenge">Checking your browser before accessing</div>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1"></script></body></html>"""

LOGIN_PAGE = """<html><body><form action="takelogin.php"><input name="username"></form>
<div>请先登录</div></body></html>"""

NORESULT_PAGE = """<html><body><div>欢迎回来</div><a href="logout.php">logout</a>
<div>个人中心</div></body></html>"""

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
            if mode == "norez":
                body, code = NORESULT_PAGE, 200
            elif mode == "norez_then_ok":
                # 前 2 次返回「没有签到文案」，之后返回已签到：模拟站点状态异步刷新
                with STATE["lock"]:
                    STATE["norez_count"] += 1
                    n = STATE["norez_count"]
                body, code = (NORESULT_PAGE if n <= 2 else OK_PAGE), 200
            elif mode == "down":
                body, code = "服务暂时不可用", 503
            elif mode == "fake_success":
                if "action=signin" in self.path:
                    body, code = OK_PAGE, 200
                else:
                    body, code = FAKE_SUCCESS_PAGE, 200
            elif mode == "action":
                if "action=signin" in self.path:
                    body, code = OK_PAGE, 200
                else:
                    body, code = ACTION_PAGE, 200
            elif mode == "login":
                body, code = LOGIN_PAGE, 200
            elif mode == "ok":
                body, code = OK_PAGE, 200
            else:
                cookie = self.headers.get("Cookie", "")
                ua = self.headers.get("User-Agent", "")
                ok = "cf_clearance=good" in cookie and "FlareSolverr" in ua
                body = OK_PAGE if ok else CHALLENGE
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


FAKE_SUCCESS_PAGE = """<html><body><div>签到成功，获得 10 个魔力值</div>
<a href="/attendance.php?action=signin">签到</a></body></html>"""

ACTION_PAGE = """<html><body><div>今日还未签到，签到可获得 10 个魔力值</div>
<a href="/attendance.php?action=signin">签到</a></body></html>"""


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
    from app.helper.browser import PlaywrightHelper
    PlaywrightHelper.canned_html = None
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
    # ---------- 场景 2b：UA 不匹配则盾会重弹（验证 UA 绑定处理） ----------
    reset()
    p2 = make_plugin()
    p2.signin_site(site_info())          # 直连被盾 → 解盾 → 缓存
    before_calls = FLARE["calls"]
    # 人为把缓存 UA 改错，模拟“用站点 UA 直连”
    cache_path = os.path.join(p2.get_data_path(), "cf_clearance.json")
    data = json.load(open(cache_path))
    data["127.0.0.1"]["ua"] = "UA/1.0"
    json.dump(data, open(cache_path, "w"))
    name, msg = p2.signin_site(site_info())
    check("场景2b UA 不匹配会重新解盾", FLARE["calls"] > before_calls,
          f"calls {before_calls}→{FLARE['calls']}")

    # ---------- 场景 3：Cookie 失效 → 分类为登录失效，不重试 ----------
    reset(mode="login")
    os.remove(cache_file)
    p = make_plugin()
    hits_before = len(STATE["hits"])
    name, msg = p.signin_site(site_info())
    check("场景3 判定需要登录", "需要重新登录" in msg or "Cookie 已失效" in msg, msg)
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
    check("场景4 判定网络不可达", "页面访问失败" in msg or "网络" in msg, msg)
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
    # 固定抖动值，避免计时波动导致断言不稳定
    import app.plugins.autosigninenhanced as plugin_mod
    real_uniform = plugin_mod.random.uniform
    plugin_mod.random.uniform = lambda a, b: 1.5
    t0 = time.time()
    from multiprocessing.dummy import Pool as ThreadPool
    try:
        with ThreadPool(min(len(sites), p._concurrency)) as pool:
            out = pool.map(p.signin_site, sites)
    finally:
        plugin_mod.random.uniform = real_uniform
    elapsed = time.time() - t0
    check("场景6 6 站全部成功", all("签到已得" in m for _, m in out), f"{len(out)} 个")
    check("场景6 并发不超过 2", STATE["max_in_flight"] <= 2, f"峰值 {STATE['max_in_flight']}")
    check("场景6 每站抖动生效（6站×1.5s/2并发≈4.5s）", elapsed > 4, f"耗时 {elapsed:.1f}s")

    # ---------- 场景 7：已登录但文案不匹配 → 改版分类 + 保存原始页面 ----------
    reset(mode="norez")
    p = make_plugin()
    debug_dir = os.path.join(str(p.get_data_path()), "debug")
    for f in os.listdir(debug_dir) if os.path.isdir(debug_dir) else []:
        os.remove(os.path.join(debug_dir, f))
    name, msg = p.signin_site(site_info())
    check("场景7 判定结果待核验", "签到证据" in msg, msg[:60])
    check("场景7 消息带页面文本片段", "欢迎回来" in msg, msg[:80])
    saved = os.listdir(debug_dir) if os.path.isdir(debug_dir) else []
    check("场景7 原始页面已落盘", len(saved) == 1, f"{saved}")

    # ---------- 场景 8：首读无文案、回读拿到回执 → 应判定成功 ----------
    reset(mode="norez_then_ok")
    STATE["norez_count"] = 0
    p = make_plugin()
    name, msg = p.signin_site(site_info())
    check("场景8 回读拿到权威状态", "签到已得" in msg, msg[:60])
    check("场景8 确实做了回读", len(STATE["hits"]) >= 2, f"请求 {len(STATE['hits'])} 次")

    # ---------- 场景 9：同域上游连续失败 → 熔断跳过 ----------
    reset(mode="down")
    p = make_plugin(_concurrency=1, _retry_times=0)
    outs = [p.signin_site(site_info(name=f"S{i}")) for i in range(3)]
    check("场景9 首个站点正常尝试", "服务" not in outs[0][1] and "签到" in outs[0][1] or "503" in outs[0][1] or "故障" in outs[0][1], outs[0][1][:50])
    check("场景9 连续 2 次失败后熔断后续站点", "熔断" in outs[2][1],
          f"{outs[1][1][:30]} / {outs[2][1][:30]}")

    # ---------- 场景 10：页面只有提示 + 签到链接 → 应自动访问动作地址 ----------
    reset(mode="action")
    p = make_plugin(_retry_times=0)
    name, msg = p.signin_site(site_info())
    check("场景10 提示文案不算成功", "可获得" not in msg or "签到已得" in msg, msg[:50])
    check("场景10 跟随签到动作后成功", "签到已得" in msg, msg[:60])
    check("场景10 访问了动作地址", any("action=signin" in h[1] for h in STATE["hits"]),
          f"{[h[1] for h in STATE['hits']][:4]}")

    # ---------- 场景 11：页面写「签到成功」但仍挂着签到入口 → 不能信 ----------
    reset(mode="fake_success")
    p = make_plugin(_retry_times=0)
    name, msg = p.signin_site(site_info())
    check("场景11 不把说明文案当回执", "签到已得" in msg, msg[:60])
    check("场景11 真的执行了签到动作", any("action=signin" in h[1] for h in STATE["hits"]),
          f"{[h[1] for h in STATE['hits']][:4]}")

    # ---------- 汇总 ----------
    ok = sum(1 for _, c, _ in results if c)
    print(f"\n{ok}/{len(results)} 通过")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
