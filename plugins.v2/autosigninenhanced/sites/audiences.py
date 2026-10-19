"""观众（audiences.me）签到适配器。

该站 attendance.php 挂着 Cloudflare Turnstile：页面本身能正常打开，
但要签到必须由浏览器拿到 Turnstile token 填进 `#cf-token` 再提交表单。
纯 HTTP 请求永远拿不到 token，所以这里走 PlaywrightHelper().action()
用真浏览器等 token、提交表单，再回读页面判定结果。
"""
import re
import time
from typing import Tuple

from ruamel.yaml import CommentedMap

from app.helper.browser import PlaywrightHelper
from app.log import logger
from app.plugins.autosigninenhanced.sites import _ISiteSigninHandler
from app.utils.string import StringUtils

# 签到成功的页面特征
_OK_MARKERS = ("签到已得", "本次签到", "获得", "已签到", "签到成功", "爆米花")


class Audiences(_ISiteSigninHandler):
    """观众 Turnstile 签到"""

    site_url = "audiences.me"

    @classmethod
    def match(cls, url: str) -> bool:
        return True if StringUtils.url_equal(url, cls.site_url) else False

    def signin(self, site_info: CommentedMap) -> Tuple[bool, str]:
        site = site_info.get("name") or "观众"
        cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        url = "https://audiences.me/attendance.php"
        # MoviePilot 的 action() 会等 networkidle，15s 默认超时对带 Turnstile 的页太短
        timeout = max(90, int(site_info.get("timeout") or 0))

        html = None
        for headless in (False, True):
            try:
                html = PlaywrightHelper().action(
                    url=url,
                    callback=self._submit_turnstile,
                    cookies=cookie,
                    ua=ua,
                    headless=headless,
                    timeout=timeout,
                )
            except Exception as e:
                logger.warning(f"{site} 浏览器签到异常（headless={headless}）：{e}")
                html = None
            if html and any(m in html for m in _OK_MARKERS):
                break

        if not html:
            return False, "签到失败[浏览器]：页面未打开"

        if "人机验证" in html and not any(m in html for m in _OK_MARKERS):
            return False, "签到失败[人机验证]：Turnstile 未通过"

        for marker in ("签到已得", "本次签到", "获得", "已签到", "签到成功"):
            idx = html.find(marker)
            if idx >= 0:
                snippet = html[max(0, idx - 20):idx + 60]
                snippet = re.sub(r"<[^>]+>", " ", snippet)
                snippet = " ".join(snippet.split())
                return True, f"签到成功：{snippet[:40]}"

        return False, "签到失败[解析]：页面无可确认的签到结果"

    @staticmethod
    def _submit_turnstile(page) -> str:
        """等 Turnstile token 出现 → 提交表单 → 返回最终页面 HTML。"""
        # action() 已经导航过一次；这里再显式进签到页，避免停在首页
        try:
            page.goto("https://audiences.me/attendance.php")
        except Exception:
            pass
        time.sleep(2)
        try:
            html = page.content()
        except Exception:
            return ""
        if any(m in html for m in _OK_MARKERS):
            return html

        token = ""
        for _ in range(25):
            try:
                token = page.evaluate(
                    "() => (document.getElementById('cf-token') || {}).value || ''"
                ) or ""
            except Exception:
                token = ""
            if token:
                break
            time.sleep(1)

        if token:
            try:
                page.evaluate(
                    "() => { const f = document.getElementById('attendance-form');"
                    " if (f) f.submit(); }"
                )
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            time.sleep(2)
        try:
            return page.content()
        except Exception:
            return html
