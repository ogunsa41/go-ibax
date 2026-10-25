#!/usr/bin/env python3
"""Backtest işlemleri üzerinde kural taraması ve kontrol dönemi doğrulaması.

Kural: Kombinasyonlar YALNIZCA eğitim döneminde seçilir. Seçim ölçütü önceden sabittir: en az
MIN_ISLEM işlem yapan kombinasyonlar arasında net ortalama R'nin istatistiksel gücü
(ort / standart hata). Seçilen kombinasyon kontrol döneminde (hiç bakılmamış veri) bir kez ölçülür.
Ek olarak, iki dönemde de kârlı kalan kombinasyon sayısı raporlanır; bu sayı keşif amaçlıdır ve
kontrol dönemine bakılarak seçildiği için tek başına kanıt sayılmaz.

Kullanım:
  python3 optimize.py --egitim DIZIN... --kontrol DIZIN... [--out rapor.json]
  (her dizin backtest.py çıktısıdır; meta.bias ve meta.secim ile eşleştirilir)
"""
import argparse
import itertools
import json
import math

MIN_ISLEM = 40
ADAY = 10
GUVEN_SIRA = {"DUSUK": 0, "ORTA": 1, "YUKSEK": 2}

IZGARA = {
    "bias": ["hafta", "sma"],
    "secim": ["genis", "dar"],
    "giris": ["BOLGE", "KIRILIM"],
    "maxRange": [2, 3, 6],
    "minGuven": ["DUSUK", "ORTA"],
    "displacement": [False, True],
    "minRR2": [0, 2],
    "maxStop": [99, 2.0],
    "cikis": ["TP2", "1.5R", "2R", "3R", "TP2_BE", "1.5R_BE", "2R_BE", "3R_BE"],
}


def yukle(dizinler):
    out = {}
    for d in dizinler:
        with open(f"{d}/islemler.json") as f:
            veri = json.load(f)
        m = veri["meta"]
        out[(m.get("bias", "hafta"), m["secim"])] = veri
    return out


def uygun(x, k):
    return (x["durum"] == "KAPANDI" and x.get("giris_tipi") == k["giris"] and x["rN"] <= k["maxRange"]
            and GUVEN_SIRA[x["guven"]] >= GUVEN_SIRA[k["minGuven"]]
            and (not k["displacement"] or x["displacement"])
            and x["rr2"] >= k["minRR2"] and x["stopYuzde"] <= k["maxStop"])


def olc(isler, k):
    sec = sorted([x for x in isler if uygun(x, k)], key=lambda x: x["cikis"][k["cikis"]])
    R = [x["R"][k["cikis"]] - x["maliyet"] for x in sec]
    n = len(R)
    if not n:
        return {"islem": 0}
    ort = sum(R) / n
    sd = math.sqrt(sum((r - ort) ** 2 for r in R) / (n - 1)) if n > 1 else 0
    egri = tepe = dd = 0.0
    for r in R:
        egri += r; tepe = max(tepe, egri); dd = min(dd, egri - tepe)
    kaz = sum(r for r in R if r > 0)
    kay = -sum(r for r in R if r < 0)
    return {"islem": n, "isabet": round(sum(r > 0 for r in R) / n * 100, 1), "netR": round(sum(R), 2),
            "ortR": round(ort, 3), "skor": round(ort / (sd / math.sqrt(n)), 2) if sd else 0,
            "pf": round(kaz / kay, 2) if kay else None, "maxDD": round(dd, 2),
            "long": sum(x["yon"] == "LONG" for x in sec), "short": sum(x["yon"] == "SHORT" for x in sec),
            "coin": len({x["s"] for x in sec})}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--egitim", nargs="+", required=True)
    p.add_argument("--kontrol", nargs="+", required=True)
    p.add_argument("--out", default="optimize-rapor.json")
    a = p.parse_args()
    egitim, kontrol = yukle(a.egitim), yukle(a.kontrol)
    anahtarlar = list(IZGARA)
    sonuc, ikisinde = [], []
    for deger in itertools.product(*IZGARA.values()):
        k = dict(zip(anahtarlar, deger))
        if k["bias"] == "sma" and k["minGuven"] != "DUSUK":
            continue  # SMA bias'ında güven seviyesi yok
        veri = (k["bias"], k["secim"])
        if veri not in egitim or veri not in kontrol:
            continue
        m = olc(egitim[veri]["isler"], k)
        if m["islem"] < MIN_ISLEM:
            continue
        sonuc.append((k, m))
        c = olc(kontrol[veri]["isler"], k)
        if c["islem"] >= MIN_ISLEM:
            ikisinde.append((k, m, c))
    sonuc.sort(key=lambda km: -km[1]["skor"])
    adaylar = [{"kural": k, "egitim": m, "kontrol": olc(kontrol[(k["bias"], k["secim"])]["isler"], k)}
               for k, m in sonuc[:ADAY]]
    karli_iki = [(k, m, c) for k, m, c in ikisinde if m["netR"] > 0 and c["netR"] > 0]
    karli_iki.sort(key=lambda x: -min(x[1]["ortR"], x[2]["ortR"]))
    ilk = next(iter(egitim.values()))["meta"]
    ilk_k = next(iter(kontrol.values()))["meta"]
    rapor = {
        "egitimDonemi": f'{ilk["bas"]} – {ilk["bit"]}',
        "kontrolDonemi": f'{ilk_k["bas"]} – {ilk_k["bit"]}',
        "yeterliIslemliKombinasyon": len(sonuc),
        "egitimdeKarliOran": round(sum(m["netR"] > 0 for _, m in sonuc) / len(sonuc) * 100, 1) if sonuc else None,
        "secilen": adaylar[0] if adaylar else None,
        "ilk10Aday": adaylar,
        "ikiDonemdeYeterliIslem": len(ikisinde),
        "ikiDonemdeKarli": len(karli_iki),
        "ikiDonemdeKarliIlk5": [{"kural": k, "egitim": m, "kontrol": c} for k, m, c in karli_iki[:5]],
    }
    with open(a.out, "w") as f:
        json.dump(rapor, f, ensure_ascii=False, indent=1)
    print(json.dumps(rapor, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
