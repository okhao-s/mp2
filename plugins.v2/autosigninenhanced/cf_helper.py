"""
Cloudflare 盾处理与失败分类 —— 供站点自动签到插件使用。

设计要点（见 skill: cloudflare-shielded-automation）：
- 只认真实挑战页标记，裸词 "cloudflare" 不算（所有 CF 站点页脚都有）
- cf_clearance 与 UA 绑定，缓存时两者一起存
- 失败必须分类：盾未过 / 登录失效 / 站点改版 / 网络超时，否则用户不知道该改 cookie 还是等站点恢复
- FlareSolverr 的 proxy 是逐请求参数，客户端 timeout 必须大于 maxTimeout
"""
import json
import os
import re
import time
from typing import Optional, Tuple

import requests

# 真实挑战页标记（不要加裸词 cloudflare，也不要加 cdn-cgi/challenge-platform：
# 已通过盾的正常页面也会带 /cdn-cgi/challenge-platform/scripts/jsd/main.js）
CF_MARKERS = (
    "<title>just a moment",
    "just a moment...",
    "cf-challenge-running",
    "cf_chl_opt",
    "checking your browser before accessing",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
    "sorry, you have been blocked",
    "ddos protection by cloudflare",
    "cf-error-details",
)

# 登录失效标记
LOGIN_MARKERS = (
    "login.php",
    "takelogin.php",
    "请先登录",
    "请登录后",
    "你还没有登录",
    "登录后才能",
    "not logged in",
    "sign in to continue",
)

KIND_OK = "ok"
KIND_SHIELD = "shield"
KIND_LOGIN = "login"
KIND_LAYOUT = "layout"
KIND_NETWORK = "network"

KIND_LABEL = {
    KIND_SHIELD: "盾未过",
    KIND_LOGIN: "登录失效",
    KIND_LAYOUT: "站点改版",
    KIND_NETWORK: "网络超时",
    KIND_OK: "成功",
}

RETRYABLE = {KIND_SHIELD, KIND_NETWORK}


def is_challenge(html: str) -> bool:
    """判断页面是否为 CF 挑战页。"""
    if not html:
        return False
    low = html.lower()
    return any(m in low for m in CF_MARKERS)


def looks_logged_out(html: str) -> bool:
    if not html:
        return False
    low = html.lower()
    return any(m in low for m in LOGIN_MARKERS)


def classify(html: str, status_code: Optional[int] = None, error: Optional[str] = None) -> str:
    """把一次响应归类到四种失败原因之一（或 ok）。"""
    if error:
        return KIND_NETWORK
    if is_challenge(html):
        return KIND_SHIELD
    if status_code in (401, 403):
        return KIND_LOGIN if looks_logged_out(html or "") else KIND_SHIELD
    if status_code and status_code >= 500:
        return KIND_NETWORK
    if html and looks_logged_out(html) and not _has_signin_text(html):
        return KIND_LOGIN
    if not html:
        return KIND_NETWORK
    return KIND_OK


def _has_signin_text(html: str) -> bool:
    """页面里出现签到结果字样，说明登录态是好的（有些站点页脚也有 login.php 链接）。"""
    return any(k in html for k in ("已签到", "签到已得", "次签到", "签到成功", "本次签到"))


def classify_message(msg: str) -> str:
    """给适配器返回的中文消息归类（适配器不走统一 HTTP 层）。"""
    if not msg:
        return KIND_LAYOUT
    if "Cloudflare" in msg or "盾" in msg:
        return KIND_SHIELD
    if "Cookie" in msg or "登录" in msg:
        return KIND_LOGIN
    if "超时" in msg or "无法打开" in msg or "连接" in msg:
        return KIND_NETWORK
    return KIND_LAYOUT


def fmt_failure(kind: str, detail: str = "") -> str:
    """统一失败文案：签到失败[盾未过]：xxx"""
    label = KIND_LABEL.get(kind, "未知")
    return f"签到失败[{label}]：{detail}" if detail else f"签到失败[{label}]"


class FlareSolverr:
    """FlareSolverr v3 客户端。"""

    def __init__(self, base_url: str, timeout: int = 60):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = int(timeout or 60)

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def solve(self, url: str, cookies: str = "", proxy: Optional[str] = None,
              max_timeout_ms: int = 60000) -> Optional[dict]:
        """
        解盾，返回 {"html","cookies","user_agent","status"}，失败返回 None。
        :param cookies: "k=v; k2=v2" 形式的站点 cookie，会带给浏览器
        :param proxy: 逐请求代理，形如 http://ip:port
        """
        if not self.enabled:
            return None
        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": int(max_timeout_ms),
        }
        if cookies:
            payload["cookies"] = _parse_cookie_string(cookies)
        if proxy:
            payload["proxy"] = {"url": proxy}
        # 客户端超时必须比 maxTimeout 大，否则难盾必然先超时
        read_timeout = max(self.timeout, max_timeout_ms // 1000) + 20
        try:
            resp = requests.post(f"{self.base_url}/v1", json=payload,
                                 timeout=(10, read_timeout), verify=False)
            data = resp.json()
        except Exception:
            return None
        if data.get("status") != "ok":
            return None
        sol = data.get("solution") or {}
        html = sol.get("response") or ""
        return {
            "html": html,
            "cookies": sol.get("cookies") or [],
            "user_agent": sol.get("userAgent") or "",
            "status": sol.get("status"),
        }


def _parse_cookie_string(cookie_str: str) -> list:
    out = []
    for part in (cookie_str or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        out.append({"name": name.strip(), "value": value.strip(), "domain": ""})
    return out


def cookies_to_string(cookies: list, keep_domains: Optional[set] = None) -> str:
    """FlareSolverr 返回的 cookie 列表 → "k=v; k2=v2"。"""
    parts = []
    for c in cookies or []:
        name = c.get("name")
        if not name:
            continue
        if keep_domains is not None:
            dom = (c.get("domain") or "").lstrip(".")
            if dom and not any(dom.endswith(d) for d in keep_domains):
                continue
        parts.append(f"{name}={c.get('value', '')}")
    return "; ".join(parts)


def merge_cookies(base: str, extra: str) -> str:
    """extra 覆盖 base 中的同名项。"""
    merged = {}
    for chunk in (base or "", extra or ""):
        for part in chunk.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                merged[k.strip()] = v.strip()
    return "; ".join(f"{k}={v}" for k, v in merged.items())


class ClearanceStore:
    """按站点缓存 cf_clearance（含 UA，UA 与 clearance 绑定）。"""

    def __init__(self, path: str, ttl: int = 1800):
        self.path = path
        self.ttl = int(ttl or 1800)

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    def get(self, domain: str) -> Optional[dict]:
        rec = self._load().get(domain)
        if not rec:
            return None
        if time.time() > rec.get("expires", 0):
            return None
        return rec

    def set(self, domain: str, cookie: str, ua: str, ttl: Optional[int] = None) -> None:
        data = self._load()
        data[domain] = {
            "cookie": cookie,
            "ua": ua,
            "expires": time.time() + int(ttl or self.ttl),
            "saved_at": int(time.time()),
        }
        self._save(data)

    def invalidate(self, domain: str) -> None:
        data = self._load()
        if domain in data:
            data.pop(domain, None)
            self._save(data)


def extract_clearance(cookies: list) -> str:
    """从 FlareSolverr cookie 列表里只挑 cf_clearance。"""
    for c in cookies or []:
        if c.get("name") == "cf_clearance":
            return f"cf_clearance={c.get('value', '')}"
    return ""


def site_domain(url: str) -> str:
    m = re.match(r"https?://([^/:]+)", url or "")
    return m.group(1).lower() if m else (url or "").lower()
