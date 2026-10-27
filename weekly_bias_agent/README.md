# Haftalık Bias Ajanı

Kripto paralar için haftalık yön bias'ı (LONG / SHORT / NÖTR) bulan ve bulduğu bias'ı
walk-forward yöntemiyle backtest eden ajan.

| Dosya | Görev |
|---|---|
| `agent.py` | Tek coin: veri, kurallar, walk-forward backtest, rapor |
| `scan.py` | Top 50 coin listesi + her coin için bias ve backtest özeti |
| `test_agent.py` | Doğruluk testleri (`python3 -m unittest -v test_agent`) |

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
