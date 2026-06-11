# Saham Fundamental Toolkit

Toolkit Python untuk membantu analisa saham Indonesia dan global dengan dua mesin utama:

1. `scripts/analyze_stocks.py` - mengambil data pasar/laporan keuangan via `yfinance`, menghitung metrik fundamental, lalu memberi skor riset.
2. `scripts/process_news.py` - mengambil berita terkini via GDELT dan Google News RSS, lalu mengubah headline/snippet menjadi sinyal sentimen, risiko, dan tag katalis.

Ada juga `scripts/run_pipeline.py` untuk menjalankan keduanya dalam satu perintah.

Sekarang toolkit juga bisa dijalankan sebagai web app di hosting Python seperti Bot-Hosting.net:

```powershell
python main.py
```

Panduan deploy Bot-Hosting.net ada di:

```text
C:\saham-fundamental-toolkit\BOT_HOSTING_DEPLOY.md
```

Panduan deploy Wispbyte ada di:

```text
C:\saham-fundamental-toolkit\WISPBYTE_DEPLOY.md
```

> Catatan penting: output toolkit ini adalah bahan triage riset, bukan rekomendasi beli/jual. Untuk keputusan investasi, cek laporan keuangan resmi, keterkinian data, valuasi relatif, likuiditas, dan risiko berita dari sumber primer.

## Instalasi

Buka PowerShell:

```powershell
cd C:\saham-fundamental-toolkit
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Jika `py` tidak tersedia, gunakan:

```powershell
python -m venv .venv
```

## Watchlist

Contoh watchlist tersedia di:

```text
C:\saham-fundamental-toolkit\data\watchlist.example.csv
```

Kolom yang dipakai:

```text
ticker,company,aliases,country,sector
```

Untuk saham Indonesia, gunakan format Yahoo Finance seperti:

```text
BBCA.JK
BBRI.JK
TLKM.JK
ASII.JK
```

Untuk saham luar negeri:

```text
AAPL
MSFT
NVDA
```

## Watchlist Semua Saham Indonesia

File Excel IDX dari kamu sudah bisa dikonversi otomatis:

```powershell
python scripts\build_idx_watchlist.py --input "C:\Users\Alfath\Downloads\Daftar Saham  - 20260610.xlsx" --output data\watchlist.idx.csv
```

Hasilnya:

```text
C:\saham-fundamental-toolkit\data\watchlist.idx.csv
```

File ini berisi 957 ticker IDX dengan format Yahoo Finance `.JK`, contoh `AALI.JK`, `BBCA.JK`, `TLKM.JK`, beserta nama emiten, papan pencatatan, jumlah saham tercatat, dan tanggal pencatatan mentah dari Excel.

## Monitor Semua Saham Lokal

Untuk memantau berita/info terbaru semua saham Indonesia, gunakan runner khusus IDX:

```powershell
python scripts\monitor_idx.py --mode news --days 1 --max-records 5 --no-gdelt
```

Atau pakai launcher PowerShell:

```powershell
.\run_idx_monitor.ps1 -Mode news -Days 1 -MaxRecords 5
```

Saya sarankan mulai dari `--no-gdelt` karena GDELT kadang memberi rate limit `429`; Google News RSS biasanya lebih stabil untuk monitoring ringan.

Output utama:

```text
outputs\idx_monitor\idx_news_summary.csv
outputs\idx_monitor\idx_news_items.csv
outputs\idx_monitor\idx_news_digest.md
```

`idx_news_summary.csv` akan tetap memuat saham tanpa berita dengan `article_count = 0` dan `sentiment_label = no_news`, sehingga seluruh watchlist tetap bisa dipantau. Kalau hanya ingin melihat saham yang punya berita, tambahkan `--hide-empty`.

Untuk memproses per batch, misalnya 100 saham pertama:

```powershell
python scripts\monitor_idx.py --mode news --days 1 --max-records 5 --no-gdelt --offset 0 --limit 100
```

Batch berikutnya:

```powershell
python scripts\monitor_idx.py --mode news --days 1 --max-records 5 --no-gdelt --offset 100 --limit 100
```

Untuk fundamental + berita pada batch tertentu:

```powershell
python scripts\monitor_idx.py --mode full --days 3 --max-records 3 --no-gdelt --offset 0 --limit 50
```

Catatan: menjalankan fundamental untuk seluruh 957 saham bisa lama dan rawan rate limit dari Yahoo Finance. Untuk pemantauan harian, mode `news` lebih cocok; fundamental bisa dijalankan per batch atau hanya untuk kandidat yang muncul dari berita.

## Realtime Rolling Monitor

Untuk hosting kecil, realtime dibuat sebagai rolling batch, bukan mengambil semua 957 saham setiap detik. Defaultnya memproses 10 saham per siklus, lalu lanjut ke batch berikutnya. Ini sengaja dibuat ringan untuk server RAM 512 MB dan disk 1 GB.

```powershell
python scripts\realtime_monitor.py --once --batch-size 10 --max-records 2 --no-macro
```

Mode loop:

```powershell
python scripts\realtime_monitor.py --interval-minutes 30 --batch-size 10 --max-records 2 --no-macro
```

Output utama:

```text
outputs\realtime\latest_recommendations.json
outputs\realtime\latest_recommendations.csv
outputs\realtime\latest_recommendations.html
outputs\realtime\state.json
```

Di web hosting, set:

```text
REALTIME_ENABLED=1
REALTIME_INTERVAL_MINUTES=30
REALTIME_BATCH_SIZE=10
REALTIME_NEWS_DAYS=1
REALTIME_MAX_RECORDS=2
REALTIME_CLEANUP_DAYS=1
REALTIME_NO_MACRO=1
REALTIME_TOP=60
BANDAR_PROVIDER=auto
RAPIDAPI_IDX_KEYS=
RAPIDAPI_BANDAR_DAYS=30
RAPIDAPI_TECHNICAL_ENABLED=1
RAPIDAPI_TECHNICAL_DAYS=30
RAPIDAPI_SENTIMENT_ENABLED=1
RAPIDAPI_SENTIMENT_DAYS=7
RAPIDAPI_REQUEST_SLEEP_SECONDS=1.15
RAPIDAPI_IDX_HOST=indonesia-stock-exchange-idx.p.rapidapi.com
INDEXALPHA_API_KEYS=
INDEXALPHA_API_TOKEN=
BANDAR_LOOKBACK_DAYS=7
BANDAR_MARKET=RG
BANDAR_INVESTOR=all
BANDAR_API_CACHE_SECONDS=1800
BANDAR_CUSTOM_API_URL=
BANDAR_CUSTOM_API_KEY=
BANDAR_CUSTOM_API_HEADER=Authorization
BANDAR_CUSTOM_API_PREFIX=Bearer
```

AI tidak wajib untuk mengolah data. Engine bawaan memakai rule-based scoring yang bisa jalan tanpa API key. AI bisa ditambahkan nanti untuk ringkasan naratif yang lebih halus, tetapi keputusan skor, sumber, TP/SL, dan confidence tetap harus transparan.

Chart memakai TradingView embed di browser pengguna. Server hanya mengirim halaman HTML, jadi server tidak menarik data chart real-time sendiri dan lebih aman dari beban RAM/CPU/rate limit. Halaman `/chart/BBCA` atau `/tradingview/BBCA` menampilkan TradingView Suite langsung di aplikasi.

## Olah Dokumen

Dokumen bisa diproses dari CLI:

```powershell
python scripts\document_processor.py --input "C:\path\laporan.pdf" --input "C:\path\berita.xlsx" --output-dir outputs\document_analysis
```

Format yang didukung: PDF, DOCX, XLSX/XLS, CSV, TXT, MD, HTML, dan JSON. Output berisi ticker yang terdeteksi, sentiment, tag dampak, risk flags, snippets, dan confidence.

## Fokus 1 Emiten

Untuk mencari seluruh data penting dari satu emiten target:

```powershell
python scripts\issuer_deep_dive.py --ticker BBCA --days 7 --max-records 10
```

Dengan upload laporan resmi IDX dan dokumen pendukung:

```powershell
python scripts\issuer_deep_dive.py --ticker BBCA --report-file "C:\path\FinancialStatement-BBCA.xlsx" --document-input "C:\path\folder-dokumen"
```

Output utama:

```text
outputs\issuer_deep_dive\issuer_deep_dive_BBCA_YYYYMMDD_HHMMSS.html
outputs\issuer_deep_dive\issuer_deep_dive_BBCA_YYYYMMDD_HHMMSS.json
```

Di web app, gunakan kartu `Fokus 1 Emiten`.

## Auto TP/SL Di TradingView Suite

Halaman `/tradingview/BBCA` sekarang menampilkan dua kalkulasi otomatis:

- `Market/Technical TP/SL`: memakai data harga pasar, ATR, support/resistance, SMA20/SMA50, RSI, volume, dan risk/reward.
- `Bandar Volume`: memakai RapidAPI IDX bandar accumulation/distribution sebagai provider utama jika key tersedia, membandingkan dengan Index Alpha jika key tersedia, lalu fallback ke custom API/file Stockbit/IDX-style yang diupload; jika semuanya tidak ada, sistem memakai proxy akumulasi/distribusi dari volume spike, OBV, Accumulation/Distribution Line, Money Flow Index, dan price-volume action.
- `RapidAPI Technical`: memakai RSI, MACD, stochastic, Bollinger Bands, ATR, OBV, VWAP, trend, pivot, support, dan resistance untuk memperkuat TP/SL dan entry.
- `RapidAPI Sentiment`: memakai retail sentiment, bandar sentiment, foreign flow, dan top broker net flow sebagai penguat/risk warning.
- `Entry Range`: area entry bertahap, breakout entry, dan invalidation level.
- `Composite TP/SL`: menggabungkan hasil teknikal + Bandar Volume dengan laporan IDX resmi yang tersedia, skor fundamental, berita terbaru, dokumen pendukung, dan rekomendasi internal.

CLI:

```powershell
python scripts\tp_sl_calculator.py --ticker BBCA --news-days 3 --news-records 3
```

Catatan: widget TradingView resmi berjalan sebagai iframe dan tidak membuka API harga/indikatornya ke server aplikasi. Karena itu kalkulasi otomatis memakai data pasar lokal via `yfinance` dan ditampilkan berdampingan dengan TradingView Suite.

Untuk bandarmology otomatis tanpa upload, isi `RAPIDAPI_IDX_KEYS` di `.env` atau environment hosting. Bisa satu key atau banyak key dipisah koma; jika satu key terkena limit/error, aplikasi otomatis mencoba key berikutnya. Provider RapidAPI memanggil:

```text
/api/analysis/bandar/accumulation/{ticker}?days=30
/api/analysis/bandar/distribution/{ticker}?days=30
```

Index Alpha tetap bisa dipakai sebagai pembanding. Isi `INDEXALPHA_API_KEYS` jika tersedia. Sistem menyimpan cache JSON selama `BANDAR_API_CACHE_SECONDS`, lalu memakai provider dengan kualitas data terbaik pada `Composite TP/SL` dan `Entry Range`.

```text
BANDAR_PROVIDER=auto
RAPIDAPI_IDX_KEYS=
RAPIDAPI_BANDAR_DAYS=30
RAPIDAPI_TECHNICAL_ENABLED=1
RAPIDAPI_TECHNICAL_DAYS=30
RAPIDAPI_SENTIMENT_ENABLED=1
RAPIDAPI_SENTIMENT_DAYS=7
RAPIDAPI_REQUEST_SLEEP_SECONDS=1.15
INDEXALPHA_API_KEYS=
BANDAR_LOOKBACK_DAYS=7
BANDAR_MARKET=RG
BANDAR_INVESTOR=all
BANDAR_API_CACHE_SECONDS=1800
```

Jika punya provider broker-summary lain yang menyediakan JSON API, set:

```text
BANDAR_PROVIDER=auto
BANDAR_CUSTOM_API_URL=https://contoh-api/broker-summary?ticker={ticker}&from={from}&to={to}&market={market}
BANDAR_CUSTOM_API_KEY=
BANDAR_CUSTOM_API_HEADER=Authorization
BANDAR_CUSTOM_API_PREFIX=Bearer
```

Format response yang paling mudah dibaca adalah objek `{"data": [...]}` atau `{"rows": [...]}` berisi kolom broker, buy/sell value, buy/sell lot, net value/net lot, dan avg buy/sell.

Jika punya data broker summary/bandar asli dari Stockbit/Ajaib/IDX, upload sebagai Excel/CSV lewat `Fokus 1 Emiten` atau `Upload Dokumen`. Sistem akan membaca kolom seperti `Broker`, `Buy Value`, `Sell Value`, `Net Value`, `Buy Lot`, `Sell Lot`, `Net Lot`, `Avg Buy`, dan `Avg Sell`, lalu memakainya sebagai sinyal broker summary pada `Composite TP/SL` dan `Entry Range`. Tanpa API token dan tanpa data broker summary, `Bandar Volume` adalah proxy berbasis harga-volume.

Jika semua API key limit/gagal, halaman TradingView Suite akan menampilkan opsi sumber bandarmology lain: RapidAPI IDX, Index Alpha, Stockbit, Ajaib Terminal, IDX Broker Summary, dan TradingView chart. Data dari sumber itu baru bisa masuk kalkulasi otomatis jika ada API JSON atau file export/upload; aplikasi tidak mengarang angka broker summary.

Stockbit dan Ajaib cocok untuk bandarmology karena fiturnya memang Broker Summary/Bandar Detector, tetapi akses otomatis realtime dari web/app mereka biasanya butuh login/fitur berbayar dan tidak disediakan sebagai public API. TradingView dipakai untuk chart, technicals, financials, profile, dan news widget, tetapi widget TradingView tidak memberikan data broker summary mentah ke server aplikasi. Karena itu aplikasi tidak melakukan scraping/login Stockbit/Ajaib; gunakan API resmi/berlisensi, custom API, atau upload/export file jika API belum tersedia.

## Cleanup Otomatis

Untuk membersihkan file generated lama:

```powershell
python scripts\cleanup_system.py --days 2
```

Cek tanpa menghapus:

```powershell
python scripts\cleanup_system.py --days 2 --dry-run
```

## Web App Untuk Hosting

Jalankan lokal:

```powershell
cd C:\saham-fundamental-toolkit
.\.venv\Scripts\Activate.ps1
$env:APP_USERNAME=admin
$env:APP_PASSWORD=change-this-password
python main.py
```

Buka:

```text
http://localhost:8080
```

Fitur web:

- Login pakai akun dan password.
- Monitor semua saham IDX per batch.
- Realtime rolling monitor untuk berita dan kandidat riset seluruh saham IDX.
- Ranking kandidat riset otomatis berbasis berita, fundamental, dokumen, dan laporan resmi IDX yang tersedia.
- Upload dan olah PDF, Word DOCX, Excel, CSV, TXT, HTML, dan JSON.
- TradingView Suite per ticker IDX: advanced chart, technical analysis, symbol info, fundamental data, company profile, top stories, plus panel TP/SL dari engine riset bila data tersedia.
- Fokus 1 Emiten untuk berita, fundamental, IDX resmi, dokumen pendukung, rekomendasi buy/watch/wait, dan TP/SL bila data cukup.
- Cleanup otomatis file generated/cache lama agar server tidak cepat penuh.
- Delete file output dan upload langsung dari halaman web.
- Analisa laporan keuangan per ticker.
- Analisa laporan resmi IDX dengan upload XLSX/PDF atau auto-download jika endpoint IDX bisa diakses.
- Research view TP/SL ilustratif, actionability, dan sumber data yang membentuk kesimpulan.
- Download output CSV, XLSX, HTML, dan JSON.
- API JSON: `/api/financials/BBCA.JK?period=annual&years=5`, `/api/recommendations`, `/api/realtime/status`.

Untuk Bot-Hosting.net, set startup file ke `main.py`, upload `requirements.txt`, lalu set environment variable `APP_USERNAME`, `APP_PASSWORD`, `APP_SECRET_KEY`, dan `PORT`.

## Analisa Laporan Keuangan

CLI:

```powershell
python scripts\financial_statement_analysis.py --ticker BBCA.JK --period annual --years 5
```

Output:

```text
outputs\financial_statement_analysis_BBCA_JK_YYYYMMDD_HHMMSS.csv
outputs\financial_statement_analysis_BBCA_JK_YYYYMMDD_HHMMSS.xlsx
outputs\financial_statement_analysis_BBCA_JK_YYYYMMDD_HHMMSS.html
outputs\financial_statement_analysis_BBCA_JK_YYYYMMDD_HHMMSS.json
```

Yang dianalisa:

- Pendapatan, laba kotor, laba operasi, laba bersih, EPS.
- Aset, liabilitas, ekuitas, utang, kas, current ratio.
- Operating cash flow, capex, free cash flow, dividend coverage.
- Margin, ROE, ROA, debt/equity, cash/debt, OCF/net income.
- Skor kualitas laporan keuangan dan flag risiko otomatis.

## Analisa Laporan Resmi IDX + TP/SL

Mode ini memakai sumber utama laporan resmi IDX.

CLI dengan auto IDX:

```powershell
python scripts\idx_official_report_analysis.py --ticker BBCA --years 2025
```

Jika endpoint IDX terkena Cloudflare/403 dari server, unduh file resmi dari halaman IDX lalu jalankan:

```powershell
python scripts\idx_official_report_analysis.py --ticker BBCA --no-auto-idx --report-file "C:\path\FinancialStatement-2025-Tahunan-BBCA.xlsx"
```

Output:

```text
outputs\idx_official_analysis\idx_official_analysis_BBCA_JK_YYYYMMDD_HHMMSS.html
outputs\idx_official_analysis\idx_official_analysis_BBCA_JK_YYYYMMDD_HHMMSS.json
outputs\idx_official_analysis\idx_official_analysis_BBCA_JK_source_reasoning_YYYYMMDD_HHMMSS.csv
```

Yang disajikan:

- Kesimpulan utama: layak masuk buy candidate, watchlist, wait, atau tidak layak beli saat ini.
- TP dan SL ilustratif berbasis rule engine.
- Rasio utama: revenue growth, net income growth, net margin, ROE, ROA, debt/equity, current ratio, FCF, PE, PBV.
- Sumber data yang dipakai untuk tiap kesimpulan.
- Peringatan data gap: metrik yang tidak terbaca, PDF tanpa XLSX, atau endpoint IDX diblokir.

Sumber untuk kesimpulan:

- `IDX official financial report attachments`: sumber utama angka laporan keuangan.
- `Yahoo Finance via yfinance`: harga pasar, market cap, saham beredar, dan histori harga untuk TP/SL.
- `Derived by local rule engine`: asumsi valuasi seperti target PE/PB, TP, SL, dan actionability.

Catatan penting: TP/SL dan keputusan "layak/tidak" adalah research view otomatis. Itu bukan nasihat investasi personal dan bukan instruksi transaksi. Tidak ada TP/SL saham yang bisa dijamin 100% akurat. Gunakan untuk screening, lalu cek ulang laporan IDX, berita, likuiditas, teknikal, dan profil risiko pribadi.

## Cara Pakai Cepat

Jalankan pipeline penuh:

```powershell
cd C:\saham-fundamental-toolkit
.\.venv\Scripts\Activate.ps1
python scripts\run_pipeline.py --watchlist data\watchlist.example.csv --days 7 --max-records 20
```

Pipeline penuh juga bisa langsung memakai Excel IDX:

```powershell
python scripts\run_pipeline.py --idx-excel "C:\Users\Alfath\Downloads\Daftar Saham  - 20260610.xlsx" --days 1 --max-records 3 --no-gdelt --offset 0 --limit 50
```

Output akan masuk ke:

```text
C:\saham-fundamental-toolkit\outputs
```

File utama yang biasanya dibuka:

```text
outputs\fundamental_scores_YYYYMMDD_HHMMSS.xlsx
outputs\fundamental_scores_YYYYMMDD_HHMMSS.html
outputs\news_digest.md
outputs\news_summary.csv
```

## Jalankan Berita Saja

```powershell
python scripts\process_news.py --watchlist data\watchlist.example.csv --days 7 --max-records 25
```

Hasil:

```text
outputs\news_items.csv
outputs\news_summary.csv
outputs\news_digest.md
```

Makna output berita:

- `avg_sentiment`: skala -1 sampai +1.
- `risk_article_count`: jumlah artikel yang mengandung flag risiko.
- `top_impact_tags`: tag seperti `earnings`, `rates`, `fx`, `commodity`, `regulation`, `corporate_action`.
- `top_headlines`: headline paling berdampak menurut skor sentimen dan confidence.

## Jalankan Fundamental Saja

```powershell
python scripts\analyze_stocks.py --watchlist data\watchlist.example.csv
```

Jika ingin menggabungkan hasil berita:

```powershell
python scripts\analyze_stocks.py --watchlist data\watchlist.example.csv --news-summary outputs\news_summary.csv
```

## Metodologi Skor Fundamental

Skor fundamental memakai weighted average dari lima blok:

```text
Quality                30%
Growth                 20%
Balance sheet          20%
Valuation              20%
Shareholder return     10%
```

Contoh metrik:

- Quality: ROE, ROA, net margin, operating margin, FCF margin.
- Growth: revenue growth, net income growth.
- Balance sheet: debt/equity, cash/debt, current ratio, FCF margin.
- Valuation: trailing PE, forward PE, PBV, FCF yield, earnings yield.
- Shareholder return: dividend yield dan FCF yield.

Bucket hasil:

```text
A - fundamental sangat kuat untuk riset lanjut
B - kuat / layak masuk shortlist
C - campuran / watchlist
D - lemah atau butuh bukti tambahan
Data tipis - cek sumber primer
```

`data_confidence` menunjukkan seberapa lengkap data yang berhasil diambil. Skor tinggi dengan confidence rendah harus dianggap belum layak untuk keputusan.

## Sumber Data

- Fundamental dan harga: [`yfinance`](https://pypi.org/project/yfinance/), yang mengambil data publik dari Yahoo Finance.
- Berita global/multibahasa: [GDELT DOC API](https://gdelt.github.io/).
- Berita pencarian umum: [Google News RSS](https://news.google.com/rss).

Keterbatasan yang perlu diingat:

- Data Yahoo Finance bisa kosong, terlambat, atau berbeda dari laporan resmi emiten.
- GDELT dan Google News memberi artikel/headline, bukan validasi fakta final.
- Sentimen berbasis lexicon sederhana, jadi ironi, konteks sektor, dan berita campuran bisa salah dibaca.
- Untuk saham bank, asuransi, komoditas, dan emiten dengan struktur khusus, interpretasi rasio perlu penyesuaian sektor.

## Contoh Query Ticker Langsung

Tanpa watchlist:

```powershell
python scripts\run_pipeline.py --tickers BBCA.JK,TLKM.JK,AAPL,MSFT --days 3 --max-records 15
```

## Menambah Query Makro

```powershell
python scripts\process_news.py --watchlist data\watchlist.example.csv --macro-query "coal price OR batubara OR China demand" --macro-query "rupiah OR BI Rate OR inflasi"
```

## Workflow Riset Yang Disarankan

1. Jalankan `run_pipeline.py`.
2. Buka file HTML/XLSX fundamental.
3. Ambil kandidat dengan skor fundamental tinggi dan confidence memadai.
4. Baca `news_digest.md` untuk risiko dan katalis terbaru.
5. Buka artikel asli dan laporan keuangan resmi.
6. Buat model valuasi atau skenario sendiri sebelum menyimpulkan saham tersebut menarik.
