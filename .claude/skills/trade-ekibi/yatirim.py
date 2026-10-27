#!/usr/bin/env python3
"""Ana yatırım sistemi: BTC trend takibi (spot, kaldıraçsız).

Kural: BTC günlük kapanışı 100 günlük ortalamanın (SMA100) üstündeyse BTC tutulur, altındaysa nakitte (USDT) kalınır.
Karar kapanmış günlük mumla verilir (Binance günü 00:00 UTC = 03:00 TR'de kapanır), ertesi gün uygulanır.
Komisyon: her alım ve satımda %0,1.

Neden bu sistem: 2018-2020, 2021-2023 ve 2024-2026 dönemlerinin üçünde de kârlı; ortalama uzunluğu 50-200 gün
arasında değişse de üç dönemde de kârlı (tek bir ayara bağlı değil); tek coin olduğu için hayatta kalan yanlılığı yok.
Risk: sistem de %36-60 düşüş yaşadı; kâr garantisi değildir.

Kullanım: python3 yatirim.py --out DOSYA.json
"""
import argparse
import calendar
import json
import os
import sys
import time
import urllib.request

GUN = 86_400_000
N = 100
KOM = 0.001
HOSTLAR = ["https://data-api.binance.vision", "https://api.binance.com"]
DONEMLER = [("2018-01-01", "2020-12-31"), ("2021-01-01", "2023-12-31"), ("2024-01-01", None)]


def gunluk(sembol="BTCUSDT", bas="2017-08-17"):
    t = calendar.timegm(time.strptime(bas, "%Y-%m-%d")) * 1000
    out = []
    while True:
        ham = None
        for h in HOSTLAR:
            try:
                with urllib.request.urlopen(f"{h}/api/v3/klines?symbol={sembol}&interval=1d&startTime={t}&limit=1000", timeout=30) as r:
                    ham = json.load(r)
                break
            except OSError:
                continue
        if ham is None:
            raise SystemExit("Binance günlük verisi alınamadı")
        out += [{"t": k[0], "c": float(k[4]), "kapali": k[6] < time.time() * 1000} for k in ham]
        if len(ham) < 1000:
            return [m for m in out if m["kapali"]]
        t = ham[-1][0] + GUN


def hesapla(D):
    """Her gün: kapanmış mumla karar, ertesi kapanışa kadar getiri. İşlem listesi ve günlük eğri döner."""
    c = [m["c"] for m in D]
    eq, btc, poz = 1.0, 1.0, False
    egri, islemler, acik = [], [], None
    for i in range(N, len(D) - 1):
        sma = sum(c[i - N + 1:i + 1]) / N
        yeni = c[i] > sma
        if yeni != poz:
            eq *= 1 - KOM
            if yeni:
                acik = {"giris": D[i]["t"], "girisFiyat": c[i]}
            else:
                islemler.append({**acik, "cikis": D[i]["t"], "cikisFiyat": c[i],
                                 "getiri": (c[i] / acik["girisFiyat"] * (1 - KOM) ** 2 - 1) * 100})
                acik = None
            poz = yeni
        r = c[i + 1] / c[i] - 1
        if poz:
            eq *= 1 + r
        btc *= 1 + r
        egri.append((D[i + 1]["t"], eq, btc))
    return egri, islemler, acik


def donem_ozet(egri, a, b):
    ta = calendar.timegm(time.strptime(a, "%Y-%m-%d")) * 1000
    tb = calendar.timegm(time.strptime(b, "%Y-%m-%d")) * 1000 if b else egri[-1][0]
    E = [x for x in egri if ta <= x[0] <= tb]
    if len(E) < 2:
        return None

    def dd(k):
        tepe, en = 0, 0
        for x in E:
            tepe = max(tepe, x[k]); en = min(en, x[k] / tepe - 1)
        return en * 100
    return {"bas": a, "bit": b or time.strftime("%Y-%m-%d", time.gmtime(tb / 1000)),
            "sistem": (E[-1][1] / E[0][1] - 1) * 100, "btc": (E[-1][2] / E[0][2] - 1) * 100,
            "sistemDD": dd(1), "btcDD": dd(2)}


def uret():
    D = gunluk()
    egri, islemler, acik = hesapla(D)
    c = [m["c"] for m in D]
    sma = sum(c[-N:]) / N
    al = c[-1] > sma
    # sinyalin kaç gündür sürdüğü
    gun = 0
    for i in range(len(D) - 1, N - 1, -1):
        if (c[i] > sum(c[i - N + 1:i + 1]) / N) != al:
            break
        gun += 1
    haftalik = [x for i, x in enumerate(egri) if i % 7 == 0 or i == len(egri) - 1]
    return {
        "zaman": int(time.time() * 1000),
        "kural": f"BTC günlük kapanışı {N} günlük ortalamanın üstündeyse BTC tut, altındaysa nakitte kal (spot, kaldıraçsız).",
        "sinyal": {"durum": "AL" if al else "NAKIT", "kapanis": c[-1], "kapanisT": D[-1]["t"], "sma": sma,
                   "fark": (c[-1] / sma - 1) * 100, "gun": gun},
        "acik": acik and {**acik, "getiri": (c[-1] / acik["girisFiyat"] * (1 - KOM) - 1) * 100},
        "islemler": islemler[-12:][::-1],
        "islemOzet": {"adet": len(islemler), "kazanan": sum(1 for x in islemler if x["getiri"] > 0),
                      "ortKazanc": sum(x["getiri"] for x in islemler if x["getiri"] > 0) / max(1, sum(1 for x in islemler if x["getiri"] > 0)),
                      "ortKayip": sum(x["getiri"] for x in islemler if x["getiri"] <= 0) / max(1, sum(1 for x in islemler if x["getiri"] <= 0))},
        "donemler": [d for d in (donem_ozet(egri, a, b) for a, b in DONEMLER) if d],
        "egri": [[t, round(e, 4), round(b, 4)] for t, e, b in haftalik],
        "fiyat": [[m["t"], m["c"]] for m in D[-200:]],
        "smaSeri": [[D[i]["t"], sum(c[i - N + 1:i + 1]) / N] for i in range(len(D) - 200, len(D))],
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    a = p.parse_args()
    d = uret()
    with open(a.out, "w") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
    s = d["sinyal"]
    print(json.dumps({"sinyal": s["durum"], "kapanis": s["kapanis"], "sma100": round(s["sma"], 2), "fark%": round(s["fark"], 2),
                      "gun": s["gun"], "donemler": [(x["bas"], round(x["sistem"]), round(x["btc"])) for x in d["donemler"]],
                      "boyut": os.path.getsize(a.out)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
