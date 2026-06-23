import unittest
import os
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import fund_monitor_core as core


CN_TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 6, 23, 14, 40, tzinfo=CN_TZ)
FUND = {
    "fund_code": "000000",
    "fund_name": "测试基金",
    "etf_market": "sh",
    "etf_code": "510300",
    "total_shares": 10,
    "trigger_pct": 4.0,
}


def quote(current=0.95, previous=1.0, update_time="20260623144000"):
    return {
        "symbol": "sh510300",
        "name": "测试ETF",
        "current_price": current,
        "prev_close": previous,
        "change_pct": (current / previous - 1) * 100,
        "update_time": update_time,
    }


class AnalyzeOneTests(unittest.TestCase):
    @patch("fund_monitor_core.fetch_fund_last_nav")
    @patch("fund_monitor_core.fetch_etf_realtime")
    def test_initial_reference_uses_etf_not_fund_nav(self, mock_quote, mock_nav):
        mock_quote.return_value = quote()
        mock_nav.return_value = {"last_nav": 8.88, "last_nav_date": "2026-06-22"}
        states = {}

        result, changed = core.analyze_one(FUND, states, NOW)

        self.assertTrue(changed)
        self.assertEqual(result["ref_price"], 1.0)
        self.assertAlmostEqual(result["drop_pct"], -5.0)
        self.assertTrue(result["should_buy"])
        self.assertEqual(states["000000"]["total_shares_bought"], 0)
        self.assertEqual(states["000000"]["pending_signal"]["etf_price"], 0.95)

    @patch("fund_monitor_core.fetch_fund_last_nav", return_value={})
    @patch("fund_monitor_core.fetch_etf_realtime")
    def test_position_limit_uses_current_holdings(self, mock_quote, _mock_nav):
        mock_quote.return_value = quote()
        states = {
            "000000": {
                "anchor_etf_price": 1.0,
                "total_shares_bought": 10,
                "total_shares_sold": 2,
            }
        }

        result, _changed = core.analyze_one(FUND, states, NOW)

        self.assertEqual(core.held_shares(result["state"]), 8)
        self.assertTrue(result["should_buy"])
        self.assertEqual(result["state"]["total_shares_bought"], 10)

    @patch("fund_monitor_core.fetch_fund_last_nav", return_value={})
    @patch("fund_monitor_core.fetch_etf_realtime")
    def test_stale_quote_never_triggers(self, mock_quote, _mock_nav):
        mock_quote.return_value = quote(update_time="20260622150000")
        states = {"000000": {"anchor_etf_price": 1.0}}

        result, changed = core.analyze_one(FUND, states, NOW)

        self.assertFalse(changed)
        self.assertFalse(result["should_buy"])
        self.assertIsNotNone(result["skipped_reason"])

    @patch("fund_monitor_core.fetch_fund_last_nav", return_value={})
    @patch("fund_monitor_core.fetch_etf_realtime")
    def test_sell_after_15_percent_then_4_percent_drawdown(self, mock_quote, _mock_nav):
        mock_quote.return_value = quote(current=1.15, previous=1.16)
        states = {
            "000000": {
                "anchor_etf_price": 1.0,
                "buy_history": [{"date": "2026-01-01", "etf_price": 1.0}],
                "total_shares_bought": 1,
                "total_shares_sold": 0,
                "sell_watch_high_etf_price": 1.20,
            }
        }

        result, changed = core.analyze_one(FUND, states, NOW)

        self.assertTrue(changed)
        self.assertTrue(result["should_sell"])
        self.assertFalse(result["should_buy"])
        self.assertAlmostEqual(result["sell_drawdown_pct"], -4.1666666, places=4)
        self.assertTrue(result["state"]["pending_sell_signal"]["id"])

    @patch.dict(
        os.environ,
        {
            "FEEDBACK_BASE_URL": "https://feedback.example.com",
            "FEEDBACK_SIGNING_SECRET": "test-secret",
        },
        clear=False,
    )
    def test_feedback_url_is_only_built_for_pending_signal(self):
        result = {
            "fund_cfg": {**FUND},
            "state": {
                "pending_signal": {
                    "id": "signal-1",
                    "etf_price": 0.95,
                    "drop_pct": -5,
                }
            },
        }
        url = core.build_feedback_url(result)
        self.assertTrue(url.startswith("https://feedback.example.com/feedback?t="))

    @patch.dict(
        os.environ,
        {
            "FEEDBACK_BASE_URL": "https://feedback.example.com",
            "FEEDBACK_SIGNING_SECRET": "test-secret",
        },
        clear=False,
    )
    def test_sell_feedback_url_uses_pending_sell_signal(self):
        result = {
            "should_sell": True,
            "fund_cfg": {**FUND},
            "state": {
                "pending_sell_signal": {
                    "id": "sell-1",
                    "etf_price": 1.15,
                    "drawdown_pct": -4.2,
                    "profit_pct": 17,
                    "suggested_shares": 2,
                }
            },
        }
        url = core.build_feedback_url(result)
        self.assertTrue(url.startswith("https://feedback.example.com/feedback?t="))


if __name__ == "__main__":
    unittest.main()
