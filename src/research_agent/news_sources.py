"""Bounded public-news collection with explicit coverage and provenance."""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup
from pydantic import Field

from .models import StrictModel


class Article(StrictModel):
    id: str
    company: str
    title: str
    url: str
    published_at: str
    source: str
    level: str
    text: str


class Coverage(StrictModel):
    source: str
    company: str
    status: str
    recent_items: int = 0
    detail: str = ""


class Collection(StrictModel):
    articles: list[Article] = Field(default_factory=list)
    coverage: list[Coverage] = Field(default_factory=list)


def clean_html(value: str) -> str:
    soup = BeautifulSoup(value, "html.parser")
    for node in soup(["script", "style", "nav", "footer", "header"]):
        node.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def safe_url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme not in {"https", "http"}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError("invalid_news_url")
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.startswith("utm_")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def stamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        result = parsedate_to_datetime(value)
    if result.tzinfo is None:
        raise ValueError("news_date_missing_timezone")
    return result.astimezone(UTC)


def article(company, title, url, published, source, level, body):
    url = safe_url(url)
    title = clean_html(title)
    if not title:
        raise ValueError("news_title_missing")
    return Article(
        id=hashlib.sha256(url.encode()).hexdigest()[:24],
        company=company,
        title=title[:500],
        url=url,
        published_at=stamp(published).isoformat(),
        source=source,
        level=level,
        text=(title + "\n" + clean_html(body))[:12000],
    )


def parse_rss(text: str, company: str, source: str) -> list[Article]:
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("unsafe_xml")
    root = ElementTree.fromstring(text)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("rss_channel_missing")
    items = []
    nodes = channel.findall("item")
    for node in nodes[:150]:
        try:
            origin = node.findtext("source") or source
            items.append(
                article(
                    company,
                    node.findtext("title") or "",
                    node.findtext("link") or "",
                    node.findtext("pubDate") or "",
                    origin,
                    "标题与订阅摘要",
                    node.findtext("description") or "",
                )
            )
        except (ValueError, TypeError, OverflowError):
            continue
    if nodes and not items:
        raise ValueError("rss_items_unparseable")
    return items


def relevant(item: Article) -> bool:
    title = item.title.rsplit(" - ", 1)[0].casefold()
    aliases = (
        ("nvidia", "nvda", "英伟达", "英偉達")
        if item.company == "英伟达"
        else ("腾讯", "騰訊", "tencent", "微信", "wechat", "混元", "hunyuan")
    )
    return any(alias in title for alias in aliases)


def importance(item: Article) -> tuple:
    official = item.source in {"腾讯官网", "NVIDIA 官网"}
    text = item.title.casefold()
    business = sum(
        term in text
        for term in (
            "earnings",
            "revenue",
            "export",
            "profit",
            "regulat",
            "财报",
            "业绩",
            "营收",
            "回购",
            "出口",
            "监管",
            "盈利",
            "合作",
            "partnership",
        )
    )
    established = any(
        term in item.source.casefold()
        for term in ("reuters", "bloomberg", "cnbc", "financial times", "财新", "证券", "第一财经")
    )
    return official, established, business, item.published_at


def parse_tencent(text: str) -> list[Article]:
    soup = BeautifulSoup(text, "html.parser")
    cards = soup.select("article.tc-blog-grid")
    if not cards:
        raise ValueError("tencent_page_structure_changed")
    output = []
    for card in cards:
        title = card.select_one("h2 a")
        date = card.select_one(".tc-blogpost-date")
        if not title or not date:
            continue
        parts = re.findall(r"\d+", date.get_text())
        if len(parts) != 3:
            continue
        published = f"{int(parts[0]):04}-{int(parts[1]):02}-{int(parts[2]):02}T00:00:00+08:00"
        output.append(
            article(
                "腾讯",
                title.get_text(),
                title.get("href", ""),
                published,
                "腾讯官网",
                "官方标题；日期精度为天",
                "",
            )
        )
    if not output:
        raise ValueError("tencent_cards_unparseable")
    return output


async def fetch(client: httpx.AsyncClient, url: str) -> str:
    # Only configured public endpoints and same-host redirects are fetched.
    original = urlsplit(url).hostname
    for _ in range(4):
        async with client.stream("GET", url) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("location", ""))
                if urlsplit(url).hostname != original or urlsplit(url).scheme != "https":
                    raise ValueError("cross_host_redirect")
                continue
            response.raise_for_status()
            data = bytearray()
            async for block in response.aiter_bytes():
                data.extend(block)
                if len(data) > 3_000_000:
                    raise ValueError("source_too_large")
            return bytes(data).decode("utf-8-sig", errors="replace")
    raise ValueError("too_many_redirects")


FEEDS = [
    ("英伟达", "NVIDIA 官网", "https://nvidianews.nvidia.com/releases.xml"),
    ("腾讯", "腾讯官网", "https://www.tencent.com/zh-cn/newsroom/all-news/"),
    (
        "英伟达",
        "Google News / NVIDIA",
        "https://news.google.com/rss/search?"
        + urlencode({"q": "(NVIDIA OR NVDA) when:2d", "hl": "en-US", "gl": "US", "ceid": "US:en"}),
    ),
    (
        "腾讯",
        "Google News / 腾讯",
        "https://news.google.com/rss/search?"
        + urlencode(
            {"q": "(腾讯 OR Tencent) when:2d", "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"}
        ),
    ),
]


async def collect(
    now: datetime,
    hours: int = 36,
    limit: int = 8,
    client: httpx.AsyncClient | None = None,
    exclude_ids: set[str] | None = None,
) -> Collection:
    owned = client is None
    client = client or httpx.AsyncClient(timeout=25, headers={"User-Agent": "ChenxiNews/0.3"})
    start = now - timedelta(hours=hours)
    exclude_ids = exclude_ids or set()

    async def one(company, source, url):
        try:
            async with asyncio.timeout(40):
                text = await fetch(client, url)
            items = (
                parse_tencent(text) if source == "腾讯官网" else parse_rss(text, company, source)
            )
            recent = [a for a in items if start <= stamp(a.published_at) <= now]
            if source.startswith("Google News"):
                recent = [a for a in recent if relevant(a)]
            return recent, Coverage(
                source=source, company=company, status="ok", recent_items=len(recent)
            )
        except (httpx.HTTPError, TimeoutError, ValueError, ElementTree.ParseError) as exc:
            detail = (
                f"http_{exc.response.status_code}"
                if isinstance(exc, httpx.HTTPStatusError)
                else type(exc).__name__
            )
            return [], Coverage(source=source, company=company, status="failed", detail=detail)

    try:
        results = await asyncio.gather(*(one(*feed) for feed in FEEDS))
        output = Collection(coverage=[coverage for _, coverage in results])
        seen = set()
        for company in ("英伟达", "腾讯"):
            candidates = [a for rows, _ in results for a in rows if a.company == company]
            candidates.sort(key=importance, reverse=True)
            for a in candidates:
                if a.id in exclude_ids:
                    continue
                key = re.sub(r"\W", "", a.title.casefold())
                if a.id in seen or key in seen:
                    continue
                seen.update((a.id, key))
                output.articles.append(a)
                if sum(b.company == company for b in output.articles) >= limit:
                    break
        semaphore = asyncio.Semaphore(3)

        async def enrich(a):
            host = urlsplit(a.url).hostname
            if host not in {"www.tencent.com", "nvidianews.nvidia.com"}:
                return
            try:
                async with semaphore, asyncio.timeout(30):
                    soup = BeautifulSoup(await fetch(client, a.url), "html.parser")
                body = soup.select_one(".entry-content, .body, .article-content, .post-content")
                if body and len(body.get_text(strip=True)) > 150:
                    a.text = (a.title + "\n" + clean_html(str(body)))[:12000]
                    a.level = "官方正文（最多12000字符）"
            except (httpx.HTTPError, TimeoutError, ValueError):
                a.level += "；正文获取失败"

        await asyncio.gather(*(enrich(a) for a in output.articles))
        return output
    finally:
        if owned:
            await client.aclose()
