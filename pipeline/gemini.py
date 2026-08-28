"""Thin Gemini REST client: JSON generation + embeddings, with self-throttle + backoff.

Uses the free Generative Language API. Get a key at https://aistudio.google.com/apikey
No SDK dependency — just requests.
"""
from __future__ import annotations

import json
import logging
import time

import requests

from . import config

log = logging.getLogger("gemini")

_BASE = "https://generativelanguage.googleapis.com/v1beta"
_last_call = 0.0


def _throttle() -> None:
    global _last_call
    rpm = config.settings()["gemini"]["requests_per_minute"]
    min_gap = 60.0 / max(rpm, 1)
    wait = min_gap - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def _retry_after_seconds(resp: requests.Response, attempt: int) -> int:
    """Honor the API's RetryInfo / Retry-After if present, else exponential backoff."""
    hdr = resp.headers.get("Retry-After")
    if hdr and hdr.isdigit():
        return min(int(hdr), 90)
    try:
        for d in resp.json().get("error", {}).get("details", []):
            if d.get("@type", "").endswith("RetryInfo"):
                delay = d.get("retryDelay", "0s").rstrip("s")
                return min(int(float(delay)) + 1, 90)
    except (ValueError, KeyError, TypeError):
        pass
    return min(2 ** attempt * 5, 90)


def _post(path: str, payload: dict, *, retries: int | None = None) -> dict:
    key = config.require_env("GEMINI_API_KEY")
    retries = retries or config.settings()["gemini"].get("max_retries", 6)
    url = f"{_BASE}/{path}?key={key}"
    for attempt in range(retries):
        _throttle()
        try:
            r = requests.post(url, json=payload, timeout=120)
        except requests.RequestException as exc:
            log.warning("gemini network error (%d/%d): %s", attempt + 1, retries, exc)
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 503):
            wait = _retry_after_seconds(r, attempt)
            log.warning("gemini %s (%d/%d), backing off %ds", r.status_code,
                        attempt + 1, retries, wait)
            time.sleep(wait)
            continue
        raise RuntimeError(f"gemini {r.status_code}: {r.text[:400]}")
    raise RuntimeError("gemini: exhausted retries")


def _close_json(s: str) -> str:
    """Best-effort close of a JSON fragment truncated at the token limit."""
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    out = s
    if in_str:
        out += '"'
    out = out.rstrip(", \n\t")
    for opener in reversed(stack):
        out += "}" if opener == "{" else "]"
    return out


def _loads_lenient(text: str) -> dict:
    """Parse model JSON; salvage a response truncated at the token limit."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1].lstrip("json").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    salvage = _close_json(text)
    for _ in range(8):
        try:
            return json.loads(salvage)
        except json.JSONDecodeError as exc:
            if exc.pos <= 0 or exc.pos >= len(salvage):
                break
            salvage = _close_json(text[:exc.pos])
    raise ValueError(f"could not parse model JSON: {text[:200]}...")


def generate_json(prompt: str, *, schema: dict | None = None, system: str | None = None) -> dict:
    s = config.settings()["gemini"]
    gen_cfg = {
        "temperature": s["temperature"],
        "maxOutputTokens": s["max_output_tokens"],
        "responseMimeType": "application/json",
    }
    if schema:
        gen_cfg["responseSchema"] = schema
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": gen_cfg,
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    data = _post(f"models/{s['model']}:generateContent", payload)
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"gemini: unexpected response shape: {json.dumps(data)[:400]}") from exc
    return _loads_lenient(text)


def embed(texts: list[str]) -> list[list[float]]:
    """Embeddings via embedContent (one request per text)."""
    s = config.settings()["gemini"]
    model = s["embed_model"]
    out: list[list[float]] = []
    for text in texts:
        payload = {"content": {"parts": [{"text": text[:2000]}]}}
        data = _post(f"models/{model}:embedContent", payload)
        out.append(data["embedding"]["values"])
    return out
