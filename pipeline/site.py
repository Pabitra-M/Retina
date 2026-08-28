"""Export the archive to the static dashboard under docs/."""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone

from . import config, db

log = logging.getLogger("site")


def build(conn) -> None:
    items = db.export_items(conn)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "items": items,
    }
    out = config.DOCS_DIR / "data.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")

    src_index = config.TEMPLATES_DIR / "dashboard" / "index.html"
    shutil.copyfile(src_index, config.DOCS_DIR / "index.html")
    (config.DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")

    log.info("dashboard built: %d items -> %s", len(items), out)
