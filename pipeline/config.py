"""Configuration + environment loading."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
TEMPLATES_DIR = ROOT / "templates"

DB_PATH = DATA_DIR / "archive.db"


def _load_dotenv() -> None:
    """Minimal .env loader (no dependency). Real env vars always win."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


@lru_cache(maxsize=None)
def _yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def sources() -> dict:
    return _yaml("sources.yaml")


def keywords() -> dict:
    return _yaml("keywords.yaml")


def settings() -> dict:
    return _yaml("settings.yaml")


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default) or default


def require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {key}. "
            f"Copy .env.example to .env (local) or set it as a GitHub Actions secret."
        )
    return value


DATA_DIR.mkdir(parents=True, exist_ok=True)
DOCS_DIR.mkdir(parents=True, exist_ok=True)
