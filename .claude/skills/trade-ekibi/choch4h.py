#!/usr/bin/env python3
"""4H karakter değişimi (CHoCH) ajanının kuralları, backtest'i ve kağıt (sanal) işlem takibi.

Tanım (yön bağımsız, haftalık bias kullanılmaz):
  - Swing: 4H'de sol ve sağında 2'şer mumdan yüksek (high) / düşük (low) mum; sağdaki 2 mum
    kapandığında onaylanır.
  - Bullish CHoCH: o ana kadar onaylanmış son iki swing high ve son iki swing low düşüyorsa (LH-LL),
    bir 4H mumun GÖVDESİ son swing high'ın üstünde kapanır (önceki mum altında kapanmıştır).
  - Bearish CHoCH: simetrik (HH-HL yapısında son swing low'un altında gövde kapanışı).
  - İşlem: kırılım mumunun kapanışında piyasa girişi. Stop: son swing'den kırılıma kadarki en uç
    nokta (bullish → en düşük low) + küçük tampon. Hedef: sabit R katları.

Kullanım:
  python3 choch4h.py backtest --egitim DIZIN --kontrol DIZIN      (backtest.py önbelleğini kullanır)
  python3 choch4h.py tara --top 50 --out DIZIN [--kagit kagit.json]
"""
import argparse
import bisect
import calendar
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as sc  # noqa: E402
import backtest as bt  # noqa: E402

H4, DK15, GUN = 4 * 3_600_000, 900_000, 86_400_000
N = 2
HEDEFLER = ("1.5R", "2R", "3R")
KOMISYON = 0.002


def olaylar(c4):
    """Kapanmış 4H mumlarında tüm CHoCH olaylarını sırayla döndürür."""
    hs, ls = [], []  # onaylanmış swing indeksleri
    out = []
    for j in range(len(c4)):
        k = j - N  # j anında onaylanan aday swing (sağında N mum var: k+1..j)
        if k >= N:
            if all(c4[k]["h"] > c4[k - d]["h"] and c4[k]["h"] > c4[k + d]["h"] for d in range(1, N + 1)):
                hs.append(k)
            if all(c4[k]["l"] < c4[k - d]["l"] and c4[k]["l"] < c4[k + d]["l"] for d in range(1, N + 1)):
                ls.append(k)
        if len(hs) < 2 or len(ls) < 2 or j == 0:
            continue
        h1, h2, l1, l2 = c4[hs[-2]]["h"], c4[hs[-1]]["h"], c4[ls[-2]]["l"], c4[ls[-1]]["l"]
        m, onceki = c4[j], c4[j - 1]
        if h2 < h1 and l2 < l1 and m["c"] > h2 >= onceki["c"]:
            uc = min(x["l"] for x in c4[hs[-1]:j + 1])
            out.append({"j": j, "t": m["t"], "yon": "LONG", "seviye": h2, "giris": m["c"], "uc": uc})
        elif h2 > h1 and l2 > l1 and m["c"] < l2 <= onceki["c"]:
            uc = max(x["h"] for x in c4[ls[-1]:j + 1])
            out.append({"j": j, "t": m["t"], "yon": "SHORT", "seviye": l2, "giris": m["c"], "uc": uc})
    return out


def plan(o):
    long = o["yon"] == "LONG"
    tampon = abs(o["giris"] - o["uc"]) * 0.1
    sl = o["uc"] - tampon if long else o["uc"] + tampon
    risk = abs(o["giris"] - sl)
    y = 1 if long else -1
    return {**o, "sl": sl, "risk%": risk / o["giris"] * 100,
            "tp": {k: o["giris"] + float(k[:-1]) * y * risk for k in HEDEFLER}}


# ---------- backtest ----------
def coin_bt(args):
    sembol, bas, bit, cache = args
    try:
        c4 = bt.aralik_cek(sembol, "4h", bas - 60 * GUN, bit, cache)
        m15 = bt.aralik_cek(sembol, "15m", bas - 11 * GUN, bit, cache)
    except SystemExit as e:
        return {"s": sembol, "hata": str(e), "isler": []}
    m15t = [m["t"] for m in m15]
    isler = []
    for o in olaylar(c4):
        T = o["t"] + H4
        if not (bas <= T <= bit):
            continue
        p = plan(o)
        long = p["yon"] == "LONG"
        hedefler = {k: p["tp"][k] for k in HEDEFLER}
        R, cikis, tamam = bt.cikislar(long, p["giris"], p["sl"], hedefler, m15, bisect.bisect_left(m15t, T))
        isler.append({"s": sembol, "T": T, "yon": p["yon"], "risk%": p["risk%"],
                      "maliyet": KOMISYON * p["giris"] / abs(p["giris"] - p["sl"]),
                      "durum": "KAPANDI" if tamam else "ACIK", "R": R})
    return {"s": sembol, "isler": isler}


def ozet(isler, hedef, max_risk=None):
    R = [x["R"][hedef] - x["maliyet"] for x in isler
         if x["durum"] == "KAPANDI" and (max_risk is None or x["risk%"] <= max_risk)]
    n = len(R)
    if not n:
        return {"islem": 0}
    ort = sum(R) / n
    sd = math.sqrt(sum((r - ort) ** 2 for r in R) / (n - 1)) if n > 1 else 0
    kaz, kay = sum(r for r in R if r > 0), -sum(r for r in R if r < 0)
    return {"islem": n, "isabet": round(sum(r > 0 for r in R) / n * 100, 1), "netR": round(sum(R), 1),
            "ortR": round(ort, 3), "t": round(ort / (sd / math.sqrt(n)), 2) if sd else 0,
            "pf": round(kaz / kay, 2) if kay else None}


def backtest(a):
    simdi = calendar.timegm(time.strptime(a.simdi, "%Y-%m-%d")) * 1000
    with open(a.semboller) as f:
        semboller = f.read().split()
    sonuc = {}
    for ad, (b_gun, s_gun) in {"egitim": (90, 0), "kontrol": (180, 90)}.items():
        bas, bit = simdi - b_gun * GUN, simdi - s_gun * GUN
        with ProcessPoolExecutor(4) as ex:
            r = list(ex.map(coin_bt, [(s, bas, bit, a.cache) for s in semboller]))
        isler = [x for c in r for x in c["isler"]]
        sonuc[ad] = {"donem": f'{time.strftime("%Y-%m-%d", time.gmtime(bas / 1000))} – {time.strftime("%Y-%m-%d", time.gmtime(bit / 1000))}',
                     "sinyal": len(isler),
                     "hedefler": {h: ozet(isler, h) for h in [*HEDEFLER, *(k + "_BE" for k in HEDEFLER)]},
                     "risk<=3%": {h: ozet(isler, h, 3.0) for h in HEDEFLER},
                     "long_2R": ozet([x for x in isler if x["yon"] == "LONG"], "2R"),
                     "short_2R": ozet([x for x in isler if x["yon"] == "SHORT"], "2R")}
    print(json.dumps(sonuc, ensure_ascii=False, indent=1))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(sonuc, f, ensure_ascii=False, indent=1)


# ---------- canlı tarama ve kağıt işlem ----------
def coin_canli(sembol):
    c4 = [m for m in sc.mumlar(sembol, "4h", 300) if m["closed"]]
    ol = olaylar(c4)
    son = [plan(o) for o in ol if o["j"] >= len(c4) - 6]  # son 24 saatteki kırılımlar
    m15 = sc.mumlar(sembol, "15m", 200)
    return {"s": sembol, "fiyat": m15[-1]["c"], "sinyaller": son, "son4h": c4[-1]["t"],
            "m15": [m for m in m15 if m["closed"]]}


def kagit_guncelle(kagit, veriler, hedef="2R"):
    """Açık sanal işlemleri 15m mumlarla kapatır; yeni kırılımlar için sanal işlem açar."""
    simdi = int(time.time() * 1000)
    acik = [x for x in kagit["islemler"] if x["durum"] == "ACIK"]
    for x in acik:
        v = veriler.get(x["s"])
        if not v:
            continue
        long = x["yon"] == "LONG"
        for m in v["m15"]:
            if m["t"] < x["acilis"]:
                continue
            stop = (m["l"] <= x["sl"]) if long else (m["h"] >= x["sl"])
            tp = (m["h"] >= x["tp"]) if long else (m["l"] <= x["tp"])
            if stop or tp:
                r = -1.0 if stop else float(hedef[:-1])
                x.update(durum="KAPANDI", sonuc="STOP" if stop else "HEDEF", kapanis=m["t"],
                         R=round(r - x["maliyet"], 3))
                break
    mevcut = {(x["s"], x["sinyal_t"]) for x in kagit["islemler"]}
    yeni = []
    for v in veriler.values():
        for p in v["sinyaller"]:
            # yalnızca son kapanan 4H mumdaki kırılım (gerçek zamanlı yakalanan) işleme dönüşür
            if p["t"] != v["son4h"] or (v["s"], p["t"]) in mevcut:
                continue
            if any(x["s"] == v["s"] and x["durum"] == "ACIK" for x in kagit["islemler"]):
                continue
            islem = {"s": v["s"], "yon": p["yon"], "sinyal_t": p["t"], "acilis": p["t"] + H4,
                     "giris": p["giris"], "sl": p["sl"], "tp": p["tp"][hedef], "hedef": hedef,
                     "maliyet": round(KOMISYON * p["giris"] / abs(p["giris"] - p["sl"]), 4),
                     "risk%": round(p["risk%"], 3), "durum": "ACIK"}
            kagit["islemler"].append(islem)
            yeni.append(islem)
    kapali = [x for x in kagit["islemler"] if x["durum"] == "KAPANDI"]
    kagit["ozet"] = {"acik": sum(x["durum"] == "ACIK" for x in kagit["islemler"]), "kapali": len(kapali),
                     "kazanan": sum(x["R"] > 0 for x in kapali), "netR": round(sum(x["R"] for x in kapali), 2),
                     "guncelleme": simdi}
    return yeni


def tara(a):
    os.makedirs(a.out, exist_ok=True)
    semboller = [s for s, _ in sc.top_semboller(a.top)]
    with ThreadPoolExecutor(8) as ex:
        veriler = {}
        for s, r in zip(semboller, ex.map(lambda s: _guvenli(coin_canli, s), semboller)):
            if r:
                veriler[s] = r
    kagit = {"islemler": []}
    if a.kagit and os.path.exists(a.kagit):
        with open(a.kagit) as f:
            d = json.load(f)
        kagit = d.get("kagit", d) if isinstance(d, dict) else kagit
        kagit.setdefault("islemler", [])
    yeni = kagit_guncelle(kagit, veriler)
    sinyal = []
    for v in veriler.values():
        for p in v["sinyaller"]:
            sinyal.append({"s": v["s"], "yon": p["yon"], "t": p["t"], "seviye": p["seviye"], "giris": p["giris"],
                           "sl": p["sl"], "tp2R": p["tp"]["2R"], "risk%": round(p["risk%"], 3), "fiyat": v["fiyat"]})
    sinyal.sort(key=lambda x: -x["t"])
    doc = {"zaman": int(time.time() * 1000), "sinyaller": sinyal[:60], "kagit": kagit}
    with open(os.path.join(a.out, "choch4h.json"), "w") as f:
        json.dump(sc.temizle(doc), f, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps({"sinyal": len(sinyal), "yeniIslem": [(x["s"], x["yon"]) for x in yeni],
                      "kagitOzet": kagit["ozet"]}, ensure_ascii=False))


def _guvenli(f, s):
    try:
        return f(s)
    except Exception:  # noqa: BLE001 — tek coin hatası taramayı durdurmasın
        return None


def main():
    p = argparse.ArgumentParser()
    alt = p.add_subparsers(dest="komut", required=True)
    b = alt.add_parser("backtest")
    b.add_argument("--simdi", default="2026-10-24")
    b.add_argument("--semboller", required=True)
    b.add_argument("--cache", required=True)
    b.add_argument("--out")
    t = alt.add_parser("tara")
    t.add_argument("--top", type=int, default=50)
    t.add_argument("--out", default="tarama")
    t.add_argument("--kagit", help="önceki kağıt işlem durumu (choch4h.json)")
    a = p.parse_args()
    backtest(a) if a.komut == "backtest" else tara(a)


if __name__ == "__main__":
    main()
