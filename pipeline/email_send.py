"""Render the digest to HTML + text and send it via Gmail SMTP."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from jinja2 import Environment, FileSystemLoader

from . import config

log = logging.getLogger("email")

_loader = FileSystemLoader(str(config.TEMPLATES_DIR))
_html_env = Environment(loader=_loader, autoescape=True)          # escape user text in HTML
_text_env = Environment(loader=_loader, autoescape=False, trim_blocks=True,
                        lstrip_blocks=True, keep_trailing_newline=True)


def _subject(digest: dict) -> str:
    date = digest["date"]
    if digest["mode"] == "revisit":
        rv = digest.get("revisit")
        tail = (rv["title"][:60] if rv else "Research worth revisiting")
        return f"AI Research Daily — {date} | Revisit: {tail}"
    hero = digest.get("hero") or {}
    tail = hero.get("title", "")[:70]
    return f"AI Research Daily — {date} | {tail}"


def render(digest: dict) -> tuple[str, str, str]:
    ctx = dict(digest, dashboard_url=config.env("DASHBOARD_URL"))
    html = _html_env.get_template("email.html.j2").render(**ctx)
    text = _text_env.get_template("email.txt.j2").render(**ctx)
    return _subject(digest), html, text


def send(digest: dict, *, dry_run: bool = False) -> bool:
    subject, html, text = render(digest)

    if dry_run:
        log.info("[dry-run] would send: %s", subject)
        preview = config.DATA_DIR / "last_email.html"
        preview.write_text(html, encoding="utf-8")
        log.info("[dry-run] HTML preview written to %s", preview)
        return False

    sender = config.require_env("GMAIL_ADDRESS")
    password = config.require_env("GMAIL_APP_PASSWORD")
    recipient = config.require_env("EMAIL_TO")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(sender, password)
        server.sendmail(sender, [r.strip() for r in recipient.split(",")], msg.as_string())
    log.info("email sent to %s", recipient)
    return True
