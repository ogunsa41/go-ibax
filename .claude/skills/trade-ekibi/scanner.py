#!/usr/bin/env python3
"""Top-N Binance tarayıcı — ekibin kurallarını (bias → key level → CRT → CHoCH → giriş → kontrol)
deterministik olarak her coin'e uygular ve CRT Trade Masası panelinin veritabanına yazılacak
dokümanları üretir.

Kullanım:
  python3 scanner.py tara [--top 50] [--out DIZIN] [--sembol BTCUSDT ...]

Çıktı (DIZIN altında):
  ozet.json        — tüm coin'lerin özet satırları (panel listesi)
  d0.json … dN.json — coin detayları (seviyeler, plan, kontroller, grafik mumları), parça parça
"""
import argparse
import calendar
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from binance import klines, SPOT  # noqa: E402

H4 = 4 * 3600 * 1000
STABIL = {"USDC", "USD1", "FDUSD", "TUSD", "DAI", "USDP", "RLUSD", "BFUSD", "EUR", "EURI", "AEUR",
          "XUSD", "USDE", "PYUSD", "U", "XAUT", "PAXG", "WBTC", "WBETH", "BNSOL"}
PARCA = 10


def get_json(path):
    for host in SPOT:
        try:
            with urllib.request.urlopen(host + path, timeout=30) as r:
                return json.load(r)
        except OSError:
            continue
    raise SystemExit(f"Binance erişilemedi: {path}")


def top_semboller(n):
    info = get_json("/api/v3/exchangeInfo?permissions=SPOT")
    hisse = {s["symbol"] for s in info["symbols"]
             if any("TRD_GRP_261" in p for p in s.get("permissionSets", []))}
    aktif = {s["symbol"]: s["baseAsset"] for s in info["symbols"]
             if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
    tick = get_json("/api/v3/ticker/24hr?type=MINI")
    aday = [t for t in tick if t["symbol"] in aktif and t["symbol"] not in hisse
            and aktif[t["symbol"]] not in STABIL
            and not aktif[t["symbol"]].endswith(("UP", "DOWN", "BULL", "BEAR"))]
    aday.sort(key=lambda t: -float(t["quoteVolume"]))
    return [(t["symbol"], float(t["quoteVolume"])) for t in aday[:n]]


def mumlar(sembol, aralik, limit):
    out = []
    for k in klines(sembol, aralik, limit):
        t = calendar.timegm(time.strptime(k["timestamp"], "%Y-%m-%dT%H:%M:%SZ")) * 1000
        out.append({"t": t, "o": k["open"], "h": k["high"], "l": k["low"], "c": k["close"], "closed": k["kapali"]})
    return out


def body(m):
    return abs(m["c"] - m["o"])


def swings(c, n, key):
    out = []
    for i in range(n, len(c) - n):
        ok = True
        for j in range(1, n + 1):
            if key == "h":
                ok = c[i]["h"] > c[i - j]["h"] and c[i]["h"] > c[i + j]["h"]
            else:
                ok = c[i]["l"] < c[i - j]["l"] and c[i]["l"] < c[i + j]["l"]
            if not ok:
                break
        if ok:
            out.append(i)
    return out


# ---------- 1 · haftalık bias ----------
def ajan_bias(W, price):
    cw = [m for m in W if m["closed"]]
    if len(cw) < 4:
        return {"bias": "NEUTRAL", "guven": "DUSUK", "neden": ["Yeterli kapanmış hafta yok"]}
    PW, PPW = cw[-1], cw[-2]
    neden, beh = [], None
    sl, sh = PW["l"] < PPW["l"], PW["h"] > PPW["h"]
    if PW["c"] > PPW["h"]:
        beh = "BULLISH"; neden.append("PW, PPW high üstünde kapandı")
    elif PW["c"] < PPW["l"]:
        beh = "BEARISH"; neden.append("PW, PPW low altında kapandı")
    elif sl and not sh:
        beh = "BULLISH"; neden.append("PW, PPW low'unu süpürüp içeride kapandı")
    elif sh and not sl:
        beh = "BEARISH"; neden.append("PW, PPW high'ını süpürüp içeride kapandı")
    elif sh and sl:
        beh = "BULLISH" if PW["c"] > (PW["h"] + PW["l"]) / 2 else "BEARISH"
        neden.append("PW dış hafta; kapanış aralığın " + ("üst" if beh == "BULLISH" else "alt") + " yarısında")
    else:
        neden.append("PW inside week; yapıya bakıldı")
    last = cw[-10:]
    H = [last[i]["h"] for i in swings(last, 1, "h")]
    L = [last[i]["l"] for i in swings(last, 1, "l")]
    yapi = "KARISIK"
    if len(H) >= 2 and len(L) >= 2:
        if H[-1] > H[-2] and L[-1] > L[-2]:
            yapi = "HH-HL"
        elif H[-1] < H[-2] and L[-1] < L[-2]:
            yapi = "LH-LL"
    neden.append(f"Haftalık yapı {yapi}")
    sdir = {"HH-HL": "BULLISH", "LH-LL": "BEARISH"}.get(yapi)
    bias = beh or sdir or "NEUTRAL"
    rng = cw[-12:]
    hi, lo = max(m["h"] for m in rng), min(m["l"] for m in rng)
    eq = (hi + lo) / 2
    konum = "DISCOUNT" if price < eq else "PREMIUM"
    score = (1 if beh else 0) + (1 if sdir == bias else 0)
    score += 1 if (bias == "BULLISH" and konum == "DISCOUNT") or (bias == "BEARISH" and konum == "PREMIUM") else 0
    score -= 1 if sdir and beh and sdir != beh else 0
    guven = "YUKSEK" if score >= 3 else "ORTA" if score == 2 else "DUSUK"
    hedef = None
    if bias == "BULLISH":
        a = [m["h"] for m in cw if m["h"] > price]; hedef = min(a) if a else hi
    elif bias == "BEARISH":
        a = [m["l"] for m in cw if m["l"] < price]; hedef = max(a) if a else lo
    neden.append(f"Fiyat {konum}, equilibrium {eq:.6g}")
    return {"bias": bias, "guven": guven, "yapi": yapi, "equilibrium": eq, "konum": konum, "hedef": hedef,
            "gecersizlik": PW["l"] if bias == "BULLISH" else PW["h"] if bias == "BEARISH" else None,
            "pw": {k: PW[k] for k in ("t", "o", "h", "l", "c")}, "neden": neden}


# ---------- 2 · 4H key level ----------
def ajan_kl(c4, W, bias, price):
    if bias == "NEUTRAL":
        return {"durum": "ATLANDI", "seviyeler": []}
    c = [m for m in c4 if m["closed"]]
    bull = bias == "BULLISH"
    cand = []
    avg = sum(body(m) for m in c) / len(c)
    for i in range(len(c) - 2):
        a, z, later = c[i], c[i + 2], c[i + 3:]
        if bull and a["h"] < z["l"]:
            dolum = min([m["l"] for m in later] + [z["l"]])
            if dolum > a["h"] and a["h"] <= price:
                cand.append({"tip": "FVG", "alt": a["h"], "ust": min(z["l"], dolum), "t": c[i + 1]["t"]})
        if not bull and a["l"] > z["h"]:
            dolum = max([m["h"] for m in later] + [z["h"]])
            if dolum < a["l"] and a["l"] >= price:
                cand.append({"tip": "FVG", "alt": max(z["h"], dolum), "ust": a["l"], "t": c[i + 1]["t"]})
    for i in range(len(c) - 1):
        a, n, later = c[i], c[i + 1], c[i + 2:]
        if bull and a["c"] < a["o"] and n["c"] > a["h"] and body(n) > 1.2 * avg \
                and not any(m["c"] < a["l"] for m in later) and sum(m["l"] <= a["h"] for m in later) < 2 and a["l"] <= price:
            cand.append({"tip": "OB", "alt": a["l"], "ust": a["h"], "t": a["t"]})
        if not bull and a["c"] > a["o"] and n["c"] < a["l"] and body(n) > 1.2 * avg \
                and not any(m["c"] > a["h"] for m in later) and sum(m["h"] >= a["l"] for m in later) < 2 and a["h"] >= price:
            cand.append({"tip": "OB", "alt": a["l"], "ust": a["h"], "t": a["t"]})
    pad = price * 0.0015
    if bull:
        for i in swings(c, 2, "l"):
            if not any(m["l"] < c[i]["l"] for m in c[i + 1:]) and c[i]["l"] < price:
                cand.append({"tip": "LIKIDITE", "alt": c[i]["l"] - pad, "ust": c[i]["l"], "t": c[i]["t"]})
    else:
        for i in swings(c, 2, "h"):
            if not any(m["h"] > c[i]["h"] for m in c[i + 1:]) and c[i]["h"] > price:
                cand.append({"tip": "LIKIDITE", "alt": c[i]["h"], "ust": c[i]["h"] + pad, "t": c[i]["t"]})
    gun = (int(time.time() * 1000) // 86400000) * 86400000
    pd = [m for m in c if gun - 86400000 <= m["t"] < gun]
    if pd:
        if bull:
            v = min(m["l"] for m in pd)
            if v < price:
                cand.append({"tip": "PDL", "alt": v - pad, "ust": v, "t": pd[0]["t"]})
        else:
            v = max(m["h"] for m in pd)
            if v > price:
                cand.append({"tip": "PDH", "alt": v, "ust": v + pad, "t": pd[0]["t"]})
    cw = [m for m in W if m["closed"]]
    if cw:
        PW = cw[-1]
        if bull and PW["l"] < price:
            cand.append({"tip": "PWL", "alt": PW["l"] - pad, "ust": PW["l"], "t": PW["t"]})
        if not bull and PW["h"] > price:
            cand.append({"tip": "PWH", "alt": PW["h"], "ust": PW["h"] + pad, "t": PW["t"]})
    for k in cand:
        ov = sum(1 for o in cand if o is not k and o["tip"] != k["tip"] and o["alt"] <= k["ust"] and o["ust"] >= k["alt"])
        k["kalite"] = "A" if ov else "B" if k["tip"] in ("FVG", "OB") else "C"
        k["mesafe"] = 0 if k["alt"] <= price <= k["ust"] else min(abs(price - k["alt"]), abs(price - k["ust"]))
    kept = sorted([k for k in cand if k["kalite"] != "C" or len(cand) <= 4], key=lambda k: k["mesafe"])[:4]
    for i, k in enumerate(kept):
        k["id"] = f"KL{i + 1}"
    return {"durum": "TAMAM" if kept else "YOK", "seviyeler": kept}


# ---------- 3 · 4H CRT ----------
def crt_scan(c4, yon, max_r=6, max_s=3, son_n=12):
    k = [m for m in c4 if m["closed"]]
    out = []
    for b in range(len(k)):
        for r in range(1, max_r + 1):
            re_ = b + r - 1
            if re_ >= len(k):
                break
            rng = k[b:re_ + 1]
            rH, rL = max(m["h"] for m in rng), min(m["l"] for m in rng)
            for s in range(1, max_s + 1):
                se = re_ + s
                if se >= len(k):
                    break
                if se < len(k) - son_n:
                    continue
                sw = k[re_ + 1:se + 1]
                last = sw[-1]
                inside = rL < last["c"] < rH
                if yon == "BULLISH":
                    if not (sw[0]["l"] < rL and inside and all(m["c"] <= rL for m in sw[:-1])):
                        continue
                    uc, hedef = min(m["l"] for m in sw), rH
                else:
                    if not (sw[0]["h"] > rH and inside and all(m["c"] >= rH for m in sw[:-1])):
                        continue
                    uc, hedef = max(m["h"] for m in sw), rL
                after = k[se + 1:]
                if any((m["c"] < uc) if yon == "BULLISH" else (m["c"] > uc) for m in after):
                    durum = "GECERSIZ"
                elif any((m["h"] >= hedef) if yon == "BULLISH" else (m["l"] <= hedef) for m in after):
                    durum = "HEDEFE_ULASTI"
                else:
                    durum = "AKTIF"
                out.append({"yon": yon, "tip": f"{r + s}-mum" if r + s <= 3 else "cok-mum", "rN": r, "sN": s,
                            "rStart": rng[0]["t"], "rEnd": rng[-1]["t"], "rH": rH, "rL": rL,
                            "sStart": sw[0]["t"], "sEnd": last["t"],
                            "swHi": max(m["h"] for m in sw), "swLo": min(m["l"] for m in sw),
                            "sweep": uc, "hedef": hedef, "durum": durum})
    return out


def ajan_crt(c4, bias, kl):
    if bias == "NEUTRAL" or not kl["seviyeler"]:
        return {"durum": "ATLANDI", "liste": [], "aktif": None}
    allc = crt_scan(c4, bias)
    for x in allc:
        x["kl"] = None
        for s in kl["seviyeler"]:
            if x["swLo"] <= s["ust"] and x["swHi"] >= s["alt"] and \
                    (x["sweep"] <= s["ust"] if bias == "BULLISH" else x["sweep"] >= s["alt"]):
                x["kl"] = s["id"]; break
    by = {}
    for x in allc:
        if x["kl"] and x["durum"] != "GECERSIZ":
            p = by.get(x["sEnd"])
            if not p or x["rN"] > p["rN"]:
                by[x["sEnd"]] = x
    rank = {"AKTIF": 0, "HEDEFE_ULASTI": 1}
    liste = sorted(by.values(), key=lambda x: (rank[x["durum"]], -x["sEnd"]))[:3]
    for i, x in enumerate(liste):
        x["id"] = f"CRT{i + 1}"
    aktif = next((x for x in liste if x["durum"] == "AKTIF"), None)
    return {"durum": "AKTIF" if aktif else "HEDEFE_ULASTI" if liste else "YOK", "liste": liste, "aktif": aktif,
            "aday": len(allc)}


# ---------- 4 · 15m CHoCH ----------
def ajan_choch(m15, crt):
    x = crt.get("aktif")
    if not x:
        return {"durum": "ATLANDI"}
    c = [m for m in m15 if m["closed"]]
    bull = x["yon"] == "BULLISH"
    t0, t1 = x["sStart"], x["sEnd"] + H4
    win = [i for i, m in enumerate(c) if t0 <= m["t"] < t1]
    if not win:
        return {"durum": "VERI_YOK"}
    sp = win[0]
    for i in win:
        if (c[i]["l"] < c[sp]["l"]) if bull else (c[i]["h"] > c[sp]["h"]):
            sp = i
    piv = [i for i in swings(c, 2, "h" if bull else "l") if i < sp]
    if piv:
        li = piv[-1]
    else:
        pre = list(range(max(0, sp - 8), sp))
        if not pre:
            return {"durum": "VERI_YOK"}
        li = pre[0]
        for i in pre:
            if (c[i]["h"] > c[li]["h"]) if bull else (c[i]["l"] < c[li]["l"]):
                li = i
    lvl = c[li]["h"] if bull else c[li]["l"]
    brk = next((i for i in range(sp + 1, len(c)) if (c[i]["c"] > lvl if bull else c[i]["c"] < lvl)), None)
    avg = sum(body(m) for m in c) / len(c)
    res = {"yon": x["yon"], "sp": sp, "spT": c[sp]["t"], "spPrice": c[sp]["l"] if bull else c[sp]["h"],
           "lvl": lvl, "lvlT": c[li]["t"], "_c": c}
    if brk is None:
        enyakin = max(m["c"] for m in c[sp + 1:]) if bull and sp + 1 < len(c) else \
            min(m["c"] for m in c[sp + 1:]) if sp + 1 < len(c) else None
        return {**res, "durum": "BEKLENIYOR", "enYakinKapanis": enyakin}
    return {**res, "durum": "ONAYLANDI", "brk": brk, "brkT": c[brk]["t"], "brkC": c[brk]["c"],
            "displacement": body(c[brk]) > 1.5 * avg}


# ---------- 5 · 15m giriş ----------
def ajan_giris(ch, crt, bias, price):
    if ch.get("durum") != "ONAYLANDI":
        return {"durum": "BEKLE"}
    c, bull, x = ch["_c"], ch["yon"] == "BULLISH", crt["aktif"]
    z = None
    for i in range(ch["sp"], min(ch["brk"] + 1, len(c) - 1) - 1):
        a, d = c[i], c[i + 2]
        if bull and a["h"] < d["l"]:
            z = {"tip": "FVG", "alt": a["h"], "ust": d["l"], "idx": i + 2, "t": c[i + 1]["t"]}
        if not bull and a["l"] > d["h"]:
            z = {"tip": "FVG", "alt": d["h"], "ust": a["l"], "idx": i + 2, "t": c[i + 1]["t"]}
    if not z:
        for i in range(ch["brk"] - 1, ch["sp"] - 1, -1):
            m = c[i]
            if (m["c"] < m["o"]) if bull else (m["c"] > m["o"]):
                z = {"tip": "OB", "alt": m["l"], "ust": m["h"], "idx": i, "t": m["t"]}
                break
    if not z:
        return {"durum": "BEKLE", "neden": "Displacement bacağında FVG/OB yok"}
    after = c[z["idx"] + 1:]
    if any((m["c"] < z["alt"]) if bull else (m["c"] > z["ust"]) for m in after):
        return {"durum": "BOLGE_GECILDI", "zone": z}
    giris = z["ust"] if bull else z["alt"]
    spc = c[ch["sp"]]
    buf = max(0.1 * (spc["h"] - spc["l"]), price * 0.0005)
    sl = min(ch["spPrice"], x["sweep"]) - buf if bull else max(ch["spPrice"], x["sweep"]) + buf
    sw = [c[i]["h"] if bull else c[i]["l"] for i in swings(c, 2, "h" if bull else "l")]
    sw = [v for v in sw if (v > giris if bull else v < giris)]
    tp1 = (min(sw) if bull else max(sw)) if sw else (c[ch["brk"]]["h"] if bull else c[ch["brk"]]["l"])
    tp2, tp3 = x["hedef"], bias.get("hedef")
    risk = abs(giris - sl)
    rr = lambda v: abs(v - giris) / risk
    if ((tp1 >= tp2) if bull else (tp1 <= tp2)) or rr(tp1) < 1:
        tp1 = None
    tps = []
    if tp1 is not None:
        tps.append({"ad": "TP1", "seviye": tp1, "rr": rr(tp1), "aciklama": "15m karşı likidite"})
    tps.append({"ad": "TP2", "seviye": tp2, "rr": rr(tp2), "aciklama": "CRT hedefi"})
    if tp3 is not None and ((tp3 > tp2) if bull else (tp3 < tp2)):
        tps.append({"ad": "TP3", "seviye": tp3, "rr": rr(tp3), "aciklama": "Haftalık hedef"})
    touched = any((m["l"] <= giris) if bull else (m["h"] >= giris) for m in after)
    return {"durum": "RR_YETERSIZ" if rr(tp2) < 2 else "HAZIR", "yon": "LONG" if bull else "SHORT",
            "zone": z, "giris": giris, "sl": sl, "tps": tps, "touched": touched}


# ---------- 6 · kontrolcü ----------
def ajan_kontrol(W, c4, bias, kl, crt, ch, en, price):
    L = []
    add = lambda k, s, d="": L.append({"k": k, "s": s, "d": d})
    add("Kapanmamış mum kullanılmadı", "GECTI", "Tüm kararlar kapanmış mumlardan")
    if bias["bias"] == "NEUTRAL":
        add("Bias NEUTRAL → zincir durdu", "GECTI")
        return bitir(L)
    cw = [m for m in W if m["closed"]]
    PW, PPW = cw[-1], cw[-2]
    bull = bias["bias"] == "BULLISH"
    ok = (bull and (PW["c"] > PPW["h"] or PW["l"] < PPW["l"] or bias["yapi"] == "HH-HL")) or \
         (not bull and (PW["c"] < PPW["l"] or PW["h"] > PPW["h"] or bias["yapi"] == "LH-LL"))
    add("Haftalık bias ham veriyle tutarlı", "GECTI" if ok else "KALDI")
    if bias["guven"] == "DUSUK":
        add("Bias güveni düşük", "UYARI", "Kurallar kısmen çelişiyor")
    if kl["seviyeler"]:
        side = all((s["alt"] <= price) if bull else (s["ust"] >= price) for s in kl["seviyeler"])
        add("Key level'lar bias tarafında", "GECTI" if side else "KALDI")
    x = crt.get("aktif")
    if not x:
        return bitir(L)
    k = [m for m in c4 if m["closed"]]
    rng = [m for m in k if x["rStart"] <= m["t"] <= x["rEnd"]]
    sw = [m for m in k if x["sStart"] <= m["t"] <= x["sEnd"]]
    rH, rL = max(m["h"] for m in rng), min(m["l"] for m in rng)
    ok = rH == x["rH"] and rL == x["rL"] and ((sw[0]["l"] < rL) if bull else (sw[0]["h"] > rH)) and rL < sw[-1]["c"] < rH
    add("CRT ham 4H veriyle doğrulandı", "GECTI" if ok else "KALDI")
    z = next((s for s in kl["seviyeler"] if s["id"] == x["kl"]), None)
    add("CRT sweep'i key level'a değiyor", "GECTI" if z and x["swLo"] <= z["ust"] and x["swHi"] >= z["alt"] else "KALDI")
    if ch.get("durum") != "ONAYLANDI":
        return bitir(L)
    bc = ch["_c"][ch["brk"]]
    add("CHoCH gövde kapanışı", "GECTI" if ((bc["c"] > ch["lvl"]) if bull else (bc["c"] < ch["lvl"])) else "KALDI")
    order = x["rEnd"] < x["sStart"] <= ch["spT"] < ch["brkT"] and (not en.get("zone") or en["zone"]["t"] >= ch["spT"])
    add("Zaman sırası", "GECTI" if order else "KALDI")
    if not en.get("giris"):
        return bitir(L)
    add("Giriş bölgenin değdiği kenarda", "GECTI" if en["giris"] == (en["zone"]["ust"] if bull else en["zone"]["alt"]) else "KALDI")
    seq = [en["sl"], en["giris"]] + [t["seviye"] for t in en["tps"]]
    srt = all((seq[i] > seq[i - 1]) if bull else (seq[i] < seq[i - 1]) for i in range(1, len(seq)))
    add("SL / giriş / TP sıralaması", "GECTI" if srt else "KALDI")
    ucs = min(ch["spPrice"], x["sweep"]) if bull else max(ch["spPrice"], x["sweep"])
    add("SL sweep noktasının ötesinde", "GECTI" if ((en["sl"] < ucs) if bull else (en["sl"] > ucs)) else "KALDI")
    tp2 = next(t for t in en["tps"] if t["ad"] == "TP2")
    rr2 = abs(tp2["seviye"] - en["giris"]) / abs(en["giris"] - en["sl"])
    add("TP2 R:R ≥ 2", "GECTI" if rr2 >= 2 else "UYARI" if en["durum"] == "RR_YETERSIZ" else "KALDI", f"{rr2:.2f}")
    return bitir(L)


def bitir(L):
    g = "KALDI" if any(c["s"] == "KALDI" for c in L) else "UYARI" if any(c["s"] == "UYARI" for c in L) else "GECTI"
    return {"genel": g, "list": L}


# ---------- 7 · sınıflandırma ----------
KATEGORI = [  # (kod, etiket, öncelik)
    ("FVG_BEKLENIYOR", "İşlem hazır · FVG/OB'ye dönüş bekleniyor", 1),
    ("TETIKLENDI", "Giriş seviyesine değdi", 2),
    ("CHOCH_BEKLENIYOR", "Karakter değişimi bekleniyor", 3),
    ("RR_YETERSIZ", "Yapı var · R:R yetersiz", 4),
    ("BOLGE_GECILDI", "Giriş bölgesi geçildi", 5),
    ("CRT_BEKLENIYOR", "Key level'da CRT bekleniyor", 6),
    ("HEDEFE_ULASTI", "CRT hedefine ulaştı", 7),
    ("KL_YOK", "Bias yönünde key level yok", 8),
    ("NOTR", "Haftalık bias nötr", 9),
    ("KONTROL_KALDI", "Kontrol başarısız", 10),
    ("HATA", "Veri alınamadı", 11),
]
KAT = {k: (e, o) for k, e, o in KATEGORI}


def sinifla(bias, kl, crt, ch, en, qa):
    if qa["genel"] == "KALDI":
        return "KONTROL_KALDI"
    if bias["bias"] == "NEUTRAL":
        return "NOTR"
    if not kl["seviyeler"]:
        return "KL_YOK"
    if not crt.get("aktif"):
        return "HEDEFE_ULASTI" if crt["liste"] else "CRT_BEKLENIYOR"
    if ch.get("durum") != "ONAYLANDI":
        return "CHOCH_BEKLENIYOR"
    if en["durum"] == "HAZIR":
        return "TETIKLENDI" if en["touched"] else "FVG_BEKLENIYOR"
    if en["durum"] == "RR_YETERSIZ":
        return "RR_YETERSIZ"
    return "BOLGE_GECILDI"


def yuvarla(v, p):
    if v is None or isinstance(v, bool) or not isinstance(v, float):
        return v
    return float(f"{v:.{p}g}")


def temizle(o, p=8):
    if isinstance(o, dict):
        return {k: temizle(v, p) for k, v in o.items() if not k.startswith("_") and k not in ("mesafe", "idx", "sp", "brk")}
    if isinstance(o, list):
        return [temizle(v, p) for v in o]
    return yuvarla(o, p)


def analiz(sembol):
    W = mumlar(sembol, "1w", 60)
    c4 = mumlar(sembol, "4h", 300)
    m15 = mumlar(sembol, "15m", 1000)
    price = m15[-1]["c"]
    bias = ajan_bias(W, price)
    kl = ajan_kl(c4, W, bias["bias"], price)
    crt = ajan_crt(c4, bias["bias"], kl)
    ch = ajan_choch(m15, crt)
    en = ajan_giris(ch, crt, bias, price)
    qa = ajan_kontrol(W, c4, bias, kl, crt, ch, en, price)
    kat = sinifla(bias, kl, crt, ch, en, qa)
    ozet = {"s": sembol, "kat": kat, "fiyat": price, "bias": bias["bias"], "guven": bias["guven"],
            "qa": qa["genel"], "crt": crt["durum"], "choch": ch.get("durum"), "giris": en.get("durum")}
    if crt.get("aktif"):
        ozet["crtTip"] = crt["aktif"]["tip"]; ozet["hedef"] = crt["aktif"]["hedef"]
    if ch.get("lvl") is not None:
        ozet["chochSeviye"] = ch["lvl"]
        ozet["chochMesafe"] = abs(ch["lvl"] - price) / price * 100
    if en.get("giris") is not None:
        ozet.update({"yon": en["yon"], "girisFiyat": en["giris"], "sl": en["sl"],
                     "rr2": next(t["rr"] for t in en["tps"] if t["ad"] == "TP2"),
                     "girisMesafe": abs(en["giris"] - price) / price * 100})
    detay = {"bias": bias, "kl": kl, "crt": {k: v for k, v in crt.items() if k != "aktif"},
             "aktifId": crt["aktif"]["id"] if crt.get("aktif") else None,
             "ch": ch, "en": en, "qa": qa}
    if kat in ("FVG_BEKLENIYOR", "TETIKLENDI", "CHOCH_BEKLENIYOR", "RR_YETERSIZ", "BOLGE_GECILDI", "CRT_BEKLENIYOR", "HEDEFE_ULASTI"):
        detay["m4"] = [[m["t"], m["o"], m["h"], m["l"], m["c"]] for m in c4[-60:]]
        detay["m15"] = [[m["t"], m["o"], m["h"], m["l"], m["c"]] for m in m15[-96:]]
    return ozet, detay


def main():
    p = argparse.ArgumentParser()
    p.add_argument("komut", choices=["tara"])
    p.add_argument("--top", type=int, default=50)
    p.add_argument("--out", default="tarama")
    p.add_argument("--sembol", nargs="*")
    a = p.parse_args()
    os.makedirs(a.out, exist_ok=True)
    liste = [(s, None) for s in a.sembol] if a.sembol else top_semboller(a.top)
    t0 = time.time()

    def is_(sv):
        s, vol = sv
        try:
            o, d = analiz(s)
        except Exception as e:  # noqa: BLE001 — tek coin hatası taramayı durdurmasın
            o, d = {"s": s, "kat": "HATA", "hata": str(e)[:200]}, {}
        if vol is not None:
            o["hacim"] = vol
        return o, d

    with ThreadPoolExecutor(8) as ex:
        sonuc = list(ex.map(is_, liste))
    for i, (o, _) in enumerate(sonuc):
        o["sira"] = i + 1
        o["etiket"], o["oncelik"] = KAT[o["kat"]]
    now = int(time.time() * 1000)
    parcalar = [sonuc[i:i + PARCA] for i in range(0, len(sonuc), PARCA)]
    ozet = {"zaman": now, "sure": round(time.time() - t0, 1), "kaynak": "Binance spot (data-api.binance.vision)",
            "parca": len(parcalar), "coinler": [temizle(o) for o, _ in sonuc]}
    for o in ozet["coinler"]:
        o["parca"] = (o["sira"] - 1) // PARCA
    with open(os.path.join(a.out, "ozet.json"), "w") as f:
        json.dump(ozet, f, ensure_ascii=False, separators=(",", ":"))
    for j, grup in enumerate(parcalar):
        with open(os.path.join(a.out, f"d{j}.json"), "w") as f:
            json.dump({"zaman": now, "coinler": {o["s"]: temizle(d) for o, d in grup}}, f,
                      ensure_ascii=False, separators=(",", ":"))
    say = {}
    for o, _ in sonuc:
        say[o["kat"]] = say.get(o["kat"], 0) + 1
    boyut = {fn: os.path.getsize(os.path.join(a.out, fn)) for fn in sorted(os.listdir(a.out))}
    print(json.dumps({"coin": len(sonuc), "sure_sn": ozet["sure"], "kategoriler": say, "dosyalar": boyut}, ensure_ascii=False))
    for o, _ in sorted(sonuc, key=lambda x: x[0]["oncelik"]):
        if o["oncelik"] <= 4:
            print(f'{o["s"]:<12} {o["etiket"]}' + (f' · {o.get("yon")} giriş {o.get("girisFiyat")}' if o.get("girisFiyat") else ""))


if __name__ == "__main__":
    main()
