"""Optional LLM headline scorer (Claude), a drop-in for the lexicon.

Off unless NEWS_SCORER=llm and ANTHROPIC_API_KEY is set. Scores are the
same -1..+1 "likely effect on this company's share price over the next
few days" the lexicon produces, so everything downstream (aggregation,
thresholds, the archive, the backtest) is unchanged; the archive records
which scorer produced each day's entry so a backtest can tell them apart.

Deterministic enough for the archive: each distinct headline is scored
once and cached locally (logs/news_llm_cache.json, gitignored), and the
request asks for structured JSON so there's nothing to parse loosely. Any
failure -- no SDK installed, auth, rate limit, a refusal, malformed
output -- falls back to the lexicon for that batch with a warning rather
than leaving the cycle without scores.

Cost: ~30 symbols x ~20 headlines = ~600 short lines per cycle before
caching; after the first cycle of a day only new headlines are sent.
NEWS_LLM_MODEL picks the model (default claude-opus-5-5; claude-haiku-5-5
is the cheap option if that matters more than reading quality).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .news import _normalize_title, score_headline

log = logging.getLogger(__name__)

CACHE_PATH = Path(__file__).resolve().parent.parent / "logs" / "news_llm_cache.json"
BATCH_SIZE = 40
DEFAULT_MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = (
    "You score financial news headlines for a long-only swing-trading bot. For each headline, "
    "estimate the likely effect on the named company's share price over the next 1-5 trading days: "
    "+1.0 clearly and materially positive (guidance raised, big earnings beat, major approval or contract), "
    "-1.0 clearly and materially negative (guidance cut, big miss, fraud probe, bankruptcy), "
    "0 for anything not price-relevant to that specific company -- market round-ups, listicles, "
    "'should you buy' opinion pieces, generic sector commentary, or news about a different company. "
    "Use intermediate values for mixed or minor news. Score the information in the headline itself, "
    "not the price move it may describe: 'shares jump on earnings beat' is about the beat (positive), "
    "'shares jump' alone is nearly 0. Never guess beyond what the headline says."
)

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"i": {"type": "integer"}, "score": {"type": "number"}},
                "required": ["i", "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scores"],
    "additionalProperties": False,
}


def configured(scorer: str) -> bool:
    return scorer.strip().lower() == "llm" and bool(os.getenv("ANTHROPIC_API_KEY"))


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache))


def _score_batch(client, model: str, titles: list[str]) -> dict[int, float]:
    import anthropic

    numbered = "\n".join(f"{i}: {t}" for i, t in enumerate(titles))
    request = dict(
        model=model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Score each headline. Return one entry per index.\n\n{numbered}"}],
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    )
    try:
        # Server-side refusal fallback: if the primary model declines, the API
        # re-runs the request on a fallback model inside the same call.
        response = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                               fallbacks="default", **request)
    except anthropic.BadRequestError:
        # Fallback parameter not accepted on this platform/model: plain call.
        response = client.messages.create(**request)
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined to score this batch")
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    return {int(e["i"]): max(-1.0, min(1.0, float(e["score"]))) for e in data.get("scores", [])}


def make_scorer(scorer: str, model: str | None = None):
    """Callable(list[str]) -> {normalized_title: score}, or None when the
    LLM scorer isn't configured (callers then use the lexicon)."""
    if not configured(scorer):
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("NEWS_SCORER=llm but the anthropic package isn't installed -- using the lexicon")
        return None
    client = anthropic.Anthropic()
    model = model or DEFAULT_MODEL
    cache = _load_cache()

    def score(titles: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        pending: dict[str, str] = {}
        for t in titles:
            key = _normalize_title(t)
            if key in cache:
                out[key] = cache[key]
            elif key:
                pending[key] = t
        keys = list(pending)
        for start in range(0, len(keys), BATCH_SIZE):
            batch_keys = keys[start:start + BATCH_SIZE]
            batch = [pending[k] for k in batch_keys]
            try:
                scored = _score_batch(client, model, batch)
            except (anthropic.APIError, RuntimeError, ValueError, KeyError, StopIteration, json.JSONDecodeError) as exc:
                log.warning("LLM headline scoring failed for a batch of %d (%s) -- lexicon used for them",
                            len(batch), exc)
                for k, t in zip(batch_keys, batch):
                    out[k] = score_headline(t)
                continue
            for i, k in enumerate(batch_keys):
                val = scored.get(i)
                if val is None:
                    val = score_headline(batch[i])
                cache[k] = val
                out[k] = val
        if keys:
            _save_cache(cache)
        return out

    return score
