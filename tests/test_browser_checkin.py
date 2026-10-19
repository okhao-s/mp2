#!/usr/bin/env python3
"""浏览器签到引擎单测：用假 page 模拟「点按钮才签到」「先人机验证后出结果」等场景。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "stubs"))
sys.path.insert(0, os.path.join(HERE, "..", "plugins.v2", "autosigninenhanced"))

import browser_checkin as B  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


class FakePage:
    """最小页面桩：用 stages 描述每次 evaluate 时看到的页面状态。"""

    def __init__(self, stages, advance_on_snapshot=False):
        self.stages = stages
        self.idx = 0
        self.clicked = False
        self.advance_on_snapshot = advance_on_snapshot
        self._snap_count = 0

    def _stage(self):
        return self.stages[min(self.idx, len(self.stages) - 1)]

    def evaluate(self, expr, arg=None):
        if "bodyText" in expr:  # SNAPSHOT_JS
            st = self._stage()
            self._snap_count += 1
            if self.advance_on_snapshot and self._snap_count > 1 and self.idx < len(self.stages) - 1:
                self.idx += 1
                st = self._stage()
            return {"bodyText": st["body"], "hasPassword": st.get("password", False),
                    "challenge": st.get("challenge", False), "title": st.get("title", "PT")}
        if "els.map" in expr:  # FIND_ACTIONS_JS
            st = self._stage()
            return st.get("actions", [])
        if "el.click()" in expr:  # CLICK_JS
            self.clicked = True
            self.idx += 1
            return True
        return None

    def goto(self, url, *a, **kw):
        return None

    def wait_for_load_state(self, *a, **kw):
        return None


def main():
    # 1) 分类：提示文案不是成功
    st = B.classify_page("https://ourbits.club/attendance.php", "签到", "今日还未签到，签到可获得 10 个魔力值")
    check("提示文案分类为 ready", st["status"] == "ready", st["status"])

    # 2) 分类：已签到
    st = B.classify_page("https://x/", "签到", "今日已签到，连续 5 天")
    check("已签到分类正确", st["status"] == "already_signed", st["status"])

    # 3) 分类：登录页
    st = B.classify_page("https://x/login.php", "登录", "", has_password=True)
    check("登录页分类正确", st["status"] == "login_required", st["status"])

    # 4) 动作打分
    check("签到按钮得分最高", B.score_action_text("签到") == 100)
    check("签到记录被排除", B.score_action_text("签到记录") == -1)
    check("无关按钮被排除", B.score_action_text("论坛") == -1)

    # 5) 完整流程：点按钮后才出现回执
    page = FakePage([
        {"body": "今日还未签到，签到可获得 10 个魔力值",
         "actions": [{"index": 3, "text": "签到", "visible": True, "disabled": False,
                      "tag": "A", "href": "https://ourbits.club/attendance.php?action=signin", "formAction": ""}]},
        {"body": "签到成功，获得 10 个魔力值"},
    ])
    out = B.run(page, "https://ourbits.club/attendance.php")
    check("点击签到按钮", out.get("clicked") is True, str(out.get("clicked")))
    check("拿到回执判成功", out["status"] in ("signed", "already_signed"), out["status"])
    check("回执带魔力值", "10" in out.get("evidence", ""), out.get("evidence", "")[:40])

    # 6) 人机验证：等待后自动出结果
    page = FakePage([
        {"body": "请验证您是真人", "challenge": True},
        {"body": "签到已得 20"},
    ], advance_on_snapshot=True)
    out = B.run(page, "https://audiences.me/attendance.php")
    check("人机验证后出回执", out["status"] in ("signed", "already_signed"), out["status"])

    # 6b) 站点自带 Turnstile：页面先显示「等待验证程序」，通过后站点自己提交表单
    page = FakePage([
        {"body": "签到 请耐心等待签到验证程序加载..... 首次签到获得 10 个魔力值。", "challenge": True},
        {"body": "今日已签到，连续签到 3 天"},
    ], advance_on_snapshot=True)
    out = B.run(page, "https://ourbits.club/attendance.php")
    check("Turnstile 自动提交后判已签到", out["status"] in ("signed", "already_signed"), out["status"])
    check("自动提交不需要点击", out.get("clicked") is False, str(out.get("clicked")))

    # 6c) Turnstile 一直加载不出来 → 明确报「验证程序未加载」
    page = FakePage([{"body": "签到 请耐心等待签到验证程序加载.....", "challenge": True}])
    out = B.run(page, "https://ourbits.club/attendance.php")
    check("验证程序未加载给出明确原因", out["status"] == "challenge_pending", out["status"])

    # 7) 登录失效
    page = FakePage([{"body": "请先登录", "password": True}])
    out = B.run(page, "https://x/attendance.php")
    check("登录失效分类", out["status"] == "login_required", out["status"])

    ok = sum(1 for _, c, _ in results if c)
    print(f"\n{ok}/{len(results)} 通过")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
