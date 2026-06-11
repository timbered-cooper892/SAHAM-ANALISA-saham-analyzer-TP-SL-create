#!/usr/bin/env python
"""
Rolling realtime-ish monitor for IDX stocks.

For a small hosting server, the safe design is a rolling batch monitor:
it checks a slice of the IDX universe every interval, writes latest news
signals, refreshes recommendations, and cleans generated junk periodically.
"""

import argparse
import csv
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
DEFAULT_WATCHLIST = ROOT / "data" / "watchlist.idx.csv"
DEFAULT_OUTPUT = ROOT / "outputs" / "realtime"


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def read_watchlist(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def run_command(command: List[str], label: str, state: Dict[str, Any]) -> int:
    print(f"\n[{utc_now_text()}] {label}")
    print("$ " + subprocess.list2cmdline(command))
    process = subprocess.run(command, cwd=str(ROOT), text=True, capture_output=True)
    output = (process.stdout or "") + (process.stderr or "")
    if output.strip():
        print(output[-12000:])
    state.setdefault("last_commands", []).append(
        {
            "label": label,
            "returncode": process.returncode,
            "finished_at": utc_now_text(),
            "output_tail": output[-4000:],
        }
    )
    state["last_commands"] = state["last_commands"][-10:]
    return process.returncode


def compute_batch_offset(state: Dict[str, Any], total: int, batch_size: int) -> int:
    offset = int(state.get("next_offset") or 0)
    if total <= 0:
        return 0
    if offset >= total:
        offset = 0
    state["next_offset"] = (offset + batch_size) % total
    return offset


def run_cycle(args: argparse.Namespace, state: Dict[str, Any]) -> Dict[str, Any]:
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    watchlist = read_watchlist(args.watchlist)
    total = len(watchlist)
    offset = compute_batch_offset(state, total, args.batch_size)
    limit = min(args.batch_size, max(0, total - offset)) if total else args.batch_size
    state.update(
        {
            "status": "running_cycle",
            "updated_at": utc_now_text(),
            "watchlist_size": total,
            "current_offset": offset,
            "current_limit": limit,
            "batch_size": args.batch_size,
            "interval_minutes": args.interval_minutes,
        }
    )
    write_state(args.state_file, state)

    news_cmd = [
        sys.executable,
        str(SCRIPTS / "process_news.py"),
        "--watchlist",
        str(args.watchlist),
        "--days",
        str(args.days),
        "--max-records",
        str(args.max_records),
        "--offset",
        str(offset),
        "--limit",
        str(limit),
        "--sleep",
        str(args.sleep),
        "--output-dir",
        str(output_dir),
        "--prefix",
        "realtime_news",
        "--include-empty",
    ]
    if args.no_gdelt:
        news_cmd.append("--no-gdelt")
    if args.no_google_news:
        news_cmd.append("--no-google-news")
    if args.no_macro:
        news_cmd.append("--no-macro")
    news_code = run_command(news_cmd, "collect realtime news batch", state)

    if args.fundamental_every_batches > 0:
        cycle_index = int(state.get("cycle_index") or 0)
        if cycle_index % args.fundamental_every_batches == 0:
            fundamental_cmd = [
                sys.executable,
                str(SCRIPTS / "analyze_stocks.py"),
                "--watchlist",
                str(args.watchlist),
                "--offset",
                str(offset),
                "--limit",
                str(limit),
                "--sleep",
                str(args.sleep),
                "--output-dir",
                str(output_dir),
                "--prefix",
                "realtime_fundamental",
            ]
            news_summary = output_dir / "realtime_news_summary.csv"
            if news_summary.exists():
                fundamental_cmd.extend(["--news-summary", str(news_summary)])
            run_command(fundamental_cmd, "refresh fundamentals for current batch", state)

    recommendation_cmd = [
        sys.executable,
        str(SCRIPTS / "recommendation_engine.py"),
        "--watchlist",
        str(args.watchlist),
        "--news-summary",
        str(output_dir / "realtime_news_summary.csv"),
        "--idx-json-dir",
        str(ROOT / "outputs" / "hosted" / "idx_official_analysis"),
        "--idx-json-dir",
        str(ROOT / "outputs" / "idx_official_analysis"),
        "--document-json-dir",
        str(ROOT / "outputs" / "hosted" / "document_analysis"),
        "--document-json-dir",
        str(ROOT / "outputs" / "document_analysis"),
        "--output-dir",
        str(output_dir),
        "--prefix",
        "realtime_recommendations",
        "--top",
        str(args.top),
    ]
    reco_code = run_command(recommendation_cmd, "build latest recommendations", state)

    cleanup_cmd = [
        sys.executable,
        str(SCRIPTS / "cleanup_system.py"),
        "--root",
        str(ROOT),
        "--days",
        str(args.cleanup_days),
    ]
    cleanup_code = run_command(cleanup_cmd, "cleanup old generated files", state)

    state["cycle_index"] = int(state.get("cycle_index") or 0) + 1
    state["last_cycle_finished_at"] = utc_now_text()
    state["last_news_returncode"] = news_code
    state["last_recommendation_returncode"] = reco_code
    state["last_cleanup_returncode"] = cleanup_code
    state["status"] = "idle_between_cycles" if news_code == 0 and reco_code == 0 else "cycle_finished_with_errors"
    write_state(args.state_file, state)
    return state


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Realtime rolling monitor berita/rekomendasi seluruh saham IDX.")
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST, help="CSV watchlist IDX.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT, help="Folder output realtime.")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_OUTPUT / "state.json", help="File state monitor.")
    parser.add_argument("--interval-minutes", type=float, default=30.0, help="Jeda antar batch.")
    parser.add_argument("--batch-size", type=int, default=10, help="Jumlah saham per batch.")
    parser.add_argument("--days", type=int, default=1, help="Rentang berita ke belakang.")
    parser.add_argument("--max-records", type=int, default=2, help="Max artikel per saham.")
    parser.add_argument("--sleep", type=float, default=1.0, help="Jeda antar saham.")
    parser.add_argument("--top", type=int, default=60, help="Jumlah rekomendasi ditampilkan.")
    parser.add_argument("--cleanup-days", type=float, default=1.0, help="Usia file generated sebelum dibersihkan.")
    parser.add_argument("--fundamental-every-batches", type=int, default=0, help="Refresh fundamental per N batch; 0 untuk nonaktif.")
    parser.add_argument("--no-gdelt", action="store_true", default=True, help="Matikan GDELT agar server lebih ringan.")
    parser.add_argument("--use-gdelt", action="store_false", dest="no_gdelt", help="Aktifkan GDELT.")
    parser.add_argument("--no-google-news", action="store_true", help="Matikan Google News RSS.")
    parser.add_argument("--no-macro", action="store_true", help="Matikan berita makro.")
    parser.add_argument("--once", action="store_true", help="Jalankan satu siklus lalu keluar.")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    if not args.watchlist.exists():
        parser.error(f"Watchlist tidak ditemukan: {args.watchlist}")
    if args.batch_size < 1:
        parser.error("--batch-size minimal 1")
    if args.interval_minutes < 1 and not args.once:
        parser.error("--interval-minutes minimal 1 untuk mode loop")
    if args.no_gdelt and args.no_google_news:
        parser.error("Minimal satu sumber berita harus aktif.")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    state = load_state(args.state_file)
    state.setdefault("started_at", utc_now_text())
    state["mode"] = "once" if args.once else "loop"

    while True:
        state = run_cycle(args, state)
        if args.once:
            state["status"] = "stopped_after_once"
            write_state(args.state_file, state)
            break
        sleep_seconds = max(60.0, args.interval_minutes * 60.0)
        state["next_cycle_eta_seconds"] = sleep_seconds
        write_state(args.state_file, state)
        print(f"[{utc_now_text()}] sleeping {sleep_seconds:.0f}s")
        time.sleep(sleep_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
