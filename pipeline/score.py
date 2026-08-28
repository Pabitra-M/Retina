"""No-LLM heuristic pre-ranking. Cheap filter to pick the shortlist for the LLM stage."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import config
from .normalize import Item, parse_date

log = logging.getLogger("score")


def _keyword_hits(text: str, terms: list[str]) -> int:
    return sum(1 for t in terms if t and t in text)


def _recency_bonus(published_at: str | None) -> float:
    dt = parse_date(published_at)
    if not dt:
        return 0.0
    age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    if age_h <= 24:
        return 4.0
    if age_h <= 168:
        return 2.0
    if age_h <= 720:
        return 0.5
    return 0.0


def _engagement_bonus(item: Item) -> float:
    e = item.engagement
    if e <= 0:
        return 0.0
    if item.source == "github":
        return min(e / 200.0, 4.0)
    if item.source == "huggingface_daily":
        return min(e / 15.0, 4.0)
    if item.source == "hackernews":
        return min(e / 100.0, 3.0)
    return 0.0


def score_items(items: list[Item]) -> list[Item]:
    kw = config.keywords()
    p1 = kw["priority1_computer_vision"]
    p2 = kw["priority2_general_ai"]
    authority = kw["source_authority"]

    for item in items:
        if item.prelim_score == -1:   # already-known items keep their sentinel
            continue
        blob = f"{item.title}\n{item.abstract}".lower()
        p1_hits = _keyword_hits(blob, p1["terms"])
        p2_hits = _keyword_hits(blob, p2["terms"])

        score = 0.0
        score += min(p1_hits, 4) * (p1["weight"] / 2.0)
        score += min(p2_hits, 4) * (p2["weight"] / 3.0)
        score += _recency_bonus(item.published_at)
        score += _engagement_bonus(item)
        score += authority.get(item.source, 1.0)
        if item.code_url or item.source == "github":
            score += 1.5
        if p1_hits == 0 and p2_hits == 0:
            score -= 3.0            # off-topic

        item.prelim_score = round(score, 2)

    ranked = sorted(items, key=lambda i: i.prelim_score, reverse=True)
    log.info("scored %d items; top prelim=%.1f", len(ranked),
             ranked[0].prelim_score if ranked else 0.0)
    return ranked


def shortlist(items: list[Item]) -> list[Item]:
    n = config.settings()["shortlist"]["size"]
    ranked = score_items(items)
    picks = [it for it in ranked if it.prelim_score != -1][:n]
    log.info("shortlist: %d items for LLM scoring", len(picks))
    return picks
