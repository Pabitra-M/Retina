"""Source collectors. Every fetcher is best-effort: failures are logged, never fatal."""
from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from . import config
from .normalize import Item, clean_text, iso

log = logging.getLogger("collect")

USER_AGENT = "ai-research-daily/1.0 (+https://github.com/; research digest bot)"
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": USER_AGENT})


def _get(url: str, timeout: int, **kw) -> requests.Response:
    r = _SESSION.get(url, timeout=timeout, **kw)
    r.raise_for_status()
    return r


# --------------------------------------------------------------------------- arXiv
_ARXIV_NS = {"a": "http://www.w3.org/2005/Atom"}


def fetch_arxiv(cfg: dict, timeout: int) -> list[Item]:
    cats = cfg.get("categories", ["cs.CV"])
    max_results = config.settings()["collect"]["arxiv_max_results"]
    search = "+OR+".join(f"cat:{c}" for c in cats)
    url = (
        "http://export.arxiv.org/api/query"
        f"?search_query={search}"
        f"&sortBy=submittedDate&sortOrder=descending&max_results={max_results}"
    )
    r = _get(url, timeout)
    root = ET.fromstring(r.text)
    items: list[Item] = []
    for entry in root.findall("a:entry", _ARXIV_NS):
        def text(tag: str) -> str:
            el = entry.find(f"a:{tag}", _ARXIV_NS)
            return (el.text or "").strip() if el is not None else ""

        abs_url = text("id")
        authors = ", ".join(
            (a.findtext("a:name", default="", namespaces=_ARXIV_NS) or "").strip()
            for a in entry.findall("a:author", _ARXIV_NS)
        )
        pdf_url = None
        code_url = None
        for link in entry.findall("a:link", _ARXIV_NS):
            if link.get("title") == "pdf":
                pdf_url = link.get("href")
        doi_el = entry.find("{http://arxiv.org/schemas/atom}doi")
        items.append(Item(
            source="arxiv",
            source_type="paper",
            title=text("title"),
            url=abs_url,
            published_at=iso(_parse(text("published"))),
            abstract=text("summary"),
            authors=authors,
            pdf_url=pdf_url,
            code_url=code_url,
            doi=doi_el.text.strip() if doi_el is not None and doi_el.text else None,
        ))
    log.info("arxiv: %d entries", len(items))
    return items


# ------------------------------------------------------------- Hugging Face daily
def fetch_hf_daily(cfg: dict, timeout: int) -> list[Item]:
    headers = {}
    token = config.env("HF_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = _SESSION.get(cfg["url"], timeout=timeout, headers=headers)
    r.raise_for_status()
    data = r.json()
    items: list[Item] = []
    for row in data:
        paper = row.get("paper", row)
        arxiv_id = paper.get("id") or row.get("arxivId")
        title = paper.get("title") or row.get("title") or ""
        summary = paper.get("summary") or row.get("summary") or ""
        upvotes = int(paper.get("upvotes") or row.get("upvotes") or 0)
        authors = ", ".join(
            a.get("name", "") for a in (paper.get("authors") or []) if a.get("name")
        )
        url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else row.get("url", "")
        if not url:
            continue
        items.append(Item(
            source="huggingface_daily",
            source_type="paper",
            title=title,
            url=url,
            published_at=iso(_parse(row.get("publishedAt") or paper.get("publishedAt"))),
            abstract=summary,
            authors=authors,
            arxiv_id=arxiv_id,
            pdf_url=f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else None,
            engagement=upvotes,
        ))
    log.info("huggingface_daily: %d papers", len(items))
    return items


# ------------------------------------------------------------- Papers With Code
def fetch_paperswithcode(cfg: dict, timeout: int) -> list[Item]:
    r = _SESSION.get(cfg["trending_url"], timeout=timeout,
                     headers={"Accept": "application/json"},
                     params={"ordering": "-id", "items_per_page": 50})
    r.raise_for_status()
    data = r.json()
    items: list[Item] = []
    for row in data.get("results", []):
        title = row.get("title") or ""
        url = row.get("url_abs") or row.get("url_pdf") or ""
        if not url:
            continue
        items.append(Item(
            source="paperswithcode",
            source_type="paper",
            title=title,
            url=url,
            published_at=iso(_parse(row.get("published"))),
            abstract=row.get("abstract") or "",
            authors=", ".join(row.get("authors") or []),
            arxiv_id=row.get("arxiv_id"),
            pdf_url=row.get("url_pdf"),
        ))
    log.info("paperswithcode: %d papers", len(items))
    return items


# ------------------------------------------------------------------------ GitHub
def fetch_github(cfg: dict, timeout: int) -> list[Item]:
    token = config.env("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    min_stars = config.settings()["collect"]["github_min_stars"]
    since = (datetime.now(timezone.utc) - timedelta(days=cfg["created_within_days"])).date()
    items: list[Item] = []
    seen: set[str] = set()
    for topic in cfg.get("topics", []):
        q = f"topic:{topic} created:>={since} stars:>={min_stars}"
        try:
            r = _SESSION.get(
                "https://api.github.com/search/repositories",
                params={"q": q, "sort": "stars", "order": "desc", "per_page": 15},
                headers=headers, timeout=timeout,
            )
            r.raise_for_status()
        except requests.RequestException as exc:
            log.warning("github topic %s failed: %s", topic, exc)
            continue
        for repo in r.json().get("items", []):
            full = repo["full_name"]
            if full in seen:
                continue
            seen.add(full)
            items.append(Item(
                source="github",
                source_type="repo",
                title=f"{repo['name']} — {repo.get('description') or 'GitHub repository'}",
                url=repo["html_url"],
                published_at=iso(_parse(repo.get("created_at"))),
                abstract=clean_text(repo.get("description") or ""),
                authors=repo["owner"]["login"],
                code_url=repo["html_url"],
                project_url=repo.get("homepage") or None,
                engagement=int(repo.get("stargazers_count") or 0),
            ))
        time.sleep(1)
    log.info("github: %d repos", len(items))
    return items


# -------------------------------------------------------------------- Hacker News
def fetch_hackernews(cfg: dict, timeout: int) -> list[Item]:
    min_points = config.settings()["collect"]["hn_min_points"]
    since = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
    items: list[Item] = []
    seen: set[str] = set()
    for query in cfg.get("queries", []):
        try:
            r = _get(
                "https://hn.algolia.com/api/v1/search_by_date", timeout,
                params={
                    "query": query, "tags": "story",
                    "numericFilters": f"points>{min_points},created_at_i>{since}",
                    "hitsPerPage": 10,
                },
            )
        except requests.RequestException as exc:
            log.warning("hn query %r failed: %s", query, exc)
            continue
        for hit in r.json().get("hits", []):
            url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}"
            if url in seen:
                continue
            seen.add(url)
            items.append(Item(
                source="hackernews",
                source_type="news",
                title=hit.get("title") or "",
                url=url,
                published_at=iso(_parse(hit.get("created_at"))),
                abstract=clean_text(hit.get("story_text") or hit.get("title") or ""),
                authors=hit.get("author") or "",
                project_url=f"https://news.ycombinator.com/item?id={hit['objectID']}",
                engagement=int(hit.get("points") or 0),
            ))
    log.info("hackernews: %d stories", len(items))
    return items


# --------------------------------------------------------------------------- RSS
def fetch_rss(cfg: dict, timeout: int) -> list[Item]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=10)
    items: list[Item] = []
    for feed in cfg.get("feeds", []):
        try:
            raw = _get(feed["url"], timeout).content
        except requests.RequestException as exc:
            log.warning("rss %s failed: %s", feed["name"], exc)
            continue
        parsed = feedparser.parse(raw)
        for entry in parsed.entries[:15]:
            published = _parse(entry.get("published") or entry.get("updated"))
            if published and published < cutoff:
                continue
            summary = entry.get("summary") or ""
            items.append(Item(
                source="rss",
                source_type="news",
                title=entry.get("title") or "",
                url=entry.get("link") or "",
                published_at=iso(published),
                abstract=clean_text(_strip_html(summary)),
                authors=feed["name"],
            ))
    log.info("rss: %d posts", len(items))
    return items


def _strip_html(text: str) -> str:
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(text, "lxml").get_text(" ")
    except Exception:
        return text


def _parse(value) -> datetime | None:
    from .normalize import parse_date
    return parse_date(value)


_FETCHERS = {
    "arxiv": fetch_arxiv,
    "huggingface_daily": fetch_hf_daily,
    "paperswithcode": fetch_paperswithcode,
    "github": fetch_github,
    "hackernews": fetch_hackernews,
    "rss": fetch_rss,
}


def collect_all() -> list[Item]:
    src_cfg = config.sources()
    timeout = config.settings()["collect"]["per_source_timeout_sec"]
    all_items: list[Item] = []
    for name, fetcher in _FETCHERS.items():
        cfg = src_cfg.get(name, {})
        if not cfg.get("enabled", False):
            continue
        for attempt in range(3):
            try:
                all_items.extend(fetcher(cfg, timeout))
                break
            except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
                wait = 2 ** attempt
                log.warning("%s failed (attempt %d/3): %s", name, attempt + 1, exc)
                time.sleep(wait)
        else:
            log.error("%s skipped for this run after 3 failures", name)
        if name == "arxiv":
            time.sleep(3)  # be polite
    finalized = [it.finalize() for it in all_items if it.title and it.url]
    log.info("collected %d raw items", len(finalized))
    return finalized
