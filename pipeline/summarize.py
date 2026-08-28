"""LLM stage: per-item technical summary + rubric scores, plus digest-level copy.

The `summarizer` setting picks the engine. Only `gemini` is implemented; `ollama` and
`extractive` raise NotImplementedError so the switch point is obvious for later.
"""
from __future__ import annotations

import json
import logging

from . import config
from .normalize import Item

log = logging.getLogger("summarize")

SYSTEM = (
    "You are a rigorous AI research analyst producing a personal daily intelligence brief "
    "for an engineer who works on computer vision. Be precise and technical but explain "
    "methods in plain language. NEVER invent paper titles, authors, results, benchmark "
    "numbers, repositories, or URLs. Only use facts present in the provided text. If a fact "
    "is not given, say so. Distinguish peer-reviewed papers, preprints, company "
    "announcements, and unverified claims."
)

_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "one_line": {"type": "string"},
        "problem": {"type": "string"},
        "method": {"type": "string"},
        "architecture": {"type": "string"},
        "results": {"type": "string"},
        "why_it_matters": {"type": "string"},
        "limitations": {"type": "string"},
        "future_potential": {"type": "string"},
        "project_idea": {"type": "string"},
        "category": {
            "type": "string",
            "enum": ["computer-vision", "llm", "agents", "multimodal", "robotics",
                     "generative", "infrastructure", "other"],
        },
        "claim_type": {
            "type": "string",
            "enum": ["peer-reviewed", "preprint", "company-announcement", "unverified"],
        },
        "keywords": {"type": "array", "items": {"type": "string"}},
        "score_novelty": {"type": "number"},
        "score_usefulness": {"type": "number"},
        "score_future_potential": {"type": "number"},
        "score_cv_relevance": {"type": "number"},
        "score_reproducibility": {"type": "number"},
        "score_significance": {"type": "number"},
        "overall_score": {"type": "number"},
        "worth_including": {"type": "boolean"},
    },
    "required": ["one_line", "problem", "method", "results", "why_it_matters",
                 "future_potential", "category", "claim_type", "overall_score",
                 "worth_including"],
}

_SUMMARY_FIELDS = ["one_line", "problem", "method", "architecture", "results",
                   "why_it_matters", "limitations", "future_potential", "project_idea",
                   "category", "claim_type"]
_SCORE_FIELDS = ["score_novelty", "score_usefulness", "score_future_potential",
                 "score_cv_relevance", "score_reproducibility", "score_significance",
                 "overall_score"]


def _engine():
    name = config.settings().get("summarizer", "gemini")
    if name == "gemini":
        from . import gemini
        return gemini
    raise NotImplementedError(
        f"summarizer '{name}' not implemented yet. Set summarizer: gemini in config/settings.yaml"
    )


def get_embedder():
    """Returns a callable(list[str]) -> list[vec], or None if disabled/unavailable."""
    if not config.settings()["gemini"].get("use_embeddings_for_dedupe", True):
        return None
    try:
        eng = _engine()
        return eng.embed
    except Exception as exc:  # noqa: BLE001
        log.warning("embedder unavailable: %s", exc)
        return None


def summarize_item(item: Item) -> dict:
    eng = _engine()
    prompt = (
        f"Analyze this {item.source_type} for the daily brief.\n\n"
        f"SOURCE: {item.source}\n"
        f"TITLE: {item.title}\n"
        f"AUTHORS: {item.authors or 'unknown'}\n"
        f"PUBLISHED: {item.published_at or 'unknown'}\n"
        f"URL: {item.url}\n"
        f"COMMUNITY SIGNAL: {item.engagement}\n\n"
        f"ABSTRACT / TEXT:\n{item.abstract or '(none provided)'}\n\n"
        "Return JSON matching the schema. Guidance:\n"
        "- one_line: the core idea in one sentence a beginner understands.\n"
        "- method/architecture: the real technical mechanism (loss, training trick, "
        "pipeline, backbone). Do not hand-wave.\n"
        "- results: only numbers/benchmarks actually stated in the text; else 'not reported'.\n"
        "- project_idea: one concrete thing the reader could build/experiment with, or '' .\n"
        "- scores are 0-10. overall_score reflects novelty + usefulness + future potential + "
        "significance, weighted toward practical computer-vision value.\n"
        "- worth_including: true only if this genuinely deserves a spot in a quality-over-"
        "quantity brief."
    )
    data = eng.generate_json(prompt, schema=_ITEM_SCHEMA, system=SYSTEM)
    return data


def apply_summary(item: Item, data: dict) -> dict:
    """Merge LLM output into a row dict ready for db.upsert_item."""
    row = {
        "id": item.id,
        "arxiv_id": item.arxiv_id,
        "doi": item.doi,
        "source": item.source,
        "source_type": item.source_type,
        "title": item.title,
        "title_norm": item.title_norm,
        "authors": item.authors,
        "url": item.url,
        "pdf_url": item.pdf_url,
        "code_url": item.code_url,
        "project_url": item.project_url,
        "dataset_url": item.dataset_url,
        "published_at": item.published_at,
        "fetched_at": item.fetched_at,
        "engagement": item.engagement,
        "prelim_score": item.prelim_score,
        "raw_abstract": item.abstract,
        "summary_model": config.settings()["gemini"]["model"],
        "keywords": json.dumps(data.get("keywords", [])[:6]),
    }
    for f in _SUMMARY_FIELDS:
        row[f] = data.get(f, "")
    for f in _SCORE_FIELDS:
        row[f] = float(data.get(f, 0) or 0)
    row["worth_including"] = int(bool(data.get("worth_including")))
    return row


def digest_copy(hero: dict, cv_items: list[dict], other_items: list[dict]) -> dict:
    """Generate the 30-second bullets + recommendation from the selected set."""
    eng = _engine()

    def brief(d: dict) -> str:
        return f"- {d['title']} ({d.get('category')}): {d.get('one_line', '')}"

    lines = [brief(hero)] + [brief(d) for d in cv_items + other_items]
    prompt = (
        "Here are today's selected AI research items:\n\n" + "\n".join(lines) + "\n\n"
        "Return JSON with:\n"
        '- "thirty_seconds": array of 3-5 short punchy strings, the most important things '
        "to know today (no fluff, specific).\n"
        '- "recommendation_id_hint": the title of the single item most worth the reader\'s '
        "time today.\n"
        '- "recommendation_reason": 2-3 sentences explaining why that one deserves the time.'
    )
    schema = {
        "type": "object",
        "properties": {
            "thirty_seconds": {"type": "array", "items": {"type": "string"}},
            "recommendation_id_hint": {"type": "string"},
            "recommendation_reason": {"type": "string"},
        },
        "required": ["thirty_seconds", "recommendation_reason"],
    }
    return eng.generate_json(prompt, schema=schema, system=SYSTEM)


def revisit_retrospective(item: Item) -> dict:
    """Section 7: 'Research Worth Revisiting' write-up for an older influential paper."""
    eng = _engine()
    prompt = (
        "Write a 'Research Worth Revisiting' retrospective for this older paper.\n\n"
        f"TITLE: {item.title}\nAUTHORS: {item.authors or 'unknown'}\n"
        f"PUBLISHED: {item.published_at or 'unknown'}\nURL: {item.url}\n\n"
        f"ABSTRACT:\n{item.abstract or '(none provided)'}\n\n"
        "Return JSON with: introduced (what it introduced), how_it_works (the method, plain "
        "language + real detail), why_important (impact at the time), still_relevant (is the "
        "idea still useful today and why/why not), what_came_after (follow-up research "
        "directions it seeded), learn_today (what the reader can take from it now). "
        "Use only facts from the abstract; if unsure about lineage, phrase carefully."
    )
    schema = {
        "type": "object",
        "properties": {k: {"type": "string"} for k in
                       ["introduced", "how_it_works", "why_important", "still_relevant",
                        "what_came_after", "learn_today"]},
        "required": ["introduced", "how_it_works", "why_important", "still_relevant",
                     "what_came_after", "learn_today"],
    }
    return eng.generate_json(prompt, schema=schema, system=SYSTEM)
