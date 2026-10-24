#!/usr/bin/env python3
"""定时服务注册（get_service）回归测试。

覆盖三个真实踩过的坑：
1. 留空 cron 的「随机两次」两个服务 id 必须以唯一前缀开头，否则 MoviePilot
   用 ``job_id = f"{pid}_{id}".split("|")[0]`` 去重，后一个会把前一个顶掉；
2. 非法的 5 段 cron 不能让 get_service 返回 None（MP 会 TypeError，整个插件不注册），
   应回退到随机时间；
3. 从 N/9-23 形式切到别的形式后，时间窗必须被清空，否则 sign_in 会按旧窗口拦截。
"""
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


def make_plugin(**overrides):
    plugin = AutoSignInEnhanced()
    plugin._enabled = overrides.get("enabled", True)
    plugin._cron = overrides.get("cron", "")
    plugin._start_time = overrides.get("start", None)
    plugin._end_time = overrides.get("end", None)
    return plugin


def split_prefix(service):
    """复刻 MoviePilot: job_id = f"{pid}_{service['id']}".split("|")[0]"""
    return f"AutoSignInEnhanced_{service['id']}".split("|")[0]


def main():
    # 1) 留空 cron → 两次随机，且 job_id 前缀唯一
    plugin = make_plugin(cron="")
    services = plugin.get_service()
    check("留空 cron 注册 2 个任务", len(services) == 2, f"{len(services)} 个")
    prefixes = [split_prefix(s) for s in services]
    check("两个任务 job_id 前缀唯一", len(set(prefixes)) == len(prefixes), str(prefixes))
    check("随机任务都是 cron 触发器", all(s["trigger"] == "cron" for s in services),
          str([s["trigger"] for s in services]))
    check("随机任务带 hour/minute", all({"hour", "minute"} <= set(s["kwargs"]) for s in services),
          str([s["kwargs"] for s in services]))

    # 2) 标准 5 段 cron
    plugin = make_plugin(cron="5 8 * * *")
    services = plugin.get_service()
    check("5 段 cron 注册 1 个任务", len(services) == 1, f"{len(services)} 个")
    check("5 段 cron 用 CronTrigger", services and hasattr(services[0]["trigger"], "fields"),
          type(services[0]["trigger"]).__name__ if services else "-")

    # 3) 非法 cron → 回退随机，不能返回 None/空
    plugin = make_plugin(cron="99 99 * * *")
    services = plugin.get_service()
    check("非法 cron 回退随机两次", services is not None and len(services) == 2,
          "None" if services is None else f"{len(services)} 个")

    # 4) 周期形式
    plugin = make_plugin(cron="2/9-23")
    services = plugin.get_service()
    check("2/9-23 → interval 2 小时", services and services[0]["trigger"] == "interval"
          and services[0]["kwargs"] == {"hours": 2.0}, str(services))
    check("2/9-23 记录时间窗", (plugin._start_time, plugin._end_time) == (9, 23),
          f"{plugin._start_time}-{plugin._end_time}")

    # 5) 切换配置后时间窗必须被清空
    plugin._cron = "5 8 * * *"
    plugin.get_service()
    check("切换 cron 后时间窗被清空", (plugin._start_time, plugin._end_time) == (None, None),
          f"{plugin._start_time}-{plugin._end_time}")

    # 6) 关闭插件不注册
    plugin = make_plugin(enabled=False, cron="5 8 * * *")
    check("关闭时不注册服务", plugin.get_service() == [])

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
