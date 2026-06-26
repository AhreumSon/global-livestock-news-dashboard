#!/usr/bin/env python3
"""
Global livestock news collector

Purpose
- Collect news metadata from 5 livestock/feed media sources.
- Save only title, date, original URL, short summary, and classification fields.
- Do not store full article text.
- Keep the previous JSON articles when collection fails, and record failure status.

Expected output
- news-data.json in the same directory as this script by default.

Install
    pip install requests beautifulsoup4 feedparser python-dateutil

Run
    python collect_news.py
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = BASE_DIR / "news-data.json"
MAX_ARTICLES_PER_SOURCE = 10
REQUEST_TIMEOUT = 20
USER_AGENT = "EASYBIO-NBD-NewsMonitor/0.1 (+internal dashboard; contact: ardongja@gmail.com)"


@dataclass
class SourceConfig:
    name: str
    source_url: str
    # RSS URLs are candidates. Some websites may disable or change RSS without notice.
    rss_urls: List[str] = field(default_factory=list)
    # Listing URL is used as a fallback when RSS is absent or fails.
    listing_url: Optional[str] = None
    # Optional CSS selectors for listing cards. The generic parser also runs if these fail.
    selectors: List[str] = field(default_factory=list)


SOURCES: List[SourceConfig] = [
    SourceConfig(
        name="Feed Strategy",
        source_url="https://www.feedstrategy.com/",
        rss_urls=["https://www.feedstrategy.com/feed/"],
        listing_url="https://www.feedstrategy.com/",
        selectors=["article", ".card", ".post", ".article-card"],
    ),
    SourceConfig(
        name="All About Feed",
        source_url="https://www.allaboutfeed.net/",
        rss_urls=["https://www.allaboutfeed.net/feed/"],
        listing_url="https://www.allaboutfeed.net/",
        selectors=["article", ".teaser", ".card", ".post"],
    ),
    SourceConfig(
        name="Feed & Additive Magazine",
        source_url="https://www.feedandadditive.com/",
        rss_urls=["https://www.feedandadditive.com/feed/"],
        listing_url="https://www.feedandadditive.com/",
        selectors=["article", ".post", ".elementor-post", ".td-module-container"],
    ),
    SourceConfig(
        name="The Pig Site",
        source_url="https://www.thepigsite.com/",
        rss_urls=[],
        listing_url="https://www.thepigsite.com/latest?section=news",
        selectors=["article", ".post-listing", ".card", "li"],
    ),
    SourceConfig(
        name="The Poultry Site",
        source_url="https://www.thepoultrysite.com/",
        rss_urls=[],
        listing_url="https://www.thepoultrysite.com/latest?section=news",
        selectors=["article", ".post-listing", ".card", "li"],
    ),
]

CATEGORY_RULES: List[Tuple[str, List[str]]] = [
    ("Disease / Biosecurity", ["asf", "avian influenza", "hpai", "bird flu", "salmonella", "e. coli", "clostridium", "biosecurity", "disease", "vaccine", "pathogen"]),
    ("Feed Additives", ["additive", "enzyme", "phytase", "probiotic", "postbiotic", "prebiotic", "yeast", "organic acid", "phytogenic", "mycotoxin", "emulsifier", "lecithin", "amino acid", "vitamin", "mineral"]),
    ("Animal Nutrition", ["nutrition", "diet", "feed formulation", "digestibility", "fcr", "feed conversion", "growth performance", "weaning", "broiler", "layer", "rumen", "gut health", "nutrient"]),
    ("Regulation", ["regulation", "policy", "ban", "approval", "efsa", "fda", "usda", "compliance", "traceability", "feed safety"]),
    ("Market / Trade", ["market", "price", "export", "import", "trade", "commodity", "futures", "demand", "supply", "tariff"]),
    ("Sustainability", ["sustainability", "carbon", "methane", "climate", "emission", "resource efficiency", "circular", "environment"]),
]

SPECIES_RULES: List[Tuple[str, List[str]]] = [
    ("Swine", ["swine", "pig", "piglet", "pork", "sow", "weaning", "hog"]),
    ("Poultry", ["poultry", "broiler", "layer", "chicken", "egg", "avian", "turkey", "hen"]),
    ("Ruminant", ["ruminant", "dairy", "cattle", "cow", "beef", "calf", "rumen", "methane"]),
    ("Aquaculture", ["aquaculture", "shrimp", "fish", "salmon", "tilapia"]),
]

HIGH_PRIORITY_TERMS = [
    "asf", "avian influenza", "hpai", "bird flu", "salmonella", "mycotoxin", "feed safety",
    "regulation", "ban", "recall", "outbreak", "biosecurity", "heat stress", "phytase",
    "enzyme", "postbiotic", "probiotic", "gut health", "feed additive", "growth performance", "fcr"
]
MEDIUM_PRIORITY_TERMS = [
    "market", "price", "export", "import", "trade", "commodity", "sustainability",
    "vitamin", "amino acid", "mineral", "feed cost", "formulation"
]


def now_kst() -> datetime:
    return datetime.now(tz=KST)


def iso_kst(dt: Optional[datetime] = None) -> str:
    return (dt or now_kst()).isoformat(timespec="seconds")


def clean_text(value: Any) -> str:
    text = BeautifulSoup(str(value or ""), "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    # Remove fragments and common tracking query parameters.
    clean = parsed._replace(fragment="")
    return urlunparse(clean)


def slug_hash(*values: str, length: int = 8) -> str:
    raw = "|".join(values).encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:length]


def make_article_id(source: str, title: str, url: str, published_date: str) -> str:
    source_slug = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
    date_part = published_date.replace("-", "") or "undated"
    return f"{source_slug}-{date_part}-{slug_hash(title, url)}"


def parse_date(value: Any) -> str:
    if not value:
        return now_kst().date().isoformat()
    try:
        dt = date_parser.parse(str(value))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST).date().isoformat()
    except Exception:
        return now_kst().date().isoformat()


def summarize(title: str, description: str, max_chars: int = 230) -> str:
    """Use publisher-provided snippet/description only; do not fetch or store full article body."""
    text = clean_text(description) or clean_text(title)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rsplit(" ", 1)[0] + "…"


def infer_category(text: str) -> str:
    lower = text.lower()
    for category, terms in CATEGORY_RULES:
        if any(term in lower for term in terms):
            return category
    return "Unclassified"


def infer_species(text: str) -> List[str]:
    lower = text.lower()
    species = [name for name, terms in SPECIES_RULES if any(term in lower for term in terms)]
    return species or ["Multi-species"]


def extract_keywords(text: str, limit: int = 6) -> List[str]:
    lower = text.lower()
    candidate_terms = sorted(
        set(term for _, terms in CATEGORY_RULES + SPECIES_RULES for term in terms),
        key=len,
        reverse=True,
    )
    keywords = []
    for term in candidate_terms:
        if term in lower and term not in keywords:
            keywords.append(term)
        if len(keywords) >= limit:
            break
    return keywords


def priority_score(text: str, category: str) -> Tuple[str, int]:
    lower = text.lower()
    score = 30
    score += sum(12 for term in HIGH_PRIORITY_TERMS if term in lower)
    score += sum(6 for term in MEDIUM_PRIORITY_TERMS if term in lower)
    if category in {"Disease / Biosecurity", "Feed Additives", "Regulation"}:
        score += 10
    score = max(0, min(score, 100))
    if score >= 75:
        return "High", score
    if score >= 50:
        return "Medium", score
    return "Low", score


def make_pm_note(category: str, species: List[str], keywords: List[str]) -> str:
    species_text = ", ".join(species)
    keyword_text = ", ".join(keywords[:3]) if keywords else "핵심 키워드"
    templates = {
        "Disease / Biosecurity": f"{species_text} 질병·방역 이슈로, 면역·장건강·스트레스 대응 제품 메시지와 연결 가능성을 검토해야 합니다.",
        "Feed Additives": f"{keyword_text} 관련 첨가제 claim, 적용 축종, 근거 수준, 경쟁제품 포지셔닝을 비교할 필요가 있습니다.",
        "Animal Nutrition": f"{species_text} 영양 전략 이슈로, 성장성·FCR·영양소 이용률 claim과 연결 가능성을 검토할 수 있습니다.",
        "Regulation": "규제·품질 이슈로, 제품 등록 문구, 원료 추적성, QC 자료 업데이트 필요성을 확인해야 합니다.",
        "Market / Trade": "시장·무역 이슈로, 고객사의 원가 관리와 생산성 개선 니즈에 미치는 영향을 검토할 수 있습니다.",
        "Sustainability": "지속가능성 이슈로, FCR 개선, 배출 저감, 자원 효율 claim의 수치 근거 확보가 필요합니다.",
    }
    return templates.get(category, "PM 관점에서 제품 포지셔닝, claim, 고객 영향도를 추가 검토해야 합니다.")


def classify_article(source_name: str, title: str, url: str, published_date: str, description: str) -> Dict[str, Any]:
    text = " ".join([source_name, title, description])
    category = infer_category(text)
    species = infer_species(text)
    keywords = extract_keywords(text)
    priority, score = priority_score(text, category)
    return {
        "id": make_article_id(source_name, title, url, published_date),
        "source": source_name,
        "title": clean_text(title),
        "url": normalize_url(url),
        "published_date": published_date,
        "collected_at": iso_kst(),
        "summary": summarize(title, description),
        "category": category,
        "species": species,
        "priority": priority,
        "priority_score": score,
        "keywords": keywords,
        "why_it_matters": make_pm_note(category, species, keywords),
    }


def get_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/rss+xml,application/xml;q=0.9,*/*;q=0.8"})
    return session


def fetch_rss(source: SourceConfig, session: requests.Session) -> List[Dict[str, Any]]:
    articles: List[Dict[str, Any]] = []
    errors = []
    for rss_url in source.rss_urls:
        try:
            resp = session.get(rss_url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            if feed.bozo and not feed.entries:
                raise RuntimeError(f"RSS parse failed: {feed.bozo_exception}")
            for entry in feed.entries[:MAX_ARTICLES_PER_SOURCE * 2]:
                title = clean_text(entry.get("title", ""))
                link = entry.get("link") or source.source_url
                published = parse_date(entry.get("published") or entry.get("updated"))
                description = entry.get("summary") or entry.get("description") or title
                if title and link:
                    articles.append(classify_article(source.name, title, link, published, description))
            if articles:
                return articles[:MAX_ARTICLES_PER_SOURCE]
        except Exception as exc:
            errors.append(f"{rss_url}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))
    return articles


def extract_listing_candidates(soup: BeautifulSoup, source: SourceConfig) -> Iterable[BeautifulSoup]:
    seen = set()
    selectors = source.selectors or ["article", ".card", ".post", "li"]
    for selector in selectors:
        for node in soup.select(selector):
            key = id(node)
            if key not in seen:
                seen.add(key)
                yield node
    # Generic fallback: anchors with enough title-like text.
    for link in soup.find_all("a", href=True):
        text = clean_text(link.get_text(" "))
        if len(text) >= 25:
            key = id(link)
            if key not in seen:
                seen.add(key)
                yield link


def fetch_listing(source: SourceConfig, session: requests.Session) -> List[Dict[str, Any]]:
    if not source.listing_url:
        return []
    resp = session.get(source.listing_url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    articles: List[Dict[str, Any]] = []
    for node in extract_listing_candidates(soup, source):
        link = node.find("a", href=True) if hasattr(node, "find") else None
        if node.name == "a" and node.get("href"):
            link = node
        if not link:
            continue
        title = clean_text(link.get_text(" "))
        href = urljoin(source.source_url, link.get("href"))
        if len(title) < 20 or href == source.source_url:
            continue
        node_text = clean_text(node.get_text(" "))
        # Try to find a date from nearby text; if absent, use today's date.
        date_match = re.search(r"\b(?:\d{1,2}\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})\b", node_text)
        published = parse_date(date_match.group(0) if date_match else None)
        articles.append(classify_article(source.name, title, href, published, node_text))
        if len(articles) >= MAX_ARTICLES_PER_SOURCE * 2:
            break
    return articles[:MAX_ARTICLES_PER_SOURCE]


def collect_source(source: SourceConfig, previous_source: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    session = get_session()
    attempted_at = iso_kst()
    errors: List[str] = []

    for method_name, fetcher in (("rss", fetch_rss), ("listing", fetch_listing)):
        try:
            articles = fetcher(source, session)
            articles = deduplicate_articles(articles)
            if articles:
                return {
                    "name": source.name,
                    "source_url": source.listing_url or source.source_url,
                    "status": "success",
                    "last_success_at": attempted_at,
                    "last_attempt_at": attempted_at,
                    "collection_method": method_name,
                    "error_message": "",
                    "articles": articles,
                }
            errors.append(f"{method_name}: no articles found")
        except Exception as exc:
            errors.append(f"{method_name}: {exc}")

    # Failure policy: keep previous articles, update only status/error fields.
    fallback_articles = previous_source.get("articles", []) if previous_source else []
    last_success = previous_source.get("last_success_at", "") if previous_source else ""
    return {
        "name": source.name,
        "source_url": source.listing_url or source.source_url,
        "status": "failed" if not fallback_articles else "stale",
        "last_success_at": last_success,
        "last_attempt_at": attempted_at,
        "collection_method": "previous_json_fallback",
        "error_message": " | ".join(errors)[:800],
        "articles": fallback_articles,
    }


def dedupe_key(article: Dict[str, Any]) -> Tuple[str, str]:
    url = normalize_url(article.get("url", "")).lower().rstrip("/")
    title = re.sub(r"[^a-z0-9가-힣]+", " ", article.get("title", "").lower()).strip()
    title_hash = slug_hash(title, length=12)
    return url, title_hash


def deduplicate_articles(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove duplicates by canonical URL first, then normalized title hash."""
    seen_urls = set()
    seen_titles = set()
    unique = []
    for article in articles:
        url_key, title_key = dedupe_key(article)
        if url_key and url_key in seen_urls:
            continue
        if title_key in seen_titles:
            continue
        if url_key:
            seen_urls.add(url_key)
        seen_titles.add(title_key)
        unique.append(article)
    return unique


def load_previous(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def find_previous_source(previous: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    for source in previous.get("sources", []):
        if source.get("name") == name:
            return source
    return None


def build_output(sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    generated = now_kst()
    success = sum(1 for s in sources if s.get("status") == "success")
    failed = sum(1 for s in sources if s.get("status") == "failed")
    stale = sum(1 for s in sources if s.get("status") == "stale")
    run_status = "success" if success == len(sources) else ("partial" if success > 0 or stale > 0 else "failed")
    return {
        "schema_version": "1.0.0",
        "meta": {
            "data_generated_at": generated.isoformat(timespec="seconds"),
            "dashboard_version": "0.2.0-prototype",
            "timezone": "Asia/Seoul",
            "data_mode": "external_json_auto",
            "sources_checked": len(sources),
            "sources_success": success,
            "sources_failed": failed,
            "sources_stale": stale,
            "run_status": run_status,
            "copyright_policy": "Store title, published date, source URL, short summary, classification fields only. Do not store full article text.",
        },
        "last_updated": generated.strftime("%Y-%m-%d %H:%M KST"),
        "sources": sources,
    }


def main() -> None:
    previous = load_previous(OUTPUT_PATH)
    collected_sources = []
    for source in SOURCES:
        previous_source = find_previous_source(previous, source.name)
        collected_sources.append(collect_source(source, previous_source))

    output = build_output(collected_sources)

    # If every source failed and there was a previous JSON, keep previous article data through per-source fallback.
    # The file is still written so the dashboard can show failure/stale status.
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} with run_status={output['meta']['run_status']}")


if __name__ == "__main__":
    main()
