#!/usr/bin/env python3
"""Tarayıcı kurallarının geriye dönük testi (walk-forward).

Her coin için geçmişte 15 dakikalık adımlarla ilerler; o ana kadar KAPANMIŞ mumlarla scanner.py zincirini
(bias → key level → CRT → CHoCH → giriş → kontrol) çalıştırır. "FVG bekleniyor" sinyali ilk kez
göründüğünde limit emri kurar ve sonrasını 15m mumlarla simüle eder.

Simülasyon kuralları (muhafazakâr):
  - Emir: giriş = bölgenin ilk değilen kenarı (limit). 48 saat içinde dolmazsa iptal.
  - Dolmadan önce: 15m kapanış bölgenin ötesine geçerse veya fiyat TP2'ye ulaşırsa iptal.
  - Dolum mumunda stop da görüldüyse kayıp sayılır.
  - Dolumdan sonra stop ve TP2 aynı 15m mumunda görülürse kayıp sayılır.
  - Komisyon / kayma yok (sonuçlar R cinsinden).

Kullanım:
  python3 backtest.py --gun 60 --top 50 [--sembol BTCUSDT ...] [--out DIZIN]
"""
import argparse
import bisect
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as sc  # noqa: E402

DK15, SAAT, H4, HAFTA = 900_000, 3_600_000, 4 * 3_600_000, 7 * 86_400_000
ADIM = DK15  # canlı tarama gibi 15 dakikada bir
SURE = {"15m": DK15, "4h": H4, "1w": HAFTA}
EMIR_OMRU = 48 * SAAT


def aralik_cek(sembol, aralik, bas, bit, cache):
    yol = os.path.join(cache, f"{sembol}_{aralik}_{bas}_{bit}.json")
    if os.path.exists(yol):
        with open(yol) as f:
            return json.load(f)
    out, t = [], bas
    while t < bit:
        ham = sc.get_json(f"/api/v3/klines?symbol={sembol}&interval={aralik}&startTime={t}&endTime={bit}&limit=1000")
        if not ham:
            break
        out += [{"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]), "closed": True} for k in ham]
        t = ham[-1][0] + SURE[aralik]
        if len(ham) < 1000:
            break
    with open(yol, "w") as f:
        json.dump(out, f)
    return out


def kapanmis(seri, zamanlar, T, n, sure):
    # T anında kapanmış (açılış + süre <= T) son n mum
    j = bisect.bisect_right(zamanlar, T - sure)
    return seri[max(0, j - n):j]


def simule(plan, m15, m15t, T):
    long = plan["yon"] == "LONG"
    giris, sl = plan["giris"], plan["sl"]
    tp2 = next(t["seviye"] for t in plan["tps"] if t["ad"] == "TP2")
    rr2 = abs(tp2 - giris) / abs(giris - sl)
    hedef2r = giris + 2 * (giris - sl)
    zone = plan["zone"]
    i = bisect.bisect_left(m15t, T)
    dolum = None
    sonuc = {"rr2": rr2}
    while i < len(m15):
        m = m15[i]
        if dolum is None:
            if m["t"] - T > EMIR_OMRU:
                return {**sonuc, "durum": "SURESI_DOLDU"}
            if (m["l"] <= giris) if long else (m["h"] >= giris):
                dolum = m["t"]
                if (m["l"] <= sl) if long else (m["h"] >= sl):
                    return {**sonuc, "durum": "KAYIP", "R": -1.0, "R2": -1.0, "dolum": dolum, "cikis": m["t"]}
                i += 1
                break
            if (m["h"] >= tp2) if long else (m["l"] <= tp2):
                return {**sonuc, "durum": "KACIRILDI"}
            if (m["c"] < zone["alt"]) if long else (m["c"] > zone["ust"]):
                return {**sonuc, "durum": "IPTAL_BOLGE"}
        i += 1
    if dolum is None:
        return {**sonuc, "durum": "ACIK_EMIR"}
    # dolum sonrası: TP2 hedefli ana strateji ve sabit 2R hedefli karşılaştırma
    r_ana = r_2r = None
    cikis = None
    while i < len(m15):
        m = m15[i]
        stop = (m["l"] <= sl) if long else (m["h"] >= sl)
        t2 = (m["h"] >= tp2) if long else (m["l"] <= tp2)
        t2r = (m["h"] >= hedef2r) if long else (m["l"] <= hedef2r)
        if r_2r is None and (stop or t2r):
            r_2r = -1.0 if stop else 2.0
        if r_ana is None and (stop or t2):
            r_ana = -1.0 if stop else rr2
            cikis = m["t"]
        if r_ana is not None and r_2r is not None:
            break
        i += 1
    if r_ana is None:
        return {**sonuc, "durum": "ACIK_POZISYON", "dolum": dolum, "R2": r_2r}
    return {**sonuc, "durum": "KAZANC" if r_ana > 0 else "KAYIP", "R": r_ana, "R2": r_2r, "dolum": dolum, "cikis": cikis}


def coin_test(args):
    sembol, bas, bit, cache = args
    try:
        W = aralik_cek(sembol, "1w", bas - 70 * HAFTA, bit, cache)
        c4 = aralik_cek(sembol, "4h", bas - 60 * 86_400_000, bit, cache)
        m15 = aralik_cek(sembol, "15m", bas - 11 * 86_400_000, bit, cache)
    except SystemExit as e:
        return {"s": sembol, "hata": str(e)}
    Wt, c4t, m15t = [m["t"] for m in W], [m["t"] for m in c4], [m["t"] for m in m15]
    sayac, isler, gorulen = {}, [], set()
    T = (bas // ADIM) * ADIM
    while T <= bit:
        w = kapanmis(W, Wt, T, 60, HAFTA)
        k4 = kapanmis(c4, c4t, T, 300, H4)
        k15 = kapanmis(m15, m15t, T, 1000, DK15)
        T += ADIM
        if len(w) < 4 or len(k4) < 50 or len(k15) < 100:
            continue
        price = k15[-1]["c"]
        Tn = T - ADIM
        bias = sc.ajan_bias(w, price)
        kl = sc.ajan_kl(k4, w, bias["bias"], price, now_ms=Tn)
        crt = sc.ajan_crt(k4, bias["bias"], kl)
        ch = sc.ajan_choch(k15, crt)
        en = sc.ajan_giris(ch, crt, bias, price)
        qa = sc.ajan_kontrol(w, k4, bias, kl, crt, ch, en, price)
        kat = sc.sinifla(bias, kl, crt, ch, en, qa)
        sayac[kat] = sayac.get(kat, 0) + 1
        if kat not in ("FVG_BEKLENIYOR", "TETIKLENDI"):
            continue
        anahtar = (crt["aktif"]["sEnd"], en["zone"]["t"])
        if anahtar in gorulen:
            continue
        gorulen.add(anahtar)
        if kat == "TETIKLENDI":
            isler.append({"s": sembol, "T": Tn, "yon": en["yon"], "durum": "GEC_TESPIT"})
            continue
        sim = simule(en, m15, m15t, Tn)
        isler.append({"s": sembol, "T": Tn, "yon": en["yon"], "giris": en["giris"], "sl": en["sl"],
                      "crtTip": crt["aktif"]["tip"], "guven": bias["guven"], "zone": en["zone"]["tip"], **sim})
    return {"s": sembol, "sayac": sayac, "isler": isler}


def ozetle(isler):
    kapali = [x for x in isler if x["durum"] in ("KAZANC", "KAYIP")]
    R = [x["R"] for x in kapali]
    kaz = [r for r in R if r > 0]
    kay = [r for r in R if r < 0]
    egri, tepe, dd = 0.0, 0.0, 0.0
    for x in sorted(kapali, key=lambda x: x["cikis"]):
        egri += x["R"]; tepe = max(tepe, egri); dd = min(dd, egri - tepe)
    r2 = [x["R2"] for x in isler if x.get("R2") is not None]
    return {
        "islem": len(kapali), "kazanc": len(kaz), "kayip": len(kay),
        "isabet": round(len(kaz) / len(kapali) * 100, 1) if kapali else None,
        "toplamR": round(sum(R), 2), "ortR": round(sum(R) / len(R), 3) if R else None,
        "profitFactor": round(sum(kaz) / -sum(kay), 2) if kay else None,
        "maxDrawdownR": round(dd, 2),
        "ortHedefRR": round(sum(x["rr2"] for x in kapali) / len(kapali), 2) if kapali else None,
        "sabit2R": {"islem": len(r2), "toplamR": round(sum(r2), 2),
                    "isabet": round(sum(1 for r in r2 if r > 0) / len(r2) * 100, 1) if r2 else None},
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--gun", type=int, default=60)
    p.add_argument("--top", type=int, default=50)
    p.add_argument("--sembol", nargs="*")
    p.add_argument("--out", default="backtest")
    a = p.parse_args()
    os.makedirs(a.out, exist_ok=True)
    cache = os.path.join(a.out, "cache")
    os.makedirs(cache, exist_ok=True)
    bit = (int(time.time() * 1000) // DK15) * DK15
    bas = bit - a.gun * 86_400_000
    semboller = a.sembol or [s for s, _ in sc.top_semboller(a.top)]
    t0 = time.time()
    with ProcessPoolExecutor(4) as ex:
        sonuc = list(ex.map(coin_test, [(s, bas, bit, cache) for s in semboller]))
    isler = [x for r in sonuc for x in r.get("isler", [])]
    akis = {}
    for r in sonuc:
        for k, v in r.get("sayac", {}).items():
            akis[k] = akis.get(k, 0) + v
    durumlar = {}
    for x in isler:
        durumlar[x["durum"]] = durumlar.get(x["durum"], 0) + 1
    coin = {}
    for x in isler:
        if x["durum"] in ("KAZANC", "KAYIP"):
            c = coin.setdefault(x["s"], {"islem": 0, "R": 0.0})
            c["islem"] += 1; c["R"] = round(c["R"] + x["R"], 2)
    rapor = {
        "donem": {"bas": time.strftime("%Y-%m-%d %H:%M", time.gmtime(bas / 1000)),
                  "bit": time.strftime("%Y-%m-%d %H:%M", time.gmtime(bit / 1000)), "gun": a.gun},
        "coin": len(semboller), "sure_sn": round(time.time() - t0),
        "hatalar": [r for r in sonuc if r.get("hata")],
        "sinyal": len(isler), "sinyalSonuclari": durumlar,
        "genel": ozetle(isler),
        "long": ozetle([x for x in isler if x["yon"] == "LONG"]),
        "short": ozetle([x for x in isler if x["yon"] == "SHORT"]),
        "guvenYuksekOrta": ozetle([x for x in isler if x.get("guven") in ("YUKSEK", "ORTA")]),
        "guvenDusuk": ozetle([x for x in isler if x.get("guven") == "DUSUK"]),
        "coinBazinda": dict(sorted(coin.items(), key=lambda kv: -kv[1]["R"])),
        "saatlikDurumDagilimi": akis,
    }
    with open(os.path.join(a.out, "rapor.json"), "w") as f:
        json.dump(rapor, f, ensure_ascii=False, indent=1)
    with open(os.path.join(a.out, "islemler.json"), "w") as f:
        json.dump(isler, f, ensure_ascii=False, indent=1)
    print(json.dumps(rapor, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
