#!/usr/bin/env python3
"""Top 50 coin taraması.

1. CoinGecko'dan piyasa değerine göre sıralı listeyi alır; stablecoin, wrapped ve
   staked türevleri ele, Binance'te USDT paritesi olanlardan ilk N'i seçer
   (CoinGecko'ya ulaşılamazsa Binance 24s USDT hacmine göre sıralar).
2. Listeyi reports/top50_<tarih>.csv olarak kaydeder.
3. Her coin için agent.analyze'ı iki modda çalıştırır: yön bias'ı (long/short) ve işlem
   modu (yalnız long + volatilite hedefi). Tek bir özet tablo (Markdown + CSV) üretir.

Kullanım:
    python3 scan.py
    python3 scan.py --top 30 --vol-target 0.4
"""
import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

import agent

COINGECKO = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
             "&order=market_cap_desc&per_page=250&page=1")
STABLES = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDS", "PYUSD", "USDD", "USD1",
           "BUSD", "USDP", "GUSD", "FRAX", "LUSD", "EURC", "EURT", "XAUT", "PAXG", "USDX",
           "RLUSD", "USDF", "USDTB", "BFUSD", "SUSDE", "USD0", "USDG", "USDY", "OUSG", "BUIDL"}
DERIV_WORDS = ("wrapped", "staked", "bridged", "restaked", "liquid staking", "binance-peg")
BIAS_TXT = {1: "🟢 LONG", -1: "🔴 SHORT", 0: "⚪ NÖTR"}


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "weekly-bias-agent"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def binance_usdt_pairs():
    info = agent._binance_get("/api/v3/exchangeInfo?permissions=SPOT")
    return {s["baseAsset"]: s["symbol"] for s in info["symbols"]
            if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}


def is_stable_or_derivative(sym, name, price, change_24h=None):
    n = name.lower()
    pegged = price is not None and 0.97 < price < 1.03 and (
        "usd" in n or "stable" in n or (change_24h is not None and abs(change_24h) < 0.5))
    return sym in STABLES or any(wd in n for wd in DERIV_WORDS) or pegged


def coingecko_markets(cache_dir):
    """CoinGecko listesini günlük önbellekle ve 429'a karşı yeniden deneyerek alır."""
    cache = Path(cache_dir) / f"coingecko_{pd.Timestamp.now(tz='UTC'):%Y-%m-%d}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    for wait in (0, 20, 60):
        time.sleep(wait)
        try:
            coins = get_json(COINGECKO)
            cache.write_text(json.dumps(coins))
            return coins
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
    raise RuntimeError("CoinGecko hız sınırı (429)")


def top_coins(n, cache_dir):
    pairs = binance_usdt_pairs()
    try:
        coins = coingecko_markets(cache_dir)
        px = {t["symbol"]: float(t["price"]) for t in agent._binance_get("/api/v3/ticker/price")}
        out = []
        for c in coins:
            sym = c["symbol"].upper()
            price = c.get("current_price")
            if is_stable_or_derivative(sym, c["name"], price,
                                       c.get("price_change_percentage_24h")):
                continue
            # Aynı sembol farklı token olabilir: fiyatlar %10'dan fazla ayrışıyorsa atla
            if sym in pairs and price and abs(px.get(pairs[sym], 0) / price - 1) > 0.10:
                continue
            if sym in pairs and pairs[sym] not in {o["pair"] for o in out}:
                out.append({"rank": len(out) + 1, "symbol": sym, "name": c["name"],
                            "pair": pairs[sym], "market_cap": c["market_cap"]})
            if len(out) == n:
                break
        return pd.DataFrame(out), "CoinGecko piyasa değeri"
    except Exception as e:
        print(f"CoinGecko alınamadı ({e}); Binance hacmine göre sıralanıyor")
    tick = agent._binance_get("/api/v3/ticker/24hr")
    vol = {t["symbol"]: float(t["quoteVolume"]) for t in tick}
    rows = [(b, p, vol.get(p, 0)) for b, p in pairs.items()
            if not is_stable_or_derivative(b, b, None)]
    rows.sort(key=lambda x: -x[2])
    df = pd.DataFrame([{"rank": i + 1, "symbol": b, "name": b, "pair": p, "market_cap": None}
                       for i, (b, p, _) in enumerate(rows[:n])])
    return df, "Binance 24s USDT hacmi"


def summarize(res, enough):
    ag, bh = res["agent_stats"], res["bh_stats"]
    g = lambda d, k: d.get(k) if enough else None
    return {"signal": res["signal"], "rule": res["rule"] or "-", "size": res["size"],
            "ret": g(ag, "total_return"), "sharpe": g(ag, "sharpe"), "dd": g(ag, "max_dd"),
            "psr": g(ag, "psr"), "bh_ret": g(bh, "total_return"), "bh_sharpe": g(bh, "sharpe"),
            "bh_dd": g(bh, "max_dd"), "dsr": res["dsr"] if enough else None,
            **{f"acc_{k}": v for k, v in res["acc"].items()}}


def main():
    ap = argparse.ArgumentParser(description="Top N coin için haftalık bias taraması")
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--fee", type=float, default=0.001)
    ap.add_argument("--vol-target", type=float, default=0.5,
                    help="işlem modu için yıllık hedef volatilite (0 = kapalı)")
    ap.add_argument("--min-train", type=int, default=52)
    ap.add_argument("--min-active", type=int, default=20)
    ap.add_argument("--out", default=str(Path(__file__).parent / "reports"))
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")

    coins, basis = top_coins(a.top, out)
    coins.to_csv(out / f"top{a.top}_{stamp}.csv", index=False)
    print(f"Top {len(coins)} liste ({basis}): " + ", ".join(coins["symbol"]))

    results, week = [], None
    for c in coins.itertuples():
        row = {"rank": c.rank, "symbol": c.symbol, "pair": c.pair}
        try:
            daily = agent.fetch_binance(c.pair)
            # Yön bias'ı: long/short, sabit pozisyon
            bias = agent.analyze(daily, a.fee, False, a.min_train, a.min_active)
            # İşlem modu: yalnız long + volatilite hedefi (kripto pozitif drift, short likidasyon riski)
            trade = agent.analyze(daily, a.fee, True, a.min_train, a.min_active,
                                  vol_target=a.vol_target or None)
            weeks = len(bias["w"])
            enough = weeks >= a.min_train + 26
            week = bias["week"]
            row.update({"weeks": weeks, "note": "" if enough else "yetersiz geçmiş, backtest yok"})
            row.update({f"bias_{k}": v for k, v in summarize(bias, enough).items()})
            row.update({f"trade_{k}": v for k, v in summarize(trade, enough).items()})
        except Exception as e:
            row.update({"bias_signal": None, "note": f"hata: {e}"})
        results.append(row)
        acc = row.get("bias_acc_acc")
        print(f"{c.rank:>2}. {c.symbol:<8} {BIAS_TXT.get(row.get('bias_signal'), '?'):<9} "
              f"isabet %{(acc if acc == acc and acc is not None else 0) * 100:.1f} "
              f"{row.get('bias_rule', '')} | "
              f"işlem: %{(row.get('trade_size') or 0) * 100:.0f} "
              f"{row.get('note', '')}")
        time.sleep(0.2)

    df = pd.DataFrame(results)
    df.to_csv(out / f"scan_{stamp}.csv", index=False)

    counts = df["bias_signal"].map(BIAS_TXT).value_counts()
    bt = df.dropna(subset=["bias_sharpe"])
    longs = df[(df["trade_size"].fillna(0) > 0)]
    ok = bt.dropna(subset=["bias_acc_acc"])

    def beat(mode):
        return int((bt[f"{mode}_sharpe"] > bt[f"{mode}_bh_sharpe"]).sum())

    def signif(mode):
        return int((bt[f"{mode}_psr"] >= 0.95).sum())

    def pp(r, k, d=0):
        v = r.get(k)
        return "—" if v is None or pd.isna(v) else f"%{v * 100:.{d}f}"

    def cnt(r, k):
        v = r.get(k)
        return "—" if v is None or pd.isna(v) else str(int(v))

    def num(r, k, f="{:.2f}"):
        v = r.get(k)
        return "—" if v is None or pd.isna(v) else f.format(v)

    def p(r, k):
        v = r.get(k)
        return "—" if v is None or pd.isna(v) else f"{v * 100:+.0f}%"

    md = [
        f"# Top {len(df)} Haftalık Bias Taraması — {week.date() if week is not None else stamp} haftası",
        "",
        f"Liste: {basis} (stablecoin/wrapped hariç, Binance USDT paritesi olanlar). "
        f"Maliyet {a.fee * 100:.2f}%/değişim, walk-forward ısınma {a.min_train} hafta, "
        f"{len(agent.RULE_SOURCES)} aday kural.",
        "",
        "İki mod çalıştırılır:",
        "- **Yön bias'ı (L/S):** kurallar long ve short serbest, sabit 1x pozisyon.",
        f"- **İşlem modu:** yalnız long, yıllık %{a.vol_target * 100:.0f} volatilite hedefi "
        "(pozisyon = hedef vol / son 12 haftanın volü, en fazla 1x).",
        "",
        "**Yön bias'ı dağılımı:** " + ", ".join(f"{k}: {v}" for k, v in counts.items()),
        "",
        f"**Bu hafta işlem modunda long önerilenler ({len(longs)}):** " +
        (", ".join(f"{r.symbol} (%{r.trade_size * 100:.0f})" for r in longs.itertuples()) or "yok"),
        "",
        f"**Ajanın ortalama bias isabeti:** %{ok['bias_acc_acc'].mean() * 100:.1f} "
        f"(şans seviyesi ortalaması %{ok['bias_acc_chance'].mean() * 100:.1f}); "
        f"şansı geçen {int((ok['bias_acc_acc'] > ok['bias_acc_chance']).sum())}/{len(ok)} coin, "
        f"istatistiksel olarak anlamlı (p < 0.05) {int((ok['bias_acc_p_value'] < 0.05).sum())} coin: "
        + (", ".join(f"{r.symbol} %{r.bias_acc_acc * 100:.1f}"
                     for r in ok[ok["bias_acc_p_value"] < 0.05].itertuples()) or "yok"),
        "",
        "**En isabetli 10:** " + ", ".join(
            f"{r.symbol} %{r.bias_acc_acc * 100:.1f}" for r in ok.nlargest(10, "bias_acc_acc").itertuples()),
        "",
        "**En isabetsiz 10:** " + ", ".join(
            f"{r.symbol} %{r.bias_acc_acc * 100:.1f}" for r in ok.nsmallest(10, "bias_acc_acc").itertuples()),
        "",
        f"Backtesti olan {len(bt)} coinde Sharpe'ı buy & hold'u geçen: L/S {beat('bias')}, "
        f"işlem modu {beat('trade')}. PSR ≥ %95 olan: L/S {signif('bias')}, "
        f"işlem modu {signif('trade')}.",
        "",
        "| # | Coin | Bias isabeti | %95 aralık | Bias sayısı | LONG isabeti | SHORT isabeti | Şans seviyesi | p | Son 52 hafta | Bu haftaki bias | Kural | İşlem pozisyonu | Hafta | L/S getiri | L/S SR | L/S DD | "
        "İşlem getiri | İşlem SR | İşlem DD | İşlem PSR | B&H getiri | B&H SR | B&H DD | Not |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        size = r.get("trade_size") or 0
        md.append(
            f"| {r['rank']} | {r['symbol']} | {pp(r, 'bias_acc_acc', 1)} | "
            f"{pp(r, 'bias_acc_ci_low')}–{pp(r, 'bias_acc_ci_high')} | {cnt(r, 'bias_acc_n')} | "
            f"{pp(r, 'bias_acc_long_acc')} ({cnt(r, 'bias_acc_long_n')}) | "
            f"{pp(r, 'bias_acc_short_acc')} ({cnt(r, 'bias_acc_short_n')}) | "
            f"{pp(r, 'bias_acc_chance', 1)} | {num(r, 'bias_acc_p_value', '{:.3f}')} | "
            f"{pp(r, 'bias_acc_recent_acc')} ({cnt(r, 'bias_acc_recent_n')}) | "
            f"{BIAS_TXT.get(r.get('bias_signal'), '?')} | "
            f"`{r.get('bias_rule', '-')}` | {'LONG %' + format(size * 100, '.0f') if size > 0 else '—'} | "
            f"{r.get('weeks', '—')} | {p(r, 'bias_ret')} | {num(r, 'bias_sharpe')} | "
            f"{p(r, 'bias_dd')} | {p(r, 'trade_ret')} | {num(r, 'trade_sharpe')} | "
            f"{p(r, 'trade_dd')} | {p(r, 'trade_psr').lstrip('+')} | {p(r, 'bias_bh_ret')} | "
            f"{num(r, 'bias_bh_sharpe')} | {p(r, 'bias_bh_dd')} | {r.get('note', '')} |")
    md += ["", "> Bias isabeti = ajanın walk-forward backtestte (her hafta yalnız geçmişe bakarak) "
           "verdiği LONG/SHORT bias'larının doğru çıkma oranı; NÖTR haftalar sayılmaz. "
           "Şans seviyesi = aynı LONG/SHORT karışımıyla rastgele tahminin beklenen isabeti. "
           "p = isabetin şans seviyesinden yüksek olmasının tesadüf olma olasılığı (< 0.05 anlamlı).",
           "Tüm getiri/SR/DD sütunları walk-forward örneklem dışı sonuçlardır. "
           "PSR = gerçek Sharpe'ın 0'dan büyük olma olasılığı; < %95 anlamlı sayılmaz. "
           "Fonlama ücreti ve kayma dahil değildir. Yatırım tavsiyesi değildir."]
    md_text = "\n".join(md)
    (out / f"scan_{stamp}.md").write_text(md_text)
    print("\n" + md_text)
    print(f"\nRaporlar: {out}")


if __name__ == "__main__":
    main()
