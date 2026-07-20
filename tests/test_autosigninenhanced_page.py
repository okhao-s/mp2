import ast
import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = PROJECT_ROOT / "plugins.v2" / "autosigninenhanced" / "__init__.py"
PACKAGE_PATH = PROJECT_ROOT / "package.v2.json"
LOGGER = SimpleNamespace(debug=lambda *args, **kwargs: None)


def _load_page_harness(indexers=None, persisted_sites=None, custom_sites=None):
    module_ast = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"), filename=str(PLUGIN_PATH))
    plugin_class = copy.deepcopy(
        next(
            node
            for node in module_ast.body
            if isinstance(node, ast.ClassDef) and node.name == "AutoSignInEnhanced"
        )
    )
    plugin_class.name = "AutoSignInEnhancedPageHarness"
    plugin_class.bases = []
    plugin_class.keywords = []
    plugin_class.decorator_list = []
    supported_methods = {
        "get_page",
        "_add_site_info",
        "_build_sites_info",
        "_get_site_display_name",
        "_status_meta",
        "_latest_record",
        "_date_label",
        "_calculate_day_stats",
        "_site_sort_key",
        "_create_expansion_panel",
    }
    plugin_class.body = [
        node
        for node in plugin_class.body
        if isinstance(node, ast.FunctionDef) and node.name in supported_methods
    ]

    namespace = {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "Optional": Optional,
        "SiteOper": lambda: SimpleNamespace(
            list_order_by_pri=lambda: persisted_sites or []
        ),
        "SitesHelper": lambda: SimpleNamespace(get_indexers=lambda: indexers or []),
        "datetime": datetime,
        "timedelta": timedelta,
        "logger": LOGGER,
    }
    harness_module = ast.Module(body=[plugin_class], type_ignores=[])
    exec(compile(ast.fix_missing_locations(harness_module), str(PLUGIN_PATH), "exec"), namespace)

    harness = namespace[plugin_class.name]()
    setattr(
        harness,
        "_AutoSignInEnhancedPageHarness__custom_sites",
        lambda: custom_sites or [],
    )
    return harness


def _flatten_nodes(value):
    if isinstance(value, dict):
        yield value
        for item in value.get("content", []):
            yield from _flatten_nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _flatten_nodes(item)


def test_page_helpers_resolve_site_ids_and_keep_unknown_site_fallback():
    harness = _load_page_harness()
    sites_info = {}

    harness._add_site_info(sites_info, 101, "索引站")

    assert sites_info == {101: "索引站", "101": "索引站"}
    assert harness._get_site_display_name("101", sites_info) == "索引站"
    assert harness._get_site_display_name(999, sites_info) == "站点ID: 999"


def test_page_helpers_build_site_info_from_all_history_sources():
    persisted_site = SimpleNamespace(id="persisted-1", name="持久化站点")
    harness = _load_page_harness(
        indexers=[
            {"id": 101, "name": "索引站", "public": False},
            {"id": 102, "name": "公开站", "public": True},
        ],
        persisted_sites=[persisted_site],
        custom_sites=[{"id": "custom-1", "name": "自定义站"}],
    )

    sites_info = harness._build_sites_info()

    assert sites_info[101] == "索引站"
    assert sites_info["101"] == "索引站"
    assert sites_info["persisted-1"] == "持久化站点"
    assert sites_info["custom-1"] == "自定义站"
    assert 102 not in sites_info


def test_page_status_helpers_calculate_today_stats_and_prioritize_errors():
    harness = _load_page_harness()
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    today_label = harness._date_label(today)
    site_data = {
        "成功站": [
            {
                "date": today_label,
                "day_obj": today,
                "status": "签到成功",
                "message": "签到已得100",
            }
        ],
        "重试站": [{"date": today_label, "day_obj": today, "status": "需要重试"}],
        "失败站": [{"date": today_label, "day_obj": today, "status": "签到失败"}],
        "未记录站": [{"date": harness._date_label(yesterday), "day_obj": yesterday, "status": "已签到"}],
    }

    stats = harness._calculate_day_stats(site_data, today_label)
    sorted_names = [
        name
        for name, _ in sorted(
            site_data.items(),
            key=lambda item: harness._site_sort_key(item[0], item[1], [today]),
        )
    ]

    assert stats == {
        "total": 4,
        "recorded": 3,
        "success": 1,
        "warning": 1,
        "error": 1,
        "none": 1,
    }
    assert harness._status_meta("Cookie已失效")["level"] == "error"
    assert harness._latest_record(site_data["成功站"])["date"] == today_label
    assert sorted_names[:2] == ["失败站", "重试站"]


def test_page_keeps_message_panels_and_places_summary_before_them():
    harness = _load_page_harness(
        indexers=[{"id": 101, "name": "索引站", "public": False}],
        persisted_sites=[SimpleNamespace(id="login-1", name="登录站")],
    )
    today = datetime.now().date()
    today_label = f"{today.month}月{today.day}日"
    today_key = today.strftime("%Y-%m-%d")
    history = {
        today_label: [
            {
                "site": "消息站",
                "status": "已签到",
                "message": "连续签到 5 天",
            }
        ],
        f"签到-{today_key}": {"do": [101, 999], "retry": [999]},
        f"登录-{today_key}": {"do": ["login-1"], "retry": []},
    }
    harness._sign_sites = []
    harness._login_sites = []
    harness.get_data = lambda key: history.get(key)

    page = harness.get_page()
    nodes = list(_flatten_nodes(page))
    texts = [node["text"] for node in nodes if isinstance(node.get("text"), str)]
    first_panel_index = next(
        index
        for index, node in enumerate(nodes)
        if node.get("component") == "VExpansionPanels"
    )
    summary_indices = [
        next(index for index, node in enumerate(nodes) if node.get("text") == label)
        for label in ("今日签到", "异常重试", "今日登录", "历史范围")
    ]

    assert "连续签到 5 天" in texts
    assert "站点ID: 999" in texts
    assert f"显示 1 天数据" in texts
    assert sum(node.get("component") == "VExpansionPanels" for node in nodes) == 2
    assert all(index < first_panel_index for index in summary_indices)


def test_page_upgrade_metadata_matches_the_enhanced_summary_release():
    module_ast = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"), filename=str(PLUGIN_PATH))
    plugin_class = next(
        node
        for node in module_ast.body
        if isinstance(node, ast.ClassDef) and node.name == "AutoSignInEnhanced"
    )
    version_assignment = next(
        node
        for node in plugin_class.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "plugin_version" for target in node.targets)
    )
    package_data = json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
    version = "2.9.1-Enhanced-v0.3"

    assert version_assignment.value.value == version
    assert package_data["AutoSignInEnhanced"]["version"] == version
    assert f"v{version}" in package_data["AutoSignInEnhanced"]["history"]
