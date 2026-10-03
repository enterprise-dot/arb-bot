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
import itertools
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
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

# Prop markets checked per sport (each one costs a credit per game per check).
DEFAULT_PROP_MARKETS = {
    "americanfootball_nfl": "player_pass_yds,player_rush_yds,player_reception_yds,player_receptions",
    "americanfootball_ncaaf": "player_pass_yds,player_rush_yds,player_reception_yds",
    "basketball_nba": "player_points,player_rebounds,player_assists,player_threes",
    "icehockey_nhl": "player_points,player_shots_on_goal,player_assists",
    "baseball_mlb": "batter_hits,batter_total_bases,pitcher_strikeouts",
}


# --------------------------------------------------------------------------- config

WEBHOOK_SETTINGS = {
    "main": ("DISCORD_WEBHOOK_URL", "arbs (and anything without its own channel)"),
    "ev": ("DISCORD_EV_WEBHOOK_URL", "+EV bets, props, outliers and parlays"),
    "outlier": ("DISCORD_OUTLIER_WEBHOOK_URL", "outliers"),
    "parlay": ("DISCORD_PARLAY_WEBHOOK_URL", "parlays"),
    "live": ("DISCORD_LIVE_WEBHOOK_URL", "live arbs"),
    "status": ("DISCORD_STATUS_WEBHOOK_URL", "bot health messages"),
}


def set_env_value(path: Path, key: str, value: str) -> None:
    """Replace KEY=... in .env (or add it), keeping every other line as it is."""
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [l for l in lines if not l.strip().startswith(key + "=")]
    lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")


def set_webhook(channel: str) -> None:
    """Ask for a webhook URL, check it, save it to .env and send a test message."""
    key, what = WEBHOOK_SETTINGS[channel]
    print(f"Paste the Discord webhook URL for {what}, then press Enter:")
    url = input("> ").strip().strip("'\"")
    if not url.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
        sys.exit("That doesn't look like a Discord webhook URL (it should start with "
                 "https://discord.com/api/webhooks/). Nothing was changed.")
    try:
        _webhook(url, {"username": "Arb Bot", "content": f"✅ Connected. This channel now gets {what}."})
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Discord rejected that URL ({e}). Copy it again from the channel's webhook settings. "
                 "Nothing was changed.")
    set_env_value(HERE / ".env", key, url)
    print(f"Saved {key}. A ✅ test message was sent to that channel.")
    print("Now restart the bot so it uses it:  systemctl restart arbbot")


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, no override of real env vars."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        while value.startswith(key + "="):  # forgive "SPORTS=SPORTS=..." from pasting a whole line
            value = value[len(key) + 1:]
        os.environ.setdefault(key, value)


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
    # Up to 10 books cost the same as one region. Pinnacle is read only, as the sharp
    # reference for +EV; you never bet there.
    bookmakers: str = "pinnacle,draftkings,fanduel,betmgm,williamhill_us,espnbet,betrivers,fanatics,hardrockbet,ballybet"
    poll_seconds: int = 60        # fastest check rate for sports with live games
    pregame_minutes: int = 15     # check rate before kickoff (0 = live games only)
    pregame_hours: float = 2.0    # how far before kickoff pre-game checks start
    monthly_credits: int = 100_000
    billing_day: int = 1          # day of month your plan's credits reset
    events_refresh_minutes: int = 10
    game_minutes: dict[str, int] = field(default_factory=dict)  # per-sport override
    min_profit_pct: float = 0.5
    min_profit_dollars: float = 0.0  # skip arbs that lock in less than this (at BANKROLL)
    max_profit_pct: float = 15.0  # above this it's almost always a stale/bad line
    max_age_seconds: int = 120    # live games: ignore prices not updated this recently
    pregame_max_age_seconds: int = 900  # pre-game lines can sit unchanged for a while
    realert_jump_pct: float = 2.5  # send a fresh alert if the edge grows by this many points
    bankroll: float = 100.0
    round_stakes: float = 5       # round stakes to this many dollars (0 = exact cents)
    round_keep_pct: float = 85    # only use a rounding that keeps this much of the exact edge
    arb_live: bool = True         # alert on arbs in games already in progress
    min_live_profit_pct: float = 1.0  # live gaps are often one book lagging; ask for more
    live_webhook_url: str = ""    # send live arbs to a separate Discord channel
    ev_webhook_url: str = ""      # +EV (incl. props) channel; outliers and parlays fall back to it
    parlay_webhook_url: str = ""
    include_links: bool = True    # ask for bet-slip deep links where books support them
    discord_mention: str = ""     # e.g. @everyone or <@USER_ID> to force a phone ping
    status_webhook_url: str = ""  # health messages; defaults to the alerts webhook
    low_credits: int = 5000       # warn on Discord below this many credits
    log_file: str = "arbs.csv"    # every arb with how long it lasted ("" = off)
    summary_hour: int = 9         # local hour for the daily Discord summary (-1 = off)
    odds_format: str = "american"  # "american" (+152 / -154) or "decimal" (2.52)
    # +EV
    ev_enabled: bool = True
    bet_at_sharp: bool = False      # true only if you can actually bet at the sharp book
    sharp_books: str = "pinnacle"   # fair-odds references (blended if more than one)
    sharp_weights: dict[str, float] = field(default_factory=dict)  # e.g. pinnacle=0.6; default equal
    sharp_disagree_pct: float = 3.0  # skip a line if two sharps' fair odds differ by more (points)
    single_source_stake: float = 0.5  # stake multiplier when only 1 of several sharps priced it
    devig_method: str = "power"     # "power" (handles long-shot bias) or "multiplicative"
    unit_size: float = 0            # dollars per unit; > 0 shows stakes in units too
    ev_books: str = ""              # only alert for these books ("" = every non-sharp book)
    min_ev_pct: float = 4.0
    max_ev_pct: float = 25.0        # above this it's almost always a stale line
    ev_max_odds: float = 5.0        # skip long shots; their fair odds are least reliable
    ev_live: bool = False           # live +EV is mostly feed-timing noise; pre-game only
    ev_bankroll: float = 1000.0
    kelly_fraction: float = 0.25
    ev_max_stake_pct: float = 3.0   # never stake more than this % of EV_BANKROLL on one bet
    ev_mention: str = ""
    ev_log_file: str = "ev_bets.csv"
    ev_results_file: str = "ev_results.csv"
    closing_file: str = "closing_lines.csv"
    # Player props (fetched per game, so they're budgeted separately and checked less often)
    props_enabled: bool = True
    prop_sports: list[str] = field(default_factory=lambda: [
        "americanfootball_nfl", "basketball_nba", "icehockey_nhl"])
    prop_markets: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PROP_MARKETS))
    prop_minutes: int = 30          # how often to check a game's props
    prop_hours: float = 3.0         # start checking this long before kickoff
    prop_min_ev_pct: float = 7.0    # prop prices are noisier, so ask for more edge
    prop_min_books: int = 4         # without a sharp price, use the median of at least this many books
    consensus_min_books: int = 0    # (internal) consensus fallback for +EV; set for props only
    # Parlays built from current +EV bets
    parlays_enabled: bool = True
    parlay_max_legs: int = 3
    parlay_min_ev_pct: float = 10.0
    parlay_leg_min_ev_pct: float = 2.0  # each leg must be at least this +EV at that book
    parlay_max_alerts: int = 3          # best few per check, no leg reused
    parlay_max_stake_pct: float = 1.0   # of EV_BANKROLL
    parlay_mention: str = ""
    parlay_log_file: str = "parlays.csv"
    closing_minutes: int = 5        # one last check this close to kickoff for games with logged bets
    # Outliers: one book far off every other book's price
    outliers_enabled: bool = True
    outlier_min_pct: float = 10.0   # edge vs the other books' median fair price
    outlier_min_books: int = 3      # need at least this many other books to compare against
    outlier_live: bool = True       # live is where stale books show up most
    outlier_mention: str = ""
    outlier_webhook_url: str = ""
    outlier_log_file: str = "outliers.csv"
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
            bookmakers=e("BOOKMAKERS", d.bookmakers),
            poll_seconds=int(e("POLL_SECONDS", d.poll_seconds)),
            pregame_minutes=int(e("PREGAME_MINUTES", d.pregame_minutes)),
            pregame_hours=float(e("PREGAME_HOURS", d.pregame_hours)),
            monthly_credits=int(e("MONTHLY_CREDITS", d.monthly_credits)),
            billing_day=int(e("BILLING_DAY", d.billing_day)),
            events_refresh_minutes=int(e("EVENTS_REFRESH_MINUTES", d.events_refresh_minutes)),
            game_minutes={k.strip(): int(v) for k, v in
                          (p.split("=") for p in _csv(e("GAME_MINUTES", "")))},
            min_profit_pct=float(e("MIN_PROFIT_PCT", d.min_profit_pct)),
            min_profit_dollars=float(e("MIN_PROFIT_DOLLARS", d.min_profit_dollars)),
            max_profit_pct=float(e("MAX_PROFIT_PCT", d.max_profit_pct)),
            max_age_seconds=int(e("MAX_AGE_SECONDS", d.max_age_seconds)),
            pregame_max_age_seconds=int(e("PREGAME_MAX_AGE_SECONDS", d.pregame_max_age_seconds)),
            realert_jump_pct=float(e("REALERT_JUMP_PCT", d.realert_jump_pct)),
            bankroll=float(e("BANKROLL", d.bankroll)),
            round_stakes=float(e("ROUND_STAKES", d.round_stakes)),
            round_keep_pct=float(e("ROUND_KEEP_PCT", d.round_keep_pct)),
            arb_live=e("ARB_LIVE", "true").lower() in ("1", "true", "yes"),
            min_live_profit_pct=float(e("MIN_LIVE_PROFIT_PCT", d.min_live_profit_pct)),
            live_webhook_url=e("DISCORD_LIVE_WEBHOOK_URL", ""),
            ev_webhook_url=e("DISCORD_EV_WEBHOOK_URL", ""),
            parlay_webhook_url=e("DISCORD_PARLAY_WEBHOOK_URL", ""),
            include_links=e("INCLUDE_LINKS", "true").lower() in ("1", "true", "yes"),
            discord_mention=e("DISCORD_MENTION", ""),
            status_webhook_url=e("DISCORD_STATUS_WEBHOOK_URL", ""),
            low_credits=int(e("LOW_CREDITS", d.low_credits)),
            log_file=e("LOG_FILE", d.log_file),
            summary_hour=int(e("SUMMARY_HOUR", d.summary_hour)),
            odds_format=e("ODDS_FORMAT", d.odds_format).strip().lower(),
            ev_enabled=e("EV_ENABLED", "true").lower() in ("1", "true", "yes"),
            sharp_books=e("SHARP_BOOKS", d.sharp_books),
            sharp_weights={k.strip(): float(v) for k, v in
                           (x.split("=") for x in _csv(e("SHARP_WEIGHTS", "")))},
            sharp_disagree_pct=float(e("SHARP_DISAGREE_PCT", d.sharp_disagree_pct)),
            single_source_stake=float(e("SINGLE_SOURCE_STAKE", d.single_source_stake)),
            devig_method=e("DEVIG_METHOD", d.devig_method).strip().lower(),
            unit_size=float(e("UNIT_SIZE", d.unit_size)),
            bet_at_sharp=e("BET_AT_SHARP", "false").lower() in ("1", "true", "yes"),
            ev_books=e("EV_BOOKS", ""),
            min_ev_pct=float(e("MIN_EV_PCT", d.min_ev_pct)),
            max_ev_pct=float(e("MAX_EV_PCT", d.max_ev_pct)),
            ev_max_odds=float(e("EV_MAX_ODDS", d.ev_max_odds)),
            ev_live=e("EV_LIVE", "false").lower() in ("1", "true", "yes"),
            ev_bankroll=float(e("EV_BANKROLL", d.ev_bankroll)),
            kelly_fraction=float(e("KELLY_FRACTION", d.kelly_fraction)),
            ev_max_stake_pct=float(e("EV_MAX_STAKE_PCT", d.ev_max_stake_pct)),
            ev_mention=e("EV_MENTION", ""),
            ev_log_file=e("EV_LOG_FILE", d.ev_log_file),
            ev_results_file=e("EV_RESULTS_FILE", d.ev_results_file),
            closing_file=e("CLOSING_FILE", d.closing_file),
            props_enabled=e("PROPS_ENABLED", "true").lower() in ("1", "true", "yes"),
            prop_sports=_csv(e("PROP_SPORTS", "")) or d.prop_sports,
            prop_markets={**d.prop_markets, **{k.strip(): v.strip().replace("|", ",") for k, v in
                          (x.split("=", 1) for x in e("PROP_MARKETS", "").split(";") if "=" in x)}},
            prop_minutes=int(e("PROP_MINUTES", d.prop_minutes)),
            prop_hours=float(e("PROP_HOURS", d.prop_hours)),
            prop_min_ev_pct=float(e("PROP_MIN_EV_PCT", d.prop_min_ev_pct)),
            prop_min_books=int(e("PROP_MIN_BOOKS", d.prop_min_books)),
            parlays_enabled=e("PARLAYS_ENABLED", "true").lower() in ("1", "true", "yes"),
            parlay_max_legs=int(e("PARLAY_MAX_LEGS", d.parlay_max_legs)),
            parlay_min_ev_pct=float(e("PARLAY_MIN_EV_PCT", d.parlay_min_ev_pct)),
            parlay_leg_min_ev_pct=float(e("PARLAY_LEG_MIN_EV_PCT", d.parlay_leg_min_ev_pct)),
            parlay_max_alerts=int(e("PARLAY_MAX_ALERTS", d.parlay_max_alerts)),
            parlay_max_stake_pct=float(e("PARLAY_MAX_STAKE_PCT", d.parlay_max_stake_pct)),
            parlay_mention=e("PARLAY_MENTION", ""),
            parlay_log_file=e("PARLAY_LOG_FILE", d.parlay_log_file),
            closing_minutes=int(e("CLOSING_MINUTES", d.closing_minutes)),
            outliers_enabled=e("OUTLIERS_ENABLED", "true").lower() in ("1", "true", "yes"),
            outlier_min_pct=float(e("OUTLIER_MIN_PCT", d.outlier_min_pct)),
            outlier_min_books=int(e("OUTLIER_MIN_BOOKS", d.outlier_min_books)),
            outlier_live=e("OUTLIER_LIVE", "true").lower() in ("1", "true", "yes"),
            outlier_mention=e("OUTLIER_MENTION", ""),
            outlier_webhook_url=e("DISCORD_OUTLIER_WEBHOOK_URL", ""),
            outlier_log_file=e("OUTLIER_LOG_FILE", d.outlier_log_file),
            live_only=e("LIVE_ONLY", "false").lower() in ("1", "true", "yes"),
            active_hours=e("ACTIVE_HOURS", "").strip(),
            timezone=e("TIMEZONE", d.timezone),
        )

    def bad_webhooks(self) -> list[str]:
        """Clear any webhook setting that isn't a URL (e.g. a leftover placeholder) and name it,
        so those alerts fall back to the main channel instead of failing silently."""
        bad = []
        for attr, env in (("ev_webhook_url", "DISCORD_EV_WEBHOOK_URL"),
                          ("outlier_webhook_url", "DISCORD_OUTLIER_WEBHOOK_URL"),
                          ("parlay_webhook_url", "DISCORD_PARLAY_WEBHOOK_URL"),
                          ("live_webhook_url", "DISCORD_LIVE_WEBHOOK_URL"),
                          ("status_webhook_url", "DISCORD_STATUS_WEBHOOK_URL")):
            value = getattr(self, attr)
            if value and not value.startswith(("https://", "http://")):
                bad.append(env)
                setattr(self, attr, "")
        return bad

    def credits_per_call(self) -> int:
        """The Odds API charges markets x regions; every 10 bookmakers count as one region."""
        n_markets = len(_csv(self.markets))
        if self.bookmakers:
            return n_markets * math.ceil(len(_csv(self.bookmakers)) / 10)
        return n_markets * len(_csv(self.regions))

    def prop_credits_per_call(self, sport: str) -> int:
        regions = math.ceil(len(_csv(self.bookmakers)) / 10) if self.bookmakers else len(_csv(self.regions))
        return len(_csv(self.prop_markets.get(sport, ""))) * regions

    def for_props(self) -> "Config":
        """The same settings with the prop thresholds swapped in."""
        return replace(self, min_ev_pct=self.prop_min_ev_pct, consensus_min_books=self.prop_min_books)

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
    def exact_pct(self) -> float:
        """Edge with perfectly split stakes."""
        return (1 / self.margin - 1) * 100

    @property
    def profit_pct(self) -> float:
        """Edge for the stakes we actually print (after rounding)."""
        if self.total_stake <= 0:
            return self.exact_pct
        return self.guaranteed_profit / self.total_stake * 100

    @property
    def key(self) -> str:
        return f"{self.event_id}|{self.market}|{self.line}"

    @property
    def fingerprint(self) -> str:
        return self.key + "|" + ",".join(f"{l.book}@{l.price}" for l in self.legs)

    def set_stakes(self, bankroll: float, round_to: float = 0, keep_pct: float = 85) -> None:
        """Split the bankroll so every outcome pays about the same.

        With round_to (e.g. 5), stakes are rounded to natural-looking amounts like $55
        instead of $57.65 (exact amounts are a giveaway to books). Rounding costs some edge,
        so it steps down ($5 -> $1 -> $0.50 -> cents) until a rounding keeps at least
        keep_pct of the exact edge.
        """
        exact = [bankroll * (1 / l.price) / self.margin for l in self.legs]
        exact_profit = bankroll * (1 / self.margin - 1)
        units = sorted({u for u in (round_to, 1, 0.5) if 0 < u <= round_to}, reverse=True) + [0.01]
        for unit in units:
            stakes = [round(max(unit, round(x / unit) * unit), 2) for x in exact]
            profit = min(st * l.price for st, l in zip(stakes, self.legs)) - sum(stakes)
            if profit >= exact_profit * keep_pct / 100:
                break
        for leg, st in zip(self.legs, stakes):
            leg.stake = st

    def worst_ok_price(self, i: int, keep_pct: float = 0.5) -> float | None:
        """Lowest price leg i can drop to (others unchanged) and still lock in keep_pct profit."""
        room = 1 / (1 + keep_pct / 100) - sum(1 / l.price for j, l in enumerate(self.legs) if j != i)
        return 1 / room if room > 0 else None

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

    def event_odds(self, sport: str, event_id: str, markets: str) -> dict:
        """One game's odds for the given markets (used for player props). Costs markets x regions."""
        params = {"markets": markets, "oddsFormat": "decimal", "dateFormat": "iso"}
        if self.cfg.bookmakers:
            params["bookmakers"] = self.cfg.bookmakers
        else:
            params["regions"] = self.cfg.regions
        if self.cfg.include_links:
            params["includeLinks"] = "true"
        return self._get(f"/sports/{sport}/events/{event_id}/odds", params)

    def scores(self, sport: str, days_from: int = 3) -> list[dict]:
        """Final scores for recent games. Costs 2 credits."""
        return self._get(f"/sports/{sport}/scores", {"daysFrom": days_from, "dateFormat": "iso"})

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

def is_fresh(mkt: dict, bm: dict, now: datetime, live: bool, cfg: Config) -> bool:
    """Live prices must be recent; pre-game lines legitimately sit still for longer."""
    updated = mkt.get("last_update") or bm.get("last_update")
    if not updated:
        return True
    limit = cfg.max_age_seconds if live else cfg.pregame_max_age_seconds
    return (now - _parse_time(updated)).total_seconds() <= limit


def _line_for(market: str, outcome: dict, home_team: str):
    """Return a key so that both sides of the same line group together.

    spreads: Home -3.5 pairs with Away +3.5, so key on the home team's point.
    totals:  Over 220.5 pairs with Under 220.5, so key on the point.
    props:   (player, point), so LeBron Over 25.5 pairs with LeBron Under 25.5 only.
    """
    point = outcome.get("point")
    if outcome.get("description"):
        return (outcome["description"], point)
    if point is None:
        return None
    if market == "spreads":
        return point if outcome["name"] == home_team else -point
    return point


def find_arbs(events: list[dict], cfg: Config, now: datetime | None = None) -> list[Arb]:
    now = now or datetime.now(timezone.utc)
    arbs: list[Arb] = []
    sharp_only = set() if cfg.bet_at_sharp else set(_csv(cfg.sharp_books))

    for ev in events:
        start = _parse_time(ev["commence_time"])
        is_live = start <= now
        if (cfg.live_only and not is_live) or (is_live and not cfg.arb_live):
            continue

        # (market, line) -> outcome name -> best Leg
        best: dict[tuple, dict[str, Leg]] = {}
        # (market, line) -> most outcomes any single book offers (2 or 3-way)
        n_outcomes: dict[tuple, int] = {}

        for bm in ev.get("bookmakers", []):
            if bm["key"] in sharp_only:
                continue  # reference-only book (e.g. Pinnacle): not bettable from the US
            for mkt in bm.get("markets", []):
                if not is_fresh(mkt, bm, now, is_live, cfg):
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
            min_pct = cfg.min_live_profit_pct if is_live else cfg.min_profit_pct
            if min_pct <= arb.exact_pct <= cfg.max_profit_pct:
                arb.set_stakes(cfg.bankroll, cfg.round_stakes, cfg.round_keep_pct)
                if arb.profit_pct >= min_pct and arb.guaranteed_profit >= cfg.min_profit_dollars:
                    arbs.append(arb)

    return sorted(arbs, key=lambda a: a.profit_pct, reverse=True)


# --------------------------------------------------------------------------- alerts

MARKET_NAMES = {
    "h2h": "Moneyline", "spreads": "Spread", "totals": "Total",
    # player props
    "player_pass_yds": "Passing Yards", "player_pass_tds": "Passing TDs", "player_rush_yds": "Rushing Yards",
    "player_reception_yds": "Receiving Yards", "player_receptions": "Receptions",
    "player_points": "Points", "player_rebounds": "Rebounds", "player_assists": "Assists",
    "player_threes": "Threes", "player_shots_on_goal": "Shots on Goal", "player_goals": "Goals",
    "player_total_saves": "Saves", "batter_hits": "Hits", "batter_total_bases": "Total Bases",
    "pitcher_strikeouts": "Strikeouts",
}


def is_prop(line) -> bool:
    return isinstance(line, tuple)


def market_label(market: str, line) -> str:
    """'Moneyline', 'Spread -3.5', 'Total 220.5', or 'LeBron James · Points 25.5'."""
    name = MARKET_NAMES.get(market, market.replace("_", " ").title())
    if is_prop(line):
        player, point = line
        return f"{player} · {name}" + (f" {point:g}" if point is not None else "")
    if line is None:
        return name
    return f"{name} {line:+g}" if market == "spreads" else f"{name} {line:g}"


def _line_label(arb: Arb) -> str:
    """Suffix after the market name (kept for the console text)."""
    if arb.line is None:
        return ""
    if is_prop(arb.line):
        return f" ({market_label(arb.market, arb.line)})"
    return f" {arb.line:+g}" if arb.market == "spreads" else f" {arb.line:g}"


ODDS_FORMAT = "american"  # set from ODDS_FORMAT in .env at startup


OK_EDGE_PCT = 1.5  # a single-side bet is still worth taking down to this much edge


def american(dec: float) -> str:
    """2.52 -> +152, 1.65 -> -154."""
    if dec >= 2:
        return f"+{round((dec - 1) * 100)}"
    return f"-{round(100 / (dec - 1))}"


def odds(dec: float) -> str:
    return american(dec) if ODDS_FORMAT == "american" else f"{dec:.2f}"


def money(x: float) -> str:
    """$58 for whole dollars, $57.50 otherwise."""
    return f"${x:,.0f}" if abs(x - round(x)) < 0.005 else f"${x:,.2f}"


def _fmt_secs(s: float) -> str:
    s = int(round(s))
    return f"{s}s" if s < 90 else f"{s // 60}m {s % 60:02d}s"


def format_text(arb: Arb) -> str:
    status = "🔴 LIVE" if arb.is_live else f"starts {arb.commence_time}"
    rows = "\n".join(
        f"  • {l.outcome} {odds(l.price)} on {l.book}  → stake {money(l.stake)}"
        + (f"\n    {l.link}" if l.link else "")
        for l in arb.legs
    )
    return (
        f"💰 {arb.profit_pct:.2f}% ARB | {arb.sport} | {arb.matchup} ({status})\n"
        f"  {market_label(arb.market, arb.line)}\n{rows}\n"
        f"  Total {money(arb.total_stake)} → returns ≥ ${arb.guaranteed_return:.2f} "
        f"(+${arb.guaranteed_profit:.2f})"
    )


SPORT_ICONS = [("americanfootball", "🏈"), ("basketball", "🏀"), ("icehockey", "🏒"), ("baseball", "⚾"),
               ("soccer", "⚽"), ("mma", "🥊"), ("boxing", "🥊"), ("tennis", "🎾"), ("golf", "⛳")]
GREY = 0x95A5A6
DIVIDER = "\n\n───────────────\n"


def sport_icon(sport_key: str) -> str:
    return next((icon for prefix, icon in SPORT_ICONS if sport_key.startswith(prefix)), "🏟️")


def _link(text: str, url: str) -> str:
    return f"[{text}]({url})" if url else text


def _when(is_live: bool, commence_time: str, first_seen: float | None = None) -> str:
    if is_live:
        when = "🔴 **LIVE**"
    else:
        ts = int(_parse_time(commence_time).timestamp())
        when = f"⏰ Starts <t:{ts}:t> (<t:{ts}:R>)"
    return when + (f" · spotted <t:{int(first_seen)}:R>" if first_seen else "")


def _card(title: str, description: str, color: int, url: str = "", footer: str = "",
          mention: str = "") -> dict:
    embed = {"title": title[:256], "description": description[:4000], "color": color,
             "timestamp": datetime.now(timezone.utc).isoformat()}
    if url:
        embed["url"] = url  # makes the title tappable
    if footer:
        embed["footer"] = {"text": footer}
    payload = {"username": "Arb Bot", "embeds": [embed],
               "allowed_mentions": {"parse": ["everyone", "roles", "users"]}}
    if mention:
        payload["content"] = mention
    return payload


def _gone_card(title: str, line: str) -> dict:
    return _card(title, line, GREY)


def discord_payload(arb: Arb, mention: str = "", gone_after: float | None = None,
                    first_seen: float | None = None) -> dict:
    """Arb card: numbered instructions first, then the details."""
    market = market_label(arb.market, arb.line)
    if gone_after is not None:
        return _gone_card(f"❌ GONE after {_fmt_secs(gone_after)} · ~~{arb.profit_pct:.2f}%~~ {arb.matchup}",
                          f"Ignore this one. The prices moved. ({sport_icon(arb.sport_key)} {arb.sport} · {market})")
    steps = []
    for i, l in enumerate(arb.legs):
        worst = arb.worst_ok_price(i)
        skip = f"\n     ↳ skip if the price is worse than {odds(worst)}" if worst else ""
        num = ["1️⃣", "2️⃣", "3️⃣"][i] if i < 3 else "•"
        what = f"{arb.line[0]} {l.outcome} {arb.line[1]:g}" if is_prop(arb.line) else l.outcome
        steps.append(f"{num} Open **{_link(l.book, l.link)}** → bet "
                     f"**{money(l.stake)}** on **{what} {odds(l.price)}**{skip}")
    rounding = (f"\n*{arb.exact_pct:.2f}% with exact stakes; rounded to look like normal bets*"
                if arb.exact_pct - arb.profit_pct >= 0.05 else "")
    desc = (f"👉 **DO THIS NOW: place BOTH bets. You profit no matter who wins.**\n\n"
            + "\n\n".join(steps)
            + f"\n\n💵 You bet **{money(arb.total_stake)}** and get back at least "
              f"**{money(arb.guaranteed_return)}** (+{money(arb.guaranteed_profit)}).{rounding}"
            + "\nDo them back to back. If one price moved past its skip line, don't place the other."
            + f"\n\n───────────────\n{sport_icon(arb.sport_key)} **{arb.sport}** · {arb.matchup}\n"
              f"{market} · {_when(arb.is_live, arb.commence_time, first_seen)}")
    return _card(f"💰 ARB · +{money(arb.guaranteed_profit)} guaranteed ({arb.profit_pct:.2f}%)", desc,
                 0xE74C3C if arb.is_live else 0x2ECC71, url=arb.legs[0].link,
                 footer="ARB = bet every side at different books, profit locked in.", mention=mention)


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
    arb: "Arb | EVBet"
    first_seen: float
    last_seen: float
    best_pct: float
    message_id: str | None = None
    url: str = ""             # webhook the message was posted with (edits must use the same one)
    alerted_pct: float = 0.0  # edge when we last sent a (pinging) alert


def data_path(name: str) -> Path:
    path = Path(name)
    return path if path.is_absolute() else HERE / path


def append_csv(name: str, fields: list[str], row: dict) -> None:
    path = data_path(name)
    new_file = not path.exists()
    if not new_file:
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            old_fields = reader.fieldnames or []
            if missing := [x for x in fields if x not in old_fields]:  # a newer version added columns
                rows = list(reader)
        if missing:
            with path.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=old_fields + missing, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            fields = old_fields + missing
        else:
            fields = old_fields
    try:
        with path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if new_file:
                w.writeheader()
            w.writerow(row)
    except OSError as e:
        print(f"  ! Couldn't write {path}: {e}", file=sys.stderr)


LOG_FIELDS = ["first_seen", "gone_at", "seconds_open", "sport", "matchup", "market", "line",
              "live", "best_profit_pct", "legs"]


class Alerter:
    """Tracks arbs from first sighting until they close.

    New arb -> new Discord message (with optional @mention).
    Prices change while still an arb -> the same message is edited in place.
    Arb disappears -> the message is edited to "GONE after Ns" and logged to CSV.
    """

    noun = "arbs"
    log_fields = LOG_FIELDS
    on_log = None  # optional callback(row) after a row is logged
    log_on_open = False  # arbs are logged when they close, so the row has how long it lasted

    def __init__(self, cfg: Config, dry_run: bool):
        self.cfg = cfg
        self.dry_run = dry_run or not cfg.webhook_url
        self.open: dict[str, OpenArb] = {}
        self.reset_stats()

    # ---- hooks (overridden for +EV)
    def text(self, item) -> str:
        return format_text(item)

    def payload(self, item, mention: str = "", gone_after: float | None = None,
                first_seen: float | None = None) -> dict:
        return discord_payload(item, mention, gone_after, first_seen)

    def value(self, item) -> float:
        return item.profit_pct

    def label(self, item) -> str:
        return f"{item.matchup} {item.market}"

    def mention(self) -> str:
        return self.cfg.discord_mention

    def carry(self, old, new) -> None:
        """Copy anything from the previous sighting the new one should remember."""

    def log_path(self) -> str:
        return self.cfg.log_file

    def row(self, op: OpenArb) -> dict:
        a = op.arb
        return {
            "sport": a.sport, "matchup": a.matchup, "market": a.market,
            "line": "" if a.line is None else a.line, "live": a.is_live,
            "best_profit_pct": round(op.best_pct, 2),
            "legs": "; ".join(f"{l.outcome} @{l.price} {l.book}" for l in a.legs),
        }

    def webhook_for(self, item) -> str:
        if item.is_live and self.cfg.live_webhook_url:
            return self.cfg.live_webhook_url
        return self.cfg.webhook_url

    def _discord(self, payload: dict, message_id: str | None = None, url: str = "") -> str | None:
        if self.dry_run:
            return None
        url = url or self.cfg.webhook_url
        try:
            if message_id:
                _webhook(url, payload, "PATCH", message_id)
                return message_id
            msg = _webhook(url, payload)
            return msg.get("id") if msg else None
        except Exception as e:  # keep scanning even if Discord hiccups
            print(f"  ! Discord send failed: {e}", file=sys.stderr)
            return message_id

    def handle(self, arbs: list[Arb], checked_sports: list[str] | None = None,
               now: float | None = None, checked_events: set[str] | None = None) -> int:
        """Process one scan's arbs. checked_sports = sports whose odds were just fetched;
        open arbs in those sports that weren't found again are closed."""
        now = now or time.time()
        new = 0
        seen = set()
        for arb in arbs:
            seen.add(arb.key)
            cur = self.open.get(arb.key)
            if cur is None:
                print(self.text(arb), flush=True)
                op = OpenArb(arb, now, now, self.value(arb), alerted_pct=self.value(arb),
                             url=self.webhook_for(arb))
                op.message_id = self._discord(self.payload(arb, self.mention(), first_seen=now), url=op.url)
                self.open[arb.key] = op
                if self.log_on_open:
                    self._log(op, now, None)
                self.stats["found"] += 1
                if self.value(arb) > self.stats["best_pct"]:
                    self.stats["best_pct"], self.stats["best"] = self.value(arb), arb.matchup
                new += 1
            else:
                changed = cur.arb.fingerprint != arb.fingerprint
                self.carry(cur.arb, arb)
                cur.arb, cur.last_seen = arb, now
                cur.best_pct = max(cur.best_pct, self.value(arb))
                if self.value(arb) >= cur.alerted_pct + self.cfg.realert_jump_pct:
                    # Edits don't ping your phone, so a much better price gets a fresh alert.
                    print(f"  ⬆️ improved: {self.label(arb)} now {self.value(arb):.2f}%", flush=True)
                    print(self.text(arb), flush=True)
                    if cur.message_id:
                        self._discord({"embeds": [{"title": "⬆️ Better price: see the newer alert below",
                                                   "color": 0x95A5A6}]}, cur.message_id, cur.url)
                    cur.message_id = self._discord(self.payload(arb, self.mention(), first_seen=now),
                                                   url=cur.url)
                    cur.alerted_pct = self.value(arb)
                    new += 1
                elif changed:
                    print(f"  ↻ updated: {self.label(arb)} now {self.value(arb):.2f}%", flush=True)
                    self._discord(self.payload(arb, first_seen=cur.first_seen), cur.message_id, cur.url)

        checked = set(checked_sports) if checked_sports is not None else None
        for key in list(self.open):
            op = self.open[key]
            if key in seen or (checked is not None and op.arb.sport_key not in checked):
                continue
            if checked_events is not None and op.arb.event_id not in checked_events:
                continue  # that game wasn't re-checked this round
            self._close(key, now)
        return new

    def _close(self, key: str, now: float) -> None:
        op = self.open.pop(key)
        lasted = now - op.first_seen  # first seen -> first check where it was gone
        print(f"  ❌ gone after {_fmt_secs(lasted)}: {self.label(op.arb)}", flush=True)
        if op.message_id:
            self._discord(self.payload(op.arb, gone_after=lasted), op.message_id, op.url)
        self.stats["closed"] += 1
        self.stats["open_seconds"] += lasted
        if not self.log_on_open:
            self._log(op, now, lasted)

    def _log(self, op: OpenArb, now: float, lasted: float | None) -> None:
        if not self.log_path():
            return
        row = {
            "first_seen": datetime.fromtimestamp(op.first_seen, timezone.utc).isoformat(timespec="seconds"),
            "gone_at": "" if lasted is None else datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
            "seconds_open": "" if lasted is None else round(lasted),
            **self.row(op),
        }
        append_csv(self.log_path(), self.log_fields, row)
        if self.on_log:
            self.on_log(row)

    def summary(self) -> str:
        s = self.stats
        avg = s["open_seconds"] / s["closed"] if s["closed"] else 0
        best = f", best {s['best_pct']:.2f}% ({s['best']})" if s["found"] else ""
        return f"{s['found']} {self.noun} found{best}, open {_fmt_secs(avg)} on average"

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


# --------------------------------------------------------------------------- +EV bets

@dataclass
class EVBet:
    """One side priced better than the sharp book's no-vig ("fair") odds."""
    event_id: str
    sport: str
    sport_key: str
    matchup: str
    home_team: str
    away_team: str
    commence_time: str
    is_live: bool
    market: str
    line: float | None        # grouping key (home team's point for spreads)
    outcome: str
    point: float | None       # this outcome's own point (-3.5, 220.5, ...)
    book: str
    price: float
    fair_prob: float
    sharp_book: str           # e.g. "Pinnacle" or "Pinnacle + Betfair"
    n_outcomes: int
    link: str = ""
    also: list[tuple[str, float]] = field(default_factory=list)  # other +EV books
    stake: float = 0.0
    sources_used: int = 1
    sources_total: int = 1
    unit_size: float = 0.0
    sharp_quotes: list[tuple[str, list[float]]] = field(default_factory=list)  # (book, [this side, other side(s)])
    board: list[tuple[str, float, float, str]] = field(default_factory=list)   # every book: (book, price, EV%, link)
    hedge: list[tuple[str, str, float, str]] = field(default_factory=list)  # (outcome, book, price, link)
    hedge_pct: float = 0.0           # guaranteed profit % if the hedge is placed too
    first_fair_prob: float = 0.0     # fair probability when first alerted (to show movement)
    first_sharp_quotes: list[tuple[str, list[float]]] = field(default_factory=list)

    def worst_ok_price(self, min_edge_pct: float = OK_EDGE_PCT) -> float:
        """Lowest price that still leaves min_edge_pct of edge against the fair price."""
        return (1 + min_edge_pct / 100) / self.fair_prob

    @property
    def stake_label(self) -> str:
        units = f" ({self.stake / self.unit_size:.2g}u)" if self.unit_size > 0 else ""
        return f"{money(self.stake)}{units}"

    @property
    def ev_pct(self) -> float:
        return (self.fair_prob * self.price - 1) * 100

    @property
    def fair_odds(self) -> float:
        return 1 / self.fair_prob

    @property
    def player(self) -> str:
        return self.line[0] if is_prop(self.line) else ""

    @property
    def key(self) -> str:
        return f"ev|{self.event_id}|{self.market}|{self.line}|{self.outcome}"

    @property
    def fingerprint(self) -> str:
        return f"{self.key}|{self.book}@{self.price}"

    @property
    def pick(self) -> str:
        if is_prop(self.line):
            player, point = self.line
            stat = MARKET_NAMES.get(self.market, self.market)
            return f"{player} {self.outcome}" + (f" {point:g}" if point is not None else "") + f" {stat}"
        if self.market == "h2h":
            return self.outcome if self.outcome == "Draw" else f"{self.outcome} ML"
        if self.point is None:
            return self.outcome
        return f"{self.outcome} {self.point:+g}" if self.market == "spreads" else f"{self.outcome} {self.point:g}"


def devig(prices: list[float], method: str = "multiplicative") -> list[float]:
    """No-vig probabilities from a full set of decimal odds.

    multiplicative: scale every implied probability down by the same factor.
    power: raise implied probabilities to the power k that makes them sum to 1. Books
      shade long shots more than favorites, and this removes more of the margin from the
      long shot, so it's less likely to flag a fake long-shot edge.
    """
    inv = [1 / p for p in prices]
    if method != "power":
        total = sum(inv)
        return [x / total for x in inv]
    lo, hi = 0.2, 20.0  # sum(q**k) falls as k rises; bisect for sum == 1
    for _ in range(80):
        k = (lo + hi) / 2
        if sum(q ** k for q in inv) > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    probs = [q ** k for q in inv]
    total = sum(probs)
    return [x / total for x in probs]


def kelly_stake(fair_prob: float, price: float, cfg: Config, mult: float = 1.0) -> float:
    edge = (fair_prob * price - 1) / (price - 1)
    stake = cfg.ev_bankroll * cfg.kelly_fraction * max(0.0, edge) * mult
    stake = min(stake, cfg.ev_bankroll * cfg.ev_max_stake_pct / 100)
    if cfg.round_stakes > 0 and stake >= cfg.round_stakes:
        return float(round(stake / cfg.round_stakes) * cfg.round_stakes)
    return float(round(stake)) if stake >= 1 else round(stake, 2)


def sharp_fair(ev: dict, cfg: Config, now: datetime, is_live: bool):
    """Fair (no-vig) probabilities per line for one game, from the sharp book(s).

    Returns (fair, sharp_name, n_used, raw_src, titles): fair[(market, line)][outcome] = prob.
    """
    sharp = _csv(cfg.sharp_books)
    books = {bm["key"]: bm for bm in ev.get("bookmakers", [])}
    # Each sharp book's no-vig probabilities, per line (and its raw prices, for display).
    raw_src: dict[tuple, dict[str, dict[str, float]]] = {}
    per_src: dict[tuple, dict[str, dict[str, float]]] = {}
    titles: dict[str, str] = {}
    for sk in sharp:
        bm = books.get(sk)
        if not bm:
            continue
        titles[sk] = bm.get("title", sk)
        for mkt in bm.get("markets", []):
            if not is_fresh(mkt, bm, now, is_live, cfg):
                continue
            groups: dict[tuple, dict[str, float]] = {}
            for oc in mkt.get("outcomes", []):
                price = float(oc.get("price") or 0)
                if price > 1.0:
                    k = (mkt["key"], _line_for(mkt["key"], oc, ev["home_team"]))
                    groups.setdefault(k, {})[oc["name"]] = price
            for k, outs in groups.items():
                if len(outs) >= 2:
                    names = list(outs)
                    raw_src.setdefault(k, {})[sk] = outs
                    per_src.setdefault(k, {})[sk] = dict(
                        zip(names, devig([outs[n] for n in names], cfg.devig_method)))

    # Blend them into one fair price per line (weights renormalised over the sources present).
    fair: dict[tuple, dict[str, float]] = {}
    sharp_name: dict[tuple, str] = {}
    n_used: dict[tuple, int] = {}
    for k, srcs in per_src.items():
        first = next(iter(srcs.values()))
        same = {sk: pr for sk, pr in srcs.items() if set(pr) == set(first)}  # same outcomes
        if len(same) >= 2 and any(
                max(pr[n] for pr in same.values()) - min(pr[n] for pr in same.values())
                > cfg.sharp_disagree_pct / 100 for n in first):
            continue  # the sharps disagree: no reliable fair price for this line
        weights = {sk: cfg.sharp_weights.get(sk, 1.0) for sk in same}
        total_w = sum(weights.values()) or 1.0
        fair[k] = {n: sum(same[sk][n] * w for sk, w in weights.items()) / total_w for n in first}
        sharp_name[k] = " + ".join(titles[sk] for sk in same)
        n_used[k] = len(same)
    return fair, sharp_name, n_used, raw_src, titles


def consensus_fair(ev: dict, cfg: Config, now: datetime, is_live: bool, skip: set[str]) -> dict:
    """Median no-vig probability per line across books (when no sharp prices the line).
    Returns {(market, line): ({outcome: prob}, n_books)} for lines with enough books."""
    lines: dict[tuple, dict[str, dict[str, float]]] = {}
    for bm in ev.get("bookmakers", []):
        if bm["key"] in skip:
            continue
        for mkt in bm.get("markets", []):
            if not is_fresh(mkt, bm, now, is_live, cfg):
                continue
            for oc in mkt.get("outcomes", []):
                price = float(oc.get("price") or 0)
                if price > 1.0:
                    k = (mkt["key"], _line_for(mkt["key"], oc, ev["home_team"]))
                    lines.setdefault(k, {}).setdefault(bm["key"], {})[oc["name"]] = price
    out = {}
    for k, books in lines.items():
        n_out = max(len(o) for o in books.values())
        full = [o for o in books.values() if len(o) == n_out >= 2]
        names = set(full[0]) if full else set()
        full = [o for o in full if set(o) == names]
        if len(full) < cfg.consensus_min_books:
            continue
        probs = [dict(zip(o, devig(list(o.values()), cfg.devig_method))) for o in full]
        med = {}
        for n in names:
            xs = sorted(pr[n] for pr in probs)
            m = len(xs) // 2
            med[n] = xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2
        total = sum(med.values())
        out[k] = ({n: v / total for n, v in med.items()}, len(full))
    return out


def find_evs(events: list[dict], cfg: Config, now: datetime | None = None) -> list[EVBet]:
    if not cfg.ev_enabled:
        return []
    now = now or datetime.now(timezone.utc)
    sharp = _csv(cfg.sharp_books)
    allowed = set(_csv(cfg.ev_books))
    out: list[EVBet] = []

    for ev in events:
        is_live = _parse_time(ev["commence_time"]) <= now
        if (cfg.live_only and not is_live) or (is_live and not cfg.ev_live):
            continue
        books = {bm["key"]: bm for bm in ev.get("bookmakers", [])}

        fair, sharp_name, n_used, raw_src, titles = sharp_fair(ev, cfg, now, is_live)
        n_total = {k: len(sharp) for k in fair}
        if cfg.consensus_min_books:
            for k, (probs, n) in consensus_fair(ev, cfg, now, is_live, set(sharp)).items():
                if k not in fair:
                    fair[k], sharp_name[k], n_used[k], n_total[k] = probs, f"consensus of {n} books", n, n

        # Every soft-book price that beats fair by enough (and every price, for the board).
        offers: dict[tuple, list[tuple[float, str, str, float | None]]] = {}
        boards: dict[tuple, list[tuple[str, float, float]]] = {}
        for key, bm in books.items():
            if key in sharp or (allowed and key not in allowed):
                continue
            for mkt in bm.get("markets", []):
                if not is_fresh(mkt, bm, now, is_live, cfg):
                    continue
                for oc in mkt.get("outcomes", []):
                    k = (mkt["key"], _line_for(mkt["key"], oc, ev["home_team"]))
                    p = fair.get(k, {}).get(oc["name"])
                    price = float(oc.get("price") or 0)
                    if p is None or price <= 1.0:
                        continue
                    ev_pct = (p * price - 1) * 100
                    link = oc.get("link") or mkt.get("link") or bm.get("link") or ""
                    boards.setdefault((k, oc["name"]), []).append((bm.get("title", key), price, ev_pct, link))
                    if price > cfg.ev_max_odds:
                        continue
                    if cfg.min_ev_pct <= ev_pct <= cfg.max_ev_pct:
                        link = oc.get("link") or mkt.get("link") or bm.get("link") or ""
                        offers.setdefault((k, oc["name"]), []).append(
                            (price, bm.get("title", key), link, oc.get("point")))

        for (k, name), lst in offers.items():
            lst.sort(key=lambda o: o[0], reverse=True)
            price, book, link, point = lst[0]
            bet = EVBet(
                event_id=ev["id"], sport=ev.get("sport_title", ev.get("sport_key", "")),
                sport_key=ev.get("sport_key", ""), matchup=f"{ev['away_team']} @ {ev['home_team']}",
                home_team=ev["home_team"], away_team=ev["away_team"],
                commence_time=ev["commence_time"], is_live=is_live,
                market=k[0], line=k[1], outcome=name, point=point,
                book=book, price=price, link=link,
                fair_prob=fair[k][name], sharp_book=sharp_name[k], n_outcomes=len(fair[k]),
                also=[(b, pr) for pr, b, _, _ in lst[1:]],
                sources_used=n_used[k], sources_total=n_total[k], unit_size=cfg.unit_size,
                sharp_quotes=[(titles[sk], [outs[name]] + [pr for n, pr in outs.items() if n != name])
                              for sk, outs in raw_src.get(k, {}).items() if name in outs],
                board=sorted(boards.get((k, name), []), key=lambda r: r[1], reverse=True),
            )
            # Fewer references -> less certainty -> smaller bet (the edge itself isn't changed).
            mult = cfg.single_source_stake if n_total[k] > 1 and n_used[k] == 1 else 1.0
            bet.stake = kelly_stake(bet.fair_prob, bet.price, cfg, mult)
            out.append(bet)

    return sorted(out, key=lambda b: b.ev_pct, reverse=True)


def _quotes(quotes: list[tuple[str, list[float]]]) -> str:
    return "; ".join(f"{bk} {' / '.join(odds(x) for x in prs)}" for bk, prs in quotes)


def _fair_line(b: EVBet) -> str:
    """'+140' or '+142 → +140 (-0.9%)' when the fair price moved since the first alert."""
    now = odds(b.fair_odds)
    if b.first_fair_prob and abs(b.first_fair_prob - b.fair_prob) > 1e-6:
        change = (b.fair_prob / b.first_fair_prob - 1) * 100
        return f"{odds(1 / b.first_fair_prob)} → {now} ({change:+.1f}% win chance)"
    return now


def _board(b: EVBet, rows: int = 12) -> str:
    lines = [f"{bk[:14]:<14} {odds(pr):>6} {ev:+5.1f}%" for bk, pr, ev, _ in b.board[:rows]]
    return "```\n" + "\n".join(lines) + "\n```" if lines else ""


def format_ev_text(b: EVBet) -> str:
    status = "🔴 LIVE" if b.is_live else f"starts {b.commence_time}"
    also = f"\n  Also +EV at: {', '.join(f'{bk} {odds(pr)}' for bk, pr in b.also)}" if b.also else ""
    sharp = f"\n  Sharp: {_quotes(b.sharp_quotes)}" if b.sharp_quotes else ""
    return (
        f"📈 +{b.ev_pct:.1f}% EV | {b.sport} | {b.matchup} ({status})\n"
        f"  {b.pick} {odds(b.price)} on {b.book}  → stake {b.stake_label}"
        + (f"\n    {b.link}" if b.link else "")
        + f"\n  Fair {_fair_line(b)} ({b.fair_prob:.1%}, {b.sharp_book} no-vig, "
          f"{b.sources_used}/{b.sources_total} sources){sharp}{also}"
    )


def _board_lines(b: EVBet, rows: int = 12) -> str:
    """Every book's price on this bet, best first, each with its link."""
    lines = []
    for bk, pr, ev, link in b.board[:rows]:
        mark = "🟢" if ev > 0 else "⚪"
        lines.append(f"{mark} {_link(bk, link)} — **{odds(pr)}** · {ev:+.1f}%")
    return "\n".join(lines)


def ev_payload(b: EVBet, mention: str = "", gone_after: float | None = None,
                first_seen: float | None = None) -> dict:
    """+EV card: the instruction first, then why (fair value, sharp prices), then every book."""
    icon = sport_icon(b.sport_key)
    if gone_after is not None:
        return _gone_card(f"❌ GONE after {_fmt_secs(gone_after)} · ~~+{b.ev_pct:.1f}%~~ {b.pick} {odds(b.price)}",
                          f"Ignore this one. {b.book} moved and the value is gone. ({icon} {b.matchup})")
    parts = [
        f"👉 **DO THIS: bet this ONE side.** Good value, but it won't win every time.\n\n"
        f"Open **{_link(b.book, b.link)}** → bet **{b.stake_label}** on **{b.pick} {odds(b.price)}**\n"
        f"↳ skip if the price is worse than **{odds(b.worst_ok_price())}**",
    ]
    if len([r for r in b.board if r[2] > 0]) > 1:
        parts.append("Can't use that book? Any 🟢 book below works too, at its price.")
    details = (f"{icon} **{b.sport}** · {b.matchup}\n{_when(b.is_live, b.commence_time, first_seen)}\n\n"
               f"**Fair value** {_fair_line(b)} · {b.fair_prob:.1%} to win"
               + (f"\n**{b.sharp_quotes[0][0]}** {' / '.join(odds(x) for x in b.sharp_quotes[0][1])}"
                  if b.sharp_quotes else "")
               + (f" *(was {' / '.join(odds(x) for x in b.first_sharp_quotes[0][1])})*"
                  if b.first_sharp_quotes and b.first_sharp_quotes != b.sharp_quotes else ""))
    desc = "\n\n".join(parts) + DIVIDER + details
    if b.board:
        desc += "\n\n**Every book**\n" + _board_lines(b)
    footer = f"+EV = better price than the true odds ({b.sharp_book}). Wins over many bets, not every bet."
    return _card(f"📈 +EV {b.ev_pct:.1f}% · {b.pick} {odds(b.price)} at {b.book}", desc,
                 0x3498DB, url=b.link, footer=footer, mention=mention)


EV_LOG_FIELDS = ["first_seen", "event_id", "sport", "sport_key", "matchup",
                 "home_team", "away_team", "commence_time", "live", "market", "outcome", "point",
                 "n_outcomes", "book", "price", "fair_odds", "best_ev_pct", "stake", "player"]


class EVAlerter(Alerter):
    noun = "+EV bets"
    log_fields = EV_LOG_FIELDS
    log_on_open = True  # logged at first sight, so a restart never loses a bet from the results

    def text(self, item) -> str:
        return format_ev_text(item)

    def payload(self, item, mention="", gone_after=None, first_seen=None) -> dict:
        return ev_payload(item, mention, gone_after, first_seen)

    def value(self, item) -> float:
        return item.ev_pct

    def label(self, item) -> str:
        return f"{item.pick} ({item.book})"

    def mention(self) -> str:
        return self.cfg.ev_mention

    def carry(self, old, new) -> None:
        new.first_fair_prob = old.first_fair_prob or old.fair_prob
        new.first_sharp_quotes = old.first_sharp_quotes or old.sharp_quotes

    def log_path(self) -> str:
        return self.cfg.ev_log_file

    def webhook_for(self, item) -> str:
        return self.cfg.ev_webhook_url or self.cfg.webhook_url

    def row(self, op: OpenArb) -> dict:
        b = op.arb
        return {
            "event_id": b.event_id, "sport": b.sport, "sport_key": b.sport_key, "matchup": b.matchup,
            "home_team": b.home_team, "away_team": b.away_team, "commence_time": b.commence_time,
            "live": b.is_live, "market": b.market, "outcome": b.outcome,
            "point": "" if b.point is None else b.point, "n_outcomes": b.n_outcomes,
            "book": b.book, "price": b.price, "fair_odds": round(b.fair_odds, 3),
            "best_ev_pct": round(b.ev_pct, 2), "stake": b.stake, "player": b.player,
        }


# --------------------------------------------------------------------------- outliers

def find_outliers(events: list[dict], cfg: Config, now: datetime | None = None) -> list[EVBet]:
    """One book's price far above what every other book thinks (e.g. +400 where the rest
    are around -400). Usually a book that hasn't moved yet. Fair odds here are the median of
    all the OTHER books' no-vig prices, so it works even when Pinnacle is slow or missing."""
    if not cfg.outliers_enabled:
        return []
    now = now or datetime.now(timezone.utc)
    reference_only = set() if cfg.bet_at_sharp else set(_csv(cfg.sharp_books))
    out: list[EVBet] = []

    for ev in events:
        is_live = _parse_time(ev["commence_time"]) <= now
        if (cfg.live_only and not is_live) or (is_live and not cfg.outlier_live):
            continue
        # (market, line) -> book key -> {outcome: price}, fresh full markets only
        lines: dict[tuple, dict[str, dict[str, float]]] = {}
        titles: dict[str, str] = {}
        links: dict[tuple, str] = {}
        points: dict[tuple, float | None] = {}
        for bm in ev.get("bookmakers", []):
            titles[bm["key"]] = bm.get("title", bm["key"])
            for mkt in bm.get("markets", []):
                if not is_fresh(mkt, bm, now, is_live, cfg):
                    continue
                for oc in mkt.get("outcomes", []):
                    price = float(oc.get("price") or 0)
                    if price <= 1.0:
                        continue
                    k = (mkt["key"], _line_for(mkt["key"], oc, ev["home_team"]))
                    lines.setdefault(k, {}).setdefault(bm["key"], {})[oc["name"]] = price
                    links[(k, bm["key"], oc["name"])] = oc.get("link") or mkt.get("link") or bm.get("link") or ""
                    points[(k, oc["name"])] = oc.get("point")

        for k, books in lines.items():
            n_out = max(len(o) for o in books.values())
            full = {bk: o for bk, o in books.items() if len(o) == n_out and n_out >= 2}
            names = set().union(*full.values()) if full else set()
            full = {bk: o for bk, o in full.items() if set(o) == names}
            probs = {bk: dict(zip(o, devig(list(o.values()), cfg.devig_method))) for bk, o in full.items()}
            for name in names:
                for bk, o in full.items():
                    if bk in reference_only:
                        continue
                    others = sorted(probs[ob][name] for ob in probs if ob != bk)
                    if len(others) < cfg.outlier_min_books:
                        continue
                    mid = len(others) // 2
                    fair_p = others[mid] if len(others) % 2 else (others[mid - 1] + others[mid]) / 2
                    price = o[name]
                    edge = (fair_p * price - 1) * 100
                    if edge < cfg.outlier_min_pct:
                        continue
                    board = sorted(((titles[b], oo[name], (fair_p * oo[name] - 1) * 100,
                                     links.get((k, b, name), ""))
                                    for b, oo in full.items() if b not in reference_only),
                                   key=lambda r: r[1], reverse=True)
                    bet = EVBet(
                        event_id=ev["id"], sport=ev.get("sport_title", ev.get("sport_key", "")),
                        sport_key=ev.get("sport_key", ""), matchup=f"{ev['away_team']} @ {ev['home_team']}",
                        home_team=ev["home_team"], away_team=ev["away_team"],
                        commence_time=ev["commence_time"], is_live=is_live,
                        market=k[0], line=k[1], outcome=name, point=points.get((k, name)),
                        book=titles[bk], price=price, link=links.get((k, bk, name), ""),
                        fair_prob=fair_p, sharp_book=f"median of {len(others)} other books",
                        n_outcomes=n_out, sources_used=len(others), sources_total=len(others),
                        unit_size=cfg.unit_size, board=board,
                    )
                    bet.stake = kelly_stake(fair_p, price, cfg)
                    # Can the other side(s) be bet elsewhere to lock in a profit?
                    hedge = []
                    for other in sorted(names - {name}):
                        cands = [(oo[other], b) for b, oo in full.items()
                                 if b != bk and b not in reference_only]
                        if cands:
                            pr, b = max(cands)
                            hedge.append((other, titles[b], pr, links.get((k, b, other), "")))
                    if len(hedge) == len(names) - 1:
                        margin = 1 / price + sum(1 / h[2] for h in hedge)
                        if margin < 1:
                            bet.hedge, bet.hedge_pct = hedge, (1 / margin - 1) * 100
                    out.append(bet)
    return sorted(out, key=lambda b: b.ev_pct, reverse=True)


def without_outliers(evs: list[EVBet], outs: list[EVBet]) -> list[EVBet]:
    """Drop +EV alerts for bets that already have an outlier alert."""
    taken = {(o.event_id, o.market, o.line, o.outcome) for o in outs}
    return [b for b in evs if (b.event_id, b.market, b.line, b.outcome) not in taken]


def outlier_payload(b: EVBet, mention: str = "", gone_after: float | None = None,
                    first_seen: float | None = None) -> dict:
    """Outlier card: bet-now instruction, optional lock-in, then why and every book."""
    icon = sport_icon(b.sport_key)
    if gone_after is not None:
        return _gone_card(f"❌ GONE after {_fmt_secs(gone_after)} · ~~🚨 +{b.ev_pct:.0f}%~~ {b.pick} {odds(b.price)}",
                          f"Ignore this one. {b.book} fixed its price. ({icon} {b.matchup})")
    parts = [
        f"👉 **DO THIS NOW, before {b.book} fixes its price.**\n\n"
        f"Open **{_link(b.book, b.link)}** → bet **{b.stake_label}** on **{b.pick} {odds(b.price)}**\n"
        f"↳ skip if the price is worse than **{odds(b.worst_ok_price())}**",
    ]
    if b.hedge:
        legs = [(b.pick, b.book, b.price, b.link)] + [(o, bk, pr, ln) for o, bk, pr, ln in b.hedge]
        margin = sum(1 / pr for _, _, pr, _ in legs)
        lines = [f"• Open **{_link(bk, ln)}** → bet **{money(round(100 / pr / margin, 2))}** on {o} {odds(pr)}"
                 for o, bk, pr, ln in legs]
        parts.append(f"🔒 **Want a sure profit instead?** Place all of these for **+{b.hedge_pct:.1f}% "
                     f"guaranteed** (per $100 total):\n" + "\n".join(lines))
    details = (f"{icon} **{b.sport}** · {b.matchup}\n{_when(b.is_live, b.commence_time, first_seen)}\n\n"
               f"**Why:** {b.book} has {b.pick} at **{odds(b.price)}**, but the other {b.sources_used} books "
               f"say **{odds(b.fair_odds)}** ({b.fair_prob:.1%} to win).")
    desc = "\n\n".join(parts) + DIVIDER + details
    if b.board:
        desc += "\n\n**Every book**\n" + _board_lines(b)
    return _card(f"🚨 OUTLIER +{b.ev_pct:.0f}% · {b.pick} {odds(b.price)} at {b.book}", desc,
                 0xE67E22, url=b.link,
                 footer="OUTLIER = one book is way off. Bet fast; books can void obvious pricing errors.",
                 mention=mention)


class OutlierAlerter(EVAlerter):
    noun = "outliers"

    def text(self, item) -> str:
        t = format_ev_text(item).replace("📈", "🚨 OUTLIER", 1)
        if item.hedge:
            t += (f"\n  🔒 Lock in +{item.hedge_pct:.1f}%: also bet "
                  + ", ".join(f"{o} {odds(pr)} on {bk}" for o, bk, pr, _ in item.hedge))
        return t

    def payload(self, item, mention="", gone_after=None, first_seen=None) -> dict:
        return outlier_payload(item, mention, gone_after, first_seen)

    def mention(self) -> str:
        return self.cfg.outlier_mention

    def log_path(self) -> str:
        return self.cfg.outlier_log_file

    def webhook_for(self, item) -> str:
        return self.cfg.outlier_webhook_url or self.cfg.ev_webhook_url or self.cfg.webhook_url


# --------------------------------------------------------------------------- parlays

@dataclass
class Parlay:
    """2-3 +EV legs from different games on one ticket at one book."""
    book: str
    legs: list[tuple["EVBet", float, str]]  # (the +EV bet, its price at this book, link)
    stake: float = 0.0
    unit_size: float = 0.0
    is_live: bool = False
    sport: str = "Parlay"
    sport_key: str = "parlay"
    event_id: str = "parlay"

    @property
    def fair_prob(self) -> float:
        return math.prod(b.fair_prob for b, _, _ in self.legs)

    @property
    def price(self) -> float:
        return math.prod(pr for _, pr, _ in self.legs)

    @property
    def ev_pct(self) -> float:
        return (self.fair_prob * self.price - 1) * 100

    @property
    def matchup(self) -> str:
        return " + ".join(b.pick for b, _, _ in self.legs)

    @property
    def key(self) -> str:
        return "parlay|" + self.book + "|" + "|".join(sorted(b.key for b, _, _ in self.legs))

    @property
    def fingerprint(self) -> str:
        return self.key + "|" + ",".join(f"{pr}" for _, pr, _ in self.legs)

    @property
    def stake_label(self) -> str:
        units = f" ({self.stake / self.unit_size:.2g}u)" if self.unit_size > 0 else ""
        return f"{money(self.stake)}{units}"


def find_parlays(bets: list["EVBet"], cfg: Config) -> list[Parlay]:
    """Best +EV parlays from the current +EV bets: same book, different games, pre-game only."""
    if not cfg.parlays_enabled:
        return []
    per_book: dict[str, list[tuple["EVBet", float, str]]] = {}
    for b in bets:
        if b.is_live:
            continue
        for book, price, ev, link in b.board:
            if ev >= cfg.parlay_leg_min_ev_pct:  # boards only hold books allowed by EV_BOOKS
                per_book.setdefault(book, []).append((b, price, link))
    found: list[Parlay] = []
    for book, legs in per_book.items():
        legs = sorted(legs, key=lambda x: (x[0].fair_prob * x[1]), reverse=True)[:8]  # keep it small
        for n in range(2, cfg.parlay_max_legs + 1):
            for combo in itertools.combinations(legs, n):
                if len({b.event_id for b, _, _ in combo}) < n:
                    continue  # same game: legs are correlated and books price them differently
                p = Parlay(book, list(combo), unit_size=cfg.unit_size)
                if p.ev_pct >= cfg.parlay_min_ev_pct:
                    found.append(p)
    # Best first, and don't reuse a leg across alerts.
    out, used = [], set()
    for p in sorted(found, key=lambda p: p.ev_pct, reverse=True):
        keys = {b.key for b, _, _ in p.legs}
        if keys & used:
            continue
        p.stake = min(kelly_stake(p.fair_prob, p.price, cfg),
                      round(cfg.ev_bankroll * cfg.parlay_max_stake_pct / 100))
        out.append(p)
        used |= keys
        if len(out) >= cfg.parlay_max_alerts:
            break
    return out


def format_parlay_text(p: Parlay) -> str:
    legs = "\n".join(f"  {i}. {b.pick} {odds(pr)}  ({b.matchup})" for i, (b, pr, _) in enumerate(p.legs, 1))
    return (f"📦 PARLAY +{p.ev_pct:.1f}% EV | {len(p.legs)} legs at {p.book} | pays {odds(p.price)}"
            f"  → stake {p.stake_label}\n{legs}")


def parlay_payload(p: Parlay, mention: str = "", gone_after: float | None = None,
                   first_seen: float | None = None) -> dict:
    if gone_after is not None:
        return _gone_card(f"❌ GONE after {_fmt_secs(gone_after)} · ~~📦 +{p.ev_pct:.1f}%~~ parlay at {p.book}",
                          "Ignore this one. A leg's price moved and the parlay isn't worth it anymore.")
    nums = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
    legs = "\n".join(
        f"{nums[i] if i < 4 else '•'} **{_link(b.pick, ln)} {odds(pr)}**\n     {sport_icon(b.sport_key)} {b.matchup} · "
        + ("🔴 LIVE" if b.is_live else f"<t:{int(_parse_time(b.commence_time).timestamp())}:t>")
        for i, (b, pr, ln) in enumerate(p.legs))
    worst = (1 + OK_EDGE_PCT / 100) / p.fair_prob
    desc = (f"👉 **DO THIS: one parlay ticket at {p.book}.** Every leg must win. Pays big, wins less often.\n\n"
            f"Open **{p.book}** → add these {len(p.legs)} legs → bet **{p.stake_label}**\n\n{legs}\n\n"
            f"Ticket should pay about **{odds(p.price)}** · skip if it's worse than **{odds(worst)}**"
            f"{DIVIDER}**Chance all legs win:** {p.fair_prob:.1%} (fair) · each leg is +EV on its own.\n"
            f"Legs are from different games, so they don't affect each other.")
    return _card(f"📦 PARLAY +{p.ev_pct:.1f}% EV · {len(p.legs)} legs at {p.book} · {odds(p.price)}", desc,
                 0x9B59B6, footer="PARLAY = several +EV bets on one ticket. Small stake: parlays swing a lot.",
                 mention=mention)


PARLAY_FIELDS = ["first_seen", "book", "legs", "price", "fair_prob", "best_ev_pct", "stake", "games"]


class ParlayAlerter(Alerter):
    noun = "parlays"
    log_fields = PARLAY_FIELDS
    log_on_open = True

    def text(self, item) -> str:
        return format_parlay_text(item)

    def payload(self, item, mention="", gone_after=None, first_seen=None) -> dict:
        return parlay_payload(item, mention, gone_after, first_seen)

    def value(self, item) -> float:
        return item.ev_pct

    def label(self, item) -> str:
        return f"parlay at {item.book}"

    def mention(self) -> str:
        return self.cfg.parlay_mention

    def log_path(self) -> str:
        return self.cfg.parlay_log_file

    def webhook_for(self, item) -> str:
        return self.cfg.parlay_webhook_url or self.cfg.ev_webhook_url or self.cfg.webhook_url

    def row(self, op: OpenArb) -> dict:
        p = op.arb
        return {"book": p.book, "legs": " + ".join(f"{b.pick} {odds(pr)}" for b, pr, _ in p.legs),
                "price": round(p.price, 3), "fair_prob": round(p.fair_prob, 4),
                "best_ev_pct": round(op.best_pct, 2), "stake": p.stake,
                "games": " | ".join(b.matchup for b, _, _ in p.legs)}


# --------------------------------------------------------------------------- +EV results

RESULT_FIELDS = EV_LOG_FIELDS + ["home_score", "away_score", "result", "profit", "kind",
                                 "closing_fair_odds", "clv_pct"]


def settle(bet: dict, home_score: float, away_score: float) -> tuple[str, float]:
    """Grade one logged +EV bet against the final score. Returns (win/loss/push, profit)."""
    home, away = bet["home_team"], bet["away_team"]
    pick, market = bet["outcome"], bet["market"]
    point = float(bet["point"]) if bet.get("point") not in ("", None) else 0.0
    if market == "h2h":
        if pick == "Draw":
            res = "win" if home_score == away_score else "loss"
        elif home_score == away_score:
            res = "loss" if int(bet.get("n_outcomes") or 2) == 3 else "push"
        else:
            res = "win" if pick == (home if home_score > away_score else away) else "loss"
    elif market == "spreads":
        mine, theirs = (home_score, away_score) if pick == home else (away_score, home_score)
        diff = mine + point - theirs
        res = "win" if diff > 0 else ("push" if diff == 0 else "loss")
    elif market == "totals":
        total = home_score + away_score
        diff = total - point if pick == "Over" else point - total
        res = "win" if diff > 0 else ("push" if diff == 0 else "loss")
    else:
        return "unknown", 0.0
    stake, price = float(bet["stake"]), float(bet["price"])
    profit = stake * (price - 1) if res == "win" else (-stake if res == "loss" else 0.0)
    return res, round(profit, 2)


def _read_csv(name: str) -> list[dict]:
    path = data_path(name)
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def _bet_id(row: dict) -> str:
    player = f"{row['player']}|" if row.get("player") else ""
    return f"{row['event_id']}|{row['market']}|{player}{row['outcome']}|{row['point']}"


def settle_pending(cfg: Config, api: "OddsAPI") -> int:
    """Grade logged +EV alerts whose games have finished. Uses the scores endpoint
    (2 credits per sport, only for sports with ungraded bets from the last 3 days)."""
    done = {_bet_id(r) for r in _read_csv(cfg.ev_results_file)}
    pending: dict[str, dict] = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    closing = {r["bet_id"]: float(r["closing_fair_prob"]) for r in _read_csv(cfg.closing_file)}
    for kind, name in (("ev", cfg.ev_log_file), ("outlier", cfg.outlier_log_file)):
        for r in _read_csv(name) if name else []:
            bid = _bet_id(r)
            if bid in done or bid in pending or r.get("player"):
                continue  # repeats count once; props need player stats, so CLV judges those
            start = _parse_time(r["commence_time"])
            if cutoff < start < datetime.now(timezone.utc) - timedelta(hours=2):
                cp = closing.get(bid)
                pending[bid] = {**r, "kind": kind,
                                "closing_fair_odds": round(1 / cp, 3) if cp else "",
                                "clv_pct": round(clv_pct(float(r["price"]), cp), 2) if cp else ""}
    if not pending:
        return 0
    graded = 0
    for sport in sorted({r["sport_key"] for r in pending.values()}):
        try:
            scores = {s["id"]: s for s in api.scores(sport) if s.get("completed") and s.get("scores")}
        except Exception as e:  # noqa: BLE001
            print(f"! Couldn't load {sport} scores: {e}", file=sys.stderr)
            continue
        for r in pending.values():
            s = scores.get(r["event_id"]) if r["sport_key"] == sport else None
            if not s:
                continue
            pts = {x["name"]: float(x["score"]) for x in s["scores"]}
            if r["home_team"] not in pts or r["away_team"] not in pts:
                continue
            res, profit = settle(r, pts[r["home_team"]], pts[r["away_team"]])
            append_csv(cfg.ev_results_file, RESULT_FIELDS, {
                **r, "home_score": pts[r["home_team"]], "away_score": pts[r["away_team"]],
                "result": res, "profit": profit})
            graded += 1
    return graded


def ev_record(cfg: Config, days: int | None = None) -> str:
    rows = _read_csv(cfg.ev_results_file)
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        rows = [r for r in rows if _parse_time(r["commence_time"]) >= cutoff]
    rows = [r for r in rows if r["result"] in ("win", "loss", "push")]
    if not rows:
        return "no graded bets yet"
    w = sum(r["result"] == "win" for r in rows)
    l = sum(r["result"] == "loss" for r in rows)
    pu = len(rows) - w - l
    staked = sum(float(r["stake"]) for r in rows if r["result"] != "push")
    profit = sum(float(r["profit"]) for r in rows)
    avg_ev = sum(float(r["best_ev_pct"]) for r in rows) / len(rows)
    roi = profit / staked * 100 if staked else 0
    return (f"{len(rows)} bets, {w}-{l}-{pu}, {'+' if profit >= 0 else '-'}${abs(profit):,.2f} "
            f"on ${staked:,.0f} staked (ROI {roi:+.1f}%, avg edge {avg_ev:.1f}%)")


# --------------------------------------------------------------------------- closing line value (CLV)

CLOSING_FIELDS = ["bet_id", "closing_fair_prob", "closing_fair_odds", "closed_at"]


def _row_line(row: dict) -> float | None:
    """The (market, line) grouping key's line for a logged bet (see _line_for)."""
    if row.get("player"):
        return (row["player"], float(row["point"]) if row.get("point") not in ("", None) else None)
    if row.get("point") in ("", None):
        return None
    point = float(row["point"])
    if row["market"] == "spreads":
        return point if row["outcome"] == row["home_team"] else -point
    return point


def clv_pct(price: float, closing_fair_prob: float) -> float:
    """Edge of the price you got, measured against the closing fair line."""
    return (price * closing_fair_prob - 1) * 100


class ClosingTracker:
    """Keeps the latest sharp fair price for every logged pre-game bet until kickoff, then
    saves it as the closing line. Restarts are fine: pending bets reload from the logs."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.latest: dict[str, tuple[float, datetime]] = {}
        self.written = {r["bet_id"] for r in _read_csv(cfg.closing_file)}
        self.tracked: dict[str, dict] = {}
        now = datetime.now(timezone.utc)
        for name in (cfg.ev_log_file, cfg.outlier_log_file):
            for r in _read_csv(name) if name else []:
                if _parse_time(r["commence_time"]) > now:
                    self.add(r)

    def add(self, row: dict) -> None:
        bid = _bet_id(row)
        if bid in self.written or bid in self.tracked:
            return
        if _parse_time(row["first_seen"]) >= _parse_time(row["commence_time"]):
            return  # live bet: there's no closing line to beat
        self.tracked[bid] = row

    def needs_close(self) -> set[str]:
        """Event ids that still need a last look before kickoff."""
        return {r["event_id"] for r in self.tracked.values()}

    def observe(self, events: list[dict], now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        wanted = self.needs_close()
        for ev in events:
            if ev["id"] not in wanted or _parse_time(ev["commence_time"]) <= now:
                continue
            fair = sharp_fair(ev, self.cfg, now, False)[0]
            for bid, r in self.tracked.items():
                if r["event_id"] != ev["id"]:
                    continue
                p = fair.get((r["market"], _row_line(r)), {}).get(r["outcome"])
                if p:
                    self.latest[bid] = (p, now)

    def finalize(self, now: datetime | None = None) -> int:
        """Save closing lines for games that have started. Returns how many were saved."""
        now = now or datetime.now(timezone.utc)
        saved = 0
        for bid, r in list(self.tracked.items()):
            if _parse_time(r["commence_time"]) > now:
                continue
            del self.tracked[bid]
            if bid in self.latest:
                p, ts = self.latest.pop(bid)
                append_csv(self.cfg.closing_file, CLOSING_FIELDS, {
                    "bet_id": bid, "closing_fair_prob": round(p, 5), "closing_fair_odds": round(1 / p, 3),
                    "closed_at": ts.isoformat(timespec="seconds")})
                self.written.add(bid)
                saved += 1
        return saved


def clv_rows(cfg: Config, days: int | None = None) -> list[dict]:
    """Every logged bet (first alert only) that has a closing line, with its CLV."""
    closing = {r["bet_id"]: float(r["closing_fair_prob"]) for r in _read_csv(cfg.closing_file)}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days is not None else None
    out, seen = [], set()
    for kind, name in (("ev", cfg.ev_log_file), ("outlier", cfg.outlier_log_file)):
        for r in _read_csv(name) if name else []:
            bid = _bet_id(r)
            if bid in seen or bid not in closing:
                continue
            if cutoff and _parse_time(r["commence_time"]) < cutoff:
                continue
            seen.add(bid)
            p = closing[bid]
            out.append({**r, "kind": kind, "closing_fair_odds": 1 / p,
                        "clv_pct": clv_pct(float(r["price"]), p),
                        "beat_close": float(r["price"]) > 1 / p})
    return out


def clv_record(cfg: Config, days: int | None = None) -> str:
    rows = clv_rows(cfg, days)
    if not rows:
        return "no closing lines yet"
    avg = sum(r["clv_pct"] for r in rows) / len(rows)
    beat = sum(r["beat_close"] for r in rows) / len(rows) * 100
    return f"avg {avg:+.1f}%, beat the close on {beat:.0f}% of {len(rows)} bets"


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
        self.need_close: set[str] = set()  # event ids wanting a last pre-kickoff check (CLV)
        self.last_props: dict[str, float] = {}  # event id -> last prop check
        self.scale = 1.0
        self.forecast = 0.0      # credits the next 24h would cost at full speed
        self.allowance = 0.0     # credits we can afford per day
        self.budget_at = 0.0

    # ---- game state

    def state(self, sport: str, now: datetime) -> str | None:
        dur = timedelta(minutes=self.cfg.minutes_for(sport))
        soon = timedelta(hours=self.cfg.pregame_hours)
        want_live = (self.cfg.arb_live or (self.cfg.ev_enabled and self.cfg.ev_live)
                     or (self.cfg.outliers_enabled and self.cfg.outlier_live))
        pregame = False
        for gid, start in self.games[sport]:
            if gid in self.ended:
                continue
            if start <= now < start + dur:
                if want_live:
                    return LIVE
                continue  # nothing live is wanted: don't pay to check games in progress
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
            demand += STEP * self._prop_rate(t)
        self.forecast = demand
        if self.allowance <= 0:
            self.scale = math.inf
        else:
            self.scale = max(1.0, demand / self.allowance)
        self.budget_at = time.time()

    def _prop_games(self, now: datetime) -> list[tuple[str, str]]:
        """(sport, event id) for games inside the props window before kickoff."""
        if not self.cfg.props_enabled:
            return []
        window = timedelta(hours=self.cfg.prop_hours)
        return [(sport, gid) for sport in self.cfg.prop_sports if sport in self.games
                and self.cfg.prop_markets.get(sport)
                for gid, start in self.games[sport] if now < start <= now + window]

    def _prop_rate(self, t: datetime) -> float:
        """Credits per second spent on props at time t (at full speed)."""
        return sum(self.cfg.prop_credits_per_call(sport) / (self.cfg.prop_minutes * 60)
                   for sport, _ in self._prop_games(t))

    def props_due(self, now: datetime) -> list[tuple[str, str]]:
        ts = time.time()
        every = self.cfg.prop_minutes * 60 * self.scale
        return [(sport, gid) for sport, gid in self._prop_games(now)
                if ts - self.last_props.get(gid, 0) >= every]

    def fetch_props(self, games: list[tuple[str, str]]) -> list[dict]:
        def one(item):
            sport, gid = item
            try:
                return item, self.api.event_odds(sport, gid, self.cfg.prop_markets[sport])
            except Exception as e:  # noqa: BLE001 - reported below
                return item, e

        events: list[dict] = []
        with ThreadPoolExecutor(max_workers=min(8, len(games))) as pool:
            for (sport, gid), result in pool.map(one, games):
                self.last_props[gid] = time.time()
                if isinstance(result, urllib.error.HTTPError):
                    if result.code in (401, 429):
                        raise result
                    print(f"! Props error for {sport} {gid}: {result.code}", file=sys.stderr)
                elif isinstance(result, Exception):
                    print(f"! Props error for {sport} {gid}: {result}", file=sys.stderr)
                elif isinstance(result, dict) and result.get("bookmakers"):
                    events.append(result)
        return events

    # ---- polling

    def closing_due(self, sport: str, now: datetime) -> bool:
        """A game with a logged bet starts within CLOSING_MINUTES and we haven't looked since."""
        if not self.cfg.closing_minutes or not self.need_close:
            return False
        window = timedelta(minutes=self.cfg.closing_minutes)
        return any(gid in self.need_close and now < start <= now + window
                   and self.last_odds[sport] < (start - window).timestamp()
                   for gid, start in self.games[sport])

    def due(self, now: datetime) -> list[str]:
        ts = time.time()
        out = []
        for sport in self.cfg.sports:
            st = self.state(sport, now)
            if (st and ts - self.last_odds[sport] >= self.interval(st)) or self.closing_due(sport, now):
                out.append(sport)
        return out

    def seconds_to_next(self, now: datetime) -> float:
        ts = time.time()
        waits = [self.last_odds[s] + self.interval(st) - ts
                 for s in self.cfg.sports if (st := self.state(s, now))]
        if self.cfg.props_enabled:
            every = self.cfg.prop_minutes * 60 * self.scale
            waits += [self.last_props.get(gid, 0) + every - ts for _, gid in self._prop_games(now)]
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

def feed_freshness(events: list[dict], now: datetime | None = None) -> str:
    """How old the feed's prices are, split by live and pre-game games."""
    now = now or datetime.now(timezone.utc)
    ages: dict[str, list[float]] = {"live": [], "pre-game": []}
    for ev in events:
        group = "live" if _parse_time(ev["commence_time"]) <= now else "pre-game"
        for bm in ev.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                ts = mkt.get("last_update") or bm.get("last_update")
                if ts:
                    ages[group].append(max(0.0, (now - _parse_time(ts)).total_seconds()))
    parts = []
    for group, xs in ages.items():
        if xs:
            xs.sort()
            mid, p90 = xs[len(xs) // 2], xs[min(len(xs) - 1, int(len(xs) * 0.9))]
            parts.append(f"{group}: half of prices under {_fmt_secs(mid)} old, 90% under {_fmt_secs(p90)}")
    return "; ".join(parts) or "no prices"


GUIDE = """**When an alert pops up, do what its 👉 line says. That's it.**

💰 **ARB** (green, or red if the game is live)
Bet **every** side shown, each at the book listed, using the exact amounts. You profit no matter who wins.
• Place the bets back to back.
• If a price has moved past its "skip" line, don't place the other bet.

📈 **+EV** (blue)
Bet **one** side at the book shown, for the amount shown. It's a better price than it should be, but it won't win every time. It pays off over many bets.

🚨 **OUTLIER** (orange, pings everyone)
One book's price is way off from all the others. Bet it fast, before they fix it.
Optional: also place the 🔒 bets it lists to lock in a guaranteed profit.

📦 **PARLAY** (purple)
One ticket with 2-3 +EV bets from different games, all at the same book. Every leg must win. Bigger payout, wins less often, so the stake is small.

🎯 **Player props** show up as normal +EV, arb or outlier alerts, e.g. "LeBron James Over 25.5 Points".

❌ **GONE** (grey)
The chance is over. Ignore it.

**Every time**
1. Tap the book name to open it. Check the price matches the alert, or is better.
2. Worse than the "skip" price? Don't bet.
3. Bet the exact amount shown. Don't go bigger.
4. 🟢 in "Every book" means that book's price is also good to bet.
5. Books sometimes cancel bets on obvious mistakes and refund the money. With an arb, the other bet still stands, so you're left with one normal bet."""


def guide_payload() -> dict:
    return _card("📖 How to use these alerts", GUIDE, 0x5865F2,
                 footer="Pin this message: hover over it → ⋯ → Pin Message")


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
    if cfg.props_enabled:
        prop_games = {gid for h in range(0, 24 * 60, 10) for _, gid in sched._prop_games(now + timedelta(minutes=h))}
        print(f"\nProps: {len(prop_games)} games in the next 24h, checked every {cfg.prop_minutes}m "
              f"in the {cfg.prop_hours:g}h before kickoff ({', '.join(short(s) for s in cfg.prop_sports)}).")
    print(f"\nFull speed would use {sched.forecast:,.0f} credits in the next 24h; "
          f"you can afford {sched.allowance:,.0f}/day.")
    if sched.scale > 1:
        print(f"→ Will slow down {sched.scale:.2f}×: live checks every "
              f"{sched.interval(LIVE):.0f}s instead of {cfg.poll_seconds}s.")
    else:
        print(f"→ Fits the budget at full speed (live checks every {cfg.poll_seconds}s).")


def run(cfg: Config, args: argparse.Namespace, status: Status) -> None:
    alerter = Alerter(cfg, dry_run=args.dry_run)
    ev_alerter = EVAlerter(cfg, dry_run=args.dry_run)
    out_alerter = OutlierAlerter(cfg, dry_run=args.dry_run)
    tracker = ClosingTracker(cfg)
    ev_alerter.on_log = out_alerter.on_log = tracker.add
    # Props are fetched per game on their own schedule, so they get their own alert trackers
    # (a main-line check must never "close" a prop alert it didn't look at).
    prop_arbs, prop_evs, prop_outs = (Alerter(cfg, dry_run=args.dry_run), EVAlerter(cfg, dry_run=args.dry_run),
                                      OutlierAlerter(cfg, dry_run=args.dry_run))
    for a, noun in ((prop_arbs, "prop arbs"), (prop_evs, "+EV props"), (prop_outs, "prop outliers")):
        a.noun = noun
    prop_evs.on_log = prop_outs.on_log = tracker.add
    prop_cfg = cfg.for_props()
    parlay_alerter = ParlayAlerter(cfg, dry_run=args.dry_run)

    def update_parlays() -> int:
        open_bets = [op.arb for a in (ev_alerter, prop_evs) for op in a.open.values()]
        return parlay_alerter.handle(find_parlays(open_bets, cfg))

    if args.demo:
        # Sample data must never reach the real logs (it would be graded and counted).
        for a in (alerter, ev_alerter, out_alerter, parlay_alerter):
            a.log_path = lambda: ""
        events = demo_events()
        outs = find_outliers(events, cfg)
        arbs, evs = find_arbs(events, cfg), without_outliers(find_evs(events, cfg), outs)
        alerter.handle(arbs)
        ev_alerter.handle(evs)
        out_alerter.handle(outs)
        print(f"[demo] {len(arbs)} arb(s), {len(evs)} +EV bet(s), {len(outs)} outlier(s) in sample data")
        return

    api = OddsAPI(cfg)
    sched = Scheduler(cfg, api)

    if args.plan:
        print_plan(cfg, sched)
        return

    if args.results:
        n = settle_pending(cfg, api)
        print(f"Graded {n} new bet(s).")
        print(f"+EV record, last 7 days (every alert, at the alerted price): {ev_record(cfg, 7)}")
        print(f"+EV record, all time: {ev_record(cfg)}")
        print(f"CLV (price you got vs the closing fair line), last 7 days: {clv_record(cfg, 7)}")
        print(f"CLV, all time: {clv_record(cfg)}")
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
    sharp_seen = False
    sharp_checks = 0
    while True:
        now = datetime.now(timezone.utc)
        local = now.astimezone(tz)
        if (cfg.summary_hour >= 0 and not args.once and local.hour >= cfg.summary_hour
                and local.date() != summary_day):
            summary_day = local.date()
            msg = f"📊 Last 24h: {alerter.summary()}."
            if cfg.outliers_enabled:
                msg += f"\n🚨 {out_alerter.summary()}."
                out_alerter.reset_stats()
            if cfg.parlays_enabled:
                msg += f"\n📦 {parlay_alerter.summary()}."
                parlay_alerter.reset_stats()
            if cfg.props_enabled:
                msg += f"\n🎯 Props: {prop_evs.summary()}; {prop_arbs.summary()}; {prop_outs.summary()}."
                for a in (prop_arbs, prop_evs, prop_outs):
                    a.reset_stats()
            if cfg.ev_enabled:
                try:
                    settle_pending(cfg, api)
                except Exception as e:  # noqa: BLE001 - a summary shouldn't crash the bot
                    print(f"! Grading +EV bets failed: {e}", file=sys.stderr)
                msg += (f"\n📈 {ev_alerter.summary()}.\n"
                        f"+EV results if you bet every alert: last 7 days {ev_record(cfg, 7)}; "
                        f"all time {ev_record(cfg)}.\n"
                        f"📐 CLV: last 7 days {clv_record(cfg, 7)}; all time {clv_record(cfg)}.")
            left = f"{api.remaining:,.0f}" if api.remaining is not None else "?"
            status.send(f"{msg}\nCredits left: {left}.")
            alerter.reset_stats()
            ev_alerter.reset_stats()

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
            sched.need_close = tracker.needs_close()
            events = sched.fetch(due, now)
            arbs = find_arbs(events, cfg)
            sent = alerter.handle(arbs, checked_sports=due)
            outs = find_outliers(events, cfg)
            out_sent = out_alerter.handle(outs, checked_sports=due)
            evs = without_outliers(find_evs(events, cfg), outs)  # the outlier alert covers those
            ev_sent = ev_alerter.handle(evs, checked_sports=due)
            tracker.observe(events, now)
            tracker.finalize()
            status.check_credits(api.remaining)
            left = f"{api.remaining:,.0f}" if api.remaining is not None else "?"
            ev_note = f" | {len(evs)} +EV, {ev_sent} new" if cfg.ev_enabled else ""
            ev_note += f" | {len(outs)} outliers, {out_sent} new" if cfg.outliers_enabled else ""
            print(f"[{datetime.now():%H:%M:%S}] checked {', '.join(short(s) for s in due)} "
                  f"({len(events)} games, {time.time() - t0:.1f}s) | {len(arbs)} arbs, {sent} new"
                  f"{ev_note} | credits left {left}", flush=True)

            seen_books = {bm["key"] for ev in events for bm in ev.get("bookmakers", [])}
            if args.once:
                wanted = set(_csv(cfg.bookmakers))
                print(f"Feed freshness: {feed_freshness(events)}")
                print(f"Books in the feed: {', '.join(sorted(seen_books)) or 'none'}")
                if wanted - seen_books:
                    print(f"Not in the feed right now (check the key spelling): "
                          f"{', '.join(sorted(wanted - seen_books))}")
            if cfg.ev_enabled and not sharp_seen and events:
                sharp_seen = bool(seen_books & set(_csv(cfg.sharp_books)))
                sharp_checks += 1
                if not sharp_seen and sharp_checks == 5:
                    status.send(f"⚠️ No {cfg.sharp_books} odds in the feed, so +EV alerts can't work. "
                                f"Add {_csv(cfg.sharp_books)[0]} to BOOKMAKERS in .env.")
        elif not idle_logged and not any(sched.state(s, now) for s in cfg.sports):
            # Only when nothing is on at all, not just between checks of a live game.
            idle_logged = True
            print(f"[{datetime.now():%H:%M:%S}] No games live or starting soon. Waiting (no credits used).",
                  flush=True)

        prop_games = sched._prop_games(now) if args.once else sched.props_due(now)
        if prop_games:
            t0 = time.time()
            prop_events = sched.fetch_props(prop_games)
            checked = {gid for _, gid in prop_games}
            p_outs = find_outliers(prop_events, cfg)
            n_arb = prop_arbs.handle(find_arbs(prop_events, cfg), checked_events=checked)
            n_out = prop_outs.handle(p_outs, checked_events=checked)
            n_ev = prop_evs.handle(without_outliers(find_evs(prop_events, prop_cfg), p_outs),
                                   checked_events=checked)
            tracker.observe(prop_events, now)
            left = f"{api.remaining:,.0f}" if api.remaining is not None else "?"
            print(f"[{datetime.now():%H:%M:%S}] props: {len(prop_games)} games ({time.time() - t0:.1f}s) | "
                  f"{n_arb} new arbs, {n_ev} new +EV, {n_out} new outliers | credits left {left}", flush=True)

        if (due or prop_games) and cfg.parlays_enabled:
            n_par = update_parlays()
            if n_par:
                print(f"  📦 {n_par} new parlay(s)", flush=True)

        if args.once:
            if not due and not prop_games:
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
    p.add_argument("--set-webhook", choices=sorted(WEBHOOK_SETTINGS), metavar="CHANNEL",
                   help="paste a Discord webhook URL for a channel: " + ", ".join(sorted(WEBHOOK_SETTINGS)))
    p.add_argument("--post-guide", action="store_true",
                   help="post a how-to-use guide to your Discord channel (then pin it)")
    p.add_argument("--results", action="store_true",
                   help="grade finished +EV alerts and print the win/loss record (2 credits per sport)")
    args = p.parse_args()

    if args.set_webhook:
        set_webhook(args.set_webhook)
        return

    cfg = Config.from_env()
    global ODDS_FORMAT
    ODDS_FORMAT = cfg.odds_format

    if args.post_guide:
        if not cfg.webhook_url:
            sys.exit("Set DISCORD_WEBHOOK_URL in .env first.")
        _webhook(cfg.webhook_url, guide_payload())
        print("Posted the guide. In Discord: hover over it → ⋯ → Pin Message.")
        return

    if args.test_discord:
        if not cfg.webhook_url:
            sys.exit("Set DISCORD_WEBHOOK_URL in .env first.")
        events = demo_events()
        arb = find_arbs(events, cfg)[0]
        cfg.bad_webhooks()
        ev_url = cfg.ev_webhook_url or cfg.webhook_url
        samples = [(discord_payload(arb), cfg.webhook_url, "arb")] \
            + [(ev_payload(b), ev_url, "+EV") for b in find_evs(events, cfg)[:1]] \
            + [(outlier_payload(o), cfg.outlier_webhook_url or ev_url, "outlier") for o in find_outliers(events, cfg)[:1]]
        ids = []
        for payload, url, kind in samples:  # each to the channel real alerts of that kind use
            emb = payload["embeds"][0]
            emb["title"] = ("🧪 SAMPLE · " + emb["title"])[:256]
            emb["footer"] = {"text": "SAMPLE ALERT: made-up game and prices. Don't bet this."}
            emb.pop("url", None)
            ids.append((_webhook(url, payload) or {}).get("id"))
            print(f"  sent the {kind} sample to {'the main channel' if url == cfg.webhook_url else 'its own channel'}")
        print(f"Sent {len(samples)} sample alerts. In 5 seconds the arb turns 'GONE'...")
        time.sleep(5)
        if ids and ids[0]:
            _webhook(cfg.webhook_url, discord_payload(arb, gone_after=5), "PATCH", ids[0])
        print("Done. Check your Discord channel.")
        return

    if not args.demo and not cfg.api_key:
        print("Set ODDS_API_KEY in .env (key at https://the-odds-api.com), or run with --demo.", file=sys.stderr)
        sys.exit(2)

    bad = cfg.bad_webhooks()
    status = Status(cfg, dry_run=args.dry_run or args.demo or args.plan or args.once or args.results)
    if bad:
        status.send(f"⚠️ {', '.join(bad)} in .env isn't a Discord webhook URL, so those alerts are going "
                    f"to this channel for now. Fix it with: nano /opt/arb-bot/.env")
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
