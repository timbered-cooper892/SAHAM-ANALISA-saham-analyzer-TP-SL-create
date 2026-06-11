#!/usr/bin/env python
"""
Focused issuer deep dive.

Runs a lightweight one-ticker workflow:
- latest company news
- fundamental score
- optional official IDX report analysis
- optional uploaded document scan
- consolidated recommendation with TP/SL when source data is available
"""

import argparse
import csv
import html
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
DEFAULT_WATCHLIST = ROOT / "data" / "watchlist.idx.csv"


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def timestamp_text() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def normalize_ticker(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9.:\-_]", "", value or "").upper()
    if text.startswith("IDX:"):
        text = text.split(":", 1)[1]
    text = text.replace(".JK", "")
    if re.fullmatch(r"[A-Z0-9]{3,6}", text):
        return f"{text}.JK"
    return text


def ticker_base(value: str) -> str:
    return normalize_ticker(value).replace(".JK", "")


def read_watchlist(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


def profile_for_ticker(ticker: str, watchlist: Path) -> Dict[str, str]:
    normalized = normalize_ticker(ticker)
    base = ticker_base(ticker)
    for row in read_watchlist(watchlist):
        row_ticker = normalize_ticker(str(row.get("ticker", "")))
        if row_ticker == normalized or ticker_base(row_ticker) == base:
            return {str(key): "" if value is None else str(value) for key, value in row.items()}
    return {
        "ticker": normalized,
        "company": base,
        "aliases": base,
        "country": "Indonesia" if normalized.endswith(".JK") else "",
        "sector": "",
    }


def write_single_watchlist(profile: Dict[str, str], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["ticker", "company", "aliases", "country", "sector"]
    for key in profile:
        if key not in fieldnames:
            fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerow(profile)
    return path


def run_command(command: List[str], label: str, cwd: Path = ROOT) -> Dict[str, Any]:
    print(f"\n[{label}]")
    print("$ " + subprocess.list2cmdline(command))
    process = subprocess.run(command, cwd=str(cwd), text=True, capture_output=True)
    output = (process.stdout or "") + (process.stderr or "")
    if output.strip():
        print(output[-10000:])
    return {
        "label": label,
        "command": subprocess.list2cmdline(command),
        "returncode": process.returncode,
        "output_tail": output[-5000:],
        "finished_at": utc_now_text(),
    }


def latest_file(directory: Path, pattern: str) -> Optional[Path]:
    if not directory.exists():
        return None
    files = sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return files[0] if files else None


def read_csv_first(path: Optional[Path], ticker: str) -> Dict[str, Any]:
    if not path or not path.exists():
        return {}
    wanted = normalize_ticker(ticker)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]
    for row in rows:
        if normalize_ticker(str(row.get("ticker", ""))) == wanted:
            return row
    return rows[0] if rows else {}


def read_json(path: Optional[Path]) -> Dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def recommendation_row(path: Optional[Path], ticker: str) -> Dict[str, Any]:
    payload = read_json(path)
    wanted = normalize_ticker(ticker)
    for row in payload.get("rows", []):
        if normalize_ticker(str(row.get("ticker", ""))) == wanted:
            return row
    rows = payload.get("rows", [])
    return rows[0] if rows else {}


def document_rows(path: Optional[Path], ticker: str) -> List[Dict[str, Any]]:
    payload = read_json(path)
    wanted = normalize_ticker(ticker)
    return [row for row in payload.get("rows", []) if normalize_ticker(str(row.get("ticker", ""))) == wanted]


def news_items(path: Optional[Path], limit: int = 8) -> List[Dict[str, Any]]:
    if not path or not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return payload[:limit] if isinstance(payload, list) else []


def val(value: Any, fallback: str = "n/a") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def render_html(package: Dict[str, Any]) -> str:
    ticker = package["ticker"]
    reco = package.get("recommendation", {})
    news_summary = package.get("news_summary", {})
    fundamentals = package.get("fundamental", {})
    idx = package.get("idx_official", {})
    docs = package.get("documents", [])
    commands = package.get("commands", [])
    news = package.get("news_items", [])

    def table_rows(rows: Sequence[Dict[str, Any]], columns: Sequence[str], limit: int = 12) -> str:
        result = []
        for row in rows[:limit]:
            result.append("<tr>" + "".join(f"<td>{html.escape(val(row.get(col), ''))}</td>" for col in columns) + "</tr>")
        return "".join(result)

    news_rows = table_rows(news, ["source_name", "published_at", "title", "sentiment_label", "impact_tags"], 8)
    doc_rows = table_rows(docs, ["source_file", "mention_count", "sentiment_label", "impact_tags", "risk_flags"], 8)
    command_rows = "".join(
        f"<tr><td>{html.escape(item['label'])}</td><td>{item['returncode']}</td><td><code>{html.escape(item['command'])}</code></td></tr>"
        for item in commands
    )
    conclusion = package.get("conclusion", [])
    conclusion_html = "".join(f"<li>{html.escape(line)}</li>" for line in conclusion)
    source_notes = "".join(f"<li>{html.escape(line)}</li>" for line in package.get("source_notes", []))
    idx_warnings = idx.get("source_posture", {}).get("warnings", []) if isinstance(idx, dict) else []
    warning_html = "".join(f"<li>{html.escape(str(item))}</li>" for item in idx_warnings) or "<li>Tidak ada warning IDX utama, atau IDX belum dijalankan.</li>"
    return f"""<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Focused Issuer Deep Dive - {html.escape(ticker)}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 28px; color: #172033; background: #f5f7fb; }}
    main {{ max-width: 1180px; margin: 0 auto; }}
    section {{ background: #fff; border: 1px solid #dbe4ee; border-radius: 8px; padding: 18px; margin-bottom: 16px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }}
    .tile {{ border: 1px solid #dbe4ee; border-radius: 8px; padding: 12px; background: #fbfdff; }}
    .label {{ color: #637184; font-size: 12px; }}
    .value {{ font-weight: 800; font-size: 19px; margin-top: 4px; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; }}
    th, td {{ padding: 8px; border: 1px solid #dbe4ee; text-align: left; font-size: 13px; vertical-align: top; }}
    th {{ background: #eaf1f8; }}
    code {{ white-space: normal; }}
  </style>
</head>
<body>
<main>
  <section>
    <h1>Focused Issuer Deep Dive - {html.escape(ticker)}</h1>
    <p>{html.escape(package.get("generated_at", ""))}. Research support only, bukan nasihat investasi personal.</p>
    <div class="grid">
      <div class="tile"><div class="label">Actionability</div><div class="value">{html.escape(val(reco.get("actionability")))}</div></div>
      <div class="tile"><div class="label">Score</div><div class="value">{html.escape(val(reco.get("combined_score")))}</div></div>
      <div class="tile"><div class="label">Confidence</div><div class="value">{html.escape(val(reco.get("confidence")))}%</div></div>
      <div class="tile"><div class="label">Take Profit</div><div class="value">{html.escape(val(reco.get("take_profit")))}</div></div>
      <div class="tile"><div class="label">Stop Loss</div><div class="value">{html.escape(val(reco.get("stop_loss")))}</div></div>
    </div>
    <ul>{conclusion_html}</ul>
  </section>
  <section>
    <h2>Sumber dan Basis Kesimpulan</h2>
    <ul>{source_notes}</ul>
    <p><strong>TP/SL basis:</strong> {html.escape(val(reco.get("tp_sl_basis")))}</p>
    <p><strong>Key reasons:</strong> {html.escape(val(reco.get("key_reasons")))}</p>
  </section>
  <section>
    <h2>Berita Target</h2>
    <p>Article count: {html.escape(val(news_summary.get("article_count"), "0"))}; sentiment: {html.escape(val(news_summary.get("avg_sentiment")))}; risk articles: {html.escape(val(news_summary.get("risk_article_count"), "0"))}</p>
    <table><thead><tr><th>Source</th><th>Published</th><th>Title</th><th>Sentiment</th><th>Tags</th></tr></thead><tbody>{news_rows}</tbody></table>
  </section>
  <section>
    <h2>Fundamental Snapshot</h2>
    <div class="grid">
      <div class="tile"><div class="label">Fundamental Score</div><div class="value">{html.escape(val(fundamentals.get("fundamental_score")))}</div></div>
      <div class="tile"><div class="label">ROE</div><div class="value">{html.escape(val(fundamentals.get("roe")))}</div></div>
      <div class="tile"><div class="label">Revenue Growth</div><div class="value">{html.escape(val(fundamentals.get("revenue_growth")))}</div></div>
      <div class="tile"><div class="label">Debt/Equity</div><div class="value">{html.escape(val(fundamentals.get("debt_to_equity")))}</div></div>
      <div class="tile"><div class="label">PE</div><div class="value">{html.escape(val(fundamentals.get("trailing_pe")))}</div></div>
    </div>
    <p>{html.escape(val(fundamentals.get("research_bucket"), ""))}</p>
  </section>
  <section>
    <h2>Laporan IDX Resmi</h2>
    <p>Financial score: {html.escape(val((idx.get("financial_score") or {}).get("score") if isinstance(idx, dict) else None))}; IDX records found: {html.escape(val((idx.get("source_posture") or {}).get("idx_records_found") if isinstance(idx, dict) else None))}</p>
    <ul>{warning_html}</ul>
  </section>
  <section>
    <h2>Dokumen Pendukung</h2>
    <table><thead><tr><th>File</th><th>Mentions</th><th>Sentiment</th><th>Tags</th><th>Risks</th></tr></thead><tbody>{doc_rows}</tbody></table>
  </section>
  <section>
    <h2>Command Audit</h2>
    <table><thead><tr><th>Step</th><th>Code</th><th>Command</th></tr></thead><tbody>{command_rows}</tbody></table>
  </section>
</main>
</body>
</html>
"""


def build_conclusion(package: Dict[str, Any]) -> List[str]:
    reco = package.get("recommendation", {})
    lines = [
        f"Kesimpulan: {val(reco.get('actionability'))}. {val(reco.get('action_reason'), '')}",
        f"Skor gabungan {val(reco.get('combined_score'))} dengan confidence {val(reco.get('confidence'))}%.",
    ]
    if reco.get("take_profit") and reco.get("stop_loss"):
        lines.append(
            f"TP {reco.get('take_profit')} dan SL {reco.get('stop_loss')} memakai basis: {val(reco.get('tp_sl_basis'))}."
        )
    else:
        lines.append("TP/SL belum dihitung karena data resmi/harga/histori belum cukup.")
    lines.append("TP/SL tidak bisa dijamin 100% akurat; gunakan sebagai bahan riset dan validasi ulang.")
    return lines


def write_outputs(package: Dict[str, Any], output_dir: Path, prefix: str) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    ticker = ticker_base(package["ticker"])
    stamp = timestamp_text()
    base = output_dir / f"{prefix}_{ticker}_{stamp}"
    latest_base = output_dir / f"{prefix}_{ticker}_latest"
    package["conclusion"] = build_conclusion(package)
    html_text = render_html(package)
    paths = {
        "json": base.with_suffix(".json"),
        "html": base.with_suffix(".html"),
        "latest_json": latest_base.with_suffix(".json"),
        "latest_html": latest_base.with_suffix(".html"),
    }
    for path in [paths["json"], paths["latest_json"]]:
        path.write_text(json.dumps(package, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    for path in [paths["html"], paths["latest_html"]]:
        path.write_text(html_text, encoding="utf-8")
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fokus cari semua data penting untuk 1 emiten target.")
    parser.add_argument("--ticker", required=True, help="Ticker target. Contoh: BBCA atau BBCA.JK.")
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST, help="Watchlist IDX.")
    parser.add_argument("--days", type=int, default=7, help="Hari berita ke belakang.")
    parser.add_argument("--max-records", type=int, default=10, help="Max artikel berita.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs" / "issuer_deep_dive", help="Folder output.")
    parser.add_argument("--prefix", default="issuer_deep_dive", help="Prefix output.")
    parser.add_argument("--report-file", type=Path, action="append", default=[], help="File laporan resmi IDX XLSX/PDF.")
    parser.add_argument("--document-input", type=Path, action="append", default=[], help="File/folder dokumen pendukung.")
    parser.add_argument("--no-auto-idx", action="store_true", help="Jangan coba auto-download IDX.")
    parser.add_argument("--use-gdelt", action="store_true", help="Aktifkan GDELT selain Google News RSS.")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    ticker = normalize_ticker(args.ticker)
    base = ticker_base(ticker)
    output_dir = args.output_dir.resolve()
    work_dir = output_dir / f"_work_{base}_{timestamp_text()}"
    news_dir = work_dir / "news"
    fund_dir = work_dir / "fundamental"
    idx_dir = work_dir / "idx_official"
    docs_dir = work_dir / "documents"
    reco_dir = work_dir / "recommendation"
    work_dir.mkdir(parents=True, exist_ok=True)
    profile = profile_for_ticker(ticker, args.watchlist)
    single_watchlist = write_single_watchlist(profile, work_dir / "target_watchlist.csv")

    commands: List[Dict[str, Any]] = []
    news_cmd = [
        sys.executable,
        str(SCRIPTS / "process_news.py"),
        "--watchlist",
        str(single_watchlist),
        "--days",
        str(max(1, args.days)),
        "--max-records",
        str(max(1, args.max_records)),
        "--output-dir",
        str(news_dir),
        "--prefix",
        "target_news",
        "--include-empty",
        "--no-macro",
    ]
    if not args.use_gdelt:
        news_cmd.append("--no-gdelt")
    commands.append(run_command(news_cmd, "target news"))

    fund_cmd = [
        sys.executable,
        str(SCRIPTS / "analyze_stocks.py"),
        "--watchlist",
        str(single_watchlist),
        "--news-summary",
        str(news_dir / "target_news_summary.csv"),
        "--output-dir",
        str(fund_dir),
        "--prefix",
        "target_fundamental",
    ]
    commands.append(run_command(fund_cmd, "target fundamentals"))

    idx_json: Optional[Path] = None
    if args.report_file or not args.no_auto_idx:
        idx_cmd = [
            sys.executable,
            str(SCRIPTS / "idx_official_report_analysis.py"),
            "--ticker",
            base,
            "--years",
            str(datetime.now().year),
            "--output-dir",
            str(idx_dir),
            "--prefix",
            "target_idx_official",
        ]
        if args.no_auto_idx:
            idx_cmd.append("--no-auto-idx")
        for report_file in args.report_file:
            idx_cmd.extend(["--report-file", str(report_file)])
        commands.append(run_command(idx_cmd, "target IDX official report"))
        idx_json = latest_file(idx_dir, "*idx_official*.json")

    doc_json: Optional[Path] = None
    existing_doc_inputs = [path for path in args.document_input if path.exists()]
    if existing_doc_inputs:
        doc_cmd = [
            sys.executable,
            str(SCRIPTS / "document_processor.py"),
            "--watchlist",
            str(single_watchlist),
            "--output-dir",
            str(docs_dir),
            "--prefix",
            "target_documents",
        ]
        for item in existing_doc_inputs:
            doc_cmd.extend(["--input", str(item)])
        commands.append(run_command(doc_cmd, "target document scan"))
        doc_json = docs_dir / "target_documents.json"

    news_summary = news_dir / "target_news_summary.csv"
    news_json = news_dir / "target_news_items.json"
    fund_csv = latest_file(fund_dir, "target_fundamental_*.csv")
    reco_cmd = [
        sys.executable,
        str(SCRIPTS / "recommendation_engine.py"),
        "--watchlist",
        str(single_watchlist),
        "--news-summary",
        str(news_summary),
        "--output-dir",
        str(reco_dir),
        "--prefix",
        "target_recommendation",
        "--top",
        "10",
    ]
    if fund_csv:
        reco_cmd.extend(["--fundamental-csv", str(fund_csv)])
    if idx_json:
        reco_cmd.extend(["--idx-json", str(idx_json)])
    if doc_json:
        reco_cmd.extend(["--document-json", str(doc_json)])
    commands.append(run_command(reco_cmd, "target recommendation"))

    reco_json = reco_dir / "latest_recommendations.json"
    package: Dict[str, Any] = {
        "ticker": ticker,
        "profile": profile,
        "generated_at": utc_now_text(),
        "recommendation": recommendation_row(reco_json, ticker),
        "news_summary": read_csv_first(news_summary, ticker),
        "news_items": news_items(news_json),
        "fundamental": read_csv_first(fund_csv, ticker),
        "idx_official": read_json(idx_json),
        "documents": document_rows(doc_json, ticker),
        "commands": commands,
        "source_notes": [
            "News: Google News RSS by default; GDELT optional.",
            "Fundamental: yfinance public market/financial data.",
            "IDX official: official IDX financial report endpoint or uploaded official XLSX/PDF when available.",
            "Documents: uploaded PDF/DOCX/XLSX/CSV/TXT/HTML/JSON scanned locally.",
            "Recommendation/TP/SL: local rule engine with explicit source basis and confidence.",
        ],
        "disclaimer": "Research support only; bukan nasihat investasi personal. TP/SL tidak bisa dijamin 100% akurat.",
    }
    paths = write_outputs(package, output_dir, args.prefix)
    print("Outputs created:")
    for kind, path in paths.items():
        print(f"- {kind}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
