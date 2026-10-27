# Haftalık Bias Ajanı

Kripto paralar için haftalık yön bias'ı (LONG / SHORT / NÖTR) bulan ve bulduğu bias'ı
walk-forward yöntemiyle backtest eden ajan.

| Dosya | Görev |
|---|---|
| `agent.py` | Tek coin: veri, kurallar, walk-forward backtest, rapor |
| `scan.py` | Top 50 coin listesi + her coin için bias ve backtest özeti |
| `rr_study.py` | Bias yönüyle TP/SL (2R/1R) stop mesafesi hassasiyet çalışması |
| `reversal_agent.py` | Dönüş noktası ajanı: fiyatın döndüğü yerlerde ortak özellik arar |
| `test_agent.py`, `test_reversal.py` | Doğruluk testleri (`python3 -m unittest -v test_agent test_reversal`) |

```bash
python3 agent.py --symbol BTCUSDT                       # long/short, sabit 1x
python3 agent.py --symbol ETHUSDT --long-only --vol-target 0.5
python3 scan.py                                          # top 50, ~4-5 dk
```

Veri: Binance spot günlük mumları (`api.binance.com`, erişilemezse resmi ayna
`data-api.binance.vision`); isteğe bağlı Crypto.com. Haftalar Pazartesi 00:00 UTC'de başlar.

## Nasıl çalışır

1. Günlük mumlar haftalığa çevrilir; kapanmamış gün ve hafta atılır.
2. Her kural, *t* haftası için yalnızca *t-1* sonuna kadarki veriyle sinyal üretir.
3. Walk-forward: her hafta, o haftadan önceki veride en yüksek Sharpe'ı veren kural seçilir
   (hiçbiri pozitif değilse NÖTR). İlk 52 hafta yalnızca ısınmadır.
4. Pozisyon haftanın açılışında alınır, kapanışında kapanır. Pozisyon değişiminde komisyon
   düşülür. Hafta içi ters hareket teminatı silerse (ör. 1x short'ta fiyat +%100) hafta
   -%100 sayılır ve hesap o noktada biter.
5. İsteğe bağlı volatilite hedefleme: pozisyon = hedef vol / son 12 haftanın volü (≤ 1x).

## Aday kurallar ve kaynakları

| Kural | Mantık | Kaynak |
|---|---|---|
| `momentum_1w`, `momentum_4w` | Son 1 / 4 haftanın yönü devam eder | Liu & Tsyvinski, *Risks and Returns of Cryptocurrency* (RFS 2021); ICT "weekly open üstü/altı" |
| `tsmom_12w`, `tsmom_26w` | 12 / 26 haftalık time-series momentum | Moskowitz, Ooi & Pedersen, *Time Series Momentum* (JFE 2012) |
| `close_location` | Haftalık kapanış aralığın üst/alt %30'unda | Klasik price action (kapanış gücü) |
| `brooks_trend_bar` | Gövde ≥ aralığın %50'si ve kapanış uç %40'ta | Al Brooks, *Trading Price Action* serisi (trend bar tanımı) |
| `outside_week` | Önceki haftanın iki ucunu aşıp uçta kapanış | Larry Williams, *Long-Term Secrets to Short-Term Trading*; Brooks outside bar |
| `breakout_prev_range` | Önceki haftanın high/low'u dışında kapanış | Klasik kırılım |
| `liquidity_sweep` | PWL süpürülüp üstünde kapanış → long (PWH için tersi) | ICT: previous week high/low likidite, draw on liquidity |
| `turtle_soup_20w` | Yeni 20 haftalık uç (önceki uç ≥ 3 hafta önce) ve kapanış geri içeride → ters yön | Raschke & Connors, *Street Smarts* (1995) — günlükten haftalığa uyarlandı |
| `premium_discount_4w` | 4 haftalık dealing range'in alt çeyreği long, üst çeyreği short | ICT premium/discount, equilibrium |
| `trend_sma10`, `trend_sma20`, `trend_and_close_loc` | Ortalama üstü/altı, trend + güçlü kapanış | Klasik trend takibi |
| `reversal_1w`, `close_location_rev` | Yukarıdakilerin tersi | Kontrol kuralları (mean reversion) |

Ek olarak rapor, gün bazlı getiri profilini verir (Kinateder & Papavassiliou: Bitcoin'de
klasik gün etkisi yok; Quantpedia / NYDIG: Pazartesi-Cuma güçlü — kaynaklar çelişiyor).

Uygulanmayanlar: inside week kırılımı ve ICT Pazartesi/Salı giriş zamanlaması, hafta içi
stop emirli giriş gerektirir; bu model yalnızca haftanın açılış-kapanışını işler.

## Aşırı uyum (overfitting) kontrolleri

- **Walk-forward**: kural seçimi hiçbir zaman o haftanın veya sonrasının verisini görmez.
- **PSR** (Bailey & López de Prado 2012): gerçek Sharpe'ın 0'dan büyük olma olasılığı,
  çarpıklık ve basıklık düzeltmeli. %95 altı anlamlı sayılmaz.
- **Deflated Sharpe** (Bailey & López de Prado, JPM 2014): eğitimde en iyi çıkan kuralın,
  16 kural denendiği hesaba katılınca hâlâ anlamlı olup olmadığı.
- **Testler**: gelecekteki veri değiştirildiğinde geçmiş sinyal ve pozisyonların değişmediği,
  canlı "bu hafta" sinyalinin backtestle birebir aynı olduğu, komisyon, likidasyon ve kural
  tanımları doğrulanır.

## Sınırlamalar

Fonlama ücreti (perp), kayma ve spread modellenmez. Kural seti ve varsayılan ayarlar
(long-only, %50 vol hedefi) aynı veri üzerinde seçildiği için sonuçlar hafif iyimserdir.
Yatırım tavsiyesi değildir.

## Dönüş noktası ajanı (`reversal_agent.py`)

```bash
python3 reversal_agent.py --list reports/top50_<tarih>.csv   # ~3 dk
python3 reversal_agent.py --symbols BTCUSDT ETHUSDT --swing-atr 3
```

1. Günlük grafikte ATR tabanlı zigzag ile dönüşler bulunur: bir tepe/dip, fiyat ondan
   `--swing-atr` × ATR(14) (varsayılan 4) ters yöne gidince onaylanır.
2. Her gün için dip (günün low'u) ve tepe (günün high'ı) tarafında özellikler hesaplanır:
   önceki gün/hafta/ay ucu, haftalık/aylık açılış, eski swing seviyeleri (aynı ve ters yön),
   yuvarlak sayılar, SMA20/50/200, 20 haftalık SMA, Fibonacci bölgeleri (son tamamlanmış
   bacak), RSI aşırılığı, SMA20'den uzaklık, hacim patlaması, uzun fitil, geniş mum, önceki
   hafta ucunu / eski swing'i süpürüp geri kapanış, ardışık mumlar, haftanın günü. Seviyeye
   "değme" = 0.25 ATR tolerans. Seviyeler yalnızca o günden önce bilinen veriden gelir.
3. Lift = dönüş günlerindeki sıklık / tüm günlerdeki sıklık. Binom testi + Benjamini-Hochberg,
   coinler arası tutarlılık, ilk %70 / son %30 dönem tekrarı ve zigzag'dan bağımsız olarak
   özellik görülen günden 10 gün sonraki getiri raporlanır.

Not: dönüşler hareketin en uç günü olduğundan "SMA20'den uzak", "RSI aşırı", "geniş mum"
gibi özellikler dönüşlerde kısmen mekanik olarak sık görülür; ileriye dönük getiri sütunu bu
yüzden asıl sınamadır.

### Sol taraf yapısı

Rapor ayrıca dönüş fiyatının solunda (geçmişte) yapı olup olmadığını inceler: son 1 yılın
küçük (2 ATR) ve büyük swing seviyeleri, aynı seviyeden ≥ 2 kez dönüş, son 180 günün hacim
profili (POC, HVN, LVN), kapanmamış FVG'ler ve bozulmamış order block'lar. Normal günlerle
kıyaslamanın yanında daha adil bir test de yapılır: fiyat 20 günlük yeni dip/tepe yaptığında,
solda yapı varken ve yokken o günün gerçek dönüş olma oranı.
