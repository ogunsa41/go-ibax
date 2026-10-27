#!/usr/bin/env python3
"""Top 50 coin taraması.

1. CoinGecko'dan piyasa değerine göre sıralı listeyi alır; stablecoin, wrapped ve
   staked türevleri ele, Binance'te USDT paritesi olanlardan ilk N'i seçer
   (CoinGecko'ya ulaşılamazsa Binance 24s USDT hacmine göre sıralar).
2. Listeyi reports/top50_<tarih>.csv olarak kaydeder.
3. Her coin için agent.analyze ile bu haftanın bias'ını ve walk-forward backtestini
   hesaplar, tek bir özet tablo (Markdown + HTML + CSV) üretir.

Kullanım:
    python3 scan.py
    python3 scan.py --top 30 --long-only
"""
import argparse
import json
import math
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


def fmt_pct(x):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.1f}%"


def main():
    ap = argparse.ArgumentParser(description="Top N coin için haftalık bias taraması")
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--fee", type=float, default=0.001)
    ap.add_argument("--long-only", action="store_true")
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
            res = agent.analyze(daily, a.fee, a.long_only, a.min_train, a.min_active)
            weeks = len(res["w"])
            enough = weeks >= a.min_train + 26
            week = res["week"]
            ag, bh = res["agent_stats"], res["bh_stats"]
            row.update({
                "bias": res["signal"], "rule": res["rule"] or "-",
                "rule_sharpe": res["rule_sharpe"], "weeks": weeks,
                "agent_return": ag.get("total_return") if enough else None,
                "bh_return": bh.get("total_return") if enough else None,
                "agent_sharpe": ag.get("sharpe") if enough else None,
                "bh_sharpe": bh.get("sharpe") if enough else None,
                "agent_max_dd": ag.get("max_dd") if enough else None,
                "hit_rate": ag.get("hit_rate") if enough else None,
                "t_stat": ag.get("t_stat") if enough else None,
                "note": "" if enough else "yetersiz geçmiş, backtest yok",
            })
        except Exception as e:
            row.update({"bias": None, "note": f"hata: {e}"})
        results.append(row)
        b = BIAS_TXT.get(row.get("bias"), "?")
        print(f"{c.rank:>2}. {c.symbol:<8} {b:<9} {row.get('rule', '')} {row.get('note', '')}")
        time.sleep(0.2)

    df = pd.DataFrame(results)
    df.to_csv(out / f"scan_{stamp}.csv", index=False)

    counts = df["bias"].map(BIAS_TXT).value_counts()
    beat = df.dropna(subset=["agent_sharpe"])
    n_beat = int((beat["agent_sharpe"] > beat["bh_sharpe"]).sum())
    signif = int((beat["t_stat"].abs() >= 2).sum())
    md = [
        f"# Top {len(df)} Haftalık Bias Taraması — {week.date() if week is not None else stamp} haftası",
        "",
        f"Liste: {basis} (stablecoin/wrapped hariç, Binance USDT paritesi olanlar). "
        f"Maliyet {a.fee * 100:.2f}%/değişim, {'yalnız long' if a.long_only else 'long/short'}, "
        f"walk-forward ısınma {a.min_train} hafta.",
        "",
        "**Dağılım:** " + ", ".join(f"{k}: {v}" for k, v in counts.items()),
        "",
        f"Backtesti olan {len(beat)} coinin {n_beat} tanesinde ajan Sharpe'ı buy & hold'u geçti; "
        f"{signif} tanesinde |t-stat| ≥ 2.",
        "",
        "| # | Coin | Bu hafta | Kural | Kural Sharpe | Hafta | Ajan getiri | B&H getiri | "
        "Ajan Sharpe | B&H Sharpe | Ajan Max DD | İsabet | t-stat | Not |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        num = lambda k, f="{:.2f}": "—" if r.get(k) is None or pd.isna(r.get(k)) else f.format(r[k])
        md.append(
            f"| {r['rank']} | {r['symbol']} | {BIAS_TXT.get(r.get('bias'), '?')} | "
            f"`{r.get('rule', '-')}` | {num('rule_sharpe')} | {r.get('weeks', '—')} | "
            f"{fmt_pct(r.get('agent_return'))} | {fmt_pct(r.get('bh_return'))} | "
            f"{num('agent_sharpe')} | {num('bh_sharpe')} | {fmt_pct(r.get('agent_max_dd'))} | "
            f"{fmt_pct(r.get('hit_rate'))} | {num('t_stat')} | {r.get('note', '')} |")
    md += ["", "> Kural Sharpe = seçilen kuralın tüm geçmişteki Sharpe'ı (örneklem içi). "
           "Ajan/B&H sütunları walk-forward örneklem dışı sonuçlardır. |t-stat| < 2 anlamlı "
           "sayılmaz. Yatırım tavsiyesi değildir."]
    md_text = "\n".join(md)
    (out / f"scan_{stamp}.md").write_text(md_text)
    print("\n" + md_text)
    print(f"\nRaporlar: {out}")


if __name__ == "__main__":
    main()
