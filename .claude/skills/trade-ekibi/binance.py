#!/usr/bin/env python3
"""Binance mum verisi ve CRT tarayıcı — trade ekibi ajanlarının ortak aracı.

Kullanım:
  python3 binance.py mum  SEMBOL ARALIK [LIMIT] [--futures]
  python3 binance.py crt  SEMBOL ARALIK [LIMIT] [--futures] [--yon BULLISH|BEARISH]
                          [--max-range 6] [--max-sweep 3] [--son 12] [--hepsi]

SEMBOL Binance formatındadır (BTCUSDT). ARALIK: 1w, 1d, 4h, 1h, 15m, 5m ...
Çıktı JSON'dur; mumlar ESKİDEN YENİYE sıralıdır ve her mumda "kapali" alanı vardır.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

SPOT = ["https://api.binance.com", "https://data-api.binance.vision"]
FUTURES = ["https://fapi.binance.com"]


def klines(sembol, aralik, limit=200, futures=False):
    yol = "/fapi/v1/klines" if futures else "/api/v3/klines"
    sorgu = f"{yol}?symbol={sembol.upper()}&interval={aralik}&limit={min(limit, 1000)}"
    hata = None
    for host in FUTURES if futures else SPOT:
        try:
            with urllib.request.urlopen(host + sorgu, timeout=20) as r:
                ham = json.load(r)
            break
        except (urllib.error.URLError, OSError) as e:
            hata = e
    else:
        raise SystemExit(f"Binance verisi alınamadı ({sembol} {aralik}): {hata}")

    simdi_ms = int(time.time() * 1000)
    mumlar = []
    for k in ham:
        mumlar.append({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(k[0] / 1000)),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "kapali": k[6] < simdi_ms,
        })
    return mumlar


def crt_tara(mumlar, yon=None, max_range=6, max_sweep=3, son_n=None, hepsi=False):
    """Kapanmış mumlarda 2 mumlu, 3 mumlu ve çok mumlu CRT yapılarını bulur.

    Range: ardışık 1..max_range mumun en yüksek high / en düşük low'u.
    Sweep: range'den hemen sonraki 1..max_sweep mum. Bullish için ilk sweep mumu range low'unun
    altına iner, son sweep mumu range içinde kapanır, aradaki sweep mumları range low'unun altında
    kapanır (geri alınana kadar süren çok mumlu sweep). Bearish simetrik.
    son_n: sweep'i son N kapanmış mum içinde bitenler. hepsi=False ise GECERSIZ olanlar atılır.
    """
    kapali = [m for m in mumlar if m["kapali"]]
    sonuc = []
    for bas in range(len(kapali)):
        for r_uzun in range(1, max_range + 1):
            r_son = bas + r_uzun - 1
            if r_son >= len(kapali):
                break
            rng = kapali[bas:r_son + 1]
            r_high = max(m["high"] for m in rng)
            r_low = min(m["low"] for m in rng)
            for s_uzun in range(1, max_sweep + 1):
                s_son = r_son + s_uzun
                if s_son >= len(kapali):
                    break
                sw = kapali[r_son + 1:s_son + 1]
                son = sw[-1]
                icerde = r_low < son["close"] < r_high
                for y in ("BULLISH", "BEARISH"):
                    if yon and y != yon:
                        continue
                    if y == "BULLISH":
                        uc = min(m["low"] for m in sw)
                        if not (sw[0]["low"] < r_low and icerde
                                and all(m["close"] <= r_low for m in sw[:-1])):
                            continue
                        hedef, gecersiz = r_high, uc
                    else:
                        uc = max(m["high"] for m in sw)
                        if not (sw[0]["high"] > r_high and icerde
                                and all(m["close"] >= r_high for m in sw[:-1])):
                            continue
                        hedef, gecersiz = r_low, uc
                    if son_n and s_son < len(kapali) - son_n:
                        continue
                    sonra = kapali[s_son + 1:]
                    if any((m["close"] < gecersiz) if y == "BULLISH" else (m["close"] > gecersiz)
                           for m in sonra):
                        durum = "GECERSIZ"
                    elif any((m["high"] >= hedef) if y == "BULLISH" else (m["low"] <= hedef)
                             for m in sonra):
                        durum = "HEDEFE_ULASTI"
                    else:
                        durum = "AKTIF"
                    if durum == "GECERSIZ" and not hepsi:
                        continue
                    sonuc.append({
                        "yon": y,
                        "tip": f"{r_uzun + s_uzun}-mum" if r_uzun + s_uzun <= 3 else "cok-mum",
                        "range_mum_sayisi": r_uzun,
                        "sweep_mum_sayisi": s_uzun,
                        "range_bas": rng[0]["timestamp"],
                        "range_son": rng[-1]["timestamp"],
                        "range_high": r_high,
                        "range_low": r_low,
                        "sweep_bas": sw[0]["timestamp"],
                        "sweep_son": son["timestamp"],
                        "sweep_seviyesi": uc,
                        "crt_hedef": hedef,
                        "durum": durum,
                        "mum_sonrasi": len(sonra),
                    })
    return sonuc


def main():
    p = argparse.ArgumentParser()
    p.add_argument("komut", choices=["mum", "crt"])
    p.add_argument("sembol")
    p.add_argument("aralik")
    p.add_argument("limit", nargs="?", type=int, default=200)
    p.add_argument("--futures", action="store_true")
    p.add_argument("--yon", choices=["BULLISH", "BEARISH"])
    p.add_argument("--max-range", type=int, default=6)
    p.add_argument("--max-sweep", type=int, default=3)
    p.add_argument("--son", type=int, default=12, help="sweep son N kapanmış mumda bitmeli")
    p.add_argument("--hepsi", action="store_true", help="GECERSIZ yapıları da göster")
    a = p.parse_args()

    mumlar = klines(a.sembol, a.aralik, a.limit, a.futures)
    if a.komut == "mum":
        cikti = mumlar
    else:
        cikti = crt_tara(mumlar, a.yon, a.max_range, a.max_sweep, a.son, a.hepsi)
    json.dump(cikti, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main()
