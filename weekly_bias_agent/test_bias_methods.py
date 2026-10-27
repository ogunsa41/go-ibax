"""4 bias yöntemi testleri: python3 -m unittest -v test_bias_methods"""
import unittest

import numpy as np
import pandas as pd

import bias_methods as bm
from test_agent import synthetic_daily


def bars(rows, freq="D"):
    idx = pd.date_range("2024-01-01", periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


class TestCandleCloseCRT(unittest.TestCase):
    def signal_after(self, prev, cur):
        b = bars([prev, cur, [0, 0, 0, 0]])
        s, case = bm.candle_close_crt(b)
        return s.iloc[2], case.iloc[2]

    def test_close_above_prev_high_is_long(self):
        self.assertEqual(self.signal_after([100, 110, 95, 105], [105, 115, 100, 112])[0], 1)

    def test_high_taken_close_inside_is_short(self):
        s, case = self.signal_after([100, 110, 95, 105], [105, 112, 100, 104])
        self.assertEqual(s, -1)
        self.assertTrue(case.startswith("CRT: high"))

    def test_low_taken_close_inside_is_long(self):
        self.assertEqual(self.signal_after([100, 110, 95, 105], [104, 108, 93, 100])[0], 1)

    def test_inside_bar_uses_color(self):
        self.assertEqual(self.signal_after([100, 110, 95, 105], [104, 108, 97, 101])[0], -1)

    def test_outside_bar_closing_inside_is_neutral(self):
        self.assertEqual(self.signal_after([100, 110, 95, 105], [104, 112, 93, 101])[0], 0)


class TestNoLookahead(unittest.TestCase):
    def check(self, fn):
        d = synthetic_daily(150)
        k = 700
        d2 = d.copy()
        d2.iloc[k:, :4] *= np.random.default_rng(1).uniform(0.6, 1.6, (len(d) - k, 4))
        d2["high"] = d2[["open", "high", "low", "close"]].max(axis=1)
        d2["low"] = d2[["open", "high", "low", "close"]].min(axis=1)
        s1, s2 = fn(d), fn(d2)
        # t günü sinyali yalnızca t-1'e kadarki veriye bağlı olmalı
        pd.testing.assert_series_equal(s1.iloc[:k + 1], s2.iloc[:k + 1])

    def test_trend_liquidity(self):
        self.check(bm.trend_liquidity)

    def test_candle_close(self):
        self.check(lambda d: bm.candle_close_crt(d)[0])

    def test_liquidity_to_imbalance(self):
        self.check(bm.liquidity_to_imbalance)

    def test_ema_uses_only_closed_4h_candles(self):
        idx = pd.date_range("2024-01-01", periods=6 * 300, freq="4h", tz="UTC")
        c = np.r_[np.linspace(100, 200, 1500), np.linspace(200, 50, 300)]
        h4 = pd.DataFrame({"open": c, "high": c, "low": c, "close": c}, index=idx)
        days = pd.date_range("2024-01-01", periods=300, freq="D", tz="UTC")
        s1 = bm.ema_50_200(h4, days)
        # günün içindeki 4 saatlik mumları değiştirmek o günün sinyalini etkilememeli
        h4b = h4.copy()
        h4b.loc[h4b.index >= days[260], "close"] = 1.0
        s2 = bm.ema_50_200(h4b, days)
        pd.testing.assert_series_equal(s1.iloc[:261], s2.iloc[:261])
        self.assertEqual(s1.iloc[240], 1)


class TestEvaluate(unittest.TestCase):
    def test_counts(self):
        b = bars([[100, 110, 90, 105], [105, 112, 100, 110], [110, 111, 95, 98], [98, 99, 90, 92]])
        sig = pd.Series([0, 1, 1, -1], index=b.index)
        e = bm.evaluate(sig, b)
        self.assertEqual((e["long"], e["short"]), (2, 1))
        self.assertEqual(e["long_dir"], 1)    # 2. mum yükseldi, 3. düştü
        self.assertEqual(e["short_dir"], 1)   # 4. mum düştü
        self.assertEqual(e["long_tgt"], 1)    # yalnız 2. mum önceki high'ı aldı
        self.assertEqual(e["short_tgt"], 1)   # 4. mum önceki low'u aldı
        self.assertEqual(e["long_opp"], 1)    # 3. mum (long) önceki low'u aldı
        self.assertEqual(e["short_opp"], 0)   # 4. mum (short) önceki high'ı almadı
        s = bm.summarize([e])
        self.assertAlmostEqual(s["acc"], 2 / 3)
        self.assertAlmostEqual(s["opp"], 1 / 3)


if __name__ == "__main__":
    unittest.main()
