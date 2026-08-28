"""Archive store: SQLite file locally, Turso (libSQL) when TURSO_DATABASE_URL is set.

Backend selection is transparent to callers — every function takes a connection from
`connect()` and gets dict rows back regardless of backend.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from . import config
from .config import DATA_DIR, DB_PATH
from .normalize import Item

log = logging.getLogger("db")

_ITEM_COLUMNS = [
    "id", "arxiv_id", "doi", "source", "source_type", "title", "title_norm",
    "authors", "url", "pdf_url", "code_url", "project_url", "dataset_url",
    "published_at", "fetched_at", "date_discovered", "featured_date",
    "category", "keywords", "engagement", "prelim_score",
    "one_line", "problem", "method", "architecture", "results",
    "why_it_matters", "limitations", "future_potential", "project_idea",
    "score_novelty", "score_usefulness", "score_future_potential",
    "score_cv_relevance", "score_reproducibility", "score_significance",
    "overall_score", "worth_including", "claim_type", "summary_model",
    "raw_abstract",
]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS items (
    {", ".join(f"{c} TEXT" for c in _ITEM_COLUMNS if c not in (
        "engagement", "prelim_score", "overall_score", "worth_including",
        "score_novelty", "score_usefulness", "score_future_potential",
        "score_cv_relevance", "score_reproducibility", "score_significance"))},
    engagement INTEGER DEFAULT 0,
    prelim_score REAL DEFAULT 0,
    score_novelty REAL DEFAULT 0,
    score_usefulness REAL DEFAULT 0,
    score_future_potential REAL DEFAULT 0,
    score_cv_relevance REAL DEFAULT 0,
    score_reproducibility REAL DEFAULT 0,
    score_significance REAL DEFAULT 0,
    overall_score REAL DEFAULT 0,
    worth_including INTEGER DEFAULT 0,
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_items_title_norm ON items(title_norm);
CREATE INDEX IF NOT EXISTS idx_items_arxiv ON items(arxiv_id);
CREATE INDEX IF NOT EXISTS idx_items_discovered ON items(date_discovered);

CREATE TABLE IF NOT EXISTS runs (
    date TEXT PRIMARY KEY,
    mode TEXT,
    candidates INTEGER,
    selected INTEGER,
    email_sent INTEGER,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS embeddings (
    id TEXT PRIMARY KEY,
    vector TEXT
);
"""


class _Cursor:
    """Wraps a DB-API cursor so fetch* always yields dicts (keyed by column name)."""

    def __init__(self, cur):
        self._cur = cur

    def _cols(self) -> list[str]:
        return [d[0] for d in self._cur.description] if self._cur.description else []

    def fetchone(self):
        row = self._cur.fetchone()
        return dict(zip(self._cols(), row)) if row is not None else None

    def fetchall(self):
        cols = self._cols()
        return [dict(zip(cols, r)) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class _Conn:
    """Uniform connection facade over stdlib sqlite3 and libsql (Turso)."""

    def __init__(self, raw, *, remote: bool):
        self._raw = raw
        self._remote = remote

    def execute(self, sql: str, params=()):
        return _Cursor(self._raw.execute(sql, params))

    def executescript(self, sql: str):
        self._raw.executescript(sql)
        return self

    def commit(self):
        self._raw.commit()
        if self._remote:
            try:
                self._raw.sync()
            except Exception as exc:  # noqa: BLE001
                log.warning("turso sync after commit failed: %s", exc)

    def close(self):
        try:
            self.commit()
        finally:
            self._raw.close()


def connect() -> _Conn:
    url = config.env("TURSO_DATABASE_URL")
    if url:
        import libsql  # type: ignore

        token = config.require_env("TURSO_AUTH_TOKEN")
        replica = str(DATA_DIR / "turso-replica.db")
        raw = libsql.connect(replica, sync_url=url, auth_token=token)
        raw.sync()
        conn = _Conn(raw, remote=True)
        log.info("archive backend: Turso (%s)", url.split("//")[-1])
    else:
        raw = sqlite3.connect(DB_PATH)
        conn = _Conn(raw, remote=False)
        log.info("archive backend: local SQLite (%s)", DB_PATH)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def get(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def find_by_arxiv(conn: sqlite3.Connection, arxiv_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM items WHERE arxiv_id = ?", (arxiv_id,)).fetchone()


def find_by_title_norm(conn: sqlite3.Connection, title_norm: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM items WHERE title_norm = ?", (title_norm,)
    ).fetchone()


def recent_titles(conn: sqlite3.Connection, days: int) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return conn.execute(
        "SELECT id, title, title_norm, engagement FROM items WHERE date_discovered >= ?",
        (cutoff,),
    ).fetchall()


def featured_arxiv_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT arxiv_id FROM items WHERE featured_date IS NOT NULL AND arxiv_id IS NOT NULL"
    ).fetchall()
    return {r["arxiv_id"] for r in rows}


def upsert_item(conn: sqlite3.Connection, data: dict) -> None:
    """Insert or merge. On conflict, only overwrite fields that carry new info."""
    existing = get(conn, data["id"])
    row = {c: data.get(c) for c in _ITEM_COLUMNS}

    if existing is None:
        if not row.get("date_discovered"):
            row["date_discovered"] = datetime.now(timezone.utc).isoformat()
        cols = ", ".join(_ITEM_COLUMNS)
        placeholders = ", ".join("?" for _ in _ITEM_COLUMNS)
        conn.execute(f"INSERT INTO items ({cols}) VALUES ({placeholders})",
                     tuple(row.get(c) for c in _ITEM_COLUMNS))
        return

    merged = dict(existing)
    for col in _ITEM_COLUMNS:
        new_val = row.get(col)
        if new_val in (None, "", 0, "0"):
            continue
        if col in ("date_discovered",):
            continue
        if col == "engagement":
            merged[col] = max(int(existing["engagement"] or 0), int(new_val or 0))
        else:
            merged[col] = new_val
    set_clause = ", ".join(f"{c} = ?" for c in _ITEM_COLUMNS)
    conn.execute(f"UPDATE items SET {set_clause} WHERE id = ?",
                 tuple(merged.get(c) for c in _ITEM_COLUMNS) + (data["id"],))


def item_to_row(item: Item) -> dict:
    d = item.as_dict()
    d["raw_abstract"] = item.abstract
    d.setdefault("keywords", None)
    return d


def save_embedding(conn: sqlite3.Connection, item_id: str, vector: list[float]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO embeddings (id, vector) VALUES (?, ?)",
        (item_id, json.dumps(vector)),
    )


def record_run(conn: sqlite3.Connection, date: str, mode: str, candidates: int,
               selected: int, email_sent: bool, notes: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO runs (date, mode, candidates, selected, email_sent, notes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (date, mode, candidates, selected, int(email_sent), notes),
    )


def export_items(conn: sqlite3.Connection) -> list[dict]:
    """Everything the dashboard needs, newest first."""
    rows = conn.execute(
        "SELECT * FROM items ORDER BY COALESCE(featured_date, date_discovered) DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["keywords"] = json.loads(d["keywords"]) if d.get("keywords") else []
        except (json.JSONDecodeError, TypeError):
            d["keywords"] = []
        d["worth_including"] = bool(d.get("worth_including"))
        d.pop("raw_abstract", None)
        d.pop("title_norm", None)
        out.append(d)
    return out
