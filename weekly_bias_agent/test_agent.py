"""Ajanın doğruluk testleri: python3 -m unittest -v test_agent"""
import unittest

import numpy as np
import pandas as pd

import agent


def synthetic_daily(weeks=160, seed=0, start="2022-01-03"):
    """Pazartesi başlayan, rastgele yürüyüşlü günlük OHLC verisi."""
    rng = np.random.default_rng(seed)
    n = weeks * 7
    idx = pd.date_range(start, periods=n, freq="D", tz="UTC")
    close = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.03, n)))
    open_ = np.r_[100, close[:-1]]
    hi = np.maximum(open_, close) * (1 + rng.uniform(0, 0.02, n))
    lo = np.minimum(open_, close) * (1 - rng.uniform(0, 0.02, n))
    return pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close,
                         "volume": 1.0}, index=idx)


def weekly_from_rows(rows, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(rows), freq="7D", tz="UTC")
    w = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    w["volume"], w["days"] = 1.0, 7
    return w


class TestWeekly(unittest.TestCase):
    def test_weeks_start_monday_and_aggregate(self):
        d = synthetic_daily(4)
        w = agent.to_weekly(d)
        self.assertTrue((w.index.dayofweek == 0).all())
        first = d.iloc[:7]
        self.assertAlmostEqual(w["open"].iloc[0], first["open"].iloc[0])
        self.assertAlmostEqual(w["close"].iloc[0], first["close"].iloc[-1])
        self.assertAlmostEqual(w["high"].iloc[0], first["high"].max())
        self.assertAlmostEqual(w["low"].iloc[0], first["low"].min())

    def test_complete_weeks_drops_partial_first_and_current(self):
        d = synthetic_daily(10).iloc[3:]  # listeleme Perşembe günü
        now = d.index[-1] - pd.Timedelta(days=2)  # son hafta henüz bitmedi
        w = agent.complete_weeks(agent.to_weekly(d), now)
        self.assertEqual(w["days"].iloc[0], 7)
        self.assertTrue(((w.index + pd.Timedelta(weeks=1)) <= now).all())

    def test_drop_open_candle(self):
        d = synthetic_daily(1)
        now = d.index[-1] + pd.Timedelta(hours=5)
        self.assertEqual(len(agent.drop_open_candle(d, now)), len(d) - 1)


class TestNoLookahead(unittest.TestCase):
    def test_signals_ignore_current_and_future_weeks(self):
        w = agent.complete_weeks(agent.to_weekly(synthetic_daily(120)),
                                 pd.Timestamp("2100-01-01", tz="UTC"))
        sig, _ = agent.bias_rules(w)
        for k in (40, 80, 110):
            tampered = w.copy()
            tampered.iloc[k:, :4] *= np.random.default_rng(k).uniform(0.5, 2, (len(w) - k, 4))
            sig2, _ = agent.bias_rules(tampered)
            # k. haftanın sinyali yalnızca k-1'e kadarki veriye bağlı olmalı
            pd.testing.assert_frame_equal(sig.iloc[:k + 1], sig2.iloc[:k + 1])

    def test_walk_forward_positions_ignore_future(self):
        w = agent.complete_weeks(agent.to_weekly(synthetic_daily(140)),
                                 pd.Timestamp("2100-01-01", tz="UTC"))
        sig, mkt = agent.bias_rules(w)
        pos, chosen, _ = agent.walk_forward(sig, mkt, 0.001, 52, 20)
        k = 100
        tampered = w.copy()
        tampered.iloc[k:, :4] *= 3
        sig2, mkt2 = agent.bias_rules(tampered)
        pos2, chosen2, _ = agent.walk_forward(sig2, mkt2, 0.001, 52, 20)
        pd.testing.assert_series_equal(pos.iloc[:k + 1], pos2.iloc[:k + 1])
        pd.testing.assert_series_equal(chosen.iloc[:k + 1], chosen2.iloc[:k + 1])

    def test_live_signal_matches_backtest(self):
        """Canlı 'bu hafta' sinyali, backtestin o hafta için ürettiği pozisyonla aynı olmalı."""
        for seed in range(3):
            for long_only in (False, True):
                for vol_target in (None, 0.5):
                    w = agent.complete_weeks(agent.to_weekly(synthetic_daily(150, seed)),
                                             pd.Timestamp("2100-01-01", tz="UTC"))
                    sig, mkt = agent.bias_rules(w)
                    if long_only:
                        sig = sig.clip(lower=0)
                    pos, chosen, _ = agent.walk_forward(sig, mkt, 0.001, 52, 20,
                                                        vol_target=vol_target)
                    for k in (90, 120, 149):
                        week, rule, _, signal, size = agent.next_week_signal(
                            w.iloc[:k], 0.001, 20, long_only, vol_target=vol_target)
                        self.assertEqual(week, w.index[k])
                        self.assertEqual(rule, chosen.iloc[k])
                        expected = pos.iloc[k]
                        self.assertAlmostEqual(size if vol_target else signal, expected)


class TestReturns(unittest.TestCase):
    def setUp(self):
        idx = pd.date_range("2024-01-01", periods=4, freq="7D", tz="UTC")
        self.mkt = pd.DataFrame({"ret": [0.10, -0.05, 0.20, 0.50],
                                 "up": [0.12, 0.01, 0.25, 1.20],
                                 "dn": [-0.02, -0.08, -0.01, -0.03]}, index=idx)
        self.idx = idx

    def test_fee_charged_on_position_changes(self):
        pos = pd.Series([1, 1, -1, 0], index=self.idx, dtype=float)
        r = agent.strat_returns(pos, self.mkt.assign(up=0.0, dn=0.0), 0.001)
        np.testing.assert_allclose(r.values, [0.099, -0.05, -0.202, -0.001])

    def test_short_liquidated_when_price_doubles_intraweek(self):
        pos = pd.Series([0, 0, 0, -1], index=self.idx, dtype=float)
        r = agent.strat_returns(pos, self.mkt, 0.0)
        self.assertEqual(r.iloc[-1], -1.0)  # kapanış +%50 ama hafta içi +%120

    def test_half_size_short_survives(self):
        pos = pd.Series([0, 0, 0, -0.5], index=self.idx)
        r = agent.strat_returns(pos, self.mkt, 0.0)
        self.assertAlmostEqual(r.iloc[-1], -0.25)

    def test_no_trading_after_ruin(self):
        r = pd.Series([0.1, -1.0, 0.5, 0.2], index=self.idx)
        self.assertEqual(agent.apply_ruin(r).tolist(), [0.1, -1.0, 0.0, 0.0])
        s = agent.stats(r)
        self.assertTrue(s["ruined"])
        self.assertEqual(s["total_return"], -1.0)
        self.assertEqual(s["max_dd"], -1.0)
        self.assertEqual(s["weeks"], 2)

    def test_vol_scale_uses_only_past_volatility(self):
        sig = pd.Series(1, index=self.idx)
        s1 = agent.vol_scale(sig, self.mkt, 0.5, lookback=2)
        m2 = self.mkt.copy()
        m2.iloc[-1, 0] = 5.0
        s2 = agent.vol_scale(sig, m2, 0.5, lookback=2)
        pd.testing.assert_series_equal(s1, s2)
        self.assertTrue((s1 <= 1).all())


class TestRules(unittest.TestCase):
    def last_signal(self, rows, rule):
        w = weekly_from_rows(rows + [rows[-1]])  # son satır yer tutucu
        sig, _ = agent.bias_rules(w)
        return sig[rule].iloc[-1]

    def test_momentum_and_close_location(self):
        rows = [[100, 101, 99, 100]] * 5 + [[100, 112, 99, 111]]
        self.assertEqual(self.last_signal(rows, "momentum_1w"), 1)
        self.assertEqual(self.last_signal(rows, "close_location"), 1)
        self.assertEqual(self.last_signal(rows, "brooks_trend_bar"), 1)
        self.assertEqual(self.last_signal(rows, "reversal_1w"), -1)

    def test_liquidity_sweep_of_prior_week_low(self):
        rows = [[100, 105, 95, 100]] * 3 + [[100, 103, 92, 101]]
        self.assertEqual(self.last_signal(rows, "liquidity_sweep"), 1)
        rows = [[100, 105, 95, 100]] * 3 + [[100, 108, 97, 99]]
        self.assertEqual(self.last_signal(rows, "liquidity_sweep"), -1)

    def test_outside_week(self):
        rows = [[100, 105, 95, 100]] * 3 + [[100, 110, 90, 108]]
        self.assertEqual(self.last_signal(rows, "outside_week"), 1)

    def test_turtle_soup_needs_old_prior_extreme(self):
        base = [[100, 110, 90, 100]] * 25
        rows = base[:20] + [[100, 110, 85, 100]] + base[:4] + [[100, 104, 80, 95]]
        self.assertEqual(self.last_signal(rows, "turtle_soup_20w"), 1)
        rows_fresh = base + [[100, 104, 85, 95]] + [[100, 104, 84, 95]]
        self.assertEqual(self.last_signal(rows_fresh, "turtle_soup_20w"), 0)

    def test_premium_discount(self):
        rows = [[100, 120, 100, 118]] * 3 + [[118, 119, 99, 101]]
        self.assertEqual(self.last_signal(rows, "premium_discount_4w"), 1)


class TestStatistics(unittest.TestCase):
    def test_psr_increases_with_sharpe(self):
        rng = np.random.default_rng(1)
        noise = pd.Series(rng.normal(0, 0.05, 300))
        self.assertLess(agent.psr(noise - 0.01), 0.5)
        self.assertGreater(agent.psr(noise + 0.02), 0.95)

    def test_deflated_sharpe_penalises_many_trials(self):
        rng = np.random.default_rng(2)
        r = pd.Series(rng.normal(0.006, 0.05, 300))
        few = agent.deflated_sharpe(r, [0.12, 0.0])
        many = agent.deflated_sharpe(r, list(rng.normal(0, 0.06, 50)))
        self.assertLess(many, few)
        self.assertLessEqual(many, agent.psr(r))


if __name__ == "__main__":
    unittest.main()
