#!/usr/bin/env python3
"""签到结果解析的单元测试：覆盖各站不同的文案变体。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "stubs"))
os.environ.setdefault("MP_CONFIG_PATH", "/tmp/mp-test-config")

from app.plugins.autosigninenhanced import check_attendance  # noqa: E402

CASES = [
    # (说明, HTML, 期望成功, 期望消息包含)
    ("签到已得（无空格）", "<div>签到已得42 个魔力值</div>", True, "签到已得42"),
    ("签到已得（有空格）", "<div>签到已得 42 个魔力值</div>", True, "签到已得42"),
    ("签到成功，获得 N", "<div>签到成功，获得 5 个魔力值</div>", True, "获得 5"),
    ("本次签到获得", "<div>本次签到获得 8 个魔力值</div>", True, "本次签到获得 8"),
    ("第 N 次签到完整句", "<div>这是您第 3 次签到，已连续 7 天，获得 10 个魔力值</div>", True, "第 3 次签到"),
    ("今天已经签到过", "<div>对不起，您今天已经签到过了</div>", True, "已签到"),
    ("已签到", "<div>今日已签到</div>", True, "已签到"),
    ("打卡成功", "<div>打卡成功</div>", True, "已签到"),
    ("连续签到天数", "<div>已连续签到 12 天</div>", True, "连续签到 12 天"),
    ("登录页（未签到）", "<form action='takelogin.php'></form>", False, ""),
    ("空页面", "", False, ""),
    ("只有导航的页面", "<div>首页 论坛 种子 个人中心</div>", False, "首页"),
]


def main():
    ok = 0
    for desc, html, want_ok, want_msg in CASES:
        got_ok, got_msg = check_attendance(html)
        cond = got_ok == want_ok and (not want_msg or want_msg in got_msg)
        print(("✅ " if cond else "❌ ") + f"{desc}: {got_ok} / {got_msg[:60]!r}")
        ok += bool(cond)
    print(f"\n{ok}/{len(CASES)} 通过")
    return 0 if ok == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
