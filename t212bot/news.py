"""Free news feed + deterministic headline scoring for the news-driven strategy.

Source: Yahoo Finance's per-ticker RSS feed (no API key, no sign-up), with
Google News RSS as a per-symbol fallback when Yahoo returns nothing or
errors. Both are polled, not streamed -- a cycle sees whatever headlines
the feed holds at that moment (typically the latest ~20 per ticker), so
"checks the news constantly" here means "every cycle", not "the instant a
story breaks". Anyone paying for a low-latency wire has already traded the
headline by the time a free RSS poll sees it; the edge this strategy is
built around is the slower *drift* after clearly good news, not the first
seconds of the reaction -- see NewsSentiment in strategy.py.

Scoring is a finance-specific lexicon (phrases first, then single words,
with simple negation handling), not an LLM: it needs no key, costs nothing,
is fully deterministic (the same headline always scores the same, which
matters for a backtest to be reproducible), and runs offline on a CI
runner. It is cruder than a language model -- it reads "misses estimates"
as bad and "raises guidance" as good, and little beyond that -- which is
the main reason the strategy requires several headlines to agree AND the
price to confirm before acting on it, rather than trusting any one score.
An LLM scorer can slot in later behind score_headline() without changing
anything downstream.

Score archive: each cycle records that day's per-symbol aggregate score to
state/news.json (score, headline counts and dates only -- no prices, no
dollar figures, this repo is public). RSS feeds hold no history, so this
archive, built up by paper-trading the bot, is the only thing a news
backtest can replay against prices -- see backtest.print_news_backtest.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

log = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "news.json"
ARCHIVE_DAYS = 400

YAHOO_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search?q={symbol}+stock&hl=en-US&gl=US&ceid=US:en"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; t212bot/1.0; +https://github.com/isabaji/t212)"}
_REQUEST_TIMEOUT = 15
_POLITE_DELAY_SECONDS = 0.25


@dataclass
class Headline:
    symbol: str
    title: str
    published: datetime | None   # tz-aware UTC, None if the feed gave no usable date
    link: str = ""
    source: str = ""


@dataclass
class NewsScore:
    """Aggregate of one symbol's recent headlines.

    score: recency-weighted mean of the scored headlines, -1 (uniformly
    bad) .. +1 (uniformly good). 0 when nothing scored.
    n_scored: headlines the lexicon actually had an opinion on (score != 0)
    -- the count the strategy's min_headlines gate uses, since a headline
    the lexicon can't read says nothing either way.
    n_total: all deduplicated, in-window headlines, scored or not.
    pos_frac: share of scored headlines that were positive -- lets the
    strategy require broad agreement, not one strong story averaging out
    several mild ones.
    top_headline: the highest-|score| headline, for the decision log.
    """

    symbol: str
    score: float = 0.0
    n_scored: int = 0
    n_total: int = 0
    pos_frac: float = 0.0
    top_headline: str | None = None
    top_score: float = 0.0
    # "own" (this symbol's own headlines), "peers:NVDA+AVGO" (sector
    # read-through, see peer_scores), and which scorer produced it.
    source: str = "own"
    scorer: str = "lexicon"


# ---------------------------------------------------------------------------
# Scoring lexicon
# ---------------------------------------------------------------------------
# Multi-word phrases are matched before single words so "price target cut"
# scores as one negative event rather than "price target" (neutral) + "cut"
# (negative, but far weaker than the phrase deserves). Weights are rough
# priors on how price-relevant each wording tends to be: guidance changes
# and earnings surprises move stocks for days; a generic "rises"/"falls" is
# mostly describing a move that already happened, so it's weighted low.
PHRASES: dict[str, float] = {
    # earnings / guidance
    "beats estimates": 0.9, "beat estimates": 0.9, "tops estimates": 0.9, "top estimates": 0.9,
    "beats expectations": 0.9, "beat expectations": 0.9, "tops expectations": 0.9,
    "beats on": 0.7, "beat on": 0.7, "better than expected": 0.8, "better-than-expected": 0.8,
    "raises guidance": 1.0, "raised guidance": 1.0, "lifts guidance": 1.0, "boosts guidance": 1.0,
    "raises forecast": 1.0, "raised forecast": 1.0, "raises outlook": 1.0, "lifts outlook": 1.0,
    "raises full-year": 0.9, "raises annual": 0.9,
    "record revenue": 0.8, "record profit": 0.8, "record quarter": 0.8, "record sales": 0.8,
    "record earnings": 0.8, "strong quarter": 0.7, "strong results": 0.7, "strong demand": 0.7,
    "profit rises": 0.6, "profit jumps": 0.7, "profit surges": 0.8, "revenue grows": 0.6,
    "revenue jumps": 0.7, "sales jump": 0.7, "sales rise": 0.5,
    "misses estimates": -0.9, "miss estimates": -0.9, "misses expectations": -0.9,
    "miss expectations": -0.9, "falls short": -0.7, "fell short": -0.7, "comes up short": -0.7,
    "worse than expected": -0.8, "worse-than-expected": -0.8, "weaker than expected": -0.7,
    "cuts guidance": -1.0, "cut guidance": -1.0, "lowers guidance": -1.0, "lowered guidance": -1.0,
    "slashes guidance": -1.0, "trims guidance": -0.8, "cuts forecast": -1.0, "lowers forecast": -1.0,
    "cuts outlook": -1.0, "lowers outlook": -1.0, "slashes outlook": -1.0, "trims outlook": -0.8,
    "guidance below": -0.8, "outlook below": -0.8, "profit warning": -1.0, "warns on": -0.6,
    "weak demand": -0.7, "soft demand": -0.6, "demand slows": -0.6, "weak quarter": -0.7,
    "profit falls": -0.6, "profit drops": -0.6, "profit plunges": -0.8, "revenue falls": -0.6,
    "revenue drops": -0.6, "sales fall": -0.5, "sales drop": -0.5, "loss widens": -0.8,
    "wider loss": -0.7, "swings to loss": -0.8, "swings to a loss": -0.8,
    # analysts
    "price target raised": 0.6, "raises price target": 0.6, "lifts price target": 0.6,
    "hikes price target": 0.6, "boosts price target": 0.6, "buy rating": 0.5,
    "top pick": 0.5, "initiated at buy": 0.5, "initiates at buy": 0.5, "upgraded to buy": 0.8,
    "upgrades to buy": 0.8, "upgraded to outperform": 0.8, "upgraded to overweight": 0.8,
    "price target cut": -0.6, "cuts price target": -0.6, "lowers price target": -0.6,
    "trims price target": -0.5, "slashes price target": -0.7, "sell rating": -0.6,
    "downgraded to sell": -0.9, "downgrades to sell": -0.9, "downgraded to hold": -0.6,
    "downgraded to neutral": -0.6, "downgraded to underperform": -0.9,
    "downgraded to underweight": -0.9, "initiated at sell": -0.6,
    # corporate events
    "fda approval": 0.9, "fda approves": 0.9, "wins approval": 0.8, "wins contract": 0.7,
    "awarded contract": 0.7, "lands contract": 0.7, "secures contract": 0.7, "share buyback": 0.5,
    "share repurchase": 0.5, "buyback program": 0.5, "raises dividend": 0.6, "dividend increase": 0.6,
    "hikes dividend": 0.6, "special dividend": 0.5, "to acquire": 0.3, "agrees to buy": 0.3,
    "all-time high": 0.5, "record high": 0.5, "new high": 0.3, "stock split": 0.3,
    "cuts dividend": -0.8, "dividend cut": -0.8, "suspends dividend": -0.9, "slashes dividend": -0.9,
    "sec charges": -0.9, "sec probe": -0.7, "sec investigation": -0.7, "doj probe": -0.7,
    "doj investigation": -0.7, "under investigation": -0.6, "antitrust probe": -0.6,
    "class action": -0.6, "data breach": -0.8, "cyber attack": -0.7, "cyberattack": -0.7,
    "chapter 11": -1.0, "files for bankruptcy": -1.0, "going concern": -0.9, "debt default": -0.8,
    "ceo resigns": -0.5, "ceo steps down": -0.4, "ceo departs": -0.4, "ceo ousted": -0.6,
    "cfo resigns": -0.5, "job cuts": -0.3, "lays off": -0.3, "to lay off": -0.3,
    "52-week low": -0.5, "new low": -0.3, "credit downgrade": -0.7, "rating cut": -0.6,
    "trading halted": -0.6, "halts trading": -0.6, "production halt": -0.6,
    "halts production": -0.6, "safety recall": -0.7, "recalls": -0.6,
}

WORDS: dict[str, float] = {
    # positive
    "beats": 0.5, "beat": 0.4, "tops": 0.4, "upgrade": 0.7, "upgraded": 0.7, "upgrades": 0.7,
    "outperform": 0.6, "overweight": 0.5, "bullish": 0.5, "surges": 0.5, "surge": 0.4,
    "soars": 0.5, "soar": 0.4, "jumps": 0.4, "jump": 0.3, "rallies": 0.4, "rally": 0.3,
    "climbs": 0.3, "gains": 0.3, "rises": 0.3, "rise": 0.2, "growth": 0.3, "grows": 0.3,
    "profit": 0.2, "profitable": 0.4, "profitability": 0.3, "buyback": 0.5, "approval": 0.5,
    "approved": 0.4, "approves": 0.4, "wins": 0.4, "win": 0.3, "awarded": 0.4, "partnership": 0.3,
    "expands": 0.3, "expansion": 0.3, "launches": 0.2, "exceeds": 0.5, "exceeded": 0.5,
    "strong": 0.4, "strength": 0.3, "robust": 0.4, "momentum": 0.3, "accelerates": 0.4,
    "accelerating": 0.4, "boosts": 0.4, "boost": 0.3, "optimistic": 0.4, "upbeat": 0.5,
    "breakthrough": 0.5, "milestone": 0.3, "dividend": 0.2, "upside": 0.3, "recovery": 0.3,
    "rebounds": 0.3, "rebound": 0.3, "outperforms": 0.6, "surpasses": 0.5,
    # negative
    "misses": -0.6, "miss": -0.4, "downgrade": -0.7, "downgraded": -0.7, "downgrades": -0.7,
    "underperform": -0.6, "underweight": -0.5, "bearish": -0.5, "plunges": -0.7, "plunge": -0.6,
    "plummets": -0.7, "plummet": -0.6, "tumbles": -0.6, "tumble": -0.5, "slumps": -0.5,
    "slump": -0.5, "sinks": -0.5, "sink": -0.4, "falls": -0.3, "fall": -0.2, "drops": -0.3,
    "drop": -0.2, "slides": -0.3, "slide": -0.2, "declines": -0.3, "decline": -0.3,
    "lawsuit": -0.5, "lawsuits": -0.5, "sued": -0.5, "sues": -0.4, "probe": -0.5,
    "investigation": -0.5, "investigates": -0.5, "fraud": -0.9, "recall": -0.6, "bankruptcy": -1.0,
    "bankrupt": -1.0, "insolvency": -0.9, "default": -0.5, "defaults": -0.5, "layoffs": -0.3,
    "warns": -0.5, "warning": -0.4, "halts": -0.5, "halt": -0.4, "suspends": -0.5, "suspended": -0.5,
    "delays": -0.4, "delay": -0.3, "delayed": -0.4, "outage": -0.5, "breach": -0.6, "hack": -0.5,
    "hacked": -0.6, "fined": -0.5, "fines": -0.4, "penalty": -0.4, "antitrust": -0.4,
    "tariff": -0.3, "tariffs": -0.3, "strike": -0.4, "selloff": -0.5, "sell-off": -0.5,
    "resigns": -0.3, "resignation": -0.3, "disappointing": -0.6, "disappoints": -0.6,
    "disappointment": -0.6, "shortfall": -0.6, "downturn": -0.4, "slowdown": -0.4, "slows": -0.3,
    "weak": -0.4, "weakness": -0.4, "weaker": -0.4, "loss": -0.3, "losses": -0.3, "cuts": -0.3,
    "cut": -0.2, "slashes": -0.5, "slash": -0.4, "concerns": -0.3, "concern": -0.3, "fears": -0.4,
    "risk": -0.2, "risks": -0.2, "crash": -0.7, "crashes": -0.7, "scandal": -0.7,
    "glitch": -0.4, "shortage": -0.4, "writedown": -0.6, "write-down": -0.6, "impairment": -0.6,
    "restatement": -0.8, "subpoena": -0.6, "indicted": -0.9, "indictment": -0.9,
}

NEGATORS = {"not", "no", "never", "fails", "failed", "fail", "without", "isn't", "doesn't",
            "didn't", "won't", "can't", "cannot"}
_NEGATION_WINDOW = 3

# Listicles, explainers and questions ("Should you buy X?", "3 stocks to
# watch") are opinion filler that tracks the recent price move rather than
# carrying new information; they're kept but heavily down-weighted.
_NOISE_PATTERN = re.compile(
    r"(\bstocks? to (buy|watch|sell|own|avoid)\b|\bshould you\b|\bis it time\b|\bhere'?s why\b"
    r"|\bwhat to know\b|\bwhat you need to know\b|\b\d+ (reasons?|stocks?|things?|charts?)\b"
    r"|\bvs\.?\b|\bversus\b|\bbetter buy\b|\bbest stocks?\b|\btop \d+\b|\bmotley fool\b"
    r"|\bprediction\b|\bcould\b.*\bmillionaire\b|\bbuy the dip\b|\?\s*$)",
    re.IGNORECASE,
)
_NOISE_WEIGHT = 0.3

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-']*")


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def score_headline(title: str) -> float:
    """Lexicon score of one headline, -1..+1 (0 = nothing recognisable).

    Phrases are matched on the normalised title first and their text
    blanked out so the single-word pass can't double count; the word pass
    then flips the sign of any hit within _NEGATION_WINDOW tokens after a
    negator ("not profitable", "fails to beat"). The summed weights go
    through tanh so a headline packed with strong words saturates at +-1
    instead of dominating the aggregate.
    """
    text = " " + _normalize_title(title) + " "
    total = 0.0
    for phrase, weight in PHRASES.items():
        key = " " + _normalize_title(phrase) + " "
        count = text.count(key)
        if count:
            total += weight * count
            text = text.replace(key, " ")
    tokens = _TOKEN_RE.findall(text)
    negate_until = -1
    for i, tok in enumerate(tokens):
        if tok in NEGATORS:
            negate_until = i + _NEGATION_WINDOW
            continue
        weight = WORDS.get(tok)
        if weight is None:
            continue
        total += -weight if i <= negate_until else weight
    if total == 0.0:
        return 0.0
    score = math.tanh(total)
    if _NOISE_PATTERN.search(title):
        score *= _NOISE_WEIGHT
    return score


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def _parse_rss(xml_text: str, symbol: str, source: str) -> list[Headline]:
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        if source == "google":
            # Google News titles end with " - Publisher"; strip it so the
            # publisher name can't score (e.g. "- Seeking Alpha").
            title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)
        published = None
        raw_date = item.findtext("pubDate")
        if raw_date:
            try:
                published = parsedate_to_datetime(raw_date.strip())
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
                published = published.astimezone(timezone.utc)
            except (TypeError, ValueError):
                published = None
        out.append(Headline(symbol=symbol, title=title, published=published,
                            link=(item.findtext("link") or "").strip(), source=source))
    return out


def _fetch_feed(url: str, symbol: str, source: str, session: requests.Session) -> list[Headline]:
    resp = session.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
    if not resp.ok:
        raise RuntimeError(f"{source} feed HTTP {resp.status_code} for {symbol}")
    return _parse_rss(resp.text, symbol, source)


def fetch_headlines(symbols: list[str], max_age_hours: float = 48.0,
                    now: datetime | None = None,
                    session: requests.Session | None = None) -> dict[str, list[Headline]]:
    """{symbol: [Headline]} -- deduplicated, no older than max_age_hours.

    Yahoo first; Google News only if Yahoo errors or returns nothing for
    that symbol. A failing symbol logs a warning and yields an empty list
    rather than failing the whole cycle: no news just means no signal.
    Undated items are kept (treated as fresh) -- some feeds omit pubDate
    and dropping them would silently blind the strategy to that source.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)
    session = session or requests.Session()
    out: dict[str, list[Headline]] = {}
    for i, sym in enumerate(symbols):
        if i:
            time.sleep(_POLITE_DELAY_SECONDS)
        items: list[Headline] = []
        for source, template in (("yahoo", YAHOO_RSS_URL), ("google", GOOGLE_NEWS_RSS_URL)):
            try:
                items = _fetch_feed(template.format(symbol=sym), sym, source, session)
            except (requests.RequestException, RuntimeError, ET.ParseError) as exc:
                log.warning("News fetch failed for %s via %s: %s", sym, source, exc)
                items = []
            if items:
                break
        seen: set[str] = set()
        kept: list[Headline] = []
        for h in items:
            if h.published is not None and h.published < cutoff:
                continue
            key = _normalize_title(h.title)
            if not key or key in seen:
                continue
            seen.add(key)
            kept.append(h)
        out[sym] = kept
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def aggregate(symbol: str, headlines: list[Headline], now: datetime | None = None,
              half_life_hours: float = 12.0, scores_by_title: dict[str, float] | None = None,
              scorer: str = "lexicon") -> NewsScore:
    """Recency-weighted mean of the scored headlines (weight halves every
    half_life_hours), so this morning's guidance cut outweighs yesterday's
    analyst note even though both are inside the window.

    scores_by_title: optional {normalised title: score} from another scorer
    (t212bot/news_llm.py); a headline missing from it falls back to the
    lexicon."""
    now = now or datetime.now(timezone.utc)
    weighted_sum = weight_sum = 0.0
    n_scored = n_pos = 0
    top: tuple[float, str] | None = None
    for h in headlines:
        s = None
        if scores_by_title is not None:
            s = scores_by_title.get(_normalize_title(h.title))
        if s is None:
            s = score_headline(h.title)
        if s == 0.0:
            continue
        age_h = max(0.0, (now - h.published).total_seconds() / 3600) if h.published else 0.0
        w = 0.5 ** (age_h / half_life_hours) if half_life_hours > 0 else 1.0
        weighted_sum += w * s
        weight_sum += w
        n_scored += 1
        n_pos += s > 0
        if top is None or abs(s) > abs(top[0]):
            top = (s, h.title)
    score = weighted_sum / weight_sum if weight_sum > 0 else 0.0
    return NewsScore(symbol=symbol, score=score, n_scored=n_scored, n_total=len(headlines),
                     pos_frac=(n_pos / n_scored) if n_scored else 0.0,
                     top_headline=top[1] if top else None, top_score=top[0] if top else 0.0,
                     scorer=scorer)


def aggregate_all(headlines: dict[str, list[Headline]], now: datetime | None = None,
                  half_life_hours: float = 12.0, scorer=None) -> dict[str, NewsScore]:
    """scorer: optional callable(list[str]) -> {normalised title: score}
    (see news_llm.make_scorer); None means the lexicon."""
    scores_by_title = None
    name = "lexicon"
    if scorer is not None:
        titles = [h.title for items in headlines.values() for h in items]
        scores_by_title = scorer(titles)
        name = "llm"
    return {sym: aggregate(sym, items, now, half_life_hours, scores_by_title, name)
            for sym, items in headlines.items()}


def peer_scores(scores: dict[str, NewsScore], sector_of, min_headlines: int = 3,
                discount: float = 0.5, min_peers: int = 2) -> dict[str, NewsScore]:
    """Sector read-through: a symbol without enough headlines of its own
    borrows its sector peers' consensus, discounted.

    Only fills gaps -- a symbol with min_headlines scored headlines of its
    own keeps its own score untouched. Requires at least min_peers peers in
    the same sector that each clear min_headlines themselves, averages
    their scores weighted by headline count, and multiplies by `discount`
    (0.5 by default: good news on NVDA is weaker evidence for AVGO than
    for NVDA). The result carries the peers' combined headline count so the
    strategy's min_headlines gate passes, and source="peers:..." so the
    decision log and the archive say where it came from. discount <= 0
    disables (returns scores unchanged)."""
    if discount <= 0:
        return scores
    by_sector: dict[str, list[NewsScore]] = {}
    for ns in scores.values():
        if ns.n_scored >= min_headlines and ns.source == "own":
            by_sector.setdefault(sector_of(ns.symbol), []).append(ns)
    out = dict(scores)
    for sym, ns in scores.items():
        if ns.n_scored >= min_headlines:
            continue
        sector = sector_of(sym)
        if sector == "Other":
            continue
        peers = [p for p in by_sector.get(sector, []) if p.symbol != sym]
        if len(peers) < min_peers:
            continue
        weight = sum(p.n_scored for p in peers)
        score = sum(p.score * p.n_scored for p in peers) / weight * discount
        pos = sum(p.pos_frac * p.n_scored for p in peers) / weight
        strongest = max(peers, key=lambda p: abs(p.score))
        out[sym] = NewsScore(symbol=sym, score=score, n_scored=weight, n_total=ns.n_total,
                             pos_frac=pos, top_headline=f"[{strongest.symbol}] {strongest.top_headline}",
                             top_score=strongest.top_score,
                             source="peers:" + "+".join(sorted(p.symbol for p in peers)),
                             scorer=strongest.scorer)
    return out


# ---------------------------------------------------------------------------
# State / score archive
# ---------------------------------------------------------------------------
def load_state() -> dict:
    """{"scores": {"YYYY-MM-DD": {sym: {"score", "n", "total", "pos"}}},
        "positions": {t212_ticker: {"entered": "YYYY-MM-DD"}},
        "closed": {t212_ticker: "YYYY-MM-DD"}}
    scores is the backtest archive; positions tracks entry dates for the
    max-hold exit; closed stops a symbol being re-bought the same day it
    was stopped out. Dates, counts and -1..1 scores only."""
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
            if isinstance(state, dict):
                state.setdefault("scores", {})
                state.setdefault("positions", {})
                state.setdefault("closed", {})
                return state
        except (json.JSONDecodeError, OSError):
            pass
    return {"scores": {}, "positions": {}, "closed": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def record_scores(state: dict, day: str, scores: dict[str, NewsScore]) -> None:
    """Archive today's aggregates (later cycles the same day overwrite
    earlier ones -- the end-of-day entry is the one the backtest replays).
    Symbols with no scored headlines are still recorded with n=0 so the
    backtest can tell "no news" from "no data"."""
    entry = state.setdefault("scores", {}).setdefault(day, {})
    for sym, ns in scores.items():
        rec = {"score": round(ns.score, 4), "n": ns.n_scored,
               "total": ns.n_total, "pos": round(ns.pos_frac, 3)}
        if ns.source != "own":
            rec["src"] = ns.source
        if ns.scorer != "lexicon":
            rec["scorer"] = ns.scorer
        entry[sym] = rec
    for old in sorted(state["scores"])[:-ARCHIVE_DAYS]:
        del state["scores"][old]


def score_history(state: dict) -> dict[str, dict[str, NewsScore]]:
    """Archive -> {day: {sym: NewsScore}}, the shape NewsSentiment replays."""
    out: dict[str, dict[str, NewsScore]] = {}
    for day, by_sym in state.get("scores", {}).items():
        out[day] = {
            sym: NewsScore(symbol=sym, score=float(v.get("score", 0.0)), n_scored=int(v.get("n", 0)),
                           n_total=int(v.get("total", 0)), pos_frac=float(v.get("pos", 0.0)),
                           source=v.get("src", "own"), scorer=v.get("scorer", "lexicon"))
            for sym, v in by_sym.items()
        }
    return out
