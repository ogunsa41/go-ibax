#!/usr/bin/env python3
"""4 farklı videodaki günlük/haftalık bias yöntemlerinin testi.

Her yöntem, t mumu için yalnızca t-1 kapanışına kadar bilinen veriyle +1 (long), -1 (short)
veya 0 (işlem yok) üretir. Ölçülenler:
  - Yön isabeti: t mumunun bias yönünde kapanma oranı (şans seviyesine karşı).
  - Hedef isabeti: long'da t mumunun t-1'in high'ını alması, short'ta low'unu alması
    (videolardaki "önceki mumun high/low'unu hedefle" iddiası). Taban: aynı yönde, sinyalden
    bağımsız olarak tüm mumların önceki mumun ucunu alma oranı.

Yöntemler:
  1. trend_liquidity  — Trend (günlük yapı: yükselen tepe+dip) + son 5 günde trend yönünde
                        likidite alınıp geri kapanış (eski swing, önceki hafta ucu, FVG).
                        Alım yoksa işlem yok.
  2. candle_close_crt — Mum kapanışı: önceki mumun high'ı üstünde kapanış long, low'u altında
                        short; high alınıp içeride kapanış short (CRT), low alınıp içeride
                        kapanış long; diğer durumda mumun rengi. Günlük ve haftalık.
  3. ema_50_200       — 4 saatlikte kapanış > EMA50 > EMA200 long, tersi short, yoksa işlem yok.
  4. liquidity_to_imbalance — Dün önceki günün low'u süpürüldü ya da kapanmamış boğa FVG'sine
                        dokunuldu ve mum üst yarıda kapandı -> long (tersi short).

Kullanım:
    python3 bias_methods.py --list reports/top50_2026-10-27.csv
    python3 bias_methods.py --symbols BTCUSDT ETHUSDT
"""
import argparse
import math
import time
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

import agent
import reversal_agent as ra

N = NormalDist()


# ----------------------------------------------------------------------------- yöntemler
def structure_trend(daily, swing_atr=2.0):
    """Her gün t için, t'den önce onaylanmış son iki tepe ve dibe göre trend:
    +1 yükselen tepe ve dip, -1 alçalan tepe ve dip, 0 karışık."""
    piv = ra.zigzag(daily, swing_atr).sort_values("confirmed")
    out = np.zeros(len(daily), int)
    conf = piv["confirmed"].values
    kinds, prices = piv["kind"].values, piv["price"].values
    for t, d in enumerate(daily.index.values):
        n = np.searchsorted(conf, d, side="left")  # onay günü < t
        hs = prices[:n][kinds[:n] == "high"][-2:]
        ls = prices[:n][kinds[:n] == "low"][-2:]
        if len(hs) == 2 and len(ls) == 2:
            if hs[1] > hs[0] and ls[1] > ls[0]:
                out[t] = 1
            elif hs[1] < hs[0] and ls[1] < ls[0]:
                out[t] = -1
    return pd.Series(out, index=daily.index)


def liquidity_events(daily, swing_atr=2.0):
    """Her gün j için: boğa olayı (destek likiditesi alınıp üstünde kapanış) ve ayı olayı.
    Destek: j'den önce onaylanmış son swing dibi, önceki haftanın low'u ya da kapanmamış boğa FVG."""
    l, h, c = daily["low"], daily["high"], daily["close"]
    piv = ra.zigzag(daily, swing_atr)
    sw_lo = ra.prior_swings(piv, daily.index, "low", k=1)
    sw_hi = ra.prior_swings(piv, daily.index, "high", k=1)
    pwh, pwl, _ = ra.period_levels(daily, "W")
    zones = ra._zone_scan(daily, np.zeros(len(daily)), 365)
    bull = np.zeros(len(daily), bool)
    bear = np.zeros(len(daily), bool)
    for j in range(len(daily)):
        sup = [v[0] for v in (sw_lo[j],) if len(v)] + ([pwl[j]] if pwl[j] == pwl[j] else [])
        res = [v[0] for v in (sw_hi[j],) if len(v)] + ([pwh[j]] if pwh[j] == pwh[j] else [])
        bull[j] = any(l.iloc[j] < s < c.iloc[j] for s in sup) or zones["fvg_low"][j]
        bear[j] = any(c.iloc[j] < r < h.iloc[j] for r in res) or zones["fvg_high"][j]
    # FVG dokunuşunda da geri kapanış şartı: mum ters yönde kapanmamış olsun
    clv = (c - l) / (h - l).replace(0, np.nan)
    bull &= (clv >= 0.5).fillna(False).values
    bear &= (clv <= 0.5).fillna(False).values
    return pd.Series(bull, index=daily.index), pd.Series(bear, index=daily.index)


def trend_liquidity(daily, window=5):
    """Yöntem 1: trend + son `window` günde trend yönünde likidite alımı."""
    trend = structure_trend(daily)
    bull, bear = liquidity_events(daily)
    recent_bull = bull.rolling(window, min_periods=1).max().astype(bool).shift(1, fill_value=False)
    recent_bear = bear.rolling(window, min_periods=1).max().astype(bool).shift(1, fill_value=False)
    sig = np.where((trend == 1) & recent_bull, 1, np.where((trend == -1) & recent_bear, -1, 0))
    return pd.Series(sig, index=daily.index)


def candle_close_crt(bars):
    """Yöntem 2: t-1 mumunun t-2'ye göre kapanışı (günlük ya da haftalık mumlarla)."""
    o, h, l, c = (bars[k] for k in ("open", "high", "low", "close"))
    ph, pl = h.shift(1), l.shift(1)
    took_hi, took_lo = h > ph, l < pl
    sig = np.select(
        [took_hi & took_lo & (c > ph), took_hi & took_lo & (c < pl), took_hi & took_lo,
         took_hi & (c <= ph), took_lo & (c >= pl),
         c > ph, c < pl, c > o, c < o],
        [1, -1, 0, -1, 1, 1, -1, 1, -1], 0)
    case = np.select(
        [took_hi & took_lo, took_hi & (c <= ph), took_lo & (c >= pl), c > ph, c < pl],
        ["dış mum", "CRT: high alındı, içeride kapandı", "CRT: low alındı, içeride kapandı",
         "önceki high üstünde kapanış", "önceki low altında kapanış"], "iç mum (renk)")
    s = pd.Series(sig, index=bars.index).where(ph.notna(), 0).shift(1, fill_value=0).astype(int)
    return s, pd.Series(case, index=bars.index).shift(1)


def ema_50_200(h4, daily_index):
    """Yöntem 3: her günün başında, kapanmış son 4 saatlik muma göre EMA50/200 trendi."""
    c = h4["close"]
    e50, e200 = c.ewm(span=50, adjust=False).mean(), c.ewm(span=200, adjust=False).mean()
    raw = pd.Series(np.where((c > e50) & (e50 > e200), 1, np.where((c < e50) & (e50 < e200), -1, 0)),
                    index=h4.index)
    raw.iloc[:200] = 0
    # 4 saatlik mum t+4s'de kapanır; gün başında (00:00) bilinen son sinyal = kapanışı ≤ 00:00 olan
    closed_at = raw.copy()
    closed_at.index = h4.index + pd.Timedelta(hours=4)
    return closed_at.reindex(daily_index, method="ffill").fillna(0).astype(int)


def liquidity_to_imbalance(daily):
    """Yöntem 4: dün önceki günün ucu süpürüldü ya da FVG'ye dokunuldu ve mum o yönden reddedildi."""
    o, h, l, c = (daily[k] for k in ("open", "high", "low", "close"))
    clv = (c - l) / (h - l).replace(0, np.nan)
    zones = ra._zone_scan(daily, np.zeros(len(daily)), 365)
    bull = (((l < l.shift(1)) & (c > l.shift(1))) | pd.Series(zones["fvg_low"], index=daily.index)) & (clv > 0.5)
    bear = (((h > h.shift(1)) & (c < h.shift(1))) | pd.Series(zones["fvg_high"], index=daily.index)) & (clv < 0.5)
    sig = np.where(bull & ~bear, 1, np.where(bear & ~bull, -1, 0))
    return pd.Series(sig, index=daily.index).shift(1, fill_value=0).astype(int)


# ----------------------------------------------------------------------------- değerlendirme
def evaluate(sig, bars):
    """Sinyalin yön ve hedef isabeti için sayımlar (havuzlanabilir)."""
    o, h, l, c = (bars[k] for k in ("open", "high", "low", "close"))
    up, dn = c > o, c < o
    take_hi, take_lo = h > h.shift(1), l < l.shift(1)
    ok = h.shift(1).notna()
    s = sig.reindex(bars.index).fillna(0)
    L, S = (s == 1) & ok, (s == -1) & ok
    return {
        "bars": int(ok.sum()), "long": int(L.sum()), "short": int(S.sum()),
        "long_dir": int((L & up).sum()), "short_dir": int((S & dn).sum()),
        "long_tgt": int((L & take_hi).sum()), "short_tgt": int((S & take_lo).sum()),
        "up": int((up & ok).sum()), "down": int((dn & ok).sum()),
        "take_hi": int((take_hi & ok).sum()), "take_lo": int((take_lo & ok).sum()),
        # şans seviyesi için sinyal verilen mumlardaki yükseliş sayısı
        "up_in_calls": int(((L | S) & up).sum()),
    }


def summarize(counts):
    k = {key: sum(c[key] for c in counts) for key in counts[0]}
    calls = k["long"] + k["short"]
    hits = k["long_dir"] + k["short_dir"]
    acc = hits / calls if calls else float("nan")
    up_rate = k["up_in_calls"] / calls if calls else float("nan")
    share_long = k["long"] / calls if calls else float("nan")
    chance = share_long * up_rate + (1 - share_long) * (1 - up_rate) if calls else float("nan")
    z = (hits - calls * chance) / math.sqrt(calls * chance * (1 - chance)) if calls and 0 < chance < 1 else 0
    tgt = (k["long_tgt"] + k["short_tgt"]) / calls if calls else float("nan")
    base_hi, base_lo = k["take_hi"] / k["bars"], k["take_lo"] / k["bars"]
    tgt_base = share_long * base_hi + (1 - share_long) * base_lo if calls else float("nan")
    zt = ((k["long_tgt"] + k["short_tgt"]) - calls * tgt_base) / math.sqrt(calls * tgt_base * (1 - tgt_base)) \
        if calls and 0 < tgt_base < 1 else 0
    return {
        "coverage": calls / k["bars"], "calls": calls, "acc": acc, "chance": chance,
        "p_dir": 1 - N.cdf(z),
        "long_acc": k["long_dir"] / k["long"] if k["long"] else float("nan"),
        "short_acc": k["short_dir"] / k["short"] if k["short"] else float("nan"),
        "long_n": k["long"], "short_n": k["short"],
        "tgt": tgt, "tgt_base": tgt_base, "p_tgt": 1 - N.cdf(zt),
        "long_tgt": k["long_tgt"] / k["long"] if k["long"] else float("nan"),
        "short_tgt": k["short_tgt"] / k["short"] if k["short"] else float("nan"),
        "base_hi": base_hi, "base_lo": base_lo,
    }


def split_eval(sig, bars, frac=0.7):
    cut = bars.index[int(len(bars) * frac)]
    return evaluate(sig, bars), evaluate(sig[sig.index < cut], bars[bars.index < cut]), \
        evaluate(sig[sig.index >= cut], bars[bars.index >= cut])


METHODS = [
    ("1. Trend + likidite alındı (günlük)", "daily"),
    ("2. Mum kapanışı / CRT (günlük)", "daily"),
    ("2. Mum kapanışı / CRT (haftalık)", "weekly"),
    ("3. EMA 50/200, 4 saatlik (günlük bias)", "daily"),
    ("4. Likiditeden dengesizliğe (günlük)", "daily"),
]


def run_coin(pair):
    daily = agent.fetch_binance(pair)
    weekly = agent.complete_weeks(agent.to_weekly(daily))
    h4 = agent.fetch_binance(pair, "4h")
    crt_d, case_d = candle_close_crt(daily)
    crt_w, case_w = candle_close_crt(weekly)
    sigs = {
        METHODS[0][0]: (trend_liquidity(daily), daily),
        METHODS[1][0]: (crt_d, daily),
        METHODS[2][0]: (crt_w, weekly),
        METHODS[3][0]: (ema_50_200(h4, daily.index), daily),
        METHODS[4][0]: (liquidity_to_imbalance(daily), daily),
    }
    out = {name: split_eval(s, bars) for name, (s, bars) in sigs.items()}
    # CRT vakalarının ayrı isabeti
    cases = {}
    for tf, s, case, bars in (("günlük", crt_d, case_d, daily), ("haftalık", crt_w, case_w, weekly)):
        for cname in case.dropna().unique():
            m = (case == cname)
            cases[(tf, cname)] = evaluate(s.where(m, 0), bars)
    return out, cases


# ----------------------------------------------------------------------------- rapor
def pct(x, d=1):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"%{x * 100:.{d}f}"


def report(results, cases, symbols, stamp):
    lines = [
        f"# 4 Bias Yönteminin Testi — {stamp}",
        "",
        f"{len(symbols)} coin ({', '.join(symbols[:12])}{'…' if len(symbols) > 12 else ''}), Binance "
        "spot. Her sinyal yalnızca önceki mumun kapanışına kadar bilinen veriyle üretildi.",
        "",
        "**Yön isabeti** = mumun bias yönünde kapanma oranı; **şans** = aynı long/short karışımıyla "
        "rastgele tahminin beklenen isabeti. **Hedef isabeti** = long'da mumun önceki mumun "
        "high'ını, short'ta low'unu alması; **taban** = sinyalden bağımsız olarak her mumun "
        "önceki mumun aynı ucunu alma oranı. p < 0.05 anlamlı.",
        "",
        "| Yöntem | Sinyal verilen mum | Yön isabeti | Şans | p | LONG / SHORT isabeti | "
        "Hedef isabeti | Taban | p | Eğitim → Test yön isabeti | Lift > şans olan coin |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, _ in METHODS:
        per = [r[name] for r in results.values() if name in r]
        a = summarize([p[0] for p in per])
        tr = summarize([p[1] for p in per])
        te = summarize([p[2] for p in per])
        better = sum(1 for p in per if summarize([p[0]])["acc"] > summarize([p[0]])["chance"])
        lines.append(
            f"| {name} | {pct(a['coverage'], 0)} ({a['calls']}) | **{pct(a['acc'])}** | {pct(a['chance'])} | "
            f"{a['p_dir']:.3f} | {pct(a['long_acc'], 0)} ({a['long_n']}) / {pct(a['short_acc'], 0)} ({a['short_n']}) | "
            f"**{pct(a['tgt'])}** | {pct(a['tgt_base'])} | {a['p_tgt']:.3f} | "
            f"{pct(tr['acc'])} → {pct(te['acc'])} | {better}/{len(per)} |")
    lines += ["", "## Mum kapanışı / CRT — vaka bazında", "",
              "| Zaman dilimi | Önceki mum | Mum sayısı | Verilen bias | Yön isabeti | Hedef isabeti | Hedef tabanı |",
              "|---|---|---|---|---|---|---|"]
    keys = sorted({k for c in cases.values() for k in c})
    for k in keys:
        per = [c[k] for c in cases.values() if k in c]
        s = summarize(per)
        side = "LONG" if s["long_n"] >= s["short_n"] else "SHORT"
        lines.append(f"| {k[0]} | {k[1]} | {s['calls']} | {side} | {pct(s['acc'])} | {pct(s['tgt'])} | {pct(s['tgt_base'])} |")
    lines += ["", "> Yön isabeti %50 civarı ve p ≥ 0.05 ise yöntem yönü şanstan iyi bulmuyor demektir. "
              "Hedef isabeti tabandan yüksek değilse 'önceki mumun ucunu hedefler' iddiası veride "
              "doğrulanmıyor demektir. Giriş, stop ve alt zaman dilimi onayı (MSS, FVG girişi) "
              "test edilmedi; yalnızca bias'ın kendisi ölçüldü. Yatırım tavsiyesi değildir."]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="4 bias yönteminin testi")
    ap.add_argument("--symbols", nargs="*", default=[])
    ap.add_argument("--list", help="scan.py'nin yazdığı top N csv")
    ap.add_argument("--out", default=str(Path(__file__).parent / "reports"))
    a = ap.parse_args()
    pairs = list(a.symbols) + (pd.read_csv(a.list)["pair"].tolist() if a.list else [])
    pairs = pairs or ["BTCUSDT"]
    results, cases, used = {}, {}, []
    for pair in pairs:
        try:
            r, c = run_coin(pair)
        except Exception as e:
            print(f"{pair}: {e}")
            continue
        if not r[METHODS[1][0]][0]["bars"] or r[METHODS[1][0]][0]["bars"] < 400:
            print(f"{pair}: yetersiz veri")
            continue
        results[pair], cases[pair] = r, c
        used.append(pair.replace("USDT", ""))
        print(pair, flush=True)
        time.sleep(0.2)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    md = report(results, cases, used, stamp)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"bias_methods_{stamp}.md").write_text(md)
    print("\n" + md)


if __name__ == "__main__":
    main()
