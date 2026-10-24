---
name: trade-ekibi
description: Haftalık bias → 4H key level → 4H CRT → 15m CHoCH → 15m giriş → kontrol → sunum zincirini ajan ekibiyle çalıştırır. Veri Binance'ten gelir. Kullanım: /trade-ekibi BTCUSDT
---

# Trade Analiz Ekibi — Orkestrasyon

Argüman: Binance sembolü (ör. `BTCUSDT`, `ETHUSDT`). Verilmezse kullanıcıya sor.

Ajanlar birbirini çağıramaz; zinciri SEN yönetirsin. Her ajanı `Agent` aracıyla, ilgili
`subagent_type` ile, önceki adımların JSON çıktılarını prompt'a ekleyerek SIRAYLA çalıştır
(her adım bir öncekine bağlı, `run_in_background: false`).

| # | Ajan | Girdi | Durma koşulu |
|---|---|---|---|
| 1 | `haftalik-bias` | sembol | `bias = NEUTRAL` → 6 ve 7'ye atla |
| 2 | `key-level-4h` | 1 | seviye yok → 6 ve 7'ye atla |
| 3 | `crt-4h` | 1, 2 | `AKTIF` CRT yok → 6 ve 7'ye atla |
| 4 | `choch-15m` | 3 | `ONAYLANDI` değil → 6 ve 7'ye atla (durum: BEKLE) |
| 5 | `giris-15m` | 1, 3, 4 | — |
| 6 | `kontrolcu` | tüm çıktılar | `KALDI` → `yeniden_calistir` listesindeki ajanları düzeltme notuyla 1 kez yeniden çalıştır, sonra kontrolcüyü tekrar çalıştır |
| 7 | `sunucu` | tüm çıktılar + kontrol | — |

Kurallar:
- Ajan çıktılarındaki son JSON bloğunu aynen bir sonraki ajana aktar; değiştirme.
- Kontrolcü her zaman çalışır (zincir erken dursa bile, durma kararını doğrular).
- Kullanıcıya yalnızca `sunucu` raporunu göster; ardından raporun sonundaki sorularla
  kullanıcıyla iletişime devam et. Kullanıcı cevap verirse ilgili ajanı tekrar çağır.
- Veri kaynağı: Binance public API, ortak script `.claude/skills/trade-ekibi/binance.py`
  (`mum` → OHLC, `crt` → 2/3/çok mumlu CRT taraması). Mumlar eskiden yeniye sıralı,
  `kapali: false` mum oluşmaktadır. Futures verisi için `--futures` ekle.
- Script Binance'e erişemezse zinciri durdur ve kullanıcıya hatayı olduğu gibi bildir;
  başka kaynaktan veya tahmini veri kullanma.

## Top 50 tarama ve panel
- `python3 .claude/skills/trade-ekibi/scanner.py tara --top 50 --out <dizin>`: Binance'te 24 saatlik
  USDT hacmine göre ilk 50 coin'i (stabil coinler ve tokenize hisseler hariç) aynı kurallarla
  deterministik olarak tarar. Her coin'i bir kategoriye koyar (FVG bekleniyor, CHoCH bekleniyor, …)
  ve panel için `ozet.json` ile `d0..dN.json` dosyalarını üretir.
- Panel (CRT Trade Masası): https://claude.ai/artifact/F9ppD6A4rujkWZS4jZ7Ax1. Veriyi kendi
  veritabanının `tarama` koleksiyonundan okur; dosyalar oraya ArtifactData ile yazılır.
- Tarayıcı LLM ajanlarının yerine geçmez: tek bir coin için derin analiz istenirse yukarıdaki zinciri çalıştır.
