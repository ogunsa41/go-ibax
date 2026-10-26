#!/usr/bin/env python3
"""Videodaki CRT modeli (günlük CRT → 1 saatlik karakter değişimi), backtest.

Kurallar (videolardan):
1. Bias — mum kapanışı: kapanmış hafta, önceki haftanın high'ı üstünde kapandıysa BULLISH, low'u altında
   kapandıysa BEARISH. High'ı alıp üstünde kapatamadıysa BEARISH, low'u alıp altında kapatamadıysa BULLISH
   (trend değişimi). İçeride kalan hafta önceki yönü korur.
2. Key level: günlük ve haftalık FVG / likidite / PDH-PDL / PWH-PWL (CRT ile aynı ya da daha yüksek zaman dilimi),
   manipülasyondan ÖNCEKİ verilerle.
3. CRT: range = önceki günlük mumlardan biri (araya giren mumlar iki ucunu da almamış olmalı, en fazla 10 gün geri).
   Manipülasyon = bias yönünde range ucunun alınması ve bu sırada bir key level'a değilmesi. Hedef = karşı uç.
4. Karakter değişimi (1 saatlik, günlük kapanış beklenmez): manipülasyon ucuna giden son hareketin başladığı 1 saatlik
   swing mumunun GÖVDESİ ötesinde 1 saatlik kapanış.
5. Giriş: "piyasa" = karakter değişimi mumunun kapanışı; "bolge" = karakter değişimi seviyesine geri dönüşte limit.
6. Stop: manipülasyon ucunun %0,1 ötesi. Hedef: range'in karşı ucu. "BE": fiyat range'in %50'sine gelince stop girişe.
7. SMT (filtre): BTC için ETH, diğerleri için BTC kendi önceki günlük mumunun aynı ucunu o ana kadar ALMAMIŞ olmalı.

Kullanım: python3 crt_gunluk.py --simdi 2026-10-24 --semboller top50.txt --cache DIZIN --out sonuc.json
"""
import argparse, bisect, calendar, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt
import scanner as sc

SAAT, GUN, HAFTA = bt.SAAT, bt.GUN, bt.HAFTA
KOM = bt.KOMISYON
GERI = 10           # range en fazla kaç gün geri
CHOCH_SURE = 36     # manipülasyon başladıktan sonra karakter değişimi için en fazla saat
EMIR_SURE = 24      # bölge girişinde limit emrin ömrü (saat)


def bias_kapanis(W):
    """Kapanmış haftalar (eskiden yeniye) → son haftadan sonraki hafta için bias."""
    yon = "NEUTRAL"
    for i in range(1, len(W)):
        p, m = W[i - 1], W[i]
        if m["c"] > p["h"]:
            yon = "BULLISH"
        elif m["c"] < p["l"]:
            yon = "BEARISH"
        elif m["h"] > p["h"] and m["l"] >= p["l"]:
            yon = "BEARISH"      # tepeyi aldı, üstünde kapatamadı
        elif m["l"] < p["l"] and m["h"] <= p["h"]:
            yon = "BULLISH"      # dibi aldı, altında kapatamadı
        elif m["h"] > p["h"] and m["l"] < p["l"]:
            yon = "BULLISH" if m["c"] > m["o"] else "BEARISH"
    return yon


def keylevels(D, W, j, bias):
    """Gün j açılmadan önce bilinen günlük + haftalık key level'lar."""
    if j < 30:
        return []
    price = D[j - 1]["c"]
    Wk = [m for m in W if m["t"] + HAFTA <= D[j]["t"]]
    sv = sc.ajan_kl(D[max(0, j - 120):j], Wk[-60:], bias, price, now_ms=D[j]["t"])["seviyeler"]
    if len(Wk) > 10:
        sv += sc.ajan_kl(Wk[-60:], Wk[-60:], bias, price, now_ms=D[j]["t"])["seviyeler"]
    return sv


def swing_govde(H, a, b, bull):
    """[a, b) aralığında manipülasyon ucuna giden son hareketin başladığı swing (n=2) ve gövde seviyesi."""
    for i in range(b - 3, max(a, 2) - 1, -1):
        if i + 2 >= len(H):
            continue
        if bull and all(H[i]["h"] > H[i + d]["h"] and H[i]["h"] > H[i - d]["h"] for d in (1, 2)):
            return i, max(H[i]["o"], H[i]["c"])
        if not bull and all(H[i]["l"] < H[i + d]["l"] and H[i]["l"] < H[i - d]["l"] for d in (1, 2)):
            return i, min(H[i]["o"], H[i]["c"])
    return None, None


def sim(H, i, long, giris, sl, hedef, be_seviye):
    """1 saatlik mumlarla çıkış: aynı mumda stop ve hedef → stop. Dönen: (R, R_BE, çıkış zamanı)."""
    risk = abs(giris - sl)
    r = r_be = None
    be = False
    while i < len(H) and (r is None or r_be is None):
        m = H[i]
        stop = m["l"] <= sl if long else m["h"] >= sl
        hit = m["h"] >= hedef if long else m["l"] <= hedef
        be_stop = be and (m["l"] <= giris if long else m["h"] >= giris)
        if r is None:
            r = -1.0 if stop else abs(hedef - giris) / risk if hit else None
        if r_be is None:
            r_be = -1.0 if stop else 0.0 if be_stop else abs(hedef - giris) / risk if hit else None
        if not be and not stop and (m["h"] >= be_seviye if long else m["l"] <= be_seviye):
            be = True
        i += 1
    return r, r_be, H[min(i, len(H)) - 1]["t"]


def coin(args):
    s, bas, bit, cache, kor = args
    try:
        W = bt.aralik_cek(s, "1w", bas - 120 * HAFTA, bit, cache)
        D = bt.aralik_cek(s, "1d", bas - 200 * GUN, bit, cache)
        H = bt.aralik_cek(s, "1h", bas - 2 * GUN, bit + 10 * GUN, cache)
        K = bt.aralik_cek(kor, "1h", bas - 2 * GUN, bit + 10 * GUN, cache)
        KD = bt.aralik_cek(kor, "1d", bas - 200 * GUN, bit, cache)
    except SystemExit as e:
        return {"s": s, "hata": str(e)}
    Ht = [m["t"] for m in H]; Kt = [m["t"] for m in K]; KDt = [m["t"] for m in KD]
    isler, sayac = [], {}
    for j in range(len(D)):
        g0 = D[j]["t"]
        if g0 < bas or g0 >= bit:
            continue
        Wk = [m for m in W if m["t"] + HAFTA <= g0]
        bias = bias_kapanis(Wk[-30:])
        if bias == "NEUTRAL":
            continue
        bull = bias == "BULLISH"
        # range: en yakın, araya giren mumların iki ucunu da almadığı önceki günlük mum
        R = None
        for k in range(j - 1, max(0, j - GERI) - 1, -1):
            ara = D[k + 1:j]
            if all(D[k]["l"] <= m["l"] and m["h"] <= D[k]["h"] for m in ara):
                R = D[k]
                break  # en yakın geçerli range
        if R is None:
            continue
        rH, rL = R["h"], R["l"]
        a = bisect.bisect_left(Ht, g0)
        son = bisect.bisect_left(Ht, g0 + GUN)
        # manipülasyon: gün j içinde range ucunun alındığı ilk saat
        s0 = next((i for i in range(a, son) if (H[i]["l"] < rL if bull else H[i]["h"] > rH)), None)
        if s0 is None:
            continue
        sayac["manipulasyon"] = sayac.get("manipulasyon", 0) + 1
        # aynı gün karşı uç önceden alınmışsa range zaten hedefe gitmiş
        if any((H[i]["h"] >= rH if bull else H[i]["l"] <= rL) for i in range(a, s0 + 1)):
            continue
        kl = keylevels(D, W, j, bias)
        ch = None
        uc_i = s0
        for i in range(s0, min(len(H), s0 + CHOCH_SURE)):
            m = H[i]
            if (m["h"] >= rH) if bull else (m["l"] <= rL):
                break   # karakter değişimi gelmeden hedef
            if (m["l"] < H[uc_i]["l"]) if bull else (m["h"] > H[uc_i]["h"]):
                uc_i = i
                continue
            o_i, lvl = swing_govde(H, max(a - 48, 0), uc_i + 1, bull)
            if lvl is not None and (m["c"] > lvl if bull else m["c"] < lvl) and i > uc_i:
                ch = (i, lvl, o_i)
                break
        if ch is None:
            continue
        sayac["choch"] = sayac.get("choch", 0) + 1
        uc = H[uc_i]["l"] if bull else H[uc_i]["h"]
        man_ust = max(m["h"] for m in H[s0:uc_i + 1]); man_alt = min(m["l"] for m in H[s0:uc_i + 1])
        z = next((v for v in kl if man_alt <= v["ust"] and man_ust >= v["alt"]), None)
        if z is None:
            continue
        sayac["keylevel"] = sayac.get("keylevel", 0) + 1
        # SMT: korele parite aynı anda kendi önceki günlük mumunun aynı ucunu almamış mı
        kj = bisect.bisect_left(KDt, g0)
        smt = None
        if 0 < kj < len(KD) and KD[kj]["t"] == g0:
            kR = KD[kj - 1]
            ka, kb = bisect.bisect_left(Kt, g0), bisect.bisect_right(Kt, H[ch[0]]["t"])
            aldi = any((K[x]["l"] < kR["l"]) if bull else (K[x]["h"] > kR["h"]) for x in range(ka, kb))
            smt = not aldi
        i, lvl, _ = ch
        sl = uc * (0.999 if bull else 1.001)
        be_sev = (rH + rL) / 2
        for tip in ("piyasa", "bolge"):
            if tip == "piyasa":
                giris, gi = H[i]["c"], i + 1
            else:
                giris, gi = lvl, None
                for x in range(i + 1, min(len(H), i + 1 + EMIR_SURE)):
                    m = H[x]
                    if (m["h"] >= rH) if bull else (m["l"] <= rL):
                        break
                    if (m["l"] <= giris) if bull else (m["h"] >= giris):
                        gi = x
                        break
                if gi is None:
                    continue
            if (giris <= sl or rH <= giris) if bull else (giris >= sl or rL >= giris):
                continue
            hedef = rH if bull else rL
            risk = abs(giris - sl)
            if tip == "bolge":   # dolum mumu stopu da aldıysa kayıp
                m = H[gi]
                if (m["l"] <= sl) if bull else (m["h"] >= sl):
                    r = r_be = -1.0; ct = m["t"]
                else:
                    r, r_be, ct = sim(H, gi + 1, bull, giris, sl, hedef, be_sev)
            else:
                r, r_be, ct = sim(H, gi, bull, giris, sl, hedef, be_sev)
            if r is None or r_be is None:
                continue
            isler.append({"s": s, "t": H[i]["t"], "cikis": ct, "tip": tip, "yon": "LONG" if bull else "SHORT",
                          "giris": giris, "sl": sl, "hedef": hedef, "rr": abs(hedef - giris) / risk,
                          "stopYuzde": risk / giris * 100, "maliyet": KOM * giris / risk,
                          "R": r, "R_BE": r_be, "smt": smt, "kl": z["tip"]})
    return {"s": s, "isler": isler, "sayac": sayac}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--simdi", required=True)
    p.add_argument("--bas-gun", type=int, default=180)
    p.add_argument("--bit-gun", type=int, default=0)
    p.add_argument("--semboller", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--out", required=True)
    a = p.parse_args()
    simdi = calendar.timegm(time.strptime(a.simdi, "%Y-%m-%d")) * 1000
    bas, bit = simdi - a.bas_gun * GUN, simdi - a.bit_gun * GUN
    L = open(a.semboller).read().split()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        R = list(ex.map(coin, [(s, bas, bit, a.cache, "ETHUSDT" if s == "BTCUSDT" else "BTCUSDT") for s in L]))
    isler = [x for r in R for x in r.get("isler", [])]
    hatalar = [r for r in R if "hata" in r]
    json.dump({"meta": {"bas": a.simdi, "bas_gun": a.bas_gun, "bit_gun": a.bit_gun, "hatalar": hatalar}, "isler": isler},
              open(a.out, "w"))
    say = {}
    for r in R:
        for k, v in r.get("sayac", {}).items():
            say[k] = say.get(k, 0) + v
    print(json.dumps({"islem": len(isler), "hata": len(hatalar), "sayac": say}))


if __name__ == "__main__":
    main()
