"""浏览器签到引擎（移植自 codex-bookmark-checkin-template 的签到逻辑）。

参考项目的签到流程是「浏览器优先」：
    snapshot → classifyPageText → 找签到按钮 → 点击 → 等权威回读 → 出回执

这里把它翻译成 Python，跑在 MoviePilot 的 PlaywrightHelper.action() 回调里，
页面对象只需要 evaluate/content/title 三个能力（CloakBrowser 兼容）。

相比原来的「取一次 HTML 就判定」，这套逻辑能处理：
- 需要点按钮/链接才真正签到的站（NexusPHP 的 onclick=signin）
- 页面先显示 Turnstile 人机验证、验证通过后才出签到结果
- 状态异步刷新（点完要等一会儿页面才变）
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional

# ---------------------------------------------------------------- 动作文本
CHECKIN_EXACT = {
    "签到", "簽到", "立即签到", "立即簽到", "每日签到", "每日簽到",
    "今日签到", "今日簽到", "去签到", "去簽到", "打卡", "立即打卡",
    "福利站", "check in", "check-in", "daily check in", "daily check-in",
    "attendance", "开始转动", "開始轉動", "转动转盘", "轉動轉盤",
    "申请额度", "申請額度", "领取 codex 权益", "領取 codex 權益",
}

EXCLUDED_ACTION = re.compile(
    r"(签到记录|簽到記錄|签到排行|签到规则|补签|補簽|历史|history|rules?)", re.I)

# 页面元素选择器（与参考项目一致）
ACTION_SELECTOR = 'button, a, [role="button"], input[type="button"], input[type="submit"]'
CHALLENGE_SELECTOR = (
    'iframe[src*="captcha" i], iframe[src*="turnstile" i], iframe[src*="challenge" i], '
    '.cf-turnstile, .h-captcha, .g-recaptcha, cap-widget, [data-cap-api-endpoint]'
)

SNAPSHOT_JS = """(sel) => {
  const bodyText = String(document.body ? document.body.innerText : '').slice(0, 30000);
  const pwd = [...document.querySelectorAll('input[type="password"]')].some(el => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  });
  const challenge = [...document.querySelectorAll(sel)].some(el => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  });
  return { bodyText, hasPassword: pwd, challenge, title: document.title || '' };
}"""

FIND_ACTIONS_JS = """(sel) => {
  const els = [...document.querySelectorAll(sel)].slice(0, 400);
  return els.map((el, index) => {
    const st = getComputedStyle(el), r = el.getBoundingClientRect();
    const text = String(el.innerText || el.value || el.getAttribute('aria-label') || el.title || '')
      .replace(/\\s+/g, ' ').trim();
    return {
      index,
      text,
      visible: st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0,
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      tag: el.tagName,
      href: el.href || '',
      formAction: (el.form && el.form.action) || '',
    };
  });
}"""

CLICK_JS = """(args) => {
  const els = [...document.querySelectorAll(args.sel)].slice(0, 400);
  const el = els[args.index];
  if (!el) return false;
  try { el.scrollIntoView({block: 'center'}); } catch (e) {}
  el.click();
  return true;
}"""


def normalize(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


# ---------------------------------------------------------------- 分类
def classify_page(url: str = "", title: str = "", body: str = "",
                  has_password: bool = False, challenge: bool = False) -> Dict[str, str]:
    """页面状态分类，顺序与参考项目 classifyPageText 保持一致。"""
    text = normalize(f"{title}\n{body}")[:30000]
    low_url = str(url or "").lower()

    if re.search(r"(异地登录安全验证|异地登錄安全驗證|忘记二级验证|二级验证代码|二級驗證碼|\b2fa\b)", text, re.I):
        return {"status": "needs_attention", "cause": "two_factor_required", "reason": "站点要求完成二次验证"}

    if has_password or re.search(r"(login\.php|takelogin|/login|signin\.php)", low_url):
        return {"status": "login_required", "cause": "login_required", "reason": "登录状态失效"}

    if re.search(r"(未登录|尚未登录|请先登录|請先登入|not logged in|sign in to continue)", text, re.I):
        return {"status": "login_required", "cause": "login_required", "reason": "页面要求先登录"}

    if re.search(r"(操作过于频繁|请求过于频繁|too many requests|rate limit|try again later|请稍后再试)", text, re.I):
        return {"status": "deferred", "cause": "rate_limit", "reason": "站点触发频率限制"}

    if re.search(r"(维护通知|scheduled maintenance|正在维护|系统维护|维护中|服务暂时不可用|服务暫時不可用)", text, re.I):
        return {"status": "deferred", "cause": "site_maintenance", "reason": "站点维护中"}

    if re.search(r"(connection timed out|bad gateway|service unavailable|502|503|504)", text, re.I):
        return {"status": "deferred", "cause": "upstream_unavailable", "reason": "站点服务器暂时不可用"}

    if re.search(r"(今日已签到|今天已签到|今天已经签到过|已经签到|已完成签到|已签到|已簽到|无需重复签到|already checked[ -]?in)", text, re.I):
        return {"status": "already_signed", "cause": "", "reason": "今天已经签到"}

    if re.search(r"(签到成功|簽到成功|成功签到|打卡成功|本次签到获得|本次簽到獲得|申请额度成功|额度已发放|签到已得)", text, re.I):
        return {"status": "signed", "cause": "", "reason": "页面显示签到成功"}

    if challenge or re.search(r"(请验证您是真人|請驗證您是真人|verify you are human)", text, re.I):
        return {"status": "interactive_challenge", "cause": "interactive_challenge", "reason": "检测到人机验证"}

    if re.search(r"(just a moment|performing security verification|正在验证您是否是真人)", text, re.I):
        return {"status": "managed_challenge", "cause": "managed_challenge", "reason": "等待托管安全验证"}

    return {"status": "ready", "cause": "", "reason": "页面可继续处理"}


# ---------------------------------------------------------------- 动作打分
def score_action_text(text: str) -> int:
    t = normalize(text)
    if not t or len(t) > 24:
        return -1
    if EXCLUDED_ACTION.search(t):
        return -1
    low = t.lower()
    if low in CHECKIN_EXACT or t in CHECKIN_EXACT:
        return 100
    if re.search(r"(签到|簽到|打卡|check[ -]?in|attendance)", t, re.I):
        return 70
    if re.search(r"(领取|領取|申请额度|申請額度)", t, re.I):
        return 50
    return -1


def pick_action(candidates, origin: str = "") -> Optional[dict]:
    """从候选元素里挑出最像「签到」的那一个。"""
    best, best_score = None, -1
    for c in candidates or []:
        if not c.get("visible") or c.get("disabled"):
            continue
        score = score_action_text(c.get("text", ""))
        if score < 0:
            continue
        href = c.get("href") or ""
        if href and origin and href.startswith("http") and origin not in href:
            # 外链只允许明确是签到相关的
            if not re.search(r"(attendance|check[-_]?in|sign|签到|簽到)", href, re.I):
                continue
        if score > best_score:
            best, best_score = c, score
    return best


# ---------------------------------------------------------------- 主流程
def _snapshot(page) -> Dict[str, Any]:
    try:
        return page.evaluate(SNAPSHOT_JS, CHALLENGE_SELECTOR) or {}
    except Exception:
        return {}


def _wait_for_status(page, url, wanted, timeout_s: int) -> Dict[str, str]:
    deadline = time.time() + timeout_s
    last = {}
    while time.time() < deadline:
        snap = _snapshot(page)
        last = classify_page(url, snap.get("title", ""), snap.get("bodyText", ""),
                             snap.get("hasPassword", False), snap.get("challenge", False))
        if last["status"] in wanted:
            return last
        time.sleep(0.8)
    return last


def run(page, url: str) -> Dict[str, Any]:
    """在已打开的页面上跑完整签到流程，返回 {status, reason, evidence, clicked}。"""
    origin = ""
    m = re.match(r"https?://[^/]+", url or "")
    if m:
        origin = m.group(0)

    # action() 只能等 networkidle，对带 Turnstile 的页永远等不到；
    # 所以外面先用 robots.txt 之类的轻量页把浏览器开起来，这里自己导航到签到页。
    try:
        page.goto(url)
    except Exception:
        pass
    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    time.sleep(1.5)

    snap = _snapshot(page)
    state = classify_page(url, snap.get("title", ""), snap.get("bodyText", ""),
                          snap.get("hasPassword", False), snap.get("challenge", False))

    # 已经签过：直接出回执
    if state["status"] in ("signed", "already_signed"):
        return {**state, "evidence": _evidence(snap.get("bodyText", "")), "clicked": False}

    # 人机验证/托管盾：等它自己过（Turnstile 在真浏览器里通常几秒内自动通过）
    if state["status"] in ("interactive_challenge", "managed_challenge"):
        state = _wait_for_status(page, url, ("signed", "already_signed", "ready"), 25)
        if state["status"] in ("signed", "already_signed"):
            return {**state, "evidence": _evidence(_snapshot(page).get("bodyText", "")), "clicked": False}

    if state["status"] == "login_required":
        return {**state, "evidence": state["reason"], "clicked": False}

    # 找签到按钮并点击
    candidates = []
    try:
        candidates = page.evaluate(FIND_ACTIONS_JS, ACTION_SELECTOR) or []
    except Exception:
        candidates = []
    action = pick_action(candidates, origin)

    clicked = False
    if action:
        try:
            clicked = bool(page.evaluate(CLICK_JS, {"sel": ACTION_SELECTOR, "index": action["index"]}))
        except Exception:
            clicked = False
        if clicked:
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            state = _wait_for_status(page, url, ("signed", "already_signed", "needs_attention", "login_required"), 15)

    body = _snapshot(page).get("bodyText", "")
    final = classify_page(url, "", body, False, False)
    # 点击后如果分类没变，以「是否真的出现回执」为准
    if final["status"] in ("signed", "already_signed"):
        state = final
    return {
        "status": state.get("status", "unknown"),
        "reason": state.get("reason", ""),
        "evidence": _evidence(body),
        "clicked": clicked,
    }


def _evidence(body: str, limit: int = 120) -> str:
    """从页面文本里截一段最像回执的话。"""
    text = normalize(body)
    for pat in (r"签到已得\s*\d+", r"本次签到获得\s*\d+", r"签到成功[，,、\s]*获得\s*\d+",
                r"获得\s*\d+\s*个?魔力值", r"今日已签到", r"已经签到", r"签到成功"):
        m = re.search(pat, text)
        if m:
            start = max(0, m.start() - 20)
            return text[start:m.end() + 20][:limit]
    return text[:limit]
