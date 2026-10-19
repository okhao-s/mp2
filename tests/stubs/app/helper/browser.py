class PlaywrightHelper:
    """默认返回 None（模拟仿真失败），测试可替换。"""
    canned_html = None
    calls = []

    def get_page_source(self, url=None, cookies=None, ua=None, proxies=None, timeout=None):
        PlaywrightHelper.calls.append(url)
        return PlaywrightHelper.canned_html

    def action(self, url=None, callback=None, cookies=None, ua=None, proxies=None,
               headless=None, timeout=None):
        """测试桩：模拟真浏览器，callback 收到一个假 page。"""
        PlaywrightHelper.calls.append(url)
        if not PlaywrightHelper.canned_html or callback is None:
            return None

        class _Page:
            def __init__(self, html):
                self._html = html

            def content(self):
                return self._html

            def evaluate(self, expr, *a, **kw):
                return ""

            def wait_for_load_state(self, *a, **kw):
                return None

        return callback(_Page(PlaywrightHelper.canned_html))
