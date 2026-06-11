#!/usr/bin/env python
"""
Official IDX report analyzer.

Primary path:
- Query the official IDX financial-report endpoint.
- Download official attachments when the endpoint is reachable.
- Prefer IDX XLSX financial statement files for structured extraction.

Fallback path:
- Analyze user-supplied official IDX XLSX/PDF files with --report-file.

The output is a source-backed research view with transparent evidence, not
personalized investment advice or trade execution guidance.
"""

import argparse
import csv
import html
import json
import math
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

from analyze_stocks import compact_number, pct, safe_div, safe_float, score_range, weighted_average


IDX_ENDPOINT = "https://www.idx.co.id/primary/ListedCompany/GetFinancialReport"
IDX_REPORT_PAGE = "https://www.idx.co.id/id/perusahaan-tercatat/laporan-keuangan-dan-tahunan/"


CANONICAL_PATTERNS: Dict[str, List[str]] = {
    "revenue": [
        r"\btotal revenue\b",
        r"\brevenues?\b",
        r"\bsales\b",
        r"penjualan",
        r"pendapatan",
        r"pendapatan usaha",
        r"jumlah pendapatan",
    ],
    "gross_profit": [r"gross profit", r"laba bruto", r"laba kotor"],
    "operating_income": [
        r"operating profit",
        r"operating income",
        r"profit from operations",
        r"laba usaha",
        r"laba operasi",
    ],
    "net_income": [
        r"profit for the year",
        r"profit for the period",
        r"net income",
        r"profit attributable to owners",
        r"laba tahun berjalan",
        r"laba periode berjalan",
        r"laba bersih",
        r"laba yang dapat diatribusikan",
    ],
    "total_assets": [r"total assets", r"jumlah aset", r"total aset"],
    "current_assets": [r"current assets", r"aset lancar"],
    "cash": [r"cash and cash equivalents", r"kas dan setara kas", r"kas"],
    "total_liabilities": [r"total liabilities", r"jumlah liabilitas", r"total liabilitas", r"jumlah kewajiban"],
    "current_liabilities": [r"current liabilities", r"liabilitas jangka pendek", r"kewajiban lancar"],
    "debt": [
        r"borrowings",
        r"bank loans",
        r"interest-bearing debt",
        r"utang bank",
        r"pinjaman",
        r"liabilitas berbunga",
    ],
    "equity": [
        r"total equity",
        r"jumlah ekuitas",
        r"ekuitas yang dapat diatribusikan",
        r"equity attributable to owners",
    ],
    "operating_cash_flow": [
        r"net cash.*operating activities",
        r"cash flows.*operating activities",
        r"arus kas.*aktivitas operasi",
        r"kas neto.*operasi",
    ],
    "capex": [
        r"purchase.*fixed assets",
        r"purchase.*property",
        r"acquisition.*fixed assets",
        r"perolehan aset tetap",
        r"pembelian aset tetap",
        r"belanja modal",
    ],
    "shares": [
        r"weighted average number of shares",
        r"number of shares outstanding",
        r"jumlah saham beredar",
        r"rata-rata tertimbang.*saham",
    ],
    "eps": [r"basic earnings per share", r"diluted earnings per share", r"laba per saham", r"eps"],
}

NEGATIVE_LABEL_HINTS = {
    "total_assets": [r"liabilities", r"liabilitas", r"kewajiban", r"equity", r"ekuitas"],
    "total_liabilities": [r"assets", r"aset", r"equity", r"ekuitas"],
    "equity": [r"liabilities", r"liabilitas", r"kewajiban"],
    "net_income": [r"other comprehensive", r"komprehensif lain", r"cash flow", r"arus kas"],
    "operating_cash_flow": [r"investing", r"financing", r"investasi", r"pendanaan"],
}


@dataclass
class ExtractedMetric:
    metric: str
    value: Optional[float]
    prior_value: Optional[float]
    label: str
    sheet: str
    row_number: int
    source_file: str
    confidence: str
    evidence: str


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_label(value: str) -> str:
    value = normalize_text(value).lower()
    value = re.sub(r"[^a-z0-9%/()., \-]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def parse_number(value: Any) -> Optional[float]:
    parsed = safe_float(value)
    if parsed is not None:
        return parsed
    text = normalize_text(value)
    if not text or not re.search(r"\d", text):
        return None
    neg = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^0-9,\.\-]", "", text)
    if cleaned.count(",") > 0 and cleaned.count(".") > 0:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    elif cleaned.count(",") > 1:
        cleaned = cleaned.replace(",", "")
    elif cleaned.count(",") == 1 and cleaned.count(".") == 0:
        left, right = cleaned.split(",")
        cleaned = left + "." + right if len(right) <= 2 else left + right
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if neg:
        number = -abs(number)
    return number if not math.isnan(number) and not math.isinf(number) else None


def metric_score(label: str, metric: str) -> int:
    normalized = normalize_label(label)
    if not normalized:
        return 0
    score = 0
    for pattern in CANONICAL_PATTERNS[metric]:
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            score += 10
    for bad in NEGATIVE_LABEL_HINTS.get(metric, []):
        if re.search(bad, normalized, flags=re.IGNORECASE):
            score -= 8
    # Favor exact total lines over subtotal/details when relevant.
    if score > 0 and metric in {"total_assets", "total_liabilities", "equity"} and re.search(r"\b(total|jumlah)\b", normalized):
        score += 3
    return score


def numeric_cells(row: Sequence[Any], start_index: int = 0) -> List[Tuple[int, float]]:
    values: List[Tuple[int, float]] = []
    for idx, cell in enumerate(row[start_index:], start=start_index):
        value = parse_number(cell)
        if value is not None:
            values.append((idx, value))
    return values


def row_label(row: Sequence[Any]) -> Tuple[str, int]:
    pieces = []
    last_text_index = 0
    for idx, cell in enumerate(row[:8]):
        text = normalize_text(cell)
        if not text:
            continue
        if parse_number(text) is not None:
            continue
        pieces.append(text)
        last_text_index = idx
    return " ".join(pieces), last_text_index


def choose_values(values: List[Tuple[int, float]]) -> Tuple[Optional[float], Optional[float]]:
    if not values:
        return None, None
    # IDX XBRL sheets normally put latest period before comparative period.
    filtered = [(idx, val) for idx, val in values if abs(val) > 0]
    if not filtered:
        filtered = values
    latest = filtered[0][1]
    prior = filtered[1][1] if len(filtered) > 1 else None
    return latest, prior


def extract_from_xlsx(path: Path) -> Tuple[Dict[str, ExtractedMetric], List[str]]:
    import pandas as pd

    issues: List[str] = []
    best: Dict[str, Tuple[int, ExtractedMetric]] = {}
    try:
        sheets = pd.read_excel(path, sheet_name=None, header=None, engine="openpyxl")
    except Exception as exc:
        raise RuntimeError(f"Cannot read XLSX {path}: {exc}") from exc

    for sheet_name, df in sheets.items():
        for row_idx, raw_row in df.iterrows():
            row = list(raw_row.values)
            label, last_text_index = row_label(row)
            if not label:
                continue
            values = numeric_cells(row, start_index=max(0, last_text_index + 1))
            if not values:
                continue
            for metric in CANONICAL_PATTERNS:
                score = metric_score(label, metric)
                if score <= 0:
                    continue
                latest, prior = choose_values(values)
                confidence = "high" if score >= 13 and latest is not None else "medium"
                extracted = ExtractedMetric(
                    metric=metric,
                    value=latest,
                    prior_value=prior,
                    label=label[:300],
                    sheet=str(sheet_name),
                    row_number=int(row_idx) + 1,
                    source_file=str(path),
                    confidence=confidence,
                    evidence=f"{path.name} / {sheet_name} row {int(row_idx) + 1}: {label[:180]}",
                )
                if metric not in best or score > best[metric][0]:
                    best[metric] = (score, extracted)

    metrics = {metric: item[1] for metric, item in best.items()}
    for required in ["total_assets", "total_liabilities", "equity", "revenue", "net_income"]:
        if required not in metrics:
            issues.append(f"Metric not found in XLSX: {required}")
    return metrics, issues


def extract_from_pdf(path: Path) -> Tuple[Dict[str, ExtractedMetric], List[str]]:
    # PDF table extraction is intentionally conservative. We record source
    # presence and rely on XLSX when available for decision-grade numbers.
    try:
        import pypdf
    except ImportError:
        return {}, [f"PDF found but pypdf is not installed: {path.name}"]
    try:
        reader = pypdf.PdfReader(str(path))
        page_count = len(reader.pages)
    except Exception as exc:
        return {}, [f"Cannot read PDF {path.name}: {exc}"]
    return {}, [f"PDF source captured ({path.name}, {page_count} pages), but structured extraction requires XLSX or manual review."]


def extract_official_reports(paths: Sequence[Path]) -> Tuple[Dict[str, ExtractedMetric], List[Dict[str, Any]], List[str]]:
    merged: Dict[str, ExtractedMetric] = {}
    source_index: List[Dict[str, Any]] = []
    issues: List[str] = []
    for path in paths:
        suffix = path.suffix.lower()
        source_index.append(
            {
                "source_id": f"SRC-{len(source_index) + 1:03d}",
                "source_name": path.name,
                "source_type": "IDX official attachment" if suffix in {".xlsx", ".xls", ".pdf"} else "file",
                "location": str(path),
                "retrieved_at": utc_now_text(),
            }
        )
        if suffix in {".xlsx", ".xlsm", ".xls"}:
            metrics, file_issues = extract_from_xlsx(path)
            issues.extend(file_issues)
            for metric, extracted in metrics.items():
                if metric not in merged or (merged[metric].confidence != "high" and extracted.confidence == "high"):
                    merged[metric] = extracted
        elif suffix == ".pdf":
            _, file_issues = extract_from_pdf(path)
            issues.extend(file_issues)
        else:
            issues.append(f"Unsupported report file type: {path}")
    return merged, source_index, issues


def idx_api_params(ticker: str, year: int, period: str, page_size: int) -> Dict[str, Any]:
    params = {
        "indexFrom": 1,
        "pageSize": page_size,
        "year": year,
        "reportType": "fs",
        "EmitenType": "S",
        "KodeEmiten": ticker.replace(".JK", "").upper(),
    }
    if period:
        params["periode"] = period
    return params


def query_idx_reports(ticker: str, years: Sequence[int], period: str = "", page_size: int = 50) -> Tuple[List[Dict[str, Any]], List[str]]:
    import requests

    warnings: List[str] = []
    records: List[Dict[str, Any]] = []
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": IDX_REPORT_PAGE,
    }
    for year in years:
        try:
            response = session.get(IDX_ENDPOINT, params=idx_api_params(ticker, year, period, page_size), headers=headers, timeout=30)
            if response.status_code == 403:
                warnings.append(
                    "IDX official endpoint returned 403/Cloudflare from this environment. "
                    "Use --report-file with official IDX downloaded XLSX/PDF, or retry from a host/IP allowed by IDX."
                )
                continue
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            warnings.append(f"IDX query failed for {ticker} {year}: {exc}")
            continue
        records.extend(normalize_idx_payload(payload))
        time.sleep(0.5)
    return records, warnings


def normalize_idx_payload(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ["Results", "results", "data", "Data", "Items", "items"]:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = normalize_idx_payload(value)
            if nested:
                return nested
    return [payload] if any(isinstance(v, (str, int, float, list, dict)) for v in payload.values()) else []


def attachment_candidates(record: Dict[str, Any]) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    possible_fields = ["Attachments", "attachments", "Attachment", "Files", "files", "File", "file"]
    raw_items: List[Any] = []
    for field in possible_fields:
        value = record.get(field)
        if isinstance(value, list):
            raw_items.extend(value)
        elif value:
            raw_items.append(value)
    for item in raw_items:
        if isinstance(item, str):
            candidates.append({"name": Path(item).name, "url": item})
        elif isinstance(item, dict):
            url = ""
            name = ""
            for key, value in item.items():
                low = str(key).lower()
                text = str(value)
                if any(token in low for token in ["url", "path", "link", "download"]) and text:
                    url = text
                if any(token in low for token in ["name", "file"]) and text:
                    name = text
            if url:
                candidates.append({"name": name or Path(url).name, "url": url})
    return candidates


def download_idx_attachments(records: Sequence[Dict[str, Any]], output_dir: Path, ticker: str) -> Tuple[List[Path], List[str]]:
    import requests

    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded: List[Path] = []
    warnings: List[str] = []
    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0", "Referer": IDX_REPORT_PAGE}
    for record in records:
        for attachment in attachment_candidates(record):
            raw_url = attachment["url"]
            url = urljoin("https://www.idx.co.id/", raw_url)
            name = attachment.get("name") or Path(raw_url).name or f"{ticker}_idx_attachment"
            if not re.search(r"\.(xlsx|xls|pdf)$", name, flags=re.IGNORECASE):
                continue
            target = output_dir / re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
            try:
                response = session.get(url, headers=headers, timeout=60)
                response.raise_for_status()
                target.write_bytes(response.content)
                downloaded.append(target)
            except Exception as exc:
                warnings.append(f"Failed to download IDX attachment {url}: {exc}")
    return downloaded, warnings


def latest_value(metrics: Dict[str, ExtractedMetric], name: str) -> Optional[float]:
    metric = metrics.get(name)
    return metric.value if metric else None


def prior_value(metrics: Dict[str, ExtractedMetric], name: str) -> Optional[float]:
    metric = metrics.get(name)
    return metric.prior_value if metric else None


def calculate_ratios(metrics: Dict[str, ExtractedMetric], market: Dict[str, Any]) -> Dict[str, Any]:
    revenue = latest_value(metrics, "revenue")
    prior_revenue = prior_value(metrics, "revenue")
    net_income = latest_value(metrics, "net_income")
    prior_net_income = prior_value(metrics, "net_income")
    assets = latest_value(metrics, "total_assets")
    liabilities = latest_value(metrics, "total_liabilities")
    equity = latest_value(metrics, "equity")
    current_assets = latest_value(metrics, "current_assets")
    current_liabilities = latest_value(metrics, "current_liabilities")
    debt = latest_value(metrics, "debt")
    cash = latest_value(metrics, "cash")
    ocf = latest_value(metrics, "operating_cash_flow")
    capex = latest_value(metrics, "capex")
    shares = latest_value(metrics, "shares") or market.get("shares_outstanding")
    eps = latest_value(metrics, "eps") or safe_div(net_income, shares)
    free_cash_flow = (ocf + capex) if ocf is not None and capex is not None else None
    price = market.get("price")
    bvps = safe_div(equity, shares)

    return {
        "revenue": revenue,
        "revenue_growth": growth(revenue, prior_revenue),
        "net_income": net_income,
        "net_income_growth": growth(net_income, prior_net_income),
        "total_assets": assets,
        "total_liabilities": liabilities,
        "equity": equity,
        "current_assets": current_assets,
        "current_liabilities": current_liabilities,
        "debt": debt,
        "cash": cash,
        "net_margin": safe_div(net_income, revenue),
        "roa": safe_div(net_income, assets),
        "roe": safe_div(net_income, equity),
        "debt_to_equity": safe_div(debt if debt is not None else liabilities, equity),
        "current_ratio": safe_div(current_assets, current_liabilities),
        "cash_to_debt": safe_div(cash, debt),
        "operating_cash_flow": ocf,
        "free_cash_flow": free_cash_flow,
        "fcf_margin": safe_div(free_cash_flow, revenue),
        "ocf_to_net_income": safe_div(ocf, net_income),
        "shares_outstanding": shares,
        "eps": eps,
        "book_value_per_share": bvps,
        "current_price": price,
        "pe": safe_div(price, eps),
        "pbv": safe_div(price, bvps),
        "market_cap": market.get("market_cap"),
    }


def growth(latest: Optional[float], prior: Optional[float]) -> Optional[float]:
    if latest is None or prior is None or prior == 0 or prior < 0:
        return None
    return latest / prior - 1


def fetch_market_data(ticker: str) -> Tuple[Dict[str, Any], List[str]]:
    warnings: List[str] = []
    try:
        import yfinance as yf
    except ImportError:
        return {}, ["yfinance not installed; market price/TP-SL unavailable."]
    yf_ticker = ticker if ticker.upper().endswith(".JK") else f"{ticker.upper()}.JK"
    try:
        stock = yf.Ticker(yf_ticker)
        info = stock.get_info() or {}
        price = safe_float(info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose"))
        market_cap = safe_float(info.get("marketCap"))
        shares = safe_float(info.get("sharesOutstanding"))
    except Exception as exc:
        info = {}
        price = market_cap = shares = None
        warnings.append(f"Market info fetch failed: {exc}")
    try:
        hist = stock.history(period="1y", interval="1d")
    except Exception as exc:
        hist = None
        warnings.append(f"Price history fetch failed: {exc}")

    volatility = None
    sma20 = sma50 = sma200 = low20 = None
    if hist is not None and not getattr(hist, "empty", True) and "Close" in hist:
        closes = hist["Close"].dropna()
        returns = closes.pct_change().dropna()
        if len(returns) > 20:
            volatility = float(returns.std() * math.sqrt(252))
        if len(closes) >= 20:
            sma20 = float(closes.tail(20).mean())
            low20 = float(closes.tail(20).min())
        if len(closes) >= 50:
            sma50 = float(closes.tail(50).mean())
        if len(closes) >= 200:
            sma200 = float(closes.tail(200).mean())

    return {
        "ticker": yf_ticker,
        "price": price,
        "market_cap": market_cap,
        "shares_outstanding": shares,
        "sector": info.get("sector") or "",
        "industry": info.get("industry") or "",
        "currency": info.get("currency") or "IDR",
        "volatility_annualized": volatility,
        "sma20": sma20,
        "sma50": sma50,
        "sma200": sma200,
        "low20": low20,
        "market_source": "Yahoo Finance via yfinance",
        "market_retrieved_at": utc_now_text(),
    }, warnings


def score_financial_quality(ratios: Dict[str, Any]) -> Tuple[Optional[float], float, Dict[str, Any]]:
    profitability, p_conf = weighted_average(
        [
            (score_range(ratios.get("roe"), 0.00, 0.20), 0.35),
            (score_range(ratios.get("roa"), 0.00, 0.10), 0.20),
            (score_range(ratios.get("net_margin"), 0.00, 0.20), 0.25),
            (score_range(ratios.get("fcf_margin"), -0.05, 0.15), 0.20),
        ]
    )
    growth_score, g_conf = weighted_average(
        [
            (score_range(ratios.get("revenue_growth"), -0.10, 0.20), 0.55),
            (score_range(ratios.get("net_income_growth"), -0.20, 0.30), 0.45),
        ]
    )
    balance, b_conf = weighted_average(
        [
            (score_range(ratios.get("debt_to_equity"), 0.50, 3.00, invert=True), 0.45),
            (score_range(ratios.get("current_ratio"), 0.80, 2.00), 0.25),
            (score_range(ratios.get("cash_to_debt"), 0.00, 1.00), 0.15),
            (score_range(ratios.get("ocf_to_net_income"), 0.50, 1.50), 0.15),
        ]
    )
    final, f_conf = weighted_average([(profitability, 0.40), (growth_score, 0.25), (balance, 0.35)])
    subs = {
        "profitability_score": round(profitability, 1) if profitability is not None else None,
        "growth_score": round(growth_score, 1) if growth_score is not None else None,
        "balance_score": round(balance, 1) if balance is not None else None,
        "profitability_confidence": round(p_conf * 100, 1),
        "growth_confidence": round(g_conf * 100, 1),
        "balance_confidence": round(b_conf * 100, 1),
    }
    return (round(final, 1) if final is not None else None), round(f_conf * 100, 1), subs


def derive_trade_view(ratios: Dict[str, Any], market: Dict[str, Any], score: Optional[float], confidence: float, issues: List[str]) -> Dict[str, Any]:
    price = ratios.get("current_price")
    eps = ratios.get("eps")
    bvps = ratios.get("book_value_per_share")
    roe = ratios.get("roe")
    revenue_growth = ratios.get("revenue_growth") or 0
    net_income_growth = ratios.get("net_income_growth") or 0
    volatility = market.get("volatility_annualized")
    low20 = market.get("low20")

    target_pe = None
    if score is not None:
        target_pe = max(6.0, min(22.0, 8.0 + (score / 100.0) * 14.0 + max(-2.0, min(2.0, revenue_growth * 10))))
    target_pb = None
    if roe is not None:
        target_pb = max(0.6, min(4.5, 0.8 + roe * 9.0 + max(-0.4, min(0.4, net_income_growth * 2))))

    valuation_points = []
    if eps is not None and target_pe is not None and eps > 0:
        valuation_points.append(("EPS x target PE", eps * target_pe, 0.55))
    if bvps is not None and target_pb is not None and bvps > 0:
        valuation_points.append(("BVPS x target PB", bvps * target_pb, 0.45))

    fair_value = None
    if valuation_points:
        total_weight = sum(weight for _, _, weight in valuation_points)
        fair_value = sum(value * weight for _, value, weight in valuation_points) / total_weight

    upside = safe_div(fair_value - price, price) if fair_value is not None and price is not None else None
    stop_loss = None
    if price is not None:
        volatility_stop = price * (1 - max(0.08, min(0.18, (volatility or 0.30) * 0.35)))
        support_stop = low20 * 0.97 if low20 else volatility_stop
        stop_loss = min(volatility_stop, support_stop)

    red_flags = []
    if ratios.get("net_margin") is not None and ratios["net_margin"] < 0:
        red_flags.append("Net margin negatif")
    if ratios.get("free_cash_flow") is not None and ratios["free_cash_flow"] < 0:
        red_flags.append("Free cash flow negatif")
    if ratios.get("debt_to_equity") is not None and ratios["debt_to_equity"] > 3:
        red_flags.append("Debt/equity tinggi")
    if confidence < 55:
        red_flags.append("Confidence data rendah")
    if any("Metric not found" in issue for issue in issues):
        red_flags.append("Sebagian metrik wajib tidak terbaca dari IDX")

    if score is None or price is None or fair_value is None:
        action = "NO CALL / DATA BELUM CUKUP"
        reason = "Belum cukup data harga atau laporan untuk menghitung risk/reward."
    elif red_flags and score < 65:
        action = "TIDAK LAYAK BELI SAAT INI"
        reason = "Kualitas laporan atau red flag belum memenuhi ambang minimal."
    elif upside is not None and upside >= 0.18 and score >= 70 and confidence >= 65 and len(red_flags) == 0:
        action = "LAYAK MASUK BUY CANDIDATE"
        reason = "Kualitas keuangan kuat, data cukup, dan upside model sederhana di atas 18%."
    elif upside is not None and upside >= 0.08 and score >= 60:
        action = "WATCHLIST / BELI HANYA JIKA KONFIRMASI"
        reason = "Risk/reward belum cukup tebal atau masih ada data/teknikal yang perlu dikonfirmasi."
    else:
        action = "BELUM LAYAK BELI / WAIT"
        reason = "Upside, kualitas laporan, atau confidence belum memenuhi threshold."

    return {
        "actionability": action,
        "reason": reason,
        "illustrative_target_price": round_to_tick(fair_value) if fair_value is not None else None,
        "illustrative_stop_loss": round_to_tick(stop_loss) if stop_loss is not None else None,
        "upside_to_target": upside,
        "risk_to_stop": safe_div(price - stop_loss, price) if price is not None and stop_loss is not None else None,
        "target_pe_assumption": target_pe,
        "target_pb_assumption": target_pb,
        "valuation_methods": [
            {"method": method, "value": round_to_tick(value), "weight": weight} for method, value, weight in valuation_points
        ],
        "red_flags": red_flags,
        "model_note": "TP/SL bersifat ilustratif berbasis rule engine, bukan rekomendasi personal atau instruksi transaksi.",
    }


def round_to_tick(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    if value < 200:
        tick = 1
    elif value < 500:
        tick = 2
    elif value < 2000:
        tick = 5
    elif value < 5000:
        tick = 10
    else:
        tick = 25
    return round(value / tick) * tick


def build_conclusion(metrics: Dict[str, ExtractedMetric], ratios: Dict[str, Any], trade: Dict[str, Any], score: Optional[float], confidence: float) -> List[str]:
    lines = [
        f"Kesimpulan utama: {trade['actionability']}. {trade['reason']}",
    ]
    if score is not None:
        lines.append(f"Skor kualitas keuangan dari laporan resmi IDX: {score}/100 dengan confidence {confidence}%.")
    if ratios.get("roe") is not None:
        lines.append(f"ROE {pct(ratios.get('roe'))}, ROA {pct(ratios.get('roa'))}, net margin {pct(ratios.get('net_margin'))}.")
    if ratios.get("revenue_growth") is not None or ratios.get("net_income_growth") is not None:
        lines.append(f"Pertumbuhan: revenue {pct(ratios.get('revenue_growth')) or 'n/a'}, laba bersih {pct(ratios.get('net_income_growth')) or 'n/a'}.")
    if ratios.get("debt_to_equity") is not None:
        lines.append(f"Neraca: debt/equity {ratios.get('debt_to_equity'):.2f}, current ratio {fmt_num(ratios.get('current_ratio'))}.")
    if trade.get("illustrative_target_price") is not None:
        lines.append(
            f"TP ilustratif {trade['illustrative_target_price']:,} dan SL ilustratif {trade.get('illustrative_stop_loss'):,}; "
            f"upside {pct(trade.get('upside_to_target')) or 'n/a'}, risk-to-stop {pct(trade.get('risk_to_stop')) or 'n/a'}."
        )
    return lines


def fmt_num(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def source_reasoning(metrics: Dict[str, ExtractedMetric], market: Dict[str, Any], trade: Dict[str, Any]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    metric_labels = {
        "revenue": "Pendapatan",
        "net_income": "Laba bersih",
        "total_assets": "Total aset",
        "total_liabilities": "Total liabilitas",
        "equity": "Ekuitas",
        "operating_cash_flow": "Arus kas operasi",
        "capex": "Capex",
        "eps": "EPS",
        "shares": "Jumlah saham",
    }
    for metric, label in metric_labels.items():
        extracted = metrics.get(metric)
        if extracted:
            rows.append(
                {
                    "data_point": label,
                    "value": str(extracted.value),
                    "source": extracted.evidence,
                    "role_in_conclusion": "Dipakai untuk rasio, skor kualitas, dan valuasi." if metric in {"revenue", "net_income", "equity", "eps", "shares"} else "Dipakai untuk risk flag dan kualitas neraca/kas.",
                }
            )
    if market.get("price") is not None:
        rows.append(
            {
                "data_point": "Harga pasar terakhir",
                "value": str(market.get("price")),
                "source": f"{market.get('market_source')} retrieved {market.get('market_retrieved_at')}",
                "role_in_conclusion": "Dipakai untuk upside, TP/SL, PE, PBV, dan actionability.",
            }
        )
    if trade.get("valuation_methods"):
        rows.append(
            {
                "data_point": "Asumsi model TP",
                "value": json.dumps(trade.get("valuation_methods"), ensure_ascii=False),
                "source": "Derived by local rule engine from IDX financial metrics and market price.",
                "role_in_conclusion": "Membentuk TP ilustratif; bukan guidance perusahaan atau konsensus analis.",
            }
        )
    return rows


def analyze_idx_official(
    ticker: str,
    report_files: Sequence[Path],
    years: Sequence[int],
    period: str,
    auto_download: bool,
    output_dir: Path,
) -> Dict[str, Any]:
    ticker_clean = ticker.replace(".JK", "").upper()
    all_files = [Path(path) for path in report_files]
    warnings: List[str] = []
    idx_records: List[Dict[str, Any]] = []
    if auto_download:
        records, query_warnings = query_idx_reports(ticker_clean, years=years, period=period)
        idx_records.extend(records)
        warnings.extend(query_warnings)
        downloaded, dl_warnings = download_idx_attachments(records, output_dir / "idx_downloads" / ticker_clean, ticker_clean)
        all_files.extend(downloaded)
        warnings.extend(dl_warnings)

    metrics, source_index, extract_issues = extract_official_reports(all_files)
    warnings.extend(extract_issues)
    market, market_warnings = fetch_market_data(f"{ticker_clean}.JK")
    warnings.extend(market_warnings)
    ratios = calculate_ratios(metrics, market)
    score, confidence, subscores = score_financial_quality(ratios)
    trade = derive_trade_view(ratios, market, score, confidence, warnings)
    conclusion = build_conclusion(metrics, ratios, trade, score, confidence)
    reasoning = source_reasoning(metrics, market, trade)

    return {
        "ticker": f"{ticker_clean}.JK",
        "generated_at": utc_now_text(),
        "source_posture": {
            "primary_source": "IDX official financial report attachments",
            "market_source": market.get("market_source"),
            "idx_endpoint": IDX_ENDPOINT,
            "idx_records_found": len(idx_records),
            "files_analyzed": [str(path) for path in all_files],
            "warnings": warnings,
        },
        "metrics": {metric: asdict(value) for metric, value in metrics.items()},
        "ratios": ratios,
        "financial_score": {"score": score, "confidence": confidence, **subscores},
        "trade_view": trade,
        "conclusion": conclusion,
        "source_reasoning": reasoning,
        "disclaimer": "Output ini adalah research view berbasis data dan asumsi eksplisit, bukan nasihat investasi personal atau instruksi transaksi.",
    }


def write_outputs(package: Dict[str, Any], output_dir: Path, prefix: str) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ticker = package["ticker"].replace(".", "_")
    base = output_dir / f"{prefix}_{ticker}_{timestamp}"
    paths = {
        "json": base.with_suffix(".json"),
        "html": base.with_suffix(".html"),
        "summary_csv": output_dir / f"{prefix}_{ticker}_source_reasoning_{timestamp}.csv",
    }
    paths["json"].write_text(json.dumps(package, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    paths["html"].write_text(render_html(package), encoding="utf-8")
    with paths["summary_csv"].open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = ["data_point", "value", "source", "role_in_conclusion"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(package["source_reasoning"])
    return paths


def render_html(package: Dict[str, Any]) -> str:
    trade = package["trade_view"]
    ratios = package["ratios"]
    score = package["financial_score"]
    source_posture = package["source_posture"]
    conclusion_html = "".join(f"<li>{html.escape(line)}</li>" for line in package["conclusion"])
    warning_html = "".join(f"<li>{html.escape(str(item))}</li>" for item in source_posture.get("warnings", [])) or "<li>Tidak ada warning utama.</li>"
    reasoning_rows = "".join(
        "<tr>"
        f"<td>{html.escape(row['data_point'])}</td>"
        f"<td>{html.escape(row['value'])}</td>"
        f"<td>{html.escape(row['source'])}</td>"
        f"<td>{html.escape(row['role_in_conclusion'])}</td>"
        "</tr>"
        for row in package["source_reasoning"]
    )
    metric_rows = "".join(
        f"<tr><td>{html.escape(key)}</td><td>{html.escape(format_ratio_value(key, value))}</td></tr>"
        for key, value in ratios.items()
        if value is not None
    )
    target = trade.get("illustrative_target_price")
    stop = trade.get("illustrative_stop_loss")
    return f"""<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>IDX Official Report Analysis - {html.escape(package['ticker'])}</title>
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; background: #f4f7fb; color: #182033; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px 22px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    h2 {{ margin-top: 26px; }}
    .muted {{ color: #64748b; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 20px 0; }}
    .tile {{ background: #fff; border: 1px solid #dce6f0; border-radius: 8px; padding: 14px; }}
    .label {{ font-size: 12px; color: #64748b; text-transform: uppercase; }}
    .value {{ margin-top: 7px; font-size: 22px; font-weight: 700; }}
    .verdict {{ border-left: 6px solid #0f766e; background: #fff; padding: 16px; border-radius: 8px; border-top: 1px solid #dce6f0; border-right: 1px solid #dce6f0; border-bottom: 1px solid #dce6f0; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #dce6f0; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid #e7eef6; text-align: left; vertical-align: top; font-size: 13px; }}
    th {{ background: #eaf1f8; }}
    li {{ margin: 6px 0; }}
    .note {{ font-size: 13px; color: #64748b; line-height: 1.5; }}
  </style>
</head>
<body>
<main>
  <h1>{html.escape(package['ticker'])} - IDX Official Report Analysis</h1>
  <p class="muted">Generated {html.escape(package['generated_at'])}</p>
  <div class="verdict">
    <strong>{html.escape(trade['actionability'])}</strong>
    <p>{html.escape(trade['reason'])}</p>
  </div>
  <div class="grid">
    <div class="tile"><div class="label">Financial Score</div><div class="value">{html.escape(str(score.get('score')))}</div></div>
    <div class="tile"><div class="label">Confidence</div><div class="value">{html.escape(str(score.get('confidence')))}%</div></div>
    <div class="tile"><div class="label">Current Price</div><div class="value">{html.escape(format_price(ratios.get('current_price')))}</div></div>
    <div class="tile"><div class="label">TP Ilustratif</div><div class="value">{html.escape(format_price(target))}</div></div>
    <div class="tile"><div class="label">SL Ilustratif</div><div class="value">{html.escape(format_price(stop))}</div></div>
    <div class="tile"><div class="label">Upside</div><div class="value">{html.escape(pct(trade.get('upside_to_target')) or 'n/a')}</div></div>
  </div>
  <h2>Kesimpulan</h2>
  <ul>{conclusion_html}</ul>
  <h2>Rasio Utama</h2>
  <table><tbody>{metric_rows}</tbody></table>
  <h2>Sumber Yang Membentuk Kesimpulan</h2>
  <table><thead><tr><th>Data</th><th>Value</th><th>Source</th><th>Role</th></tr></thead><tbody>{reasoning_rows}</tbody></table>
  <h2>Warnings / Data Gaps</h2>
  <ul>{warning_html}</ul>
  <p class="note">{html.escape(package['disclaimer'])}</p>
</main>
</body>
</html>
"""


def format_price(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:,.0f}"


def format_ratio_value(key: str, value: Any) -> str:
    parsed = safe_float(value)
    if parsed is None:
        return str(value)
    if key in {"revenue_growth", "net_income_growth", "net_margin", "roa", "roe", "fcf_margin"}:
        return pct(parsed) or "n/a"
    if key in {"revenue", "net_income", "operating_cash_flow", "free_cash_flow", "market_cap"}:
        return compact_number(parsed) or "n/a"
    return f"{parsed:.2f}"


def parse_years(text: str) -> List[int]:
    years = []
    for part in re.split(r"[,; ]+", text):
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            years.extend(range(int(start), int(end) + 1))
        else:
            years.append(int(part))
    return sorted(set(years), reverse=True)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analisa laporan resmi IDX dan buat research view TP/SL.")
    parser.add_argument("--ticker", required=True, help="Kode saham, contoh BBCA atau BBCA.JK.")
    parser.add_argument("--report-file", type=Path, action="append", default=[], help="File resmi IDX XLSX/PDF yang sudah diunduh. Bisa diulang.")
    parser.add_argument("--years", default=str(datetime.now().year), help="Tahun IDX untuk auto-download, contoh 2025 atau 2023-2025.")
    parser.add_argument("--period", default="", help="Filter periode IDX jika endpoint menerima parameter ini.")
    parser.add_argument("--no-auto-idx", action="store_true", help="Jangan coba query endpoint IDX; pakai report-file saja.")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/idx_official_analysis"), help="Folder output.")
    parser.add_argument("--prefix", default="idx_official_analysis", help="Prefix output.")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    report_files = [path for path in args.report_file if path.exists()]
    missing = [str(path) for path in args.report_file if not path.exists()]
    if missing:
        print("Missing report files: " + ", ".join(missing), file=sys.stderr)
    package = analyze_idx_official(
        args.ticker,
        report_files=report_files,
        years=parse_years(args.years),
        period=args.period,
        auto_download=not args.no_auto_idx,
        output_dir=args.output_dir,
    )
    paths = write_outputs(package, args.output_dir, args.prefix)
    print("Outputs created:")
    for kind, path in paths.items():
        print(f"- {kind}: {path}")
    print("Actionability:", package["trade_view"]["actionability"])
    print("Reason:", package["trade_view"]["reason"])
    if package["source_posture"]["warnings"]:
        print("Warnings:")
        for warning in package["source_posture"]["warnings"]:
            print(f"- {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
