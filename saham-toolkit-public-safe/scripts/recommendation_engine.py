#!/usr/bin/env python
"""
Combine news, fundamentals, IDX official reports, and uploaded documents into
ranked equity research candidates.

This is an idea triage engine, not a final trade recommendation engine. TP/SL
levels are only surfaced when a source-backed IDX official analysis package is
available for the ticker.
"""

import argparse
import csv
import html
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WATCHLIST = ROOT / "data" / "watchlist.idx.csv"


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    text = text.replace("%", "").replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def pct(value: Any) -> str:
    number = safe_float(value)
    if number is None:
        return "n/a"
    return f"{number * 100:.1f}%"


def price_text(value: Any) -> str:
    number = safe_float(value)
    if number is None:
        return ""
    if abs(number) >= 1000:
        return f"{number:,.0f}"
    return f"{number:,.2f}"


def normalize_ticker(value: Any) -> str:
    text = str(value or "").strip().upper()
    text = re.sub(r"\s+", "", text)
    if not text:
        return ""
    if text.startswith("IDX:"):
        text = text.split(":", 1)[1]
    if "." not in text and re.fullmatch(r"[A-Z0-9]{3,6}", text):
        text = f"{text}.JK"
    return text


def ticker_base(value: Any) -> str:
    return normalize_ticker(value).replace(".JK", "")


def read_csv_rows(path: Path) -> List[Dict[str, Any]]:
    if not path or not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


def read_watchlist(path: Path) -> Dict[str, Dict[str, Any]]:
    rows = read_csv_rows(path)
    profiles: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        ticker = normalize_ticker(row.get("ticker"))
        if ticker:
            profiles[ticker] = row
    return profiles


def latest_files(root: Path, patterns: Sequence[str], limit: int = 8) -> List[Path]:
    files: List[Path] = []
    if not root.exists():
        return []
    for pattern in patterns:
        files.extend(path for path in root.rglob(pattern) if path.is_file())
    files = sorted(set(files), key=lambda path: path.stat().st_mtime, reverse=True)
    return files[:limit]


def newest_by_ticker(rows: Iterable[Dict[str, Any]], ticker_key: str = "ticker") -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        ticker = normalize_ticker(row.get(ticker_key))
        if ticker and ticker not in result:
            result[ticker] = row
    return result


def load_news(paths: Sequence[Path]) -> Dict[str, Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in paths:
        rows.extend(read_csv_rows(path))
    return newest_by_ticker(rows)


def load_fundamentals(paths: Sequence[Path]) -> Dict[str, Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in paths:
        rows.extend(read_csv_rows(path))
    ranked = sorted(
        rows,
        key=lambda row: safe_float(row.get("research_score_with_news"))
        if safe_float(row.get("research_score_with_news")) is not None
        else safe_float(row.get("fundamental_score")) or -1,
        reverse=True,
    )
    return newest_by_ticker(ranked)


def load_idx_json(paths: Sequence[Path]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    sorted_paths = sorted([path for path in paths if path.exists()], key=lambda p: p.stat().st_mtime, reverse=True)
    for path in sorted_paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        ticker = normalize_ticker(payload.get("ticker"))
        if ticker and ticker not in result:
            payload["_source_file"] = str(path)
            result[ticker] = payload
    return result


def load_documents(paths: Sequence[Path]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows = payload.get("rows") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            continue
        for row in rows:
            ticker = normalize_ticker(row.get("ticker"))
            if ticker:
                grouped.setdefault(ticker, []).append(row)
    result: Dict[str, Dict[str, Any]] = {}
    for ticker, rows in grouped.items():
        mention_count = sum(int(safe_float(row.get("mention_count")) or 0) for row in rows)
        avg_sentiment = sum(safe_float(row.get("sentiment_score")) or 0.0 for row in rows) / max(1, len(rows))
        risk_flags = sorted(set(flag for row in rows for flag in str(row.get("risk_flags", "")).split(";") if flag))
        impact_tags = sorted(set(tag for row in rows for tag in str(row.get("impact_tags", "")).split(";") if tag))
        result[ticker] = {
            "ticker": ticker,
            "document_count": len(rows),
            "mention_count": mention_count,
            "avg_sentiment": round(avg_sentiment, 3),
            "risk_flags": ";".join(risk_flags[:8]),
            "impact_tags": ";".join(impact_tags[:8]),
            "top_snippets": " || ".join(str(row.get("snippets", "")) for row in rows[:3] if row.get("snippets")),
        }
    return result


def weighted_average(items: Iterable[Tuple[Optional[float], float]]) -> Tuple[Optional[float], float]:
    total = 0.0
    available = 0.0
    weighted = 0.0
    for score, weight in items:
        total += weight
        if score is None:
            continue
        available += weight
        weighted += score * weight
    if available == 0:
        return None, 0.0
    return weighted / available, available / total if total else 0.0


def news_component(row: Optional[Dict[str, Any]]) -> Tuple[Optional[float], float, List[str]]:
    if not row:
        return None, 0.0, []
    avg_sentiment = safe_float(row.get("avg_sentiment"))
    article_count = safe_float(row.get("article_count")) or 0.0
    risk_count = safe_float(row.get("risk_article_count")) or 0.0
    if avg_sentiment is None and article_count <= 0:
        return None, 0.0, []
    score = clamp(((avg_sentiment or 0.0) + 1.0) * 50.0 - min(10.0, risk_count * 1.5))
    confidence = clamp(25.0 + min(40.0, article_count * 8.0) + (10.0 if row.get("top_headlines") else 0.0), 0, 80)
    reasons = []
    if article_count:
        reasons.append(f"berita {int(article_count)} artikel, sentimen {avg_sentiment or 0:.2f}")
    if risk_count:
        reasons.append(f"{int(risk_count)} artikel berisiko")
    return score, confidence, reasons


def fundamental_component(row: Optional[Dict[str, Any]]) -> Tuple[Optional[float], float, List[str]]:
    if not row:
        return None, 0.0, []
    score = safe_float(row.get("research_score_with_news"))
    if score is None:
        score = safe_float(row.get("fundamental_score"))
    confidence = safe_float(row.get("data_confidence")) or 0.0
    reasons = []
    if score is not None:
        reasons.append(f"skor fundamental {score:.1f}")
    if row.get("research_bucket"):
        reasons.append(str(row.get("research_bucket")))
    return score, confidence, reasons


def idx_component(payload: Optional[Dict[str, Any]]) -> Tuple[Optional[float], float, List[str], Dict[str, Any]]:
    empty_trade: Dict[str, Any] = {}
    if not payload:
        return None, 0.0, [], empty_trade
    financial_score = payload.get("financial_score", {}) or {}
    trade = payload.get("trade_view", {}) or {}
    score = safe_float(financial_score.get("score"))
    confidence = safe_float(financial_score.get("confidence")) or 0.0
    reasons: List[str] = []
    if score is not None:
        reasons.append(f"laporan IDX score {score:.1f}, confidence {confidence:.1f}%")
    actionability = trade.get("actionability")
    if actionability:
        reasons.append(str(actionability))
    if trade.get("red_flags"):
        reasons.append("red flags: " + ", ".join(str(item) for item in trade.get("red_flags", [])[:4]))
    return score, confidence, reasons, trade


def document_component(row: Optional[Dict[str, Any]]) -> Tuple[Optional[float], float, List[str]]:
    if not row:
        return None, 0.0, []
    avg_sentiment = safe_float(row.get("avg_sentiment"))
    mentions = safe_float(row.get("mention_count")) or 0
    doc_count = safe_float(row.get("document_count")) or 0
    if avg_sentiment is None and mentions <= 0:
        return None, 0.0, []
    risk_flags = [flag for flag in str(row.get("risk_flags", "")).split(";") if flag]
    score = clamp(((avg_sentiment or 0.0) + 1.0) * 50.0 - min(12.0, len(risk_flags) * 3.0))
    confidence = clamp(20.0 + min(45.0, mentions * 4.0) + min(15.0, doc_count * 5.0), 0, 80)
    reasons = [f"dokumen {int(doc_count)} file/{int(mentions)} mention"]
    if risk_flags:
        reasons.append("risiko dokumen: " + ", ".join(risk_flags[:4]))
    return score, confidence, reasons


def trade_levels(trade: Dict[str, Any], idx_confidence: float) -> Dict[str, Any]:
    target = safe_float(trade.get("illustrative_target_price"))
    stop = safe_float(trade.get("illustrative_stop_loss"))
    upside = safe_float(trade.get("upside_to_target"))
    risk_to_stop = safe_float(trade.get("risk_to_stop"))
    methods = trade.get("valuation_methods") or []
    if target is None or stop is None:
        return {
            "take_profit": None,
            "stop_loss": None,
            "upside_to_tp": None,
            "risk_to_sl": None,
            "tp_sl_confidence": 0.0,
            "tp_sl_basis": "Tidak dihitung: butuh laporan IDX resmi + harga pasar + histori harga.",
            "tp_sl_source": "",
        }
    basis_parts = []
    for item in methods:
        if isinstance(item, dict):
            basis_parts.append(f"{item.get('method')}={price_text(item.get('value'))}")
    basis = "; ".join(part for part in basis_parts if part) or "IDX official analyzer valuation + volatility/support stop"
    return {
        "take_profit": target,
        "stop_loss": stop,
        "upside_to_tp": upside,
        "risk_to_sl": risk_to_stop,
        "tp_sl_confidence": clamp(idx_confidence, 0, 85),
        "tp_sl_basis": basis,
        "tp_sl_source": "IDX official financial report extraction + yfinance market data + local rule engine",
    }


def actionability(score: Optional[float], confidence: float, trade: Dict[str, Any], reasons: List[str]) -> Tuple[str, str]:
    if score is None:
        return "NO CALL / DATA BELUM CUKUP", "Belum ada gabungan data yang cukup untuk triage."
    red_flags = trade.get("red_flags") or []
    upside = safe_float(trade.get("upside_to_target"))
    if red_flags and score < 70:
        return "WAIT / RISIKO TINGGI", "Ada red flag dan skor belum cukup kuat."
    if score >= 78 and confidence >= 65 and (upside is None or upside >= 0.12):
        return "BUY CANDIDATE UNTUK RISET LANJUT", "Skor, confidence, dan risk/reward melewati ambang triage."
    if score >= 65 and confidence >= 50:
        return "WATCHLIST PRIORITAS", "Data cukup menarik, tetapi perlu konfirmasi harga, laporan resmi, atau katalis."
    if score >= 50:
        return "WAIT / BUTUH KONFIRMASI", "Sinyal campuran; belum cukup kuat untuk masuk kandidat utama."
    return "NO CALL / BELUM LAYAK", "Skor gabungan masih rendah atau data belum cukup."


def build_recommendations(
    profiles: Dict[str, Dict[str, Any]],
    news: Dict[str, Dict[str, Any]],
    fundamentals: Dict[str, Dict[str, Any]],
    idx_reports: Dict[str, Dict[str, Any]],
    documents: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    all_tickers = sorted(set(profiles) | set(news) | set(fundamentals) | set(idx_reports) | set(documents))
    rows: List[Dict[str, Any]] = []
    for ticker in all_tickers:
        profile = profiles.get(ticker, {})
        n_score, n_conf, n_reasons = news_component(news.get(ticker))
        f_score, f_conf, f_reasons = fundamental_component(fundamentals.get(ticker))
        i_score, i_conf, i_reasons, trade = idx_component(idx_reports.get(ticker))
        d_score, d_conf, d_reasons = document_component(documents.get(ticker))
        combined, availability = weighted_average(
            [
                (i_score, 0.35),
                (f_score, 0.30),
                (n_score, 0.20),
                (d_score, 0.10),
                (50.0 if profile else None, 0.05),
            ]
        )
        confidence = clamp((i_conf * 0.35 + f_conf * 0.25 + n_conf * 0.20 + d_conf * 0.10 + availability * 100 * 0.10))
        if combined is not None and confidence < 35:
            combined = clamp(combined - 6.0)
        action, action_reason = actionability(combined, confidence, trade, i_reasons + f_reasons + n_reasons + d_reasons)
        levels = trade_levels(trade, i_conf)
        source_count = sum(1 for item in [news.get(ticker), fundamentals.get(ticker), idx_reports.get(ticker), documents.get(ticker)] if item)
        rows.append(
            {
                "ticker": ticker,
                "company": profile.get("company", "") or fundamentals.get(ticker, {}).get("company", "") or news.get(ticker, {}).get("entity", ""),
                "sector": profile.get("sector", "") or fundamentals.get(ticker, {}).get("sector", ""),
                "combined_score": round(combined, 1) if combined is not None else None,
                "confidence": round(confidence, 1),
                "actionability": action,
                "action_reason": action_reason,
                "take_profit": levels["take_profit"],
                "stop_loss": levels["stop_loss"],
                "upside_to_tp": levels["upside_to_tp"],
                "risk_to_sl": levels["risk_to_sl"],
                "tp_sl_confidence": levels["tp_sl_confidence"],
                "tp_sl_basis": levels["tp_sl_basis"],
                "tp_sl_source": levels["tp_sl_source"],
                "source_count": source_count,
                "idx_score": i_score,
                "fundamental_score": f_score,
                "news_score": round(n_score, 1) if n_score is not None else None,
                "document_score": round(d_score, 1) if d_score is not None else None,
                "news_article_count": news.get(ticker, {}).get("article_count", ""),
                "news_risk_article_count": news.get(ticker, {}).get("risk_article_count", ""),
                "news_top_headlines": news.get(ticker, {}).get("top_headlines", ""),
                "document_mentions": documents.get(ticker, {}).get("mention_count", ""),
                "key_reasons": " | ".join((i_reasons + f_reasons + n_reasons + d_reasons)[:8]),
                "generated_at": utc_now_text(),
                "disclaimer": "Research triage only; bukan nasihat investasi personal. TP/SL tidak bisa dijamin 100% akurat.",
            }
        )
    rows.sort(
        key=lambda row: (
            safe_float(row.get("combined_score")) if safe_float(row.get("combined_score")) is not None else -1,
            safe_float(row.get("confidence")) or 0,
            safe_float(row.get("source_count")) or 0,
        ),
        reverse=True,
    )
    return rows


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        rows = []
    fieldnames = [
        "ticker",
        "company",
        "sector",
        "combined_score",
        "confidence",
        "actionability",
        "action_reason",
        "take_profit",
        "stop_loss",
        "upside_to_tp",
        "risk_to_sl",
        "tp_sl_confidence",
        "tp_sl_basis",
        "tp_sl_source",
        "source_count",
        "idx_score",
        "fundamental_score",
        "news_score",
        "document_score",
        "news_article_count",
        "news_risk_article_count",
        "news_top_headlines",
        "document_mentions",
        "key_reasons",
        "generated_at",
        "disclaimer",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def tradingview_symbol(ticker: str) -> str:
    return "IDX:" + ticker_base(ticker)


def render_html(rows: List[Dict[str, Any]], generated_at: str) -> str:
    table_rows = []
    for row in rows[:150]:
        ticker = str(row.get("ticker", ""))
        chart_href = f"/chart/{html.escape(ticker_base(ticker))}"
        table_rows.append(
            "<tr>"
            f"<td><a href=\"{chart_href}\">{html.escape(ticker)}</a></td>"
            f"<td>{html.escape(str(row.get('company', '')))}</td>"
            f"<td>{html.escape(str(row.get('combined_score', '')))}</td>"
            f"<td>{html.escape(str(row.get('confidence', '')))}%</td>"
            f"<td>{html.escape(str(row.get('actionability', '')))}</td>"
            f"<td>{price_text(row.get('take_profit'))}</td>"
            f"<td>{price_text(row.get('stop_loss'))}</td>"
            f"<td>{pct(row.get('upside_to_tp'))}</td>"
            f"<td>{html.escape(str(row.get('key_reasons', ''))[:500])}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Realtime Stock Research Candidates</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 28px; color: #172033; background: #f5f7fb; }}
    main {{ max-width: 1220px; margin: 0 auto; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; }}
    th, td {{ padding: 9px; border: 1px solid #dbe4ee; text-align: left; font-size: 13px; vertical-align: top; }}
    th {{ background: #eaf1f8; }}
    .note {{ color: #5f6f82; }}
  </style>
</head>
<body>
<main>
  <h1>Realtime Stock Research Candidates</h1>
  <p class="note">Generated at {html.escape(generated_at)}. TP/SL hanya muncul jika ada laporan IDX resmi + data pasar yang cukup. Tidak ada TP/SL yang bisa dijamin 100% akurat.</p>
  <table>
    <thead><tr><th>Ticker</th><th>Company</th><th>Score</th><th>Confidence</th><th>Actionability</th><th>TP</th><th>SL</th><th>Upside</th><th>Reasons</th></tr></thead>
    <tbody>{''.join(table_rows)}</tbody>
  </table>
</main>
</body>
</html>
"""


def write_outputs(rows: List[Dict[str, Any]], output_dir: Path, prefix: str, top: int) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = utc_now_text()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    rows = rows[:top]
    payload = {
        "generated_at": generated_at,
        "rows": rows,
        "methodology": {
            "source_weights": {
                "IDX official financial report analysis": 0.35,
                "fundamental score": 0.30,
                "recent news": 0.20,
                "uploaded documents": 0.10,
                "watchlist/profile presence": 0.05,
            },
            "tp_sl_policy": "TP/SL only shown when IDX official analysis supplies source-backed target and stop levels.",
            "disclaimer": "Research triage only; no TP/SL or stock forecast is guaranteed 100% accurate.",
        },
    }
    paths = {
        "csv": output_dir / f"{prefix}_{timestamp}.csv",
        "json": output_dir / f"{prefix}_{timestamp}.json",
        "html": output_dir / f"{prefix}_{timestamp}.html",
        "latest_csv": output_dir / "latest_recommendations.csv",
        "latest_json": output_dir / "latest_recommendations.json",
        "latest_html": output_dir / "latest_recommendations.html",
    }
    write_csv(paths["csv"], rows)
    write_csv(paths["latest_csv"], rows)
    paths["json"].write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["latest_json"].write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report = render_html(rows, generated_at)
    paths["html"].write_text(report, encoding="utf-8")
    paths["latest_html"].write_text(report, encoding="utf-8")
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gabungkan berita, fundamental, IDX resmi, dan dokumen menjadi kandidat riset saham.")
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST, help="CSV watchlist.")
    parser.add_argument("--news-summary", type=Path, action="append", default=[], help="File news_summary.csv.")
    parser.add_argument("--fundamental-csv", type=Path, action="append", default=[], help="File fundamental_scores.csv.")
    parser.add_argument("--idx-json", type=Path, action="append", default=[], help="File JSON hasil idx_official_report_analysis.")
    parser.add_argument("--idx-json-dir", type=Path, action="append", default=[], help="Folder JSON hasil idx official.")
    parser.add_argument("--document-json", type=Path, action="append", default=[], help="File JSON document_processor.")
    parser.add_argument("--document-json-dir", type=Path, action="append", default=[], help="Folder JSON document_processor.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs" / "realtime", help="Folder output.")
    parser.add_argument("--prefix", default="recommendations", help="Prefix output timestamped.")
    parser.add_argument("--top", type=int, default=120, help="Jumlah baris maksimum.")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    if not args.watchlist.exists():
        parser.error(f"Watchlist tidak ditemukan: {args.watchlist}")

    output_root = ROOT / "outputs"
    news_paths = list(args.news_summary) or latest_files(output_root, ["*news_summary.csv"], limit=10)
    fundamental_paths = list(args.fundamental_csv) or latest_files(output_root, ["*fundamental*.csv"], limit=10)
    idx_paths = list(args.idx_json)
    for directory in args.idx_json_dir:
        idx_paths.extend(latest_files(directory, ["*.json"], limit=50))
    if not idx_paths:
        idx_paths = latest_files(output_root, ["*idx_official*.json"], limit=50)
    document_paths = list(args.document_json)
    for directory in args.document_json_dir:
        document_paths.extend(latest_files(directory, ["*.json"], limit=20))
    if not document_paths:
        document_paths = latest_files(output_root, ["*document_analysis*.json"], limit=20)

    profiles = read_watchlist(args.watchlist)
    rows = build_recommendations(
        profiles=profiles,
        news=load_news(news_paths),
        fundamentals=load_fundamentals(fundamental_paths),
        idx_reports=load_idx_json(idx_paths),
        documents=load_documents(document_paths),
    )
    paths = write_outputs(rows, args.output_dir, args.prefix, max(1, args.top))
    print("Sources used:")
    print(f"- news: {len(news_paths)} file")
    print(f"- fundamental: {len(fundamental_paths)} file")
    print(f"- idx official: {len(idx_paths)} file")
    print(f"- document: {len(document_paths)} file")
    print("Outputs created:")
    for kind, path in paths.items():
        print(f"- {kind}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
