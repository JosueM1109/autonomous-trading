"""Subprocess tests for risk.py: snapshot, validate, commit, release.

Each test creates an isolated temp directory layout so risk.py's
Path(__file__).resolve().parents[2] resolves to the temp dir and
logs/state.json is scoped to the test. No real logs are touched.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_RISK = REPO_ROOT / "tools" / "stock-trading" / "risk.py"
REAL_CONFIG = REPO_ROOT / "tools" / "stock-trading" / "config.json"


class RiskTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="risk_test_"))
        stage = self.tmp / "tools" / "stock-trading"
        stage.mkdir(parents=True)
        shutil.copyfile(REAL_RISK, stage / "risk.py")
        shutil.copyfile(REAL_CONFIG, stage / "config.json")
        (self.tmp / "logs").mkdir()
        self.risk = stage / "risk.py"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_risk(self, mode, payload):
        proc = subprocess.run(
            ["python3", str(self.risk), f"--{mode}"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        return json.loads(proc.stdout)

    def read_state(self):
        state_path = self.tmp / "logs" / "state.json"
        if not state_path.exists():
            return {}
        with state_path.open() as f:
            return json.load(f)

    @staticmethod
    def buy(
        ticker="NVDA",
        qty=2,
        limit_price=425.0,
        bid=424.80,
        ask=425.20,
        equity=10000,
        cash=5000,
        day_trades=0,
        existing=False,
    ):
        return {
            "date": "2026-04-14",
            "ticker": ticker,
            "side": "buy",
            "qty": qty,
            "limit_price": limit_price,
            "bid": bid,
            "ask": ask,
            "account_equity": equity,
            "account_cash": cash,
            "day_trade_count": day_trades,
            "existing_position": existing,
        }

    @staticmethod
    def sell(**kwargs):
        base = RiskTestCase.buy(**kwargs)
        base["side"] = "sell"
        base["existing_position"] = True
        return base

    @staticmethod
    def ref(ticker="NVDA", side="buy"):
        return {"date": "2026-04-14", "ticker": ticker, "side": side}


class TestValidateHappyPath(RiskTestCase):
    def test_approved_buy_reserves_pending(self):
        result = self.run_risk("validate", self.buy())
        self.assertTrue(result["approved"], result)
        bucket = self.read_state()["2026-04-14"]
        self.assertEqual(bucket["session_deployed"], 850.0)
        self.assertEqual(
            bucket["pending"]["NVDA"], [{"side": "buy", "notional": 850.0}]
        )
        self.assertEqual(bucket["submitted"], {})
        self.assertEqual(bucket["session_deployed_confirmed"], 0.0)

    def test_approved_sell_reserves_no_session_impact(self):
        result = self.run_risk("validate", self.sell())
        self.assertTrue(result["approved"], result)
        bucket = self.read_state()["2026-04-14"]
        self.assertEqual(bucket["session_deployed"], 0.0)
        self.assertIn("NVDA", bucket["pending"])


class TestCommitAndRelease(RiskTestCase):
    def test_commit_promotes_pending(self):
        self.run_risk("validate", self.buy())
        result = self.run_risk("commit", self.ref())
        self.assertTrue(result["ok"], result)
        bucket = self.read_state()["2026-04-14"]
        self.assertEqual(bucket["session_deployed"], 850.0)
        self.assertEqual(bucket["session_deployed_confirmed"], 850.0)
        self.assertEqual(bucket["pending"], {})
        self.assertEqual(bucket["submitted"]["NVDA"], ["buy"])

    def test_release_refunds_session(self):
        self.run_risk("validate", self.buy())
        result = self.run_risk("release", self.ref())
        self.assertTrue(result["ok"], result)
        bucket = self.read_state()["2026-04-14"]
        self.assertEqual(bucket["session_deployed"], 0.0)
        self.assertEqual(bucket["pending"], {})
        self.assertEqual(bucket["submitted"], {})

    def test_validate_release_validate_reapproved(self):
        self.run_risk("validate", self.buy())
        self.run_risk("release", self.ref())
        result = self.run_risk("validate", self.buy())
        self.assertTrue(result["approved"], result)

    def test_commit_without_validate_errors(self):
        result = self.run_risk("commit", self.ref())
        self.assertFalse(result["ok"])
        self.assertIn("no pending reservation", result["error"])

    def test_release_without_validate_errors(self):
        result = self.run_risk("release", self.ref())
        self.assertFalse(result["ok"])
        self.assertIn("no pending reservation", result["error"])


class TestIdempotency(RiskTestCase):
    def test_duplicate_pending_rejected(self):
        self.run_risk("validate", self.buy())
        result = self.run_risk("validate", self.buy())
        self.assertFalse(result["approved"])
        self.assertIn("pending reservation", result["reason"])

    def test_duplicate_after_commit_rejected(self):
        self.run_risk("validate", self.buy())
        self.run_risk("commit", self.ref())
        result = self.run_risk("validate", self.buy())
        self.assertFalse(result["approved"])
        self.assertIn("already submitted", result["reason"])


class TestRiskRules(RiskTestCase):
    def test_under_notional_rejected(self):
        result = self.run_risk("validate", self.buy(qty=1, limit_price=20.0))
        self.assertFalse(result["approved"])
        self.assertIn("notional", result["reason"])

    def test_over_position_cap_rejected(self):
        # max_per_position = 10000 * 0.20 = 2000. 10 * 300 = 3000 > 2000.
        result = self.run_risk(
            "validate", self.buy(qty=10, limit_price=300.0, bid=299.5, ask=300.5)
        )
        self.assertFalse(result["approved"])
        self.assertIn("per-position cap", result["reason"])

    def test_pdt_blocks_opening_buy(self):
        result = self.run_risk(
            "validate", self.buy(day_trades=3, existing=False)
        )
        self.assertFalse(result["approved"])
        self.assertIn("PDT block", result["reason"])

    def test_pdt_does_not_block_closing_sell(self):
        result = self.run_risk("validate", self.sell(day_trades=3))
        self.assertTrue(result["approved"], result)

    def test_spread_too_wide_rejected(self):
        # Paper cap is 1.5%. spread = (428.56 - 420)/424.28 ≈ 2.02%.
        result = self.run_risk("validate", self.buy(bid=420.0, ask=428.56))
        self.assertFalse(result["approved"])
        self.assertIn("spread", result["reason"])

    def test_session_cap_exceeded(self):
        # cap = 5000 * 0.80 = 4000. Reserve 1800, 2000, then try another 400.
        r1 = self.run_risk(
            "validate",
            self.buy(ticker="AAA", qty=2, limit_price=900.0, bid=899.5, ask=900.5),
        )
        self.assertTrue(r1["approved"], r1)
        r2 = self.run_risk(
            "validate",
            self.buy(ticker="BBB", qty=4, limit_price=500.0, bid=499.5, ask=500.5),
        )
        self.assertTrue(r2["approved"], r2)
        r3 = self.run_risk(
            "validate",
            self.buy(ticker="CCC", qty=2, limit_price=200.0, bid=199.5, ask=200.5),
        )
        self.assertFalse(r3["approved"])
        self.assertIn("session cap", r3["reason"])


class TestInputValidation(RiskTestCase):
    def test_invalid_side(self):
        payload = self.buy()
        payload["side"] = "hold"
        result = self.run_risk("validate", payload)
        self.assertFalse(result["approved"])

    def test_qty_zero(self):
        result = self.run_risk("validate", self.buy(qty=0))
        self.assertFalse(result["approved"])
        self.assertIn("qty", result["reason"])

    def test_qty_negative(self):
        result = self.run_risk("validate", self.buy(qty=-1))
        self.assertFalse(result["approved"])

    def test_limit_price_zero(self):
        result = self.run_risk("validate", self.buy(limit_price=0))
        self.assertFalse(result["approved"])
        self.assertIn("limit_price", result["reason"])

    def test_bid_gt_ask(self):
        result = self.run_risk("validate", self.buy(bid=426.0, ask=425.0))
        self.assertFalse(result["approved"])
        self.assertIn("bid", result["reason"])

    def test_negative_bid(self):
        result = self.run_risk("validate", self.buy(bid=-1.0, ask=425.0))
        self.assertFalse(result["approved"])


class TestSnapshot(RiskTestCase):
    @staticmethod
    def account(trading_blocked=False, account_blocked=False, day_trades=0):
        return {
            "date": "2026-04-14",
            "account": {
                "equity": 10000,
                "cash": 5000,
                "day_trade_count": day_trades,
                "trading_blocked": trading_blocked,
                "account_blocked": account_blocked,
                "pattern_day_trader": False,
            },
            "positions": [],
        }

    def test_snapshot_happy_path(self):
        result = self.run_risk("snapshot", self.account())
        self.assertFalse(result["abort"])
        self.assertEqual(result["max_per_position"], 2000.0)
        self.assertEqual(result["max_session_allocation"], 4000.0)

    def test_trading_blocked_aborts(self):
        result = self.run_risk("snapshot", self.account(trading_blocked=True))
        self.assertTrue(result["abort"])
        self.assertIn("trading_blocked", result["abort_reason"])

    def test_account_blocked_aborts(self):
        result = self.run_risk("snapshot", self.account(account_blocked=True))
        self.assertTrue(result["abort"])
        self.assertIn("account_blocked", result["abort_reason"])


if __name__ == "__main__":
    unittest.main()
