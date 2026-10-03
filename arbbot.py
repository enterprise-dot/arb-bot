#!/usr/bin/env python3
"""Sports betting arbitrage scanner.

Polls The Odds API (https://the-odds-api.com) for odds across many sportsbooks,
finds markets where the best price on every outcome comes from different books
such that the implied probabilities sum to less than 100% (a guaranteed-profit
"arb"), and posts an alert to a Discord webhook.

Built to stretch a monthly credit plan as far as it goes:
  * The free /events endpoint tells us when each game starts, so paid /odds calls
    are only made for sports with a game in progress (plus optional slower
    pre-game checks shortly before kickoff). Out-of-season sports cost nothing.
  * Finished games are detected when books pull them, so polling stops promptly.
  * A budget governor looks at the next 24h of schedule and your remaining
    credits, and stretches the polling interval only as much as needed to make
    the credits last until your plan resets.
  * All due sports are fetched in parallel, so an alert goes out seconds after
    the price moves into an arb.

Standard library only. Run `python arbbot.py --help`.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

API_BASE = os.environ.get("ODDS_API_BASE", "https://api.the-odds-api.com/v4")
HERE = Path(__file__).resolve().parent

# How long a game typically runs, by sport-key prefix (first match wins).
# Used to know when a game is "live"; finished games are also detected from the feed.
DEFAULT_GAME_MINUTES = [
    ("americanfootball", 215),
    ("basketball_ncaab", 140),
    ("basketball", 160),
    ("icehockey", 170),
    ("baseball", 210),
    ("soccer", 125),
    ("tennis", 180),
    ("mma", 360),
    ("boxing", 300),
]
FALLBACK_GAME_MINUTES = 180


# --------------------------------------------------------------------------- config

def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, no override of real env vars."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


@dataclass
class Config:
    api_key: str = ""
    webhook_url: str = ""
    sports: list[str] = field(default_factory=lambda: [
        "americanfootball_nfl", "basketball_nba", "icehockey_nhl", "baseball_mlb"])
    regions: str = "us"
    markets: str = "h2h,spreads,totals"
    bookmakers: str = ""          # optional comma list; overrides regions if set
    poll_seconds: int = 60        # fastest check rate for sports with live games
    pregame_minutes: int = 15     # check rate before kickoff (0 = live games only)
    pregame_hours: float = 2.0    # how far before kickoff pre-game checks start
    monthly_credits: int = 100_000
    billing_day: int = 1          # day of month your plan's credits reset
    events_refresh_minutes: int = 10
    game_minutes: dict[str, int] = field(default_factory=dict)  # per-sport override
    min_profit_pct: float = 0.5
    max_profit_pct: float = 15.0  # above this it's almost always a stale/bad line
    max_age_seconds: int = 120    # ignore book prices not updated this recently
    bankroll: float = 100.0
    round_stakes: float = 5       # round stakes to this many dollars (0 = exact cents)
    include_links: bool = True    # ask for bet-slip deep links where books support them
    discord_mention: str = ""     # e.g. @everyone or <@USER_ID> to force a phone ping
    status_webhook_url: str = ""  # health messages; defaults to the alerts webhook
    low_credits: int = 5000       # warn on Discord below this many credits
    log_file: str = "arbs.csv"    # every arb with how long it lasted ("" = off)
    summary_hour: int = 9         # local hour for the daily Discord summary (-1 = off)
    live_only: bool = False       # only alert on games in progress
    active_hours: str = ""        # e.g. "08:00-22:00"; empty = always on
    timezone: str = "America/New_York"

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        d = cls()
        return cls(
            api_key=e("ODDS_API_KEY", ""),
            webhook_url=e("DISCORD_WEBHOOK_URL", ""),
            sports=_csv(e("SPORTS", "")) or d.sports,
            regions=e("REGIONS", d.regions),
            markets=e("MARKETS", d.markets),
            bookmakers=e("BOOKMAKERS", ""),
            poll_seconds=int(e("POLL_SECONDS", d.poll_seconds)),
            pregame_minutes=int(e("PREGAME_MINUTES", d.pregame_minutes)),
            pregame_hours=float(e("PREGAME_HOURS", d.pregame_hours)),
            monthly_credits=int(e("MONTHLY_CREDITS", d.monthly_credits)),
            billing_day=int(e("BILLING_DAY", d.billing_day)),
            events_refresh_minutes=int(e("EVENTS_REFRESH_MINUTES", d.events_refresh_minutes)),
            game_minutes={k.strip(): int(v) for k, v in
                          (p.split("=") for p in _csv(e("GAME_MINUTES", "")))},
            min_profit_pct=float(e("MIN_PROFIT_PCT", d.min_profit_pct)),
            max_profit_pct=float(e("MAX_PROFIT_PCT", d.max_profit_pct)),
            max_age_seconds=int(e("MAX_AGE_SECONDS", d.max_age_seconds)),
            bankroll=float(e("BANKROLL", d.bankroll)),
            round_stakes=float(e("ROUND_STAKES", d.round_stakes)),
            include_links=e("INCLUDE_LINKS", "true").lower() in ("1", "true", "yes"),
            discord_mention=e("DISCORD_MENTION", ""),
            status_webhook_url=e("DISCORD_STATUS_WEBHOOK_URL", ""),
            low_credits=int(e("LOW_CREDITS", d.low_credits)),
            log_file=e("LOG_FILE", d.log_file),
            summary_hour=int(e("SUMMARY_HOUR", d.summary_hour)),
            live_only=e("LIVE_ONLY", "false").lower() in ("1", "true", "yes"),
            active_hours=e("ACTIVE_HOURS", "").strip(),
            timezone=e("TIMEZONE", d.timezone),
        )

    def credits_per_call(self) -> int:
        """The Odds API charges markets x regions; every 10 bookmakers count as one region."""
        n_markets = len(_csv(self.markets))
        if self.bookmakers:
            return n_markets * math.ceil(len(_csv(self.bookmakers)) / 10)
        return n_markets * len(_csv(self.regions))

    def minutes_for(self, sport: str) -> int:
        if sport in self.game_minutes:
            return self.game_minutes[sport]
        for prefix, mins in DEFAULT_GAME_MINUTES:
            if sport.startswith(prefix):
                return mins
        return FALLBACK_GAME_MINUTES


# --------------------------------------------------------------------------- data

@dataclass
class Leg:
    outcome: str
    price: float      # decimal odds
    book: str
    stake: float = 0.0
    link: str = ""    # deep link to the bet slip, when the book provides one


@dataclass
class Arb:
    event_id: str
    sport: str
    matchup: str
    commence_time: str
    is_live: bool
    market: str
    line: float | None
    legs: list[Leg]
    margin: float     # sum of 1/price; < 1.0 means arb
    sport_key: str = ""

    @property
    def profit_pct(self) -> float:
        return (1 / self.margin - 1) * 100

    @property
    def key(self) -> str:
        return f"{self.event_id}|{self.market}|{self.line}"

    @property
    def fingerprint(self) -> str:
        return self.key + "|" + ",".join(f"{l.book}@{l.price}" for l in self.legs)

    def set_stakes(self, bankroll: float, round_to: float = 0) -> None:
        """Split the bankroll so every outcome pays about the same.

        With round_to (e.g. 5), stakes are rounded to natural-looking amounts like $55
        instead of $57.65 (exact amounts are a giveaway to books). If rounding would kill
        the profit, it falls back to $1 rounding, then to exact cents.
        """
        exact = [bankroll * (1 / l.price) / self.margin for l in self.legs]
        for unit in ([round_to, 1] if round_to > 0 else []) + [0.01]:
            stakes = [max(unit, round(x / unit) * unit) for x in exact]
            stakes = [round(x, 2) for x in stakes]
            if min(st * l.price for st, l in zip(stakes, self.legs)) > sum(stakes):
                break
        for leg, st in zip(self.legs, stakes):
            leg.stake = st

    @property
    def total_stake(self) -> float:
        return round(sum(l.stake for l in self.legs), 2)

    @property
    def guaranteed_return(self) -> float:
        return round(min(l.stake * l.price for l in self.legs), 2)

    @property
    def guaranteed_profit(self) -> float:
        return round(self.guaranteed_return - self.total_stake, 2)


# --------------------------------------------------------------------------- odds feed

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class OddsAPI:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.remaining: float | None = None
        self.used: float | None = None
        self._warned_events_cost = False

    def _get(self, path: str, params: dict) -> list | dict:
        params = {"apiKey": self.cfg.api_key, **params}
        url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "arbbot/2.0", "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            if (rem := resp.headers.get("x-requests-remaining")) is not None:
                self.remaining = float(rem)
            if (used := resp.headers.get("x-requests-used")) is not None:
                self.used = float(used)
            return json.loads(raw)

    def events(self, sport: str, horizon_hours: float = 26) -> list[dict]:
        """Live + upcoming games for a sport. Free: doesn't use credits."""
        used_before = self.used
        data = self._get(f"/sports/{sport}/events", {
            "dateFormat": "iso",
            "commenceTimeTo": _iso(datetime.now(timezone.utc) + timedelta(hours=horizon_hours)),
        })
        if used_before is not None and self.used is not None and self.used > used_before \
                and not self._warned_events_cost:
            self._warned_events_cost = True
            print("! The events endpoint used credits. Raise EVENTS_REFRESH_MINUTES.", file=sys.stderr)
        return data

    def odds(self, sport: str, until: datetime) -> list[dict]:
        """Odds for every game starting before `until` (and all live ones). Costs credits."""
        params = {
            "markets": self.cfg.markets,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
            "commenceTimeTo": _iso(until),  # trims the payload; same credit cost
        }
        if self.cfg.bookmakers:
            params["bookmakers"] = self.cfg.bookmakers
        else:
            params["regions"] = self.cfg.regions
        if self.cfg.include_links:
            params["includeLinks"] = "true"
        return self._get(f"/sports/{sport}/odds", params)


# --------------------------------------------------------------------------- detection

def _line_for(market: str, outcome: dict, home_team: str) -> float | None:
    """Return a key so that both sides of the same line group together.

    spreads: Home -3.5 pairs with Away +3.5, so key on the home team's point.
    totals:  Over 220.5 pairs with Under 220.5, so key on the point.
    """
    point = outcome.get("point")
    if point is None:
        return None
    if market == "spreads":
        return point if outcome["name"] == home_team else -point
    return point


def find_arbs(events: list[dict], cfg: Config, now: datetime | None = None) -> list[Arb]:
    now = now or datetime.now(timezone.utc)
    arbs: list[Arb] = []

    for ev in events:
        start = _parse_time(ev["commence_time"])
        is_live = start <= now
        if cfg.live_only and not is_live:
            continue

        # (market, line) -> outcome name -> best Leg
        best: dict[tuple, dict[str, Leg]] = {}
        # (market, line) -> most outcomes any single book offers (2 or 3-way)
        n_outcomes: dict[tuple, int] = {}

        for bm in ev.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                updated = mkt.get("last_update") or bm.get("last_update")
                if updated and (now - _parse_time(updated)).total_seconds() > cfg.max_age_seconds:
                    continue  # stale price, likely already moved
                per_line: dict[tuple, int] = {}
                for oc in mkt.get("outcomes", []):
                    price = float(oc.get("price") or 0)
                    if price <= 1.0:
                        continue
                    k = (mkt["key"], _line_for(mkt["key"], oc, ev["home_team"]))
                    per_line[k] = per_line.get(k, 0) + 1
                    slot = best.setdefault(k, {})
                    cur = slot.get(oc["name"])
                    if cur is None or price > cur.price:
                        link = oc.get("link") or mkt.get("link") or bm.get("link") or ""
                        slot[oc["name"]] = Leg(oc["name"], price, bm.get("title", bm["key"]), link=link)
                for k, n in per_line.items():
                    n_outcomes[k] = max(n_outcomes.get(k, 0), n)

        for (market, line), legs_by_name in best.items():
            legs = list(legs_by_name.values())
            # Need every outcome covered (e.g. don't miss the Draw in soccer).
            if len(legs) < 2 or len(legs) < n_outcomes.get((market, line), 0):
                continue
            if len({l.book for l in legs}) < 2:
                continue
            margin = sum(1 / l.price for l in legs)
            if margin >= 1:
                continue
            arb = Arb(
                event_id=ev["id"],
                sport=ev.get("sport_title", ev.get("sport_key", "")),
                matchup=f"{ev['away_team']} @ {ev['home_team']}",
                commence_time=ev["commence_time"],
                is_live=is_live,
                market=market,
                line=line,
                legs=sorted(legs, key=lambda l: l.outcome),
                margin=margin,
                sport_key=ev.get("sport_key", ""),
            )
            if cfg.min_profit_pct <= arb.profit_pct <= cfg.max_profit_pct:
                arb.set_stakes(cfg.bankroll, cfg.round_stakes)
                arbs.append(arb)

    return sorted(arbs, key=lambda a: a.profit_pct, reverse=True)


# --------------------------------------------------------------------------- alerts

MARKET_NAMES = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}


def _line_label(arb: Arb) -> str:
    if arb.line is None:
        return ""
    return f" {arb.line:+g}" if arb.market == "spreads" else f" {arb.line:g}"


def _fmt_secs(s: float) -> str:
    s = int(round(s))
    return f"{s}s" if s < 90 else f"{s // 60}m {s % 60:02d}s"


def format_text(arb: Arb) -> str:
    status = "🔴 LIVE" if arb.is_live else f"starts {arb.commence_time}"
    rows = "\n".join(
        f"  • {l.outcome} @ {l.price:.2f} on {l.book}  → stake ${l.stake:g}"
        + (f"\n    {l.link}" if l.link else "")
        for l in arb.legs
    )
    return (
        f"💰 {arb.profit_pct:.2f}% ARB | {arb.sport} | {arb.matchup} ({status})\n"
        f"  {MARKET_NAMES.get(arb.market, arb.market)}{_line_label(arb)}\n{rows}\n"
        f"  Total ${arb.total_stake:g} → returns ≥ ${arb.guaranteed_return:.2f} "
        f"(+${arb.guaranteed_profit:.2f})"
    )


def discord_payload(arb: Arb, mention: str = "", gone_after: float | None = None,
                    first_seen: float | None = None) -> dict:
    """Embed for an arb. With gone_after set, renders the 'closed' version of the message."""
    gone = gone_after is not None
    title = f"💰 {arb.profit_pct:.2f}% arb: {arb.matchup}"
    if gone:
        title = f"❌ GONE after {_fmt_secs(gone_after)} · ~~{arb.profit_pct:.2f}%~~ {arb.matchup}"
    when = ("🔴 **LIVE**" if arb.is_live
            else f"starts <t:{int(_parse_time(arb.commence_time).timestamp())}:R>")
    seen = f" · first seen <t:{int(first_seen)}:R>" if first_seen and not gone else ""
    fields = [
        {
            "name": f"{l.outcome} @ {l.price:.2f}",
            "value": f"**{l.book}**\nStake **${l.stake:g}**"
                     + (f"\n[Open bet slip]({l.link})" if l.link and not gone else ""),
            "inline": True,
        }
        for l in arb.legs
    ]
    fields.append({
        "name": "Result",
        "value": f"Stake ${arb.total_stake:g} → return ≥ ${arb.guaranteed_return:.2f} "
                 f"(**+${arb.guaranteed_profit:.2f}**)",
        "inline": False,
    })
    payload = {
        "username": "Arb Bot",
        "embeds": [{
            "title": title[:256],
            "description": f"**{arb.sport}** · {MARKET_NAMES.get(arb.market, arb.market)}"
                           f"{_line_label(arb)} · {when}{seen}",
            "color": 0x95A5A6 if gone else (0xE74C3C if arb.is_live else 0x2ECC71),
            "fields": fields,
            "footer": {"text": "Prices moved; don't bet this one." if gone
                       else "Check both prices before betting; odds move fast."},
        }],
        "allowed_mentions": {"parse": ["everyone", "roles", "users"]},
    }
    if mention and not gone:
        payload["content"] = mention
    return payload


def _webhook(url: str, payload: dict, method: str = "POST", message_id: str | None = None) -> dict | None:
    """POST a new message (returns it, incl. id) or PATCH an existing one."""
    target = f"{url}/messages/{message_id}" if message_id else f"{url}?wait=true"
    req = urllib.request.Request(
        target, data=json.dumps(payload).encode(), method=method,
        headers={"Content-Type": "application/json", "User-Agent": "arbbot/3.0"},
    )
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            if e.code == 429:  # Discord rate limit
                time.sleep(float(e.headers.get("Retry-After", "1")))
                continue
            raise
    return None


def send_discord(webhook_url: str, arb: Arb, mention: str = "") -> str | None:
    msg = _webhook(webhook_url, discord_payload(arb, mention))
    return msg.get("id") if msg else None


@dataclass
class OpenArb:
    arb: Arb
    first_seen: float
    last_seen: float
    best_pct: float
    message_id: str | None = None


LOG_FIELDS = ["first_seen", "gone_at", "seconds_open", "sport", "matchup", "market", "line",
              "live", "best_profit_pct", "legs"]


class Alerter:
    """Tracks arbs from first sighting until they close.

    New arb -> new Discord message (with optional @mention).
    Prices change while still an arb -> the same message is edited in place.
    Arb disappears -> the message is edited to "GONE after Ns" and logged to CSV.
    """

    def __init__(self, cfg: Config, dry_run: bool):
        self.cfg = cfg
        self.dry_run = dry_run or not cfg.webhook_url
        self.open: dict[str, OpenArb] = {}
        self.stats = {"found": 0, "closed": 0, "open_seconds": 0.0, "best_pct": 0.0, "best": ""}

    def _discord(self, payload: dict, message_id: str | None = None) -> str | None:
        if self.dry_run:
            return None
        try:
            if message_id:
                _webhook(self.cfg.webhook_url, payload, "PATCH", message_id)
                return message_id
            msg = _webhook(self.cfg.webhook_url, payload)
            return msg.get("id") if msg else None
        except Exception as e:  # keep scanning even if Discord hiccups
            print(f"  ! Discord send failed: {e}", file=sys.stderr)
            return message_id

    def handle(self, arbs: list[Arb], checked_sports: list[str] | None = None,
               now: float | None = None) -> int:
        """Process one scan's arbs. checked_sports = sports whose odds were just fetched;
        open arbs in those sports that weren't found again are closed."""
        now = now or time.time()
        new = 0
        seen = set()
        for arb in arbs:
            seen.add(arb.key)
            cur = self.open.get(arb.key)
            if cur is None:
                print(format_text(arb), flush=True)
                op = OpenArb(arb, now, now, arb.profit_pct)
                op.message_id = self._discord(discord_payload(arb, self.cfg.discord_mention, first_seen=now))
                self.open[arb.key] = op
                self.stats["found"] += 1
                if arb.profit_pct > self.stats["best_pct"]:
                    self.stats["best_pct"], self.stats["best"] = arb.profit_pct, arb.matchup
                new += 1
            else:
                changed = cur.arb.fingerprint != arb.fingerprint
                cur.arb, cur.last_seen = arb, now
                cur.best_pct = max(cur.best_pct, arb.profit_pct)
                if changed:
                    print(f"  ↻ updated: {arb.matchup} {arb.market} now {arb.profit_pct:.2f}%", flush=True)
                    self._discord(discord_payload(arb, first_seen=cur.first_seen), cur.message_id)

        checked = set(checked_sports) if checked_sports is not None else None
        for key in list(self.open):
            op = self.open[key]
            if key in seen or (checked is not None and op.arb.sport_key not in checked):
                continue
            self._close(key, now)
        return new

    def _close(self, key: str, now: float) -> None:
        op = self.open.pop(key)
        lasted = now - op.first_seen  # first seen -> first check where it was gone
        print(f"  ❌ gone after {_fmt_secs(lasted)}: {op.arb.matchup} {op.arb.market}", flush=True)
        if op.message_id:
            self._discord(discord_payload(op.arb, gone_after=lasted), op.message_id)
        self.stats["closed"] += 1
        self.stats["open_seconds"] += lasted
        self._log(op, now, lasted)

    def _log(self, op: OpenArb, now: float, lasted: float) -> None:
        if not self.cfg.log_file:
            return
        path = Path(self.cfg.log_file)
        if not path.is_absolute():
            path = HERE / path
        a = op.arb
        row = {
            "first_seen": datetime.fromtimestamp(op.first_seen, timezone.utc).isoformat(timespec="seconds"),
            "gone_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
            "seconds_open": round(lasted),
            "sport": a.sport, "matchup": a.matchup, "market": a.market,
            "line": "" if a.line is None else a.line, "live": a.is_live,
            "best_profit_pct": round(op.best_pct, 2),
            "legs": "; ".join(f"{l.outcome} @{l.price} {l.book}" for l in a.legs),
        }
        new_file = not path.exists()
        try:
            with path.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
                if new_file:
                    w.writeheader()
                w.writerow(row)
        except OSError as e:
            print(f"  ! Couldn't write {path}: {e}", file=sys.stderr)

    def summary(self) -> str:
        s = self.stats
        avg = s["open_seconds"] / s["closed"] if s["closed"] else 0
        best = f", best {s['best_pct']:.2f}% ({s['best']})" if s["found"] else ""
        return f"{s['found']} arbs found{best}, open {_fmt_secs(avg)} on average"

    def reset_stats(self) -> None:
        self.stats = {"found": 0, "closed": 0, "open_seconds": 0.0, "best_pct": 0.0, "best": ""}


class Status:
    """Bot health messages (online, low credits, crashes, daily summary) to Discord."""

    def __init__(self, cfg: Config, dry_run: bool):
        self.url = cfg.status_webhook_url or cfg.webhook_url
        self.dry_run = dry_run or not self.url
        self.cfg = cfg
        self.low_warned = False
        self.budget_warned = False

    def send(self, text: str) -> None:
        print(f"[status] {text}", flush=True)
        if self.dry_run:
            return
        try:
            _webhook(self.url, {"username": "Arb Bot", "content": text[:1900]})
        except Exception as e:
            print(f"  ! Status message failed: {e}", file=sys.stderr)

    def check_credits(self, remaining: float | None) -> None:
        if remaining is None or not self.cfg.low_credits:
            return
        if remaining < self.cfg.low_credits and not self.low_warned:
            self.low_warned = True
            self.send(f"⚠️ Only {remaining:,.0f} Odds API credits left. The bot will slow down to "
                      f"make them last until the plan resets.")
        elif remaining >= self.cfg.low_credits:
            self.low_warned = False


# --------------------------------------------------------------------------- schedule

def seconds_until_active(cfg: Config, now: datetime | None = None) -> float:
    """0 if inside ACTIVE_HOURS (or none set), else seconds until the window opens.

    Handles windows that cross midnight, e.g. "18:00-02:00".
    """
    if not cfg.active_hours:
        return 0.0
    tz = ZoneInfo(cfg.timezone)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    start_s, end_s = cfg.active_hours.split("-")
    start, end = dtime.fromisoformat(start_s.strip()), dtime.fromisoformat(end_s.strip())
    t = now.time()
    inside = start <= t < end if start < end else (t >= start or t < end)
    if inside:
        return 0.0
    nxt = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


def next_reset(cfg: Config, now: datetime) -> datetime:
    """When the plan's credits next reset (BILLING_DAY at midnight UTC)."""
    day = min(cfg.billing_day, 28)
    cand = now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    if cand <= now:
        y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        cand = cand.replace(year=y, month=m)
    return cand


LIVE, PREGAME = "live", "pregame"
STEP = 300  # seconds per slice when forecasting the next 24h


class Scheduler:
    """Decides which sports to fetch when, spending credits only where games are on."""

    def __init__(self, cfg: Config, api: OddsAPI):
        self.cfg = cfg
        self.api = api
        self.cost = cfg.credits_per_call()
        self.games: dict[str, list[tuple[str, datetime]]] = {s: [] for s in cfg.sports}
        self.events_at: dict[str, float] = {s: 0.0 for s in cfg.sports}
        self.last_odds: dict[str, float] = {s: 0.0 for s in cfg.sports}
        self.misses: dict[str, int] = {}
        self.ended: set[str] = set()
        self.scale = 1.0
        self.forecast = 0.0      # credits the next 24h would cost at full speed
        self.allowance = 0.0     # credits we can afford per day
        self.budget_at = 0.0

    # ---- game state

    def state(self, sport: str, now: datetime) -> str | None:
        dur = timedelta(minutes=self.cfg.minutes_for(sport))
        soon = timedelta(hours=self.cfg.pregame_hours)
        pregame = False
        for gid, start in self.games[sport]:
            if gid in self.ended:
                continue
            if start <= now < start + dur:
                return LIVE
            if self.cfg.pregame_minutes and now < start <= now + soon:
                pregame = True
        return PREGAME if pregame else None

    def interval(self, st: str) -> float:
        base = self.cfg.poll_seconds if st == LIVE else self.cfg.pregame_minutes * 60
        return base * self.scale

    def refresh_events(self, force: bool = False) -> None:
        now = time.time()
        for sport in self.cfg.sports:
            if not force and now - self.events_at[sport] < self.cfg.events_refresh_minutes * 60:
                continue
            try:
                evs = self.api.events(sport)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise
                print(f"! Couldn't load {sport} schedule: {e.code}", file=sys.stderr)
                continue
            except urllib.error.URLError as e:
                print(f"! Network error loading {sport} schedule: {e.reason}", file=sys.stderr)
                continue
            self.games[sport] = [(ev["id"], _parse_time(ev["commence_time"])) for ev in evs]
            self.events_at[sport] = now

    def note_feed(self, sport: str, events: list[dict], now: datetime) -> None:
        """Mark live games ended once books stop listing them (two fetches in a row)."""
        listed = {ev["id"] for ev in events if ev.get("bookmakers")}
        dur = timedelta(minutes=self.cfg.minutes_for(sport))
        for gid, start in self.games[sport]:
            if not (start <= now < start + dur) or gid in self.ended:
                continue
            if gid in listed:
                self.misses.pop(gid, None)
            else:
                self.misses[gid] = self.misses.get(gid, 0) + 1
                if self.misses[gid] >= 2:
                    self.ended.add(gid)

    # ---- budget

    def update_budget(self, now: datetime) -> None:
        """Pick the smallest slow-down that makes credits last until the plan resets."""
        remaining = self.api.remaining if self.api.remaining is not None else self.cfg.monthly_credits
        reserve = 0.02 * remaining  # small cushion for forecast misses
        days_left = max(1.0, (next_reset(self.cfg, now) - now).total_seconds() / 86400)
        self.allowance = max(0.0, remaining - reserve) / days_left

        demand = 0.0
        for step in range(0, 86400, STEP):
            t = now + timedelta(seconds=step)
            if seconds_until_active(self.cfg, t):
                continue
            for sport in self.cfg.sports:
                st = self.state(sport, t)
                if st == LIVE:
                    demand += STEP * self.cost / self.cfg.poll_seconds
                elif st == PREGAME:
                    demand += STEP * self.cost / (self.cfg.pregame_minutes * 60)
        self.forecast = demand
        if self.allowance <= 0:
            self.scale = math.inf
        else:
            self.scale = max(1.0, demand / self.allowance)
        self.budget_at = time.time()

    # ---- polling

    def due(self, now: datetime) -> list[str]:
        ts = time.time()
        out = []
        for sport in self.cfg.sports:
            st = self.state(sport, now)
            if st and ts - self.last_odds[sport] >= self.interval(st):
                out.append(sport)
        return out

    def seconds_to_next(self, now: datetime) -> float:
        ts = time.time()
        waits = [self.last_odds[s] + self.interval(st) - ts
                 for s in self.cfg.sports if (st := self.state(s, now))]
        return min([15.0] + waits)

    def fetch(self, sports: list[str], now: datetime) -> list[dict]:
        until = now + timedelta(hours=self.cfg.pregame_hours if self.cfg.pregame_minutes else 0, minutes=1)

        def one(sport: str) -> tuple[str, list[dict] | Exception]:
            try:
                return sport, self.api.odds(sport, until)
            except Exception as e:  # noqa: BLE001 - reported below
                return sport, e

        events: list[dict] = []
        with ThreadPoolExecutor(max_workers=len(sports)) as pool:
            for sport, result in pool.map(one, sports):
                self.last_odds[sport] = time.time()
                if isinstance(result, urllib.error.HTTPError):
                    if result.code in (401, 429):
                        raise result
                    print(f"! Odds error for {sport}: {result.code}", file=sys.stderr)
                elif isinstance(result, Exception):
                    print(f"! Odds error for {sport}: {result}", file=sys.stderr)
                else:
                    self.note_feed(sport, result, now)
                    events.extend(result)
        return events


# --------------------------------------------------------------------------- main loop

def demo_events() -> list[dict]:
    """Sample data with one planted arb, re-stamped as fresh so it passes the age filter."""
    data = json.loads((HERE / "sample_odds.json").read_text())
    fresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for ev in data:
        for bm in ev["bookmakers"]:
            bm["last_update"] = fresh
            for m in bm["markets"]:
                m["last_update"] = fresh
    return data


def short(sport: str) -> str:
    return sport.split("_", 1)[-1].upper()


def print_plan(cfg: Config, sched: Scheduler) -> None:
    now = datetime.now(timezone.utc)
    sched.refresh_events(force=True)
    sched.update_budget(now)
    tz = ZoneInfo(cfg.timezone)
    print(f"\nCredits per check: {sched.cost} ({cfg.markets} × "
          f"{'bookmakers' if cfg.bookmakers else cfg.regions})")
    print(f"Credits left: {sched.api.remaining if sched.api.remaining is not None else '?'} "
          f"| plan resets {next_reset(cfg, now):%b %d} (UTC)")
    print("\nNext 24h, when each sport will be checked (L = live every "
          f"{cfg.poll_seconds}s, p = pre-game every {cfg.pregame_minutes}m):")
    hours = [now + timedelta(hours=h) for h in range(24)]
    print("            " + "".join(f"{h.astimezone(tz):%H}"[0] for h in hours))
    print("            " + "".join(f"{h.astimezone(tz):%H}"[1] for h in hours))
    for sport in cfg.sports:
        row = ""
        for h in hours:
            states = {sched.state(sport, h + timedelta(minutes=m)) for m in range(0, 60, 10)}
            row += "L" if LIVE in states else ("p" if PREGAME in states else "·")
        n = len(sched.games[sport])
        print(f"  {short(sport):<9} {row}   {n} game{'s' if n != 1 else ''}")
    print(f"\nFull speed would use {sched.forecast:,.0f} credits in the next 24h; "
          f"you can afford {sched.allowance:,.0f}/day.")
    if sched.scale > 1:
        print(f"→ Will slow down {sched.scale:.2f}×: live checks every "
              f"{sched.interval(LIVE):.0f}s instead of {cfg.poll_seconds}s.")
    else:
        print(f"→ Fits the budget at full speed (live checks every {cfg.poll_seconds}s).")


def run(cfg: Config, args: argparse.Namespace, status: Status) -> None:
    alerter = Alerter(cfg, dry_run=args.dry_run)

    if args.demo:
        arbs = find_arbs(demo_events(), cfg)
        alerter.handle(arbs)
        print(f"[demo] {len(arbs)} arb(s) in sample data")
        return

    api = OddsAPI(cfg)
    sched = Scheduler(cfg, api)

    if args.plan:
        print_plan(cfg, sched)
        return

    mode = "dry run (console only)" if alerter.dry_run else "sending to Discord"
    print(f"Arb bot started: {', '.join(short(s) for s in cfg.sports)} | {cfg.markets} | "
          f"{sched.cost} credits/check | {mode}")
    if cfg.active_hours:
        print(f"Active hours: {cfg.active_hours} ({cfg.timezone})")
    if not args.once:
        status.send(f"🟢 Arb bot online: watching {', '.join(short(s) for s in cfg.sports)} "
                    f"({cfg.markets}).")

    tz = ZoneInfo(cfg.timezone)
    summary_day = datetime.now(tz).date()
    idle_logged = False
    while True:
        now = datetime.now(timezone.utc)
        local = now.astimezone(tz)
        if (cfg.summary_hour >= 0 and not args.once and local.hour >= cfg.summary_hour
                and local.date() != summary_day):
            summary_day = local.date()
            left = f"{api.remaining:,.0f}" if api.remaining is not None else "?"
            status.send(f"📊 Last 24h: {alerter.summary()}. Credits left: {left}.")
            alerter.reset_stats()

        wait = seconds_until_active(cfg, now)
        if wait:
            if args.once:
                sys.exit(f"Outside ACTIVE_HOURS ({cfg.active_hours}); not scanning.")
            print(f"Outside active hours. Sleeping {wait / 3600:.1f}h.", flush=True)
            time.sleep(wait)
            continue

        refreshed = any(time.time() - t >= cfg.events_refresh_minutes * 60 for t in sched.events_at.values())
        sched.refresh_events()
        if refreshed or time.time() - sched.budget_at > 300:
            old = sched.scale
            sched.update_budget(now)
            if sched.scale == math.inf:
                if not status.budget_warned:
                    status.budget_warned = True
                    status.send("⛔ Out of Odds API credits until the plan resets. Pausing.")
                time.sleep(3600)
                continue
            status.budget_warned = False
            if abs(sched.scale - old) > 0.05:
                print(f"Budget: {sched.allowance:,.0f} credits/day, next 24h needs "
                      f"{sched.forecast:,.0f} at full speed → live checks every "
                      f"{sched.interval(LIVE):.0f}s", flush=True)

        due = [s for s in cfg.sports if sched.state(s, now)] if args.once else sched.due(now)
        if due:
            idle_logged = False
            t0 = time.time()
            events = sched.fetch(due, now)
            arbs = find_arbs(events, cfg)
            sent = alerter.handle(arbs, checked_sports=due)
            status.check_credits(api.remaining)
            left = f"{api.remaining:,.0f}" if api.remaining is not None else "?"
            print(f"[{datetime.now():%H:%M:%S}] checked {', '.join(short(s) for s in due)} "
                  f"({len(events)} games, {time.time() - t0:.1f}s) | {len(arbs)} arbs, {sent} new | "
                  f"credits left {left}", flush=True)
        elif not idle_logged:
            idle_logged = True
            print(f"[{datetime.now():%H:%M:%S}] No games live or starting soon. Waiting (no credits used).",
                  flush=True)

        if args.once:
            if not due:
                print("Nothing live or starting soon to check right now.")
            return
        time.sleep(max(1.0, sched.seconds_to_next(now)))


def main() -> None:
    load_dotenv(HERE / ".env")
    p = argparse.ArgumentParser(description="Find sports betting arbitrage and alert on Discord.")
    p.add_argument("--plan", action="store_true",
                   help="show the next 24h schedule and credit forecast (uses no credits)")
    p.add_argument("--once", action="store_true", help="check every live/upcoming sport once and exit")
    p.add_argument("--demo", action="store_true", help="use bundled sample data (no API key needed)")
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending to Discord")
    p.add_argument("--test-discord", action="store_true", help="send one sample alert to Discord and exit")
    args = p.parse_args()

    cfg = Config.from_env()

    if args.test_discord:
        if not cfg.webhook_url:
            sys.exit("Set DISCORD_WEBHOOK_URL in .env first.")
        arb = find_arbs(demo_events(), cfg)[0]
        msg_id = send_discord(cfg.webhook_url, arb, cfg.discord_mention)
        print("Sent a sample alert. In 5 seconds it will change to 'GONE'...")
        time.sleep(5)
        if msg_id:
            _webhook(cfg.webhook_url, discord_payload(arb, gone_after=5), "PATCH", msg_id)
        print("Done. Check your Discord channel.")
        return

    if not args.demo and not cfg.api_key:
        print("Set ODDS_API_KEY in .env (key at https://the-odds-api.com), or run with --demo.", file=sys.stderr)
        sys.exit(2)

    status = Status(cfg, dry_run=args.dry_run or args.demo or args.plan or args.once)
    while True:
        try:
            run(cfg, args, status)
            return
        except urllib.error.HTTPError as e:
            if e.code == 401:
                status.send("🔴 Odds API rejected the key (401). Check ODDS_API_KEY. Bot stopped.")
                print("Odds API rejected the key (401). Check ODDS_API_KEY.", file=sys.stderr)
                sys.exit(2)  # config problem: the service won't keep restarting
            if e.code == 429:
                status.send("⏸️ Odds API says rate-limited or out of credits (429). Retrying in 15 min.")
                time.sleep(900)
                continue
            status.send(f"🔴 Bot crashed: HTTP {e.code}. Restarting automatically if run as a service.")
            raise
        except KeyboardInterrupt:
            raise
        except Exception as e:
            status.send(f"🔴 Bot crashed: {type(e).__name__}: {e}. "
                        "Restarting automatically if run as a service.")
            raise


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
