#!/usr/bin/env python
"""
Clean generated cache, uploads, and old output files safely.

This script only deletes files inside the toolkit root. It never removes source
code, configuration files, watchlists, virtual environments, or deployment
archives.
"""

import argparse
import shutil
import time
from pathlib import Path
from typing import Iterable, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
KEEP_NAMES = {
    ".env",
    ".env.example",
    ".gitignore",
    "app.py",
    "main.py",
    "requirements.txt",
    "web_app.py",
    "README.md",
    "BOT_HOSTING_DEPLOY.md",
    "WISPBYTE_DEPLOY.md",
}
KEEP_SUFFIXES = {".py", ".md", ".csv"}
KEEP_DEPLOY_SUFFIXES = {".tar.gz", ".zip"}
DEFAULT_TARGETS = ["outputs", "uploads"]
CACHE_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def should_keep_source_file(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if len(rel.parts) == 1 and rel.name in KEEP_NAMES:
        return True
    if rel.parts and rel.parts[0] in {"scripts", "data"} and path.suffix.lower() in KEEP_SUFFIXES:
        return True
    return any(path.name.endswith(suffix) for suffix in KEEP_DEPLOY_SUFFIXES)


def iter_old_files(root: Path, target_names: Iterable[str], max_age_seconds: float) -> Iterable[Path]:
    cutoff = time.time() - max_age_seconds
    for name in target_names:
        target = (root / name).resolve()
        if not target.exists() or not is_relative_to(target, root):
            continue
        if target.is_file():
            if target.stat().st_mtime < cutoff and not should_keep_source_file(target, root):
                yield target
            continue
        for path in target.rglob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff and not should_keep_source_file(path, root):
                yield path.resolve()


def iter_cache_dirs(root: Path, include_dot_cache: bool) -> Iterable[Path]:
    names = set(CACHE_DIR_NAMES)
    if include_dot_cache:
        names.update({".cache"})
    for path in root.rglob("*"):
        if not path.is_dir():
            continue
        if path.name in names and ".venv" not in path.parts and is_relative_to(path.resolve(), root):
            yield path.resolve()


def remove_empty_dirs(root: Path, target_names: Iterable[str], dry_run: bool, actions: List[Tuple[str, str]]) -> None:
    for name in target_names:
        target = (root / name).resolve()
        if not target.exists() or not target.is_dir() or not is_relative_to(target, root):
            continue
        for directory in sorted([p for p in target.rglob("*") if p.is_dir()], key=lambda p: len(p.parts), reverse=True):
            try:
                if any(directory.iterdir()):
                    continue
            except OSError:
                continue
            actions.append(("empty_dir", str(directory)))
            if not dry_run:
                directory.rmdir()


def clean(root: Path, days: float, targets: List[str], include_dot_cache: bool, dry_run: bool) -> List[Tuple[str, str]]:
    root = root.resolve()
    max_age_seconds = max(0.0, days) * 86400
    actions: List[Tuple[str, str]] = []

    for cache_dir in sorted(iter_cache_dirs(root, include_dot_cache), key=lambda p: len(p.parts), reverse=True):
        if not is_relative_to(cache_dir, root):
            continue
        actions.append(("cache_dir", str(cache_dir)))
        if not dry_run:
            shutil.rmtree(cache_dir, ignore_errors=True)

    for file_path in sorted(iter_old_files(root, targets, max_age_seconds)):
        if not is_relative_to(file_path, root):
            continue
        actions.append(("old_file", str(file_path)))
        if not dry_run:
            try:
                file_path.unlink()
            except FileNotFoundError:
                pass

    remove_empty_dirs(root, targets, dry_run, actions)
    return actions


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bersihkan cache dan file output lama secara aman.")
    parser.add_argument("--root", type=Path, default=ROOT, help="Root project.")
    parser.add_argument("--days", type=float, default=2.0, help="Hapus file generated yang lebih tua dari N hari.")
    parser.add_argument("--target", action="append", default=[], help="Folder target relatif ke root. Bisa dipakai berkali-kali.")
    parser.add_argument("--include-dot-cache", action="store_true", help="Ikut hapus folder .cache di dalam root project.")
    parser.add_argument("--dry-run", action="store_true", help="Tampilkan yang akan dihapus tanpa menghapus.")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    targets = args.target or DEFAULT_TARGETS
    actions = clean(args.root, args.days, targets, args.include_dot_cache, args.dry_run)
    mode = "DRY RUN" if args.dry_run else "CLEANED"
    print(f"{mode}: {len(actions)} item")
    for kind, path in actions[:300]:
        print(f"- {kind}: {path}")
    if len(actions) > 300:
        print(f"... {len(actions) - 300} item lain tidak ditampilkan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
