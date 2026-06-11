#!/usr/bin/env python
"""
News and macro signal processor for equity research.

Sources:
- GDELT DOC API, no API key required.
- Google News RSS search, no API key required.

The output is a research signal, not a factual substitute for reading primary
filings, company releases, and the original articles.
"""

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode


POSITIVE_TERMS = [
    "laba naik",
    "laba bersih naik",
    "pendapatan naik",
    "penjualan naik",
    "margin naik",
    "dividen",
    "buyback",
    "akuisisi strategis",
    "kontrak baru",
    "ekspansi",
    "upgrade",
    "rating naik",
    "revisi naik",
    "guidance raised",
    "beats estimates",
    "beat estimates",
    "profit rises",
    "revenue growth",
    "strong demand",
    "record profit",
    "rate cut",
    "suku bunga turun",
    "inflasi turun",
]

NEGATIVE_TERMS = [
    "laba turun",
    "rugi",
    "pendapatan turun",
    "penjualan turun",
    "margin turun",
    "default",
    "gagal bayar",
    "downgrade",
    "rating turun",
    "revisi turun",
    "guidance cut",
    "misses estimates",
    "profit falls",
    "revenue decline",
    "weak demand",
    "layoff",
    "phk",
    "fraud",
    "korupsi",
    "lawsuit",
    "investigasi",
    "sanksi",
    "rate hike",
    "suku bunga naik",
    "inflasi naik",
    "rupiah melemah",
]

IMPACT_TAG_TERMS = {
    "earnings": [
        "laba",
        "profit",
        "earnings",
        "revenue",
        "pendapatan",
        "margin",
        "guidance",
    ],
    "rates": [
        "suku bunga",
        "bi rate",
        "federal reserve",
        "the fed",
        "interest rate",
        "yield",
        "treasury",
    ],
    "fx": [
        "rupiah",
        "dollar",
        "usd",
        "kurs",
        "exchange rate",
        "currency",
    ],
    "commodity": [
        "coal",
        "batubara",
        "oil",
        "minyak",
        "cpo",
        "nikel",
        "nickel",
        "emas",
        "gold",
        "commodity",
    ],
    "regulation": [
        "ojk",
        "bei",
        "idx",
        "regulasi",
        "regulation",
        "antitrust",
        "tariff",
        "pajak",
        "tax",
    ],
    "corporate_action": [
        "dividen",
        "rights issue",
        "right issue",
        "buyback",
        "merger",
        "akuisisi",
        "ipo",
        "spin off",
    ],
    "balance_sheet": [
        "utang",
        "debt",
        "bond",
        "obligasi",
        "refinancing",
        "liquidity",
        "gagal bayar",
        "default",
    ],
    "macro_growth": [
        "gdp",
        "pdb",
        "ekonomi",
        "economic growth",
        "manufacturing",
        "pmi",
        "consumer confidence",
    ],
}

RISK_FLAG_TERMS = {
    "legal_or_governance": ["fraud", "korupsi", "lawsuit", "investigasi", "audit", "governance"],
    "regulatory": ["regulasi", "ojk", "bei", "idx", "regulator", "antitrust", "tariff", "pajak", "tax"],
    "credit_or_liquidity": ["default", "gagal bayar", "refinancing", "liquidity", "utang", "debt", "bond", "obligasi"],
    "macro_pressure": ["suku bunga naik", "rate hike", "inflasi naik", "rupiah melemah", "recession", "resesi"],
    "operational": ["strike", "mogok", "shutdown", "kebakaran", "recall", "supply disruption"],
}


@dataclass
class NewsItem:
    ticker: str
    entity: str
    query_type: str
    source_route: str
    source_name: str
    published_at: str
    title: str
    url: str
    snippet: str
    sentiment_score: float
    sentiment_label: str
    impact_tags: str
    risk_flags: str
    matched_positive_terms: str
    matched_negative_terms: str
    confidence: float
    retrieved_at: str


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def normalize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_key(value: str) -> str:
    value = re.sub(r"https?://", "", value.lower())
    value = re.sub(r"[?#].*$", "", value)
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value[:180]


def read_watchlist(path: Path) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "ticker" not in reader.fieldnames:
            raise ValueError("Watchlist CSV must contain a 'ticker' column.")
        for row in reader:
            rows.append({key: normalize_text(value) for key, value in row.items()})
    return rows


def parse_ticker_list(value: str) -> List[Dict[str, str]]:
    rows = []
    for raw in value.split(","):
        ticker = raw.strip()
        if ticker:
            rows.append({"ticker": ticker, "company": "", "aliases": "", "country": "", "sector": ""})
    return rows


def slice_profiles(rows: List[Dict[str, str]], offset: int, limit: Optional[int]) -> List[Dict[str, str]]:
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    sliced = rows[offset:]
    return sliced[:limit] if limit else sliced


def split_aliases(value: str) -> List[str]:
    parts = re.split(r"[;|,]", value or "")
    return [part.strip() for part in parts if part.strip()]


def ticker_without_suffix(ticker: str) -> str:
    return ticker.split(".")[0].strip()


def unique_terms(terms: Iterable[str], limit: int = 7) -> List[str]:
    seen = set()
    result = []
    for term in terms:
        cleaned = normalize_text(term)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        if len(result) >= limit:
            break
    return result


def build_terms(profile: Dict[str, str]) -> List[str]:
    ticker = normalize_text(profile.get("ticker"))
    company = normalize_text(profile.get("company"))
    aliases = split_aliases(profile.get("aliases", ""))
    terms = [company, ticker_without_suffix(ticker), *aliases]
    return unique_terms(terms)


def quote_term(term: str) -> str:
    if " " in term:
        return f'"{term}"'
    return term


def build_query(terms: Sequence[str]) -> str:
    return " OR ".join(quote_term(term) for term in terms if term)


def fetch_gdelt(query: str, days: int, max_records: int) -> List[Dict[str, Any]]:
    import requests

    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": max_records,
        "sort": "HybridRel",
        "timespan": f"{days}d",
    }
    response = requests.get(url, params=params, timeout=25, headers={"User-Agent": "saham-fundamental-toolkit/1.0"})
    response.raise_for_status()
    payload = response.json()
    return payload.get("articles", []) or []


def fetch_google_news(query: str, days: int, max_records: int, locale: str) -> List[Dict[str, Any]]:
    import feedparser

    if locale == "id":
        params = {"q": f"{query} when:{days}d", "hl": "id", "gl": "ID", "ceid": "ID:id"}
    else:
        params = {"q": f"{query} when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    url = "https://news.google.com/rss/search?" + urlencode(params)
    feed = feedparser.parse(url)
    articles = []
    for entry in feed.entries[:max_records]:
        source_name = ""
        if hasattr(entry, "source") and isinstance(entry.source, dict):
            source_name = entry.source.get("title", "")
        articles.append(
            {
                "title": normalize_text(entry.get("title", "")),
                "url": entry.get("link", ""),
                "source": source_name or "Google News",
                "seendate": entry.get("published", ""),
                "snippet": normalize_text(entry.get("summary", "")),
            }
        )
    return articles


def matched_terms(text: str, terms: Sequence[str]) -> List[str]:
    lower = text.lower()
    return [term for term in terms if term.lower() in lower]


def tags_for_text(text: str, taxonomy: Dict[str, Sequence[str]]) -> List[str]:
    lower = text.lower()
    tags = []
    for tag, terms in taxonomy.items():
        if any(term.lower() in lower for term in terms):
            tags.append(tag)
    return tags


def term_matches_text(term: str, text: str) -> bool:
    clean = normalize_text(term)
    if not clean:
        return False
    lower_text = text.lower()
    lower_term = clean.lower()
    if re.fullmatch(r"[a-z0-9]{2,6}(\.jk)?", lower_term):
        base = lower_term.replace(".jk", "")
        return re.search(rf"(?<![a-z0-9]){re.escape(base)}(?:\.jk)?(?![a-z0-9])", lower_text) is not None
    return lower_term in lower_text


def article_matches_terms(item: NewsItem, terms: Sequence[str]) -> bool:
    text = f"{item.title} {item.snippet} {item.source_name}".lower()
    return any(term_matches_text(term, text) for term in terms)


def sentiment_label(score: float) -> str:
    if score >= 0.25:
        return "positive"
    if score <= -0.25:
        return "negative"
    return "neutral"


def score_article(title: str, snippet: str) -> Tuple[float, str, List[str], List[str], List[str], List[str], float]:
    text = f"{title} {snippet}".lower()
    positives = matched_terms(text, POSITIVE_TERMS)
    negatives = matched_terms(text, NEGATIVE_TERMS)
    impact_tags = tags_for_text(text, IMPACT_TAG_TERMS)
    risk_flags = tags_for_text(text, RISK_FLAG_TERMS)

    raw = (len(positives) - len(negatives)) / max(1, len(positives) + len(negatives))
    risk_penalty = min(0.25, 0.06 * len(risk_flags))
    score = max(-1.0, min(1.0, raw - risk_penalty))
    confidence = min(1.0, 0.35 + 0.08 * (len(positives) + len(negatives)) + 0.05 * len(impact_tags) + 0.04 * len(risk_flags))
    return score, sentiment_label(score), impact_tags, risk_flags, positives, negatives, round(confidence, 2)


def normalize_article(raw: Dict[str, Any], source_route: str, ticker: str, entity: str, query_type: str) -> NewsItem:
    title = normalize_text(raw.get("title"))
    snippet = normalize_text(raw.get("snippet") or raw.get("description") or raw.get("summary"))
    url = normalize_text(raw.get("url"))
    source_name = normalize_text(raw.get("source") or raw.get("domain") or raw.get("sourceCountry") or source_route)
    published = normalize_text(raw.get("seendate") or raw.get("publishedAt") or raw.get("published") or "")
    score, label, impact_tags, risk_flags, positives, negatives, confidence = score_article(title, snippet)
    return NewsItem(
        ticker=ticker,
        entity=entity,
        query_type=query_type,
        source_route=source_route,
        source_name=source_name,
        published_at=published,
        title=title,
        url=url,
        snippet=snippet,
        sentiment_score=round(score, 3),
        sentiment_label=label,
        impact_tags=";".join(impact_tags),
        risk_flags=";".join(risk_flags),
        matched_positive_terms=";".join(positives),
        matched_negative_terms=";".join(negatives),
        confidence=confidence,
        retrieved_at=utc_now_text(),
    )


def dedupe_items(items: List[NewsItem]) -> List[NewsItem]:
    seen = set()
    deduped = []
    for item in items:
        key_source = item.url or item.title
        key = normalize_key(key_source)
        if not key:
            key = hashlib.sha1(f"{item.ticker}:{item.title}".encode("utf-8")).hexdigest()
        ticker_scoped_key = f"{item.ticker}:{key}"
        if ticker_scoped_key in seen:
            continue
        seen.add(ticker_scoped_key)
        deduped.append(item)
    return deduped


def profile_locale(profile: Dict[str, str]) -> str:
    country = normalize_text(profile.get("country")).lower()
    ticker = normalize_text(profile.get("ticker")).upper()
    if country == "indonesia" or ticker.endswith(".JK"):
        return "id"
    return "en"


def collect_for_profile(
    profile: Dict[str, str],
    days: int,
    max_records: int,
    use_gdelt: bool,
    use_google: bool,
) -> List[NewsItem]:
    ticker = normalize_text(profile.get("ticker")) or "UNKNOWN"
    entity = normalize_text(profile.get("company")) or ticker
    terms = build_terms(profile)
    if not terms:
        return []
    query = build_query(terms)
    items: List[NewsItem] = []

    if use_gdelt:
        try:
            for raw in fetch_gdelt(query, days=days, max_records=max_records):
                item = normalize_article(raw, "GDELT", ticker, entity, "company")
                if article_matches_terms(item, terms):
                    items.append(item)
        except Exception as exc:
            print(f"[news] GDELT failed for {ticker}: {exc}", file=sys.stderr)

    if use_google:
        try:
            for raw in fetch_google_news(query, days=days, max_records=max_records, locale=profile_locale(profile)):
                item = normalize_article(raw, "Google News RSS", ticker, entity, "company")
                if article_matches_terms(item, terms):
                    items.append(item)
        except Exception as exc:
            print(f"[news] Google News failed for {ticker}: {exc}", file=sys.stderr)

    return dedupe_items(items)


DEFAULT_MACRO_QUERIES = [
    {
        "ticker": "MACRO_ID",
        "entity": "Indonesia Macro",
        "query": '"ekonomi Indonesia" OR inflasi OR "BI Rate" OR rupiah OR IHSG OR OJK OR "Bank Indonesia"',
        "locale": "id",
    },
    {
        "ticker": "MACRO_GLOBAL",
        "entity": "Global Macro",
        "query": '"Federal Reserve" OR inflation OR "interest rates" OR "oil prices" OR "China economy" OR "global economy"',
        "locale": "en",
    },
]


def collect_macro(
    days: int,
    max_records: int,
    use_gdelt: bool,
    use_google: bool,
    extra_queries: Sequence[str],
) -> List[NewsItem]:
    queries = list(DEFAULT_MACRO_QUERIES)
    for idx, query in enumerate(extra_queries, start=1):
        queries.append({"ticker": f"MACRO_CUSTOM_{idx}", "entity": f"Custom Macro {idx}", "query": query, "locale": "id"})

    items: List[NewsItem] = []
    for item in queries:
        ticker = item["ticker"]
        entity = item["entity"]
        query = item["query"]
        locale = item["locale"]
        if use_gdelt:
            try:
                for raw in fetch_gdelt(query, days=days, max_records=max_records):
                    items.append(normalize_article(raw, "GDELT", ticker, entity, "macro"))
            except Exception as exc:
                print(f"[news] GDELT failed for {ticker}: {exc}", file=sys.stderr)
        if use_google:
            try:
                for raw in fetch_google_news(query, days=days, max_records=max_records, locale=locale):
                    items.append(normalize_article(raw, "Google News RSS", ticker, entity, "macro"))
            except Exception as exc:
                print(f"[news] Google News failed for {ticker}: {exc}", file=sys.stderr)
    return dedupe_items(items)


def empty_summary_row(profile: Dict[str, str]) -> Dict[str, Any]:
    ticker = normalize_text(profile.get("ticker"))
    entity = normalize_text(profile.get("company")) or ticker
    return {
        "ticker": ticker,
        "entity": entity,
        "query_type": "company",
        "article_count": 0,
        "avg_sentiment": 0.0,
        "sentiment_label": "no_news",
        "risk_article_count": 0,
        "top_impact_tags": "",
        "top_risk_flags": "",
        "top_headlines": "",
        "retrieved_at": utc_now_text(),
    }


def summarize(
    items: List[NewsItem],
    profiles: Optional[List[Dict[str, str]]] = None,
    include_empty: bool = False,
) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[NewsItem]] = defaultdict(list)
    for item in items:
        grouped[item.ticker].append(item)

    rows = []
    if include_empty and profiles:
        for profile in profiles:
            ticker = normalize_text(profile.get("ticker"))
            if ticker and ticker not in grouped:
                rows.append(empty_summary_row(profile))

    for ticker, group in grouped.items():
        sentiments = [item.sentiment_score for item in group if item.sentiment_score is not None]
        avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0
        risk_count = sum(1 for item in group if item.risk_flags)
        tag_counter: Counter[str] = Counter()
        risk_counter: Counter[str] = Counter()
        for item in group:
            tag_counter.update([tag for tag in item.impact_tags.split(";") if tag])
            risk_counter.update([tag for tag in item.risk_flags.split(";") if tag])
        top = sorted(group, key=lambda x: (abs(x.sentiment_score), x.confidence), reverse=True)[:4]
        rows.append(
            {
                "ticker": ticker,
                "entity": group[0].entity,
                "query_type": group[0].query_type,
                "article_count": len(group),
                "avg_sentiment": round(avg_sentiment, 3),
                "sentiment_label": sentiment_label(avg_sentiment),
                "risk_article_count": risk_count,
                "top_impact_tags": ";".join([tag for tag, _ in tag_counter.most_common(6)]),
                "top_risk_flags": ";".join([tag for tag, _ in risk_counter.most_common(6)]),
                "top_headlines": " | ".join([item.title for item in top if item.title]),
                "retrieved_at": utc_now_text(),
            }
        )
    return sorted(rows, key=lambda row: (row["query_type"], row["ticker"]))


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def render_digest(summary_rows: List[Dict[str, Any]], items: List[NewsItem], generated_at: str) -> str:
    lines = [
        "# News And Macro Signal Digest",
        "",
        f"Generated at: {generated_at}",
        "",
        "Catatan: ini sinyal awal untuk triage riset. Buka artikel asli dan sumber primer sebelum membuat keputusan investasi.",
        "",
        "## Summary",
        "",
        "| Entity | Articles | Sentiment | Risks | Top Tags |",
        "|---|---:|---:|---:|---|",
    ]
    for row in summary_rows:
        if row["article_count"] == 0:
            continue
        lines.append(
            f"| {row['ticker']} - {row['entity']} | {row['article_count']} | {row['avg_sentiment']} ({row['sentiment_label']}) | {row['risk_article_count']} | {row['top_impact_tags']} |"
        )

    lines.extend(["", "## Top Headlines", ""])
    by_ticker: Dict[str, List[NewsItem]] = defaultdict(list)
    for item in items:
        by_ticker[item.ticker].append(item)

    for row in summary_rows:
        ticker = row["ticker"]
        if row["article_count"] == 0:
            continue
        lines.append(f"### {ticker} - {row['entity']}")
        top_items = sorted(by_ticker[ticker], key=lambda x: (abs(x.sentiment_score), x.confidence), reverse=True)[:5]
        for item in top_items:
            title = item.title or "(no title)"
            source = item.source_name or item.source_route
            label = item.sentiment_label
            tags = item.impact_tags or "-"
            url = item.url
            if url:
                lines.append(f"- [{title}]({url}) - {source}; sentiment {item.sentiment_score} ({label}); tags: {tags}")
            else:
                lines.append(f"- {title} - {source}; sentiment {item.sentiment_score} ({label}); tags: {tags}")
        lines.append("")
    return "\n".join(lines)


def write_outputs(
    items: List[NewsItem],
    output_dir: Path,
    prefix: str,
    profiles: Optional[List[Dict[str, str]]] = None,
    include_empty: bool = False,
) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    generated_at = utc_now_text()
    summary_rows = summarize(items, profiles=profiles, include_empty=include_empty)

    item_rows = [asdict(item) for item in items]
    item_fields = list(NewsItem.__dataclass_fields__.keys())
    summary_fields = [
        "ticker",
        "entity",
        "query_type",
        "article_count",
        "avg_sentiment",
        "sentiment_label",
        "risk_article_count",
        "top_impact_tags",
        "top_risk_flags",
        "top_headlines",
        "retrieved_at",
    ]

    paths = {
        "items_csv": output_dir / f"{prefix}_items_{timestamp}.csv",
        "summary_csv": output_dir / f"{prefix}_summary_{timestamp}.csv",
        "digest_md": output_dir / f"{prefix}_digest_{timestamp}.md",
        "items_latest": output_dir / f"{prefix}_items.csv",
        "summary_latest": output_dir / f"{prefix}_summary.csv",
        "digest_latest": output_dir / f"{prefix}_digest.md",
        "json_latest": output_dir / f"{prefix}_items.json",
    }

    write_csv(paths["items_csv"], item_rows, item_fields)
    write_csv(paths["summary_csv"], summary_rows, summary_fields)
    write_csv(paths["items_latest"], item_rows, item_fields)
    write_csv(paths["summary_latest"], summary_rows, summary_fields)
    digest = render_digest(summary_rows, items, generated_at)
    paths["digest_md"].write_text(digest, encoding="utf-8")
    paths["digest_latest"].write_text(digest, encoding="utf-8")
    paths["json_latest"].write_text(json.dumps(item_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ambil dan olah berita ekonomi/perusahaan terkini menjadi sinyal riset saham.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--tickers", help="Daftar ticker dipisah koma. Contoh: BBCA.JK,TLKM.JK,AAPL")
    source.add_argument("--watchlist", type=Path, help="CSV watchlist dengan kolom ticker, company, aliases, country, sector.")
    parser.add_argument("--days", type=int, default=7, help="Rentang berita ke belakang dalam hari.")
    parser.add_argument("--max-records", type=int, default=25, help="Maksimum artikel per query per sumber.")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"), help="Folder output.")
    parser.add_argument("--prefix", default="news", help="Prefix nama file output.")
    parser.add_argument("--macro-query", action="append", default=[], help="Query makro tambahan. Bisa dipakai berkali-kali.")
    parser.add_argument("--no-macro", action="store_true", help="Jangan ambil berita makro default.")
    parser.add_argument("--no-gdelt", action="store_true", help="Matikan sumber GDELT.")
    parser.add_argument("--no-google-news", action="store_true", help="Matikan sumber Google News RSS.")
    parser.add_argument("--offset", type=int, default=0, help="Lewati N ticker pertama untuk proses batch.")
    parser.add_argument("--limit", type=int, help="Batasi jumlah ticker untuk proses batch.")
    parser.add_argument("--sleep", type=float, default=0.0, help="Jeda detik antar ticker untuk mengurangi rate limit.")
    parser.add_argument("--include-empty", action="store_true", help="Masukkan ticker tanpa berita ke summary dengan article_count 0.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.days < 1:
        parser.error("--days minimal 1")
    if args.max_records < 1:
        parser.error("--max-records minimal 1")
    use_gdelt = not args.no_gdelt
    use_google = not args.no_google_news
    if not use_gdelt and not use_google:
        parser.error("Minimal satu sumber harus aktif: GDELT atau Google News RSS.")

    profiles: List[Dict[str, str]] = []
    if args.watchlist:
        profiles = read_watchlist(args.watchlist)
    elif args.tickers:
        profiles = parse_ticker_list(args.tickers)
    profiles = slice_profiles(profiles, args.offset, args.limit)

    items: List[NewsItem] = []
    for index, profile in enumerate(profiles):
        ticker = profile.get("ticker", "")
        print(f"[news] collecting company news for {ticker}...", file=sys.stderr)
        items.extend(collect_for_profile(profile, args.days, args.max_records, use_gdelt, use_google))
        if args.sleep > 0 and index < len(profiles) - 1:
            time.sleep(args.sleep)

    if not args.no_macro:
        print("[news] collecting macro news...", file=sys.stderr)
        items.extend(collect_macro(args.days, args.max_records, use_gdelt, use_google, args.macro_query))

    items = dedupe_items(items)
    paths = write_outputs(items, args.output_dir, args.prefix, profiles=profiles, include_empty=args.include_empty)

    print("Outputs created:")
    for kind, path in paths.items():
        print(f"- {kind}: {path}")
    print(f"Articles processed: {len(items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
