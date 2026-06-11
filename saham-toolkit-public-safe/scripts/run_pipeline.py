#!/usr/bin/env python
"""
One-command pipeline:
1. collect company and macro news;
2. score company fundamentals;
3. blend the news summary into the research score.
"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


def script_path(name: str) -> Path:
    return Path(__file__).resolve().parent / name


def run_step(command: List[str]) -> None:
    print("\n$ " + " ".join(command))
    subprocess.run(command, check=True)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Jalankan pipeline berita + fundamental saham.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--tickers", help="Daftar ticker dipisah koma. Contoh: BBCA.JK,TLKM.JK,AAPL")
    source.add_argument("--watchlist", type=Path, help="CSV watchlist dengan kolom ticker, company, aliases, country, sector.")
    source.add_argument("--idx-excel", type=Path, help="File Excel daftar saham IDX untuk otomatis dibuat watchlist .JK.")
    parser.add_argument("--idx-watchlist-output", type=Path, default=Path("data/watchlist.idx.csv"), help="Output watchlist jika --idx-excel dipakai.")
    parser.add_argument("--days", type=int, default=7, help="Rentang berita ke belakang dalam hari.")
    parser.add_argument("--max-records", type=int, default=25, help="Maksimum artikel per query per sumber.")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"), help="Folder output.")
    parser.add_argument("--news-prefix", default="news", help="Prefix output berita.")
    parser.add_argument("--fundamental-prefix", default="fundamental_scores", help="Prefix output fundamental.")
    parser.add_argument("--skip-news", action="store_true", help="Lewati pengambilan berita dan pakai summary yang sudah ada.")
    parser.add_argument("--news-summary", type=Path, help="Path news summary manual jika --skip-news dipakai.")
    parser.add_argument("--no-macro", action="store_true", help="Jangan ambil berita makro default.")
    parser.add_argument("--no-gdelt", action="store_true", help="Matikan sumber GDELT.")
    parser.add_argument("--no-google-news", action="store_true", help="Matikan sumber Google News RSS.")
    parser.add_argument("--offset", type=int, default=0, help="Lewati N ticker pertama untuk proses batch.")
    parser.add_argument("--limit", type=int, help="Batasi jumlah ticker untuk proses batch.")
    parser.add_argument("--sleep", type=float, default=0.0, help="Jeda detik antar ticker untuk mengurangi rate limit.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    source_args: List[str]
    if args.idx_excel:
        idx_output = args.idx_watchlist_output.resolve()
        build_cmd = [
            sys.executable,
            str(script_path("build_idx_watchlist.py")),
            "--input",
            str(args.idx_excel.resolve()),
            "--output",
            str(idx_output),
        ]
        run_step(build_cmd)
        source_args = ["--watchlist", str(idx_output)]
    elif args.watchlist:
        source_args = ["--watchlist", str(args.watchlist.resolve())]
    else:
        source_args = ["--tickers", args.tickers]

    news_summary = args.news_summary.resolve() if args.news_summary else output_dir / f"{args.news_prefix}_summary.csv"

    if not args.skip_news:
        news_cmd = [
            sys.executable,
            str(script_path("process_news.py")),
            *source_args,
            "--days",
            str(args.days),
            "--max-records",
            str(args.max_records),
            "--output-dir",
            str(output_dir),
            "--prefix",
            args.news_prefix,
            "--offset",
            str(args.offset),
        ]
        if args.limit:
            news_cmd.extend(["--limit", str(args.limit)])
        if args.sleep > 0:
            news_cmd.extend(["--sleep", str(args.sleep)])
        if args.no_macro:
            news_cmd.append("--no-macro")
        if args.no_gdelt:
            news_cmd.append("--no-gdelt")
        if args.no_google_news:
            news_cmd.append("--no-google-news")
        run_step(news_cmd)

    fundamental_cmd = [
        sys.executable,
        str(script_path("analyze_stocks.py")),
        *source_args,
        "--news-summary",
        str(news_summary),
        "--output-dir",
        str(output_dir),
        "--prefix",
        args.fundamental_prefix,
        "--offset",
        str(args.offset),
    ]
    if args.limit:
        fundamental_cmd.extend(["--limit", str(args.limit)])
    if args.sleep > 0:
        fundamental_cmd.extend(["--sleep", str(args.sleep)])
    run_step(fundamental_cmd)
    print("\nPipeline selesai. Cek folder output:", output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
