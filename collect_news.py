#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect_news.py v0.4-google-fallback

역할
- 5개 글로벌 축산/사료 전문 매체의 공개 뉴스 메타데이터를 수집해 news-data.json을 갱신합니다.
- 기사 전문은 저장하지 않습니다. 제목, 날짜, 링크, 짧은 snippet/summary, 분류 정보만 저장합니다.
- 직접 RSS/listing 접근이 막히면 Google News RSS fallback을 사용합니다.
- 수집 실패 시 기존 news-data.json 데이터를 유지하고 source status를 stale로 기록합니다.

필요 패키지
pip install requests beautifulsoup4 feedparser python-dateutil
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
OUTPUT_PATH = Path("news-data.json")
DASHBOARD_VERSION = "0.2.0-prototype"
SCHEMA_VERSION = "1.0.0"
MAX_ARTICLES_PER_SOURCE = 8
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_REQUESTS = 1.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
}


@dataclass
class SourceConfig:
    name: str
    source_url: str
    rss_urls: List[str]
    listing_urls: List[str]
    include_domains: List[str]
    google_news_query: str


SOURCES: List[SourceConfig] = [
    SourceConfig(
        name="Feed Strategy",
        source_url="https://www.feedstrategy.com/",
        rss_urls=["https://www.feedstrategy.com/feed/", "https://www.feedstrategy.com/rss"],
        listing_urls=["https://www.feedstrategy.com/", "https://www.feedstrategy.com/latest-news/"],
        include_domains=["feedstrategy.com"],
        google_news_query='site:feedstrategy.com "Feed Strategy" feed poultry swine additive',
    ),
    SourceConfig(
        name="All About Feed",
        source_url="https://www.allaboutfeed.net/",
        rss_urls=[
            "https://www.allaboutfeed.net/feed/",
            "https://www.allaboutfeed.net/rss",
            "https://www.foodagribusiness.world/feed/rss",
        ],
        listing_urls=["https://www.allaboutfeed.net/", "https://www.foodagribusiness.world/feed"],
        include_domains=["allaboutfeed.net", "foodagribusiness.world"],
        google_news_query='site:allaboutfeed.net OR site:foodagribusiness.world/feed "All About Feed" feed',
    ),
    SourceConfig(
        name="Feed & Additive Magazine",
        source_url="https://www.feedandadditive.com/",
        rss_urls=["https://www.feedandadditive.com/feed/"],
        listing_urls=["https://www.feedandadditive.com/", "https://www.feedandadditive.com/news/"],
        include_domains=["feedandadditive.com"],
        google_news_query='site:feedandadditive.com feed additive animal nutrition',
    ),
    SourceConfig(
        name="The Pig Site",
        source_url="https://www.thepigsite.com/",
        rss_urls=["https://www.thepigsite.com/rss", "https://www.thepigsite.com/rss/news"],
        listing_urls=["https://www.thepigsite.com/latest?section=news", "https://www.thepigsite.com/news"],
        include_domains=["thepigsite.com"],
        google_news_query='site:thepigsite.com pig swine pork news',
    ),
    SourceConfig(
        name="The Poultry Site",
        source_url="https://www.thepoultrysite.com/",
        rss_urls=["https://www.thepoultrysite.com/rss", "https://www.thepoultrysite.com/rss/news"],
        listing_urls=["https://www.thepoultrysite.com/latest?section=news", "https://www.thepoultrysite.com/news"],
        include_domains=["thepoultrysite.com"],
        google_news_query='site:thepoultrysite.com poultry broiler layer avian news',
    ),
]


CATEGORY_RULES: List[Tuple[str, List[str]]] = [
    ("Disease / Biosecurity", ["asf", "african swine fever", "hpai", "avian influenza", "biosecurity", "salmonella", "e. coli", "clostridium", "disease", "outbreak", "pathogen"]),
    ("Feed Additives", ["feed additive", "additive", "enzyme", "phytase", "xylanase", "protease", "probiotic", "prebiotic", "postbiotic", "mycotoxin", "emulsifier", "organic acid", "phytogenic", "antioxidant"]),
    ("Animal Nutrition", ["nutrition", "diet", "digestibility", "feed efficiency", "fcr", "growth performance", "weaning", "piglet", "broiler", "layer", "amino acid", "energy", "protein", "mineral", "vitamin"]),
    ("Regulation", ["regulation", "approval", "ban", "policy", "legislation", "compliance", "traceability", "feed safety", "standard", "efsa", "fda", "fao"]),
    ("Market / Trade", ["market", "trade", "export", "import", "price", "commodity", "corn", "soybean", "supply", "demand", "production", "cost"]),
    ("Sustainability", ["sustainability", "carbon", "emission", "methane", "climate", "resource efficiency", "environment", "circular"]),
]

SPECIES_RULES: List[Tuple[str, List[str]]] = [
    ("Swine", ["swine", "pig", "pork", "piglet", "sow", "weaning", "weaned"]),
    ("Poultry", ["poultry", "broiler", "layer", "hen", "avian", "egg", "chicken", "turkey"]),
    ("Ruminant", ["ruminant", "dairy", "cattle", "cow", "calf", "beef", "rumen", "methane"]),
    ("Aquaculture", ["aquaculture", "shrimp", "fish", "salmon", "tilapia"]),
]

PRIORITY_KEYWORDS: Dict[str, int] = {
    "asf": 35, "african swine fever": 35, "hpai": 35, "avian influenza": 35,
    "biosecurity": 25, "outbreak": 25, "feed additive": 20, "enzyme": 18,
    "phytase": 18, "postbiotic": 18, "mycotoxin": 18, "gut health": 20,
    "digestibility": 18, "feed efficiency": 20, "fcr": 20, "weaning": 18,
    "regulation": 18, "approval": 16, "feed safety": 18, "traceability": 16,
    "market": 8, "trade": 8, "sustainability": 8,
}


def now_kst() -> datetime:
    return datetime.now(tz=KST)


def iso_kst(dt: Optional[datetime] = None) -> str:
    return (dt or now_kst()).isoformat(timespec="seconds")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def normalize_url(url: str, base: str = "") -> str:
    if not url:
        return ""
    absolute = urljoin(base, url)
    parsed = urlparse(absolute)
    return parsed._replace(fragment="").geturl()


def domain_allowed(url: str, allowed_domains: List[str]) -> bool:
    host = urlparse(url).netloc.lower()
    return any(domain.lower() in host for domain in allowed_domains)


def parse_date(value: Any) -> str:
    if not value:
        return now_kst().date().isoformat()
    try:
        try:
            dt = parsedate_to_datetime(str(value))
        except Exception:
            dt = dateparser.parse(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST).date().isoformat()
    except Exception:
        return now_kst().date().isoformat()


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]+", "", title.lower())


def article_id(source_name: str, title: str, url: str, published_date: str) -> str:
    raw = f"{source_name}|{title}|{url}|{published_date}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9]+", "-", source_name.lower()).strip("-")
    date_part = (published_date or "unknown").replace("-", "")
    return f"{slug}-{date_part}-{digest}"


def classify_category(text: str) -> str:
    lower = text.lower()
    scores: Dict[str, int] = {}
    for category, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw in lower:
                scores[category] = scores.get(category, 0) + 1
    return max(scores.items(), key=lambda x: x[1])[0] if scores else "Unclassified"


def classify_species(text: str) -> List[str]:
    lower = text.lower()
    species = [label for label, keywords in SPECIES_RULES if any(kw in lower for kw in keywords)]
    return species if species else ["Multi-species"]


def extract_keywords(text: str, limit: int = 8) -> List[str]:
    lower = text.lower()
    candidates: List[str] = []
    for _, kws in CATEGORY_RULES:
        candidates.extend(kws)
    for _, kws in SPECIES_RULES:
        candidates.extend(kws)
    candidates.extend(PRIORITY_KEYWORDS.keys())

    found: List[str] = []
    for kw in sorted(set(candidates), key=len, reverse=True):
        if kw in lower and kw not in found:
            found.append(kw)
    return found[:limit]


def score_priority(text: str, category: str) -> Tuple[str, int]:
    lower = text.lower()
    score = 20
    for kw, weight in PRIORITY_KEYWORDS.items():
        if kw in lower:
            score += weight
    if category in {"Disease / Biosecurity", "Regulation"}:
        score += 15
    elif category == "Feed Additives":
        score += 12
    elif category == "Animal Nutrition":
        score += 8

    score = min(score, 100)
    if score >= 75:
        return "High", score
    if score >= 45:
        return "Medium", score
    return "Low", score


def make_pm_note(category: str, species: List[str], keywords: List[str]) -> str:
    species_text = ", ".join(species)
    kw_text = ", ".join(keywords[:4]) if keywords else "핵심 키워드"

    if category == "Disease / Biosecurity":
        return f"{species_text} 질병·방역 이슈로 생산성 저하, 면역, 장건강, 사료섭취 유지 관련 제품 메시지를 검토할 필요가 있습니다."
    if category == "Feed Additives":
        return f"{kw_text} 관련 첨가제 claim, 작용기전, 적용 축종, 경쟁제품 포지셔닝을 점검할 필요가 있습니다."
    if category == "Animal Nutrition":
        return f"{species_text} 영양 전략과 성장성, FCR, 소화율, nutrient utilization claim 연결 가능성을 검토할 필요가 있습니다."
    if category == "Regulation":
        return "규제·품질·안전 관련 이슈이므로 제품 등록, 표시 claim, 원료 추적성, 고객 커뮤니케이션 리스크를 점검할 필요가 있습니다."
    if category == "Market / Trade":
        return "원료 가격, 수급, 수출입 변화가 고객사의 원가 관리와 생산성 개선 니즈로 이어질 수 있습니다."
    if category == "Sustainability":
        return "지속가능성 claim은 FCR, 배출 저감, 자원 효율 등 측정 가능한 지표와 연결해 검토할 필요가 있습니다."
    return "PM 관점에서 제품 적용 가능성, 고객 영향도, 기술마케팅 활용 가능성을 추가 검토할 필요가 있습니다."


def summarize_text(title: str, snippet: str) -> str:
    snippet = clean_text(snippet)
    if snippet:
        return snippet[:260].rstrip()
    return f"Public metadata indicates this article is related to: {title[:180].rstrip()}."


def build_article(source_name: str, title: str, url: str, published_date: str, snippet: str, collected_at: str, collection_method: str) -> Dict[str, Any]:
    title = clean_text(title)
    snippet = clean_text(snippet)
    full_text = f"{title} {snippet}"
    category = classify_category(full_text)
    species = classify_species(full_text)
    keywords = extract_keywords(full_text)
    priority, priority_score = score_priority(full_text, category)

    return {
        "id": article_id(source_name, title, url, published_date),
        "source": source_name,
        "title": title,
        "url": url,
        "published_date": published_date,
        "collected_at": collected_at,
        "summary": summarize_text(title, snippet),
        "category": category,
        "species": species,
        "priority": priority,
        "priority_score": priority_score,
        "keywords": keywords,
        "why_it_matters": make_pm_note(category, species, keywords),
        "collection_method": collection_method,
    }


def dedupe_articles(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen_urls, seen_titles, unique = set(), set(), []
    for article in articles:
        url = article.get("url", "").strip()
        tkey = title_key(article.get("title", ""))
        if url in seen_urls or (tkey and tkey in seen_titles):
            continue
        seen_urls.add(url)
        if tkey:
            seen_titles.add(tkey)
        unique.append(article)

    unique.sort(key=lambda x: (x.get("published_date", ""), int(x.get("priority_score", 0))), reverse=True)
    return unique


def request_url(url: str) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response


def collect_from_rss(config: SourceConfig, collected_at: str) -> List[Dict[str, Any]]:
    errors: List[str] = []
    for rss_url in config.rss_urls:
        try:
            print(f"[{config.name}] Trying RSS: {rss_url}")
            response = request_url(rss_url)
            feed = feedparser.parse(response.content)
            articles: List[Dict[str, Any]] = []

            for entry in feed.entries[: MAX_ARTICLES_PER_SOURCE * 3]:
                title = clean_text(entry.get("title", ""))
                url = normalize_url(entry.get("link", ""), config.source_url)
                if not title or not url or not domain_allowed(url, config.include_domains):
                    continue

                published_date = parse_date(entry.get("published") or entry.get("updated") or entry.get("created"))
                snippet = clean_text(entry.get("summary") or entry.get("description") or "")
                articles.append(build_article(config.name, title, url, published_date, snippet, collected_at, "rss"))

            articles = dedupe_articles(articles)
            if articles:
                return articles[:MAX_ARTICLES_PER_SOURCE]
            errors.append(f"{rss_url}: no entries")
        except Exception as exc:
            errors.append(f"{rss_url}: {exc}")
    raise RuntimeError("rss failed | " + " | ".join(errors))


def collect_from_listing(config: SourceConfig, collected_at: str) -> List[Dict[str, Any]]:
    errors: List[str] = []
    for listing_url in config.listing_urls:
        try:
            print(f"[{config.name}] Trying listing: {listing_url}")
            response = request_url(listing_url)
            soup = BeautifulSoup(response.text, "html.parser")
            articles: List[Dict[str, Any]] = []

            for a in soup.find_all("a", href=True):
                title = clean_text(a.get_text(" ", strip=True))
                url = normalize_url(a["href"], listing_url)

                if len(title) < 18:
                    continue
                if not domain_allowed(url, config.include_domains):
                    continue
                if any(skip in url.lower() for skip in ["/tag/", "/category/", "/author/", "/about", "/contact", "/advert", "/subscribe", "/privacy", "/terms", "#"]):
                    continue

                articles.append(build_article(config.name, title, url, now_kst().date().isoformat(), title, collected_at, "listing"))

            articles = dedupe_articles(articles)
            if articles:
                return articles[:MAX_ARTICLES_PER_SOURCE]
            errors.append(f"{listing_url}: no article-like links")
        except Exception as exc:
            errors.append(f"{listing_url}: {exc}")
    raise RuntimeError("listing failed | " + " | ".join(errors))


def google_news_rss_url(query: str) -> str:
    encoded = quote_plus(query)
    return f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"


def collect_from_google_news(config: SourceConfig, collected_at: str) -> List[Dict[str, Any]]:
    # IMPORTANT:
    # This is a fallback for metadata only. It does not scrape or store article full text.
    url = google_news_rss_url(config.google_news_query)
    print(f"[{config.name}] Trying google_news_rss_fallback: {url}")
    feed = feedparser.parse(url)
    articles: List[Dict[str, Any]] = []

    for entry in feed.entries[: MAX_ARTICLES_PER_SOURCE * 4]:
        title = clean_text(entry.get("title", ""))
        link = normalize_url(entry.get("link", ""), "https://news.google.com/")
        if not title or not link:
            continue

        published_date = parse_date(entry.get("published") or entry.get("updated"))
        snippet = clean_text(entry.get("summary") or "")
        text_for_filter = f"{title} {snippet}".lower()

        # Relevance guardrail for Google News fallback.
        if config.name == "Feed Strategy":
            if not any(term in text_for_filter for term in ["feed", "poultry", "swine", "livestock", "additive"]):
                continue
        elif config.name == "All About Feed":
            if not any(term in text_for_filter for term in ["feed", "poultry", "swine", "livestock", "nutrition"]):
                continue

        articles.append(build_article(config.name, title, link, published_date, snippet, collected_at, "google_news_rss_fallback"))

    articles = dedupe_articles(articles)
    if not articles:
        raise RuntimeError("google_news_rss_fallback failed: no relevant entries")
    return articles[:MAX_ARTICLES_PER_SOURCE]


def load_previous_json() -> Dict[str, Any]:
    if not OUTPUT_PATH.exists():
        return {}
    try:
        return json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_previous_source(previous: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    for source in previous.get("sources", []) or []:
        if source.get("name") == name:
            return source
    return None


def collect_source(config: SourceConfig, previous: Dict[str, Any], collected_at: str) -> Dict[str, Any]:
    errors: List[str] = []

    for method_name, collector in [
        ("rss", collect_from_rss),
        ("listing", collect_from_listing),
        ("google_news_rss_fallback", collect_from_google_news),
    ]:
        try:
            articles = collector(config, collected_at)
            return {
                "name": config.name,
                "source_url": config.source_url,
                "status": "success",
                "last_success_at": collected_at,
                "last_attempt_at": collected_at,
                "collection_method": method_name,
                "error_message": "",
                "articles": articles,
            }
        except Exception as exc:
            errors.append(str(exc))
            time.sleep(SLEEP_BETWEEN_REQUESTS)

    error_message = " | ".join(errors)
    print(f"[{config.name}] FAILED. {error_message}", file=sys.stderr)

    previous_source = get_previous_source(previous, config.name)
    if previous_source and previous_source.get("articles"):
        fallback = dict(previous_source)
        fallback.update({
            "name": config.name,
            "source_url": config.source_url,
            "status": "stale",
            "last_attempt_at": collected_at,
            "collection_method": "previous_json_fallback",
            "error_message": error_message,
        })
        return fallback

    return {
        "name": config.name,
        "source_url": config.source_url,
        "status": "failed",
        "last_success_at": "",
        "last_attempt_at": collected_at,
        "collection_method": "none",
        "error_message": error_message,
        "articles": [],
    }


def save_json(data: Dict[str, Any]) -> None:
    OUTPUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_output(sources: List[Dict[str, Any]], generated_at: str) -> Dict[str, Any]:
    success_count = sum(1 for s in sources if s.get("status") == "success")
    failed_count = sum(1 for s in sources if s.get("status") == "failed")
    stale_count = sum(1 for s in sources if s.get("status") == "stale")

    if success_count == len(sources):
        run_status = "success"
    elif success_count > 0:
        run_status = "partial"
    else:
        run_status = "failed"

    generated_dt = dateparser.parse(generated_at)
    last_updated = generated_dt.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")

    return {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "data_generated_at": generated_at,
            "dashboard_version": DASHBOARD_VERSION,
            "timezone": "Asia/Seoul",
            "data_mode": "external_json_auto",
            "sources_checked": len(sources),
            "sources_success": success_count,
            "sources_failed": failed_count,
            "sources_stale": stale_count,
            "run_status": run_status,
        },
        "last_updated": last_updated,
        "sources": sources,
    }


def main() -> int:
    generated_at = iso_kst()
    previous = load_previous_json()
    sources_output: List[Dict[str, Any]] = []

    print(f"Starting news collection at {generated_at}")

    for config in SOURCES:
        print(f"--- Collecting source: {config.name} ---")
        source_result = collect_source(config, previous, generated_at)
        print(
            f"[{config.name}] status={source_result.get('status')} "
            f"method={source_result.get('collection_method')} "
            f"articles={len(source_result.get('articles', []))}"
        )
        if source_result.get("error_message"):
            print(f"[{config.name}] error_message={source_result.get('error_message')}")
        sources_output.append(source_result)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    output = build_output(sources_output, generated_at)
    save_json(output)

    print(
        f"Wrote {OUTPUT_PATH.resolve()} with "
        f"run_status={output['meta']['run_status']} "
        f"sources_success={output['meta']['sources_success']}/{output['meta']['sources_checked']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
