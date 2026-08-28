# Retina — Daily AI Research & Innovation Intelligence Agent

A personal research-intelligence platform. Once a day it:

1. **Collects** from arXiv, Hugging Face Daily Papers, Papers With Code, GitHub, Hacker News,
   and research-lab blogs.
2. **Deduplicates** (arXiv id / DOI / URL → normalized title → fuzzy → optional embedding).
3. **Pre-ranks** with a no-LLM heuristic (Computer Vision keywords first), keeps a shortlist.
4. **Summarizes & scores** each shortlisted item with **Gemini** (free tier) — plain-language
   method, why it matters, future potential, project idea, rubric scores.
5. **Selects** the strongest items (quality over quantity). Widens the time window
   24h → 7d → 30d only if needed. If the day is genuinely thin it sends a
   **"Research Worth Revisiting"** email about an older influential paper instead of filler.
6. **Archives** everything to a database — local SQLite file by default, or **Turso**
   (libSQL cloud) if `TURSO_DATABASE_URL` is set.
7. **Publishes** a searchable dashboard to `docs/` (GitHub Pages).
8. **Emails** you a 5–10 minute digest via Gmail.

Everything runs on free infrastructure: **GitHub Actions** (cron) + **GitHub Pages** +
**Gemini free tier** + **Gmail SMTP**.

---

## Setup (about 10 minutes)

### 1. Get the free keys

| What | Where | Notes |
|---|---|---|
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey | Free, no credit card |
| `GMAIL_APP_PASSWORD` | https://myaccount.google.com/apppasswords | Requires 2-Step Verification on the Google account |

### 2. Run it locally first

```bash
python -m venv .venv && . .venv/Scripts/activate      # Windows
pip install -r requirements.txt
cp .env.example .env        # then edit .env with your keys

python run_daily.py --dry-run      # fetch + summarize + archive + site, NO email
python -m http.server -d docs 8000 # open http://localhost:8000 to see the dashboard
python run_daily.py --no-email     # real run, still no email
python run_daily.py                # full run incl. email to EMAIL_TO
python run_daily.py --revisit      # test the "no important news" path
pytest                             # unit tests
```

`--dry-run` also writes `data/last_email.html` so you can preview the email in a browser.

### 3. Put it on GitHub

1. Create a repo and push this folder.
2. **Settings → Secrets and variables → Actions → New repository secret** — add:
   `GEMINI_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `EMAIL_TO`.
   (`GITHUB_TOKEN` is provided automatically.)
3. Add a **variable** (not secret) `DASHBOARD_URL` =
   `https://<user>.github.io/<repo>/` for the email footer link.
4. **Settings → Pages → Build and deployment → Deploy from a branch → `main` / `/docs`**.
5. **Actions** tab → *Daily AI Research Digest* → **Run workflow** (choose `dry-run` first).
6. Once it looks good, the `cron` runs it every day at 06:00 UTC (change in
   `.github/workflows/daily.yml`).

---

## Database: local SQLite vs Turso

By default the archive is a SQLite file at `data/archive.db`. That's the right choice for a
personal once-a-day agent — zero infra, no credentials.

To use **Turso** (libSQL cloud) instead, set two env vars / secrets:

```
TURSO_DATABASE_URL=libsql://YOUR-DB.turso.io
TURSO_AUTH_TOKEN=eyJ...            # dashboard -> Connect -> Create Token (Full Access)
```

When those are present, `pipeline/db.py` connects to Turso automatically (embedded replica at
`data/turso-replica.db`, synced on every commit — gitignored). Nothing else changes. The
GitHub Action then commits only `docs/`, not the DB.

Free tier: 500 DBs, 9 GB, 1B row reads/mo — far more than this project needs.

## Configuration

| File | Controls |
|---|---|
| `config/sources.yaml` | which sources are on, arXiv categories, RSS feed list, HN queries, GitHub topics |
| `config/keywords.yaml` | Priority-1 (Computer Vision) and Priority-2 keyword vocabularies, source trust weights |
| `config/settings.yaml` | score threshold, section caps, shortlist size, freshness windows, Gemini model & rate limit |

---

## Running without any AI API key (later)

The LLM stage is isolated in `pipeline/summarize.py` behind the `summarizer:` setting.
`gemini` is implemented. To go fully keyless you'd add an `ollama` branch (local Llama /
Qwen via `http://localhost:11434`) or an `extractive` branch (TextRank summaries + keyword
tags, no narrative sections). The rest of the pipeline doesn't change.

---

## Notes & limits

- **Bookmarks** ("Saved" tab) are stored in your browser's `localStorage` — per-device, since
  the dashboard is a static site with no backend.
- **Papers With Code** API is flaky; it's best-effort. arXiv + Hugging Face carry the load.
- **CVPR/ICCV/NeurIPS/…** have no clean API; virtually all their papers cross-post to arXiv and
  appear in Papers With Code / HF, so they're covered indirectly.
- The agent **never invents** titles, authors, results, repos, or URLs — the Gemini prompt
  forbids it and labels each item as peer-reviewed / preprint / company-announcement / unverified.
- `data/archive.db` and `docs/` are committed back to the repo by the Action each run, so the
  archive grows over time and the dashboard stays current.

---

## Project layout

```
config/          source registry + keyword vocab + tuning
pipeline/
  collect.py     source fetchers (best-effort)
  normalize.py   common Item schema
  dedupe.py      duplicate detection
  score.py       no-LLM heuristic pre-ranking
  gemini.py      Gemini REST client (throttle + backoff)
  summarize.py   LLM summaries, rubric scores, digest copy, revisit write-up
  digest.py      selection, freshness widening, revisit fallback
  db.py          SQLite archive
  site.py        dashboard export
  email_send.py  Gmail SMTP
templates/       email.html.j2, email.txt.j2, dashboard/index.html
docs/            GitHub Pages output (index.html + data.json)
data/archive.db  the archive (committed)
run_daily.py     entrypoint
.github/workflows/daily.yml
```
