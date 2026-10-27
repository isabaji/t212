"""Event study ("how did similar news move this stock before?") and the
earnings calendar, both from free, keyless sources.

There is no free archive of historical *headlines*, but there are free
archives of historical *events*, which is what matters for the question:

- Earnings beats/misses: yfinance's per-ticker earnings history (report
  dates with the EPS surprise %), years deep.
- Company events by category: SEC EDGAR's submissions feed, free and
  keyless, lists every 8-K a company filed with its item codes -- 2.02 is
  results, 5.02 an officer change, 2.01 a completed acquisition, 2.05
  layoffs/restructuring, 4.02 a restatement, and so on (EDGAR_ITEM_EVENTS).
  Because item codes carry no direction (a 5.02 is a CEO leaving or a CEO
  hired), each filing is additionally tagged with the market's own day-0
  reaction, and the study keeps only past events whose initial reaction had
  the same sign as today's -- "past events of this type the market first
  read the same way".
- The market's reaction signature: past days this stock moved like today
  on heavy volume, regardless of why. The fallback when the headline
  category has no archive (analyst actions, lawsuits, product news).
- The bot's own score archive (state/news.json): past days this symbol's
  lexicon score crossed the buy threshold. Thin at first, but the most
  exact "similar news" once there's history.

For every analog the study measures forward returns at +1/+3/+5/+10
trading days from that day's close (the strategy also enters at/near a
close), in excess of SPY over the same window so a bull-market tailwind
can't masquerade as a news effect, and reports n / median / hit rate /
worst / best. Nothing is said below EVENT_STUDY_MIN_SAMPLES analogs.

Earnings calendar: next report date per symbol, cached in
state/earnings_calendar.json (dates and surprise percentages only) and
refreshed daily, so all three bots can avoid opening a position into a
report (Config.earnings_blackout_days) without a yfinance call per symbol
per cycle.

SEC asks automated clients to identify themselves in the User-Agent
(SEC_USER_AGENT env var; the default names this repo -- put your contact
in it if you run this seriously). EDGAR responses are cached locally per
run only (logs/, gitignored) -- a few hundred KB nobody needs committed.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from .news import Headline, _normalize_title

log = logging.getLogger(__name__)

CALENDAR_PATH = Path(__file__).resolve().parent.parent / "state" / "earnings_calendar.json"
EDGAR_CACHE_PATH = Path(__file__).resolve().parent.parent / "logs" / "edgar_cache.json"
EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
HORIZONS = (1, 3, 5, 10)
MAX_PAST_EARNINGS = 40


def _sec_headers() -> dict:
    ua = os.getenv("SEC_USER_AGENT", "t212bot/1.0 (https://github.com/isabaji/t212)")
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}


# ---------------------------------------------------------------------------
# Headline -> event category
# ---------------------------------------------------------------------------
EVENT_PATTERNS: dict[str, re.Pattern] = {
    "earnings_beat": re.compile(r"\b(beats?|tops?|exceeds?) (estimates|expectations|forecasts?|views?)\b|"
                                r"\bbetter than expected\b|\brecord (quarter|revenue|profit|earnings|sales)\b"),
    "earnings_miss": re.compile(r"\b(miss(es)?|falls? short of|fell short of) (estimates|expectations|forecasts?)\b|"
                                r"\bworse than expected\b|\bweaker than expected\b|\bswings to (a )?loss\b"),
    "guidance_raise": re.compile(r"\b(raises?|raised|lifts?|boosts?|hikes?) (its )?(full year |full-year |annual |fy )?"
                                 r"(guidance|forecast|outlook|view)\b"),
    "guidance_cut": re.compile(r"\b(cuts?|lowers?|lowered|slashes|trims?) (its )?(full year |full-year |annual |fy )?"
                               r"(guidance|forecast|outlook|view)\b|\bprofit warning\b|\bguidance below\b"),
    "upgrade": re.compile(r"\bupgrade[sd]?\b|\bprice target (raised|lifted|hiked)\b|\b(raises|lifts|hikes) price target\b"),
    "downgrade": re.compile(r"\bdowngrade[sd]?\b|\bprice target (cut|lowered|slashed)\b|\b(cuts|lowers|slashes) price target\b"),
    "exec_change": re.compile(r"\b(ceo|cfo|coo|chairman|chief executive|chief financial) "
                              r"(resigns?|steps? down|departs?|ousted|to leave|leaves|named|appointed|hired)\b|"
                              r"\bnames? (new )?(ceo|cfo)\b|\bappoints? (new )?(ceo|cfo)\b"),
    "m_and_a": re.compile(r"\b(to acquire|acquires|acquisition|agrees to buy|to buy|merger|merge with|takeover|buyout)\b"),
    "legal": re.compile(r"\b(lawsuit|sued|sues|class action|probe|investigation|investigates|sec charges|antitrust|"
                        r"fined|fine of|penalty|subpoena|indict(ed|ment))\b"),
    "approval_contract": re.compile(r"\b(fda approv(al|es|ed)|wins? (approval|contract)|awarded (a )?contract|"
                                    r"lands? (a )?contract|secures? (a )?contract|clearance)\b"),
    "dividend_buyback": re.compile(r"\b(buyback|repurchase|dividend)\b"),
    "layoffs_restructure": re.compile(r"\b(layoffs?|lays? off|job cuts|restructur(e|ing)|plant closure|closes plant)\b"),
    "product": re.compile(r"\b(launch(es|ed)?|unveils?|introduces?|recall(s|ed)?|outage|breach|hack(ed)?)\b"),
}

# 8-K item code -> event category, and which headline categories each
# category is a historical analog for.
EDGAR_ITEM_EVENTS = {
    "2.02": "earnings", "5.02": "exec_change", "1.01": "material_agreement", "2.01": "m_and_a",
    "2.05": "layoffs_restructure", "2.06": "impairment", "4.02": "restatement", "1.03": "bankruptcy",
    "3.01": "delisting_notice", "7.01": "reg_fd", "8.01": "other",
}
HEADLINE_TO_EDGAR = {
    "earnings_beat": ["earnings"], "earnings_miss": ["earnings"],
    "guidance_raise": ["earnings", "reg_fd"], "guidance_cut": ["earnings", "reg_fd"],
    "exec_change": ["exec_change"], "m_and_a": ["m_and_a", "material_agreement"],
    "approval_contract": ["material_agreement"], "layoffs_restructure": ["layoffs_restructure"],
}


def classify_headline(title: str) -> set[str]:
    text = _normalize_title(title)
    return {name for name, pat in EVENT_PATTERNS.items() if pat.search(text)}


def classify_headlines(headlines: list[Headline]) -> Counter:
    counts: Counter = Counter()
    for h in headlines:
        counts.update(classify_headline(h.title))
    return counts


def dominant_events(headlines: list[Headline], max_types: int = 3) -> list[str]:
    """Most frequent event categories across a symbol's headlines, most
    common first; generic 'product'/'dividend_buyback' only when nothing
    more specific is present."""
    counts = classify_headlines(headlines)
    specific = [t for t, _ in counts.most_common() if t not in ("product", "dividend_buyback")]
    if specific:
        return specific[:max_types]
    return [t for t, _ in counts.most_common(max_types)]


# ---------------------------------------------------------------------------
# Earnings calendar (state/earnings_calendar.json)
# ---------------------------------------------------------------------------
def load_calendar() -> dict:
    if CALENDAR_PATH.exists():
        try:
            data = json.loads(CALENDAR_PATH.read_text())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_calendar(state: dict) -> None:
    CALENDAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    CALENDAR_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def fetch_earnings(symbol: str) -> dict:
    """{"next": "YYYY-MM-DD" | None, "past": [["YYYY-MM-DD", surprise_pct | None], ...]}
    from yfinance's earnings history (newest first)."""
    df = yf.Ticker(symbol).earnings_dates
    out = {"next": None, "past": []}
    if df is None or df.empty:
        return out
    today = pd.Timestamp.now(tz="UTC").normalize()
    future, past = [], []
    for ts, row in df.iterrows():
        ts = pd.Timestamp(ts)
        ts_utc = (ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")).normalize()
        reported = row.get("Reported EPS")
        surprise = row.get("Surprise(%)")
        has_report = reported is not None and not (isinstance(reported, float) and math.isnan(reported))
        day = ts_utc.strftime("%Y-%m-%d")
        if ts_utc >= today and not has_report:
            future.append(day)
        elif has_report:
            s = None if surprise is None or (isinstance(surprise, float) and math.isnan(surprise)) else float(surprise)
            past.append([day, s])
    out["next"] = min(future) if future else None
    past.sort(reverse=True)
    out["past"] = past[:MAX_PAST_EARNINGS]
    return out


def refresh_calendar(state: dict, symbols: list[str], today: date, max_age_days: int = 1) -> None:
    """Refresh entries older than max_age_days or whose next date has passed.
    A failed fetch keeps the stale entry (and logs), so a yfinance hiccup
    can't wipe a known report date."""
    today_s = today.isoformat()
    for sym in symbols:
        entry = state.get(sym)
        fresh = (entry and entry.get("fetched")
                 and (today - date.fromisoformat(entry["fetched"])).days < max_age_days
                 and not (entry.get("next") and entry["next"] < today_s))
        if fresh:
            continue
        try:
            data = fetch_earnings(sym)
        except Exception as exc:  # noqa: BLE001 - any yfinance failure
            log.warning("Earnings calendar fetch failed for %s: %s", sym, exc)
            continue
        data["fetched"] = today_s
        state[sym] = data


def days_to_earnings(state: dict, symbol: str, today: date) -> int | None:
    nxt = (state.get(symbol) or {}).get("next")
    if not nxt:
        return None
    return (date.fromisoformat(nxt) - today).days


def in_earnings_blackout(state: dict, symbol: str, today: date, blackout_days: int,
                         allow_report_day: bool = False) -> bool:
    """True if the next report is within blackout_days ahead (inclusive of
    today unless allow_report_day). 0 disables."""
    if blackout_days <= 0:
        return False
    delta = days_to_earnings(state, symbol, today)
    if delta is None or delta > blackout_days:
        return False
    return delta >= 1 or not allow_report_day


def upcoming_reports(state: dict, today: date, within_days: int = 7) -> list[tuple[str, str]]:
    out = []
    for sym, entry in state.items():
        nxt = (entry or {}).get("next")
        if nxt and 0 <= (date.fromisoformat(nxt) - today).days <= within_days:
            out.append((nxt, sym))
    return sorted(out)


# ---------------------------------------------------------------------------
# EDGAR 8-K events
# ---------------------------------------------------------------------------
_cik_cache: dict[str, int] | None = None


def _load_edgar_cache() -> dict:
    if EDGAR_CACHE_PATH.exists():
        try:
            return json.loads(EDGAR_CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_edgar_cache(cache: dict) -> None:
    EDGAR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EDGAR_CACHE_PATH.write_text(json.dumps(cache))


def lookup_cik(symbol: str, session: requests.Session | None = None) -> int | None:
    global _cik_cache
    if _cik_cache is None:
        session = session or requests.Session()
        resp = session.get(EDGAR_TICKERS_URL, headers=_sec_headers(), timeout=20)
        if not resp.ok:
            raise RuntimeError(f"EDGAR ticker list HTTP {resp.status_code}")
        _cik_cache = {v["ticker"].upper(): int(v["cik_str"]) for v in resp.json().values()}
    return _cik_cache.get(symbol.upper().replace("-", "."))


def fetch_edgar_events(symbol: str, session: requests.Session | None = None,
                       max_age_days: int = 7) -> list[tuple[str, list[str]]]:
    """[(filing_date, [event categories]), ...] for the company's 8-Ks,
    newest first, from EDGAR's submissions feed (cached locally)."""
    cache = _load_edgar_cache()
    entry = cache.get(symbol)
    if entry and (datetime.now(timezone.utc).date() - date.fromisoformat(entry["fetched"])).days < max_age_days:
        return [tuple(x) for x in entry["events"]]
    session = session or requests.Session()
    cik = lookup_cik(symbol, session)
    if cik is None:
        log.info("No EDGAR CIK for %s", symbol)
        return []
    resp = session.get(EDGAR_SUBMISSIONS_URL.format(cik=cik), headers=_sec_headers(), timeout=20)
    if not resp.ok:
        raise RuntimeError(f"EDGAR submissions HTTP {resp.status_code} for {symbol}")
    recent = resp.json().get("filings", {}).get("recent", {})
    events = []
    for form, filed, items in zip(recent.get("form", []), recent.get("filingDate", []), recent.get("items", [])):
        if form not in ("8-K", "8-K/A"):
            continue
        cats = sorted({EDGAR_ITEM_EVENTS[i.strip()] for i in (items or "").split(",") if i.strip() in EDGAR_ITEM_EVENTS})
        if cats:
            events.append((filed, cats))
    cache[symbol] = {"fetched": datetime.now(timezone.utc).date().isoformat(), "events": events}
    _save_edgar_cache(cache)
    time.sleep(0.15)  # SEC's fair-use guidance: stay well under 10 req/s
    return events


# ---------------------------------------------------------------------------
# Forward returns and the study itself
# ---------------------------------------------------------------------------
@dataclass
class HorizonStats:
    n: int
    median: float
    mean: float
    hit_rate: float
    worst: float
    best: float


@dataclass
class SourceResult:
    name: str            # "earnings_beat" / "edgar:exec_change" / "price_reaction" / "archive"
    n: int
    horizons: dict[int, HorizonStats] = field(default_factory=dict)
    dates: list[str] = field(default_factory=list)


@dataclass
class EventStudy:
    symbol: str
    event_types: list[str]
    today_move: float | None
    sources: list[SourceResult]

    def best_source(self, min_samples: int) -> SourceResult | None:
        """Most specific source with enough analogs: typed (earnings/EDGAR)
        first, then the bot's own archive, then the price-reaction fallback."""
        order = {"price_reaction": 2, "archive": 1}
        ranked = sorted((s for s in self.sources if s.n >= min_samples),
                        key=lambda s: (order.get(s.name, 0), -s.n))
        return ranked[0] if ranked else None

    def verdict(self, horizon: int, min_samples: int) -> tuple[str, str]:
        """("supportive" | "adverse" | "insufficient", one-line summary)."""
        src = self.best_source(min_samples)
        if src is None or horizon not in src.horizons:
            n_any = max((s.n for s in self.sources), default=0)
            return "insufficient", (f"no comparable history (fewer than {min_samples} analogs; best source had {n_any})")
        h = src.horizons[horizon]
        kind = "supportive" if (h.median > 0 and h.hit_rate >= 0.5) else "adverse"
        return kind, (f"{src.n} prior {src.name.replace('_', ' ')} analogs -> median {h.median:+.1%} vs SPY over "
                      f"{horizon}d, {h.hit_rate:.0%} positive, worst {h.worst:+.1%}, best {h.best:+.1%}")

    def lines(self, horizon: int) -> list[str]:
        out = []
        for s in self.sources:
            h = s.horizons.get(horizon)
            if h is None:
                continue
            out.append(f"{s.name.replace('_', ' ')}: n={s.n}, median {h.median:+.1%}, {h.hit_rate:.0%} positive "
                       f"over {horizon}d (worst {h.worst:+.1%}, best {h.best:+.1%})")
        return out


def _naive_index(df: pd.DataFrame) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(df.index)
    return (idx.tz_localize(None) if idx.tz is not None else idx).normalize()


def forward_returns(df: pd.DataFrame, event_dates: list[str], spy: pd.DataFrame | None = None,
                    horizons: tuple[int, ...] = HORIZONS, match_sign: float | None = None) -> dict[int, list[float]]:
    """Per horizon, the list of forward returns from each event day's close
    (first trading day on/after the event date), minus SPY's over the same
    bars when given. match_sign: keep only events whose day-0 return has
    this sign (direction-matching for undirected event categories)."""
    idx = _naive_index(df)
    close = df["Close"].to_numpy(dtype=float)
    spy_close = None
    if spy is not None and not spy.empty:
        spy_close = pd.Series(spy["Close"].to_numpy(dtype=float), index=_naive_index(spy))
        spy_close = spy_close[~spy_close.index.duplicated(keep="last")].sort_index()
    out: dict[int, list[float]] = {h: [] for h in horizons}
    for d in event_dates:
        pos = idx.searchsorted(pd.Timestamp(d), side="left")
        if pos >= len(close) or pos == 0:
            continue
        if match_sign is not None:
            day0 = close[pos] / close[pos - 1] - 1
            if day0 == 0 or (day0 > 0) != (match_sign > 0):
                continue
        for h in horizons:
            if pos + h >= len(close):
                continue
            r = close[pos + h] / close[pos] - 1
            if spy_close is not None:
                s0, sh = idx[pos], idx[pos + h]
                try:
                    r -= float(spy_close.asof(sh) / spy_close.asof(s0) - 1)
                except (KeyError, TypeError, ZeroDivisionError):
                    pass
            out[h].append(float(r))
    return out


def _stats(values: list[float]) -> HorizonStats | None:
    if not values:
        return None
    return HorizonStats(n=len(values), median=statistics.median(values), mean=statistics.fmean(values),
                        hit_rate=sum(v > 0 for v in values) / len(values), worst=min(values), best=max(values))


def _source(name: str, rets: dict[int, list[float]], dates: list[str]) -> SourceResult | None:
    horizons = {h: s for h, vals in rets.items() if (s := _stats(vals)) is not None}
    if not horizons:
        return None
    return SourceResult(name=name, n=max(s.n for s in horizons.values()), horizons=horizons, dates=dates)


def price_reaction_dates(df: pd.DataFrame, today_move: float, min_abs_move: float = 0.03,
                         volume_ratio: float = 1.5, exclude_last: int = 10) -> list[str]:
    """Past days with a same-sign move of at least max(min_abs_move, 0.6x
    today's) on >= volume_ratio x trailing-20-day average volume."""
    idx = _naive_index(df)
    close = df["Close"].astype(float)
    ret = close.pct_change()
    vol = df["Volume"].astype(float) if "Volume" in df.columns else None
    vol_ratio = (vol / vol.rolling(20).mean()) if vol is not None else None
    threshold = max(min_abs_move, 0.6 * abs(today_move))
    sign = 1 if today_move >= 0 else -1
    out = []
    for i in range(21, len(close) - exclude_last):
        r = ret.iloc[i]
        if pd.isna(r) or r * sign < threshold:
            continue
        if vol_ratio is not None and not (pd.isna(vol_ratio.iloc[i]) or vol_ratio.iloc[i] >= volume_ratio):
            continue
        out.append(idx[i].strftime("%Y-%m-%d"))
    return out


def event_study(symbol: str, df: pd.DataFrame, event_types: list[str], today_move: float | None,
                spy: pd.DataFrame | None = None, calendar_entry: dict | None = None,
                edgar_events: list[tuple[str, list[str]]] | None = None,
                archive_dates: list[str] | None = None, min_samples: int = 5) -> EventStudy:
    """Assemble every applicable analog source for `symbol` given today's
    headline categories and move. All inputs are pre-fetched by the caller
    (so this is pure and testable); sources with no analogs are omitted."""
    sources: list[SourceResult] = []
    idx = _naive_index(df)
    last_day = idx[-1].strftime("%Y-%m-%d") if len(idx) else None

    past = (calendar_entry or {}).get("past") or []
    if "earnings_beat" in event_types or "guidance_raise" in event_types:
        dates = [d for d, s in past if s is not None and s > 0 and d != last_day]
        if (src := _source("earnings_beat", forward_returns(df, dates, spy), dates)):
            sources.append(src)
    if "earnings_miss" in event_types or "guidance_cut" in event_types:
        dates = [d for d, s in past if s is not None and s < 0 and d != last_day]
        if (src := _source("earnings_miss", forward_returns(df, dates, spy), dates)):
            sources.append(src)

    if edgar_events:
        wanted = {cat for t in event_types for cat in HEADLINE_TO_EDGAR.get(t, [])}
        wanted -= {"earnings"} if any(t.startswith("earnings") for t in event_types) else set()
        for cat in sorted(wanted):
            dates = sorted({d for d, cats in edgar_events if cat in cats and d != last_day})
            rets = forward_returns(df, dates, spy, match_sign=today_move if today_move else None)
            if (src := _source(f"edgar:{cat}", rets, dates)):
                sources.append(src)

    if archive_dates:
        dates = [d for d in archive_dates if d != last_day]
        if (src := _source("archive", forward_returns(df, dates, spy), dates)):
            sources.append(src)

    if today_move is not None and abs(today_move) > 0:
        dates = price_reaction_dates(df, today_move)
        if (src := _source("price_reaction", forward_returns(df, dates, spy), dates)):
            sources.append(src)

    return EventStudy(symbol=symbol, event_types=event_types, today_move=today_move, sources=sources)


def archive_signal_dates(score_history: dict, symbol: str, buy_threshold: float, min_headlines: int,
                         positive: bool = True) -> list[str]:
    """Days the bot's own archive had a buy-grade (or sell-grade) score."""
    out = []
    for day, by_sym in score_history.items():
        ns = by_sym.get(symbol)
        if ns is None or ns.n_scored < min_headlines:
            continue
        if (positive and ns.score >= buy_threshold) or (not positive and ns.score <= -buy_threshold):
            out.append(day)
    return sorted(out)


def run_event_study(symbol: str, headlines: list[Headline], prices_long: pd.DataFrame,
                    spy: pd.DataFrame | None, calendar: dict, score_history: dict,
                    buy_threshold: float, min_headlines: int, min_samples: int = 5,
                    use_edgar: bool = True, session: requests.Session | None = None) -> EventStudy:
    """Live/CLI convenience: classify the headlines, fetch what's fetchable
    (EDGAR; earnings come from the calendar cache), and run event_study()."""
    types = dominant_events(headlines)
    close = prices_long["Close"].astype(float)
    today_move = float(close.iloc[-1] / close.iloc[-2] - 1) if len(close) >= 2 else None
    edgar = None
    if use_edgar and any(t in HEADLINE_TO_EDGAR for t in types):
        try:
            edgar = fetch_edgar_events(symbol, session)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            log.warning("EDGAR fetch failed for %s: %s", symbol, exc)
    archive_dates = archive_signal_dates(score_history, symbol, buy_threshold, min_headlines,
                                         positive=(today_move or 0) >= 0)
    return event_study(symbol, prices_long, types, today_move, spy, calendar.get(symbol), edgar,
                       archive_dates, min_samples)
