#!/usr/bin/env python
"""
Build a complete IDX watchlist CSV from the IDX stock-list Excel file.

Expected source columns:
- Kode
- Nama Perusahaan
- Tanggal Pencatatan
- Saham
- Papan Pencatatan
"""

import argparse
import csv
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional


DEFAULT_INPUT = Path(r"C:\Users\Alfath\Downloads\Daftar Saham  - 20260610.xlsx")
DEFAULT_OUTPUT = Path("data/watchlist.idx.csv")


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    return re.sub(r"\s+", " ", text)


def parse_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"[,;|]", value) if part.strip()]


def parse_shares(value: object) -> Optional[int]:
    text = normalize_text(value)
    if not text:
        return None
    digits = re.sub(r"[^0-9]", "", text)
    return int(digits) if digits else None


def clean_company_alias(name: str) -> str:
    cleaned = name
    cleaned = re.sub(r"\bPT\b\.?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bTbk\b\.?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bPersero\b\.?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" .,-")


def find_column(columns: Iterable[str], candidates: Iterable[str]) -> str:
    normalized = {str(col).strip().lower(): str(col) for col in columns}
    for candidate in candidates:
        key = candidate.strip().lower()
        if key in normalized:
            return normalized[key]
    raise ValueError(f"Missing required column. Tried: {', '.join(candidates)}")


def build_rows(
    input_path: Path,
    include_boards: List[str],
    exclude_boards: List[str],
    limit: Optional[int],
) -> List[Dict[str, object]]:
    import pandas as pd

    df = pd.read_excel(input_path)
    code_col = find_column(df.columns, ["Kode", "Code", "Symbol"])
    name_col = find_column(df.columns, ["Nama Perusahaan", "Company Name", "Nama"])
    listing_col = find_column(df.columns, ["Tanggal Pencatatan", "Listing Date"])
    shares_col = find_column(df.columns, ["Saham", "Shares", "Listed Shares"])
    board_col = find_column(df.columns, ["Papan Pencatatan", "Board"])

    include_set = {item.lower() for item in include_boards}
    exclude_set = {item.lower() for item in exclude_boards}
    rows: List[Dict[str, object]] = []

    for _, source in df.iterrows():
        code = normalize_text(source.get(code_col)).upper()
        company = normalize_text(source.get(name_col))
        board = normalize_text(source.get(board_col))
        if not code or not company:
            continue
        if include_set and board.lower() not in include_set:
            continue
        if exclude_set and board.lower() in exclude_set:
            continue

        clean_alias = clean_company_alias(company)
        aliases = [code, f"{code}.JK", company]
        if clean_alias and clean_alias.lower() != company.lower():
            aliases.append(clean_alias)

        rows.append(
            {
                "ticker": f"{code}.JK",
                "company": company,
                "aliases": ";".join(dict.fromkeys(aliases)),
                "country": "Indonesia",
                "sector": "",
                "idx_code": code,
                "idx_board": board,
                "listed_shares": parse_shares(source.get(shares_col)),
                "listing_date_raw": normalize_text(source.get(listing_col)),
            }
        )
        if limit and len(rows) >= limit:
            break
    return rows


def write_watchlist(rows: List[Dict[str, object]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "ticker",
        "company",
        "aliases",
        "country",
        "sector",
        "idx_code",
        "idx_board",
        "listed_shares",
        "listing_date_raw",
    ]
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert IDX stock-list XLSX into a toolkit watchlist CSV.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Path file Excel daftar saham IDX.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Path output watchlist CSV.")
    parser.add_argument("--include-boards", help="Filter papan pencatatan, dipisah koma/semicolon. Kosong = semua.")
    parser.add_argument("--exclude-boards", help="Papan pencatatan yang dikecualikan, dipisah koma/semicolon.")
    parser.add_argument("--limit", type=int, help="Batasi jumlah baris untuk testing.")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    if not args.input.exists():
        parser.error(f"Input tidak ditemukan: {args.input}")
    rows = build_rows(
        args.input,
        include_boards=parse_list(args.include_boards),
        exclude_boards=parse_list(args.exclude_boards),
        limit=args.limit,
    )
    write_watchlist(rows, args.output)
    print(f"Created {args.output} with {len(rows)} IDX tickers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
