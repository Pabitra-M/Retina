"""Duplicate detection: exact id -> normalized title -> fuzzy -> (optional) embedding.

Returns the list of items to keep, and a map of item.id -> existing archive row id
for items that already exist (so the caller can update-in-place instead of inserting).
"""
from __future__ import annotations

import logging

from rapidfuzz import fuzz

from . import config, db
from .normalize import Item

log = logging.getLogger("dedupe")


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def deduplicate(items: list[Item], conn, embedder=None) -> tuple[list[Item], dict[str, str]]:
    cfg = config.settings()["dedupe"]
    fuzzy_threshold = cfg["fuzzy_title_threshold"]
    cos_threshold = cfg["embedding_cosine_threshold"]
    lookback = cfg["compare_against_last_days"]

    archive_titles = db.recent_titles(conn, lookback)

    kept: list[Item] = []
    existing_map: dict[str, str] = {}
    batch_seen: dict[str, Item] = {}          # title_norm -> item kept so far
    batch_ids: set[str] = set()

    for item in sorted(items, key=lambda i: i.engagement, reverse=True):
        # 1. exact id / arxiv / doi against archive
        if item.id in batch_ids:
            continue
        archived = db.get(conn, item.id)
        if archived is None and item.arxiv_id:
            archived = db.find_by_arxiv(conn, item.arxiv_id)
        if archived is None and item.title_norm:
            archived = db.find_by_title_norm(conn, item.title_norm)
        if archived is not None:
            existing_map[item.id] = archived["id"]
            batch_ids.add(item.id)
            kept.append(item)      # keep so links/engagement can be merged; not re-summarized
            item.prelim_score = -1  # sentinel: already-known, skip LLM unless re-selected
            continue

        # 2. exact normalized-title dup within this batch
        if item.title_norm and item.title_norm in batch_seen:
            continue

        # 3. fuzzy title vs batch + archive
        is_dup = False
        for other in list(batch_seen.values()):
            if fuzz.token_set_ratio(item.title_norm, other.title_norm) >= fuzzy_threshold:
                is_dup = True
                break
        if not is_dup:
            for row in archive_titles:
                if fuzz.token_set_ratio(item.title_norm, row["title_norm"] or "") >= fuzzy_threshold:
                    existing_map[item.id] = row["id"]
                    is_dup = True
                    break
        if is_dup:
            continue

        batch_seen[item.title_norm] = item
        batch_ids.add(item.id)
        kept.append(item)

    # 4. optional semantic pass over the fresh (not-yet-known) items.
    #    Capped: `kept` is already sorted by engagement desc, so take the most-notable few.
    cap = config.settings()["gemini"].get("semantic_dedupe_max", 40)
    fresh = [it for it in kept if it.prelim_score != -1][:cap]
    if embedder is not None and len(fresh) > 1:
        try:
            vectors = embedder([f"{it.title}. {it.abstract[:500]}" for it in fresh])
            drop: set[str] = set()
            for i in range(len(fresh)):
                if fresh[i].id in drop:
                    continue
                for j in range(i + 1, len(fresh)):
                    if fresh[j].id in drop:
                        continue
                    if _cosine(vectors[i], vectors[j]) >= cos_threshold:
                        drop.add(fresh[j].id)
            if drop:
                log.info("semantic dedupe dropped %d near-duplicates", len(drop))
            kept = [it for it in kept if it.id not in drop]
        except Exception as exc:  # noqa: BLE001
            log.warning("semantic dedupe skipped: %s", exc)

    log.info("dedupe: %d -> %d items (%d already in archive)",
             len(items), len(kept), len(existing_map))
    return kept, existing_map
