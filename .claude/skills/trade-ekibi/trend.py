#!/usr/bin/env python3
"""Trend takibi stratejileri (literatürde en sağlam kanıtı olan aile), günlük veri, önceden sabit kurallar.

S1 BTC trend      : BTC günlük kapanış > 100 günlük SMA ise BTC, değilse nakit. Günlük kontrol.
S2 Sepet trend    : BTC > 100g SMA iken, kapanışı kendi 50g SMA'sının üstündeki coinlere eşit ağırlık; haftalık.
S3 Göreceli mom.  : BTC > 100g SMA iken son 28 günde en çok getiri yapan 10 coin, eşit ağırlık; haftalık.
Kıyas             : BTC al-tut, 50 coin eşit ağırlık al-tut (haftalık yeniden dengelenen).
Kararlar kapanmış mumla verilir, ertesi günün açılışında uygulanır. Komisyon: her alım/satımda %0,1.
Not: evren BUGÜNKÜ top 50 → sepet stratejilerinde hayatta kalan yanlılığı (survivorship) vardır; S1'de yoktur.
"""
import json, math, sys, os, calendar, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt

GUN = bt.GUN
KOM = 0.001


def veri(semboller, bas, bit, cache):
    D = {}
    for s in semboller:
        try:
            m = bt.aralik_cek(s, "1d", bas, bit, cache)
        except SystemExit:
            continue
        if m:
            D[s] = {x["t"]: x for x in m}
    return D


def sma(seri, n):
    return sum(seri[-n:]) / n if len(seri) >= n else None


def calis(D, gunler, strateji):
    """Her gün: hedef ağırlıkları kapanmış veriyle hesapla, ertesi gün açılışından kapanışa getiriyi uygula."""
    agirlik = {}
    eq, tepe, dd, egri, piyasada = 1.0, 1.0, 0.0, [], 0
    kapanis = {s: [] for s in D}
    for gi in range(len(gunler) - 1):
        t, t2 = gunler[gi], gunler[gi + 1]
        for s in D:
            if t in D[s]:
                kapanis[s].append(D[s][t]["c"])
        haftalik = time.gmtime(t2 / 1000).tm_wday == 0  # pazartesi açılışında dengele
        yeni = strateji(kapanis, haftalik, agirlik)
        if yeni is not None:
            devir = sum(abs(yeni.get(s, 0) - agirlik.get(s, 0)) for s in set(yeni) | set(agirlik))
            eq *= 1 - KOM * devir
            agirlik = yeni
        # t2 günü getirisi (açılıştan kapanışa değil, kapanıştan kapanışa: t kapanışında karar → t2 kapanışına kadar)
        r = 0.0
        for s, w in agirlik.items():
            if t in D[s] and t2 in D[s]:
                r += w * (D[s][t2]["c"] / D[s][t]["c"] - 1)
        eq *= 1 + r
        piyasada += sum(agirlik.values()) > 0
        tepe = max(tepe, eq); dd = min(dd, eq / tepe - 1)
        egri.append(r)
    yil = (gunler[-1] - gunler[0]) / (365 * GUN)
    ort = sum(egri) / len(egri); sd = math.sqrt(sum((x - ort) ** 2 for x in egri) / len(egri))
    return {"getiri": (eq - 1) * 100, "yillik": (eq ** (1 / yil) - 1) * 100 if eq > 0 else -100,
            "maxDD": dd * 100, "sharpe": ort / sd * math.sqrt(365) if sd else 0, "piyasada": piyasada / len(egri) * 100}


def btc_trend(k, haftalik, w):
    b = k["BTCUSDT"]
    m = sma(b, 100)
    if m is None:
        return {}
    return {"BTCUSDT": 1.0} if b[-1] > m else {}


def btc_al_tut(k, haftalik, w):
    return {"BTCUSDT": 1.0} if not w else None


def sepet_al_tut(k, haftalik, w):
    if not haftalik and w:
        return None
    ok = [s for s in k if len(k[s]) >= 1]
    return {s: 1 / len(ok) for s in ok} if ok else {}


def sepet_trend(k, haftalik, w):
    b = k["BTCUSDT"]; mb = sma(b, 100)
    if mb is None or b[-1] <= mb:
        return {}  # piyasa filtresi kapalı: hemen nakde geç (günlük)
    if not haftalik and w:
        return None
    ok = [s for s in k if (m := sma(k[s], 50)) is not None and k[s][-1] > m]
    return {s: 1 / len(ok) for s in ok} if ok else {}


def goreceli_mom(k, haftalik, w):
    b = k["BTCUSDT"]; mb = sma(b, 100)
    if mb is None or b[-1] <= mb:
        return {}
    if not haftalik and w:
        return None
    ok = sorted([s for s in k if len(k[s]) >= 29], key=lambda s: -(k[s][-1] / k[s][-29] - 1))[:10]
    return {s: 1 / len(ok) for s in ok} if ok else {}


STRATEJI = {"S1 BTC trend (100g)": btc_trend, "S2 Sepet trend": sepet_trend, "S3 Göreceli momentum": goreceli_mom,
            "Kıyas: BTC al-tut": btc_al_tut, "Kıyas: 50 coin al-tut": sepet_al_tut}


def main():
    sem, cache = sys.argv[1], sys.argv[2]
    ts = lambda d: calendar.timegm(time.strptime(d, "%Y-%m-%d")) * 1000
    bas, bit = ts("2021-01-01"), ts("2026-10-25")
    D = veri(open(sem).read().split(), bas, bit, cache)
    for ad, (a, b) in {"2021-04 → 2023-12 (tasarım)": ("2021-04-15", "2023-12-31"),
                       "2024-01 → 2026-10 (kontrol)": ("2024-01-01", "2026-10-24")}.items():
        # 100 günlük ısınma için başlangıçtan önceki veriler de kullanılır
        gunler = sorted(t for t in D["BTCUSDT"] if ts(a) - 110 * GUN <= t <= ts(b))
        print("==", ad, "| coin:", len(D))
        for sad, f in STRATEJI.items():
            # ısınma dönemi stratejiye veri olarak girer, performans yalnız dönem içinden ölçülür
            Dk = {s: {t: v for t, v in D[s].items() if t <= ts(b)} for s in D}
            g = [t for t in gunler]
            r = calis_donem(Dk, g, f, ts(a))
            print(f"  {sad:24} getiri {r['getiri']:+8.1f}%  yıllık {r['yillik']:+6.1f}%  max düşüş {r['maxDD']:6.1f}%  Sharpe {r['sharpe']:5.2f}  piyasada %{r['piyasada']:.0f}")


def calis_donem(D, gunler, f, olcum_bas):
    """Isınma günlerinde sadece veri birikir (pozisyon yok), ölçüm olcum_bas'tan başlar."""
    kapanis = {s: [] for s in D}
    for t in gunler:
        if t >= olcum_bas:
            break
        for s in D:
            if t in D[s]:
                kapanis[s].append(D[s][t]["c"])
    kalan = [t for t in gunler if t >= olcum_bas]
    # calis() kapanis'ı sıfırdan kurduğu için ısınmayı önceden doldurulmuş listeyle sağlarız
    agirlik = {}
    eq, tepe, dd, egri, piyasada = 1.0, 1.0, 0.0, [], 0
    for gi in range(len(kalan) - 1):
        t, t2 = kalan[gi], kalan[gi + 1]
        for s in D:
            if t in D[s]:
                kapanis[s].append(D[s][t]["c"])
        haftalik = time.gmtime(t2 / 1000).tm_wday == 0
        yeni = f({s: v for s, v in kapanis.items() if v and t in D[s]}, haftalik, agirlik)
        if yeni is not None:
            devir = sum(abs(yeni.get(s, 0) - agirlik.get(s, 0)) for s in set(yeni) | set(agirlik))
            eq *= 1 - KOM * devir
            agirlik = yeni
        r = sum(w * (D[s][t2]["c"] / D[s][t]["c"] - 1) for s, w in agirlik.items() if t in D[s] and t2 in D[s])
        eq *= 1 + r
        piyasada += sum(agirlik.values()) > 0
        tepe = max(tepe, eq); dd = min(dd, eq / tepe - 1)
        egri.append(r)
    yil = (kalan[-1] - kalan[0]) / (365 * GUN)
    ort = sum(egri) / len(egri); sd = math.sqrt(sum((x - ort) ** 2 for x in egri) / len(egri))
    return {"getiri": (eq - 1) * 100, "yillik": (eq ** (1 / yil) - 1) * 100 if eq > 0 else -100,
            "maxDD": dd * 100, "sharpe": ort / sd * math.sqrt(365) if sd else 0, "piyasada": piyasada / len(egri) * 100}


def sinyal(sem, cache):
    """Bugünkü hedef pozisyonlar (son KAPANMIŞ günlük mumla)."""
    import scanner as sc
    L = open(sem).read().split()
    k = {}
    for s in L:
        try:
            m = [x for x in sc.mumlar(s, "1d", 200) if x["closed"]]
        except Exception:
            continue
        if m:
            k[s] = [x["c"] for x in m]
    b = k["BTCUSDT"]; mb = sma(b, 100)
    rejim = b[-1] > mb
    print(json.dumps({
        "btc_kapanis": b[-1], "btc_sma100": round(mb, 2), "piyasa_filtresi": "ACIK (risk al)" if rejim else "KAPALI (nakit)",
        "S1_BTC_trend": {"BTCUSDT": 1.0} if rejim else {},
        "S2_sepet_trend": sepet_trend(k, True, {}),
        "S3_goreceli_momentum": goreceli_mom(k, True, {}),
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "sinyal":
        sinyal(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        main()
