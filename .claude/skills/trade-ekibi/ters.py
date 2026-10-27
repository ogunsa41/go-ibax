#!/usr/bin/env python3
"""Ters (bağımsız) sistem: tarayıcı zincirini scanner.py'nin hiçbir fonksiyonunu kullanmadan, ters yönden
yeniden yazar ve iki sistemi aynı mumlarla karşılaştırır. Farklı çıkan her adım bir hatadır.

Ters yön:
  - CRT: scanner range mumundan ileri gider; burada her kapanmış 4H mum SWEEP adayı kabul edilir ve
    range (ana mum + içindeki mumlar) sweep mumundan GERİYE doğru aranır.
  - CHoCH/CISD: kırılım mumundan geriye, süpürme ucuna ve kırılan seviyeye gidilir.
  - Ayrıca zincirin sonundaki her iddia (giriş planı, CRT, key level, bias) ham mumlardan geri doğrulanır.

Kullanım:
  python3 ters.py canli [--top 50 | --sembol ETHUSDT ...]      # canlı veride iki sistemi karşılaştır
  python3 ters.py gecmis --cache DIZIN --bas YYYY-MM-DD --bit YYYY-MM-DD [--adim-saat 4] [--top 50]
"""
import argparse
import bisect
import calendar
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DK15, H4, GUN, HAFTA = 900_000, 14_400_000, 86_400_000, 604_800_000
UZUN = {"15m": DK15, "4h": H4, "1w": HAFTA}
HOST = "https://data-api.binance.vision"


# ---------- veri (binance.py / scanner.py kullanılmaz) ----------
def cek(sembol, aralik, limit):
    with urllib.request.urlopen(f"{HOST}/api/v3/klines?symbol={sembol}&interval={aralik}&limit={limit}", timeout=30) as r:
        ham = json.load(r)
    simdi = time.time() * 1000
    return [{"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]), "closed": k[6] < simdi}
            for k in ham]


# ---------- 1 · bias: haftalık yapının tersi ----------
def t_bias(W):
    w = [m for m in W if m["closed"]][-10:]
    tepe = [w[i]["h"] for i in range(1, len(w) - 1) if w[i]["h"] > w[i - 1]["h"] and w[i]["h"] > w[i + 1]["h"]]
    dip = [w[i]["l"] for i in range(1, len(w) - 1) if w[i]["l"] < w[i - 1]["l"] and w[i]["l"] < w[i + 1]["l"]]
    if len(tepe) < 2 or len(dip) < 2:
        return "NEUTRAL"
    if tepe[-1] > tepe[-2] and dip[-1] > dip[-2]:
        return "BEARISH"  # HH-HL → tersi
    if tepe[-1] < tepe[-2] and dip[-1] < dip[-2]:
        return "BULLISH"  # LH-LL → tersi
    return "NEUTRAL"


# ---------- 2 · key level (gövde kapanışıyla kırılana kadar geçerli) ----------
def t_kl(k, W, yon, fiyat, simdi):
    if yon == "NEUTRAL" or not k:
        return []
    up = yon == "BULLISH"
    ort = sum(abs(m["c"] - m["o"]) for m in k) / len(k)
    # min_c[j] / max_c[j]: j ve sonrasındaki en düşük / en yüksek kapanış (geriden hesaplanır)
    min_c, max_c = [float("inf")] * (len(k) + 1), [float("-inf")] * (len(k) + 1)
    for j in range(len(k) - 1, -1, -1):
        min_c[j] = min(min_c[j + 1], k[j]["c"])
        max_c[j] = max(max_c[j + 1], k[j]["c"])
    L = []
    for i in range(len(k) - 2):
        a, z = k[i], k[i + 2]
        if up and z["l"] > a["h"] and min_c[i + 3] >= a["h"] and a["h"] <= fiyat:
            L.append(("FVG", a["h"], z["l"], k[i + 1]["t"]))
        if not up and z["h"] < a["l"] and max_c[i + 3] <= a["l"] and a["l"] >= fiyat:
            L.append(("FVG", z["h"], a["l"], k[i + 1]["t"]))
    for i in range(len(k) - 1):
        a, n = k[i], k[i + 1]
        guclu = abs(n["c"] - n["o"]) > 1.2 * ort
        if up and a["c"] < a["o"] and n["c"] > a["h"] and guclu and min_c[i + 2] >= a["l"] and a["l"] <= fiyat:
            L.append(("OB", a["l"], a["h"], a["t"]))
        if not up and a["c"] > a["o"] and n["c"] < a["l"] and guclu and max_c[i + 2] <= a["h"] and a["h"] >= fiyat:
            L.append(("OB", a["l"], a["h"], a["t"]))
    pay = fiyat * 0.0015
    for i in range(2, len(k) - 2):
        if up and all(k[i]["l"] < k[i + d]["l"] for d in (-2, -1, 1, 2)) and min_c[i + 1] >= k[i]["l"] and k[i]["l"] < fiyat:
            L.append(("LIKIDITE", k[i]["l"] - pay, k[i]["l"], k[i]["t"]))
        if not up and all(k[i]["h"] > k[i + d]["h"] for d in (-2, -1, 1, 2)) and max_c[i + 1] <= k[i]["h"] and k[i]["h"] > fiyat:
            L.append(("LIKIDITE", k[i]["h"], k[i]["h"] + pay, k[i]["t"]))
    gun = simdi // GUN * GUN
    dun = [m for m in k if gun - GUN <= m["t"] < gun]
    if dun:
        v = min(m["l"] for m in dun) if up else max(m["h"] for m in dun)
        if (v < fiyat) if up else (v > fiyat):
            L.append(("PDL", v - pay, v, dun[0]["t"]) if up else ("PDH", v, v + pay, dun[0]["t"]))
    w = [m for m in W if m["closed"]]
    if w:
        v = w[-1]["l"] if up else w[-1]["h"]
        if (v < fiyat) if up else (v > fiyat):
            L.append(("PWL", v - pay, v, w[-1]["t"]) if up else ("PWH", v, v + pay, w[-1]["t"]))
    out = []
    for n, (tip, alt, ust, t) in enumerate(L):
        cakisan = any(j != n and L[j][0] != tip and L[j][1] <= ust and L[j][2] >= alt for j in range(len(L)))
        kalite = "A" if cakisan else ("B" if tip in ("FVG", "OB") else "C")
        uzak = 0 if alt <= fiyat <= ust else min(abs(fiyat - alt), abs(fiyat - ust))
        out.append({"tip": tip, "alt": alt, "ust": ust, "t": t, "kalite": kalite, "_u": uzak, "_n": n})
    if len(out) > 4:
        out = [s for s in out if s["kalite"] != "C"]
    out.sort(key=lambda s: (s["_u"], s["_n"]))
    return out[:4]


# ---------- 3 · CRT: sweep mumundan geriye ----------
def t_crt_hepsi(c4, yon, son_n=12, max_r=6):
    k = [m for m in c4 if m["closed"]]
    acik = [m for m in c4 if not m["closed"]][-1:]
    up = yon == "BULLISH"
    out = []
    for j in range(len(k) - 1, max(0, len(k) - son_n) - 1, -1):
        S = k[j]
        for b in range(j - 1, max(-1, j - 1 - max_r), -1):
            M = k[b]
            if any(m["h"] > M["h"] or m["l"] < M["l"] for m in k[b + 1:j]):
                continue
            ic = M["l"] < S["c"] < M["h"]
            if up:
                gecerli = S["l"] < M["l"] and S["h"] <= M["h"] and ic
                uc, hedef = S["l"], M["h"]
            else:
                gecerli = S["h"] > M["h"] and S["l"] >= M["l"] and ic
                uc, hedef = S["h"], M["l"]
            if not gecerli:
                continue
            sonra = k[j + 1:]
            if any((m["c"] < uc) if up else (m["c"] > uc) for m in sonra):
                durum = "GECERSIZ"
            elif any((m["h"] >= hedef) if up else (m["l"] <= hedef) for m in sonra + acik):
                durum = "HEDEFE_ULASTI"
            else:
                durum = "AKTIF"
            out.append({"yon": yon, "rStart": M["t"], "rEnd": k[j - 1]["t"], "rN": j - b, "sStart": S["t"], "sEnd": S["t"],
                        "rH": M["h"], "rL": M["l"], "sweep": uc, "hedef": hedef, "durum": durum, "_j": j})
    return out


def t_crt(c4, W, yon):
    """Seçim: aynı sweep mumunu paylaşanlardan en geniş range; key level sweep anındaki verilerle."""
    k = [m for m in c4 if m["closed"]]
    if yon == "NEUTRAL":
        return None, []
    secili = {}
    for x in t_crt_hepsi(c4, yon):
        if x["durum"] == "GECERSIZ":
            continue
        j = x["_j"]
        Wo = [m for m in W if m["closed"] and m["t"] + HAFTA <= k[j]["t"]]
        sev = t_kl(k[:j], Wo, yon, k[j - 1]["c"], k[j]["t"])
        S = k[j]
        z = next((s for s in sev if S["l"] <= s["ust"] and S["h"] >= s["alt"] and
                  ((x["sweep"] <= s["ust"]) if yon == "BULLISH" else (x["sweep"] >= s["alt"]))), None)
        if not z:
            continue
        x["kl"] = z
        if x["sEnd"] not in secili or x["rN"] > secili[x["sEnd"]]["rN"]:
            secili[x["sEnd"]] = x
    liste = sorted(secili.values(), key=lambda x: (x["durum"] != "AKTIF", -x["sEnd"]))[:3]
    return next((x for x in liste if x["durum"] == "AKTIF"), None), liste


# ---------- 4 · CHoCH / CISD ----------
def t_choch(m15, x, cisd=True):
    c = [m for m in m15 if m["closed"]]
    up = x["yon"] == "BULLISH"
    pen = [i for i, m in enumerate(c) if x["sStart"] <= m["t"] < x["sEnd"] + H4]
    if not pen:
        return None
    sp = min(pen, key=lambda i: (c[i]["l"] if up else -c[i]["h"], i))
    tepe = [i for i in range(2, len(c) - 2) if i < sp and
            all((c[i]["h"] > c[i + d]["h"]) if up else (c[i]["l"] < c[i + d]["l"]) for d in (-2, -1, 1, 2))]
    if tepe:
        li = tepe[-1]
    else:
        onceki = list(range(max(0, sp - 8), sp))
        if not onceki:
            return None
        li = min(onceki, key=lambda i: (-c[i]["h"] if up else c[i]["l"], i))
    lvl, tur = (c[li]["h"] if up else c[li]["l"]), "CHOCH"
    brk = next((i for i in range(sp + 1, len(c)) if ((c[i]["c"] > lvl) if up else (c[i]["c"] < lvl))), None)
    if cisd:
        ters = (lambda m: m["c"] < m["o"]) if up else (lambda m: m["c"] > m["o"])
        bas = sp if ters(c[sp]) else sp - 1
        if bas >= 0 and ters(c[bas]):
            while bas > 0 and ters(c[bas - 1]):
                bas -= 1
            cl = c[bas]["o"]
            cb = next((i for i in range(sp + 1, len(c)) if ((c[i]["c"] > cl) if up else (c[i]["c"] < cl))), None)
            if cb is not None and (brk is None or cb < brk):
                brk, lvl, li, tur = cb, cl, bas, "CISD"
    return {"sp": sp, "spT": c[sp]["t"], "lvl": lvl, "lvlT": c[li]["t"], "tur": tur,
            "brk": brk, "brkT": c[brk]["t"] if brk is not None else None, "_c": c}


# ---------- 5 · giriş ----------
def t_giris(ch, x, fiyat):
    c, up = ch["_c"], x["yon"] == "BULLISH"
    sp, brk = ch["sp"], ch["brk"]
    z = None
    for i in range(min(brk, len(c) - 2) - 1, sp - 1, -1):  # geriden: kırılıma en yakın FVG
        a, d = c[i], c[i + 2]
        if up and a["h"] < d["l"]:
            z = ("FVG", a["h"], d["l"], i + 2, c[i + 1]["t"]); break
        if not up and a["l"] > d["h"]:
            z = ("FVG", d["h"], a["l"], i + 2, c[i + 1]["t"]); break
    if not z:
        for i in range(brk - 1, sp - 1, -1):
            if (c[i]["c"] < c[i]["o"]) if up else (c[i]["c"] > c[i]["o"]):
                z = ("OB", c[i]["l"], c[i]["h"], i, c[i]["t"]); break
    if not z:
        return {"durum": "BEKLE"}
    tip, alt, ust, idx, t = z
    if any((m["c"] < alt) if up else (m["c"] > ust) for m in c[idx + 1:]):
        return {"durum": "BOLGE_GECILDI", "zoneT": t}
    giris = ust if up else alt
    tampon = max(0.1 * (c[sp]["h"] - c[sp]["l"]), fiyat * 0.0005)
    uc = min(c[sp]["l"], x["sweep"]) if up else max(c[sp]["h"], x["sweep"])
    sl = uc - tampon if up else uc + tampon
    return {"durum": "PLAN", "zoneT": t, "tip": tip, "giris": giris, "sl": sl, "tp2": x["hedef"]}


# ---------- karşılaştırma ----------
def karsilastir(W, c4, m15, simdi, sc):
    """Aynı mumlarla scanner zincirini ve ters zinciri çalıştırır; farkları döndürür."""
    fiyat = m15[-1]["c"]
    fark = []
    b = sc.ajan_bias_ters(W, fiyat)
    yon = t_bias(W)
    if b["bias"] != yon:
        fark.append(("BIAS", f"scanner {b['bias']} / ters {yon}"))
        return fark, b, yon
    kl = sc.ajan_kl(c4, W, yon, fiyat, now_ms=simdi)
    tk = t_kl([m for m in c4 if m["closed"]], W, yon, fiyat, simdi)
    a = [(s["tip"], s["alt"], s["ust"], s["t"]) for s in kl["seviyeler"]]
    bb = [(s["tip"], s["alt"], s["ust"], s["t"]) for s in tk]
    if a != bb:
        fark.append(("KL", f"scanner {a} / ters {bb}"))
    for y in ("BULLISH", "BEARISH"):  # CRT tanımı iki yönde de karşılaştırılır (bias'tan bağımsız)
        s1 = {(x["rStart"], x["sEnd"], x["durum"]) for x in sc.crt_scan(c4, y)}
        s2 = {(x["rStart"], x["sEnd"], x["durum"]) for x in t_crt_hepsi(c4, y)}
        if s1 != s2:
            fark.append(("CRT_TANIM", f"{y} yalnız scanner {sorted(s1 - s2)} / yalnız ters {sorted(s2 - s1)}"))
    if yon == "NEUTRAL":
        return fark, b, yon
    crt = sc.ajan_crt(c4, yon, kl, W=W)
    ta, tl = t_crt(c4, W, yon)
    k1 = [(x["rStart"], x["sEnd"], x["durum"]) for x in crt["liste"]]
    k2 = [(x["rStart"], x["sEnd"], x["durum"]) for x in tl]
    if k1 != k2 and (kl["seviyeler"] or not tl):
        fark.append(("CRT_SECIM", f"scanner {k1} / ters {k2}"))
    x = crt.get("aktif")
    if not x or not ta or x["sEnd"] != ta["sEnd"]:
        return fark, b, yon
    ch = sc.ajan_choch(m15, crt)
    tc = t_choch(m15, ta, sc.CISD)
    if tc is None or ch.get("durum") in (None, "VERI_YOK"):
        return fark, b, yon
    if (ch["spT"], ch["lvl"], ch["tur"], ch.get("brkT")) != (tc["spT"], tc["lvl"], tc["tur"], tc["brkT"]):
        fark.append(("CHOCH", f"scanner {(ch['spT'], ch['lvl'], ch['tur'], ch.get('brkT'))} / ters "
                              f"{(tc['spT'], tc['lvl'], tc['tur'], tc['brkT'])}"))
        return fark, b, yon
    if tc["brk"] is None:
        return fark, b, yon
    en = sc.ajan_giris(ch, crt, b, fiyat)
    tg = t_giris(tc, ta, fiyat)
    e1 = (en.get("zone") or {}).get("t"), en.get("giris"), en.get("sl")
    e2 = tg.get("zoneT"), tg.get("giris"), tg.get("sl")
    if e1 != e2:
        fark.append(("GIRIS", f"scanner {en.get('durum')} {e1} / ters {tg['durum']} {e2}"))
    return fark, b, yon


def _sc():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import scanner
    return scanner


def canli(semboller):
    sc = _sc()
    sonuc = {}

    def bir(s):
        W, c4, m15 = cek(s, "1w", 60), cek(s, "4h", 300), cek(s, "15m", 1000)
        f, *_ = karsilastir(W, c4, m15, int(time.time() * 1000), sc)
        return s, f
    with ThreadPoolExecutor(8) as ex:
        for s, f in ex.map(bir, semboller):
            sonuc[s] = f
    return sonuc


def gecmis(semboller, cache, bas, bit, adim):
    sc = _sc()
    say, ornek = {}, {}
    nokta = 0
    for s in semboller:
        yuk = {}
        for ar in ("1w", "4h", "15m"):
            d = {}
            for fn in os.listdir(cache):
                if fn.startswith(f"{s}_{ar}_"):
                    for m in json.load(open(os.path.join(cache, fn))):
                        d[m["t"]] = m
            yuk[ar] = [d[t] for t in sorted(d)]
        if not yuk["15m"]:
            continue
        zt = {a: [m["t"] for m in v] for a, v in yuk.items()}
        T = bas
        while T <= bit:
            def kes(a, n):
                j = bisect.bisect_right(zt[a], T - UZUN[a])
                return [dict(m, closed=True) for m in yuk[a][max(0, j - n):j]]
            W, c4, m15 = kes("1w", 60), kes("4h", 300), kes("15m", 1000)
            if len(W) >= 12 and len(c4) >= 50 and len(m15) >= 100:
                nokta += 1
                f, *_ = karsilastir(W, c4, m15, T, sc)
                for tur, d in f:
                    say[tur] = say.get(tur, 0) + 1
                    ornek.setdefault(tur, []).append((s, T, d))
            T += adim
    return nokta, say, ornek


def main():
    p = argparse.ArgumentParser()
    p.add_argument("komut", choices=["canli", "gecmis"])
    p.add_argument("--top", type=int, default=50)
    p.add_argument("--sembol", nargs="*")
    p.add_argument("--cache")
    p.add_argument("--bas")
    p.add_argument("--bit")
    p.add_argument("--adim-saat", type=float, default=4)
    a = p.parse_args()
    if a.sembol:
        semboller = a.sembol
    else:
        semboller = [s for s, _ in _sc().top_semboller(a.top)]
    if a.komut == "canli":
        r = canli(semboller)
        hatali = {s: f for s, f in r.items() if f}
        print(json.dumps({"coin": len(r), "farkli": len(hatali)}, ensure_ascii=False))
        for s, f in hatali.items():
            for tur, d in f:
                print(f"{s:<12} {tur:<10} {d[:300]}")
    else:
        ms = lambda d: calendar.timegm(time.strptime(d, "%Y-%m-%d")) * 1000
        n, say, ornek = gecmis(semboller, a.cache, ms(a.bas), ms(a.bit), int(a.adim_saat * 3_600_000))
        print(json.dumps({"nokta": n, "fark": say}, ensure_ascii=False))
        for tur, L in ornek.items():
            for s, T, d in L[:4]:
                print(f"{tur:<10} {s:<12} {time.strftime('%m-%d %H:%M', time.gmtime(T / 1000))} {d[:300]}")


if __name__ == "__main__":
    main()
