#!/usr/bin/env python3
"""记录页单站重试按钮的结构测试。"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "stubs"))
os.environ.setdefault("MP_CONFIG_PATH", "/tmp/mp-test-config")

from app.plugins.autosigninenhanced import AutoSignInEnhanced  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


def walk(node, out):
    if isinstance(node, dict):
        out.append(node)
        for v in node.values():
            walk(v, out)
    elif isinstance(node, list):
        for v in node:
            walk(v, out)


def main():
    panel = AutoSignInEnhanced._create_expansion_panel(
        "我堡", [{"date": "10月19日", "status": "签到成功", "message": "签到成功，获得 10 个魔力值"}],
        "success", "mdi-check", "签到成功", show_retry=True)
    nodes = []
    walk(panel, nodes)
    btns = [n for n in nodes if n.get("component") == "VBtn"]
    check("签到面板有重试按钮", len(btns) == 1, f"{len(btns)} 个按钮")
    if btns:
        api = json.dumps(btns[0], ensure_ascii=False)
        check("按钮调用 retry_site", "retry_site" in api, api[:80])
        check("按钮带上站点名", "site=%E6%88%91%E5%A0%A1" in api or "site=我堡" in api, api[:120])

    # 登录面板不应出现重试按钮
    panel2 = AutoSignInEnhanced._create_expansion_panel(
        "我堡", [{"date": "10月19日", "status": "登录成功", "message": "模拟登录成功"}],
        "success", "mdi-check", "登录成功")
    nodes2 = []
    walk(panel2, nodes2)
    btns2 = [n for n in nodes2 if n.get("component") == "VBtn"]
    check("登录面板没有重试按钮", len(btns2) == 0, f"{len(btns2)} 个")

    ok = sum(1 for _, c, _ in results if c)
    print(f"\n{ok}/{len(results)} 通过")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
