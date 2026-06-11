import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, abort, jsonify, redirect, render_template_string, request, send_from_directory, session, url_for


ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
OUTPUT_ROOT = ROOT / "outputs" / "hosted"
WATCHLIST_IDX = ROOT / "data" / "watchlist.idx.csv"
UPLOAD_ROOT = ROOT / "uploads" / "idx_reports"
DOC_UPLOAD_ROOT = ROOT / "uploads" / "documents"
FOCUS_UPLOAD_ROOT = ROOT / "uploads" / "issuer_focus"
REALTIME_OUTPUT = OUTPUT_ROOT / "realtime"

jobs: Dict[str, Dict[str, Any]] = {}
jobs_lock = threading.Lock()
realtime_start_lock = threading.Lock()
realtime_auto_started = False


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(ROOT / ".env")


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_int(value: Optional[str], default: int, minimum: int = 0, maximum: int = 5000) -> int:
    try:
        parsed = int(value) if value not in (None, "") else default
    except ValueError:
        parsed = default
    return max(minimum, min(maximum, parsed))


def parse_float(value: Optional[str], default: float, minimum: float = 0.0, maximum: float = 60.0) -> float:
    try:
        parsed = float(value) if value not in (None, "") else default
    except ValueError:
        parsed = default
    return max(minimum, min(maximum, parsed))


def command_text(command: List[str]) -> str:
    return subprocess.list2cmdline(command)


def start_job(title: str, command: List[str]) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "title": title,
        "status": "queued",
        "command": command_text(command),
        "log": "",
        "returncode": None,
        "created_at": now_text(),
        "started_at": "",
        "finished_at": "",
    }
    with jobs_lock:
        jobs[job_id] = job

    def runner() -> None:
        with jobs_lock:
            jobs[job_id]["status"] = "running"
            jobs[job_id]["started_at"] = now_text()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                with jobs_lock:
                    current = jobs[job_id]["log"]
                    updated = current + line
                    jobs[job_id]["log"] = updated[-60000:]
            returncode = process.wait()
            with jobs_lock:
                jobs[job_id]["returncode"] = returncode
                jobs[job_id]["status"] = "finished" if returncode == 0 else "failed"
                jobs[job_id]["finished_at"] = now_text()
        except Exception as exc:
            with jobs_lock:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["returncode"] = -1
                jobs[job_id]["log"] += f"\nJob error: {exc}\n"
                jobs[job_id]["finished_at"] = now_text()

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return job_id


def recent_jobs(limit: int = 12) -> List[Dict[str, Any]]:
    with jobs_lock:
        return list(reversed(list(jobs.values())))[0:limit]


def find_running_realtime_job() -> Optional[Dict[str, Any]]:
    with jobs_lock:
        for job in reversed(list(jobs.values())):
            if job["title"].lower().startswith("realtime monitor") and job["status"] in {"queued", "running"}:
                return job
    return None


def build_realtime_command(once: bool = False, form: Optional[Dict[str, str]] = None) -> List[str]:
    form = form or {}
    interval = parse_float(form.get("interval_minutes") or os.environ.get("REALTIME_INTERVAL_MINUTES"), 30.0, 1.0, 1440.0)
    batch_size = parse_int(form.get("batch_size") or os.environ.get("REALTIME_BATCH_SIZE"), 10, 1, 100)
    days = parse_int(form.get("days") or os.environ.get("REALTIME_NEWS_DAYS"), 1, 1, 7)
    max_records = parse_int(form.get("max_records") or os.environ.get("REALTIME_MAX_RECORDS"), 2, 1, 10)
    cleanup_days = parse_float(form.get("cleanup_days") or os.environ.get("REALTIME_CLEANUP_DAYS"), 1.0, 1.0, 14.0)
    top = parse_int(form.get("top") or os.environ.get("REALTIME_TOP"), 60, 10, 200)
    command = [
        sys.executable,
        str(SCRIPTS / "realtime_monitor.py"),
        "--watchlist",
        str(WATCHLIST_IDX),
        "--output-dir",
        str(REALTIME_OUTPUT),
        "--state-file",
        str(REALTIME_OUTPUT / "state.json"),
        "--interval-minutes",
        str(interval),
        "--batch-size",
        str(batch_size),
        "--days",
        str(days),
        "--max-records",
        str(max_records),
        "--cleanup-days",
        str(cleanup_days),
        "--top",
        str(top),
    ]
    if once:
        command.append("--once")
    if form.get("use_gdelt"):
        command.append("--use-gdelt")
    no_macro_default = os.environ.get("REALTIME_NO_MACRO", "1").strip().lower() in {"1", "true", "yes", "on"}
    if form.get("no_macro") or (not form and no_macro_default):
        command.append("--no-macro")
    return command


def start_realtime_loop_if_needed() -> Optional[str]:
    global realtime_auto_started
    enabled = os.environ.get("REALTIME_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return None
    with realtime_start_lock:
        if realtime_auto_started:
            return None
        running = find_running_realtime_job()
        if running:
            realtime_auto_started = True
            return str(running["id"])
        job_id = start_job("Realtime monitor otomatis", build_realtime_command(once=False))
        realtime_auto_started = True
        return job_id


def read_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def latest_realtime_payload() -> Dict[str, Any]:
    return read_json_file(REALTIME_OUTPUT / "latest_recommendations.json")


def realtime_state() -> Dict[str, Any]:
    return read_json_file(REALTIME_OUTPUT / "state.json")


def normalize_chart_ticker(ticker: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9.:\-_]", "", ticker or "").upper()
    clean = clean.replace(".JK", "")
    if clean.startswith("IDX:"):
        clean = clean.split(":", 1)[1]
    return clean[:12] or "BBCA"


def tradingview_symbol(ticker: str) -> str:
    return f"IDX:{normalize_chart_ticker(ticker)}"


def recommendation_for_ticker(ticker: str) -> Dict[str, Any]:
    wanted = normalize_chart_ticker(ticker)
    payload = latest_realtime_payload()
    for row in payload.get("rows", []):
        row_ticker = normalize_chart_ticker(str(row.get("ticker", "")))
        if row_ticker == wanted:
            return row
    return {}


def list_output_files(limit: int = 80) -> List[Dict[str, Any]]:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    return list_files_under(OUTPUT_ROOT, limit=limit)


def list_upload_files(limit: int = 80) -> List[Dict[str, Any]]:
    files = list_files_under(UPLOAD_ROOT, limit=limit)
    files.extend(list_files_under(DOC_UPLOAD_ROOT, limit=limit))
    files.extend(list_files_under(FOCUS_UPLOAD_ROOT, limit=limit))
    files.sort(key=lambda item: item["modified"], reverse=True)
    return files[:limit]


def list_files_under(root: Path, limit: int = 80) -> List[Dict[str, Any]]:
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        stat = path.stat()
        rel = path.relative_to(root).as_posix()
        files.append(
            {
                "name": rel,
                "root": root.name,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    files.sort(key=lambda item: item["modified"], reverse=True)
    return files[:limit]


def remove_empty_parent_dirs(path: Path, stop_root: Path) -> None:
    stop_root = stop_root.resolve()
    parent = path.parent.resolve()
    while parent != stop_root and parent.is_relative_to(stop_root):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent.resolve()


def safe_file_in_root(root: Path, filename: str) -> Optional[Path]:
    if not filename:
        return None
    target = (root / filename).resolve()
    root_resolved = root.resolve()
    if target.is_file() and target.is_relative_to(root_resolved):
        return target
    return None


BASE_TEMPLATE = """
<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }}</title>
  <style>
    :root {
      --bg: #f4f7fb;
      --ink: #152033;
      --muted: #637184;
      --line: #dce5ef;
      --panel: #ffffff;
      --accent: #0f766e;
      --accent-2: #2563eb;
      --warn: #b45309;
      --bad: #b91c1c;
      --good: #047857;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--ink); font-family: Arial, sans-serif; }
    header { background: #101827; color: #fff; padding: 16px 24px; }
    header .wrap, main { max-width: 1180px; margin: 0 auto; }
    h1 { margin: 0; font-size: 22px; }
    h2 { margin: 0 0 14px; font-size: 18px; }
    p { line-height: 1.5; }
    nav { display: flex; gap: 12px; margin-top: 10px; flex-wrap: wrap; }
    nav a { color: #dbeafe; text-decoration: none; font-size: 14px; }
    main { padding: 24px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
    .mini-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 12px 0; }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px; }
    .metric { border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #fbfdff; }
    .metric .label { font-size: 12px; color: var(--muted); }
    .metric .value { font-size: 20px; font-weight: 800; margin-top: 4px; }
    .pill { display: inline-block; border: 1px solid #c7d7e8; border-radius: 999px; padding: 4px 8px; font-size: 12px; background: #f7fbff; color: #243447; }
    .chart-frame { width: 100%; min-height: 560px; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: #fff; }
    .chart-frame iframe, .chart-frame > div { width: 100%; min-height: 560px; }
    .tv-grid { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(300px, 0.85fr); gap: 16px; align-items: start; }
    .tv-column { display: grid; gap: 16px; }
    .tv-widget-box { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: #fff; min-height: 430px; }
    .tv-widget-box.tall { min-height: 620px; }
    .tv-widget-box.compact { min-height: 250px; }
    .tv-widget-box.profile { min-height: 390px; }
    .tv-widget-box.financials { min-height: 500px; }
    .tv-widget-box .tradingview-widget-container { width: 100%; height: 100%; min-height: inherit; }
    .tv-widget-box .tradingview-widget-container__widget { width: 100%; height: 100%; min-height: inherit; }
    @media (max-width: 900px) { .tv-grid { grid-template-columns: 1fr; } }
    label { display: block; font-size: 13px; color: var(--muted); margin: 10px 0 5px; }
    input, select { width: 100%; padding: 10px 11px; border: 1px solid #cbd7e3; border-radius: 6px; font-size: 14px; background: #fff; }
    input[type="checkbox"] { width: auto; margin-right: 8px; }
    .check { display: flex; align-items: center; color: var(--ink); margin: 10px 0; }
    button, .button { display: inline-block; border: 0; border-radius: 6px; background: var(--accent); color: #fff; padding: 10px 14px; margin-top: 14px; cursor: pointer; text-decoration: none; font-weight: 700; font-size: 14px; }
    .button.secondary, button.secondary { background: var(--accent-2); }
    .button.light { background: #edf2f7; color: var(--ink); }
    table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid var(--line); }
    th, td { padding: 9px 10px; border-bottom: 1px solid #e8eef5; text-align: left; font-size: 13px; vertical-align: top; }
    th { background: #eaf1f8; color: #243447; }
    .muted { color: var(--muted); }
    .banner { padding: 12px 14px; border: 1px solid #f1c27d; background: #fff7ed; color: #7c2d12; border-radius: 8px; margin-bottom: 16px; }
    .status-running { color: var(--accent-2); font-weight: 700; }
    .status-finished { color: var(--good); font-weight: 700; }
    .status-failed { color: var(--bad); font-weight: 700; }
    pre { white-space: pre-wrap; background: #0b1220; color: #e5edf8; padding: 14px; border-radius: 8px; overflow: auto; max-height: 560px; }
    .footer-note { margin-top: 22px; font-size: 13px; color: var(--muted); }
  </style>
</head>
<body>
  <header>
    <div class="wrap">
      <h1>Saham Fundamental Toolkit</h1>
      <nav>
        <a href="{{ url_for('index') }}">Dashboard</a>
        <a href="{{ url_for('realtime') }}">Realtime</a>
        <a href="{{ url_for('chart_redirect') }}?ticker=BBCA">Chart</a>
        <a href="{{ url_for('outputs') }}">Output Files</a>
        <a href="{{ url_for('uploaded_files') }}">Upload Files</a>
        {% if auth_enabled %}<a href="{{ url_for('logout') }}">Logout</a>{% endif %}
      </nav>
    </div>
  </header>
  <main>
    {% if auth_warning %}<div class="banner">{{ auth_warning }}</div>{% endif %}
    {{ body|safe }}
    <div class="footer-note">Research support only. Verify important numbers with IDX filings, annual reports, and company disclosures.</div>
  </main>
</body>
</html>
"""


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("APP_SECRET_KEY") or secrets.token_hex(32)
    app_username = os.environ.get("APP_USERNAME") or ""
    app_password = os.environ.get("APP_PASSWORD") or ""
    auth_enabled = bool(app_username and app_password)
    auth_warning = "" if auth_enabled else "APP_USERNAME dan APP_PASSWORD belum diset. Untuk hosting publik, set environment variable ini agar halaman tidak terbuka bebas."

    def render_page(title: str, body: str):
        return render_template_string(
            BASE_TEMPLATE,
            title=title,
            body=body,
            auth_enabled=auth_enabled,
            auth_warning=auth_warning,
        )

    @app.before_request
    def require_login():
        if not auth_enabled:
            return None
        if request.endpoint in {"login", "static"}:
            return None
        if session.get("authenticated"):
            return None
        return redirect(url_for("login", next=request.path))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = ""
        if request.method == "POST":
            username_ok = secrets.compare_digest(request.form.get("username", ""), app_username)
            password_ok = secrets.compare_digest(request.form.get("password", ""), app_password)
            if username_ok and password_ok:
                session["authenticated"] = True
                session["username"] = app_username
                return redirect(request.args.get("next") or url_for("index"))
            error = "Akun atau password salah."
        body = f"""
        <div class="panel" style="max-width:420px;margin:40px auto;">
          <h2>Masuk</h2>
          <p class="muted">Masukkan akun dan password aplikasi.</p>
          {'<div class="banner">' + error + '</div>' if error else ''}
          <form method="post">
            <label>Akun</label>
            <input type="text" name="username" autocomplete="username" required>
            <label>Password</label>
            <input type="password" name="password" autocomplete="current-password" required>
            <button type="submit">Masuk</button>
          </form>
        </div>
        """
        return render_page("Login", body)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    def index():
        watchlist_status = "ada" if WATCHLIST_IDX.exists() else "belum ada"
        body = f"""
        <div class="grid">
          <section class="panel">
            <h2>Monitor Semua Saham IDX</h2>
            <p class="muted">Watchlist IDX: {watchlist_status}. Gunakan batch untuk ratusan emiten agar lebih stabil.</p>
            <form method="post" action="{url_for('start_idx_monitor')}">
              <label>Mode</label>
              <select name="mode">
                <option value="news">Berita saja</option>
                <option value="fundamental">Fundamental saja</option>
                <option value="full">Berita + fundamental</option>
              </select>
              <label>Hari berita ke belakang</label>
              <input type="number" name="days" value="1" min="1" max="30">
              <label>Max artikel per saham</label>
              <input type="number" name="max_records" value="5" min="1" max="50">
              <label>Offset batch</label>
              <input type="number" name="offset" value="0" min="0">
              <label>Limit batch</label>
              <input type="number" name="limit" value="100" min="1" max="957">
              <label>Jeda antar saham, detik</label>
              <input type="number" name="sleep" value="1" min="0" max="30" step="0.1">
              <label class="check"><input type="checkbox" name="no_gdelt" checked> Matikan GDELT agar lebih aman dari rate limit</label>
              <label class="check"><input type="checkbox" name="no_macro"> Jangan ambil berita makro</label>
              <label class="check"><input type="checkbox" name="hide_empty"> Sembunyikan saham tanpa berita</label>
              <button type="submit">Mulai Monitor</button>
            </form>
          </section>

          <section class="panel">
            <h2>Realtime Rekomendasi</h2>
            <p class="muted">Rolling batch untuk seluruh saham IDX. Aman untuk server kecil karena tidak menarik 957 saham sekaligus.</p>
            <form method="post" action="{url_for('start_realtime_once')}">
              <label>Interval loop, menit</label>
              <input type="number" name="interval_minutes" value="{os.environ.get('REALTIME_INTERVAL_MINUTES', '30')}" min="1" max="1440">
              <label>Batch size</label>
              <input type="number" name="batch_size" value="{os.environ.get('REALTIME_BATCH_SIZE', '10')}" min="1" max="100">
              <label>Hari berita ke belakang</label>
              <input type="number" name="days" value="{os.environ.get('REALTIME_NEWS_DAYS', '1')}" min="1" max="7">
              <label>Max artikel per saham</label>
              <input type="number" name="max_records" value="{os.environ.get('REALTIME_MAX_RECORDS', '2')}" min="1" max="10">
              <label>Cleanup file lama, hari</label>
              <input type="number" name="cleanup_days" value="{os.environ.get('REALTIME_CLEANUP_DAYS', '1')}" min="1" max="14" step="0.5">
              <label class="check"><input type="checkbox" name="no_macro" checked> Matikan berita makro agar ringan</label>
              <label class="check"><input type="checkbox" name="use_gdelt"> Aktifkan GDELT juga</label>
              <button type="submit">Run 1 Batch Sekarang</button>
              <button class="secondary" type="submit" formaction="{url_for('start_realtime_loop')}">Start Loop Realtime</button>
            </form>
            <p><a class="button light" href="{url_for('realtime')}">Lihat rekomendasi realtime</a></p>
          </section>

          <section class="panel">
            <h2>Analisa Laporan Keuangan</h2>
            <p class="muted">Analisa income statement, balance sheet, cash flow, margin, ROE/ROA, leverage, cash conversion, FCF, dan flag risiko.</p>
            <form method="post" action="{url_for('start_statement_analysis')}">
              <label>Ticker</label>
              <input name="ticker" value="BBCA.JK" required>
              <label>Periode</label>
              <select name="period">
                <option value="annual">Annual</option>
                <option value="quarterly">Quarterly</option>
              </select>
              <label>Jumlah periode</label>
              <input type="number" name="years" value="5" min="1" max="12">
              <button class="secondary" type="submit">Analisa Laporan</button>
            </form>
          </section>

          <section class="panel">
            <h2>Analisa Resmi IDX + TP/SL</h2>
            <p class="muted">Pakai laporan resmi IDX XLSX/PDF. Jika endpoint IDX diblokir Cloudflare, upload file XLSX/PDF dari halaman IDX.</p>
            <form method="post" action="{url_for('start_idx_official_analysis')}" enctype="multipart/form-data">
              <label>Ticker IDX</label>
              <input name="ticker" value="BBCA" required>
              <label>Tahun auto IDX</label>
              <input name="years" value="2025">
              <label>Upload laporan resmi IDX XLSX/PDF</label>
              <input type="file" name="report_files" multiple accept=".xlsx,.xls,.pdf">
              <label class="check"><input type="checkbox" name="no_auto_idx"> Jangan coba auto-download IDX, pakai file upload saja</label>
              <button class="secondary" type="submit">Analisa IDX Resmi</button>
            </form>
          </section>

          <section class="panel">
            <h2>Fokus 1 Emiten</h2>
            <p class="muted">Cari berita, fundamental, laporan IDX, dokumen pendukung, lalu buat ringkasan TP/SL dan buy/watch/wait untuk satu ticker target.</p>
            <form method="post" action="{url_for('start_issuer_deep_dive')}" enctype="multipart/form-data">
              <label>Ticker IDX</label>
              <input name="ticker" value="BBCA" required>
              <label>Hari berita ke belakang</label>
              <input type="number" name="days" value="7" min="1" max="30">
              <label>Max artikel berita</label>
              <input type="number" name="max_records" value="10" min="1" max="30">
              <label>Upload laporan resmi IDX XLSX/PDF</label>
              <input type="file" name="report_files" multiple accept=".xlsx,.xls,.pdf">
              <label>Upload dokumen tambahan</label>
              <input type="file" name="document_files" multiple accept=".pdf,.docx,.xlsx,.xls,.csv,.txt,.md,.html,.htm,.json">
              <label class="check"><input type="checkbox" name="no_auto_idx"> Jangan coba auto-download IDX</label>
              <label class="check"><input type="checkbox" name="use_gdelt"> Aktifkan GDELT juga</label>
              <button class="secondary" type="submit">Deep Dive Emiten</button>
            </form>
          </section>

          <section class="panel">
            <h2>API Cepat</h2>
            <p class="muted">Endpoint JSON untuk integrasi bot lain atau dashboard sederhana.</p>
            <p><code>/api/financials/BBCA.JK?period=annual&years=5</code></p>
            <a class="button light" href="{url_for('api_financials', ticker='BBCA.JK')}?period=annual&years=5">Coba API BBCA.JK</a>
          </section>

          <section class="panel">
            <h2>Upload Dokumen</h2>
            <p class="muted">Olah PDF, Word DOCX, Excel, CSV, TXT, HTML, dan JSON untuk mencari mention saham, risiko, dan sinyal berita.</p>
            <form method="post" action="{url_for('start_document_analysis')}" enctype="multipart/form-data">
              <label>Upload file</label>
              <input type="file" name="document_files" multiple accept=".pdf,.docx,.xlsx,.xls,.csv,.txt,.md,.html,.htm,.json">
              <label>Minimal mention per ticker</label>
              <input type="number" name="min_mentions" value="1" min="1" max="20">
              <button class="secondary" type="submit">Analisa Dokumen</button>
            </form>
          </section>

          <section class="panel">
            <h2>TradingView Suite</h2>
            <p class="muted">Buka advanced chart, technical analysis, fundamental data, profile, news, dan panel TP/SL dalam aplikasi.</p>
            <form method="get" action="{url_for('chart_redirect')}">
              <label>Ticker IDX</label>
              <input name="ticker" value="BBCA">
              <button type="submit">Buka TradingView Suite</button>
            </form>
          </section>
        </div>

        <section class="panel" style="margin-top:16px;">
          <h2>Recent Jobs</h2>
          {jobs_table()}
        </section>
        """
        return render_page("Dashboard", body)

    def jobs_table() -> str:
        rows = recent_jobs()
        if not rows:
            return '<p class="muted">Belum ada job.</p>'
        tr = []
        for job in rows:
            status = job["status"]
            tr.append(
                f"<tr><td><a href='{url_for('job_detail', job_id=job['id'])}'>{job['id']}</a></td>"
                f"<td>{job['title']}</td><td class='status-{status}'>{status}</td>"
                f"<td>{job['created_at']}</td><td>{job.get('returncode')}</td></tr>"
            )
        return "<table><thead><tr><th>ID</th><th>Job</th><th>Status</th><th>Created</th><th>Code</th></tr></thead><tbody>" + "".join(tr) + "</tbody></table>"

    @app.route("/jobs/idx-monitor", methods=["POST"])
    def start_idx_monitor():
        mode = request.form.get("mode", "news")
        if mode not in {"news", "fundamental", "full"}:
            mode = "news"
        days = parse_int(request.form.get("days"), 1, 1, 30)
        max_records = parse_int(request.form.get("max_records"), 5, 1, 50)
        offset = parse_int(request.form.get("offset"), 0, 0, 10000)
        limit = parse_int(request.form.get("limit"), 100, 1, 5000)
        sleep = parse_float(request.form.get("sleep"), 1.0, 0.0, 30.0)

        command = [
            sys.executable,
            str(SCRIPTS / "monitor_idx.py"),
            "--mode",
            mode,
            "--days",
            str(days),
            "--max-records",
            str(max_records),
            "--offset",
            str(offset),
            "--limit",
            str(limit),
            "--sleep",
            str(sleep),
            "--output-dir",
            str(OUTPUT_ROOT / "idx_monitor"),
        ]
        if request.form.get("no_gdelt"):
            command.append("--no-gdelt")
        if request.form.get("no_macro"):
            command.append("--no-macro")
        if request.form.get("hide_empty"):
            command.append("--hide-empty")
        job_id = start_job(f"IDX monitor {mode} offset {offset} limit {limit}", command)
        return redirect(url_for("job_detail", job_id=job_id))

    @app.route("/jobs/realtime-once", methods=["POST"])
    def start_realtime_once():
        command = build_realtime_command(once=True, form=request.form)
        job_id = start_job("Realtime monitor satu batch", command)
        return redirect(url_for("job_detail", job_id=job_id))

    @app.route("/jobs/realtime-loop", methods=["POST"])
    def start_realtime_loop():
        running = find_running_realtime_job()
        if running:
            return redirect(url_for("job_detail", job_id=running["id"]))
        command = build_realtime_command(once=False, form=request.form)
        job_id = start_job("Realtime monitor loop", command)
        return redirect(url_for("job_detail", job_id=job_id))

    @app.route("/jobs/document-analysis", methods=["POST"])
    def start_document_analysis():
        upload_dir = DOC_UPLOAD_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
        upload_dir.mkdir(parents=True, exist_ok=True)
        allowed = {".pdf", ".docx", ".xlsx", ".xls", ".csv", ".txt", ".md", ".html", ".htm", ".json"}
        saved_files: List[Path] = []
        for file in request.files.getlist("document_files"):
            if not file or not file.filename:
                continue
            suffix = Path(file.filename).suffix.lower()
            if suffix not in allowed:
                continue
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(file.filename).name)
            target = upload_dir / safe_name
            file.save(target)
            saved_files.append(target)
        if not saved_files:
            abort(400, "Tidak ada file dokumen yang didukung.")
        min_mentions = parse_int(request.form.get("min_mentions"), 1, 1, 20)
        command = [
            sys.executable,
            str(SCRIPTS / "document_processor.py"),
            "--input-dir",
            str(upload_dir),
            "--watchlist",
            str(WATCHLIST_IDX),
            "--output-dir",
            str(OUTPUT_ROOT / "document_analysis"),
            "--prefix",
            "document_analysis",
            "--min-mentions",
            str(min_mentions),
        ]
        job_id = start_job(f"Document analysis {len(saved_files)} file", command)
        return redirect(url_for("job_detail", job_id=job_id))

    @app.route("/jobs/issuer-deep-dive", methods=["POST"])
    def start_issuer_deep_dive():
        ticker = normalize_chart_ticker(request.form.get("ticker", ""))
        if not ticker:
            abort(400, "ticker is required")
        if not re.fullmatch(r"[A-Z0-9]{1,12}", ticker):
            abort(400, "ticker contains invalid characters")
        days = parse_int(request.form.get("days"), 7, 1, 30)
        max_records = parse_int(request.form.get("max_records"), 10, 1, 30)
        upload_dir = FOCUS_UPLOAD_ROOT / ticker / datetime.now().strftime("%Y%m%d_%H%M%S")
        report_dir = upload_dir / "idx_reports"
        document_dir = upload_dir / "documents"
        report_dir.mkdir(parents=True, exist_ok=True)
        document_dir.mkdir(parents=True, exist_ok=True)
        report_args: List[str] = []
        document_args: List[str] = []

        for file in request.files.getlist("report_files"):
            if not file or not file.filename:
                continue
            suffix = Path(file.filename).suffix.lower()
            if suffix not in {".xlsx", ".xls", ".pdf"}:
                continue
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(file.filename).name)
            target = report_dir / safe_name
            file.save(target)
            report_args.extend(["--report-file", str(target)])

        for file in request.files.getlist("document_files"):
            if not file or not file.filename:
                continue
            suffix = Path(file.filename).suffix.lower()
            if suffix not in {".pdf", ".docx", ".xlsx", ".xls", ".csv", ".txt", ".md", ".html", ".htm", ".json"}:
                continue
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(file.filename).name)
            target = document_dir / safe_name
            file.save(target)

        if any(document_dir.iterdir()):
            document_args.extend(["--document-input", str(document_dir)])

        command = [
            sys.executable,
            str(SCRIPTS / "issuer_deep_dive.py"),
            "--ticker",
            ticker,
            "--days",
            str(days),
            "--max-records",
            str(max_records),
            "--watchlist",
            str(WATCHLIST_IDX),
            "--output-dir",
            str(OUTPUT_ROOT / "issuer_deep_dive"),
            "--prefix",
            "issuer_deep_dive",
        ]
        command.extend(report_args)
        command.extend(document_args)
        if request.form.get("no_auto_idx"):
            command.append("--no-auto-idx")
        if request.form.get("use_gdelt"):
            command.append("--use-gdelt")
        job_id = start_job(f"Issuer deep dive {ticker}", command)
        return redirect(url_for("job_detail", job_id=job_id))

    @app.route("/jobs/idx-official-analysis", methods=["POST"])
    def start_idx_official_analysis():
        ticker = (request.form.get("ticker") or "").strip().upper().replace(".JK", "")
        if not ticker:
            abort(400, "ticker is required")
        if not re.fullmatch(r"[A-Z0-9]{1,12}", ticker):
            abort(400, "ticker contains invalid characters")
        years = request.form.get("years") or str(datetime.now().year)
        if not re.fullmatch(r"[0-9,; \\-]{4,40}", years):
            abort(400, "years contains invalid characters")

        upload_dir = UPLOAD_ROOT / ticker / datetime.now().strftime("%Y%m%d_%H%M%S")
        upload_dir.mkdir(parents=True, exist_ok=True)
        report_args: List[str] = []
        for file in request.files.getlist("report_files"):
            if not file or not file.filename:
                continue
            suffix = Path(file.filename).suffix.lower()
            if suffix not in {".xlsx", ".xls", ".pdf"}:
                continue
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(file.filename).name)
            target = upload_dir / safe_name
            file.save(target)
            report_args.extend(["--report-file", str(target)])

        command = [
            sys.executable,
            str(SCRIPTS / "idx_official_report_analysis.py"),
            "--ticker",
            ticker,
            "--years",
            years,
            "--output-dir",
            str(OUTPUT_ROOT / "idx_official_analysis"),
        ]
        command.extend(report_args)
        if request.form.get("no_auto_idx"):
            command.append("--no-auto-idx")
        job_id = start_job(f"IDX official analysis {ticker}", command)
        return redirect(url_for("job_detail", job_id=job_id))

    @app.route("/jobs/statement-analysis", methods=["POST"])
    def start_statement_analysis():
        ticker = (request.form.get("ticker") or "").strip().upper()
        if not ticker:
            abort(400, "ticker is required")
        if not re.fullmatch(r"[A-Z0-9.\-_]{1,24}", ticker):
            abort(400, "ticker contains invalid characters")
        period = request.form.get("period", "annual")
        if period not in {"annual", "quarterly"}:
            period = "annual"
        years = parse_int(request.form.get("years"), 5, 1, 12)
        command = [
            sys.executable,
            str(SCRIPTS / "financial_statement_analysis.py"),
            "--ticker",
            ticker,
            "--period",
            period,
            "--years",
            str(years),
            "--output-dir",
            str(OUTPUT_ROOT / "financials"),
        ]
        job_id = start_job(f"Financial statement analysis {ticker}", command)
        return redirect(url_for("job_detail", job_id=job_id))

    @app.route("/jobs/<job_id>")
    def job_detail(job_id: str):
        with jobs_lock:
            job = jobs.get(job_id)
        if not job:
            abort(404)
        body = f"""
        <section class="panel">
          <h2>{job['title']}</h2>
          <p>Status: <span class="status-{job['status']}">{job['status']}</span></p>
          <p class="muted">Command: <code>{job['command']}</code></p>
          <p><a class="button light" href="{url_for('outputs')}">Lihat output files</a></p>
          <pre id="log">{job['log'] or 'Job belum menulis log.'}</pre>
        </section>
        <script>
          async function refreshJob() {{
            const response = await fetch("{url_for('api_job', job_id=job_id)}");
            const data = await response.json();
            document.getElementById("log").textContent = data.log || "Job belum menulis log.";
            if (data.status === "running" || data.status === "queued") {{
              setTimeout(refreshJob, 2500);
            }}
          }}
          refreshJob();
        </script>
        """
        return render_page(f"Job {job_id}", body)

    @app.route("/api/jobs/<job_id>")
    def api_job(job_id: str):
        with jobs_lock:
            job = jobs.get(job_id)
        if not job:
            abort(404)
        return jsonify(job)

    @app.route("/api/financials/<ticker>")
    def api_financials(ticker: str):
        sys.path.insert(0, str(SCRIPTS))
        from financial_statement_analysis import analyze_financial_statements

        period = request.args.get("period", "annual")
        if period not in {"annual", "quarterly"}:
            period = "annual"
        years = parse_int(request.args.get("years"), 5, 1, 12)
        package = analyze_financial_statements(ticker.upper(), period=period, years=years)
        return jsonify(package)

    @app.route("/api/recommendations")
    def api_recommendations():
        payload = latest_realtime_payload()
        if not payload:
            return jsonify({"generated_at": "", "rows": [], "warning": "Belum ada output realtime."})
        return jsonify(payload)

    @app.route("/api/realtime/status")
    def api_realtime_status():
        return jsonify({"state": realtime_state(), "running_job": find_running_realtime_job()})

    @app.route("/api/levels/<ticker>")
    def api_levels(ticker: str):
        clean = normalize_chart_ticker(ticker)
        sys.path.insert(0, str(SCRIPTS))
        from tp_sl_calculator import build_levels

        news_days = parse_int(request.args.get("news_days"), 3, 1, 14)
        news_records = parse_int(request.args.get("news_records"), 3, 1, 10)
        package = build_levels(
            clean,
            watchlist=WATCHLIST_IDX,
            news_days=news_days,
            news_records=news_records,
            cache_dir=OUTPUT_ROOT / "levels" / "cache",
            cache_ttl_seconds=600,
        )
        return jsonify(package)

    @app.route("/realtime")
    def realtime():
        payload = latest_realtime_payload()
        state = realtime_state()
        rows = payload.get("rows", []) if payload else []
        generated_at = payload.get("generated_at", "") if payload else ""
        running = find_running_realtime_job()
        stats = f"""
        <div class="mini-grid">
          <div class="metric"><div class="label">Generated</div><div class="value" style="font-size:15px;">{html.escape(generated_at or 'belum ada')}</div></div>
          <div class="metric"><div class="label">Rows</div><div class="value">{len(rows)}</div></div>
          <div class="metric"><div class="label">Batch</div><div class="value">{html.escape(str(state.get('current_offset', '-')))} / {html.escape(str(state.get('watchlist_size', '-')))}</div></div>
          <div class="metric"><div class="label">Realtime Job</div><div class="value" style="font-size:15px;">{html.escape(running['status'] if running else 'tidak running')}</div></div>
        </div>
        """
        if rows:
            table_rows = []
            for row in rows[:120]:
                ticker = html.escape(str(row.get("ticker", "")))
                chart = url_for("chart", ticker=normalize_chart_ticker(str(row.get("ticker", ""))))
                table_rows.append(
                    "<tr>"
                    f"<td><a href='{chart}'>{ticker}</a></td>"
                    f"<td>{html.escape(str(row.get('company', '')))}</td>"
                    f"<td>{html.escape(str(row.get('combined_score', '')))}</td>"
                    f"<td>{html.escape(str(row.get('confidence', '')))}%</td>"
                    f"<td>{html.escape(str(row.get('actionability', '')))}</td>"
                    f"<td>{html.escape(str(row.get('take_profit') or ''))}</td>"
                    f"<td>{html.escape(str(row.get('stop_loss') or ''))}</td>"
                    f"<td>{html.escape(str(row.get('tp_sl_basis', ''))[:120])}</td>"
                    f"<td>{html.escape(str(row.get('key_reasons', ''))[:260])}</td>"
                    "</tr>"
                )
            table = (
                "<table><thead><tr><th>Ticker</th><th>Company</th><th>Score</th><th>Confidence</th>"
                "<th>Action</th><th>TP</th><th>SL</th><th>TP/SL Basis</th><th>Reasons</th></tr></thead><tbody>"
                + "".join(table_rows)
                + "</tbody></table>"
            )
        else:
            table = "<p class='muted'>Belum ada rekomendasi realtime. Jalankan 1 batch dari dashboard atau aktifkan REALTIME_ENABLED=1.</p>"
        body = f"""
        <section class="panel">
          <h2>Realtime Research Candidates</h2>
          <p class="muted">Ini kandidat riset berbasis berita terbaru, fundamental, dokumen, dan laporan IDX resmi yang tersedia. Bukan nasihat investasi personal.</p>
          <p><span class="pill">TP/SL hanya muncul jika data resmi dan harga cukup</span> <span class="pill">Tidak ada akurasi 100%</span></p>
          {stats}
          <form method="post" action="{url_for('start_realtime_once')}">
            <button type="submit">Run 1 Batch Sekarang</button>
            <a class="button light" href="{url_for('outputs')}">Download Output</a>
          </form>
        </section>
        <section class="panel" style="margin-top:16px;">
          <h2>Top Candidates</h2>
          {table}
        </section>
        """
        return render_page("Realtime", body)

    @app.route("/chart")
    @app.route("/tradingview")
    def chart_redirect():
        ticker = normalize_chart_ticker(request.args.get("ticker", "BBCA"))
        return redirect(url_for("chart", ticker=ticker))

    @app.route("/chart/<ticker>")
    @app.route("/tradingview/<ticker>")
    def chart(ticker: str):
        ticker_clean = normalize_chart_ticker(ticker)
        symbol = tradingview_symbol(ticker_clean)
        row = recommendation_for_ticker(ticker_clean)
        tp = row.get("take_profit") if row else None
        sl = row.get("stop_loss") if row else None
        basis = row.get("tp_sl_basis") if row else "Belum ada output rekomendasi untuk ticker ini."
        actionability = row.get("actionability") if row else "Belum ada data rekomendasi."
        score = row.get("combined_score") if row else ""
        confidence = row.get("confidence") if row else ""
        widget_config = {
            "autosize": True,
            "symbol": symbol,
            "interval": "D",
            "timezone": "Asia/Jakarta",
            "theme": "light",
            "style": "1",
            "locale": "id",
            "enable_publishing": False,
            "withdateranges": True,
            "hide_side_toolbar": False,
            "details": True,
            "hotlist": True,
            "allow_symbol_change": True,
            "calendar": False,
            "studies": ["Volume@tv-basicstudies", "MACD@tv-basicstudies", "RSI@tv-basicstudies"],
            "support_host": "https://www.tradingview.com",
        }
        symbol_info_config = {
            "symbol": symbol,
            "width": "100%",
            "locale": "id",
            "colorTheme": "light",
            "isTransparent": True,
        }
        technical_config = {
            "interval": "1D",
            "width": "100%",
            "isTransparent": True,
            "height": "100%",
            "symbol": symbol,
            "showIntervalTabs": True,
            "displayMode": "single",
            "locale": "id",
            "colorTheme": "light",
        }
        financials_config = {
            "colorTheme": "light",
            "isTransparent": True,
            "largeChartUrl": "",
            "displayMode": "adaptive",
            "width": "100%",
            "height": "100%",
            "symbol": symbol,
            "locale": "id",
        }
        profile_config = {
            "width": "100%",
            "height": "100%",
            "isTransparent": True,
            "colorTheme": "light",
            "symbol": symbol,
            "locale": "id",
        }
        news_config = {
            "feedMode": "symbol",
            "symbol": symbol,
            "colorTheme": "light",
            "isTransparent": True,
            "displayMode": "regular",
            "width": "100%",
            "height": "100%",
            "locale": "id",
        }
        def widget(src: str, config: Dict[str, Any]) -> str:
            return f"""
            <div class="tradingview-widget-container">
              <div class="tradingview-widget-container__widget"></div>
              <script type="text/javascript" src="{src}" async>
              {json.dumps(config)}
              </script>
            </div>
            """
        body = f"""
        <section class="panel">
          <h2>TradingView Suite {html.escape(symbol)}</h2>
          <div class="mini-grid">
            <div class="metric"><div class="label">Actionability</div><div class="value" style="font-size:16px;">{html.escape(str(actionability))}</div></div>
            <div class="metric"><div class="label">Score</div><div class="value">{html.escape(str(score or 'n/a'))}</div></div>
            <div class="metric"><div class="label">Confidence</div><div class="value">{html.escape(str(confidence or 'n/a'))}%</div></div>
            <div class="metric"><div class="label">Take Profit</div><div class="value">{html.escape(str(tp or 'n/a'))}</div></div>
            <div class="metric"><div class="label">Stop Loss</div><div class="value">{html.escape(str(sl or 'n/a'))}</div></div>
          </div>
          <p class="muted">Basis TP/SL: {html.escape(str(basis))}</p>
          <p class="muted">Widget TradingView berjalan di halaman aplikasi ini, jadi user tidak perlu membuka web TradingView manual. Server hanya mengirim HTML; data chart dimuat oleh browser dari TradingView.</p>
          <form method="get" action="{url_for('chart_redirect')}" style="max-width:360px;">
            <label>Ganti ticker IDX</label>
            <input name="ticker" value="{html.escape(ticker_clean)}">
            <button type="submit">Buka</button>
          </form>
        </section>

        <section class="panel" style="margin-top:16px;">
          <h2>Auto TP/SL Calculation</h2>
          <p class="muted">Dua kalkulasi otomatis: nilai teknikal dari data harga pasar, dan nilai komposit dari teknikal + laporan IDX/fundamental + berita + dokumen yang tersedia.</p>
          <div id="levels-status" class="banner">Menghitung TP/SL otomatis...</div>
          <div class="mini-grid">
            <div class="metric">
              <div class="label">Market/Technical Action</div>
              <div class="value" id="tech-action" style="font-size:16px;">loading</div>
            </div>
            <div class="metric">
              <div class="label">Technical TP</div>
              <div class="value" id="tech-tp">-</div>
            </div>
            <div class="metric">
              <div class="label">Technical SL</div>
              <div class="value" id="tech-sl">-</div>
            </div>
            <div class="metric">
              <div class="label">Technical RR</div>
              <div class="value" id="tech-rr">-</div>
            </div>
            <div class="metric">
              <div class="label">Bandar Volume</div>
              <div class="value" id="bandar-label" style="font-size:16px;">-</div>
            </div>
            <div class="metric">
              <div class="label">Bandar Score</div>
              <div class="value" id="bandar-score">-</div>
            </div>
            <div class="metric">
              <div class="label">Entry Range</div>
              <div class="value" id="entry-range" style="font-size:16px;">-</div>
            </div>
            <div class="metric">
              <div class="label">Breakout Entry</div>
              <div class="value" id="breakout-entry">-</div>
            </div>
            <div class="metric">
              <div class="label">Composite Action</div>
              <div class="value" id="comp-action" style="font-size:16px;">loading</div>
            </div>
            <div class="metric">
              <div class="label">Composite TP</div>
              <div class="value" id="comp-tp">-</div>
            </div>
            <div class="metric">
              <div class="label">Composite SL</div>
              <div class="value" id="comp-sl">-</div>
            </div>
            <div class="metric">
              <div class="label">Composite Confidence</div>
              <div class="value" id="comp-conf">-</div>
            </div>
            <div class="metric">
              <div class="label">Composite Entry</div>
              <div class="value" id="comp-entry-range" style="font-size:16px;">-</div>
            </div>
          </div>
          <table style="margin-top:12px;">
            <thead><tr><th>Model</th><th>Basis Data</th><th>Alasan</th></tr></thead>
            <tbody>
              <tr><td>Market/Technical</td><td id="tech-source">-</td><td id="tech-reasons">-</td></tr>
              <tr><td>Composite</td><td id="comp-source">-</td><td id="comp-reasons">-</td></tr>
            </tbody>
          </table>
          <div id="bandar-options" class="banner" style="display:none;margin-top:12px;"></div>
          <p class="muted">Catatan: data TradingView widget tidak bisa dibaca langsung oleh server. Kalkulasi otomatis memakai data pasar lokal dan ditampilkan berdampingan dengan chart TradingView.</p>
        </section>

        <section class="panel" style="margin-top:16px;">
          <div class="tv-grid">
            <div class="tv-column">
              <div class="tv-widget-box compact">
                {widget("https://s3.tradingview.com/external-embedding/embed-widget-symbol-info.js", symbol_info_config)}
              </div>
              <div class="tv-widget-box tall">
                {widget("https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js", widget_config)}
              </div>
              <div class="tv-widget-box financials">
                {widget("https://s3.tradingview.com/external-embedding/embed-widget-financials.js", financials_config)}
              </div>
            </div>
            <div class="tv-column">
              <div class="tv-widget-box">
                {widget("https://s3.tradingview.com/external-embedding/embed-widget-technical-analysis.js", technical_config)}
              </div>
              <div class="tv-widget-box profile">
                {widget("https://s3.tradingview.com/external-embedding/embed-widget-symbol-profile.js", profile_config)}
              </div>
              <div class="tv-widget-box">
                {widget("https://s3.tradingview.com/external-embedding/embed-widget-timeline.js", news_config)}
              </div>
            </div>
          </div>
        </section>
        <script>
          async function loadLevels() {{
            const status = document.getElementById("levels-status");
            const fmt = (value) => value === null || value === undefined || value === "" ? "-" : Number(value).toLocaleString("id-ID");
            const pct = (value) => value === null || value === undefined ? "-" : (Number(value) * 100).toFixed(1) + "%";
            try {{
              const response = await fetch("{url_for('api_levels', ticker=ticker_clean)}");
              if (!response.ok) throw new Error("HTTP " + response.status);
              const data = await response.json();
              const tech = data.market_technical || {{}};
              const comp = data.composite || {{}};
              const context = data.context || {{}};
              const bandar = comp.bandar_volume || tech.bandar_volume || {{}};
              const brokerSummary = comp.broker_summary || context.broker_summary || {{}};
              const technicalBandar = tech.bandar_volume || {{}};
              const techEntry = tech.entry_range || {{}};
              const compEntry = comp.entry_range || {{}};
              document.getElementById("tech-action").textContent = tech.action || "-";
              document.getElementById("tech-tp").textContent = fmt(tech.take_profit);
              document.getElementById("tech-sl").textContent = fmt(tech.stop_loss);
              document.getElementById("tech-rr").textContent = tech.risk_reward || "-";
              document.getElementById("bandar-label").textContent = bandar.effective_label || bandar.label || "-";
              document.getElementById("bandar-score").textContent = bandar.effective_score === null || bandar.effective_score === undefined ? (bandar.score === null || bandar.score === undefined ? "-" : bandar.score) : bandar.effective_score;
              document.getElementById("entry-range").textContent = fmt(techEntry.preferred_entry_low) + " - " + fmt(techEntry.preferred_entry_high);
              document.getElementById("breakout-entry").textContent = fmt(techEntry.breakout_entry);
              document.getElementById("tech-source").textContent = tech.source || "-";
              document.getElementById("tech-reasons").textContent = (tech.reasons || []).concat(technicalBandar.reasons || []).join(" | ") || "-";
              document.getElementById("comp-action").textContent = comp.action || "-";
              document.getElementById("comp-tp").textContent = fmt(comp.take_profit);
              document.getElementById("comp-sl").textContent = fmt(comp.stop_loss);
              document.getElementById("comp-conf").textContent = comp.confidence ? comp.confidence + "%" : "-";
              document.getElementById("comp-entry-range").textContent = fmt(compEntry.preferred_entry_low) + " - " + fmt(compEntry.preferred_entry_high);
              document.getElementById("comp-source").textContent = (comp.sources || []).join(" | ") || "-";
              document.getElementById("comp-reasons").textContent = (comp.reasons || []).join(" | ") || "-";
              const optionBox = document.getElementById("bandar-options");
              const alternatives = comp.bandarmology_alternatives || brokerSummary.alternatives || [];
              if (alternatives.length && !brokerSummary.score) {{
                optionBox.style.display = "block";
                optionBox.innerHTML = "<strong>Bandarmology source options:</strong> " + alternatives.map((item) => "<a href='" + item.url + "' target='_blank' rel='noopener'>" + item.name + "</a> <span class='muted'>(" + item.integration + ")</span>").join(" | ");
              }} else {{
                optionBox.style.display = "none";
              }}
              status.textContent = "Auto calculation selesai. Current price: " + fmt(tech.current_price) + "; entry " + fmt(compEntry.preferred_entry_low || techEntry.preferred_entry_low) + " - " + fmt(compEntry.preferred_entry_high || techEntry.preferred_entry_high) + "; composite upside " + pct(comp.upside_to_tp) + "; bandar source " + (bandar.effective_source || "price-volume proxy") + ".";
              status.style.borderColor = "#bbf7d0";
              status.style.background = "#f0fdf4";
              status.style.color = "#14532d";
            }} catch (error) {{
              status.textContent = "Gagal menghitung TP/SL otomatis: " + error.message;
              status.style.borderColor = "#fecaca";
              status.style.background = "#fef2f2";
              status.style.color = "#7f1d1d";
            }}
          }}
          loadLevels();
        </script>
        """
        return render_page(f"TradingView {ticker_clean}", body)

    @app.route("/outputs")
    def outputs():
        files = list_output_files()
        if files:
            rows = "".join(
                f"<tr><td><a href='{url_for('download_output', filename=item['name'])}'>{item['name']}</a></td>"
                f"<td>{item['size_kb']}</td><td>{item['modified']}</td>"
                f"<td><form method='post' action='{url_for('delete_output')}' onsubmit=\"return confirm('Hapus file ini?');\">"
                f"<input type='hidden' name='filename' value='{html.escape(item['name'])}'>"
                f"<button type='submit' style='margin-top:0;background:#b91c1c;'>Delete</button></form></td></tr>"
                for item in files
            )
            table = "<table><thead><tr><th>File</th><th>KB</th><th>Modified</th><th>Action</th></tr></thead><tbody>" + rows + "</tbody></table>"
        else:
            table = '<p class="muted">Belum ada output di folder hosted.</p>'
        cleanup_form = f"""
        <form method="post" action="{url_for('start_cleanup_job')}">
          <label>Hapus generated file lebih lama dari N hari</label>
          <input type="number" name="days" value="1" min="1" max="14" step="0.5">
          <button type="submit">Cleanup Sekarang</button>
        </form>
        """
        return render_page("Output Files", f"<section class='panel'><h2>Output Files</h2>{table}</section><section class='panel' style='margin-top:16px;'><h2>Cleanup</h2>{cleanup_form}</section>")

    @app.route("/download/<path:filename>")
    def download_output(filename: str):
        root = OUTPUT_ROOT.resolve()
        target = safe_file_in_root(root, filename)
        if not target:
            abort(404)
        return send_from_directory(root, filename, as_attachment=True)

    @app.route("/outputs/delete", methods=["POST"])
    def delete_output():
        filename = request.form.get("filename", "")
        root = OUTPUT_ROOT.resolve()
        target = safe_file_in_root(root, filename)
        if not target:
            abort(404)
        target.unlink()
        remove_empty_parent_dirs(target, root)
        return redirect(url_for("outputs"))

    @app.route("/uploads-files")
    def uploaded_files():
        files = list_upload_files()
        if files:
            rows = []
            root_names = {
                UPLOAD_ROOT.name: "idx",
                DOC_UPLOAD_ROOT.name: "documents",
                FOCUS_UPLOAD_ROOT.name: "issuer_focus",
            }
            for item in files:
                root_key = root_names.get(item["root"], "documents")
                rows.append(
                    f"<tr><td>{html.escape(root_key)}</td><td>{html.escape(item['name'])}</td>"
                    f"<td>{item['size_kb']}</td><td>{item['modified']}</td>"
                    f"<td><form method='post' action='{url_for('delete_upload')}' onsubmit=\"return confirm('Hapus upload ini?');\">"
                    f"<input type='hidden' name='root' value='{html.escape(root_key)}'>"
                    f"<input type='hidden' name='filename' value='{html.escape(item['name'])}'>"
                    f"<button type='submit' style='margin-top:0;background:#b91c1c;'>Delete</button></form></td></tr>"
                )
            table = "<table><thead><tr><th>Type</th><th>File</th><th>KB</th><th>Modified</th><th>Action</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        else:
            table = "<p class='muted'>Belum ada file upload.</p>"
        return render_page("Upload Files", f"<section class='panel'><h2>Upload Files</h2>{table}</section>")

    @app.route("/uploads-files/delete", methods=["POST"])
    def delete_upload():
        root_key = request.form.get("root", "")
        filename = request.form.get("filename", "")
        root = UPLOAD_ROOT if root_key == "idx" else DOC_UPLOAD_ROOT if root_key == "documents" else FOCUS_UPLOAD_ROOT if root_key == "issuer_focus" else None
        if root is None:
            abort(400)
        root = root.resolve()
        target = safe_file_in_root(root, filename)
        if not target:
            abort(404)
        target.unlink()
        remove_empty_parent_dirs(target, root)
        return redirect(url_for("uploaded_files"))

    @app.route("/jobs/cleanup", methods=["POST"])
    def start_cleanup_job():
        days = parse_float(request.form.get("days"), 1.0, 1.0, 14.0)
        command = [
            sys.executable,
            str(SCRIPTS / "cleanup_system.py"),
            "--root",
            str(ROOT),
            "--days",
            str(days),
        ]
        job_id = start_job(f"Cleanup generated files > {days} day", command)
        return redirect(url_for("job_detail", job_id=job_id))

    start_realtime_loop_if_needed()
    return app


app = create_app()


if __name__ == "__main__":
    port = int(
        os.environ.get("PORT")
        or os.environ.get("SERVER_PORT")
        or os.environ.get("APP_PORT")
        or os.environ.get("WISP_PORT")
        or 8080
    )
    app.run(host="0.0.0.0", port=port)
