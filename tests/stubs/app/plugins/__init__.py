import os
from abc import ABCMeta, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


class _PluginBase(metaclass=ABCMeta):
    """
    与 MoviePilot `app.sdk.plugin.base._PluginBase` 对齐的桩：
    抽象方法必须全部实现，否则真实环境会报
    "Can't instantiate abstract class ... without an implementation for abstract method"。
    """

    def __init__(self):
        self._config = {}
        self._data = {}
        self._messages = []

    # ---- 抽象契约（子类必须实现） ----
    @abstractmethod
    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        ...

    @abstractmethod
    def get_state(self) -> bool:
        ...

    @abstractmethod
    def get_api(self) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def get_form(self) -> Tuple[Optional[List[Dict[str, Any]]], Dict[str, Any]]:
        ...

    @abstractmethod
    def get_page(self) -> Optional[List[Dict[str, Any]]]:
        ...

    @abstractmethod
    def stop_service(self) -> None:
        ...

    # ---- 基类提供的公共能力 ----
    def update_config(self, config=None):
        self._config.update(config or {})

    def get_config(self, key=None):
        return self._config.get(key)

    def save_data(self, key, value):
        self._data[key] = value

    def get_data(self, key=None):
        return self._data.get(key)

    def post_message(self, mtype=None, title=None, text=None, **kw):
        self._messages.append({"type": mtype, "title": title, "text": text})

    def get_data_path(self):
        p = os.path.join(os.environ.get("MP_CONFIG_PATH", "/tmp/mp-test-config"),
                         "plugin_data", "autosigninenhanced")
        os.makedirs(p, exist_ok=True)
        return p


# 让 app.plugins.autosigninenhanced / resourcesitecheckin 解析到仓库里的真实插件目录
__path__.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "plugins.v2")))
