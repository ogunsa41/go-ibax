---
name: trade-ekibi
description: Haftalık bias → 4H key level → 4H CRT → 15m CHoCH → 15m giriş → kontrol → sunum zincirini ajan ekibiyle çalıştırır. Kullanım: /trade-ekibi BTC_USDT
---

# Trade Analiz Ekibi — Orkestrasyon

Argüman: enstrüman adı (Crypto.com formatı, ör. `BTC_USDT`, `ETH_USDT`). Verilmezse kullanıcıya sor.

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
- Veri kaynağı: Crypto.com MCP (`mcp__Crypto_com__get_candlestick`, zaman dilimleri `7D`, `1D`,
  `4h`, `15m`; çağrı başına en fazla 50 mum, en yeni başta, `data[0]` kapanmamış mum).
