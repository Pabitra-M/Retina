"""Common item schema + helpers shared by every source fetcher."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from dateutil import parser as dateparser

_ARXIV_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
_WS_RE = re.compile(r"\s+")
_NONWORD_RE = re.compile(r"[^a-z0-9 ]+")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_date(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = dateparser.parse(str(value))
    except (ValueError, OverflowError, TypeError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for exact dup matching."""
    t = _NONWORD_RE.sub(" ", (title or "").lower())
    return _WS_RE.sub(" ", t).strip()


def extract_arxiv_id(*values: str) -> str | None:
    for value in values:
        if not value:
            continue
        m = _ARXIV_RE.search(str(value))
        if m:
            return m.group(1)
    return None


def clean_text(text: str, limit: int = 4000) -> str:
    text = _WS_RE.sub(" ", (text or "").replace("\n", " ")).strip()
    return text[:limit]


@dataclass
class Item:
    source: str                       # arxiv | huggingface_daily | github | hackernews | rss | paperswithcode
    source_type: str                  # paper | repo | news
    title: str
    url: str
    published_at: str | None = None   # ISO8601
    abstract: str = ""
    authors: str = ""
    arxiv_id: str | None = None
    doi: str | None = None
    pdf_url: str | None = None
    code_url: str | None = None
    project_url: str | None = None
    dataset_url: str | None = None
    engagement: int = 0               # stars / upvotes / points, normalized meaning per source
    fetched_at: str = field(default_factory=lambda: iso(now_utc()))
    # filled downstream
    id: str = ""
    title_norm: str = ""
    prelim_score: float = 0.0

    def finalize(self) -> "Item":
        self.title = clean_text(self.title, 400)
        self.abstract = clean_text(self.abstract, 6000)
        self.title_norm = normalize_title(self.title)
        if not self.arxiv_id:
            self.arxiv_id = extract_arxiv_id(self.url, self.pdf_url or "")
        self.id = self._make_id()
        return self

    def _make_id(self) -> str:
        if self.arxiv_id:
            basis = f"arxiv:{self.arxiv_id}"
        elif self.doi:
            basis = f"doi:{self.doi.lower()}"
        else:
            basis = f"{self.source}:{self.url.split('?')[0].rstrip('/').lower()}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]

    def as_dict(self) -> dict:
        return asdict(self)
