#!/usr/bin/env python3
"""Haftalık Bias Ajanı.

1. Crypto.com Exchange'den günlük mumları çeker, haftalık mumlara (Pzt 00:00 UTC) çevirir.
2. Birkaç aday "haftalık bias" kuralını hesaplar (yalnızca geçmiş haftaların verisiyle,
   ileriye bakma yok).
3. Walk-forward: her hafta, o güne kadarki veride en iyi Sharpe'a sahip kuralı seçer ve
   o haftanın bias'ını (LONG / SHORT / NÖTR) belirler.
4. Backtest: haftanın açılışında pozisyon al, kapanışında kapat; komisyon pozisyon
   değişiminde düşülür. Sonuçları buy & hold ile karşılaştırır.
5. Bu haftanın bias'ını ve bir Markdown + HTML rapor üretir.

Kullanım:
    python3 agent.py --symbol BTC_USDT
    python3 agent.py --symbol ETH_USDT --long-only --fee 0.001
"""
import argparse
import json
import math
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

API = "https://api.crypto.com/exchange/v1/public/get-candlestick"
DAY_MS = 86_400_000


# ----------------------------------------------------------------------------- veri
def fetch_daily(symbol, max_pages=20):
    """Günlük mumları geriye doğru sayfalayarak çeker."""
    rows, end = {}, int(time.time() * 1000)
    for _ in range(max_pages):
        url = (f"{API}?instrument_name={symbol}&timeframe=1D&count=300"
               f"&start_ts={end - 300 * DAY_MS}&end_ts={end}")
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.load(r)["result"]["data"]
        if not data:
            break
        for c in data:
            rows[c["t"]] = c
        earliest = min(c["t"] for c in data)
        if len(data) < 2 or earliest >= end:
            break
        end = earliest - 1
    df = pd.DataFrame(rows.values())
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.set_index("t").sort_index()[["o", "h", "l", "c", "v"]].astype(float)
    df.columns = ["open", "high", "low", "close", "volume"]
    return df


def to_weekly(daily):
    w = daily.resample("W-MON", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    w["days"] = daily["close"].resample("W-MON", label="left", closed="left").count()
    return w.dropna()


# ----------------------------------------------------------------------------- kurallar
def bias_rules(w):
    """Her kural, hafta t için t-1 sonuna kadarki veriyle {-1, 0, +1} sinyal üretir."""
    ret = w["close"] / w["open"] - 1
    rng = (w["high"] - w["low"]).replace(0, np.nan)
    clv = (w["close"] - w["low"]) / rng  # kapanışın haftalık aralıktaki yeri (0..1)
    sma10 = w["close"].rolling(10).mean()
    sma20 = w["close"].rolling(20).mean()

    raw = pd.DataFrame(index=w.index)
    raw["momentum_1w"] = np.sign(ret)
    raw["reversal_1w"] = -np.sign(ret)
    raw["momentum_4w"] = np.sign(w["close"] / w["open"].shift(3) - 1)
    raw["close_location"] = np.select([clv > 0.7, clv < 0.3], [1, -1], 0)
    raw["close_location_rev"] = -raw["close_location"]
    # Önceki haftanın high/low'unun dışında kapanış -> devam
    raw["breakout_prev_range"] = np.select(
        [w["close"] > w["high"].shift(1), w["close"] < w["low"].shift(1)], [1, -1], 0)
    raw["trend_sma10"] = np.sign(w["close"] - sma10)
    raw["trend_sma20"] = np.sign(w["close"] - sma20)
    # Trend + kapanış konumu aynı yönü gösteriyorsa (ICT tarzı "güçlü kapanış")
    raw["trend_and_close_loc"] = np.where(raw["trend_sma20"] == raw["close_location"],
                                          raw["close_location"], 0)
    # Bir sonraki haftaya kaydır: t haftasının sinyali t-1 bilgisiyle oluşur
    return raw.shift(1).fillna(0).astype(int), ret


# ----------------------------------------------------------------------------- metrikler
def strat_returns(signal, ret, fee):
    turnover = signal.diff().abs().fillna(signal.abs())
    return signal * ret - turnover * fee


def stats(r, signal=None):
    r = r.dropna()
    if len(r) == 0:
        return {}
    eq = (1 + r).cumprod()
    years = len(r) / 52
    active = r[signal.reindex(r.index) != 0] if signal is not None else r
    sd = r.std()
    return {
        "weeks": len(r),
        "active_weeks": len(active),
        "hit_rate": float((active > 0).mean()) if len(active) else float("nan"),
        "avg_week": float(active.mean()) if len(active) else float("nan"),
        "total_return": float(eq.iloc[-1] - 1),
        "cagr": float(eq.iloc[-1] ** (1 / years) - 1) if years > 0 and eq.iloc[-1] > 0 else float("nan"),
        "sharpe": float(r.mean() / sd * math.sqrt(52)) if sd > 0 else 0.0,
        "max_dd": float((eq / eq.cummax() - 1).min()),
        "t_stat": float(active.mean() / active.std() * math.sqrt(len(active)))
        if len(active) > 2 and active.std() > 0 else 0.0,
    }


def sharpe(r):
    sd = r.std()
    return r.mean() / sd * math.sqrt(52) if sd and sd > 0 else -np.inf


# ----------------------------------------------------------------------------- ajan
def walk_forward(signals, ret, fee, min_train, min_active):
    """Her hafta yalnızca geçmişe bakarak en iyi kuralı seçer."""
    per_rule = {k: strat_returns(signals[k], ret, fee) for k in signals}
    chosen, pos = [], []
    for i in range(len(ret)):
        if i < min_train:
            chosen.append(None)
            pos.append(0)
            continue
        best, best_s = None, 0.0  # Sharpe <= 0 ise bias yok (NÖTR)
        for k, r in per_rule.items():
            past = r.iloc[:i]
            if (signals[k].iloc[:i] != 0).sum() < min_active:
                continue
            s = sharpe(past)
            if s > best_s:
                best, best_s = k, s
        chosen.append(best)
        pos.append(int(signals[best].iloc[i]) if best else 0)
    pos = pd.Series(pos, index=ret.index)
    return pos, pd.Series(chosen, index=ret.index), strat_returns(pos, ret, fee)


def current_bias(w_all, last_complete_idx, fee, min_train, min_active, long_only):
    """Tamamlanan son haftaya kadar olan veriyle bu haftanın bias'ını üretir."""
    sig, ret = bias_rules(w_all)
    if long_only:
        sig = sig.clip(lower=0)
    hist = slice(0, last_complete_idx + 1)
    best, best_s = None, 0.0
    for k in sig:
        r = strat_returns(sig[k].iloc[hist], ret.iloc[hist], fee)
        if (sig[k].iloc[hist] != 0).sum() < min_active:
            continue
        s = sharpe(r)
        if s > best_s:
            best, best_s = k, s
    nxt = last_complete_idx + 1
    if nxt < len(sig):
        signal = int(sig[best].iloc[nxt]) if best else 0
        week = sig.index[nxt]
    else:  # yeni hafta henüz başlamadı
        ext = w_all.iloc[[-1]].copy()
        ext.index = ext.index + pd.Timedelta(weeks=1)
        sig2, _ = bias_rules(pd.concat([w_all, ext]))
        signal = int(sig2[best].iloc[-1]) if best else 0
        week = ext.index[0]
        if long_only:
            signal = max(signal, 0)
    return week, best, best_s, signal


def weekday_profile(daily):
    r = (daily["close"] / daily["open"] - 1).to_frame("ret")
    r["day"] = r.index.day_name()
    g = r.groupby("day")["ret"]
    out = pd.DataFrame({"avg": g.mean(), "hit_rate": g.apply(lambda x: (x > 0).mean()),
                        "t_stat": g.mean() / g.std() * np.sqrt(g.count()), "n": g.count()})
    order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return out.reindex(order)


# ----------------------------------------------------------------------------- rapor
def pct(x):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.2f}%"


def stats_row(name, s):
    return (f"| {name} | {s['active_weeks']}/{s['weeks']} | {pct(s['hit_rate'])} | "
            f"{pct(s['avg_week'])} | {pct(s['total_return'])} | {pct(s['cagr'])} | "
            f"{s['sharpe']:.2f} | {pct(s['max_dd'])} | {s['t_stat']:.2f} |")


HDR = ("| Strateji | Aktif/Toplam hafta | İsabet | Ort. hafta | Toplam getiri | CAGR | "
       "Sharpe | Max DD | t-stat |\n|---|---|---|---|---|---|---|---|---|")


def svg_equity(curves, width=900, height=320):
    colors = ["#2563eb", "#9ca3af", "#16a34a"]
    allv = np.log(pd.concat(curves, axis=1).dropna())
    lo, hi = allv.min().min(), allv.max().max()
    n = len(allv)
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">']
    for (name, _), col in zip(curves.items(), colors):
        ys = allv[name].values
        pts = " ".join(f"{i / (n - 1) * (width - 20) + 10:.1f},"
                       f"{height - 30 - (y - lo) / (hi - lo + 1e-12) * (height - 50):.1f}"
                       for i, y in enumerate(ys))
        parts.append(f'<polyline fill="none" stroke="{col}" stroke-width="2" points="{pts}"/>')
    for j, ((name, _), col) in enumerate(zip(curves.items(), colors)):
        parts.append(f'<text x="{14 + j * 220}" y="{height - 8}" fill="{col}" '
                     f'font-size="13">■ {name}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser(description="Haftalık bias bulucu + backtest ajanı")
    ap.add_argument("--symbol", default="BTC_USDT")
    ap.add_argument("--fee", type=float, default=0.001, help="pozisyon değişimi başına maliyet (0.001=%%0.1)")
    ap.add_argument("--long-only", action="store_true", help="short sinyallerini nötr say")
    ap.add_argument("--min-train", type=int, default=52, help="walk-forward ısınma süresi (hafta)")
    ap.add_argument("--min-active", type=int, default=20, help="kural seçimi için min. aktif hafta")
    ap.add_argument("--out", default=str(Path(__file__).parent / "reports"))
    a = ap.parse_args()

    daily = fetch_daily(a.symbol)
    w_all = to_weekly(daily)
    now = pd.Timestamp.now(tz="UTC")
    complete = w_all[(w_all.index + pd.Timedelta(weeks=1)) <= now]
    complete = complete[complete["days"] == 7]
    w = complete

    sig, ret = bias_rules(w)
    if a.long_only:
        sig = sig.clip(lower=0)

    split = int(len(w) * 0.7)
    rule_tbl = []
    for k in sig:
        r = strat_returns(sig[k], ret, a.fee)
        rule_tbl.append((k, stats(r.iloc[:split], sig[k]), stats(r.iloc[split:], sig[k])))
    rule_tbl.sort(key=lambda x: x[1]["sharpe"], reverse=True)

    pos, chosen, agent_r = walk_forward(sig, ret, a.fee, a.min_train, a.min_active)
    oos = slice(a.min_train, None)
    bh = ret.iloc[oos]
    agent_s = stats(agent_r.iloc[oos], pos)
    bh_s = stats(bh)

    last_idx = w_all.index.get_loc(w.index[-1])
    week, best, best_sh, signal = current_bias(w_all, last_idx, a.fee, a.min_train,
                                               a.min_active, a.long_only)
    label = {1: "LONG (yükseliş) 🟢", -1: "SHORT (düşüş) 🔴", 0: "NÖTR ⚪"}[signal]
    wd = weekday_profile(daily)
    picks = chosen.iloc[oos].value_counts()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d")
    md = [
        f"# Haftalık Bias Raporu — {a.symbol} ({stamp})",
        "",
        f"Veri: {w.index[0].date()} → {w.index[-1].date()} ({len(w)} tamamlanmış hafta), "
        f"maliyet {a.fee * 100:.2f}%/değişim, {'yalnız long' if a.long_only else 'long/short'}.",
        "",
        f"## Bu haftanın bias'ı ({week.date()} haftası)",
        f"**{label}** — seçilen kural: `{best}` (geçmiş Sharpe {best_sh:.2f})" if best else
        f"**{label}** — hiçbir kural geçmişte pozitif Sharpe vermedi, bias yok.",
        "",
        "## Ajanın walk-forward backtesti (örneklem dışı)",
        f"İlk {a.min_train} hafta ısınma; sonraki her hafta kural yalnızca geçmiş veriyle seçildi.",
        "",
        HDR, stats_row("Bias ajanı", agent_s), stats_row("Buy & Hold", bh_s), "",
        "Ajanın seçtiği kurallar (hafta sayısı): " +
        ", ".join(f"`{k}` {v}" for k, v in picks.items()),
        "",
        f"## Aday kurallar — eğitim (ilk %70, {w.index[0].date()}→{w.index[split - 1].date()})",
        HDR, *[stats_row(k, tr) for k, tr, _ in rule_tbl], "",
        f"## Aday kurallar — test (son %30, {w.index[split].date()}→{w.index[-1].date()})",
        HDR, *[stats_row(k, te) for k, _, te in rule_tbl], "",
        "## Gün bazlı profil (günlük açılış→kapanış, UTC)",
        "| Gün | Ort. getiri | İsabet | t-stat | n |\n|---|---|---|---|---|",
        *[f"| {d} | {pct(r.avg)} | {pct(r.hit_rate)} | {r.t_stat:.2f} | {int(r.n)} |"
          for d, r in wd.iterrows()],
        "",
        "> |t-stat| < 2 olan sonuçlar istatistiksel olarak anlamlı sayılmaz. "
        "Geçmiş performans geleceği garanti etmez; yatırım tavsiyesi değildir.",
    ]
    md_text = "\n".join(md)
    (out / f"{a.symbol}_{stamp}.md").write_text(md_text)

    curves = {"Bias ajanı": (1 + agent_r.iloc[oos]).cumprod(),
              "Buy & Hold": (1 + bh).cumprod()}
    html = (f"<!doctype html><meta charset='utf-8'><title>Weekly Bias {a.symbol}</title>"
            "<style>body{font-family:system-ui;max-width:1000px;margin:auto;padding:16px}"
            "table{border-collapse:collapse;font-size:13px}td,th{border:1px solid #ddd;padding:4px 8px}"
            "</style><h2>Equity (log ölçek, örneklem dışı)</h2>" + svg_equity(curves) +
            "<pre style='white-space:pre-wrap'>" + md_text + "</pre>")
    (out / f"{a.symbol}_{stamp}.html").write_text(html)
    pd.DataFrame({"week": w.index, "return": ret.values, "agent_pos": pos.values,
                  "rule": chosen.values, "agent_ret": agent_r.values}).to_csv(
        out / f"{a.symbol}_{stamp}_trades.csv", index=False)

    print(md_text)
    print(f"\nRaporlar: {out}")


if __name__ == "__main__":
    main()
