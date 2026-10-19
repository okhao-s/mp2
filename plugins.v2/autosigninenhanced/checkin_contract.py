"""签到状态契约（移植自 codex-bookmark-checkin-template 的 shared/checkin-contract.json）。

三条原则：
1. **没有回执就不算成功** —— 只有页面上找到明确的签到证据才记 signed；
2. **只重试可重试原因** —— 结果不明（submission_outcome_unknown）绝不自动重放；
3. **状态/原因/文案集中管理** —— 避免各处字符串不一致导致统计和 UI 对不上。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------- 状态
STATUS_SIGNED = "signed"
STATUS_ALREADY = "already_signed"
STATUS_NOT_SIGNED = "not_signed"
STATUS_UNKNOWN = "unknown"
STATUS_LOGIN = "login_required"
STATUS_UNREACHABLE = "unreachable"
STATUS_ATTENTION = "needs_attention"
STATUS_UNAVAILABLE = "not_available"
STATUS_FAILED = "failed"
STATUS_NOT_STARTED = "not_started"

ALL_STATUSES = (
    STATUS_SIGNED, STATUS_ALREADY, STATUS_NOT_SIGNED, STATUS_UNKNOWN,
    STATUS_LOGIN, STATUS_UNREACHABLE, STATUS_ATTENTION, STATUS_UNAVAILABLE,
    STATUS_FAILED, STATUS_NOT_STARTED,
)

STATUS_LABELS = {
    STATUS_SIGNED: "已签到",
    STATUS_ALREADY: "今日已签到",
    STATUS_NOT_SIGNED: "未签到",
    STATUS_UNKNOWN: "结果待核验",
    STATUS_LOGIN: "需登录",
    STATUS_UNREACHABLE: "不可访问",
    STATUS_ATTENTION: "需关注",
    STATUS_UNAVAILABLE: "未开放",
    STATUS_FAILED: "失败",
    STATUS_NOT_STARTED: "未执行（缺少回执）",
}

# ---------------------------------------------------------------- 原因
CAUSE_SUBMISSION_UNKNOWN = "submission_outcome_unknown"
CAUSE_SITE_MAINTENANCE = "site_maintenance"
CAUSE_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
CAUSE_RATE_LIMIT = "rate_limit"
CAUSE_LOGIN_REQUIRED = "login_required"
CAUSE_TWO_FACTOR = "two_factor_required"
CAUSE_INTERACTIVE = "interactive_challenge"
CAUSE_MANAGED_CHALLENGE = "managed_challenge"
CAUSE_NETWORK = "network_error"
CAUSE_NO_EVIDENCE = "authoritative_status_unavailable"
CAUSE_SERVER_ERROR = "site_server_error"
CAUSE_ACCOUNT_MISMATCH = "account_mismatch"
CAUSE_TLS = "tls_certificate_invalid"

FAILURE_MESSAGES = {
    CAUSE_SUBMISSION_UNKNOWN: "提交结果不明，先只读核验，禁止自动重放",
    CAUSE_SITE_MAINTENANCE: "站点公告正在维护或恢复数据，等待恢复后核验",
    CAUSE_UPSTREAM_UNAVAILABLE: "站点服务暂时不可用，等待恢复后有限复核",
    CAUSE_RATE_LIMIT: "站点触发频率限制，等待冷却后重试",
    CAUSE_LOGIN_REQUIRED: "执行会话需要重新登录",
    CAUSE_TWO_FACTOR: "站点要求完成二次验证",
    CAUSE_INTERACTIVE: "站点要求完成浏览器验证",
    CAUSE_MANAGED_CHALLENGE: "站点返回浏览器安全验证页，须先完成验证",
    CAUSE_NETWORK: "页面访问失败，尚未取得签到状态",
    CAUSE_NO_EVIDENCE: "页面尚无可确认的今日签到证据",
    CAUSE_SERVER_ERROR: "站点服务端返回故障，等待恢复后核验",
    CAUSE_ACCOUNT_MISMATCH: "页面账号与执行绑定不一致",
    CAUSE_TLS: "站点 TLS 证书异常，等待站点恢复",
}

# 只有这些原因值得自动重试；结果不明永远不重放
RETRY_CAUSES = frozenset({
    CAUSE_UPSTREAM_UNAVAILABLE,
    CAUSE_RATE_LIMIT,
    CAUSE_NETWORK,
    CAUSE_SERVER_ERROR,
    # 注意：managed_challenge 不再自动重试。浏览器里过一次 Turnstile 要 45s，
    # 过不了再重试 2 次只是白等 200s，结果不会变。
})
NEVER_REPLAY_CAUSES = frozenset({CAUSE_SUBMISSION_UNKNOWN})

# 原因 → 状态
CAUSE_STATUS = {
    CAUSE_SUBMISSION_UNKNOWN: STATUS_UNKNOWN,
    CAUSE_NO_EVIDENCE: STATUS_UNKNOWN,
    CAUSE_SITE_MAINTENANCE: STATUS_ATTENTION,
    CAUSE_UPSTREAM_UNAVAILABLE: STATUS_UNREACHABLE,
    CAUSE_RATE_LIMIT: STATUS_ATTENTION,
    CAUSE_LOGIN_REQUIRED: STATUS_LOGIN,
    CAUSE_TWO_FACTOR: STATUS_ATTENTION,
    CAUSE_INTERACTIVE: STATUS_ATTENTION,
    CAUSE_MANAGED_CHALLENGE: STATUS_ATTENTION,
    CAUSE_NETWORK: STATUS_UNREACHABLE,
    CAUSE_SERVER_ERROR: STATUS_UNREACHABLE,
    CAUSE_ACCOUNT_MISMATCH: STATUS_ATTENTION,
    CAUSE_TLS: STATUS_UNREACHABLE,
}


def label(status: str) -> str:
    return STATUS_LABELS.get(status, status or STATUS_UNKNOWN)


def message(cause: str, detail: str = "") -> str:
    base = FAILURE_MESSAGES.get(cause, "")
    detail = (detail or "").strip()
    if base and detail:
        return f"{base}：{detail}"
    return base or detail or "未知原因"


def is_retryable(cause: str) -> bool:
    return cause in RETRY_CAUSES


def status_for_cause(cause: str) -> str:
    return CAUSE_STATUS.get(cause, STATUS_FAILED)


# ---------------------------------------------------------------- 回执
@dataclass
class Receipt:
    site: str
    status: str
    cause: str = ""
    evidence: str = ""
    url: str = ""
    at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    attempts: int = 1
    duration_ms: int = 0
    source: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_SIGNED, STATUS_ALREADY)

    def to_dict(self) -> dict:
        return asdict(self)


_SENSITIVE = re.compile(
    r"(passwd|passkey|password|pass|token|secret|api_key|apikey|authorization|cookie)"
    r"([\"']?\s*[:=]\s*[\"']?)"
    r"([^\";\s,}]+)",
    re.I,
)


def redact(text: str, limit: int = 300) -> str:
    """日志/回执里脱敏，避免把 Cookie/Token 写进文件。"""
    text = str(text or "")
    text = _SENSITIVE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


class Ledger:
    """每日一份 JSONL 回执账本：<base>/ledger/YYYY-MM-DD.jsonl（自动脱敏）。"""

    def __init__(self, base_dir: str, keep_days: int = 30):
        self.base_dir = os.path.join(str(base_dir), "ledger")
        self.keep_days = keep_days

    def write(self, receipt: Receipt) -> str:
        try:
            os.makedirs(self.base_dir, exist_ok=True)
            path = os.path.join(self.base_dir, f"{datetime.now():%Y-%m-%d}.jsonl")
            data = receipt.to_dict()
            data["evidence"] = redact(data.get("evidence", ""))
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
            self._prune()
            return path
        except Exception:
            return ""

    def _prune(self) -> None:
        try:
            files = sorted(f for f in os.listdir(self.base_dir) if f.endswith(".jsonl"))
            for name in files[:-max(1, self.keep_days)]:
                os.remove(os.path.join(self.base_dir, name))
        except Exception:
            pass
