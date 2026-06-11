#!/usr/bin/env python
"""
Financial statement analysis for public equities.

Fetches annual or quarterly financial statements with yfinance, normalizes key
line items, computes ratios and trend flags, then writes CSV/XLSX/JSON/HTML
outputs for review.

This is a research support tool, not investment advice.
"""

import argparse
import html
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from analyze_stocks import (
    BALANCE_ROWS,
    CASHFLOW_ROWS,
    INCOME_ROWS,
    compact_number,
    normalize_yield,
    pct,
    row_series,
    safe_div,
    safe_float,
    safe_growth,
    score_range,
    value_from_info,
    weighted_average,
)


Metric = Optional[float]


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fetch_frames(ticker: str, period: str) -> Tuple[Dict[str, Any], Any, Any, Any, List[str]]:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is not installed. Run: pip install -r requirements.txt") from exc

    stock = yf.Ticker(ticker)
    warnings: List[str] = []

    try:
        info = stock.get_info() or {}
    except Exception as exc:
        info = {}
        warnings.append(f"info fetch failed: {exc}")

    frame_attrs = {
        "annual": ("financials", "balance_sheet", "cashflow"),
        "quarterly": ("quarterly_financials", "quarterly_balance_sheet", "quarterly_cashflow"),
    }[period]

    frames = []
    for attr in frame_attrs:
        try:
            frame = getattr(stock, attr)
        except Exception as exc:
            warnings.append(f"{attr} fetch failed: {exc}")
            frame = None
        frames.append(frame)
    return info, frames[0], frames[1], frames[2], warnings


def period_columns(*frames: Any, limit: int) -> List[Any]:
    columns: List[Any] = []
    seen = set()
    for frame in frames:
        if frame is None or getattr(frame, "empty", True):
            continue
        for col in list(frame.columns):
            key = str(col)
            if key not in seen:
                seen.add(key)
                columns.append(col)
    try:
        columns = sorted(columns, reverse=True)
    except TypeError:
        pass
    return columns[:limit]


def period_label(col: Any) -> str:
    if hasattr(col, "strftime"):
        return col.strftime("%Y-%m-%d")
    return str(col)


def value_at(df: Any, aliases: Iterable[str], col: Any) -> Metric:
    series = row_series(df, aliases)
    if series is None:
        return None
    try:
        return safe_float(series.get(col))
    except Exception:
        return None


def previous_column(columns: List[Any], index: int) -> Optional[Any]:
    next_index = index + 1
    if next_index >= len(columns):
        return None
    return columns[next_index]


def coalesce(*values: Any) -> Metric:
    for value in values:
        parsed = safe_float(value)
        if parsed is not None:
            return parsed
    return None


def calculate_period_rows(
    ticker: str,
    info: Dict[str, Any],
    income: Any,
    balance: Any,
    cashflow: Any,
    years: int,
) -> List[Dict[str, Any]]:
    columns = period_columns(income, balance, cashflow, limit=years)
    market_cap = value_from_info(info, "marketCap")
    rows: List[Dict[str, Any]] = []

    for index, col in enumerate(columns):
        prev = previous_column(columns, index)
        revenue = value_at(income, INCOME_ROWS["revenue"], col)
        prior_revenue = value_at(income, INCOME_ROWS["revenue"], prev) if prev is not None else None
        gross_profit = value_at(income, INCOME_ROWS["gross_profit"], col)
        operating_income = value_at(income, INCOME_ROWS["operating_income"], col)
        net_income = value_at(income, INCOME_ROWS["net_income"], col)
        prior_net_income = value_at(income, INCOME_ROWS["net_income"], prev) if prev is not None else None
        diluted_eps = value_at(income, INCOME_ROWS["diluted_eps"], col)
        prior_eps = value_at(income, INCOME_ROWS["diluted_eps"], prev) if prev is not None else None

        assets = value_at(balance, BALANCE_ROWS["assets"], col)
        prior_assets = value_at(balance, BALANCE_ROWS["assets"], prev) if prev is not None else None
        equity = value_at(balance, BALANCE_ROWS["equity"], col)
        prior_equity = value_at(balance, BALANCE_ROWS["equity"], prev) if prev is not None else None
        liabilities = value_at(balance, BALANCE_ROWS["liabilities"], col)
        debt = value_at(balance, BALANCE_ROWS["debt"], col)
        cash = value_at(balance, BALANCE_ROWS["cash"], col)
        current_assets = value_at(balance, BALANCE_ROWS["current_assets"], col)
        current_liabilities = value_at(balance, BALANCE_ROWS["current_liabilities"], col)

        operating_cash_flow = value_at(cashflow, CASHFLOW_ROWS["operating_cash_flow"], col)
        capex = value_at(cashflow, CASHFLOW_ROWS["capex"], col)
        reported_fcf = value_at(cashflow, CASHFLOW_ROWS["free_cash_flow"], col)
        free_cash_flow = coalesce(
            reported_fcf,
            (operating_cash_flow or 0) + (capex or 0) if operating_cash_flow is not None or capex is not None else None,
        )
        dividends_paid = value_at(cashflow, CASHFLOW_ROWS["dividends_paid"], col)

        avg_assets = (assets + prior_assets) / 2 if assets is not None and prior_assets is not None else assets
        avg_equity = (equity + prior_equity) / 2 if equity is not None and prior_equity is not None else equity

        rows.append(
            {
                "ticker": ticker,
                "period": period_label(col),
                "currency": info.get("financialCurrency") or info.get("currency") or "",
                "revenue": revenue,
                "revenue_growth": safe_growth(revenue, prior_revenue),
                "gross_profit": gross_profit,
                "gross_margin": safe_div(gross_profit, revenue),
                "operating_income": operating_income,
                "operating_margin": safe_div(operating_income, revenue),
                "net_income": net_income,
                "net_income_growth": safe_growth(net_income, prior_net_income),
                "net_margin": safe_div(net_income, revenue),
                "diluted_eps": diluted_eps,
                "eps_growth": safe_growth(diluted_eps, prior_eps),
                "total_assets": assets,
                "total_liabilities": liabilities,
                "equity": equity,
                "debt": debt,
                "cash": cash,
                "net_debt": (debt or 0) - (cash or 0) if debt is not None or cash is not None else None,
                "current_assets": current_assets,
                "current_liabilities": current_liabilities,
                "current_ratio": safe_div(current_assets, current_liabilities),
                "debt_to_equity": safe_div(debt if debt is not None else liabilities, equity),
                "cash_to_debt": safe_div(cash, debt),
                "roe": safe_div(net_income, avg_equity),
                "roa": safe_div(net_income, avg_assets),
                "operating_cash_flow": operating_cash_flow,
                "capex": capex,
                "free_cash_flow": free_cash_flow,
                "fcf_margin": safe_div(free_cash_flow, revenue),
                "ocf_to_net_income": safe_div(operating_cash_flow, net_income),
                "dividends_paid": dividends_paid,
                "dividend_coverage_by_fcf": safe_div(free_cash_flow, abs(dividends_paid) if dividends_paid is not None else None),
                "fcf_yield_latest_market_cap": safe_div(free_cash_flow, market_cap),
            }
        )
    return rows


def data_confidence(row: Dict[str, Any]) -> float:
    required = [
        "revenue",
        "net_income",
        "total_assets",
        "equity",
        "operating_cash_flow",
        "free_cash_flow",
        "roe",
        "debt_to_equity",
    ]
    present = sum(1 for key in required if row.get(key) is not None)
    return round(present / len(required) * 100, 1)


def score_statement_quality(latest: Dict[str, Any]) -> Dict[str, Any]:
    profitability, profitability_conf = weighted_average(
        [
            (score_range(latest.get("roe"), 0.00, 0.20), 0.30),
            (score_range(latest.get("roa"), 0.00, 0.10), 0.15),
            (score_range(latest.get("gross_margin"), 0.00, 0.45), 0.15),
            (score_range(latest.get("operating_margin"), 0.00, 0.25), 0.20),
            (score_range(latest.get("net_margin"), 0.00, 0.20), 0.20),
        ]
    )
    growth, growth_conf = weighted_average(
        [
            (score_range(latest.get("revenue_growth"), -0.10, 0.20), 0.50),
            (score_range(latest.get("net_income_growth"), -0.20, 0.30), 0.35),
            (score_range(latest.get("eps_growth"), -0.20, 0.30), 0.15),
        ]
    )
    cash_quality, cash_conf = weighted_average(
        [
            (score_range(latest.get("fcf_margin"), -0.05, 0.15), 0.45),
            (score_range(latest.get("ocf_to_net_income"), 0.50, 1.50), 0.35),
            (score_range(latest.get("dividend_coverage_by_fcf"), 0.80, 2.50), 0.20),
        ]
    )
    balance, balance_conf = weighted_average(
        [
            (score_range(latest.get("debt_to_equity"), 0.50, 3.00, invert=True), 0.45),
            (score_range(latest.get("cash_to_debt"), 0.00, 1.00), 0.20),
            (score_range(latest.get("current_ratio"), 0.80, 2.00), 0.25),
            (score_range(latest.get("fcf_margin"), -0.05, 0.10), 0.10),
        ]
    )
    final, final_conf = weighted_average(
        [
            (profitability, 0.35),
            (growth, 0.20),
            (cash_quality, 0.25),
            (balance, 0.20),
        ]
    )
    return {
        "statement_quality_score": round(final, 1) if final is not None else None,
        "profitability_score": round(profitability, 1) if profitability is not None else None,
        "growth_score": round(growth, 1) if growth is not None else None,
        "cash_quality_score": round(cash_quality, 1) if cash_quality is not None else None,
        "balance_sheet_score": round(balance, 1) if balance is not None else None,
        "statement_score_confidence": round(final_conf * 100, 1),
        "profitability_confidence": round(profitability_conf * 100, 1),
        "growth_confidence": round(growth_conf * 100, 1),
        "cash_quality_confidence": round(cash_conf * 100, 1),
        "balance_confidence": round(balance_conf * 100, 1),
    }


def direction(latest: Metric, older: Metric, threshold: float = 0.01) -> str:
    if latest is None or older is None:
        return "not enough data"
    delta = latest - older
    if delta > threshold:
        return "improving"
    if delta < -threshold:
        return "deteriorating"
    return "stable"


def fmt_metric(value: Metric, percent: bool = False) -> str:
    if value is None:
        return "n/a"
    if percent:
        return pct(value) or "n/a"
    return compact_number(value) or "n/a"


def build_findings(rows: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    if not rows:
        return [], ["No financial statement data returned by provider."]

    latest = rows[0]
    older = rows[-1] if len(rows) > 1 else {}
    findings = []
    flags = []

    findings.append(
        f"Latest period {latest.get('period')}: revenue {fmt_metric(latest.get('revenue'))}, "
        f"net income {fmt_metric(latest.get('net_income'))}, FCF {fmt_metric(latest.get('free_cash_flow'))}."
    )
    if latest.get("revenue_growth") is not None:
        findings.append(f"Revenue growth versus prior period: {fmt_metric(latest.get('revenue_growth'), percent=True)}.")
    if latest.get("net_margin") is not None:
        margin_dir = direction(latest.get("net_margin"), older.get("net_margin"))
        findings.append(f"Net margin is {fmt_metric(latest.get('net_margin'), percent=True)} and is {margin_dir} versus oldest available period.")
    if latest.get("roe") is not None:
        findings.append(f"ROE is {fmt_metric(latest.get('roe'), percent=True)}; ROA is {fmt_metric(latest.get('roa'), percent=True)}.")
    if latest.get("debt_to_equity") is not None:
        findings.append(f"Debt/equity is {latest.get('debt_to_equity'):.2f}; cash/debt is {fmt_metric(latest.get('cash_to_debt'))}.")
    if latest.get("ocf_to_net_income") is not None:
        findings.append(f"Operating cash flow to net income is {latest.get('ocf_to_net_income'):.2f}.")

    if latest.get("revenue") is None:
        flags.append("Revenue missing from provider statement data.")
    if latest.get("net_income") is None:
        flags.append("Net income missing from provider statement data.")
    if latest.get("revenue_growth") is not None and latest["revenue_growth"] < 0:
        flags.append("Latest revenue declined versus prior period.")
    if latest.get("net_margin") is not None and latest["net_margin"] < 0:
        flags.append("Latest net margin is negative.")
    if latest.get("free_cash_flow") is not None and latest["free_cash_flow"] < 0:
        flags.append("Latest free cash flow is negative.")
    if latest.get("ocf_to_net_income") is not None and latest["ocf_to_net_income"] < 0.7:
        flags.append("Cash conversion is weak: OCF/net income below 0.7.")
    if latest.get("debt_to_equity") is not None and latest["debt_to_equity"] > 3:
        flags.append("Debt/equity is high; check sector context and maturity profile.")
    if latest.get("current_ratio") is not None and latest["current_ratio"] < 1:
        flags.append("Current ratio is below 1; liquidity needs review. For banks/financials this ratio may be less meaningful.")
    if data_confidence(latest) < 60:
        flags.append("Provider data is incomplete; verify with IDX filings or company annual report.")

    if not flags:
        flags.append("No major automated red flags from available statement data.")
    return findings, flags


def build_summary(ticker: str, info: Dict[str, Any], rows: List[Dict[str, Any]], warnings: List[str]) -> Dict[str, Any]:
    latest = rows[0] if rows else {}
    findings, flags = build_findings(rows)
    score = score_statement_quality(latest) if latest else {}
    return {
        "ticker": ticker,
        "company": info.get("shortName") or info.get("longName") or ticker,
        "sector": info.get("sector") or "",
        "industry": info.get("industry") or "",
        "currency": info.get("financialCurrency") or info.get("currency") or "",
        "latest_period": latest.get("period", ""),
        "latest_price": value_from_info(info, "currentPrice", "regularMarketPrice", "previousClose"),
        "market_cap": value_from_info(info, "marketCap"),
        "dividend_yield": normalize_yield(value_from_info(info, "dividendYield", "trailingAnnualDividendYield")),
        "data_confidence": data_confidence(latest) if latest else 0.0,
        **score,
        "findings": findings,
        "risk_flags": flags,
        "warnings": warnings,
        "retrieved_at": utc_now_text(),
    }


def analyze_financial_statements(ticker: str, period: str = "annual", years: int = 5) -> Dict[str, Any]:
    if period not in {"annual", "quarterly"}:
        raise ValueError("period must be annual or quarterly")
    if years < 1:
        raise ValueError("years must be >= 1")
    info, income, balance, cashflow, warnings = fetch_frames(ticker, period)
    rows = calculate_period_rows(ticker, info, income, balance, cashflow, years=years)
    summary = build_summary(ticker, info, rows, warnings)
    return {
        "summary": summary,
        "period_rows": rows,
        "source_notes": {
            "provider": "yfinance/Yahoo Finance public data",
            "period": period,
            "rows_returned": len(rows),
            "retrieved_at": utc_now_text(),
        },
    }


def serialize_value(value: Any) -> Any:
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def write_outputs(package: Dict[str, Any], output_dir: Path, prefix: str) -> Dict[str, Path]:
    import pandas as pd

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ticker = str(package["summary"]["ticker"]).replace(".", "_")
    base = output_dir / f"{prefix}_{ticker}_{timestamp}"
    rows = [{key: serialize_value(value) for key, value in row.items()} for row in package["period_rows"]]
    summary = package["summary"]

    csv_path = base.with_suffix(".csv")
    json_path = base.with_suffix(".json")
    xlsx_path = base.with_suffix(".xlsx")
    html_path = base.with_suffix(".html")

    pd.DataFrame(rows).to_csv(csv_path, index=False)
    json_path.write_text(json.dumps(package, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, index=False, sheet_name="Period Analysis")
        summary_rows = []
        for key, value in summary.items():
            if isinstance(value, list):
                value = "\n".join(value)
            summary_rows.append({"field": key, "value": value})
        pd.DataFrame(summary_rows).to_excel(writer, index=False, sheet_name="Summary")
    html_path.write_text(render_html(package), encoding="utf-8")
    return {"csv": csv_path, "json": json_path, "xlsx": xlsx_path, "html": html_path}


def render_html(package: Dict[str, Any]) -> str:
    summary = package["summary"]
    rows = package["period_rows"]
    cols = [
        "period",
        "revenue",
        "revenue_growth",
        "gross_margin",
        "operating_margin",
        "net_margin",
        "roe",
        "roa",
        "debt_to_equity",
        "current_ratio",
        "operating_cash_flow",
        "free_cash_flow",
        "fcf_margin",
        "ocf_to_net_income",
    ]

    def cell(col: str, value: Any) -> str:
        parsed = safe_float(value)
        if col in {"revenue_growth", "gross_margin", "operating_margin", "net_margin", "roe", "roa", "fcf_margin"}:
            text = pct(parsed) if parsed is not None else ""
        elif col in {"revenue", "operating_cash_flow", "free_cash_flow"}:
            text = compact_number(parsed) if parsed is not None else ""
        elif parsed is not None and col != "period":
            text = f"{parsed:.2f}"
        else:
            text = "" if value is None else str(value)
        return f"<td>{html.escape(text)}</td>"

    table_rows = []
    for row in rows:
        table_rows.append("<tr>" + "".join(cell(col, row.get(col)) for col in cols) + "</tr>")

    findings = "".join(f"<li>{html.escape(item)}</li>" for item in summary.get("findings", []))
    flags = "".join(f"<li>{html.escape(item)}</li>" for item in summary.get("risk_flags", []))
    headers = "".join(f"<th>{html.escape(col.replace('_', ' ').title())}</th>" for col in cols)

    return f"""<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Financial Statement Analysis - {html.escape(summary.get('ticker', ''))}</title>
  <style>
    body {{ margin: 0; background: #f5f7fa; color: #18212f; font-family: Arial, sans-serif; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 30px 22px; }}
    h1 {{ margin: 0 0 6px; font-size: 28px; }}
    .sub {{ color: #5f6f7f; margin-bottom: 20px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 20px 0; }}
    .tile {{ background: #ffffff; border: 1px solid #dfe7ef; border-radius: 8px; padding: 14px; }}
    .label {{ color: #627384; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
    .value {{ font-size: 22px; font-weight: 700; margin-top: 6px; }}
    section {{ margin-top: 22px; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #dfe7ef; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid #e8eef4; font-size: 13px; text-align: left; }}
    th {{ background: #eaf1f8; }}
    li {{ margin: 6px 0; }}
    .note {{ color: #627384; font-size: 13px; line-height: 1.5; margin-top: 18px; }}
  </style>
</head>
<body>
<main>
  <h1>{html.escape(summary.get('company', summary.get('ticker', '')))} ({html.escape(summary.get('ticker', ''))})</h1>
  <div class="sub">Financial statement analysis - latest period {html.escape(summary.get('latest_period', ''))}; retrieved {html.escape(summary.get('retrieved_at', ''))}</div>
  <div class="grid">
    <div class="tile"><div class="label">Statement Score</div><div class="value">{html.escape(str(summary.get('statement_quality_score', 'n/a')))}</div></div>
    <div class="tile"><div class="label">Data Confidence</div><div class="value">{html.escape(str(summary.get('data_confidence', 'n/a')))}%</div></div>
    <div class="tile"><div class="label">Market Cap</div><div class="value">{html.escape(compact_number(summary.get('market_cap')) or 'n/a')}</div></div>
    <div class="tile"><div class="label">Dividend Yield</div><div class="value">{html.escape(pct(summary.get('dividend_yield')) or 'n/a')}</div></div>
  </div>
  <section>
    <h2>Key Findings</h2>
    <ul>{findings}</ul>
  </section>
  <section>
    <h2>Risk Flags / Data Gaps</h2>
    <ul>{flags}</ul>
  </section>
  <section>
    <h2>Period Analysis</h2>
    <table><thead><tr>{headers}</tr></thead><tbody>{''.join(table_rows)}</tbody></table>
  </section>
  <p class="note">Automated analysis uses public provider data. Verify with IDX filings, annual reports, and company disclosures before making investment decisions.</p>
</main>
</body>
</html>
"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analisa laporan keuangan saham publik dari data yfinance.")
    parser.add_argument("--ticker", required=True, help="Ticker, contoh BBCA.JK atau AAPL.")
    parser.add_argument("--period", choices=["annual", "quarterly"], default="annual", help="Jenis periode statement.")
    parser.add_argument("--years", type=int, default=5, help="Jumlah periode yang diambil.")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"), help="Folder output.")
    parser.add_argument("--prefix", default="financial_statement_analysis", help="Prefix nama file output.")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    package = analyze_financial_statements(args.ticker, period=args.period, years=args.years)
    paths = write_outputs(package, args.output_dir, args.prefix)
    print("Outputs created:")
    for kind, path in paths.items():
        print(f"- {kind}: {path}")
    score = package["summary"].get("statement_quality_score")
    confidence = package["summary"].get("data_confidence")
    print(f"Statement score: {score}; data confidence: {confidence}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
