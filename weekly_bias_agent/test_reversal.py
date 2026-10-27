"""Dönüş ajanı testleri: python3 -m unittest -v test_reversal"""
import unittest

import numpy as np
import pandas as pd

import reversal_agent as ra
from test_agent import synthetic_daily


def path_daily(closes, spread=0.5):
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="D", tz="UTC")
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + spread,
                         "low": np.minimum(o, c) - spread, "close": c, "volume": 1.0}, index=idx)


class TestZigzag(unittest.TestCase):
    def test_finds_known_turns(self):
        up = list(np.linspace(100, 150, 40))
        down = list(np.linspace(150, 110, 30))[1:]
        up2 = list(np.linspace(110, 160, 30))[1:]
        d = path_daily(up + down + up2)
        piv = ra.zigzag(d, swing_atr=4)
        # başlangıçtaki en düşük nokta da bir dip olarak işaretlenir
        self.assertEqual(piv["kind"].tolist()[:3], ["low", "high", "low"])
        self.assertEqual(piv["date"].iloc[1], d.index[39])
        self.assertEqual(piv["date"].iloc[2], d.index[68])
        self.assertTrue((piv["confirmed"] > piv["date"]).all())

    def test_alternates_and_confirmation_after_turn(self):
        d = synthetic_daily(200)
        piv = ra.zigzag(d)
        self.assertTrue((piv["kind"] != piv["kind"].shift()).iloc[1:].all())
        self.assertTrue((piv["confirmed"] > piv["date"]).all())
        for r in piv.itertuples():
            col = "high" if r.kind == "high" else "low"
            self.assertEqual(d.loc[r.date, col], r.price)

    def test_past_pivots_ignore_future(self):
        d = synthetic_daily(200)
        piv = ra.zigzag(d)
        k = 900
        d2 = d.copy()
        d2.iloc[k:, :4] *= 1.7
        piv2 = ra.zigzag(d2)
        cut = d.index[k - 1]
        a = piv[piv["confirmed"] <= cut].reset_index(drop=True)
        b = piv2[piv2["confirmed"] <= cut].reset_index(drop=True)
        pd.testing.assert_frame_equal(a, b)


class TestFeatures(unittest.TestCase):
    def test_features_use_only_past_levels(self):
        d = synthetic_daily(150)
        k = 700
        d2 = d.copy()
        d2.iloc[k + 1:, :4] *= 1.5
        lo1, hi1 = ra.features(d, ra.zigzag(d))
        lo2, hi2 = ra.features(d2, ra.zigzag(d2))
        pd.testing.assert_frame_equal(lo1.iloc[:k + 1], lo2.iloc[:k + 1])
        pd.testing.assert_frame_equal(hi1.iloc[:k + 1], hi2.iloc[:k + 1])

    def test_round_step(self):
        self.assertEqual(ra.round_step(63000), 5000)
        self.assertEqual(ra.round_step(3100), 500)
        self.assertAlmostEqual(ra.round_step(0.45), 0.05)


class TestStats(unittest.TestCase):
    def test_enrichment_lift(self):
        idx = pd.date_range("2024-01-01", periods=10, freq="D", tz="UTC")
        f = pd.DataFrame({"x": [1, 1, 0, 0, 0, 0, 0, 0, 0, 0]}, index=idx).astype(bool)
        sw = pd.Series([1, 1, 0, 0, 0, 0, 0, 0, 0, 0], index=idx).astype(bool)
        fwd = pd.Series(0.01, index=idx)
        r = ra.enrichment(f, sw, pd.Series(True, index=idx), fwd).iloc[0]
        self.assertAlmostEqual(r["p_swing"], 1.0)
        self.assertAlmostEqual(r["p_all"], 0.2)
        self.assertAlmostEqual(r["lift"], 5.0)
        self.assertAlmostEqual(r["precision"], 1.0)
        self.assertAlmostEqual(r["base_precision"], 0.2)

    def test_bh_adjust(self):
        q = ra.bh_adjust([0.01, 0.04, 0.03, 0.5])
        np.testing.assert_allclose(q, [0.04, 0.0533333, 0.0533333, 0.5], rtol=1e-4)


if __name__ == "__main__":
    unittest.main()
