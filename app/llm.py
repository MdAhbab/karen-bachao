"""Operator-note interpretation across two independent model providers.

Rate limits and outages are the main operational risk here: the Gemini free
tier returns transient 503 "high demand" and 429 quota errors, and per-model
daily caps can run out mid-event. Four things keep interpretation available
and inside the judge's latency budget:

  1. two independent providers (Google Gemini and Groq) on separate quotas;
  2. a small tier of models raced concurrently, first valid answer wins,
     escalating to further tiers only when a whole tier fails;
  3. a short per-model timeout and a total budget under the 30s judge limit;
  4. interpretations cached by note text, so repeated notes cost nothing.

Only if every model on every provider fails does app.fallback take over.
"""

import asyncio
import hashlib
import json
import logging
import re
from typing import Any

import httpx

from app import fallback
from app.config import (
    GEMINI_API_KEY,
    GEMINI_ENDPOINT,
    GEMINI_MODELS,
    GROQ_API_KEY,
    GROQ_ENDPOINT,
    GROQ_MODELS,
    LLM_RACE_SIZE,
    LLM_TIMEOUT_S,
    LLM_TOTAL_BUDGET_S,
)
from app.guardrails import sanitize
from app.prompt import RESPONSE_SCHEMA, build_prompt

log = logging.getLogger("gridwise.llm")

_CACHE: dict[str, list[dict]] = {}
_CACHE_LIMIT = 512

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def _cache_key(notes: list[str], battery: Any) -> str:
    payload = json.dumps(
        {"notes": notes, "capacity": battery.capacity_kwh,
         "minimum": battery.minimum_energy_kwh},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_entries(text: str) -> list:
    """Pull the interpretation array out of a model's raw text response.

    Some models wrap JSON in a markdown fence even when asked for raw JSON,
    so strip that before parsing rather than losing a correct answer.
    """
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    parsed = json.loads(text)
    if isinstance(parsed, dict):
        for key in ("directive_interpretation", "directives", "interpretations"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
        raise ValueError("response object has no directive_interpretation array")
    if isinstance(parsed, list):
        return parsed
    raise ValueError("response is neither an object nor an array")


async def _post_with_retry(client, url: str, body: dict, headers: dict):
    """POST once, retried a single time on a transient provider error."""
    last: Exception | None = None
    for attempt in range(2):
        try:
            response = await client.post(url, json=body, headers=headers,
                                         timeout=LLM_TIMEOUT_S)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            last = exc
            # 503 "high demand" clears in moments, so one retry is worth it.
            # A 429 means this model's quota window is spent; retrying just
            # burns budget that escalating to the next tier spends better.
            if exc.response.status_code != 503 or attempt == 1:
                raise
            await asyncio.sleep(0.5)
    raise last  # type: ignore[misc]


async def _call_gemini(client: httpx.AsyncClient, model: str, prompt: str) -> list:
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }
    response = await _post_with_retry(
        client,
        f"{GEMINI_ENDPOINT}/{model}:generateContent",
        body,
        {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
    )
    payload = response.json()
    return _parse_entries(payload["candidates"][0]["content"]["parts"][-1]["text"])


async def _call_groq(client: httpx.AsyncClient, model: str, prompt: str) -> list:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    response = await _post_with_retry(
        client,
        GROQ_ENDPOINT,
        body,
        {"Authorization": f"Bearer {GROQ_API_KEY}",
         "Content-Type": "application/json"},
    )
    payload = response.json()
    return _parse_entries(payload["choices"][0]["message"]["content"])


def _providers() -> list[tuple[str, str, Any]]:
    """Every (provider, model, caller) triple, interleaved across providers.

    Interleaving matters: a tier drawn off the front of this list then always
    spans both providers, so one provider being rate limited never takes the
    whole tier down with it.
    """
    groq = [("groq", m, _call_groq) for m in GROQ_MODELS] if GROQ_API_KEY else []
    gemini = ([("gemini", m, _call_gemini) for m in GEMINI_MODELS]
              if GEMINI_API_KEY else [])

    entries: list[tuple[str, str, Any]] = []
    for i in range(max(len(groq), len(gemini))):
        if i < len(groq):
            entries.append(groq[i])
        if i < len(gemini):
            entries.append(gemini[i])
    return entries


def _tiers() -> list[list[tuple[str, str, Any]]]:
    """Split the model list into escalating race groups."""
    entries = _providers()
    size = max(1, LLM_RACE_SIZE)
    return [entries[i:i + size] for i in range(0, len(entries), size)]


async def _race(client: httpx.AsyncClient, prompt: str,
                entries: list[tuple[str, str, Any]]) -> tuple[list, str]:
    """Run one tier of models at once and take the first usable answer."""
    if not entries:
        raise RuntimeError("no model provider configured")

    tasks = {
        asyncio.create_task(call(client, model, prompt)): f"{provider}:{model}"
        for provider, model, call in entries
    }
    try:
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                label = tasks[task]
                try:
                    result = task.result()
                except Exception as exc:  # noqa: BLE001 - provider errors expected
                    log.warning("model %s failed: %s", label, type(exc).__name__)
                    continue
                log.info("interpretation served by %s", label)
                return result, label
        raise RuntimeError("all models failed")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()


async def interpret(notes: list[str], battery: Any) -> tuple[list[dict], str]:
    """Interpret every note. Returns (entries, source) naming the path taken."""
    key = _cache_key(notes, battery)
    if key in _CACHE:
        return [dict(e) for e in _CACHE[key]], "cache"

    if not _providers():
        log.error("no API key configured; using the deterministic parser")
        return sanitize(fallback.interpret(notes, battery), notes, battery), "fallback"

    prompt = build_prompt(notes, battery)
    raw: Any = None
    source = "fallback"

    try:
        async with httpx.AsyncClient() as client:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + LLM_TOTAL_BUDGET_S
            for tier in _tiers():
                remaining = deadline - loop.time()
                if remaining <= 0.5:
                    log.warning("interpretation budget exhausted")
                    break
                try:
                    raw, source = await asyncio.wait_for(
                        _race(client, prompt, tier), timeout=remaining)
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("tier %s failed: %s",
                                [f"{p}:{m}" for p, m, _ in tier],
                                type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - transport errors must not escape
        log.warning("llm transport error: %s", type(exc).__name__)

    if raw is None:
        log.error("all providers unavailable; using the deterministic parser")
        raw = fallback.interpret(notes, battery)
        source = "fallback"

    clean = sanitize(raw, notes, battery)

    if source != "fallback":
        if len(_CACHE) >= _CACHE_LIMIT:
            _CACHE.clear()
        _CACHE[key] = [dict(e) for e in clean]
    return clean, source
