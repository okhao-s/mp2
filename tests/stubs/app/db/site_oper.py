class _Rec:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class SiteOper:
    """记录成功/失败调用，测试断言用。"""
    calls = []

    def list_order_by_pri(self):
        return []

    def success(self, domain=None, seconds=None):
        SiteOper.calls.append(("success", domain, seconds))

    def fail(self, domain=None):
        SiteOper.calls.append(("fail", domain))
