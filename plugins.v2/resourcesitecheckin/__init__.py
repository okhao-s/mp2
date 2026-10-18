import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType

from .cf_helper import FlareSolverr, is_challenge

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

DEFAULT_SITE = "https://www.jying.top"
MODE_LABEL = {"normal": "普通签到", "luck": "幸运签到"}


class ResourceSiteCheckin(_PluginBase):
    """聚影签到：登录 → 查状态 → 普通/幸运签到 → 通知"""

    plugin_name = "聚影签到"
    plugin_desc = "聚影（jying.top）自动签到，支持普通签到 / 幸运签到、CF 盾兜底、签到状态查询。"
    plugin_icon = "fruit.png"
    plugin_version = "1.3"
    plugin_author = "okhao"
    author_url = "https://github.com/okhao-s/mp2"
    plugin_config_prefix = "resourcesitecheckin_"
    plugin_order = 26
    auth_level = 1

    # 运行时状态
    _enabled: bool = False
    _notify: bool = True
    _cron: str = ""
    _onlyonce: bool = False
    _site_url: str = DEFAULT_SITE
    _username: str = ""
    _password: str = ""
    _token: str = ""
    _checkin_mode: str = "normal"
    _flare_url: str = ""
    _flare_proxy: str = ""
    _proxy: str = ""

    _session: Optional[requests.Session] = None
    _csrf: str = ""
    _ua: str = UA
    _last_result: str = ""

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._cron = config.get("cron") or ""
            self._onlyonce = bool(config.get("onlyonce"))
            self._site_url = (config.get("site_url") or DEFAULT_SITE).rstrip("/")
            self._username = (config.get("username") or "").strip()
            self._password = config.get("password") or ""
            self._token = (config.get("token") or "").strip()
            self._checkin_mode = config.get("checkin_mode") or "normal"
            self._flare_url = (config.get("flare_url") or "").strip()
            self._flare_proxy = (config.get("flare_proxy") or "").strip()
            self._proxy = (config.get("proxy") or "").strip()
            self.__update_config()

        # 用插件数据里缓存的 token（服务端会通过 x-refreshed-token 续期）
        if not self._token:
            self._token = (self.get_data("token") or "").strip() or ""

        if self._onlyonce and self._enabled:
            logger.info("资源站签到：立即执行一次")
            self._onlyonce = False
            self.__update_config()
            self.checkin()

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "site_url": self._site_url,
            "username": self._username,
            "password": self._password,
            "token": self._token,
            "checkin_mode": self._checkin_mode,
            "flare_url": self._flare_url,
            "flare_proxy": self._flare_proxy,
            "proxy": self._proxy,
        })

    # ---------------- 配置界面 ----------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "notify", "label": "发送通知"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即运行一次"}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "site_url", "label": "站点地址",
                                    "placeholder": DEFAULT_SITE}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSelect", "props": {
                                    "model": "checkin_mode", "label": "签到方式",
                                    "items": [
                                        {"title": "普通签到", "value": "normal"},
                                        {"title": "幸运签到（自愿，可能扣分）", "value": "luck"},
                                    ]}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "cron", "label": "签到周期",
                                    "placeholder": "5 位 cron，如 30 8 * * *"}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "username", "label": "用户名 / 邮箱"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "password", "label": "密码", "type": "password"}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VAlert", "props": {
                                    "type": "info", "variant": "tonal",
                                    "text": "账号密码用于自动登录拿 token；也可以只填 token（浏览器 F12 → "
                                            "localStorage/请求头 X-App-User-Token），token 失效时会自动用账号密码重登。"}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "flare_url", "label": "FlareSolverr 地址",
                                    "placeholder": "http://flaresolverr:8191（留空则跳过）"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "flare_proxy", "label": "解盾代理",
                                    "placeholder": "http://ip:port"}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "proxy", "label": "请求代理",
                                    "placeholder": "http://ip:port（可选）"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "token", "label": "手动 Token（可选）",
                                    "placeholder": "留空则用账号密码登录"}}]},
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "site_url": DEFAULT_SITE,
            "username": "",
            "password": "",
            "token": "",
            "checkin_mode": "normal",
            "cron": "30 8 * * *",
            "flare_url": "",
            "flare_proxy": "",
            "proxy": "",
        }

    def get_api(self) -> List[Dict[str, Any]]:
        """插件对外 API（本插件由定时任务驱动，不需要额外接口）"""
        return []

    def get_command(self) -> List[Dict[str, Any]]:
        """插件命令（无）"""
        return []

    def get_page(self) -> List[dict]:
        """数据页：任何异常都不向外抛，否则前端会显示「插件数据加载失败」"""
        try:
            text = self._last_result or (self.get_data("last_result") or "还没有运行记录")
            last_time = self.get_data("last_time") or "-"
        except Exception as e:
            logger.warning(f"资源站签到：读取数据页失败 {e}")
            text, last_time = f"读取插件数据失败：{e}", "-"
        return [{
            "component": "VRow",
            "content": [{
                "component": "VCol", "props": {"cols": 12},
                "content": [{"component": "VAlert", "props": {
                    "type": "info", "variant": "tonal",
                    "title": f"最近一次签到：{last_time}",
                    "text": str(text)}}],
            }],
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron and str(self._cron).strip().count(" ") == 4:
            try:
                return [{
                    "id": "ResourceSiteCheckin",
                    "name": "资源站签到服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.checkin,
                    "kwargs": {},
                }]
            except Exception as e:
                logger.error(f"资源站签到：定时配置错误 {e}")
        return []

    def stop_service(self):
        self._session = None

    # ---------------- HTTP ----------------

    def _http(self, method: str, url: str, **kwargs) -> Optional[requests.Response]:
        if self._session is None:
            self._session = requests.Session()
        headers = kwargs.pop("headers", {}) or {}
        headers.setdefault("User-Agent", self._ua)
        headers.setdefault("X-Requested-With", "XMLHttpRequest")
        if method.upper() == "POST":
            headers.setdefault("Content-Type", "application/json")
            headers.setdefault("Referer", f"{self._base()}/checkin")
            headers.setdefault("Origin", self._base())
        if self._token:
            headers["X-App-User-Token"] = self._token
        if self._csrf:
            headers["X-CSRFToken"] = self._csrf
        proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None
        try:
            return self._session.request(method, url, headers=headers, timeout=30,
                                         proxies=proxies, verify=False, **kwargs)
        except Exception as e:
            logger.warning(f"请求失败 {method} {url}：{e}")
            return None

    def _base(self) -> str:
        return self._site_url.rstrip("/")

    def _bootstrap(self) -> bool:
        """拿 csrftoken，必要时先解盾"""
        url = f"{self._base()}/api/csrf/"
        res = self._http("GET", url)
        if res is not None and res.status_code == 403 and is_challenge(res.text):
            if not self._solve_challenge(url):
                logger.warning("资源站签到：CF 挑战未通过")
                return False
            res = self._http("GET", url)
        if res is None:
            return False
        token = self._session.cookies.get("csrftoken") if self._session else None
        self._csrf = token or ""
        return res.status_code < 500

    def _solve_challenge(self, url: str) -> bool:
        """用 FlareSolverr 解盾，并把 clearance（含 UA）写入会话"""
        if not self._flare_url:
            logger.warning("资源站签到：命中 CF 挑战但未配置 FlareSolverr")
            return False
        logger.warning(f"资源站签到：命中 CF 挑战，尝试 FlareSolverr 解盾 {url}")
        solver = FlareSolverr(self._flare_url, timeout=60)
        sol = solver.solve(url, proxy=self._flare_proxy or None, max_timeout_ms=60000)
        if not sol:
            return False
        for c in sol.get("cookies") or []:
            name, value = c.get("name"), c.get("value")
            if name and value:
                try:
                    self._session.cookies.set(name, value)
                except Exception:
                    pass
        # cf_clearance 与 UA 绑定
        if sol.get("user_agent"):
            self._ua = sol["user_agent"]
        return True

    def _json(self, res: Optional[requests.Response]) -> dict:
        if res is None:
            return {}
        try:
            data = res.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ---------------- 业务 ----------------

    @staticmethod
    def _pick(data: dict, *keys, default=None):
        """在 data / data.data 两层里找第一个存在的键"""
        for scope in (data or {}, (data or {}).get("data") or {}):
            if isinstance(scope, dict):
                for k in keys:
                    if k in scope and scope[k] is not None:
                        return scope[k]
        return default

    def _login(self) -> Tuple[bool, str]:
        if not self._username or not self._password:
            return False, "未配置账号密码，且没有可用的 token"
        res = self._http("POST", f"{self._base()}/api/app/login/",
                         json={"username": self._username, "password": self._password})
        data = self._json(res)
        token = self._pick(data, "token")
        if token:
            self._token = str(token)
            self.save_data("token", self._token)
            return True, "登录成功"
        msg = data.get("message") or (res.status_code if res is not None else "无响应")
        return False, f"登录失败：{msg}"

    def _stats(self) -> dict:
        res = self._http("GET", f"{self._base()}/api/app/checkin/stats/")
        return self._json(res)

    def _do_normal(self) -> Tuple[bool, str]:
        res = self._http("POST", f"{self._base()}/api/app/checkin/do/", json={})
        data = self._json(res)
        if res is not None and res.status_code == 401:
            return False, "TOKEN_EXPIRED"
        if data.get("status") == "error":
            return False, data.get("message") or "签到失败"
        delta = self._pick(data, "delta", "points", "awarded", "gain")
        total = self._pick(data, "total_points", "points_total")
        msg = data.get("message") or "签到成功"
        if delta is not None:
            msg += f"，+{delta} 积分"
        if total is not None:
            msg += f"，当前 {total} 积分"
        return True, msg

    def _do_luck(self) -> Tuple[bool, str]:
        info = self._json(self._http("GET", f"{self._base()}/api/app/checkin/luck/"))
        if info.get("status") == "error":
            return False, info.get("message") or "幸运签到状态获取失败"
        can_draw = self._pick(info, "can_draw")
        reason = self._pick(info, "can_draw_reason") or ""
        if can_draw is False:
            return False, f"幸运签到不可用：{reason or '今日已参与或未开放'}"
        res = self._http("POST", f"{self._base()}/api/app/checkin/luck/draw/", json={})
        data = self._json(res)
        if res is not None and res.status_code == 401:
            return False, "TOKEN_EXPIRED"
        if data.get("status") == "error":
            return False, data.get("message") or "幸运签到失败"
        delta = self._pick(data, "delta", "points", "awarded", "gain")
        total = self._pick(data, "total_points")
        msg = data.get("message") or "幸运签到成功"
        if delta is not None:
            msg += f"，{'+' if float(delta) >= 0 else ''}{delta} 积分"
        if total is not None:
            msg += f"，当前 {total} 积分"
        return True, msg

    def checkin(self, retry: int = 1):
        """执行一次签到"""
        start = time.time()
        mode = self._checkin_mode if self._checkin_mode in MODE_LABEL else "normal"

        if not self._bootstrap():
            return self.__finish(False, "初始化失败：无法访问站点或未过 CF 盾")

        # 没有 token 或 token 失效 → 登录
        if not self._token:
            ok, msg = self._login()
            if not ok:
                return self.__finish(False, msg)

        stats = self._stats()
        if stats.get("status") == "error":
            message = str(stats.get("message") or "")
            if "登录" in message or "token" in message.lower():
                ok, msg = self._login()
                if not ok:
                    return self.__finish(False, msg)
                stats = self._stats()
            if stats.get("status") == "error":
                return self.__finish(False, f"查询签到状态失败：{message or '未知错误'}")

        checked = bool(self._pick(stats, "checked_today", "checked", "is_checked", default=False))
        total = self._pick(stats, "total_points")
        streak = self._pick(stats, "streak", "continuous_days", "streak_days")

        if checked:
            detail = "今日已签到"
            if streak:
                detail += f"，连续 {streak} 天"
            if total is not None:
                detail += f"，当前 {total} 积分"
            return self.__finish(True, detail)

        if mode == "luck":
            ok, msg = self._do_luck()
        else:
            ok, msg = self._do_normal()

        if not ok and msg == "TOKEN_EXPIRED" and retry > 0:
            logger.info("资源站签到：token 失效，重新登录后重试")
            self._token = ""
            ok_login, msg_login = self._login()
            if ok_login:
                return self.checkin(retry=retry - 1)
            return self.__finish(False, msg_login)

        if ok:
            stats2 = self._stats()
            total2 = self._pick(stats2, "total_points")
            if total2 is not None and "当前" not in msg:
                msg += f"，当前 {total2} 积分"
        elapsed = time.time() - start
        return self.__finish(ok, f"{MODE_LABEL[mode]}：{msg}（耗时 {elapsed:.1f}s）")

    def __finish(self, ok: bool, message: str):
        icon = "✅" if ok else "❌"
        text = f"{icon} {message}"
        self._last_result = text
        self.save_data("last_result", text)
        self.save_data("last_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        (logger.info if ok else logger.warning)(f"资源站签到：{message}")
        if self._notify:
            try:
                self.post_message(mtype=NotificationType.Plugin,
                                  title="【资源站签到】", text=text)
            except Exception as e:
                logger.warning(f"资源站签到：通知发送失败 {e}")
        return ok, message
