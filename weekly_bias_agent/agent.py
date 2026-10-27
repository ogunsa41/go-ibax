#!/usr/bin/env python3
"""Haftalık Bias Ajanı.

1. Binance'ten (varsayılan) veya Crypto.com'dan günlük mumları çeker, haftalık mumlara
   (Pzt 00:00 UTC) çevirir.
2. Price-action literatüründen derlenen aday "haftalık bias" kurallarını hesaplar
   (yalnızca tamamlanmış geçmiş haftaların verisiyle, ileriye bakma yok). Kaynaklar
   için README.md'ye bakın.
3. Walk-forward: her hafta, o güne kadarki veride en iyi Sharpe'a sahip kuralı seçer ve
   o haftanın bias'ını (LONG / SHORT / NÖTR) belirler.
4. Backtest: haftanın açılışında pozisyon al, kapanışında kapat; komisyon pozisyon
   değişiminde düşülür, hafta içi fiyat teminatı silerse likidasyon sayılır. İsteğe bağlı
   volatilite hedefleme (Moskowitz-Ooi-Pedersen 2012) pozisyonu küçültür.
5. Seçim yanlılığına karşı Probabilistic / Deflated Sharpe (Bailey & López de Prado 2014)
   raporlar; bu haftanın bias'ını ve Markdown + HTML rapor üretir.

Kullanım:
    python3 agent.py --symbol BTCUSDT
    python3 agent.py --symbol ETHUSDT --long-only --vol-target 0.5
    python3 agent.py --source cryptocom --symbol BTC_USDT
"""
import argparse
import json
import math
import time
import urllib.request
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

API = "https://api.crypto.com/exchange/v1/public/get-candlestick"
# api.binance.com bazı bölgelerde (ör. ABD) 451 döner; data-api.binance.vision aynı
# public piyasa verisini sunan resmi aynadır.
BINANCE_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]
DAY_MS = 86_400_000
N = NormalDist()


# ----------------------------------------------------------------------------- veri
def fetch_daily(symbol, source="binance"):
    if source == "binance":
        return fetch_binance(symbol)
    return fetch_cryptocom(symbol)


def _binance_get(path):
    err = None
    for host in BINANCE_HOSTS:
        try:
            with urllib.request.urlopen(host + path, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # 451 coğrafi engel vb. -> sıradaki host
            err = e
    raise RuntimeError(f"Binance'e ulaşılamadı: {err}")


def fetch_binance(symbol):
    """Binance spot günlük mumlarını en baştan itibaren 1000'erli sayfalarla çeker."""
    symbol = symbol.replace("_", "").replace("/", "").upper()
    rows, start = [], 0
    while True:
        data = _binance_get(f"/api/v3/klines?symbol={symbol}&interval=1d"
                            f"&startTime={start}&limit=1000")
        if not data:
            break
        rows += data
        if len(data) < 1000:
            break
        start = data[-1][0] + DAY_MS
    if not rows:
        raise RuntimeError(f"{symbol} için Binance verisi bulunamadı")
    df = pd.DataFrame([r[:6] for r in rows],
                      columns=["t", "open", "high", "low", "close", "volume"])
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.drop_duplicates("t").set_index("t").sort_index().astype(float)
    return drop_open_candle(df)


def fetch_cryptocom(symbol, max_pages=20):
    """Crypto.com günlük mumlarını geriye doğru sayfalayarak çeker."""
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
    return drop_open_candle(df)


def drop_open_candle(df, now=None):
    """Henüz kapanmamış günlük mumu at (kapanmamış mumla sinyal üretmemek için)."""
    now = now or pd.Timestamp.now(tz="UTC")
    return df[df.index + pd.Timedelta(days=1) <= now]


def to_weekly(daily):
    w = daily.resample("W-MON", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    w["days"] = daily["close"].resample("W-MON", label="left", closed="left").count()
    return w.dropna()


def complete_weeks(w_all, now=None):
    """Bitmiş haftalar; listeleme haftası gibi baştaki yarım hafta atılır."""
    now = now or pd.Timestamp.now(tz="UTC")
    w = w_all[(w_all.index + pd.Timedelta(weeks=1)) <= now]
    while len(w) and w["days"].iloc[0] < 7:
        w = w.iloc[1:]
    return w


# ----------------------------------------------------------------------------- kurallar
RULE_SOURCES = {
    "momentum_1w": "Liu & Tsyvinski (2021): kripto haftalık momentum; ICT 'weekly open üstü/altı'",
    "reversal_1w": "momentum_1w'nin tersi (kontrol kuralı)",
    "momentum_4w": "Liu & Tsyvinski (2021) 1-4 haftalık momentum",
    "tsmom_12w": "Moskowitz, Ooi & Pedersen (2012) time-series momentum (12 hafta)",
    "tsmom_26w": "Moskowitz, Ooi & Pedersen (2012) time-series momentum (26 hafta)",
    "close_location": "Haftalık kapanışın aralıktaki yeri (üst %30 / alt %30)",
    "close_location_rev": "close_location'ın tersi (kontrol kuralı)",
    "brooks_trend_bar": "Al Brooks trend bar: gövde ≥ aralığın %50'si, kapanış uç %40'ta",
    "outside_week": "Larry Williams / Brooks outside bar: iki yönü de aşıp uçta kapanış",
    "breakout_prev_range": "Önceki haftanın high/low'u dışında kapanış -> devam",
    "liquidity_sweep": "ICT: PWH/PWL likidite alınıp içeri kapanış -> karşı likiditeye yönel",
    "turtle_soup_20w": "Raschke & Connors (1995) Turtle Soup, 20 haftalık uçta sahte kırılım",
    "premium_discount_4w": "ICT dealing range: 4 haftalık aralığın discount'u long, premium'u short",
    "trend_sma10": "Kapanış 10 haftalık ortalamanın üstü/altı",
    "trend_sma20": "Kapanış 20 haftalık ortalamanın üstü/altı",
    "trend_and_close_loc": "trend_sma20 ile güçlü kapanış aynı yöndeyse",
}


def bias_rules(w):
    """Her kural, hafta t için t-1 sonuna kadarki veriyle {-1, 0, +1} sinyal üretir.

    Dönen `mkt`: haftalık açılış→kapanış getirisi ve hafta içi en uç hareketler
    (likidasyon kontrolü için).
    """
    o, h, l, c = w["open"], w["high"], w["low"], w["close"]
    ret = c / o - 1
    rng = (h - l).replace(0, np.nan)
    clv = (c - l) / rng  # kapanışın haftalık aralıktaki yeri (0..1)
    body = (c - o).abs() / rng
    ph, pl = h.shift(1), l.shift(1)
    sma10, sma20 = c.rolling(10).mean(), c.rolling(20).mean()

    raw = pd.DataFrame(index=w.index)
    raw["momentum_1w"] = np.sign(ret)
    raw["reversal_1w"] = -np.sign(ret)
    raw["momentum_4w"] = np.sign(c / o.shift(3) - 1)
    raw["tsmom_12w"] = np.sign(c / o.shift(11) - 1)
    raw["tsmom_26w"] = np.sign(c / o.shift(25) - 1)
    raw["close_location"] = np.select([clv > 0.7, clv < 0.3], [1, -1], 0)
    raw["close_location_rev"] = -raw["close_location"]
    raw["brooks_trend_bar"] = np.select(
        [(c > o) & (body >= 0.5) & (clv >= 0.6), (c < o) & (body >= 0.5) & (clv <= 0.4)],
        [1, -1], 0)
    outside = (h > ph) & (l < pl)
    raw["outside_week"] = np.select([outside & (clv > 0.7), outside & (clv < 0.3)], [1, -1], 0)
    raw["breakout_prev_range"] = np.select([c > ph, c < pl], [1, -1], 0)
    # ICT: sell-side (PWL) süpürüldü ama haftalık kapanış PWL üstünde -> buy-side'a yönel
    swept_low = (l < pl) & (c > pl)
    swept_high = (h > ph) & (c < ph)
    raw["liquidity_sweep"] = np.select([swept_low & ~swept_high, swept_high & ~swept_low],
                                       [1, -1], 0)
    # Raschke Turtle Soup: yeni 20 haftalık dip, önceki dip en az 3 hafta önce, kapanış geri içeri
    prior_low = l.shift(1).rolling(19).min()
    prior_high = h.shift(1).rolling(19).max()
    low_age = l.shift(1).rolling(19).apply(lambda x: len(x) - 1 - np.argmin(x), raw=True)
    high_age = h.shift(1).rolling(19).apply(lambda x: len(x) - 1 - np.argmax(x), raw=True)
    raw["turtle_soup_20w"] = np.select(
        [(l < prior_low) & (low_age >= 3) & (c > prior_low),
         (h > prior_high) & (high_age >= 3) & (c < prior_high)], [1, -1], 0)
    # ICT premium/discount: 4 haftalık dealing range'in alt çeyreği discount, üst çeyreği premium
    hi4, lo4 = h.rolling(4).max(), l.rolling(4).min()
    pos4 = (c - lo4) / (hi4 - lo4).replace(0, np.nan)
    raw["premium_discount_4w"] = np.select([pos4 < 0.25, pos4 > 0.75], [1, -1], 0)
    raw["trend_sma10"] = np.sign(c - sma10)
    raw["trend_sma20"] = np.sign(c - sma20)
    raw["trend_and_close_loc"] = np.where(raw["trend_sma20"] == raw["close_location"],
                                          raw["close_location"], 0)
    raw = raw.fillna(0)
    mkt = pd.DataFrame({"ret": ret, "up": h / o - 1, "dn": l / o - 1})
    # Bir sonraki haftaya kaydır: t haftasının sinyali t-1 bilgisiyle oluşur
    return raw.shift(1).fillna(0).astype(int), mkt


# ----------------------------------------------------------------------------- metrikler
def strat_returns(pos, mkt, fee):
    """Haftalık strateji getirisi. Hafta içi ters hareket teminatı silerse -%100."""
    if isinstance(mkt, pd.Series):  # yalnız getiri verilmişse
        mkt = pd.DataFrame({"ret": mkt, "up": mkt.clip(lower=0), "dn": mkt.clip(upper=0)})
    turnover = pos.diff().abs().fillna(pos.abs())
    r = pos * mkt["ret"] - turnover * fee
    worst = pd.Series(np.where(pos < 0, pos * mkt["up"], pos * mkt["dn"]), index=pos.index)
    return r.where(worst > -1, -1.0).clip(lower=-1)


def apply_ruin(r):
    """Hesap sıfırlandıktan (-%100) sonra işlem yapılamaz: sonraki getiriler 0."""
    dead = (r <= -1).cummax().shift(1, fill_value=False)
    return r.mask(dead, 0.0)


def vol_scale(signal, mkt, target, lookback=12, cap=1.0):
    """Moskowitz-Ooi-Pedersen tarzı: pozisyon = sinyal × hedef_vol / geçmiş_vol (≤ cap)."""
    if not target:
        return signal.astype(float)
    vol = mkt["ret"].rolling(lookback).std().shift(1) * math.sqrt(52)
    return (signal * (target / vol).clip(upper=cap)).fillna(0)


def stats(r, pos=None):
    r = r.dropna()
    if len(r) == 0:
        return {}
    ruined = bool((r <= -1).any())
    if ruined:  # sıfırlanan hesabın metrikleri yalnız tasfiyeye kadar olan haftalardan
        r = r.iloc[:int(np.argmax((r <= -1).values)) + 1]
    eq = (1 + r).cumprod()
    years = len(r) / 52
    active = r[pos.reindex(r.index) != 0] if pos is not None else r
    sd = r.std()
    return {
        "weeks": len(r),
        "active_weeks": len(active),
        "hit_rate": float((active > 0).mean()) if len(active) else float("nan"),
        "avg_week": float(active.mean()) if len(active) else float("nan"),
        "total_return": float(eq.iloc[-1] - 1),
        "cagr": float(eq.iloc[-1] ** (1 / years) - 1) if years > 0 and eq.iloc[-1] > 0 else -1.0,
        "sharpe": float(r.mean() / sd * math.sqrt(52)) if sd > 0 else 0.0,
        "max_dd": -1.0 if ruined else float((eq / eq.cummax() - 1).min()),
        "ruined": ruined,
        "t_stat": float(active.mean() / active.std() * math.sqrt(len(active)))
        if len(active) > 2 and active.std() > 0 else 0.0,
        "psr": psr(r),
    }


def sharpe(r):
    sd = r.std()
    return r.mean() / sd * math.sqrt(52) if sd and sd > 0 else -np.inf


def psr(r, sr_benchmark=0.0):
    """Probabilistic Sharpe Ratio (Bailey & López de Prado 2012): P(gerçek SR > benchmark).

    SR'ler haftalık (yıllıklandırılmamış) birimdedir.
    """
    r = r.dropna()
    n, sd = len(r), r.std()
    if n < 10 or not sd > 0:
        return float("nan")
    sr = r.mean() / sd
    g3, g4 = r.skew(), r.kurt() + 3
    denom = 1 - g3 * sr + (g4 - 1) / 4 * sr ** 2
    if denom <= 0:
        return float("nan")
    return float(N.cdf((sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(denom)))


def deflated_sharpe(best_r, trial_srs):
    """Deflated Sharpe Ratio (Bailey & López de Prado 2014).

    `trial_srs`: denenen tüm kuralların haftalık SR'leri. En iyi kuralın SR'sinin, bu kadar
    deneme arasından şansla çıkabilecek beklenen maksimumu aşma olasılığını verir.
    """
    trial_srs = np.asarray([s for s in trial_srs if np.isfinite(s)])
    k = len(trial_srs)
    if k < 2:
        return psr(best_r)
    em = 0.5772156649
    sr0 = trial_srs.std(ddof=1) * ((1 - em) * N.inv_cdf(1 - 1 / k)
                                   + em * N.inv_cdf(1 - 1 / (k * math.e)))
    return psr(best_r, sr0)


# ----------------------------------------------------------------------------- ajan
def select_rule(signals, rule_returns, i, min_active, lookback=0):
    """i. haftadan ÖNCEKİ verilerle en iyi Sharpe'lı kuralı seçer (Sharpe ≤ 0 ise None)."""
    lo = max(0, i - lookback) if lookback else 0
    best, best_s = None, 0.0
    for k, r in rule_returns.items():
        if (signals[k].iloc[lo:i] != 0).sum() < min_active:
            continue
        s = sharpe(r.iloc[lo:i])
        if s > best_s:
            best, best_s = k, s
    return best, best_s


def walk_forward(signals, mkt, fee, min_train, min_active, lookback=0, vol_target=None):
    """Her hafta yalnızca geçmişe bakarak en iyi kuralı seçer ve o haftayı işler."""
    rule_returns = {k: strat_returns(signals[k], mkt, fee) for k in signals}
    chosen, sig = [], []
    for i in range(len(mkt)):
        best = select_rule(signals, rule_returns, i, min_active, lookback)[0] \
            if i >= min_train else None
        chosen.append(best)
        sig.append(int(signals[best].iloc[i]) if best else 0)
    sig = pd.Series(sig, index=mkt.index)
    pos = vol_scale(sig, mkt, vol_target)
    pos.iloc[:min_train] = 0
    return pos, pd.Series(chosen, index=mkt.index), apply_ruin(strat_returns(pos, mkt, fee))


def next_week_signal(w, fee, min_active, long_only, lookback=0, vol_target=None):
    """Tamamlanmış haftalar `w` ile bir sonraki (içinde bulunulan) haftanın bias'ı.

    walk_forward ile aynı seçim fonksiyonunu kullanır; testler iki yolun aynı sonucu
    verdiğini doğrular.
    """
    nxt = w.iloc[[-1]].copy()  # yer tutucu: bu satırın fiyatları hiçbir sinyale girmez
    nxt.index = nxt.index + pd.Timedelta(weeks=1)
    sig, mkt = bias_rules(pd.concat([w, nxt]))
    if long_only:
        sig = sig.clip(lower=0)
    i = len(sig) - 1
    rule_returns = {k: strat_returns(sig[k].iloc[:i], mkt.iloc[:i], fee) for k in sig}
    best, best_s = select_rule(sig, rule_returns, i, min_active, lookback)
    if not best:
        return nxt.index[0], None, best_s, 0, 0.0
    size = float(vol_scale(sig[best], mkt, vol_target).iloc[i])
    return nxt.index[0], best, best_s, int(sig[best].iloc[i]), size


def wilson(k, n, z=1.96):
    """Başarı oranı için Wilson %95 güven aralığı."""
    if n == 0:
        return float("nan"), float("nan")
    ph = k / n
    mid = (ph + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return mid - half, mid + half


def bias_accuracy(pos, ret, recent=52):
    """Ajanın bias isabeti: LONG dediği haftaların yükselişle, SHORT dediklerinin düşüşle
    kapanma oranı (NÖTR haftalar sayılmaz).

    Şans seviyesi: aynı LONG/SHORT karışımıyla rastgele tahmin yapılsaydı beklenen isabet
    (LONG payı × yükseliş oranı + SHORT payı × düşüş oranı). p-değeri bu seviyeye karşı
    tek yönlü binom testinin normal yaklaşımıdır.
    """
    d = np.sign(pos)
    calls = d != 0
    hit = (d * ret > 0)[calls]
    n, k = int(calls.sum()), int(hit.sum())
    out = {"n": n, "acc": k / n if n else float("nan")}
    out["ci_low"], out["ci_high"] = wilson(k, n)
    for name, side in (("long", 1), ("short", -1)):
        m = d == side
        out[f"{name}_n"] = int(m.sum())
        out[f"{name}_acc"] = float((ret[m] > 0).mean() if side > 0 else (ret[m] < 0).mean()) \
            if m.any() else float("nan")
    up = float((ret[calls] > 0).mean()) if n else float("nan")
    share_long = out["long_n"] / n if n else float("nan")
    out["chance"] = share_long * up + (1 - share_long) * (1 - up) if n else float("nan")
    if n and 0 < out["chance"] < 1:
        z = (k - n * out["chance"]) / math.sqrt(n * out["chance"] * (1 - out["chance"]))
        out["p_value"] = 1 - N.cdf(z)
    else:
        out["p_value"] = float("nan")
    rd, rr = d.iloc[-recent:], ret.iloc[-recent:]
    rc = rd != 0
    out["recent_n"] = int(rc.sum())
    out["recent_acc"] = float((rd * rr > 0)[rc].mean()) if rc.any() else float("nan")
    return out


def weekday_profile(daily):
    r = (daily["close"] / daily["open"] - 1).to_frame("ret")
    r["day"] = r.index.day_name()
    g = r.groupby("day")["ret"]
    out = pd.DataFrame({"avg": g.mean(), "hit_rate": g.apply(lambda x: (x > 0).mean()),
                        "t_stat": g.mean() / g.std() * np.sqrt(g.count()), "n": g.count()})
    order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return out.reindex(order)


def analyze(daily, fee=0.001, long_only=False, min_train=52, min_active=20,
            lookback=0, vol_target=None, now=None):
    """Tek bir coin için kural tablosu, walk-forward backtest ve bu haftanın bias'ı."""
    w = complete_weeks(to_weekly(daily), now)
    sig, mkt = bias_rules(w)
    if long_only:
        sig = sig.clip(lower=0)

    split = int(len(w) * 0.7)
    rule_tbl, trial_srs = [], []
    for k in sig:
        r = strat_returns(sig[k], mkt, fee)
        tr = r.iloc[:split]
        trial_srs.append(tr.mean() / tr.std() if tr.std() > 0 else np.nan)
        rule_tbl.append((k, stats(tr, sig[k]), stats(r.iloc[split:], sig[k])))
    rule_tbl.sort(key=lambda x: x[1].get("sharpe", 0), reverse=True)
    best_train = rule_tbl[0][0]
    dsr = deflated_sharpe(strat_returns(sig[best_train], mkt, fee).iloc[:split], trial_srs)

    pos, chosen, agent_r = walk_forward(sig, mkt, fee, min_train, min_active, lookback,
                                        vol_target)
    oos = slice(min_train, None)
    bh = mkt["ret"].iloc[oos]

    week, best, best_sh, signal, size = next_week_signal(w, fee, min_active, long_only,
                                                         lookback, vol_target)
    acc = bias_accuracy(pos.iloc[oos], mkt["ret"].iloc[oos])
    return {"w": w, "sig": sig, "ret": mkt["ret"], "mkt": mkt, "rule_tbl": rule_tbl, "acc": acc,
            "split": split, "best_train": best_train, "dsr": dsr,
            "pos": pos, "chosen": chosen, "agent_r": agent_r, "oos": oos, "bh": bh,
            "agent_stats": stats(agent_r.iloc[oos], pos), "bh_stats": stats(bh),
            "picks": chosen.iloc[oos].value_counts(),
            "week": week, "rule": best, "rule_sharpe": best_sh, "signal": signal,
            "size": size}


# ----------------------------------------------------------------------------- rapor
def pct(x):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.2f}%"


def stats_row(name, s):
    return (f"| {name} | {s['active_weeks']}/{s['weeks']} | {pct(s['hit_rate'])} | "
            f"{pct(s['avg_week'])} | {pct(s['total_return'])} | {pct(s['cagr'])} | "
            f"{s['sharpe']:.2f} | {pct(s['max_dd'])} | {s['t_stat']:.2f} | {pct(s['psr'])} |")


HDR = ("| Strateji | Aktif/Toplam hafta | İsabet | Ort. hafta | Toplam getiri | CAGR | "
       "Sharpe | Max DD | t-stat | PSR |\n|---|---|---|---|---|---|---|---|---|---|")


def svg_equity(curves, width=900, height=320):
    colors = ["#2563eb", "#9ca3af", "#16a34a"]
    allv = np.log(pd.concat(curves, axis=1).dropna().clip(lower=1e-4))
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
    ap.add_argument("--source", choices=["binance", "cryptocom"], default="binance")
    ap.add_argument("--symbol", default="BTCUSDT", help="Binance: BTCUSDT, Crypto.com: BTC_USDT")
    ap.add_argument("--fee", type=float, default=0.001, help="pozisyon değişimi başına maliyet (0.001=%%0.1)")
    ap.add_argument("--long-only", action="store_true", help="short sinyallerini nötr say")
    ap.add_argument("--vol-target", type=float, default=None,
                    help="yıllık hedef volatilite (ör. 0.5); boşsa sabit 1x pozisyon")
    ap.add_argument("--lookback", type=int, default=0,
                    help="kural seçiminde bakılacak hafta (0 = tüm geçmiş)")
    ap.add_argument("--min-train", type=int, default=52, help="walk-forward ısınma süresi (hafta)")
    ap.add_argument("--min-active", type=int, default=20, help="kural seçimi için min. aktif hafta")
    ap.add_argument("--out", default=str(Path(__file__).parent / "reports"))
    a = ap.parse_args()

    daily = fetch_daily(a.symbol, a.source)
    res = analyze(daily, a.fee, a.long_only, a.min_train, a.min_active, a.lookback,
                  a.vol_target)
    w, rule_tbl, split = res["w"], res["rule_tbl"], res["split"]
    pos, chosen, agent_r = res["pos"], res["chosen"], res["agent_r"]
    agent_s, bh_s, oos, bh = res["agent_stats"], res["bh_stats"], res["oos"], res["bh"]
    week, best, best_sh, signal = res["week"], res["rule"], res["rule_sharpe"], res["signal"]
    label = {1: "LONG (yükseliş) 🟢", -1: "SHORT (düşüş) 🔴", 0: "NÖTR ⚪"}[signal]
    wd = weekday_profile(daily)
    picks = res["picks"]
    now = pd.Timestamp.now(tz="UTC")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d")
    size_txt = f", pozisyon büyüklüğü %{res['size'] * 100:.0f}" if a.vol_target and best else ""
    md = [
        f"# Haftalık Bias Raporu — {a.symbol} ({stamp})",
        "",
        f"Kaynak: {a.source}. Veri: {w.index[0].date()} → {w.index[-1].date()} ({len(w)} tamamlanmış hafta), "
        f"maliyet {a.fee * 100:.2f}%/değişim, {'yalnız long' if a.long_only else 'long/short'}, "
        f"{'vol hedefi ' + format(a.vol_target, '.0%') if a.vol_target else 'sabit 1x pozisyon'}, "
        f"kural seçimi {'son ' + str(a.lookback) + ' hafta' if a.lookback else 'tüm geçmiş'}.",
        "",
        f"## Bu haftanın bias'ı ({week.date()} haftası)",
        f"**Ajanın bias isabeti: %{res['acc']['acc'] * 100:.1f}** — geçmişte verdiği "
        f"{res['acc']['n']} LONG/SHORT bias'ın doğru çıkma oranı "
        f"(%95 aralık %{res['acc']['ci_low'] * 100:.0f}–%{res['acc']['ci_high'] * 100:.0f}; "
        f"LONG %{res['acc']['long_acc'] * 100:.0f} / {res['acc']['long_n']}, "
        f"SHORT %{res['acc']['short_acc'] * 100:.0f} / {res['acc']['short_n']}; "
        f"şans seviyesi %{res['acc']['chance'] * 100:.1f}, p = {res['acc']['p_value']:.3f}; "
        f"son 52 hafta %{res['acc']['recent_acc'] * 100:.0f} / {res['acc']['recent_n']})",
        "",
        f"**{label}** — seçilen kural: `{best}` (geçmiş Sharpe {best_sh:.2f}{size_txt})" if best else
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
        f"Eğitimde en iyi kural `{res['best_train']}`; {len(rule_tbl)} kural denendiği "
        f"hesaba katılınca Deflated Sharpe = {pct(res['dsr'])} (≥ %95 anlamlı sayılır).",
        "",
        HDR, *[stats_row(k, tr) for k, tr, _ in rule_tbl], "",
        f"## Aday kurallar — test (son %30, {w.index[split].date()}→{w.index[-1].date()})",
        HDR, *[stats_row(k, te) for k, _, te in rule_tbl], "",
        "## Gün bazlı profil (günlük açılış→kapanış, UTC)",
        "| Gün | Ort. getiri | İsabet | t-stat | n |\n|---|---|---|---|---|",
        *[f"| {d} | {pct(r.avg)} | {pct(r.hit_rate)} | {r.t_stat:.2f} | {int(r.n)} |"
          for d, r in wd.iterrows()],
        "",
        "> PSR = gerçek Sharpe'ın 0'dan büyük olma olasılığı (çarpıklık/basıklık düzeltmeli). "
        "|t-stat| < 2 veya PSR < %95 olan sonuçlar anlamlı sayılmaz. Fonlama ücreti ve "
        "kayma (slippage) modele dahil değildir. Yatırım tavsiyesi değildir.",
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
    pd.DataFrame({"week": w.index, "return": res["ret"].values, "agent_pos": pos.values,
                  "rule": chosen.values, "agent_ret": agent_r.values}).to_csv(
        out / f"{a.symbol}_{stamp}_trades.csv", index=False)

    print(md_text)
    print(f"\nRaporlar: {out}")


if __name__ == "__main__":
    main()
