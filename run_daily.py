#!/usr/bin/env python3
"""Daily AI Research & Innovation Intelligence Agent — orchestration entrypoint.

Pipeline:
  collect -> normalize -> dedupe -> heuristic pre-rank -> LLM summary+score
          -> select (freshness widening / revisit fallback) -> archive
          -> build dashboard -> email

Usage:
  python run_daily.py                 # full run: fetch, summarize, archive, site, email
  python run_daily.py --dry-run       # everything except sending email (writes preview)
  python run_daily.py --no-email      # run + archive + site, skip email
  python run_daily.py --revisit       # force "Research Worth Revisiting" mode
  python run_daily.py --no-collect    # reuse existing archive only (site + email)
"""
from __future__ import annotations

import argparse
import logging
import sys

from pipeline import collect, config, db, dedupe, digest, email_send, score, site, summarize

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("run")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="no email send (writes HTML preview)")
    ap.add_argument("--no-email", action="store_true", help="skip email entirely")
    ap.add_argument("--revisit", action="store_true", help="force revisit mode")
    ap.add_argument("--no-collect", action="store_true", help="skip source collection")
    args = ap.parse_args()

    conn = db.connect()

    shortlist_items = []
    if not args.no_collect and not args.revisit:
        raw = collect.collect_all()
        embedder = summarize.get_embedder()
        kept, _existing = dedupe.deduplicate(raw, conn, embedder=embedder)
        shortlist_items = score.shortlist(kept)
    else:
        log.info("collection skipped")

    the_digest = digest.build_digest(conn, shortlist_items, force_revisit=args.revisit)
    log.info("digest mode=%s candidates=%s", the_digest["mode"], the_digest.get("candidates"))

    site.build(conn)

    email_sent = False
    if args.no_email:
        log.info("email skipped (--no-email)")
    else:
        try:
            email_sent = email_send.send(the_digest, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001
            log.error("email failed: %s", exc)

    selected = len(the_digest.get("selected_ids", [])) or (1 if the_digest.get("revisit") else 0)
    db.record_run(conn, the_digest["date"], the_digest["mode"],
                  the_digest.get("candidates", 0), selected, email_sent)
    conn.commit()
    conn.close()

    log.info("done. mode=%s selected=%d email_sent=%s", the_digest["mode"], selected, email_sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
