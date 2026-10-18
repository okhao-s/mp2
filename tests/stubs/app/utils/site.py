class SiteUtils:
    @staticmethod
    def is_logged_in(html: str) -> bool:
        if not html:
            return False
        low = html.lower()
        if any(m in low for m in ("takelogin.php", "请先登录", "你还没有登录")):
            return False
        return any(m in html for m in ("已签到", "签到已得", "次签到", "魔力值", "个人中心", "logout"))
