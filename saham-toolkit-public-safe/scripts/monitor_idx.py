#!/usr/bin/env python
"""
Convenience runner for monitoring Indonesian listed stocks.

Default mode is news-only because monitoring all IDX tickers through live
fundamental endpoints can be slow and rate-limited. Use --mode full when you
want both news and fundamental scoring for the selected batch.
"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXCEL = Path(r"C:\Users\Alfath\Downloads\Daftar Saham  - 20260610.xlsx")
DEFAULT_WATCHLIST = ROOT / "data" / "watchlist.idx.csv"


def script_path(name: str) -> Path:
    return ROOT / "scripts" / name


def run_step(command: List[str]) -> None:
    print("\n$ " + " ".join(command))
    subprocess.run(command, check=True)


def ensure_watchlist(excel: Path, watchlist: Path, rebuild: bool) -> None:
    if watchlist.exists() and not rebuild:
        return
    run_step(
        [
            sys.executable,
            str(script_path("build_idx_watchlist.py")),
            "--input",
            str(excel),
            "--output",
            str(watchlist),
        ]
    )


def add_batch_args(command: List[str], args: argparse.Namespace) -> None:
    command.extend(["--offset", str(args.offset)])
    if args.limit:
        command.extend(["--limit", str(args.limit)])
    if args.sleep > 0:
        command.extend(["--sleep", str(args.sleep)])


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor berita dan fundamental semua saham lokal Indonesia/IDX.")
    parser.add_argument("--idx-excel", type=Path, default=DEFAULT_EXCEL, help="File Excel daftar saham IDX.")
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST, help="Watchlist CSV IDX hasil konversi.")
    parser.add_argument("--rebuild-watchlist", action="store_true", help="Bangun ulang watchlist dari Excel.")
    parser.add_argument("--mode", choices=["news", "fundamental", "full"], default="news", help="Jenis monitoring.")
    parser.add_argument("--days", type=int, default=1, help="Rentang berita ke belakang dalam hari.")
    parser.add_argument("--max-records", type=int, default=5, help="Maksimum artikel per ticker per sumber.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs" / "idx_monitor", help="Folder output.")
    parser.add_argument("--prefix", default="idx", help="Prefix file output.")
    parser.add_argument("--offset", type=int, default=0, help="Lewati N ticker pertama untuk proses batch.")
    parser.add_argument("--limit", type=int, help="Batasi jumlah ticker untuk proses batch.")
    parser.add_argument("--sleep", type=float, default=1.0, help="Jeda detik antar ticker untuk mengurangi rate limit.")
    parser.add_argument("--no-gdelt", action="store_true", help="Matikan sumber GDELT.")
    parser.add_argument("--no-google-news", action="store_true", help="Matikan sumber Google News RSS.")
    parser.add_argument("--no-macro", action="store_true", help="Jangan ambil berita makro default.")
    parser.add_argument("--hide-empty", action="store_true", help="Jangan tampilkan ticker tanpa berita di summary.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if not args.idx_excel.exists() and not args.watchlist.exists():
        parser.error(f"Excel IDX/watchlist tidak ditemukan: {args.idx_excel} / {args.watchlist}")

    watchlist = args.watchlist.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    ensure_watchlist(args.idx_excel.resolve(), watchlist, args.rebuild_watchlist)

    news_summary = output_dir / f"{args.prefix}_news_summary.csv"
    if args.mode in {"news", "full"}:
        news_cmd = [
            sys.executable,
            str(script_path("process_news.py")),
            "--watchlist",
            str(watchlist),
            "--days",
            str(args.days),
            "--max-records",
            str(args.max_records),
            "--output-dir",
            str(output_dir),
            "--prefix",
            f"{args.prefix}_news",
        ]
        add_batch_args(news_cmd, args)
        if args.no_gdelt:
            news_cmd.append("--no-gdelt")
        if args.no_google_news:
            news_cmd.append("--no-google-news")
        if args.no_macro:
            news_cmd.append("--no-macro")
        if not args.hide_empty:
            news_cmd.append("--include-empty")
        run_step(news_cmd)
        news_summary = output_dir / f"{args.prefix}_news_summary.csv"

    if args.mode in {"fundamental", "full"}:
        fundamental_cmd = [
            sys.executable,
            str(script_path("analyze_stocks.py")),
            "--watchlist",
            str(watchlist),
            "--output-dir",
            str(output_dir),
            "--prefix",
            f"{args.prefix}_fundamental",
        ]
        if news_summary.exists():
            fundamental_cmd.extend(["--news-summary", str(news_summary)])
        add_batch_args(fundamental_cmd, args)
        run_step(fundamental_cmd)

    print("\nIDX monitor selesai. Cek folder output:", output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
