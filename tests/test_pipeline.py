import sqlite3

import pytest

from pipeline import db as dbmod
from pipeline.normalize import Item, normalize_title, extract_arxiv_id
from pipeline import dedupe, score


def make_item(**kw):
    base = dict(source="arxiv", source_type="paper", title="A Test Paper",
                url="https://arxiv.org/abs/2408.12345", abstract="object detection with yolo")
    base.update(kw)
    return Item(**base).finalize()


def test_normalize_title():
    assert normalize_title("YOLO-v9: Real-Time!  Detection") == "yolo v9 real time detection"


def test_extract_arxiv_id():
    assert extract_arxiv_id("https://arxiv.org/abs/2408.12345v2") == "2408.12345"
    assert extract_arxiv_id("no id here") is None


def test_item_id_stable_by_arxiv():
    a = make_item(url="https://arxiv.org/abs/2408.12345")
    b = make_item(url="https://arxiv.org/pdf/2408.12345v3")
    assert a.id == b.id


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    c = dbmod.connect()
    yield c
    c.close()


def test_dedupe_exact_and_fuzzy(conn):
    items = [
        make_item(title="Fast Object Detection via YOLO", url="https://arxiv.org/abs/2408.00001"),
        make_item(title="Fast Object Detection via YOLO!!", url="https://example.com/x"),
        make_item(title="A Totally Different Paper on Segmentation", url="https://arxiv.org/abs/2408.00002"),
    ]
    kept, existing = dedupe.deduplicate(items, conn, embedder=None)
    titles = {k.title for k in kept}
    assert len(kept) == 2
    assert "A Totally Different Paper on Segmentation" in titles


def test_score_prefers_cv_keywords():
    cv = make_item(title="Real-time multi-object tracking on Jetson",
                   abstract="object tracking, quantization, edge ai")
    off = make_item(title="A study of medieval poetry", abstract="sonnets and verse")
    ranked = score.score_items([off, cv])
    assert ranked[0].title == cv.title
    assert ranked[0].prelim_score > ranked[1].prelim_score


def test_select_falls_back_to_revisit_when_thin():
    from pipeline import digest
    rows = [{"worth_including": 1, "overall_score": 3.0, "category": "llm",
             "published_at": None, "id": "x", "title": "weak"}]
    hero, cv, other, chosen = digest._select(rows)
    assert hero is None
