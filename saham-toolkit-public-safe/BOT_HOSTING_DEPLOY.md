# Deploy Ke Bot-Hosting.net

Panduan ini menyiapkan Saham Fundamental Toolkit sebagai web app Python ringan yang bisa diakses oleh orang yang punya akun dan password.

## File Penting

Upload seluruh folder ini ke Bot-Hosting.net:

```text
main.py
app.py
web_app.py
requirements.txt
scripts/
data/watchlist.idx.csv
```

`main.py` adalah startup file utama.

## Environment Variables

Set di dashboard Bot-Hosting.net:

```text
APP_USERNAME=admin
APP_PASSWORD=change-this-password
APP_SECRET_KEY=change-this-to-a-long-random-secret
PORT=8080
REALTIME_ENABLED=1
REALTIME_INTERVAL_MINUTES=30
REALTIME_BATCH_SIZE=10
REALTIME_NEWS_DAYS=1
REALTIME_MAX_RECORDS=2
REALTIME_CLEANUP_DAYS=1
REALTIME_NO_MACRO=1
REALTIME_TOP=60
```

Catatan:

- `APP_USERNAME` dan `APP_PASSWORD` adalah akun login web. Bagikan hanya ke orang yang boleh memakai aplikasi.
- `APP_SECRET_KEY` dipakai Flask untuk session. Isi string acak panjang.
- Jika panel hosting memberi port khusus, isi `PORT` sesuai port itu.

## Startup

Di tab Startup:

```text
Bot Python file: main.py
Requirements file: requirements.txt
```

Jika panel memakai startup command manual:

```bash
python main.py
```

## Port / Network

Aktifkan port web di panel hosting. Aplikasi mendengarkan:

```text
0.0.0.0:$PORT
```

Default lokal jika `PORT` kosong:

```text
8080
```

## Cara Pakai Setelah Online

1. Buka URL atau IP:PORT dari Bot-Hosting.net.
2. Login dengan akun `admin` dan password `change-this-password`.
3. Pilih:
   - `Monitor Semua Saham IDX` untuk berita/info semua saham lokal.
   - `Realtime` untuk kandidat riset terbaru dari berita, fundamental, dokumen, dan laporan IDX yang tersedia.
   - `Fokus 1 Emiten` untuk deep dive satu ticker sampai rekomendasi TP/SL.
   - `TradingView Suite` untuk chart dan widget TradingView langsung di aplikasi.
   - `Analisa Laporan Keuangan` untuk satu ticker, misalnya `BBCA.JK`.
   - `Analisa Resmi IDX + TP/SL` untuk upload laporan resmi IDX atau mencoba auto-download IDX.
   - `Upload Dokumen` untuk PDF, Word DOCX, Excel, CSV, TXT, HTML, atau JSON.
4. Buka halaman `Output Files` untuk download atau delete CSV/XLSX/HTML/JSON.

## API

Contoh endpoint JSON:

```text
/api/financials/BBCA.JK?period=annual&years=5
```

Untuk quarterly:

```text
/api/financials/BBCA.JK?period=quarterly&years=8
```

## Saran Resource

Mode `news` untuk semua IDX lebih ringan daripada `full`.

Untuk hosting kecil, jalankan batch:

```text
offset 0 limit 100
offset 100 limit 100
offset 200 limit 100
```

Analisa fundamental dan laporan keuangan memakai data provider publik sehingga bisa terkena rate limit. Gunakan jeda `sleep` 1-3 detik untuk batch besar.

Realtime memakai rolling batch. Default 10 saham per 30 menit agar server 512 MB tetap ringan. Cleanup otomatis default `REALTIME_CLEANUP_DAYS=1`.

## Bandarmology Otomatis

Untuk membaca broker summary/bandarmology otomatis tanpa upload file, gunakan API yang memang menyediakan akses data. Setting `.env`:

```text
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

Jika `RAPIDAPI_IDX_KEYS` atau `INDEXALPHA_API_KEYS` berisi banyak key, aplikasi mencoba key berikutnya saat key sebelumnya limit/error. RapidAPI IDX dipakai untuk bandar accumulation/distribution, Index Alpha menjadi pembanding jika tersedia. Tanpa token API, aplikasi tetap bisa membaca file Broker Summary Stockbit/Ajaib/IDX-style yang diupload sebagai CSV/XLSX. TradingView hanya dipakai untuk chart/widget dan tidak memberikan broker summary mentah ke server aplikasi.

## Catatan IDX Cloudflare

Endpoint resmi:

```text
https://www.idx.co.id/primary/ListedCompany/GetFinancialReport
```

Sebagian server bisa terkena `403 Cloudflare`. Jika ini terjadi, gunakan mode upload di web app:

1. Buka halaman IDX `Laporan Keuangan dan Tahunan`.
2. Cari kode emiten dan tahun.
3. Unduh file `FinancialStatement-...xlsx` atau PDF resmi.
4. Upload file itu di form `Analisa Resmi IDX + TP/SL`.

Jika file XLSX tersedia, parser bisa membaca angka lebih terstruktur. Jika hanya PDF, aplikasi tetap mencatat sumber resmi tetapi confidence angka lebih rendah karena ekstraksi tabel PDF tidak selalu stabil.
