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
import calendar
import csv
import functools
import gzip
import hashlib
import itertools
import json
import math
import re
import os
import statistics
import sys
import textwrap
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple
from zoneinfo import ZoneInfo

API_BASE = os.environ.get("ODDS_API_BASE", "https://api.the-odds-api.com/v4")
HERE = Path(__file__).resolve().parent
BOT_NAME = "EV BOT"   # the sender name on every Discord message (and what phones read out for it)

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

CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}

# Books with the same owner, which share most of their lines: one vote in a market consensus.
SISTER_BOOKS = ({"betonlineag", "lowvig"},)
# Exchanges: anyone can post a price there, so a thin or forgotten order can sit on a prop line. Their
# prices count in a consensus like any book's, but not toward its "6 sportsbooks price it" point.
EXCHANGES = {"kalshi", "novig", "prophetx", "polymarket", "betfair_ex_eu", "betfair_ex_uk", "betfair_ex_au",
             "matchbook", "smarkets"}
# Each fair-odds reference's weight in a blend, in the blueprint's trust order: Pinnacle, then Circa (not
# in The Odds API's feed: kept for later), then Kalshi; any other sharp book OTHER_SOURCE_WEIGHT.
# SHARP_WEIGHTS (props: SHARP_WEIGHTS_PROPS) can set any of them; one it doesn't list keeps its weight
# here. They're shared out over the sources that price a line: Pinnacle + Kalshi = 0.77 / 0.23.
SOURCE_WEIGHTS = {"pinnacle": 0.50, "circasports": 0.35, "kalshi": 0.15}
OTHER_SOURCE_WEIGHT = 0.15

# ALERT_MODE=locks floors: only alerts worth acting on.
LOCKS = {
    "arb_pct": 2.0, "live_arb_pct": 5.0, "arb_dollars": 0.0,   # $2+ per $100 staked, guaranteed
    "ev_pct": 5.0, "prop_ev_pct": 8.0, "min_confidence": "high",
    "outlier_pct": 15.0, "outlier_live_pct": 20.0, "parlay_ev_pct": 15.0, "parlay_legs": 2,
    "ev_per_hour": 6, "prop_per_hour": 6, "parlay_per_hour": 2,
    "arb_per_hour": 4, "outlier_per_hour": 6,
    "live_per_hour": 3,   # every live alert together (arbs, outliers, live +EV): fewer live, more pre-game
    # A live alert pings only when two checks in a row find it, and the price you're told to bet
    # was updated by its book in the last 60 seconds: so it's still there when you tap it.
    "live_confirm": 2, "live_max_age": 60,
    # LIVE_MARKETS=h2h (opt-in, set it in .env) splits the checks: live checks ask for moneylines only
    # (1 credit; upcoming games' moneylines come with them) and spreads/totals of upcoming games get
    # their own check (2 credits) every minute. On a tight budget the live check then slows down first
    # (up to LIVE_MAX_STRETCH), upcoming moneylines with it. Off by default: it trades upcoming
    # moneylines for spreads/totals, which is the users' call.
    "live_markets": "", "live_max_stretch": 3.0,
    # A pre-game moneyline, spread or total can go out from 3.5% (college 4.5%) instead of 5% when a
    # second sharp source agrees with Pinnacle's fair odds: Kalshi, or 4+ other sportsbooks, within
    # 1.5 points of win chance. It still needs high confidence and every other check.
    "pregame_confirmed_ev_pct": 3.5, "kalshi_confirm_pts": 1.5, "consensus_confirm_pts": 1.5,
    # Player props: Overs only (PROP_SIDES=both to get the Unders too).
    "prop_sides": "over",
}

# Confirmed pre-game bets (PREGAME_CONFIRMED_EV_PCT): main lines only, college games need this much
# more edge, and a spread or total needs this many other sportsbooks (not exchanges; sister books are
# one) in the consensus that agrees with the sharp book. The bet's own book is never one of them.
MAIN_MARKETS = ("h2h", "spreads", "totals")
CONFIRM_COLLEGE_EXTRA = 1.0
CONFIRM_MIN_BOOKS = 4
CONFIRMED = "confirmed"   # ev_bets.csv's `tier` for one ("" for every other bet)
SHARP_MOVE_PTS = 1.5      # the sharp line moved this many win-% points within MOVE_WINDOW_MINUTES: it counts
# Why a confirmed bet misses when something is only missing this check (not disagreeing): Kalshi has no
# usable price, too few other sportsbooks price the line, or the sharp line was never seen before (a
# restart, a new line). A new bet waits; one whose card is already up stays up quietly.
CONFIRM_MISSING = ("no Kalshi price", f"under {CONFIRM_MIN_BOOKS} other sportsbooks", "no sharp history yet")

# An arb whose price that will move is on Kalshi, with every sportsbook bet at or worse than fair,
# is easy on your sportsbook accounts (they only see normal bets). It ranks this many points higher
# for the hourly caps; the card doesn't change.
ACCOUNT_SAFE_BONUS = 0.5

# Bigger minimum edge where the sharp line is less reliable (lots of small games).
DEFAULT_SPORT_MIN_EV = {"americanfootball_ncaaf": 6.0, "basketball_ncaab": 6.0}

DEFAULT_BUDGET_WEIGHTS = {"mon": 1.2, "tue": 0.6, "wed": 0.6, "thu": 1.2, "fri": 1.0, "sat": 2.0, "sun": 2.2}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# The Odds API's bookmaker keys whose display names aren't just the key ("williamhill_us" is Caesars).
BOOK_TITLES = {"williamhill_us": "Caesars", "espnbet": "ESPN BET", "hardrockbet": "Hard Rock Bet",
               "ballybet": "Bally Bet", "betonlineag": "BetOnline.ag", "lowvig": "LowVig.ag",
               "mybookieag": "MyBookie.ag", "betparx": "betPARX", "betrivers": "BetRivers", "fanatics": "Fanatics"}

# New York's licensed sportsbooks can't take bets on a game with a New York college team in it
# (Racing, Pari-Mutuel Wagering and Breeding Law 1367). Kalshi (an exchange) isn't one of them.
NY_BOOKS = {"fanduel", "draftkings", "betmgm", "williamhill_us", "betrivers", "fanatics", "ballybet"}
NY_SCHOOLS = ["albany", "ualbany", "army", "binghamton", "buffalo", "canisius", "clarkson", "colgate", "columbia",
              "cornell", "fordham", "hobart", "hofstra", "iona", "le moyne", "liu", "long island", "manhattan",
              "marist", "niagara", "rensselaer", "rpi", "rit", "siena", "st bonaventure", "st johns",
              "st lawrence", "stony brook", "syracuse", "union", "wagner"]   # as _words() spells them

# Prop markets checked per sport (each one costs a credit per game per check).
DEFAULT_PROP_MARKETS = {
    "americanfootball_nfl": "player_pass_yds,player_rush_yds,player_reception_yds,player_receptions",
    "americanfootball_ncaaf": "player_pass_yds,player_rush_yds,player_reception_yds",
    "basketball_nba": "player_points,player_rebounds,player_assists,player_threes",
    "icehockey_nhl": "player_points,player_shots_on_goal,player_assists",
    "baseball_mlb": "batter_hits,batter_total_bases,pitcher_strikeouts",
}
# More prop types asked for only in the last PROP_HOURS before kickoff, in the same request (each costs a
# credit per game per check there). The Odds API rejecting one (422) only turns these off for that sport.
DEFAULT_PROP_NEAR_MARKETS = {
    "icehockey_nhl": "player_goals,player_total_saves",
    # Alternate lines ("250+ yards" ladders): only ever a price to bet at exactly the line a fair price
    # is for, never a fair price themselves (see base_market).
    "americanfootball_nfl": "player_pass_yds_alternate,player_rush_yds_alternate,player_reception_yds_alternate,"
                            "player_receptions_alternate",
}


# --------------------------------------------------------------------------- config

WEBHOOK_SETTINGS = {
    "main": ("DISCORD_WEBHOOK_URL", "arbs (and anything without its own channel)"),
    "ev": ("DISCORD_EV_WEBHOOK_URL", "+EV bets, outliers and parlays (props too, unless they have their own channel)"),
    "outlier": ("DISCORD_OUTLIER_WEBHOOK_URL", "outliers"),
    "parlay": ("DISCORD_PARLAY_WEBHOOK_URL", "parlays"),
    "props": ("DISCORD_PROPS_WEBHOOK_URL", "player props (+EV and outliers)"),
    "live": ("DISCORD_LIVE_WEBHOOK_URL", "live arbs"),
    "status": ("DISCORD_STATUS_WEBHOOK_URL", "bot health messages"),
    "results": ("DISCORD_RESULTS_WEBHOOK_URL", "bet results (what hit and what missed)"),
    "test": ("DISCORD_TEST_WEBHOOK_URL", "sample alerts from --test-discord (nothing else)"),
}


def _env_key(line: str) -> str | None:
    """The setting a .env line sets, read the way load_dotenv reads it ("KEY = v" counts)."""
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    return s.split("=", 1)[0].strip()


def set_env_value(path: Path, key: str, value: str) -> None:
    """Replace KEY=... in .env (or add it), keeping every other line as it is."""
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [l for l in lines if _env_key(l) != key]
    lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")


def with_unit_bankroll(changes: dict[str, str]) -> dict[str, str]:
    """--set UNIT_SIZE=X also sets EV_BANKROLL to 100 x X, so changing the unit scales every stake
    (100 units = the bankroll), unless the same command sets EV_BANKROLL itself. UNIT_SIZE=0 (1u =
    1% of EV_BANKROLL) and a value that isn't a number (refused later) change nothing else."""
    try:
        unit = float(changes.get("UNIT_SIZE", ""))
    except ValueError:
        return changes
    if not math.isfinite(unit) or unit <= 0 or "EV_BANKROLL" in changes:
        return changes
    return {**changes, "EV_BANKROLL": _plain(unit * 100)}


def check_settings(changes: dict[str, str]) -> tuple[str, list[str]]:
    """Would these new values load? Returns (the problem with one of them, or "", and problems
    with OTHER settings already in .env). Each key is checked on its own merits: a bad setting
    elsewhere doesn't hide (or block) a bad new one."""
    saved = {k: os.environ.get(k) for k in changes}
    os.environ.update(changes)
    removed: dict[str, str | None] = {}
    others: list[str] = []
    try:
        for _ in range(50):
            try:
                Config.from_env()
                return "", others
            except ValueError as ex:
                msg = str(ex)
                key = msg.split("=", 1)[0].split(":", 1)[0].strip()
                if key in changes:
                    return msg, others
                if key not in os.environ or key in removed:
                    return "", others + [msg]   # can't isolate it; it isn't one of the new values
                others.append(msg)
                removed[key] = os.environ.pop(key)   # set the other bad one aside and look again
        return "", others
    finally:
        for k, v in {**removed, **saved}.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def set_webhook(channel: str) -> None:
    """Ask for a webhook URL, check it, save it to .env and send a test message."""
    key, what = WEBHOOK_SETTINGS[channel]
    print(f"Paste the Discord webhook URL for {what}, then press Enter:")
    url = input("> ").strip().strip("'\"")
    if not url.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
        sys.exit("That doesn't look like a Discord webhook URL (it should start with "
                 "https://discord.com/api/webhooks/). Nothing was changed.")
    try:
        _webhook(url, {"username": BOT_NAME, "content": f"✅ Connected. This channel now gets {what}."})
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Discord rejected that URL ({e}). Copy it again from the channel's webhook settings. "
                 "Nothing was changed.")
    set_env_value(HERE / ".env", key, url)
    print(f"Saved {key}. A ✅ test message was sent to that channel.")
    print("Now restart the bot so it uses it:  systemctl restart arbbot")


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a .env-style file ({} if it's missing)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        while value.startswith(key + "="):  # forgive "SPORTS=SPORTS=..." from pasting a whole line
            value = value[len(key) + 1:]
        out[key] = value
    return out


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, no override of real env vars."""
    for key, value in read_env_file(path).items():
        os.environ.setdefault(key, value)


# Settings Claude can push for you through the automatic updates (the bot can't be logged into
# from outside). They win over .env; put REMOTE_SETTINGS=off in .env to ignore them.
REMOTE_ENV = "remote.env"


def load_settings(here: Path | None = None) -> dict[str, str]:
    """Load remote.env (unless .env says REMOTE_SETTINGS=off), then .env. Real environment variables
    still win over both. Returns the settings remote.env supplied."""
    here = here or HERE
    off = (os.environ.get("REMOTE_SETTINGS") or read_env_file(here / ".env").get("REMOTE_SETTINGS", ""))
    used: dict[str, str] = {}
    if off.strip().lower() not in ("off", "0", "false", "no"):
        for key, value in read_env_file(here / REMOTE_ENV).items():
            if key not in os.environ:
                os.environ[key] = value
                used[key] = value
    load_dotenv(here / ".env")
    return used


REMOTE_USED: dict[str, str] = {}


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
    # Bet types the live check asks for ("" = the mode's choice: locks h2h, otherwise MARKETS). Each
    # costs a credit per check. With fewer than MARKETS, a sport with a live game gets two checks: the
    # live one (these bet types for every game, live and upcoming, every POLL_SECONDS) and a pre-game
    # one for the other bet types of upcoming games.
    live_markets: str = ""
    # That pre-game check runs once every this many live checks (1: with every live check, every
    # POLL_SECONDS, as when it came with the live check), unless the normal pre-game pace is faster. It
    # keeps this pace when live checks slow down to save credits (only the live check slows first), and
    # with a bigger number spare credits speed it up first (to every live check at most). 0 = the
    # normal pre-game pace (every 15 min near kickoff, hourly further out).
    pregame_with_live_every: int = 1
    # Up to 10 books cost the same as one region. Pinnacle is read only, as the sharp
    # reference for +EV; you never bet there.
    bookmakers: str = "pinnacle,draftkings,fanduel,betmgm,williamhill_us,kalshi,espnbet,betrivers,fanatics,hardrockbet"
    # Books you actually bet at. Alerts only ever tell you to bet at these; the rest of
    # BOOKMAKERS is used for reference (true odds, market consensus). Empty = all of them.
    my_books: str = ""
    kalshi_fee_rate: float = 0.07   # Kalshi's trading fee factor (fee = rate x P x (1-P) per $1 contract)
    us_state: str = ""              # two letters (e.g. nj); some books' links need it (sports.{state}.betmgm.com)
    ny_rules: bool = False          # New York: no NY sportsbook on games with a New York college team
    poll_seconds: int = 60        # fastest check rate for sports with live games
    pregame_minutes: int = 15     # check rate before kickoff (0 = live games only)
    pregame_hours: float = 2.0    # how far before kickoff pre-game checks start
    early_minutes: int = 60       # games later today/tomorrow (within 24h): main lines this often
    lookahead_hours: float = 48   # look this far ahead at all (0 = only near kickoff)
    far_minutes: int = 180        # games 24-48h out: main lines this often
    far_max_age_seconds: int = 10800  # early lines can sit unchanged for hours without being stale
    # On a tight budget, slow early checks up to this much first (then early props and 1-2 days out
    # up to this much again).
    extra_max_stretch: float = 4.0
    # On a tight budget, slow LIVE checks up to this much before anything else (0 = the mode's
    # choice: locks 3, otherwise 1, i.e. live checks are protected).
    live_max_stretch: float = 0
    # Spare credits (a day that fits with room left) buy faster pre-game checks, never faster live
    # ones, down to these floors (minutes; 0 = never faster than the normal rate).
    pregame_min_minutes: int = 3
    early_min_minutes: int = 15
    prop_early_min_minutes: int = 60
    prop_min_minutes: int = 15
    far_min_minutes: int = 60
    spare_use_pct: float = 85         # plan the faster pace to use at most this % of the day's allowance
    # How the month's credits are shared across days of the week (relative weights, local time).
    # Busy days (Sun NFL, Sat college, Mon/Thu night games) get more; quiet midweek days less.
    budget_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_BUDGET_WEIGHTS))
    monthly_credits: int = 100_000
    billing_day: int = 1          # day of month your plan's credits reset
    events_refresh_minutes: int = 10
    game_minutes: dict[str, int] = field(default_factory=dict)  # per-sport override
    min_profit_pct: float = 0.5
    min_profit_dollars: float = 0.0  # skip arbs that lock in less than this (at BANKROLL)
    max_profit_pct: float = 15.0  # above this it's almost always a stale/bad line
    # "locks": only strong alerts (bigger arbs, high-confidence +EV, a few per hour).
    # "balanced": the individual thresholds below as set. "all": everything that clears them.
    alert_mode: str = "locks"
    max_ev_per_hour: int = 0      # cap on new +EV alerts per hour, best first (0 = no cap)
    max_prop_per_hour: int = 0
    max_parlay_per_hour: int = 0
    max_arb_per_hour: int = 0     # arbs and prop arbs together
    max_outlier_per_hour: int = 0  # outliers and prop outliers together
    live_per_hour: int = 0        # every new live alert together: arbs, outliers, live +EV
    max_age_seconds: int = 120    # live games: ignore prices not updated this recently
    pregame_max_age_seconds: int = 900  # pre-game lines can sit unchanged for a while
    # The same three age limits for SHARP_BOOKS prices, the ones fair odds come from (None = the limit for
    # every book: MAX_AGE_SECONDS live, PREGAME_MAX_AGE_SECONDS, FAR_MAX_AGE_SECONDS beyond PREGAME_HOURS).
    sharp_max_age_seconds: int | None = None
    sharp_pregame_max_age_seconds: int | None = None
    sharp_far_max_age_seconds: int | None = None
    realert_jump_pct: float = 2.5  # send a fresh alert if the edge grows by this many points
    bankroll: float = 100.0
    round_stakes: float = 5       # round stakes to this many dollars (0 = exact cents)
    round_keep_pct: float = 85    # only use a rounding that keeps this much of the exact edge
    tax_rate: float = 0.0         # e.g. 0.33: arb cards add a rough after-tax profit line (0 = off)
    min_after_tax_pct: float = 0.0  # with TAX_RATE: skip arbs whose after-tax profit is under this % (0 = off)
    arb_live: bool = True         # alert on arbs in games already in progress
    min_live_profit_pct: float = 1.0  # live gaps are often one book lagging; ask for more
    live_arb_max_skew: int = 60   # live arb legs must be priced within this many seconds of each other
    live_confirm_checks: int = 1  # a new live alert pings once this many checks in a row find it (1 = off)
    live_max_age_alert: int = 0   # ...and only if the price to bet is at most this many seconds old (0 = off)
    live_sports: list[str] = field(default_factory=list)  # sports to check while live ([] = all)
    live_webhook_url: str = ""    # send live arbs to a separate Discord channel
    ev_webhook_url: str = ""      # +EV (incl. props) channel; outliers and parlays fall back to it
    parlay_webhook_url: str = ""
    props_webhook_url: str = ""   # player props' +EV and outlier cards ("" = where other +EV and outliers go)
    include_links: bool = True    # ask for bet-slip deep links where books support them
    include_sids: bool = True     # ...and each book's own ids, to build a bet-slip link where it sent none (no credits)
    discord_mention: str = ""     # e.g. @everyone or <@USER_ID> to force a phone ping
    arbs_enabled: bool = True     # look for arbs (main lines and props) at all; false = no arb alerts
    # Discord role ids by book (BOOK_ROLES=draftkings=123,fanduel=456): a new +EV, prop, outlier or parlay
    # card pings its book's role instead of EV_MENTION / OUTLIER_MENTION / PARLAY_MENTION. Empty = those.
    book_roles: dict = field(default_factory=dict)
    # One alert per bet: a bet that went out is never alerted again (not at a better price, not at another
    # book, not after a restart), and its card isn't edited except to mark it GONE.
    one_alert_per_bet: bool = False
    results_daily_only: bool = False   # results: one card per finished day (~12:30am), none as bets settle
    # Tuning from the bot's own record (TUNE_ENABLED), by book and by sport, main lines and props apart:
    # - a book whose price is still there on the next check under TUNE_SLOW_STILL_PCT% of the time (TUNE_SLOW_BETS+
    #   bets): its new bets need TUNE_SLOW_EXTRA_PCT more edge (you can't get there in time often enough);
    # - CLV clearly above 0 (TUNE_CLV_BETS+ bets, the 95% range above 0): stake x TUNE_CLV_BOOST; clearly below 0:
    #   x TUNE_CLV_CUT. A book and a sport both: multiplied, kept between the two.
    tune_enabled: bool = False
    # Sports dropped first when credits get tight (SHED_SPORTS=soccer: a prefix covers every league): when the next
    # 24h can't be checked at full speed, these aren't checked at all before anything else slows down. They come
    # back once everything fits in 90% of the day's credits again. Empty = off.
    shed_sports: str = ""
    # ✅ bet tracking (DISCORD_BOT_TOKEN, a Discord bot's token; set it on the server with --set, never in remote.env):
    # the bot puts a ✅ under each new bet card, reads who tapped it every REACTION_MINUTES until the game starts,
    # and the results card reports the bets members actually took.
    discord_bot_token: str = ""
    reaction_minutes: int = 10
    tune_slow_still_pct: float = 40.0
    tune_slow_bets: int = 30
    tune_slow_extra_pct: float = 2.0
    tune_clv_bets: int = 50
    tune_clv_boost: float = 1.25
    tune_clv_cut: float = 0.5
    # A new alert (or a much better price's re-alert) isn't posted on odds fetched longer ago than this by the
    # time it's ready (a slow pass, Discord making the bot wait): the next check looks again and sends it on its
    # fresh odds, only if it still qualifies. Seconds, before the game / live; 0 = no limit.
    send_max_delay_seconds: int = 600
    live_send_max_delay_seconds: int = 90
    discord_max_wait_seconds: float = 10   # a new alert's post told to wait longer (429): don't, try at the next check
                                           # (0 = wait); edits and status messages always wait
    # A +EV or outlier card already posted is edited at most this often when only other books' prices or its notes
    # changed (each edit waits out Discord's limits). Its own book, price or stake changing, a GONE mark, a much
    # better price (a new alert), a live bet, arbs and parlays: right away (0 = every change right away).
    card_edit_min_seconds: int = 0
    status_webhook_url: str = ""  # health messages; defaults to the alerts webhook
    results_webhook_url: str = ""  # graded bets (what hit); defaults to the health channel
    test_webhook_url: str = ""     # --test-discord's samples go here when set (with real book-role pings); else to
                                   # the channels real alerts use, without pings
    results_minutes: int = 30     # look for finished games to grade this often (0 = daily only)
    prop_grading: str = "espn"    # grade player props from ESPN box scores ("off" = check them yourself)
    # Main lines are graded from the Odds API's scores (2 credits per sport). "shadow" also asks
    # ESPN's free scoreboard and logs whether it agreed (SCORE_CHECK_FILE), to see if it can be
    # trusted later; "off" doesn't ask ESPN (except to spot rain-shortened MLB games, always).
    free_scores: str = "shadow"
    low_credits: int = 5000       # warn on Discord below this many credits
    # Outage messages in the health channel: one when a data source has been down this many minutes (what it
    # means for the alerts), one when it's back. The Odds API not answering for a sport; the sharp book
    # (SHARP_BOOKS) missing from every game of a sport other books price (while it is, props without its price
    # are at most medium confidence and Kalshi-only bets wait); Kalshi unreachable. 0 = that one off.
    health_alerts: bool = True
    odds_down_minutes: int = 10
    sharp_down_minutes: int = 15
    kalshi_down_minutes: int = 30
    log_file: str = "arbs.csv"    # every arb with how long it lasted ("" = off)
    state_dir: str = "state"      # open alerts survive restarts (no duplicate posts) ("" = off)
    summary_hour: int = 9         # local hour for the daily Discord summary (-1 = off)
    odds_format: str = "american"  # "american" (+152 / -154) or "decimal" (2.52)
    # +EV
    ev_enabled: bool = True
    bet_at_sharp: bool = False      # true only if you can actually bet at the sharp book
    sharp_books: str = "pinnacle"   # fair-odds references (blended if more than one)
    sharp_weights: dict[str, float] = field(default_factory=dict)  # e.g. pinnacle=0.6; unlisted: SOURCE_WEIGHTS
    sharp_weights_props: dict[str, float] | None = None   # the same for player props (None = SHARP_WEIGHTS)
    sharp_disagree_pct: float = 3.0  # skip a line if two sharps' fair odds differ by more (points)
    sharp_disagree_prop_pct: float = 4.0  # the same for player props
    # With 3+ fair-odds references on a line (sharp books, Kalshi), one more than this many points from
    # the middle of the others is left out (at most one; never the most trusted: then the line is skipped).
    outlier_source_pts: float = 3.0
    outlier_source_prop_pts: float = 4.0
    single_source_stake: float = 0.5  # stake multiplier when only 1 of several sharps priced it
    one_source_stake: float = 1.0     # ...and when the one sharp listed priced it alone (no Kalshi in it; 1 = full)
    consensus_stake: float = 0.7      # stake multiplier when no sharp priced it (props: median of 4+ books)
    kalshi_check: bool = True         # cross-check moneylines against Kalshi's own (free) exchange prices
    kalshi_max_gap: float = 3.0       # skip a +EV bet when Kalshi's win chance differs by more (points; see kalshi_gap_limit)
    kalshi_max_spread: float = 3.0    # trust a Kalshi price only if its bid-ask spread is at most this (cents; college +2)
    kalshi_min_size: float = 100.0    # ...and at least this many contracts sit at the best bid and ask
    kalshi_live: bool = False         # also use Kalshi once a game has started (its in-game prices lag)
    # Kalshi's own price goes into a pre-game moneyline's fair odds, next to the sharp book's (weight:
    # SHARP_WEIGHTS' kalshi, else 0.15), when both teams have a usable quote. Only the edge, the stake and
    # the card use it: every check (Kalshi's gap, confirmed bets, the sharp's history, CLV) uses the sharp's.
    kalshi_blend: bool = True
    # A pre-game moneyline no sharp book lists at all (while it lists other games in that sport) can be
    # priced by Kalshi alone: the other sportsbooks must roughly agree, it's at most medium confidence
    # (never in locks mode) and the stake is cut to KALSHI_ONLY_STAKE.
    kalshi_only: bool = True
    kalshi_only_stake: float = 0.5
    # A pre-game moneyline, spread or total under MIN_EV_PCT still goes out down to this edge (college
    # +1) when a second sharp source agrees with the sharp book's fair odds: Kalshi within
    # KALSHI_CONFIRM_PTS (moneylines), or the consensus of 4+ other sportsbooks within
    # CONSENSUS_CONFIRM_PTS (spreads, totals). It must start within CONFIDENT_HOURS, be high confidence
    # and pass every other check. None = the mode's choice (locks 3.5, otherwise off); 0 = off.
    pregame_confirmed_ev_pct: float | None = None
    kalshi_confirm_pts: float = 1.5
    consensus_confirm_pts: float = 1.5
    devig_method: str = "power"     # "power" (handles long-shot bias) or "multiplicative"
    # Bet-quality checks on +EV
    max_sharp_hold_pct: float = 8.0       # skip if the sharp's own margin is wider than this
    sharp_consensus_max_gap: float = 10.0  # skip if sharp and the other books' median differ by more (points)
    min_confidence: str = "low"           # low / medium / high: alert only at or above this
    confident_hours: float = 24           # main lines this close to kickoff can be high confidence
    sport_min_ev: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SPORT_MIN_EV))
    move_window_minutes: int = 60         # how far back to look for sharp line movement
    confidence_stakes: str = "1,0.75,0.5"  # stake multiplier for high, medium, low
    unit_size: float = 0            # dollars per unit (1u) on bet cards; 0 = EV_BANKROLL / 100 (100u = the bankroll)
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
    markout_file: str = "markouts.csv"   # every alert's price checked again a few minutes later ("" = off)
    score_check_file: str = "score_checks.csv"   # FREE_SCORES=shadow: ESPN's finals vs the Odds API's
    # Every +EV or outlier bet that came within a point of its bar, and what happened to it: alerted (with how
    # long the post took), held back (caps, live rules, old odds), or which check stopped it ("" = off).
    candidate_log_file: str = "candidates.csv"
    # Once a day, candidates.csv, arbs.csv and markouts.csv drop rows older than this many days (0 = keep all).
    # The bet logs (ev_bets, outliers, parlays, results, closing lines) and score_checks.csv are never cut.
    log_keep_days: int = 60
    # Player props (fetched per game, so they're budgeted separately and checked less often)
    props_enabled: bool = True
    prop_sports: list[str] = field(default_factory=lambda: [
        "americanfootball_nfl", "basketball_nba", "icehockey_nhl"])
    prop_markets: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PROP_MARKETS))
    # ...plus these near kickoff (PROP_HOURS), in the same request: sport -> markets ("" = none)
    prop_near_markets: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PROP_NEAR_MARKETS))
    # ...paid for from spare credits only (never slowing a live or main-line check); false: like any near-kickoff
    # prop check (a short day then slows live and main-line checks a little to pay for them)
    prop_near_spare_only: bool = True
    prop_minutes: int = 30          # how often to check a game's props
    prop_hours: float = 3.0         # check every PROP_MINUTES this close to kickoff
    prop_early_hours: float = 24.0  # and every PROP_EARLY_MINUTES from this far out
    prop_early_minutes: int = 240
    # At most this many games' props per pass, the most overdue first (closing-line checks before any); the rest
    # stay due and go in the next pass, a second later. Spreads a slate starting together over a few passes, so
    # the Odds API isn't asked for dozens at once and the next live check isn't held up (0 = no limit).
    prop_max_per_pass: int = 0
    prop_min_ev_pct: float = 7.0    # prop prices are noisier, so ask for more edge
    # "over": prop Unders aren't alerted (+EV, outliers, so parlays too; prop arbs still need both sides),
    # "both": Overs and Unders. "" = the mode's choice: locks over (the blueprint), otherwise both.
    prop_sides: str = ""
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
    outlier_live_min_pct: float = 0  # live outliers need this edge (0 = OUTLIER_MIN_PCT)
    outlier_min_books: int = 3      # need at least this many other books to compare against
    outlier_prop_max_pct: float = 40.0  # a prop price further off the other books than this is bad data, left out
                                        # of every prop check (0 = no cap)
    outlier_prop_cluster: int = 4   # one book off on this many players of one prop type in a game (and on at least
                                    # half the players it lists there): its whole market there is left out (0 = off)
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

        def number(v: str, typ, bad: str):
            try:
                f = float(v)
            except ValueError:
                raise ValueError(bad) from None
            if not math.isfinite(f) or (typ is int and not f.is_integer()):
                raise ValueError(bad + (" (a whole number)" if typ is int else ""))
            return int(f) if typ is int else f

        def num(key: str, default, typ):
            """A number setting; empty means the default."""
            v = (e(key) or "").strip()
            return number(v, typ, f"{key}={v} should be a number") if v else default

        def pairs(key: str, typ) -> dict:
            """A name=value list (BUDGET_WEIGHTS=sat=1.6,sun=1.6). Commas or semicolons."""
            out = {}
            for x in (e(key) or "").replace(";", ",").split(","):
                if not x.strip():
                    continue
                k, sep, v = x.partition("=")
                bad = f"{key}: '{x.strip()}' should look like name=number"
                if not sep or not k.strip():
                    raise ValueError(bad)
                out[k.strip()] = number(v.strip(), typ, bad)
            return out
        cfg = cls(
            api_key=e("ODDS_API_KEY", ""),
            webhook_url=e("DISCORD_WEBHOOK_URL", ""),
            sports=_csv(e("SPORTS", "")) or d.sports,
            regions=e("REGIONS", d.regions),
            markets=e("MARKETS", d.markets),
            live_markets=(e("LIVE_MARKETS") or "").strip(),
            pregame_with_live_every=num("PREGAME_WITH_LIVE_EVERY", d.pregame_with_live_every, int),
            bookmakers=e("BOOKMAKERS", d.bookmakers),
            my_books=e("MY_BOOKS", ""),
            kalshi_fee_rate=num("KALSHI_FEE_RATE", d.kalshi_fee_rate, float),
            us_state=e("US_STATE", "").strip().lower(),
            ny_rules=(e("NY_RULES") or ("true" if e("US_STATE", "").strip().lower() == "ny" else "false")
                      ).strip().lower() in ("1", "true", "yes"),
            poll_seconds=num("POLL_SECONDS", d.poll_seconds, int),
            pregame_minutes=num("PREGAME_MINUTES", d.pregame_minutes, int),
            pregame_hours=num("PREGAME_HOURS", d.pregame_hours, float),
            early_minutes=num("EARLY_MINUTES", d.early_minutes, int),
            lookahead_hours=num("LOOKAHEAD_HOURS", d.lookahead_hours, float),
            far_minutes=num("FAR_MINUTES", d.far_minutes, int),
            far_max_age_seconds=num("FAR_MAX_AGE_SECONDS", d.far_max_age_seconds, int),
            extra_max_stretch=num("EXTRA_MAX_STRETCH", d.extra_max_stretch, float),
            live_max_stretch=num("LIVE_MAX_STRETCH", d.live_max_stretch, float),
            # (A floor left unset never asks for more than the normal rate: EARLY_MINUTES=10 is fine.)
            pregame_min_minutes=num("PREGAME_MIN_MINUTES", min(d.pregame_min_minutes, num(
                "PREGAME_MINUTES", d.pregame_minutes, int)), int),
            early_min_minutes=num("EARLY_MIN_MINUTES", min(d.early_min_minutes, num(
                "EARLY_MINUTES", d.early_minutes, int)), int),
            prop_early_min_minutes=num("PROP_EARLY_MIN_MINUTES", min(d.prop_early_min_minutes, num(
                "PROP_EARLY_MINUTES", d.prop_early_minutes, int)), int),
            prop_min_minutes=num("PROP_NEAR_MIN_MINUTES", min(d.prop_min_minutes, num(
                "PROP_MINUTES", d.prop_minutes, int)), int),
            far_min_minutes=num("FAR_MIN_MINUTES", min(d.far_min_minutes, num("FAR_MINUTES", d.far_minutes, int)), int),
            spare_use_pct=num("SPARE_USE_PCT", d.spare_use_pct, float),
            budget_weights={**d.budget_weights,
                            **{k.lower()[:3]: v for k, v in pairs("BUDGET_WEIGHTS", float).items()}},
            monthly_credits=num("MONTHLY_CREDITS", d.monthly_credits, int),
            billing_day=num("BILLING_DAY", d.billing_day, int),
            events_refresh_minutes=num("EVENTS_REFRESH_MINUTES", d.events_refresh_minutes, int),
            game_minutes=pairs("GAME_MINUTES", int),
            min_profit_pct=num("MIN_PROFIT_PCT", d.min_profit_pct, float),
            alert_mode=e("ALERT_MODE", d.alert_mode).strip().lower(),
            max_ev_per_hour=num("MAX_EV_PER_HOUR", d.max_ev_per_hour, int),
            max_prop_per_hour=num("MAX_PROP_PER_HOUR", d.max_prop_per_hour, int),
            max_parlay_per_hour=num("MAX_PARLAY_PER_HOUR", d.max_parlay_per_hour, int),
            max_arb_per_hour=num("MAX_ARB_PER_HOUR", d.max_arb_per_hour, int),
            max_outlier_per_hour=num("MAX_OUTLIER_PER_HOUR", d.max_outlier_per_hour, int),
            live_per_hour=num("LIVE_PER_HOUR", d.live_per_hour, int),
            min_profit_dollars=num("MIN_PROFIT_DOLLARS", d.min_profit_dollars, float),
            max_profit_pct=num("MAX_PROFIT_PCT", d.max_profit_pct, float),
            max_age_seconds=num("MAX_AGE_SECONDS", d.max_age_seconds, int),
            pregame_max_age_seconds=num("PREGAME_MAX_AGE_SECONDS", d.pregame_max_age_seconds, int),
            sharp_max_age_seconds=num("SHARP_MAX_AGE_SECONDS", None, int),
            sharp_pregame_max_age_seconds=num("SHARP_PREGAME_MAX_AGE_SECONDS", None, int),
            sharp_far_max_age_seconds=num("SHARP_FAR_MAX_AGE_SECONDS", None, int),
            realert_jump_pct=num("REALERT_JUMP_PCT", d.realert_jump_pct, float),
            bankroll=num("BANKROLL", d.bankroll, float),
            round_stakes=num("ROUND_STAKES", d.round_stakes, float),
            round_keep_pct=num("ROUND_KEEP_PCT", d.round_keep_pct, float),
            tax_rate=num("TAX_RATE", d.tax_rate, float),
            min_after_tax_pct=num("MIN_AFTER_TAX_PCT", d.min_after_tax_pct, float),
            arb_live=e("ARB_LIVE", "true").lower() in ("1", "true", "yes"),
            arbs_enabled=e("ARBS_ENABLED", "true").lower() in ("1", "true", "yes"),
            book_roles=parse_book_roles(e("BOOK_ROLES", "")),
            one_alert_per_bet=e("ONE_ALERT_PER_BET", "false").lower() in ("1", "true", "yes"),
            results_daily_only=e("RESULTS_DAILY_ONLY", "false").lower() in ("1", "true", "yes"),
            tune_enabled=e("TUNE_ENABLED", "false").lower() in ("1", "true", "yes"),
            shed_sports=e("SHED_SPORTS", "").strip().lower(),
            discord_bot_token=e("DISCORD_BOT_TOKEN", "").strip(),
            reaction_minutes=num("REACTION_MINUTES", d.reaction_minutes, int),
            tune_slow_still_pct=num("TUNE_SLOW_STILL_PCT", d.tune_slow_still_pct, float),
            tune_slow_bets=num("TUNE_SLOW_BETS", d.tune_slow_bets, int),
            tune_slow_extra_pct=num("TUNE_SLOW_EXTRA_PCT", d.tune_slow_extra_pct, float),
            tune_clv_bets=num("TUNE_CLV_BETS", d.tune_clv_bets, int),
            tune_clv_boost=num("TUNE_CLV_BOOST", d.tune_clv_boost, float),
            tune_clv_cut=num("TUNE_CLV_CUT", d.tune_clv_cut, float),
            min_live_profit_pct=num("MIN_LIVE_PROFIT_PCT", d.min_live_profit_pct, float),
            live_arb_max_skew=num("LIVE_ARB_MAX_SKEW", d.live_arb_max_skew, int),
            live_confirm_checks=num("LIVE_CONFIRM_CHECKS", d.live_confirm_checks, int),
            live_max_age_alert=num("LIVE_MAX_AGE_ALERT", d.live_max_age_alert, int),
            live_sports=_csv(e("LIVE_SPORTS", "")),
            live_webhook_url=e("DISCORD_LIVE_WEBHOOK_URL", ""),
            ev_webhook_url=e("DISCORD_EV_WEBHOOK_URL", ""),
            parlay_webhook_url=e("DISCORD_PARLAY_WEBHOOK_URL", ""),
            props_webhook_url=e("DISCORD_PROPS_WEBHOOK_URL", ""),
            include_links=e("INCLUDE_LINKS", "true").lower() in ("1", "true", "yes"),
            include_sids=(e("INCLUDE_SIDS") or "true").strip().lower() in ("1", "true", "yes"),
            discord_mention=e("DISCORD_MENTION", ""),
            send_max_delay_seconds=num("SEND_MAX_DELAY_SECONDS", d.send_max_delay_seconds, int),
            live_send_max_delay_seconds=num("LIVE_SEND_MAX_DELAY_SECONDS", d.live_send_max_delay_seconds, int),
            discord_max_wait_seconds=num("DISCORD_MAX_WAIT_SECONDS", d.discord_max_wait_seconds, float),
            card_edit_min_seconds=num("CARD_EDIT_MIN_SECONDS", d.card_edit_min_seconds, int),
            status_webhook_url=e("DISCORD_STATUS_WEBHOOK_URL", ""),
            results_webhook_url=e("DISCORD_RESULTS_WEBHOOK_URL", ""),
            test_webhook_url=e("DISCORD_TEST_WEBHOOK_URL", ""),
            results_minutes=num("RESULTS_MINUTES", d.results_minutes, int),
            prop_grading=(e("PROP_GRADING") or d.prop_grading).strip().lower(),
            free_scores=(e("FREE_SCORES") or d.free_scores).strip().lower(),
            low_credits=num("LOW_CREDITS", d.low_credits, int),
            health_alerts=(e("HEALTH_ALERTS") or "true").strip().lower() in ("1", "true", "yes"),
            odds_down_minutes=num("ODDS_DOWN_MINUTES", d.odds_down_minutes, int),
            sharp_down_minutes=num("SHARP_DOWN_MINUTES", d.sharp_down_minutes, int),
            kalshi_down_minutes=num("KALSHI_DOWN_MINUTES", d.kalshi_down_minutes, int),
            log_file=e("LOG_FILE", d.log_file),
            state_dir=e("STATE_DIR", d.state_dir),
            summary_hour=num("SUMMARY_HOUR", d.summary_hour, int),
            odds_format=e("ODDS_FORMAT", d.odds_format).strip().lower(),
            ev_enabled=e("EV_ENABLED", "true").lower() in ("1", "true", "yes"),
            sharp_books=e("SHARP_BOOKS", d.sharp_books),
            sharp_weights=pairs("SHARP_WEIGHTS", float),
            sharp_weights_props=pairs("SHARP_WEIGHTS_PROPS", float) if (e("SHARP_WEIGHTS_PROPS") or "").strip() else None,
            sharp_disagree_pct=num("SHARP_DISAGREE_PCT", d.sharp_disagree_pct, float),
            sharp_disagree_prop_pct=num("SHARP_DISAGREE_PROP_PCT", d.sharp_disagree_prop_pct, float),
            outlier_source_pts=num("OUTLIER_SOURCE_PTS", d.outlier_source_pts, float),
            outlier_source_prop_pts=num("OUTLIER_SOURCE_PROP_PTS", d.outlier_source_prop_pts, float),
            single_source_stake=num("SINGLE_SOURCE_STAKE", d.single_source_stake, float),
            one_source_stake=num("ONE_SOURCE_STAKE", d.one_source_stake, float),
            consensus_stake=num("CONSENSUS_STAKE", d.consensus_stake, float),
            kalshi_check=e("KALSHI_CHECK", "true").lower() in ("1", "true", "yes"),
            kalshi_max_gap=num("KALSHI_MAX_GAP", d.kalshi_max_gap, float),
            kalshi_max_spread=num("KALSHI_MAX_SPREAD", d.kalshi_max_spread, float),
            kalshi_min_size=num("KALSHI_MIN_SIZE", d.kalshi_min_size, float),
            kalshi_live=e("KALSHI_LIVE", "false").lower() in ("1", "true", "yes"),
            kalshi_blend=(e("KALSHI_BLEND") or "true").strip().lower() in ("1", "true", "yes"),
            kalshi_only=(e("KALSHI_ONLY") or "true").strip().lower() in ("1", "true", "yes"),
            kalshi_only_stake=num("KALSHI_ONLY_STAKE", d.kalshi_only_stake, float),
            pregame_confirmed_ev_pct=num("PREGAME_CONFIRMED_EV_PCT", None, float),
            kalshi_confirm_pts=num("KALSHI_CONFIRM_PTS", d.kalshi_confirm_pts, float),
            consensus_confirm_pts=num("CONSENSUS_CONFIRM_PTS", d.consensus_confirm_pts, float),
            devig_method=e("DEVIG_METHOD", d.devig_method).strip().lower(),
            max_sharp_hold_pct=num("MAX_SHARP_HOLD_PCT", d.max_sharp_hold_pct, float),
            sharp_consensus_max_gap=num("SHARP_CONSENSUS_MAX_GAP", d.sharp_consensus_max_gap, float),
            min_confidence=e("MIN_CONFIDENCE", d.min_confidence).strip().lower(),
            confident_hours=num("CONFIDENT_HOURS", d.confident_hours, float),
            sport_min_ev={**d.sport_min_ev, **pairs("SPORT_MIN_EV", float)},
            move_window_minutes=num("MOVE_WINDOW_MINUTES", d.move_window_minutes, int),
            confidence_stakes=(e("CONFIDENCE_STAKES") or d.confidence_stakes).replace(";", ","),
            unit_size=num("UNIT_SIZE", d.unit_size, float),
            bet_at_sharp=e("BET_AT_SHARP", "false").lower() in ("1", "true", "yes"),
            ev_books=e("EV_BOOKS", ""),
            min_ev_pct=num("MIN_EV_PCT", d.min_ev_pct, float),
            max_ev_pct=num("MAX_EV_PCT", d.max_ev_pct, float),
            ev_max_odds=num("EV_MAX_ODDS", d.ev_max_odds, float),
            ev_live=e("EV_LIVE", "false").lower() in ("1", "true", "yes"),
            ev_bankroll=num("EV_BANKROLL", d.ev_bankroll, float),
            kelly_fraction=num("KELLY_FRACTION", d.kelly_fraction, float),
            ev_max_stake_pct=num("EV_MAX_STAKE_PCT", d.ev_max_stake_pct, float),
            ev_mention=e("EV_MENTION", ""),
            ev_log_file=e("EV_LOG_FILE", d.ev_log_file),
            ev_results_file=e("EV_RESULTS_FILE", d.ev_results_file),
            closing_file=e("CLOSING_FILE", d.closing_file),
            markout_file=e("MARKOUT_FILE", d.markout_file),
            score_check_file=e("SCORE_CHECK_FILE", d.score_check_file),
            candidate_log_file=e("CANDIDATE_LOG_FILE", d.candidate_log_file),
            log_keep_days=num("LOG_KEEP_DAYS", d.log_keep_days, int),
            props_enabled=e("PROPS_ENABLED", "true").lower() in ("1", "true", "yes"),
            prop_sports=_csv(e("PROP_SPORTS", "")) or d.prop_sports,
            prop_markets={**d.prop_markets, **{k.strip(): v.strip().replace("|", ",") for k, v in
                          (x.split("=", 1) for x in e("PROP_MARKETS", "").split(";") if "=" in x)}},
            prop_near_markets={**d.prop_near_markets, **{k.strip(): v.strip().replace("|", ",") for k, v in
                               (x.split("=", 1) for x in e("PROP_NEAR_MARKETS", "").split(";") if "=" in x)}},
            prop_near_spare_only=(e("PROP_NEAR_SPARE_ONLY") or "true").strip().lower() in ("1", "true", "yes"),
            prop_minutes=num("PROP_MINUTES", d.prop_minutes, int),
            prop_hours=num("PROP_HOURS", d.prop_hours, float),
            prop_early_hours=num("PROP_EARLY_HOURS", d.prop_early_hours, float),
            prop_early_minutes=num("PROP_EARLY_MINUTES", d.prop_early_minutes, int),
            prop_max_per_pass=num("PROP_MAX_PER_PASS", d.prop_max_per_pass, int),
            prop_min_ev_pct=num("PROP_MIN_EV_PCT", d.prop_min_ev_pct, float),
            prop_sides=(e("PROP_SIDES") or "").strip().lower(),
            prop_min_books=num("PROP_MIN_BOOKS", d.prop_min_books, int),
            parlays_enabled=e("PARLAYS_ENABLED", "true").lower() in ("1", "true", "yes"),
            parlay_max_legs=num("PARLAY_MAX_LEGS", d.parlay_max_legs, int),
            parlay_min_ev_pct=num("PARLAY_MIN_EV_PCT", d.parlay_min_ev_pct, float),
            parlay_leg_min_ev_pct=num("PARLAY_LEG_MIN_EV_PCT", d.parlay_leg_min_ev_pct, float),
            parlay_max_alerts=num("PARLAY_MAX_ALERTS", d.parlay_max_alerts, int),
            parlay_max_stake_pct=num("PARLAY_MAX_STAKE_PCT", d.parlay_max_stake_pct, float),
            parlay_mention=e("PARLAY_MENTION", ""),
            parlay_log_file=e("PARLAY_LOG_FILE", d.parlay_log_file),
            closing_minutes=num("CLOSING_MINUTES", d.closing_minutes, int),
            outliers_enabled=e("OUTLIERS_ENABLED", "true").lower() in ("1", "true", "yes"),
            outlier_min_pct=num("OUTLIER_MIN_PCT", d.outlier_min_pct, float),
            outlier_live_min_pct=num("OUTLIER_LIVE_MIN_PCT", d.outlier_live_min_pct, float),
            outlier_min_books=num("OUTLIER_MIN_BOOKS", d.outlier_min_books, int),
            outlier_prop_max_pct=num("OUTLIER_PROP_MAX_PCT", d.outlier_prop_max_pct, float),
            outlier_prop_cluster=num("OUTLIER_PROP_CLUSTER", d.outlier_prop_cluster, int),
            outlier_live=e("OUTLIER_LIVE", "true").lower() in ("1", "true", "yes"),
            outlier_mention=e("OUTLIER_MENTION", ""),
            outlier_webhook_url=e("DISCORD_OUTLIER_WEBHOOK_URL", ""),
            outlier_log_file=e("OUTLIER_LOG_FILE", d.outlier_log_file),
            live_only=e("LIVE_ONLY", "false").lower() in ("1", "true", "yes"),
            active_hours=e("ACTIVE_HOURS", "").strip(),
            timezone=e("TIMEZONE", d.timezone),
        )
        cfg.check()
        return cfg.with_mode()

    def check(self) -> list[str]:
        """Catch settings that would otherwise crash the bot later (raises ValueError naming one).
        Returns the warnings: settings that load but probably aren't what you meant (see warnings)."""
        for key, v in (("POLL_SECONDS", self.poll_seconds),
                       ("PROP_MINUTES", self.prop_minutes if self.props_enabled else 1)):
            if v < 1:
                raise ValueError(f"{key}={v} should be at least 1")
        if not 0 < self.tune_clv_cut <= 1 <= self.tune_clv_boost:
            raise ValueError(f"TUNE_CLV_CUT={self.tune_clv_cut:g} should be over 0 and at most 1, "
                             f"and TUNE_CLV_BOOST={self.tune_clv_boost:g} at least 1")
        if self.tune_slow_extra_pct < 0 or self.tune_slow_bets < 1 or self.tune_clv_bets < 2:
            raise ValueError("TUNE_SLOW_EXTRA_PCT can't be negative, TUNE_SLOW_BETS should be at least 1 "
                             "and TUNE_CLV_BETS at least 2")
        try:
            ZoneInfo(self.timezone)
        except Exception:  # noqa: BLE001 - unknown or malformed names raise several types
            raise ValueError(f"TIMEZONE={self.timezone} isn't a time zone name (like America/New_York)") from None
        if self.active_hours:
            try:
                start_s, end_s = self.active_hours.split("-")
                dtime.fromisoformat(start_s.strip()), dtime.fromisoformat(end_s.strip())
            except ValueError:
                raise ValueError(f"ACTIVE_HOURS={self.active_hours} should look like 08:00-22:00") from None
        try:
            stakes = [float(x) for x in _csv(self.confidence_stakes)]
        except ValueError:
            stakes = []
        if len(stakes) != 3 or not all(math.isfinite(x) and x >= 0 for x in stakes):
            raise ValueError(f"CONFIDENCE_STAKES={self.confidence_stakes} should be 3 numbers, like 1,0.75,0.5")
        if self.live_markets:
            extra = set(_csv(self.live_markets)) - set(_csv(self.markets))
            if extra or not _csv(self.live_markets):
                raise ValueError(f"LIVE_MARKETS={self.live_markets} should only list bet types that are in MARKETS "
                                 f"({self.markets})")
        if self.pregame_with_live_every < 0:
            raise ValueError(f"PREGAME_WITH_LIVE_EVERY={self.pregame_with_live_every} should be 0 (the normal "
                             f"pre-game pace) or a number of live checks, like 2")
        if self.live_max_stretch and self.live_max_stretch < 1:
            raise ValueError(f"LIVE_MAX_STRETCH={self.live_max_stretch:g} should be 0 (the mode's choice) or at least 1")
        for key, floor, base_key, base in (
                ("PREGAME_MIN_MINUTES", self.pregame_min_minutes, "PREGAME_MINUTES", self.pregame_minutes),
                ("EARLY_MIN_MINUTES", self.early_min_minutes, "EARLY_MINUTES", self.early_minutes),
                ("PROP_EARLY_MIN_MINUTES", self.prop_early_min_minutes, "PROP_EARLY_MINUTES", self.prop_early_minutes),
                ("PROP_NEAR_MIN_MINUTES", self.prop_min_minutes, "PROP_MINUTES", self.prop_minutes),
                ("FAR_MIN_MINUTES", self.far_min_minutes, "FAR_MINUTES", self.far_minutes)):
            # A negative or tiny floor would check those games on every pass and burn the plan.
            if floor and not 1 <= floor <= base:
                raise ValueError(f"{key}={floor} should be 0 (off) or between 1 and {base_key} ({base})")
        if not 0 <= self.spare_use_pct <= 95:
            raise ValueError(f"SPARE_USE_PCT={self.spare_use_pct:g} should be between 0 (off) and 95")
        if self.prop_sides not in ("", "over", "both"):
            raise ValueError(f"PROP_SIDES={self.prop_sides} should be over or both (empty = the mode's choice: "
                             f"over in locks mode, both otherwise)")
        if self.free_scores not in ("shadow", "off"):
            raise ValueError(f"FREE_SCORES={self.free_scores} should be shadow or off")
        if not 0 <= self.tax_rate < 1:
            raise ValueError(f"TAX_RATE={self.tax_rate:g} should be a fraction like 0.33 (0 = off)")
        if (self.pregame_confirmed_ev_pct or 0) < 0:
            raise ValueError(f"PREGAME_CONFIRMED_EV_PCT={self.pregame_confirmed_ev_pct:g} should be 0 (off) or an "
                             f"edge like 3.5 (empty = the mode's choice)")
        for key, v in (("KALSHI_CONFIRM_PTS", self.kalshi_confirm_pts),
                       ("CONSENSUS_CONFIRM_PTS", self.consensus_confirm_pts)):
            if v < 0:
                raise ValueError(f"{key}={v:g} should be 0 or more (points of win chance, like 1.5)")
        for key, v, same in (("SHARP_MAX_AGE_SECONDS", self.sharp_max_age_seconds, "MAX_AGE_SECONDS"),
                             ("SHARP_PREGAME_MAX_AGE_SECONDS", self.sharp_pregame_max_age_seconds,
                              "PREGAME_MAX_AGE_SECONDS"),
                             ("SHARP_FAR_MAX_AGE_SECONDS", self.sharp_far_max_age_seconds, "FAR_MAX_AGE_SECONDS")):
            if v is not None and v < 0:
                raise ValueError(f"{key}={v} should be empty (the same as {same}) or a number of seconds, like 300")
        if self.log_keep_days < 0:
            raise ValueError(f"LOG_KEEP_DAYS={self.log_keep_days} should be 0 (keep everything) or a number of days, "
                             f"like 60")
        if self.prop_max_per_pass < 0:
            raise ValueError(f"PROP_MAX_PER_PASS={self.prop_max_per_pass} should be 0 (no limit) or a number of games, "
                             f"like 8")
        bar = max(self.outlier_min_pct, LOCKS["outlier_pct"] if self.alert_mode == "locks" else 0)
        if self.outlier_prop_max_pct < 0 or 0 < self.outlier_prop_max_pct <= bar:
            raise ValueError(f"OUTLIER_PROP_MAX_PCT={self.outlier_prop_max_pct:g} should be 0 (no cap) or an edge in % "
                             f"above the outlier bar ({bar:g}), like 40")
        if self.outlier_prop_cluster < 0 or self.outlier_prop_cluster == 1:
            raise ValueError(f"OUTLIER_PROP_CLUSTER={self.outlier_prop_cluster} should be 0 (off) or a number of "
                             f"players, 2 or more, like 4")
        for key, v in (("ODDS_DOWN_MINUTES", self.odds_down_minutes), ("SHARP_DOWN_MINUTES", self.sharp_down_minutes),
                       ("KALSHI_DOWN_MINUTES", self.kalshi_down_minutes)):
            if v < 0:
                raise ValueError(f"{key}={v} should be 0 (off) or a number of minutes, like 15")
        for key, v in (("SEND_MAX_DELAY_SECONDS", self.send_max_delay_seconds),
                       ("LIVE_SEND_MAX_DELAY_SECONDS", self.live_send_max_delay_seconds),
                       ("DISCORD_MAX_WAIT_SECONDS", self.discord_max_wait_seconds)):
            if v < 0:
                raise ValueError(f"{key}={v:g} should be 0 (no limit) or a number of seconds, like 60")
        if self.card_edit_min_seconds < 0:
            raise ValueError(f"CARD_EDIT_MIN_SECONDS={self.card_edit_min_seconds} should be 0 (every change right "
                             f"away) or a number of seconds, like 600")
        for key, v in (("ONE_SOURCE_STAKE", self.one_source_stake), ("KALSHI_ONLY_STAKE", self.kalshi_only_stake)):
            if v < 0:
                raise ValueError(f"{key}={v:g} should be a fraction of the normal stake, like 0.5 (1 = the full stake)")
        for key, weights in (("SHARP_WEIGHTS", self.sharp_weights), ("SHARP_WEIGHTS_PROPS", self.sharp_weights_props or {})):
            if bad := [k for k, w in weights.items() if w < 0]:
                raise ValueError(f"{key}: {bad[0]}={weights[bad[0]]:g} should be 0 (not used) or more")
        return self.warnings()

    def warnings(self) -> list[str]:
        """Settings that load but probably aren't what you meant, in plain English (the bot says
        them at startup, in the console and the health channel)."""
        out = []
        if self.unit_size > 0 and not math.isclose(self.unit_size * 100, self.ev_bankroll):
            unit = _plain(self.unit_size)
            out.append(f"UNIT_SIZE={unit} but EV_BANKROLL={_plain(self.ev_bankroll)}: cards say 1u = "
                       f"{money(self.unit_size)}, but stakes are still worked out from a {money(self.ev_bankroll)} "
                       f"bankroll, not 100 units ({money(self.unit_size * 100)}). To make every bet scale with "
                       f"your unit, run: python arbbot.py --set UNIT_SIZE={unit} (it sets "
                       f"EV_BANKROLL={_plain(self.unit_size * 100)} too)")
        for key, weights, prop in (("SHARP_WEIGHTS", self.sharp_weights, False),
                                   ("SHARP_WEIGHTS_PROPS", self.sharp_weights_props or {}, True)):
            pin = source_weight(self, "pinnacle", prop)
            if over := [k for k, w in weights.items() if k != "pinnacle" and w > pin]:
                out.append(f"{key} gives {', '.join(f'{k} ({_plain(weights[k])})' for k in over)} more weight than "
                           f"Pinnacle ({_plain(pin)}): fair odds will lean on {'it' if len(over) == 1 else 'them'} "
                           f"more than on Pinnacle, the sharpest book (the blueprint trusts Pinnacle most), and with "
                           f"3+ sources Pinnacle can be the one left out as the odd one out (the heaviest never is)")
        return out

    def unit(self) -> float:
        """Dollars per unit (1u) on bet cards: UNIT_SIZE, or 1% of EV_BANKROLL when that's 0 (100 units
        = the bankroll). It only labels the stake: stakes are worked out in dollars from EV_BANKROLL."""
        return self.unit_size if self.unit_size > 0 else self.ev_bankroll / 100

    def with_mode(self) -> "Config":
        """Apply ALERT_MODE. "locks" raises every bar to at least the levels below (your own
        stricter settings still win) and caps how many alerts go out per hour, live ones most.
        PREGAME_CONFIRMED_EV_PCT: in locks yours wins only when stricter, i.e. a higher edge, or 0
        (off); left empty it's 3.5. Other modes: off unless you set it. PROP_SIDES left empty: over in
        locks (prop Unders aren't alerted), both otherwise."""
        confirmed = self.pregame_confirmed_ev_pct
        if self.alert_mode != "locks":
            return replace(self, pregame_confirmed_ev_pct=0.0 if confirmed is None else confirmed,
                           prop_sides=self.prop_sides or "both")
        L = LOCKS
        conf = max(self.min_confidence, L["min_confidence"], key=lambda c: CONFIDENCE_ORDER.get(c, 0))
        cap = lambda mine, lock: min(x for x in (mine, lock) if x) if (mine or lock) else 0
        return replace(
            self,
            min_profit_pct=max(self.min_profit_pct, L["arb_pct"]),
            min_live_profit_pct=max(self.min_live_profit_pct, L["live_arb_pct"]),
            min_profit_dollars=max(self.min_profit_dollars, L["arb_dollars"]),
            min_ev_pct=max(self.min_ev_pct, L["ev_pct"]),
            prop_min_ev_pct=max(self.prop_min_ev_pct, L["prop_ev_pct"]),
            outlier_min_pct=max(self.outlier_min_pct, L["outlier_pct"]),
            outlier_live_min_pct=max(self.outlier_live_min_pct, L["outlier_live_pct"]),
            parlay_min_ev_pct=max(self.parlay_min_ev_pct, L["parlay_ev_pct"]),
            parlay_max_legs=min(self.parlay_max_legs, L["parlay_legs"]),
            min_confidence=conf,
            max_ev_per_hour=cap(self.max_ev_per_hour, L["ev_per_hour"]),
            max_prop_per_hour=cap(self.max_prop_per_hour, L["prop_per_hour"]),
            max_parlay_per_hour=cap(self.max_parlay_per_hour, L["parlay_per_hour"]),
            max_arb_per_hour=cap(self.max_arb_per_hour, L["arb_per_hour"]),
            max_outlier_per_hour=cap(self.max_outlier_per_hour, L["outlier_per_hour"]),
            live_per_hour=cap(self.live_per_hour, L["live_per_hour"]),
            live_confirm_checks=max(self.live_confirm_checks, L["live_confirm"]),
            live_max_age_alert=cap(self.live_max_age_alert, L["live_max_age"]),
            # (Your own LIVE_MARKETS / LIVE_MAX_STRETCH win; no moneylines in MARKETS: no split.)
            live_markets=self.live_markets or (L["live_markets"] if set(_csv(L["live_markets"])) <= set(
                _csv(self.markets)) else ""),
            live_max_stretch=self.live_max_stretch or L["live_max_stretch"],
            pregame_confirmed_ev_pct=(L["pregame_confirmed_ev_pct"] if confirmed is None
                                      else confirmed and max(confirmed, L["pregame_confirmed_ev_pct"])),
            kalshi_confirm_pts=min(self.kalshi_confirm_pts, L["kalshi_confirm_pts"]),
            consensus_confirm_pts=min(self.consensus_confirm_pts, L["consensus_confirm_pts"]),
            prop_sides=self.prop_sides or L["prop_sides"],
        )

    def bad_webhooks(self) -> list[str]:
        """Clear any webhook setting that isn't a URL (e.g. a leftover placeholder) and name it,
        so those alerts fall back to the main channel instead of failing silently."""
        bad = []
        for attr, env in (("ev_webhook_url", "DISCORD_EV_WEBHOOK_URL"),
                          ("outlier_webhook_url", "DISCORD_OUTLIER_WEBHOOK_URL"),
                          ("parlay_webhook_url", "DISCORD_PARLAY_WEBHOOK_URL"),
                          ("props_webhook_url", "DISCORD_PROPS_WEBHOOK_URL"),
                          ("live_webhook_url", "DISCORD_LIVE_WEBHOOK_URL"),
                          ("status_webhook_url", "DISCORD_STATUS_WEBHOOK_URL"),
                          ("results_webhook_url", "DISCORD_RESULTS_WEBHOOK_URL"),
                          ("test_webhook_url", "DISCORD_TEST_WEBHOOK_URL")):
            value = getattr(self, attr)
            if value and not value.startswith(("https://", "http://")):
                bad.append(env)
                setattr(self, attr, "")
        return bad

    def bettable(self, book_key: str, ev: dict | None = None) -> bool:
        """Can alerts tell you to bet at this book (on this game, when given)?"""
        mine = _csv(self.my_books)
        if mine and book_key not in mine:
            return False
        return not (ev is not None and self.ny_rules and book_key in NY_BOOKS and ny_college_game(ev))

    def counts(self, book_title: str) -> bool:
        """Does a logged bet at this book count in your results? Only your books (MY_BOOKS) do:
        an alert at a book you can't bet isn't a bet you could have made."""
        mine = _csv(self.my_books)
        if not mine or not book_title:
            return True
        t = _norm(book_title)
        return any(t in (_norm(k), _norm(BOOK_TITLES.get(k, ""))) for k in mine)

    def ev_allowed(self) -> set[str]:
        """Books +EV-style alerts (and parlays) may use: EV_BOOKS, but never outside MY_BOOKS
        (an old EV_BOOKS list must not send you to a book you don't have). Empty = no limit."""
        ev, mine = set(_csv(self.ev_books)), set(_csv(self.my_books))
        if ev and mine:
            return (ev & mine) or {"(none of EV_BOOKS is in MY_BOOKS)"}
        return ev or mine

    def credits_per_call(self, markets: str | None = None) -> int:
        """The Odds API charges markets x regions; every 10 bookmakers count as one region."""
        return len(_csv(self.markets if markets is None else markets)) * self.regions_billed()

    def regions_billed(self) -> int:
        return math.ceil(len(_csv(self.bookmakers)) / 10) if self.bookmakers else len(_csv(self.regions))

    def split_markets(self) -> tuple[str, str] | None:
        """(live check's bet types, the pre-game check's) when LIVE_MARKETS is narrower than
        MARKETS, else None (one check per sport asks for everything, live and upcoming)."""
        live, every = _csv(self.live_markets), _csv(self.markets)
        if not live or not set(live) < set(every):   # (compared as sets: the order doesn't matter)
            return None
        return ",".join(live), ",".join(m for m in every if m not in live)

    def confirmed_floor(self, sport_key: str) -> float:
        """The smallest edge a confirmed pre-game bet in this sport may have: PREGAME_CONFIRMED_EV_PCT,
        college games CONFIRM_COLLEGE_EXTRA more (0 = off)."""
        pct = self.pregame_confirmed_ev_pct or 0.0
        return pct + (CONFIRM_COLLEGE_EXTRA if "ncaa" in sport_key else 0.0) if pct > 0 else 0.0

    def prop_request(self, sport: str, near: bool = False) -> str:
        """The prop types one game's request asks for: PROP_MARKETS, plus PROP_NEAR_MARKETS when the game
        is within PROP_HOURS of kickoff (near), each once."""
        base = _csv(self.prop_markets.get(sport, "").replace("|", ","))
        extra = _csv(self.prop_near_markets.get(sport, "").replace("|", ",")) if near else []
        return ",".join(base + [m for m in dict.fromkeys(extra) if m not in base])

    def prop_near_extra(self, sport: str) -> list[str]:
        """The prop types only a game near kickoff asks for (PROP_NEAR_MARKETS not in PROP_MARKETS)."""
        plain = _csv(self.prop_request(sport))
        return [m for m in _csv(self.prop_request(sport, near=True)) if m not in plain]

    def prop_types(self, sport: str) -> list[str]:
        """The prop stats a sport's alerts can be on, each once: PROP_MARKETS and PROP_NEAR_MARKETS, an
        alternate-lines market as its main one (it's graded the same way)."""
        return list(dict.fromkeys(base_market(m) for m in _csv(self.prop_request(sport, near=True))))

    def alerts_side(self, line, outcome: str) -> bool:
        """May this side be alerted? Not a prop Under with PROP_SIDES=over (its prices still go into the
        fair odds: the Over's no-vig price needs both sides)."""
        return not (self.prop_sides == "over" and outcome == "Under" and is_prop(line))

    def prop_credits_per_call(self, sport: str, near: bool = False) -> int:
        return len(_csv(self.prop_request(sport, near))) * self.regions_billed()

    def for_props(self) -> "Config":
        """The same settings with the prop thresholds swapped in (prop markets carry more margin)."""
        return replace(self, min_ev_pct=self.prop_min_ev_pct, consensus_min_books=self.prop_min_books,
                       max_sharp_hold_pct=self.max_sharp_hold_pct + 4)

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
    updated: datetime | None = None  # when the book last updated this price
    edge: float | None = None        # % better (+) or worse (-) than Pinnacle's fair odds (None: no fair price)
    room: float = 0.0                # a Kalshi bet bigger than Kalshi's order book holds at this price: the
                                     # dollars there (0 = enough, or not known); the card says so
    alt: bool = False                # the price is on the book's alternate line at this point, not its main one


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
    age: float | None = None   # seconds since the oldest bet price was updated, when first sent (None = unknown)
    fair_from: str = ""        # whose fair odds the legs' edges are from ("Pinnacle"; "" = none priced it)
    keep_stake: float = 0.0    # the first leg is a good bet alone: what to stake on it by itself (0 = it isn't)
    ages: str = ""             # how old each price was, the sharp book's too: "DraftKings 12s; FanDuel 40s; Pinnacle 3s"
    first: dict = field(default_factory=dict)   # snapshot() as first sent (arbs.csv describes that alert)
    found: tuple = ()          # live, once the live checks confirm it: (first check that found it, checks in a row)
    tax: tuple = ()            # TAX_RATE set: (the rate, rough profit per $100 after tax) for the card

    @property
    def tagged(self) -> bool:
        """Pinnacle prices the line, so legs[0] is the price that will move (bet it first)."""
        return bool(self.legs) and self.legs[0].edge is not None

    @property
    def account_safe(self) -> bool:
        """The price that will move is on Kalshi and every sportsbook bet is at or worse than fair."""
        return (self.tagged and self.legs[0].book.lower().startswith("kalshi")
                and all(l.edge <= 0 for l in self.legs[1:]))

    def snapshot(self) -> dict:
        """What arbs.csv says about the alert itself: the price that would move and by how much,
        every leg's edge against fair in card order (the other legs' cost), and how old each price
        was, Pinnacle's included. Two possible rules, "the other bets must be near fair" and "live:
        Pinnacle's price must be fresh too", are only measured from these for now, never applied."""
        t = self.tagged
        return {"stale_book": self.legs[0].book if t else "",
                "stale_edge_pct": round(self.legs[0].edge, 2) if t else "",
                "leg_edges": "; ".join(f"{l.book} {l.edge:+.1f}%" for l in self.legs) if t else "",
                "fair_from": self.fair_from, "leg_ages": self.ages}

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
        """Lowest price leg i can drop to and still lock in keep_pct profit, betting the printed
        stakes (the other legs unchanged). If leg i wins it must pay back the whole total."""
        leg = self.legs[i]
        if leg.stake <= 0 or self.total_stake <= 0:  # stakes not set yet: assume a re-split
            room = 1 / (1 + keep_pct / 100) - sum(1 / l.price for j, l in enumerate(self.legs) if j != i)
            return 1 / room if room > 0 else None
        worst = self.total_stake * (1 + keep_pct / 100) / leg.stake
        if worst >= leg.price:
            # This leg's printed stake returns less than keep_pct over the total (rounding, or a
            # thin arb): any drop costs money, so the line is break-even.
            worst = self.total_stake / leg.stake
            if worst >= leg.price:
                return None
        # Rounded the safe way to a price the card can show, never past the posted price.
        return min(shown_at_or_above(worst), leg.price)

    def after_tax_pct(self, rate: float) -> float:
        """Rough profit per $100 staked after tax, whichever bet wins (the worst case), on the simple
        model: the winning bet's winnings are taxed at `rate`, and the losing stakes are deducted at 90%
        (you itemize). A rough guide, not tax advice."""
        total = self.total_stake
        if total <= 0:
            return 0.0
        worst = math.inf
        for l in self.legs:
            won, lost = l.stake * (l.price - 1), total - l.stake
            worst = min(worst, won - lost - rate * (won - 0.9 * lost))
        return worst / total * 100

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


PROP_COST_HOURS = (6, 12, 24)   # prop calls' costs are averaged apart by hours to start: up to 6h, 12h, 24h


def prop_cost_kind(sport: str, hours: float, near_hours: float) -> str:
    """The CostBook kind of a prop call for a game this many hours away: "near" (PROP_HOURS),
    then one bucket per PROP_COST_HOURS (books post more props as kickoff gets closer)."""
    if hours <= near_hours:
        return f"props:{sport}:near"
    edge = next((h for h in PROP_COST_HOURS if hours <= h), None)
    return f"props:{sport}:{edge}h" if edge else f"props:{sport}:later"


class CostBook:
    """What each kind of Odds API call really costs (the x-requests-last header), as a running
    average, so the budget forecasts real spending instead of markets x regions. Player props only
    bill the markets a book actually returned (an empty answer is free), so props far from kickoff
    usually cost less than the formula says. Thread-safe: odds and props are fetched in parallel."""
    PRIOR = 3      # the formula counts as this many calls until real ones outweigh it
    ALPHA = 0.1    # after the first 10 calls, recent calls weigh more

    def __init__(self, path: Path | None = None):
        self.path = path
        self.stats: dict[str, list[float]] = {}   # "kind@formula" -> [calls, average, credits]
        self.today: dict[str, float] = {}         # kind -> credits since the last daily summary
        # Since start: what this process's calls cost by the header, or by the formula when a call
        # didn't say (and how much of it was by the formula).
        self.expected = self.unmeasured = 0.0
        self.lock = threading.Lock()
        self.dirty = False
        if path and path.exists():
            try:
                saved = json.loads(path.read_text())
                self.stats = {str(k): [float(x) for x in v][:3] for k, v in saved.items() if len(v) >= 3}
            except (OSError, ValueError, TypeError, AttributeError):
                self.stats = {}   # unreadable: start again from the formula

    def add(self, kind: str, cost: float | None, formula: float) -> None:
        """One call's cost. A missing or nonsense header (negative, or over 10x the formula) is
        ignored, so the formula stands. Averages are kept per formula: changing MARKETS or
        PROP_MARKETS starts that kind's average again."""
        if cost is None or not math.isfinite(cost) or cost < 0 or (formula and cost > 10 * formula):
            with self.lock:
                self.expected += formula
                self.unmeasured += formula
            return
        with self.lock:
            self.expected += cost
            n, avg, total = self.stats.get(f"{kind}@{formula:g}", [0, 0.0, 0.0])
            n += 1
            avg += (cost - avg) * max(self.ALPHA, 1 / n)
            self.stats[f"{kind}@{formula:g}"] = [n, avg, total + cost]
            self.today[kind] = self.today.get(kind, 0.0) + cost
            self.dirty = True

    def estimate(self, kind: str, formula: float) -> float:
        """Credits a call of this kind will cost: the formula at first, the measured average once
        there are calls (the formula weighs as PRIOR calls, so a few can't swing it)."""
        n, avg, _ = self.stats.get(f"{kind}@{formula:g}", [0, 0.0, 0.0])
        return (n * avg + self.PRIOR * formula) / (n + self.PRIOR)

    def calls(self, kind: str, formula: float) -> int:
        return int(self.stats.get(f"{kind}@{formula:g}", [0])[0])

    def take_today(self) -> dict[str, float]:
        """Credits by kind since the last call (for the daily summary), then start again."""
        with self.lock:
            out, self.today = self.today, {}
        return out

    def save(self) -> None:
        if not (self.path and self.dirty):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with self.lock:
                tmp.write_text(json.dumps(self.stats))
                self.dirty = False
            tmp.replace(self.path)
        except OSError as e:
            print(f"  ! Couldn't save call costs: {e}", file=sys.stderr)


def read_api_error(e: urllib.error.HTTPError) -> None:
    """Note on an Odds API error what it said: e.odds_error_code (e.g. EXCEEDED_FREQ_LIMIT, OUT_OF_USAGE_CREDITS,
    INVALID_MARKET; "" if it didn't say) and e.odds_message. Its body can only be read once."""
    if hasattr(e, "odds_error_code"):
        return
    try:
        raw = e.read() or b""
    except Exception:  # noqa: BLE001 - no body: nothing said
        raw = b""
    try:   # (asked with "Accept-Encoding: gzip", an error's answer may come zipped like any other)
        if raw and (e.headers or {}).get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    except Exception:  # noqa: BLE001 - not really zipped: read it as it is
        pass
    body = raw.decode("utf-8", "replace")
    try:
        said = json.loads(body)
    except ValueError:
        said = None
    said = said if isinstance(said, dict) else {}
    e.odds_error_code = str(said.get("error_code") or "")
    e.odds_message = str(said.get("message") or body)[:500]


class OddsAPI:
    RATE = 10.0        # Odds API calls per second at most, on average (it allows 30; jitter near that gets 429s)
    BURST = 20         # ...and this many at once (so never more than 30 in any one second). A check of up to 20
    #                    calls never waits; a bigger one (a restart's first check) waits (calls - 20) / 10 seconds
    FREQ_RETRIES = 2   # "slow down" (429 EXCEEDED_FREQ_LIMIT): asked again after 2 and 4 seconds, then given up

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.remaining: float | None = None
        self.used: float | None = None
        self._warned_events_cost = False
        self.costs = CostBook(data_path(cfg.state_dir) / "call_costs.json" if cfg.state_dir else None)
        self._pace_lock = threading.Lock()
        self._tokens, self._token_at = float(self.BURST), time.monotonic()

    def _pace(self) -> None:
        """Wait for a turn: at most BURST calls at once, RATE per second on average (all threads together)."""
        with self._pace_lock:
            now = time.monotonic()
            self._tokens = min(float(self.BURST), self._tokens + (now - self._token_at) * self.RATE)
            self._token_at = now
            self._tokens -= 1
            wait = -self._tokens / self.RATE if self._tokens < 0 else 0.0
        if wait > 0:
            time.sleep(wait)

    def _request(self, path: str, params: dict) -> tuple[list | dict, float | None]:
        """(the JSON, what this one call cost in credits if the API said). A "slow down" (429 that isn't out of
        credits) is asked again FREQ_RETRIES times, 2 and 4 seconds later (a refused call costs nothing)."""
        for attempt in range(self.FREQ_RETRIES + 1):
            try:
                return self._request_once(path, params)
            except urllib.error.HTTPError as e:
                read_api_error(e)
                if e.code != 429 or e.odds_error_code == "OUT_OF_USAGE_CREDITS" or attempt == self.FREQ_RETRIES:
                    raise
                time.sleep(2.0 * (attempt + 1))
        raise AssertionError("unreachable")

    def _request_once(self, path: str, params: dict) -> tuple[list | dict, float | None]:
        self._pace()
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
            cost = resp.headers.get("x-requests-last")
            try:
                cost = float(cost) if cost is not None else None
            except ValueError:
                cost = None
            return json.loads(raw), cost

    def events(self, sport: str, horizon_hours: float = 26) -> list[dict]:
        """Live + upcoming games for a sport. Free: doesn't use credits."""
        used_before = self.used
        data, cost = self._request(f"/sports/{sport}/events", {
            "dateFormat": "iso",
            "commenceTimeTo": _iso(datetime.now(timezone.utc) + timedelta(hours=horizon_hours)),
        })
        if cost is None and used_before is not None and self.used is not None:
            cost = self.used - used_before   # no per-call header: the running total (another process can bump it)
        if cost and cost > 0 and not self._warned_events_cost:
            self._warned_events_cost = True
            print("! The events endpoint used credits. Raise EVENTS_REFRESH_MINUTES.", file=sys.stderr)
        return data

    def event_odds(self, sport: str, event_id: str, markets: str) -> dict:
        """One game's odds for the given markets (used for player props). Costs the markets
        returned x regions (nothing when no book has any yet)."""
        params = {"markets": markets, "oddsFormat": "decimal", "dateFormat": "iso"}
        if self.cfg.bookmakers:
            params["bookmakers"] = self.cfg.bookmakers
        else:
            params["regions"] = self.cfg.regions
        if self.cfg.include_links:
            params["includeLinks"] = "true"
        if self.cfg.include_sids:
            params["includeSids"] = "true"
        data, cost = self._request(f"/sports/{sport}/events/{event_id}/odds", params)
        try:   # (an empty answer still says when the game starts)
            hours = (_parse_time(data["commence_time"]) - datetime.now(timezone.utc)).total_seconds() / 3600
        except (KeyError, TypeError, ValueError, AttributeError):
            hours = 0.0
        self.costs.add(prop_cost_kind(sport, hours, self.cfg.prop_hours), cost,
                       len(_csv(markets)) * self.cfg.regions_billed())
        return data

    def scores(self, sport: str, days_from: int = 3) -> list[dict]:
        """Final scores for recent games. Costs 2 credits."""
        data, cost = self._request(f"/sports/{sport}/scores", {"daysFrom": days_from, "dateFormat": "iso"})
        self.costs.add("scores", cost, 2 if days_from else 1)
        return data

    def odds(self, sport: str, until: datetime, markets: str | None = None, since: datetime | None = None,
             kind: str = "odds") -> list[dict]:
        """Odds for every game starting before `until` (and all live ones; with `since`, only games
        starting after it). Costs markets x regions, however many games come back."""
        markets = markets or self.cfg.markets
        params = {
            "markets": markets,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
            "commenceTimeTo": _iso(until),  # trims the payload; same credit cost
        }
        if since is not None:
            params["commenceTimeFrom"] = _iso(since)
        if self.cfg.bookmakers:
            params["bookmakers"] = self.cfg.bookmakers
        else:
            params["regions"] = self.cfg.regions
        if self.cfg.include_links:
            params["includeLinks"] = "true"
        if self.cfg.include_sids:
            params["includeSids"] = "true"
        data, cost = self._request(f"/sports/{sport}/odds", params)
        self.costs.add(kind, cost, self.cfg.credits_per_call(markets))
        return data


# --------------------------------------------------------------------------- detection

def is_alt(key: str) -> bool:
    """An alternate-lines market: player_pass_yds_alternate (or alternate_spreads/alternate_totals)."""
    return key.endswith("_alternate") or key.startswith("alternate_")


def base_market(key: str) -> str:
    """The main market an alternate-lines market belongs to: player_pass_yds_alternate -> player_pass_yds,
    alternate_spreads -> spreads (any other key: itself). A bet on an alternate line is the same bet as
    the main market's at the same point: labels, grading, CLV and alerts use the main market's key."""
    if key.endswith("_alternate"):
        return key[:-len("_alternate")]
    return key[len("alternate_"):] if key.startswith("alternate_") else key


def book_offers(bm: dict, ev: dict, now: datetime, is_live: bool,
                cfg: Config) -> list[tuple[dict, dict, tuple, bool, str | None]]:
    """One book's prices to bet in one game, one per line and side: (market, outcome, (market, line),
    alternate?, stamp). The line's market is the main one: the book's alternate line at exactly the same
    point is the same bet, and the better price of the two is its offer, with that market's link (the
    main market's on a tie). Fair odds never come from alternate lines (the references read main markets
    only): they're priced off the book's own main line, with more margin. stamp: when the book last
    changed the price (None for a prop: one stamp for every player's line). Fresh markets only."""
    best: dict[tuple, tuple[dict, dict, tuple, bool, str | None]] = {}
    start = _parse_time(ev["commence_time"])
    for mkt in sorted(bm.get("markets", []), key=lambda m: is_alt(m["key"])):   # main markets first
        if not is_fresh(mkt, bm, now, is_live, cfg, start):
            continue
        base, alt = base_market(mkt["key"]), is_alt(mkt["key"])
        stamp = (mkt.get("last_update") or bm.get("last_update")) if _one_line(mkt, ev["home_team"]) else None
        for oc in mkt.get("outcomes", []):
            price = float(oc.get("price") or 0)
            if price <= 1.0:
                continue
            k = (base, _line_for(base, oc, ev["home_team"]))
            have = best.get((k, oc["name"]))
            if have is None or price > float(have[1]["price"]):
                best[(k, oc["name"])] = (mkt, oc, k, alt, stamp)
    return list(best.values())


def is_fresh(mkt: dict, bm: dict, now: datetime, live: bool, cfg: Config,
             start: datetime | None = None, sharp: bool = False) -> bool:
    """Live prices must be recent; pre-game lines legitimately sit still for longer, and lines
    for games a day or two out can go hours without moving. (The stamp is when The Odds API last saw
    the market, so an old one means a market taken down, not a price that just didn't change.)
    sharp: a SHARP_BOOKS price for fair odds, held to the SHARP_* age limits where they're set."""
    updated = mkt.get("last_update") or bm.get("last_update")
    if not updated:
        return True
    if live:
        limit, mine = cfg.max_age_seconds, cfg.sharp_max_age_seconds
    elif start is not None and start - now > timedelta(hours=cfg.pregame_hours):
        limit, mine = cfg.far_max_age_seconds, cfg.sharp_far_max_age_seconds
    else:
        limit, mine = cfg.pregame_max_age_seconds, cfg.sharp_pregame_max_age_seconds
    if sharp and mine is not None:
        limit = mine
    return (now - _parse_time(updated)).total_seconds() <= limit


def _fill_links(obj: dict, state: str) -> None:
    """Some books' links are per-state templates (sports.{state}.betmgm.com). Fill in US_STATE,
    or drop the link if it isn't set (a broken link is worse than none)."""
    link = obj.get("link")
    if link and "{state}" in link:
        obj["link"] = link.replace("{state}", state) if state else ""


def _draftkings_link(ev: dict, bm: dict, mkt: dict, oc: dict) -> str:
    """DraftKings' bet slip from its own ids (INCLUDE_SIDS): the game's id (the book's sid) and the bet's."""
    game, bet = bm.get("sid"), oc.get("sid")
    if not (game and bet):
        return ""
    q = functools.partial(urllib.parse.quote, safe="")
    return f"https://sportsbook.draftkings.com/event/{q(str(game))}?outcomes={q(str(bet))}"


def _fanduel_link(ev: dict, bm: dict, mkt: dict, oc: dict) -> str:
    """FanDuel's bet slip from its own ids (INCLUDE_SIDS): the market's id and the bet's (selection)."""
    market, bet = mkt.get("sid"), oc.get("sid")
    if not (market and bet):
        return ""
    q = functools.partial(urllib.parse.quote, safe="")
    return f"https://sportsbook.fanduel.com/addToBetslip?marketId={q(str(market))}&selectionId={q(str(bet))}"


def _feed_link_only(ev: dict, bm: dict, mkt: dict, oc: dict) -> str:
    """No link of its own: the feed's (BetMGM's per-state template filled in with US_STATE)."""
    return ""


# Each book's own link logic, one replaceable function per book: given a price the feed had no bet-slip link for
# (none, or only the game's page), build one from the book's ids, or "" (the feed's link stands).
LINK_ADAPTERS = {"draftkings": _draftkings_link, "fanduel": _fanduel_link,
                 "betmgm": _feed_link_only, "williamhill_us": _feed_link_only}
LINK_LEVELS = {"outcome": "bet slip", "market": "market page", "event": "game page", "ids": "built from ids",
               "": "no link"}


def link_level(ev: dict, bm: dict, mkt: dict, oc: dict, cfg: Config) -> tuple[str, str]:
    """(the link for this price, where it's from): the feed's link for the bet itself ("outcome": opens it in
    the slip), else the market's ("market"), else the game's page ("event"), with a per-state template filled
    in from US_STATE (dropped without it). When that's only the game's page or nothing, the book's
    LINK_ADAPTERS entry may build the bet-slip link from its own ids ("ids"). "" = no link at all."""
    link, level = "", ""
    for obj, where in ((oc, "outcome"), (mkt, "market"), (bm, "event")):
        got = obj.get("link") or ""
        if "{state}" in got:
            got = got.replace("{state}", cfg.us_state) if cfg.us_state else ""
        if got:
            link, level = got, where
            break
    if level in ("", "event") and (build := LINK_ADAPTERS.get(bm.get("key"))):
        if built := build(ev, bm, mkt, oc):
            return built, "ids"
    return link, level


def book_link(ev: dict, bm: dict, mkt: dict, oc: dict, cfg: Config) -> str:
    """The link a card sends you to for this price (see link_level)."""
    return link_level(ev, bm, mkt, oc, cfg)[0]


def link_levels(events: list[dict], cfg: Config) -> dict[str, dict[str, int]]:
    """For --once: for each book alerts can send you to, how many of its prices' links are the bet in the slip,
    the market's, the game's page, built from ids or none ({book name: {level: count}})."""
    out: dict[str, dict[str, int]] = {}
    sharp = set() if cfg.bet_at_sharp else set(_csv(cfg.sharp_books))
    for ev in events:
        for bm in ev.get("bookmakers", []):
            if bm["key"] in sharp or not cfg.bettable(bm["key"]):
                continue
            mine = out.setdefault(bm.get("title", bm["key"]), {})
            for mkt in bm.get("markets", []):
                for oc in mkt.get("outcomes", []):
                    level = link_level(ev, bm, mkt, oc, cfg)[1]
                    mine[level] = mine.get(level, 0) + 1
    return out


def links_line(events: list[dict], cfg: Config) -> str:
    """'Bet links per price: DraftKings 210 bet slip, 6 built from ids; FanDuel 220 bet slip' ("" with none)."""
    parts = [f"{book} " + ", ".join(f"{n} {LINK_LEVELS[lv]}" for lv, n in sorted(levels.items(),
                                                                                key=lambda x: list(LINK_LEVELS).index(x[0])))
             for book, levels in sorted(link_levels(events, cfg).items()) if levels]
    return "Bet links per price: " + "; ".join(parts) if parts else ""


def apply_fees(events: list[dict], cfg: Config) -> list[dict]:
    """Prepare fresh feed data: fill in per-state links, and lower exchange prices by their
    trading fee so every comparison uses what you'd really get.

    Kalshi: a contract costs P and pays $1; the fee is rate x P x (1 - P) on top, so the real
    decimal odds are 1 / (P + fee)."""
    for ev in events:
        for bm in ev.get("bookmakers", []):
            _fill_links(bm, cfg.us_state)
            for mkt in bm.get("markets", []):
                _fill_links(mkt, cfg.us_state)
                for oc in mkt.get("outcomes", []):
                    _fill_links(oc, cfg.us_state)
    rate = cfg.kalshi_fee_rate
    if rate <= 0:
        return events
    for ev in events:
        for bm in ev.get("bookmakers", []):
            if bm["key"] != "kalshi" or bm.get("_fees_applied"):
                continue
            for mkt in bm.get("markets", []):
                for oc in mkt.get("outcomes", []):
                    price = float(oc.get("price") or 0)
                    if price > 1.0:
                        p = 1 / price
                        oc["price"] = round(1 / (p + rate * p * (1 - p)), 4)
            bm["_fees_applied"] = True
            bm["title"] = bm.get("title", "Kalshi")
    return events


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


def _one_line(mkt: dict, home_team: str) -> bool:
    """The market holds a single line (a moneyline, the main spread or total), so its time stamp is
    when that price last changed. A player-prop market holds every player's line under one stamp,
    which says nothing about any one of them."""
    return len({_line_for(mkt["key"], oc, home_team) for oc in mkt.get("outcomes", [])}) == 1


def ny_college_game(ev: dict) -> bool:
    """A college game with a New York school in it (New York's sportsbooks can't take it)."""
    if "ncaa" not in (ev.get("sport_key") or ""):
        return False
    return _ny_school(ev.get("home_team", "")) or _ny_school(ev.get("away_team", ""))


@functools.lru_cache(maxsize=4096)
def _ny_school(team: str) -> bool:
    """"Buffalo Bulls" and "St. John's Red Storm" are New York schools; "Buffalo State" and
    "Albany St" aren't (the same check Kalshi team names use: COLLEGE_QUALIFIERS)."""
    w = ["st" if x == "saint" else x for x in _words(team)]
    for school in NY_SCHOOLS:
        s = school.split()
        if w[:len(s)] == s and (len(w) == len(s) or w[len(s)] not in COLLEGE_QUALIFIERS):
            return True
    return False


def mark_will_move(arb: Arb, probs: dict[str, float], fair_from: str, cfg: Config) -> None:
    """With Pinnacle's fair odds on every side, put the leg that beats them most first: that price is
    off, so it's the one that will move. Bet it first; if the other price is gone, it's still a good
    bet alone when its own edge clears the +EV minimum (keep_stake: the +EV stake for it)."""
    if not arb.legs or set(probs) != {l.outcome for l in arb.legs}:
        return   # no fair price for every side: today's order and wording
    for l in arb.legs:
        l.edge = (probs[l.outcome] * l.price - 1) * 100
    arb.legs.sort(key=lambda l: (-round(l.edge, 1), l.outcome))
    arb.fair_from = fair_from
    top = arb.legs[0]
    floor = max(cfg.prop_min_ev_pct if is_prop(arb.line) else cfg.min_ev_pct,
                cfg.sport_min_ev.get(arb.sport_key, 0))
    if top.edge >= floor:
        arb.keep_stake = kelly_stake(probs[top.outcome], top.price, cfg)


def price_ages(arb: Arb, ev: dict, cfg: Config, now: datetime) -> str:
    """"DraftKings 12s; FanDuel 40s; Pinnacle 3s": how old each price to bet was (card order),
    then each sharp book's price on the same market. "?" = not known (a player prop: the book's one
    time stamp covers every player's line)."""
    def age(t: datetime) -> str:
        return _fmt_secs(max(0.0, (now - t).total_seconds()))
    parts = [f"{l.book} {age(l.updated) if l.updated else '?'}" for l in arb.legs]
    sharp = _csv(cfg.sharp_books)
    for bm in ev.get("bookmakers", []):
        if bm["key"] in sharp:
            mkt = next((m for m in bm.get("markets", []) if m["key"] == arb.market), None)
            ts = mkt and (mkt.get("last_update") or bm.get("last_update"))
            if ts:
                parts.append(f"{bm.get('title', bm['key'])} "
                             f"{age(_parse_time(ts)) if _one_line(mkt, ev['home_team']) else '?'}")
    return "; ".join(parts)


def find_arbs(events: list[dict], cfg: Config, now: datetime | None = None) -> list[Arb]:
    now = now or datetime.now(timezone.utc)
    arbs: list[Arb] = []
    sharp_only = set() if cfg.bet_at_sharp else set(_csv(cfg.sharp_books))

    for ev in events:
        start = _parse_time(ev["commence_time"])
        is_live = start <= now
        if (cfg.live_only and not is_live) or (is_live and not cfg.arb_live):
            continue

        fair = None   # Pinnacle's no-vig prices for this game, worked out once if it has an arb
        # (market, line) -> outcome name -> best Leg
        best: dict[tuple, dict[str, Leg]] = {}
        # (market, line) -> most outcomes any single book offers (2 or 3-way)
        n_outcomes: dict[tuple, int] = {}

        for bm in ev.get("bookmakers", []):
            if bm["key"] in sharp_only or not cfg.bettable(bm["key"], ev):
                continue  # reference-only book (e.g. Pinnacle), or one you can't bet at
            per_line: dict[tuple, int] = {}
            # Fresh prices only (a stale one has likely moved already); an alternate line at the same point
            # as the main one is the same bet (book_offers: the better price).
            for mkt, oc, k, alt, ts in book_offers(bm, ev, now, is_live, cfg):
                price = float(oc["price"])
                per_line[k] = per_line.get(k, 0) + 1
                slot = best.setdefault(k, {})
                cur = slot.get(oc["name"])
                title = bm.get("title", bm["key"])
                if cur is None or price > cur.price or (price == cur.price and title < cur.book):
                    link = book_link(ev, bm, mkt, oc, cfg)
                    slot[oc["name"]] = Leg(oc["name"], price, title, link=link,
                                           updated=_parse_time(ts) if ts else None, alt=alt)
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
            if all(l.updated for l in legs):
                arb.age = max(0.0, max((now - l.updated).total_seconds() for l in legs))
            if is_live and cfg.live_arb_max_skew:
                stamps = [l.updated for l in arb.legs if l.updated]
                if len(stamps) == len(arb.legs) and \
                        (max(stamps) - min(stamps)).total_seconds() > cfg.live_arb_max_skew:
                    continue  # one side's price is older: usually a lagging line, not a real arb
            min_pct = cfg.min_live_profit_pct if is_live else cfg.min_profit_pct
            if min_pct <= arb.exact_pct <= cfg.max_profit_pct:
                arb.set_stakes(cfg.bankroll, cfg.round_stakes, cfg.round_keep_pct)
                if arb.profit_pct >= min_pct and arb.guaranteed_profit >= cfg.min_profit_dollars:
                    if cfg.tax_rate > 0:   # the card's rough after-tax line (and MIN_AFTER_TAX_PCT, when set)
                        arb.tax = (cfg.tax_rate, arb.after_tax_pct(cfg.tax_rate))
                        if cfg.min_after_tax_pct and arb.tax[1] < cfg.min_after_tax_pct:
                            continue
                    if fair is None:
                        fair, sharp_name = sharp_fair(ev, cfg, now, is_live)[:2]
                    mark_will_move(arb, fair.get((market, line), {}), sharp_name.get((market, line), ""), cfg)
                    arb.ages = price_ages(arb, ev, cfg, now)
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
    # NFL
    "player_pass_completions": "Pass Completions", "player_pass_attempts": "Pass Attempts",
    "player_pass_interceptions": "Interceptions Thrown", "player_rush_attempts": "Rush Attempts",
    "player_rush_reception_yds": "Rush + Rec Yards", "player_anytime_td": "Anytime TD",
    "player_rush_tds": "Rushing TDs", "player_reception_tds": "Receiving TDs",
    "player_rush_longest": "Longest Rush", "player_reception_longest": "Longest Reception",
    "player_pass_rush_yds": "Pass + Rush Yards", "player_pass_rush_reception_yds": "Pass + Rush + Rec Yards",
    "player_pass_rush_reception_tds": "Pass + Rush + Rec TDs", "player_sacks": "Sacks",
    "player_solo_tackles": "Solo Tackles", "player_tackles_assists": "Tackles + Assists",
    "player_defensive_interceptions": "Interceptions", "player_field_goals": "Field Goals Made",
    "player_pats": "Extra Points Made", "player_kicking_points": "Kicking Points",
    # NBA
    "player_blocks": "Blocks", "player_steals": "Steals", "player_blocks_steals": "Blocks + Steals",
    "player_turnovers": "Turnovers", "player_points_rebounds_assists": "Pts + Reb + Ast",
    "player_frees_made": "Free Throws Made", "player_frees_attempts": "Free Throw Attempts",
    "player_double_double": "Double-Double", "player_triple_double": "Triple-Double",
    "player_points_rebounds": "Pts + Reb", "player_points_assists": "Pts + Ast", "player_rebounds_assists": "Reb + Ast",
    # NHL
    "player_blocked_shots": "Blocked Shots", "player_goal_scorer_anytime": "Anytime Goal Scorer",
    # MLB
    "batter_home_runs": "Home Runs", "batter_rbis": "RBIs", "batter_runs_scored": "Runs", "batter_walks": "Walks",
    "batter_strikeouts": "Batter Strikeouts", "batter_hits_runs_rbis": "Hits + Runs + RBIs",
    "pitcher_outs": "Pitching Outs", "pitcher_earned_runs": "Earned Runs", "pitcher_hits_allowed": "Hits Allowed",
    "pitcher_walks": "Walks Allowed",
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
VOID_WARN_PCT = 40   # an outlier with at least this much edge says it may be a pricing error the book can void


def american(dec: float) -> str:
    """2.52 -> +152, 1.65 -> -154."""
    if dec >= 2:
        return f"+{round((dec - 1) * 100)}"
    return f"-{round(100 / (dec - 1))}"


def odds(dec: float) -> str:
    return american(dec) if ODDS_FORMAT == "american" else f"{dec:.2f}"


def shown_at_or_above(dec: float) -> float:
    """The smallest price ODDS_FORMAT can print that is at least dec (so a skip line printed
    on a card is never a price that loses money)."""
    eps = 1e-9
    if ODDS_FORMAT != "american":
        return math.ceil(dec * 100 - eps) / 100
    if dec >= 2:
        return 1 + math.ceil((dec - 1) * 100 - eps) / 100
    a = math.floor(100 / (dec - 1) + eps)   # the size of the minus line
    return 1 + 100 / a if a > 100 else 2.0


def _printed(dec: float) -> float:
    """dec as a card prints it (odds()), back as a decimal price: 1.96456 prints -104, which is
    1.9615. Compare prices this way to judge them the way the card showed them."""
    shown = odds(dec)
    if ODDS_FORMAT != "american":
        return float(shown)
    a = int(shown)
    return 1 + a / 100 if a > 0 else 1 + 100 / -a


def money(x: float) -> str:
    """$58 for whole dollars, $57.50 otherwise."""
    return f"${x:,.0f}" if abs(x - round(x)) < 0.005 else f"${x:,.2f}"


def _plain(x: float) -> str:
    """A number as a setting is written: 1500, 1250.5 (never 1.5e+03)."""
    return f"{x:.2f}".rstrip("0").rstrip(".")


def units(stake: float, unit: float) -> str:
    """A stake in units, at most one decimal: '1.5u', '12.5u', '105u' (a sliver under 0.05u: '<0.1u')."""
    s = f"{stake / unit:.1f}"
    if float(s) == 0 and stake > 0:
        return "<0.1u"
    return (s[:-2] if s.endswith(".0") else s) + "u"


def stake_text(stake: float, unit: float) -> str:
    """'$15 (1.5u)': the dollars to bet, then the same in units (just dollars without a unit size)."""
    return f"{money(stake)} ({units(stake, unit)})" if unit > 0 else money(stake)


def _fmt_secs(s: float) -> str:
    s = int(round(s))
    return f"{s}s" if s < 90 else f"{s // 60}m {s % 60:02d}s"


def _age_note(age: float | None, many: bool = False) -> str:
    """'⏱ price was 35s old when sent': how long the book had shown the price when the alert went
    out (the bigger it is, the likelier it has moved by the time you tap)."""
    if age is None:
        return ""
    s = int(round(age))
    when = f"{s}s" if s < 90 else f"{round(s / 60)} min" if s < 5400 else f"{s / 3600:.0f}h"
    return f"\n⏱ {'prices were up to' if many else 'price was'} {when} old when sent"


def format_text(arb: Arb) -> str:
    status = "🔴 LIVE" if arb.is_live else f"starts {arb.commence_time}"
    rows = "\n".join(
        f"  • {l.outcome} {odds(l.price)} on {l.book}{' (alternate line)' if l.alt else ''}"
        f"  → stake {money(l.stake)}"
        + (f"  (bet first: {l.edge:+.1f}% vs {arb.fair_from})" if i == 0 and arb.tagged else "")
        + (f"\n    {l.link}" if l.link else "")
        for i, l in enumerate(arb.legs)
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
ALT_NOTE = " *(alternate line)*"   # after a pick priced on the book's alternate line (find it in its alt lines)


def sport_icon(sport_key: str) -> str:
    return next((icon for prefix, icon in SPORT_ICONS if sport_key.startswith(prefix)), "🏟️")


def _link(text: str, url: str) -> str:
    return f"[{text}]({url})" if url else text


def _when(is_live: bool, commence_time: str, first_seen: float | None = None) -> str:
    if is_live:
        when = "🔴 **LIVE**"
    else:
        start = _parse_time(commence_time)
        ts = int(start.timestamp())
        style = "t" if start - datetime.now(timezone.utc) < timedelta(hours=12) else "F"  # add the day
        when = f"⏰ Starts <t:{ts}:{style}> (<t:{ts}:R>)"
    return when + (f" · spotted <t:{int(first_seen)}:R>" if first_seen else "")


def _card(title: str, description: str, color: int, url: str = "", footer: str = "",
          mention: str = "") -> dict:
    embed = {"title": title[:256], "description": description[:4000], "color": color,
             "timestamp": datetime.now(timezone.utc).isoformat()}
    if url:
        embed["url"] = url  # makes the title tappable
    if footer:
        embed["footer"] = {"text": footer}
    payload = {"username": BOT_NAME, "embeds": [embed], "allowed_mentions": allowed_mentions(mention)}
    if mention:
        payload["content"] = mention
    return payload


def allowed_mentions(mention: str) -> dict:
    """Discord may ping only what the message names: these roles and users, and @everyone/@here only
    when it says so. A book's role mention can't ping anyone else."""
    out: dict = {"parse": ["everyone"] if "@everyone" in mention or "@here" in mention else []}
    roles = re.findall(r"<@&(\d+)>", mention)
    users = re.findall(r"<@!?(\d+)>", mention)
    if roles:
        out["roles"] = list(dict.fromkeys(roles))
    if users:
        out["users"] = list(dict.fromkeys(users))
    return out


def book_role_mention(cfg: "Config", book: str) -> str:
    """The Discord role to ping for a bet at this book (BOOK_ROLES), or "" when the book has none."""
    role = cfg.book_roles.get(book_slug(book))
    return f"<@&{role}>" if role else ""


def book_slug(book: str) -> str:
    """A book's name or Odds API key, reduced to lowercase letters and digits: "BetMGM", "betmgm" -> "betmgm"."""
    return "".join(c for c in book.lower() if c.isalnum())


def parse_book_roles(text: str) -> dict[str, str]:
    """BOOK_ROLES=DraftKings=123,FanDuel=456 (commas or semicolons; a book's name as its cards show it):
    {book slug: role id}. A role id is the number Discord copies with Developer Mode on."""
    out = {}
    for x in text.replace(";", ",").split(","):
        if not x.strip():
            continue
        book, sep, role = (s.strip() for s in x.partition("="))
        if not sep or not book_slug(book) or not role.isdigit():
            raise ValueError(f"BOOK_ROLES: '{x.strip()}' should look like DraftKings=123456789012345678 "
                             "(the book, then its Discord role id)")
        out[book_slug(book)] = role
    return out


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
        # (A Yes/No prop, like an anytime TD, has no line number: "Bijan Robinson Yes".)
        what = (f"{arb.line[0]} {l.outcome}" + (f" {arb.line[1]:g}" if arb.line[1] is not None else "")
                if is_prop(arb.line) else l.outcome)
        first = ""
        if i == 0 and arb.tagged:
            first = "\n     ↳ **Bet this one first:** it's the price that will move."
            if arb.keep_stake:
                first += (f"\n     ↳ If the other price{'s are' if len(arb.legs) > 2 else ' is'} gone, keep this one: "
                          f"it's a good bet alone. Only betting this one? Bet **{money(arb.keep_stake)}**.")
        steps.append(f"{num} Open **{_link(l.book, l.link)}** → bet "
                     f"**{money(l.stake)}** on **{what} {odds(l.price)}**{ALT_NOTE if l.alt else ''}{skip}"
                     f"{kalshi_room_line(l.room, '     ')}"
                     f"{kalshi_tie_note(arb.sport_key, arb.market, l.book, '     ')}{first}")
    rounding = (f"\n*{arb.exact_pct:.2f}% with exact stakes; rounded to look like normal bets*"
                if arb.exact_pct - arb.profit_pct >= 0.05 else "")
    if arb.tagged:
        both, rest = ("BOTH", "2️⃣") if len(arb.legs) == 2 else (f"all {len(arb.legs)}", "the others")
        head = f"place {both} bets, 1️⃣ first"
        order = (f"\nPlace 1️⃣ first, then {rest} right away. If 1️⃣ has moved past its skip line, "
                 f"skip {'both' if len(arb.legs) == 2 else 'them all'}.")
    else:
        head = "place BOTH bets"
        order = "\nDo them back to back. If one price moved past its skip line, don't place the other."
    desc = (f"👉 **DO THIS NOW: {head}. You profit no matter who wins.**\n\n"
            + "\n\n".join(steps)
            + f"\n\n💵 You bet **{money(arb.total_stake)}** and get back at least "
              f"**{money(arb.guaranteed_return)}** (+{money(arb.guaranteed_profit)}).{rounding}"
            + (f"\n🧾 After tax (~{arb.tax[0] * 100:.0f}%): about {signed_money(round(arb.tax[1], 2))} per $100 "
               f"(rough guide, not tax advice)" if arb.tax else "")
            + order
            + f"\n\n───────────────\n{sport_icon(arb.sport_key)} **{arb.sport}** · {arb.matchup}\n"
              f"{market} · {_when(arb.is_live, arb.commence_time, first_seen)}" + _age_note(arb.age, many=True))
    return _card(f"💰 ARB · +{money(arb.guaranteed_profit)} guaranteed ({arb.profit_pct:.2f}%)", desc,
                 0xE74C3C if arb.is_live else 0x2ECC71, url=arb.legs[0].link,
                 footer="ARB = bet every side at different books, profit locked in.", mention=mention)


def _webhook(url: str, payload: dict, method: str = "POST", message_id: str | None = None,
             max_wait: float = 0) -> dict | None:
    """POST a new message (returns it, incl. id) or PATCH an existing one. Discord asking to slow down
    (429) is waited out up to 3 times, each wait at most max_wait seconds (0 = as long as it asks): a longer
    one returns None at once. Only a new alert's post sets max_wait (DISCORD_MAX_WAIT_SECONDS): it has a
    retry, at the next check. Edits (GONE marks, refreshes) and status messages have none, so they wait."""
    target = f"{url}/messages/{message_id}" if message_id else f"{url}?wait=true"
    limit = max_wait
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
                try:
                    wait = float(e.headers.get("Retry-After", "1"))
                except (TypeError, ValueError):
                    wait = 1.0
                if limit and wait > limit:
                    return None   # (Discord never took it: Alerter._discord marks it safe to send again)
                time.sleep(wait)
                continue
            raise
    return None


# Webhooks Discord refused a new card on (webhook URL -> HTTP code), for the main loop to report (discord_trouble).
DISCORD_TROUBLE: dict[str, int] = {}
TROUBLE_HINTS = {404: "the webhook was deleted: make a new one in that channel and run --set-webhook {channel}",
                 401: "the webhook isn't valid any more: make a new one and run --set-webhook {channel}",
                 403: "the webhook isn't allowed to post there: check the channel's permissions",
                 400: "Discord didn't accept the card itself: send Claude this message"}


def channel_name(cfg: "Config", url: str) -> tuple[str, str]:
    """(--set-webhook name, what it's for) of the channel a webhook URL is set for."""
    for name, (env, what) in WEBHOOK_SETTINGS.items():
        if getattr(cfg, env.lower()[len("discord_"):], None) == url:
            return name, what
    return "main", "alerts"


def discord_trouble(cfg: "Config", status: "Status", told: dict[str, float], now: float | None = None) -> None:
    """Tell the health channel about each webhook Discord refuses new cards on, once a day per webhook (those
    cards are retried every check and would otherwise only show in the server log)."""
    now = time.time() if now is None else now
    for url, code in list(DISCORD_TROUBLE.items()):
        DISCORD_TROUBLE.pop(url)
        if url in told and now - told[url] < 86400:
            continue
        told[url] = now
        name, what = channel_name(cfg, url)
        hint = TROUBLE_HINTS.get(code, "").format(channel=name)
        status.send(f"⚠️ Discord turned down new alerts for the {name} channel ({what}): error {code}. "
                    f"They aren't reaching Discord. Fix: {hint}.")


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
    card: str = ""            # hash of the card as last sent; a different one means edit it
    retry: bool = False       # the first post failed before Discord got it: safe to send again
    checks: int = 1           # how many checks found it (live: the ones that confirmed it too)
    spotted: float = 0.0      # when a check first found it (live: before the checks that confirmed it); 0 = first_seen
    better: int = 0           # live: checks in a row that found a much better price not re-alerted yet
    quiet: bool = False       # its post goes out without the ping (a hand-over that isn't much better): a retry too
    sent: bool = False        # it went out (posted, or printed in a dry run), or the card it was handed over from
                              # did: logged and followed from then on
    deferred: bool = False    # its post is held: the odds were too old by the time it was ready (SEND_MAX_DELAY_SECONDS)
    post_delay: float | None = None   # seconds from fetching its odds to the post that sent it (None: not known)
    pinged_pct: float | None = None   # edge of the last post of this bet that went out WITH the ping (or its other
                                      # card's, before a hand-over); None: none did yet
    slots: tuple = ()         # the hourly-cap slots its first post took ((times list, time) pairs): given back if
                              # it never goes out
    prev: tuple = ()          # (message id, webhook) of the card it was handed over from (now a pointer to this
                              # one): if it closes before its own post lands, that card says GONE
    edited: float = 0.0       # when its card was last posted or edited (CARD_EDIT_MIN_SECONDS)
    shown: tuple = ()         # (book, price, stake) of the bet as its card shows it (an arb: ())
    # ONE_ALERT_PER_BET: the bet as its card went out, how that card was drawn, and its ✅ / ⚠️ line ("" = none yet)
    sent_item: object = None
    render: object = None
    status: str = ""


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


CANDIDATE_FIELDS = ["time", "sport", "event_id", "matchup", "market", "line", "outcome", "book", "price", "fair_prob",
                    "fair_from", "sources", "kalshi_prob", "ev_pct", "confidence", "decision", "post_delay"]


class CandidateLog:
    """CANDIDATE_LOG_FILE: the +EV and outlier bets that came within a point of their bar, and what happened
    to each: "alerted" (with post_delay, seconds from fetching its odds to the post), held back ("capped",
    "live cap", "old price", "unconfirmed", "deferred": its odds too old by sending time), or the check that
    stopped it ("sharp hold", "market gap", "kalshi gap", "kalshi no", "sharp no", "sharp other line",
    "sharp last price", "confirm fail: <why>", "low confidence", "moved first", "first look", "under bar"), or
    a prop price left out as bad data ("too big", "book market off": see prop_glitches).
    The same bet and decision again within HELD_LOG_GAP seconds isn't written again. Off ("" or a run that
    mustn't write the service's files) writes nothing."""

    def __init__(self, cfg: Config):
        self.name = cfg.candidate_log_file
        self.last: dict[tuple, float] = {}   # (bet key, decision) -> when last written

    def add(self, item, decision: str, now: float, post_delay: float | None = None) -> None:
        if not self.name:
            return
        try:
            key = (item.key, decision)
            last = self.last.get(key)
            if last is not None and now - last <= HELD_LOG_GAP:
                return   # (a bet that stays in the same state gets a row every HELD_LOG_GAP seconds)
            self.last[key] = now
            if len(self.last) > 5000:
                self.last = {k: t for k, t in self.last.items() if now - t <= HELD_LOG_GAP}
            line = item.line
            kq = getattr(item, "kalshi", None)
            append_csv(self.name, CANDIDATE_FIELDS, {
                "time": _utc(now), "sport": item.sport or item.sport_key, "event_id": item.event_id,
                "matchup": item.matchup, "market": item.market,
                "line": (f"{line[0]} {line[1]:g}" if line[1] is not None else line[0]) if is_prop(line)
                        else "" if item.point is None else item.point,
                "outcome": item.outcome, "book": item.book, "price": item.price,
                "fair_prob": round(item.fair_prob, 4), "fair_from": item.sharp_book,
                "sources": sources_line(item) if getattr(item, "refs", None) else "",
                "kalshi_prob": round(kq[0], 4) if kq else "", "ev_pct": round(item.ev_pct, 2),
                "confidence": getattr(item, "confidence", ""), "decision": decision,
                "post_delay": "" if post_delay is None else round(post_delay, 1)})
        except Exception as e:  # noqa: BLE001 - a diagnostics file must never get in the way of the alerts
            print(f"  ! Candidate log: {e!r:.200}", file=sys.stderr)

    def extend(self, rejects: list, now: float) -> None:
        for item, why in rejects:
            self.add(item, why, now)


# Files cut to LOG_KEEP_DAYS once a day, and each one's time column. Nothing that grades bets or counts a
# record (ev_bets, outliers, parlays, results, closing lines; score_checks.csv's running "ESPN agreed on N of
# M" is a trust measure) is ever cut.
PRUNED_LOGS = (("candidate_log_file", "time"), ("log_file", "first_seen"), ("markout_file", "first_seen"))


def prune_csv(name: str, keep_days: float, now: datetime, column: str) -> int:
    """Drop rows whose `column` time is over keep_days old, rewriting the file atomically (a temporary file,
    then a rename: a crash halfway leaves the old file). A row whose time can't be read is kept. Returns
    how many rows went."""
    path = data_path(name)
    if not name or keep_days <= 0 or not path.exists():
        return 0
    cut = now - timedelta(days=keep_days)
    tmp = path.with_name(path.name + ".tmp")

    def old(row: dict) -> bool:
        try:
            t = _parse_time(row.get(column) or "")
        except (TypeError, ValueError):
            return False
        return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)) < cut
    try:
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            fields, rows = reader.fieldnames or [], list(reader)
        keep = [r for r in rows if not old(r)]
        if len(keep) == len(rows):
            return 0
        with tmp.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(keep)
        os.replace(tmp, path)
        return len(rows) - len(keep)
    except (OSError, csv.Error, UnicodeDecodeError) as e:
        print(f"  ! Couldn't trim {path}: {e}", file=sys.stderr)
        try:
            tmp.unlink(missing_ok=True)   # (the old file is untouched)
        except OSError:
            pass
        return 0


def prune_logs(cfg: Config, now: datetime | None = None) -> dict[str, int]:
    """LOG_KEEP_DAYS: cut candidates.csv, arbs.csv and markouts.csv to that many days (0 = keep all).
    Returns {file: rows dropped} for the files that lost some."""
    now = now or datetime.now(timezone.utc)
    out = {}
    for attr, column in PRUNED_LOGS:
        name = getattr(cfg, attr)
        if n := prune_csv(name, cfg.log_keep_days, now, column):
            out[name] = n
    return out


LOG_FIELDS = ["first_seen", "gone_at", "seconds_open", "sport", "matchup", "market", "line",
              "live", "best_profit_pct", "legs", "checks", "spotted", "stale_book", "stale_edge_pct", "leg_edges",
              "fair_from", "leg_ages", "reason"]
HELD_LOG_GAP = 900   # an arb held back again within this many seconds isn't logged again


def _utc(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


class HourlyCap:
    """New alerts in the last hour, counted across every alerter that shares it (LIVE_PER_HOUR:
    every live alert, whatever its kind, and every better-price re-alert on a live one)."""

    def __init__(self, limit: int = 0):
        self.limit = limit      # 0 = no cap
        self.times: list[float] = []

    def room(self, now: float) -> float:
        """How many more may go out this hour (infinite without a cap)."""
        self.times[:] = [t for t in self.times if now - t < 3600]
        return self.limit - len(self.times) if self.limit else math.inf


class Scope:
    """What one pass's main-line checks looked at, when LIVE_MARKETS splits a sport's checks in two
    (each on its own clock). An alert counts as looked for, so it closes if it wasn't found, only
    when a check this pass asked for its bet type and its game."""

    def __init__(self):
        self.sports: set[str] = set()   # one check asked for every bet type of every game in these
        # (sport, bet types, games starting after `since` (None = any), and up to `until`)
        self.looks: list[tuple[str, frozenset, datetime | None, datetime]] = []

    def add(self, sport: str, markets, since: datetime | None, until: datetime) -> None:
        self.looks.append((sport, frozenset(_csv(markets) if isinstance(markets, str) else markets), since, until))

    def covers(self, sport_key: str, event_id: str = "", market: str = "", commence_time: str = "") -> bool:
        if sport_key in self.sports:
            return True
        try:
            start = _parse_time(commence_time) if commence_time else None
        except (TypeError, ValueError):
            start = None
        for sport, markets, since, until in self.looks:
            if sport != sport_key or (market and market not in markets):
                continue   # (an alert saved before the bet type was remembered: any check of its game)
            if start is None or ((since is None or start > since) and start <= until):
                return True
        return False


def looked_at(checked, sport_key: str, event_id: str, market: str = "", commence_time: str = "") -> bool:
    """Did this check look at that line? checked: sport keys and event ids (every bet type of those
    sports or games was checked), or a Scope (LIVE_MARKETS: some bet types of some games)."""
    if isinstance(checked, Scope):
        return checked.covers(sport_key, event_id, market, commence_time)
    return sport_key in checked or event_id in checked


class AlertedBets:
    """Every bet alerted so far (ONE_ALERT_PER_BET), so none goes out twice: not at a better price, not at
    another book, not as the other alert type, not after a restart. Saved in STATE_DIR (alerted_bets.json);
    each one is forgotten a day after its game starts. path None: remembered only while the bot runs."""

    def __init__(self, path: Path | None):
        self.path = path
        self.until: dict[str, float] = {}   # bet -> when it can be forgotten (time.time())
        if path and path.exists():
            try:
                self.until = {k: float(v) for k, v in json.loads(path.read_text()).items()}
            except (OSError, ValueError, AttributeError):
                print(f"  ! Couldn't read {path.name}; starting it again", file=sys.stderr)

    def __contains__(self, bet: str) -> bool:
        return bet in self.until

    def add(self, bet: str, until: float, now: float | None = None) -> None:
        """Remember a bet until `until`; the ones whose time is up by `now` (the check's clock) go."""
        if self.until.get(bet) == until:
            return
        now = time.time() if now is None else now
        self.until = {k: t for k, t in self.until.items() if t > now}
        self.until[bet] = until
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.until))
            tmp.replace(self.path)
        except OSError as e:
            print(f"  ! Couldn't save the alerted bets: {e}", file=sys.stderr)


class Alerter:
    """Tracks arbs from first sighting until they close.

    New arb -> new Discord message (with optional @mention).
    Prices change while still an arb -> the same message is edited in place.
    Arb disappears -> the message is edited to "GONE after Ns" and logged to CSV.
    """

    noun = "arbs"
    log_fields = LOG_FIELDS
    on_log = None  # optional callback(row) after a row is logged
    on_open = None  # optional callback(item, first_seen) when a brand-new alert goes out (markouts)
    on_held = None  # optional callback(item, why, now) when the caps or live rules hold one back (weekly card)
    on_candidate = None  # optional callback(item, decision, now, post_delay) for the candidate log (bets)
    on_posted = None     # optional callback(log row, message id, webhook) when a bet's card goes up (✅ tracking)
    log_on_open = False  # arbs are logged when they close, so the row has how long it lasted
    log_held = True      # arbs the caps or live rules held back go in the log too, with the reason

    RESTORE_GRACE = 900  # seconds a restored alert gets to show up again before it's marked gone
    scoped = True        # handle() is told which sports/games were checked (parlays: no)
    # An open card whose own bet didn't change (only other books' prices, its notes) is edited at most every
    # CARD_EDIT_MIN_SECONDS: +EV and outlier cards. Arbs, prop arbs and parlays: every change right away.
    throttle_edits = False
    book_pings = False   # a new card pings its book's Discord role when BOOK_ROLES is set (bets, not arbs)
    one_alert = False    # ONE_ALERT_PER_BET applies (bets, not arbs)
    alerted: "AlertedBets | None" = None   # the bets already alerted, shared by every bet alerter (Trackers)

    def __init__(self, cfg: Config, dry_run: bool, noun: str | None = None, props: bool = False):
        self.cfg = cfg
        self.props = props   # player props' +EV and outlier cards: DISCORD_PROPS_WEBHOOK_URL when it's set
        if noun:
            self.noun = noun  # before loading state: it names the state file
        self.dry_run = dry_run or not cfg.webhook_url
        self.open: dict[str, OpenArb] = {}
        self.restored: dict[str, dict] = {}
        self.started = time.time()
        self.max_per_hour = 0          # 0 = no cap
        self.posted_at: list[float] = []   # may be shared with a sibling (arbs and prop arbs: one count)
        self.live_cap: HourlyCap | None = None   # the cap every live alert shares (LIVE_PER_HOUR)
        self.held_counts: dict[str, int] = {}    # alerts held back, by why (the console line)
        self.held_logged: dict[tuple, float] = {}  # (key, why) -> last time it was held back
        self.handed: dict[str, float] = {}  # key -> first_seen, for bets taken over from a sibling alerter
        self.handed_pct: dict[str, float] = {}  # ...and the edge that sibling last alerted it at (with the ping)
        self.handed_unsent: set[str] = set()    # ...whose old card never went out (never logged or followed either)
        self.handed_msg: dict[str, tuple] = {}  # ...and that old card's (message id, webhook), now pointing here
        self.send_retryable = False
        self._slots: list[tuple[list, float]] = []   # the cap slots the last capped() call took
        self.reset_stats()
        self._load_state()

    # ---- remembering open alerts across restarts
    def _state_path(self) -> Path | None:
        if self.dry_run or not self.cfg.state_dir:
            return None
        slug = "".join(c if c.isalnum() else "_" for c in self.noun).strip("_")
        return data_path(self.cfg.state_dir) / f"open_{slug}.json"

    def _load_state(self) -> None:
        path = self._state_path()
        if not path or not path.exists():
            return
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        cutoff = time.time() - 86400
        self.restored = {k: v for k, v in saved.items() if v.get("first_seen", 0) > cutoff}

    def _save_state(self) -> None:
        path = self._state_path()
        if not path:
            return
        data = {k: {"message_id": op.message_id, "url": op.url, "first_seen": op.first_seen,
                    "alerted_pct": op.alerted_pct, "pinged_pct": op.pinged_pct, "best_pct": op.best_pct,
                    "card": op.card, "checks": op.checks,
                    "spotted": op.spotted,
                    "label": self.label(op.arb), "sport_key": op.arb.sport_key,
                    "event_id": op.arb.event_id, "commence_time": getattr(op.arb, "commence_time", ""),
                    "market": getattr(op.arb, "market", ""),
                    **self.extra_state(op.arb)}
                for k, op in self.open.items() if op.message_id}
        data.update(self.restored)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(path)
        except OSError as e:
            print(f"  ! Couldn't save alert state: {e}", file=sys.stderr)

    def _restore(self, arb, now: float) -> OpenArb | None:
        """Pick an alert back up after a restart: same Discord message, no new ping."""
        saved = self.restored.pop(arb.key, None)
        if not saved:
            return None
        self.restore_extra(arb, saved)
        op = OpenArb(arb, saved["first_seen"], now, max(saved.get("best_pct", 0), self.value(arb)),
                     message_id=saved.get("message_id"), url=saved.get("url", ""),
                     alerted_pct=saved.get("alerted_pct", self.value(arb)),
                     pinged_pct=saved.get("pinged_pct", saved.get("alerted_pct", self.value(arb))),
                     card=self.card_hash(arb, saved["first_seen"]), checks=saved.get("checks", 1),
                     spotted=saved.get("spotted") or 0.0, sent=True)
        self.open[arb.key] = op
        if self.once and self.alerted is not None:   # (state saved before ONE_ALERT_PER_BET was on)
            self.alerted.add(self.bet_identity(arb), self.bet_expires(arb), now)
        if saved.get("card") != op.card and op.message_id and not self.once:
            self._discord(self.payload(arb, first_seen=op.first_seen), op.message_id, op.url)
            op.edited, op.shown = now, self.shown(arb)
        return op

    def _drop_restored(self, key: str, why: str) -> None:
        saved = self.restored.pop(key)
        if saved.get("message_id"):
            self._discord(_gone_card(f"❌ GONE · {saved.get('label', 'alert')}", why),
                          saved["message_id"], saved.get("url", ""))

    def _expire_restored(self, now: float) -> None:
        """Saved alerts nobody has seen again: their game is over, or (for state saved by an
        older version, and parlays) the grace period after the restart has passed."""
        if not self.restored:
            return
        grace_over = now - self.started >= self.RESTORE_GRACE
        for key, saved in list(self.restored.items()):
            if self.scoped and saved.get("sport_key") and saved.get("commence_time"):
                end = (_parse_time(saved["commence_time"])
                       + timedelta(minutes=self.cfg.minutes_for(saved["sport_key"])))
                if end.timestamp() > now:
                    continue   # closes when its sport or game is next checked (see handle)
            elif not grace_over:
                continue
            self._drop_restored(key, "Ignore this one. It closed while the bot was restarting.")

    def card_hash(self, item, first_seen: float | None) -> str:
        """Everything the card shows, so any visible change (a book's price on the list, the
        confidence, related alerts...) edits it."""
        emb = {k: v for k, v in self.payload(item, first_seen=first_seen)["embeds"][0].items() if k != "timestamp"}
        return hashlib.sha1(json.dumps(emb, sort_keys=True).encode()).hexdigest()[:16]

    @staticmethod
    def shown(item) -> tuple:
        """The bet as its card shows it, (book, price, stake): what an edit can't wait for (an arb: ())."""
        return tuple(getattr(item, k) for k in ("book", "price", "stake")) if hasattr(item, "book") else ()

    def edit_waits(self, item, cur: OpenArb, now: float) -> bool:
        """Can this change to an open card wait (CARD_EDIT_MIN_SECONDS)? Only on a +EV or outlier card already
        posted, before the game, when the bet itself (book, price, stake) is as the card shows it and the card
        was posted or edited less than that long ago. The card isn't marked as edited, so a later check edits
        it with whatever is newest then."""
        limit = self.cfg.card_edit_min_seconds
        return (self.throttle_edits and limit > 0 and bool(cur.message_id) and not cur.retry
                and not getattr(item, "is_live", False) and self.shown(item) == cur.shown
                and now - cur.edited < limit)

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

    def mention_for(self, item) -> str:
        """Who a new card for this item pings: its book's role (BOOK_ROLES; nobody when its book has none),
        or else this alert type's mention."""
        if self.book_pings and self.cfg.book_roles:
            return book_role_mention(self.cfg, getattr(item, "book", ""))
        return self.mention()

    @property
    def once(self) -> bool:
        """One alert per bet (ONE_ALERT_PER_BET): no re-alerts, no edits but GONE, never the same bet twice."""
        return self.one_alert and self.cfg.one_alert_per_bet

    def bet_identity(self, item) -> str:
        """The bet itself, whatever book has it (the +EV / outlier key has no book in it)."""
        return item.key

    def bet_expires(self, item) -> float:
        """When the bet can't come back any more, so the memory of it can go: a day after its game starts."""
        start = getattr(item, "commence_time", "")
        return (_parse_time(start).timestamp() if start else time.time()) + 86400

    def ranked(self, items: list) -> list:
        """The order new alerts go out in, so an hourly cap keeps the best: games that haven't
        started first (you have time to place them), then the biggest edge, an account-safe arb
        counting ACCOUNT_SAFE_BONUS points more."""
        def score(it) -> float:
            return self.value(it) + (ACCOUNT_SAFE_BONUS if getattr(it, "account_safe", False) else 0)
        return sorted(items, key=lambda it: (bool(getattr(it, "is_live", False)), -score(it)))

    def capped(self, item, now: float) -> str:
        """Why an hourly cap holds this new alert back: "capped" (its own kind's cap), "live cap"
        (the cap all live alerts share), or "" when both have room (the slots are then taken, and noted in
        self._slots so an alert that never goes out can give them back)."""
        live = self.live_cap if getattr(item, "is_live", False) and self.live_cap else None
        self._slots = []
        if self.max_per_hour:
            self.posted_at[:] = [t for t in self.posted_at if now - t < 3600]   # in place: it may be shared
            if len(self.posted_at) >= self.max_per_hour:
                return "capped"
        if live is not None and live.room(now) <= 0:
            return "live cap"
        if self.max_per_hour:
            self.posted_at.append(now)
            self._slots.append((self.posted_at, now))
        if live is not None:
            live.times.append(now)
            self._slots.append((live.times, now))
        return ""

    def reping_waits(self, item, op: OpenArb, age: float | None, now: float) -> str:
        """A much better price on an open alert gets a new, pinging alert. When the bet is live, that
        ping follows the live rules like any new live alert: the price to bet is fresh
        (LIVE_MAX_AGE_ALERT), LIVE_CONFIRM_CHECKS checks in a row found the better price, and
        LIVE_PER_HOUR has room (the ping takes a slot). Returns what it waits for ("old price",
        "waiting", "live cap"), or "" when it may ping now. Until then the card is only edited and the
        edge it was alerted at stays, so a later check can still send it."""
        if not getattr(item, "is_live", False):
            return ""
        limit = self.cfg.live_max_age_alert
        if limit and (age is None or age > limit):
            op.better = 0     # not a sighting: counting starts again at the next fresh one
            return "old price"
        op.better += 1
        if op.better < self.cfg.live_confirm_checks:
            return "waiting"
        if self.live_cap is not None:
            if self.live_cap.room(now) <= 0:
                return "live cap"
            self.live_cap.times.append(now)
        return ""

    def note_held(self, item, why: str, now: float, first_seen: float | None = None, checks: int = 1) -> None:
        """An alert the hourly caps or the live rules held back: not posted, not followed. Counted
        for the console line. Arbs also go in arbs.csv with the reason ("capped", "live cap", "old
        price", "unconfirmed": gone before enough checks found it), once per stretch of checks, so the rules
        can be tuned from what they held back. `spotted` is when a check first found it, as on alerted rows."""
        self.held_counts[why] = self.held_counts.get(why, 0) + 1
        if self.on_held:
            self.on_held(item, why, now)
        if self.on_candidate and why != "waiting":
            self.on_candidate(item, why, now)
        if not self.log_held or why == "waiting" or not self.log_path():
            return
        last, self.held_logged[(item.key, why)] = self.held_logged.get((item.key, why)), now
        if len(self.held_logged) > 2000:
            self.held_logged = {k: t for k, t in self.held_logged.items() if now - t <= HELD_LOG_GAP}
        if last is not None and now - last <= HELD_LOG_GAP:
            return
        first, gone = first_seen or now, why == "unconfirmed"
        found = getattr(item, "found", ())   # confirmed by the live checks, then capped
        row = {**self.row(OpenArb(item, first, now, self.value(item), spotted=found[0] if found else first)),
               "first_seen": _utc(first), "gone_at": _utc(now) if gone else "",
               "seconds_open": round(now - first) if gone else "", "checks": checks if gone else "", "reason": why}
        append_csv(self.log_path(), self.log_fields, row)

    def carry(self, old, new) -> None:
        """Copy anything from the previous sighting the new one should remember: how old the price
        was when the alert went out (an edit doesn't change it)."""
        if hasattr(new, "age"):
            new.age = old.age
        if isinstance(new, Arb):
            new.first = old.first or old.snapshot()   # arbs.csv describes the alert as first sent

    def extra_state(self, item) -> dict:
        """What carry() remembers, saved so a restart can rebuild the same card."""
        out = {"age": item.age} if hasattr(item, "age") else {}
        if isinstance(item, Arb):
            out["first"] = item.first or item.snapshot()
        return out

    def restore_extra(self, item, saved: dict) -> None:
        """Put extra_state back onto a freshly found item after a restart."""
        if hasattr(item, "age"):
            item.age = saved.get("age")   # none saved (an older version sent it): unknown, not today's age
        if isinstance(item, Arb) and isinstance(saved.get("first"), dict):
            item.first = saved["first"]

    def log_path(self) -> str:
        return self.cfg.log_file

    def row(self, op: OpenArb) -> dict:
        a = op.arb
        return {
            "sport": a.sport, "matchup": a.matchup, "market": a.market,
            "line": "" if a.line is None else a.line, "live": a.is_live,
            "best_profit_pct": round(op.best_pct, 2),
            "legs": "; ".join(f"{l.outcome} @{l.price} {l.book}" for l in a.legs),
            "checks": op.checks, "spotted": _utc(op.spotted or op.first_seen), **(a.first or a.snapshot()),
            "reason": "",
        }

    def webhook_for(self, item) -> str:
        if item.is_live and self.cfg.live_webhook_url:
            return self.cfg.live_webhook_url
        return self.cfg.webhook_url

    def _discord(self, payload: dict, message_id: str | None = None, url: str = "") -> str | None:
        self.send_retryable = False
        if self.dry_run:
            return None
        url = url or self.cfg.webhook_url
        try:
            if message_id:   # (an edit has no retry: a long "slow down" is waited out, as for status messages)
                _webhook(url, payload, "PATCH", message_id)
                return message_id
            # A new alert's post: a longer wait than DISCORD_MAX_WAIT_SECONDS isn't waited out, the next check sends it.
            msg = _webhook(url, payload, max_wait=self.cfg.discord_max_wait_seconds)
            self.send_retryable = not msg   # rate-limited (3 times, or for too long): Discord never took it
            return msg.get("id") if msg else None
        except Exception as e:  # keep scanning even if Discord hiccups
            print(f"  ! Discord send failed: {e}", file=sys.stderr)
            if not message_id and isinstance(e, urllib.error.HTTPError) and e.code in (400, 401, 403, 404):
                DISCORD_TROUBLE[url] = e.code   # a new card Discord refuses for good: the health channel is told
            # urllib wraps errors before Discord has the message (refused, DNS, an HTTP error
            # reply) in URLError. A timeout or drop while reading the reply isn't: the post may
            # have gone through, so sending it again could double the alert.
            self.send_retryable = isinstance(e, urllib.error.URLError)
            return message_id

    def data_age(self, item, fetched: dict[str, float] | None) -> float | None:
        """Seconds since the odds this alert comes from were fetched (fetched: {event id: time.time() of the
        fetch}, see fetched_at); None when that isn't known (the demo, tests), and then it's never held for it."""
        t = fetched.get(getattr(item, "event_id", "")) if fetched else None
        return None if t is None else max(0.0, time.time() - t)

    def too_old(self, item, age: float | None) -> bool:
        """Are these odds too old to post on now? SEND_MAX_DELAY_SECONDS before the game,
        LIVE_SEND_MAX_DELAY_SECONDS once it's live (0 = no limit)."""
        limit = (self.cfg.live_send_max_delay_seconds if getattr(item, "is_live", False)
                 else self.cfg.send_max_delay_seconds)
        return bool(limit) and age is not None and age > limit

    def defer(self, op: OpenArb, age: float, now: float) -> None:
        """Hold a pinging post whose odds are too old by now. The alert stays open and unsent; the next check
        that finds it again sends it on its fresh odds (so only if it still qualifies then), and one that
        doesn't find it closes it, never sent."""
        op.retry = op.deferred = True
        self.held_counts["old data"] = self.held_counts.get("old data", 0) + 1
        if self.on_candidate:
            self.on_candidate(op.arb, "deferred", now)
        print(f"  ⏸ {self.label(op.arb)}: not sent, its odds were {_fmt_secs(age)} old by then "
              f"(the next check looks again)", flush=True)

    def not_taken(self, op: OpenArb) -> None:
        """A post Discord didn't take (it asked the bot to wait longer than DISCORD_MAX_WAIT_SECONDS, or refused
        it): the alert stays open and unsent, and the next check that finds it sends it. Counted for the console."""
        self.held_counts["not sent"] = self.held_counts.get("not sent", 0) + 1
        print(f"  ⏸ {self.label(op.arb)}: Discord didn't take it (busy or unreachable), not sent "
              f"(the next check tries again)", flush=True)

    def _went_out(self, op: OpenArb, now: float, fetched: dict[str, float] | None) -> None:
        """An alert's post went out (or was printed, in a dry run): note how long after its odds were fetched,
        then log it and follow it (markouts), unless that was done when its other card went out (a hand-over)."""
        op.post_delay, op.deferred = self.data_age(op.arb, fetched), False
        if self.on_candidate:
            self.on_candidate(op.arb, "alerted", now, op.post_delay)
        if self.once and self.alerted is not None:
            self.alerted.add(self.bet_identity(op.arb), self.bet_expires(op.arb), now)
        if self.once:
            op.sent_item, op.render = op.arb, self.payload
        if self.on_posted and op.message_id and not self.dry_run:
            try:
                self.on_posted({"first_seen": _utc(op.first_seen), **self.row(op)}, op.message_id, op.url)
            except Exception as e:  # noqa: BLE001 - ✅ tracking must never hold up an alert
                print(f"  ! ✅ tracking: {e!r:.150}", file=sys.stderr)
        if not op.sent:
            op.sent = True
            if self.log_on_open:
                self._log(op, now, None)
            if self.on_open:
                self.on_open(op.arb, op.first_seen)
        self.stats["found"] += 1
        if self.value(op.arb) > self.stats["best_pct"]:
            self.stats["best_pct"], self.stats["best"] = self.value(op.arb), op.arb.matchup

    def handle(self, arbs: list[Arb], checked_sports: list[str] | None = None,
               now: float | None = None, checked_events: "set[str] | Scope | None" = None,
               fetched: dict[str, float] | None = None) -> int:
        """Process one scan's arbs. checked_sports = sports whose odds were just fetched;
        open arbs in those sports that weren't found again are closed. checked_events narrows that
        to the games (event ids) or, as a Scope, the bet types and games that were checked.
        fetched: when each game's odds were fetched ({event id: time.time()}): a new alert or a much better
        price's re-alert isn't posted on odds older than SEND_MAX_DELAY_SECONDS (live:
        LIVE_SEND_MAX_DELAY_SECONDS) by the time it's ready; the next check that finds it sends it then.
        An alert is logged (bets) and followed (markouts) once its post went out."""
        now = now or time.time()
        new = 0
        seen = set()
        for arb in self.ranked(arbs):
            seen.add(arb.key)
            fresh_age = getattr(arb, "age", None)   # (before a restored card puts back the age it was sent with)
            cur = self.open.get(arb.key) or self._restore(arb, now)
            handed = self.handed.pop(arb.key, None)
            last_pct = self.handed_pct.pop(arb.key, None)
            if (cur is None and handed is None and self.once and self.alerted is not None
                    and self.bet_identity(arb) in self.alerted):
                # Alerted once already (its card closed, or another book has it now): bet once, never again.
                self.held_counts["alerted before"] = self.held_counts.get("alerted before", 0) + 1
                continue
            if cur is None and handed is None and (why := self.capped(arb, now)):
                self.note_held(arb, why, now)
                continue  # an hourly cap is full; the best go first, so the best already went out
            if cur is None:
                first = handed or now
                found = getattr(arb, "found", ())   # live: the checks that confirmed it count too
                # A bet handed over from its other card (+EV <-> outlier) is the same bet: like any re-alert,
                # it pings only when its edge beats the edge it was last alerted at by REALERT_JUMP_PCT.
                # Otherwise its new card goes up quietly, and that edge stays the one a re-alert must beat.
                # (Nor does a bet kept up under MIN_CONFIDENCE, handed over from its other card, ping.)
                jumped = last_pct is None or self.value(arb) >= last_pct + self.cfg.realert_jump_pct
                quiet = getattr(arb, "kept", False) or not jumped
                # (A handed-over bet whose other card went out was logged and followed then.)
                op = OpenArb(arb, first, now, self.value(arb),
                             alerted_pct=self.value(arb) if jumped else last_pct,
                             url=self.webhook_for(arb), checks=found[1] if found else 1,
                             spotted=found[0] if found else first, quiet=quiet,
                             sent=handed is not None and arb.key not in self.handed_unsent,
                             pinged_pct=last_pct, slots=tuple(self._slots) if handed is None else (),
                             prev=self.handed_msg.pop(arb.key, ()))
                self.open[arb.key] = op
                age = self.data_age(arb, fetched)
                if self.too_old(arb, age):
                    self.defer(op, age, now)
                else:
                    print(self.text(arb), flush=True)
                    if not jumped:
                        print(f"  ↔ {self.label(arb)}: same bet as its other card, no new ping (not "
                              f"{self.cfg.realert_jump_pct:g}+ points better than the {last_pct:.2f}% alerted)",
                              flush=True)
                    op.message_id = self._discord(self.payload(arb, "" if quiet else self.mention_for(arb),
                                                               first_seen=first), url=op.url)
                    op.edited, op.shown = now, self.shown(arb)
                    op.retry = op.message_id is None and self.send_retryable
                    if op.retry:
                        self.not_taken(op)
                op.card = self.card_hash(arb, first)
                if not op.retry:
                    if not quiet:
                        op.pinged_pct = self.value(arb)
                    self._went_out(op, now, fetched)
                    new += 1
            else:
                self.carry(cur.arb, arb)
                card = self.card_hash(arb, cur.first_seen)
                changed = card != cur.card
                cur.arb, cur.last_seen = arb, now
                cur.checks += 1
                cur.best_pct = max(cur.best_pct, self.value(arb))
                # (Not for a bet kept up under MIN_CONFIDENCE: it wouldn't be alerted new, so no new ping.)
                # (Nor with ONE_ALERT_PER_BET: a bet goes out once, whatever its price does later.)
                better = (self.value(arb) >= cur.alerted_pct + self.cfg.realert_jump_pct
                          and not getattr(arb, "kept", False) and not self.once)
                if not better:
                    cur.better = 0
                age = self.data_age(arb, fetched)
                # (A re-alert on odds too old to post waits too: the card is edited, and the next check may send it.)
                wait = ("old data" if self.too_old(arb, age) else self.reping_waits(arb, cur, fresh_age, now)
                        ) if better else ""
                if better and not wait:
                    # Edits don't ping your phone, so a much better price gets a fresh alert
                    # (showing how old ITS price is).
                    cur.better = 0
                    if hasattr(arb, "age"):
                        arb.age = fresh_age
                        card = self.card_hash(arb, cur.first_seen)
                    print(f"  ⬆️ improved: {self.label(arb)} now {self.value(arb):.2f}%", flush=True)
                    print(self.text(arb), flush=True)
                    if cur.message_id:
                        self._discord({"embeds": [{"title": "⬆️ Better price: see the newer alert below",
                                                   "color": 0x95A5A6}]}, cur.message_id, cur.url)
                    cur.deferred = False
                    cur.message_id = self._discord(self.payload(arb, self.mention_for(arb), first_seen=cur.first_seen),
                                                   url=cur.url)
                    cur.edited, cur.shown = now, self.shown(arb)
                    cur.card, cur.retry = card, cur.message_id is None and self.send_retryable
                    if cur.retry:
                        self.not_taken(cur)
                    else:
                        cur.pinged_pct = self.value(arb)
                    cur.alerted_pct = self.value(arb)
                    if not cur.sent and not cur.retry:   # (an alert held back until now goes out as this one)
                        self._went_out(cur, now, fetched)
                    new += 1
                elif self.once and cur.sent:
                    self.still_good(cur, arb, now)   # (ONE_ALERT_PER_BET: the card stays as sent, but for ✅ / ⚠️)
                elif changed and self.edit_waits(arb, cur, now):
                    pass   # (only other books' prices or notes changed: edited by a later check, CARD_EDIT_MIN_SECONDS)
                elif changed or cur.retry:
                    # (A live bet's better price that the live rules hold back: edited, no new ping yet.)
                    if changed:
                        print(f"  ↻ updated: {self.label(arb)} now {self.value(arb):.2f}%"
                              + (f" (better price, no new ping yet: {HELD_LABELS.get(wait, wait)})" if wait else ""),
                              flush=True)
                    if cur.message_id:
                        self._discord(self.payload(arb, first_seen=cur.first_seen), cur.message_id, cur.url)
                        cur.edited, cur.shown = now, self.shown(arb)
                    elif cur.retry and self.too_old(arb, age):
                        self.defer(cur, age, now)   # this check's odds are too old by now as well: the next one
                    elif cur.retry:
                        # The first post never reached Discord (refused, rate limited) or was held for old
                        # odds: send it now, with the ping, and keep its id so edits and the GONE mark reach it.
                        # A live one only while its price is still fresh enough for a new live alert
                        # (LIVE_MAX_AGE_ALERT); until then it waits (still open, not logged until it goes out).
                        limit = self.cfg.live_max_age_alert
                        if not (getattr(arb, "is_live", False) and limit and (fresh_age is None or fresh_age > limit)):
                            if hasattr(arb, "age"):
                                arb.age = fresh_age   # how old the price is now, when it really goes out
                                card = self.card_hash(arb, cur.first_seen)
                            if cur.deferred:   # (never shown: it was held before its first post)
                                print(self.text(arb), flush=True)
                            cur.deferred = False   # (from here, a post Discord doesn't take is "not sent")
                            cur.message_id = self._discord(self.payload(arb, "" if cur.quiet else self.mention_for(arb),
                                                                        first_seen=cur.first_seen), url=cur.url)
                            cur.edited, cur.shown = now, self.shown(arb)
                            cur.retry = cur.message_id is None and self.send_retryable
                            if cur.retry:
                                self.not_taken(cur)
                            else:
                                if not cur.quiet:   # its ping is the alert: a re-alert must beat the edge it showed
                                    cur.alerted_pct = cur.pinged_pct = self.value(arb)
                                    cur.better = 0
                                self._went_out(cur, now, fetched)
                                new += 1
                    cur.card = card

        checked = set(checked_sports) if checked_sports is not None else None
        for key in list(self.open):
            op = self.open[key]
            if key in seen or (checked is not None and op.arb.sport_key not in checked):
                continue
            if checked_events is not None and not looked_at(checked_events, op.arb.sport_key, op.arb.event_id,
                                                            op.arb.market, op.arb.commence_time):
                continue  # that game (or bet type) wasn't re-checked this round
            self._close(key, now)
        # Alerts saved before a restart that this check looked for and didn't find.
        if self.scoped and (checked is not None or checked_events is not None):
            for key, saved in list(self.restored.items()):
                if not saved.get("sport_key"):
                    continue   # saved by an older version: the grace period decides
                if checked is not None and saved["sport_key"] not in checked:
                    continue
                if checked_events is not None and not looked_at(checked_events, saved["sport_key"],
                                                                saved.get("event_id", ""), saved.get("market", ""),
                                                                saved.get("commence_time", "")):
                    continue
                self._drop_restored(key, "Ignore this one. The prices moved while the bot was restarting.")
        self.handed.clear()
        self.handed_pct.clear()
        self.handed_unsent.clear()
        self.handed_msg.clear()
        self._expire_restored(now)
        self._save_state()
        return new

    def still_good(self, op: OpenArb, item, now: float) -> None:
        """ONE_ALERT_PER_BET: a card that went out keeps its bet, stake and odds as sent; only its last line says
        whether the book it named still has a good price (✅: at least OK_EDGE_PCT of edge against today's fair
        price) or not (⚠️). Edited when that changes, never pinging. Not for parlays, nor after a restart."""
        sent = op.sent_item
        if sent is None or op.render is None or not op.message_id or not hasattr(item, "worst_ok_price"):
            return
        price = next((pr for bk, pr, *_ in item.board if bk == sent.book), None)
        status = "ok" if price is not None and _printed(price) >= _printed(item.worst_ok_price()) else "moved"
        if status != op.status:
            op.status, op.edited = status, now
            self._discord(op.render(sent, first_seen=op.first_seen, status=status), op.message_id, op.url)

    def _close(self, key: str, now: float) -> None:
        op = self.open.pop(key)
        lasted = now - op.first_seen  # first seen -> first check where it was gone
        if op.prev and not op.message_id:
            # Handed over, and gone before its own card went up: the old card points at an alert that never
            # came, so it's the one marked GONE.
            self._discord(self.payload(op.arb, gone_after=lasted), *op.prev)
        if not op.sent:
            # Never went out (held for old odds, or Discord never took it): not an alert. An arb is logged with
            # why ("old data", "not sent"), so it isn't counted as alerted; a bet was never logged.
            print(f"  ❌ gone before it was sent: {self.label(op.arb)}", flush=True)
            for times, t in op.slots:   # the hourly-cap slots it took go back: nobody saw it
                if t in times:
                    times.remove(t)
            if not self.log_on_open:
                self._log(op, now, lasted, "old data" if op.deferred else "not sent")
            return
        print(f"  ❌ gone after {_fmt_secs(lasted)}: {self.label(op.arb)}", flush=True)
        if op.message_id:
            self._discord(self.payload(op.arb, gone_after=lasted), op.message_id, op.url)
        self.stats["closed"] += 1
        self.stats["open_seconds"] += lasted
        if not self.log_on_open:
            self._log(op, now, lasted)

    def _log(self, op: OpenArb, now: float, lasted: float | None, reason: str = "") -> None:
        if not self.log_path():
            return
        row = {
            "first_seen": _utc(op.first_seen),
            "gone_at": "" if lasted is None else _utc(now),
            "seconds_open": "" if lasted is None else round(lasted),
            **self.row(op),
            **({"reason": reason} if reason else {}),
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
            _webhook(self.url, {"username": BOT_NAME, "content": text[:1900]})
        except Exception as e:
            print(f"  ! Status message failed: {e}", file=sys.stderr)

    def send_card(self, payload: dict) -> None:
        emb = payload["embeds"][0]
        print(f"[status] {emb['title']}\n{emb.get('description', '')}", flush=True)
        if self.dry_run:
            return
        try:
            _webhook(self.url, payload)
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


def fail_reason(e: BaseException) -> str:
    """A failed request in a few words, for a health message: "timeouts", "HTTP 503", "can't connect"."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError) \
            or "timed out" in str(e).lower():
        return "timeouts"
    if isinstance(e, (urllib.error.URLError, ConnectionError)):
        return "can't connect"
    return "bad answers"


def kalshi_down_since() -> float | None:
    """When Kalshi stopped answering (unreachable, or it asked the bot to slow down), if it hasn't answered
    since: time.time(); None while it's fine."""
    return _KALSHI_STATE.get("down_since")


class Health:
    """Outage messages in the health channel (HEALTH_ALERTS), one per outage and one when it's over:
    - the Odds API not answering for a sport for ODDS_DOWN_MINUTES (that sport has no alerts meanwhile; its
      open alerts stay up, since nothing looked at them);
    - the sharp book (SHARP_BOOKS) missing from every game of a sport whose games other books price, for
      SHARP_DOWN_MINUTES: missing from the answers, not just an old price (an old stamp is normal). While it
      lasts, the sport is in sharp_down: its main-line +EV has no fair odds anyway, Kalshi-only bets wait,
      and props without a sharp price are at most medium confidence (find_evs);
    - Kalshi unreachable for KALSHI_DOWN_MINUTES while there are games before kickoff to use it on.
    Each check is off at 0 minutes; HEALTH_ALERTS=false keeps sharp_down working, without the messages."""

    def __init__(self, cfg: Config, status: "Status"):
        self.cfg, self.status = cfg, status
        self.odds_down: dict[str, list] = {}     # sport -> [first failure, why, said, last failure]
        self.sharp_gone: dict[str, float] = {}   # sport -> since when no sharp book prices any of its games
        self.sharp_last: dict[str, float] = {}   # sport -> when that was last seen
        self.sharp_down: set[str] = set()        # ...for SHARP_DOWN_MINUTES or more
        self.kalshi_said: float | None = None    # the start of the Kalshi outage that was said

    def say(self, text: str) -> None:
        if self.cfg.health_alerts:
            self.status.send(text)

    def _clock(self, t: float) -> str:
        return datetime.fromtimestamp(t, ZoneInfo(self.cfg.timezone)).strftime("%-I:%M %p")

    def odds(self, tried, answered: set[str], why: dict[str, str], now: float | None = None) -> None:
        """One pass's Odds API requests: the sports it asked for (main lines or props), the ones that
        answered at least once, and why the others failed. A failure more than twice ODDS_DOWN_MINUTES after
        the last one (the sport wasn't asked for in between: overnight) starts the clock again."""
        now = now or time.time()
        limit = self.cfg.odds_down_minutes * 60
        for sport in sorted(set(tried)):
            if sport in answered:
                down = self.odds_down.pop(sport, None)
                if down and down[2]:
                    self.say(f"✅ Odds API back for {short(sport)} after {round((now - down[0]) / 60)} min.")
                continue
            down = self.odds_down.setdefault(sport, [now, "no answer", False, now])
            if not down[2] and now - down[3] > 2 * limit:
                down[0] = now
            down[1], down[3] = why.get(sport, down[1]), now
            if limit and not down[2] and now - down[0] >= limit:
                down[2] = True
                self.say(f"⚠️ Odds API not answering for {short(sport)} since {self._clock(down[0])} ({down[1]}): "
                         f"no {short(sport)} alerts until it's back. Open alerts stay up.")

    def sharp(self, events: list[dict], checked, now: float | None = None) -> None:
        """One pass's main-line odds, for the sports that answered: is the sharp book in them?"""
        now = now or time.time()
        refs = set(sharp_refs(self.cfg))
        limit = self.cfg.sharp_down_minutes * 60
        if not refs or not limit:
            self.sharp_gone.clear()
            self.sharp_down.clear()
            return
        name = " / ".join(_title(k) for k in sharp_refs(self.cfg))
        for sport in sorted(set(checked)):
            priced = {bm["key"] in refs for ev in events if ev.get("sport_key") == sport
                      for bm in ev.get("bookmakers", []) if any(m.get("outcomes") for m in bm.get("markets", []))}
            if True in priced:   # it prices at least one game: up
                self.sharp_gone.pop(sport, None)
                self.sharp_last.pop(sport, None)
                if sport in self.sharp_down:
                    self.sharp_down.discard(sport)
                    self.say(f"✅ {name} prices back for {short(sport)}: +EV carries on as normal.")
                continue
            if not priced:
                continue   # no book prices anything there: says nothing about the sharp book
            since = self.sharp_gone.setdefault(sport, now)
            if sport not in self.sharp_down and now - self.sharp_last.get(sport, now) > 2 * limit:
                since = self.sharp_gone[sport] = now   # (last seen missing long ago, not since: a new look)
            self.sharp_last[sport] = now
            if sport not in self.sharp_down and now - since >= limit:
                self.sharp_down.add(sport)
                self.say(f"⚠️ No {name} prices for {short(sport)} for {round((now - since) / 60)} min (other books "
                         f"have them): {short(sport)} main-line +EV is paused (its open +EV cards close, and post again "
                         f"once {name} is back), props without a {name} price go out at medium confidence at most, "
                         f"arbs and outliers carry on.")

    def kalshi(self, pregame: bool, now: float | None = None) -> None:
        """After a pass's Kalshi lookups: has Kalshi been unreachable for KALSHI_DOWN_MINUTES? pregame: the
        pass had games before kickoff in a sport Kalshi lists (only then is it asked)."""
        now = now or time.time()
        since = kalshi_down_since()
        if since is None:
            if self.kalshi_said is not None:
                self.say(f"✅ Kalshi reachable again after {round((now - self.kalshi_said) / 60)} min: moneylines are "
                         f"checked against it again.")
                self.kalshi_said = None
            return
        limit = self.cfg.kalshi_down_minutes * 60
        if (self.kalshi_said is None and limit and pregame and kalshi_useful(self.cfg)
                and now - since >= limit):
            self.kalshi_said = since
            name = " / ".join(_title(k) for k in sharp_refs(self.cfg)) or "the sharp book"
            self.say(f"⚠️ Kalshi unreachable for {round((now - since) / 60)} min: moneyline +EV goes out on "
                     f"{name} alone.")


# --------------------------------------------------------------------------- +EV bets

def ev_key(event_id: str, market: str, line, outcome: str) -> str:
    """The alert key of a +EV or outlier bet (EVBet.key)."""
    return f"ev|{event_id}|{market}|{line}|{outcome}"


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
    sharp_book: str           # whose fair odds: "Pinnacle", "Pinnacle + Betfair", "consensus of DraftKings, ..."
    n_outcomes: int
    link: str = ""
    also: list[tuple[str, float]] = field(default_factory=list)  # other +EV books
    stake: float = 0.0
    sources_used: int = 1
    sources_total: int = 1
    # The sharp books' own fair win chance for this side and whose it is (with Kalshi blended in, fair_prob
    # isn't theirs alone); None / "" when no sharp book priced the line.
    sharp_prob: float | None = None
    sharp_name: str = ""
    left_out: tuple[str, float] | None = None   # a reference left out as the odd one out: (its name, points off)
    kalshi_only: bool = False        # priced by Kalshi alone: no sharp book lists this moneyline (KALSHI_ONLY)
    unit_size: float = 0.0           # dollars per unit (Config.unit()), for the card's "$15 (1.5u)"
    # The fair-odds references that apply to this line (top trust first: SHARP_BOOKS, then Kalshi on a
    # pre-game moneyline), each with its no-vig win chance for this side; None = no usable price.
    refs: list[tuple[str, float | None]] = field(default_factory=list)
    sharp_quotes: list[tuple[str, list[float]]] = field(default_factory=list)  # (book, [this side, other side(s)])
    # Every book: (book, price, EV%, link, ok). ok = passes the +EV filters (odds cap, not a stale line).
    board: list[tuple[str, float, float, str, bool]] = field(default_factory=list)
    hedge: list[tuple[str, str, float, str]] = field(default_factory=list)  # (outcome, book, price, link)
    hedge_pct: float = 0.0           # guaranteed profit % if the hedge is placed too
    related: list[str] = field(default_factory=list)  # other alerts open on the same game
    confidence: str = ""             # high / medium / low
    confidence_notes: list[str] = field(default_factory=list)
    first_fair_prob: float = 0.0     # fair probability when first alerted (to show movement)
    first_sharp_quotes: list[tuple[str, list[float]]] = field(default_factory=list)
    parlay_books: set[str] | None = None   # books this bet may go into a parlay at (None: all on the board)
    kalshi: tuple[float, float, float] | None = None   # Kalshi's (win chance, bid, ask) for this side
    age: float | None = None         # seconds since the book updated this price, when first sent
    found: tuple = ()                # live, once the live checks confirm it: (first check that found it, checks in a row)
    kept: bool = False               # shown only because its card is up (under MIN_CONFIDENCE, its book just
                                     # moved away from the others, or a confirmed bet's second source is only
                                     # missing this check): no new ping, not a new parlay's leg
    kalshi_room: float = 0.0         # a bet at Kalshi bigger than its order book holds at this price: the dollars
                                     # there (the stake is cut to fit and the card says so; 0 = enough)
    tier: str = ""                   # CONFIRMED: under MIN_EV_PCT, out because a second sharp source agrees
    confirm: tuple = ()              # ...that source: (its name on the card, its win chance, the usual edge floor,
                                     # the edge by the lower of the two chances); no chance or edge (None): the
                                     # card is up and the source has no price this check
    alt: bool = False                # the price is on the book's alternate line at this point (an "X+" ladder),
                                     # not its main one: the same bet, priced from main lines (book_offers)
    alt_books: set[str] = field(default_factory=set)   # books on the board whose price is their alternate line's
    hedge_alt: set[str] = field(default_factory=set)   # hedge outcomes priced on an alternate line
    tune_note: str = ""              # TUNE_ENABLED: why its stake was raised or cut by the bot's own record ("" = not)

    def worst_ok_price(self, min_edge_pct: float = OK_EDGE_PCT) -> float:
        """Lowest price that still leaves min_edge_pct of edge against the fair price."""
        return (1 + min_edge_pct / 100) / self.fair_prob

    @property
    def stake_label(self) -> str:
        return stake_text(self.stake, self.unit_size)

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
        return ev_key(self.event_id, self.market, self.line, self.outcome)

    @property
    def fingerprint(self) -> str:
        """Everything on the card that can change; a different value means the card gets edited."""
        hedge = ",".join(f"{o}:{bk}@{pr}" for o, bk, pr, _ in self.hedge)
        return (f"{self.key}|{self.book}@{self.price}|{self.fair_prob:.3f}|{self.stake}|{hedge}"
                + ("|alt" if self.alt else ""))

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
    """Fractional Kelly, capped at EV_MAX_STAKE_PCT of EV_BANKROLL, then scaled by mult
    (confidence, single source) so smaller-confidence bets are smaller even at the cap."""
    edge = (fair_prob * price - 1) / (price - 1)
    cap = cfg.ev_bankroll * cfg.ev_max_stake_pct / 100
    return round_stake(min(cfg.ev_bankroll * cfg.kelly_fraction * max(0.0, edge), cap) * mult, cap, cfg)


def round_stake(stake: float, cap: float, cfg: Config) -> float:
    """Round to ROUND_STAKES (else whole dollars, else cents) without ever going over cap."""
    stake = min(stake, cap)
    rs = cfg.round_stakes
    if rs > 0 and stake >= rs:
        r = round(stake / rs) * rs
        if r > cap:
            r = math.floor(cap / rs) * rs
        if r > 0:
            return float(r)
    r = float(round(stake)) if stake >= 1 else round(stake, 2)
    return min(r, float(math.floor(cap)) if cap >= 1 else round(cap, 2))


def source_weight(cfg: Config, source: str, prop: bool = False) -> float:
    """A fair-odds reference's weight in a blend: its SHARP_WEIGHTS (props: SHARP_WEIGHTS_PROPS, when
    set), else its trust-order default (SOURCE_WEIGHTS, OTHER_SOURCE_WEIGHT). 0 = not a reference at all."""
    mine = cfg.sharp_weights if not prop or cfg.sharp_weights_props is None else cfg.sharp_weights_props
    return mine.get(source, SOURCE_WEIGHTS.get(source, OTHER_SOURCE_WEIGHT))


def sharp_refs(cfg: Config, prop: bool = False) -> list[str]:
    """The SHARP_BOOKS that are fair-odds references for main lines (prop: props), as listed: every one
    with a weight over 0."""
    return [sk for sk in _csv(cfg.sharp_books) if source_weight(cfg, sk, prop) > 0]


def top_trust_first(srcs, weights: dict[str, float]) -> list[str]:
    """Fair-odds references, most trusted first: by weight, a tie by the trust order (SOURCE_WEIGHTS: Pinnacle
    first), then as listed. The first is the one blend_refs never leaves out."""
    return sorted(srcs, key=lambda s: (-weights[s], -SOURCE_WEIGHTS.get(s, OTHER_SOURCE_WEIGHT)))


def blend_refs(probs: dict[str, dict[str, float]], weights: dict[str, float], agree_pts: float | None,
               out_pts: float | None = None) -> tuple[dict[str, float] | None, list[str], tuple[str, float] | None]:
    """One fair price from several references' no-vig win chances (source -> outcome -> chance, the same
    outcomes each, top trust first), weighted, the weights shared out over the ones here.
    out_pts: with 3 or more, the one farthest from the middle (median) of the others is left out when
      it's more than this many win-% points away: one at most, the less trusted of a tie, and never the
      top one (if that's the odd one out, there's no fair price at all). None: nobody is left out.
      Nothing is remembered: a source left out counts again on the next check it's back within it.
    agree_pts: with two or more (after that), they must all be within this many win-% points of each
      other, or there's no fair price (None: not checked).
    Returns (the fair price or None, the sources in it, (the one left out, its points off) or None)."""
    srcs = list(probs)
    names = list(probs[srcs[0]])
    left_out = None
    if out_pts is not None and len(srcs) >= 3:
        off = {s: round(max(abs(probs[s][n] - statistics.median(probs[o][n] for o in srcs if o != s))
                            for n in names) * 100, 6) for s in srcs}
        worst = max(off.values())
        if worst > out_pts:
            odd = [s for s in srcs if off[s] == worst][-1]   # (a tie: the less trusted one goes)
            if odd == srcs[0]:
                return None, [], None   # the most trusted source is the odd one out: no fair price
            srcs.remove(odd)
            left_out = (odd, worst)
    if agree_pts is not None and len(srcs) >= 2 and any(   # (in points, rounded as above: 3.0 apart is within 3)
            round((max(probs[s][n] for s in srcs) - min(probs[s][n] for s in srcs)) * 100, 6) > agree_pts
            for n in names):
        return None, srcs, left_out   # the references disagree: no reliable fair price for this line
    total = sum(weights[s] for s in srcs)
    return {n: sum(probs[s][n] * weights[s] for s in srcs) / total for n in names}, srcs, left_out


class SharpFair(NamedTuple):
    """sharp_fair's answer for one game, per line: (market, line)."""
    fair: dict[tuple, dict[str, float]]                # outcome -> the sharp books' blended no-vig win chance
    names: dict[tuple, str]                            # whose: "Pinnacle", "Pinnacle + Betfair"
    n_used: dict[tuple, int]                           # how many sharp books that is
    raw_src: dict[tuple, dict[str, dict[str, float]]]  # each sharp book's prices (for display)
    titles: dict[str, str]                             # book key -> its name
    used: dict[tuple, dict[str, dict[str, float]]]     # each book in the fair price: its no-vig chances (top trust first)
    left_out: dict[tuple, tuple[str, float]]           # a book left out as the odd one out: (its key, points off)


def sharp_fair(ev: dict, cfg: Config, now: datetime, is_live: bool) -> SharpFair:
    """Fair (no-vig) probabilities per line for one game, from the sharp book(s), blended by
    source_weight (fair[(market, line)][outcome] = prob). A book with a weight of 0 for the line isn't
    used at all: a line only it prices has no fair price (so a prop falls back to the other books).
    Three or more that price a line: an odd one out is left out (OUTLIER_SOURCE_PTS, see blend_refs).
    The rest must agree within SHARP_DISAGREE_PCT (props: the _PROP_ settings)."""
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
            if is_alt(mkt["key"]) or not is_fresh(mkt, bm, now, is_live, cfg, _parse_time(ev["commence_time"]),
                                                  sharp=True):
                continue   # (alternate lines are never a fair price: see book_offers)
            groups: dict[tuple, dict[str, float]] = {}
            for oc in mkt.get("outcomes", []):
                price = float(oc.get("price") or 0)
                if price > 1.0:
                    k = (mkt["key"], _line_for(mkt["key"], oc, ev["home_team"]))
                    groups.setdefault(k, {})[oc["name"]] = price
            for k, outs in groups.items():
                if len(outs) >= 2 and source_weight(cfg, sk, is_prop(k[1])) > 0:
                    names = list(outs)
                    raw_src.setdefault(k, {})[sk] = outs
                    per_src.setdefault(k, {})[sk] = dict(
                        zip(names, devig([outs[n] for n in names], cfg.devig_method)))

    # Blend them into one fair price per line (weights renormalised over the sources present).
    fair: dict[tuple, dict[str, float]] = {}
    sharp_name: dict[tuple, str] = {}
    n_used: dict[tuple, int] = {}
    used: dict[tuple, dict[str, dict[str, float]]] = {}
    left_out: dict[tuple, tuple[str, float]] = {}
    for k, srcs in per_src.items():
        first = next(iter(srcs.values()))
        same = {sk: pr for sk, pr in srcs.items() if set(pr) == set(first)}  # same outcomes
        prop = is_prop(k[1])
        weights = {sk: source_weight(cfg, sk, prop) for sk in same}
        top_first = top_trust_first(same, weights)
        probs, srcs_in, out = blend_refs({sk: same[sk] for sk in top_first}, weights,
                                         cfg.sharp_disagree_prop_pct if prop else cfg.sharp_disagree_pct,
                                         cfg.outlier_source_prop_pts if prop else cfg.outlier_source_pts)
        if probs is None:
            continue
        fair[k] = probs
        sharp_name[k] = " + ".join(titles[sk] for sk in same if sk in srcs_in)
        n_used[k] = len(srcs_in)
        used[k] = {sk: same[sk] for sk in top_first if sk in srcs_in}
        if out:
            left_out[k] = out
    return SharpFair(fair, sharp_name, n_used, raw_src, titles, used, left_out)


@functools.lru_cache(maxsize=1 << 16)
def _novig(prices: tuple[float, ...], method: str) -> tuple[float, ...]:
    """devig, remembered: a prop check works out one consensus per book being judged, from the same prices."""
    return tuple(devig(list(prices), method))


def _vote(book: str) -> str:
    """The vote a book's prices count toward in a consensus (sister books share one)."""
    return next((min(g) for g in SISTER_BOOKS if book in g), book)


class Consensus(NamedTuple):
    """The market's fair price for one line, from consensus_fair."""
    probs: dict[str, float]              # outcome -> the median no-vig win chance
    books: tuple[tuple[str, ...], ...]   # each vote's books (keys): sister books share one vote
    spread: dict[str, float]             # outcome -> how far apart the votes are (win-% points), not
                                         # counting the one farthest from the median (with 4+ votes)
    full_spread: dict[str, float]        # ...counting every vote

    @property
    def votes(self) -> int:
        return len(self.books)


def consensus_fair(ev: dict, cfg: Config, now: datetime, is_live: bool, skip: set[str],
                   leave_out: str = "", sisters: bool = True) -> dict[tuple, Consensus]:
    """Median no-vig probability per line across books (when no sharp prices the line), for lines
    at least CONSENSUS_MIN_BOOKS votes price.
    leave_out: the book being judged, kept out of its own median (and so are its sister books).
    sisters: sister books (SISTER_BOOKS) are one vote, the average of their prices. Off for the
      main-line check of the sharp against the rest of the market."""
    out_vote = _vote(leave_out) if leave_out else None
    lines: dict[tuple, dict[str, dict[str, float]]] = {}
    for bm in ev.get("bookmakers", []):
        if bm["key"] in skip or (out_vote is not None and _vote(bm["key"]) == out_vote):
            continue
        for mkt in bm.get("markets", []):
            if is_alt(mkt["key"]) or not is_fresh(mkt, bm, now, is_live, cfg, _parse_time(ev["commence_time"])):
                continue   # (alternate lines don't vote: each book hangs them off its own main line)
            for oc in mkt.get("outcomes", []):
                price = float(oc.get("price") or 0)
                if price > 1.0:
                    k = (mkt["key"], _line_for(mkt["key"], oc, ev["home_team"]))
                    lines.setdefault(k, {}).setdefault(bm["key"], {})[oc["name"]] = price
    out = {}
    for k, books in lines.items():
        n_out = max(len(o) for o in books.values())
        full = {bk: o for bk, o in books.items() if len(o) == n_out >= 2}
        names = set(next(iter(full.values()))) if full else set()
        groups: dict[str, list[str]] = {}
        for bk, o in full.items():
            if set(o) == names:
                groups.setdefault(_vote(bk) if sisters else bk, []).append(bk)
        if not groups or len(groups) < cfg.consensus_min_books:
            continue
        probs = []
        for bks in groups.values():
            each = [dict(zip(full[bk], _novig(tuple(full[bk].values()), cfg.devig_method))) for bk in bks]
            probs.append({n: sum(p[n] for p in each) / len(each) for n in names})
        med, spread, full_spread = {}, {}, {}
        for n in names:
            xs = sorted(pr[n] for pr in probs)
            m = len(xs) // 2
            med[n] = xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2
            full_spread[n] = round((xs[-1] - xs[0]) * 100, 6)
            if len(xs) >= 4:   # one odd book (a thin exchange quote) can't swing it on its own
                xs.remove(max(xs, key=lambda x: abs(x - med[n])))
            spread[n] = round((xs[-1] - xs[0]) * 100, 6)
        total = sum(med.values())
        out[k] = Consensus({n: v / total for n, v in med.items()}, tuple(tuple(b) for b in groups.values()),
                           spread, full_spread)
    return out


CONFIDENCE_BADGE = {"high": "🟢 High", "medium": "🟡 Medium", "low": "🟠 Low"}
PROP_CONFIDENT_HOURS = 12   # prop lines mature later: only this close to kickoff do they get the point
# A line no sharp prices is judged on how closely the other books agree (win-% points apart, not
# counting the one farthest out) and how many sportsbooks price it.
CONSENSUS_AGREE = 2.0       # this close: 2 points
CONSENSUS_ROUGHLY = 4.0     # this close: 1 point
CONSENSUS_SPORTSBOOKS = 6   # this many sportsbooks on the line (the one being judged too): 1 point


class Agreement(NamedTuple):
    """How much the market agrees on a line no sharp prices (rate_confidence's `consensus`)."""
    spread: float          # win-% points between the other books, not counting the one farthest out
    full_spread: float     # ...counting every one
    sportsbooks: int       # sportsbooks pricing the line, this one included (exchanges don't count)
    cant_bet: int = 0      # of those, ones you can't bet at (not in MY_BOOKS)


def rate_confidence(hold: float | None, gap: float | None, hours_to_start: float, ev_pct: float,
                    prop: bool, move: float | None = None, kalshi_gap: float | None = None,
                    confident_hours: float = 24, consensus: Agreement | None = None,
                    missed: list[str] | None = None) -> tuple[str, list[str]]:
    """How much to trust a +EV price. Points for: a tight sharp market, the other books agreeing
    with the sharp, a game close enough that the sharp line has matured (main lines: within
    CONFIDENT_HOURS, so tonight's and tomorrow's games can be high confidence; props: 12 hours),
    a believable edge, and the sharp line moving toward this side (sharp money agrees; moving away
    costs a point).
    consensus: when no sharp prices the line, fair value is the other books' median, so the points
    for the sharp's margin and agreement go to how closely those books agree (2) and whether 6+
    sportsbooks price it (1). At most 5, just the "high" bar.
    missed: when given, a short reason for each of those points (and the hours and edge points)
    it didn't get is added, for the console."""
    pts, notes = 0, []
    miss = missed.append if missed is not None else (lambda _: None)
    if move is not None and abs(move) >= SHARP_MOVE_PTS:
        if move > 0:
            pts += 1
            notes.append(f"sharp line moving this way (+{move:.1f} pts)")
        else:
            pts -= 1
            notes.append(f"sharp line moving against it ({move:.1f} pts)")
    tight, ok = (6.5, 9.0) if prop else (3.5, 6.0)
    if consensus is not None:
        a = consensus

        def within(limit: float) -> str:   # said honestly when the farthest book was left out
            return (f"within {a.full_spread:.1f}% win chance" if a.full_spread <= limit
                    else f"all but one within {a.spread:.1f}% win chance")
        if a.spread <= CONSENSUS_AGREE:
            pts += 2
            notes.append(f"other books agree ({within(CONSENSUS_AGREE)})")
        else:
            miss("books disagree")
            if a.spread <= CONSENSUS_ROUGHLY:
                pts += 1
                notes.append(f"other books roughly agree ({within(CONSENSUS_ROUGHLY)})")
            else:
                notes.append(f"other books disagree ({a.full_spread:.0f}% win chance apart)")
        cant = f" ({a.cant_bet} you can't bet at)" if a.cant_bet else ""
        if a.sportsbooks >= CONSENSUS_SPORTSBOOKS:
            pts += 1
            notes.append(f"{a.sportsbooks} sportsbooks price it{cant}")
        else:
            miss(f"under {CONSENSUS_SPORTSBOOKS} sportsbooks")
            notes.append(f"only {a.sportsbooks} sportsbook{'' if a.sportsbooks == 1 else 's'} price it{cant}")
    else:
        if hold is None:
            pts += 1
        elif hold <= tight:
            pts += 2
            notes.append(f"tight sharp market ({hold:.1f}% margin)")
        elif hold <= ok:
            pts += 1
        else:
            notes.append(f"wide sharp market ({hold:.1f}% margin)")
        if gap is None:
            pts += 1
        elif gap <= 3:
            pts += 2
            notes.append("other books agree")
        elif gap <= 6:
            pts += 1
        else:
            notes.append(f"other books disagree by {gap:.0f} pts")
    hours = PROP_CONFIDENT_HOURS if prop else confident_hours
    if hours_to_start <= hours:
        pts += 1
    else:
        miss(f"over {hours:g} hours out")
        notes.append("early line (less tested)")
    if ev_pct <= 12:
        pts += 1
    else:
        miss("edge over 12%")
        notes.append("unusually big edge, double-check it")
    if kalshi_gap is not None and kalshi_gap <= 2:
        pts += 1
        notes.append("Kalshi agrees")
    return ("high" if pts >= 5 else "medium" if pts >= 3 else "low"), notes


class SharpHistory:
    """Recent sharp fair prices per bet, to see which way the sharp line is moving."""

    def __init__(self, window_minutes: int = 60):
        self.window = timedelta(minutes=window_minutes)
        self.points: dict[tuple, list[tuple[datetime, float]]] = {}
        # Main lines, for confirmed pre-game bets: every check's sighting, repeats merged into
        # [first seen, last seen, win chance], kept up to 3 windows (pre-game checks can be an hour or
        # more apart, and `points` forgets a price once it's a window old).
        self.seen: dict[tuple, list[list]] = {}

    def record(self, key: tuple, now: datetime, prob: float) -> float | None:
        """Store this sighting; return the move (in win-% points) since the oldest one in the window."""
        pts = [x for x in self.points.get(key, []) if now - x[0] <= self.window]
        move = (prob - pts[0][1]) * 100 if pts else None
        if not pts or pts[-1][1] != prob:
            pts.append((now, prob))
        self.points[key] = pts
        if key[1] in MAIN_MARKETS:
            runs = [r for r in self.seen.get(key, []) if now - r[1] <= self.window * 3]
            if runs and runs[-1][2] == prob:
                runs[-1][1] = max(runs[-1][1], now)
            else:
                runs.append([now, now, prob])
            self.seen[key] = runs
        return move

    def before(self, key: tuple, now: datetime) -> float | None:
        """A main line's sharp win chance a window ago, from the checks before this one (call it after
        this check's record(), which forgets prices last seen over 3 windows ago): the last price seen
        by then, or the first one seen since if the line is newer. None: no earlier sighting at all (the
        first check after a restart, a new line, or none in 3 windows)."""
        runs = [r for r in self.seen.get(key, []) if r[0] < now]
        if not runs:
            return None
        cut = now - self.window
        return next((r for r in reversed(runs) if r[0] <= cut), runs[0])[2]

    def prune(self, now: datetime) -> None:
        self.points = {k: v for k, v in self.points.items() if v and now - v[-1][0] <= self.window * 3}
        seen = {}
        for k, runs in self.seen.items():
            runs = [r for r in runs if now - r[1] <= self.window * 3]
            old = [i for i, r in enumerate(runs) if r[0] <= now - self.window]   # only the last of these counts now
            if runs:
                seen[k] = runs[old[-1]:] if old else runs
        self.seen = seen


class PriceHistory:
    """Per book and line, across scans: the price and market fair price when the book was last
    in line with the market (its anchor), and since when it's been out in front. Tells a stale
    book (it sits still while the market moves) from a fast one (it moves away from a market
    that hasn't caught up yet: in a live game, after a goal). Kept for a day, because pre-game
    lines can go hours between checks."""

    def __init__(self, keep_minutes: int = 26 * 60):
        self.keep = timedelta(minutes=keep_minutes)
        # key -> (when, leading since, anchor, (price, fair) at that look: +EV props only)
        self.seen: dict[tuple, tuple[datetime, datetime | None, tuple | None, tuple | None]] = {}
        # +EV props: the sharp's last no-vig win chance on each prop line side it priced, and the game's
        # kickoff. Kept until kickoff: a line the sharp took down still has its last word.
        self.sharp: dict[tuple, tuple[float, datetime]] = {}
        # For the console, filled by find_evs (props no sharp prices) and emptied by scan_props:
        self.held: dict[str, int] = {}      # prices that would have alerted, held back: "first look" / "moved first"
        self.missed: list[list[str]] = []   # lines that cleared the edge but weren't high confidence: why not

    def get(self, key: tuple):
        return self.seen.get(key)

    def put(self, key: tuple, now: datetime, since: datetime | None, anchor: tuple | None,
            last: tuple | None = None) -> None:
        self.seen[key] = (now, since, anchor, last)

    def prune(self, now: datetime) -> None:
        self.seen = {k: v for k, v in self.seen.items() if now - v[0] <= self.keep}
        self.sharp = {k: v for k, v in self.sharp.items() if v[1] > now}


def candidate(ev: dict, k: tuple, name: str, point, book: str, price: float, fair_p: float, src: str,
              kalshi=None) -> SimpleNamespace:
    """A price the candidate log notes before a bet was made of it (find_evs, find_outliers): the same
    fields as an EVBet's that the log reads."""
    return SimpleNamespace(key=ev_key(ev["id"], k[0], k[1], name), event_id=ev["id"],
                           sport=ev.get("sport_title", ev.get("sport_key", "")), sport_key=ev.get("sport_key", ""),
                           matchup=f"{ev['away_team']} @ {ev['home_team']}", market=k[0], line=k[1], outcome=name,
                           point=point, book=book, price=price, fair_prob=fair_p, sharp_book=src, confidence="",
                           kalshi=kalshi, refs=[], ev_pct=(fair_p * price - 1) * 100)


def find_evs(events: list[dict], cfg: Config, now: datetime | None = None,
             history: SharpHistory | None = None, kalshi: dict | None = None,
             prices: PriceHistory | None = None, keep: set[str] | frozenset = frozenset(),
             confirm_misses: dict[str, int] | None = None, alt_seen: set | None = None,
             sharp_down: set[str] | frozenset = frozenset(), rejects: list | None = None) -> list[EVBet]:
    """+EV prices vs the sharp book's fair odds. With CONSENSUS_MIN_BOOKS (props), a line no sharp
    prices is judged against the median of the OTHER books instead (the book being judged, and its
    sister books, never count toward its own fair price), and rated on how much they agree.
    prices: those lines' price history across checks. A book that just moved away from the others
      (it may have seen injury or lineup news first) is held back, the same test outliers use, and
      so is a line never seen before: it waits one check. It also keeps the sharp's last price on
      each prop line until kickoff: a line the sharp took down is skipped when that price says
      there's no edge, and is never a lock until the sharp prices it again.
    keep: keys of bets with a card up (open or restored). They skip the "never seen before" wait (a
      restart forgets the history), and on a line no sharp prices they stay while they're at least
      medium confidence (a new one still needs MIN_CONFIDENCE): there are no points to spare there,
      so one book pulling its line mustn't post a false GONE and then a new alert. Nor must their
      book moving further away from the others: such a bet stays up as a quiet 🟡 card instead.
    Confirmed pre-game bets (PREGAME_CONFIRMED_EV_PCT): a moneyline, spread or total of a game that
      starts within CONFIDENT_HOURS may clear a lower edge than MIN_EV_PCT when a second sharp source
      agrees with the sharp book (confirmed_by) and the edge clears it by both of them, the sharp line
      isn't moving against it (since a check a window ago, or the last check when they're further
      apart: with a history, a line never seen before waits one check), and it's high confidence
      whatever MIN_CONFIDENCE says. Every other check still applies. One that clears MIN_EV_PCT is a
      normal bet. One in `keep` whose only misses are CONFIRM_MISSING (a source missing this check,
      not disagreeing) stays up quietly. confirm_misses, when given, counts the ones that cleared the
      lower edge but not the rest, by why (for the console).
    Kalshi (pre-game moneylines, both teams with a usable quote): blended into the sharp books' fair
      odds (KALSHI_BLEND), or the fair odds by itself on a game no sharp book lists (KALSHI_ONLY).
    Alternate lines are prices to bet only, at exactly a line a fair price is for (book_offers): bet.alt.
      alt_seen, when given, collects the ones that had a fair price at their line (for the console).
    sharp_down: sports the sharp book has been missing from for SHARP_DOWN_MINUTES (Health): no Kalshi-only
      bets there, and a line no sharp prices is at most medium confidence.
    rejects, when given, collects (candidate, why) for the candidate log (CANDIDATE_LOG_FILE): each bet that
      cleared the edge and a quality check then stopped, and the best price on each side that came within 1
      point of the bar without clearing it ("under bar")."""
    if not cfg.ev_enabled:
        return []
    now = now or datetime.now(timezone.utc)
    sharp = _csv(cfg.sharp_books)
    refs_for = {prop: sharp_refs(cfg, prop) for prop in (False, True)}   # the ones with a weight, main lines / props
    # Games a sharp book lists a moneyline for, and the sports it does that in here: there it's up, so a
    # game it doesn't list is one it skipped, not an outage (a Kalshi-only bet needs that).
    sharp_ml = {ev["id"] for ev in events for bm in ev.get("bookmakers", []) if bm["key"] in refs_for[False]
                and any(m.get("key") == "h2h" and m.get("outcomes") for m in bm.get("markets", []))}
    sharp_up = {ev.get("sport_key") for ev in events if ev["id"] in sharp_ml}
    sharp_title = sharp[0].title() if sharp else "Sharp"
    allowed = cfg.ev_allowed()
    out: list[EVBet] = []

    def miss(why: str) -> None:   # a bet only the confirmed floor let in that didn't go out, and why
        if confirm_misses is not None:
            confirm_misses[why] = confirm_misses.get(why, 0) + 1

    def reject(item, why: str) -> None:   # (for the candidate log)
        if rejects is not None:
            rejects.append((item, why))

    for ev in events:
        is_live = _parse_time(ev["commence_time"]) <= now
        if (cfg.live_only and not is_live) or (is_live and not cfg.ev_live):
            continue
        books = {bm["key"]: bm for bm in ev.get("bookmakers", [])}
        hours = (_parse_time(ev["commence_time"]) - now).total_seconds() / 3600

        sf = sharp_fair(ev, cfg, now, is_live)
        fair, sharp_name, n_used, raw_src, titles = sf[:5]
        n_total = {k: len(refs_for[is_prop(k[1])]) for k in fair}
        # The rest of the market, as a sanity check on the sharp's price.
        market = consensus_fair(ev, replace(cfg, consensus_min_books=3), now, is_live, set(sharp), sisters=False)
        if history:
            for k, probs in fair.items():
                for name, prob in probs.items():
                    history.record((ev["id"], k[0], k[1], name), now, prob)
        if prices is not None:
            kickoff = _parse_time(ev["commence_time"])
            for k, probs in fair.items():
                if is_prop(k[1]):
                    for name, prob in probs.items():
                        prices.sharp[(ev["id"], k[0], k[1], name)] = (prob, kickoff)
        holds = {}
        for k, srcs in raw_src.items():
            first = next(iter(srcs.values()))
            holds[k] = (sum(1 / x for x in first.values()) - 1) * 100
        min_ev = max(cfg.min_ev_pct, cfg.sport_min_ev.get(ev.get("sport_key", ""), 0))
        # A confirmed pre-game bet's lower edge (0: not for this game).
        floor = cfg.confirmed_floor(ev.get("sport_key", "")) if not is_live and hours <= cfg.confident_hours else 0.0
        own: dict[str, dict[tuple, Consensus]] = {}   # book -> the other books' consensus (it's left out)
        agreeing: dict[str, dict[tuple, Consensus]] = {}   # book -> the consensus that may confirm its bet
        # Kalshi's own price, a fair-odds reference on a pre-game moneyline when both teams have a usable
        # quote (kalshi_fair's spread, size and add-up checks): {team: its win chance}.
        ml, quotes = ("h2h", None), (kalshi or {}).get(ev["id"], {})
        kp = ({t: quotes[t][0] for t in (ev["home_team"], ev["away_team"])}
              if not is_live and kalshi_applies(cfg, ev.get("sport_key", ""), "h2h", is_live)
              and ev["home_team"] in quotes and ev["away_team"] in quotes else None)
        # KALSHI_BLEND: it goes into that line's fair odds with the sharp books' (by weight), for the edge,
        # the stake and the card only. A bet at Kalshi keeps the sharps' (Kalshi can't confirm itself),
        # and every check below still uses the sharps' own price.
        # With 3+ references (2+ sharp books and Kalshi) an odd one out is left out, as in sharp_fair (only
        # if sharp_fair left none out: one per line), and the rest must agree, or the line is skipped. With
        # Kalshi and one sharp book, Kalshi's gap to it (below) is the test, as before.
        blend: dict[tuple, dict[str, float]] = {}
        blend_name: dict[tuple, str] = {}
        blend_out: dict[tuple, tuple[str, float]] = {}   # one the blend left out (Kalshi, or a sharp book)
        blend_dead: set[tuple] = set()   # lines the references with Kalshi in disagree on: no fair price, but a
                                         # bet at Kalshi keeps the sharps' own (Kalshi can't veto its own price)
        if cfg.kalshi_blend and kp and ml in fair and set(fair[ml]) == set(kp) and source_weight(cfg, "kalshi") > 0:
            refs_ml = {**sf.used[ml], "kalshi": kp}
            weights = {s: source_weight(cfg, s) for s in refs_ml}
            mixed, srcs_in, odd = blend_refs({s: refs_ml[s] for s in top_trust_first(refs_ml, weights)},
                                             weights, cfg.sharp_disagree_pct if len(refs_ml) >= 3 else None,
                                             None if ml in sf.left_out else cfg.outlier_source_pts)
            if mixed is None:
                blend_dead.add(ml)   # they disagree: no fair price for this line (except a bet at Kalshi's)
            else:
                if odd:
                    blend_out[ml] = odd
                if "kalshi" in srcs_in:
                    blend[ml] = mixed
                    blend_name[ml] = " + ".join([titles[sk] for sk in sharp if sk in sf.used[ml] and sk in srcs_in]
                                                + ["Kalshi"])
        # KALSHI_ONLY: a moneyline no sharp book lists at all (absent, not just old: an old stamp means a market
        # taken down) in a sport it's up in, priced by Kalshi alone. The market check below then compares it
        # with 3+ sportsbooks, exchanges left out (Kalshi mustn't check itself); never a bet at Kalshi.
        alone: dict[tuple, dict[str, float]] = {}
        alone_check = None
        if (cfg.kalshi_only and kp and ml not in fair and ev["id"] not in sharp_ml and ev.get("sport_key") in sharp_up
                and ev.get("sport_key") not in sharp_down and source_weight(cfg, "kalshi") > 0):
            alone_check = consensus_fair(ev, replace(cfg, consensus_min_books=3), now, is_live, set(sharp) | EXCHANGES,
                                         sisters=False).get(ml)
            if alone_check is not None and set(alone_check.probs) == set(kp):
                alone[ml] = kp

        # Every soft-book price that beats fair by enough (and every price, for the board).
        offers: dict[tuple, list[tuple]] = {}
        near: dict[tuple, tuple] = {}   # (line, side) -> (price, candidate): the best price just under the bar
        boards: dict[tuple, list[tuple[str, float, float, str, bool]]] = {}
        alt_board: dict[tuple, set[str]] = {}   # (line, side) -> the books whose board price is an alternate line's
        for key, bm in books.items():
            if key in sharp or (allowed and key not in allowed) or not cfg.bettable(key, ev):
                continue
            if cfg.consensus_min_books:
                own[key] = consensus_fair(ev, cfg, now, is_live, set(sharp), leave_out=key)
            mains = None   # its main lines' prices, {(line, side): price}, when an alternate line is its offer
            # Its fresh prices, one per line and side (an alternate line at the same point: the better price).
            for mkt, oc, k, alt, stamp in book_offers(bm, ev, now, is_live, cfg):
                if not cfg.alerts_side(k[1], oc["name"]):
                    continue   # a prop Under with PROP_SIDES=over (the fair odds above still used it)
                if k in blend_dead and key != "kalshi":
                    continue
                price = float(oc["price"])
                cons = None if k in fair or k in alone else own.get(key, {}).get(k)   # no sharp price: the other books
                p = ((blend[k] if k in blend and key != "kalshi" else fair[k]).get(oc["name"]) if k in fair
                     else alone[k].get(oc["name"]) if k in alone else cons.probs.get(oc["name"]) if cons else None)
                if p is None:
                    continue
                if alt and alt_seen is not None:
                    alt_seen.add((ev["id"], k, oc["name"], key))
                ev_pct = (p * price - 1) * 100
                link = book_link(ev, bm, mkt, oc, cfg)
                # (Kalshi's price can't be both the fair odds and the bet.)
                ok = price <= cfg.ev_max_odds and ev_pct <= cfg.max_ev_pct and not (k in alone and key == "kalshi")
                waiting = False
                if cons and prices is not None:
                    # A book that just moved away from the others makes ITS price look +EV against a
                    # median that hasn't caught up: the outliers' test, at the +EV bar. mine/theirs
                    # are unknown (a prop's stamp covers every player), so it works from anchors kept
                    # across checks; the first look anchors too, so a jump after it is measured, and
                    # so does each look's price, so a book that moves again is held again.
                    # (An alternate line's price is judged by the book's main line at that point, when it has
                    # one, as outliers do: the main line is what the book moved, or didn't.)
                    main_p = price
                    if alt:
                        if mains is None:
                            mains = {(k2, o2["name"]): float(o2["price"]) for _, o2, k2, _, _ in book_offers(
                                {**bm, "markets": [m for m in bm.get("markets", []) if not is_alt(m["key"])]},
                                ev, now, is_live, cfg)}
                        main_p = mains.get((k, oc["name"]))
                    gp = price if main_p is None else main_p
                    hkey = (ev["id"], k, key, oc["name"])
                    prev = prices.get(hkey)
                    since, anchor = _leading(prev, now, gp, p, (p * gp - 1) * 100, cfg, None, [], is_live, bar=min_ev)
                    prices.put(hkey, now, since, anchor or (gp, p), (gp, p))
                    carded = ev_key(ev["id"], k[0], k[1], oc["name"]) in keep
                    held = "moved first" if since is not None else "first look" if prev is None and not carded else ""
                    if held == "moved first" and carded:
                        # Its card is up: dropping it would post a false GONE, then a new alert once
                        # the hold is over. It stays, as a quiet 🟡 card, while the others can follow.
                        waiting = True
                    elif held:
                        if ok and ev_pct >= min_ev:
                            prices.held[held] = prices.held.get(held, 0) + 1
                        if ok and rejects is not None and min_ev - 1 <= ev_pct <= cfg.max_ev_pct:
                            reject(candidate(ev, k, oc["name"], oc.get("point"), bm.get("title", key), price, p,
                                             "consensus of " + str(cons.votes) + " other books"), held)
                        ok = False
                boards.setdefault((k, oc["name"]), []).append((bm.get("title", key), price, ev_pct, link, ok))
                if alt:
                    alt_board.setdefault((k, oc["name"]), set()).add(bm.get("title", key))
                if price > cfg.ev_max_odds or not ok:
                    continue
                low = min(min_ev, floor) if floor and k[0] in MAIN_MARKETS and k in fair else min_ev
                if low <= ev_pct <= cfg.max_ev_pct:
                    offers.setdefault((k, oc["name"]), []).append(
                        (price, bm.get("title", key), link, oc.get("point"), key, stamp, p, cons, waiting, alt))
                elif rejects is not None and low - 1 <= ev_pct < low and price > near.get((k, oc["name"]), (0,))[0]:
                    src = (blend_name[k] if k in blend and key != "kalshi" else sharp_name[k] if k in fair
                           else f"Kalshi (no {sharp_title} price)" if k in alone
                           else f"consensus of {cons.votes} other books")
                    near[(k, oc["name"])] = (price, candidate(ev, k, oc["name"], oc.get("point"), bm.get("title", key),
                                                              price, p, src))
        for side, (_, cand) in near.items():
            if side not in offers:   # (one over the bar for this side is a candidate itself)
                reject(cand, "under bar")

        for (k, name), lst in offers.items():
            lst.sort(key=lambda o: (-o[0], o[1]))   # best price; ties by book name, so it's stable
            kq = kalshi.get(ev["id"], {}).get(name) if kalshi and k[0] == "h2h" else None
            if kq is not None:   # a Kalshi price its own order book no longer has isn't an offer
                lst = [o for o in lst if o[4] != "kalshi" or kalshi_still_there(kq, o[0], cfg)]
                if not lst:
                    continue
            price, book, link, point, book_key, stamp, p, cons, waiting, alt = lst[0]
            # Under MIN_EV_PCT: only the confirmed floor let it in, so it must be confirmed below. Over it:
            # a normal bet, and the other books listed with it are the ones over it too, as always.
            # (Where the confirmed tier applies, a bet with Kalshi blended in must clear MIN_EV_PCT by the
            # sharp books' own price too, or it's a confirmed bet: the tier judges the sharps' price.)
            p_sh = fair[k].get(name) if floor and k in fair else None
            low_p = lambda q: min(q, p_sh) if p_sh is not None else q
            tier = CONFIRMED if (low_p(p) * price - 1) * 100 < min_ev else ""
            if not tier:
                lst = [o for o in lst if (low_p(o[6]) * o[0] - 1) * 100 >= min_ev]
            kalshi_alone = k in alone
            from_sharp = cons is None and not kalshi_alone
            blended = from_sharp and k in blend and book_key != "kalshi"
            left_out = None   # a reference left out of the fair price as the odd one out: (key, points off)
            if from_sharp:
                fair_k, src, n_src, n_all = fair[k], sharp_name[k], n_used[k], n_total[k]
                if blended:
                    src = blend_name[k]
                # (A bet at Kalshi uses the sharp books' price alone: the blend's choice doesn't apply.)
                left_out = (blend_out.get(k) if book_key != "kalshi" else None) or sf.left_out.get(k)
            elif kalshi_alone:
                fair_k, src, n_src, n_all = alone[k], f"Kalshi (no {sharp_title} price)", 1, 1
            else:   # the median of the OTHER books (this one left out), named on the card
                fair_k, n_src = cons.probs, cons.votes
                src, n_all = "consensus of " + ", ".join(
                    "/".join(books[b].get("title", b) for b in vote) for vote in cons.books), n_src
            # The fair-odds references for this line, top trust first, each with its no-vig win chance for
            # this side (None: no usable price), for the card's Sources line. (Only the count of SHARP_BOOKS
            # that priced it, n_src of n_all, sizes the stake.) Kalshi's quote can't be one for a bet at Kalshi.
            refs = [(titles.get(sk) or _title(sk),
                     _side_prob(raw_src.get(k, {}).get(sk), name, fair_k, cfg) if from_sharp else None)
                    for sk in refs_for[is_prop(k[1])]]
            # (Kalshi is one only before the game, with a weight: as for SHARP_BOOKS, 0 means not a reference.)
            if (book_key != "kalshi" and not is_live and source_weight(cfg, "kalshi") > 0
                    and kalshi_applies(cfg, ev.get("sport_key", ""), k[0], is_live)):
                refs.append(("Kalshi", kq[0] if kq is not None else None))
            bet = EVBet(
                event_id=ev["id"], sport=ev.get("sport_title", ev.get("sport_key", "")),
                sport_key=ev.get("sport_key", ""), matchup=f"{ev['away_team']} @ {ev['home_team']}",
                home_team=ev["home_team"], away_team=ev["away_team"],
                commence_time=ev["commence_time"], is_live=is_live,
                market=k[0], line=k[1], outcome=name, point=point,
                book=book, price=price, link=link,
                fair_prob=p, sharp_book=src, n_outcomes=len(fair_k),
                also=[(b, pr) for pr, b, *_ in lst[1:]],
                sources_used=n_src, sources_total=n_all, unit_size=cfg.unit(),
                sharp_prob=fair_k[name] if from_sharp else None, sharp_name=sharp_name[k] if from_sharp else "",
                left_out=left_out and (titles.get(left_out[0]) or _title(left_out[0]), left_out[1]),
                sharp_quotes=[(titles[sk], [outs[name]] + [pr for n, pr in outs.items() if n != name])
                              for sk, outs in raw_src.get(k, {}).items() if name in outs],
                refs=refs,
                board=sorted(boards.get((k, name), []), key=lambda r: (-r[1], r[0])),
                age=max(0.0, (now - _parse_time(stamp)).total_seconds()) if stamp else None, alt=alt,
                alt_books=alt_board.get((k, name), set()),
            )
            # Quality checks: skip prices whose fair value is shaky, rate the rest. They judge the sharp
            # books' own price (with Kalshi blended in, bet.fair_prob isn't theirs alone).
            p_own = bet.sharp_prob if from_sharp else bet.fair_prob
            hold = holds.get(k) if from_sharp else None
            gap = None
            if from_sharp and k in market and name in market[k].probs:
                gap = abs(market[k].probs[name] - p_own) * 100
            elif kalshi_alone:   # (it exists: no Kalshi-only line without it)
                gap = abs(alone_check.probs[name] - p_own) * 100
            if hold is not None and hold > cfg.max_sharp_hold_pct:
                reject(bet, "sharp hold")
                continue
            if gap is not None and gap > cfg.sharp_consensus_max_gap:
                reject(bet, "market gap")
                continue  # sharp and market far apart: one side is stale, can't trust either
            # Kalshi's exchange price is a second opinion on moneylines: if it disagrees with the
            # sharp book by more than KALSHI_MAX_GAP points, or says the price isn't good, skip it.
            kgap = None
            if kq is not None:
                bet.kalshi = kq
                if book_key != "kalshi" and not kalshi_alone:   # (at Kalshi, its quote only confirmed the price above)
                    kgap = round(abs(bet.kalshi[0] - p_own) * 100, 6)
                    if kgap > kalshi_gap_limit(cfg, ev.get("sport_key", ""), hours):
                        if tier:
                            miss("Kalshi too far off")
                        reject(bet, "kalshi gap")
                        continue
                    if bet.kalshi[0] * price < 1:
                        if tier:
                            miss("Kalshi says the price isn't good")
                        reject(bet, "kalshi no")
                        continue
            agree, missed, doubt = None, None, ""
            if cons is not None:
                missed = []
                sportsbooks = [v for v in cons.books if any(b not in EXCHANGES for b in v)]
                agree = Agreement(cons.spread[name], cons.full_spread[name],
                                  len(sportsbooks) + (book_key not in EXCHANGES),
                                  sum(not any(cfg.bettable(b, ev) for b in v) for v in sportsbooks))
                # No sharp price on this exact line doesn't mean the sharp has no opinion: it may price
                # the same player at another point (US books often hang a different line), or have
                # taken this one down (news). Either way the other books may be behind.
                doubt, cap = _sharp_doubt(fair, k, name, sharp_title)
                if cap is not None and (cap * price - 1) * 100 < min_ev:
                    if prices is not None:
                        prices.missed.append([f"{sharp_title}'s other line says no edge"])
                    reject(bet, "sharp other line")
                    continue   # Over a higher point can't be likelier than the sharp's Over a lower one
                why = f"{sharp_title} has another line"
                # Its last price on this exact line, until kickoff (prop checks can be hours apart).
                last = prices.sharp.get((ev["id"], k[0], k[1], name)) if prices is not None else None
                if last is not None:
                    if (last[0] * price - 1) * 100 < min_ev:
                        prices.missed.append([f"{sharp_title}'s last price says no edge"])
                        reject(bet, "sharp last price")
                        continue
                    if not doubt:   # never a lock until the sharp prices the line again
                        doubt = why = f"{sharp_title} took this line down"
            move = history.record((ev["id"], k[0], k[1], name), now, p_own) if history and from_sharp else None
            bet.confidence, bet.confidence_notes = rate_confidence(hold, gap, hours, bet.ev_pct, is_prop(k[1]), move,
                                                                   kgap, cfg.confident_hours, agree, missed)
            if kalshi_alone:   # one exchange's price is the whole fair price: at most medium, never a lock
                bet.confidence = "medium" if bet.confidence == "high" else bet.confidence
                bet.confidence_notes.append(f"only Kalshi prices it (no {sharp_title} line)")
                bet.kalshi_only = True
            if doubt:   # at most medium: never a lock
                bet.confidence = "medium" if bet.confidence == "high" else bet.confidence
                bet.confidence_notes.append(doubt)
                missed.append(why)
            if cons is not None and ev.get("sport_key") in sharp_down:   # the sharp is missing from the whole sport
                bet.confidence = "medium" if bet.confidence == "high" else bet.confidence
                bet.confidence_notes.append(f"no {sharp_title} prices in this sport right now")
                missed.append(f"{sharp_title} down")
            if waiting:   # its book just moved away from the others: up only because its card is
                bet.confidence = "medium" if bet.confidence == "high" else bet.confidence
                bet.confidence_notes.append(f"{book} just moved its price away from the other books")
                missed.append("book moved first")
            if missed and prices is not None:
                prices.missed.append(missed)   # cleared the edge, not high: why (for tuning the bars)
            if tier:
                if k[0] != "h2h" and book_key not in agreeing:
                    agreeing[book_key] = consensus_fair(ev, replace(cfg, consensus_min_books=CONFIRM_MIN_BOOKS),
                                                        now, is_live, set(sharp), leave_out=book_key)
                who, why, theirs = confirmed_by(k[0], name, p_own, kq, book_key,
                                                agreeing.get(book_key, {}).get(k), cfg)
                fails = [why] if why else []
                # The sharp line since a check a window ago, or since the last check when they're further
                # apart (and the move within the window that confidence uses): not against the bet.
                then = history.before((ev["id"], k[0], k[1], name), now) if history else None
                since = None if then is None else (p_own - then) * 100
                if min([m for m in (move, since) if m is not None], default=0.0) <= -SHARP_MOVE_PTS:
                    fails.append("sharp line moving against it")
                if bet.confidence != "high":   # never medium, whatever MIN_CONFIDENCE says
                    fails.append("not high confidence")
                # The edge by the less generous of the two win chances: it must clear the floor by both.
                lower = None if why or theirs is None else min(p_own, theirs)
                both = None if lower is None else (lower * price - 1) * 100
                if both is not None and both < floor:
                    fails.append(f"under {floor:g}% by both")
                if history is not None and then is None:
                    fails.append("no sharp history yet")   # it waits one check
                if fails and bet.key in keep and set(fails) <= set(CONFIRM_MISSING):
                    # Its card is up and a source is only missing this check, not disagreeing: it stays up
                    # quietly (a false GONE, then a new alert with a ping, would be worse).
                    bet.kept = True
                elif fails:
                    miss(fails[0])
                    reject(bet, f"confirm fail: {fails[0]}")
                    continue
                if lower is not None:   # the other books listed with it clear the floor by both too
                    bet.also = [(bk, pr) for bk, pr in bet.also if (lower * pr - 1) * 100 >= floor]
                bet.tier, bet.confirm = tier, (who, theirs, min_ev, both)
            least = CONFIDENCE_ORDER.get(cfg.min_confidence, 0)
            if cons is not None and bet.key in keep and (waiting or CONFIDENCE_ORDER[bet.confidence] < least):
                # Its card is up: it stays at medium, shown as 🟡 (no GONE), and pings nothing new.
                least, bet.kept = min(least, CONFIDENCE_ORDER["medium"]), True
            if CONFIDENCE_ORDER[bet.confidence] < least:
                reject(bet, "low confidence")
                continue
            # Less certainty -> smaller bet (the edge itself isn't changed).
            mult = 1.0
            if kalshi_alone:
                mult = cfg.kalshi_only_stake
                if mult < 1:
                    bet.confidence_notes.append(f"Kalshi's price alone: stake {round((1 - mult) * 100)}% smaller")
            elif n_all > 1 and n_src == 1:
                mult = cfg.single_source_stake
            elif from_sharp and n_src == 1 and not blended:   # one sharp book alone (Kalshi not in it)
                mult = cfg.one_source_stake
                if mult < 1:
                    bet.confidence_notes.append(f"one source: stake {round((1 - mult) * 100)}% smaller")
            if cons is not None and cfg.consensus_stake < 1:
                # No sharp price (usually a prop Pinnacle doesn't offer): the fair price is the
                # median of other books, which is less certain, so the stake is smaller.
                mult *= cfg.consensus_stake
                bet.confidence_notes.append(f"no {sharp_title} price: stake "
                                            f"{round((1 - cfg.consensus_stake) * 100)}% smaller")
            stakes = [float(x) for x in _csv(cfg.confidence_stakes)] or [1, 1, 1]
            mult *= dict(zip(("high", "medium", "low"), stakes + [1] * 3)).get(bet.confidence, 1)
            bet.stake = kelly_stake(bet.fair_prob, bet.price, cfg, mult)
            if book_key == "kalshi":
                cap_to_kalshi(bet, kq, cfg)
            out.append(bet)

    return sorted(out, key=lambda b: b.ev_pct, reverse=True)


def _title(book_key: str) -> str:
    """A book's name when the game's odds don't give it (it has no prices there): pinnacle -> Pinnacle,
    betfair_ex_eu -> Betfair."""
    return BOOK_TITLES.get(book_key) or book_key.split("_")[0].title()


def _side_prob(outs: dict[str, float] | None, name: str, names, cfg: Config) -> float | None:
    """One sharp book's no-vig win chance for this side, when its prices went into the line's fair
    price (fresh, with the same outcomes: see sharp_fair), else None."""
    if not outs or set(outs) != set(names):
        return None
    return dict(zip(outs, _novig(tuple(outs.values()), cfg.devig_method)))[name]


def kalshi_applies(cfg: Config, sport_key: str, market: str, is_live: bool) -> bool:
    """Is Kalshi one of a line's fair-odds references? Moneylines in the sports Kalshi has game markets
    for (KALSHI_SERIES), before the game (during it too with KALSHI_LIVE), with KALSHI_CHECK on."""
    return cfg.kalshi_check and market == "h2h" and sport_key in KALSHI_SERIES and (cfg.kalshi_live or not is_live)


def confirmed_by(market: str, name: str, fair_prob: float, kq, book_key: str, agree: Consensus | None,
                 cfg: Config) -> tuple[str, str, float | None]:
    """Does a second sharp source agree with the sharp book's fair win chance for a confirmed pre-game
    bet? Moneylines: Kalshi's, within KALSHI_CONFIRM_PTS (not for a bet at Kalshi: its quote can't
    confirm itself). Spreads and totals: the consensus of CONFIRM_MIN_BOOKS or more other sportsbooks
    (the bet's own book left out, sister books as one), within CONSENSUS_CONFIRM_PTS. Returns (that
    source, for the card, why not, for the console, or "", its win chance or None)."""
    if market == "h2h":
        if kq is None:
            return "Kalshi", "no Kalshi price", None
        if book_key == "kalshi":
            return "Kalshi", "bet is at Kalshi", None
        if round(abs(kq[0] - fair_prob) * 100, 6) > cfg.kalshi_confirm_pts:
            return "Kalshi", "Kalshi too far off", kq[0]
        return "Kalshi", "", kq[0]
    who = "the other books"
    sportsbooks = sum(any(b not in EXCHANGES for b in vote) for vote in agree.books) if agree else 0
    if sportsbooks < CONFIRM_MIN_BOOKS or name not in agree.probs:
        return who, f"under {CONFIRM_MIN_BOOKS} other sportsbooks", None
    if round(abs(agree.probs[name] - fair_prob) * 100, 6) > cfg.consensus_confirm_pts:
        return who, "other books too far off", agree.probs[name]
    return who, "", agree.probs[name]


def _sharp_doubt(fair: dict, k: tuple, name: str, sharp_title: str) -> tuple[str, float | None]:
    """For a prop line the sharp doesn't price: does it price the same player and stat at another
    point? Returns (a note for the card, or "", and the most this side can be worth by the sharp's
    other lines, or None). An Over can't be likelier than the sharp's Over at a lower point, nor an
    Under than its Under at a higher one."""
    if not is_prop(k[1]):
        return "", None
    player, point = k[1]
    others = sorted(((kk[1][1], probs) for kk, probs in fair.items()
                     if kk[0] == k[0] and is_prop(kk[1]) and kk[1][0] == player and kk != k and kk[1][1] is not None),
                    key=lambda x: x[0])
    if not others:
        return "", None
    caps = [probs[name] for q, probs in others if point is not None and name in probs
            and ((name == "Over" and q < point) or (name == "Under" and q > point))]
    return (f"{sharp_title} has a different line ({', '.join(f'{q:g}' for q, _ in others)})",
            min(caps) if caps else None)


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
    lines = [f"{bk[:14]:<14} {odds(pr):>6} {ev:+5.1f}%" for bk, pr, ev, *_ in b.board[:rows]]
    return "```\n" + "\n".join(lines) + "\n```" if lines else ""


def confirm_lines(b: EVBet) -> tuple[str, str]:
    """A confirmed pre-game bet's two lines for its card: (who agrees, why the smaller edge is fine);
    ("", "") for any other bet."""
    if b.tier != CONFIRMED or not b.confirm:
        return "", ""
    who, theirs, usual, both = b.confirm
    sharp, mine = b.sharp_name or b.sharp_book, b.fair_prob if b.sharp_prob is None else b.sharp_prob
    agree = f"✅✅ Two sharp books agree: {sharp} and {who}"
    if theirs is None:   # its card is up, and the second source has no price this check
        gone = "Kalshi has no price" if who == "Kalshi" else f"Fewer than {CONFIRM_MIN_BOOKS} other books price it"
        return agree, (f"A smaller edge than the usual {usual:g}% is OK here: both gave it almost the same chance "
                       f"to win when it went out. {gone} right now ({sharp} {mine:.1%}).")
    return agree, (f"A smaller edge than the usual {usual:g}% is OK here: both give it almost the same chance to "
                   f"win ({sharp} {mine:.1%}, {who} {theirs:.1%}). At least +{both:.1f}% by both.")


def sources_line(b: EVBet, bold: bool = False) -> str:
    """How many of the fair-odds references that apply to this line priced it, and each one's no-vig
    win chance for this side: "Sources 2/2 · Pinnacle 50.0% · Kalshi 52.0%". One that applies but had
    no usable price says so ("Kalshi: no usable price"); when that's the top one, the line ends "(no
    Pinnacle price)" instead. One left out as the odd one out isn't counted: "LowVig left out (4.1 pts
    off)". None of them priced it (a prop): "Sources: median of 5 books (no Pinnacle price)". "" for a
    bet that doesn't use them (outliers)."""
    if not b.refs:
        return ""
    label = "**Sources**" if bold else "Sources"
    top, top_p = b.refs[0]
    if all(p is None for _, p in b.refs):
        return f"{label}: median of {b.sources_used} books (no {top} price)"
    gone, pts = b.left_out or ("", 0.0)
    priced = [f"{t} {p:.1%}" for t, p in b.refs if p is not None and t != gone]
    out = [f"{gone} left out ({pts:.1f} pts off)"] if b.left_out else []
    missing = [f"{t}: no usable price" for t, p in b.refs[1:] if p is None]
    return (f"{label} {len(priced)}/{len(b.refs)} · " + " · ".join(priced + out + missing)
            + (f" (no {top} price)" if top_p is None else ""))


def format_ev_text(b: EVBet) -> str:
    status = "🔴 LIVE" if b.is_live else f"starts {b.commence_time}"
    also = f"\n  Also +EV at: {', '.join(f'{bk} {odds(pr)}' for bk, pr in b.also)}" if b.also else ""
    sharp = f"\n  Sharp: {_quotes(b.sharp_quotes)}" if b.sharp_quotes else ""
    agree, why = confirm_lines(b)
    return (
        f"📈 +{b.ev_pct:.1f}% EV | {b.sport} | {b.matchup} ({status})\n"
        f"  {b.pick} {odds(b.price)} on {b.book}{' (alternate line)' if b.alt else ''}  → stake {b.stake_label}"
        + (f"\n    {b.link}" if b.link else "")
        + f"\n  Fair {_fair_line(b)} ({b.fair_prob:.1%}, {b.sharp_book} no-vig"
        + (f")\n  {sources_line(b)}" if b.refs else f", {b.sources_used}/{b.sources_total} sources)")
        + f"{sharp}{also}"
        + (f"\n  Confidence: {b.confidence}" + (f" ({', '.join(b.confidence_notes)})" if b.confidence_notes else "")
           if b.confidence else "")
        + (f"\n  {agree}. {why}" if agree else "")
    )


def _kalshi_line(b: EVBet) -> str:
    if not b.kalshi:
        return ""
    p, bid, ask = b.kalshi
    return f"\n**Kalshi** {p:.1%} to win (buy {ask * 100:.0f}¢ · sell {bid * 100:.0f}¢)"


def _board_lines(b: EVBet, rows: int = 12) -> str:
    """Every book's price on this bet, best first, each with its link."""
    lines = []
    for bk, pr, ev, link, ok in b.board[:rows]:
        mark = "🟢" if ev > 0 and ok else "⚪"
        lines.append(f"{mark} {_link(bk, link)} — **{odds(pr)}** · {ev:+.1f}%")
    return "\n".join(lines)


EV_DIVIDER = "\n\n\n───────────────\n\n\n"   # (two blank lines each side: Discord keeps them)


def confidence_line(confidence: str) -> str:
    """"🟡 Confidence: Medium" ("" for a bet with no rating)."""
    if not confidence:
        return ""
    icon, word = CONFIDENCE_BADGE[confidence].split(" ", 1)
    return f"{icon} Confidence: {word}"


# ONE_ALERT_PER_BET: the card's line on its own price since it went out (no numbers: the card stays as sent).
STATUS_LINES = {"ok": "✅ **Still good** at {book}",
                "moved": "⚠️ **Price moved** at {book}: not good value now. Wait for ✅ before betting it."}


def status_line(status: str, book: str) -> str:
    return STATUS_LINES[status].format(book=book) if status in STATUS_LINES else ""


def ev_payload(b: EVBet, mention: str = "", gone_after: float | None = None,
                first_seen: float | None = None, status: str = "") -> dict:
    """+EV card: the game and when first, then the bet itself (stake, odds, pick), then why (fair value,
    sharp prices), then every book. The title (the bet at its book) opens the bet slip."""
    icon = sport_icon(b.sport_key)
    if gone_after is not None:
        return _gone_card(f"❌ GONE after {_fmt_secs(gone_after)} · ~~+{b.ev_pct:.1f}%~~ {b.pick} {odds(b.price)}",
                          f"Ignore this one. {b.book} moved and the value is gone. ({icon} {b.matchup})")
    game = f"{icon} **{b.sport}** · {b.matchup}\n{_when(b.is_live, b.commence_time, first_seen)}{_age_note(b.age)}"
    bet = (f"**BET SIZE: {b.stake_label}**\n**ODDS: {odds(b.price)}**\n\n"
           f"**{b.pick}**{ALT_NOTE if b.alt else ''}")
    parts = [status_line(status, b.book)] if status else []
    notes = (confidence_line(b.confidence) + kalshi_room_line(b.kalshi_room)
             + kalshi_tie_note(b.sport_key, b.market, b.book)
             + (f"\n📊 {b.tune_note}" if b.tune_note else "")).strip("\n")
    if notes:
        parts.append(notes)
    agree, why = confirm_lines(b)
    if agree:
        parts.append(f"**{agree}**\n{why}")
    if b.related:
        parts.append(f"⚠️ Also alerted on this game: {', '.join(b.related)}. These move together, "
                     f"so treat them as one bet, not separate ones.")
    parts.append(f"**Fair value** {_fair_line(b)} · {b.fair_prob:.1%} to win"
                 + (f"\n{sources_line(b, bold=True)}" if b.refs else "")
                 + (f"\n**{b.sharp_quotes[0][0]}** {' / '.join(odds(x) for x in b.sharp_quotes[0][1])}"
                    if b.sharp_quotes else "")
                 + (f" *(was {' / '.join(odds(x) for x in b.first_sharp_quotes[0][1])})*"
                    if b.first_sharp_quotes and b.first_sharp_quotes != b.sharp_quotes else "")
                 + _kalshi_line(b))
    if b.board:
        parts.append("**Every book**\n" + _board_lines(b))
    desc = game + EV_DIVIDER + bet + "\n\n\n" + "\n\n".join(parts)
    return _card(f"📈 +EV {b.ev_pct:.1f}%{' ✅✅' if agree else ''} · {b.pick} {odds(b.price)} at {b.book}", desc,
                 0x3498DB, url=b.link, mention=mention)


EV_LOG_FIELDS = ["first_seen", "event_id", "sport", "sport_key", "matchup",
                 "home_team", "away_team", "commence_time", "live", "market", "outcome", "point",
                 "n_outcomes", "book", "price", "fair_odds", "best_ev_pct", "stake", "player", "confidence",
                 "fair_from",   # whose fair odds: "Pinnacle", "consensus of DraftKings, ...", "median of 4 other books"
                 "tier",        # CONFIRMED: a confirmed pre-game bet, under MIN_EV_PCT ("" = any other)
                 "units",       # the stake in units, as the card showed it ("$15 (1.5u)" -> 1.5)
                 "sources",     # the card's Sources line: "Sources 2/2 · Pinnacle 50.0% · Kalshi 52.0%"
                 "pinnacle_prob",   # the sharp books' own fair win chance (Pinnacle's no-vig), before any Kalshi
                 "kalshi_prob",     # Kalshi's win chance for this side, when it had a usable quote ("" = none)
                 "excluded",        # a reference left out of the fair price as the odd one out: "LowVig (4.1 pts off)"
                 "alt",             # True: the price was on the book's alternate line at this point, not its main one
                 "post_delay"]      # seconds from fetching the odds to the post that sent the alert ("" = not known)


class EVAlerter(Alerter):
    noun = "+EV bets"
    book_pings = True   # (outliers too)
    one_alert = True
    log_fields = EV_LOG_FIELDS
    throttle_edits = True   # (CARD_EDIT_MIN_SECONDS; outliers too)
    log_on_open = True  # logged at first sight, so a restart never loses a bet from the results
    log_held = False    # the bet log is what was alerted (results are graded from it)

    def text(self, item) -> str:
        return format_ev_text(item)

    def payload(self, item, mention="", gone_after=None, first_seen=None, status="") -> dict:
        return ev_payload(item, mention, gone_after, first_seen, status)

    def value(self, item) -> float:
        return item.ev_pct

    def label(self, item) -> str:
        return f"{item.pick} ({item.book})"

    def mention(self) -> str:
        return self.cfg.ev_mention

    def carry(self, old, new) -> None:
        super().carry(old, new)
        new.first_fair_prob = old.first_fair_prob or old.fair_prob
        new.first_sharp_quotes = old.first_sharp_quotes or old.sharp_quotes

    def extra_state(self, item) -> dict:
        return {**super().extra_state(item), "first_fair_prob": item.first_fair_prob or item.fair_prob,
                "first_sharp_quotes": item.first_sharp_quotes or item.sharp_quotes, "pick": item.pick}

    def restore_extra(self, item, saved: dict) -> None:
        super().restore_extra(item, saved)
        if saved.get("first_fair_prob"):
            item.first_fair_prob = saved["first_fair_prob"]
            item.first_sharp_quotes = [(bk, list(prs)) for bk, prs in saved.get("first_sharp_quotes", [])]

    def log_path(self) -> str:
        return self.cfg.ev_log_file

    def webhook_for(self, item) -> str:
        return (self.props and self.cfg.props_webhook_url) or self.cfg.ev_webhook_url or self.cfg.webhook_url

    def row(self, op: OpenArb) -> dict:
        b = op.arb
        return {
            "event_id": b.event_id, "sport": b.sport, "sport_key": b.sport_key, "matchup": b.matchup,
            "home_team": b.home_team, "away_team": b.away_team, "commence_time": b.commence_time,
            "live": b.is_live, "market": b.market, "outcome": b.outcome,
            "point": "" if b.point is None else b.point, "n_outcomes": b.n_outcomes,
            "book": b.book, "price": b.price, "fair_odds": round(b.fair_odds, 3),
            "best_ev_pct": round(b.ev_pct, 2), "stake": b.stake, "player": b.player,
            "confidence": b.confidence, "fair_from": b.sharp_book, "tier": b.tier,
            "units": round(b.stake / b.unit_size, 2) if b.unit_size > 0 else "", "sources": sources_line(b),
            "pinnacle_prob": "" if b.sharp_prob is None else round(b.sharp_prob, 4),
            "kalshi_prob": round(b.kalshi[0], 4) if b.kalshi else "",
            "excluded": f"{b.left_out[0]} ({b.left_out[1]:.1f} pts off)" if b.left_out else "",
            "alt": b.alt, "post_delay": "" if (pd := getattr(op, "post_delay", None)) is None else round(pd, 1),
        }



# --------------------------------------------------------------------------- Kalshi: a free second opinion

KALSHI_APIS = [os.environ.get("KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2"),
               "https://external-api.kalshi.com/trade-api/v2"]
KALSHI_SERIES = {   # game-winner markets per sport (alternative series names tried in order)
    "americanfootball_nfl": ["KXNFLGAME"], "americanfootball_ncaaf": ["KXNCAAFGAME"],
    "basketball_nba": ["KXNBAGAME"], "basketball_wnba": ["KXWNBAGAME"],
    "basketball_ncaab": ["KXNCAAMBGAME", "KXNCAABGAME"],
    "icehockey_nhl": ["KXNHLGAME"], "baseball_mlb": ["KXMLBGAME"],
}
KALSHI_TIMED = {"KXMLBGAME"}   # tickers that carry the Eastern start time too: KXMLBGAME-26SEP262040AZSD
KALSHI_ALIASES = {   # Kalshi team labels the usual name matching can't read -> the full team name
    "chicagows": "Chicago White Sox", "as": "Athletics", "miamifl": "Miami Hurricanes",
    "umass": "Massachusetts Minutemen", "connecticut": "UConn Huskies", "fiu": "Florida International Panthers",
    "floridaintl": "Florida International Panthers", "appst": "Appalachian State Mountaineers",
    "ulmonroe": "Louisiana Monroe Warhawks", "samhouston": "Sam Houston State Bearkats",
    "centralconnecticut": "Central Connecticut State Blue Devils", "nicholls": "Nicholls State Colonels",
    "grambling": "Grambling State Tigers",
}
KALSHI_MIN_GAP = 0.2            # seconds between Kalshi calls (at most 5 a second)
KALSHI_TIMEOUT = 5              # seconds to wait for Kalshi before giving up on it for this scan
KALSHI_SCAN_SECONDS = 8         # stop asking Kalshi for more sports once a scan has spent this long on it
KALSHI_COOLDOWNS = (60, 300, 900)   # after Kalshi can't be reached: leave it alone this long (grows)
# A Kalshi failure this long after the last one, with nothing asked in between (a lookup that worked would
# have ended the outage), is a new outage: its clock starts again (an outage keeps failing at least every
# longest cooldown).
KALSHI_QUIET_GAP = 2 * max(KALSHI_COOLDOWNS)
COLLEGE_QUALIFIERS = {   # "Texas" mustn't claim "Texas Southern" / "Texas A&M" / "Texas State"...
    "state", "st", "tech", "am", "at", "southern", "northern", "eastern", "western", "central", "pine",
    "valley", "international", "intl", "atlantic", "gulf", "christian", "baptist", "oh", "fl", "city",
    "upstate", "monroe", "lafayette", "poly", "wesleyan", "methodist", "little", "el", "rio", "san",
    "arlington", "martin", "chattanooga", "commerce", "corpus", "coast",
}
KALSHI_PAGE_ERRORS = (400, 404, 410)   # errors about the page asked for (e.g. an unknown series), not Kalshi
KALSHI_MAX_ASK_SUM = 1.10       # both teams' asks together above this: not a real market yet
KALSHI_MID_SUM = (0.96, 1.04)   # both teams' middle prices should add up to about 100%
_KALSHI_STATE = {"host": 0, "last": 0.0, "pause_until": 0.0, "fails": 0}
_KALSHI_WARNED: dict[str, float] = {}
_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT",
                                        "NOV", "DEC"), 1)}


def _kalshi_num(m: dict, key: str) -> float | None:
    v = m.get(key)
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _kalshi_price(m: dict, field: str) -> float | None:
    """A YES price in dollars (0-1): the "<field>_dollars" string, else the mirror of the NO side
    (YES bid = 1 - NO ask, YES ask = 1 - NO bid), else the old whole-cents number."""
    v = _kalshi_num(m, f"{field}_dollars")
    if v is None:
        mirror = _kalshi_num(m, ("no_ask" if field == "yes_bid" else "no_bid") + "_dollars")
        v = 1 - mirror if mirror is not None else None
    if v is None:
        cents = _kalshi_num(m, field)
        v = cents / 100 if cents is not None else None
    return None if v is None else round(v, 6)


class KalshiQuote(tuple):
    """Kalshi's (win chance, bid, ask) for one team, plus .ask_size: the contracts on offer at that ask
    (None = not known). It is still a 3-tuple, so code that unpacks three values keeps working, and
    so does a plain (chance, bid, ask) tuple wherever a quote goes: read the size with
    getattr(q, "ask_size", None)."""

    def __new__(cls, chance: float, bid: float, ask: float, ask_size: float | None = None):
        q = super().__new__(cls, (chance, bid, ask))
        q.ask_size = ask_size
        return q

    def __getnewargs__(self):
        return (*self, self.ask_size)


def _kalshi_quote(m: dict) -> tuple[float, float] | None:
    """Best YES (bid, ask), or None when a side is empty (Kalshi shows a $0 bid or a $1 ask then)."""
    bid, ask = _kalshi_price(m, "yes_bid"), _kalshi_price(m, "yes_ask")
    if bid is None or ask is None or not 0 < bid <= ask < 1:
        return None
    return bid, ask


def _kalshi_day(ticker: str):
    """The game date in an event ticker: KXNFLGAME-26SEP20CLETB -> 2026-09-20."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", ticker or "")
    if not m or m.group(2) not in _MONTHS:
        return None
    try:
        return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


def _kalshi_start(ticker: str) -> datetime | None:
    """The start time in an MLB event ticker: KXMLBGAME-26SEP262040AZSD -> Sep 26 2026, 8:40 PM Eastern.
    Other sports' tickers carry only the date, so this is None for them."""
    if (ticker or "").split("-")[0] not in KALSHI_TIMED:
        return None
    m = re.search(r"-\d{2}[A-Z]{3}\d{2}(\d{2})(\d{2})", ticker)
    day = _kalshi_day(ticker)
    if not m or day is None or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return datetime.combine(day, dtime(int(m.group(1)), int(m.group(2))), ZoneInfo("America/New_York"))


def _kalshi_label_match(team: str, label: str, college: bool = False) -> bool:
    """Does a Kalshi team label ("Tampa Bay", "New York G", "Los Angeles R", "LA Rams") name this
    team ("Tampa Bay Buccaneers")? In college, a label that stops just before "State", "Tech",
    "A&M", "Southern"... names a different school ("Texas" isn't "Texas Southern Tigers")."""
    t, l = _words(team), _words(label)
    if not t or not l or len(l) > len(t):
        return False
    if l[:-1] == t[:len(l) - 1] and t[len(l) - 1].startswith(l[-1]):
        if not (college and len(t) > len(l) and t[len(l)] in COLLEGE_QUALIFIERS):
            return True                               # same start, last word maybe shortened
    return len(l[-1]) > 2 and l[-1] == t[-1]          # same nickname


def _kalshi_code_match(team: str, code: str) -> bool:
    """Does a Kalshi team code (the ticker's last part: SEA, NE, NYG, LAR, CWS) fit this team?"""
    w, code = _words(team), code.lower()
    if len(code) < 2 or not w:
        return False
    return "".join(x[0] for x in w).startswith(code) or w[0][:3] == code


def _kalshi_strength(team: str, m: dict, pro: bool) -> int:
    """How well a Kalshi market names this team: 0 = not at all; higher = a longer, surer match.
    The team label first (and the alias table for labels like "Chicago WS"), then, in pro sports
    only, the team code at the end of the ticker."""
    label = m.get("yes_sub_title") or ""
    best = len(_words(label)) if _kalshi_label_match(team, label, not pro) else 0
    alias = KALSHI_ALIASES.get(_norm(label))
    if alias and _kalshi_label_match(team, alias, not pro):
        best = max(best, len(_words(alias)))
    if not best and pro and _kalshi_code_match(team, (m.get("ticker") or "").rsplit("-", 1)[-1]):
        best = 1
    return best


def _kalshi_pair(ev: dict, ms: list[dict], pro: bool) -> tuple[int, dict, dict] | None:
    """(strength, home market, away market) when exactly one way round fits this game's teams."""
    fits = []
    for h, a in ((ms[0], ms[1]), (ms[1], ms[0])):
        sh, sa = _kalshi_strength(ev["home_team"], h, pro), _kalshi_strength(ev["away_team"], a, pro)
        if sh and sa:
            fits.append((sh + sa, h, a))
    fits.sort(key=lambda f: -f[0])
    if not fits or (len(fits) == 2 and fits[0][0] == fits[1][0]):
        return None
    return fits[0]


def _kalshi_failed(now: float | None = None) -> None:
    """Kalshi couldn't be reached (or asked the bot to slow down): note when the outage began, for the health
    message. A failure long after the last one (KALSHI_QUIET_GAP: Kalshi wasn't asked in between) starts it again."""
    now = now or time.time()
    if "down_since" not in _KALSHI_STATE or now - _KALSHI_STATE.get("last_fail", now) > KALSHI_QUIET_GAP:
        _KALSHI_STATE["down_since"] = now
    _KALSHI_STATE["last_fail"] = now


def _kalshi_fetch(url: str) -> dict:
    """One Kalshi GET, at most 5 a second. When Kalshi says "too many requests" (429), wait 1, 2,
    then 4 seconds and retry; after that, leave Kalshi alone for 2 minutes."""
    for wait in (1, 2, 4, None):
        gap = _KALSHI_STATE["last"] + KALSHI_MIN_GAP - time.time()
        if gap > 0:
            time.sleep(gap)
        _KALSHI_STATE["last"] = time.time()
        try:
            return _get_json(url, timeout=KALSHI_TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            if wait is None:
                _KALSHI_STATE["pause_until"] = time.time() + 120
                _kalshi_failed()   # (for the health message)
                raise
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _kalshi_get(query: str) -> dict:
    """GET a Kalshi market-data page (public, no key; cached 30 s). Starts with the address that
    worked last time and tries the other if it fails (but not when Kalshi asked to slow down).
    When neither address gives data (down, timing out, refusing with 401/403), Kalshi is left alone
    for a minute, then 5, then 15, so an outage never slows the scans down. Only an error about the
    page itself (KALSHI_PAGE_ERRORS, e.g. a series name Kalshi doesn't know) doesn't count."""
    def fetch() -> dict:
        if time.time() < _KALSHI_STATE["pause_until"]:
            raise RuntimeError(f"Kalshi paused until {datetime.fromtimestamp(_KALSHI_STATE['pause_until']):%H:%M:%S} "
                               "(it was unreachable or asked the bot to slow down)")
        bases = list(dict.fromkeys(KALSHI_APIS))
        first = _KALSHI_STATE["host"] % len(bases)
        last: Exception | None = None
        page_error: Exception | None = None
        for base in bases[first:] + bases[:first]:
            try:
                data = _kalshi_fetch(f"{base}/{query}")
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    raise
                if e.code in KALSHI_PAGE_ERRORS:
                    page_error = e
                else:
                    last = e
                continue
            except Exception as e:  # noqa: BLE001 - timeouts, refused connections, bad JSON
                last = e
                continue
            if not isinstance(data, dict):
                last = ValueError(f"unexpected reply from {base}")
                continue
            _KALSHI_STATE["host"] = bases.index(base)
            _KALSHI_STATE["fails"] = 0
            _KALSHI_STATE.pop("down_since", None)
            return data
        if last is None and page_error is not None:
            raise page_error   # every address said the page doesn't exist: not an outage
        wait = KALSHI_COOLDOWNS[min(_KALSHI_STATE["fails"], len(KALSHI_COOLDOWNS) - 1)]
        _KALSHI_STATE["fails"] += 1
        _KALSHI_STATE["pause_until"] = time.time() + wait
        _kalshi_failed()   # (for the health message)
        raise last or RuntimeError("no Kalshi address to try")
    return _cached("kalshi:" + query, fetch, ttl=30)


def kalshi_markets(sport_key: str) -> list[dict]:
    """Open game-winner markets for a sport. A series name Kalshi doesn't know (a 4xx) moves on
    to the next name; any other failure is raised."""
    for series in KALSHI_SERIES.get(sport_key, []):
        out, cursor = [], ""
        try:
            for _ in range(5):   # pages of up to 1,000
                page = _kalshi_get(f"markets?series_ticker={series}&status=open&limit=1000"
                                   + (f"&cursor={urllib.parse.quote(str(cursor))}" if cursor else ""))
                out += [m for m in page.get("markets") or [] if isinstance(m, dict)]
                cursor = page.get("cursor") or ""
                if not cursor:
                    break
        except urllib.error.HTTPError as e:
            if e.code not in KALSHI_PAGE_ERRORS:
                raise
            continue
        if out:
            return out
    return []


def kalshi_spread_limit(cfg: Config, sport_key: str) -> float:
    """The widest bid-ask spread (cents) trusted: KALSHI_MAX_SPREAD, 2 more for college games."""
    return cfg.kalshi_max_spread + (2 if "ncaa" in sport_key else 0)


def kalshi_gap_limit(cfg: Config, sport_key: str, hours_to_start: float) -> float:
    """How far (points) Kalshi may sit from the sharp price before a bet is skipped: KALSHI_MAX_GAP
    for pro games within a day, 1 more further out (lines are still settling), 2 more for college."""
    if "ncaa" in sport_key:
        return cfg.kalshi_max_gap + 2
    return cfg.kalshi_max_gap + (1 if hours_to_start > 24 else 0)


def _kalshi_problem(ms: tuple[dict, dict], cfg: Config, sport_key: str) -> str:
    """Why this game's two team markets can't be trusted as a price, or "" if they can."""
    quotes = []
    for m in ms:
        status = m.get("status") or "active"
        if status != "active" or (m.get("market_type") or "binary") != "binary" or m.get("mve_collection_ticker"):
            return f"not trading right now ({status})"
        q = _kalshi_quote(m)
        if q is None:
            return "one side has no bid or no offer"
        if round((q[1] - q[0]) * 100, 6) > kalshi_spread_limit(cfg, sport_key):
            return f"quotes too wide ({(q[1] - q[0]) * 100:.0f}¢ between bid and ask)"
        sizes = [_kalshi_num(m, f) for f in ("yes_bid_size_fp", "yes_ask_size_fp")]
        if any(s is None for s in sizes):
            return "Kalshi didn't say how many contracts are on offer"
        if any(s < cfg.kalshi_min_size for s in sizes):
            return f"fewer than {cfg.kalshi_min_size:.0f} contracts at the best price"
        quotes.append(q)
    asks, mids = quotes[0][1] + quotes[1][1], sum((b + a) / 2 for b, a in quotes)
    if round(asks, 6) > KALSHI_MAX_ASK_SUM or not KALSHI_MID_SUM[0] <= round(mids, 6) <= KALSHI_MID_SUM[1]:
        return f"the two teams' prices don't add up (middles total {mids:.0%})"
    return ""


def kalshi_fair(events: list[dict], cfg: Config, now: datetime | None = None,
                report: dict | None = None) -> dict[str, dict[str, tuple[float, float, float]]]:
    """Kalshi's win chance for each team in these games: {event id: {team: (chance, bid, ask)}}.

    Each Kalshi game has a "will X win?" market per team. The fair chance is the middle of its bid
    and ask, the two teams scaled to add up to 100%. Used only before the game starts (unless
    KALSHI_LIVE), and only when both markets are trading, quoted tight (KALSHI_MAX_SPREAD), deep
    enough (KALSHI_MIN_SIZE) and add up. Anything missing or odd means no Kalshi price for that game,
    and alerts go ahead as they would without Kalshi. `report`, if given, gets
    {event id: (Kalshi event ticker or "", why it wasn't used or "")} for --check-kalshi."""
    if not kalshi_useful(cfg):
        return {}
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo("America/New_York")
    report = {} if report is None else report
    out: dict[str, dict] = {}
    t0 = time.monotonic()
    for sport in sorted({ev.get("sport_key", "") for ev in events} & set(KALSHI_SERIES)):
        pool = [ev for ev in events if ev.get("sport_key") == sport]
        use = {ev["id"] for ev in pool if cfg.kalshi_live or now < _parse_time(ev["commence_time"])}
        for ev in pool:
            if ev["id"] not in use:
                report[ev["id"]] = ("", "game has started (KALSHI_LIVE=false)")
        if not use:
            continue   # nothing before kickoff: no need to ask Kalshi
        if time.monotonic() - t0 > KALSHI_SCAN_SECONDS:
            for i in use:
                report[i] = ("", "skipped: Kalshi was slow this scan")
            continue
        try:
            markets = kalshi_markets(sport)
        except Exception as e:  # noqa: BLE001 - Kalshi down or refusing: carry on without it
            _kalshi_warn(sport, f"! Kalshi prices unavailable for {sport}: {e!r:.120}")
            for i in use:
                report[i] = ("", f"couldn't reach Kalshi ({e!r:.80})")
            continue
        try:
            out.update(_kalshi_match(pool, use, markets, sport, cfg, tz, report))
        except Exception as e:  # noqa: BLE001 - an odd reply must never stop the scan
            _kalshi_warn("read:" + sport, f"! Couldn't read Kalshi's {sport} markets: {e!r:.120}")
    return out


def _kalshi_warn(key: str, msg: str) -> None:
    """Log a Kalshi problem once an hour, not every scan."""
    if time.time() - _KALSHI_WARNED.get(key, 0) > 3600:
        _KALSHI_WARNED[key] = time.time()
        print(msg, file=sys.stderr)


def _kalshi_match(pool: list[dict], use: set[str], markets: list[dict], sport: str, cfg: Config,
                  tz, report: dict) -> dict[str, dict]:
    """Match the sport's games to Kalshi games and read the usable prices (see kalshi_fair).

    Each game takes its single best-fitting Kalshi game (same Eastern date, and for MLB a start
    within 90 minutes; the longest team-name match wins). A Kalshi game that two games both pick is
    used by neither: better no second opinion than someone else's. Games that have started still
    take part in the matching, so their Kalshi game can't be handed to a later one."""
    games: dict[str, list[dict]] = {}
    for m in markets:
        games.setdefault(str(m.get("event_ticker") or ""), []).append(m)
    pro = "ncaa" not in sport
    best: dict[str, tuple[str, dict, dict] | None] = {}
    for ev in pool:
        start = _parse_time(ev["commence_time"])
        fits: list[tuple[int, str, dict, dict]] = []
        # A started game only holds the Kalshi game on its own Eastern date (its market may have
        # closed already, and the UTC date could be tomorrow's rematch).
        days = (start.astimezone(tz).date(),) + ((start.date(),) if ev["id"] in use else ())
        for day in dict.fromkeys(days):
            for ticker, ms in games.items():
                if len(ms) != 2 or _kalshi_day(ticker) != day:
                    continue
                when = _kalshi_start(ticker)
                if when is not None and abs((when - start).total_seconds()) > 90 * 60:
                    continue   # the other game of a doubleheader
                pair = _kalshi_pair(ev, ms, pro)
                if pair:
                    fits.append((pair[0], ticker, pair[1], pair[2]))
            if fits:
                break   # the Eastern date found it; only try the UTC date if it didn't
        fits.sort(key=lambda f: -f[0])
        best[ev["id"]] = None
        if not fits:
            reason = "no Kalshi game found for it"
        elif len(fits) > 1 and fits[0][0] == fits[1][0]:
            reason = f"{fits[0][1]} and {fits[1][1]} both fit; can't tell which"
        else:
            best[ev["id"]], reason = fits[0][1:], ""
        if reason and ev["id"] in use:
            report[ev["id"]] = ("", reason)
    picked: dict[str, int] = {}
    for b in best.values():
        if b:
            picked[b[0]] = picked.get(b[0], 0) + 1
    for ev in pool:
        if best.get(ev["id"]) and ev["id"] not in use:   # (so --check-kalshi knows that Kalshi game is placed)
            report[ev["id"]] = (best[ev["id"]][0], report.get(ev["id"], ("", ""))[1])
    out: dict[str, dict] = {}
    for ev in pool:
        b = best.get(ev["id"])
        if not b or ev["id"] not in use:
            continue
        ticker, hm, am = b
        if picked[ticker] > 1:
            report[ev["id"]] = (ticker, "that Kalshi game also fits another game; not using it")
            continue
        why = _kalshi_problem((hm, am), cfg, sport)
        report[ev["id"]] = (ticker, why)
        if why:
            continue
        (hb, ha), (ab, aa) = _kalshi_quote(hm), _kalshi_quote(am)
        h_mid, a_mid = (hb + ha) / 2, (ab + aa) / 2
        out[ev["id"]] = {ev["home_team"]: KalshiQuote(h_mid / (h_mid + a_mid), hb, ha, _kalshi_num(hm, "yes_ask_size_fp")),
                         ev["away_team"]: KalshiQuote(a_mid / (h_mid + a_mid), ab, aa, _kalshi_num(am, "yes_ask_size_fp"))}
    return out


def kalshi_useful(cfg: Config) -> bool:
    """Would a Kalshi price change anything this scan? (Moneyline +EV or outliers, before games.)"""
    return (cfg.kalshi_check and (cfg.ev_enabled or cfg.outliers_enabled) and "h2h" in _csv(cfg.markets)
            and not (cfg.live_only and not cfg.kalshi_live))


def kalshi_still_there(q: tuple[float, float, float], price: float, cfg: Config) -> bool:
    """For a bet AT Kalshi: is Kalshi's own best ask, after its fee, still at least this price?
    (Kalshi's quote can't be a second opinion on itself, but it does show the price is real.)"""
    ask = q[2]
    real = 1 / (ask + cfg.kalshi_fee_rate * ask * (1 - ask))
    return real >= price * 0.995


def kalshi_room(q) -> float | None:
    """Dollars on offer at Kalshi's best ask (contracts x ask), or None when the size isn't known."""
    size = getattr(q, "ask_size", None)
    return None if size is None else size * q[2]


def cap_to_kalshi(bet: "EVBet", q, cfg: Config) -> None:
    """A bet at Kalshi bigger than Kalshi's order book holds at its best ask (moneylines before the
    game: that's when there's a quote): the stake is cut to what's there, rounded down the usual way,
    and the card says why (kalshi_room)."""
    room = kalshi_room(q) if q is not None else None
    if room is None or bet.stake <= room:
        return
    bet.kalshi_room = room
    bet.stake = round_stake(bet.stake, room, cfg)


def note_kalshi_room(arbs: list["Arb"], kalshi: dict, cfg: Config) -> None:
    """Mark each Kalshi arb bet (a moneyline with a Kalshi quote) whose stake is more than Kalshi's
    order book holds at that price, for a note on the card. The stakes stay: an arb's bets must stay
    balanced. Only while Kalshi's best ask is still the card's price (otherwise there's nothing to count)."""
    for arb in arbs:
        if arb.market != "h2h":
            continue
        for leg in arb.legs:
            q = kalshi.get(arb.event_id, {}).get(leg.outcome) if leg.book.lower().startswith("kalshi") else None
            room = kalshi_room(q) if q is not None and kalshi_still_there(q, leg.price, cfg) else None
            if room is not None and leg.stake > room:
                leg.room = room


def kalshi_arb_bets(arbs: list["Arb"], cfg: Config) -> bool:
    """Is there an arb with a Kalshi bet that Kalshi's own quote could say something about (a
    moneyline before the game, or during it with KALSHI_LIVE)?"""
    return any(a.market == "h2h" and (cfg.kalshi_live or not a.is_live)
               and any(l.book.lower().startswith("kalshi") for l in a.legs) for a in arbs)


def kalshi_room_line(room: float, indent: str = "") -> str:
    """The card's note on a bet at Kalshi bigger than Kalshi's order book holds at that price."""
    if not room:
        return ""
    about = money(math.floor(room)) if room >= 1 else money(room)
    return f"\n{indent}↳ Kalshi only has about {about} at this price; the rest would fill at a worse price."


def kalshi_tie_note(sport_key: str, market: str, book: str, indent: str = "") -> str:
    """The card's settlement note on a bet at Kalshi on an NFL moneyline: an NFL game can end in a tie,
    which sportsbooks refund, while Kalshi settles it by its own market rules (--check-kalshi prints
    them). Only a note: the edge is worked out the same way. "" for any other bet."""
    if sport_key != "americanfootball_nfl" or market != "h2h" or not book.lower().startswith("kalshi"):
        return ""
    return f"\n{indent}↳ Kalshi settles ties by its own rules; sportsbooks refund a tie."


def check_kalshi(cfg: Config, api: "OddsAPI", now: datetime | None = None) -> None:
    """Show how the bot reads Kalshi for the next two days of games (free: Kalshi and the
    Odds API's schedule both cost nothing)."""
    print("Checking Kalshi prices against the schedule...\n")
    if not cfg.kalshi_check:
        print("(KALSHI_CHECK=false, so the bot isn't using these right now.)\n")
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo("America/New_York")
    for sport in cfg.sports:
        if sport not in KALSHI_SERIES:
            print(f"{short(sport)}: Kalshi has no game markets the bot knows of.\n")
            continue
        try:
            markets = kalshi_markets(sport)
        except Exception as e:  # noqa: BLE001
            print(f"{short(sport)}: couldn't reach Kalshi ({e!r:.150})\n")
            continue
        try:
            events = [ev for ev in api.events(sport, horizon_hours=48)]
        except Exception as e:  # noqa: BLE001
            print(f"{short(sport)}: couldn't load the schedule ({e!r:.150})\n")
            continue
        for ev in events:
            ev.setdefault("sport_key", sport)
        report: dict = {}
        fair = kalshi_fair(events, replace(cfg, kalshi_check=True, ev_enabled=True, markets="h2h", live_only=False),
                           now=now, report=report)
        print(f"{short(sport)}: {len(markets)} open Kalshi markets, {len(fair)} of {len(events)} upcoming games usable")
        if markets:
            m = markets[0]
            print(f"  e.g. {m.get('ticker')}: '{m.get('yes_sub_title')}' {m.get('status')} "
                  f"bid {m.get('yes_bid_dollars')} ask {m.get('yes_ask_dollars')} "
                  f"(sizes {m.get('yes_bid_size_fp')} / {m.get('yes_ask_size_fp')})")
            print(f"  fields: {', '.join(sorted(m))[:400]}")
            for field_name, what in (("rules_primary", "rules"), ("rules_secondary", "more rules")):
                # How Kalshi settles these markets (an NFL tie: sportsbooks refund one), when it says.
                text = next((str(x[field_name]) for x in markets if x.get(field_name)), "")
                if text:
                    print(f"  {what}: {' '.join(text.split())[:600]}")
            if _kalshi_num(m, "yes_bid_size_fp") is None:
                print("  ⚠️ Kalshi's reply doesn't say how many contracts are on offer (yes_bid_size_fp), so every "
                      "game is skipped. The bot needs an update for Kalshi's new format.")
        for ev in events[:12]:
            f = fair.get(ev["id"])
            ticker, why = report.get(ev["id"], ("", ""))
            if f:
                (h, (hp, hb, ha)), (a, (ap, ab, aa)) = list(f.items())
                print(f"  ✅ {ev['away_team']} @ {ev['home_team']}: {a} {ap:.0%} · {h} {hp:.0%} "
                      f"(spreads {(aa - ab) * 100:.0f}¢ / {(ha - hb) * 100:.0f}¢) [{ticker}]")
            else:
                print(f"  ·  {ev['away_team']} @ {ev['home_team']}: {why or 'not used'}"
                      + (f" [{ticker}]" if ticker else ""))
        if len(events) > 12:
            print(f"  ...and {len(events) - 12} more")
        # Kalshi games in the same days the bot couldn't place: usually a team name it can't read.
        days = {_parse_time(ev["commence_time"]).astimezone(tz).date() for ev in events}
        used = {t for t, _ in report.values() if t}
        lost: dict[str, list[str]] = {}
        for m in markets:
            t = m.get("event_ticker") or ""
            if t not in used and _kalshi_day(t) in days:
                lost.setdefault(t, []).append(str(m.get("yes_sub_title")))
        if lost:
            print(f"  Kalshi games not placed ({len(lost)}): "
                  + "; ".join(f"{t} ({' / '.join(v)})" for t, v in list(lost.items())[:6]))
        print()

def check_upcoming(cfg: Config, api: "OddsAPI", now: datetime | None = None) -> dict:
    """One-time probe of the combined "upcoming" odds call (every sport's live games, plus the next
    few games, in one request): what it really costs (x-requests-last) and whether it has every live
    game and book that each sport's own live call has. Costs one call (LIVE_MARKETS x regions) plus
    one per sport with a live game, for the comparison. Changes nothing: it only reports."""
    now = now or datetime.now(timezone.utc)
    sched = Scheduler(cfg, api)
    sched.refresh_events(force=True)
    live = {sport: set(sched.live_games(sport, now)) for sport in cfg.sports if sched.wants_live(sport)}
    live = {k: v for k, v in live.items() if v}
    markets = (sched.split or (cfg.markets,))[0]
    formula = cfg.credits_per_call(markets)
    params = {"markets": markets, "oddsFormat": "decimal", "dateFormat": "iso",
              "commenceTimeTo": _iso(now + timedelta(minutes=1))}
    if cfg.bookmakers:
        params["bookmakers"] = cfg.bookmakers
    else:
        params["regions"] = cfg.regions
    report = {"cost": None, "formula": formula, "events": 0, "not_live": 0, "missing": {}, "fewer_books": {},
              "schedule_only": {}, "per_sport_cost": 0.0}
    if not live:
        print("No game in your sports is live right now (by the free schedule), so there's nothing to compare. "
              "Run this again while two or more sports have games on. No credits used.")
        return report
    combined, cost = api._request("/sports/upcoming/odds", params)
    combined = combined if isinstance(combined, list) else []
    got = {ev.get("id"): ev for ev in combined}
    later = [ev for ev in combined if _parse_time(ev["commence_time"]) > now + timedelta(minutes=1)]
    report.update(cost=cost, events=len(combined), not_live=len(later))
    print(f"Combined call: {len(combined)} games across {len({ev.get('sport_key') for ev in combined})} sports, "
          f"cost {cost if cost is not None else '?'} credits (x-requests-last; markets x regions = {formula}).")
    if later:
        print(f"  {len(later)} games that haven't started came back too (the combined call adds the next few "
              f"games); a real switch would drop them.")
    for sport, ids in sorted(live.items()):
        try:
            own, own_cost = api._request(f"/sports/{sport}/odds", params)
        except Exception as e:  # noqa: BLE001
            print(f"  {short(sport)}: couldn't load its own live call to compare ({e!r:.100})")
            continue
        own = own if isinstance(own, list) else []
        report["per_sport_cost"] += own_cost or 0
        listed = {ev["id"] for ev in own if ev.get("bookmakers")}
        # Missing = a game the sport's own call has odds for and the combined call doesn't. A game only
        # the schedule still calls live (it ended early, or a rain delay) isn't the combined call's fault.
        missing = sorted(listed - set(got))
        fewer = []
        for ev in own:
            mine = {bm["key"] for bm in ev.get("bookmakers", [])}
            theirs = {bm["key"] for bm in got.get(ev["id"], {}).get("bookmakers", [])}
            if ev["id"] in got and mine - theirs:
                fewer.append(f"{ev['away_team']} @ {ev['home_team']}: no {', '.join(sorted(mine - theirs))}")
        report["missing"][sport], report["fewer_books"][sport] = missing, fewer
        report["schedule_only"][sport] = sorted(ids - listed - set(got))
        ok = "✅" if not missing and not fewer else "⚠️"
        print(f"  {ok} {short(sport)}: {len(listed) - len(missing)} of {len(listed)} games with live odds are in the "
              f"combined call (its own call cost {own_cost if own_cost is not None else '?'})")
        for gid in missing:
            print(f"     missing: {gid}")
        for line in fewer[:5]:
            print(f"     fewer books: {line}")
        if report["schedule_only"][sport]:
            print(f"     (for information: {len(report['schedule_only'][sport])} game(s) the schedule still calls live "
                  f"have no odds in either call; they probably just ended)")
    good = not any(report["missing"].values()) and not any(report["fewer_books"].values())
    if not good:
        print("→ Don't switch: the combined call misses games or books that the sports' own calls have.")
    elif cost is not None and report["per_sport_cost"] > cost:
        print(f"→ Looks good: the combined call had every live game for {cost:g} credits instead of "
              f"{report['per_sport_cost']:g}. Run this 2-3 times on busy nights before trusting it.")
    else:
        print("→ No saving this time (only one sport live, or the costs didn't say). Try again with more sports on.")
    return report


# --------------------------------------------------------------------------- outliers

PROP_GLITCH_SHARE = 0.6   # a book's prop market is bad data when it's off on at least this share of the players it
                          # can be compared on there (and on OUTLIER_PROP_CLUSTER of them). News moves one team (about
                          # half a game's players), or a few teammates.
PROP_GLITCH_CLEAR = 2     # a market left out stays out until it's been back in line on this many checks in a row


def prop_glitches(events: list[dict], cfg: Config, now: datetime,
                  hold: dict | None = None) -> tuple[dict, list, list]:
    """Player-prop prices that look like bad data rather than a price that hasn't caught up, found the way
    find_outliers looks: each book's price to bet (main line, or its alternate line at the same point) against the
    median of the OTHER books' no-vig prices on that main line (at least OUTLIER_MIN_BOOKS of them).
    - A line more than OUTLIER_PROP_MAX_PCT off: that line at that book ("too big").
    - A book off by the outlier bar on OUTLIER_PROP_CLUSTER players or more of one prop type in a game, and on at
      least PROP_GLITCH_SHARE of the players it can be compared on there: that book's whole prop type in that game,
      alternate lines too ("book market off"). One morning BetMGM's NHL points came through priced like goals (Roope
      Hintz 1+ point at +190, -125 everywhere else), off on nearly every player; a book slow after news (a star
      ruled out) is off on a few teammates, and those still go out.
    The sharp books (SHARP_BOOKS) are never judged: when Pinnacle moves first on news it's the others that are off.
    And a line the sharp book prices and says is no edge (under MIN_EV_PCT, as outliers' "sharp no") isn't off.
    hold (kept between checks, see Trackers): a market left out stays out until it's been back in line on
    PROP_GLITCH_CLEAR checks of its game in a row, so one price near the bar can't make a whole book's cards go GONE
    and come back with new pings on every other check. The console says when one is left out and when it's back.
    Returns ({event id: {(book key, market key, line or None for the whole market)}} to leave out of every prop
    check (drop_glitches), [(candidate, why)] for the candidate log, [console lines])."""
    bad: dict[str, set] = {}
    notes: list = []
    said: list = []
    if not (cfg.outlier_prop_max_pct or cfg.outlier_prop_cluster):
        return bad, notes, said
    if hold is not None:
        for key in [key for key, (_, start) in hold.items() if start <= now]:
            del hold[key]   # its game has started: no more prop checks
    reference = set(_csv(cfg.sharp_books))
    for ev in events:
        is_live = _parse_time(ev["commence_time"]) <= now
        bar = max(cfg.outlier_min_pct, cfg.outlier_live_min_pct) if is_live else cfg.outlier_min_pct
        lines: dict[tuple, dict[str, dict[str, float]]] = {}   # (market, line) -> book -> {outcome: price}, main lines
        offers: dict[tuple, dict[str, float]] = {}             # ((market, line), outcome) -> book -> price to bet
        titles: dict[str, str] = {}
        for bm in ev.get("bookmakers", []):
            titles[bm["key"]] = bm.get("title", bm["key"])
            for mkt, oc, k, alt, _ in book_offers(bm, ev, now, is_live, cfg):
                if is_prop(k[1]):
                    offers.setdefault((k, oc["name"]), {})[bm["key"]] = float(oc["price"])
            for mkt in bm.get("markets", []):
                if is_alt(mkt["key"]) or not is_fresh(mkt, bm, now, is_live, cfg, _parse_time(ev["commence_time"])):
                    continue
                for oc in mkt.get("outcomes", []):
                    price = float(oc.get("price") or 0)
                    k = (mkt["key"], _line_for(mkt["key"], oc, ev["home_team"]))
                    if price > 1.0 and is_prop(k[1]):
                        lines.setdefault(k, {}).setdefault(bm["key"], {})[oc["name"]] = price
        sharp = sharp_fair(ev, cfg, now, is_live)[0] if lines else {}
        compared: dict[tuple, set] = {}    # (market, book) -> players it could be compared on
        off: dict[tuple, list] = {}        # (market, book) -> its lines off by the bar: (k, name, price, fair, n)
        big: list = []                     # lines over OUTLIER_PROP_MAX_PCT: (book, k, name, price, fair, n)
        for k, books in lines.items():
            n_out = max(len(o) for o in books.values())
            full = {bk: o for bk, o in books.items() if len(o) == n_out and n_out >= 2}
            names = set().union(*full.values()) if full else set()
            full = {bk: o for bk, o in full.items() if set(o) == names}
            probs = {bk: dict(zip(o, devig(list(o.values()), cfg.devig_method))) for bk, o in full.items()}
            for name in names:
                sp = sharp.get(k, {}).get(name)
                for bk, price in offers.get((k, name), {}).items():
                    others = [probs[ob][name] for ob in probs if ob != bk]
                    if bk in reference or len(others) < cfg.outlier_min_books:
                        continue
                    fair_p = statistics.median(others)
                    edge = (fair_p * price - 1) * 100
                    compared.setdefault((k[0], bk), set()).add(k[1][0])
                    if sp is not None and (sp * price - 1) * 100 < cfg.min_ev_pct:
                        continue   # the sharp book prices it and says it's no edge: the others are behind, not it
                    if edge >= bar:
                        off.setdefault((k[0], bk), []).append((k, name, price, fair_p, len(others)))
                    if cfg.outlier_prop_max_pct and edge > cfg.outlier_prop_max_pct:
                        big.append((bk, k, name, price, fair_p, len(others)))
        whole = set()
        for (mkt, bk), rows in off.items():
            players = {r[0][1][0] for r in rows}
            if (cfg.outlier_prop_cluster and len(players) >= cfg.outlier_prop_cluster
                    and len(players) >= PROP_GLITCH_SHARE * len(compared[(mkt, bk)])):
                whole.add((mkt, bk))
                notes += [(candidate(ev, k, name, k[1][1], titles[bk], price, fair_p, f"median of {n} other books"),
                           "book market off") for k, name, price, fair_p, n in rows]
                if hold is None or (ev["id"], bk, mkt) not in hold:
                    said.append(f"  ! {titles[bk]}'s {MARKET_NAMES.get(mkt, mkt)} for {ev['away_team']} @ "
                                f"{ev['home_team']} look wrong (off on {len(players)} of {len(compared[(mkt, bk)])} "
                                f"players): left out" + ("" if hold is None else
                                                         f" until back in line for {PROP_GLITCH_CLEAR} checks"))
                if hold is not None:
                    hold[(ev["id"], bk, mkt)] = (0, _parse_time(ev["commence_time"]))
        if hold is not None:
            for (gid, bk, mkt), (clean, start) in list(hold.items()):
                if gid != ev["id"] or (mkt, bk) in whole:
                    continue
                if clean + 1 >= PROP_GLITCH_CLEAR:
                    del hold[(gid, bk, mkt)]
                    said.append(f"  ✓ {titles.get(bk, bk)}'s {MARKET_NAMES.get(mkt, mkt)} for {ev['away_team']} @ "
                                f"{ev['home_team']} are back in line: included again")
                else:
                    hold[(gid, bk, mkt)] = (clean + 1, start)
                    whole.add((mkt, bk))
        for mkt, bk in whole:
            bad.setdefault(ev["id"], set()).add((bk, mkt, None))
        for bk, k, name, price, fair_p, n in big:
            if (k[0], bk) not in whole:
                bad.setdefault(ev["id"], set()).add((bk, k[0], k[1]))
                notes.append((candidate(ev, k, name, k[1][1], titles[bk], price, fair_p, f"median of {n} other books"),
                              "too big"))
    return bad, notes, said


def drop_glitches(events: list[dict], bad: dict) -> list[dict]:
    """events without the prices prop_glitches found: a book's whole prop type in a game (alternate lines too),
    or one line of it (both sides). Events and books that change are copies; the rest are the same objects."""
    if not bad:
        return events
    out = []
    for ev in events:
        drop = bad.get(ev["id"])
        if not drop:
            out.append(ev)
            continue
        books = []
        for bm in ev.get("bookmakers", []):
            markets = []
            for mkt in bm.get("markets", []):
                base = base_market(mkt["key"])
                if (bm["key"], base, None) in drop:
                    continue
                lines = {ln for b, m, ln in drop if b == bm["key"] and m == base and ln is not None}
                if lines:
                    mkt = {**mkt, "outcomes": [oc for oc in mkt.get("outcomes", [])
                                               if _line_for(mkt["key"], oc, ev["home_team"]) not in lines]}
                markets.append(mkt)
            books.append({**bm, "markets": markets})
        out.append({**ev, "bookmakers": books})
    return out


def find_outliers(events: list[dict], cfg: Config, now: datetime | None = None,
                  history: PriceHistory | None = None, kalshi: dict | None = None,
                  alt_seen: set | None = None, rejects: list | None = None) -> list[EVBet]:
    """One book's price far above what every other book thinks (e.g. +400 where the rest
    are around -400). Usually a book that hasn't moved yet. Fair odds here are the median of
    all the OTHER books' no-vig prices, so it works even when Pinnacle is slow or missing.
    The median comes from main lines only; a book's alternate line at exactly the same point is a
    price to bet (the better of its two, see book_offers). alt_seen, when given, collects the
    alternate-line prices that had such a median (for the console). rejects, when given, collects
    (candidate, why) for the candidate log: a price within 1 point of the bar under it ("under bar"), one
    over it whose book just moved away from the others ("moved first"), and one the sharp book or Kalshi
    says isn't good ("sharp no", "kalshi no")."""
    if not cfg.outliers_enabled:
        return []
    now = now or datetime.now(timezone.utc)
    reference_only = set() if cfg.bet_at_sharp else set(_csv(cfg.sharp_books))
    ev_allowed = cfg.ev_allowed()
    out: list[EVBet] = []

    for ev in events:
        is_live = _parse_time(ev["commence_time"]) <= now
        if (cfg.live_only and not is_live) or (is_live and not cfg.outlier_live):
            continue
        # Live prices move in seconds, so a live outlier needs a bigger edge (OUTLIER_LIVE_MIN_PCT).
        bar = max(cfg.outlier_min_pct, cfg.outlier_live_min_pct) if is_live else cfg.outlier_min_pct
        # (market, line) -> book key -> {outcome: price}, fresh full main markets only
        lines: dict[tuple, dict[str, dict[str, float]]] = {}
        alts: dict[tuple, dict[str, tuple[float, str]]] = {}   # (line, outcome) -> book -> its alternate line's
                                                               # (price, link), when better than its main line's
        titles: dict[str, str] = {}
        links: dict[tuple, str] = {}
        points: dict[tuple, float | None] = {}
        stamps: dict[tuple, datetime] = {}   # (line, book) -> when that book last moved it (props: unknown)
        for bm in ev.get("bookmakers", []):
            titles[bm["key"]] = bm.get("title", bm["key"])
            for mkt, oc, k, alt, _ in book_offers(bm, ev, now, is_live, cfg):
                if alt:   # a price to bet, never one of the prices the median comes from
                    alts.setdefault((k, oc["name"]), {})[bm["key"]] = (float(oc["price"]),
                                                                       book_link(ev, bm, mkt, oc, cfg))
                    points[(k, oc["name"])] = oc.get("point")
            for mkt in bm.get("markets", []):
                if is_alt(mkt["key"]) or not is_fresh(mkt, bm, now, is_live, cfg, _parse_time(ev["commence_time"])):
                    continue
                stamp = mkt.get("last_update") or bm.get("last_update")
                if not _one_line(mkt, ev["home_team"]):
                    stamp = None   # one stamp for many lines (every player's prop): says nothing about this one
                for oc in mkt.get("outcomes", []):
                    price = float(oc.get("price") or 0)
                    if price <= 1.0:
                        continue
                    k = (mkt["key"], _line_for(mkt["key"], oc, ev["home_team"]))
                    lines.setdefault(k, {}).setdefault(bm["key"], {})[oc["name"]] = price
                    links[(k, bm["key"], oc["name"])] = book_link(ev, bm, mkt, oc, cfg)
                    points[(k, oc["name"])] = oc.get("point")
                    if stamp:
                        stamps[(k, bm["key"])] = _parse_time(stamp)

        sharp = sharp_fair(ev, cfg, now, is_live)[0] if lines else {}
        for k, books in lines.items():
            n_out = max(len(o) for o in books.values())
            full = {bk: o for bk, o in books.items() if len(o) == n_out and n_out >= 2}
            names = set().union(*full.values()) if full else set()
            full = {bk: o for bk, o in full.items() if set(o) == names}
            probs = {bk: dict(zip(o, devig(list(o.values()), cfg.devig_method))) for bk, o in full.items()}
            def offer(b: str, nm: str) -> tuple[float | None, str, bool]:
                """Book b's price to bet on side nm of this line: (price, link, alternate?). Its main line's,
                or its alternate line's at the same point when that pays more (None: neither)."""
                main, a = full.get(b, {}).get(nm), alts.get((k, nm), {}).get(b)
                if a is not None and (main is None or a[0] > main):
                    return a[0], a[1], True
                return main, links.get((k, b, nm), ""), False

            for name in names:
                if not cfg.alerts_side(k[1], name):
                    continue   # a prop Under with PROP_SIDES=over (de-vigging the Over above still used it)
                # Every book with a price to bet: those in the median, and those with only an alternate line here.
                sellers = list(full) + sorted(b for b in alts.get((k, name), {}) if b not in full)
                for bk in sellers:
                    others = sorted(probs[ob][name] for ob in probs if ob != bk)
                    if len(others) < cfg.outlier_min_books:
                        continue
                    mid = len(others) // 2
                    fair_p = others[mid] if len(others) % 2 else (others[mid - 1] + others[mid]) / 2
                    price, link, alt = offer(bk, name)
                    if alt and alt_seen is not None:
                        alt_seen.add((ev["id"], k, name, bk))
                    edge = (fair_p * price - 1) * 100
                    # A real outlier is a book that hasn't caught up. A book that just moved AWAY
                    # from the others (in a live game: it reacted to a goal first) is probably the
                    # right one, and the rest are behind: betting it is no edge at all.
                    # That test reads the book's main line at this point when it has one, whichever price
                    # is the bet: its alternate line paying more isn't the book moving (alternate lines
                    # may not be asked for on every check, e.g. only near kickoff with PROP_NEAR_MARKETS, so
                    # the first one seen isn't a move from the main line's price). With only an alternate line
                    # here (its main line moved to another point), that price is what the book's price here
                    # moved to.
                    main_p = full.get(bk, {}).get(name)
                    gp = price if main_p is None else main_p
                    hkey = (ev["id"], k, bk, name)
                    since, anchor = _leading(history.get(hkey) if history else None, now, gp, fair_p,
                                             (fair_p * gp - 1) * 100, cfg, None if main_p is None else stamps.get((k, bk)),
                                             sorted(t for (kk, ob), t in stamps.items() if kk == k and ob != bk and ob in full),
                                             is_live)
                    if history:
                        history.put(hkey, now, since, anchor)
                    leading = since is not None
                    if bk in reference_only or not cfg.bettable(bk, ev) or edge < bar or leading:
                        if (rejects is not None and bk not in reference_only and cfg.bettable(bk, ev)
                                and edge >= bar - 1):   # (the candidate log)
                            rejects.append((candidate(ev, k, name, points.get((k, name)), titles[bk], price, fair_p,
                                                      f"median of {len(others)} other books"),
                                            "under bar" if edge < bar else "moved first"))
                        continue
                    kq = kalshi.get(ev["id"], {}).get(name) if kalshi and k[0] == "h2h" else None
                    # And when the sharp book prices this line, it has to agree the price is good.
                    sp = sharp.get(k, {}).get(name)
                    no = "sharp no" if sp is not None and (sp * price - 1) * 100 < cfg.min_ev_pct else ""
                    # So does Kalshi's exchange price, when there is one (before the game only, unless KALSHI_LIVE).
                    if not no and kq is not None and (not kalshi_still_there(kq, price, cfg) if bk == "kalshi"
                                                      else (kq[0] * price - 1) * 100 < cfg.min_ev_pct):
                        no = "kalshi no"
                    if no:
                        if rejects is not None:
                            rejects.append((candidate(ev, k, name, points.get((k, name)), titles[bk], price, fair_p,
                                                      f"median of {len(others)} other books", kq), no))
                        continue
                    board = sorted(((titles[b], pr, (fair_p * pr - 1) * 100, ln, True)
                                    for b in sellers if b not in reference_only and cfg.bettable(b, ev)
                                    for pr, ln, _ in [offer(b, name)]), key=lambda r: (-r[1], r[0]))
                    bet = EVBet(
                        event_id=ev["id"], sport=ev.get("sport_title", ev.get("sport_key", "")),
                        sport_key=ev.get("sport_key", ""), matchup=f"{ev['away_team']} @ {ev['home_team']}",
                        home_team=ev["home_team"], away_team=ev["away_team"],
                        commence_time=ev["commence_time"], is_live=is_live,
                        market=k[0], line=k[1], outcome=name, point=points.get((k, name)),
                        book=titles[bk], price=price, link=link,
                        fair_prob=fair_p, sharp_book=f"median of {len(others)} other books",
                        n_outcomes=n_out, sources_used=len(others), sources_total=len(others),
                        unit_size=cfg.unit(), board=board, kalshi=kq, alt=alt,
                        alt_books={titles[b] for b in sellers if offer(b, name)[2]},
                    )
                    if not alt and (at := stamps.get((k, bk))) is not None:
                        bet.age = max(0.0, (now - at).total_seconds())
                    bet.stake = kelly_stake(fair_p, price, cfg)
                    if sp is None and cfg.consensus_stake < 1:
                        # No sharp price backs the other books up (usually a prop Pinnacle doesn't
                        # offer): same checks, smaller bet (CONSENSUS_STAKE).
                        bet.stake = kelly_stake(fair_p, price, cfg, cfg.consensus_stake)
                        bet.confidence_notes.append(f"no {_csv(cfg.sharp_books)[0].title()} price: stake "
                                                    f"{round((1 - cfg.consensus_stake) * 100)}% smaller")
                    if bk == "kalshi":
                        cap_to_kalshi(bet, kq, cfg)
                    # The board shows every book you have; parlays stick to EV_BOOKS like +EV bets.
                    bet.parlay_books = {titles[b] for b in sellers if (not ev_allowed or b in ev_allowed)
                                        and cfg.bettable(b, ev)}
                    # Can the other side(s) be bet elsewhere to lock in a profit?
                    hedge = []
                    for other in sorted(names - {name}):
                        cands = [(pr, b, ln, a) for b in set(full) | set(alts.get((k, other), {}))
                                 if b != bk and b not in reference_only and cfg.bettable(b, ev)
                                 for pr, ln, a in [offer(b, other)] if pr is not None]
                        if cands:
                            pr, b, ln, a = max(cands)
                            hedge.append((other, titles[b], pr, ln))
                            if a:
                                bet.hedge_alt.add(other)
                    if len(hedge) == len(names) - 1:
                        margin = 1 / price + sum(1 / h[2] for h in hedge)
                        if margin < 1:
                            bet.hedge, bet.hedge_pct = hedge, (1 / margin - 1) * 100
                    out.append(bet)
    # Several books can be off-market on the same bet. Alerts are keyed by the bet, so keep only
    # the best price per bet (otherwise a worse book would overwrite the best one's alert) and
    # list the rest under "also".
    best: dict[tuple, EVBet] = {}
    for b in sorted(out, key=lambda b: (-b.ev_pct, b.book)):   # ties by book name: stable cards
        k = (b.event_id, b.market, b.line, b.outcome)
        if k in best:
            best[k].also.append((b.book, b.price))
        else:
            best[k] = b
    return list(best.values())


def _leading(prev, now: datetime, price: float, fair_p: float, edge: float, cfg: Config,
             mine: datetime | None, theirs: list[datetime], is_live: bool,
             bar: float | None = None) -> tuple[datetime | None, tuple | None]:
    """Is this book out in front of the market (not a stale one worth betting)? Returns (out in
    front since, anchor) for the history; since is None when it isn't.

    - In line with the market (edge under half the outlier bar): remember this price as the
      anchor; not out in front.
    - Out in front already: stays so for up to 5 minutes live (30 pre-game) for the others to
      catch up. If they never do, it's this book that's off after all: it's re-anchored at its price
      at the last look (+EV props keep it; otherwise this one), so only a new move counts.
    - Otherwise: out in front if, since its anchor, it moved its own price away from the market
      by a real amount (in one step or several) while the market moved less.
    - No anchor yet (first look at a live line): only if its quote is much newer than theirs.
    bar: the edge that counts as off the market (OUTLIER_MIN_PCT unless given; +EV props: their bar)."""
    bar = cfg.outlier_min_pct if bar is None else bar
    _, since, anchor, last = prev if prev is not None else (None, None, None, None)
    if edge < bar / 2:
        return None, (price, fair_p)
    if since is not None:
        if (now - since).total_seconds() <= (300 if is_live else 1800):
            return since, anchor
        # The market never followed: re-anchor so it isn't flagged again for that move. If the book
        # moved again since the last look, that new move is measured below (and held again).
        since, anchor = None, last or (price, fair_p)
    if anchor is not None:
        a_price, a_fair = anchor
        moved, market = abs(1 / price - 1 / a_price), abs(fair_p - a_fair)
        if price != a_price and moved > market and edge - (a_fair * a_price - 1) * 100 >= bar / 2:
            return now, anchor
        return None, anchor
    if is_live and mine and theirs:
        if (mine - theirs[len(theirs) // 2]).total_seconds() >= max(30, cfg.live_arb_max_skew):
            return now, None
    return None, None


def note_related(groups: list[tuple[list[EVBet], "Alerter", set[str]]], now: float | None = None) -> None:
    """Tell each +EV/outlier alert about other alerts on the same game (they tend to win or lose
    together, so they aren't independent bets).

    groups: (bets about to be handled, their alerter, what this scan re-checked: sport keys or
    event ids, or a Scope). Only alerts that will exist after this scan count: a bet the hourly cap will
    skip was never sent, and an open alert that was re-checked but not found is about to close.
    """
    now = now or time.time()
    pool: dict[str, EVBet] = {}
    live_left: dict[int, float] = {}   # the live cap is shared, so what one group takes the next can't
    for bets, a, checked in groups:
        slots = None
        if a.max_per_hour:
            slots = a.max_per_hour - len([t for t in a.posted_at if now - t < 3600])
        cap = a.live_cap
        if cap is not None and id(cap) not in live_left:
            live_left[id(cap)] = cap.room(now)
        for b in a.ranked(bets):  # the same order handle() posts them in
            live = cap is not None and b.is_live
            if b.key in a.open or b.key in a.restored or b.key in a.handed:
                pool[b.key] = b
            elif (slots is None or slots > 0) and not (live and live_left[id(cap)] <= 0):
                pool[b.key] = b
                slots = None if slots is None else slots - 1
                if live:
                    live_left[id(cap)] -= 1
        for k, op in a.open.items():
            if k not in pool and not looked_at(checked, op.arb.sport_key, op.arb.event_id, op.arb.market,
                                               op.arb.commence_time):
                pool[k] = op.arb  # not looked at this scan, so it stays open
        for k, saved in a.restored.items():   # saved before a restart and not looked at yet
            if (k not in pool and saved.get("pick")
                    and not looked_at(checked, saved.get("sport_key"), saved.get("event_id"),
                                      saved.get("market", ""), saved.get("commence_time", ""))):
                pool[k] = SimpleNamespace(key=k, event_id=saved.get("event_id"), pick=saved["pick"])
    by_game: dict[str, list[EVBet]] = {}
    for b in pool.values():
        by_game.setdefault(b.event_id, []).append(b)
    for bets, _, _ in groups:
        for b in bets:
            # The strongest three, listed alphabetically so a change in rank alone doesn't edit the card.
            b.related = sorted([o.pick for o in by_game.get(b.event_id, []) if o.key != b.key][:3])


def hand_over(evs: list[EVBet], outs: list[EVBet], ev_alr: "EVAlerter", out_alr: "OutlierAlerter",
              now: float | None = None) -> None:
    """A bet that turns from +EV into an outlier (or back) is the same bet: retire its old card
    with a pointer instead of a false "GONE", and let the other alerter post it without logging
    it twice, and without the ping unless its edge is REALERT_JUMP_PCT better than the edge it was
    last alerted at (the re-alert rule). Call right before both handle() calls (and the live rules).

    Not when the new alert is live and the old card went out before the game started: that card
    says nothing about a live price, so it closes as usual and the live bet is a new live alert (two
    checks, a fresh price, LIVE_PER_HOUR)."""
    now = now or time.time()
    out_keys, ev_keys = {o.key for o in outs}, {b.key for b in evs}
    live = {b.key for b in (*outs, *evs) if b.is_live}

    def before_kickoff(first_seen: float, commence_time: str) -> bool:
        return bool(commence_time) and first_seen < _parse_time(commence_time).timestamp()
    for src, dst, keys, note in (
            (ev_alr, out_alr, out_keys, "⬆️ Now an OUTLIER (bigger edge): see the new 🚨 alert"),
            (out_alr, ev_alr, ev_keys - out_keys, "↘️ Back to a normal +EV edge: see the new 📈 alert")):
        for k in [k for k in src.open if k in keys and k not in dst.open]:
            if k in live and before_kickoff(src.open[k].first_seen, src.open[k].arb.commence_time):
                src._close(k, now)
                continue
            if src.once:   # ONE_ALERT_PER_BET: the card it went out on stays, now followed by the other alerter
                dst.open[k] = src.open.pop(k)
                continue
            op = src.open.pop(k)
            dst.handed[k] = op.first_seen
            if op.pinged_pct is not None:   # (no post of this bet pinged yet: the new card is the alert)
                dst.handed_pct[k] = op.pinged_pct
            if not op.sent:    # ...and it was never logged or followed: the new card's post does that
                dst.handed_unsent.add(k)
            if op.message_id:
                src._discord(_gone_card(note, f"{op.arb.pick} {odds(op.arb.price)} at {op.arb.book} · "
                                              f"{op.arb.matchup}"), op.message_id, op.url)
                dst.handed_msg[k] = (op.message_id, op.url)
            elif op.prev:   # (its own card never went up: the card before it is still the one pointing here)
                dst.handed_msg[k] = op.prev
        for k in [k for k in src.restored if k in keys and k not in dst.open]:
            if k in live and before_kickoff(src.restored[k].get("first_seen", now),
                                            src.restored[k].get("commence_time", "")):
                src._drop_restored(k, "Ignore this one. The game has started.")
                continue
            if src.once:
                dst.restored[k] = src.restored.pop(k)
                continue
            saved = src.restored.pop(k)
            dst.handed[k] = saved.get("first_seen", time.time())
            if saved.get("pinged_pct", saved.get("alerted_pct")) is not None:
                dst.handed_pct[k] = saved.get("pinged_pct", saved.get("alerted_pct"))
            if saved.get("message_id"):
                src._discord(_gone_card(note, saved.get("label", "")), saved["message_id"], saved.get("url", ""))
                dst.handed_msg[k] = (saved["message_id"], saved.get("url", ""))


def without_outliers(evs: list[EVBet], outs: list[EVBet]) -> list[EVBet]:
    """Drop +EV alerts for bets that already have an outlier alert."""
    taken = {(o.event_id, o.market, o.line, o.outcome) for o in outs}
    return [b for b in evs if (b.event_id, b.market, b.line, b.outcome) not in taken]


def outlier_payload(b: EVBet, mention: str = "", gone_after: float | None = None,
                    first_seen: float | None = None, status: str = "") -> dict:
    """Outlier card: bet-now instruction, optional lock-in, then why and every book."""
    icon = sport_icon(b.sport_key)
    if gone_after is not None:
        return _gone_card(f"❌ GONE after {_fmt_secs(gone_after)} · ~~🚨 +{b.ev_pct:.0f}%~~ {b.pick} {odds(b.price)}",
                          f"Ignore this one. {b.book} fixed its price. ({icon} {b.matchup})")
    parts = [
        f"👉 **DO THIS NOW, before {b.book} fixes its price.**\n\n"
        f"Open **{_link(b.book, b.link)}** → bet **{b.stake_label}** on **{b.pick} {odds(b.price)}**"
        f"{ALT_NOTE if b.alt else ''}\n"
        f"↳ skip if the price is worse than **{odds(b.worst_ok_price())}**" + kalshi_room_line(b.kalshi_room)
        + kalshi_tie_note(b.sport_key, b.market, b.book) + (f"\n📊 {b.tune_note}" if b.tune_note else ""),
    ]
    if status:
        parts.insert(0, status_line(status, b.book))
    if b.ev_pct >= VOID_WARN_PCT:
        parts.append(f"⚠️ An edge this big may be a pricing error: {b.book} can void the bet. Bet a normal stake, "
                     f"not more.")
    if b.related:
        parts.append(f"⚠️ Also alerted on this game: {', '.join(b.related)}.")
    if b.hedge:
        legs = ([(b.pick, b.book, b.price, b.link, b.alt)]
                + [(o, bk, pr, ln, o in b.hedge_alt) for o, bk, pr, ln in b.hedge])
        margin = sum(1 / pr for _, _, pr, _, _ in legs)
        lines = [f"• Open **{_link(bk, ln)}** → bet **{money(round(100 / pr / margin, 2))}** on {o} {odds(pr)}"
                 f"{ALT_NOTE if alt else ''}" for o, bk, pr, ln, alt in legs]
        parts.append(f"🔒 **Want a sure profit instead?** Place all of these for **+{b.hedge_pct:.1f}% "
                     f"guaranteed** (per $100 total):\n" + "\n".join(lines))
    details = (f"{icon} **{b.sport}** · {b.matchup}\n{_when(b.is_live, b.commence_time, first_seen)}"
               f"{_age_note(b.age)}\n\n**Why:** {b.book} has {b.pick} at **{odds(b.price)}**, but the other {b.sources_used} books "
               f"say **{odds(b.fair_odds)}** ({b.fair_prob:.1%} to win)." + _kalshi_line(b)
               + "".join(f"\n📉 {n[0].upper()}{n[1:]}." for n in b.confidence_notes if "smaller" in n))
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

    def payload(self, item, mention="", gone_after=None, first_seen=None, status="") -> dict:
        return outlier_payload(item, mention, gone_after, first_seen, status)

    def mention(self) -> str:
        return self.cfg.outlier_mention

    def log_path(self) -> str:
        return self.cfg.outlier_log_file

    def webhook_for(self, item) -> str:
        return ((self.props and self.cfg.props_webhook_url) or self.cfg.outlier_webhook_url or self.cfg.ev_webhook_url
                or self.cfg.webhook_url)


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

    def __post_init__(self) -> None:
        self.legs = sorted(self.legs, key=lambda x: x[0].key)   # one order however it was built

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
        legs = ",".join(f"{pr}:{b.fair_prob:.3f}" for b, pr, _ in self.legs)
        return f"{self.key}|{legs}|{self.stake}"

    @property
    def stake_label(self) -> str:
        return stake_text(self.stake, self.unit_size)


def _parlay_leg_keys(key: str) -> tuple[str, list[str]]:
    """(book, leg keys) from a Parlay.key ("parlay|<book>|ev|...|ev|...")."""
    _, book, rest = key.split("|", 2)
    return book, re.split(r"\|(?=ev\|)", rest)


def find_parlays(bets: list["EVBet"], cfg: Config, keep: frozenset | set = frozenset(),
                 now: datetime | None = None) -> list[Parlay]:
    """Best +EV parlays from the current +EV bets: same book, different games, pre-game only.
    keep = keys of parlays already posted (kept ahead of new ones while they still qualify)."""
    if not cfg.parlays_enabled:
        return []

    def leg_row(b: "EVBet", book: str | None = None):
        """(book, price, link) rows where this bet is a good parlay leg."""
        if b.is_live or (now is not None and _parse_time(b.commence_time) <= now):
            return []   # pre-game only (and it may have started since it was alerted)
        # +EV boards only hold EV_BOOKS; outlier boards say which books via parlay_books.
        return [(bk, price, link) for bk, price, ev, link, ok in b.board
                if (book is None or bk == book) and ok and price <= cfg.ev_max_odds
                and cfg.parlay_leg_min_ev_pct <= ev <= cfg.max_ev_pct
                and (b.parlay_books is None or bk in b.parlay_books)]

    # A confirmed pre-game bet (under MIN_EV_PCT) is a single bet only: parlays are built as before.
    by_key = {b.key: b for b in bets if getattr(b, "tier", "") != CONFIRMED}
    per_book: dict[str, list[tuple["EVBet", float, str]]] = {}
    floor = CONFIDENCE_ORDER.get(cfg.min_confidence, 0)
    for b in by_key.values():
        # A new parlay is a new alert, so a leg needs MIN_CONFIDENCE too: not a bet that's only up
        # because its card is (kept). (Outliers aren't rated. Parlays already up are re-scored below.)
        if b.kept or (b.confidence and CONFIDENCE_ORDER.get(b.confidence, 0) < floor):
            continue
        for bk, price, link in leg_row(b):
            per_book.setdefault(bk, []).append((b, price, link))
    found: dict[str, Parlay] = {}
    for book, legs in per_book.items():
        legs = sorted(legs, key=lambda x: (x[0].fair_prob * x[1]), reverse=True)[:8]  # keep it small
        for n in range(2, cfg.parlay_max_legs + 1):
            for combo in itertools.combinations(legs, n):
                if len({b.event_id for b, _, _ in combo}) < n:
                    continue  # same game: legs are correlated and books price them differently
                p = Parlay(book, list(combo), unit_size=cfg.unit())
                if p.ev_pct >= cfg.parlay_min_ev_pct:
                    found[p.key] = p
    # Parlays already posted are re-scored from today's prices directly, even if their legs
    # fell outside the top few used above.
    for key in keep:
        try:
            book, leg_keys = _parlay_leg_keys(key)
        except ValueError:
            continue
        legs = []
        for lk in leg_keys:
            rows = leg_row(by_key[lk], book) if lk in by_key else []
            if not rows:
                break
            legs.append((by_key[lk], rows[0][1], rows[0][2]))
        else:
            p = Parlay(book, legs, unit_size=cfg.unit())
            if p.key == key and p.ev_pct >= cfg.parlay_min_ev_pct:
                found[key] = p
    # Parlays already posted that still qualify first (replacing one with a slightly better
    # combo would kill a good card), then best first; a leg is never reused across alerts.
    out, used = [], set()
    for p in sorted(found.values(), key=lambda p: (p.key in keep, p.ev_pct), reverse=True):
        keys = {b.key for b, _, _ in p.legs}
        if keys & used:
            continue
        p.stake = round_stake(kelly_stake(p.fair_prob, p.price, cfg),
                              cfg.ev_bankroll * cfg.parlay_max_stake_pct / 100, cfg)
        out.append(p)
        used |= keys
        if len(out) >= cfg.parlay_max_alerts:
            break
    return out


def format_parlay_text(p: Parlay) -> str:
    legs = "\n".join(f"  {i}. {b.pick} {odds(pr)}{' (alternate line)' if p.book in b.alt_books else ''}  ({b.matchup})"
                     for i, (b, pr, _) in enumerate(p.legs, 1))
    return (f"📦 PARLAY +{p.ev_pct:.1f}% EV | {len(p.legs)} legs at {p.book} | pays {odds(p.price)}"
            f"  → stake {p.stake_label}\n{legs}")


def parlay_payload(p: Parlay, mention: str = "", gone_after: float | None = None,
                   first_seen: float | None = None) -> dict:
    if gone_after is not None:
        return _gone_card(f"❌ GONE after {_fmt_secs(gone_after)} · ~~📦 +{p.ev_pct:.1f}%~~ parlay at {p.book}",
                          "Ignore this one. A leg's price moved and the parlay isn't worth it anymore.")
    nums = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
    legs = "\n".join(
        f"{nums[i] if i < 4 else '•'} **{_link(b.pick, ln)} {odds(pr)}**{ALT_NOTE if p.book in b.alt_books else ''}"
        f"\n     {sport_icon(b.sport_key)} {b.matchup} · "
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


PARLAY_FIELDS = ["first_seen", "book", "legs", "price", "fair_prob", "best_ev_pct", "stake", "games", "legs_json"]
LEG_FIELDS = ["event_id", "sport_key", "matchup", "home_team", "away_team", "commence_time", "market",
              "outcome", "point", "n_outcomes", "player"]


class ParlayAlerter(Alerter):
    noun = "parlays"
    scoped = False
    book_pings = True
    one_alert = True
    log_fields = PARLAY_FIELDS
    log_on_open = True
    log_held = False

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

    def bet_identity(self, item) -> str:
        """The same legs are the same parlay at any book."""
        return "parlay|" + "|".join(b.key for b, _, _ in item.legs)

    def bet_expires(self, item) -> float:
        return max(Alerter.bet_expires(self, b) for b, _, _ in item.legs)

    def log_path(self) -> str:
        return self.cfg.parlay_log_file

    def webhook_for(self, item) -> str:
        return self.cfg.parlay_webhook_url or self.cfg.ev_webhook_url or self.cfg.webhook_url

    def row(self, op: OpenArb) -> dict:
        p = op.arb
        return {"book": p.book, "legs": " + ".join(f"{b.pick} {odds(pr)}" for b, pr, _ in p.legs),
                "price": round(p.price, 3), "fair_prob": round(p.fair_prob, 4),
                "best_ev_pct": round(op.best_pct, 2), "stake": p.stake,
                "games": " | ".join(b.matchup for b, _, _ in p.legs),
                # Each leg in full, so the parlay can be graded once its games finish.
                "legs_json": json.dumps([{**{f: _blank(getattr(b, f)) for f in LEG_FIELDS}, "price": pr}
                                         for b, pr, _ in p.legs])}


# --------------------------------------------------------------------------- +EV results

RESULT_FIELDS = EV_LOG_FIELDS + ["home_score", "away_score", "result", "profit", "kind",
                                 "closing_fair_odds", "clv_pct", "manual_legs", "actual"]
# "actual" of a run line or total (or a parlay with such a leg) on an MLB game that ended before
# the 9th: written once with no result, for you to check at your book (they usually void these).
RAIN_HELD = "rain-shortened"


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
    if row.get("market") == "parlay":
        return row["event_id"]   # book + the legs, not their prices: a re-alert is the same ticket
    player = f"{row['player']}|" if row.get("player") else ""
    return f"{row['event_id']}|{row['market']}|{player}{row['outcome']}|{row['point']}"


def _blank(v):
    return "" if v is None else v


def _parlay_row(r: dict) -> dict | None:
    """A logged parlay as a result-file row (None for parlays logged before legs were saved)."""
    try:
        legs = json.loads(r.get("legs_json") or "[]")
    except ValueError:
        return None
    if not legs:
        return None
    ids = "+".join(sorted(_bet_id(l) for l in legs))
    return {
        "first_seen": r["first_seen"], "event_id": f"parlay:{r['book']}:{ids}", "sport": "Parlay",
        "sport_key": "parlay", "matchup": r.get("games", ""), "home_team": "", "away_team": "",
        "commence_time": max(l["commence_time"] for l in legs), "live": False, "market": "parlay",
        "outcome": r.get("legs", ""), "point": "", "n_outcomes": "", "book": r["book"], "price": r["price"],
        "fair_odds": round(1 / float(r["fair_prob"]), 3) if float(r.get("fair_prob") or 0) else "",
        "best_ev_pct": r["best_ev_pct"], "stake": r["stake"], "player": "", "confidence": "",
        "kind": "parlay", "_legs": legs,
    }


def logged_bets(cfg: Config, since: datetime | None = None) -> dict[str, dict]:
    """Every alerted bet (first alert only), by bet id: +EV, outliers, props and parlays."""
    out: dict[str, dict] = {}
    closing = {r["bet_id"]: float(r["closing_fair_prob"]) for r in _read_csv(cfg.closing_file)}
    for kind, name in (("ev", cfg.ev_log_file), ("outlier", cfg.outlier_log_file),
                       ("parlay", cfg.parlay_log_file)):
        for r in _read_csv(name) if name else []:
            if kind == "parlay":
                r = _parlay_row(r)
                if r is None:
                    continue
            bid = _bet_id(r)
            if bid in out or (since and _parse_time(r["commence_time"]) < since):
                continue  # repeats count once
            if not cfg.counts(r.get("book", "")):
                continue  # not one of your books
            cp = closing.get(bid)
            out[bid] = {**r, "kind": kind,
                        "closing_fair_odds": round(1 / cp, 3) if cp else "",
                        "clv_pct": round(clv_pct(float(r["price"]), cp), 2) if cp else ""}
    return out


def _legs(r: dict) -> list[dict]:
    return r["_legs"] if r.get("kind") == "parlay" else [r]


def gradable(r: dict) -> bool:
    """Can the bot grade it? Main lines from final scores; player props from box scores, for the
    sports and stats it knows how to read (PROP_GRADING=espn)."""
    return all(not l.get("player") or box_supported(l) for l in _legs(r))


def settle_parlay(r: dict, finals: dict[str, dict[str, float]],
                  known: dict[str, str] | None = None, hold: set[str] = frozenset()) -> tuple[str, float] | None:
    """Grade a parlay from the legs' results (known = results of the legs' own alerts, by bet
    id) or final scores. One losing leg loses it (even before the other games end, and even if
    another leg is a prop); a pushed leg drops out, like at the books. None = not decided yet.
    hold = games whose run lines and totals can't be graded from the score (rain-shortened MLB)."""
    known = known or {}
    results = []
    for leg in r["_legs"]:
        if _bet_id(leg) in known:
            results.append(known[_bet_id(leg)])
            continue
        pts = finals.get(leg["event_id"])
        if (leg.get("player") or not pts or leg["home_team"] not in pts or leg["away_team"] not in pts
                or (leg["event_id"] in hold and leg.get("market") in ("spreads", "totals"))):
            results.append(None)
            continue
        results.append(settle({**leg, "stake": 1, "price": leg["price"]},
                              pts[leg["home_team"]], pts[leg["away_team"]])[0])
    stake = float(r["stake"])
    if "loss" in results:
        return "loss", -stake   # see parlay_unresolved() for a loss with legs still unknown
    if None in results or "unknown" in results:
        return None
    price = math.prod(float(l["price"]) for l, res in zip(r["_legs"], results) if res == "win")
    if "win" not in results:
        return "push", 0.0
    return "win", round(stake * (price - 1), 2)


def settle_pending(cfg: Config, api: "OddsAPI", now: datetime | None = None,
                   recent_hours: float | None = None) -> list[dict]:
    """Grade alerted bets whose games have finished; returns the newly graded rows. A run line or
    total on a game that ended early (rain) is written once too, with no result (RAIN_HELD), so
    later passes leave it alone; it isn't returned and never counts in the record.

    Uses the scores endpoint: 2 credits per sport, only for sports with an ungraded bet (or
    parlay leg) whose game should be over. With recent_hours, only games that ended within that
    many hours count (postponed or very old games are left to the daily pass); a parlay counts
    as one bet, so when its last game ends every leg's sport is fetched."""
    now = now or datetime.now(timezone.utc)
    graded_rows = _read_csv(cfg.ev_results_file)
    done = {_bet_id(r) for r in graded_rows}
    known = {_bet_id(r): r["result"] for r in graded_rows if r.get("result") in ("win", "loss", "push")}
    pending = {bid: r for bid, r in logged_bets(cfg, since=now - timedelta(days=3)).items()
               if bid not in done and (gradable(r) or r["kind"] == "parlay")}

    def end(leg: dict) -> datetime:
        return _parse_time(leg["commence_time"]) + timedelta(minutes=cfg.minutes_for(leg["sport_key"]))

    def recent(t: datetime) -> bool:
        return t <= now and (recent_hours is None or now - t <= timedelta(hours=recent_hours))

    sports = set()
    need: dict[str, dict] = {}         # event id -> a leg that needs that game's final score
    kinds: dict[str, set[str]] = {}    # event id -> the bet types waiting on it
    for r in pending.values():
        legs = _legs(r)
        if r["kind"] == "parlay" and "loss" in (known.get(_bet_id(l)) for l in legs):
            continue   # already lost through a leg's own alert: no scores needed
        last = max(end(l) for l in legs)
        for leg in legs:
            if leg.get("player") or (r["kind"] == "parlay" and _bet_id(leg) in known):
                continue
            if recent(end(leg)) or (end(leg) <= now and recent(last)):
                sports.add(leg["sport_key"])
                need.setdefault(leg["event_id"], leg)
                kinds.setdefault(leg["event_id"], set()).add(leg.get("market", ""))
    finals: dict[str, dict[str, float]] = {}
    for sport in sorted(sports):
        try:
            for g in api.scores(sport):
                if g.get("completed") and g.get("scores"):
                    finals[g["id"]] = {x["name"]: float(x["score"]) for x in g["scores"]}
        except Exception as e:  # noqa: BLE001
            print(f"! Couldn't load {sport} scores: {e}", file=sys.stderr)
    # Free: rain-shortened MLB games keep their run lines and totals out of grading (every mode),
    # and FREE_SCORES=shadow logs whether ESPN's finals agree with these.
    hold, rain = espn_checks(cfg, need, kinds, finals, now)
    graded = []
    rained: dict[str, dict] = {}   # games whose rain-held bets were written this pass
    for r in pending.values():
        extra = {}
        if r.get("player"):
            # Player props: from the box score (free, ESPN), once the game should be over.
            if not recent(end(r)):
                continue
            res, said = grade_prop(r)
            if res is None:
                continue
            extra = {"actual": said}
        elif r["kind"] == "parlay":
            res = settle_parlay(r, finals, known, hold)
            if res is None:
                # Decided but for legs on a game that ended early (rain)? Then it's written once, for
                # you to check (books usually void those legs): legs held count as decided here.
                held = {_bet_id(l): "push" for l in r["_legs"] if _bet_id(l) not in known
                        and l["event_id"] in rain and l.get("market") in ("spreads", "totals")}
                if not held or settle_parlay(r, finals, {**known, **held}, hold) is None:
                    continue
                res, extra = ("", ""), {"actual": RAIN_HELD}
                rained.update((l["event_id"], l) for l in r["_legs"] if _bet_id(l) in held)
            elif any(_bet_id(l) not in known and (l.get("player") or l["event_id"] not in finals
                                                 or (l["event_id"] in hold and l.get("market") in ("spreads", "totals")))
                   for l in r["_legs"]):
                extra["manual_legs"] = 1   # lost on one leg while another can't be graded: it stays
                                           # out of the record (its wins couldn't be counted either)
        else:
            pts = finals.get(r["event_id"])
            if not pts or r["home_team"] not in pts or r["away_team"] not in pts:
                continue
            extra = {"home_score": pts[r["home_team"]], "away_score": pts[r["away_team"]]}
            if r["event_id"] in hold and r["market"] in ("spreads", "totals"):
                if r["event_id"] not in rain:
                    continue   # ESPN couldn't be asked in time: next pass
                # A rain-shortened MLB game: books usually void these. Written once, for you to check.
                res, extra["actual"] = ("", ""), RAIN_HELD
                rained[r["event_id"]] = r
            else:
                res = settle(r, pts[r["home_team"]], pts[r["away_team"]])
                if res[0] == "unknown":
                    continue
        row = {**{k: v for k, v in r.items() if k != "_legs"}, **extra, "result": res[0], "profit": res[1]}
        append_csv(cfg.ev_results_file, RESULT_FIELDS, row)
        if not res[0]:
            continue
        graded.append(row)
        if r["kind"] != "parlay":
            known[_bet_id(r)] = res[0]
    for leg in rained.values():
        print(f"  ⚾ {leg['away_team']} @ {leg['home_team']} ended before the 9th: run lines and totals on it "
              f"are left for you to check (books usually void them).", flush=True)
    return graded


def _record(rows: list[dict]) -> tuple[int, int, int, float, float]:
    """(wins, losses, pushes, profit, staked) for graded rows."""
    rows = [r for r in rows if r.get("result") in ("win", "loss", "push") and not r.get("manual_legs")]
    w = sum(r["result"] == "win" for r in rows)
    l = sum(r["result"] == "loss" for r in rows)
    staked = sum(float(r["stake"]) for r in rows if r["result"] != "push")
    return w, l, len(rows) - w - l, sum(float(r["profit"]) for r in rows), staked


def signed_money(x: float) -> str:
    return ("+" if x >= 0 else "−") + money(abs(x))


def record_line(rows: list[dict]) -> str:
    w, l, pu, profit, staked = _record(rows)
    if not w + l + pu:
        return "no graded bets yet"
    roi = f" (ROI {profit / staked * 100:+.1f}%)" if staked else ""
    return f"{w}-{l}" + (f"-{pu}" if pu else "") + f" · {signed_money(profit)} on {money(staked)} staked{roi}"


def ev_record(cfg: Config, days: int | None = None, kinds: tuple[str, ...] = ("ev", ""),
              live: bool | None = None) -> str:
    """The record of these alert types (live=False: pre-game bets only; None: both)."""
    rows = [r for r in _read_csv(cfg.ev_results_file) if r.get("kind", "") in kinds and not r.get("manual_legs")
            and cfg.counts(r.get("book", ""))
            and (live is None or (str(r.get("live")).lower() == "true") == live)]
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


# --------------------------------------------------------------------------- player props: box scores

PROP_GRADING = True   # set from PROP_GRADING in .env at startup
ESPN_BASE = os.environ.get("ESPN_API_BASE", "https://site.api.espn.com/apis/site/v2/sports")
ESPN_LEAGUES = {   # Odds API sport -> (ESPN league path, extra scoreboard query)
    "americanfootball_nfl": ("football/nfl", ""),
    "americanfootball_ncaaf": ("football/college-football", "groups=80&limit=500"),
    "basketball_nba": ("basketball/nba", ""),
    "basketball_wnba": ("basketball/wnba", ""),
    "basketball_ncaab": ("basketball/mens-college-basketball", "groups=50&limit=500"),
    "icehockey_nhl": ("hockey/nhl", ""),
    "baseball_mlb": ("baseball/mlb", ""),
}


def _t(labels, keys=(), cats=None, part=None, optional=False):
    """One box-score column: its labels and keys, the stat groups it's in (None = any),
    which part of "10-21" / "12/20" (0 or 1), or "outs" for innings pitched."""
    return (cats, labels, keys, part, optional)


_PTS, _REB, _AST = _t(("PTS",), ("points",)), _t(("REB",), ("rebounds", "totalRebounds")), _t(("AST",), ("assists",))
_G, _A = _t(("G",), ("goals",)), _t(("A",), ("assists",))
BOX_STATS = {   # (sport family, prop market) -> columns added together
    ("basketball", "player_points"): [_PTS],
    ("basketball", "player_rebounds"): [_REB],
    ("basketball", "player_assists"): [_AST],
    ("basketball", "player_threes"): [_t(("3PT",), ("threePointFieldGoalsMade-threePointFieldGoalsAttempted",), part=0)],
    ("basketball", "player_blocks"): [_t(("BLK",), ("blocks",))],
    ("basketball", "player_steals"): [_t(("STL",), ("steals",))],
    ("basketball", "player_turnovers"): [_t(("TO",), ("turnovers",))],
    ("basketball", "player_points_rebounds_assists"): [_PTS, _REB, _AST],
    ("basketball", "player_points_rebounds"): [_PTS, _REB],
    ("basketball", "player_points_assists"): [_PTS, _AST],
    ("basketball", "player_rebounds_assists"): [_REB, _AST],
    ("icehockey", "player_goals"): [_G],
    ("icehockey", "player_assists"): [_A],
    ("icehockey", "player_points"): [_G, _A],
    ("icehockey", "player_shots_on_goal"): [_t(("S", "SOG"), ("shotsTotal", "shots", "shotsOnGoal"))],
    ("icehockey", "player_blocked_shots"): [_t(("BS",), ("blockedShots",))],
    ("icehockey", "player_total_saves"): [_t(("SV",), ("saves",))],
    ("americanfootball", "player_pass_yds"): [_t(("YDS",), ("passingYards",), ("passing",))],
    ("americanfootball", "player_pass_tds"): [_t(("TD",), ("passingTouchdowns",), ("passing",))],
    ("americanfootball", "player_pass_completions"): [_t(("C/ATT",), ("completions/passingAttempts",), ("passing",), 0)],
    ("americanfootball", "player_pass_attempts"): [_t(("C/ATT",), ("completions/passingAttempts",), ("passing",), 1)],
    ("americanfootball", "player_pass_interceptions"): [_t(("INT",), ("interceptions",), ("passing",))],
    ("americanfootball", "player_rush_yds"): [_t(("YDS",), ("rushingYards",), ("rushing",))],
    ("americanfootball", "player_rush_attempts"): [_t(("CAR",), ("rushingAttempts",), ("rushing",))],
    ("americanfootball", "player_receptions"): [_t(("REC",), ("receptions",), ("receiving",))],
    ("americanfootball", "player_reception_yds"): [_t(("YDS",), ("receivingYards",), ("receiving",))],
    ("americanfootball", "player_anytime_td"): [   # any TD a player scores counts, returns and defense too
        _t(("TD",), ("rushingTouchdowns",), ("rushing",), optional=True),
        _t(("TD",), ("receivingTouchdowns",), ("receiving",), optional=True),
        _t(("TD",), ("kickReturnTouchdowns",), ("kickreturns",), optional=True),
        _t(("TD",), ("puntReturnTouchdowns",), ("puntreturns",), optional=True)],
    ("baseball", "batter_hits"): [_t(("H",), ("hits",), ("batting",))],
    ("baseball", "batter_total_bases"): [_t(("TB",), ("totalBases",), ("batting",))],   # MLB's own box score
    ("baseball", "batter_home_runs"): [_t(("HR",), ("homeRuns",), ("batting",))],
    ("baseball", "batter_rbis"): [_t(("RBI",), ("RBIs", "rbis"), ("batting",))],
    ("baseball", "batter_runs_scored"): [_t(("R",), ("runs",), ("batting",))],
    ("baseball", "batter_walks"): [_t(("BB",), ("walks",), ("batting",))],
    ("baseball", "batter_strikeouts"): [_t(("K", "SO"), ("strikeouts",), ("batting",))],
    ("baseball", "pitcher_strikeouts"): [_t(("K", "SO"), ("strikeouts",), ("pitching",))],
    ("baseball", "pitcher_hits_allowed"): [_t(("H",), ("hits",), ("pitching",))],
    ("baseball", "pitcher_walks"): [_t(("BB",), ("walks",), ("pitching",))],
    ("baseball", "pitcher_earned_runs"): [_t(("ER",), ("earnedRuns",), ("pitching",))],
    ("baseball", "pitcher_outs"): [_t(("IP",), ("fullInnings.partInnings", "inningsPitched"), ("pitching",), "outs")],
    # Added with the bigger plan's prop types: sums of columns already read.
    ("americanfootball", "player_rush_reception_yds"): [_t(("YDS",), ("rushingYards",), ("rushing",), optional=True),
                                                        _t(("YDS",), ("receivingYards",), ("receiving",), optional=True)],
    ("basketball", "player_blocks_steals"): [_t(("BLK",), ("blocks",)), _t(("STL",), ("steals",))],
    ("icehockey", "player_goal_scorer_anytime"): [_G],   # Yes/No: at least one goal
    ("baseball", "batter_hits_runs_rbis"): [_t(("H",), ("hits",), ("batting",)), _t(("R",), ("runs",), ("batting",)),
                                            _t(("RBI",), ("RBIs", "rbis"), ("batting",))],
    # Oct 9: touchdowns by kind, the same columns as an anytime TD (read like rushing and receiving yards).
    ("americanfootball", "player_rush_tds"): [_t(("TD",), ("rushingTouchdowns",), ("rushing",))],
    ("americanfootball", "player_reception_tds"): [_t(("TD",), ("receivingTouchdowns",), ("receiving",))],
    # Oct 9, more props, from columns ESPN's box scores have (checked against a real one): longest plays, yards and
    # TDs added across passing / rushing / receiving, defense (a defender missing from a group had none of it) and
    # the kicker.
    ("americanfootball", "player_rush_longest"): [_t(("LONG",), ("longRushing",), ("rushing",))],
    ("americanfootball", "player_reception_longest"): [_t(("LONG",), ("longReception",), ("receiving",))],
    ("americanfootball", "player_pass_rush_yds"): [_t(("YDS",), ("passingYards",), ("passing",)),
                                                   _t(("YDS",), ("rushingYards",), ("rushing",), optional=True)],
    ("americanfootball", "player_pass_rush_reception_yds"): [
        _t(("YDS",), ("passingYards",), ("passing",), optional=True),
        _t(("YDS",), ("rushingYards",), ("rushing",), optional=True),
        _t(("YDS",), ("receivingYards",), ("receiving",), optional=True)],
    ("americanfootball", "player_pass_rush_reception_tds"): [
        _t(("TD",), ("passingTouchdowns",), ("passing",), optional=True),
        _t(("TD",), ("rushingTouchdowns",), ("rushing",), optional=True),
        _t(("TD",), ("receivingTouchdowns",), ("receiving",), optional=True)],
    ("americanfootball", "player_sacks"): [_t(("SACKS",), ("sacks",), ("defensive",), optional=True)],
    ("americanfootball", "player_solo_tackles"): [_t(("SOLO",), ("soloTackles",), ("defensive",), optional=True)],
    ("americanfootball", "player_tackles_assists"): [_t(("TOT",), ("totalTackles",), ("defensive",), optional=True)],
    ("americanfootball", "player_defensive_interceptions"): [_t(("INT",), ("interceptions",), ("interceptions",),
                                                                optional=True)],
    ("americanfootball", "player_field_goals"): [_t(("FG",), ("fieldGoalsMade/fieldGoalAttempts",), ("kicking",), 0)],
    ("americanfootball", "player_pats"): [_t(("XP",), ("extraPointsMade/extraPointAttempts",), ("kicking",), 0)],
    ("americanfootball", "player_kicking_points"): [_t(("PTS",), ("totalKickingPoints",), ("kicking",))],
    ("basketball", "player_field_goals"): [_t(("FG",), ("fieldGoalsMade-fieldGoalsAttempted",), part=0)],
    ("basketball", "player_frees_made"): [_t(("FT",), ("freeThrowsMade-freeThrowsAttempted",), part=0)],
    ("basketball", "player_frees_attempts"): [_t(("FT",), ("freeThrowsMade-freeThrowsAttempted",), part=1)],
    # Yes/No: 10+ in two (three) of points, rebounds, assists, steals and blocks (see _read_stat).
    ("basketball", "player_double_double"): [_PTS, _REB, _AST, _t(("STL",), ("steals",)), _t(("BLK",), ("blocks",))],
    ("basketball", "player_triple_double"): [_PTS, _REB, _AST, _t(("STL",), ("steals",)), _t(("BLK",), ("blocks",))],
}
# Optional columns of which at least one must be in the box score: a player in neither group is left to check by hand.
NEED_ONE = {"player_rush_reception_yds", "player_pass_rush_reception_yds", "player_pass_rush_reception_tds"}
# Yes/No props on how many of their columns reach 10: market -> how many must.
DOUBLES = {"player_double_double": 2, "player_triple_double": 3}
# Total bases isn't in the ESPN box score (no doubles/triples column); MLB's own box score has it.


def _family(sport_key: str) -> str:
    return sport_key.split("_", 1)[0]


def box_supported(leg: dict) -> bool:
    sport = leg.get("sport_key")
    if not PROP_GRADING or (sport not in ESPN_LEAGUES and sport not in BOX_SOURCES):
        return False
    if leg.get("market") == "batter_total_bases":
        return sport in BOX_SOURCES   # only MLB's own box score has total bases
    return (_family(sport), leg.get("market")) in BOX_STATS


def _words(name: str) -> list[str]:
    """Lowercase ASCII words without punctuation or Jr./III: "D'Andre Swift Jr." -> ["dandre", "swift"]."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return [w for w in re.sub(r"[^a-z0-9 ]", "", s.replace("-", " ").replace(".", " ")).split()
            if w not in ("jr", "sr", "ii", "iii", "iv", "v")]


def _norm(name: str) -> str:
    return "".join(_words(name))


_ESPN_CACHE: dict[str, tuple[float, dict]] = {}


def _get_json(url: str, timeout: float = 20) -> dict:
    """GET a public JSON page (free stats sites and Kalshi, no key)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "arbbot/3.0 (bet results)", "Accept-Encoding": "gzip", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw)


ESPN_HOSTS = [ESPN_BASE, ESPN_BASE.replace("://site.api.", "://site.web.api.")]


def _espn_get(path: str) -> dict:
    """GET an ESPN site-API page, trying its second host if the first refuses (403)."""
    last: Exception | None = None
    for base in dict.fromkeys(ESPN_HOSTS):
        try:
            return _get_json(f"{base}/{path}")
        except urllib.error.HTTPError as e:
            if e.code not in (403, 404):
                raise
            last = e
    raise last


def _espn(path: str, ttl: float = 600) -> dict:
    return _cached("espn:" + path, lambda: _espn_get(path), ttl)


def _cached(key: str, fetch, ttl: float) -> dict:
    hit = _ESPN_CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    data = fetch()
    _ESPN_CACHE[key] = (time.time(), data)
    return data


def _web(url: str, ttl: float = 600) -> dict:
    return _cached(url, lambda: _get_json(url), ttl)


def _team_match(odds_name: str, team: dict) -> int:
    """2 = same full name, 1 = same nickname ("Los Angeles Clippers" / "LA Clippers"), 0 = no."""
    n = _norm(odds_name)
    full = {_norm(team.get("displayName", "")), _norm(f"{team.get('location', '')} {team.get('name', '')}")}
    if n in full - {""}:
        return 2
    nick = _norm(team.get("name", "") or team.get("shortDisplayName", ""))
    return 1 if nick and n.endswith(nick) else 0


def espn_event_id(sport_key: str, home: str, away: str, commence: str) -> str | None:
    """The ESPN game for an Odds API game: both teams match, within 12 hours of the start."""
    ev = _espn_game(sport_key, home, away, commence)
    return ev["id"] if ev else None


ESPN_FAIL_SECONDS = 300   # a scoreboard page that just failed isn't asked again for this long (grading)
ESPN_PASS_SECONDS = 15    # a grading pass stops asking ESPN about more games after this long
_ESPN_FAILED: dict[str, float] = {}


def _espn_quick(path: str, ttl: float = 300) -> dict:
    """_espn, but a page that just failed isn't asked again for ESPN_FAIL_SECONDS: an ESPN outage
    mustn't cost every grading pass a 20-second timeout per game while the checks wait."""
    if time.time() - _ESPN_FAILED.get(path, 0.0) < ESPN_FAIL_SECONDS:
        raise OSError("ESPN didn't answer a few minutes ago; not asking again yet")
    try:
        return _espn(path, ttl)
    except Exception:
        _ESPN_FAILED[path] = time.time()
        raise


def _espn_game(sport_key: str, home: str, away: str, commence: str, fetch=None) -> dict | None:
    """The one ESPN scoreboard event for an Odds API game (both teams match, within 12 hours of
    the start), or None: none, or two that fit equally (nickname-only matches, doubleheaders)."""
    fetch = fetch or _espn
    league, extra = ESPN_LEAGUES[sport_key]
    start = _parse_time(commence)
    days = sorted({start.astimezone(ZoneInfo("America/New_York")).date(), start.date()})
    found: list[tuple[int, str]] = []
    by_id: dict[str, dict] = {}
    for d in days:
        sb = fetch(f"{league}/scoreboard?dates={d:%Y%m%d}" + (f"&{extra}" if extra else ""))
        for ev in sb.get("events", []):
            try:
                comp = ev["competitions"][0]
                teams = [c["team"] for c in comp["competitors"]]
                when = _parse_time(ev.get("date") or comp.get("date"))
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if abs(when - start) > timedelta(hours=12) or len(teams) != 2:
                continue
            a, b = teams
            score = max(min(_team_match(home, a), _team_match(away, b)),   # either way round:
                        min(_team_match(home, b), _team_match(away, a)))   # neutral sites
            if score:
                found.append((score, ev["id"]))
                by_id[ev["id"]] = ev
    best = [eid for sc, eid in set(found) if sc == max((x for x, _ in found), default=0)]
    return by_id[best[0]] if len(best) == 1 else None   # nickname-only matches must be unambiguous


ESPN_FINAL = {"STATUS_FINAL", "STATUS_FINAL_OT", "STATUS_FULL_TIME"}   # any other status isn't a final here
ESPN_OFF = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_SUSPENDED", "STATUS_FORFEIT",
            "STATUS_ABANDONED", "STATUS_DELAYED"}
NO_TIES = {"icehockey", "basketball", "baseball"}   # a tied final here means the feed is wrong


def espn_final(sport_key: str, home: str, away: str, commence: str) -> tuple[dict[str, float] | None, str]:
    """({home: score, away: score}, "") once ESPN's free scoreboard calls the game final and it
    checks out, else (None, why). A wrong grade is worse than none, so anything unexpected is None:

    - final: completed, state "post" and a final status (an unknown status isn't one; the reason
      names it, so it can be added);
    - both teams match one way round only (neutral sites are fine, a swap is not);
    - each team's periods add up to its score (not hockey: a shootout adds a goal);
    - no tied final in hockey, basketball or baseball; hockey needs ESPN's winner flag, and the
      flag must agree with the score wherever it's given;
    - baseball: the team that batted first (the one with more innings, whichever way ESPN lists
      the teams) batted fewer than 9 innings = "shortened game" (rain): books void run lines and
      totals then, so those are checked by hand."""
    ev = _espn_game(sport_key, home, away, commence, fetch=_espn_quick)
    if ev is None:
        return None, "not found"
    comp = (ev.get("competitions") or [{}])[0]
    st = (comp.get("status") or ev.get("status") or {}).get("type") or {}
    name = str(st.get("name") or "?")
    if name in ESPN_OFF:
        return None, f"postponed: {name}"
    if not (st.get("completed") is True and st.get("state") == "post" and name in ESPN_FINAL):
        return None, f"not final: {name}"
    sides = comp.get("competitors") or []
    if len(sides) != 2:   # (_espn_game already skips these; this doesn't count on it)
        return None, "not two teams"
    a, b = sides
    straight = min(_team_match(home, a.get("team", {})), _team_match(away, b.get("team", {})))
    swapped = min(_team_match(home, b.get("team", {})), _team_match(away, a.get("team", {})))
    if straight == swapped:
        return None, "can't tell which team is which"
    h, w = (a, b) if straight > swapped else (b, a)
    fam = _family(sport_key)
    out: dict[str, float] = {}
    innings = 0
    for team, c in ((home, h), (away, w)):
        pts = _num(str(c.get("score", "")))
        if pts is None or pts < 0:
            return None, "no score"
        periods = [_num(str(x.get("value", ""))) for x in c.get("linescores") or []]
        if periods and None not in periods and sum(periods) != pts and fam != "icehockey":
            return None, f"{team}: periods add up to {sum(periods):g}, not {pts:g}"
        innings = max(innings, len(periods))   # the team that batted first has the most
        out[team] = pts
    if out[home] == out[away] and fam in NO_TIES:
        return None, "tied score"
    if fam == "icehockey" and "winner" not in (h if out[home] > out[away] else w):
        return None, "no winner flag"
    for c, won in ((h, out[home] > out[away]), (w, out[away] > out[home])):
        if "winner" in c and out[home] != out[away] and bool(c["winner"]) != won:
            return None, "ESPN's winner flag disagrees with the score"
    if fam == "baseball" and 0 < innings < 9:
        return None, "shortened game"
    return out, ""


SCORE_CHECK_FIELDS = ["checked_at", "event_id", "sport_key", "matchup", "odds_api", "espn", "verdict"]


def espn_checks(cfg: Config, need: dict[str, dict], kinds: dict[str, set[str]],
                finals: dict[str, dict[str, float]], now: datetime) -> tuple[set[str], set[str]]:
    """Ask ESPN's free scoreboard about games the Odds API has a final for. Returns (hold, rain):
    hold = the games whose run lines and totals must not be graded now, rain = those of them that
    ended before the 9th (MLB: books void those bets; the moneyline still stands). The rest of hold
    are games ESPN couldn't be asked about in time (next pass).

    FREE_SCORES=shadow also logs, once per game, whether ESPN's final agrees with the Odds API's
    (SCORE_CHECK_FILE; a disagreement is printed too). Grading still uses the Odds API's scores."""
    logged = None
    if cfg.free_scores == "shadow" and cfg.score_check_file:
        try:
            logged = {r.get("event_id") for r in _read_csv(cfg.score_check_file)}
        except (OSError, csv.Error, UnicodeDecodeError) as e:
            print(f"  ! Couldn't read {cfg.score_check_file}: {e!r:.100}", file=sys.stderr)
    ask = []
    for gid, leg in need.items():
        if gid not in finals or leg.get("sport_key") not in ESPN_LEAGUES:
            continue
        rain = _family(leg["sport_key"]) == "baseball" and bool(kinds.get(gid, set()) & {"spreads", "totals"})
        shadow = logged is not None and gid not in logged
        if rain or shadow:
            ask.append((not rain, gid, leg, shadow))
    ask.sort(key=lambda x: x[0])   # rain checks first: they change what gets graded
    hold: set[str] = set()
    shortened: set[str] = set()
    t0, failed = time.monotonic(), False
    for i, (no_rain, gid, leg, shadow) in enumerate(ask):
        if time.monotonic() - t0 > ESPN_PASS_SECONDS:
            hold |= {g for nr, g, _, _ in ask[i:] if not nr}   # unchecked run lines and totals wait
            break
        try:
            pts, why = espn_final(leg["sport_key"], leg["home_team"], leg["away_team"], leg["commence_time"])
        except Exception as e:  # noqa: BLE001 - ESPN down or changed: grade from the Odds API as before
            if not failed:   # (once a pass)
                print(f"  ! ESPN scoreboard: {e!r:.120}", file=sys.stderr)
            failed = True
            continue   # (no row: an outage says nothing about ESPN's finals)
        if why == "shortened game":
            hold.add(gid)
            shortened.add(gid)
        if not shadow:
            continue
        api = finals[gid]
        verdict = ("agree" if pts and all(api.get(t) == v for t, v in pts.items())
                   else "disagree" if pts else f"espn: {why}")
        append_csv(cfg.score_check_file, SCORE_CHECK_FIELDS, {
            "checked_at": now.isoformat(timespec="seconds"), "event_id": gid, "sport_key": leg["sport_key"],
            "matchup": f"{leg['away_team']} @ {leg['home_team']}", "odds_api": json.dumps(api),
            "espn": json.dumps(pts or {}), "verdict": verdict})
        if verdict == "disagree":
            print(f"! ESPN and the Odds API disagree on {leg['away_team']} @ {leg['home_team']}: "
                  f"ESPN {pts}, Odds API {api} (graded from the Odds API)", file=sys.stderr)
    return hold, shortened


def score_check_line(cfg: Config) -> str:
    """'ESPN agreed on 48 of 50 finals (0 disagreements, ...)' from SCORE_CHECK_FILE ('' if none)."""
    try:
        rows = _read_csv(cfg.score_check_file) if cfg.score_check_file else []
    except (OSError, csv.Error, UnicodeDecodeError):
        return ""
    if not rows:
        return ""
    agree = sum(r.get("verdict") == "agree" for r in rows)
    disagree = sum(r.get("verdict") == "disagree" for r in rows)
    return (f"ESPN's free scoreboard agreed on {agree} of {len(rows)} finals ({disagree} disagreement"
            f"{'' if disagree == 1 else 's'}; the rest it couldn't grade). Details: {cfg.score_check_file}")


def _num(raw: str, part=None) -> float | None:
    raw = (raw or "").strip()
    if not raw or raw in ("--", "-"):
        return None
    try:
        if part == "outs":   # innings pitched: 6.1 = 6 innings and 1 out
            whole, _, frac = raw.partition(".")
            return int(whole) * 3 + int(frac or 0)
        if part is not None:
            return float(re.split(r"[-/]", raw)[part])
        return float(raw)
    except (ValueError, IndexError):
        return None


def parse_box(summary: dict) -> dict:
    """ESPN game summary -> {"final", "teams": {team id: {"name", "score", "players": {norm name:
    {"name", "dnp", "groups": {group name: {LABEL or key: raw}}}}}}}"""
    comp = (summary.get("header", {}).get("competitions") or [{}])[0]
    status = comp.get("status", {}).get("type", {})
    teams: dict[str, dict] = {}
    for c in comp.get("competitors", []):
        tid = str(c.get("id") or c.get("team", {}).get("id"))
        teams[tid] = {"name": c.get("team", {}).get("displayName", ""), "score": _num(str(c.get("score", ""))),
                      "players": {}}
    for side in summary.get("boxscore", {}).get("players", []):
        tid = str(side.get("team", {}).get("id"))
        team = teams.setdefault(tid, {"name": side.get("team", {}).get("displayName", ""), "score": None,
                                      "players": {}})
        for group in side.get("statistics", []):
            gname = (group.get("name") or group.get("text") or "").lower()
            labels = [str(x).upper() for x in (group.get("labels") or group.get("names") or [])]
            keys = [str(x) for x in (group.get("keys") or [])]
            for a in group.get("athletes", []):
                who = a.get("athlete", {})
                name = who.get("displayName") or who.get("fullName") or ""
                if not name:
                    continue
                p = team["players"].setdefault(_norm(name), {"name": name, "dnp": True, "groups": {}})
                stats = a.get("stats") or []
                if stats and not a.get("didNotPlay"):
                    p["dnp"] = False
                    cols = p["groups"].setdefault(gname, {})
                    for i, raw in enumerate(stats):
                        if i < len(labels):
                            cols.setdefault(labels[i], raw)
                        if i < len(keys):
                            cols.setdefault(keys[i], raw)
    plays = summary.get("scoringPlays")
    tds = None if plays is None else sum(
        1 for sp in plays if "touchdown" in str((sp.get("scoringType") or {}).get("name", "")).lower()
        or str((sp.get("scoringType") or {}).get("abbreviation", "")).upper() == "TD")
    return {"final": bool(status.get("completed")) or status.get("state") == "post", "teams": teams,
            "touchdowns": tds}


_DEFENSE_TD = [_t(("TD",), ("interceptionTouchdowns",), ("interceptions",), optional=True),
               _t(("TD",), ("defensiveTouchdowns",), ("defensive",), optional=True)]


def _read_stat(player: dict, market: str, sport_key: str) -> float | None:
    """A player's number for a prop market. For anytime TD, a pick-six shows up both as an
    interception TD and a defensive TD, so the larger of the two counts, not both."""
    terms = BOX_STATS[(_family(sport_key), market)]
    if market in NEED_ONE and all(_read(player, [t[:4] + (False,)]) is None for t in terms):
        return None
    if market in DOUBLES:   # 1 = yes, 0 = no
        values = [_read(player, [t]) for t in terms]
        return None if None in values else float(sum(v >= 10 for v in values) >= DOUBLES[market])
    value = _read(player, terms)
    if market == "player_anytime_td" and value is not None:
        value += max((_read(player, [t]) or 0) for t in _DEFENSE_TD)
    return value


def _read(player: dict, terms: list) -> float | None:
    total, found = 0.0, False
    for cats, labels, keys, part, optional in terms:
        value = None
        for gname, cols in player["groups"].items():
            if cats and not any(c in gname for c in cats):
                continue
            raw = next((cols[x] for x in [*labels, *keys] if x in cols), None)
            if raw is not None:
                value = _num(raw, part)
                break
        if value is None and not optional:
            return None
        if value is not None:
            total, found = total + value, True
    return total if found or all(t[4] for t in terms) else None


def box_checks(sport_key: str, box: dict) -> list[str]:
    """Self-checks that the columns were read right; returns the problems (empty = trust it).
    Points add up to the score (NBA), goals to the score (NHL, give or take a shootout goal),
    runs to the score (MLB), receptions to completions (football)."""
    fam, problems = _family(sport_key), []
    for team in box["teams"].values():
        players = [p for p in team["players"].values() if not p["dnp"]]
        if not players:
            problems.append(f"{team['name']}: no players in the box score")
            continue
        def total(terms):
            vals = [_read(p, terms) for p in players]
            return sum(v for v in vals if v is not None), sum(v is not None for v in vals)
        score = team["score"]
        if fam == "basketball":
            pts, n = total([_PTS])
            if not n or score is None or pts != score:
                problems.append(f"{team['name']}: players' points {pts:g} vs score {score}")
        elif fam == "icehockey":
            goals, n = total([_G])
            if score is None or not 0 <= score - goals <= 1:
                problems.append(f"{team['name']}: players' goals {goals:g} vs score {score}")
        elif fam == "baseball":
            runs, n = total([_t(("R",), ("runs",), ("batting",))])
            if not n or score is None or runs != score:
                problems.append(f"{team['name']}: players' runs {runs:g} vs score {score}")
        elif fam == "americanfootball":
            rec, n1 = total(BOX_STATS[("americanfootball", "player_receptions")])
            comp, n2 = total(BOX_STATS[("americanfootball", "player_pass_completions")])
            if not n1 or not n2 or rec != comp:
                problems.append(f"{team['name']}: receptions {rec:g} vs completions {comp:g}")
    return problems



# ---- the leagues' own stats sites (free, public), tried before ESPN

NHL_API = os.environ.get("NHL_API_BASE", "https://api-web.nhle.com/v1")
MLB_API = os.environ.get("MLB_API_BASE", "https://statsapi.mlb.com/api/v1")


def _game_days(commence: str) -> list:
    start = _parse_time(commence)
    return sorted({start.astimezone(ZoneInfo("America/New_York")).date(), start.date()})


def _pick_game(games: list[tuple[str, datetime, list[dict]]], home: str, away: str, commence: str) -> str | None:
    """The one game whose two teams match (either way round), within 12 hours of the start."""
    start, found = _parse_time(commence), []
    for gid, when, teams in games:
        if abs(when - start) > timedelta(hours=12) or len(teams) != 2:
            continue
        a, b = teams
        score = max(min(_team_match(home, a), _team_match(away, b)), min(_team_match(home, b), _team_match(away, a)))
        if score:
            found.append((score, gid))
    best = {gid for sc, gid in found if sc == max((x for x, _ in found), default=0)}
    return best.pop() if len(best) == 1 else None


def _txt(v) -> str:
    return v.get("default", "") if isinstance(v, dict) else str(v or "")


def _nhl_box(sport_key: str, home: str, away: str, commence: str) -> tuple[str | None, dict | None]:
    """NHL.com: the day's scores, then the game's box score and its roster (for full names)."""
    games = []
    for d in _game_days(commence):
        for g in _web(f"{NHL_API}/score/{d:%Y-%m-%d}").get("games", []):
            teams = [{"name": _txt(t.get("commonName") or t.get("name")), "location": _txt(t.get("placeName")),
                      "displayName": " ".join(x for x in (_txt(t.get("placeName")), _txt(t.get("commonName") or t.get("name"))) if x)}
                     for t in (g.get("homeTeam", {}), g.get("awayTeam", {}))]
            games.append((str(g.get("id")), _parse_time(g.get("startTimeUTC", "1970-01-01T00:00:00Z")), teams))
    gid = _pick_game(games, home, away, commence)
    if not gid:
        return None, None
    raw = _web(f"{NHL_API}/gamecenter/{gid}/boxscore", ttl=300)
    try:
        roster = _web(f"{NHL_API}/gamecenter/{gid}/play-by-play", ttl=3600).get("rosterSpots", [])
    except Exception:  # noqa: BLE001 - without full names, players just aren't matched (manual)
        roster = []
    full = {r.get("playerId"): f"{_txt(r.get('firstName'))} {_txt(r.get('lastName'))}".strip() for r in roster}
    teams = {}
    for side in ("homeTeam", "awayTeam"):
        t = raw.get(side, {})
        team = teams.setdefault(str(t.get("id", side)), {"name": _txt(t.get("commonName")) or side,
                                                         "score": _num(str(t.get("score", ""))), "players": {}})
        stats = raw.get("playerByGameStats", {}).get(side, {})
        for group in ("forwards", "defense", "goalies"):
            for p in stats.get(group, []):
                name = full.get(p.get("playerId")) or _txt(p.get("name"))
                cols = {"G": p.get("goals"), "A": p.get("assists"), "S": p.get("sog", p.get("shots")),
                        "BS": p.get("blockedShots")}
                if group == "goalies":
                    saves = p.get("saves")
                    if saves is None and p.get("shotsAgainst") is not None and p.get("goalsAgainst") is not None:
                        saves = p["shotsAgainst"] - p["goalsAgainst"]
                    if saves is None and "/" in str(p.get("saveShotsAgainst", "")):
                        saves = str(p["saveShotsAgainst"]).split("/")[0]
                    cols = {"G": p.get("goals", 0), "A": p.get("assists", 0), "SV": saves}
                team["players"][_norm(name)] = {"name": name, "dnp": False, "groups": {
                    "goalies" if group == "goalies" else "skaters":
                        {k: str(v) for k, v in cols.items() if v is not None}}}
    return f"nhl:{gid}", {"final": raw.get("gameState") in ("OFF", "FINAL"), "teams": teams, "touchdowns": None}


def _mlb_box(sport_key: str, home: str, away: str, commence: str) -> tuple[str | None, dict | None]:
    """MLB's stats API: the day's schedule, then the game's box score."""
    games, final = [], {}
    for d in _game_days(commence):
        for day in _web(f"{MLB_API}/schedule?sportId=1&date={d:%Y-%m-%d}").get("dates", []):
            for g in day.get("games", []):
                teams = [{"displayName": g["teams"][s]["team"].get("name", ""),
                          "name": g["teams"][s]["team"].get("teamName", "")} for s in ("home", "away")]
                pk = str(g.get("gamePk"))
                games.append((pk, _parse_time(g.get("gameDate", "1970-01-01T00:00:00Z")), teams))
                final[pk] = g.get("status", {}).get("abstractGameState") == "Final"
    pk = _pick_game(games, home, away, commence)
    if not pk:
        return None, None
    raw = _web(f"{MLB_API}/game/{pk}/boxscore", ttl=300)
    teams = {}
    for side in ("home", "away"):
        t = raw.get("teams", {}).get(side, {})
        team = teams.setdefault(side, {"name": t.get("team", {}).get("name", side),
                                       "score": _num(str(t.get("teamStats", {}).get("batting", {}).get("runs", ""))),
                                       "players": {}})
        for p in t.get("players", {}).values():
            name = p.get("person", {}).get("fullName", "")
            bat, pit = p.get("stats", {}).get("batting") or {}, p.get("stats", {}).get("pitching") or {}
            groups = {}
            if bat:
                groups["batting"] = {k: str(bat[f]) for k, f in (("H", "hits"), ("R", "runs"), ("HR", "homeRuns"),
                                     ("RBI", "rbi"), ("BB", "baseOnBalls"), ("K", "strikeOuts"), ("TB", "totalBases"))
                                     if f in bat}
            if pit:
                groups["pitching"] = {k: str(pit[f]) for k, f in (("K", "strikeOuts"), ("H", "hits"), ("BB", "baseOnBalls"),
                                      ("ER", "earnedRuns"), ("IP", "inningsPitched")) if f in pit}
            if name:   # on the roster but no stats: didn't play (books void those bets)
                team["players"][_norm(name)] = {"name": name, "dnp": not groups, "groups": groups}
    return f"mlb:{pk}", {"final": final.get(pk, False), "teams": teams, "touchdowns": None}


BOX_SOURCES = {"icehockey_nhl": [("NHL.com", _nhl_box)], "baseball_mlb": [("MLB.com", _mlb_box)]}

_BOX_CACHE: dict[str, dict] = {}


def _espn_box(sport_key: str, home: str, away: str, commence: str) -> tuple[str | None, dict | None]:
    """(game id, raw box) from ESPN, or (None, None) if the game isn't there."""
    if sport_key not in ESPN_LEAGUES:
        return None, None
    eid = espn_event_id(sport_key, home, away, commence)
    if not eid:
        return None, None
    league, _ = ESPN_LEAGUES[sport_key]
    return f"espn:{eid}", parse_box(_espn(f"{league}/summary?event={eid}", ttl=300))


def game_box(sport_key: str, home: str, away: str, commence: str) -> tuple[dict | None, str]:
    """(box score, "") once the game is final and the box passed its checks, else (None, why).
    Tries the league's own stats site first (NHL, MLB), then ESPN."""
    why = []
    for name, source in BOX_SOURCES.get(sport_key, []) + [("ESPN", _espn_box)]:
        try:
            gid, box = source(sport_key, home, away, commence)
        except Exception as e:  # noqa: BLE001 - a source that's down or refuses: try the next one
            why.append(f"{name}: {e!r:.80}")
            continue
        if gid is None:
            why.append(f"{name}: game not found")
            continue
        if gid in _BOX_CACHE:
            return _BOX_CACHE[gid], ""
        if not box["final"]:
            return None, "not final yet"
        if problems := box_checks(sport_key, box):
            why.append(f"{name}: box score didn't check out: " + "; ".join(problems))
            continue
        _BOX_CACHE[gid] = box
        return box, ""
    return None, " / ".join(why)


NICKNAMES = {   # short form -> full first name (both directions are checked)
    "mike": "michael", "mikey": "michael", "nick": "nicholas", "nic": "nicholas", "gabe": "gabriel",
    "matt": "matthew", "matty": "matthew", "jake": "jacob", "dave": "david", "dan": "daniel", "danny": "daniel",
    "tony": "anthony", "bill": "william", "will": "william", "billy": "william", "bob": "robert", "rob": "robert",
    "bobby": "robert", "jim": "james", "jimmy": "james", "ken": "kenneth", "kenny": "kenneth", "joe": "joseph",
    "joey": "joseph", "chris": "christopher", "steve": "steven", "tom": "thomas", "tommy": "thomas",
    "zach": "zachary", "zack": "zachary", "josh": "joshua", "alex": "alexander", "sasha": "alexander",
    "ben": "benjamin", "sam": "samuel", "ed": "edward", "eddie": "edward", "pat": "patrick", "andy": "andrew",
    "drew": "andrew", "greg": "gregory", "jon": "jonathan", "johnny": "john", "fred": "frederick",
    "freddie": "frederick", "vince": "vincent", "jeff": "jeffrey", "rick": "richard", "ricky": "richard",
    "dick": "richard", "charlie": "charles", "chuck": "charles", "nate": "nathan", "max": "maxwell",
    "cam": "cameron", "mitch": "mitchell", "tim": "timothy", "timmy": "timothy", "theo": "theodore",
    "teddy": "theodore", "ted": "theodore", "liz": "elizabeth", "kate": "katherine",
}


def find_player(box: dict, name: str) -> dict | None:
    """Exact name, else a short form of the same first name with the same rest ("Mitch Marner" =
    "Mitchell Marner"). Never a different player: a scratched player isn't in the box score, and
    grading his bet on someone else's stats would be wrong, so that stays a manual check."""
    every = [(k, p) for t in box["teams"].values() for k, p in t["players"].items()]
    exact = [p for k, p in every if k == _norm(name)]
    if len(exact) == 1:
        return exact[0]
    want = _words(name)
    if len(want) < 2 or exact:
        return None

    def same_person(w: list[str]) -> bool:
        if w[1:] != want[1:]:
            return False
        short, longer = sorted((w[0], want[0]), key=len)
        return ((len(short) >= 3 and longer.startswith(short))
                or NICKNAMES.get(w[0]) == want[0] or NICKNAMES.get(want[0]) == w[0]
                or (NICKNAMES.get(w[0]) is not None and NICKNAMES.get(w[0]) == NICKNAMES.get(want[0])))

    close = [p for _, p in every if len(w := _words(p["name"])) >= 2 and same_person(w)]
    return close[0] if len(close) == 1 else None


def settle_prop(row: dict, value: float) -> tuple[str, float]:
    """Over/Under the line, or Yes/No (anytime TD: at least one)."""
    point = float(row["point"]) if row.get("point") not in ("", None) else None
    side = row["outcome"].lower()
    if point is None:
        res = "win" if (value >= 1) == (side != "no") else "loss"
    else:
        diff = (value - point) if side != "under" else (point - value)
        res = "win" if diff > 0 else ("push" if diff == 0 else "loss")
    stake, price = float(row["stake"]), float(row["price"])
    return res, round(stake * (price - 1) if res == "win" else (-stake if res == "loss" else 0.0), 2)


def grade_prop(row: dict) -> tuple[tuple[str, float], str] | tuple[None, str]:
    """((result, profit), what the box score said) or (None, why not yet)."""
    try:
        box, why = game_box(row["sport_key"], row["home_team"], row["away_team"], row["commence_time"])
    except Exception as e:  # noqa: BLE001 - ESPN down or changed: leave it for later / manual
        return None, f"ESPN error: {e!r:.120}"
    if box is None:
        return None, why
    p = find_player(box, row["player"])
    if p is None:
        return None, f"{row['player']} isn't in the box score"
    if p["dnp"]:
        return ("push", 0.0), "DNP"   # didn't play: books void the bet
    value = _read_stat(p, row["market"], row["sport_key"])
    if value is None:
        return None, f"no {MARKET_NAMES.get(row['market'], row['market'])} for {p['name']} in the box score"
    if row["market"] == "player_anytime_td" and value < 1:
        # "No TD" is only certain if every touchdown in the game is credited to someone in the box
        # (a fumble recovered in the end zone, say, has no column of its own).
        credited = sum(_read_stat(q, row["market"], row["sport_key"]) or 0
                       for t in box["teams"].values() for q in t["players"].values() if not q["dnp"])
        if box.get("touchdowns") is None or credited < box["touchdowns"]:
            return None, "not every touchdown in this game is in the box score"
    return settle_prop(row, value), f"{value:g}"


# --------------------------------------------------------------------------- results by day

RESULT_ICON = {"win": "✅", "loss": "❌", "push": "➖"}
KIND_LABEL = {"ev": "📈 +EV", "outlier": "🚨 Outliers", "parlay": "📦 Parlays", "prop": "🎯 Props"}


def row_pick(r: dict) -> str:
    """The bet as the alert named it ("Bills ML", "Over 47.5", "LeBron James Over 25.5 Points")."""
    if r.get("kind") == "parlay":
        return f"{len(r['_legs'])}-leg parlay"
    point = r.get("point")
    if r.get("player"):
        stat = MARKET_NAMES.get(r["market"], r["market"])
        return f"{r['player']} {r['outcome']}" + (f" {float(point):g}" if point not in ("", None) else "") + f" {stat}"
    if r["market"] == "h2h":
        return r["outcome"] if r["outcome"] == "Draw" else f"{r['outcome']} ML"
    if point in ("", None):
        return r["outcome"]
    return f"{r['outcome']} {float(point):+g}" if r["market"] == "spreads" else f"{r['outcome']} {float(point):g}"


def local_day(cfg: Config, ts: str):
    return _parse_time(ts).astimezone(ZoneInfo(cfg.timezone)).date()


def day_bets(cfg: Config, day) -> list[dict]:
    """Every alert whose game is on `day` (local time; a parlay counts on its last game's day),
    first alert per bet, with its result if it's been graded. Earliest game first."""
    graded = {_bet_id(r): r for r in _read_csv(cfg.ev_results_file)}
    since = datetime.combine(day, dtime.min, ZoneInfo(cfg.timezone)) - timedelta(days=2)
    out = []
    for bid, r in logged_bets(cfg, since=since).items():
        if local_day(cfg, r["commence_time"]) != day:
            continue
        g = graded.get(bid, {})
        out.append({**r, "result": g.get("result", ""), "profit": g.get("profit", ""),
                    "home_score": g.get("home_score", ""), "away_score": g.get("away_score", ""),
                    "actual": g.get("actual", ""), "manual_legs": g.get("manual_legs", ""),
                    "clv_pct": g.get("clv_pct") or r.get("clv_pct", "")})
    return sorted(out, key=lambda r: r["commence_time"])


def _kind(r: dict) -> str:
    return "prop" if r.get("player") else r.get("kind") or "ev"


def result_line(r: dict, cfg: Config, now: datetime | None = None, discord: bool = True) -> str:
    """One bet: what it was, at what book and stake, and how it went (or when it plays)."""
    now = now or datetime.now(timezone.utc)
    b = (lambda x: f"**{x}**") if discord else (lambda x: x)
    pick = f"{row_pick(r)} {odds(float(r['price']))}"
    head = f"{b(pick)} at {r['book']} · {money(float(r['stake']))}"
    start = _parse_time(r["commence_time"])
    if r.get("result") in RESULT_ICON:
        icon, tail = RESULT_ICON[r["result"]], f"→ {b(signed_money(float(r['profit'])))}"
        if r.get("actual") == "DNP":
            tail += " (didn't play: void)"
    elif r.get("actual") == RAIN_HELD:
        icon, tail = "🌧️", ("· has a leg on a game that ended early (rain): books usually void run lines and "
                            "totals, check your book" if r.get("kind") == "parlay" else
                            "· game ended early (rain): books usually void run lines and totals, check your book")
    elif start > now:
        ts = int(start.timestamp())
        icon, tail = "⏰", (f"· starts <t:{ts}:t>" if discord
                           else f"· starts {start.astimezone(ZoneInfo(cfg.timezone)):%-I:%M %p}")
    elif not gradable(r) or stuck(r, cfg, now):
        what = ("has a prop leg: check it yourself" if r.get("kind") == "parlay"
                else "couldn't grade it automatically: check the box score" if r.get("player") and gradable(r)
                else "check the box score yourself")
        icon, tail = "🎯", f"· {what}"
    else:
        icon, tail = "⏳", "· waiting for the box score" if r.get("player") else "· waiting for the final"
    if r.get("kind") == "parlay":
        where, icon2 = r.get("outcome", ""), "📦"
    else:
        where, icon2 = r.get("matchup", ""), sport_icon(r.get("sport_key", ""))
        if r.get("home_score") not in ("", None):
            where = (f"Final: {r['away_team']} {float(r['away_score']):g}, "
                     f"{r['home_team']} {float(r['home_score']):g}")
        elif r.get("actual") not in ("", None, "DNP"):
            where = f"Box score: {r['actual']} · {r.get('matchup', '')}"   # the pick already names the stat
    if r.get("clv_pct") not in ("", None):
        where += f" · CLV {float(r['clv_pct']):+.1f}%"
    return f"{icon} {head} {tail}\n     {icon2} {where}"


def stuck(r: dict, cfg: Config, now: datetime) -> bool:
    """Ungraded 3+ hours after the game should have ended: the bot couldn't (a prop it couldn't
    find in the box score, a postponed game...), so it's on you."""
    return not r.get("result") and max(
        _parse_time(l["commence_time"]) + timedelta(minutes=cfg.minutes_for(l["sport_key"]) + 180)
        for l in _legs(r)) < now


def day_summary(rows: list[dict], cfg: Config | None = None, now: datetime | None = None) -> str:
    """Record for the day, per kind, plus what's still to come."""
    lines = [f"**All:** {record_line(rows)}"]
    for kind, label in KIND_LABEL.items():
        group = [r for r in rows if _kind(r) == kind and r.get("result")]
        if group:
            lines.append(f"{label}: {record_line(group)}")
    now = now or datetime.now(timezone.utc)
    ungraded = [r for r in rows if not r.get("result")]
    rain = sum(1 for r in ungraded if r.get("actual") == RAIN_HELD)   # (singles and parlays)
    ungraded = [r for r in ungraded if r.get("actual") != RAIN_HELD]
    manual = [r for r in ungraded if not gradable(r) or (cfg and stuck(r, cfg, now))]
    waiting = len(ungraded) - len(manual)
    props = sum(1 for r in manual if r.get("kind") != "parlay")
    mixed = sum(1 for r in manual if r.get("kind") == "parlay")
    notes = [f"⏳ {waiting} still to finish" if waiting else "",
             f"🎯 {props} prop{'s' if props != 1 else ''} to check yourself" if props else "",
             f"📦 {mixed} parlay{'s' if mixed != 1 else ''} with a prop leg to check yourself" if mixed else "",
             f"🌧️ {rain} on {'a game' if rain == 1 else 'games'} that ended early (rain): check your book"
             if rain else ""]
    if any(notes):
        lines.append(" · ".join(x for x in notes if x))
    return "\n".join(lines)


def arbs_on(cfg: Config, day) -> str:
    """Arbs alerted that day: how many different ones (an arb that closes and reopens is logged
    each time), what they'd have locked in placed once each at BANKROLL, and how long they lasted."""
    rows = [r for r in _read_csv(cfg.log_file) if r.get("first_seen") and not r.get("reason")
            and local_day(cfg, r["first_seen"]) == day]   # (a reason: held back, never alerted)
    if not rows:
        return ""
    first: dict[tuple, dict] = {}
    for r in rows:
        first.setdefault((r.get("matchup"), r.get("market"), r.get("line")), r)
    locked = sum(float(r.get("best_profit_pct") or 0) for r in first.values()) * cfg.bankroll / 100
    lasted = sorted(float(r["seconds_open"]) for r in rows if r.get("seconds_open") not in ("", None))
    gone = f" · half were gone within {_fmt_secs(lasted[len(lasted) // 2])}" if lasted else ""
    alerts = f" ({len(rows)} alerts)" if len(rows) != len(first) else ""
    return (f"💰 **Arbs:** {len(first)} different{alerts} · about {signed_money(locked)} if you'd placed each "
            f"once at {money(cfg.bankroll)}{gone}")


def day_clv_line(rows: list[dict]) -> str:
    """The day's bet quality: "📐 CLV: avg +2.1% · beat the close on 64% (11 bets)" ("" before any closing line)."""
    clvs = [float(r["clv_pct"]) for r in rows if r.get("clv_pct") not in ("", None)]
    if not clvs:
        return ""
    beat = sum(c > 0 for c in clvs) / len(clvs) * 100
    return (f"📐 **CLV:** avg {sum(clvs) / len(clvs):+.1f}% · beat the close on {beat:.0f}% "
            f"({len(clvs)} bet{'s' if len(clvs) != 1 else ''})")


def results_payload(cfg: Config, title_prefix: str, rows: list[dict], day_rows: list[dict],
                    day, now: datetime | None = None) -> dict:
    """A results card: the given bets one by one, then the day's record."""
    now = now or datetime.now(timezone.utc)
    shown = rows[:20]
    body = "\n".join(result_line(r, cfg, now) for r in shown)
    if len(rows) > len(shown):
        body += f"\n…and {len(rows) - len(shown)} more (all of them: arbbot.py --results {day})"
    w, l, pu, profit, _ = _record(rows)
    graded = w + l + pu
    title = f"{title_prefix}" + (f" · {w}-{l}" + (f"-{pu}" if pu else "") + f" · {signed_money(profit)}"
                                 if graded else "")
    arbs = arbs_on(cfg, day)
    clv = day_clv_line(day_rows)
    members = members_lines(cfg, day_rows)
    desc = (body + DIVIDER + f"**{day:%a %b %-d}** (every alert at the stake it showed)\n"
            + day_summary(day_rows, cfg, now) + (f"\n{clv}" if clv else "") + (f"\n{arbs}" if arbs else "")
            + (f"\n\n{members}" if members else ""))
    color = 0x2ECC71 if profit > 0 else (0xE74C3C if profit < 0 else GREY)
    return _card(title, desc, color,
                 footer="Results assume every alert was bet at the stake shown. Props are graded from the box "
                        "score; 🎯 means check that one yourself.")


def _graded(cfg: Config, since: datetime | None = None) -> list[dict]:
    rows = [r for r in _read_csv(cfg.ev_results_file)
            if r.get("result") in ("win", "loss", "push") and not r.get("manual_legs") and cfg.counts(r.get("book", ""))]
    return [r for r in rows if not since or _parse_time(r["commence_time"]) >= since]


def record_block(rows: list[dict]) -> list[str]:
    """All, each kind, and live vs pre-game."""
    if not rows:
        return ["no graded bets yet"]
    lines = [f"**All:** {record_line(rows)}"]
    for kind, label in KIND_LABEL.items():
        group = [r for r in rows if _kind(r) == kind]
        if group:
            lines.append(f"{label}: {record_line(group)}")
    live = [r for r in rows if str(r.get("live")).lower() == "true"]
    if live and len(live) < len(rows):
        pre = [r for r in rows if str(r.get("live")).lower() != "true"]
        lines.append(f"🔴 Live: {record_line(live)}")
        lines.append(f"⏰ Pre-game: {record_line(pre)}")
    return lines


def scoreboard_text(cfg: Config, now: datetime | None = None) -> str:
    """Today so far, the last 7 days, all time, and bet quality (CLV): the same numbers as
    arbbot.py --results, kept up to date in one Discord message."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(cfg.timezone)).date()
    rows = day_bets(cfg, today)
    arbs = arbs_on(cfg, today)
    parts = [f"__**Today · {today:%a %b %-d}**__\n" + day_summary(rows, cfg, now) + (f"\n{arbs}" if arbs else ""),
             "__**Last 7 days**__\n" + "\n".join(record_block(_graded(cfg, now - timedelta(days=7)))),
             "__**All time**__\n" + "\n".join(record_block(_graded(cfg)))]
    ever = clv_rows(cfg)
    if ever:
        good = sum(r["clv_pct"] for r in ever) > 0 and sum(r["beat_close"] for r in ever) * 2 > len(ever)
        rows_per_group = 4 if len("\n\n".join(parts)) < 2500 else 2   # Discord cards hold 4,096 characters
        shown = [(group, items[:rows_per_group]) for group, items in clv_breakdown(cfg).items() if items]
        w = min(26, max([16] + [len(t[0]) for _, items in shown for t in items]))   # "Confirmed pre-game (3.5%+)"
        table = []
        for group, items in shown:
            table.append(group)
            table += [f"  {name[:w]:<{w}}{n:>4}  {avg:+5.1f}%  {beat:3.0f}%" for name, n, avg, beat in items]
        parts.append("__**📐 Bet quality (CLV)**__\n"
                     f"All time: {clv_record(cfg)} · last 7 days: {clv_record(cfg, 7)}\n"
                     + ("✅ Beating the closing line: the edge looks real." if good
                        else "⚠️ Not beating the closing line yet. Give it more bets.")
                     + f"\n```\n{'':<{w + 4}}bets   CLV  beat\n" + "\n".join(table) + "\n```")
    marks = _safely("scoreboard", markout_table, cfg)
    if marks and len("\n\n".join(parts + [marks])) <= 4000:   # only if the card still fits: CLV comes first
        parts.append(marks)
    return "\n\n".join(parts)


def scoreboard_payload(cfg: Config, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(cfg.timezone)).date()
    profit = _record([r for r in day_bets(cfg, today) if r.get("result")])[3]
    text = scoreboard_text(cfg, now)
    if len(text) > 4000:   # very long: drop the CLV table rather than cut a section in half
        text = text.split("\n```")[0][:4000]
    return _card("📊 Scoreboard", text,
                 0x2ECC71 if profit > 0 else (0xE74C3C if profit < 0 else 0x5865F2),
                 footer="Every alert at the stake it showed. Updates by itself as games finish. Pin this message.")


# RESULTS_DAILY_ONLY: a day's results card (12:00am-11:59pm New York, by game start) goes out at the first check
# from RECAP_AT the next night, once every bet that day the bot can grade is graded, and by RECAP_LATEST anyway.
RECAP_AT = dtime(0, 30)
RECAP_LATEST = dtime(1, 0)


class Results:
    """Grades alerted bets as games finish and posts what hit to the results channel.

    What's been posted lives in the state dir, shared with the command line (--post-results),
    so a result goes out once no matter which of them sends it."""

    def __init__(self, cfg: Config, dry_run: bool):
        self.cfg = cfg
        self.url = cfg.results_webhook_url or cfg.status_webhook_url or cfg.webhook_url
        self.dry_run = dry_run or not self.url
        self.last = 0.0
        self.path = data_path(cfg.state_dir) / "results_posted.json" if cfg.state_dir and not self.dry_run else None
        self.posted: dict[str, str] = {}   # bet id -> game start (to prune)
        self.recap_days: list[str] = []    # days whose full recap went out (YYYY-MM-DD)
        self.graded_day = ""               # last day the full (not just recent) grading pass ran
        self.recap_tried = 0.0             # last recap attempt, so a failing one isn't retried nonstop
        self.weekly_days: list[str] = []   # weeks whose report card went out (the day it covers up to, YYYY-MM-DD)
        self.weekly_tried = 0.0            # last report card attempt (as recap_tried)
        self.board: dict[str, str] = {}    # the scoreboard message: {"id", "hash"}
        if self.path and not self.path.exists():
            # First run: everything already graded counts as posted, so there's no flood.
            self.posted = {_bet_id(r): r["commence_time"] for r in _read_csv(cfg.ev_results_file)}
            self._save()
        self._load()

    def _load(self) -> None:
        """Pick up what another process (the command line) posted meanwhile."""
        if not self.path:
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and not ({"posted", "recap_day", "recap_days", "board"} & data.keys()):
            data = {"posted": data}   # the very first format: just the posted bets
        if data.get("recap_day"):     # the previous format kept only the latest recapped day
            data["recap_days"] = [*data.get("recap_days", []), data["recap_day"]]
        self.posted.update(data.get("posted", {}))
        self.recap_days = sorted(set(self.recap_days) | set(data.get("recap_days", [])))[-14:]
        self.weekly_days = sorted(set(self.weekly_days) | set(data.get("weekly_days", [])))[-8:]
        if data.get("board", {}).get("id") and not self.board.get("id"):
            self.board = data["board"]

    def _save(self, now: datetime | None = None) -> None:
        if not self.path:
            return
        self._load()
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=5)
        self.posted = {k: v for k, v in self.posted.items() if _parse_time(v) > cutoff}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"posted": self.posted, "recap_days": self.recap_days, "board": self.board,
                                       "weekly_days": self.weekly_days}))
            tmp.replace(self.path)
        except OSError as e:
            print(f"  ! Couldn't save results state: {e}", file=sys.stderr)

    def mark(self, rows: list[dict]) -> None:
        for r in rows:
            self.posted[_bet_id(r)] = r["commence_time"]

    def due(self) -> bool:
        return bool(self.cfg.results_minutes) and time.time() - self.last >= self.cfg.results_minutes * 60

    def run(self, api: "OddsAPI", now: datetime | None = None, recent_hours: float | None = 12) -> int:
        """Grade what's finished, then post every graded bet not posted yet (including ones
        graded from the command line). Returns how many were posted."""
        now = now or datetime.now(timezone.utc)
        self.last = time.time()
        try:
            settle_pending(self.cfg, api, now, recent_hours)
        except Exception as e:  # noqa: BLE001 - grading must never stop the bot
            print(f"! Grading bets failed: {e}", file=sys.stderr)
        if self.cfg.results_daily_only:
            return 0   # (RESULTS_DAILY_ONLY: graded now, posted in the day's one card after midnight)
        self._load()
        cutoff = now - timedelta(days=3)
        new = [r for r in _read_csv(self.cfg.ev_results_file)
               if _bet_id(r) not in self.posted and _parse_time(r["commence_time"]) > cutoff
               and self.cfg.counts(r.get("book", ""))]
        if not new:
            return 0
        bets = logged_bets(self.cfg, since=cutoff)
        new = [{**bets.get(_bet_id(r), {}), **r} for r in new]   # parlays need their legs back
        new = [r for r in new if r.get("kind") != "parlay" or r.get("_legs")]
        by_day: dict = {}
        for r in new:
            by_day.setdefault(local_day(self.cfg, r["commence_time"]), []).append(r)
        sent = 0
        for day, rows in sorted(by_day.items()):
            n = len(rows)
            if self.send(results_payload(self.cfg, f"📋 {n} result{'s' if n != 1 else ''} in", rows,
                                         day_bets(self.cfg, day), day, now)):
                self.mark(rows)   # a failed send is tried again next time
                sent += n
        self._save(now)
        return sent

    def tick(self, api: "OddsAPI", now: datetime | None = None) -> int:
        """The regular pass from the main loop: grade and post results, then the scoreboard."""
        n = self.run(api, now)
        self.update_board(now)
        return n

    def update_board(self, now: datetime | None = None) -> None:
        """Keep the scoreboard message current: edit it when the numbers change, post a new one
        if it was deleted (or the results channel changed)."""
        payload = scoreboard_payload(self.cfg, now)
        emb = {k: v for k, v in payload["embeds"][0].items() if k != "timestamp"}
        digest = hashlib.sha1(json.dumps(emb, sort_keys=True).encode()).hexdigest()[:16]
        if digest == self.board.get("hash"):
            return
        if self.dry_run:
            print(f"[scoreboard]\n{emb['description']}", flush=True)
            self.board["hash"] = digest
            return
        try:
            if self.board.get("id"):
                try:
                    if not _webhook(self.url, payload, "PATCH", self.board["id"]):
                        return   # rate-limited every try: keep the old hash so the next pass retries
                except urllib.error.HTTPError as e:
                    if e.code != 404:
                        raise
                    self.board.pop("id")   # deleted: post a fresh one
            if not self.board.get("id"):
                msg = _webhook(self.url, payload)
                if not msg:
                    return
                self.board["id"] = msg["id"]
            self.board["hash"] = digest
            self._save(now)
        except Exception as e:  # noqa: BLE001
            print(f"  ! Scoreboard update failed: {e}", file=sys.stderr)

    def send(self, payload: dict) -> bool:
        emb = payload["embeds"][0]
        print(f"[results] {emb['title']}\n{emb['description']}", flush=True)
        if self.dry_run:
            return True
        try:
            return _webhook(self.url, payload) is not None   # None: rate-limited every try
        except Exception as e:  # noqa: BLE001
            print(f"  ! Results message failed: {e}", file=sys.stderr)
            return False

    def daily(self, api: "OddsAPI", day, now: datetime | None = None) -> None:
        """Daily pass: grade everything still open (not just recent games) once, post the full
        card for `day`, and count that day's bets as posted so they don't also come one by one.
        A failed post is tried again at most every RESULTS_MINUTES (10 at the least)."""
        self.recap_tried = time.time()
        if self.graded_day != day.isoformat() or self.cfg.results_daily_only:
            self.graded_day = day.isoformat()
            try:
                settle_pending(self.cfg, api, now)
            except Exception as e:  # noqa: BLE001
                print(f"! Grading bets failed: {e}", file=sys.stderr)
        if self.cfg.results_daily_only and self.still_playing(day, now):
            return   # (a late game still going: the card waits for it, until RECAP_LATEST)
        self.recap(day, now)
        self.update_board(now)

    def still_playing(self, day, now: datetime | None = None) -> bool:
        """RESULTS_DAILY_ONLY: should `day`'s card wait? Yes while one of its bets the bot can grade isn't
        graded yet and it's not RECAP_LATEST yet (then it goes out anyway, showing what's left)."""
        now = now or datetime.now(timezone.utc)
        local = now.astimezone(ZoneInfo(self.cfg.timezone))
        if local.date() > day + timedelta(days=1) or local.time() >= RECAP_LATEST:
            return False
        return any(not r.get("result") and r.get("actual") != RAIN_HELD and gradable(r)
                   and not stuck(r, self.cfg, now) for r in day_bets(self.cfg, day))

    def recap_due(self, day) -> bool:
        if time.time() - self.recap_tried < max(600, self.cfg.results_minutes * 60):
            return False
        self._load()
        return day.isoformat() not in self.recap_days

    def recap(self, day, now: datetime | None = None, finished: bool = True) -> str:
        """The whole day's bets and record: "sent", "nothing" (no bets that day) or "failed".
        Once sent, that day's bets count as posted; a finished day also counts as recapped (a
        card posted by hand mid-day doesn't stop the bot's full one the next morning)."""
        rows = day_bets(self.cfg, day)
        outcome = "nothing"
        if rows or arbs_on(self.cfg, day):
            if not self.send(results_payload(self.cfg, f"📅 Results for {day:%a %b %-d}", rows, rows, day, now)):
                return "failed"
            outcome = "sent"
        self.mark([r for r in _read_csv(self.cfg.ev_results_file) if local_day(self.cfg, r["commence_time"]) == day])
        if finished:
            self.recap_days = sorted(set(self.recap_days) | {day.isoformat()})[-14:]
        self._save(now)
        return outcome

    def weekly_due(self, day) -> bool:
        """Is the report card for the week up to `day` still owed? (Retried as recap_due is.)"""
        if time.time() - self.weekly_tried < max(600, self.cfg.results_minutes * 60):
            return False
        self._load()
        return day.isoformat() not in self.weekly_days

    def weekly(self, day, now: datetime | None = None, held: dict | None = None) -> str:
        """Post the report card for the 7 days before `day`: "sent" (then that week counts as posted,
        for every process) or "failed"."""
        self.weekly_tried = time.time()
        if not self.send(weekly_payload(self.cfg, day, held)):
            return "failed"
        self.weekly_days = sorted(set(self.weekly_days) | {day.isoformat()})[-8:]
        self._save(now)
        return "sent"


# --------------------------------------------------------------------------- ✅ bet tracking (DISCORD_BOT_TOKEN)

DISCORD_API = "https://discord.com/api/v10"
TAKEN = "✅"
TAKEN_GRACE = 1800   # seconds after kickoff a ✅ still counts (a bet placed right at the start), then it's final


def _bot_api(token: str, method: str, path: str) -> dict | list | None:
    """One Discord API call as the bot. A "slow down" (429) is waited out, twice at most, up to 10s each."""
    req = urllib.request.Request(DISCORD_API + path, method=method, data=b"" if method == "PUT" else None,
                                 headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                                          "User-Agent": "DiscordBot (https://github.com/joeybuffo10-wq/arb-bot, 3.0)"})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            try:
                wait = float(e.headers.get("Retry-After", "1"))
            except (TypeError, ValueError):
                wait = 1.0
            time.sleep(min(wait, 10.0))
    return None


def row_bet_id(row: dict) -> str:
    """A logged row's bet id, as results use it (a parlay's from its legs)."""
    if "legs_json" in row:
        row = _parlay_row(row) or row
    return _bet_id(row)


def taken_path(cfg: Config) -> Path | None:
    return data_path(cfg.state_dir) / "taken_bets.json" if cfg.state_dir else None


def read_taken(cfg: Config) -> dict[str, dict]:
    """{bet id: {"start": game start, "users": {user id: name}}}: who ✅'d each bet card ({} before any)."""
    path = taken_path(cfg)
    try:
        return json.loads(path.read_text()) if path and path.exists() else {}
    except (OSError, ValueError):
        return {}


class BetReactions:
    """✅ bet tracking. Each new bet card (+EV, props, outliers, parlays) gets a ✅ from the bot; members tap it
    when they place the bet. Every REACTION_MINUTES the bot reads who tapped it, until TAKEN_GRACE after the
    game starts (taking it back before then counts), and keeps that in state/taken_bets.json for the results
    card. Off without DISCORD_BOT_TOKEN, and for --once, --demo and --dry-run. Needs the bot in the server with
    View Channel, Read Message History and Add Reactions in the bet channels."""

    def __init__(self, cfg: Config, off: bool = False):
        self.cfg = cfg
        self.token = cfg.discord_bot_token
        self.on = bool(self.token and cfg.state_dir and not off)
        self.msg_path = data_path(cfg.state_dir) / "bet_messages.json" if self.on else None
        self.messages: dict[str, dict] = {}   # message id -> {"bet", "channel", "start"}: cards still being read
        self.channels: dict[str, str] = {}    # webhook URL -> its channel id
        self.me = ""                          # the bot's own user id (its ✅ isn't a member's)
        self.last = 0.0
        self.warned: set[str] = set()         # problems said once (console)
        if self.msg_path and self.msg_path.exists():
            try:
                self.messages = json.loads(self.msg_path.read_text())
            except (OSError, ValueError):
                self.messages = {}

    def _say(self, key: str, text: str) -> None:
        if key not in self.warned:
            self.warned.add(key)
            print(f"  ! ✅ tracking: {text}", file=sys.stderr, flush=True)

    def _save(self) -> None:
        if not self.msg_path:
            return
        try:
            self.msg_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.msg_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.messages))
            tmp.replace(self.msg_path)
        except OSError as e:
            self._say("save", f"couldn't save the bet cards ({e})")

    def channel(self, webhook: str) -> str:
        """The channel a webhook posts in (asked once: a webhook's own URL says, no token needed)."""
        if webhook not in self.channels:
            req = urllib.request.Request(webhook, headers={"User-Agent": "arbbot/3.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.channels[webhook] = json.loads(resp.read())["channel_id"]
        return self.channels[webhook]

    def add(self, row: dict, message_id: str, webhook: str) -> None:
        """A bet card went up: put the ✅ under it and start reading it."""
        if not self.on:
            return
        channel = self.channel(webhook)
        self.messages[message_id] = {"bet": row_bet_id(row), "channel": channel,
                                     "start": row.get("commence_time") or _parlay_row(row)["commence_time"]}
        self._save()
        try:
            _bot_api(self.token, "PUT", f"/channels/{channel}/messages/{message_id}/reactions/"
                                        f"{urllib.parse.quote(TAKEN)}/@me")
        except urllib.error.HTTPError as e:
            self._say(f"add{e.code}", f"couldn't add the ✅ (Discord said {e.code}): give the bot Add Reactions "
                                      "and Read Message History in the bet channels")

    def users(self, channel: str, message_id: str) -> dict[str, str]:
        """{user id: name} of the members who ✅'d a card (not the bot itself, nor other bots)."""
        out, after = {}, ""
        while True:
            page = _bot_api(self.token, "GET", f"/channels/{channel}/messages/{message_id}/reactions/"
                                               f"{urllib.parse.quote(TAKEN)}?limit=100" + (f"&after={after}" if after else ""))
            if not page:
                return out
            for u in page:
                if u.get("id") != self.me and not u.get("bot"):
                    out[u["id"]] = u.get("global_name") or u.get("username") or u["id"]
            if len(page) < 100:
                return out
            after = page[-1]["id"]

    def due(self) -> bool:
        return self.on and bool(self.messages) and time.time() - self.last >= self.cfg.reaction_minutes * 60

    def tick(self, now: datetime | None = None) -> int:
        """Read every card still open to ✅s; returns how many bets have at least one taker now."""
        now = now or datetime.now(timezone.utc)
        self.last = time.time()
        if not self.me:
            try:
                self.me = (_bot_api(self.token, "GET", "/users/@me") or {}).get("id", "")
            except urllib.error.HTTPError as e:
                self._say(f"me{e.code}", f"Discord turned the bot token down ({e.code}): check DISCORD_BOT_TOKEN")
                return 0
        taken = read_taken(self.cfg)
        for mid, m in list(self.messages.items()):
            start = _parse_time(m["start"])
            try:
                users = self.users(m["channel"], mid)
            except urllib.error.HTTPError as e:
                if e.code == 404:   # the card was deleted: nothing more to read
                    self.messages.pop(mid)
                    continue
                self._say(f"read{e.code}", f"couldn't read the ✅s (Discord said {e.code}): give the bot View "
                                           "Channel and Read Message History in the bet channels")
                continue
            except OSError as e:
                self._say("net", f"couldn't reach Discord ({e!r:.100}); trying again later")
                break
            entry = taken.setdefault(m["bet"], {"start": m["start"], "users": {}})
            # Several cards of one bet (a hand-over): anyone who took it on any of them took it.
            entry.setdefault("cards", {})[mid] = users
            entry["users"] = {u: n for card in entry["cards"].values() for u, n in card.items()}
            if now.timestamp() > start.timestamp() + TAKEN_GRACE:
                self.messages.pop(mid)   # the game's on: what it says now is final
        keep = now - timedelta(days=self.cfg.log_keep_days or 3650)
        taken = {b: e for b, e in taken.items() if e["users"] and _parse_time(e["start"]) > keep}
        path = taken_path(self.cfg)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(taken))
            tmp.replace(path)
        except OSError as e:
            self._say("taken", f"couldn't save who took what ({e})")
        self._save()
        return sum(1 for e in taken.values() if e["users"])


def members_lines(cfg: Config, rows: list[dict]) -> str:
    """The results card's ✅ part for these bets: how many members took, their record at the card's stake in
    units, and the top three ("" when nobody ✅'d any of them)."""
    taken = read_taken(cfg)
    took = [(r, taken[_bet_id(r)]["users"]) for r in rows if taken.get(_bet_id(r), {}).get("users")]
    if not took:
        return ""
    unit = cfg.unit() or 1.0
    people: dict[str, list] = {}
    graded = []
    for r, users in took:
        for uid, name in users.items():
            people.setdefault(uid, [name, 0.0, 0])
            if r.get("result") in ("win", "loss", "push") and not r.get("manual_legs"):
                people[uid][1] += float(r["profit"]) / unit
                people[uid][2] += 1
                graded.append(r)
    w = sum(r["result"] == "win" for r in graded)
    l = sum(r["result"] == "loss" for r in graded)
    total = sum(p[1] for p in people.values())
    line = (f"👥 **Members took {len(took)} of {len(rows)} alerts** ({sum(len(u) for _, u in took)} bets by "
            f"{len(people)} {'person' if len(people) == 1 else 'people'})"
            + (f" · {w}-{l} · {total:+.1f}u" if graded else ""))
    top = sorted((p for p in people.values() if p[2]), key=lambda p: -p[1])[:3]
    if top:
        line += "\n" + " · ".join(f"{name} {u:+.1f}u ({n})" for name, u, n in top)
    return line


def _list_games(source: str, sport: str, days: list) -> list[tuple[str, str, str, bool]]:
    """(home, away, start, finished) for each game those days, from one source."""
    out, errors = [], []
    for d in days:
        try:
            out += _list_day(source, sport, d)
        except Exception as e:  # noqa: BLE001 - one bad day shouldn't hide the other
            errors.append(e)
    if errors and len(errors) == len(days):
        raise errors[0]
    return out


def _list_day(source: str, sport: str, d) -> list[tuple[str, str, str, bool]]:
    out = []
    if source == "NHL.com":
        for g in _web(f"{NHL_API}/score/{d:%Y-%m-%d}").get("games", []):
            name = lambda t: " ".join(x for x in (_txt(t.get("placeName")), _txt(t.get("commonName") or t.get("name"))) if x)
            out.append((name(g.get("homeTeam", {})), name(g.get("awayTeam", {})), g.get("startTimeUTC", ""),
                        g.get("gameState") in ("OFF", "FINAL")))
    elif source == "MLB.com":
        for day in _web(f"{MLB_API}/schedule?sportId=1&date={d:%Y-%m-%d}").get("dates", []):
            for g in day.get("games", []):
                out.append((g["teams"]["home"]["team"].get("name", ""), g["teams"]["away"]["team"].get("name", ""),
                            g.get("gameDate", ""), g.get("status", {}).get("abstractGameState") == "Final"))
    else:
        league, extra = ESPN_LEAGUES[sport]
        for ev in _espn(f"{league}/scoreboard?dates={d:%Y%m%d}" + (f"&{extra}" if extra else "")).get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            sides = {c.get("homeAway"): c.get("team", {}).get("displayName", "") for c in comp.get("competitors", [])}
            status = (ev.get("status") or comp.get("status") or {}).get("type", {})
            out.append((sides.get("home", ""), sides.get("away", ""), ev.get("date", ""), bool(status.get("completed"))))
    return out


def _recent_final(sport: str, days: list) -> tuple[str | None, dict | None, list[str]]:
    """(source, box) for the latest finished game those days, trying the sources in grading order."""
    tried = []
    sources = BOX_SOURCES.get(sport, []) + ([("ESPN", _espn_box)] if sport in ESPN_LEAGUES else [])
    for name, fetch in sources:
        try:
            done = [g for g in _list_games(name, sport, days) if g[3] and g[2]]
            if not done:
                tried.append(f"{name}: no finished games yesterday or today")
                continue
            home, away, start, _ = done[-1]
            gid, box = fetch(sport, home, away, start)
            if box is not None:
                return name, box, tried
            tried.append(f"{name}: couldn't open {away} @ {home}")
        except Exception as e:  # noqa: BLE001
            tried.append(f"{name}: couldn't reach it ({e!r:.100})")
    return None, None, tried


def check_props(cfg: Config, now: datetime | None = None) -> None:
    """Show what the bot reads from the latest finished game's box score in each prop sport, and
    what it would do with your ungraded prop bets. Uses no Odds API credits."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo("America/New_York")).date()
    print("Checking player-prop grading against real box scores...\n")
    for sport in cfg.prop_sports:
        if sport not in ESPN_LEAGUES and sport not in BOX_SOURCES:
            print(f"{short(sport)}: no box scores for this sport, so its props are checked by hand.\n")
            continue
        source, box, tried = _recent_final(sport, [today - timedelta(days=1), today])
        if box is None:
            hint = ("\n  ESPN refuses requests from this server, so these props stay a manual check for now."
                    if any("ESPN" in t and "403" in t for t in tried) and sport not in BOX_SOURCES else "")
            print(f"{short(sport)}: " + " / ".join(tried) + hint + "\n")
            continue
        problems = box_checks(sport, box)
        names = " vs ".join(f"{t['name']} {t['score']:g}" if t["score"] is not None else t["name"]
                            for t in box["teams"].values())
        print(f"{short(sport)} (from {source}): {names}")
        for note in tried:
            print(f"  ({note})")
        print("  ✅ box score checks out" if not problems else "  ⚠️ " + "; ".join(problems)
              + " (props in games like this are left for you to check)")
        for market in cfg.prop_types(sport):
            terms = BOX_STATS.get((_family(sport), market))
            label = MARKET_NAMES.get(market, market)
            if not terms:
                print(f"  {label}: not in the box score, so you check these yourself")
                continue
            top = sorted(((v, p["name"]) for t in box["teams"].values() for p in t["players"].values()
                          if not p["dnp"] and (v := _read(p, terms)) is not None), reverse=True)[:3]
            print(f"  {label}: " + (", ".join(f"{n} {v:g}" for v, n in top) or "nothing found"))
        print()
    graded = {_bet_id(r) for r in _read_csv(cfg.ev_results_file)}
    mine = [r for bid, r in logged_bets(cfg, since=now - timedelta(days=3)).items()
            if r.get("player") and bid not in graded]
    if mine:
        print("Your ungraded prop bets (nothing is saved by this check):")
        for r in mine[:20]:
            over = _parse_time(r["commence_time"]) + timedelta(minutes=cfg.minutes_for(r["sport_key"])) <= now
            if not box_supported(r):
                verdict = "checked by hand (this stat isn't in the box score)"
            elif not over:
                verdict = "game not over yet"
            else:
                res, said = grade_prop(r)
                verdict = (f"{RESULT_ICON[res[0]]} {res[0]} (box score: {said})" if res else f"⏳ {said}")
            print(f"  {row_pick(r)} {odds(float(r['price']))}: {verdict}")


def print_day(cfg: Config, day, now: datetime | None = None) -> None:
    rows = day_bets(cfg, day)
    print(f"\n{day:%A %b %-d}: every alert, at the stake it showed")
    if not rows:
        print("  No +EV, outlier, prop or parlay alerts for games that day.")
    for r in rows:
        print("  " + result_line(r, cfg, now, discord=False).replace("\n", "\n  "))
    print("\n" + day_summary(rows, cfg, now).replace("**", ""))
    if arbs := arbs_on(cfg, day):
        print(arbs.replace("**", ""))


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


def _book_key(ev: dict, title: str) -> str:
    """The Odds API key of a book named by its title (logs and cards name books by title)."""
    for bm in ev.get("bookmakers", []):
        if bm.get("title", bm["key"]) == title:
            return bm["key"]
    return next((k for k, t in BOOK_TITLES.items() if t == title), title.lower())


class ClosingTracker:
    """Keeps the latest sharp fair price for every logged pre-game bet until kickoff, then
    saves it as the closing line. Restarts are fine: pending bets reload from the logs."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.latest: dict[str, tuple[float, datetime]] = {}
        self.written = {r["bet_id"] for r in _read_csv(cfg.closing_file)}
        self.tracked: dict[str, dict] = {}
        now = datetime.now(timezone.utc)
        for kind, name in (("ev", cfg.ev_log_file), ("outlier", cfg.outlier_log_file)):
            for r in _read_csv(name) if name else []:
                if _parse_time(r["commence_time"]) > now:
                    self.add(r, kind)

    def add(self, row: dict, kind: str = "ev") -> None:
        """Follow a logged bet to kickoff. kind: "ev" or "outlier" (its alert's yardstick)."""
        bid = _bet_id(row)
        if bid in self.written or bid in self.tracked:
            return
        if _parse_time(row["first_seen"]) >= _parse_time(row["commence_time"]):
            return  # live bet: there's no closing line to beat
        self.tracked[bid] = {**row, "kind": kind}

    def needs_close(self) -> set[str]:
        """Event ids with a main-line bet that still needs a last look before kickoff."""
        return {r["event_id"] for r in self.tracked.values() if not r.get("player")}

    def needs_close_props(self) -> set[str]:
        """Event ids with a player-prop bet that still needs a last look (props come from a
        separate per-game request, so a main-line check can't close them)."""
        return {r["event_id"] for r in self.tracked.values() if r.get("player")}

    def observe(self, events: list[dict], now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        wanted = {r["event_id"] for r in self.tracked.values()}
        for ev in events:
            if ev["id"] not in wanted or _parse_time(ev["commence_time"]) <= now:
                continue
            fair = sharp_fair(ev, self.cfg, now, False)[0]
            market: dict[tuple, dict[tuple, Consensus]] = {}   # (outlier?, alerted book) -> the other books
            for bid, r in self.tracked.items():
                if r["event_id"] != ev["id"]:
                    continue
                k = (r["market"], _row_line(r))
                p = fair.get(k, {}).get(r["outcome"])
                if p is None and r.get("player"):
                    # No sharp price on this prop: the median of the other books (the alerted one left
                    # out) by the rules its alert used: a +EV prop's consensus (PROP_MIN_BOOKS votes,
                    # sister books as one), or an outlier's median (OUTLIER_MIN_BOOKS books). Main lines
                    # only ever use the sharp price, so for those the last sharp reading stands.
                    outlier, bk = r.get("kind") == "outlier", _book_key(ev, r.get("book", ""))
                    if (outlier, bk) not in market:
                        cfg = self.cfg.for_props()
                        if outlier:
                            cfg = replace(cfg, consensus_min_books=self.cfg.outlier_min_books)
                        market[(outlier, bk)] = consensus_fair(ev, cfg, now, False, set(_csv(self.cfg.sharp_books)),
                                                               leave_out=bk, sisters=not outlier)
                    c = market[(outlier, bk)].get(k)
                    p = c.probs.get(r["outcome"]) if c else None
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
            if bid in seen or bid not in closing or not cfg.counts(r.get("book", "")):
                continue
            if cutoff and _parse_time(r["commence_time"]) < cutoff:
                continue
            seen.add(bid)
            p = closing[bid]
            out.append({**r, "kind": kind, "closing_fair_odds": 1 / p,
                        "clv_pct": clv_pct(float(r["price"]), p),
                        "beat_close": float(r["price"]) > 1 / p})
    return out


def _market_group(row: dict) -> str:
    if row.get("player"):
        return "Player props"
    return {"h2h": "Moneylines", "spreads": "Spreads", "totals": "Totals"}.get(row["market"], row["market"])


def _fair_group(row: dict) -> str:
    """Where a logged bet's fair price came from (fair_from): the sharp book, the other books' median
    (+EV props it doesn't price), or the outlier median. Bets logged before fair_from: "Unknown"."""
    src = row.get("fair_from") or ""
    if row.get("kind") == "outlier" or src.startswith("median"):
        return "Outlier median"
    if src.startswith("consensus"):
        return "Other books"
    return src or "Unknown"


def confirmed_group(cfg: Config) -> str:
    """What confirmed pre-game bets are called in the CLV tables and the weekly report card."""
    return f"Confirmed pre-game ({cfg.pregame_confirmed_ev_pct or LOCKS['pregame_confirmed_ev_pct']:g}%+)"


def _bet_type(row: dict, confirmed: str = "") -> str:
    """+EV, Outliers, or (with its name given) confirmed pre-game bets as a type of their own."""
    if confirmed and row.get("tier") == CONFIRMED:
        return confirmed
    return {"ev": "+EV", "outlier": "Outliers"}.get(row["kind"], row["kind"])


CLV_GROUPS = {
    "Bet type": _bet_type,
    "Market": _market_group,
    "Book": lambda r: r.get("book") or "?",
    "Sport": lambda r: r.get("sport") or "?",
    "Confidence": lambda r: (r.get("confidence") or "not rated").capitalize(),
    "Fair price from": _fair_group,
}


def clv_breakdown(cfg: Config, days: int | None = None, min_bets: int = 1) -> dict[str, list[tuple]]:
    """CLV by bet type (confirmed pre-game bets on their own), market, book, sport, confidence and where
    the fair price came from: {group: [(name, n, avg_clv, beat_pct)]}."""
    rows = clv_rows(cfg, days)
    out: dict[str, list[tuple]] = {}
    groups = {**CLV_GROUPS, "Bet type": functools.partial(_bet_type, confirmed=confirmed_group(cfg))}
    for group, key in groups.items():
        buckets: dict[str, list[dict]] = {}
        for r in rows:
            buckets.setdefault(key(r), []).append(r)
        out[group] = sorted(
            ((name, len(rs), sum(x["clv_pct"] for x in rs) / len(rs),
              sum(x["beat_close"] for x in rs) / len(rs) * 100)
             for name, rs in buckets.items() if len(rs) >= min_bets),
            key=lambda t: t[1], reverse=True)
    return out


def clv_report(cfg: Config, days: int | None = None) -> str:
    """Plain-text CLV tables for --results."""
    lines = []
    for group, rows in clv_breakdown(cfg, days).items():
        if not rows:
            continue
        lines.append(f"  {group}:")
        for name, n, avg, beat in rows:
            lines.append(f"    {name:<16} {n:>4} bets   CLV {avg:+5.1f}%   beat close {beat:3.0f}%")
    return "\n".join(lines) or "  (no closing lines yet)"


def clv_record(cfg: Config, days: int | None = None) -> str:
    rows = clv_rows(cfg, days)
    if not rows:
        return "no closing lines yet"
    avg = sum(r["clv_pct"] for r in rows) / len(rows)
    beat = sum(r["beat_close"] for r in rows) / len(rows) * 100
    return f"avg {avg:+.1f}%, beat the close on {beat:.0f}% of {len(rows)} bets"


# --------------------------------------------------------------------------- markouts: did the edge hold?

# When to look at an alert's price again: (slot, earliest, latest) seconds after it went out. Checks
# don't run on a timer (live checks slow down on a tight budget, pre-game ones are 15 minutes to a
# few hours apart), so each slot takes the first check inside its window and keeps the real seconds.
MARKOUT_SLOTS = (("next", 1, 3600), ("3m", 150, 360), ("10m", 480, 1200))
MARKOUT_HEADLINE = ("3m", "10m", "next")   # the reading reports use: the first of these that's there
MARKOUT_MOVE_PTS = 0.5       # a move under this many win-% points isn't a move
MARKOUT_VERDICT_BETS = 50    # bets a group needs before it's called a real edge (or flagged)
MARKOUT_FIELDS = (["first_seen", "bet_id", "kind", "live", "sport", "sport_key", "event_id", "matchup",
                   "commence_time", "market", "pick", "player", "outcome", "point", "book", "price", "skip_price",
                   "confidence", "edge_pct", "fair_source", "fair_at_alert"]
                  + [f"{s}_{x}" for s, _, _ in MARKOUT_SLOTS for x in ("secs", "price", "fair", "pct")]
                  + ["markout_pct", "markout_secs", "moved", "still_secs", "still_price", "still_ok", "arb_id", "tier"])


@dataclass
class Markout:
    """One alerted price being followed after the alert (an arb: one per leg)."""
    first_seen: float
    kind: str                 # "ev", "outlier" or "arb"
    live: bool
    sport: str
    sport_key: str
    event_id: str
    matchup: str
    commence_time: str
    market: str
    line: object              # the grouping key (see _line_for)
    outcome: str
    point: float | None       # this outcome's own point
    pick: str
    book: str
    price: float
    skip: float               # the card's "skip if the price is worse than"
    edge: float               # the edge the card showed: +EV/outlier edge, or the arb's profit (%)
    ref: str                  # the card's fair price: "sharp", "consensus", "median" (outliers) or "arb"
    fair0: float = 0.0        # the card's fair win chance (arbs: none)
    confidence: str = ""
    player: str = ""
    arb_id: str = ""          # the legs of one arb share it
    tier: str = ""            # a +EV bet's tier (CONFIRMED: a confirmed pre-game bet)
    readings: dict = field(default_factory=dict)   # slot -> [seconds after, the book's price or None, fair or None]
    still: list | None = None  # [seconds after, the book's price or None] on the first check with this market


# What a saved Markout field may hold, by its type hint (a hint not listed takes anything).
_SAVED_TYPES = {"float": (int, float), "str": str, "bool": bool, "dict": dict, "list": list, "None": type(None)}


def _saved_markout(d: dict) -> Markout:
    """A Markout back from the saved state. TypeError if it can't be one (a missing key, or a
    value of the wrong type), so one damaged entry is skipped rather than crashing every pass."""
    if isinstance(d.get("line"), list):
        d["line"] = tuple(d["line"])   # a prop's (player, point): JSON saved it as a list
    m = Markout(**d)
    hash(m.line)   # a line is a lookup key, which a list inside it can't be
    bad = [f.name for f in fields(Markout)
           if not any(isinstance(getattr(m, f.name), _SAVED_TYPES.get(t.strip(), object)) for t in f.type.split("|"))]
    if bad:
        raise TypeError(f"wrong type for {', '.join(bad)}")
    return m


def _fresh_quotes(ev: dict, cfg: Config, now: datetime, is_live: bool,
                  offers: bool = False) -> dict[tuple, dict[str, dict[str, float]]]:
    """Every fresh main-line price in one game: {(market, line): {book title: {outcome: price}}} (the
    prices fair odds come from). offers: each book's price to bet instead, the better of its main and
    alternate line at the same point (book_offers), as its alert would have read it."""
    out: dict[tuple, dict[str, dict[str, float]]] = {}
    start = _parse_time(ev["commence_time"])
    for bm in ev.get("bookmakers", []):
        if offers:
            for _, oc, k, _, _ in book_offers(bm, ev, now, is_live, cfg):
                out.setdefault(k, {}).setdefault(bm.get("title", bm["key"]), {})[oc["name"]] = float(oc["price"])
            continue
        for mkt in bm.get("markets", []):
            if is_alt(mkt["key"]) or not is_fresh(mkt, bm, now, is_live, cfg, start):
                continue
            for oc in mkt.get("outcomes", []):
                price = float(oc.get("price") or 0)
                if price > 1.0:
                    k = (mkt["key"], _line_for(mkt["key"], oc, ev["home_team"]))
                    out.setdefault(k, {}).setdefault(bm.get("title", bm["key"]), {})[oc["name"]] = price
    return out


def _book_probs(ev: dict, cfg: Config, now: datetime, is_live: bool) -> dict[tuple, dict[str, dict[str, float]]]:
    """Each book's no-vig win chances per line, {(market, line): {book title: {outcome: prob}}}, from
    fresh full markets only: the same numbers find_outliers takes its median from."""
    lines: dict[tuple, dict[str, dict[str, float]]] = {}
    for k, books in _fresh_quotes(ev, cfg, now, is_live).items():
        n_out = max(len(o) for o in books.values())
        full = {bk: o for bk, o in books.items() if len(o) == n_out and n_out >= 2}
        names = set().union(*full.values()) if full else set()
        lines[k] = {bk: dict(zip(o, devig(list(o.values()), cfg.devig_method)))
                    for bk, o in full.items() if set(o) == names}
    return lines


def _markout_fair(m: Markout, ev: dict, cfg: Config, now: datetime, is_live: bool, cache: dict) -> float | None:
    """The fair win chance now, by the same yardstick the alert's card used, so "later" compares
    straight with the edge on the card: Pinnacle's no-vig price (+EV), the consensus of 4+ other
    books with sister books as one vote (a +EV prop Pinnacle doesn't price), or the median of the
    other books (outliers). The alerted book never counts. None when that yardstick can't price it
    now (the source is never swapped), and always for a Kalshi-only bet (Kalshi's price isn't read here)."""
    k = (m.market, m.line)
    if m.ref == "sharp":
        if "sharp" not in cache:
            cache["sharp"] = sharp_fair(ev, cfg, now, is_live)[0]
        return cache["sharp"].get(k, {}).get(m.outcome)
    if m.ref == "consensus":
        bk = _book_key(ev, m.book)
        if ("consensus", bk) not in cache:   # the card's own: the prop settings, the alerted book left out
            cache[("consensus", bk)] = consensus_fair(ev, cfg.for_props(), now, is_live, set(_csv(cfg.sharp_books)),
                                                      leave_out=bk)
        c = cache[("consensus", bk)].get(k)
        return c.probs.get(m.outcome) if c else None
    if m.ref == "median":
        if "books" not in cache:
            cache["books"] = _book_probs(ev, cfg, now, is_live)
        others = sorted(p[m.outcome] for bk, p in cache["books"].get(k, {}).items()
                        if bk != m.book and m.outcome in p)
        if len(others) < cfg.outlier_min_books:
            return None
        mid = len(others) // 2
        return others[mid] if len(others) % 2 else (others[mid - 1] + others[mid]) / 2
    return None   # an arb: the arb itself is the yardstick (are all its legs still there?); or Kalshi alone


def who_moved(price0: float, fair0: float, price1: float | None, fair1: float) -> str:
    """After an alert: "book" (the book fixed its price: it was stale, the edge was real),
    "market" (the market moved to the book: it was just fast, no edge), "neither", or "pulled"
    (the book no longer had that price: taken down, or moved to another line)."""
    if price1 is None:
        return "pulled"
    book = (1 / price1 - 1 / price0) * 100     # + = the book shortened its price, toward the market
    market = (fair0 - fair1) * 100             # + = the fair price came toward the book's
    if max(book, market) < MARKOUT_MOVE_PTS:
        return "neither"
    return "book" if book > market else "market"


class MarkoutTracker:
    """Prices every new alert again on the next checks of its game (next check, ~3 and ~10
    minutes later), against the same fair price its card used. markout = alerted price x later
    fair chance - 1, the CLV sum, but it works for live bets too and settles far sooner than
    win/loss. Every alert gets a row in MARKOUT_FILE once its slots are in or can't be any more
    (a row with no reading counts as "not measured"). Alerts still being followed are saved in
    STATE_DIR, so a restart keeps them. Measuring must never stop or change the alerts, so
    every step here prints a problem and carries on."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pending: list[Markout] = []
        self.dirty = False
        self._load()

    def _path(self) -> Path | None:
        if not self.cfg.markout_file or not self.cfg.state_dir:
            return None
        return data_path(self.cfg.state_dir) / "markouts_pending.json"

    def _load(self) -> None:
        path = self._path()
        if not path or not path.exists():
            return
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            return   # unreadable: start fresh
        for d in saved if isinstance(saved, list) else []:
            try:
                self.pending.append(_saved_markout(d))
            except (AttributeError, TypeError) as e:
                print(f"  ! Markouts: skipped a saved alert: {e!r:.200}", file=sys.stderr)

    def _save(self) -> None:
        path = self._path()
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps([vars(m) for m in self.pending]))
            tmp.replace(path)
        except (OSError, TypeError, ValueError) as e:
            print(f"  ! Couldn't save markout state: {e}", file=sys.stderr)

    def add(self, item, first_seen: float, kind: str) -> None:
        """Start following a new alert (Alerter.on_open). An arb is followed leg by leg."""
        if not self.cfg.markout_file:
            return
        try:
            self._add(item, first_seen, kind)
        except Exception as e:  # noqa: BLE001 - the alert went out; following it must never break that
            print(f"  ! Markouts: couldn't follow {getattr(item, 'matchup', '?')}: {e!r:.200}", file=sys.stderr)

    def _add(self, item, first_seen: float, kind: str) -> None:
        player = item.line[0] if is_prop(item.line) else ""
        base = dict(first_seen=first_seen, kind=kind, live=item.is_live, sport=item.sport,
                    sport_key=item.sport_key, event_id=item.event_id, matchup=item.matchup,
                    commence_time=item.commence_time, market=item.market, line=item.line, player=player)
        if isinstance(item, Arb):
            home = item.matchup.rpartition(" @ ")[2]
            for i, leg in enumerate(item.legs):
                # The arb's line is the home team's point; the away side of a spread is the other sign.
                point = (item.line[1] if player else None if item.line is None
                         else -item.line if item.market == "spreads" and leg.outcome != home else item.line)
                self.pending.append(Markout(
                    **base, outcome=leg.outcome, point=point, book=leg.book, price=leg.price,
                    pick=row_pick({"market": item.market, "outcome": leg.outcome, "point": point, "player": player}),
                    skip=item.worst_ok_price(i) or leg.price, edge=item.profit_pct, ref="arb",
                    arb_id=f"{item.key}|{int(first_seen)}"))
        else:
            ref = ("median" if item.sharp_book.startswith("median")
                   else "consensus" if item.sharp_book.startswith("consensus")
                   else "kalshi" if getattr(item, "kalshi_only", False) else "sharp")
            # (fair0: the price the later readings are measured by, the sharp books' own, without Kalshi.)
            fair0 = item.fair_prob if getattr(item, "sharp_prob", None) is None else item.sharp_prob
            self.pending.append(Markout(**base, outcome=item.outcome, point=item.point, pick=item.pick,
                                        book=item.book, price=item.price, skip=item.worst_ok_price(),
                                        edge=item.ev_pct, ref=ref, fair0=fair0,
                                        confidence=item.confidence, tier=item.tier))
        self.dirty = True

    def update(self, events: list[dict], prop_events: list[dict], now: datetime) -> int:
        """One pass of the main loop, after every alert of the pass went out: read the main-line
        check, then the prop check, then write what's finished. Returns rows written."""
        self.observe(events, now)
        self.observe(prop_events, now)
        return self.finalize(now)

    def observe(self, events: list[dict], now: datetime | None = None) -> None:
        """Read the followed lines in this check. Skipped: a check without the market at all (a
        prop in a main-line check, a market every book suspended) and a pre-game bet once its
        game starts (CLV takes over there). One bad game or alert never stops the rest."""
        now = now or datetime.now(timezone.utc)
        by_game: dict[str, list[Markout]] = {}
        for m in self.pending:
            by_game.setdefault(m.event_id, []).append(m)
        for ev in events:
            try:
                ms = by_game.get(ev.get("id"))
                if not ms:
                    continue
                is_live = _parse_time(ev["commence_time"]) <= now
                quotes = _fresh_quotes(ev, self.cfg, now, is_live, offers=True)   # (the book's price to bet)
            except Exception as e:  # noqa: BLE001 - measuring alerts must never stop them
                print(f"  ! Markouts: {e!r:.200}", file=sys.stderr)
                continue
            cache: dict = {}
            for m in ms:
                try:
                    self._read(m, ev, quotes, cache, now, is_live)
                except Exception as e:  # noqa: BLE001
                    print(f"  ! Markouts: {m.pick} ({m.book}): {e!r:.200}", file=sys.stderr)

    def _read(self, m: Markout, ev: dict, quotes: dict, cache: dict, now: datetime, is_live: bool) -> None:
        if (is_live and not m.live) or not any(market == m.market for market, _ in quotes):
            return
        k = (m.market, m.line)
        secs = now.timestamp() - m.first_seen
        # Fresh only, else it's not there. A line no book quotes any more (they all moved off
        # it) is gone too, the same as one book taking its price down: None, "pulled".
        price = quotes.get(k, {}).get(m.book, {}).get(m.outcome)
        first, last = next((lo, hi) for s, lo, hi in MARKOUT_SLOTS if s == "next")
        if m.still is None and first <= secs <= last:
            m.still = [round(secs), price]   # was it still there? Needs no fair price
            self.dirty = True
        slots = [s for s, lo, hi in MARKOUT_SLOTS if s not in m.readings and lo <= secs <= hi]
        if not slots:
            return
        fair = None
        if m.ref != "arb":
            fair = _markout_fair(m, ev, self.cfg, now, is_live, cache) if k in quotes else None
            if fair is None:
                return   # its yardstick can't price it now: no markout from this check
        for s in slots:
            m.readings[s] = [round(secs), price, None if fair is None else round(fair, 5)]
        self.dirty = True

    def finalize(self, now: datetime | None = None) -> int:
        """Write the alerts that are done: every slot in or past its window, or a pre-game bet's
        game started. Returns how many rows were written."""
        now = now or datetime.now(timezone.utc)
        written, keep = 0, []
        for m in self.pending:
            try:
                secs = now.timestamp() - m.first_seen
                done = (all(s in m.readings or secs > hi for s, _, hi in MARKOUT_SLOTS)
                        or (not m.live and _parse_time(m.commence_time) <= now))
                if not done:
                    keep.append(m)
                    continue
                self.dirty = True
                append_csv(self.cfg.markout_file, MARKOUT_FIELDS, markout_row(m))
                written += 1
            except Exception as e:  # noqa: BLE001 - dropped, so it can't fail on every pass
                self.dirty = True
                print(f"  ! Markouts: couldn't save {getattr(m, 'pick', '?')}: {e!r:.200}", file=sys.stderr)
        self.pending = keep
        if self.dirty:
            self._save()
            self.dirty = False
        return written


def make_markouts(cfg: Config, args, alerters: list[Alerter]) -> MarkoutTracker:
    """The markout tracker, following every new alert of these alerters. Off (nothing followed,
    no files written) for --once (one look can't follow anything), --demo (sample data must
    never reach the logs) and --dry-run (a test run must not write the service's files).
    Parlays aren't followed: their legs already are, one by one."""
    off = any(getattr(args, flag, False) for flag in ("once", "demo", "dry_run"))
    tracker = MarkoutTracker(replace(cfg, markout_file="") if off else cfg)
    for a in alerters:
        if isinstance(a, ParlayAlerter):
            continue
        kind = "outlier" if isinstance(a, OutlierAlerter) else "ev" if isinstance(a, EVAlerter) else "arb"
        a.on_open = lambda item, first, kind=kind: tracker.add(item, first, kind)
    return tracker


def markout_row(m: Markout) -> dict:
    """The markouts.csv row for one finished alert (or arb leg)."""
    row = {"first_seen": datetime.fromtimestamp(m.first_seen, timezone.utc).isoformat(timespec="seconds"),
           "bet_id": _bet_id({"event_id": m.event_id, "market": m.market, "player": m.player,
                              "outcome": m.outcome, "point": _blank(m.point)}),
           "kind": m.kind, "live": m.live, "sport": m.sport, "sport_key": m.sport_key, "event_id": m.event_id,
           "matchup": m.matchup, "commence_time": m.commence_time, "market": m.market, "pick": m.pick,
           "player": m.player, "outcome": m.outcome, "point": _blank(m.point), "book": m.book, "price": m.price,
           "skip_price": round(m.skip, 3), "confidence": m.confidence, "edge_pct": round(m.edge, 2),
           "fair_source": m.ref, "fair_at_alert": round(m.fair0, 5) if m.fair0 else "", "arb_id": m.arb_id,
           "tier": m.tier}
    for slot, _, _ in MARKOUT_SLOTS:
        secs, price, fair = m.readings.get(slot) or ("", None, None)
        row.update({f"{slot}_secs": secs, f"{slot}_price": _blank(price), f"{slot}_fair": _blank(fair),
                    f"{slot}_pct": "" if fair is None else round(clv_pct(m.price, fair), 2)})
    head = next((s for s in MARKOUT_HEADLINE if s in m.readings and m.readings[s][2] is not None), None)
    if head:
        secs, price, fair = m.readings[head]
        row.update(markout_pct=round(clv_pct(m.price, fair), 2), markout_secs=secs,
                   moved=who_moved(m.price, m.fair0, price, fair))
    if m.still is not None:
        secs, price = m.still
        # "At or above the skip price" as the card printed both (-104 is -104, however it's rounded).
        row.update(still_secs=secs, still_price=_blank(price),
                   still_ok=int(price is not None and _printed(price) >= _printed(m.skip)))
        if not head and price is None:
            row["moved"] = "pulled"   # nothing could price it then, but the price was gone
    return row


def markout_group(r: dict) -> str:
    """Live outliers, Pre-game outliers, Pre-game +EV, Live +EV, Props or Arbs."""
    if r.get("kind") == "arb":
        return "Arbs"
    if r.get("player"):
        return "Props"
    when = "Live" if str(r.get("live")).lower() == "true" else "Pre-game"
    return f"{when} {'outliers' if r.get('kind') == 'outlier' else '+EV'}"


MARKOUT_GROUP_ORDER = ["Live outliers", "Pre-game outliers", "Pre-game +EV", "Live +EV", "Props", "Arbs"]
# Only live bets get a ✅/⚠️ verdict. Pre-game checks can be hours apart (and props are all pre-game),
# so many pre-game alerts get no reading within the hour; CLV is the better test there and a second
# verdict could contradict it.
MARKOUT_JUDGED = {"Live outliers", "Live +EV"}


def markout_rows(cfg: Config) -> list[dict]:
    """Every finished markout at your books (MY_BOOKS), oldest first, the first card per bet only
    (the same rule as the records: a bet that comes back after GONE, or is sent again after a
    restart, counts once). Arb legs count apart from single bets."""
    if not cfg.markout_file:
        return []   # (_read_csv("") would open the bot's own folder)
    rows = sorted((r for r in _read_csv(cfg.markout_file) if cfg.counts(r.get("book", ""))),
                  key=lambda r: r.get("first_seen") or "")
    out, seen = [], set()
    for i, r in enumerate(rows):
        key = (r.get("kind") == "arb", r.get("bet_id") or i)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _cell(v) -> float | None:
    """A number from a CSV cell, or None if it's blank or not a number."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def markout_stats(xs: list[float]) -> tuple[int, float, float | None, float | None, float | None]:
    """(n, mean, standard error, low end, high end of the 95% range). Under 2 values: no range."""
    n = len(xs)
    if n < 2:
        return n, (xs[0] if xs else 0.0), None, None, None
    mean = sum(xs) / n
    se = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1) / n)
    return n, mean, se, mean - 1.96 * se, mean + 1.96 * se


def markout_verdict(n: int, lo: float | None, hi: float | None) -> str:
    if n < MARKOUT_VERDICT_BETS or lo is None:
        return ""
    return "✅ real edge" if lo > 0 else "⚠️ review" if hi < 0 else ""


def _markout_summary(name: str, rows: list[dict], slot: str = "", judge: bool = False) -> dict:
    """One group's numbers. n = bets with a reading ("measured"), of = every bet in the group."""
    col, secs_col = f"{slot or 'markout'}_pct", f"{slot or 'markout'}_secs"
    measured = [r for r in rows if _cell(r.get(col)) is not None]
    n, mean, se, lo, hi = markout_stats([_cell(r[col]) for r in measured])
    sent = [x for x in (_cell(r.get("edge_pct")) for r in measured) if x is not None]
    still = [r["still_ok"] == "1" for r in rows if r.get("still_ok") in ("0", "1")]
    # Pulled: of the bets looked at again at all, even with nothing to price them against then.
    moved = [r.get("moved") for r in rows if r.get("moved") or r.get("still_ok") in ("0", "1")]
    fixed = [x for x in moved if x in ("book", "market")]
    secs = sorted(x for x in (_cell(r.get(secs_col)) for r in measured) if x is not None)
    return {"name": name, "n": n, "of": len(rows), "sent": sum(sent) / len(sent) if sent else None,
            "mean": mean if n else None, "se": se, "lo": lo, "hi": hi,
            "still": sum(still) / len(still) * 100 if still else None,
            "book_moved": fixed.count("book") / len(fixed) * 100 if fixed else None,
            "pulled": moved.count("pulled") / len(moved) * 100 if moved else None,
            "secs": secs[len(secs) // 2] if secs else None,
            "verdict": markout_verdict(n, lo, hi) if judge else ""}


def _arb_summary(rows: list[dict]) -> dict:
    """Arbs: how often every leg was still there (at or above its skip price) on the next check."""
    legs: dict[str, list[dict]] = {}
    for r in rows:
        legs.setdefault(r.get("arb_id", ""), []).append(r)
    done = [v for v in legs.values() if all(r.get("still_ok") in ("0", "1") for r in v)]
    sent = [x for x in (_cell(v[0].get("edge_pct")) for v in done) if x is not None]
    return {"name": "Arbs", "n": len(done), "of": len(legs), "sent": sum(sent) / len(sent) if sent else None,
            "mean": None, "se": None, "lo": None, "hi": None,
            "still": sum(all(r["still_ok"] == "1" for r in v) for v in done) / len(done) * 100 if done else None,
            "book_moved": None, "pulled": None, "secs": None, "verdict": ""}


def markout_breakdown(cfg: Config) -> dict[str, list[dict]]:
    """Markouts by alert type, then (single bets only) by book and by sport. Groups with no
    measured bet are left out; markout_coverage counts them."""
    rows = markout_rows(cfg)
    single = [r for r in rows if r.get("kind") != "arb"]
    out: dict[str, list[dict]] = {"Alert type": []}
    for name in MARKOUT_GROUP_ORDER:
        group = [r for r in rows if markout_group(r) == name]
        if group:
            out["Alert type"].append(_arb_summary(group) if name == "Arbs"
                                     else _markout_summary(name, group, judge=name in MARKOUT_JUDGED))
    for title, key in (("Book", "book"), ("Sport", "sport")):
        buckets: dict[str, list[dict]] = {}
        for r in single:
            buckets.setdefault(r.get(key) or "?", []).append(r)
        out[title] = sorted((_markout_summary(k, v) for k, v in buckets.items()), key=lambda g: -g["n"])
    return {k: [g for g in v if g["n"]] for k, v in out.items()}


def markout_coverage(cfg: Config) -> tuple[int, int]:
    """(measured, all) pre-game bets and props: the groups without a verdict."""
    rows = [r for r in markout_rows(cfg) if markout_group(r) not in MARKOUT_JUDGED | {"Arbs"}]
    return sum(1 for r in rows if _cell(r.get("markout_pct")) is not None), len(rows)


def _mk_pct(x: float | None, places: int = 1, signed: bool = True) -> str:
    return "—" if x is None else f"{x:+.{places}f}%" if signed else f"{x:.{places}f}%"


def _bets(g: dict, noun: str = "bet") -> str:
    """'64 bets', or '31 of 80 bets measured' when some had no later reading."""
    def many(k: int) -> str:
        return f"{k} {noun}{'s' if k != 1 else ''}"
    return many(g["n"]) if g["n"] == g["of"] else f"{g['n']} of {many(g['of'])} measured"


def _pregame_note(cfg: Config) -> str:
    measured, total = markout_coverage(cfg)
    if not total:
        return ""
    return (f"Pre-game and props get no ✅/⚠️: their checks can be hours apart and only alerts re-checked "
            f"within an hour are measured, so CLV is the better test there (measured {measured} of {total}).")


def markout_table(cfg: Config) -> str:
    """The scoreboard's 📏 part: one line per alert type, all time."""
    groups = markout_breakdown(cfg)["Alert type"]
    if not groups:
        return ""
    w = max(len(name) for name in MARKOUT_GROUP_ORDER)   # every name fits, so the columns line up
    lines = [f"{'':<{w}}{'bets':>5}{'sent':>6}{'later':>7}{'±':>5}{'still':>6}"]
    for g in groups:
        pm = f"{1.96 * g['se']:.1f}" if g["se"] is not None else "—"
        mark = {"✅ real edge": " ✅", "⚠️ review": " ⚠️"}.get(g["verdict"], "")
        lines.append(f"{g['name']:<{w}}{g['n']:>5}{_mk_pct(g['sent'], 0):>6}{_mk_pct(g['mean']):>7}{pm:>5}"
                     f"{_mk_pct(g['still'], 0, False):>6}{mark}")
    note = _pregame_note(cfg)
    return ("__**📏 Did the edge hold?**__\n"
            "Each alert's price checked again on the next checks (live: ~3 min later), against the same "
            "fair odds its card used.\n```\n" + "\n".join(lines) + "\n```\n"
            "Sent = the edge on the card · later = that price's edge then (± = 95% range) · still = the book "
            "still had it, at or above the skip price, on the next check.\n"
            f"✅ = {MARKOUT_VERDICT_BETS}+ live bets and above 0 even at the low end · ⚠️ = "
            f"{MARKOUT_VERDICT_BETS}+ live bets and below 0 even at the high end: review"
            + (f"\n{note}" if note else ""))


def markout_line(g: dict) -> str:
    """One alert type for the daily summary."""
    still = f" · still there {g['still']:.0f}%" if g["still"] is not None else ""
    if g["mean"] is None:   # arbs
        return (f"{g['name']}: every leg still there on the next check {_mk_pct(g['still'], 0, False)} "
                f"({_bets(g, 'arb')})")
    rng = f", 95% range {g['lo']:+.1f}% to {g['hi']:+.1f}%" if g["lo"] is not None else ""
    return (f"{g['name']}: sent {_mk_pct(g['sent'])} → later {g['mean']:+.1f}% ({_bets(g)}{rng}){still}"
            + (f" · {g['verdict']}" if g["verdict"] else ""))


def markout_books_line(cfg: Config, min_bets: int = 10) -> str:
    """'Best book: ... · Worst: ...' for the daily summary, from books with min_bets or more."""
    books = [g for g in markout_breakdown(cfg)["Book"] if g["n"] >= min_bets]
    if len(books) < 2:
        return ""
    best, worst = max(books, key=lambda g: g["mean"]), min(books, key=lambda g: g["mean"])
    return (f"\nBest book: {best['name']} ({best['mean']:+.1f}% later, {best['n']} bets) · "
            f"Worst: {worst['name']} ({worst['mean']:+.1f}% later, {worst['n']} bets)")


def markout_span(cfg: Config) -> str:
    """How far back markouts.csv goes: "last 60 days" (LOG_KEEP_DAYS cuts it), or "all time"."""
    return f"last {cfg.log_keep_days} days" if cfg.log_keep_days else "all time"


def groups_off(cfg: Config) -> set[str]:
    """Markout groups for alert types that are turned off now: the daily summary leaves them out (their past
    numbers stay in --results)."""
    off = set()
    if not cfg.arbs_enabled:
        off.add("Arbs")
    if not (cfg.outliers_enabled and cfg.outlier_live):
        off.add("Live outliers")
    if not (cfg.ev_enabled and cfg.ev_live):
        off.add("Live +EV")
    if not cfg.outliers_enabled:
        off.add("Pre-game outliers")
    if not cfg.ev_enabled:
        off.add("Pre-game +EV")
    if not cfg.props_enabled:
        off.add("Props")
    return off


def markout_summary(cfg: Config) -> str:
    """The daily summary's 📏 section ("" with no markouts yet), for the alert types that are on."""
    off = groups_off(cfg)
    groups = [g for g in markout_breakdown(cfg)["Alert type"] if g["name"] not in off]
    if not groups:
        return ""
    note = _pregame_note(cfg)
    return ("📏 **Did the edge hold?** (each alert's price a few minutes later, against the same fair odds "
            f"its card used; {markout_span(cfg)})\n" + "\n".join(markout_line(g) for g in groups) + markout_books_line(cfg)
            + (f"\n{note}" if note else ""))


def markout_report(cfg: Config) -> str:
    """Plain-text markout tables for --results: by alert type (with each slot), book and sport."""
    lines = []
    rows = markout_rows(cfg)
    for group, items in markout_breakdown(cfg).items():
        if not items:
            continue
        lines.append(f"  {group}:")
        for g in items:
            if g["mean"] is None:
                lines.append(f"    {g['name']:<18} {_bets(g, 'arb')}: every leg still there on the next check "
                             f"{_mk_pct(g['still'], 0, False)}")
                continue
            rng = f" (95% range {g['lo']:+.1f}% to {g['hi']:+.1f}%)" if g["lo"] is not None else ""
            after = f" ~{_fmt_secs(g['secs'])} after the alert" if g["secs"] is not None else ""
            extra = "".join(x for x in (
                f"  still there {g['still']:.0f}%" if g["still"] is not None else "",
                f"  book fixed it {g['book_moved']:.0f}%" if g["book_moved"] is not None else "",
                f"  pulled {g['pulled']:.0f}%" if g["pulled"] is not None else "",
                f"  {g['verdict']}" if g["verdict"] else ""))
            lines.append(f"    {g['name']:<18} {_bets(g)}: sent {_mk_pct(g['sent'])} → later {g['mean']:+.1f}%"
                         f"{rng}{after}{extra}")
            if group == "Alert type":
                mine = [r for r in rows if markout_group(r) == g["name"]]
                slots = [(s, _markout_summary(s, mine, s)) for s, _, _ in MARKOUT_SLOTS]
                if by_time := " · ".join(f"{s} {x['mean']:+.1f}% (n={x['n']}, ~{_fmt_secs(x['secs'])})"
                                         for s, x in slots if x["n"]):
                    lines.append(f"      by time: {by_time}")
    note = _pregame_note(cfg)
    if lines and note:
        lines.append("  " + note)
    return "\n".join(lines)


def _safely(what: str, fn, *args) -> str:
    """fn(*args), or "" after printing the problem: a bad markouts.csv must never stop the
    scoreboard, the daily summary or --results (an error there would crash the bot)."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        print(f"  ! Markouts ({what}): {e!r:.200}", file=sys.stderr)
        return ""


# --------------------------------------------------------------------------- weekly report card

HELD_FILE = "held_back.json"   # in STATE_DIR: alerts held back, per day and alert type
WEEKLY_TYPES = {   # the card's alert types: (icon, what its graded bets' _kind() is, its markout groups)
    "Pre-game arbs": ("💰", "", ["Pre-game arbs"]), "Live arbs": ("💰", "", ["Live arbs"]),
    "+EV": ("📈", "ev", ["Pre-game +EV", "Live +EV"]),
    "Outliers": ("🚨", "outlier", ["Pre-game outliers", "Live outliers"]),
    "Props": ("🎯", "prop", ["Props"]), "Parlays": ("📦", "parlay", []),
}
CLV_KEEP_BETS = 5   # a CLV group is named (best / worst in the daily summary) from this many bets


def alert_type(item) -> str:
    """Which of the weekly card's alert types (WEEKLY_TYPES) an alert is."""
    if isinstance(item, Arb):
        return "Live arbs" if item.is_live else "Pre-game arbs"
    if isinstance(item, Parlay):
        return "Parlays"
    if is_prop(getattr(item, "line", None)):
        return "Props"
    return "Outliers" if str(getattr(item, "sharp_book", "")).startswith("median") else "+EV"


def held_path(cfg: Config) -> Path | None:
    return data_path(cfg.state_dir) / HELD_FILE if cfg.state_dir else None


def read_held(path: Path | None) -> dict[str, dict[str, dict[str, int]]]:
    """The saved tally, {day: {alert type: {why: count}}}; {} when it's missing or unreadable."""
    if not path or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return {str(d): {str(k): {str(w): int(n) for w, n in whys.items()} for k, whys in kinds.items()}
                for d, kinds in data.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


class HeldTally:
    """How many alerts the hourly caps and the live rules held back, per local day and alert type,
    for the weekly report card. Each one counts once a day for the same reason, however many checks
    held it back (pre-game and props are checked 15+ minutes apart, so a gap between checks can't
    tell a new stretch from the same one); "waiting" doesn't count (it ends up sent, or
    "unconfirmed"). Saved in STATE_DIR by save() (path None: memory only), 15 days kept; which ones
    were counted today is in memory only, so a restart can count one again. Counting must never get
    in the way of the alerts, so a problem is printed and skipped."""

    KEEP_DAYS = 15

    def __init__(self, cfg: Config, path: Path | None):
        self.cfg, self.path = cfg, path
        self.days = read_held(path)
        self.seen: dict[str, set[tuple]] = {}   # day: (alert type, item key, why) counted that day
        self.dirty = False

    def add(self, item, why: str, now: float) -> None:
        if why == "waiting":
            return
        try:
            day = datetime.fromtimestamp(now, ZoneInfo(self.cfg.timezone)).date().isoformat()
            seen = self.seen.setdefault(day, set())
            if len(self.seen) > 2:   # today and yesterday (a check that started just before midnight)
                self.seen = {d: self.seen[d] for d in sorted(self.seen)[-2:]}
            kind = alert_type(item)
            key = (kind, item.key, why)
            if key in seen:
                return
            seen.add(key)
            kinds = self.days.setdefault(day, {}).setdefault(kind, {})
            kinds[why] = kinds.get(why, 0) + 1
            self.dirty = True
        except Exception as e:  # noqa: BLE001
            print(f"  ! Couldn't count a held-back alert: {e!r:.200}", file=sys.stderr)

    def save(self) -> None:
        if not (self.path and self.dirty):
            return
        self.days = {d: self.days[d] for d in sorted(self.days)[-self.KEEP_DAYS:]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.days))
            tmp.replace(self.path)
            self.dirty = False
        except OSError as e:
            print(f"  ! Couldn't save the held-back count: {e}", file=sys.stderr)


def _in_week(cfg: Config, ts: str, start: date, end: date) -> bool:
    try:
        return start <= local_day(cfg, ts) < end
    except (TypeError, ValueError):
        return False


def _clv_text(rows: list[dict]) -> str:
    if not rows:
        return "no closing lines yet"
    avg = sum(r["clv_pct"] for r in rows) / len(rows)
    beat = sum(r["beat_close"] for r in rows) / len(rows) * 100
    return f"avg {avg:+.1f}%, beat the close on {beat:.0f}% of {len(rows)} bet{'s' if len(rows) != 1 else ''}"


def _held_text(whys: dict[str, int]) -> str:
    n = sum(whys.values())
    if not n:
        return "none"
    return f"{n} (" + ", ".join(f"{c} {HELD_LABELS.get(w, w)}" for w, c in
                                sorted(whys.items(), key=lambda x: (-x[1], x[0])) if c) + ")"


def _group_summary(name: str, rows: list[dict]) -> dict:
    if name.endswith("arbs"):
        return {**_arb_summary(rows), "name": name}
    return _markout_summary(name, rows, judge=name in MARKOUT_JUDGED)


def _weekly_group(r: dict) -> str:
    """markout_group(), with arbs split into pre-game and live like the card."""
    if r.get("kind") == "arb":
        return "Live arbs" if str(r.get("live")).lower() == "true" else "Pre-game arbs"
    return markout_group(r)


def _markout_advice(name: str, rows: list[dict], setting: str) -> str:
    """A live alert type, judged the way the 📏 tables judge it (MARKOUT_VERDICT_BETS live bets)."""
    g = _group_summary(name, rows)
    n = g["n"]
    if not n:
        return f"{name}: not measured yet → too early to tell (0 bets)"
    when = f"~{max(1, round(g['secs'] / 60))} min later" if g["secs"] is not None else "later"
    facts = (f"sent {_mk_pct(g['sent'])} → {g['mean']:+.1f}% {when}"
             + (f" (still there {g['still']:.0f}%)" if g["still"] is not None else ""))
    if n < MARKOUT_VERDICT_BETS:
        return f"{name}: {facts} → too early to tell ({n} bet{'s' if n != 1 else ''})"
    verdict = {"⚠️ review": f"consider {setting}", "✅ real edge": "keep"}.get(g["verdict"],
                                                                            "no clear signal yet: keep watching")
    return f"{name}: {n} bets, {facts} → {verdict}"


def _clv_advice(name: str, rows: list[dict], change: str) -> str:
    """A pre-game alert type, judged on CLV by the daily summary's rule (✅ = average above 0 and the
    close beaten more often than not, from CLV_KEEP_BETS bets). A change is only suggested from
    MARKOUT_VERDICT_BETS bets, when both are the other way."""
    n = len(rows)
    avg = sum(r["clv_pct"] for r in rows) / n
    beat = sum(r["beat_close"] for r in rows)
    facts = f"CLV {avg:+.1f}%, beat the close {beat / n * 100:.0f}%"
    if n >= CLV_KEEP_BETS and avg > 0 and beat * 2 > n:
        return f"{name}: {facts} over {n} bets → keep"
    if n < MARKOUT_VERDICT_BETS:
        return f"{name}: {facts} → too early to tell ({n} bet{'s' if n != 1 else ''})"
    if avg < 0 and beat * 2 < n:
        return f"{name}: {facts} over {n} bets → consider {change}"
    return f"{name}: {facts} over {n} bets → no clear signal yet: keep watching"


def weekly_suggestions(cfg: Config, clv: list[dict], marks: list[dict]) -> list[str]:
    """Plain-English suggestions from the week's CLV (pre-game) and markouts (live). Nothing is ever
    changed: the card only says what the numbers point to, and "too early to tell" until they can."""
    sharp = (_csv(cfg.sharp_books) or ["sharp"])[0].title()
    out = []
    for name, setting in (("Live outliers", "OUTLIER_LIVE=false"), ("Live +EV", "EV_LIVE=false")):
        rows = [r for r in marks if markout_group(r) == name]
        if rows:
            out.append(_markout_advice(name, rows, setting))
    for name, pick, change in (
            ("Pre-game +EV", lambda r: r["kind"] == "ev" and not r.get("player") and r.get("tier") != CONFIRMED,
             f"raising MIN_EV_PCT (now {cfg.min_ev_pct:g})"),
            (confirmed_group(cfg), lambda r: r["kind"] == "ev" and r.get("tier") == CONFIRMED,
             "PREGAME_CONFIRMED_EV_PCT=0"),
            ("Pre-game outliers", lambda r: r["kind"] == "outlier" and not r.get("player"),
             f"raising OUTLIER_MIN_PCT (now {cfg.outlier_min_pct:g})"),
            (f"Props with a {sharp} price", lambda r: r.get("player") and r["kind"] == "ev"
             and _fair_group(r) not in ("Other books", "Unknown"), f"raising PROP_MIN_EV_PCT (now {cfg.prop_min_ev_pct:g})"),
            (f"Props with no {sharp} price", lambda r: r.get("player") and _fair_group(r) == "Other books",
             f"raising PROP_MIN_BOOKS (now {cfg.prop_min_books})")):
        rows = [r for r in clv if pick(r)]
        if rows:
            out.append(_clv_advice(name, rows, change))
    return out


def _weekly_section(cfg: Config, kind: str, start: date, end: date, graded: list[dict], clv: list[dict],
                    marks: list[dict], held: dict[str, int]) -> list[str]:
    icon, result_kind, groups = WEEKLY_TYPES[kind]
    if not result_kind:   # arbs: locked in when placed, so the "record" is what they locked in
        live = kind == "Live arbs"
        rows = [r for r in (_read_csv(cfg.log_file) if cfg.log_file else []) if r.get("first_seen")
                and not r.get("reason") and _in_week(cfg, r["first_seen"], start, end)
                and (str(r.get("live")).lower() == "true") == live]
        first: dict[tuple, dict] = {}
        for r in rows:   # once per local day, as the daily recap (arbs_on): a series' games aren't one arb
            first.setdefault((local_day(cfg, r["first_seen"]), r.get("matchup"), r.get("market"), r.get("line")), r)
        pct = [_cell(r.get("best_profit_pct")) or 0.0 for r in first.values()]
        head = (f"{len(first)} different · about {signed_money(sum(pct) * cfg.bankroll / 100)} locked in at "
                f"{money(cfg.bankroll)} each (ROI {sum(pct) / len(pct):+.1f}%)" if first else "no alerts")
    else:
        rows = [r for r in graded if _kind(r) == result_kind]
        head = record_line(rows)
        live = [r for r in rows if str(r.get("live")).lower() == "true"]
        if live and len(live) < len(rows):   # both: each on its own too
            pre = [r for r in rows if str(r.get("live")).lower() != "true"]
            for label, group in (("🔴 live", live), ("⏰ pre-game", pre)):
                w, l, pu, profit, _ = _record(group)
                head += f" · {label} {w}-{l}" + (f"-{pu}" if pu else "") + f" {signed_money(profit)}"
    mine = [r for r in clv if _kind(r) == result_kind] if result_kind in ("ev", "outlier", "prop") else []
    measured = [(name, [m for m in marks if _weekly_group(m) == name]) for name in groups]
    measured = [(name, ms) for name, ms in measured if ms]
    lines = [f"{icon} **{kind}** · {head}"]
    if not (rows or mine or measured or sum(held.values())):
        return lines   # nothing at all this week: one line
    if result_kind in ("ev", "outlier", "prop"):
        lines.append(f"📐 CLV (pre-game): {_clv_text(mine)}")
    confirmed = ([r for r in rows if r.get("tier") == CONFIRMED], [r for r in mine if r.get("tier") == CONFIRMED])
    if result_kind == "ev" and any(confirmed):
        lines.append(f"✅✅ {confirmed_group(cfg)}: {record_line(confirmed[0])} · CLV {_clv_text(confirmed[1])}")
    if groups:
        parts = []
        for name, ms in measured:
            g = _group_summary(name, ms)
            parts.append(markout_line(g) if g["n"] else f"{name}: not measured ({len(ms)} alerts)")
        lines.append("📏 " + (" · ".join(parts) if parts else "Did the edge hold: nothing measured yet"))
    lines.append(f"✋ Held back: {_held_text(held)}")
    return lines


def weekly_text(cfg: Config, end_day: date, held: dict | None = None) -> tuple[str, str, float]:
    """(title, card text, the week's profit) for the 7 local days before end_day: every alert type's
    record and profit at the stakes shown, ROI, CLV, markouts and what the caps and live rules held
    back, then suggestions. Each part reads its own file; a missing, empty or broken file only
    leaves that part saying so. held: the tally ({day: {type: {why: n}}}; None = the saved one)."""
    start, last = end_day - timedelta(days=7), end_day - timedelta(days=1)

    def read(what: str, fn, default):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - one broken file must never stop the card
            print(f"  ! Weekly report card: couldn't read {what}: {e!r:.200}", file=sys.stderr)
            problems.append(what)
            return default
    problems: list[str] = []
    graded = read(cfg.ev_results_file, lambda: [r for r in (_graded(cfg) if cfg.ev_results_file else [])
                                                if _in_week(cfg, r["commence_time"], start, end_day)], [])
    clv = read(cfg.closing_file, lambda: [r for r in (clv_rows(cfg) if cfg.closing_file else [])
                                          if _in_week(cfg, r["commence_time"], start, end_day)], [])
    marks = read(cfg.markout_file, lambda: [r for r in markout_rows(cfg)
                                            if _in_week(cfg, r.get("first_seen") or "", start, end_day)], [])
    tally = read(HELD_FILE, lambda: read_held(held_path(cfg)) if held is None else held, {})
    whys: dict[str, dict[str, int]] = {}
    for day, kinds in tally.items():
        try:
            if not start <= date.fromisoformat(day) < end_day:
                continue
        except ValueError:
            continue
        for kind, counts in kinds.items():
            bucket = whys.setdefault(kind, {})
            for why, n in counts.items():
                bucket[why] = bucket.get(why, 0) + n
    parts = [f"**{start:%a %b %-d} – {last:%a %b %-d}** · every alert at the stake it showed (bets by game "
             f"day, arbs by when they went out). ✋ Held back = stopped by the hourly caps or the live rules."]
    for kind in WEEKLY_TYPES:
        parts.append("\n".join(read(f"the {kind} numbers", lambda kind=kind: _weekly_section(
            cfg, kind, start, end_day, graded, clv, marks, whys.get(kind, {})), [f"**{kind}**: couldn't read it"])))
    tips = read("the suggestions", lambda: weekly_suggestions(cfg, clv, marks), [])
    parts.append("💡 **Suggestions** (nothing changes unless you change it)\n"
                 + ("\n".join(f"• {t}" for t in tips) if tips else "• Nothing to judge yet this week."))
    if problems:
        parts.append(f"⚠️ Couldn't read {', '.join(dict.fromkeys(problems))}: those parts are left out.")
    title = f"📋 Weekly report card · {start:%b %-d} – {last:%b %-d}"
    return title, "\n\n".join(parts), _record(graded)[3]


def weekly_payload(cfg: Config, end_day: date, held: dict | None = None) -> dict:
    title, text, profit = weekly_text(cfg, end_day, held)
    return _card(title, text, 0x2ECC71 if profit > 0 else (0xE74C3C if profit < 0 else 0x5865F2),
                 footer="Suggestions only: the bot never changes a setting by itself. Each arb counts once a "
                        "day, placed at BANKROLL.")


def weekly_command(cfg: Config, args) -> None:
    """--weekly prints the report card for the 7 days before today; --post-weekly posts it and marks
    that week as posted (so a Monday one doesn't go out twice). Free: nothing is graded here (the bot
    grades as games finish)."""
    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    if not args.post_weekly:
        title, text, _ = weekly_text(cfg, today)
        print(re.sub(r"__|\*\*", "", f"{title}\n\n{text}"))
        return
    cfg.bad_webhooks()
    res = Results(cfg, dry_run=args.dry_run)
    if res.weekly(today) == "failed":
        sys.exit("Couldn't post to Discord (see the error above). Try again in a minute.")
    print("(dry run: not sent)" if res.dry_run else "Posted the weekly report card to the results channel.")


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
    """When the plan's credits next reset (BILLING_DAY at midnight UTC; the 31st means the
    last day of shorter months)."""
    def at(y: int, m: int) -> datetime:
        day = max(1, min(cfg.billing_day, calendar.monthrange(y, m)[1]))
        return now.replace(year=y, month=m, day=day, hour=0, minute=0, second=0, microsecond=0)
    cand = at(now.year, now.month)
    if cand <= now:
        y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        cand = at(y, m)
    return cand


LIVE, PREGAME, EARLY, FAR = "live", "pregame", "early", "far"
PROP_NEAR, PROP_EARLY = "prop_near", "prop_early"   # prop checks near kickoff / further out
PROP_NEAR_X = "prop_near_x"   # what PROP_NEAR_MARKETS add to the near-kickoff prop checks: paid from spare credits only
# The pre-game check of a sport that also has a live check (LIVE_MARKETS split): the other bet
# types of its later games, every PREGAME_WITH_LIVE_EVERY live checks (default: every live check,
# at POLL_SECONDS). Before the split these came with every live check, and they're the pre-game bets
# most worth keeping fresh on a busy day: on a tight budget the live check slows first, then early
# checks (early props and 1-2 days out most), and only then this one.
PRE_LIVE = "pre_live"
CORE = {LIVE, PREGAME, PRE_LIVE}   # protected on a tight budget; EARLY/FAR stretch first
STEP = 300  # seconds per slice when forecasting the next 24h
# Spare credits buy faster pre-game checks in this order, each down to its floor: the pre-game
# checks of sports with a live game first (down to every live check: only when
# PREGAME_WITH_LIVE_EVERY is above 1), then near kickoff (the bets most worth acting on), later
# today, early props, props near kickoff, and 1-2 days out last (rarely a locks alert). The extra
# prop types near kickoff (PROP_NEAR_MARKETS, all or nothing) come right after the near-kickoff
# speed-up: they never slow a live or main-line check, nor take from those two speed-ups.
SPARE_ORDER = (PRE_LIVE, PREGAME, PROP_NEAR_X, EARLY, PROP_EARLY, PROP_NEAR, FAR)
LANE_KINDS = {"live": "odds:live", "pre": "odds:pre", "all": "odds"}   # CostBook kinds of the checks


def merge_events(events: list[dict]) -> list[dict]:
    """One event per game: a live check and a pre-game check of the same game in one pass (each
    with its own bet types) become one event with every bet type."""
    out: list[dict] = []
    by_id: dict[str, dict] = {}
    for ev in events:
        have = by_id.get(ev.get("id"))
        if have is None:
            by_id[ev.get("id")] = ev
            out.append(ev)
            continue
        if ev.get("_fetched_at") and have.get("_fetched_at"):   # (its odds are as old as the older check's)
            have["_fetched_at"] = min(have["_fetched_at"], ev["_fetched_at"])
        books = {bm.get("key"): bm for bm in have.setdefault("bookmakers", [])}
        for bm in ev.get("bookmakers", []):
            mine = books.get(bm.get("key"))
            if mine is None:
                have["bookmakers"].append(bm)
                continue
            keys = {m.get("key") for m in mine.setdefault("markets", [])}
            mine["markets"].extend(m for m in bm.get("markets", []) if m.get("key") not in keys)
    return out


def stamp_fetched(got):
    """Note on each game in an odds answer (a list of games, or one game's dict) when it arrived, as
    time.time(): an alert isn't posted on odds that are too old by then (see Alerter.handle). Returns it."""
    at = time.time()
    for ev in (got if isinstance(got, list) else [got]):
        if isinstance(ev, dict):
            ev["_fetched_at"] = at
    return got


def fetched_at(events: list[dict]) -> dict[str, float]:
    """When each game's odds were fetched, {event id: time.time()}, for Alerter.handle. Games with no time
    (the demo, tests) aren't in it, so their alerts are never held for old odds."""
    return {ev["id"]: ev["_fetched_at"] for ev in events if ev.get("_fetched_at") and ev.get("id")}


class Scheduler:
    """Decides which sports to fetch when, spending credits only where games are on."""

    def __init__(self, cfg: Config, api: OddsAPI):
        self.cfg = cfg
        self.api = api
        self.cost = cfg.credits_per_call()
        # LIVE_MARKETS narrower than MARKETS: (live check's bet types, pre-game check's), else None.
        self.split = cfg.split_markets()
        self.games: dict[str, list[tuple[str, datetime]]] = {s: [] for s in cfg.sports}
        self.events_at: dict[str, float] = {s: 0.0 for s in cfg.sports}
        self.last_odds: dict[str, float] = {s: 0.0 for s in cfg.sports}   # "all" and "pre" checks
        self.last_live: dict[str, float] = {s: 0.0 for s in cfg.sports}   # "live" checks (LIVE_MARKETS)
        self.market_ok: dict[str, dict[str, float]] = {s: {} for s in cfg.sports}  # bet type -> last good look
        self.misses: dict[str, int] = {}
        self.pre_missed: set[str] = set()   # live games the live check missed that the pre-game check missed too
        self.ended: set[str] = set()
        self.need_close: set[str] = set()  # event ids wanting a last pre-kickoff check (CLV)
        self.need_close_props: set[str] = set()  # the same, for games with a logged prop bet
        self.last_props: dict[str, float] = {}  # event id -> last prop check (worked or not)
        self.props_ok: dict[str, float] = {}    # event id -> last prop check that worked
        self.odds_ok: dict[str, float] = {s: 0.0 for s in cfg.sports}  # sport -> last good odds check
        self.bad_prop_sports: set[str] = set()  # sports whose prop request the API rejected
        self.shed: set[str] = set()   # SHED_SPORTS left out right now: credits are tight (update_budget)
        self.bad_near_sports: set[str] = set()  # ...and whose near-kickoff extras (PROP_NEAR_MARKETS) it rejected
        self.bad_prop_markets: dict[str, set[str]] = {}   # sport -> PROP_MARKETS types the API rejected (until restart)
        self.notices: list[str] = []            # messages for the health channel (run() sends them)
        self.failures: dict[str, str] = {}      # sport -> why its last failed request failed (run() reads, clears)
        self.props_answered: set[str] = set()   # sports a prop request got a "no such game" (404) from: the API is up
        self.scale = 1.0         # slow-down for live and near-kickoff checks
        self.extra_scale = 1.0   # slow-down for early/far-out checks (stretched first)
        self.side_scale = 1.0    # early props and 1-2 days out: slowed this much more, before the rest
        self.live_scale = 1.0    # extra slow-down for live checks alone (LIVE_MAX_STRETCH: before the rest)
        self.speed: dict[str, float] = {}   # tier -> how much faster than normal (spare credits)
        self.spare = 0.0         # credits the faster pre-game pace plans to spend in the next 24h
        self.near_extras = True  # near-kickoff prop checks ask for PROP_NEAR_MARKETS too: only when the day's
                                 # credits cover them after the live and main-line checks (update_budget)
        self.demand: dict[str, float] = {}  # tier -> credits in the next 24h at full speed
        self.forecast = 0.0      # credits the next 24h would cost at full speed
        self.allowance = 0.0     # credits we can afford per day
        self.budget_at = 0.0
        self.overrun = 1.0       # credits going this much faster than the call costs say (no header)
        self._spend_mark: tuple[float, float, float] | None = None   # (remaining, expected, unmeasured) at start
        self.capped_since: float | None = None   # live checks held at LIVE_MAX_STRETCH (with a game on) since
        self.cap_noted = None                   # the local day that was last said in the log
        self.scope: Scope | None = None   # what the last fetch looked at (LIVE_MARKETS split only)
        self._due_lanes: dict[str, list[tuple[str, str]]] = {}

    # ---- game state

    def wants_live(self, sport: str) -> bool:
        """Is anything live wanted from this sport? (If not, games in progress aren't paid for.)"""
        cfg = self.cfg
        return bool(((cfg.arbs_enabled and cfg.arb_live) or (cfg.ev_enabled and cfg.ev_live)
                     or (cfg.outliers_enabled and cfg.outlier_live))
                    and (not cfg.live_sports or sport in cfg.live_sports))

    def live_games(self, sport: str, now: datetime) -> list[str]:
        dur = timedelta(minutes=self.cfg.minutes_for(sport))
        return [gid for gid, start in self.games[sport] if gid not in self.ended and start <= now < start + dur]

    def state(self, sport: str, now: datetime) -> str | None:
        """The most urgent reason to check this sport: a live game, then the soonest kickoff."""
        if self.wants_live(sport) and self.live_games(sport, now):
            return LIVE
        return self.pre_state(sport, now)

    def lanes(self, sport: str, now: datetime) -> list[tuple[str, str]]:
        """The checks this sport wants now, as (check, tier). One "all" check asks for every bet type
        of every game, live and upcoming. With LIVE_MARKETS, a sport with a live game gets a "live"
        check (LIVE_MARKETS, every game: upcoming games' moneylines come along for the same credit)
        and a "pre" check (the other bet types, games that haven't started), each on its own clock.
        The "pre" check runs every PREGAME_WITH_LIVE_EVERY live checks (PRE_LIVE), unless the normal
        pre-game pace of its next game is faster. A sport left out for credits (SHED_SPORTS): none."""
        if sport in self.shed:
            return []
        if self.split and self.wants_live(sport) and self.live_games(sport, now):
            pre = self.pre_state(sport, now)
            if pre and self.cfg.pregame_with_live_every and self.base_interval(PRE_LIVE) < self.base_interval(pre):
                pre = PRE_LIVE
            return [("live", LIVE)] + ([("pre", pre)] if pre else [])
        st = self.state(sport, now)
        return [("all", st)] if st else []

    def lane_markets(self, lane: str) -> str:
        if self.split and lane in ("live", "pre"):
            return self.split[0 if lane == "live" else 1]
        return self.cfg.markets

    def pre_state(self, sport: str, now: datetime) -> str | None:
        """How soon the next game that hasn't started is: PREGAME, EARLY, FAR or None."""
        cfg = self.cfg
        soonest = min((start for gid, start in self.games[sport] if gid not in self.ended and start > now),
                      default=None)
        if soonest is None or cfg.live_only:   # LIVE_ONLY: pre-game checks would be thrown away
            return None
        ahead = soonest - now
        if cfg.pregame_minutes and ahead <= timedelta(hours=cfg.pregame_hours):
            return PREGAME
        if cfg.lookahead_hours and cfg.early_minutes and ahead <= timedelta(hours=min(24, cfg.lookahead_hours)):
            return EARLY
        if cfg.lookahead_hours and cfg.far_minutes and ahead <= timedelta(hours=cfg.lookahead_hours):
            return FAR
        return None

    def base_interval(self, st: str) -> float:
        return {LIVE: self.cfg.poll_seconds, PREGAME: self.cfg.pregame_minutes * 60,
                PRE_LIVE: self.cfg.poll_seconds * self.cfg.pregame_with_live_every,
                EARLY: self.cfg.early_minutes * 60, FAR: self.cfg.far_minutes * 60}[st]

    def interval(self, st: str) -> float:
        if st == LIVE:   # slowed, never sped up: live checks are never faster than POLL_SECONDS
            return self.base_interval(st) * self.scale * self.live_scale
        # (PRE_LIVE keeps its pace when live checks alone slow down: live_scale isn't applied.)
        slow = self.scale if st in CORE else self.extra_scale * (self.side_scale if st == FAR else 1.0)
        return self.base_interval(st) * slow / self.speed.get(st, 1.0)

    def call_cost(self, kind: str, markets: str) -> float:
        """Credits one call of this kind costs: the measured average (x-requests-last), or markets x
        regions until there are calls to go on."""
        formula = self.cfg.credits_per_call(markets)
        book = getattr(self.api, "costs", None)   # (test fakes have none: the formula)
        return book.estimate(kind, formula) if book else formula

    def lane_cost(self, lane: str) -> float:
        return self.call_cost(LANE_KINDS[lane], self.lane_markets(lane))

    def prop_cost(self, sport: str, hours: float, today: bool = False) -> float:
        """Credits one prop check of a game this many hours away costs (measured per hours-to-start
        bucket). For today's games it's never put below the near-kickoff cost: books post the day's
        props by game-day morning, so overnight's free empty answers say little about the afternoon.
        Near kickoff the request carries PROP_NEAR_MARKETS too (until the API rejects them): see near_costs."""
        if hours <= self.cfg.prop_hours:
            return sum(self.near_costs(sport))
        formula = self.prop_credits(sport)
        book = getattr(self.api, "costs", None)
        if not book:
            return formula
        est = book.estimate(prop_cost_kind(sport, hours, self.cfg.prop_hours), formula)
        # (That floor is for the same prop types: an earlier check doesn't ask for PROP_NEAR_MARKETS.)
        return max(est, self.near_costs(sport)[0]) if today else est

    def prop_credits(self, sport: str, near: bool = False) -> int:
        """The formula's credits for one game's prop request now (prop types x regions): what prop_ask asks
        for, so a prop type the API rejected (bad_prop_markets) isn't counted."""
        return len(_csv(self.prop_ask(sport, near))) * self.cfg.regions_billed()

    def near_costs(self, sport: str) -> tuple[float, float]:
        """One near-kickoff prop check of a game: (the plain request's credits, what PROP_NEAR_MARKETS add on
        top; 0 once the API rejected them). Measured when it can be (each prop-type count has its own
        average), the one from the other when only one of the two requests has been made yet."""
        formula = self.prop_credits(sport)
        with_f = self.prop_credits(sport, near=sport not in self.bad_near_sports)
        book = getattr(self.api, "costs", None)
        if not book or not formula:
            return formula, with_f - formula
        kind = f"props:{sport}:near"
        if with_f == formula:
            return book.estimate(kind, formula), 0.0
        n_plain, n_with = book.calls(kind, formula), book.calls(kind, with_f)
        with_x = book.estimate(kind, with_f) if n_with or not n_plain else book.estimate(kind, formula) * with_f / formula
        plain = book.estimate(kind, formula) if n_plain or not n_with else with_x * formula / with_f
        return plain, max(0.0, with_x - plain)

    def refresh_events(self, force: bool = False) -> None:
        now = time.time()
        for sport in self.cfg.sports:
            if not force and now - self.events_at[sport] < self.cfg.events_refresh_minutes * 60:
                continue
            try:
                evs = self.api.events(sport, horizon_hours=max(26, self.cfg.lookahead_hours + 2))
                games = [(ev["id"], _parse_time(ev["commence_time"])) for ev in evs]
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise
                print(f"! Couldn't load {sport} schedule: {e.code}", file=sys.stderr)
                continue
            except Exception as e:  # noqa: BLE001 - timeouts, drops, cut-off or odd data: keep the last good schedule
                print(f"! Couldn't load {sport} schedule: {getattr(e, 'reason', e)!r:.200}", file=sys.stderr)
                continue
            self.games[sport] = games
            self.events_at[sport] = now

    def note_feed(self, sport: str, events: list[dict], now: datetime, lane: str = "all",
                  since: datetime | None = None) -> None:
        """Mark live games ended once books stop listing them (two fetches in a row). With LIVE_MARKETS
        the live check asks for moneylines only, and books sometimes pull just those for a while, so a
        game it misses is only over once the pre-game check (which then looks at that game too, see
        pre_since) doesn't list its other bet types either. (A sport with no pre-game check left: the
        live check alone decides.)"""
        listed = {ev["id"] for ev in events if ev.get("bookmakers")}
        dur = timedelta(minutes=self.cfg.minutes_for(sport))
        for gid, start in self.games[sport]:
            if not (start <= now < start + dur) or gid in self.ended:
                continue
            if gid in listed:
                self.misses.pop(gid, None)
                self.pre_missed.discard(gid)
                continue
            if lane == "pre":
                if gid in self.misses and start > since:   # (this check asked about it)
                    self.pre_missed.add(gid)
            else:
                self.misses[gid] = self.misses.get(gid, 0) + 1
            if self.misses.get(gid, 0) >= 2 and (lane == "all" or gid in self.pre_missed
                                                 or not self.pre_state(sport, now)):
                self.ended.add(gid)

    def pre_since(self, sport: str, now: datetime) -> datetime:
        """Where the pre-game check (LIVE_MARKETS) starts: games that haven't started, plus any live
        game the live check missed last time, to see whether its spreads/totals are still up (the
        moneyline pulled for a while) or not (the game is over). Same credits however many games
        come back; those started games only count for that, never for alerts."""
        live = set(self.live_games(sport, now))
        missed = [start for gid, start in self.games[sport] if gid in live and gid in self.misses]
        return min(missed) - timedelta(minutes=1) if missed else now

    # ---- budget

    def update_budget(self, now: datetime) -> None:
        """Pick the smallest slow-down that makes credits last until the plan resets: live checks
        first (up to LIVE_MAX_STRETCH), then early checks (up to EXTRA_MAX_STRETCH), then early props
        and 1-2 days out further (up to EXTRA_MAX_STRETCH again), then the rest (near kickoff, and
        the pre-game checks of sports with a live game, last). On a day with room to spare, spend the
        spare on faster pre-game checks (SPARE_ORDER), never on faster live ones. The extra prop types near
        kickoff (PROP_NEAR_MARKETS) are asked for only on a day that fits at full speed with them, after the
        near-kickoff speed-ups (near_extras): they never slow a live or main-line check."""
        cfg = self.cfg
        remaining = self.api.remaining if self.api.remaining is not None else cfg.monthly_credits
        reserve = 0.02 * remaining  # small cushion for forecast misses
        self.allowance = max(0.0, remaining - reserve) * self.next24_share(now)
        self.overrun = self._overrun()

        cost = {lane: self.lane_cost(lane) for lane in LANE_KINDS}
        spare_only = cfg.prop_near_spare_only   # (false: the extras are paid for like the rest of the prop check)
        was, self.shed = self.shed, set()
        demand = self._demand(now, cost)
        # SHED_SPORTS: when the next 24h doesn't fit at full speed, those sports go first (back once all of it fits
        # in 90% of the allowance, so they don't come and go every few minutes).
        shed = {sp for sp in cfg.sports if any(sp.startswith(p) for p in _csv(cfg.shed_sports))}
        need = sum(v for k, v in demand.items() if not (spare_only and k == PROP_NEAR_X))
        if shed and need > self.allowance * (0.9 if was else 1.0) * (1 + 1e-9):
            self.shed = shed
            demand = self._demand(now, cost)
        if self.shed != was:
            print(f"Budget: credits are tight, so {', '.join(short(sp) for sp in sorted(self.shed))} "
                  f"{'is' if len(self.shed) == 1 else 'are'} left out for now (SHED_SPORTS)" if self.shed
                  else f"Budget: room again, checking {', '.join(short(sp) for sp in sorted(was))} again", flush=True)
        self.demand = demand
        live = demand[LIVE]
        core = live + demand[PRE_LIVE] + demand[PREGAME] + demand[PROP_NEAR] + (0.0 if spare_only else demand[PROP_NEAR_X])
        extra = demand[EARLY] + demand[FAR] + demand[PROP_EARLY]
        self.forecast = core + extra
        a, cap = self.allowance, max(1.0, cfg.extra_max_stretch)
        fits = lambda need: need <= a * (1 + 1e-9)   # (a slow-down worked out to fit exactly still fits)
        self.live_scale, self.speed, self.spare, self.near_extras = 1.0, {}, 0.0, not spare_only
        # Only with the split: otherwise the live check also carries the sport's upcoming games, and
        # slowing it would slow their pre-game checks too.
        stretch = max(1.0, cfg.live_max_stretch or 1.0) if self.split else 1.0
        if a > 0 and not fits(core + extra) and live and stretch > 1:
            # Short: pre-game checks come first, so live checks slow down (up to LIVE_MAX_STRETCH)
            # before anything else does.
            left = live - (core + extra - a)
            self.live_scale = min(stretch, live / left) if left > 0 else stretch
            core -= live - live / self.live_scale
        side = demand[PROP_EARLY] + demand[FAR]   # early props and 1-2 days out: they give way most
        self.side_scale = 1.0
        if a <= 0:
            self.scale = self.extra_scale = math.inf
        elif fits(core + extra):
            self.scale = self.extra_scale = 1.0
            r = self.overrun   # (spare spending shrinks if credits go faster than the calls cost)
            self._spend_spare(a * cfg.spare_use_pct / 100 / r - (core + extra),
                              a / r - (core + extra) if cfg.spare_use_pct else 0.0, a / r - (core + extra))
            self.forecast += demand[PROP_NEAR_X] if self.near_extras and spare_only else 0.0
        elif fits(core + extra / cap):
            self.scale, self.extra_scale = 1.0, extra / (a - core)   # only early checks slow down
        elif fits(core + (extra - side) / cap + side / cap ** 2):
            # Early checks at their max stretch, and early props and 1-2 days out slow down further
            # (up to EXTRA_MAX_STRETCH again) before live/near-kickoff checks and the pre-game checks of
            # sports with a live game do.
            self.scale, self.extra_scale = 1.0, cap
            self.side_scale = side / (cap * (a - core) - (extra - side))
        else:
            # Early checks at their max stretch (early props and 1-2 days out at theirs); live/near-
            # kickoff get the rest, never less than half the budget (if needed, early checks stretch
            # further to make room).
            self.side_scale = cap if side else 1.0
            ext = extra - side + side / self.side_scale
            room = max(a - ext / cap, min(a / 2, core))
            self.extra_scale = (ext / (a - room) if a > room else cap) if ext else 1.0   # (none: nothing slowed)
            self.scale = max(1.0, core / room) if room > 0 else 1.0
        # What's left to pay for at extra_scale (early props and 1-2 days out counted at side_scale).
        self.core_demand, self.extra_demand = core, extra - side + side / self.side_scale
        self.budget_at = time.time()

    def _demand(self, now: datetime, cost: dict) -> dict:
        """Credits each tier would use over the next 24h at full speed, in STEP slices (SHED_SPORTS left out
        while self.shed has them)."""
        cfg = self.cfg
        demand = dict.fromkeys((LIVE, PRE_LIVE, PREGAME, EARLY, FAR, PROP_NEAR, PROP_NEAR_X, PROP_EARLY), 0.0)
        for step in range(0, 86400, STEP):
            t = now + timedelta(seconds=step)
            if seconds_until_active(cfg, t):
                continue
            for sport in cfg.sports:
                for lane, st in self.lanes(sport, t):
                    demand[st] += STEP * cost[lane] / self.base_interval(st)
            near, near_x, early = self._prop_rates(t)
            demand[PROP_NEAR] += STEP * near
            demand[PROP_NEAR_X] += STEP * near_x
            demand[PROP_EARLY] += STEP * early
        return demand

    def _overrun(self) -> float:
        """How much faster the credits left are falling than this process's calls cost, when some of
        those costs are only the formula (the API didn't send x-requests-last): 1.0 if not (or not
        clearly: under 10% faster, or under 50 credits to go on). Another process using the same key
        counts too, which is right: either way there's less to spare."""
        book = getattr(self.api, "costs", None)
        if not book or self.api.remaining is None:
            return 1.0
        mark = (float(self.api.remaining), book.expected, book.unmeasured)
        if self._spend_mark is None or mark[0] > self._spend_mark[0]:   # the first look, or the plan reset
            self._spend_mark = mark
            return 1.0
        rem0, expected0, unmeasured0 = self._spend_mark
        drop, expected, unmeasured = rem0 - mark[0], mark[1] - expected0, mark[2] - unmeasured0
        if unmeasured <= 0 or expected < 50 or drop <= expected * 1.1:
            return 1.0
        return drop / expected

    def live_cap_note(self, now: datetime) -> str:
        """A log line, once a day, when live checks have been held at their slowest (LIVE_MAX_STRETCH)
        for half an hour while a game is on, else "". The "Budget:" line says it when it changes;
        this says it has lasted, so it shows in the journal."""
        cfg = self.cfg
        stretch = max(1.0, cfg.live_max_stretch or 1.0) if self.split else 1.0
        on = any(self.wants_live(s) and self.live_games(s, now) for s in cfg.sports)
        if not (on and stretch > 1 and self.live_scale >= stretch):
            self.capped_since = None
            return ""
        ts = now.timestamp()
        self.capped_since = self.capped_since or ts
        tz = ZoneInfo(cfg.timezone)
        if ts - self.capped_since < 1800 or self.cap_noted == now.astimezone(tz).date():
            return ""
        self.cap_noted = now.astimezone(tz).date()
        since = datetime.fromtimestamp(self.capped_since, timezone.utc).astimezone(tz)
        return (f"Live checks have been at their slowest since {since:%-I:%M %p}: every {self.interval(LIVE):.0f}s "
                f"instead of {cfg.poll_seconds}s (LIVE_MAX_STRETCH={stretch:g}), so the credits last until the plan "
                f"resets and pre-game checks keep their pace. Until that eases, live alerts work from older prices "
                f"and take longer to confirm. (Said once a day.)")

    def _spend_spare(self, budget: float, room: float = 0.0, whole: float = 0.0) -> None:
        """Credits the day can spare go to faster pre-game checks, in SPARE_ORDER, each tier down to
        its floor (*_MIN_MINUTES) before the next one gets any. First, the pre-game checks of sports
        with a live game, down to every live check: they may use the day's whole share (room, not
        just SPARE_USE_PCT of it), since before LIVE_MARKETS they came with every live check.
        The extra prop types near kickoff (PROP_NEAR_X) are all or nothing: on (near_extras) when what's
        left of the day's whole share at full speed (whole) after the speed-ups before them covers them."""
        cfg = self.cfg
        rates = {PRE_LIVE: (cfg.poll_seconds * cfg.pregame_with_live_every / 60, cfg.poll_seconds / 60),
                 PREGAME: (cfg.pregame_minutes, cfg.pregame_min_minutes),
                 EARLY: (cfg.early_minutes, cfg.early_min_minutes),
                 PROP_EARLY: (cfg.prop_early_minutes, cfg.prop_early_min_minutes),
                 PROP_NEAR: (cfg.prop_minutes, cfg.prop_min_minutes),
                 FAR: (cfg.far_minutes, cfg.far_min_minutes)}
        for tier in SPARE_ORDER:
            if tier == PROP_NEAR_X:
                d = self.demand.get(PROP_NEAR_X, 0.0)
                if not self.cfg.prop_near_spare_only:
                    continue   # (paid for with the near-kickoff prop checks: on already)
                if d <= whole - self.spare:
                    self.near_extras = True
                    budget -= d
                continue
            base, floor = rates[tier]
            d = self.demand.get(tier, 0.0)
            if tier == PROP_NEAR and self.near_extras:   # (a faster near-kickoff prop check asks for them too)
                d += self.demand.get(PROP_NEAR_X, 0.0)
            have = max(budget, room) if tier == PRE_LIVE else budget
            if not d or have <= 0 or floor <= 0 or floor >= base:
                continue
            x = min(base / floor, 1 + have / d)
            self.speed[tier] = x
            budget -= d * (x - 1)
            self.spare += d * (x - 1)

    def _prop_slots(self, now: datetime) -> list[tuple[str, str, datetime]]:
        """(sport, event id, start) for games inside a props window."""
        cfg = self.cfg
        if not cfg.props_enabled or cfg.live_only:   # props are pre-game only
            return []
        early = timedelta(hours=max(cfg.prop_hours, cfg.prop_early_hours if cfg.prop_early_minutes else 0))
        return [(sport, gid, start) for sport in cfg.prop_sports if sport in self.games
                and cfg.prop_markets.get(sport) and sport not in self.bad_prop_sports
                for gid, start in self.games[sport] if now < start <= now + early]

    def _prop_tiers(self, now: datetime) -> list[tuple[str, str, bool]]:
        """(sport, event id, near_kickoff) for games inside a props window."""
        near = timedelta(hours=self.cfg.prop_hours)
        return [(sport, gid, start - now <= near) for sport, gid, start in self._prop_slots(now)]

    def _prop_games(self, now: datetime) -> list[tuple[str, str]]:
        return [(sport, gid) for sport, gid, _ in self._prop_tiers(now)]

    def _prop_every(self, near: bool) -> float:
        if near:
            return self.cfg.prop_minutes * 60 * self.scale / self.speed.get(PROP_NEAR, 1.0)
        return self.cfg.prop_early_minutes * 60 * self.extra_scale * self.side_scale / self.speed.get(PROP_EARLY, 1.0)

    def _prop_rates(self, t: datetime) -> tuple[float, float, float]:
        """Credits per second on props at time t at full speed: (near kickoff, what PROP_NEAR_MARKETS add to
        those, early)."""
        near = near_x = early = 0.0
        tz = ZoneInfo(self.cfg.timezone)
        day = t.astimezone(tz).date()
        for sport, _, start in self._prop_slots(t):
            hours = (start - t).total_seconds() / 3600
            if start - t <= timedelta(hours=self.cfg.prop_hours):
                plain, extra = self.near_costs(sport)
                near += plain / (self.cfg.prop_minutes * 60)
                near_x += extra / (self.cfg.prop_minutes * 60)
            else:
                early += self.prop_cost(sport, hours, start.astimezone(tz).date() == day) / (self.cfg.prop_early_minutes * 60)
        return near, near_x, early

    def props_due(self, now: datetime) -> list[tuple[str, str]]:
        """The games whose props are due now, as (sport, event id). PROP_MAX_PER_PASS: at most that many, the
        closing-line checks first, then the most overdue (time since the last check over the game's pace); the
        rest stay due for the next pass."""
        ts = time.time()
        tiers = self._prop_tiers(now)
        out = [(sport, gid) for sport, gid, near in tiers
               if ts - self.last_props.get(gid, 0) >= self._prop_every(near)]
        cfg = self.cfg
        window = timedelta(minutes=cfg.closing_minutes)

        def closing_look(gid: str, start: datetime) -> bool:   # (a logged prop bet's game, no good look yet)
            return (gid in self.need_close_props and now < start <= now + window
                    and self.props_ok.get(gid, 0) < (start - window).timestamp())
        if cfg.closing_minutes and self.need_close_props and cfg.props_enabled and not cfg.live_only:
            # One last prop check just before kickoff for games with a logged prop bet (CLV).
            have = {gid for _, gid in out}
            out += [(sport, gid) for sport in cfg.prop_sports
                    if sport in self.games and cfg.prop_markets.get(sport) and sport not in self.bad_prop_sports
                    for gid, start in self.games[sport]
                    if gid not in have and closing_look(gid, start)
                    and ts - self.last_props.get(gid, 0) >= 60]                     # retry a failure each minute
        cap = cfg.prop_max_per_pass
        if cap > 0 and len(out) > cap:
            # The closing-line checks first (also a game due anyway), then the most overdue.
            near = {(sport, gid): n for sport, gid, n in tiers}
            starts = {(sport, gid): start for sport in {s for s, _ in out} for gid, start in self.games.get(sport, [])}
            first = {item for item in out if cfg.closing_minutes and item in starts
                     and closing_look(item[1], starts[item])}

            def overdue(item) -> float:
                return (ts - self.last_props.get(item[1], 0)) / max(self._prop_every(near.get(item, True)), 1e-9)
            out = sorted(out, key=lambda item: (item not in first, -overdue(item)))[:cap]   # (stable: ties keep order)
        return out

    def prop_ask(self, sport: str, near: bool = False) -> str:
        """The prop types one game's request asks for now: PROP_MARKETS (plus PROP_NEAR_MARKETS near kickoff),
        without the ones the API rejected (bad_prop_markets)."""
        bad = self.bad_prop_markets.get(sport, set())
        return ",".join(m for m in _csv(self.cfg.prop_request(sport, near)) if m not in bad)

    def fetch_props(self, games: list[tuple[str, str]], now: datetime | None = None) -> tuple[list[dict], set[str]]:
        """Prop odds per game, plus the ids of games whose request worked (a failed request
        must not count as "looked and the bet is gone"). A game within PROP_HOURS of kickoff also
        asks for PROP_NEAR_MARKETS while the day's credits cover them (near_extras, see update_budget);
        if the API rejects that request (422), the game is asked again
        without them and the sport stops asking for them until a restart (said once, here and in
        the health channel). A rejected plain request: the prop types the API turned down are found
        (named in its answer, else each asked alone once) and only those stop until a restart; the sport's
        props stop only when it turns down every one (then it's PROP_MARKETS that's wrong, and that's what's said)."""
        cfg = self.cfg
        now = now or datetime.now(timezone.utc)
        starts = {gid: start for sport in {s for s, _ in games} for gid, start in self.games.get(sport, [])}

        def one(item):
            sport, gid = item
            plain = self.prop_ask(sport)
            near = (sport not in self.bad_near_sports and self.near_extras and gid in starts
                    and starts[gid] - now <= timedelta(hours=cfg.prop_hours))
            ask = self.prop_ask(sport, near)
            rejected = False   # the near-kickoff extras were rejected (the answer is the plain request's)
            try:
                if ask != plain:
                    try:
                        return item, stamp_fetched(self.api.event_odds(sport, gid, ask)), False
                    except urllib.error.HTTPError as e:
                        if e.code != 422:
                            raise
                        rejected = True
                return item, stamp_fetched(self.api.event_odds(sport, gid, plain)), rejected
            except Exception as e:  # noqa: BLE001 - reported below
                return item, e, rejected

        events: list[dict] = []
        ok: set[str] = set()
        refused: dict[str, list[tuple[str, Exception]]] = {}   # sport -> its games whose plain request got a 422

        def take(sport, gid, result):
            if isinstance(result, dict):
                ok.add(gid)   # an answer with no books is still a real "nothing there"
                self.props_ok[gid] = time.time()
                if result.get("bookmakers"):
                    events.append(apply_fees([result], self.cfg)[0])

        with ThreadPoolExecutor(max_workers=min(8, len(games))) as pool:
            for (sport, gid), result, rejected in pool.map(one, games):
                self.last_props[gid] = time.time()
                plain_rejected = isinstance(result, urllib.error.HTTPError) and result.code == 422
                if rejected and not plain_rejected and sport not in self.bad_near_sports:
                    self.bad_near_sports.add(sport)
                    extra = ", ".join(cfg.prop_near_extra(sport))
                    print(f"! The extra {sport} props near kickoff ({extra}) were rejected (422): check "
                          f"PROP_NEAR_MARKETS. Asking without them until restart; its other props carry on.",
                          file=sys.stderr)
                    self.notices.append(f"⚠️ The Odds API rejected the extra {short(sport)} prop types ({extra}): "
                                        f"check PROP_NEAR_MARKETS. Regular {short(sport)} props continue.")
                if isinstance(result, urllib.error.HTTPError) and result.code == 404:
                    self.props_answered.add(sport)   # (that game is gone, e.g. an old id: not an outage)
                elif isinstance(result, Exception):
                    self.failures[sport] = fail_reason(result)
                if isinstance(result, urllib.error.HTTPError):
                    if result.code in (401, 429):
                        raise result
                    if plain_rejected:
                        if sport not in self.bad_prop_sports:
                            refused.setdefault(sport, []).append((gid, result))
                    else:
                        print(f"! Props error for {sport} {gid}: {result.code}", file=sys.stderr)
                elif isinstance(result, Exception) or not isinstance(result, dict):
                    print(f"! Props error for {sport} {gid}: {result!r:.200}", file=sys.stderr)
                else:
                    take(sport, gid, result)
        for sport, refused_games in refused.items():
            for gid, result in self._after_422(sport, refused_games):
                take(sport, gid, result)
        return events, ok

    def _after_422(self, sport: str, refused: list[tuple[str, Exception]]) -> list[tuple[str, dict]]:
        """The API rejected a sport's plain prop request (422): find which prop types it turned down, stop asking
        for just those (until restart, said once), and ask those games again without them. Answers to use
        [(game id, answer)]. The types are the ones its answer names, else each is asked alone for the first game
        (a rejected request costs nothing, an accepted one what it would have cost in the full request, and those
        answers are that game's). Every type rejected: the sport's props stop, as before."""
        plain = _csv(self.prop_ask(sport))
        text = " ".join(str(getattr(e, "odds_message", "") or "") for _, e in refused)
        named = [m for m in plain if re.search(rf"(?<![A-Za-z0-9_]){re.escape(m)}(?![A-Za-z0-9_])", text)]
        bad: set[str] = set(named) if 0 < len(named) < len(plain) else set()
        first: dict | None = None
        if not bad:
            gid0, parts = refused[0][0], []
            for m in plain:
                try:
                    parts.append(stamp_fetched(self.api.event_odds(sport, gid0, m)))
                except urllib.error.HTTPError as e:
                    if e.code == 422:
                        bad.add(m)
                        continue
                    if e.code in (401, 429):
                        raise
                    return []   # (anything else isn't a verdict: those games are tried again when next due)
                except Exception:  # noqa: BLE001 - the same
                    return []
            if parts:
                first = merge_events([json.loads(json.dumps(x)) for x in parts])[0]
        if not bad or set(plain) <= bad:
            if bad:   # every one turned down: it's PROP_MARKETS that's wrong
                self.bad_prop_sports.add(sport)
                print(f"! Props for {sport} rejected (422): check PROP_MARKETS for it. "
                      f"Skipping {sport} props until restart.", file=sys.stderr)
                self.notices.append(f"⚠️ The Odds API rejected the {short(sport)} prop request: check "
                                    f"PROP_MARKETS. No {short(sport)} props until a restart.")
            else:
                print(f"! Props for {sport} rejected (422), but every prop type alone was accepted.", file=sys.stderr)
            return []
        self.bad_prop_markets.setdefault(sport, set()).update(bad)
        names = ", ".join(m for m in plain if m in bad)
        print(f"! {sport} prop types rejected (422): {names}. Asking without them until restart.", file=sys.stderr)
        self.notices.append(f"⚠️ The Odds API rejected these {short(sport)} prop types: {names} (check PROP_MARKETS). "
                            f"The other {short(sport)} props carry on.")
        out: list[tuple[str, dict]] = []
        ask = self.prop_ask(sport)
        for gid, _ in refused:
            if gid == refused[0][0] and first is not None:
                out.append((gid, first))
                continue
            try:
                out.append((gid, stamp_fetched(self.api.event_odds(sport, gid, ask))))
            except urllib.error.HTTPError as e:
                if e.code in (401, 429):
                    raise
            except Exception:  # noqa: BLE001 - tried again when next due
                pass
        return out

    def day_weight(self, t: datetime) -> float:
        day = WEEKDAYS[t.astimezone(ZoneInfo(self.cfg.timezone)).weekday()]
        return max(0.0, self.cfg.budget_weights.get(day, 1.0))

    def next24_share(self, now: datetime) -> float:
        """The part of the remaining credits the next 24h may use: its share of the weighted
        hours left until the plan resets (weekends weigh more, quiet weekdays less)."""
        end = next_reset(self.cfg, now)
        hours = int((end - now).total_seconds() // 3600)
        if hours <= 24:
            return 1.0
        weights = [self.day_weight(now + timedelta(hours=h)) for h in range(hours)]
        total = sum(weights)
        return sum(weights[:24]) / total if total else 24 / hours

    # ---- polling

    def _last(self, sport: str, lane: str) -> float:
        return self.last_live[sport] if lane == "live" else self.last_odds[sport]

    def closing_lanes(self, sport: str, now: datetime) -> list[str]:
        """The checks a game with a logged bet still needs before it starts (within CLOSING_MINUTES,
        no good look at its bet types since): its closing line needs every bet type."""
        cfg = self.cfg
        if not cfg.closing_minutes or not self.need_close or cfg.live_only:
            return []
        window = timedelta(minutes=cfg.closing_minutes)
        opens = [(start - window).timestamp() for gid, start in self.games[sport]
                 if gid in self.need_close and now < start <= now + window]
        if not opens:
            return []
        ts = time.time()   # (a failed look is retried each minute)
        if self.split and self.wants_live(sport) and self.live_games(sport, now):
            out = []
            for lane in ("live", "pre"):
                ok = min(self.market_ok[sport].get(m, 0.0) for m in _csv(self.lane_markets(lane)))
                if ts - self._last(sport, lane) >= 60 and any(ok < t for t in opens):
                    out.append(lane)
            return out
        return ["all"] if ts - self.last_odds[sport] >= 60 and any(self.odds_ok[sport] < t for t in opens) else []

    def closing_due(self, sport: str, now: datetime) -> bool:
        """A game with a logged bet starts within CLOSING_MINUTES and we haven't looked since."""
        return bool(self.closing_lanes(sport, now))

    def due(self, now: datetime) -> list[str]:
        ts = time.time()
        self._due_lanes = {}
        for sport in self.cfg.sports:
            lanes = [(lane, st) for lane, st in self.lanes(sport, now) if ts - self._last(sport, lane) >= self.interval(st)]
            for lane in self.closing_lanes(sport, now):
                if all(lane != have for have, _ in lanes):
                    lanes.append((lane, PREGAME))
            if lanes:
                self._due_lanes[sport] = lanes
        return list(self._due_lanes)

    def seconds_to_next(self, now: datetime) -> float:
        ts = time.time()
        waits = [self._last(s, lane) + self.interval(st) - ts for s in self.cfg.sports for lane, st in self.lanes(s, now)]
        waits += [self.last_props.get(gid, 0) + self._prop_every(near) - ts
                  for _, gid, near in self._prop_tiers(now)]
        return min([15.0] + waits)

    def fetch(self, sports: list[str], now: datetime) -> tuple[list[dict], set[str]]:
        """Odds for these sports, plus the sports whose request worked. With LIVE_MARKETS split
        off, self.scope says which bet types of which games were looked at."""
        cfg = self.cfg
        ahead = max(cfg.pregame_hours if cfg.pregame_minutes else 0, cfg.lookahead_hours)
        until = now + timedelta(hours=ahead, minutes=1)
        due, self._due_lanes = self._due_lanes, {}
        jobs = [(sport, lane) for sport in sports
                for lane, _ in (due.get(sport) or self.lanes(sport, now) or [("all", None)])]
        since = {sport: self.pre_since(sport, now) for sport, lane in jobs if lane == "pre"}

        def one(job: tuple[str, str]) -> tuple[tuple[str, str], list[dict] | Exception]:
            sport, lane = job
            try:
                if lane == "live":   # LIVE_MARKETS for every game: live ones, and upcoming ones for free
                    got = self.api.odds(sport, until, markets=self.lane_markets(lane), kind=LANE_KINDS[lane])
                elif lane == "pre":    # the other bet types, games that haven't started
                    got = self.api.odds(sport, until, markets=self.lane_markets(lane), since=since[sport],
                                        kind=LANE_KINDS[lane])
                else:
                    got = self.api.odds(sport, until)
                return job, stamp_fetched(got)
            except Exception as e:  # noqa: BLE001 - reported below
                return job, e

        events: list[dict] = []
        ok: set[str] = set()
        self.scope = scope = Scope() if self.split else None
        with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
            for (sport, lane), result in pool.map(one, jobs):
                ts = time.time()
                if lane == "live":
                    self.last_live[sport] = ts
                else:
                    self.last_odds[sport] = ts
                if isinstance(result, Exception):
                    self.failures[sport] = fail_reason(result)
                if isinstance(result, urllib.error.HTTPError):
                    if result.code in (401, 429):
                        raise result
                    print(f"! Odds error for {sport}: {result.code}", file=sys.stderr)
                elif isinstance(result, Exception) or not isinstance(result, list):
                    print(f"! Odds error for {sport}: {result!r:.200}", file=sys.stderr)
                else:
                    ok.add(sport)
                    apply_fees(result, cfg)
                    self.note_feed(sport, result, now, lane, since.get(sport))
                    if lane == "pre" and since[sport] < now:   # (live games it looked at for note_feed only)
                        result = [ev for ev in result if _parse_time(ev["commence_time"]) > now]
                    markets = self.lane_markets(lane)
                    for m in _csv(markets):
                        self.market_ok[sport][m] = ts
                    self.odds_ok[sport] = min(self.market_ok[sport].get(m, 0.0) for m in _csv(cfg.markets))
                    if scope is not None:
                        if lane == "all":
                            scope.sports.add(sport)
                        elif lane == "live":
                            scope.add(sport, markets, None, until)
                            # Games under way: only LIVE_MARKETS is checked live, so an alert on any
                            # other bet type of a started game is gone (its line came down at kickoff).
                            scope.add(sport, cfg.markets, None, now)
                        else:
                            scope.add(sport, markets, now, until)
                    events.extend(result)
        if scope is not None:
            # (Only what this pass's checks asked for counts as looked at. A sport with no live check
            # now, e.g. a game running past GAME_MINUTES, has its started games seen by its own next
            # "all" check, which asks for every game: a pass for another sport never closes them.)
            events = merge_events(events)
        return events, ok


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
The bot only sends strong ones (locks mode), so every alert is worth a look. Games that haven't started come first; live alerts are kept to a few an hour.

💰 **ARB** (green, or red if the game is live)
Bet **every** side shown, each at the book listed, using the exact amounts. You profit no matter who wins.
• If a bet says **Bet this one first**, place it first: it's the price that will move. Then the other right away.
• No "first" note? Place the bets back to back.
• If a price has moved past its "skip" line, don't place the other bet.
• If the other price is gone after you placed the first, the card says when the first is still worth keeping on its own ("keep this one").

📈 **+EV** (blue)
Bet **one** side at the book shown, for the amount shown. It's a better price than it should be, but it won't win every time. It pays off over many bets.
Each one has a **confidence**: 🟢 High, 🟡 Medium or 🟠 Low. Lower confidence already means a smaller stake. When unsure, skip the 🟠 ones.
✅✅ **Two sharp books agree** means Pinnacle and Kalshi (or Pinnacle and 4+ other books) give almost the same chance to win, so a smaller edge (from 3.5%) is still a lock. Bet it like any other: the stake is already a bit smaller.

🚨 **OUTLIER** (orange, pings everyone)
One book's price is way off from all the others. Bet it fast, before they fix it.
Optional: also place the 🔒 bets it lists to lock in a guaranteed profit.

📦 **PARLAY** (purple)
One ticket with 2-3 +EV bets from different games, all at the same book. Every leg must win. Bigger payout, wins less often, so the stake is small.

🏦 **Kalshi** prices in alerts already include Kalshi's trading fee. If Kalshi doesn't have enough on offer at that price for the whole stake, the card says so (a +EV or outlier stake is cut to what's there). On an NFL moneyline the card also notes that Kalshi settles a tie by its own rules (sportsbooks refund one).

⏱ **Price age** on a card: how long the book had shown that price when the alert went out. A live alert only goes out once two checks in a row (about a minute apart) find it and the price is under a minute old, so it's less likely to be gone when you tap. Before a game, a price can sit unchanged for hours: that's normal. Player props don't show one: the book doesn't say when each player's line last moved.

🎯 **Player props** show up as normal +EV, arb or outlier alerts, e.g. "LeBron James Over 25.5 Points". When Pinnacle doesn't price a prop, its true odds come from the other books: the card names them, and the stake is 30% smaller. If its card turns 🟡 later, the edge is still there, just less certain.

❌ **GONE** (grey)
The chance is over. Ignore it.

📋 **RESULTS** (in the results channel)
As games finish, the bot posts what hit: ✅ won, ❌ lost, ➖ push, with the profit at the stake shown, and the day's record so far. Props are graded from the box score too. 🎯 means check that one yourself. The pinned 📊 Scoreboard keeps the running record; its 📏 part shows whether alert prices held up a few minutes later. Every Monday morning a 📋 **Weekly report card** sums up the last 7 days for each alert type, with suggestions; it never changes a setting, that's up to you.

**Every time**
1. Tap the book name to open it. Check the price matches the alert, or is better.
2. Worse than the "skip" price? Don't bet.
3. Bet the exact amount shown (the "1.5u" next to it is the same amount in units). Don't go bigger.
4. 🟢 in "Every book" means that book's price is also good to bet.
5. Books sometimes cancel bets on obvious mistakes and refund the money. With an arb, the other bet still stands, so you're left with one normal bet."""


def spent_line(spent: dict[str, float]) -> str:
    """'Used since the last summary: 410 on live checks, ...' from CostBook.take_today()."""
    groups = {"live checks": 0.0, "pre-game checks": 0.0, "props": 0.0, "scores": 0.0}
    for kind, n in spent.items():
        group = ("live checks" if kind == "odds:live" else "props" if kind.startswith("props:")
                 else "scores" if kind == "scores" else "pre-game checks" if kind == "odds:pre" else "main-line checks")
        groups[group] = groups.get(group, 0.0) + n
    parts = [f"{n:,.0f} on {g}" for g, n in groups.items() if n]
    return f"Used since the last summary: {', '.join(parts)}" if parts else ""


def summary_payload(cfg: Config, sections: list[tuple[str, str]], credits_left: float | None,
                    spent: dict[str, float] | None = None) -> dict:
    """The daily summary card: what was found, whether the bets are good (CLV), credits (and
    where they went, from the measured cost of each call)."""
    lines = [f"{title}\n{body}" for title, body in sections if body]
    week, ever = clv_rows(cfg, 7), clv_rows(cfg)
    if ever:
        verdict = ("✅ Beating the closing line: the edge looks real."
                   if sum(r["clv_pct"] for r in ever) > 0 and sum(r["beat_close"] for r in ever) * 2 > len(ever)
                   else "⚠️ Not beating the closing line yet. Give it more bets before trusting it.")
        groups = clv_breakdown(cfg, min_bets=5)
        best = max((t for rows in groups.values() for t in rows), key=lambda t: t[2], default=None)
        worst = min((t for rows in groups.values() for t in rows), key=lambda t: t[2], default=None)
        detail = ""
        if best and worst and best != worst:
            detail = (f"\nBest: {best[0]} ({best[2]:+.1f}%, {best[1]} bets) · "
                      f"Worst: {worst[0]} ({worst[2]:+.1f}%, {worst[1]} bets)")
        lines.append(f"📐 **Bet quality (CLV)**\nLast 7 days: {clv_record(cfg, 7) if week else 'no closes yet'}\n"
                     f"All time: {clv_record(cfg)}\n{verdict}{detail}")
    if marks := _safely("daily summary", markout_summary, cfg):
        lines.append(marks)
    left = f"{credits_left:,.0f}" if credits_left is not None else "?"
    days = max(1.0, (next_reset(cfg, datetime.now(timezone.utc)) - datetime.now(timezone.utc)).total_seconds() / 86400)
    used = spent_line(spent or {})
    lines.append(f"💳 **Credits**\n{left} left · about {float(credits_left or 0) / days:,.0f}/day until "
                 f"{next_reset(cfg, datetime.now(timezone.utc)):%b %d}" + (f"\n{used}" if used else ""))
    return _card("📊 Daily summary (last 24h)", "\n\n".join(lines), 0x5865F2,
                 footer="Full breakdown on the server: arbbot.py --results")


def guide_payload() -> dict:
    return _card("📖 How to use these alerts", GUIDE, 0x5865F2,
                 footer="Pin this message: hover over it → ⋯ → Pin Message")


def demo_events(live_arb: bool = True) -> list[dict]:
    """Sample data with one planted arb, re-stamped as fresh so it passes the age filter.
    live_arb=False moves the arb's game to later today: in locks mode a live arb needs 5%+."""
    data = json.loads((HERE / "sample_odds.json").read_text())
    if not live_arb:
        for ev in data:
            if ev["id"] == "demo-nba-1":
                ev["commence_time"] = _iso(datetime.now(timezone.utc) + timedelta(hours=3))
    fresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for ev in data:
        for bm in ev["bookmakers"]:
            bm["last_update"] = fresh
            for m in bm["markets"]:
                m["last_update"] = fresh
    return data


def short(sport: str) -> str:
    return sport.split("_", 1)[-1].upper()


def mode_line(cfg: Config) -> str:
    if cfg.alert_mode == "locks":
        caps = ", ".join(f"{n} {what}/hour" for n, what in (
            (cfg.max_arb_per_hour, "arbs"), (cfg.max_ev_per_hour, "+EV"), (cfg.max_prop_per_hour, "props"),
            (cfg.max_outlier_per_hour, "outliers"), (cfg.max_parlay_per_hour, "parlays")) if n)
        if cfg.live_per_hour:
            caps += f"{', ' if caps else ''}{cfg.live_per_hour} live alerts/hour in all"
        rules = [f"{cfg.live_confirm_checks} checks in a row"] if cfg.live_confirm_checks > 1 else []
        rules += [f"a price under {cfg.live_max_age_alert}s old"] if cfg.live_max_age_alert else []
        live_out = max(cfg.outlier_min_pct, cfg.outlier_live_min_pct)
        pct = cfg.pregame_confirmed_ev_pct or 0
        confirmed = (f" (pre-game {pct:g}%+ when Kalshi or 4+ other books agree with Pinnacle)"
                     if 0 < pct < cfg.min_ev_pct else "")
        return (f"Alert mode: LOCKS: arbs {cfg.min_profit_pct:g}%+ (live {cfg.min_live_profit_pct:g}%+), "
                f"+EV {cfg.min_ev_pct:g}%+ {cfg.min_confidence}-confidence only{confirmed}, props {cfg.prop_min_ev_pct:g}%+"
                f"{' (Overs only)' if cfg.prop_sides == 'over' else ''}, "
                f"outliers {cfg.outlier_min_pct:g}%+ (live {live_out:g}%+), parlays {cfg.parlay_min_ev_pct:g}%+"
                + (f"; at most {caps}" if caps else "")
                + (f"; live alerts need {' and '.join(rules)}" if rules else ""))
    return (f"Alert mode: {cfg.alert_mode.upper()} (thresholds as set in .env)"
            + ("; player props: Overs only (PROP_SIDES=over)" if cfg.prop_sides == "over" else ""))


def unit_line(cfg: Config) -> str:
    """What one unit is on the bet cards ("1u = $10"), and where that comes from."""
    where = "UNIT_SIZE" if cfg.unit_size > 0 else f"1% of EV_BANKROLL {money(cfg.ev_bankroll)}"
    return f"Stakes in units: 1u = {money(cfg.unit())} ({where})"


def online_message(cfg: Config) -> str:
    """The health channel's message when the bot starts."""
    return (f"🟢 {BOT_NAME} online: watching {', '.join(short(s) for s in cfg.sports)} ({cfg.markets}). "
            f"Bet cards show stakes in units too: 1u = {money(cfg.unit())}."
            + ("" if cfg.arbs_enabled else " Arbs are off (ARBS_ENABLED=false).")
            + (f" Using {len(REMOTE_USED)} settings pushed for you ({REMOTE_ENV}); {mode_line(cfg)}"
               if REMOTE_USED else ""))


def print_plan(cfg: Config, sched: Scheduler) -> None:
    now = datetime.now(timezone.utc)
    sched.refresh_events(force=True)
    sched.update_budget(now)
    tz = ZoneInfo(cfg.timezone)
    print("\n" + mode_line(cfg))
    print("\nCredits per check (" + ("10 bookmakers count as 1 region" if cfg.bookmakers else f"regions: {cfg.regions}")
          + "):\n  " + "\n  ".join(cost_lines(cfg, sched)))
    print(f"Credits left: {sched.api.remaining if sched.api.remaining is not None else '?'} "
          f"| plan resets {next_reset(cfg, now):%b %d} (UTC)")
    live_note = ""
    if sched.split:
        live, pre = sched.split
        live_note = (f" ({live} only; {pre} of later games every {every(sched.base_interval(PRE_LIVE))})"
                     if cfg.pregame_with_live_every else f" ({live} only; {pre} at the pre-game pace)")
    print("\nNext 24h, when each sport will be checked:\n"
          f"  L = live every {cfg.poll_seconds}s{live_note} · p = near kickoff every {cfg.pregame_minutes}m · "
          f"e = upcoming every {cfg.early_minutes}m · f = 1-2 days out every {cfg.far_minutes}m")
    hours = [now + timedelta(hours=h) for h in range(24)]
    print("            " + "".join(f"{h.astimezone(tz):%H}"[0] for h in hours))
    print("            " + "".join(f"{h.astimezone(tz):%H}"[1] for h in hours))
    for sport in cfg.sports:
        row = ""
        for h in hours:
            states = {sched.state(sport, h + timedelta(minutes=m)) for m in range(0, 60, 10)}
            row += next((c for st, c in ((LIVE, "L"), (PREGAME, "p"), (EARLY, "e"), (FAR, "f"))
                         if st in states), "·")
        n = len(sched.games[sport])
        print(f"  {short(sport):<9} {row}   {n} game{'s' if n != 1 else ''}")
    if cfg.props_enabled:
        prop_games = {gid for h in range(0, 24 * 60, 10) for _, gid in sched._prop_games(now + timedelta(minutes=h))}
        print(f"\nProps: {len(prop_games)} games in the next 24h ({', '.join(short(s) for s in cfg.prop_sports)}): "
              f"every {every_long(cfg.prop_early_minutes * 60)} from {cfg.prop_early_hours:g}h out, "
              f"every {cfg.prop_minutes}m in the last {cfg.prop_hours:g}h.")
        near = [f"{short(s)} {', '.join(cfg.prop_near_extra(s))}" for s in cfg.prop_sports
                if cfg.prop_markets.get(s) and cfg.prop_near_extra(s)]
        if near:
            print(f"  In the last {cfg.prop_hours:g}h they also ask for (PROP_NEAR_MARKETS): {'; '.join(near)}.")
    day = WEEKDAYS[now.astimezone(tz).weekday()]
    w = cfg.budget_weights.get(day, 1.0)
    avg = sum(cfg.budget_weights.get(d, 1.0) for d in WEEKDAYS) / 7
    print(f"\nBudget weighting: today ({day.title()}) gets {w / avg:.1f}x an average day's credits "
          f"(BUDGET_WEIGHTS; busy days get more, quiet weekdays less).")
    print(f"\nFull speed would use {sched.forecast:,.0f} credits in the next 24h; "
          f"you can afford {sched.allowance:,.0f}/day.")
    print("\n".join(pace_lines(cfg, sched)))


SPEED_NAMES = {PRE_LIVE: "of later games in sports with a live game", PREGAME: "near kickoff",
               EARLY: "upcoming games", PROP_EARLY: "early props", PROP_NEAR: "props near kickoff", FAR: "1-2 days out"}


def every(seconds: float) -> str:
    """A check rate in words: 90s, 2m, 15m."""
    return f"{seconds:.0f}s" if seconds < 120 else f"{seconds / 60:.0f}m"


def every_long(seconds: float) -> str:
    """A slower check rate in words: hours from 2 hours (4h, 16h), else as every() (30m, 90m)."""
    return f"{seconds / 3600:.0f}h" if seconds >= 7200 else every(seconds)


def bet_types(markets: str) -> str:
    """'spreads,totals' -> 'spreads and totals' (h2h: moneylines)."""
    names = [{"h2h": "moneylines"}.get(m, m.replace("_", " ")) for m in _csv(markets)]
    return " and ".join(x for x in (", ".join(names[:-1]), names[-1] if names else "") if x)


def pace_lines(cfg: Config, sched: Scheduler) -> list[str]:
    """What the budget did to the check rates (for --plan and the "Budget:" log line): live checks
    slowed first, early checks slowed, everything slowed, or spare credits spent on pre-game."""
    if sched.scale == math.inf:
        return ["→ No credits for the next 24h: checks are paused."]
    lines = []
    # The pre-game checks of sports with a live game (spreads and totals of their later games, in
    # locks mode), when the next 24h has any.
    pre_live = (f"{bet_types(sched.split[1])} {SPEED_NAMES[PRE_LIVE]}"
                if sched.split and sched.demand.get(PRE_LIVE) else "")
    if sched.live_scale > 1.01 and sched.scale <= 1.01 and sched.extra_scale <= 1.01:
        lines.append(f"→ Live checks slowed first: every {sched.interval(LIVE):.0f}s instead of {cfg.poll_seconds}s "
                     f"(LIVE_MAX_STRETCH), so pre-game checks keep their pace"
                     + (f", {pre_live} included (every {every(sched.interval(PRE_LIVE))})" if pre_live else "") + ".")
    elif sched.live_scale > 1.01:
        lines.append(f"→ Live checks slowed first: every {cfg.poll_seconds * sched.live_scale:.0f}s instead of "
                     f"{cfg.poll_seconds}s, as far as LIVE_MAX_STRETCH allows. That wasn't enough:")
    if sched.extra_scale > 1.01:
        lines.append(f"→ Upcoming-game checks slowed {sched.extra_scale:.1f}× "
                     f"(every {sched.interval(EARLY) / 60:.0f}m instead of {cfg.early_minutes}m).")
    if sched.side_scale > 1.01:
        parts = ([f"early props every {every_long(sched._prop_every(False))}"] if sched.demand.get(PROP_EARLY) else []) \
            + ([f"games 1-2 days out every {every_long(sched.interval(FAR))}"] if sched.demand.get(FAR) else [])
        lines.append(f"→ Then {' and '.join(parts)} ({sched.extra_scale * sched.side_scale:.0f}× slower in all), "
                     f"before near-kickoff checks{f' and {pre_live}' if pre_live else ''} slow down.")
    if sched.scale > 1.01:
        lines.append(f"→ Live checks every {sched.interval(LIVE):.0f}s instead of {cfg.poll_seconds}s, near-kickoff "
                     f"checks every {sched.interval(PREGAME) / 60:.0f}m instead of {cfg.pregame_minutes}m"
                     + (f", {pre_live} every {every(sched.interval(PRE_LIVE))} instead of "
                        f"{every(sched.base_interval(PRE_LIVE))}" if pre_live else "") + ".")
    normal = {PRE_LIVE: sched.base_interval(PRE_LIVE), PREGAME: cfg.pregame_minutes * 60,
              EARLY: cfg.early_minutes * 60, PROP_EARLY: cfg.prop_early_minutes * 60,
              PROP_NEAR: cfg.prop_minutes * 60, FAR: cfg.far_minutes * 60}
    faster = [f"{pre_live if t == PRE_LIVE else name} every {every(normal[t] / sched.speed[t])} "
              f"(from {every(normal[t])})" for t, name in SPEED_NAMES.items() if sched.speed.get(t, 1.0) > 1.01]
    if faster:
        lines.append(f"→ Spare credits (~{sched.spare:,.0f} in the next 24h) buy faster pre-game checks: "
                     f"{', '.join(faster)}. Live checks never go faster than {cfg.poll_seconds}s.")
    if sched.demand.get(PROP_NEAR_X) and not sched.near_extras:
        lines.append("→ The extra prop types near kickoff (PROP_NEAR_MARKETS) wait: the next 24h's credits don't "
                     "cover them after live and near-kickoff checks at full speed. The usual props carry on.")
    if sched.overrun > 1.05 and max(sched.scale, sched.extra_scale, sched.live_scale) <= 1.01:
        lines.append(f"→ Credits are going {sched.overrun:.1f}× as fast as the checks should cost (the API doesn't "
                     f"say what each one cost), so less goes to faster pre-game checks.")
    return lines or [f"→ Fits the budget at full speed (live checks every {cfg.poll_seconds}s)."]


def pace_state(sched: Scheduler) -> tuple:
    """The check rates update_budget sets: (scale, extra_scale, live_scale, side_scale, near-kickoff prop
    extras on (1) or not (0), speed)."""
    return (sched.scale, sched.extra_scale, sched.live_scale, sched.side_scale, float(sched.near_extras),
            dict(sched.speed))


def pace_changed(old: tuple, sched: Scheduler) -> bool:
    """Did update_budget change any check rate noticeably (worth a "Budget:" log line)? old =
    pace_state() before it. A partly funded speed-up drifts a little every few minutes as the
    forecast moves on, so speed-ups count only when they change by 10% or more."""
    *slow, speed = old
    *cur, cur_speed = pace_state(sched)
    if any(abs(a - b) > 0.05 for a, b in zip(slow, cur) if math.isfinite(a) or math.isfinite(b)):
        return True
    return any(abs(speed.get(k, 1.0) / cur_speed.get(k, 1.0) - 1) >= 0.1 for k in set(speed) | set(cur_speed))


class CreditsShort:
    """The health channel hears it (once a local day) when the budget has kept live and near-kickoff checks 3x
    or more slower than set for 30 minutes in a row: these settings need more credits than the plan has. The
    safety net if settings made for a bigger plan ever run on a smaller one (today's settings on the 100K plan
    reach about 2.4x at the busiest)."""
    SLOW = 3.0     # live x near-kickoff slow-down (Scheduler.scale x live_scale)
    MINUTES = 30   # ...this long in a row

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.since: datetime | None = None   # when it got (and stayed) this slow
        self.said: date | None = None        # the local day it was last said

    def check(self, sched: Scheduler, now: datetime, remaining: float | None) -> str:
        """After update_budget: the message to send now, or ""."""
        slow = sched.scale * sched.live_scale
        if not math.isfinite(slow) or slow < self.SLOW:   # (out of credits altogether is said elsewhere)
            self.since = None
            return ""
        self.since = self.since or now
        day = now.astimezone(ZoneInfo(self.cfg.timezone)).date()
        if now - self.since < timedelta(minutes=self.MINUTES) or self.said == day:
            return ""
        self.said = day
        near = f" and near-kickoff checks {sched.scale:.0f}x slower" if sched.scale >= 1.5 else ""
        left = f"{remaining:,.0f}" if remaining is not None else "unknown"
        return (f"⚠️ Not enough Odds API credits for these settings: live checks every {sched.interval(LIVE):.0f}s "
                f"instead of {self.cfg.poll_seconds}s{near}, so the credits last until the plan resets "
                f"({next_reset(self.cfg, now):%b %d}). Credits left: {left}. If the plan was just upgraded, the new "
                f"credits haven't reached the bot yet.")


def cost_lines(cfg: Config, sched: Scheduler) -> list[str]:
    """What each kind of check costs: measured (x-requests-last) once there are calls to go on."""
    book = getattr(sched.api, "costs", None)

    def one(kind: str, formula: float) -> str:
        n = book.calls(kind, formula) if book else 0
        est = book.estimate(kind, formula) if book else formula
        return f"{est:.1f}" + (f" (measured over {n} calls; formula {formula:g})" if n else " (formula)")
    if sched.split:
        live, pre = sched.split
        pace = (f", every {every(sched.base_interval(PRE_LIVE))} at full speed" if cfg.pregame_with_live_every
                else ", at the pre-game pace")
        lines = [f"Sports with a live game: {one('odds:live', cfg.credits_per_call(live))} for the live check "
                 f"({live}: live games and upcoming ones, every {cfg.poll_seconds}s) + "
                 f"{one('odds:pre', cfg.credits_per_call(pre))} for the pre-game check ({pre}, upcoming games{pace})",
                 f"Other sports: {one('odds', cfg.credits_per_call())} per check ({cfg.markets})"]
    else:
        lines = [f"Each check: {one('odds', cfg.credits_per_call())} ({cfg.markets})"]
    if cfg.props_enabled:
        for sport in cfg.prop_sports:
            f = sched.prop_credits(sport)
            if not f:
                continue
            # (Near kickoff the request carries PROP_NEAR_MARKETS too, unless the API rejected them.)
            near_f = sched.prop_credits(sport, near=sport not in sched.bad_near_sports)
            buckets = [("near kickoff", "near", near_f)] + [(f"up to {h}h out", f"{h}h", f) for h in PROP_COST_HOURS
                                                             if h > cfg.prop_hours]
            if cfg.prop_early_minutes > 0 and cfg.prop_early_hours > PROP_COST_HOURS[-1]:
                buckets.append((f"more than {PROP_COST_HOURS[-1]}h out", "later", f))
            if book and any(book.calls(f"props:{sport}:{b}", bf) for _, b, bf in buckets):
                lines.append(f"{short(sport)} props per game: "
                             + ", ".join(f"{one(f'props:{sport}:{b}', bf)} {label}" for label, b, bf in buckets))
            elif near_f != f:
                lines.append(f"{short(sport)} props per game: {near_f} near kickoff, {f} earlier "
                             f"(formula; measured once props are checked)")
            else:
                lines.append(f"{short(sport)} props per game: {f} (formula; measured once props are checked)")
    return lines


def close_started(now: datetime, *alerters: Alerter) -> set[str]:
    """Close pre-game-only alerts (props) whose game has started: those lines come down at
    kickoff, and no later prop check looks at that game again."""
    started = {op.arb.event_id for a in alerters for op in a.open.values()
               if _parse_time(op.arb.commence_time) <= now}
    started |= {saved["event_id"] for a in alerters for saved in a.restored.values()
                if saved.get("event_id") and saved.get("commence_time") and _parse_time(saved["commence_time"]) <= now}
    if started:
        for a in alerters:
            a.handle([], checked_events=started)
    return started


class LiveConfirm:
    """The live rules for NEW live alerts (pre-game ones, and ones already open, restored after a
    restart or handed over from a live alert between +EV and outlier, pass straight through; a much
    better price on an open live alert meets the same rules in Alerter.reping_waits):

    - Fresh price: the book(s) you're told to bet updated the price within LIVE_MAX_AGE_ALERT
      seconds (Pinnacle's age doesn't matter here). An older price has likely moved already.
    - Two checks: it pings only when LIVE_CONFIRM_CHECKS checks of its sport in a row found it, each
      within `window` seconds of the one before. One that's gone by the next check never pings, and
      one seen, then missing, then back starts counting again. One that passes carries its streak
      (`found`: first check that found it, checks in a row) for arbs.csv.

    The memory is in-process only: after a restart, a live alert waits one more check."""

    def __init__(self, checks: int = 1, max_age: float = 0, window: float = 180):
        self.checks, self.max_age, self.window = checks, max_age, window
        self.streak: dict[str, tuple[int, float, float, object]] = {}   # key -> (checks in a row, first, last, item)
        self.held: list[tuple[object, str]] = []        # this check's: (item, "waiting" or "old price")
        self.dropped: list[tuple[object, float, int]] = []   # ended unconfirmed: (item, first seen, checks)

    def filter(self, items: list, checked: set[str], now: float, alerters=()) -> list:
        """The items that may go to handle() now. checked = the sports (or games, or a Scope: the bet
        types and games) this check looked at; alerters = the ones whose open, restored and
        handed-over alerts are exempt."""
        self.held, self.dropped = [], []
        exempt = {k for a in alerters for k in (*a.open, *a.restored, *a.handed)}
        out, found, stale = [], set(), set()
        for it in items:
            if not it.is_live or it.key in exempt:
                out.append(it)
                continue
            if self.max_age and (it.age is None or it.age > self.max_age):
                self.held.append((it, "old price"))
                stale.add(it.key)
                continue   # not a sighting: its streak (if any) ends below, without counting as gone
            if self.checks <= 1:
                out.append(it)
                continue
            found.add(it.key)
            n, first, last, _ = self.streak.get(it.key, (0, now, -math.inf, None))
            if now - last > self.window:
                n, first = 0, now
            self.streak[it.key] = (n + 1, first, now, it)
            if n + 1 >= self.checks:
                it.found = (first, n + 1)
                out.append(it)
            else:
                self.held.append((it, "waiting"))
        for k, (n, first, last, it) in list(self.streak.items()):
            if k in found:
                continue
            if looked_at(checked, it.sport_key, it.event_id, it.market, it.commence_time) or now - last > self.window:
                del self.streak[k]   # looked for and not found (or too long ago): start over
                if n < self.checks and k not in stale:
                    self.dropped.append((it, first, n))
        return out


# --------------------------------------------------------------------------- tuning from the record (TUNE_ENABLED)

def _tune_group(book_or_sport: str, props: bool) -> tuple[str, bool]:
    return (book_slug(book_or_sport), props)


@dataclass
class Tuning:
    """What the bot's own record says about each book and sport (main lines and props apart), read from the logs
    every TUNE_REFRESH_SECONDS: books whose price is usually gone by the next check, and books and sports that
    clearly beat (or lose to) the closing line. Only new alerts are tuned; a card already up isn't touched."""
    slow: dict = field(default_factory=dict)    # (book, props) -> "BetMGM props: still there 31% of 44 bets"
    boost: dict = field(default_factory=dict)   # ("book" | "sport", name, props) -> (stake multiplier, why)
    loaded: float = 0.0

    @classmethod
    def load(cls, cfg: Config, now: float | None = None) -> "Tuning":
        t = cls(loaded=time.time() if now is None else now)
        if not cfg.tune_enabled:
            return t
        try:
            t._load(cfg)
        except Exception as e:  # noqa: BLE001 - a bad log row mustn't stop alerts: no tuning until the next load
            print(f"  ! Tuning: couldn't read the logs ({e!r:.150}); no tuning for now", file=sys.stderr)
            return cls(loaded=t.loaded)
        return t

    def _load(self, cfg: Config) -> None:
        groups: dict[tuple, list[bool]] = {}
        for r in markout_rows(cfg):
            if r.get("kind") != "arb" and r.get("still_ok") in ("0", "1"):
                groups.setdefault(_tune_group(r.get("book", ""), bool(r.get("player"))), []).append(r["still_ok"] == "1")
        if cfg.tune_slow_still_pct > 0:
            for (book, props), still in groups.items():
                pct = sum(still) / len(still) * 100
                if len(still) >= cfg.tune_slow_bets and pct < cfg.tune_slow_still_pct:
                    self.slow[(book, props)] = (f"price still there on the next check only {pct:.0f}% of the time "
                                                f"({len(still)} bets)")
        clv: dict[tuple, list[float]] = {}
        for r in clv_rows(cfg, days=cfg.log_keep_days or None):
            props = bool(r.get("player"))
            clv.setdefault(("book", book_slug(r.get("book", "")), props), []).append(r["clv_pct"])
            clv.setdefault(("sport", r.get("sport_key", ""), props), []).append(r["clv_pct"])
        for key, xs in clv.items():
            n, mean, _, lo, hi = markout_stats(xs)
            if n < cfg.tune_clv_bets or lo is None:
                continue
            if lo > 0 and cfg.tune_clv_boost > 1:
                self.boost[key] = (cfg.tune_clv_boost, f"beats the close ({mean:+.1f}% avg CLV, {n} bets)")
            elif hi < 0 and cfg.tune_clv_cut < 1:
                self.boost[key] = (cfg.tune_clv_cut, f"loses to the close ({mean:+.1f}% avg CLV, {n} bets)")

    def extra_edge(self, cfg: Config, b: "EVBet") -> tuple[float, str]:
        """(points of extra edge this bet's book needs, why) — (0, "") for most."""
        why = self.slow.get(_tune_group(b.book, is_prop(b.line)))
        return (cfg.tune_slow_extra_pct, why) if why else (0.0, "")

    def stake_mult(self, cfg: Config, b: "EVBet") -> tuple[float, str]:
        props = is_prop(b.line)
        found = []
        for kind, name, key in (("book", b.book, book_slug(b.book)), ("sport", b.sport, b.sport_key)):
            if hit := self.boost.get((kind, key, props)):
                found.append((hit[0], f"{name} {'props ' if props else ''}{hit[1]}"))
        if not found:
            return 1.0, ""
        mult = min(max(math.prod(m for m, _ in found), cfg.tune_clv_cut), cfg.tune_clv_boost)
        return mult, "; ".join(w for _, w in found)


TUNE_REFRESH_SECONDS = 3600


def tune_bets(bets: list, cfg: Config, tuning: Tuning, alerters: tuple) -> list:
    """TUNE_ENABLED: new bets (no card up in these alerters) from a book whose price is usually gone by the next
    check need TUNE_SLOW_EXTRA_PCT more edge (the others are held back, counted as "tuned out"), and each new bet's
    stake follows its book's and sport's CLV. Bets with a card up go through unchanged."""
    if not cfg.tune_enabled:
        return bets
    up = {k for a in alerters for k in (*a.open, *a.restored)}
    out = []
    for b in bets:
        if b.key in up:
            out.append(b)
            continue
        extra, why = tuning.extra_edge(cfg, b)
        bar = cfg.prop_min_ev_pct if is_prop(b.line) else max(cfg.min_ev_pct, cfg.sport_min_ev.get(b.sport_key, 0))
        if extra and b.ev_pct < bar + extra:
            alerters[0].held_counts["tuned out"] = alerters[0].held_counts.get("tuned out", 0) + 1
            print(f"  ✂️ {b.pick} at {b.book} +{b.ev_pct:.1f}%: needs +{bar + extra:g}% ({b.book} {why})", flush=True)
            continue
        mult, note = tuning.stake_mult(cfg, b)
        if mult != 1.0 and b.stake:
            cap = cfg.ev_bankroll * cfg.ev_max_stake_pct / 100
            if b.kalshi_room:   # (cut to what Kalshi's order book holds: never more than that)
                cap = min(cap, b.stake)
            b.stake = round_stake(b.stake * mult, cap, cfg)
            b.tune_note = f"Stake ×{mult:g}: {note}"
        out.append(b)
    return out


class Trackers:
    """Every alert tracker the main loop keeps, wired together the way run() uses them: the hourly
    caps (arbs and prop arbs share one count, outliers and prop outliers another, and every live
    alert shares LIVE_PER_HOUR), closing lines, markouts and the line histories. scan_main() and
    scan_props() put one check through them."""

    def __init__(self, cfg: Config, args):
        dry = args.dry_run
        self.cfg, self.prop_cfg = cfg, cfg.for_props()
        self.arbs = Alerter(cfg, dry_run=dry)
        self.evs = EVAlerter(cfg, dry_run=dry)
        self.outs = OutlierAlerter(cfg, dry_run=dry)
        # Props are fetched per game on their own schedule, so they get their own alert trackers
        # (a main-line check must never "close" a prop alert it didn't look at).
        self.prop_arbs = Alerter(cfg, dry_run=dry, noun="prop arbs")
        self.prop_evs = EVAlerter(cfg, dry_run=dry, noun="+EV props", props=True)
        self.prop_outs = OutlierAlerter(cfg, dry_run=dry, noun="prop outliers", props=True)
        self.parlays = ParlayAlerter(cfg, dry_run=dry)
        self.closing = ClosingTracker(cfg)
        for a in (self.evs, self.prop_evs):
            a.on_log = self.closing.add
        for a in (self.outs, self.prop_outs):
            a.on_log = functools.partial(self.closing.add, kind="outlier")
        # Every new alert's price is checked again on the next checks (markouts; not for --once,
        # --demo or --dry-run).
        self.markouts = make_markouts(cfg, args, [self.arbs, self.evs, self.outs,
                                                  self.prop_arbs, self.prop_evs, self.prop_outs])
        # What the caps and live rules hold back, per day and alert type, for the weekly report card
        # (saved by run(); not for --once, --demo or --dry-run, which mustn't write the service's files).
        off = any(getattr(args, flag, False) for flag in ("once", "demo", "dry_run"))
        self.held = HeldTally(cfg, None if off else held_path(cfg))
        for a in (self.arbs, self.evs, self.outs, self.prop_arbs, self.prop_evs, self.prop_outs, self.parlays):
            a.on_held = self.held.add
        # Bets alerted once already (ONE_ALERT_PER_BET), one memory for every bet alerter: +EV and outliers share
        # keys. Saved only by the service (not --once, --demo or --dry-run), so a restart remembers them.
        self.alerted = AlertedBets(None if off or not cfg.state_dir or not cfg.webhook_url
                                   else data_path(cfg.state_dir) / "alerted_bets.json")
        for a in (self.evs, self.outs, self.prop_evs, self.prop_outs, self.parlays):
            a.alerted = self.alerted
        # ✅ bet tracking (DISCORD_BOT_TOKEN; not --once, --demo or --dry-run).
        self.reactions = BetReactions(cfg, off)
        if self.reactions.on:
            for a in (self.evs, self.outs, self.prop_evs, self.prop_outs, self.parlays):
                a.on_posted = self.reactions.add
        # Bets that came close and what happened to them (CANDIDATE_LOG_FILE; not for --once, --demo or --dry-run).
        self.candidates = CandidateLog(replace(cfg, candidate_log_file="") if off else cfg)
        for a in (self.evs, self.outs, self.prop_evs, self.prop_outs):
            a.on_candidate = self.candidates.add
        self.sharp_history = SharpHistory(cfg.move_window_minutes)
        self.sharp_down: set[str] = set()   # sports the sharp book is missing from (Health; run() keeps it)
        self.price_history = PriceHistory()
        self.tuning = Tuning()   # TUNE_ENABLED: read from the logs by tuned(), at most every TUNE_REFRESH_SECONDS
        # +EV props no sharp prices: spots a book that moved first on news. Its own memory: outliers
        # store the same keys against a different bar and fair price.
        self.prop_prices = PriceHistory()
        # Prop markets left out as bad data, kept out until back in line (prop_glitches).
        self.prop_glitch_hold: dict = {}
        self.evs.max_per_hour = cfg.max_ev_per_hour
        self.prop_evs.max_per_hour = cfg.max_prop_per_hour
        self.parlays.max_per_hour = cfg.max_parlay_per_hour
        self.arbs.max_per_hour = self.prop_arbs.max_per_hour = cfg.max_arb_per_hour
        self.outs.max_per_hour = self.prop_outs.max_per_hour = cfg.max_outlier_per_hour
        self.prop_arbs.posted_at, self.prop_outs.posted_at = self.arbs.posted_at, self.outs.posted_at
        self.live_cap = HourlyCap(cfg.live_per_hour)
        for a in self.singles():
            a.live_cap = self.live_cap
        # The live rules, one memory for arbs and one for bets (+EV and outliers share keys). Props
        # are pre-game only, so they never need them.
        self.arb_live = LiveConfirm(cfg.live_confirm_checks, cfg.live_max_age_alert)
        self.bet_live = LiveConfirm(cfg.live_confirm_checks, cfg.live_max_age_alert)
        self.set_live_interval(cfg.poll_seconds)

    def tuned(self) -> Tuning:
        """The current tuning (TUNE_ENABLED), read again from the logs once it's TUNE_REFRESH_SECONDS old."""
        if self.cfg.tune_enabled and time.time() - self.tuning.loaded >= TUNE_REFRESH_SECONDS:
            self.tuning = Tuning.load(self.cfg)
        return self.tuning

    def singles(self) -> list[Alerter]:
        """Every alerter but parlays."""
        return [self.arbs, self.evs, self.outs, self.prop_arbs, self.prop_evs, self.prop_outs]

    def set_live_interval(self, seconds: float) -> None:
        """How often live games are checked right now (POLL_SECONDS, or slower on a tight budget):
        "the previous check" may be up to three of those back, and at least 3 minutes, so slower
        live checks don't stop every live alert from being confirmed."""
        for rules in (self.arb_live, self.bet_live):
            rules.window = max(180.0, 3.0 * seconds)

    @staticmethod
    def screen(rules: LiveConfirm, items: list, scope: set[str], now: float, alerters: tuple) -> list:
        """Put items through the live rules. What they hold back is noted by the first alerter
        (the console line counts it; arbs.csv logs held arbs)."""
        kept = rules.filter(items, scope, now, alerters)
        for it, why in rules.held:
            alerters[0].note_held(it, why, now)
        for it, first, n in rules.dropped:
            alerters[0].note_held(it, "unconfirmed", now, first, n)
        return kept


HELD_LABELS = {"waiting": "live, waiting for another check", "unconfirmed": "live, gone before it was confirmed",
               "old price": "live, price too old", "capped": "over the hourly cap",
               "live cap": "over the live-alert cap", "old data": "odds too old by sending time (checked again)",
               "not sent": "Discord didn't take it (tried again next check)",
               "alerted before": "alerted once already (ONE_ALERT_PER_BET)",
               "tuned out": "edge too small for a book whose price is usually gone (TUNE_ENABLED)"}


def take_held(*alerters: Alerter) -> dict[str, int]:
    """What these alerters held back since the last call, by why (then reset)."""
    out: dict[str, int] = {}
    for a in alerters:
        for why, n in a.held_counts.items():
            out[why] = out.get(why, 0) + n
        a.held_counts = {}
    return out


def held_text(held: dict[str, int]) -> str:
    """' | held back: 2 over the hourly cap' for the console line ('' when nothing was)."""
    parts = [f"{n} {HELD_LABELS.get(why, why)}" for why, n in held.items() if n]
    return f" | held back: {', '.join(parts)}" if parts else ""


def scan_main(t: Trackers, events: list[dict], checked_sports: list[str], now: datetime | None = None,
              kalshi: bool = True, scope: Scope | None = None) -> SimpleNamespace:
    """One main-line check through every alert type: arbs first (they post before Kalshi is asked,
    which can take seconds, unless one has a Kalshi bet Kalshi's quote can size: see
    note_kalshi_room), then outliers and +EV. Each step reads the clock as it goes, unless
    `now` fixes it (tests, the demo). Returns what the console line reports.

    What was looked at (so only those alerts close, and only those live streaks end when not found):
    the whole of checked_sports, or with LIVE_MARKETS the Scope's bet types and games (the live and
    pre-game checks run on their own clocks, so one pass may have only one of them)."""
    cfg = t.cfg
    if scope is None:
        scope, close = set(checked_sports), {"checked_sports": checked_sports}
    else:
        close = {"checked_events": scope}

    def clock() -> datetime:
        return now or datetime.now(timezone.utc)
    take_held(t.arbs, t.outs, t.evs)
    close["fetched"] = fetched_at(events)   # (no post on odds too old by the time it's ready)
    at = clock()
    arbs = find_arbs(events, cfg, at) if cfg.arbs_enabled else []   # (ARBS_ENABLED=false: none, cards close)
    # An arb with a Kalshi bet waits for Kalshi's quotes (asked below anyway), so its card can say when
    # Kalshi's order book can't take that bet's whole stake at its price.
    kq = kalshi_fair(events, cfg, clock()) if kalshi and kalshi_arb_bets(arbs, cfg) else None
    if kq:
        note_kalshi_room(arbs, kq, cfg)
    sent = t.arbs.handle(t.screen(t.arb_live, arbs, scope, at.timestamp(), (t.arbs,)), now=at.timestamp(), **close)
    if kq is None:
        kq = kalshi_fair(events, cfg, clock()) if kalshi else {}   # free: Kalshi's own prices as a second opinion
    at = clock()
    rejects: list = []   # (for the candidate log)
    outs = find_outliers(events, cfg, at, history=t.price_history, kalshi=kq, rejects=rejects)
    misses: dict[str, int] = {}
    up = set(t.evs.open) | set(t.evs.restored)   # +EV cards up: a confirmed one stays while a source is only missing
    evs = without_outliers(find_evs(events, cfg, at, history=t.sharp_history, kalshi=kq, keep=up,
                                    confirm_misses=misses, sharp_down=t.sharp_down, rejects=rejects), outs)
    t.candidates.extend(rejects, at.timestamp())
    outs = tune_bets(outs, cfg, t.tuned(), (t.outs, t.evs))
    evs = tune_bets(evs, cfg, t.tuned(), (t.evs, t.outs))
    hand_over(evs, outs, t.evs, t.outs, at.timestamp())
    kept = {id(b) for b in t.screen(t.bet_live, outs + evs, scope, at.timestamp(), (t.outs, t.evs))}
    post_outs, post_evs = [b for b in outs if id(b) in kept], [b for b in evs if id(b) in kept]
    note_related([(post_outs, t.outs, scope), (post_evs, t.evs, scope), ([], t.prop_outs, set()),
                  ([], t.prop_evs, set())], at.timestamp())
    t.sharp_history.prune(at)
    t.price_history.prune(at)
    out_sent = t.outs.handle(post_outs, now=at.timestamp(), **close)
    ev_sent = t.evs.handle(post_evs, now=at.timestamp(), **close)
    confirmed = sum(1 for k, op in t.evs.open.items() if k not in up and op.arb.tier == CONFIRMED)
    t.closing.observe(events, at)
    t.closing.finalize(clock())
    return SimpleNamespace(arbs=arbs, sent=sent, outs=outs, out_sent=out_sent, evs=evs, ev_sent=ev_sent,
                           held=take_held(t.arbs, t.outs, t.evs), confirmed_sent=confirmed, confirm_misses=misses)


def scan_props(t: Trackers, prop_events: list[dict], checked: set[str], now: datetime | None = None) -> SimpleNamespace:
    """One prop check (games before kickoff only, so nothing here is live: the live rules and the
    live cap never apply; the hourly caps do). A +EV prop no sharp prices goes through the price
    guard (t.prop_prices), and one with a card up stays while it's at least medium (see find_evs)."""
    cfg = t.cfg
    at = now or datetime.now(timezone.utc)
    ts = at.timestamp()
    take_held(t.prop_arbs, t.prop_outs, t.prop_evs)
    t.prop_prices.held, t.prop_prices.missed = {}, []
    carded = {k for a in (t.prop_evs, t.prop_outs) for k in (*a.open, *a.restored)}
    alt_seen: set = set()   # alternate-line prices with a fair price at exactly their line
    rejects: list = []      # (for the candidate log)
    # Prices that look like bad data (a book's market that isn't the same bet) are left out of everything below:
    # outliers, +EV, arbs, boards, parlays, closing lines (see prop_glitches).
    bad, glitches, said = prop_glitches(prop_events, cfg, at, t.prop_glitch_hold)
    prop_events = drop_glitches(prop_events, bad)
    rejects += glitches
    for line in said:
        print(line, file=sys.stderr)
    p_outs = find_outliers(prop_events, cfg, at, history=t.price_history, alt_seen=alt_seen, rejects=rejects)
    p_evs = without_outliers(find_evs(prop_events, t.prop_cfg, at, history=t.sharp_history, prices=t.prop_prices,
                                      keep=carded, alt_seen=alt_seen, sharp_down=t.sharp_down, rejects=rejects), p_outs)
    t.candidates.extend(rejects, ts)
    t.prop_prices.prune(at)
    p_outs = tune_bets(p_outs, cfg, t.tuned(), (t.prop_outs, t.prop_evs))
    p_evs = tune_bets(p_evs, t.prop_cfg, t.tuned(), (t.prop_evs, t.prop_outs))
    hand_over(p_evs, p_outs, t.prop_evs, t.prop_outs, ts)
    note_related([(p_outs, t.prop_outs, checked), (p_evs, t.prop_evs, checked),
                  ([], t.outs, set()), ([], t.evs, set())], ts)
    fetched = fetched_at(prop_events)
    p_arbs = find_arbs(prop_events, cfg, at) if cfg.arbs_enabled else []
    n_arb = t.prop_arbs.handle(p_arbs, checked_events=checked, now=ts, fetched=fetched)
    n_out = t.prop_outs.handle(p_outs, checked_events=checked, now=ts, fetched=fetched)
    n_ev = t.prop_evs.handle(p_evs, checked_events=checked, now=ts, fetched=fetched)
    t.closing.observe(prop_events, at)
    t.closing.finalize(at)
    # Alternate lines (PROP_NEAR_MARKETS), when this check had any: (prices matched to a fair price at
    # their exact line, new alerts from one), for the console.
    alt = None
    if any(is_alt(m.get("key", "")) for ev in prop_events for bm in ev.get("bookmakers", [])
           for m in bm.get("markets", [])):
        alt = (len(alt_seen), sum(1 for a in (t.prop_evs, t.prop_outs) for k, op in a.open.items()
                                  if k not in carded and op.arb.alt))
    return SimpleNamespace(n_arb=n_arb, n_out=n_out, n_ev=n_ev, held=take_held(t.prop_arbs, t.prop_outs, t.prop_evs),
                           prices_held=dict(t.prop_prices.held), missed=list(t.prop_prices.missed), alt=alt,
                           events=prop_events)


def confirmed_text(sent: int, misses: dict[str, int]) -> str:
    """The console line's part on confirmed pre-game bets: how many new ones went out, and the near
    misses (over PREGAME_CONFIRMED_EV_PCT but under MIN_EV_PCT, and not confirmed) by why, to tune
    the bars from. "" when there were none of either."""
    n = sum(misses.values())
    if not (sent or n):
        return ""
    why = ", ".join(f"{c} {r}" for r, c in sorted(misses.items(), key=lambda x: (-x[1], x[0])))
    return (f" | ✅✅ confirmed pre-game: {sent} sent"
            + (f", {n} near miss{'es' if n != 1 else ''} ({why})" if n else ""))


def kalshi_only_text(evs: list["EVBet"]) -> str:
    """The console line's count of +EV bets priced by Kalshi alone (KALSHI_ONLY): " | Kalshi-only: 2", or ""."""
    n = sum(b.kalshi_only for b in evs)
    return f" | Kalshi-only: {n}" if n else ""


def alt_text(alt: tuple[int, int] | None) -> str:
    """The props console line's count of alternate lines: " | alternate lines: 12 matched, 1 alerted" (matched:
    an alternate-line price with a fair price at exactly its line; alerted: new alerts priced on one).
    "" when the check had no alternate lines."""
    return f" | alternate lines: {alt[0]} matched, {alt[1]} alerted" if alt else ""


def prop_guard_text(prices_held: dict[str, int], missed: list[list[str]], sharp: str = "Pinnacle") -> str:
    """The props console line's part on +EV props no sharp prices: prices the guard held back, and
    lines that cleared the edge but weren't high confidence, with why (to tune the bars from)."""
    out = ""
    if n := sum(prices_held.values()):
        out += (f" | {n} prop price{'' if n == 1 else 's'} held ({prices_held.get('first look', 0)} first look / "
                f"{prices_held.get('moved first', 0)} moved first)")
    if missed:
        why: dict[str, int] = {}
        for reasons in missed:
            for r in reasons:
                why[r] = why.get(r, 0) + 1
        out += (f" | {len(missed)} no-{sharp} prop{'' if len(missed) == 1 else 's'} missed high ("
                + ", ".join(f"{c} {r}" for r, c in sorted(why.items(), key=lambda x: (-x[1], x[0]))) + ")")
    return out


def parlay_bets(*alerters: Alerter) -> list:
    """The bets parlays are built from: every open card of these alerters, outliers too (a leg that grew
    into an outlier is still the same bet; find_parlays applies the +EV price limits to every leg). Not a bet
    whose own post is held because its odds were too old by then: those odds are too old for a parlay too."""
    return [op.arb for a in alerters for op in a.open.values() if not op.deferred]


def run(cfg: Config, args: argparse.Namespace, status: Status) -> None:
    t = Trackers(cfg, args)
    alerter, ev_alerter, out_alerter, parlay_alerter = t.arbs, t.evs, t.outs, t.parlays
    prop_arbs, prop_evs, prop_outs = t.prop_arbs, t.prop_evs, t.prop_outs
    tracker, markouts = t.closing, t.markouts
    results = Results(cfg, dry_run=args.dry_run)
    health = Health(cfg, status)        # outage messages (and which sports the sharp book is missing from)
    t.sharp_down = health.sharp_down
    short_credits = CreditsShort(cfg)   # checks held 3x slower or more for half an hour: said once a day
    shed_before: set[str] = set()       # SHED_SPORTS left out when the health channel was last told
    results_warned = 0.0                # when the health channel was last told grading or results failed
    trouble_told: dict[str, float] = {}   # webhook -> when the health channel was last told Discord refused it

    def update_parlays() -> int:
        open_bets = parlay_bets(ev_alerter, prop_evs, out_alerter, prop_outs)
        return parlay_alerter.handle(find_parlays(open_bets, cfg,
                                                  keep=set(parlay_alerter.open) | set(parlay_alerter.restored),
                                                  now=datetime.now(timezone.utc)))

    if args.demo:
        # Sample data must never reach the real logs (it would be graded and counted).
        for a in (alerter, ev_alerter, out_alerter, parlay_alerter):
            a.log_path = lambda: ""
            a._state_path = lambda: None
            a.restored = {}
        events = demo_events(live_arb=False)
        sports = sorted({ev["sport_key"] for ev in events})
        # Checked like the real loop, a minute apart, as many times as a live alert needs to show up
        # (LIVE_CONFIRM_CHECKS). Kalshi isn't asked: the games are made up.
        n = max(1, cfg.live_confirm_checks)
        start = datetime.now(timezone.utc) - timedelta(seconds=cfg.poll_seconds * (n - 1))
        sent, held, confirmed = [0, 0, 0], {}, 0
        for i in range(n):
            res = scan_main(t, events, sports, start + timedelta(seconds=cfg.poll_seconds * i), kalshi=False)
            sent = [sent[0] + res.sent, sent[1] + res.ev_sent, sent[2] + res.out_sent]
            held, confirmed = res.held, confirmed + res.confirmed_sent
        print(f"[demo] {sent[0]} arb(s), {sent[1]} +EV bet(s), {sent[2]} outlier(s) in sample data"
              + (f" ({n} checks: live alerts go out once {n} checks in a row find them)" if n > 1 else "")
              + held_text(held) + confirmed_text(confirmed, res.confirm_misses))
        return

    api = OddsAPI(cfg)
    sched = Scheduler(cfg, api)

    if args.plan:
        print_plan(cfg, sched)
        return

    if args.check_kalshi:
        check_kalshi(cfg, api)
        return

    if getattr(args, "check_upcoming", False):   # (getattr: tests build args without it)
        check_upcoming(cfg, api)
        return

    if args.results or args.post_results:
        tz = ZoneInfo(cfg.timezone)
        which = args.post_results or args.results
        today = datetime.now(tz).date()
        try:
            day = {"today": today, "yesterday": today - timedelta(days=1)}.get(which) or date.fromisoformat(which)
        except ValueError:
            sys.exit(f"Use today, yesterday or a date like {today:%Y-%m-%d} (got {which!r}).")
        n = len(settle_pending(cfg, api))
        print(f"Graded {n} new bet(s).")
        if args.post_results:
            res = Results(cfg, dry_run=args.dry_run)
            outcome = res.recap(day, finished=day < today)
            if outcome == "nothing":
                print(f"No alerts for games on {day:%a %b %-d}, so there's nothing to post.")
            elif res.dry_run:
                print("(dry run: not sent)")
            elif outcome == "sent":
                print("Posted to Discord. The bot won't post these results again.")
            else:
                sys.exit("Couldn't post to Discord (see the error above). Try again in a minute.")
            return
        print_day(cfg, day)
        if line := score_check_line(cfg):
            print(line)
        board = scoreboard_text(cfg).split("\n\n", 1)[1]     # the day itself is printed above
        print("\n" + re.sub(r"__|\*\*|```\n?", "", board))
        if report := _safely("--results", markout_report, cfg):
            print(f"\nMarkouts (each alert's price checked again minutes later), {markout_span(cfg)}:\n" + report)
        return

    mode = "dry run (console only)" if alerter.dry_run else "sending to Discord"
    n_live = cfg.credits_per_call(sched.split[0]) if sched.split else 0
    checks = (f"live games: {sched.split[0]} ({n_live} credit{'' if n_live == 1 else 's'}/check), the rest "
              f"before kickoff ({cfg.credits_per_call(sched.split[1])})" if sched.split else f"{sched.cost} credits/check")
    print(f"Arb bot started: {', '.join(short(s) for s in cfg.sports)} | {cfg.markets} | {checks} | {mode}")
    print(mode_line(cfg))
    if cfg.active_hours:
        print(f"Active hours: {cfg.active_hours} ({cfg.timezone})")
    if REMOTE_USED:
        print(f"Using settings pushed in {REMOTE_ENV}: " + ", ".join(f"{k}={v}" for k, v in REMOTE_USED.items()))
    print(unit_line(cfg))
    if not (args.once or status.budget_warned):   # (an hourly retry after running out of credits: said already)
        status.send(online_message(cfg))

    tz = ZoneInfo(cfg.timezone)
    started_local = datetime.now(tz)
    # Started before today's summary hour (a restart at 3am)? Today's summary is still owed.
    summary_day = (started_local.date() if started_local.hour >= max(cfg.summary_hour, 0)
                   else started_local.date() - timedelta(days=1))
    idle_logged = False
    pruned_day = None   # the local day the logs were last cut to LOG_KEEP_DAYS
    sharp_seen = False
    sharp_checks = 0
    save_costs = False
    while True:
        now = datetime.now(timezone.utc)
        local = now.astimezone(tz)
        if (cfg.summary_hour >= 0 and not args.once and local.hour >= cfg.summary_hour
                and local.date() != summary_day):
            summary_day = local.date()
            sections = ([("💰 **Arbs**", f"{alerter.summary()}; props: {prop_arbs.summary()}")]
                        if cfg.arbs_enabled else [])
            if cfg.ev_enabled:
                live = None if cfg.ev_live else False   # (live +EV off: its old bets aren't in the record shown)
                sections.append(("📈 **+EV**", f"{ev_alerter.summary()}\nIf you bet every "
                                 f"{'alert' if cfg.ev_live else 'pre-game alert'}: last 7 days "
                                 f"{ev_record(cfg, 7, live=live)}; all time {ev_record(cfg, live=live)}"))
            if cfg.props_enabled:
                sections.append(("🎯 **Props**", f"{prop_evs.summary()}; {prop_outs.summary()}"))
            if cfg.outliers_enabled:
                live = None if cfg.outlier_live else False   # (live outliers off: only what can still be alerted)
                sections.append(("🚨 **Outliers**", f"{out_alerter.summary()}\nIf you bet every "
                                 f"{'alert' if cfg.outlier_live else 'pre-game alert'}: last 7 days "
                                 f"{ev_record(cfg, 7, kinds=('outlier',), live=live)}"))
            if cfg.parlays_enabled:
                sections.append(("📦 **Parlays**", parlay_alerter.summary()))
            book = getattr(api, "costs", None)
            status.send_card(summary_payload(cfg, sections, api.remaining, book.take_today() if book else None))
            for a in (alerter, ev_alerter, out_alerter, parlay_alerter, prop_arbs, prop_evs, prop_outs):
                a.reset_stats()

        if cfg.log_keep_days and not (args.once or args.dry_run) and local.date() != pruned_day:
            pruned_day = local.date()   # once a day (and at start): candidates.csv, arbs.csv, markouts.csv
            for name, n in prune_logs(cfg, now).items():
                print(f"Trimmed {name} to the last {cfg.log_keep_days} days ({n} older rows removed).", flush=True)

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
            old = pace_state(sched)
            sched.update_budget(now)
            save_costs = not (args.once or args.dry_run)   # after this pass's checks (below)
            if sched.scale == math.inf:
                broke = api.remaining is not None and api.remaining <= 0
                if args.once:
                    sys.exit("Out of Odds API credits until the plan resets; not scanning." if broke else
                             "BUDGET_WEIGHTS gives the next 24h no budget (weight 0); not scanning.")
                if not status.budget_warned:
                    status.budget_warned = True
                    status.send("⛔ Out of Odds API credits until the plan resets. Pausing." if broke else
                                "⏸️ BUDGET_WEIGHTS gives the next 24h no budget (weight 0). Pausing.")
                time.sleep(3600)
                continue
            if status.budget_warned and api.remaining is not None:   # (once the Odds API has said how many)
                status.budget_warned = False
                health.say("✅ Credits available again: scanning resumed.")
            if note := short_credits.check(sched, now, api.remaining):
                status.send(note)
            if sched.shed != shed_before:
                status.send(f"⚠️ Credits are tight: {', '.join(short(sp) for sp in sorted(sched.shed))} left out "
                            "for now so everything else keeps its speed (SHED_SPORTS)." if sched.shed else
                            "✅ Credits have room again: back to checking every sport.")
                shed_before = set(sched.shed)
            if pace_changed(old, sched):
                print(f"Budget: {sched.allowance:,.0f} credits/day, next 24h needs "
                      f"{sched.forecast:,.0f} at full speed → live checks every "
                      f"{sched.interval(LIVE):.0f}s, upcoming games every "
                      f"{sched.interval(EARLY) / 60:.0f}m"
                      + "".join(f"\n  {line}" for line in pace_lines(cfg, sched) if "full speed" not in line),
                      flush=True)
            if note := sched.live_cap_note(now):
                print(note, flush=True)

        due = [s for s in cfg.sports if sched.state(s, now)] if args.once else sched.due(now)
        prop_games = sched._prop_games(now) if args.once else sched.props_due(now)
        sched.need_close, sched.need_close_props = tracker.needs_close(), tracker.needs_close_props()
        # Main lines and props go out together, so neither waits on the other.
        with ThreadPoolExecutor(max_workers=2) as pool:
            main_job = pool.submit(sched.fetch, due, now) if due else None
            prop_job = pool.submit(sched.fetch_props, prop_games, now) if prop_games else None
            main_events, fetched_sports = main_job.result() if main_job else ([], set())
            fetched_props, fetched_gids = prop_job.result() if prop_job else ([], set())
        for note in sched.notices:   # (a prop type the API rejected)
            status.send(note)
        sched.notices.clear()
        # Only sports/games whose request worked count as checked: a timeout isn't "the bet is gone".
        checked_sports = [s for s in due if s in fetched_sports]
        # Outages (health channel): the Odds API per sport (main lines or props answered at all), then whether
        # the sharp book is in the answers (before the checks below: while it's missing they adjust).
        health.odds(set(due) | {sport for sport, _ in prop_games},
                    set(fetched_sports) | {sport for sport, gid in prop_games if gid in fetched_gids}
                    | sched.props_answered, sched.failures)
        sched.failures.clear()
        sched.props_answered.clear()
        health.sharp(main_events, checked_sports)
        close_started(now, prop_arbs, prop_evs, prop_outs)   # first, so no new card names them
        if due:
            idle_logged = False
            t0 = time.time()
            events = main_events
            t.set_live_interval(sched.interval(LIVE))
            res = scan_main(t, events, checked_sports, scope=sched.scope)
            health.kalshi(any(ev.get("sport_key") in KALSHI_SERIES and _parse_time(ev["commence_time"]) > now
                              for ev in events))
            status.check_credits(api.remaining)
            left = f"{api.remaining:,.0f}" if api.remaining is not None else "?"
            ev_note = (f" | {len(res.evs)} +EV, {res.ev_sent} new{kalshi_only_text(res.evs)}"
                       f"{confirmed_text(res.confirmed_sent, res.confirm_misses)}" if cfg.ev_enabled else "")
            ev_note += f" | {len(res.outs)} outliers, {res.out_sent} new" if cfg.outliers_enabled else ""
            print(f"[{datetime.now():%H:%M:%S}] checked {', '.join(short(s) for s in due)} "
                  f"({len(events)} games, {time.time() - t0:.1f}s) | {len(res.arbs)} arbs, {res.sent} new"
                  f"{ev_note}{held_text(res.held)} | credits left {left}", flush=True)

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

        prop_events: list[dict] = []
        if prop_games:
            t0 = time.time()
            prop_events = [ev for ev in fetched_props if _parse_time(ev["commence_time"]) > now]
            p = scan_props(t, prop_events, fetched_gids)
            prop_events = p.events   # without prices that looked like bad data (markouts read these too)
            left = f"{api.remaining:,.0f}" if api.remaining is not None else "?"
            print(f"[{datetime.now():%H:%M:%S}] props: {len(prop_games)} games ({time.time() - t0:.1f}s) | "
                  f"{p.n_arb} new arbs, {p.n_ev} new +EV, {p.n_out} new outliers{held_text(p.held)}"
                  f"{prop_guard_text(p.prices_held, p.missed, (_csv(cfg.sharp_books) or ['sharp'])[0].title())}"
                  f"{alt_text(p.alt)} | "
                  f"credits left {left}", flush=True)

        markouts.update(main_events, prop_events, now)   # after every alert of this pass went out
        t.held.save()
        if save_costs and (book := getattr(api, "costs", None)):
            book.save()   # measured call costs survive a restart (and show in --plan), every 5 minutes or so
            save_costs = False

        if (due or prop_games) and cfg.parlays_enabled:
            n_par = update_parlays()
            if n_par:
                print(f"  📦 {n_par} new parlay(s)", flush=True)

        yesterday = local.date() - timedelta(days=1)
        # (RESULTS_DAILY_ONLY: just after midnight, as the day's only results post; else with the morning summary.)
        recap_time = (local.time() >= RECAP_AT if cfg.results_daily_only
                      else cfg.summary_hour >= 0 and local.hour >= cfg.summary_hour)
        # Results never stop the bot: a bad row in a log used to crash it, and systemd restarted it every minute
        # (paying for a fresh check each time). Now it's said in the health channel, at most once a day, and the
        # alerts keep going.
        try:
            if not args.once and recap_time and results.recap_due(yesterday):
                results.daily(api, yesterday, now)   # yesterday's full card, once (survives restarts)
            if (not args.once and cfg.summary_hour >= 0 and local.hour >= cfg.summary_hour and local.weekday() == 0
                    and results.weekly_due(local.date())):
                results.weekly(local.date(), now, t.held.days)   # Monday: last week's report card, once
            if not args.once and results.due():
                n_res = results.tick(api, now)
                if n_res:
                    print(f"  📋 {n_res} bet result(s) posted", flush=True)
        except Exception as e:  # noqa: BLE001
            results_warned = results_trouble(status, e, results_warned)
        discord_trouble(cfg, status, trouble_told)
        if not args.once and t.reactions.due():
            try:
                t.reactions.tick(now)
            except Exception as e:  # noqa: BLE001 - ✅ tracking must never stop the bot
                print(f"  ! ✅ tracking failed: {e!r:.200}", file=sys.stderr)

        if args.once:
            if line := links_line(main_events + prop_events, cfg):
                print(line)   # (which books' cards open the bet itself, and which only the game)
            if not due and not prop_games:
                print("Nothing live or starting soon to check right now.")
            return
        time.sleep(max(1.0, sched.seconds_to_next(now)))


def results_trouble(status: "Status", e: Exception, warned: float, now: float | None = None) -> float:
    """Grading or posting results failed: print it, and tell the health channel once a day. Returns when it last
    told it."""
    now = time.time() if now is None else now
    print(f"! Results failed: {e!r:.300}", file=sys.stderr, flush=True)
    if warned and now - warned < 86400:
        return warned
    status.send(f"⚠️ Couldn't grade or post results ({type(e).__name__}: {str(e)[:150]}). Bet alerts keep going; "
                "the server log has the details (journalctl -u arbbot | grep 'Results failed').")
    return now


def sample_payloads(cfg: Config) -> list[tuple[dict, str, str]]:
    """--test-discord's samples, as (payload, webhook, kind): each alert type that's on, made up from the demo
    games. With DISCORD_TEST_WEBHOOK_URL all go there and ping their book's role (BOOK_ROLES), so you see the
    notification exactly as a real one; without it they go to the real channels and ping nobody."""
    cfg = cfg.with_mode()
    events = demo_events(live_arb=False)
    test = cfg.test_webhook_url
    ev_url = test or cfg.ev_webhook_url or cfg.webhook_url
    samples = []
    if cfg.arbs_enabled:
        samples += [(discord_payload(arb), test or cfg.webhook_url, "arb") for arb in find_arbs(events, cfg)[:1]]
    if cfg.ev_enabled:
        samples += [(ev_payload(b, book_role_mention(cfg, b.book) if test else ""), ev_url, "+EV")
                    for b in find_evs(events, cfg)[:1]]
    if cfg.outliers_enabled:
        samples += [(outlier_payload(o, book_role_mention(cfg, o.book) if test else ""),
                     test or cfg.outlier_webhook_url or ev_url, "outlier") for o in find_outliers(events, cfg)[:1]]
    for payload, _, _ in samples:
        emb = payload["embeds"][0]
        emb["title"] = ("🧪 SAMPLE · " + emb["title"])[:256]
        emb["footer"] = {"text": "SAMPLE ALERT: made-up game and prices. Don't bet this."}
        emb.pop("url", None)
    return samples


def send_samples(cfg: Config) -> None:
    """--test-discord: one sample of each alert type that's on, each to its channel (or the test channel)."""
    if not (cfg.webhook_url or cfg.test_webhook_url):
        sys.exit("Set DISCORD_WEBHOOK_URL in .env first.")
    cfg.bad_webhooks()
    samples = sample_payloads(cfg)
    ids = []
    for payload, url, kind in samples:
        ids.append((kind, payload, url, (_webhook(url, payload) or {}).get("id")))
        where = ("the test channel" if url == cfg.test_webhook_url
                 else "the main channel" if url == cfg.webhook_url else "its own channel")
        print(f"  sent the {kind} sample to {where}" + (f" (pinging {payload['content']})" if payload.get("content") else ""))
    print(f"Sent {len(samples)} sample alerts.")
    arb = next(((p, u, i) for kind, p, u, i in ids if kind == "arb" and i), None)
    if arb:
        print("In 5 seconds the arb turns 'GONE'...")
        time.sleep(5)
        _webhook(arb[1], discord_payload(find_arbs(demo_events(live_arb=False), cfg)[0], gone_after=5), "PATCH", arb[2])
    print("Done. Check your Discord channel.")


# --------------------------------------------------------------------------- the blueprint (--blueprint)

BLUEPRINT_FILE = HERE / "docs" / "BLUEPRINT.md"   # every blueprint ID, its colour, status and what the bot does
BLUEPRINT_STATUSES = ("DONE", "PARTIAL", "SUPERSEDED", "NOT_FEASIBLE", "PROCESS", "DEVIATION")


def blueprint_rows(text: str) -> list[list[str]]:
    """BLUEPRINT.md's table of IDs: [ID, colour, status, what the blueprint says, what the bot does] per row."""
    rows = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 5 and cells[2] in BLUEPRINT_STATUSES:
            rows.append(cells)
    return rows


def _plain_md(s: str) -> str:
    """Markdown as plain console text: no bold, no code marks."""
    return s.replace("**", "").replace("`", "")


def setting_now(cfg: Config, key: str) -> str | None:
    """'KEY=value' as this bot has it now (ALERT_MODE applied) for a setting named like its Config field
    (SHARP_WEIGHTS -> sharp_weights); None for anything else, and for webhooks and keys."""
    name = key.lower()
    if name not in {f.name for f in fields(Config)} or "webhook" in name or "key" in name:
        return None
    v = getattr(cfg, name)
    if isinstance(v, bool):
        shown = "true" if v else "false"
    elif isinstance(v, (int, float)):
        shown = _plain(v)
    elif isinstance(v, dict):
        shown = ";".join(f"{k}={_plain(x) if isinstance(x, (int, float)) else x}" for k, x in v.items())
    else:
        shown = "" if v is None else str(v)
    return f"{key}={shown}" if shown else f"{key} (empty)"


def blueprint_text(cfg: Config | None = None, path: Path | None = None) -> str:
    """--blueprint: every YELLOW and RED item in docs/BLUEPRINT.md with its status and what was decided, then
    the open decisions, each with the settings it names as this bot has them now (cfg; None: not shown)."""
    path = path or BLUEPRINT_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return f"{path} is missing: it comes with the code (git pull brings it back).\n"
    wrap = lambda s, ind: textwrap.fill(_plain_md(s), 100, initial_indent=ind, subsequent_indent=" " * len(ind),
                                        break_long_words=False, break_on_hyphens=False)
    out = ["The blueprint's Yellow and Red items (all of them, with every other ID, in docs/BLUEPRINT.md):", ""]
    for id_, colour, status, says, does in blueprint_rows(text):
        if colour in ("YELLOW", "RED"):
            out += [f"{id_}  {colour}  {status}", wrap(says, "  "), wrap(does, "  → "), ""]
    lines = text.splitlines()
    start = next((i for i, x in enumerate(lines) if x.startswith("## Open decisions")), None)
    if start is not None:
        out.append(_plain_md(lines[start][3:]) + ":")
        items: list[str] = []
        for x in lines[start + 1:]:
            if x.startswith("## "):
                break
            if x.startswith("- "):
                items.append(x[2:].strip())
            elif x.startswith("  ") and items:
                items[-1] += " " + x.strip()
        for item in items:
            out.append(wrap(item, "- "))
            if cfg is not None:
                keys = dict.fromkeys(k for code in re.findall(r"`([^`]+)`", item)
                                     for k in re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", code))
                now = [x for x in (setting_now(cfg, k) for k in keys) if x]
                if now:
                    out.append(wrap("Yours now: " + ", ".join(now), "    "))
    return "\n".join(out).rstrip() + "\n"


def main() -> None:
    REMOTE_USED.update(load_settings())
    p = argparse.ArgumentParser(description="Find sports betting arbitrage and alert on Discord.")
    p.add_argument("--plan", action="store_true",
                   help="show the next 24h schedule and credit forecast (uses no credits)")
    p.add_argument("--once", action="store_true", help="check every live/upcoming sport once and exit")
    p.add_argument("--demo", action="store_true", help="use bundled sample data (no API key needed)")
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending to Discord")
    p.add_argument("--test-discord", action="store_true", help="send one sample alert to Discord and exit")
    p.add_argument("--set", metavar="KEY=VALUE", action="append",
                   help="change a setting in .env, e.g. --set MY_BOOKS=fanduel,draftkings (repeatable)")
    p.add_argument("--set-webhook", choices=sorted(WEBHOOK_SETTINGS), metavar="CHANNEL",
                   help="paste a Discord webhook URL for a channel: " + ", ".join(sorted(WEBHOOK_SETTINGS)))
    p.add_argument("--post-guide", action="store_true",
                   help="post a how-to-use guide to your Discord channel (then pin it)")
    p.add_argument("--check-kalshi", action="store_true",
                   help="check that Kalshi's prices are read and matched to games (free, no credits)")
    p.add_argument("--check-props", action="store_true",
                   help="check that player props can be graded from ESPN box scores (free, no credits)")
    p.add_argument("--check-upcoming", action="store_true",
                   help="one-time check of the combined live odds call: its real cost and whether it lists every "
                        "live game (about 1 credit plus 1 per sport with a live game)")
    p.add_argument("--post-results", nargs="?", const="today", metavar="DAY",
                   help="post a day's results card to the results channel (today, yesterday or YYYY-MM-DD)")
    p.add_argument("--results", nargs="?", const="today", metavar="DAY",
                   help="grade finished alerts and list a day's bets with what hit (today, yesterday or "
                        "YYYY-MM-DD), plus the records (2 credits per sport with finished games)")
    p.add_argument("--weekly", action="store_true",
                   help="print the weekly report card for the last 7 days, with suggestions (free, no credits)")
    p.add_argument("--post-weekly", action="store_true",
                   help="post the weekly report card to the results channel now (it goes out by itself on Mondays)")
    p.add_argument("--blueprint", action="store_true",
                   help="print the blueprint's Yellow and Red items: their status, what was decided, and your "
                        "current settings for the open decisions (free, no credits)")
    args = p.parse_args()

    if args.set_webhook:
        set_webhook(args.set_webhook)
        return

    if args.blueprint:   # (before the settings check: a bad .env only hides the "Yours now" values)
        try:
            cfg = Config.from_env()
        except ValueError:
            cfg = None
        print(blueprint_text(cfg), end="")
        return

    if args.set:
        changes = {}
        for item in args.set:
            key, sep, value = item.partition("=")
            key = key.strip().upper()
            if not sep or not key.replace("_", "").isalnum():
                sys.exit(f"Use KEY=VALUE, like MY_BOOKS=fanduel,draftkings (got: {item!r}). Nothing was changed.")
            changes[key] = value.strip()
        asked, changes = changes, with_unit_bankroll(changes)
        problem, others = check_settings(changes)
        if problem:
            sys.exit(f"Bad value: {problem}. Nothing was changed.")
        for key, value in changes.items():
            set_env_value(HERE / ".env", key, value)
            print(f"Saved {key}={value}" + ("" if key in asked else " (100 units, so every stake scales with UNIT_SIZE)"))
            if key in REMOTE_USED:
                print(f"Note: {REMOTE_ENV} (settings pushed for you) sets {key}={REMOTE_USED[key]}, and that wins "
                      f"over .env. To use your own settings instead: --set REMOTE_SETTINGS=off")
        for ex in others:
            print(f"Note: another setting in .env needs fixing too: {ex}")
        print("Now restart the bot so it uses the new settings:  systemctl restart arbbot")
        return

    try:
        cfg = Config.from_env()
    except ValueError as ex:
        # Exit code 2 tells systemd not to restart-loop on a setting that will fail every time.
        print(f"Bad setting in .env: {ex}. Fix it with: nano /opt/arb-bot/.env", file=sys.stderr)
        url = next((u for u in (os.environ.get("DISCORD_STATUS_WEBHOOK_URL", ""), os.environ.get("DISCORD_WEBHOOK_URL", ""))
                    if u.startswith("https://")), "")   # a placeholder status URL falls back, like Status
        interactive = (args.dry_run, args.demo, args.once, args.plan, args.results, args.test_discord,
                       args.post_guide, args.post_results, args.check_kalshi, args.check_props, args.check_upcoming,
                       args.weekly, args.post_weekly)
        if url.startswith("https://") and not any(interactive):   # the service: say why it stopped
            try:
                _webhook(url, {"username": BOT_NAME, "content": f"🔴 Bot stopped: bad setting in .env: {ex}. "
                               "Fix it (nano /opt/arb-bot/.env), then: systemctl restart arbbot"})
            except Exception:  # noqa: BLE001 - already exiting with the reason printed
                pass
        sys.exit(2)
    global ODDS_FORMAT, PROP_GRADING
    ODDS_FORMAT = cfg.odds_format
    PROP_GRADING = cfg.prop_grading != "off"

    if args.check_props:
        check_props(cfg)
        return

    if args.weekly or args.post_weekly:
        weekly_command(cfg, args)
        return

    if args.post_guide:
        if not cfg.webhook_url:
            sys.exit("Set DISCORD_WEBHOOK_URL in .env first.")
        _webhook(cfg.webhook_url, guide_payload())
        print("Posted the guide. In Discord: hover over it → ⋯ → Pin Message.")
        return

    if args.test_discord:
        send_samples(cfg)
        return

    if not args.demo and not cfg.api_key:
        print("Set ODDS_API_KEY in .env (key at https://the-odds-api.com), or run with --demo.", file=sys.stderr)
        sys.exit(2)

    bad = cfg.bad_webhooks()
    status = Status(cfg, dry_run=args.dry_run or args.demo or args.plan or args.once or bool(args.results)
                    or bool(args.post_results) or args.check_kalshi or args.check_upcoming)
    if bad:
        status.send(f"⚠️ {', '.join(bad)} in .env isn't a Discord webhook URL, so those alerts are going "
                    f"to this channel for now. Fix it with: nano /opt/arb-bot/.env")
    for w in cfg.warnings():   # settings that load but probably aren't what was meant
        status.send(f"⚠️ {w}")
    while True:
        try:
            run(cfg, args, status)
            return
        except urllib.error.HTTPError as e:
            read_api_error(e)   # (what the Odds API said: its error_code tells out of credits from a bad key)
            code = getattr(e, "odds_error_code", "")
            if e.code in (401, 429) and code == "OUT_OF_USAGE_CREDITS":
                # Said once per outage (not every hour, nor after run()'s own "out of credits" pause). The hourly
                # retries don't say "online" again; the first check with credits says "Credits available again".
                if not status.budget_warned:
                    status.budget_warned = True
                    status.send("⏸️ The Odds API says the plan's credits are used up. Checking again every hour: "
                                "they come back when the plan resets or is upgraded.")
                time.sleep(3600)
                continue
            if e.code == 401:
                status.send("🔴 Odds API rejected the key (401). Check ODDS_API_KEY. Bot stopped.")
                print("Odds API rejected the key (401). Check ODDS_API_KEY.", file=sys.stderr)
                sys.exit(2)  # config problem: the service won't keep restarting
            if e.code == 429 and code == "EXCEEDED_FREQ_LIMIT":
                status.send("⏸️ The Odds API asked the bot to slow down (too many requests at once). "
                            "Retrying in 1 minute.")
                time.sleep(60)
                continue
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
