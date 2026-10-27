#!/usr/bin/env python3
"""TP/SL hassasiyet çalışması: ajanın bias yönüyle TP = rr·R / SL = 1R backtestini
farklı stop mesafeleri (k × haftalık ATR) ve modlar (L/S, yalnız long) için top listedeki
tüm coinlerde çalıştırır.

Kullanım:
    python3 rr_study.py --list reports/top50_2026-10-27.csv
    python3 rr_study.py --list reports/top50_2026-10-27.csv --rr 3 --stops 0.5 1 1.5
"""
import argparse
import time
from pathlib import Path

import pandas as pd

import agent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True, help="scan.py'nin yazdığı top N csv")
    ap.add_argument("--rr", type=float, default=2.0)
    ap.add_argument("--stops", type=float, nargs="+", default=[0.25, 0.5, 0.75, 1.0])
    ap.add_argument("--fee", type=float, default=0.001)
    ap.add_argument("--out", default=str(Path(__file__).parent / "reports" / "rr_study.csv"))
    a = ap.parse_args()

    rows = []
    for c in pd.read_csv(a.list).itertuples():
        try:
            daily = agent.fetch_binance(c.pair)
        except Exception as e:
            print(f"{c.symbol}: {e}")
            continue
        for mode, long_only in (("L/S", False), ("long", True)):
            res = agent.analyze(daily, a.fee, long_only)
            if len(res["w"]) < 52 + 26:
                continue
            direction = res["pos"].iloc[res["oos"]]
            for k in a.stops:
                _, s = agent.rr_backtest(daily, res["w"], direction, k, a.rr, a.fee)
                if s["trades"]:
                    rows.append({"symbol": c.symbol, "mode": mode, "stop_atr": k, **s})
        print(c.symbol, end=" ", flush=True)
        time.sleep(0.2)
    d = pd.DataFrame(rows)
    d.to_csv(a.out, index=False)

    d["sum_r"] = d["avg_r"] * d["trades"]
    g = d.groupby(["mode", "stop_atr"]).agg(
        coins=("symbol", "count"), trades=("trades", "sum"), sum_r=("sum_r", "sum"),
        tp=("tp_rate", "mean"), sl=("sl_rate", "mean"), close=("close_rate", "mean"),
        win=("win_rate", "mean"), positive=("total_r", lambda x: int((x > 0).sum())),
        avg_stop=("avg_stop_pct", "mean"))
    g["avg_r"] = g["sum_r"] / g["trades"]
    print("\n\n" + g.drop(columns="sum_r").round(3).to_string())
    print(f"\nBaşa baş isabet: %{100 / (1 + a.rr):.1f}. Detay: {a.out}")


if __name__ == "__main__":
    main()
