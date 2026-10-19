"""签到契约单测：状态/原因/重试策略/回执脱敏。"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "stubs"))
sys.path.insert(0, os.path.join(HERE, "..", "plugins.v2", "autosigninenhanced"))

import checkin_contract as C


class TestStatusTaxonomy(unittest.TestCase):
    def test_all_statuses_have_labels(self):
        for s in C.ALL_STATUSES:
            self.assertTrue(C.label(s), s)
            self.assertNotEqual(C.label(s), s)

    def test_unknown_status_falls_back(self):
        self.assertEqual(C.label("weird"), "weird")


class TestRetryPolicy(unittest.TestCase):
    def test_retryable(self):
        for c in (C.CAUSE_NETWORK, C.CAUSE_SERVER_ERROR, C.CAUSE_RATE_LIMIT,
                  C.CAUSE_UPSTREAM_UNAVAILABLE, C.CAUSE_MANAGED_CHALLENGE):
            self.assertTrue(C.is_retryable(c), c)

    def test_not_retryable(self):
        for c in (C.CAUSE_LOGIN_REQUIRED, C.CAUSE_NO_EVIDENCE,
                  C.CAUSE_SITE_MAINTENANCE, C.CAUSE_TWO_FACTOR):
            self.assertFalse(C.is_retryable(c), c)

    def test_unknown_outcome_never_replayed(self):
        self.assertIn(C.CAUSE_SUBMISSION_UNKNOWN, C.NEVER_REPLAY_CAUSES)
        self.assertFalse(C.is_retryable(C.CAUSE_SUBMISSION_UNKNOWN))

    def test_status_mapping(self):
        self.assertEqual(C.status_for_cause(C.CAUSE_LOGIN_REQUIRED), C.STATUS_LOGIN)
        self.assertEqual(C.status_for_cause(C.CAUSE_NO_EVIDENCE), C.STATUS_UNKNOWN)
        self.assertEqual(C.status_for_cause(C.CAUSE_NETWORK), C.STATUS_UNREACHABLE)


class TestMessages(unittest.TestCase):
    def test_message_with_detail(self):
        msg = C.message(C.CAUSE_NO_EVIDENCE, "页面文本：abc")
        self.assertIn("签到证据", msg)
        self.assertIn("abc", msg)

    def test_message_unknown_cause(self):
        self.assertEqual(C.message("nope", "细节"), "细节")
        self.assertEqual(C.message("nope"), "未知原因")


class TestRedaction(unittest.TestCase):
    def test_cookie_stripped(self):
        out = C.redact("Cookie: uid=1; pass=abc123; token=xyz", 200)
        self.assertNotIn("abc123", out)
        self.assertNotIn("xyz", out)

    def test_password_stripped(self):
        out = C.redact('{"password": "hunter2", "user": "bob"}')
        self.assertNotIn("hunter2", out)
        self.assertIn("bob", out)

    def test_truncate(self):
        self.assertEqual(len(C.redact("x" * 500, 50)), 50)


class TestReceiptAndLedger(unittest.TestCase):
    def test_receipt_ok(self):
        self.assertTrue(C.Receipt("a", C.STATUS_SIGNED).ok)
        self.assertTrue(C.Receipt("a", C.STATUS_ALREADY).ok)
        self.assertFalse(C.Receipt("a", C.STATUS_UNKNOWN).ok)

    def test_ledger_writes_redacted_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            led = C.Ledger(d)
            path = led.write(C.Receipt("站点A", C.STATUS_UNKNOWN,
                                       cause=C.CAUSE_NO_EVIDENCE,
                                       evidence="Cookie: sid=secret123 未匹配"))
            self.assertTrue(os.path.exists(path))
            line = open(path, encoding="utf-8").read().strip()
            row = json.loads(line)
            self.assertEqual(row["site"], "站点A")
            self.assertEqual(row["status"], C.STATUS_UNKNOWN)
            self.assertEqual(row["cause"], C.CAUSE_NO_EVIDENCE)
            self.assertNotIn("secret123", line)

    def test_ledger_survives_bad_dir(self):
        led = C.Ledger("/proc/definitely/not/writable")
        self.assertEqual(led.write(C.Receipt("a", C.STATUS_SIGNED)), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
