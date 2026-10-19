#!/usr/bin/env python3
"""观众（audiences.me）Turnstile 适配器单测：不需要真浏览器，用桩 PlaywrightHelper。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "stubs"))
os.environ.setdefault("MP_CONFIG_PATH", "/tmp/mp-test-config")

from app.helper.browser import PlaywrightHelper  # noqa: E402
from app.plugins.autosigninenhanced.sites.audiences import Audiences  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


TURNSTILE_PAGE = """<html><head><title>Audiences :: 签到</title></head><body>
<div class="attendance-card"><h2>人机验证</h2><p>验证通过后将自动完成签到</p>
<form id="attendance-form" action="attendance.php" method="post">
<input type="hidden" name="cf-token" id="cf-token" value="">
<div class="cf-turnstile" data-sitekey="0x4AAAAAABfcR5-BOyur3FT4"></div>
</form></div></body></html>"""

OK_PAGE = """<html><head><title>Audiences :: 签到</title></head><body>
<div class="attendance-card"><h2>签到成功</h2><p>签到已得 20 个爆米花</p></div></body></html>"""


def make_site():
    return {"name": "观众", "url": "https://audiences.me/", "cookie": "uid=1; pass=abc",
            "ua": "UA/1.0", "render": True, "proxy": False, "timeout": 30}


def main():
    # 1) match 只认观众
    a = Audiences()
    check("匹配 audiences.me", a.match("https://audiences.me/attendance.php") is True)
    check("不匹配别的站", a.match("https://ourbits.club/attendance.php") is False)

    # 2) 页面停在人机验证 → 失败且原因准确
    PlaywrightHelper.canned_html = TURNSTILE_PAGE
    ok, msg = a.signin(make_site())
    check("Turnstile 未通过判定失败", ok is False, msg)
    check("失败原因指向人机验证", "人机验证" in msg or "Turnstile" in msg, msg)

    # 3) 已签到页面 → 成功
    PlaywrightHelper.canned_html = OK_PAGE
    ok, msg = a.signin(make_site())
    check("拿到签到回执判定成功", ok is True, msg)
    check("回执里带签到文案", "签到已得" in msg or "签到成功" in msg, msg)

    # 4) 页面打不开 → 失败但不抛异常
    PlaywrightHelper.canned_html = None
    ok, msg = a.signin(make_site())
    check("页面打不开不抛异常", ok is False and "页面未打开" in msg, msg)

    ok_cnt = sum(1 for _, c, _ in results if c)
    print(f"\n{ok_cnt}/{len(results)} 通过")
    return 0 if ok_cnt == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
