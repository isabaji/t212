"""Weekly (or any-period) markdown report from the state files and, when
present, the local logs/history.jsonl.

Everything here is percentages, counts, dates and -1..1 scores -- the
same public-repo constraint as the state files it reads -- so the output
is safe to commit (`python main.py report --out reports/latest.md`, which
the weekly-report workflow does). It exists to answer the questions that
decide whether a bot should be armed: is it trading at all, how often,
with what hit rate, which skip-reasons dominate, and is the news archive
growing.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from . import daily_target, discovery, events, history, news, pnl_history, portfolio_risk, position_ownership, trade_stats

BOTS = ("swing", "daytrade", "news")


def _pct(x: float) -> str:
    return f"{x:+.2%}"


def _trade_block(trades: list) -> str:
    if not trades:
        return "no closed trades"
    wins = [t for t in trades if t["pnl_pct"] > 0]
    total = sum(t["pnl_pct"] for t in trades)
    return (f"{len(trades)} closed, win rate {len(wins) / len(trades):.0%}, "
            f"realized {_pct(total)} of account, avg {_pct(total / len(trades))}/trade")


def build_report(days: int = 7, today: date | None = None, history_path=None) -> str:
    today = today or datetime.now(timezone.utc).date()
    since = today - timedelta(days=days)
    since_iso = since.isoformat()
    lines = [f"# Bot report — {days} days to {today.isoformat()}", ""]

    # --- trades --------------------------------------------------------
    pnl_all = pnl_history.load()
    pnl = [t for t in pnl_all if isinstance(t.get("pnl_pct"), (int, float)) and math.isfinite(t["pnl_pct"])]
    lines += [f"## Trades (last {days} days)", ""]
    if len(pnl) != len(pnl_all):
        lines.append(f"- note: {len(pnl_all) - len(pnl)} ledger entr{'y' if len(pnl_all) - len(pnl) == 1 else 'ies'} "
                     "with a non-finite P&L excluded (a NaN price got into the bookkeeping at some point)")
    for bot in BOTS:
        recent = [t for t in pnl if t.get("strategy") == bot and t.get("t", "")[:10] >= since_iso]
        alltime = [t for t in pnl if t.get("strategy") == bot]
        lines.append(f"- **{bot}**: {_trade_block(recent)} (all-time: {_trade_block(alltime)})")
    lines.append("")

    # --- positions / pauses --------------------------------------------
    owners = position_ownership.load()
    by_owner = Counter(owners.values())
    lines += ["## Open positions by owner", ""]
    lines += [f"- {bot}: {by_owner.get(bot, 0)}" for bot in BOTS]
    lines.append("")

    tstats = trade_stats.load()
    # trade_stats clears an expired pause lazily (on the next is_paused call
    # for that symbol), so compare against today rather than trusting the field.
    paused = [(r["strategy"], r["symbol"], r["paused_until"]) for r in trade_stats.summary(tstats)
              if r["paused_until"] and r["paused_until"] > today.isoformat()]
    dt = daily_target.load()
    pr = portfolio_risk.load()
    lines += ["## Risk state", ""]
    lines.append(f"- Daily target/loss-limit: {_pct(dt.get('cumulative_pct', 0.0))} realized today; "
                 f"target hit: {dt.get('target_hit')}, loss limit hit: {dt.get('loss_limit_hit')}")
    lines.append(f"- Portfolio drawdown pause: {pr.get('paused', pr.get('entries_blocked', False))} "
                 f"(state: { {k: v for k, v in pr.items() if k != 'history'} })")
    lines.append("- Losing-streak pauses: " + (", ".join(f"{s} ({b}) until {u}" for b, s, u in paused) or "none"))
    lines.append("")

    # --- news archive --------------------------------------------------
    st = news.load_state()
    hist = news.score_history(st)
    arch_days = sorted(hist)
    lines += ["## News archive", ""]
    if not arch_days:
        lines.append("- empty — the news bot hasn't run yet")
    else:
        recent_days = [d for d in arch_days if d >= since_iso]
        lines.append(f"- {len(arch_days)} days archived ({arch_days[0]} → {arch_days[-1]}), {len(recent_days)} in this period")
        buy_grade = Counter()
        sell_grade = Counter()
        for d in recent_days:
            for sym, ns in hist[d].items():
                if ns.n_scored >= 3 and ns.score >= 0.35:
                    buy_grade[sym] += 1
                if ns.n_scored >= 3 and ns.score <= -0.35:
                    sell_grade[sym] += 1
        lines.append(f"- buy-grade symbol-days this period: {sum(buy_grade.values())}"
                     + (f" ({', '.join(f'{s}×{n}' for s, n in buy_grade.most_common(8))})" if buy_grade else ""))
        lines.append(f"- sell-grade symbol-days this period: {sum(sell_grade.values())}"
                     + (f" ({', '.join(f'{s}×{n}' for s, n in sell_grade.most_common(8))})" if sell_grade else ""))
        last = hist[arch_days[-1]]
        ranked = sorted((ns for ns in last.values() if ns.n_scored >= 3), key=lambda n: n.score)
        if ranked:
            lines.append(f"- latest day ({arch_days[-1]}): most negative "
                         + ", ".join(f"{n.symbol} {n.score:+.2f}" for n in ranked[:3])
                         + "; most positive " + ", ".join(f"{n.symbol} {n.score:+.2f}" for n in ranked[-3:][::-1]))
        srcs = Counter(v.get("src", "own") for d in recent_days for v in st["scores"][d].values())
        if srcs:
            lines.append("- score sources this period: " + ", ".join(f"{k} {v}" for k, v in srcs.most_common()))
    lines.append("")

    # --- discovered universe --------------------------------------------
    uni = discovery.load_universe()
    lines += ["## Discovered universe", ""]
    if not uni:
        lines.append("- empty — no discovery scan has run yet")
    else:
        recent = {s: e for s, e in uni.items() if (e.get("last_seen") or "") >= since_iso}
        eligible = [s for s, e in recent.items() if e.get("liquid")]
        rejected = Counter((e.get("reason") or "unchecked") for e in recent.values() if not e.get("liquid"))
        lines.append(f"- {len(uni)} names seen all-time, {len(recent)} in this period, {len(eligible)} eligible to trade")
        if rejected:
            lines.append("- not eligible: " + ", ".join(f"{r} ×{n}" for r, n in rejected.most_common()))
        newest = sorted(recent.items(), key=lambda kv: kv[1].get("first_seen") or "", reverse=True)[:10]
        if newest:
            lines.append("- newest: " + ", ".join(f"{s} ({e.get('name', s)}, {e.get('first_seen')})" for s, e in newest))
    lines.append("")

    # --- earnings calendar ---------------------------------------------
    cal = events.load_calendar()
    upcoming = events.upcoming_reports(cal, today, within_days=7)
    lines += ["## Earnings in the next 7 days", ""]
    lines.append("- " + (", ".join(f"{sym} ({d})" for d, sym in upcoming) if upcoming else
                         ("none on the watchlist" if cal else "calendar not populated yet")))
    lines.append("")

    # --- local cycle log (not available on CI) --------------------------
    path = history_path or history.HISTORY_PATH
    lines += ["## Cycle log", ""]
    if not path.exists():
        lines.append("- logs/history.jsonl not present here (it's local-only; run this report on the machine "
                     "that runs the bots, or read the Actions run logs)")
    else:
        cycles = Counter()
        errors = Counter()
        skips: dict[str, Counter] = defaultdict(Counter)
        buys = Counter()
        sells = Counter()
        with path.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("t", "")[:10] < since_iso:
                    continue
                bot = rec.get("bot", "?")
                cycles[bot] += 1
                if rec.get("error"):
                    errors[bot] += 1
                for d in rec.get("decisions", []):
                    if d.get("headline") == "Buy":
                        buys[bot] += 1
                    elif d.get("headline") == "Sell":
                        sells[bot] += 1
                    elif d.get("headline") == "Skipped" and d.get("symbol") is None:
                        reason = d.get("detail", "").split("—", 1)[-1].strip().rstrip(".")[:80]
                        skips[bot][reason] += 1
        for bot in BOTS:
            if not cycles[bot]:
                continue
            lines.append(f"- **{bot}**: {cycles[bot]} cycles, {errors[bot]} errors, "
                         f"{buys[bot]} buys, {sells[bot]} sells")
            for reason, n in skips[bot].most_common(5):
                lines.append(f"  - skipped: {reason} ×{n}")
        if not any(cycles[b] for b in BOTS):
            lines.append("- no cycles logged in this period")
    lines.append("")
    return "\n".join(lines)
