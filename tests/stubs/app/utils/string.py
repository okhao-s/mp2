class StringUtils:
    @staticmethod
    def get_url_domain(url: str) -> str:
        import re
        m = re.match(r"https?://([^/:]+)", url or "")
        return m.group(1) if m else (url or "")

    @staticmethod
    def url_equal(url1: str, url2: str) -> bool:
        from urllib.parse import urlparse

        def host(u):
            if not u:
                return ""
            if "://" not in u:
                u = "http://" + u
            h = (urlparse(u).hostname or "").lower()
            return h[4:] if h.startswith("www.") else h

        h1, h2 = host(url1), host(url2)
        return bool(h1 and h2 and (h1 == h2 or h1.endswith("." + h2) or h2.endswith("." + h1)))
