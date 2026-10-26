#!/usr/bin/env python3
"""Tarayıcı kurallarının geriye dönük testi (walk-forward).

Her coin için geçmişte 15 dakikalık adımlarla ilerler (canlı tarama gibi); o ana kadar KAPANMIŞ
mumlarla scanner.py zincirini (bias → key level → CRT → CHoCH → giriş → kontrol) çalıştırır.
"FVG bekleniyor" sinyali ilk kez göründüğünde limit emri kurar ve sonrasını 15m mumlarla simüle
eder. Her işlemin özellikleri (CRT tipi, giriş bölgesi, bias güveni, stop %, ...) ve birkaç çıkış
kuralının sonucu kaydedilir; filtreler `optimize.py` ile sonradan denenir.

Simülasyon kuralları (muhafazakâr):
  - Emir: giriş = bölgenin ilk değilen kenarı (limit). 48 saat içinde dolmazsa iptal.
  - Dolmadan önce: 15m kapanış bölgenin ötesine geçerse veya fiyat TP2'ye ulaşırsa iptal.
  - Dolum mumunda stop da görüldüyse kayıp sayılır.
  - Dolumdan sonra stop ve hedef aynı 15m mumunda görülürse kayıp sayılır.
  - Sonuçlar R cinsinden; `maliyet` alanı gidiş-dönüş %0.2 komisyonun R karşılığıdır.

Kullanım:
  python3 backtest.py --bas-gun 90 --bit-gun 0 --top 50 [--secim genis|dar] [--bias hafta|sma]
                      [--simdi YYYY-MM-DD] [--sembol ...] [--out DIZIN]
  (bas-gun / bit-gun: dönemin başı ve sonu, bugünden kaç gün önce)
"""
import argparse
import bisect
import calendar
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as sc  # noqa: E402

DK15, SAAT, H4, HAFTA, GUN = 900_000, 3_600_000, 4 * 3_600_000, 7 * 86_400_000, 86_400_000
ADIM = DK15  # canlı tarama gibi 15 dakikada bir
SURE = {"15m": DK15, "4h": H4, "1w": HAFTA}
EMIR_OMRU = 48 * SAAT
KOMISYON = 0.002  # gidiş-dönüş
CIKISLAR = ("TP2", "1.5R", "2R", "3R", "TP1")


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


def cikislar(long, giris, sl, hedefler, m15, i):
    """Dolumdan sonra her hedef için R; "_BE" ekli anahtarlarda fiyat +1R'ye ulaşınca stop girişe çekilir."""
    risk = abs(giris - sl)
    yon = 1 if long else -1
    bir_r = giris + yon * risk
    R, cikis = {}, {}
    be_aktif = False
    anahtarlar = list(hedefler) + [k + "_BE" for k in hedefler]
    while i < len(m15) and len(R) < len(anahtarlar):
        m = m15[i]
        stop = (m["l"] <= sl) if long else (m["h"] >= sl)
        be_stop = be_aktif and ((m["l"] <= giris) if long else (m["h"] >= giris))
        for k, h in hedefler.items():
            hit = (m["h"] >= h) if long else (m["l"] <= h)
            if k not in R:
                if stop:
                    R[k], cikis[k] = -1.0, m["t"]
                elif hit:
                    R[k], cikis[k] = abs(h - giris) / risk, m["t"]
            kb = k + "_BE"
            if kb not in R:
                if stop:
                    R[kb], cikis[kb] = -1.0, m["t"]
                elif be_stop:
                    R[kb], cikis[kb] = 0.0, m["t"]
                elif hit:
                    R[kb], cikis[kb] = abs(h - giris) / risk, m["t"]
        if not be_aktif and not stop and ((m["h"] >= bir_r) if long else (m["l"] <= bir_r)):
            be_aktif = True  # bir sonraki mumdan itibaren stop girişte
        i += 1
    return R, cikis, len(R) == len(anahtarlar)


def hedef_seti(long, giris, sl, tp2, tp1=None):
    risk = abs(giris - sl)
    yon = 1 if long else -1
    return {"TP2": tp2, "1.5R": giris + 1.5 * yon * risk, "2R": giris + 2 * yon * risk,
            "3R": giris + 3 * yon * risk, "TP1": tp1 if tp1 is not None else tp2}


def simule(plan, m15, m15t, T):
    long = plan["yon"] == "LONG"
    giris, sl = plan["giris"], plan["sl"]
    risk = abs(giris - sl)
    tp = {t["ad"]: t["seviye"] for t in plan["tps"]}
    hedefler = hedef_seti(long, giris, sl, tp["TP2"], tp.get("TP1"))
    zone = plan["zone"]
    sonuc = {"rr2": abs(tp["TP2"] - giris) / risk, "maliyet": KOMISYON * giris / risk}
    i = bisect.bisect_left(m15t, T)
    dolum = None
    while i < len(m15):
        m = m15[i]
        if m["t"] - T > EMIR_OMRU:
            return {**sonuc, "durum": "SURESI_DOLDU"}
        if (m["l"] <= giris) if long else (m["h"] >= giris):
            dolum = m["t"]
            if (m["l"] <= sl) if long else (m["h"] >= sl):
                anah = list(hedefler) + [k + "_BE" for k in hedefler]
                return {**sonuc, "durum": "KAPANDI", "dolum": dolum, "R": {k: -1.0 for k in anah},
                        "cikis": {k: m["t"] for k in anah}}
            i += 1
            break
        yari = giris + sc.KACTI_ORAN * (tp["TP2"] - giris)
        if (m["h"] >= yari) if long else (m["l"] <= yari):
            return {**sonuc, "durum": "KACIRILDI"}
        if (m["c"] < zone["alt"]) if long else (m["c"] > zone["ust"]):
            return {**sonuc, "durum": "IPTAL_BOLGE"}
        i += 1
    if dolum is None:
        return {**sonuc, "durum": "ACIK_EMIR"}
    R, cikis, tamam = cikislar(long, giris, sl, hedefler, m15, i)
    return {**sonuc, "durum": "KAPANDI" if tamam else "ACIK_POZISYON", "dolum": dolum, "R": R, "cikis": cikis}


def simule_kirilim(ch, crt, price, m15, m15t, T):
    """CHoCH kırılım mumunun kapanışında piyasa emriyle giriş; stop giris-15m ile aynı kuralla."""
    long = ch["yon"] == "BULLISH"
    x, c = crt["aktif"], ch["_c"]
    giris = ch["brkC"]
    spc = c[ch["sp"]]
    buf = max(0.1 * (spc["h"] - spc["l"]), price * 0.0005)
    sl = min(ch["spPrice"], x["sweep"]) - buf if long else max(ch["spPrice"], x["sweep"]) + buf
    tp2 = x["hedef"]
    if (sl >= giris or tp2 <= giris) if long else (sl <= giris or tp2 >= giris):
        return None
    risk = abs(giris - sl)
    hedefler = hedef_seti(long, giris, sl, tp2)
    sonuc = {"yon": "LONG" if long else "SHORT", "giris": giris, "sl": sl, "rr2": abs(tp2 - giris) / risk,
             "maliyet": KOMISYON * giris / risk}
    R, cikis, tamam = cikislar(long, giris, sl, hedefler, m15, bisect.bisect_left(m15t, T))
    return {**sonuc, "durum": "KAPANDI" if tamam else "ACIK_POZISYON", "dolum": T, "R": R, "cikis": cikis}


def coin_test(args):
    sembol, bas, bit, cache, secim, bias_mod = args
    try:
        W = aralik_cek(sembol, "1w", bas - 70 * HAFTA, bit, cache)
        c4 = aralik_cek(sembol, "4h", bas - 60 * GUN, bit, cache)
        m15 = aralik_cek(sembol, "15m", bas - 11 * GUN, bit, cache)
    except SystemExit as e:
        return {"s": sembol, "hata": str(e)}
    Wt, c4t, m15t = [m["t"] for m in W], [m["t"] for m in c4], [m["t"] for m in m15]
    BW = aralik_cek("BTCUSDT", "1w", bas - 70 * HAFTA, bit, cache) if bias_mod in ("btc", "rs", "btcrs") else []
    BWt = [m["t"] for m in BW]
    sayac, isler, gorulen = {}, [], set()
    T = (bas // ADIM) * ADIM
    while T <= bit:
        Tn, T = T, T + ADIM
        w = kapanmis(W, Wt, Tn, 60, HAFTA)
        k4 = kapanmis(c4, c4t, Tn, 300, H4)
        k15 = kapanmis(m15, m15t, Tn, 1000, DK15)
        if len(w) < 4 or len(k4) < 50 or len(k15) < 100:
            continue
        price = k15[-1]["c"]
        bw = kapanmis(BW, BWt, Tn, 60, HAFTA) if BW else None
        for bias in sc.bias_listesi(bias_mod, w, price, bw):
            kl = sc.ajan_kl(k4, w, bias["bias"], price, now_ms=Tn)
            crt = sc.ajan_crt(k4, bias["bias"], kl, secim=secim, W=w)
            ch = sc.ajan_choch(k15, crt)
            en = sc.ajan_giris(ch, crt, bias, price)
            qa = sc.ajan_kontrol(w, k4, bias, kl, crt, ch, en, price)
            kat = sc.sinifla(bias, kl, crt, ch, en, qa)
            sayac[kat] = sayac.get(kat, 0) + 1
            if ch.get("durum") == "ONAYLANDI" and qa["genel"] != "KALDI" and ch["brkT"] == k15[-1]["t"]:
                anahtar = ("kir", crt["aktif"]["yon"], crt["aktif"]["sEnd"])
                if anahtar not in gorulen:
                    gorulen.add(anahtar)
                    sim = simule_kirilim(ch, crt, price, m15, m15t, Tn)
                    if sim:
                        x = crt["aktif"]
                        isler.append({"s": sembol, "T": Tn, "giris_tipi": "KIRILIM", "crtTip": x["tip"], "rN": x["rN"],
                                      "zone": "-", "guven": bias["guven"], "displacement": bool(ch.get("displacement")),
                                      "stopYuzde": abs(sim["giris"] - sim["sl"]) / sim["giris"] * 100, **sim})
            if kat not in ("FVG_BEKLENIYOR", "TETIKLENDI", "SONUCLANDI"):
                continue
            x = crt["aktif"]
            anahtarlar = {("crt", x["yon"], x["sEnd"], en["zone"]["t"]), ("plan", en["yon"], en["giris"], en["sl"])}
            if anahtarlar & gorulen:
                continue
            gorulen |= anahtarlar
            if kat in ("TETIKLENDI", "SONUCLANDI"):
                isler.append({"s": sembol, "T": Tn, "yon": en["yon"], "durum": "GEC_TESPIT"})
                continue
            ozellik = {
                "crtTip": x["tip"], "rN": x["rN"], "sN": x["sN"], "zone": en["zone"]["tip"], "guven": bias["guven"],
                "yapi": bias["yapi"], "konum": bias["konum"], "klTip": x["klSeviye"]["tip"],
                "displacement": bool(ch.get("displacement")), "stopYuzde": abs(en["giris"] - en["sl"]) / en["giris"] * 100,
                "crtGenislikYuzde": (x["rH"] - x["rL"]) / price * 100, "tp1Var": any(t["ad"] == "TP1" for t in en["tps"]),
                "saat": time.gmtime(Tn / 1000).tm_hour,
            }
            isler.append({"s": sembol, "T": Tn, "giris_tipi": "BOLGE", "yon": en["yon"], "giris": en["giris"], "sl": en["sl"], **ozellik,
                          **simule(en, m15, m15t, Tn)})
    return {"s": sembol, "sayac": sayac, "isler": isler}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bas-gun", type=int, default=90)
    p.add_argument("--bit-gun", type=int, default=0)
    p.add_argument("--top", type=int, default=50)
    p.add_argument("--secim", choices=["genis", "dar"], default="genis")
    p.add_argument("--bias", choices=["hafta", "sma", "ters", "iki", "choch", "btc", "rs", "btcrs"], default="hafta")
    p.add_argument("--simdi", help="dönem çapası YYYY-MM-DD (varsayılan: bugün)")
    p.add_argument("--sembol", nargs="*")
    p.add_argument("--cache")
    p.add_argument("--out", default="backtest")
    a = p.parse_args()
    os.makedirs(a.out, exist_ok=True)
    cache = a.cache or os.path.join(a.out, "cache")
    os.makedirs(cache, exist_ok=True)
    simdi = (calendar.timegm(time.strptime(a.simdi, "%Y-%m-%d")) * 1000 if a.simdi
             else (int(time.time() * 1000) // GUN) * GUN)
    bas, bit = simdi - a.bas_gun * GUN, simdi - a.bit_gun * GUN
    semboller = a.sembol or [s for s, _ in sc.top_semboller(a.top)]
    t0 = time.time()
    with ProcessPoolExecutor(4) as ex:
        sonuc = list(ex.map(coin_test, [(s, bas, bit, cache, a.secim, a.bias) for s in semboller]))
    isler = [x for r in sonuc for x in r.get("isler", [])]
    akis = {}
    for r in sonuc:
        for k, v in r.get("sayac", {}).items():
            akis[k] = akis.get(k, 0) + v
    meta = {"bas": time.strftime("%Y-%m-%d", time.gmtime(bas / 1000)), "bit": time.strftime("%Y-%m-%d", time.gmtime(bit / 1000)),
            "secim": a.secim, "bias": a.bias, "coin": semboller, "sure_sn": round(time.time() - t0),
            "hatalar": [r for r in sonuc if r.get("hata")], "durumDagilimi": akis}
    with open(os.path.join(a.out, "islemler.json"), "w") as f:
        json.dump({"meta": meta, "isler": isler}, f, ensure_ascii=False)
    durum = {}
    for x in isler:
        durum[x["durum"]] = durum.get(x["durum"], 0) + 1
    print(json.dumps({**{k: v for k, v in meta.items() if k != "coin"}, "sinyal": len(isler), "durumlar": durum}, ensure_ascii=False))


if __name__ == "__main__":
    main()
