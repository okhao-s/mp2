import requests


class RequestUtils:
    """对齐 MoviePilot RequestUtils 的最小子集。"""
    stats = []

    def __init__(self, cookies=None, ua=None, proxies=None, timeout=None, headers=None, referer=None):
        self.cookies = cookies
        self.ua = ua
        self.proxies = proxies
        self.timeout = timeout or 30
        self.headers = headers or {}
        if ua:
            self.headers.setdefault("User-Agent", ua)
        if referer:
            self.headers.setdefault("Referer", referer)

    def get_res(self, url):
        try:
            res = requests.get(url, headers=self.headers, cookies=_parse(self.cookies),
                               proxies=self.proxies, timeout=self.timeout, verify=False)
        except Exception:
            return None
        RequestUtils.stats.append((url, res.status_code, _parse(self.cookies).get("cf_clearance")))
        return res


def _parse(cookie_str):
    out = {}
    for part in (cookie_str or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out
