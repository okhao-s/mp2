import json
import os
import sys
import traceback

sys.path.insert(0, "/root/mp-plugins/tests/stubs")
os.environ.setdefault("MP_CONFIG_PATH", "/tmp/mp-test-config")

from app.plugins.resourcesitecheckin import ResourceSiteCheckin

p = ResourceSiteCheckin()
p._enabled = True

for name in ("get_form", "get_page", "get_service", "get_state"):
    try:
        val = getattr(p, name)()
        if name == "get_form":
            form, defaults = val
            print(f"✅ get_form: 区块 {len(form)} 个，默认值键 {sorted(defaults.keys())}")
            json.dumps(form, ensure_ascii=False)   # 确保可 JSON 序列化
            json.dumps(defaults, ensure_ascii=False)
        else:
            print(f"✅ {name}: {json.dumps(val, ensure_ascii=False)[:160]}")
    except Exception as e:
        print(f"❌ {name} 抛异常: {e}")
        traceback.print_exc()

# 模拟已保存过结果的情况
p.save_data("last_result", "✅ 今日已签到，连续 3 天，当前 110 积分")
p.save_data("last_time", "2026-10-18 08:30:00")
try:
    print("✅ 有历史数据时 get_page:", json.dumps(p.get_page(), ensure_ascii=False)[:200])
except Exception as e:
    print("❌ 有历史数据时 get_page 抛异常:", e)
