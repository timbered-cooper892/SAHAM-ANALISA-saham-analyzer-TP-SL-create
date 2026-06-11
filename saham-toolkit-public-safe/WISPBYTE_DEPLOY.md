# Deploy Ke Wispbyte

Subdomain server kamu:

```text
your-domain.example
```

Dari halaman Wispbyte, port yang harus dipakai:

```text
0.0.0.0:8080
```

## Main File

Di popup `Missing Main File`, pilih:

```text
app.py
```

Kalau Wispbyte meminta startup command, isi:

```bash
python app.py
```

Jika `python` tidak jalan, coba:

```bash
python3 app.py
```

## Environment Variables

Set ini di panel Wispbyte:

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

Kalau panel tidak punya environment variables, aplikasi sekarang tetap default ke port `8080`.

## Yang Penting

Aplikasi harus listen ke semua interface:

```text
0.0.0.0
```

Kode sudah memakai:

```python
app.run(host="0.0.0.0", PORT=8080
```

Jangan pakai:

```text
111.0.0.1
localhost
```

## Link Web

Setelah server running, buka:

```text
https://your-domain.example
```

Login:

```text
Akun: admin
Password: change-this-password
```

## Fitur Realtime

Jika `REALTIME_ENABLED=1`, aplikasi otomatis menjalankan monitor rolling di background. Defaultnya memproses 10 saham per 30 menit supaya cocok untuk RAM 512 MB dan disk 1 GB. Output tampil di menu `Realtime`.

TP/SL hanya muncul jika ada data laporan resmi IDX + data pasar yang cukup. Tidak ada TP/SL saham yang bisa dijamin 100% akurat.

Chart memakai TradingView embed di browser pengguna, jadi server tidak perlu menarik data chart real-time sendiri.

## TradingView Suite Dan Fokus Emiten

- Buka `https://your-domain.example/tradingview/BBCA` untuk advanced chart, technical analysis, symbol info, fundamental data, company profile, top stories, dan panel TP/SL aplikasi.
- Di halaman yang sama ada `Auto TP/SL Calculation`: nilai `Market/Technical`, `Bandar Volume`, `Entry Range`, dan `Composite` otomatis. Composite menggabungkan data harga, broker summary API/upload jika tersedia, proxy Bandar Volume, laporan IDX/fundamental, berita, dan dokumen yang tersedia.
- Gunakan kartu `Fokus 1 Emiten` di dashboard untuk mengumpulkan berita, fundamental, laporan IDX resmi/upload, dokumen pendukung, lalu membuat rekomendasi buy/watch/wait dan TP/SL bila data cukup.
- Untuk bandarmology otomatis tanpa upload, isi `RAPIDAPI_IDX_KEYS=
- Aktifkan `RAPIDAPI_TECHNICAL_ENABLED=1` dan `RAPIDAPI_SENTIMENT_ENABLED=1` agar support/resistance/ATR, technical signal, retail sentiment, bandar sentiment, dan foreign flow ikut memperkuat TP/SL serta entry.
- Untuk pembanding, isi `INDEXALPHA_API_KEYS=
- Untuk provider broker-summary lain yang punya JSON API, isi `BANDAR_CUSTOM_API_URL` dan `BANDAR_CUSTOM_API_KEY`.
- Untuk bandarmology asli manual, upload file Broker Summary Stockbit/Ajaib/IDX-style sebagai CSV/XLSX melalui `Fokus 1 Emiten` atau `Upload Dokumen`. Aplikasi membaca file itu lokal; TradingView tidak menyediakan broker summary mentah ke server.

## Jika Masih Web Server Isn't Running

1. Pastikan main file yang dipilih adalah `app.py`.
2. Pastikan startup command `python app.py`.
3. Pastikan port di panel sama dengan `PORT=8080
4. Cek console, harus muncul kira-kira:

```text
Running on http://0.0.0.0:8080
```
