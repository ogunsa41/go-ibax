#!/usr/bin/env python3
"""Dönüş Noktası Ajanı.

Fiyatın nerelerden döndüğünü inceler ve dönüş noktalarında ortak olan özellikleri arar.

1. Dönüşler: günlük grafikte ATR tabanlı zigzag ile belirgin swing dip/tepeleri bulur
   (fiyat o noktadan en az `swing_atr` × ATR geri dönmüşse dönüş sayılır).
2. Özellikler: her gün için, dip tarafı (günün low'u) ve tepe tarafı (günün high'ı) ayrı
   ayrı, seviyelere değme (önceki gün/hafta/ay uçları, açılışlar, eski swing seviyeleri,
   yuvarlak sayılar, ortalamalar, Fibonacci) ve durum özellikleri (RSI, hacim, fitil,
   likidite süpürme, ardışık mumlar, haftanın günü) hesaplanır. Seviyeler yalnızca o günden
   önce bilinen verilerden gelir.
3. Karşılaştırma: bir özelliğin dönüş günlerindeki sıklığı tüm günlerdeki sıklığıyla
   kıyaslanır (lift). Binom testi + Benjamini-Hochberg düzeltmesi, coinler arası tutarlılık
   ve dönemler arası (ilk %70 / son %30) tekrar kontrol edilir.

Kullanım:
    python3 reversal_agent.py --symbols BTCUSDT ETHUSDT
    python3 reversal_agent.py --list reports/top50_2026-10-27.csv
"""
import argparse
import math
import time
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

import agent

N = NormalDist()
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ----------------------------------------------------------------------------- dönüşler
def atr(daily, length=14):
    pc = daily["close"].shift(1)
    tr = pd.concat([daily["high"] - daily["low"], (daily["high"] - pc).abs(),
                    (daily["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(length).mean()


def zigzag(daily, swing_atr=4.0, atr_len=14):
    """ATR tabanlı zigzag. Bir tepe, fiyat ondan `swing_atr` × ATR (tepe günündeki ATR)
    aşağı inince onaylanır; dip için tersi. Tepe ve dipler sırayla birbirini izler.

    Dönen DataFrame: date (dönüş günü), kind ('high'/'low'), price, confirmed (onay günü).
    """
    a = atr(daily, atr_len).values
    hi, lo, idx = daily["high"].values, daily["low"].values, daily.index
    piv, trend = [], 0  # 0: belirsiz, 1: yükseliş (tepe aranıyor), -1: düşüş (dip aranıyor)
    start = atr_len - 1
    hi_i = lo_i = start
    for i in range(start, len(daily)):
        if trend >= 0 and hi[i] > hi[hi_i]:
            hi_i = i
        if trend <= 0 and lo[i] < lo[lo_i]:
            lo_i = i
        if trend >= 0 and hi_i < i and lo[i] <= hi[hi_i] - swing_atr * a[hi_i]:
            piv.append((idx[hi_i], "high", hi[hi_i], idx[i]))
            trend = -1
            lo_i = hi_i + 1 + int(np.argmin(lo[hi_i + 1:i + 1]))
        elif trend <= 0 and lo_i < i and hi[i] >= lo[lo_i] + swing_atr * a[lo_i]:
            piv.append((idx[lo_i], "low", lo[lo_i], idx[i]))
            trend = 1
            hi_i = lo_i + 1 + int(np.argmax(hi[lo_i + 1:i + 1]))
    return pd.DataFrame(piv, columns=["date", "kind", "price", "confirmed"])


# ----------------------------------------------------------------------------- özellikler
def rsi(close, length=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / length, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / length, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def period_levels(daily, freq):
    """Önceki dönemin (gün/hafta/ay) high/low'u ve bu dönemin açılışı, günlük indekste."""
    rule = {"W": "W-MON", "M": "MS"}[freq]
    g = daily.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min"})
    naive = daily.index.tz_localize(None)
    key = (naive.to_period("W-SUN") if freq == "W" else naive.to_period("M")).start_time \
        .tz_localize("UTC")
    prev = g.shift(1)
    return (prev["high"].reindex(key).values, prev["low"].reindex(key).values,
            g["open"].reindex(key).values)


def round_step(price):
    """Yuvarlak sayı adımı: fiyatın basamağının yarısı (60 000 → 5 000, 3 000 → 500)."""
    return 10 ** np.floor(np.log10(price)) / 2


def prior_swings(piv, dates, kind, k=3):
    """Her gün için, o günden ÖNCE onaylanmış son k swing seviyesinin listesi."""
    p = piv[piv["kind"] == kind].sort_values("confirmed")
    conf, price = p["confirmed"].values, p["price"].values
    out = []
    for d in dates.values:
        n = np.searchsorted(conf, d, side="left")  # onay günü < d
        out.append(price[max(0, n - k):n])
    return out


def last_leg(piv, dates):
    """Her gün için o günden önce onaylanmış son tam bacak (dip→tepe veya tepe→dip)."""
    p = piv.sort_values("confirmed").reset_index(drop=True)
    conf = p["confirmed"].values
    legs = []
    for d in dates.values:
        n = np.searchsorted(conf, d, side="left")
        if n >= 2:
            a, b = p.iloc[n - 2], p.iloc[n - 1]
            legs.append((a["price"], b["price"], b["kind"]))
        else:
            legs.append(None)
    return legs


def features(daily, piv, tol_atr=0.25):
    """Her gün için dip ve tepe tarafı özellik matrisleri (bool DataFrame'ler)."""
    o, h, l, c, v = (daily[k] for k in ("open", "high", "low", "close", "volume"))
    a = atr(daily).shift(1)  # tolerans için dünkü ATR
    tol = tol_atr * a
    rng = (h - l).replace(0, np.nan)

    pdh, pdl = h.shift(1), l.shift(1)
    pwh, pwl, wopen = period_levels(daily, "W")
    pmh, pml, mopen = period_levels(daily, "M")
    sma = {n: c.rolling(n).mean().shift(1) for n in (20, 50, 200)}
    sma20w = c.rolling(140).mean().shift(1)
    r = rsi(c)
    vol_ratio = v / v.rolling(20).mean().shift(1)
    step = round_step(c.shift(1))

    sw_hi = prior_swings(piv, daily.index, "high")
    sw_lo = prior_swings(piv, daily.index, "low")
    legs = last_leg(piv, daily.index)

    def near(x, level):
        return (np.abs(x - level) <= tol).fillna(False)

    def near_any(x, levels_per_day):
        return pd.Series([bool(len(L)) and bool(np.any(np.abs(L - xi) <= ti))
                          if ti == ti else False
                          for xi, L, ti in zip(x.values, levels_per_day, tol.values)],
                         index=x.index)

    def fib_zone(x, lo_r, hi_r, side):
        out = []
        for xi, leg, ti in zip(x.values, legs, tol.values):
            if leg is None or ti != ti:
                out.append(False)
                continue
            p0, p1, kind = leg
            # dip tarafı: son bacak yükseliş (dip→tepe), geri çekilme seviyesi = tepe - r·boy
            if side == "low" and kind == "high":
                top, size = p1, p1 - p0
                a1, a2 = top - hi_r * size, top - lo_r * size
            elif side == "high" and kind == "low":
                bot, size = p1, p0 - p1
                a1, a2 = bot + lo_r * size, bot + hi_r * size
            else:
                out.append(False)
                continue
            out.append(a1 - ti <= xi <= a2 + ti)
        return pd.Series(out, index=x.index)

    def build(side):
        x = l if side == "low" else h
        s = 1 if side == "low" else -1  # dip tarafında aşağı aşırılık arıyoruz
        f = pd.DataFrame(index=daily.index)
        f["önceki gün ucu"] = near(x, pdl if side == "low" else pdh)
        f["önceki hafta ucu"] = near(x, pd.Series(pwl if side == "low" else pwh, index=daily.index))
        f["önceki ay ucu"] = near(x, pd.Series(pml if side == "low" else pmh, index=daily.index))
        f["haftalık açılış"] = near(x, pd.Series(wopen, index=daily.index))
        f["aylık açılış"] = near(x, pd.Series(mopen, index=daily.index))
        f["eski swing (aynı yön)"] = near_any(x, sw_lo if side == "low" else sw_hi)
        f["eski swing (ters yön, polarite)"] = near_any(x, sw_hi if side == "low" else sw_lo)
        f["yuvarlak sayı"] = (np.abs(x - (x / step).round() * step) <= tol).fillna(False)
        for n in (20, 50, 200):
            f[f"SMA{n}"] = near(x, sma[n])
        f["20 haftalık SMA"] = near(x, sma20w)
        f["Fib 0.382–0.5"] = fib_zone(x, 0.382, 0.5, side)
        f["Fib 0.5–0.618"] = fib_zone(x, 0.5, 0.618, side)
        f["Fib 0.618–0.786 (golden pocket)"] = fib_zone(x, 0.618, 0.786, side)
        f["RSI aşırı (<30 / >70)"] = (r < 30) if side == "low" else (r > 70)
        f["SMA20'den ≥ 2 ATR uzak"] = (s * (sma[20] - x) >= 2 * a).fillna(False)
        f["hacim ≥ 2× ortalama"] = (vol_ratio >= 2).fillna(False)
        wick = ((np.minimum(o, c) - l) if side == "low" else (h - np.maximum(o, c))) / rng
        f["uzun fitil (≥ %50)"] = (wick >= 0.5).fillna(False)
        f["geniş mum (≥ 1.5 ATR)"] = (rng >= 1.5 * a).fillna(False)
        ref = pd.Series(pwl if side == "low" else pwh, index=daily.index)
        f["önceki hafta ucunu süpürüp geri kapanış"] = (
            ((l < ref) & (c > ref)) if side == "low" else ((h > ref) & (c < ref))).fillna(False)
        sweep_sw = [bool(len(L)) and ((li < L.min() and ci > L.min()) if side == "low"
                                      else (hi_ > L.max() and ci < L.max()))
                    for li, hi_, ci, L in zip(l.values, h.values, c.values,
                                              sw_lo if side == "low" else sw_hi)]
        f["eski swing'i süpürüp geri kapanış"] = pd.Series(sweep_sw, index=daily.index)
        down = (c < o) if side == "low" else (c > o)
        f["öncesinde ≥ 3 ardışık ters mum"] = (down.shift(1) & down.shift(2) & down.shift(3)).fillna(False)
        for i, dname in enumerate(DAYS):
            f[f"gün: {dname}"] = daily.index.dayofweek == i
        return f.astype(bool)

    return build("low"), build("high")


# ----------------------------------------------------------------------------- istatistik
def bh_adjust(p):
    p = np.asarray(p, dtype=float)
    n = len(p)
    order = np.argsort(p)
    ranked = p[order] * n / np.arange(1, n + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.clip(ranked, 0, 1)
    return out


def enrichment(fmat, is_swing, valid, fwd):
    """Her özellik için: dönüşlerdeki sıklık, tüm günlerdeki sıklık, lift, p-değeri, isabet
    ve özellik görülen günden sonraki getiri (dönüş yönünde işaretli; zigzag'dan bağımsız)."""
    f = fmat[valid]
    sw = is_swing[valid]
    fw = fwd[valid]
    fw_ok = fw.notna()
    n_sw = int(sw.sum())
    rows = []
    for col in f.columns:
        x = f[col]
        p0 = x.mean()
        k = int(x[sw].sum())
        p1 = k / n_sw if n_sw else np.nan
        if n_sw and 0 < p0 < 1:
            z = (k - n_sw * p0) / math.sqrt(n_sw * p0 * (1 - p0))
            pv = 2 * (1 - N.cdf(abs(z)))
        else:
            pv = 1.0
        rows.append({"feature": col, "swing_n": n_sw, "swing_hits": k, "p_swing": p1,
                     "p_all": p0, "lift": p1 / p0 if p0 > 0 else np.nan, "p_value": pv,
                     "precision": float(sw[x].mean()) if x.any() else np.nan,
                     "base_precision": float(sw.mean()),
                     "fwd_n": int((x & fw_ok).sum()),
                     "fwd_mean": float(fw[x & fw_ok].mean()) if (x & fw_ok).any() else np.nan,
                     "fwd_hit": float((fw[x & fw_ok] > 0).mean()) if (x & fw_ok).any() else np.nan,
                     "base_fwd_mean": float(fw[fw_ok].mean()),
                     "base_fwd_hit": float((fw[fw_ok] > 0).mean())})
    return pd.DataFrame(rows)


def analyze_coin(daily, swing_atr=4.0, split=0.7, horizon=10):
    piv = zigzag(daily, swing_atr)
    f_low, f_high = features(daily, piv)
    valid = pd.Series(daily.index >= daily.index[min(200, len(daily) - 1)], index=daily.index)
    cut = daily.index[int(len(daily) * split)]
    fwd = daily["close"].shift(-horizon) / daily["close"] - 1
    out = {}
    for side, fmat in (("low", f_low), ("high", f_high)):
        sw = pd.Series(daily.index.isin(piv.loc[piv["kind"] == side, "date"]), index=daily.index)
        sfwd = fwd if side == "low" else -fwd
        res = enrichment(fmat, sw, valid, sfwd)
        tr = enrichment(fmat, sw, valid & (daily.index < cut), sfwd)
        te = enrichment(fmat, sw, valid & (daily.index >= cut), sfwd)
        res["lift_train"], res["lift_test"] = tr["lift"].values, te["lift"].values
        res["swing_hits_train"], res["swing_n_train"] = tr["swing_hits"].values, tr["swing_n"].values
        res["swing_hits_test"], res["swing_n_test"] = te["swing_hits"].values, te["swing_n"].values
        res["p_all_train"], res["p_all_test"] = tr["p_all"].values, te["p_all"].values
        res["valid_days"] = int(valid.sum())
        res["side"] = side
        out[side] = res
    return piv, out


def swing_profile(daily, piv):
    """Dönüşlerin tanımlayıcı istatistikleri: salınım boyu, süre, geri çekilme derinliği, RSI."""
    p = piv.reset_index(drop=True)
    if len(p) < 3:
        return {}
    move = (p["price"] / p["price"].shift(1) - 1).abs()
    days = (p["date"] - p["date"].shift(1)).dt.days
    retr = (p["price"] - p["price"].shift(1)).abs() / (p["price"].shift(1) - p["price"].shift(2)).abs()
    r = rsi(daily["close"]).reindex(p["date"]).values
    return {"swings": len(p), "median_move": float(move.median()),
            "median_days": float(days.median()), "median_retrace": float(retr.median()),
            "retrace_hist": retr.dropna(), "rsi_low": r[p["kind"].values == "low"],
            "rsi_high": r[p["kind"].values == "high"]}


# ----------------------------------------------------------------------------- havuz
def pooled_table(results):
    """Coin sonuçlarını özellik × taraf bazında birleştirir."""
    rows = []
    allres = pd.concat([res for sides in results.values() for res in sides.values()])
    for (side, feat), g in allres.groupby(["side", "feature"], sort=False):
        n_sw, k = g["swing_n"].sum(), g["swing_hits"].sum()
        # tüm günlerdeki beklenen isabet: her coinin kendi baz oranıyla
        exp = (g["swing_n"] * g["p_all"]).sum()
        var = (g["swing_n"] * g["p_all"] * (1 - g["p_all"])).sum()
        z = (k - exp) / math.sqrt(var) if var > 0 else 0
        e_tr = (g["swing_n_train"] * g["p_all_train"]).sum()
        e_te = (g["swing_n_test"] * g["p_all_test"]).sum()
        rows.append({
            "side": side, "feature": feat, "coins": len(g), "swings": int(n_sw),
            "p_swing": k / n_sw if n_sw else np.nan, "p_all": exp / n_sw if n_sw else np.nan,
            "lift": k / exp if exp > 0 else np.nan, "z": z, "p_value": 2 * (1 - N.cdf(abs(z))),
            "coins_lift_gt1": int((g["lift"] > 1).sum()),
            "lift_train": g["swing_hits_train"].sum() / e_tr if e_tr > 0 else np.nan,
            "lift_test": g["swing_hits_test"].sum() / e_te if e_te > 0 else np.nan,
            "precision": float(np.nanmean(g["precision"])),
            "base_precision": float(np.nanmean(g["base_precision"])),
            "fwd_n": int(g["fwd_n"].sum()),
            "fwd_mean": float(np.average(g["fwd_mean"].fillna(0), weights=g["fwd_n"]))
            if g["fwd_n"].sum() else np.nan,
            "fwd_hit": float(np.average(g["fwd_hit"].fillna(0), weights=g["fwd_n"]))
            if g["fwd_n"].sum() else np.nan,
            "base_fwd_mean": float(g["base_fwd_mean"].mean()),
            "base_fwd_hit": float(g["base_fwd_hit"].mean()),
            "coins_fwd_better": int((g["fwd_mean"] > g["base_fwd_mean"]).sum()),
        })
    t = pd.DataFrame(rows)
    t["q_value"] = bh_adjust(t["p_value"].values)
    return t


# ----------------------------------------------------------------------------- rapor
def pct(x, d=1):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"%{x * 100:.{d}f}"


def feature_rows(t):
    lines = ["| Özellik | Dönüşlerde | Tüm günlerde | Lift | Eğitim lift | Test lift | "
             "Lift>1 olan coin | q | İsabet (özellik varken dönüş) | Sonraki 10 gün (dönüş yönünde) |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in t.itertuples():
        mark = " ✅" if r.q_value < 0.05 and r.lift_train > 1 and r.lift_test > 1 else \
            (" ❌" if r.q_value < 0.05 and r.lift < 1 else "")
        lines.append(
            f"| {r.feature}{mark} | {pct(r.p_swing)} | {pct(r.p_all)} | {r.lift:.2f}× | "
            f"{r.lift_train:.2f}× | {r.lift_test:.2f}× | {r.coins_lift_gt1}/{r.coins} | "
            f"{r.q_value:.3f} | {pct(r.precision)} (taban {pct(r.base_precision)}) | "
            f"ort. {pct(r.fwd_mean)}, %{r.fwd_hit * 100:.0f} pozitif (taban {pct(r.base_fwd_mean)}, "
            f"%{r.base_fwd_hit * 100:.0f}); {r.coins_fwd_better}/{r.coins} coinde tabandan iyi |")
    return lines


def report(t, profiles, symbols, swing_atr, stamp):
    sw_lo = t[t.side == "low"]["swings"].max()
    sw_hi = t[t.side == "high"]["swings"].max()
    robust = t[(t.q_value < 0.05) & (t.lift > 1) & (t.lift_train > 1) & (t.lift_test > 1)]
    allp = [p for p in profiles.values() if p]
    retr = pd.concat([p["retrace_hist"] for p in allp]) if allp else pd.Series(dtype=float)
    rsi_lo = np.concatenate([p["rsi_low"] for p in allp]) if allp else np.array([])
    rsi_hi = np.concatenate([p["rsi_high"] for p in allp]) if allp else np.array([])
    bins = [0, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618, np.inf]
    labels = ["< 0.382", "0.382–0.5", "0.5–0.618", "0.618–0.786", "0.786–1", "1–1.272",
              "1.272–1.618", "> 1.618"]
    hist = pd.cut(retr, bins, labels=labels).value_counts(normalize=True).reindex(labels)
    md = [
        f"# Dönüş Noktası Analizi — {stamp}",
        "",
        f"{len(symbols)} coin ({', '.join(symbols[:12])}{'…' if len(symbols) > 12 else ''}), "
        f"günlük veri. Dönüş = fiyatın o noktadan en az {swing_atr:g} × ATR(14) geri dönmesi. "
        f"Toplam {sw_lo} dip, {sw_hi} tepe. Bir seviyeye 'değme' = günün low'u (dip) / high'ı "
        "(tepe) seviyeye 0.25 ATR'den yakın. Seviyeler yalnızca o günden önceki veriden.",
        "",
        "**Lift** = özelliğin dönüş günlerindeki sıklığı / tüm günlerdeki sıklığı (1 = fark yok). "
        "**q** = çoklu test düzeltmeli p-değeri (< 0.05 anlamlı). ✅ = anlamlı ve hem ilk %70'te "
        "hem son %30'da lift > 1. **İsabet** = özellik görüldüğü gün dönüş olma oranı "
        "(taban = rastgele bir günün dönüş olma oranı).",
        "",
        "## Sağlam bulgular (✅)",
        *([f"- **{'Dip' if r.side == 'low' else 'Tepe'}: {r.feature}** — dönüşlerin "
           f"{pct(r.p_swing)}'inde, normal günlerin {pct(r.p_all)}'inde ({r.lift:.2f}×); "
           f"eğitim {r.lift_train:.2f}×, test {r.lift_test:.2f}×; {r.coins_lift_gt1}/{r.coins} coinde lift > 1; "
           f"bu özellik görüldüğünde o günün dönüş olma olasılığı {pct(r.precision)} "
           f"(taban {pct(r.base_precision)}); sonraki 10 gün dönüş yönünde ort. {pct(r.fwd_mean)} "
           f"(taban {pct(r.base_fwd_mean)}), {r.coins_fwd_better}/{r.coins} coinde tabandan iyi"
           for r in robust.sort_values("lift", ascending=False).itertuples()] or ["- Yok"]),
        "",
        "## Dipler — tüm özellikler",
        *feature_rows(t[t.side == "low"].sort_values("lift", ascending=False)),
        "",
        "## Tepeler — tüm özellikler",
        *feature_rows(t[t.side == "high"].sort_values("lift", ascending=False)),
        "",
        "## Salınım profili",
        f"- Ortanca salınım boyu: {pct(np.median([p['median_move'] for p in allp]))}, "
        f"ortanca süre: {np.median([p['median_days'] for p in allp]):.0f} gün",
        f"- Ortanca geri çekilme (önceki bacağın oranı): {retr.median():.2f}",
        f"- Dönüşteki RSI ortancası: dipte {np.nanmedian(rsi_lo):.0f}, tepede {np.nanmedian(rsi_hi):.0f}",
        "",
        "| Geri çekilme / önceki bacak | Dönüşlerin payı |", "|---|---|",
        *[f"| {k} | {pct(v)} |" for k, v in hist.items()],
        "",
        "> Dipler tanımı gereği hareketin en uç günüdür; 'SMA20'den uzak', 'RSI aşırı', "
        "'geniş mum', 'önceki gün ucuna yakın değil' gibi özellikler bu yüzden kısmen mekaniktir. "
        "Son sütun bu etkiden bağımsız sınamadır: özellik görülen günün kapanışından 10 gün "
        "sonraki getiri (dip tarafında yukarı, tepe tarafında aşağı pozitif sayılır). "
        "Dönüşler geriye dönük etiketlenir (zigzag geleceği görür); özellikler ise yalnızca "
        "o güne kadarki veriden hesaplanır. 'İsabet' sütunu, özelliğin tek başına ne kadar "
        "seçici olduğunu gösterir: lift yüksek olsa bile isabet düşükse o özellik tek başına "
        "işlem sinyali değildir. Yatırım tavsiyesi değildir.",
    ]
    return "\n".join(md)


def main():
    ap = argparse.ArgumentParser(description="Fiyatın döndüğü yerlerde ortak özellik arayan ajan")
    ap.add_argument("--symbols", nargs="*", default=[])
    ap.add_argument("--list", help="scan.py'nin yazdığı top N csv (pair sütunu)")
    ap.add_argument("--swing-atr", type=float, default=4.0)
    ap.add_argument("--out", default=str(Path(__file__).parent / "reports"))
    a = ap.parse_args()
    pairs = list(a.symbols)
    if a.list:
        pairs += pd.read_csv(a.list)["pair"].tolist()
    pairs = pairs or ["BTCUSDT"]

    results, profiles, used = {}, {}, []
    for pair in pairs:
        try:
            daily = agent.fetch_binance(pair)
        except Exception as e:
            print(f"{pair}: {e}")
            continue
        if len(daily) < 400:
            print(f"{pair}: yetersiz veri ({len(daily)} gün)")
            continue
        piv, res = analyze_coin(daily, a.swing_atr)
        results[pair], profiles[pair] = res, swing_profile(daily, piv)
        used.append(pair.replace("USDT", ""))
        print(f"{pair}: {len(piv)} dönüş", flush=True)
        time.sleep(0.2)

    t = pooled_table(results)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    t.to_csv(out / f"reversals_{stamp}.csv", index=False)
    md = report(t, profiles, used, a.swing_atr, stamp)
    (out / f"reversals_{stamp}.md").write_text(md)
    print("\n" + md)


if __name__ == "__main__":
    main()
