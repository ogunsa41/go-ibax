#!/usr/bin/env python3
"""Backtest işlemleri üzerinde filtre / çıkış kuralı taraması ve kontrol dönemi doğrulaması.

Kural: Filtreler YALNIZCA eğitim döneminde seçilir. Seçim ölçütü önceden sabittir: en az MIN_ISLEM
işlem yapan kombinasyonlar arasında net ortalama R'nin istatistiksel gücü (ort / standart hata).
Seçilen ilk ADAY kombinasyon kontrol döneminde (hiç bakılmamış veri) bir kez ölçülür.

Kullanım:
  python3 optimize.py --egitim DIZIN_genis DIZIN_dar --kontrol DIZIN_genis DIZIN_dar [--out rapor.json]
"""
import argparse
import itertools
import json
import math

MIN_ISLEM = 40
ADAY = 10
GUVEN_SIRA = {"DUSUK": 0, "ORTA": 1, "YUKSEK": 2}

IZGARA = {
    "secim": ["genis", "dar"],
    "maxRange": [1, 2, 3, 6],
    "zone": ["hepsi", "FVG"],
    "minGuven": ["DUSUK", "ORTA", "YUKSEK"],
    "displacement": [False, True],
    "maxStop": [99, 2.0, 1.5],
    "minStop": [0, 0.5],
    "klKalite": ["hepsi", "AB"],
    "cikis": ["TP2", "1.5R", "2R", "3R", "TP1"],
}


def yukle(dizinler):
    out = {}
    for d in dizinler:
        with open(f"{d}/islemler.json") as f:
            veri = json.load(f)
        out[veri["meta"]["secim"]] = veri
    return out


def uygun(x, k):
    return (x["durum"] == "KAPANDI" and x["rN"] <= k["maxRange"]
            and (k["zone"] == "hepsi" or x["zone"] == k["zone"])
            and GUVEN_SIRA[x["guven"]] >= GUVEN_SIRA[k["minGuven"]]
            and (not k["displacement"] or x["displacement"])
            and k["minStop"] <= x["stopYuzde"] <= k["maxStop"]
            and (k["klKalite"] == "hepsi" or x["klKalite"] in ("A", "B")))


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
    p.add_argument("--egitim", nargs=2, required=True)
    p.add_argument("--kontrol", nargs=2, required=True)
    p.add_argument("--out", default="optimize-rapor.json")
    a = p.parse_args()
    egitim, kontrol = yukle(a.egitim), yukle(a.kontrol)
    anahtarlar = list(IZGARA)
    sonuc = []
    for deger in itertools.product(*IZGARA.values()):
        k = dict(zip(anahtarlar, deger))
        m = olc(egitim[k["secim"]]["isler"], k)
        if m["islem"] >= MIN_ISLEM:
            sonuc.append((k, m))
    sonuc.sort(key=lambda km: -km[1]["skor"])
    taban = {"secim": "genis", "maxRange": 6, "zone": "hepsi", "minGuven": "DUSUK", "displacement": False,
             "maxStop": 99, "minStop": 0, "klKalite": "hepsi", "cikis": "TP2"}
    adaylar = []
    for k, m in sonuc[:ADAY]:
        adaylar.append({"kural": k, "egitim": m, "kontrol": olc(kontrol[k["secim"]]["isler"], k)})
    rapor = {
        "egitimDonemi": f'{egitim["genis"]["meta"]["bas"]} – {egitim["genis"]["meta"]["bit"]}',
        "kontrolDonemi": f'{kontrol["genis"]["meta"]["bas"]} – {kontrol["genis"]["meta"]["bit"]}',
        "denenenKombinasyon": math.prod(len(v) for v in IZGARA.values()),
        "yeterliIslemliKombinasyon": len(sonuc),
        "mevcutSistem": {"egitim": olc(egitim["genis"]["isler"], taban), "kontrol": olc(kontrol["genis"]["isler"], taban)},
        "secilen": adaylar[0] if adaylar else None,
        "ilk10Aday": adaylar,
        "egitimdeKarliOran": round(sum(m["netR"] > 0 for _, m in sonuc) / len(sonuc) * 100, 1) if sonuc else None,
    }
    with open(a.out, "w") as f:
        json.dump(rapor, f, ensure_ascii=False, indent=1)
    print(json.dumps(rapor, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
