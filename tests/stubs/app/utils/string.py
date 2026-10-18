class StringUtils:
    @staticmethod
    def get_url_domain(url: str) -> str:
        import re
        m = re.match(r"https?://([^/:]+)", url or "")
        return m.group(1) if m else (url or "")
