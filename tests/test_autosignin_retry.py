#!/usr/bin/env python3
"""签到增强插件：数据页「重试失败站点」按钮 + retry_failed 接口的离线测试。"""
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "stubs"))
os.environ.setdefault("MP_CONFIG_PATH", "/tmp/mp-test-config")

import app.plugins.autosigninenhanced as mod  # noqa: E402
from app.plugins.autosigninenhanced import AutoSignInEnhanced  # noqa: E402


SITES = [
    {"name": "观众", "url": "https://audiences.me/", "id": "s1", "public": False, "cookie": "a=1"},
    {"name": "我堡", "url": "https://ourbits.club/", "id": "s2", "public": False, "cookie": "b=2"},
    {"name": "猫站", "url": "https://pterclub.com/", "id": "s3", "public": False, "cookie": "c=3"},
]


def make_plugin():
    p = AutoSignInEnhanced()
    p._enabled = True
    p._notify = True
    p._concurrency = 2
    p._jitter = 0
    p._retry_times = 0
    p._site_schema = []
    return p


def main():
    results = []

    def check(name, cond, detail=""):
        results.append(bool(cond))
        print(("✅ " if cond else "❌ ") + name + (f" — {detail}" if detail else ""))

    # 站点列表打桩
    mod.SitesHelper = type("S", (), {"get_indexers": staticmethod(lambda: SITES)})
    mod.SiteOper = type("O", (), {
        "list_order_by_pri": staticmethod(lambda: []),
        "success": staticmethod(lambda **kw: None),
        "fail": staticmethod(lambda *a, **kw: None),
    })

    p = make_plugin()
    key = f"{datetime.now().month}月{datetime.now().day}日"
    p.save_data(key, [
        {"site": "观众", "status": "失败", "message": "签到失败[站点改版]"},
        {"site": "我堡", "status": "Cookie已失效", "message": "签到失败[登录失效]"},
        {"site": "猫站", "status": "签到成功", "message": "签到已得100"},
    ])

    calls = []

    def fake_signin(info):
        calls.append(info["name"])
        if info["name"] == "观众":
            return info["name"], "签到已得42"
        return info["name"], "签到失败[盾未过]：命中 Cloudflare 挑战页"

    p.signin_site = fake_signin

    resp = p.retry_failed()
    check("接口返回成功", resp.success, resp.message)
    check("只重试失败的 2 个站点", set(calls) == {"观众", "我堡"}, str(calls))
    check("汇总为 1/2 成功", "1/2" in resp.message, resp.message)

    records = {r["site"]: r for r in p.get_data(key)}
    check("观众 状态已更新为签到成功", records["观众"]["status"] == "签到成功", records["观众"]["status"])
    check("我堡 仍为失败", "失败" in records["我堡"]["status"], records["我堡"]["status"])
    check("猫站 未被改动", records["猫站"]["status"] == "签到成功")
    check("发了重试通知", any("重试结果" in (m.get("title") or "") for m in getattr(p, "_messages", [])),
          str(getattr(p, "_messages", []))[:80])

    # 没有失败站点时
    p2 = make_plugin()
    p2.save_data(key, [{"site": "猫站", "status": "签到成功", "message": "x"}])
    resp2 = p2.retry_failed()
    check("无失败站点时提示", resp2.success and "没有失败" in resp2.message, resp2.message)

    # 失败站点超过 3 个 → 后台执行，接口立即返回
    import time as _time
    p3 = make_plugin()
    many = [{"site": n, "status": "失败", "message": "x"} for n in ("观众", "我堡", "猫站", "听听歌")]
    p3.save_data(key, many)
    done = []

    def slow_signin(info):
        _time.sleep(0.2)
        done.append(info["name"])
        return info["name"], "签到已得10"

    p3.signin_site = slow_signin
    mod.SitesHelper = type("S", (), {"get_indexers": staticmethod(
        lambda: SITES + [{"name": "听听歌", "url": "https://x.com/", "id": "s4", "public": False, "cookie": "d=4"}])})
    t0 = _time.time()
    resp3 = p3.retry_failed()
    cost = _time.time() - t0
    check("4 个失败站点走后台立即返回", "后台重试" in resp3.message and cost < 0.3, f"{cost:.2f}s / {resp3.message}")
    for _ in range(50):
        if len(done) == 4:
            break
        _time.sleep(0.1)
    check("后台确实跑完了 4 个", len(done) == 4, str(done))
    check("后台跑完后记录已更新",
          all("失败" not in r["status"] for r in p3.get_data(key)),
          str([r["status"] for r in p3.get_data(key)]))

    # 数据页要有按钮且指向正确接口
    page = p.get_page()
    dumped = json.dumps(page, ensure_ascii=False)
    check("数据页包含重试按钮文案", "重试失败站点" in dumped)
    check("按钮绑定 retry_failed 接口", "AutoSignInEnhanced/retry_failed" in dumped)
    check("按钮 apikey 已带上", "apikey=" in dumped)

    ok = sum(results)
    print(f"\n{ok}/{len(results)} 通过")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
