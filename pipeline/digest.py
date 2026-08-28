"""Select the day's items, apply freshness widening, or fall back to 'revisit' mode."""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

from . import config, db, summarize
from .collect import _SESSION
from .normalize import Item, iso, parse_date

log = logging.getLogger("digest")

_CV_CATEGORIES = {"computer-vision"}


def _within(row: dict, hours: int) -> bool:
    dt = parse_date(row.get("published_at"))
    if not dt:
        return hours >= 720  # undated: only count in the widest window
    return (datetime.now(timezone.utc) - dt) <= timedelta(hours=hours)


def _summarize_shortlist(conn, items: list[Item]) -> list[dict]:
    rows: list[dict] = []
    for item in items:
        if item.prelim_score == -1:
            continue
        try:
            data = summarize.summarize_item(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("summarize failed for %s: %s", item.title[:60], exc)
            continue
        row = summarize.apply_summary(item, data)
        db.upsert_item(conn, row)
        merged = dict(db.get(conn, item.id))
        merged["keywords"] = data.get("keywords", [])
        rows.append(merged)
    conn.commit()
    log.info("summarized %d items", len(rows))
    return rows


def _select(rows: list[dict]) -> tuple[dict | None, list[dict], list[dict], list[dict]]:
    sel_cfg = config.settings()["select"]
    threshold = sel_cfg["min_overall_score"]
    windows = config.settings()["freshness"]["windows_hours"]
    min_items = sel_cfg["min_items_for_normal_mode"]

    qualified = [r for r in rows if r.get("worth_including") and
                 float(r.get("overall_score") or 0) >= threshold]

    chosen: list[dict] = []
    for hours in windows:
        chosen = [r for r in qualified if _within(r, hours)]
        if len(chosen) >= min_items:
            log.info("freshness window %dh -> %d items", hours, len(chosen))
            break

    if len(chosen) < min_items:
        return None, [], [], chosen  # signal: not enough

    chosen.sort(key=lambda r: (r.get("category") in _CV_CATEGORIES,
                               float(r.get("overall_score") or 0)), reverse=True)
    hero = chosen[0]
    rest = chosen[1:]
    cv = [r for r in rest if r.get("category") in _CV_CATEGORIES][:sel_cfg["max_computer_vision"]]
    other = [r for r in rest if r.get("category") not in _CV_CATEGORIES][:sel_cfg["max_other"]]
    return hero, cv, other, chosen


def _stamp_featured(conn, rows: list[dict], date: str) -> None:
    for r in rows:
        conn.execute("UPDATE items SET featured_date = ? WHERE id = ?", (date, r["id"]))
    conn.commit()


# --------------------------------------------------------------- revisit fallback
_ARXIV_NS = {"a": "http://www.w3.org/2005/Atom"}


def _fetch_revisit_candidate(conn) -> Item | None:
    cfg = config.settings()["revisit"]
    already = db.featured_arxiv_ids(conn)
    url = ("http://export.arxiv.org/api/query"
           f"?search_query={requests.utils.quote(cfg['arxiv_query'])}"
           "&sortBy=relevance&max_results=40")
    try:
        r = _SESSION.get(url, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.text)
    except Exception as exc:  # noqa: BLE001
        log.warning("revisit fetch failed: %s", exc)
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg["min_age_days"])
    for entry in root.findall("a:entry", _ARXIV_NS):
        def text(tag: str) -> str:
            el = entry.find(f"a:{tag}", _ARXIV_NS)
            return (el.text or "").strip() if el is not None else ""

        published = parse_date(text("published"))
        if not published or published > cutoff:
            continue
        abs_url = text("id")
        item = Item(
            source="arxiv", source_type="paper", title=text("title"), url=abs_url,
            published_at=iso(published), abstract=text("summary"),
            authors=", ".join(
                (a.findtext("a:name", default="", namespaces=_ARXIV_NS) or "").strip()
                for a in entry.findall("a:author", _ARXIV_NS)),
        ).finalize()
        if item.arxiv_id and item.arxiv_id in already:
            continue
        return item
    return None


def _build_revisit(conn, date: str) -> dict:
    item = _fetch_revisit_candidate(conn)
    if item is None:
        return {"date": date, "mode": "revisit", "revisit": None}
    retro = summarize.revisit_retrospective(item)
    row = summarize.apply_summary(item, {
        "one_line": retro.get("introduced", ""),
        "problem": "", "method": retro.get("how_it_works", ""),
        "why_it_matters": retro.get("why_important", ""),
        "future_potential": retro.get("still_relevant", ""),
        "results": "", "category": "computer-vision", "claim_type": "preprint",
        "overall_score": 7.0, "worth_including": True,
        "keywords": ["revisit"],
    })
    db.upsert_item(conn, row)
    conn.execute("UPDATE items SET featured_date = ? WHERE id = ?", (date, item.id))
    conn.commit()
    return {
        "date": date, "mode": "revisit",
        "revisit": {**dict(db.get(conn, item.id)), "retro": retro},
    }


# ------------------------------------------------------------------------- public
def build_digest(conn, shortlist_items: list[Item], force_revisit: bool = False) -> dict:
    date = datetime.now(timezone.utc).date().isoformat()

    if force_revisit:
        digest = _build_revisit(conn, date)
        digest["candidates"] = 0
        return digest

    rows = _summarize_shortlist(conn, shortlist_items)
    hero, cv, other, chosen = _select(rows)

    if hero is None:
        log.info("only %d qualified items -> revisit mode", len(chosen))
        digest = _build_revisit(conn, date)
        digest["candidates"] = len(rows)
        return digest

    _stamp_featured(conn, [hero, *cv, *other], date)

    try:
        copy = summarize.digest_copy(hero, cv, other)
    except Exception as exc:  # noqa: BLE001
        log.warning("digest copy failed: %s", exc)
        copy = {"thirty_seconds": [], "recommendation_reason": ""}

    rec_hint = (copy.get("recommendation_id_hint") or "").lower()
    rec = next((r for r in [hero, *cv, *other] if r["title"].lower() in rec_hint
                or rec_hint in r["title"].lower()), hero)

    return {
        "date": date,
        "mode": "normal",
        "hero": hero,
        "computer_vision": cv,
        "other": other,
        "thirty_seconds": copy.get("thirty_seconds", []),
        "recommendation": {"title": rec["title"], "url": rec["url"],
                           "reason": copy.get("recommendation_reason", "")},
        "candidates": len(rows),
        "selected_ids": [r["id"] for r in [hero, *cv, *other]],
    }
