class PlaywrightHelper:
    """默认返回 None（模拟仿真失败），测试可替换。"""
    canned_html = None
    calls = []

    def get_page_source(self, url=None, cookies=None, ua=None, proxies=None, timeout=None):
        PlaywrightHelper.calls.append(url)
        return PlaywrightHelper.canned_html
