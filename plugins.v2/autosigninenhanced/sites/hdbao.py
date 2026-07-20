from typing import Tuple
from urllib.parse import urljoin

from ruamel.yaml import CommentedMap

from app.core.config import settings
from app.log import logger
from app.plugins.autosigninenhanced.sites import _ISiteSigninHandler
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class HDBao(_ISiteSigninHandler):
    """
    HDBao签到
    """

    site_url = "hdbao.cc"

    _success_text = "本次签到获得"
    _repeat_text = "已经签到过了"

    @classmethod
    def match(cls, url: str) -> bool:
        return True if StringUtils.url_equal(url, cls.site_url) else False

    def signin(self, site_info: CommentedMap) -> Tuple[bool, str]:
        site = site_info.get("name")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        proxies = settings.PROXY if site_info.get("proxy") else None
        timeout = site_info.get("timeout")
        attendance_url = urljoin(site_info.get("url"), "/attendance.php")

        html_res = RequestUtils(cookies=site_cookie,
                                ua=ua,
                                proxies=proxies,
                                timeout=timeout
                                ).post_res(url=attendance_url, data={})
        if not html_res or html_res.status_code != 200:
            logger.error(f"{site} 签到失败，请检查站点连通性")
            return False, "签到失败，请检查站点连通性"

        if "login.php" in html_res.text:
            logger.error(f"{site} 签到失败，Cookie已失效")
            return False, "签到失败，Cookie已失效"

        if self._success_text in html_res.text:
            logger.info(f"{site} 签到成功")
            return True, "签到成功"
        if self._repeat_text in html_res.text:
            logger.info(f"{site} 今日已签到")
            return True, "今日已签到"

        logger.error(f"{site} 签到失败，签到接口返回 {html_res.text}")
        return False, "签到失败"
