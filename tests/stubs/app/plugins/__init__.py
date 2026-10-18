import os


class _PluginBase:
    def __init__(self):
        self._config = {}
        self._data = {}

    def update_config(self, config=None):
        self._config.update(config or {})

    def get_config(self, key=None):
        return self._config.get(key)

    def save_data(self, key, value):
        self._data[key] = value

    def get_data(self, key=None):
        return self._data.get(key)

    def post_message(self, mtype=None, title=None, text=None, **kw):
        self._messages = getattr(self, "_messages", [])
        self._messages.append({"type": mtype, "title": title, "text": text})

    def get_data_path(self):
        p = os.path.join(os.environ.get("MP_CONFIG_PATH", "/tmp/mp-test-config"), "plugin_data", "autosigninenhanced")
        os.makedirs(p, exist_ok=True)
        return p


# 让 app.plugins.autosigninenhanced 解析到仓库里的真实插件目录
__path__.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "plugins.v2")))
