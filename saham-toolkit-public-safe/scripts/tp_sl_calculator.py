#!/usr/bin/env python
"""
Automatic TP/SL calculator for one ticker.

Outputs two levels:
1. market_technical: price-history based levels from current market data,
   ATR, support/resistance, moving averages, RSI, and risk/reward.
2. composite: blends market technical levels with available IDX official
   report analysis, fundamental score, recent news, and document risk flags.

TradingView widgets do not expose their iframe data to this app. This module
therefore uses a local market-data provider (yfinance) and shows the result
next to the TradingView chart.
"""

import argparse
import csv
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
DEFAULT_WATCHLIST = ROOT / "data" / "watchlist.idx.csv"
DEFAULT_OUTPUT = ROOT / "outputs" / "levels"
DEFAULT_BROKER_CACHE = ROOT / "outputs" / "cache" / "broker_summary"


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

sys.path.insert(0, str(SCRIPTS))
try:
    from process_news import collect_for_profile, dedupe_items, summarize
except Exception:
    collect_for_profile = None
    dedupe_items = None
    summarize = None


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def normalize_ticker(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9.:\-_]", "", str(value or "")).upper()
    if text.startswith("IDX:"):
        text = text.split(":", 1)[1]
    text = text.replace(".JK", "")
    if re.fullmatch(r"[A-Z0-9]{3,6}", text):
        return f"{text}.JK"
    return text


def ticker_base(value: Any) -> str:
    return normalize_ticker(value).replace(".JK", "")


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "nan", "null"}:
        return None
    text = text.replace("%", "").replace(",", "")
    try:
        number = float(text)
    except ValueError:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


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
    return float(round(value / tick) * tick)


def pct(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(value, 4)


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
        row_ticker = normalize_ticker(row.get("ticker"))
        if row_ticker == normalized or ticker_base(row_ticker) == base:
            return {str(key): "" if value is None else str(value) for key, value in row.items()}
    return {"ticker": normalized, "company": base, "aliases": base, "country": "Indonesia", "sector": ""}


def latest_files(root: Path, patterns: Sequence[str], limit: int = 20) -> List[Path]:
    if not root.exists():
        return []
    files: List[Path] = []
    for pattern in patterns:
        files.extend(path for path in root.rglob(pattern) if path.is_file())
    files = sorted(set(files), key=lambda path: path.stat().st_mtime, reverse=True)
    return files[:limit]


def read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def read_csv_rows(path: Path) -> List[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except Exception:
        return []


def latest_fundamental(ticker: str, roots: Sequence[Path]) -> Dict[str, Any]:
    wanted = normalize_ticker(ticker)
    for root in roots:
        for path in latest_files(root, ["*fundamental*.csv"], limit=30):
            for row in read_csv_rows(path):
                if normalize_ticker(row.get("ticker")) == wanted:
                    row["_source_file"] = str(path)
                    return row
    return {}


def latest_recommendation(ticker: str, roots: Sequence[Path]) -> Dict[str, Any]:
    wanted = normalize_ticker(ticker)
    for root in roots:
        for path in latest_files(root, ["latest_recommendations.json", "*recommendation*.json"], limit=30):
            payload = read_json(path)
            rows = payload.get("rows", [])
            if not isinstance(rows, list):
                continue
            for row in rows:
                if normalize_ticker(row.get("ticker")) == wanted:
                    row["_source_file"] = str(path)
                    return row
    return {}


def latest_idx_official(ticker: str, roots: Sequence[Path]) -> Dict[str, Any]:
    wanted = normalize_ticker(ticker)
    for root in roots:
        for path in latest_files(root, ["*idx_official*.json", "*target_idx_official*.json"], limit=40):
            payload = read_json(path)
            if normalize_ticker(payload.get("ticker")) == wanted:
                payload["_source_file"] = str(path)
                return payload
    return {}


def latest_document_signal(ticker: str, roots: Sequence[Path]) -> Dict[str, Any]:
    wanted = normalize_ticker(ticker)
    rows: List[Dict[str, Any]] = []
    for root in roots:
        for path in latest_files(root, ["*document*.json"], limit=20):
            payload = read_json(path)
            for row in payload.get("rows", []):
                if normalize_ticker(row.get("ticker")) == wanted:
                    rows.append(row)
    if not rows:
        return {}
    mentions = sum(int(safe_float(row.get("mention_count")) or 0) for row in rows)
    risks = sorted(set(flag for row in rows for flag in str(row.get("risk_flags", "")).split(";") if flag))
    return {
        "document_count": len(rows),
        "mention_count": mentions,
        "risk_flags": ";".join(risks[:8]),
        "rows": rows[:8],
    }


def parse_broker_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return safe_float(value)
    text = str(value).strip()
    if not text:
        return None
    multiplier = 1.0
    lowered = text.lower().replace(" ", "")
    suffixes = [
        ("triliun", 1_000_000_000_000),
        ("trillion", 1_000_000_000_000),
        ("billion", 1_000_000_000),
        ("miliar", 1_000_000_000),
        ("milyar", 1_000_000_000),
        ("million", 1_000_000),
        ("juta", 1_000_000),
        ("rb", 1_000),
        ("k", 1_000),
        ("m", 1_000_000),
        ("b", 1_000_000_000),
        ("t", 1_000_000_000_000),
    ]
    for suffix, value_multiplier in suffixes:
        if lowered.endswith(suffix):
            multiplier = value_multiplier
            lowered = lowered[: -len(suffix)]
            break
    cleaned = re.sub(r"[^0-9,.\-()]", "", lowered)
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    elif cleaned.count(",") > 1:
        cleaned = cleaned.replace(",", "")
    elif cleaned.count(",") == 1 and "." not in cleaned:
        left, right = cleaned.split(",")
        cleaned = left + "." + right if len(right) <= 2 else left + right
    try:
        number = float(cleaned) * multiplier
    except ValueError:
        return None
    return -abs(number) if negative else number


def normalize_column(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower())
    return text.strip("_")


def find_column(columns: Sequence[str], candidates: Sequence[str]) -> Optional[str]:
    normalized = {normalize_column(col): col for col in columns}
    for candidate in candidates:
        key = normalize_column(candidate)
        if key in normalized:
            return normalized[key]
    for key, original in normalized.items():
        for candidate in candidates:
            candidate_key = normalize_column(candidate)
            if candidate_key and candidate_key in key:
                return original
    return None


def read_broker_tables(path: Path) -> List[List[Dict[str, Any]]]:
    suffix = path.suffix.lower()
    tables: List[List[Dict[str, Any]]] = []
    try:
        if suffix == ".csv":
            sample = path.read_text(encoding="utf-8-sig", errors="ignore")
            delimiter = ","
            try:
                dialect = csv.Sniffer().sniff(sample[:4096], delimiters=",;\t|")
                delimiter = dialect.delimiter
            except Exception:
                if sample.count(";") > sample.count(","):
                    delimiter = ";"
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                raw_rows = list(csv.reader(handle, delimiter=delimiter))
            for header_index, raw_header in enumerate(raw_rows[:12]):
                if not raw_header:
                    continue
                header_score = sum(1 for col in raw_header if normalize_column(col) in {"broker", "broker_code", "kode_broker", "buy_value", "sell_value", "net_value", "net_lot", "buy_lot", "sell_lot"})
                header_score += sum(1 for col in raw_header if any(term in normalize_column(col) for term in ["broker", "buyer", "seller", "net", "beli", "jual", "lot", "value"]))
                if header_score < 2:
                    continue
                headers = [str(col).strip() or f"column_{idx}" for idx, col in enumerate(raw_header)]
                rows = []
                for raw_row in raw_rows[header_index + 1 : header_index + 701]:
                    if not any(str(cell).strip() for cell in raw_row):
                        continue
                    padded = raw_row + [""] * max(0, len(headers) - len(raw_row))
                    rows.append(dict(zip(headers, padded[: len(headers)])))
                if rows:
                    tables.append(rows)
                    break
        elif suffix in {".xlsx", ".xls"}:
            import pandas as pd

            sheets = pd.read_excel(path, sheet_name=None, dtype=str, header=None)
            for _, frame in sheets.items():
                frame = frame.fillna("")
                for header_index in range(min(12, len(frame))):
                    raw_header = [str(value).strip() for value in frame.iloc[header_index].tolist()]
                    header_score = sum(1 for col in raw_header if normalize_column(col) in {"broker", "broker_code", "kode_broker", "buy_value", "sell_value", "net_value", "net_lot", "buy_lot", "sell_lot"})
                    header_score += sum(1 for col in raw_header if any(term in normalize_column(col) for term in ["broker", "buyer", "seller", "net", "beli", "jual", "lot", "value"]))
                    if header_score < 2:
                        continue
                    headers = [str(col).strip() or f"column_{idx}" for idx, col in enumerate(raw_header)]
                    body = frame.iloc[header_index + 1 : header_index + 701].copy()
                    body.columns = headers
                    rows = body.to_dict("records")
                    if rows:
                        tables.append(rows)
                        break
    except Exception:
        return []
    return tables


def parse_broker_summary_rows(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not rows:
        return None
    columns = list(rows[0].keys())
    broker_col = find_column(columns, ["broker", "broker_code", "kode_broker", "code", "buyer", "seller", "anggota_bursa"])
    buy_value_col = find_column(columns, ["buy_value", "buyer_value", "b_value", "buy val", "bval", "nilai_beli", "value_buy"])
    sell_value_col = find_column(columns, ["sell_value", "seller_value", "s_value", "sell val", "sval", "nilai_jual", "value_sell"])
    net_value_col = find_column(columns, ["net_value", "net val", "netval", "net_buy_value", "net", "nilai_net", "nett_value"])
    buy_volume_col = find_column(columns, ["buy_volume", "buy_lot", "buyer_lot", "b_lot", "buy vol", "bvol", "volume_beli"])
    sell_volume_col = find_column(columns, ["sell_volume", "sell_lot", "seller_lot", "s_lot", "sell vol", "svol", "volume_jual"])
    net_volume_col = find_column(columns, ["net_volume", "net_lot", "net vol", "netvol", "net_volume_lot", "volume_net"])
    avg_buy_col = find_column(columns, ["avg_buy", "average_buy", "avg_b", "b_avg", "avg beli"])
    avg_sell_col = find_column(columns, ["avg_sell", "average_sell", "avg_s", "s_avg", "avg jual"])

    if not broker_col or not (net_value_col or (buy_value_col and sell_value_col) or net_volume_col or (buy_volume_col and sell_volume_col)):
        return None

    parsed_rows: List[Dict[str, Any]] = []
    for row in rows:
        broker = str(row.get(broker_col, "")).strip()
        if not broker or broker.lower() in {"total", "grand total", "nan"}:
            continue
        buy_value = parse_broker_number(row.get(buy_value_col)) if buy_value_col else None
        sell_value = parse_broker_number(row.get(sell_value_col)) if sell_value_col else None
        net_value = parse_broker_number(row.get(net_value_col)) if net_value_col else None
        buy_volume = parse_broker_number(row.get(buy_volume_col)) if buy_volume_col else None
        sell_volume = parse_broker_number(row.get(sell_volume_col)) if sell_volume_col else None
        net_volume = parse_broker_number(row.get(net_volume_col)) if net_volume_col else None
        if net_value is None and buy_value is not None and sell_value is not None:
            net_value = buy_value - sell_value
        if net_volume is None and buy_volume is not None and sell_volume is not None:
            net_volume = buy_volume - sell_volume
        if net_value is None and net_volume is None:
            continue
        parsed_rows.append(
            {
                "broker": broker,
                "buy_value": buy_value,
                "sell_value": sell_value,
                "net_value": net_value,
                "buy_volume": buy_volume,
                "sell_volume": sell_volume,
                "net_volume": net_volume,
                "avg_buy": parse_broker_number(row.get(avg_buy_col)) if avg_buy_col else None,
                "avg_sell": parse_broker_number(row.get(avg_sell_col)) if avg_sell_col else None,
            }
        )
    if not parsed_rows:
        return None

    positive = [row for row in parsed_rows if (row.get("net_value") or row.get("net_volume") or 0) > 0]
    negative = [row for row in parsed_rows if (row.get("net_value") or row.get("net_volume") or 0) < 0]
    total_buy_value = sum(row.get("buy_value") or 0 for row in parsed_rows)
    total_sell_value = sum(row.get("sell_value") or 0 for row in parsed_rows)
    total_net_value = sum(row.get("net_value") or 0 for row in parsed_rows)
    total_buy_volume = sum(row.get("buy_volume") or 0 for row in parsed_rows)
    total_sell_volume = sum(row.get("sell_volume") or 0 for row in parsed_rows)
    total_net_volume = sum(row.get("net_volume") or 0 for row in parsed_rows)
    gross_value = total_buy_value + total_sell_value
    gross_volume = total_buy_volume + total_sell_volume
    net_ratio_value = total_net_value / gross_value if gross_value else None
    net_ratio_volume = total_net_volume / gross_volume if gross_volume else None
    top_buyers = sorted(positive, key=lambda row: row.get("net_value") or row.get("net_volume") or 0, reverse=True)[:5]
    top_sellers = sorted(negative, key=lambda row: row.get("net_value") or row.get("net_volume") or 0)[:5]
    positive_total = sum(row.get("net_value") or row.get("net_volume") or 0 for row in positive)
    top3_positive = sum(row.get("net_value") or row.get("net_volume") or 0 for row in top_buyers[:3])
    concentration = top3_positive / positive_total if positive_total else None

    score = 50.0
    reasons: List[str] = []
    ratio = net_ratio_value if net_ratio_value is not None else net_ratio_volume
    if ratio is not None:
        score += max(-30.0, min(30.0, ratio * 160.0))
        reasons.append(f"net broker ratio {ratio:.3f}")
    if concentration is not None:
        if concentration >= 0.70:
            score += 10
            reasons.append(f"top 3 buyer concentration {concentration:.1%}")
        elif concentration <= 0.35:
            score -= 4
            reasons.append(f"buyer concentration menyebar {concentration:.1%}")
    if len(top_buyers) >= 3:
        score += 4
        reasons.append("minimal 3 broker net buy")
    if len(top_sellers) > len(top_buyers) + 2:
        score -= 8
        reasons.append("jumlah broker seller lebih dominan")
    score = clamp(score)
    if score >= 72:
        label = "AKUMULASI BROKER KUAT"
    elif score >= 58:
        label = "AKUMULASI BROKER RINGAN"
    elif score >= 45:
        label = "BROKER SUMMARY NETRAL"
    elif score >= 32:
        label = "DISTRIBUSI BROKER RINGAN"
    else:
        label = "DISTRIBUSI BROKER KUAT"
    return {
        "status": "OK",
        "score": round(score, 1),
        "label": label,
        "total_net_value": round(total_net_value, 2),
        "total_net_volume": round(total_net_volume, 2),
        "net_ratio_value": round(net_ratio_value, 4) if net_ratio_value is not None else None,
        "net_ratio_volume": round(net_ratio_volume, 4) if net_ratio_volume is not None else None,
        "top_buyer_brokers": top_buyers,
        "top_seller_brokers": top_sellers,
        "top3_buyer_concentration": round(concentration, 4) if concentration is not None else None,
        "broker_count": len(parsed_rows),
        "reasons": reasons,
    }


def latest_broker_summary_signal(ticker: str, roots: Sequence[Path]) -> Dict[str, Any]:
    candidates: List[Path] = []
    base = ticker_base(ticker).lower()
    for root in roots:
        if not root.exists():
            continue
        for pattern in ["*.csv", "*.xlsx", "*.xls"]:
            candidates.extend(path for path in root.rglob(pattern) if path.is_file())
    candidates = [
        path
        for path in candidates
        if path.stat().st_size <= 5_000_000
        and any(term in path.name.lower() or term in str(path.parent).lower() for term in [base, "broker", "bandar", "summary", "stockbit"])
    ]
    candidates = sorted(set(candidates), key=lambda path: path.stat().st_mtime, reverse=True)[:30]
    for path in candidates:
        for table in read_broker_tables(path):
            parsed = parse_broker_summary_rows(table)
            if parsed:
                parsed["source_file"] = str(path)
                parsed["source"] = "Uploaded broker summary / Stockbit-style export parsed locally"
                return parsed
    return {}


def indexalpha_cache_path(ticker: str, from_date: str, to_date: str, investor: str, market: str, cache_dir: Path) -> Path:
    safe_parts = [ticker_base(ticker), from_date, to_date, investor, market]
    safe_name = "_".join(re.sub(r"[^A-Za-z0-9_-]+", "", part) for part in safe_parts)
    return cache_dir / f"indexalpha_{safe_name}.json"


def fetch_indexalpha_broker_summary(ticker: str, cache_dir: Optional[Path] = DEFAULT_BROKER_CACHE) -> Dict[str, Any]:
    token = os.getenv("INDEXALPHA_API_TOKEN") or os.getenv("INDEX_ALPHA_API_TOKEN")
    if not token:
        return {}
    try:
        import requests
    except ImportError:
        return {"status": "ERROR", "provider": "indexalpha", "warning": "requests is not installed"}

    lookback_days = int(os.getenv("BANDAR_LOOKBACK_DAYS", "7") or "7")
    lookback_days = max(1, min(30, lookback_days))
    to_day = datetime.now().date()
    from_day = to_day - timedelta(days=lookback_days)
    from_date = os.getenv("BANDAR_FROM_DATE") or from_day.isoformat()
    to_date = os.getenv("BANDAR_TO_DATE") or to_day.isoformat()
    investor = (os.getenv("BANDAR_INVESTOR") or "all").lower()
    market = (os.getenv("BANDAR_MARKET") or "RG").upper()
    base_url = (os.getenv("INDEXALPHA_BASE_URL") or "https://api.indexalpha.id").rstrip("/")
    cache_seconds = int(os.getenv("BANDAR_API_CACHE_SECONDS", "1800") or "1800")
    cache_path = indexalpha_cache_path(ticker, from_date, to_date, investor, market, cache_dir or DEFAULT_BROKER_CACHE)
    if cache_dir and cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < cache_seconds:
        cached = read_json(cache_path)
        if cached:
            cached["cache"] = {"hit": True, "path": str(cache_path)}
            return cached

    params = {
        "ticker": ticker_base(ticker),
        "from": from_date,
        "to": to_date,
        "investor": investor,
        "market": market,
    }
    try:
        response = requests.get(
            f"{base_url}/stocks/broker-summary",
            params=params,
            headers={"accept": "application/json", "Authorization": f"Bearer {token}"},
            timeout=8,
        )
    except Exception as exc:
        return {"status": "ERROR", "provider": "indexalpha", "warning": f"request failed: {exc}"}
    if response.status_code >= 400:
        return {
            "status": "ERROR",
            "provider": "indexalpha",
            "warning": f"HTTP {response.status_code}",
            "as_of_range": {"from": from_date, "to": to_date},
        }
    try:
        payload = response.json()
    except ValueError:
        return {"status": "ERROR", "provider": "indexalpha", "warning": "invalid JSON response"}

    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return {
            "status": "NO DATA",
            "provider": "indexalpha",
            "warning": "broker summary API returned no rows",
            "as_of_range": {"from": from_date, "to": to_date},
        }

    normalized_rows: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized_rows.append(
            {
                "Broker": row.get("code") or row.get("broker") or row.get("broker_code"),
                "Buy Value": row.get("buy_value"),
                "Sell Value": row.get("sell_value"),
                "Buy Lot": row.get("buy_volume") or row.get("buy_lot"),
                "Sell Lot": row.get("sell_volume") or row.get("sell_lot"),
                "Avg Buy": row.get("buy_avg") or row.get("avg_buy"),
                "Avg Sell": row.get("sell_avg") or row.get("avg_sell"),
            }
        )
    parsed = parse_broker_summary_rows(normalized_rows)
    if not parsed:
        return {
            "status": "NO DATA",
            "provider": "indexalpha",
            "warning": "broker summary rows could not be parsed",
            "as_of_range": {"from": from_date, "to": to_date},
        }

    parsed.update(
        {
            "provider": "indexalpha",
            "source": "Index Alpha API broker summary",
            "source_file": "",
            "as_of_range": {"from": from_date, "to": to_date},
            "market": market,
            "investor": investor,
            "fetched_at": utc_now_text(),
            "cache": {"hit": False, "path": str(cache_path) if cache_dir else ""},
        }
    )
    if cache_dir:
        write_json(cache_path, parsed)
    return parsed


def broker_summary_signal(ticker: str, upload_roots: Sequence[Path], cache_dir: Optional[Path]) -> Dict[str, Any]:
    provider = (os.getenv("BANDAR_PROVIDER") or "auto").lower()
    if provider in {"auto", "indexalpha"}:
        remote = fetch_indexalpha_broker_summary(ticker, cache_dir=cache_dir)
        if remote.get("score") is not None:
            return remote
        if provider == "indexalpha":
            return remote
    uploaded = latest_broker_summary_signal(ticker, upload_roots)
    if uploaded:
        return uploaded
    return {}


def split_tokens(value: str) -> List[str]:
    return [part.strip() for part in re.split(r"[,;\s|]+", value or "") if part.strip()]


def indexalpha_tokens() -> List[str]:
    candidates: List[str] = []
    for env_key in ["INDEXALPHA_API_KEYS", "INDEX_ALPHA_API_KEYS", "INDEXALPHA_API_TOKEN", "INDEX_ALPHA_API_TOKEN"]:
        candidates.extend(split_tokens(os.getenv(env_key, "")))
    unique: List[str] = []
    seen = set()
    for token in candidates:
        if token in seen:
            continue
        seen.add(token)
        unique.append(token)
    return unique


def broker_summary_query_params(ticker: str) -> Dict[str, str]:
    lookback_days = int(os.getenv("BANDAR_LOOKBACK_DAYS", "7") or "7")
    lookback_days = max(1, min(30, lookback_days))
    to_day = datetime.now().date()
    from_day = to_day - timedelta(days=lookback_days)
    return {
        "ticker": ticker_base(ticker),
        "from": os.getenv("BANDAR_FROM_DATE") or from_day.isoformat(),
        "to": os.getenv("BANDAR_TO_DATE") or to_day.isoformat(),
        "investor": (os.getenv("BANDAR_INVESTOR") or "all").lower(),
        "market": (os.getenv("BANDAR_MARKET") or "RG").upper(),
    }


def bandarmology_source_options(ticker: str) -> List[Dict[str, str]]:
    base = ticker_base(ticker)
    return [
        {
            "name": "RapidAPI Indonesia Stock Exchange IDX",
            "url": "https://rapidapi.com/yasimpratama88/api/indonesia-stock-exchange-idx",
            "integration": "automatic API via RAPIDAPI_IDX_KEYS / RAPIDAPI_IDX_KEY; bandar accumulation and distribution endpoints",
        },
        {
            "name": "Index Alpha API",
            "url": "https://indexalpha.id/docs/endpoints",
            "integration": "automatic API via INDEXALPHA_API_KEYS / INDEXALPHA_API_TOKEN",
        },
        {
            "name": "Stockbit Bandar Detector / Broker Summary",
            "url": f"https://stockbit.com/symbol/{base}",
            "integration": "view in Stockbit Pro or export/upload CSV/XLSX when available",
        },
        {
            "name": "Ajaib Terminal Broker Summary",
            "url": "https://trade.ajaib.co.id/",
            "integration": "view in Ajaib Terminal or export/upload CSV/XLSX when available",
        },
        {
            "name": "IDX Broker Summary",
            "url": "https://www.idx.co.id/en/market-data/trading-summary/broker-summary",
            "integration": "official market-data reference; upload structured export if available",
        },
        {
            "name": "TradingView IDX Chart",
            "url": f"https://www.tradingview.com/symbols/IDX-{base}/",
            "integration": "chart/technicals only; not a raw broker-summary feed",
        },
    ]


def parse_broker_summary_payload(payload: Any, ticker: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    rows: Any = None
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("rows") or payload.get("result")
    elif isinstance(payload, list):
        rows = payload
    if not isinstance(rows, list) or not rows:
        return {
            "status": "NO DATA",
            "warning": "broker summary provider returned no rows",
            "alternatives": bandarmology_source_options(ticker),
            **metadata,
        }
    normalized_rows: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized_rows.append(
            {
                "Broker": row.get("code") or row.get("broker") or row.get("broker_code") or row.get("Broker"),
                "Buy Value": row.get("buy_value") or row.get("buyValue") or row.get("b_value") or row.get("B. Val") or row.get("Buy Value"),
                "Sell Value": row.get("sell_value") or row.get("sellValue") or row.get("s_value") or row.get("S. Val") or row.get("Sell Value"),
                "Net Value": row.get("net_value") or row.get("netValue") or row.get("net") or row.get("Net Value"),
                "Buy Lot": row.get("buy_volume") or row.get("buy_lot") or row.get("buyLot") or row.get("B. Lot") or row.get("Buy Lot"),
                "Sell Lot": row.get("sell_volume") or row.get("sell_lot") or row.get("sellLot") or row.get("S. Lot") or row.get("Sell Lot"),
                "Net Lot": row.get("net_volume") or row.get("net_lot") or row.get("netLot") or row.get("Net Lot"),
                "Avg Buy": row.get("buy_avg") or row.get("avg_buy") or row.get("buyAvg") or row.get("B. Avg") or row.get("Avg Buy"),
                "Avg Sell": row.get("sell_avg") or row.get("avg_sell") or row.get("sellAvg") or row.get("S. Avg") or row.get("Avg Sell"),
            }
        )
    parsed = parse_broker_summary_rows(normalized_rows)
    if not parsed:
        return {
            "status": "NO DATA",
            "warning": "broker summary rows could not be parsed",
            "alternatives": bandarmology_source_options(ticker),
            **metadata,
        }
    parsed.update(metadata)
    parsed["alternatives"] = bandarmology_source_options(ticker)
    return parsed


def fetch_indexalpha_broker_summary(ticker: str, cache_dir: Optional[Path] = DEFAULT_BROKER_CACHE) -> Dict[str, Any]:
    tokens = indexalpha_tokens()
    if not tokens:
        return {}
    try:
        import requests
    except ImportError:
        return {"status": "ERROR", "provider": "indexalpha", "warning": "requests is not installed", "alternatives": bandarmology_source_options(ticker)}

    params = broker_summary_query_params(ticker)
    base_url = (os.getenv("INDEXALPHA_BASE_URL") or "https://api.indexalpha.id").rstrip("/")
    cache_seconds = int(os.getenv("BANDAR_API_CACHE_SECONDS", "1800") or "1800")
    cache_path = indexalpha_cache_path(ticker, params["from"], params["to"], params["investor"], params["market"], cache_dir or DEFAULT_BROKER_CACHE)
    if cache_dir and cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < cache_seconds:
        cached = read_json(cache_path)
        if cached:
            cached["cache"] = {"hit": True, "path": str(cache_path)}
            return cached

    attempts: List[Dict[str, Any]] = []
    last_error = ""
    for index, token in enumerate(tokens, start=1):
        token_label = f"key_{index}"
        try:
            response = requests.get(
                f"{base_url}/stocks/broker-summary",
                params=params,
                headers={"accept": "application/json", "Authorization": f"Bearer {token}"},
                timeout=8,
            )
        except Exception as exc:
            last_error = f"request failed: {exc}"
            attempts.append({"key": token_label, "status": "request_failed", "warning": str(exc)[:120]})
            continue
        if response.status_code >= 400:
            last_error = f"HTTP {response.status_code}"
            attempts.append({"key": token_label, "status": response.status_code})
            if response.status_code in {401, 402, 403, 408, 429, 500, 502, 503, 504}:
                continue
            break
        try:
            payload = response.json()
        except ValueError:
            last_error = "invalid JSON response"
            attempts.append({"key": token_label, "status": "invalid_json"})
            continue
        if isinstance(payload, dict) and payload.get("success") is False:
            error_text = str(payload.get("error") or payload.get("message") or "provider returned success=false")
            last_error = error_text
            attempts.append({"key": token_label, "status": "provider_error", "warning": error_text[:120]})
            if any(term in error_text.lower() for term in ["limit", "quota", "rate", "unauthorized", "forbidden", "expired"]):
                continue
            break
        parsed = parse_broker_summary_payload(
            payload,
            ticker,
            {
                "provider": "indexalpha",
                "source": "Index Alpha API broker summary",
                "source_file": "",
                "as_of_range": {"from": params["from"], "to": params["to"]},
                "market": params["market"],
                "investor": params["investor"],
                "fetched_at": utc_now_text(),
                "api_key_used": token_label,
                "api_attempts": attempts + [{"key": token_label, "status": "success"}],
                "cache": {"hit": False, "path": str(cache_path) if cache_dir else ""},
            },
        )
        if parsed.get("score") is not None:
            if cache_dir:
                write_json(cache_path, parsed)
            return parsed
        last_error = str(parsed.get("warning") or "no usable broker summary rows")
        attempts.append({"key": token_label, "status": parsed.get("status", "NO DATA"), "warning": last_error[:120]})
        break
    return {
        "status": "ERROR",
        "provider": "indexalpha",
        "warning": f"all Index Alpha API keys failed or were rate-limited; last error: {last_error or 'unknown'}",
        "api_attempts": attempts,
        "as_of_range": {"from": params["from"], "to": params["to"]},
        "alternatives": bandarmology_source_options(ticker),
    }


def custom_bandar_cache_path(ticker: str, from_date: str, to_date: str, cache_dir: Path) -> Path:
    safe_parts = [ticker_base(ticker), from_date, to_date]
    safe_name = "_".join(re.sub(r"[^A-Za-z0-9_-]+", "", part) for part in safe_parts)
    return cache_dir / f"custom_bandar_{safe_name}.json"


def fetch_custom_bandar_api(ticker: str, cache_dir: Optional[Path] = DEFAULT_BROKER_CACHE) -> Dict[str, Any]:
    url_template = os.getenv("BANDAR_CUSTOM_API_URL", "").strip()
    if not url_template:
        return {}
    try:
        import requests
    except ImportError:
        return {"status": "ERROR", "provider": "custom", "warning": "requests is not installed", "alternatives": bandarmology_source_options(ticker)}

    params = broker_summary_query_params(ticker)
    cache_seconds = int(os.getenv("BANDAR_API_CACHE_SECONDS", "1800") or "1800")
    cache_path = custom_bandar_cache_path(ticker, params["from"], params["to"], cache_dir or DEFAULT_BROKER_CACHE)
    if cache_dir and cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < cache_seconds:
        cached = read_json(cache_path)
        if cached:
            cached["cache"] = {"hit": True, "path": str(cache_path)}
            return cached

    url = url_template.format(**params)
    headers = {"accept": "application/json"}
    api_key = os.getenv("BANDAR_CUSTOM_API_KEY", "").strip()
    if api_key:
        header_name = os.getenv("BANDAR_CUSTOM_API_HEADER", "Authorization").strip() or "Authorization"
        prefix = os.getenv("BANDAR_CUSTOM_API_PREFIX", "Bearer").strip()
        headers[header_name] = f"{prefix} {api_key}".strip()
    try:
        response = requests.get(url, headers=headers, timeout=8)
    except Exception as exc:
        return {"status": "ERROR", "provider": "custom", "warning": f"request failed: {exc}", "alternatives": bandarmology_source_options(ticker)}
    if response.status_code >= 400:
        return {"status": "ERROR", "provider": "custom", "warning": f"HTTP {response.status_code}", "alternatives": bandarmology_source_options(ticker)}
    try:
        payload = response.json()
    except ValueError:
        return {"status": "ERROR", "provider": "custom", "warning": "invalid JSON response", "alternatives": bandarmology_source_options(ticker)}
    parsed = parse_broker_summary_payload(
        payload,
        ticker,
        {
            "provider": "custom",
            "source": "Custom broker summary API",
            "source_file": "",
            "as_of_range": {"from": params["from"], "to": params["to"]},
            "fetched_at": utc_now_text(),
            "cache": {"hit": False, "path": str(cache_path) if cache_dir else ""},
        },
    )
    if parsed.get("score") is not None and cache_dir:
        write_json(cache_path, parsed)
    return parsed


def rapidapi_tokens() -> List[str]:
    candidates: List[str] = []
    for env_key in ["RAPIDAPI_IDX_KEYS", "RAPIDAPI_IDX_KEY", "RAPIDAPI_KEY"]:
        candidates.extend(split_tokens(os.getenv(env_key, "")))
    unique: List[str] = []
    seen = set()
    for token in candidates:
        if token in seen:
            continue
        seen.add(token)
        unique.append(token)
    return unique


def rapidapi_cache_path(ticker: str, days: int, cache_dir: Path) -> Path:
    safe_name = "_".join(re.sub(r"[^A-Za-z0-9_-]+", "", part) for part in [ticker_base(ticker), str(days)])
    return cache_dir / f"rapidapi_bandar_{safe_name}.json"


def rapidapi_feature_cache_path(feature: str, ticker: str, days: int, cache_dir: Path) -> Path:
    safe_name = "_".join(re.sub(r"[^A-Za-z0-9_-]+", "", part) for part in [feature, ticker_base(ticker), str(days)])
    return cache_dir / f"rapidapi_{safe_name}.json"


LAST_RAPIDAPI_CALL = 0.0


def rapidapi_request(path: str, tokens: Sequence[str]) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], str]:
    global LAST_RAPIDAPI_CALL
    try:
        import requests
    except ImportError:
        return None, [], "requests is not installed"
    host = os.getenv("RAPIDAPI_IDX_HOST", "indonesia-stock-exchange-idx.p.rapidapi.com").strip()
    attempts: List[Dict[str, Any]] = []
    last_error = ""
    for index, token in enumerate(tokens, start=1):
        token_label = f"key_{index}"
        try:
            min_gap = safe_float(os.getenv("RAPIDAPI_REQUEST_SLEEP_SECONDS", "1.15")) or 0.0
            if min_gap > 0:
                elapsed = time.time() - LAST_RAPIDAPI_CALL
                if elapsed < min_gap:
                    time.sleep(min(min_gap - elapsed, 5.0))
            response = requests.get(
                f"https://{host}{path}",
                headers={
                    "accept": "application/json",
                    "Content-Type": "application/json",
                    "x-rapidapi-host": host,
                    "x-rapidapi-key": token,
                },
                timeout=10,
            )
            LAST_RAPIDAPI_CALL = time.time()
        except Exception as exc:
            last_error = f"request failed: {exc}"
            attempts.append({"key": token_label, "status": "request_failed", "warning": str(exc)[:120]})
            continue
        if response.status_code >= 400:
            last_error = f"HTTP {response.status_code}"
            attempts.append({"key": token_label, "status": response.status_code})
            if response.status_code in {401, 402, 403, 408, 429, 500, 502, 503, 504}:
                continue
            break
        try:
            payload = response.json()
        except ValueError:
            last_error = "invalid JSON response"
            attempts.append({"key": token_label, "status": "invalid_json"})
            continue
        if isinstance(payload, dict) and payload.get("success") is False:
            error_text = str(payload.get("error") or payload.get("message") or "provider returned success=false")
            last_error = error_text
            attempts.append({"key": token_label, "status": "provider_error", "warning": error_text[:120]})
            if any(term in error_text.lower() for term in ["limit", "quota", "rate", "unauthorized", "forbidden", "expired"]):
                continue
            break
        attempts.append({"key": token_label, "status": "success"})
        return payload, attempts, ""
    return None, attempts, last_error or "all RapidAPI keys failed"


def rapidapi_status_label(score: float) -> str:
    if score >= 72:
        return "AKUMULASI BANDAR KUAT"
    if score >= 58:
        return "AKUMULASI BANDAR RINGAN"
    if score >= 45:
        return "BANDAR NETRAL"
    if score >= 32:
        return "DISTRIBUSI BANDAR RINGAN"
    return "DISTRIBUSI BANDAR KUAT"


def fetch_rapidapi_bandar_analysis(ticker: str, cache_dir: Optional[Path] = DEFAULT_BROKER_CACHE) -> Dict[str, Any]:
    tokens = rapidapi_tokens()
    if not tokens:
        return {}
    days = int(os.getenv("RAPIDAPI_BANDAR_DAYS", os.getenv("BANDAR_LOOKBACK_DAYS", "30")) or "30")
    days = max(1, min(120, days))
    cache_seconds = int(os.getenv("BANDAR_API_CACHE_SECONDS", "1800") or "1800")
    cache_path = rapidapi_cache_path(ticker, days, cache_dir or DEFAULT_BROKER_CACHE)
    if cache_dir and cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < cache_seconds:
        cached = read_json(cache_path)
        if cached:
            cached["cache"] = {"hit": True, "path": str(cache_path)}
            return cached

    base = ticker_base(ticker)
    accumulation_payload, accumulation_attempts, accumulation_error = rapidapi_request(
        f"/api/analysis/bandar/accumulation/{base}?days={days}",
        tokens,
    )
    distribution_payload, distribution_attempts, distribution_error = rapidapi_request(
        f"/api/analysis/bandar/distribution/{base}?days={days}",
        tokens,
    )
    if not accumulation_payload and not distribution_payload:
        return {
            "status": "ERROR",
            "provider": "rapidapi_idx",
            "warning": f"RapidAPI bandar accumulation/distribution failed: {accumulation_error or distribution_error}",
            "api_attempts": {"accumulation": accumulation_attempts, "distribution": distribution_attempts},
            "alternatives": bandarmology_source_options(ticker),
        }

    acc = accumulation_payload.get("data", {}) if isinstance(accumulation_payload, dict) else {}
    dist = distribution_payload.get("data", {}) if isinstance(distribution_payload, dict) else {}
    acc_score_raw = safe_float(acc.get("accumulation_score"))
    dist_score_raw = safe_float(dist.get("distribution_score"))
    acc_score = clamp((acc_score_raw or 0) * 10)
    dist_score = clamp((dist_score_raw or 0) * 10)
    confidence_values = [safe_float(acc.get("confidence")), safe_float(dist.get("confidence"))]
    confidence_values = [value for value in confidence_values if value is not None]
    provider_confidence = sum(confidence_values) / len(confidence_values) if confidence_values else 55.0
    score = 50 + (acc_score - dist_score) * 0.45 + (provider_confidence - 55) * 0.10
    recommendation = str(acc.get("recommendation") or dist.get("recommendation") or "").upper()
    risk_level = str(acc.get("risk_level") or dist.get("risk_level") or "").upper()
    status_text = str(acc.get("status") or dist.get("status") or "").upper()
    if recommendation in {"BUY", "ACCUMULATE"}:
        score += 8
    elif recommendation in {"TAKE_PROFIT", "SELL", "REDUCE"}:
        score -= 8
    if risk_level == "HIGH":
        score -= 5
    elif risk_level == "LOW":
        score += 3
    if status_text in {"ACCUMULATION", "BULLISH"}:
        score += 6
    elif status_text in {"DISTRIBUTION", "BEARISH"}:
        score -= 6
    score = clamp(score)

    indicators = acc.get("indicators", {}) if isinstance(acc.get("indicators"), dict) else {}
    distribution_indicators = dist.get("indicators", {}) if isinstance(dist.get("indicators"), dict) else {}
    broker_concentration = indicators.get("broker_concentration", {}) if isinstance(indicators.get("broker_concentration"), dict) else {}
    broker_exit = distribution_indicators.get("broker_exit_pattern", {}) if isinstance(distribution_indicators.get("broker_exit_pattern"), dict) else {}
    reasons = [
        f"RapidAPI accumulation {acc_score_raw if acc_score_raw is not None else 'n/a'}/10",
        f"distribution {dist_score_raw if dist_score_raw is not None else 'n/a'}/10",
        f"provider confidence {provider_confidence:.1f}%",
    ]
    if recommendation:
        reasons.append(f"recommendation {recommendation}")
    if dist.get("signals"):
        reasons.extend(str(signal) for signal in dist.get("signals", [])[:3])
    if broker_concentration.get("net_flow") is not None:
        reasons.append(f"net flow {broker_concentration.get('net_flow')}")

    result = {
        "status": "OK",
        "score": round(score, 1),
        "label": rapidapi_status_label(score),
        "provider": "rapidapi_idx",
        "source": "RapidAPI Indonesia Stock Exchange IDX bandar accumulation/distribution",
        "source_file": "",
        "analysis_date": acc.get("analysis_date") or dist.get("analysis_date"),
        "lookback_days": days,
        "provider_confidence": round(provider_confidence, 1),
        "accumulation": acc,
        "distribution": dist,
        "entry_zone": acc.get("entry_zone") or {},
        "risk_level": risk_level,
        "recommendation": recommendation,
        "top_buyer_brokers": [{"broker": broker} for broker in broker_concentration.get("top_5_brokers", [])[:5]],
        "top_seller_brokers": [{"broker": broker} for broker in broker_exit.get("top_brokers_selling", [])[:5]],
        "total_net_volume": safe_float(broker_concentration.get("net_flow")) or safe_float(broker_exit.get("net_flow")),
        "total_net_value": None,
        "api_attempts": {"accumulation": accumulation_attempts, "distribution": distribution_attempts},
        "alternatives": bandarmology_source_options(ticker),
        "reasons": reasons,
        "provider_quality_score": round(clamp(provider_confidence + 25, 0, 95), 1),
        "cache": {"hit": False, "path": str(cache_path) if cache_dir else ""},
    }
    if cache_dir:
        write_json(cache_path, result)
    return result


def fetch_rapidapi_feature(ticker: str, feature: str, path: str, days: int, cache_dir: Optional[Path]) -> Dict[str, Any]:
    tokens = rapidapi_tokens()
    if not tokens:
        return {}
    cache_seconds = int(os.getenv("BANDAR_API_CACHE_SECONDS", "1800") or "1800")
    cache_path = rapidapi_feature_cache_path(feature, ticker, days, cache_dir or DEFAULT_BROKER_CACHE)
    if cache_dir and cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < cache_seconds:
        cached = read_json(cache_path)
        if cached:
            cached["cache"] = {"hit": True, "path": str(cache_path)}
            return cached
    payload, attempts, error = rapidapi_request(path, tokens)
    if not payload:
        return {
            "status": "ERROR",
            "provider": "rapidapi_idx",
            "feature": feature,
            "warning": error,
            "api_attempts": attempts,
        }
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return {
            "status": "NO DATA",
            "provider": "rapidapi_idx",
            "feature": feature,
            "warning": "RapidAPI response has no data object",
            "api_attempts": attempts,
        }
    result = {
        "status": "OK",
        "provider": "rapidapi_idx",
        "feature": feature,
        "source": f"RapidAPI IDX {feature}",
        "data": data,
        "api_attempts": attempts,
        "cache": {"hit": False, "path": str(cache_path) if cache_dir else ""},
    }
    if cache_dir:
        write_json(cache_path, result)
    return result


def rapidapi_signal_points(signal: str) -> float:
    text = str(signal or "").upper()
    if text in {"STRONG_BUY", "BUY", "BULLISH", "ACCUMULATING"}:
        return 12.0
    if text in {"WEAK_BUY", "MILD_BULLISH"}:
        return 6.0
    if text in {"SELL", "STRONG_SELL", "BEARISH", "DISTRIBUTING"}:
        return -12.0
    if text in {"WEAK_SELL", "MILD_BEARISH"}:
        return -6.0
    return 0.0


def score_rapidapi_technical_data(data: Dict[str, Any]) -> Tuple[Optional[float], List[str]]:
    if not data:
        return None, []
    indicators = data.get("indicators", {}) if isinstance(data.get("indicators"), dict) else {}
    trend = data.get("trend", {}) if isinstance(data.get("trend"), dict) else {}
    signal = data.get("signal", {}) if isinstance(data.get("signal"), dict) else {}
    summary = data.get("summary", {}) if isinstance(data.get("summary"), dict) else {}
    score = 50.0
    reasons: List[str] = []
    action = str(signal.get("action") or "").upper()
    confidence = safe_float(signal.get("confidence"))
    if action:
        score += rapidapi_signal_points(action)
        reasons.append(f"RapidAPI technical action {action} confidence {confidence or 0:.0f}%")
    overall_trend = str(trend.get("overallTrend") or "").upper()
    if overall_trend:
        score += rapidapi_signal_points(overall_trend)
        reasons.append(f"RapidAPI trend {overall_trend}")
    trend_strength = safe_float(trend.get("trendStrength"))
    if trend_strength is not None and overall_trend == "BULLISH":
        score += min(10, trend_strength / 10)
    elif trend_strength is not None and overall_trend == "BEARISH":
        score -= min(10, trend_strength / 10)
    rsi = indicators.get("rsi", {}) if isinstance(indicators.get("rsi"), dict) else {}
    macd = indicators.get("macd", {}) if isinstance(indicators.get("macd"), dict) else {}
    obv = indicators.get("obv", {}) if isinstance(indicators.get("obv"), dict) else {}
    vwap = indicators.get("vwap", {}) if isinstance(indicators.get("vwap"), dict) else {}
    rsi_value = safe_float(rsi.get("value"))
    if rsi_value is not None:
        if 45 <= rsi_value <= 68:
            score += 7
        elif rsi_value > 75:
            score -= 6
        elif rsi_value < 35:
            score -= 5
        reasons.append(f"RapidAPI RSI {rsi_value:.1f}")
    for label, node in [("MACD", macd), ("OBV", obv), ("VWAP", vwap)]:
        sig = str(node.get("signal") or node.get("trend") or "").upper()
        if sig:
            score += rapidapi_signal_points(sig) * 0.45
            reasons.append(f"RapidAPI {label} {sig}")
    bullish = safe_float(summary.get("bullishSignals")) or 0
    bearish = safe_float(summary.get("bearishSignals")) or 0
    score += min(8, bullish * 2) - min(8, bearish * 2)
    return clamp(score), reasons[:6]


def fetch_rapidapi_technical_analysis(ticker: str, cache_dir: Optional[Path] = DEFAULT_BROKER_CACHE) -> Dict[str, Any]:
    if os.getenv("RAPIDAPI_TECHNICAL_ENABLED", "1").strip() in {"0", "false", "False"}:
        return {}
    days = int(os.getenv("RAPIDAPI_TECHNICAL_DAYS", os.getenv("RAPIDAPI_BANDAR_DAYS", "30")) or "30")
    days = max(10, min(240, days))
    result = fetch_rapidapi_feature(ticker, "technical", f"/api/analysis/technical/{ticker_base(ticker)}?days={days}", days, cache_dir)
    data = result.get("data", {}) if isinstance(result.get("data"), dict) else {}
    score, reasons = score_rapidapi_technical_data(data)
    if score is not None:
        result["score"] = round(score, 1)
        result["reasons"] = reasons
        result["last_price"] = safe_float(data.get("lastPrice"))
        result["provider_quality_score"] = 82.0
    return result


def score_rapidapi_sentiment_data(data: Dict[str, Any]) -> Tuple[Optional[float], List[str]]:
    if not data:
        return None, []
    retail = data.get("retail_sentiment", {}) if isinstance(data.get("retail_sentiment"), dict) else {}
    bandar = data.get("bandar_sentiment", {}) if isinstance(data.get("bandar_sentiment"), dict) else {}
    retail_score = safe_float(retail.get("score"))
    bandar_score = safe_float(bandar.get("score"))
    if retail_score is None and bandar_score is None:
        return None, []
    score = 50.0
    reasons: List[str] = []
    if bandar_score is not None:
        score += (bandar_score - 5) * 8
        reasons.append(f"RapidAPI bandar sentiment {bandar_score:.1f}/10 {bandar.get('status') or ''}".strip())
    if retail_score is not None:
        retail_status = str(retail.get("status") or "").upper()
        danger = str(retail.get("danger_level") or "").upper()
        if retail_status in {"EUPHORIC", "FOMO"} or danger == "HIGH":
            score -= 9
        elif retail_status in {"FEARFUL", "PANIC"}:
            score += 3
        reasons.append(f"RapidAPI retail sentiment {retail_score:.1f}/10 {retail_status}".strip())
    indicators = bandar.get("indicators", {}) if isinstance(bandar.get("indicators"), dict) else {}
    foreign_flow = parse_broker_number(indicators.get("foreign_flow"))
    top_flow = parse_broker_number(indicators.get("top_broker_net_flow"))
    if foreign_flow is not None:
        score += 6 if foreign_flow > 0 else -6
        reasons.append(f"foreign flow {foreign_flow:,.0f}")
    if top_flow is not None:
        score += 5 if top_flow > 0 else -5
        reasons.append(f"top broker flow {top_flow:,.0f}")
    return clamp(score), reasons[:6]


def fetch_rapidapi_sentiment_analysis(ticker: str, cache_dir: Optional[Path] = DEFAULT_BROKER_CACHE) -> Dict[str, Any]:
    if os.getenv("RAPIDAPI_SENTIMENT_ENABLED", "1").strip() in {"0", "false", "False"}:
        return {}
    days = int(os.getenv("RAPIDAPI_SENTIMENT_DAYS", "7") or "7")
    days = max(1, min(60, days))
    result = fetch_rapidapi_feature(ticker, "sentiment", f"/api/analysis/sentiment/{ticker_base(ticker)}?days={days}", days, cache_dir)
    data = result.get("data", {}) if isinstance(result.get("data"), dict) else {}
    score, reasons = score_rapidapi_sentiment_data(data)
    if score is not None:
        result["score"] = round(score, 1)
        result["reasons"] = reasons
        result["last_price"] = safe_float(data.get("current_price"))
        result["provider_quality_score"] = 80.0
    return result


def provider_quality(signal: Dict[str, Any]) -> float:
    if not signal or signal.get("score") is None:
        return 0.0
    provider = str(signal.get("provider") or "").lower()
    base = safe_float(signal.get("provider_quality_score"))
    if base is not None:
        return base
    if provider == "rapidapi_idx":
        return 84.0
    if provider == "indexalpha":
        return 76.0
    if provider == "custom":
        return 72.0
    if signal.get("source_file"):
        return 68.0
    return 55.0


def broker_summary_signal(ticker: str, upload_roots: Sequence[Path], cache_dir: Optional[Path]) -> Dict[str, Any]:
    provider = (os.getenv("BANDAR_PROVIDER") or "auto").lower()
    candidates: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    provider_map = {
        "rapidapi": fetch_rapidapi_bandar_analysis,
        "rapidapi_idx": fetch_rapidapi_bandar_analysis,
        "indexalpha": fetch_indexalpha_broker_summary,
        "custom": fetch_custom_bandar_api,
    }
    order = ["rapidapi", "indexalpha", "custom"] if provider == "auto" else [provider]
    for name in order:
        fetcher = provider_map.get(name)
        if not fetcher:
            continue
        signal = fetcher(ticker, cache_dir=cache_dir)
        if signal.get("score") is not None:
            candidates.append(signal)
        elif signal:
            errors.append({"provider": signal.get("provider") or name, "status": signal.get("status"), "warning": signal.get("warning")})
        if provider != "auto" and signal:
            return signal
    uploaded = latest_broker_summary_signal(ticker, upload_roots)
    if uploaded:
        uploaded["provider"] = uploaded.get("provider") or "upload"
        uploaded["provider_quality_score"] = provider_quality(uploaded)
        candidates.append(uploaded)
    if candidates:
        for signal in candidates:
            signal["provider_quality_score"] = provider_quality(signal)
        best = sorted(candidates, key=provider_quality, reverse=True)[0]
        best["provider_comparison"] = [
            {
                "provider": item.get("provider") or item.get("source"),
                "score": item.get("score"),
                "label": item.get("label"),
                "quality": provider_quality(item),
                "source": item.get("source"),
            }
            for item in sorted(candidates, key=provider_quality, reverse=True)
        ]
        if errors:
            best["provider_errors"] = errors
        return best
    return {
        "status": "NO DATA",
        "provider_errors": errors,
        "alternatives": bandarmology_source_options(ticker),
    }


def fetch_recent_news(profile: Dict[str, str], days: int, max_records: int) -> Dict[str, Any]:
    if collect_for_profile is None or summarize is None or dedupe_items is None:
        return {"summary": {}, "items": [], "warnings": ["process_news module unavailable"]}
    warnings: List[str] = []
    try:
        items = collect_for_profile(profile, days=max(1, days), max_records=max(1, max_records), use_gdelt=False, use_google=True)
        items = dedupe_items(items)
        rows = summarize(items, profiles=[profile], include_empty=True)
        summary = rows[0] if rows else {}
        return {
            "summary": summary,
            "items": [item.__dict__ for item in items[:8]],
            "warnings": warnings,
        }
    except Exception as exc:
        return {"summary": {}, "items": [], "warnings": [str(exc)]}


def fetch_market_history(ticker: str, period: str, interval: str) -> Tuple[Any, List[str]]:
    warnings: List[str] = []
    try:
        import yfinance as yf
    except ImportError:
        return None, ["yfinance not installed"]
    try:
        data = yf.Ticker(normalize_ticker(ticker)).history(period=period, interval=interval, auto_adjust=False)
    except Exception as exc:
        return None, [f"market history fetch failed: {exc}"]
    if data is None or data.empty:
        return None, ["market history empty"]
    return data.dropna(how="all"), warnings


def calculate_rsi(close: Any, window: int = 14) -> Optional[float]:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = (-delta.clip(upper=0)).rolling(window).mean()
    rs = gain / loss.replace(0, math.nan)
    rsi = 100 - (100 / (1 + rs))
    value = safe_float(rsi.iloc[-1])
    return value


def calculate_atr(data: Any, window: int = 14) -> Optional[float]:
    high = data["High"]
    low = data["Low"]
    close = data["Close"]
    prev_close = close.shift(1)
    tr = (high - low).abs().to_frame("hl")
    tr["hc"] = (high - prev_close).abs()
    tr["lc"] = (low - prev_close).abs()
    atr = tr.max(axis=1).rolling(window).mean()
    return safe_float(atr.iloc[-1])


def calculate_mfi(data: Any, window: int = 14) -> Optional[float]:
    typical = (data["High"] + data["Low"] + data["Close"]) / 3
    raw_money_flow = typical * data["Volume"]
    direction = typical.diff()
    positive = raw_money_flow.where(direction > 0, 0.0).rolling(window).sum()
    negative = raw_money_flow.where(direction < 0, 0.0).rolling(window).sum().abs()
    ratio = positive / negative.replace(0, math.nan)
    mfi = 100 - (100 / (1 + ratio))
    return safe_float(mfi.iloc[-1])


def calculate_obv(data: Any) -> Any:
    close = data["Close"]
    volume = data["Volume"].fillna(0)
    direction = close.diff().apply(lambda value: 1 if value > 0 else -1 if value < 0 else 0)
    return (direction * volume).fillna(0).cumsum()


def calculate_ad_line(data: Any) -> Any:
    high = data["High"]
    low = data["Low"]
    close = data["Close"]
    volume = data["Volume"].fillna(0)
    denominator = (high - low).replace(0, math.nan)
    money_flow_multiplier = (((close - low) - (high - close)) / denominator).fillna(0)
    return (money_flow_multiplier * volume).cumsum()


def slope_score(series: Any, window: int = 20) -> Optional[float]:
    if series is None or len(series) < window + 1:
        return None
    latest = safe_float(series.iloc[-1])
    prior = safe_float(series.iloc[-window])
    if latest is None or prior is None:
        return None
    scale = max(abs(prior), abs(latest), 1.0)
    return (latest - prior) / scale


def bandar_volume_signal(data: Any, current: float) -> Dict[str, Any]:
    volume = data["Volume"].dropna() if "Volume" in data else None
    if volume is None or len(volume) < 25:
        return {
            "status": "NO DATA",
            "score": None,
            "label": "Bandar volume belum cukup data",
            "volume_ratio20": None,
            "mfi14": None,
            "reasons": ["butuh minimal 25 hari data volume"],
        }

    close = data["Close"].dropna()
    high = data["High"].dropna()
    low = data["Low"].dropna()
    last_volume = safe_float(volume.iloc[-1]) or 0.0
    avg_volume20 = safe_float(volume.tail(20).mean()) or 0.0
    volume_ratio20 = last_volume / avg_volume20 if avg_volume20 else None
    mfi14 = calculate_mfi(data)
    obv = calculate_obv(data)
    ad_line = calculate_ad_line(data)
    obv_slope = slope_score(obv, 20)
    ad_slope = slope_score(ad_line, 20)
    close_5 = safe_float(close.iloc[-5]) if len(close) >= 5 else None
    price_change5 = (current - close_5) / close_5 if close_5 else None
    close_position = None
    if len(high) and len(low):
        day_range = safe_float(high.iloc[-1] - low.iloc[-1])
        if day_range and day_range > 0:
            close_position = (current - safe_float(low.iloc[-1])) / day_range

    score = 50.0
    reasons: List[str] = []
    if volume_ratio20 is not None:
        if volume_ratio20 >= 1.8:
            score += 16
            reasons.append(f"volume spike {volume_ratio20:.2f}x rata-rata 20 hari")
        elif volume_ratio20 >= 1.2:
            score += 8
            reasons.append(f"volume di atas rata-rata {volume_ratio20:.2f}x")
        elif volume_ratio20 < 0.65:
            score -= 8
            reasons.append(f"volume tipis {volume_ratio20:.2f}x rata-rata")
    if obv_slope is not None:
        if obv_slope > 0.06:
            score += 14
            reasons.append("OBV 20 hari naik: indikasi akumulasi")
        elif obv_slope < -0.06:
            score -= 14
            reasons.append("OBV 20 hari turun: indikasi distribusi")
    if ad_slope is not None:
        if ad_slope > 0.05:
            score += 14
            reasons.append("Accumulation/Distribution Line naik")
        elif ad_slope < -0.05:
            score -= 14
            reasons.append("Accumulation/Distribution Line turun")
    if mfi14 is not None:
        if 50 <= mfi14 <= 78:
            score += 10
            reasons.append(f"MFI sehat {mfi14:.1f}")
        elif mfi14 > 84:
            score -= 8
            reasons.append(f"MFI sangat panas {mfi14:.1f}")
        elif mfi14 < 35:
            score -= 8
            reasons.append(f"MFI lemah {mfi14:.1f}")
    if price_change5 is not None and volume_ratio20 is not None:
        if price_change5 > 0 and volume_ratio20 >= 1.2:
            score += 8
            reasons.append("harga 5 hari naik dengan volume mendukung")
        elif price_change5 < -0.03 and volume_ratio20 >= 1.2:
            score -= 10
            reasons.append("harga turun saat volume besar")
    if close_position is not None:
        if close_position >= 0.65:
            score += 5
            reasons.append("close dekat high harian")
        elif close_position <= 0.25:
            score -= 5
            reasons.append("close dekat low harian")

    score = clamp(score)
    if score >= 72:
        label = "AKUMULASI / BANDAR VOLUME POSITIF"
    elif score >= 58:
        label = "VOLUME MENDUKUNG"
    elif score >= 45:
        label = "NETRAL"
    elif score >= 32:
        label = "DISTRIBUSI RINGAN / WASPADA"
    else:
        label = "DISTRIBUSI KUAT / HINDARI"
    return {
        "status": "OK",
        "score": round(score, 1),
        "label": label,
        "volume_ratio20": round(volume_ratio20, 2) if volume_ratio20 is not None else None,
        "mfi14": round(mfi14, 2) if mfi14 is not None else None,
        "obv_slope20": round(obv_slope, 4) if obv_slope is not None else None,
        "ad_slope20": round(ad_slope, 4) if ad_slope is not None else None,
        "price_change5": round(price_change5, 4) if price_change5 is not None else None,
        "close_position": round(close_position, 3) if close_position is not None else None,
        "reasons": reasons or ["belum ada sinyal volume dominan"],
        "source": "proxy bandarmology from volume spike, OBV, Accumulation/Distribution Line, MFI, and price-volume action",
    }


def entry_range(
    current: float,
    atr: float,
    support20: Optional[float],
    resistance20: Optional[float],
    sma20: Optional[float],
    bandar: Dict[str, Any],
) -> Dict[str, Any]:
    bandar_score = safe_float(bandar.get("score")) or 50
    pullback_low = current - 0.9 * atr
    pullback_high = current - 0.2 * atr
    if support20 is not None and support20 < current:
        pullback_low = max(pullback_low, support20 * 1.005)
    if sma20 is not None and sma20 < current:
        pullback_low = max(pullback_low, sma20 * 0.99)

    if bandar_score >= 68:
        preferred_low = max(current - 0.35 * atr, support20 * 1.005 if support20 and support20 < current else current - 0.35 * atr)
        preferred_high = current + 0.25 * atr
        strategy = "boleh entry bertahap dekat harga sekarang karena volume akumulasi mendukung"
    elif bandar_score <= 40:
        preferred_low = current - 1.2 * atr
        preferred_high = current - 0.55 * atr
        strategy = "tunggu pullback lebih dalam karena volume cenderung distribusi/lemah"
    else:
        preferred_low = pullback_low
        preferred_high = pullback_high
        strategy = "entry bertahap di area pullback, hindari mengejar harga"

    if resistance20 is not None and resistance20 > current:
        breakout = resistance20 * 1.005
    else:
        breakout = current + 0.8 * atr
    invalid_below = min(preferred_low - 0.45 * atr, current - 1.35 * atr)
    return {
        "preferred_entry_low": round_to_tick(preferred_low),
        "preferred_entry_high": round_to_tick(max(preferred_high, preferred_low + 0.15 * atr)),
        "breakout_entry": round_to_tick(breakout),
        "breakout_confirmation": "volume > 1.2x rata-rata 20 hari dan close bertahan di atas breakout",
        "invalid_if_below": round_to_tick(invalid_below),
        "strategy": strategy,
    }


def market_technical_levels(ticker: str, period: str = "6mo", interval: str = "1d") -> Dict[str, Any]:
    data, warnings = fetch_market_history(ticker, period, interval)
    if data is None:
        return {
            "status": "NO DATA",
            "warnings": warnings,
            "current_price": None,
            "take_profit": None,
            "stop_loss": None,
            "action": "NO CALL / DATA HARGA BELUM CUKUP",
            "confidence": 0,
        }
    close = data["Close"].dropna()
    high = data["High"].dropna()
    low = data["Low"].dropna()
    volume = data["Volume"].dropna() if "Volume" in data else None
    current = safe_float(close.iloc[-1])
    atr = calculate_atr(data)
    if current is None or atr is None or atr <= 0:
        return {
            "status": "NO DATA",
            "warnings": warnings + ["ATR/current price unavailable"],
            "current_price": current,
            "take_profit": None,
            "stop_loss": None,
            "action": "NO CALL / DATA HARGA BELUM CUKUP",
            "confidence": 10,
        }

    lookback20 = min(20, len(data))
    lookback60 = min(60, len(data))
    support20 = safe_float(low.tail(lookback20).min())
    support60 = safe_float(low.tail(lookback60).min())
    resistance20 = safe_float(high.tail(lookback20).max())
    resistance60 = safe_float(high.tail(lookback60).max())
    sma20 = safe_float(close.tail(min(20, len(close))).mean())
    sma50 = safe_float(close.tail(min(50, len(close))).mean())
    rsi = calculate_rsi(close)
    bandar = bandar_volume_signal(data, current)
    volume_ratio = None
    if volume is not None and len(volume) >= 20:
        avg_volume = safe_float(volume.tail(20).mean())
        last_volume = safe_float(volume.iloc[-1])
        volume_ratio = last_volume / avg_volume if avg_volume else None

    atr_pct = atr / current
    stop_risk_pct = max(0.035, min(0.12, atr_pct * 1.8))
    volatility_stop = current * (1 - stop_risk_pct)
    support_stop_candidates = [value * 0.99 for value in [support20, support60] if value is not None and value < current]
    support_stop = max(support_stop_candidates) if support_stop_candidates else volatility_stop
    stop_loss = max(volatility_stop, support_stop)
    if stop_loss >= current * 0.995:
        stop_loss = current * (1 - stop_risk_pct)

    risk = max(current - stop_loss, atr)
    resistance_candidates = [value * 1.005 for value in [resistance20, resistance60] if value is not None and value > current * 1.015]
    resistance_target = min(resistance_candidates) if resistance_candidates else None
    atr_target = current + max(2.0 * atr, 1.6 * risk)
    take_profit = max(resistance_target or 0, atr_target)
    if take_profit <= current:
        take_profit = current + max(2.2 * atr, 1.8 * risk)

    trend_points = 0
    reasons: List[str] = []
    bandar_score = safe_float(bandar.get("score"))
    if bandar_score is not None:
        if bandar_score >= 72:
            take_profit *= 1.025
            stop_loss = min(stop_loss, current * 0.955) if stop_loss >= current * 0.975 else stop_loss
            trend_points += 14
            reasons.append("Bandar Volume akumulasi kuat")
        elif bandar_score >= 58:
            take_profit *= 1.012
            trend_points += 7
            reasons.append("Bandar Volume mendukung")
        elif bandar_score <= 40:
            take_profit *= 0.965
            stop_loss = max(stop_loss, current * 0.965)
            trend_points -= 12
            reasons.append("Bandar Volume lemah/distribusi")
    if sma20 is not None and current > sma20:
        trend_points += 18
        reasons.append("harga di atas SMA20")
    else:
        reasons.append("harga belum di atas SMA20")
    if sma20 is not None and sma50 is not None and sma20 > sma50:
        trend_points += 18
        reasons.append("SMA20 di atas SMA50")
    if rsi is not None:
        if 45 <= rsi <= 68:
            trend_points += 18
            reasons.append(f"RSI sehat {rsi:.1f}")
        elif rsi > 78:
            trend_points -= 8
            reasons.append(f"RSI overbought {rsi:.1f}")
        elif rsi < 35:
            trend_points -= 4
            reasons.append(f"RSI lemah {rsi:.1f}")
    if resistance20 is not None and current < resistance20:
        trend_points += 10
        reasons.append("masih ada ruang ke resistance 20 hari")
    if volume_ratio is not None and volume_ratio >= 1.1:
        trend_points += 8
        reasons.append(f"volume di atas rata-rata {volume_ratio:.2f}x")

    risk_reward = (take_profit - current) / max(current - stop_loss, 1e-9)
    if risk_reward >= 1.6:
        trend_points += 18
        reasons.append(f"risk/reward {risk_reward:.2f}")
    else:
        reasons.append(f"risk/reward kurang kuat {risk_reward:.2f}")

    technical_score = clamp(45 + trend_points)
    confidence = clamp(35 + min(35, len(data) / 2) + (10 if atr is not None else 0) + (10 if rsi is not None else 0), 0, 85)
    if technical_score >= 72 and risk_reward >= 1.5:
        action = "BUY CANDIDATE TEKNIKAL"
    elif technical_score >= 58:
        action = "WATCHLIST TEKNIKAL"
    elif technical_score >= 45:
        action = "WAIT TEKNIKAL"
    else:
        action = "SELL / AVOID TEKNIKAL"
    entry = entry_range(current, atr, support20, resistance20, sma20, bandar)

    return {
        "status": "OK",
        "ticker": normalize_ticker(ticker),
        "generated_at": utc_now_text(),
        "current_price": round_to_tick(current),
        "raw_current_price": current,
        "take_profit": round_to_tick(take_profit),
        "stop_loss": round_to_tick(stop_loss),
        "upside_to_tp": pct((take_profit - current) / current),
        "risk_to_sl": pct((current - stop_loss) / current),
        "risk_reward": round(risk_reward, 2),
        "action": action,
        "technical_score": round(technical_score, 1),
        "confidence": round(confidence, 1),
        "atr14": round(atr, 4),
        "atr_pct": pct(atr_pct),
        "support20": round_to_tick(support20),
        "support60": round_to_tick(support60),
        "resistance20": round_to_tick(resistance20),
        "resistance60": round_to_tick(resistance60),
        "sma20": round_to_tick(sma20),
        "sma50": round_to_tick(sma50),
        "rsi14": round(rsi, 2) if rsi is not None else None,
        "volume_ratio20": round(volume_ratio, 2) if volume_ratio is not None else None,
        "bandar_volume": bandar,
        "entry_range": entry,
        "reasons": reasons,
        "warnings": warnings,
        "source": "yfinance price history; local ATR/support/resistance/MA/RSI rule engine",
    }


def score_from_news(summary: Dict[str, Any]) -> Tuple[Optional[float], List[str]]:
    if not summary:
        return None, []
    sentiment = safe_float(summary.get("avg_sentiment"))
    articles = safe_float(summary.get("article_count")) or 0
    risks = safe_float(summary.get("risk_article_count")) or 0
    if sentiment is None and articles <= 0:
        return None, []
    score = clamp(((sentiment or 0.0) + 1.0) * 50.0 - min(12.0, risks * 2.0))
    reasons = [f"news sentiment {sentiment or 0:.2f} dari {int(articles)} artikel"]
    if risks:
        reasons.append(f"{int(risks)} artikel punya risk flag")
    return score, reasons


def score_from_fundamental(row: Dict[str, Any]) -> Tuple[Optional[float], List[str]]:
    if not row:
        return None, []
    score = safe_float(row.get("research_score_with_news")) or safe_float(row.get("fundamental_score"))
    reasons = []
    if score is not None:
        reasons.append(f"fundamental score {score:.1f}")
    if row.get("research_bucket"):
        reasons.append(str(row.get("research_bucket")))
    return score, reasons


def weighted(items: Iterable[Tuple[Optional[float], float]]) -> Tuple[Optional[float], float]:
    total = 0.0
    available = 0.0
    value = 0.0
    for score, weight in items:
        total += weight
        if score is None:
            continue
        available += weight
        value += score * weight
    if available == 0:
        return None, 0.0
    return value / available, available / total if total else 0.0


def rapidapi_level_candidates(rapid_technical: Dict[str, Any], current: Optional[float]) -> Dict[str, Optional[float]]:
    if current is None:
        return {"support": None, "resistance": None, "pivot": None, "atr": None, "bb_lower": None, "bb_upper": None}
    data = rapid_technical.get("data", {}) if isinstance(rapid_technical.get("data"), dict) else {}
    indicators = data.get("indicators", {}) if isinstance(data.get("indicators"), dict) else {}
    sr = data.get("supportResistance", {}) if isinstance(data.get("supportResistance"), dict) else {}
    supports = []
    for row in sr.get("supports", []) if isinstance(sr.get("supports"), list) else []:
        level = safe_float(row.get("level")) if isinstance(row, dict) else safe_float(row)
        if level is not None and level < current:
            supports.append(level)
    resistances = []
    for row in sr.get("resistances", []) if isinstance(sr.get("resistances"), list) else []:
        level = safe_float(row.get("level")) if isinstance(row, dict) else safe_float(row)
        if level is not None and level > current:
            resistances.append(level)
    bb = indicators.get("bollingerBands", {}) if isinstance(indicators.get("bollingerBands"), dict) else {}
    atr = indicators.get("atr", {}) if isinstance(indicators.get("atr"), dict) else {}
    return {
        "support": max(supports) if supports else None,
        "resistance": min(resistances) if resistances else None,
        "pivot": safe_float(sr.get("pivotPoint")),
        "atr": safe_float(atr.get("value")),
        "bb_lower": safe_float(bb.get("lower")),
        "bb_upper": safe_float(bb.get("upper")),
    }


def composite_levels(ticker: str, technical: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    current = safe_float(technical.get("raw_current_price") or technical.get("current_price"))
    technical_tp = safe_float(technical.get("take_profit"))
    technical_sl = safe_float(technical.get("stop_loss"))
    tech_score = safe_float(technical.get("technical_score"))
    bandar = technical.get("bandar_volume", {}) or {}
    bandar_score = safe_float(bandar.get("score"))
    broker_summary = context.get("broker_summary", {}) if isinstance(context.get("broker_summary"), dict) else {}
    broker_score = safe_float(broker_summary.get("score"))
    effective_bandar_score = broker_score if broker_score is not None else bandar_score
    effective_bandar_label = broker_summary.get("label") if broker_score is not None else bandar.get("label")
    technical_entry = technical.get("entry_range", {}) or {}
    idx = context.get("idx_official", {})
    idx_trade = idx.get("trade_view", {}) if isinstance(idx, dict) else {}
    idx_score = safe_float((idx.get("financial_score") or {}).get("score")) if isinstance(idx, dict) else None
    idx_conf = safe_float((idx.get("financial_score") or {}).get("confidence")) if isinstance(idx, dict) else None
    idx_tp = safe_float(idx_trade.get("illustrative_target_price"))
    idx_sl = safe_float(idx_trade.get("illustrative_stop_loss"))
    fundamental_score, fundamental_reasons = score_from_fundamental(context.get("fundamental", {}))
    news_score, news_reasons = score_from_news(context.get("news", {}).get("summary", {}))
    document = context.get("documents", {})
    document_risks = [flag for flag in str(document.get("risk_flags", "")).split(";") if flag]
    recommendation = context.get("recommendation", {})
    reco_score = safe_float(recommendation.get("combined_score"))
    rapid_technical = context.get("rapidapi_technical", {}) if isinstance(context.get("rapidapi_technical"), dict) else {}
    rapid_sentiment = context.get("rapidapi_sentiment", {}) if isinstance(context.get("rapidapi_sentiment"), dict) else {}
    rapid_technical_score = safe_float(rapid_technical.get("score"))
    rapid_sentiment_score = safe_float(rapid_sentiment.get("score"))
    rapid_levels = rapidapi_level_candidates(rapid_technical, current)
    broker_warning = str(broker_summary.get("warning") or "").strip()
    bandar_effective = dict(bandar)
    bandar_effective["effective_score"] = round(effective_bandar_score, 1) if effective_bandar_score is not None else None
    bandar_effective["effective_label"] = effective_bandar_label
    bandar_effective["effective_source"] = (
        str(broker_summary.get("source") or broker_summary.get("provider") or "broker summary provider/upload")
        if broker_score is not None
        else "price-volume proxy"
    )

    combined_score, availability = weighted(
        [
            (tech_score, 0.35),
            (effective_bandar_score, 0.12),
            (idx_score, 0.30),
            (fundamental_score, 0.18),
            (rapid_technical_score, 0.10),
            (rapid_sentiment_score, 0.08),
            (news_score, 0.06),
            (reco_score, 0.04),
        ]
    )
    if combined_score is None:
        combined_score = tech_score
    if document_risks:
        combined_score = clamp((combined_score or 50) - min(10, len(document_risks) * 2.5))

    target_points: List[Tuple[float, float]] = []
    stop_points: List[Tuple[float, float]] = []
    if technical_tp is not None:
        target_points.append((technical_tp, 0.48))
    if idx_tp is not None:
        target_points.append((idx_tp, 0.38))
    if safe_float(recommendation.get("take_profit")) is not None:
        target_points.append((safe_float(recommendation.get("take_profit")) or 0, 0.14))
    if rapid_levels.get("resistance") is not None:
        target_points.append((rapid_levels["resistance"] or 0, 0.22))
    elif rapid_levels.get("bb_upper") is not None and current is not None and (rapid_levels["bb_upper"] or 0) > current:
        target_points.append((rapid_levels["bb_upper"] or 0, 0.12))
    elif rapid_levels.get("atr") is not None and current is not None:
        target_points.append((current + 1.8 * (rapid_levels["atr"] or 0), 0.10))
    if technical_sl is not None:
        stop_points.append((technical_sl, 0.55))
    if idx_sl is not None:
        stop_points.append((idx_sl, 0.35))
    if safe_float(recommendation.get("stop_loss")) is not None:
        stop_points.append((safe_float(recommendation.get("stop_loss")) or 0, 0.10))
    if rapid_levels.get("support") is not None:
        stop_points.append(((rapid_levels["support"] or 0) * 0.985, 0.22))
    elif rapid_levels.get("bb_lower") is not None and current is not None and (rapid_levels["bb_lower"] or 0) < current:
        stop_points.append((rapid_levels["bb_lower"] or 0, 0.12))
    elif rapid_levels.get("atr") is not None and current is not None:
        stop_points.append((current - 1.15 * (rapid_levels["atr"] or 0), 0.10))

    if not target_points or not stop_points or current is None:
        no_data_reasons = ["butuh minimal data harga teknikal; IDX/fundamental/news memperkuat jika tersedia"]
        if broker_warning:
            no_data_reasons.append(f"Broker Summary API: {broker_warning}")
        return {
            "status": "NO DATA",
            "take_profit": None,
            "stop_loss": None,
            "action": "NO CALL / DATA KOMPOSIT BELUM CUKUP",
            "confidence": 0,
            "score": combined_score,
            "bandar_volume": bandar_effective,
            "broker_summary": broker_summary,
            "reasons": no_data_reasons,
            "bandarmology_alternatives": broker_summary.get("alternatives", []),
        }

    tp = sum(value * weight for value, weight in target_points) / sum(weight for _, weight in target_points)
    sl = sum(value * weight for value, weight in stop_points) / sum(weight for _, weight in stop_points)

    sentiment_adjust = 0.0
    if news_score is not None:
        sentiment_adjust += max(-0.04, min(0.04, (news_score - 50) / 1000))
    if fundamental_score is not None:
        sentiment_adjust += max(-0.05, min(0.06, (fundamental_score - 60) / 700))
    if idx_score is not None:
        sentiment_adjust += max(-0.04, min(0.05, (idx_score - 60) / 800))
    if effective_bandar_score is not None:
        sentiment_adjust += max(-0.045, min(0.055, (effective_bandar_score - 55) / 700))
    if rapid_technical_score is not None:
        sentiment_adjust += max(-0.025, min(0.03, (rapid_technical_score - 55) / 900))
    if rapid_sentiment_score is not None:
        sentiment_adjust += max(-0.025, min(0.03, (rapid_sentiment_score - 55) / 900))
    tp = tp * (1 + sentiment_adjust)

    if document_risks or (news_score is not None and news_score < 42) or (effective_bandar_score is not None and effective_bandar_score < 42) or (rapid_sentiment_score is not None and rapid_sentiment_score < 42):
        sl = max(sl, current * 0.965)
    if combined_score is not None and combined_score >= 75:
        sl = min(sl, current * 0.94) if sl >= current * 0.975 else sl
    if sl >= current * 0.995:
        sl = current * 0.955
    if tp <= current:
        tp = max(technical_tp or current * 1.08, current * 1.06)

    entry_low = safe_float(technical_entry.get("preferred_entry_low"))
    entry_high = safe_float(technical_entry.get("preferred_entry_high"))
    if entry_low is None or entry_high is None:
        entry_low = current * 0.975
        entry_high = current * 1.005
    rapid_entry_zone = broker_summary.get("entry_zone", {}) if isinstance(broker_summary.get("entry_zone"), dict) else {}
    rapid_ideal_entry = safe_float(rapid_entry_zone.get("ideal_price"))
    rapid_max_entry = safe_float(rapid_entry_zone.get("max_price"))
    if effective_bandar_score is not None and effective_bandar_score >= 55 and rapid_ideal_entry is not None and rapid_max_entry is not None:
        entry_low = max(min(entry_low, rapid_ideal_entry), min(sl * 1.01, rapid_ideal_entry))
        entry_high = min(max(entry_high, rapid_ideal_entry), rapid_max_entry, tp * 0.92)
    if rapid_levels.get("support") is not None and rapid_levels.get("pivot") is not None:
        support_entry = (rapid_levels["support"] or entry_low) * 1.005
        pivot_entry = rapid_levels["pivot"] or entry_high
        if support_entry < current * 1.03:
            entry_low = min(entry_low, support_entry)
            entry_high = min(max(entry_high, support_entry), max(pivot_entry, support_entry))
    if combined_score is not None and combined_score >= 75 and effective_bandar_score is not None and effective_bandar_score >= 62:
        entry_low = max(entry_low, current - 0.45 * max(current - sl, 1))
        entry_high = min(max(entry_high, current + 0.20 * max(current - sl, 1)), tp * 0.92)
        entry_strategy = "entry bertahap agresif-terukur karena komposit dan Bandar Volume/Broker Summary mendukung"
    elif effective_bandar_score is not None and effective_bandar_score < 42:
        entry_low = min(entry_low, current - 0.75 * max(current - sl, 1))
        entry_high = min(entry_high, current - 0.30 * max(current - sl, 1))
        entry_strategy = "tunggu diskon/pullback; Bandar Volume/Broker Summary belum mendukung"
    else:
        entry_strategy = str(technical_entry.get("strategy") or "entry bertahap di area pullback")
    entry_low = max(entry_low, sl * 1.005)
    entry_high = max(entry_high, entry_low)

    rr = (tp - current) / max(current - sl, 1e-9)
    confidence = clamp(
        25
        + availability * 45
        + (10 if idx_tp is not None else 0)
        + (8 if fundamental_score is not None else 0)
        + (7 if news_score is not None else 0),
        0,
        88,
    )
    confidence = clamp(
        confidence
        + (6 if rapid_technical_score is not None else 0)
        + (5 if rapid_sentiment_score is not None else 0),
        0,
        92,
    )

    reasons = []
    reasons.extend(technical.get("reasons", [])[:4])
    if broker_score is not None:
        reasons.append(f"Broker Summary {broker_score:.1f}: {broker_summary.get('label')}")
        if broker_summary.get("provider") == "rapidapi_idx":
            reasons.append(
                f"RapidAPI rec {broker_summary.get('recommendation') or '-'}, risk {broker_summary.get('risk_level') or '-'}, confidence {broker_summary.get('provider_confidence') or '-'}%"
            )
        if broker_summary.get("provider_comparison"):
            comparison = broker_summary.get("provider_comparison") or []
            reasons.append(
                "provider comparison: "
                + ", ".join(
                    f"{item.get('provider')} score {item.get('score')} quality {item.get('quality')}"
                    for item in comparison[:3]
                )
            )
        if broker_summary.get("source_file"):
            reasons.append(f"broker source: {Path(str(broker_summary.get('source_file'))).name}")
    elif broker_warning:
        reasons.append(f"Broker Summary API: {broker_warning}")
    elif bandar_score is not None:
        reasons.append(f"Bandar Volume {bandar_score:.1f}: {bandar.get('label')}")
    if idx_score is not None:
        reasons.append(f"IDX official score {idx_score:.1f}, confidence {idx_conf or 0:.1f}%")
    reasons.extend(fundamental_reasons[:2])
    reasons.extend(news_reasons[:2])
    if document_risks:
        reasons.append("document risks: " + ", ".join(document_risks[:4]))
    if rapid_technical_score is not None:
        reasons.append(f"RapidAPI technical score {rapid_technical_score:.1f}: " + "; ".join((rapid_technical.get("reasons") or [])[:3]))
    if rapid_sentiment_score is not None:
        reasons.append(f"RapidAPI sentiment score {rapid_sentiment_score:.1f}: " + "; ".join((rapid_sentiment.get("reasons") or [])[:3]))
    reasons.append(f"risk/reward komposit {rr:.2f}")

    if combined_score >= 76 and rr >= 1.5 and confidence >= 58:
        action = "BUY CANDIDATE KOMPOSIT"
    elif combined_score >= 62 and rr >= 1.2:
        action = "WATCHLIST / BUY JIKA KONFIRMASI"
    elif combined_score >= 50:
        action = "WAIT / BUTUH KONFIRMASI"
    else:
        action = "SELL / AVOID KOMPOSIT"

    sources = [
        "market technical: yfinance price history + ATR/support/resistance/MA/RSI",
    ]
    if idx_score is not None:
        sources.append("IDX official report analysis")
    if fundamental_score is not None:
        sources.append("latest fundamental score")
    if news_score is not None:
        sources.append("latest Google News RSS sentiment")
    if rapid_technical_score is not None:
        sources.append("RapidAPI IDX technical indicators/support-resistance")
    if rapid_sentiment_score is not None:
        sources.append("RapidAPI IDX retail/bandar sentiment")
    if document:
        sources.append("uploaded document signal")
    if broker_score is not None:
        sources.append(str(broker_summary.get("source") or "broker summary provider/upload"))
    elif broker_summary.get("alternatives"):
        sources.append("broker summary source options available, but no live broker data was usable")

    return {
        "status": "OK",
        "ticker": normalize_ticker(ticker),
        "generated_at": utc_now_text(),
        "current_price": round_to_tick(current),
        "take_profit": round_to_tick(tp),
        "stop_loss": round_to_tick(sl),
        "upside_to_tp": pct((tp - current) / current),
        "risk_to_sl": pct((current - sl) / current),
        "risk_reward": round(rr, 2),
        "action": action,
        "score": round(combined_score, 1) if combined_score is not None else None,
        "confidence": round(confidence, 1),
        "target_blend": [{"value": round_to_tick(value), "weight": weight} for value, weight in target_points],
        "stop_blend": [{"value": round_to_tick(value), "weight": weight} for value, weight in stop_points],
        "entry_range": {
            "preferred_entry_low": round_to_tick(entry_low),
            "preferred_entry_high": round_to_tick(max(entry_high, entry_low)),
            "breakout_entry": technical_entry.get("breakout_entry"),
            "breakout_confirmation": technical_entry.get("breakout_confirmation"),
            "invalid_if_below": round_to_tick(min(safe_float(technical_entry.get("invalid_if_below")) or sl, sl, entry_low * 0.985)),
            "strategy": entry_strategy,
        },
        "bandar_volume": bandar_effective,
        "broker_summary": broker_summary,
        "bandarmology_alternatives": broker_summary.get("alternatives", []),
        "adjustment": round(sentiment_adjust, 4),
        "reasons": reasons,
        "sources": sources,
        "model_note": "TP/SL komposit berbasis data yang tersedia; bukan jaminan akurasi 100% atau instruksi transaksi.",
    }


def build_levels(
    ticker: str,
    watchlist: Path,
    news_days: int = 3,
    news_records: int = 3,
    cache_dir: Optional[Path] = None,
    cache_ttl_seconds: int = 600,
) -> Dict[str, Any]:
    ticker = normalize_ticker(ticker)
    cache_path = None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"{ticker_base(ticker)}_levels.json"
        if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < cache_ttl_seconds:
            cached = read_json(cache_path)
            if cached:
                cached["cache"] = {"hit": True, "path": str(cache_path)}
                return cached

    profile = profile_for_ticker(ticker, watchlist)
    roots = [ROOT / "outputs", ROOT / "outputs" / "hosted"]
    broker_roots = [ROOT / "uploads", ROOT / "outputs" / "hosted"]
    broker_cache = (cache_dir / "broker_summary") if cache_dir else None
    rapidapi_cache = (cache_dir / "rapidapi") if cache_dir else None
    technical = market_technical_levels(ticker)
    context = {
        "profile": profile,
        "fundamental": latest_fundamental(ticker, roots),
        "idx_official": latest_idx_official(ticker, roots),
        "documents": latest_document_signal(ticker, roots),
        "recommendation": latest_recommendation(ticker, roots),
        "broker_summary": broker_summary_signal(ticker, broker_roots, broker_cache),
        "rapidapi_technical": fetch_rapidapi_technical_analysis(ticker, rapidapi_cache),
        "rapidapi_sentiment": fetch_rapidapi_sentiment_analysis(ticker, rapidapi_cache),
        "news": fetch_recent_news(profile, days=news_days, max_records=news_records),
    }
    composite = composite_levels(ticker, technical, context)
    package = {
        "ticker": ticker,
        "generated_at": utc_now_text(),
        "market_technical": technical,
        "composite": composite,
        "context": context,
        "source_policy": {
            "tradingview_limit": "TradingView widgets are iframes and do not expose realtime indicator values to this app.",
            "market_data": "The automatic calculation uses yfinance market history next to the TradingView Suite.",
            "rapidapi_idx": "RapidAPI IDX enriches TP/SL with bandar accumulation/distribution, technical indicators/support-resistance/ATR, and retail-bandar sentiment when enabled.",
            "bandarmology": "RapidAPI IDX bandar accumulation/distribution is tried first and rotates RAPIDAPI_IDX_KEYS when a key fails or is rate-limited; Index Alpha rotates INDEXALPHA_API_KEYS/INDEXALPHA_API_TOKEN as comparison/fallback; a custom JSON API can be set with BANDAR_CUSTOM_API_URL; uploaded Stockbit/Ajaib/IDX-style broker summary files are parsed as fallback; otherwise Bandar Volume uses a price-volume proxy.",
            "disclaimer": "Research support only; TP/SL cannot be guaranteed 100% accurate.",
        },
        "cache": {"hit": False, "path": str(cache_path) if cache_path else ""},
    }
    if cache_path:
        cache_path.write_text(json.dumps(package, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return package


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hitung TP/SL teknikal dan komposit untuk satu saham.")
    parser.add_argument("--ticker", required=True, help="Ticker, contoh BBCA atau BBCA.JK.")
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST, help="Watchlist CSV.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT, help="Folder output.")
    parser.add_argument("--news-days", type=int, default=3, help="Hari berita ke belakang.")
    parser.add_argument("--news-records", type=int, default=3, help="Max artikel berita.")
    parser.add_argument("--no-cache", action="store_true", help="Jangan pakai cache.")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    package = build_levels(
        args.ticker,
        watchlist=args.watchlist,
        news_days=args.news_days,
        news_records=args.news_records,
        cache_dir=None if args.no_cache else args.output_dir / "cache",
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / f"{ticker_base(args.ticker)}_levels_latest.json"
    output_path.write_text(json.dumps(package, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(package, ensure_ascii=False, indent=2, default=str))
    print(f"Output: {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
