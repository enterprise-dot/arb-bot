import math
import time
import unittest
import urllib.error
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from datetime import datetime, timedelta, timezone

from arbbot import (LIVE, PREGAME, EARLY, FAR, PRE_LIVE, Alerter, Config, EVAlerter, OutlierAlerter, Scheduler,
                    demo_events, devig, find_outliers, outlier_payload, without_outliers,
                    discord_payload, ev_payload, ev_record, find_arbs, find_evs, kelly_stake,
                    next_reset, seconds_until_active, settle, settle_pending,
                    ClosingTracker, clv_pct, clv_record, clv_rows,
                    Parlay, ParlayAlerter, find_parlays, parlay_payload, market_label, append_csv,
                    consensus_fair)

import arbbot as _arbbot


def _no_network(path, timeout=None):
    raise OSError("tests don't use the network")


_arbbot._espn_get = _no_network   # box-score tests install a fake ESPN / NHL / MLB
_arbbot._get_json = _no_network

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
FRESH = NOW.isoformat().replace("+00:00", "Z")


def event(books, home="Home", away="Away", start="2026-10-03T18:00:00Z"):
    """books: {book_title: [(market, [(name, price, point), ...]), ...]}"""
    return {
        "id": "e1", "sport_title": "Test", "commence_time": start,
        "home_team": home, "away_team": away,
        "bookmakers": [
            {"key": title.lower(), "title": title, "last_update": FRESH,
             "markets": [{"key": mk, "last_update": FRESH, "outcomes": [
                 {"name": n, "price": p, **({"point": pt} if pt is not None else {})}
                 for n, p, pt in outs]} for mk, outs in mkts]}
            for title, mkts in books.items()
        ],
    }


CFG = Config(min_profit_pct=0.0, bankroll=100, round_stakes=0)


def temp_data(test, cfg):
    """cfg with every data file and the state folder in a temporary folder, removed after the test: an
    alerter that logs must never write the bot's own ev_bets.csv, outliers.csv... beside the code."""
    import tempfile
    from dataclasses import fields
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    d = Path(tmp.name)
    files = {f.name: str(d / Path(getattr(cfg, f.name)).name) for f in fields(Config)
             if f.name.endswith("_file") and getattr(cfg, f.name)}
    return replace(cfg, **files, state_dir=str(d / "state"))


class FindArbs(unittest.TestCase):
    def test_two_way_arb(self):
        ev = event({
            "A": [("h2h", [("Home", 2.10, None), ("Away", 1.80, None)])],
            "B": [("h2h", [("Home", 1.70, None), ("Away", 2.15, None)])],
        })
        [arb] = find_arbs([ev], CFG, NOW)
        self.assertAlmostEqual(arb.margin, 1 / 2.10 + 1 / 2.15)
        self.assertEqual({(l.outcome, l.book) for l in arb.legs}, {("Home", "A"), ("Away", "B")})
        # Stakes equalise the payout, so every leg returns the same amount.
        payouts = [l.stake * l.price for l in arb.legs]
        self.assertAlmostEqual(payouts[0], payouts[1], delta=0.05)
        self.assertGreater(arb.guaranteed_return, 100)

    def test_no_arb_when_books_agree(self):
        ev = event({
            "A": [("h2h", [("Home", 1.90, None), ("Away", 1.90, None)])],
            "B": [("h2h", [("Home", 1.95, None), ("Away", 1.87, None)])],
        })
        self.assertEqual(find_arbs([ev], CFG, NOW), [])

    def test_missing_draw_is_not_an_arb(self):
        # Book B omits the draw; ignoring it would fake a huge arb.
        ev = event({
            "A": [("h2h", [("Home", 2.0, None), ("Away", 3.0, None), ("Draw", 3.0, None)])],
            "B": [("h2h", [("Home", 2.5, None), ("Away", 3.5, None)])],
        })
        self.assertEqual(find_arbs([ev], CFG, NOW), [])

    def test_three_way_arb(self):
        ev = event({
            "A": [("h2h", [("Home", 3.0, None), ("Away", 3.0, None), ("Draw", 3.6, None)])],
            "B": [("h2h", [("Home", 3.4, None), ("Away", 2.5, None), ("Draw", 3.0, None)])],
        })
        [arb] = find_arbs([ev], CFG, NOW)
        self.assertEqual(len(arb.legs), 3)
        self.assertLess(arb.margin, 1)

    def test_spreads_pair_opposite_points(self):
        ev = event({
            "A": [("spreads", [("Home", 2.10, -3.5), ("Away", 1.75, 3.5)])],
            "B": [("spreads", [("Home", 1.75, -3.5), ("Away", 2.10, 3.5),
                               ("Home", 3.0, -7.5), ("Away", 1.35, 7.5)])],
        })
        [arb] = find_arbs([ev], CFG, NOW)
        self.assertEqual(arb.line, -3.5)

    def test_totals_dont_mix_lines(self):
        ev = event({
            "A": [("totals", [("Over", 2.3, 220.5), ("Under", 1.6, 220.5)])],
            "B": [("totals", [("Over", 1.6, 224.5), ("Under", 2.3, 224.5)])],
        })
        self.assertEqual(find_arbs([ev], CFG, NOW), [])

    def test_stale_prices_ignored(self):
        ev = event({
            "A": [("h2h", [("Home", 2.10, None), ("Away", 1.80, None)])],
            "B": [("h2h", [("Home", 1.70, None), ("Away", 2.15, None)])],
        })
        ten_min = (NOW - timedelta(minutes=10)).isoformat()
        ev["bookmakers"][1]["markets"][0]["last_update"] = ten_min
        self.assertEqual(len(find_arbs([ev], CFG, NOW)), 1)       # pre-game: 10 min is fine
        ev["commence_time"] = "2026-10-03T11:00:00Z"
        self.assertEqual(find_arbs([ev], CFG, NOW), [])           # live: too old
        ev["commence_time"] = "2026-10-03T13:00:00Z"              # kicks off in 1 hour
        ev["bookmakers"][1]["markets"][0]["last_update"] = (NOW - timedelta(minutes=20)).isoformat()
        self.assertEqual(find_arbs([ev], CFG, NOW), [])           # near kickoff: 20 min is too old
        ev["commence_time"] = "2026-10-04T12:00:00Z"              # tomorrow
        ev["bookmakers"][1]["markets"][0]["last_update"] = (NOW - timedelta(hours=2)).isoformat()
        self.assertEqual(len(find_arbs([ev], CFG, NOW)), 1)       # a day out: a 2h-old line is fine
        ev["bookmakers"][1]["markets"][0]["last_update"] = (NOW - timedelta(hours=4)).isoformat()
        self.assertEqual(find_arbs([ev], CFG, NOW), [])           # ...but not a 4h-old one

    def test_profit_filters_and_live_only(self):
        ev = event({
            "A": [("h2h", [("Home", 2.10, None), ("Away", 1.80, None)])],
            "B": [("h2h", [("Home", 1.70, None), ("Away", 2.15, None)])],
        })  # ~6.2% arb, not started yet
        self.assertEqual(find_arbs([ev], Config(min_profit_pct=10.0), NOW), [])
        self.assertEqual(find_arbs([ev], Config(min_profit_pct=0, live_only=True), NOW), [])
        ev["commence_time"] = "2026-10-03T11:00:00Z"
        [arb] = find_arbs([ev], Config(min_profit_pct=0, live_only=True), NOW)
        self.assertTrue(arb.is_live)

    def test_arb_skip_lines(self):
        [arb] = find_arbs(demo_events(), Config())
        worst = arb.worst_ok_price(0)          # the first leg, with the stakes as printed
        # At the skip-line price, the printed stake still returns total + 0.5%, rounded up to a
        # price the card can show (never one that would keep less).
        exact = arb.total_stake * 1.005 / arb.legs[0].stake
        self.assertGreaterEqual(worst, exact - 1e-9)
        self.assertLess(worst - exact, 0.01)
        self.assertLess(worst, arb.legs[0].price)
        arb.legs[0].stake = 10                 # a stake that can't cover the total at any lower price
        self.assertIsNone(arb.worst_ok_price(0))

    def test_guide(self):
        from arbbot import guide_payload
        g = guide_payload()["embeds"][0]
        for word in ("ARB", "+EV", "OUTLIER", "GONE", "skip"):
            self.assertIn(word, g["description"])

    def test_live_arb_needs_fresh_prices_on_both_sides(self):
        ev = event({
            "A": [("h2h", [("Home", 2.10, None), ("Away", 1.80, None)])],
            "B": [("h2h", [("Home", 1.70, None), ("Away", 2.15, None)])],
        }, start="2026-10-03T11:00:00Z")
        cfg = Config(min_live_profit_pct=0, round_stakes=0)
        self.assertEqual(len(find_arbs([ev], cfg, NOW)), 1)
        ev["bookmakers"][1]["markets"][0]["last_update"] = (NOW - timedelta(seconds=90)).isoformat()
        self.assertEqual(find_arbs([ev], cfg, NOW), [])                       # 90s apart: a lagging line
        self.assertEqual(len(find_arbs([ev], replace(cfg, live_arb_max_skew=0), NOW)), 1)

    def test_live_arb_controls(self):
        ev = event({
            "A": [("h2h", [("Home", 2.02, None), ("Away", 1.90, None)])],
            "B": [("h2h", [("Home", 1.90, None), ("Away", 2.02, None)])],
        }, start="2026-10-03T11:00:00Z")   # live, ~1% exact edge
        self.assertEqual(find_arbs([ev], Config(min_live_profit_pct=1.5), NOW), [])
        self.assertEqual(len(find_arbs([ev], Config(min_live_profit_pct=0.5, round_stakes=0), NOW)), 1)
        self.assertEqual(find_arbs([ev], Config(min_live_profit_pct=0, arb_live=False), NOW), [])

    def test_sharp_book_never_an_arb_leg(self):
        # Pinnacle's price would make an arb, but US bettors can't bet there.
        ev = event({
            "Pinnacle": [("h2h", [("Home", 2.20, None), ("Away", 1.75, None)])],
            "B": [("h2h", [("Home", 1.70, None), ("Away", 2.15, None)])],
        })
        ev["bookmakers"][0]["key"] = "pinnacle"
        self.assertEqual(find_arbs([ev], CFG, NOW), [])
        self.assertEqual(len(find_arbs([ev], Config(min_profit_pct=0, bet_at_sharp=True), NOW)), 1)

    def test_demo_data_has_one_arb(self):
        [arb] = find_arbs(demo_events(), Config())
        self.assertEqual(arb.matchup, "New York Knicks @ Boston Celtics")
        self.assertAlmostEqual(arb.exact_pct, 3.76, places=2)   # (1/(1/1.8+1/2.45)-1)*100
        self.assertAlmostEqual(arb.profit_pct, 3.50, places=2)  # $57.50 / $42.50 -> $103.50
        # Pinnacle has the Knicks at about +134 no-vig: DraftKings' +145 is the price that will move, so it's 1.
        self.assertEqual([(l.book, l.stake) for l in arb.legs], [("DraftKings", 42.5), ("FanDuel", 57.5)])
        desc = discord_payload(arb)["embeds"][0]["description"]
        self.assertTrue(desc.startswith("👉 **DO THIS NOW: place BOTH bets, 1️⃣ first."))
        self.assertIn("1️⃣ Open **DraftKings** → bet **$42.50** on **New York Knicks +145**", desc)
        self.assertIn("2️⃣ Open **FanDuel** → bet **$57.50** on **Boston Celtics -125**", desc)
        self.assertIn("skip if the price is worse than -133", desc)
        self.assertIn("get back at least **$103.50**", desc)


class Stakes(unittest.TestCase):
    def arb(self, round_to, keep=85):
        ev = event({
            "A": [("h2h", [("Home", 2.10, None), ("Away", 1.80, None)])],
            "B": [("h2h", [("Home", 1.70, None), ("Away", 2.15, None)])],
        })
        cfg = Config(min_profit_pct=0, bankroll=100, round_stakes=round_to, round_keep_pct=keep)
        return find_arbs([ev], cfg, NOW)[0]

    def test_rounded_to_five_when_it_keeps_enough(self):
        arb = self.arb(5, keep=50)
        self.assertTrue(all(l.stake % 5 == 0 for l in arb.legs))
        self.assertGreater(arb.guaranteed_profit, 0)

    def test_steps_down_to_keep_85_percent_of_edge(self):
        arb = self.arb(5)   # $5 rounding here keeps only ~80% of the 6.2% edge
        self.assertFalse(all(l.stake % 5 == 0 for l in arb.legs))
        self.assertGreaterEqual(arb.profit_pct, 0.85 * arb.exact_pct)

    def test_headline_is_edge_after_rounding(self):
        arb = self.arb(5, keep=50)
        self.assertLess(arb.profit_pct, arb.exact_pct)
        self.assertAlmostEqual(arb.profit_pct, arb.guaranteed_profit / arb.total_stake * 100)
        self.assertIn("with exact stakes", discord_payload(arb)["embeds"][0]["description"])

    def test_falls_back_when_rounding_kills_profit(self):
        ev = event({
            "A": [("h2h", [("Home", 1.50, None), ("Away", 2.50, None)])],
            "B": [("h2h", [("Home", 1.40, None), ("Away", 3.10, None)])],
        })  # ~1.1% arb on $20: exact 13.48/6.52; $5 or $1 rounding would lose money
        [arb] = find_arbs([ev], Config(min_profit_pct=0, bankroll=20, round_stakes=5), NOW)
        self.assertGreater(arb.guaranteed_profit, 0)
        self.assertFalse(all(l.stake % 5 == 0 for l in arb.legs))

    def test_links_carried_to_legs(self):
        ev = event({
            "A": [("h2h", [("Home", 2.10, None), ("Away", 1.80, None)])],
            "B": [("h2h", [("Home", 1.70, None), ("Away", 2.15, None)])],
        })
        ev["bookmakers"][0]["markets"][0]["outcomes"][0]["link"] = "https://a.example/slip/home"
        ev["bookmakers"][1]["link"] = "https://b.example/event"
        [arb] = find_arbs([ev], CFG, NOW)
        links = {l.book: l.link for l in arb.legs}
        self.assertEqual(links, {"A": "https://a.example/slip/home", "B": "https://b.example/event"})


class RestartMemory(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(webhook_url="https://main", state_dir=self.tmp.name, log_file="")
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def alerter(self, noun=None):
        a = Alerter(self.cfg, dry_run=False, noun=noun)
        def fake(payload, message_id=None, url=""):
            self.sent.append(("PATCH" if message_id else "POST", payload["embeds"][0]["title"]))
            return message_id or "m1"
        a._discord = fake
        return a

    def test_no_repost_after_restart(self):
        arbs = find_arbs(demo_events(), Config())
        self.alerter().handle(arbs, ["basketball_nba"], now=time.time())
        self.assertEqual([m for m, _ in self.sent], ["POST"])
        again = self.alerter()                                    # restart
        self.assertEqual(again.handle(arbs, ["basketball_nba"], now=time.time()), 0)  # no new alert
        self.assertEqual([m for m, _ in self.sent], ["POST"])      # nothing re-posted
        self.assertIn(arbs[0].key, again.open)

    def test_closed_while_down_marked_gone(self):
        arbs = find_arbs(demo_events(), Config())
        self.alerter().handle(arbs, ["basketball_nba"], now=time.time())
        again = self.alerter()
        again.started -= 1000                                     # past the grace period
        again.handle([], ["icehockey_nhl"], now=time.time())
        self.assertEqual(self.sent[-1][0], "PATCH")
        self.assertTrue(self.sent[-1][1].startswith("❌ GONE"))

    def test_state_files_kept_apart(self):
        arbs = find_arbs(demo_events(), Config())
        self.alerter().handle(arbs, ["basketball_nba"], now=time.time())
        self.assertEqual(self.alerter(noun="prop arbs").restored, {})


class LiveChannel(unittest.TestCase):
    def test_live_arbs_use_their_own_webhook(self):
        a = Alerter(Config(webhook_url="https://main", live_webhook_url="https://live"), dry_run=True)
        [arb] = find_arbs(demo_events(), Config())   # the demo NBA arb is live
        self.assertEqual(a.webhook_for(arb), "https://live")
        arb.is_live = False
        self.assertEqual(a.webhook_for(arb), "https://main")


class AlerterLifecycle(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(log_file=str(Path(self.tmp.name) / "arbs.csv"))
        self.alerter = Alerter(self.cfg, dry_run=True)
        self.arbs = find_arbs(demo_events(), Config())  # one NBA arb

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_then_same_then_gone(self):
        a = self.alerter
        self.assertEqual(a.handle(self.arbs, ["basketball_nba"], now=1000), 1)
        self.assertEqual(a.handle(self.arbs, ["basketball_nba"], now=1060), 0)  # still open, no new alert
        self.assertIn(self.arbs[0].key, a.open)
        a.handle([], ["basketball_nba"], now=1120)                              # gone
        self.assertEqual(a.open, {})
        rows = (Path(self.tmp.name) / "arbs.csv").read_text().splitlines()
        self.assertEqual(len(rows), 2)  # header + 1
        self.assertIn(",120,", rows[1])
        self.assertIn("1 arbs found", a.summary())

    def test_not_closed_when_its_sport_wasnt_checked(self):
        a = self.alerter
        a.handle(self.arbs, ["basketball_nba"], now=1000)
        a.handle([], ["icehockey_nhl"], now=1060)
        self.assertIn(self.arbs[0].key, a.open)

    def test_big_improvement_sends_fresh_alert(self):
        a = self.alerter
        a.handle(self.arbs, ["basketball_nba"], now=1000)
        celtics = next(l for l in self.arbs[0].legs if l.outcome == "Boston Celtics")
        celtics.price = 1.95                # 3.5% -> ~7.9%: jump of more than 2.5 points
        self.arbs[0].margin = 1 / 1.95 + 1 / 2.45
        self.arbs[0].set_stakes(100, 5)
        self.assertEqual(a.handle(self.arbs, ["basketball_nba"], now=1060), 1)
        self.assertEqual(a.handle(self.arbs, ["basketball_nba"], now=1120), 0)  # no repeat

    def test_price_change_updates_not_new(self):
        a = self.alerter
        a.handle(self.arbs, ["basketball_nba"], now=1000)
        self.arbs[0].legs[0].price += 0.05
        self.assertEqual(a.handle(self.arbs, ["basketball_nba"], now=1060), 0)
        self.assertEqual(a.open[self.arbs[0].key].arb.legs[0].price, self.arbs[0].legs[0].price)

    def test_gone_payload(self):
        p = discord_payload(self.arbs[0], mention="@everyone", gone_after=45)
        self.assertTrue(p["embeds"][0]["title"].startswith("❌ GONE after 45s"))
        self.assertNotIn("content", p)  # don't re-ping for a dead arb
        self.assertEqual(discord_payload(self.arbs[0], mention="@everyone")["content"], "@everyone")


def ev_event(sharp, books, start="2026-10-03T18:00:00Z", market="h2h"):
    """sharp: [(name, price, point)], books: {title: [(name, price, point)]}"""
    ev = event({"Pinnacle": [(market, sharp)], **{t: [(market, o)] for t, o in books.items()}}, start=start)
    ev["bookmakers"][0]["key"] = "pinnacle"
    return ev


EVCFG = Config(min_ev_pct=3, round_stakes=0, ev_bankroll=1000, kelly_fraction=0.25, confidence_stakes="1,1,1")


class PlusEV(unittest.TestCase):
    def test_devig(self):
        for method in ("multiplicative", "power"):
            p = devig([1.91, 1.91], method)
            self.assertAlmostEqual(p[0], 0.5)
            p = devig([1.70, 2.25], method)
            self.assertAlmostEqual(sum(p), 1)
            self.assertGreater(p[0], p[1])

    def test_power_devig_shades_long_shots(self):
        mult = devig([1.25, 4.50], "multiplicative")
        power = devig([1.25, 4.50], "power")
        self.assertAlmostEqual(sum(power), 1)
        self.assertLess(power[1], mult[1])   # long shot gets a lower fair probability

    def two_sharps(self, pin, bf, soft):
        ev = event({"Pinnacle": [("h2h", pin)], "Betfair": [("h2h", bf)], "B": [("h2h", soft)]})
        ev["bookmakers"][0]["key"], ev["bookmakers"][1]["key"] = "pinnacle", "betfair_ex_eu"
        return ev

    def test_blends_two_sharps(self):
        ev = self.two_sharps([("Home", 1.90, None), ("Away", 1.90, None)],
                             [("Home", 1.80, None), ("Away", 2.00, None)],
                             [("Home", 2.20, None)])
        cfg = Config(min_ev_pct=3, sharp_books="pinnacle,betfair_ex_eu", round_stakes=0)
        [b] = find_evs([ev], cfg, NOW)
        self.assertAlmostEqual(b.fair_prob, (0.5 + devig([1.80, 2.00], "power")[0]) / 2)
        self.assertEqual((b.sources_used, b.sources_total), (2, 2))
        self.assertEqual(b.sharp_book, "Pinnacle + Betfair")
        weighted = Config(min_ev_pct=3, sharp_books="pinnacle,betfair_ex_eu",
                          sharp_weights={"pinnacle": 3, "betfair_ex_eu": 1})
        [w] = find_evs([ev], weighted, NOW)
        self.assertAlmostEqual(w.fair_prob, (3 * 0.5 + devig([1.80, 2.00], "power")[0]) / 4)

    def test_sharps_disagree_suppresses(self):
        ev = self.two_sharps([("Home", 1.60, None), ("Away", 2.50, None)],
                             [("Home", 2.00, None), ("Away", 1.85, None)],
                             [("Home", 2.20, None)])
        cfg = Config(min_ev_pct=3, sharp_books="pinnacle,betfair_ex_eu")
        self.assertEqual(find_evs([ev], cfg, NOW), [])

    def test_single_source_stake_cut(self):
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.10, None)]})
        both = Config(min_ev_pct=3, round_stakes=0, sharp_books="pinnacle,betfair_ex_eu", confidence_stakes="1,1,1")
        [b] = find_evs([ev], both, NOW)
        self.assertEqual((b.sources_used, b.sources_total), (1, 2))
        self.assertEqual(b.stake, 6.0)   # half of the 11.4 one-sharp stake, rounded
        self.assertAlmostEqual(b.ev_pct, 5.0, places=1)   # the edge itself is unchanged

    def test_units_label(self):
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.10, None)]})
        [b] = find_evs([ev], Config(min_ev_pct=3, round_stakes=0, unit_size=10, confidence_stakes="1,1,1"), NOW)
        self.assertEqual(b.stake_label, "$11 (1.1u)")

    def test_finds_price_above_fair(self):
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)],
                      {"B": [("Home", 2.10, None), ("Away", 1.75, None)]})
        [b] = find_evs([ev], EVCFG, NOW)
        self.assertEqual((b.outcome, b.book, b.price), ("Home", "B", 2.10))
        self.assertAlmostEqual(b.ev_pct, 5.0, places=1)    # 0.5 * 2.10 - 1
        self.assertAlmostEqual(b.fair_odds, 2.0)

    def test_best_book_wins_others_listed(self):
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)],
                      {"B": [("Home", 2.10, None)], "C": [("Home", 2.15, None)], "D": [("Home", 2.0, None)]})
        [b] = find_evs([ev], EVCFG, NOW)
        self.assertEqual(b.book, "C")
        self.assertEqual(b.also, [("B", 2.10)])  # D's 2.0 is no edge

    def test_needs_sharp_reference(self):
        ev = event({"B": [("h2h", [("Home", 2.5, None), ("Away", 1.5, None)])]})
        self.assertEqual(find_evs([ev], EVCFG, NOW), [])

    def test_lines_must_match(self):
        ev = ev_event([("Home", 1.91, -3.5), ("Away", 1.91, 3.5)],
                      {"B": [("Home", 2.30, -7.5), ("Away", 1.60, 7.5)]}, market="spreads")
        self.assertEqual(find_evs([ev], EVCFG, NOW), [])

    def test_filters(self):
        ev = ev_event([("Home", 1.30, None), ("Away", 4.50, None)],
                      {"B": [("Away", 6.0, None)]})   # long shot over EV_MAX_ODDS
        self.assertEqual(find_evs([ev], EVCFG, NOW), [])
        live = ev_event([("Home", 1.91, None), ("Away", 1.91, None)],
                        {"B": [("Home", 2.10, None)]}, start="2026-10-03T11:00:00Z")
        self.assertEqual(find_evs([live], EVCFG, NOW), [])           # pre-game only by default
        self.assertEqual(len(find_evs([live], Config(min_ev_pct=3, ev_live=True), NOW)), 1)
        self.assertEqual(find_evs([live], Config(ev_enabled=False, ev_live=True), NOW), [])

    def test_kelly_stake(self):
        # fair 50%, price 2.10: full Kelly = 0.05/1.10 = 4.55% -> quarter = 1.14% of $1000
        self.assertEqual(kelly_stake(0.5, 2.10, EVCFG), 11.0)
        self.assertEqual(kelly_stake(0.5, 2.10, Config(round_stakes=5, ev_bankroll=1000)), 10.0)
        capped = Config(round_stakes=0, ev_bankroll=1000, kelly_fraction=1, ev_max_stake_pct=3)
        self.assertEqual(kelly_stake(0.6, 2.10, capped), 30.0)
        # Lower confidence shrinks the capped stake too (cap first, then the multiplier).
        self.assertEqual(kelly_stake(0.6, 2.10, capped, mult=0.5), 15.0)
        # Rounding never goes over the cap: $24 cap rounds down to $20, not up to $25.
        odd_cap = Config(round_stakes=5, ev_bankroll=1000, kelly_fraction=1, ev_max_stake_pct=2.4)
        self.assertEqual(kelly_stake(0.6, 2.10, odd_cap), 20.0)

    def test_fingerprint_tracks_what_the_card_shows(self):
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.10, None)]})
        [b] = find_evs([ev], EVCFG, NOW)
        self.assertNotEqual(b.fingerprint, replace(b, fair_prob=b.fair_prob + 0.01).fingerprint)
        self.assertNotEqual(b.fingerprint, replace(b, stake=b.stake + 5).fingerprint)
        self.assertEqual(b.fingerprint, replace(b).fingerprint)

    def test_american_odds(self):
        from arbbot import american
        self.assertEqual(american(2.52), "+152")
        self.assertEqual(american(1.65), "-154")
        self.assertEqual(american(2.0), "+100")
        self.assertEqual(american(1.909), "-110")

    def test_sharp_quotes_and_board(self):
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)],
                      {"B": [("Home", 2.10, None)], "C": [("Home", 1.95, None)], "D": [("Home", 1.80, None)]})
        [b] = find_evs([ev], EVCFG, NOW)
        self.assertEqual(b.sharp_quotes, [("Pinnacle", [1.91, 1.91])])
        self.assertEqual([r[0] for r in b.board], ["B", "C", "D"])   # every book, best price first
        self.assertAlmostEqual(b.board[2][2], (0.5 * 1.80 - 1) * 100)  # negative EV shown too
        desc = ev_payload(b)["embeds"][0]["description"]
        self.assertIn("**Pinnacle** -110 / -110", desc)
        self.assertIn("🟢 B — **+110** · +5.0%", desc)
        self.assertIn("⚪ D — **-125**", desc)

    def test_every_book_row_links_to_its_bet(self):
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)],
                      {"B": [("Home", 2.10, None)], "C": [("Home", 1.95, None)]})
        ev["bookmakers"][1]["markets"][0]["outcomes"][0]["link"] = "https://b.example/slip"
        ev["bookmakers"][2]["link"] = "https://c.example/game"
        [b] = find_evs([ev], EVCFG, NOW)
        p = ev_payload(b)["embeds"][0]
        self.assertEqual(p["url"], "https://b.example/slip")              # title opens the best bet
        self.assertIn("[B](https://b.example/slip) — **+110**", p["description"])
        self.assertIn("[C](https://c.example/game) — **-105**", p["description"])

    def test_fair_movement_carried(self):
        ev1 = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})
        ev2 = ev_event([("Home", 1.87, None), ("Away", 1.95, None)], {"B": [("Home", 2.20, None)]})
        [b1], [b2] = find_evs([ev1], EVCFG, NOW), find_evs([ev2], EVCFG, NOW)
        a = EVAlerter(temp_data(self, EVCFG), dry_run=True)
        a.handle([b1], now=1000)
        a.handle([b2], now=1060)
        cur = a.open[b2.key].arb
        self.assertAlmostEqual(cur.first_fair_prob, 0.5)
        self.assertIn("→", ev_payload(cur)["embeds"][0]["description"])

    def test_feed_freshness(self):
        from arbbot import feed_freshness
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.10, None)]})
        self.assertIn("pre-game: half of prices under 0s", feed_freshness([ev], NOW))

    def test_confidence_rating(self):
        from arbbot import rate_confidence
        self.assertEqual(rate_confidence(2.5, 1.0, 3, 5, False)[0], "high")
        self.assertEqual(rate_confidence(5.0, 4.0, 3, 5, False)[0], "medium")
        low, notes = rate_confidence(7.5, 8.0, 30, 15, False)
        self.assertEqual(low, "low")
        self.assertTrue(any("disagree" in n for n in notes))
        self.assertEqual(rate_confidence(6.0, None, 3, 5, True)[0], "high")   # props allow more margin

    def market_event(self, pin, others, soft=2.20):
        books = {"Pinnacle": [("h2h", pin)], "Soft": [("h2h", [("Home", soft, None)])]}
        for i, (h, a) in enumerate(others):
            books[f"B{i}"] = [("h2h", [("Home", h, None), ("Away", a, None)])]
        ev = event(books)
        ev["bookmakers"][0]["key"] = "pinnacle"
        return ev

    def test_skips_wide_sharp_market(self):
        ev = self.market_event([("Home", 1.80, None), ("Away", 1.80, None)], [])   # 11% margin
        self.assertEqual(find_evs([ev], Config(min_ev_pct=3), NOW), [])

    def test_skips_when_market_disagrees_with_sharp(self):
        # Pinnacle says 50/50; three other books all say Home ~67%: Pinnacle is probably stale.
        ev = self.market_event([("Home", 1.97, None), ("Away", 1.97, None)],
                               [(1.45, 2.85), (1.47, 2.80), (1.46, 2.82)])
        self.assertEqual(find_evs([ev], Config(min_ev_pct=3, max_ev_pct=100), NOW), [])

    def test_confidence_scales_stake_and_shows(self):
        ev = self.market_event([("Home", 1.97, None), ("Away", 1.97, None)],
                               [(1.95, 1.95), (1.96, 1.94), (1.94, 1.96)])
        [b] = find_evs([ev], Config(min_ev_pct=3, round_stakes=0), NOW)
        self.assertEqual(b.confidence, "high")
        self.assertIn("Confidence: **🟢 High**", ev_payload(b)["embeds"][0]["description"])
        lowcfg = Config(min_ev_pct=3, round_stakes=0, min_confidence="high")
        wide = self.market_event([("Home", 1.88, None), ("Away", 1.88, None)], [])     # ~6.4% margin
        self.assertEqual(find_evs([wide], lowcfg, NOW), [])

    def test_sharp_movement_changes_confidence(self):
        from arbbot import SharpHistory, rate_confidence
        self.assertEqual(rate_confidence(5.0, 4.0, 3, 5, False)[0], "medium")           # 4 points
        self.assertEqual(rate_confidence(5.0, 4.0, 3, 5, False, move=2.5)[0], "high")    # +1: toward us
        self.assertEqual(rate_confidence(5.0, 7.0, 3, 5, False)[0], "medium")           # 3 points
        self.assertEqual(rate_confidence(5.0, 7.0, 3, 5, False, move=-2.5)[0], "low")    # -1: away from us
        self.assertEqual(rate_confidence(5.0, 7.0, 3, 5, False, move=-1.0)[0], "medium") # small moves ignored
        h = SharpHistory(60)
        self.assertIsNone(h.record(("e", "h2h", None, "Home"), NOW, 0.50))
        self.assertAlmostEqual(h.record(("e", "h2h", None, "Home"), NOW + timedelta(minutes=20), 0.53), 3.0)
        # 90 min later both readings are over an hour old: no recent movement to report.
        self.assertIsNone(h.record(("e", "h2h", None, "Home"), NOW + timedelta(minutes=90), 0.53))

    def test_history_flows_into_alerts(self):
        from arbbot import SharpHistory
        h = SharpHistory(60)
        early = ev_event([("Home", 2.02, None), ("Away", 1.82, None)], {"B": [("Home", 2.20, None)]})
        find_evs([early], EVCFG, NOW - timedelta(minutes=30), history=h)     # Pinnacle had Home ~47%
        later = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})
        [b] = find_evs([later], EVCFG, NOW, history=h)                      # now 50%: moving toward Home
        self.assertTrue(any("moving this way" in n for n in b.confidence_notes))

    def test_sport_minimum_edge(self):
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.10, None)]})  # 5% edge
        ev["sport_key"] = "americanfootball_ncaaf"
        self.assertEqual(find_evs([ev], Config(min_ev_pct=3), NOW), [])          # college needs 6%
        ev["sport_key"] = "americanfootball_nfl"
        self.assertEqual(len(find_evs([ev], Config(min_ev_pct=3), NOW)), 1)

    def test_same_game_alerts_flagged(self):
        from arbbot import note_related
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})
        [ml] = find_evs([ev], EVCFG, NOW)
        sp = replace(ml, market="spreads", line=-3.5, point=-3.5)
        other = replace(ml, event_id="e2")
        a = EVAlerter(EVCFG, dry_run=True)
        note_related([([ml, sp, other], a, set())])
        self.assertEqual(ml.related, ["Home -3.5"])
        self.assertEqual(other.related, [])
        self.assertIn("Also alerted on this game: Home -3.5", ev_payload(ml)["embeds"][0]["description"])

    def test_related_only_names_alerts_that_will_exist(self):
        from arbbot import note_related
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})
        [ml] = find_evs([ev], EVCFG, NOW)
        over = replace(ml, market="totals", line=220.5, point=220.5, outcome="Over")
        # Hourly cap with one slot left: only the first (best) bet will be posted.
        a = EVAlerter(EVCFG, dry_run=True)
        a.max_per_hour, a.posted_at = 2, [time.time()]
        note_related([([over, ml], a, {ml.sport_key})])
        self.assertEqual(over.related, [])           # ML will be skipped by the cap, so not named
        # An open alert that this scan re-checked and didn't find is about to close: not named.
        b = EVAlerter(temp_data(self, EVCFG), dry_run=True)
        b.handle([ml], now=1000)
        note_related([([over], b, {ml.sport_key})])
        self.assertEqual(over.related, [])
        # ...but an open alert this scan didn't look at stays open, so it is named.
        note_related([([over], b, set())])
        self.assertEqual(over.related, [ml.pick])

    def test_payload(self):
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.10, None)]})
        [b] = find_evs([ev], EVCFG, NOW)
        self.assertEqual("📈 +EV 5.0% · Home ML +110 at B", ev_payload(b)["embeds"][0]["title"])
        desc = ev_payload(b)["embeds"][0]["description"]
        self.assertTrue(desc.startswith("👉 **DO THIS: bet this ONE side.**"))
        self.assertIn("Open **B** → bet **$11** on **Home ML +110**", desc)
        self.assertIn("skip if the price is worse than **+103**", desc)   # 1.5% edge vs fair +100
        self.assertTrue(ev_payload(b, gone_after=30)["embeds"][0]["title"].startswith("❌ GONE"))


def bet(market, outcome, point="", price=2.0, stake=10, n=2):
    return {"home_team": "Home", "away_team": "Away", "market": market, "outcome": outcome,
            "point": point, "price": price, "stake": stake, "n_outcomes": n}


class Grading(unittest.TestCase):
    def test_moneyline(self):
        self.assertEqual(settle(bet("h2h", "Home"), 100, 90), ("win", 10.0))
        self.assertEqual(settle(bet("h2h", "Away"), 100, 90), ("loss", -10.0))
        self.assertEqual(settle(bet("h2h", "Home"), 20, 20), ("push", 0.0))
        self.assertEqual(settle(bet("h2h", "Home", n=3), 1, 1), ("loss", -10.0))
        self.assertEqual(settle(bet("h2h", "Draw", price=3.4, n=3), 1, 1), ("win", 24.0))

    def test_spread(self):
        self.assertEqual(settle(bet("spreads", "Home", -3.5), 100, 97)[0], "loss")
        self.assertEqual(settle(bet("spreads", "Home", -3.5), 100, 96)[0], "win")
        self.assertEqual(settle(bet("spreads", "Away", 3.0), 100, 97)[0], "push")
        self.assertEqual(settle(bet("spreads", "Away", 3.5), 100, 97)[0], "win")

    def test_total(self):
        self.assertEqual(settle(bet("totals", "Over", 200.5), 100, 101)[0], "win")
        self.assertEqual(settle(bet("totals", "Under", 200.5), 100, 101)[0], "loss")
        self.assertEqual(settle(bet("totals", "Under", 201), 100, 101)[0], "push")


class EVLifecycle(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg = Config(min_ev_pct=3, ev_log_file=str(d / "ev.csv"), ev_results_file=str(d / "res.csv"),
                          outlier_log_file=str(d / "out.csv"), closing_file=str(d / "close.csv"),
                          markout_file=str(d / "mk.csv"), parlay_log_file=str(d / "par.csv"),
                          score_check_file=str(d / "sc.csv"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_log_grade_record(self):
        from datetime import datetime as dt
        start = (dt.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.10, None)]},
                      start=start)
        ev["sport_key"] = "basketball_nba"
        [b] = find_evs([ev], Config(min_ev_pct=3, max_age_seconds=10**9, pregame_max_age_seconds=10**9),
                       _parse(start) - timedelta(hours=1))
        a = EVAlerter(self.cfg, dry_run=True)
        a.handle([b], ["basketball_nba"], now=1000)   # logged right away
        self.assertEqual(len(Path(self.cfg.ev_log_file).read_text().splitlines()), 2)
        a.handle([b], ["basketball_nba"], now=1060)   # same bet again: no duplicate
        a.handle([], ["basketball_nba"], now=1120)    # gone
        a.handle([b], ["basketball_nba"], now=2000)   # reappears -> logged twice in ev.csv...
        self.assertEqual(len(Path(self.cfg.ev_log_file).read_text().splitlines()), 3)

        class Scores:
            calls = 0
            def scores(self, sport):
                Scores.calls += 1
                return [{"id": "e1", "completed": True,
                         "scores": [{"name": "Home", "score": "110"}, {"name": "Away", "score": "100"}]}]
        self.assertEqual(len(settle_pending(self.cfg, Scores())), 1)   # ...but graded once
        self.assertEqual(settle_pending(self.cfg, Scores()), [])       # nothing left; no extra API call
        self.assertEqual(Scores.calls, 1)
        rec = ev_record(self.cfg)
        self.assertTrue(rec.startswith("1 bets, 1-0-0, +$"), rec)


def _parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def outlier_event(stale_home, stale_away, start="2026-10-03T11:00:00Z"):
    books = {"Pinnacle": [("h2h", [("Home", 1.25, None), ("Away", 4.10, None)])],
             "DK": [("h2h", [("Home", 1.24, None), ("Away", 4.00, None)])],
             "FD": [("h2h", [("Home", 1.26, None), ("Away", 3.90, None)])],
             "MGM": [("h2h", [("Home", 1.25, None), ("Away", 3.95, None)])],
             "Stale": [("h2h", [("Home", stale_home, None), ("Away", stale_away, None)])]}
    ev = event(books, start=start)
    ev["bookmakers"][0]["key"] = "pinnacle"
    return ev


class Outliers(unittest.TestCase):
    def test_finds_book_far_off_market_live(self):
        [o] = find_outliers([outlier_event(1.85, 1.95)], Config(), NOW)
        self.assertEqual((o.book, o.outcome, o.price), ("Stale", "Home", 1.85))
        self.assertGreater(o.ev_pct, 40)
        self.assertEqual(o.sources_used, 4)   # compared with the 4 other books

    def test_no_upper_cap_and_hedge(self):
        # Your example: one book has the favorite at +400 while the market has it near -400.
        [o] = find_outliers([outlier_event(5.0, 1.15)], Config(), NOW)
        self.assertEqual((o.outcome, o.price), ("Home", 5.0))
        self.assertGreater(o.ev_pct, 100)   # far beyond the old 25% "too good to be true" cap
        outs = find_outliers([outlier_event(1.85, 1.95)], Config(), NOW)
        self.assertTrue(outs[0].hedge)
        self.assertEqual(outs[0].hedge[0][:2], ("Away", "DK"))   # best other-side price, not Pinnacle
        self.assertAlmostEqual(outs[0].hedge_pct, (1 / (1 / 1.85 + 1 / 4.00) - 1) * 100)
        title = outlier_payload(outs[0])["embeds"][0]["title"]
        self.assertTrue(title.startswith("🚨 OUTLIER"))
        d = outlier_payload(outs[0])["embeds"][0]["description"]
        self.assertTrue(d.startswith("👉 **DO THIS NOW, before Stale fixes its price.**"))
        self.assertIn("Want a sure profit instead?", d)

    def test_needs_enough_books_and_edge(self):
        ev = outlier_event(1.30, 3.60)                     # only slightly off: not an outlier
        self.assertEqual(find_outliers([ev], Config(), NOW), [])
        self.assertEqual(find_outliers([outlier_event(1.85, 1.95)], Config(outlier_min_books=5), NOW), [])
        self.assertEqual(find_outliers([outlier_event(1.85, 1.95)], Config(outlier_live=False), NOW), [])
        self.assertEqual(find_outliers([outlier_event(1.85, 1.95)], Config(outliers_enabled=False), NOW), [])

    def test_two_books_off_market_on_one_bet_keeps_the_best(self):
        ev = outlier_event(1.85, 1.95)
        second = {"key": "stale2", "title": "Stale2", "last_update": FRESH, "markets": [
            {"key": "h2h", "last_update": FRESH, "outcomes": [
                {"name": "Home", "price": 1.60, "point": None}, {"name": "Away", "price": 2.30, "point": None}]}]}
        ev["bookmakers"].append(second)
        outs = find_outliers([ev], Config(), NOW)
        home = [o for o in outs if o.outcome == "Home"]
        self.assertEqual(len(home), 1)                       # one alert per bet...
        self.assertEqual(home[0].book, "Stale")               # ...at the best price (1.85 beats 1.60)
        self.assertEqual(home[0].also, [("Stale2", 1.60)])
        a = OutlierAlerter(temp_data(self, Config()), dry_run=True)
        a.handle(outs, now=1000)
        self.assertEqual(a.open[home[0].key].arb.book, "Stale")   # not overwritten by the worse book

    def test_sharp_book_itself_never_flagged(self):
        ev = outlier_event(1.25, 4.00)
        ev["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = 1.90   # Pinnacle is the odd one
        self.assertEqual(find_outliers([ev], Config(), NOW), [])

    def test_ev_alert_suppressed_when_outlier_covers_it(self):
        ev = outlier_event(1.85, 1.95, start="2026-10-03T18:00:00Z")    # pre-game: both would fire
        outs = find_outliers([ev], Config(), NOW)
        evs = find_evs([ev], Config(min_ev_pct=3, max_ev_pct=100, ev_max_odds=10), NOW)
        self.assertTrue(evs)
        self.assertEqual(without_outliers(evs, outs), [])


class ArbDollarMinimum(unittest.TestCase):
    def test_min_profit_dollars(self):
        ev = event({
            "A": [("h2h", [("Home", 2.10, None), ("Away", 1.80, None)])],
            "B": [("h2h", [("Home", 1.70, None), ("Away", 2.15, None)])],
        })   # ~6% edge -> about $6 on $100
        self.assertEqual(len(find_arbs([ev], Config(min_profit_pct=0, min_profit_dollars=5), NOW)), 1)
        self.assertEqual(find_arbs([ev], Config(min_profit_pct=0, min_profit_dollars=10), NOW), [])
        self.assertEqual(len(find_arbs([ev], Config(min_profit_pct=0, min_profit_dollars=10,
                                                    bankroll=300), NOW)), 1)


class CLV(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg = Config(min_ev_pct=3, ev_log_file=str(d / "ev.csv"), ev_results_file=str(d / "res.csv"),
                          outlier_log_file=str(d / "out.csv"), closing_file=str(d / "close.csv"),
                          markout_file=str(d / "mk.csv"),
                          pregame_max_age_seconds=10**9)

    def tearDown(self):
        self.tmp.cleanup()

    def game(self, home_price, away_price, start):
        ev = ev_event([("Home", home_price, None), ("Away", away_price, None)],
                      {"B": [("Home", 2.20, None)]}, start=start)
        ev["sport_key"] = "basketball_nba"
        return ev

    def test_clv_math(self):
        self.assertAlmostEqual(clv_pct(2.20, 0.5), 10.0)    # got +120, closed fair at +100
        self.assertAlmostEqual(clv_pct(2.20, 0.40), -12.0)  # line moved away from you

    def test_tracks_until_kickoff_then_saves_close(self):
        from datetime import datetime as dt
        start_dt = dt.now(timezone.utc) + timedelta(hours=1)
        start = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        a = EVAlerter(self.cfg, dry_run=True)
        tr = ClosingTracker(self.cfg)
        a.on_log = tr.add
        [b] = find_evs([self.game(1.91, 1.91, start)], self.cfg, start_dt - timedelta(hours=1))
        a.handle([b], now=1000)                                   # logged -> tracked
        self.assertEqual(tr.needs_close(), {"e1"})
        tr.observe([self.game(1.91, 1.91, start)], start_dt - timedelta(minutes=30))
        tr.observe([self.game(1.80, 2.05, start)], start_dt - timedelta(minutes=3))  # line moves our way
        self.assertEqual(tr.finalize(start_dt - timedelta(minutes=1)), 0)   # not started yet
        self.assertEqual(tr.finalize(start_dt + timedelta(minutes=1)), 1)   # kickoff: saved
        self.assertEqual(tr.needs_close(), set())
        [row] = clv_rows(self.cfg)
        closing_p = devig([1.80, 2.05], "power")[0]
        self.assertAlmostEqual(row["clv_pct"], clv_pct(2.20, closing_p), places=2)
        self.assertTrue(row["beat_close"])
        self.assertIn("beat the close on 100% of 1 bets", clv_record(self.cfg))
        # A restart reloads nothing for a game that already has its close saved.
        self.assertEqual(ClosingTracker(self.cfg).tracked, {})

    def test_restart_reloads_pending_bets(self):
        from datetime import datetime as dt
        start_dt = dt.now(timezone.utc) + timedelta(hours=2)
        start = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        a = EVAlerter(self.cfg, dry_run=True)
        [b] = find_evs([self.game(1.91, 1.91, start)], self.cfg, start_dt - timedelta(hours=1))
        a.handle([b], now=1000)
        self.assertEqual(ClosingTracker(self.cfg).needs_close(), {"e1"})

    def test_live_bets_have_no_close(self):
        tr = ClosingTracker(self.cfg)
        tr.add({"event_id": "e1", "market": "h2h", "outcome": "Home", "point": "",
                "first_seen": "2026-10-03T12:00:00+00:00", "commence_time": "2026-10-03T11:00:00Z"})
        self.assertEqual(tr.tracked, {})

    def test_breakdown_and_summary(self):
        from arbbot import clv_breakdown, clv_report, summary_payload
        from datetime import datetime as dt
        start_dt = dt.now(timezone.utc) + timedelta(hours=1)
        start = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        a = EVAlerter(self.cfg, dry_run=True)
        tr = ClosingTracker(self.cfg)
        a.on_log = tr.add
        [b] = find_evs([self.game(1.91, 1.91, start)], self.cfg, start_dt - timedelta(hours=1))
        a.handle([b], now=1000)
        tr.observe([self.game(1.80, 2.05, start)], start_dt - timedelta(minutes=3))
        tr.finalize(start_dt + timedelta(minutes=1))
        g = clv_breakdown(self.cfg)
        self.assertEqual(g["Market"][0][0], "Moneylines")
        self.assertEqual(g["Book"][0][:2], ("B", 1))
        self.assertIn("Moneylines", clv_report(self.cfg))
        card = summary_payload(self.cfg, [("💰 **Arbs**", "2 arbs found")], 5000)["embeds"][0]
        self.assertIn("Bet quality (CLV)", card["description"])
        self.assertIn("✅ Beating the closing line", card["description"])
        self.assertIn("💳 **Credits**", card["description"])

    def test_last_check_before_kickoff(self):
        cfg = Config(sports=["basketball_nba"], closing_minutes=5)
        s = sched_with({"basketball_nba": [("g1", NOW + timedelta(minutes=4))]}, cfg)
        self.assertFalse(s.closing_due("basketball_nba", NOW))          # no logged bet on it
        s.need_close = {"g1"}
        self.assertTrue(s.closing_due("basketball_nba", NOW))
        s.last_odds["basketball_nba"] = s.odds_ok["basketball_nba"] = (NOW - timedelta(minutes=0.5)).timestamp()
        self.assertFalse(s.closing_due("basketball_nba", NOW))          # already looked recently
        # A look that failed doesn't count: it's tried again a minute later.
        s.odds_ok["basketball_nba"] = 0
        s.last_odds["basketball_nba"] = time.time() - 30
        self.assertFalse(s.closing_due("basketball_nba", NOW))
        s.last_odds["basketball_nba"] = time.time() - 61
        self.assertTrue(s.closing_due("basketball_nba", NOW))
        s.cfg = replace(cfg, live_only=True)                             # LIVE_ONLY: no pre-game checks
        self.assertFalse(s.closing_due("basketball_nba", NOW))


def prop_event(books, start="2026-10-03T18:00:00Z", market="player_points", player="LeBron James", line=25.5):
    """books: {title: (over_price, under_price)}"""
    ev = {"id": "p1", "sport_key": "basketball_nba", "sport_title": "NBA", "commence_time": start,
          "home_team": "Lakers", "away_team": "Celtics", "bookmakers": []}
    for title, (o, u) in books.items():
        ev["bookmakers"].append({"key": title.lower(), "title": title, "last_update": FRESH, "markets": [
            {"key": market, "last_update": FRESH, "outcomes": [
                {"name": "Over", "description": player, "price": o, "point": line},
                {"name": "Under", "description": player, "price": u, "point": line}]}]})
    return ev


class Props(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(market_label("player_points", ("LeBron James", 25.5)), "LeBron James · Points 25.5")
        self.assertEqual(market_label("spreads", -3.5), "Spread -3.5")
        self.assertEqual(market_label("h2h", None), "Moneyline")

    def test_prop_ev_from_pinnacle(self):
        ev = prop_event({"Pinnacle": (1.91, 1.91), "DK": (2.15, 1.70)})
        ev["bookmakers"][0]["key"] = "pinnacle"
        [b] = find_evs([ev], Config().for_props(), NOW)
        self.assertEqual(b.pick, "LeBron James Over 25.5 Points")
        self.assertAlmostEqual(b.ev_pct, 7.5, places=1)
        self.assertEqual(b.player, "LeBron James")

    def test_players_never_mixed(self):
        ev = prop_event({"Pinnacle": (1.91, 1.91)})
        ev["bookmakers"][0]["key"] = "pinnacle"
        other = prop_event({"DK": (2.30, 1.60)}, player="Anthony Davis")["bookmakers"][0]
        ev["bookmakers"].append(other)
        self.assertEqual(find_evs([ev], Config().for_props(), NOW), [])

    def test_consensus_when_no_sharp(self):
        ev = prop_event({"A": (1.87, 1.95), "B": (1.91, 1.91), "C": (1.95, 1.87), "D": (1.89, 1.93),
                         "E": (2.20, 1.68)})
        [b] = find_evs([ev], Config().for_props(), NOW)
        self.assertEqual((b.book, b.outcome), ("E", "Over"))
        self.assertEqual(b.sharp_book, "consensus of A, B, C, D")   # E is left out of its own median
        self.assertEqual(find_evs([ev], Config(), NOW), [])   # main-line settings: no consensus
        few = prop_event({"A": (1.91, 1.91), "E": (2.20, 1.68)})
        self.assertEqual(find_evs([few], Config().for_props(), NOW), [])   # needs 4+ books

    def test_prop_arb(self):
        ev = prop_event({"A": (2.15, 1.70), "B": (1.70, 2.15)})
        [arb] = find_arbs([ev], Config(min_profit_pct=0), NOW)
        desc = discord_payload(arb)["embeds"][0]["description"]
        self.assertIn("LeBron James Over 25.5", desc)
        self.assertIn("LeBron James · Points 25.5", desc)

    def test_prop_bet_ids_and_closing_line_key(self):
        from arbbot import _bet_id, _row_line
        row = {"event_id": "p1", "market": "player_points", "outcome": "Over", "point": "25.5",
               "player": "LeBron James", "home_team": "Lakers"}
        self.assertEqual(_bet_id(row), "p1|player_points|LeBron James|Over|25.5")
        self.assertEqual(_row_line(row), ("LeBron James", 25.5))
        old = {"event_id": "e1", "market": "h2h", "outcome": "Home", "point": ""}
        self.assertEqual(_bet_id(old), "e1|h2h|Home|")   # unchanged for main lines

    def test_csv_gains_new_columns_in_place(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "log.csv")
            append_csv(f, ["a", "b"], {"a": 1, "b": 2})
            append_csv(f, ["a", "b", "player"], {"a": 3, "b": 4, "player": "X"})
            lines = Path(f).read_text().splitlines()
            self.assertEqual(lines, ["a,b,player", "1,2,", "3,4,X"])

    def test_props_schedule_and_budget(self):
        cfg = Config(sports=["basketball_nba"], prop_minutes=30, prop_hours=3)
        s = sched_with({"basketball_nba": [("g1", NOW + timedelta(hours=2)), ("g2", NOW + timedelta(hours=5))]}, cfg)
        self.assertEqual(s.props_due(NOW), [("basketball_nba", "g1"), ("basketball_nba", "g2")])
        s.last_props["g1"] = s.last_props["g2"] = time.time()
        self.assertEqual(s.props_due(NOW), [])
        s.last_props["g2"] = time.time() - 31 * 60
        self.assertEqual(s.props_due(NOW), [])          # g2 is 5h out: early tier, every 4h
        s.last_props["g1"] = time.time() - 31 * 60
        self.assertEqual(s.props_due(NOW), [("basketball_nba", "g1")])   # g1 is 2h out: every 30m
        self.assertEqual(cfg.prop_credits_per_call("basketball_nba"), 4)  # 4 markets, 10 books = 1 region
        s.update_budget(NOW)
        with_props = s.forecast
        s.cfg = Config(sports=["basketball_nba"], props_enabled=False)
        s.update_budget(NOW)
        # Near kickoff (every 30m): g1 2h + g2 3h = 5h x 2/h x 4 = 40.
        # Early (every 4h, from 24h out): g1 0h left, g2 2h (5h-3h) = 2h x 0.25/h x 4 = 2.
        self.assertAlmostEqual(with_props - s.forecast, 42, delta=2)

    def test_prop_alerts_close_only_when_their_game_is_rechecked(self):
        ev = prop_event({"Pinnacle": (1.91, 1.91), "DK": (2.15, 1.70)})
        ev["bookmakers"][0]["key"] = "pinnacle"
        a = EVAlerter(temp_data(self, Config()), dry_run=True)
        a.handle(find_evs([ev], Config().for_props(), NOW), checked_events={"p1"}, now=1000)
        a.handle([], checked_events={"other-game"}, now=1060)
        self.assertEqual(len(a.open), 1)
        a.handle([], checked_events={"p1"}, now=1120)
        self.assertEqual(len(a.open), 0)


def ev_leg(event_id, book_prices, fair=0.5):
    """An EVBet whose board has the given {book: price}."""
    ev = ev_event([("Home", 1 / fair * 0.955, None), ("Away", 1 / (1 - fair) * 0.955, None)],
                  {bk: [("Home", pr, None)] for bk, pr in book_prices.items()})
    ev["id"] = event_id
    ev["sport_key"] = "icehockey_nhl"
    return find_evs([ev], Config(min_ev_pct=0.1, round_stakes=0), NOW)[0]


class Parlays(unittest.TestCase):
    def test_builds_same_book_different_games(self):
        legs = [ev_leg("g1", {"DK": 2.10, "FD": 2.08}), ev_leg("g2", {"DK": 2.12}), ev_leg("g3", {"FD": 2.15})]
        cfg = Config(parlay_min_ev_pct=5, parlay_leg_min_ev_pct=2)
        ps = find_parlays(legs, cfg)
        self.assertTrue(ps)
        best = ps[0]
        self.assertEqual(len({b.event_id for b, _, _ in best.legs}), len(best.legs))
        self.assertTrue(all(pr == dict((r[0], r[1]) for r in b.board)[best.book] for b, pr, _ in best.legs))
        self.assertAlmostEqual(best.price, math.prod(pr for _, pr, _ in best.legs))
        self.assertAlmostEqual(best.ev_pct, (best.fair_prob * best.price - 1) * 100)

    def test_no_same_game_and_no_reused_legs(self):
        same = [ev_leg("g1", {"DK": 2.15}), ev_leg("g1", {"DK": 2.15})]
        self.assertEqual(find_parlays(same, Config(parlay_min_ev_pct=1)), [])
        legs = [ev_leg(f"g{i}", {"DK": 2.20}) for i in range(4)]
        ps = find_parlays(legs, Config(parlay_min_ev_pct=1, parlay_max_legs=2, parlay_max_alerts=5))
        used = [b.key for p in ps for b, _, _ in p.legs]
        self.assertEqual(len(used), len(set(used)))
        self.assertEqual(len(ps), 2)   # 4 legs -> two 2-leg parlays, no leg twice

    def test_stake_cap_and_threshold(self):
        legs = [ev_leg("g1", {"DK": 2.40}), ev_leg("g2", {"DK": 2.40})]
        [p] = find_parlays(legs, Config(parlay_min_ev_pct=5, ev_bankroll=1000, parlay_max_stake_pct=1))
        self.assertLessEqual(p.stake, 10)
        self.assertEqual(find_parlays(legs, Config(parlay_min_ev_pct=99)), [])
        self.assertEqual(find_parlays(legs, Config(parlays_enabled=False)), [])

    def test_card_and_channel(self):
        legs = [ev_leg("g1", {"DK": 2.40}), ev_leg("g2", {"DK": 2.40})]
        [p] = find_parlays(legs, Config(parlay_min_ev_pct=5))
        d = parlay_payload(p)["embeds"][0]
        self.assertTrue(d["title"].startswith("📦 PARLAY"))
        self.assertTrue(d["description"].startswith("👉 **DO THIS: one parlay ticket at DK.**"))
        a = ParlayAlerter(Config(webhook_url="main", ev_webhook_url="ev"), dry_run=True)
        self.assertEqual(a.webhook_for(p), "ev")
        a2 = ParlayAlerter(Config(webhook_url="main", ev_webhook_url="ev", parlay_webhook_url="par"), dry_run=True)
        self.assertEqual(a2.webhook_for(p), "par")


class LocksMode(unittest.TestCase):
    def test_raises_floors_but_keeps_stricter_settings(self):
        c = Config(min_profit_pct=1.5, min_ev_pct=4, min_confidence="low", prop_min_ev_pct=10).with_mode()
        self.assertEqual(c.min_profit_pct, 2.0)
        self.assertEqual(c.min_live_profit_pct, 5.0)      # live arbs: 5%+ in locks mode
        self.assertEqual(c.min_ev_pct, 5.0)
        self.assertEqual(c.prop_min_ev_pct, 10)            # yours was already stricter
        self.assertEqual(c.min_confidence, "high")
        self.assertEqual((c.max_ev_per_hour, c.parlay_max_legs), (6, 2))
        same = Config(alert_mode="balanced", min_profit_pct=1.5).with_mode()
        self.assertEqual(same.min_profit_pct, 1.5)

    def test_small_arbs_dropped(self):
        ev = event({
            "A": [("h2h", [("Home", 2.02, None), ("Away", 1.90, None)])],
            "B": [("h2h", [("Home", 1.90, None), ("Away", 2.02, None)])],
        })   # ~1% arb: not a lock
        self.assertEqual(len(find_arbs([ev], Config(min_profit_pct=0.5, round_stakes=0), NOW)), 1)
        self.assertEqual(find_arbs([ev], Config(min_profit_pct=0.5).with_mode(), NOW), [])

    def test_hourly_cap_keeps_the_best(self):
        a = EVAlerter(temp_data(self, Config()), dry_run=True)
        a.max_per_hour = 2
        bets = [replace(ev_leg(f"g{i}", {"DK": 2.30}), event_id=f"g{i}") for i in range(4)]
        self.assertEqual(a.handle(bets, now=1000), 2)
        self.assertEqual(len(a.open), 2)
        self.assertEqual(a.handle(bets, now=1100), 0)        # still capped this hour
        self.assertEqual(a.handle(bets, now=1000 + 3700), 2)  # next hour: the next best go out


class Channels(unittest.TestCase):
    def test_set_env_value_replaces_placeholder(self):
        import tempfile
        from arbbot import set_env_value
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / ".env"
            f.write_text("ODDS_API_KEY=abc\nDISCORD_EV_WEBHOOK_URL=PASTE_URL_HERE\nBANKROLL=300\n")
            set_env_value(f, "DISCORD_EV_WEBHOOK_URL", "https://discord.com/api/webhooks/1/x")
            self.assertEqual(f.read_text().splitlines(), [
                "ODDS_API_KEY=abc", "BANKROLL=300", "DISCORD_EV_WEBHOOK_URL=https://discord.com/api/webhooks/1/x"])

    def test_placeholder_webhook_falls_back_to_main(self):
        cfg = Config(webhook_url="https://main", ev_webhook_url="PASTE_URL_HERE")
        self.assertEqual(cfg.bad_webhooks(), ["DISCORD_EV_WEBHOOK_URL"])
        self.assertEqual(EVAlerter(cfg, True).webhook_for(None), "https://main")
        self.assertEqual(Config(ev_webhook_url="https://discord.com/api/webhooks/1/x").bad_webhooks(), [])

    def test_routing(self):
        cfg = Config(webhook_url="main", ev_webhook_url="ev")
        [arb] = find_arbs(demo_events(), Config())
        arb.is_live = False
        self.assertEqual(Alerter(cfg, True).webhook_for(arb), "main")          # arbs stay put
        self.assertEqual(EVAlerter(cfg, True).webhook_for(arb), "ev")
        self.assertEqual(OutlierAlerter(cfg, True).webhook_for(arb), "ev")     # falls back to the EV channel
        self.assertEqual(OutlierAlerter(Config(webhook_url="main", ev_webhook_url="ev",
                                               outlier_webhook_url="out"), True).webhook_for(arb), "out")


class MyBooks(unittest.TestCase):
    def test_arbs_only_use_my_books(self):
        ev = event({
            "A": [("h2h", [("Home", 2.10, None), ("Away", 1.80, None)])],
            "B": [("h2h", [("Home", 1.70, None), ("Away", 2.15, None)])],
        })
        self.assertEqual(len(find_arbs([ev], Config(min_profit_pct=0, my_books="a,b"), NOW)), 1)
        self.assertEqual(find_arbs([ev], Config(min_profit_pct=0, my_books="a,c"), NOW), [])

    def test_ev_only_at_my_books(self):
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)],
                      {"B": [("Home", 2.10, None)], "C": [("Home", 2.15, None)]})
        [b] = find_evs([ev], Config(min_ev_pct=3, my_books="b"), NOW)
        self.assertEqual(b.book, "B")
        self.assertEqual([r[0] for r in b.board], ["B"])     # the list shows only your books
        [b2] = find_evs([ev], Config(min_ev_pct=3), NOW)      # no MY_BOOKS: best of all
        self.assertEqual(b2.book, "C")

    def test_outliers_flag_and_hedge_only_my_books(self):
        ev = outlier_event(1.85, 1.95)
        self.assertEqual(find_outliers([ev], Config(my_books="dk,fd"), NOW), [])     # "Stale" isn't yours
        [o] = find_outliers([ev], Config(my_books="stale,fd"), NOW)
        self.assertEqual(o.hedge[0][1], "FD")                 # hedge at your book, not DK
        self.assertEqual({r[0] for r in o.board}, {"Stale", "FD"})
        self.assertEqual(o.sources_used, 4)                   # but all books still set the market price


class StateLinks(unittest.TestCase):
    def ev(self):
        e = event({"BetMGM": [("h2h", [("Home", 2.0, None), ("Away", 2.0, None)])]})
        e["bookmakers"][0]["markets"][0]["outcomes"][0]["link"] = "https://sports.{state}.betmgm.com/x?o=1"
        return e

    def test_state_filled_in(self):
        from arbbot import apply_fees
        e = apply_fees([self.ev()], Config(us_state="nj"))[0]
        self.assertEqual(e["bookmakers"][0]["markets"][0]["outcomes"][0]["link"], "https://sports.nj.betmgm.com/x?o=1")

    def test_dropped_without_state(self):
        from arbbot import apply_fees
        e = apply_fees([self.ev()], Config())[0]
        self.assertEqual(e["bookmakers"][0]["markets"][0]["outcomes"][0]["link"], "")


class KalshiFees(unittest.TestCase):
    def kalshi_event(self, price):
        ev = event({"Kalshi": [("h2h", [("Home", price, None), ("Away", 2.0, None)])]})
        ev["bookmakers"][0]["key"] = "kalshi"
        return ev

    def test_fee_lowers_price(self):
        from arbbot import apply_fees
        ev = apply_fees([self.kalshi_event(2.0)], Config())[0]
        # P = 0.50, fee = 0.07 x 0.5 x 0.5 = 0.0175 -> 1 / 0.5175
        self.assertAlmostEqual(ev["bookmakers"][0]["markets"][0]["outcomes"][0]["price"], 1 / 0.5175, places=3)

    def test_applied_once_and_can_be_off(self):
        from arbbot import apply_fees
        ev = self.kalshi_event(2.0)
        apply_fees([ev], Config())
        once = ev["bookmakers"][0]["markets"][0]["outcomes"][0]["price"]
        apply_fees([ev], Config())
        self.assertEqual(ev["bookmakers"][0]["markets"][0]["outcomes"][0]["price"], once)
        off = apply_fees([self.kalshi_event(2.0)], Config(kalshi_fee_rate=0))[0]
        self.assertEqual(off["bookmakers"][0]["markets"][0]["outcomes"][0]["price"], 2.0)

    def test_other_books_untouched(self):
        from arbbot import apply_fees
        ev = apply_fees([event({"DK": [("h2h", [("Home", 2.0, None), ("Away", 2.0, None)])]})], Config())[0]
        self.assertEqual(ev["bookmakers"][0]["markets"][0]["outcomes"][0]["price"], 2.0)


class ActiveHours(unittest.TestCase):
    def at(self, hhmm):  # a New York local time on a fixed day, as UTC
        from zoneinfo import ZoneInfo
        h, m = map(int, hhmm.split(":"))
        return datetime(2026, 10, 3, h, m, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)

    def test_always_on_when_unset(self):
        self.assertEqual(seconds_until_active(Config(), self.at("03:00")), 0)

    def test_daytime_window(self):
        cfg = Config(active_hours="08:00-22:00")
        self.assertEqual(seconds_until_active(cfg, self.at("12:00")), 0)
        self.assertEqual(seconds_until_active(cfg, self.at("07:30")), 30 * 60)
        self.assertEqual(seconds_until_active(cfg, self.at("22:00")), 10 * 3600)

    def test_overnight_window(self):
        cfg = Config(active_hours="18:00-02:00")
        self.assertEqual(seconds_until_active(cfg, self.at("23:00")), 0)
        self.assertEqual(seconds_until_active(cfg, self.at("01:59")), 0)
        self.assertEqual(seconds_until_active(cfg, self.at("02:00")), 16 * 3600)


class FakeAPI:
    def __init__(self, remaining=None):
        self.remaining = remaining


def sched_with(games, cfg=None, remaining=None):
    """games: {sport: [(id, start_datetime), ...]}"""
    cfg = cfg or Config(sports=list(games))
    s = Scheduler(cfg, FakeAPI(remaining))
    s.games.update(games)
    return s


class DotEnv(unittest.TestCase):
    def test_forgives_doubled_key(self):
        import os, tempfile
        from arbbot import load_dotenv
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / ".env"
            f.write_text("ARBTEST_SPORTS=ARBTEST_SPORTS=americanfootball_nfl,icehockey_nhl\n")
            os.environ.pop("ARBTEST_SPORTS", None)
            load_dotenv(f)
            self.assertEqual(os.environ.pop("ARBTEST_SPORTS"), "americanfootball_nfl,icehockey_nhl")


class Credits(unittest.TestCase):
    def test_cost_markets_times_regions(self):
        self.assertEqual(Config(markets="h2h,spreads,totals", regions="us", bookmakers="").credits_per_call(), 3)
        self.assertEqual(Config(markets="h2h", regions="us,us2", bookmakers="").credits_per_call(), 2)

    def test_default_ten_books_cost_one_region(self):
        self.assertEqual(len(Config().bookmakers.split(",")), 10)
        self.assertEqual(Config(markets="h2h,spreads,totals").credits_per_call(), 3)

    def test_up_to_ten_bookmakers_cost_one_region(self):
        ten = ",".join(f"b{i}" for i in range(10))
        self.assertEqual(Config(markets="h2h,spreads", bookmakers=ten).credits_per_call(), 2)
        self.assertEqual(Config(markets="h2h", bookmakers=ten + ",b10").credits_per_call(), 2)

    def test_game_minutes(self):
        cfg = Config(game_minutes={"basketball_nba": 140})
        self.assertEqual(cfg.minutes_for("basketball_nba"), 140)
        self.assertEqual(cfg.minutes_for("basketball_ncaab"), 140)
        self.assertEqual(cfg.minutes_for("icehockey_nhl"), 170)
        self.assertEqual(cfg.minutes_for("cricket_ipl"), 180)

    def test_next_reset(self):
        cfg = Config(billing_day=15)
        self.assertEqual(next_reset(cfg, NOW).date().isoformat(), "2026-10-15")
        cfg = Config(billing_day=1)
        self.assertEqual(next_reset(cfg, NOW).date().isoformat(), "2026-11-01")
        dec = datetime(2026, 12, 20, tzinfo=timezone.utc)
        self.assertEqual(next_reset(cfg, dec).date().isoformat(), "2027-01-01")
        # Day 29-31 lands on the last day of shorter months, and still moves forward after it.
        late = Config(billing_day=31)
        nov = datetime(2026, 11, 5, tzinfo=timezone.utc)
        self.assertEqual(next_reset(late, nov).date().isoformat(), "2026-11-30")
        self.assertEqual(next_reset(late, datetime(2027, 1, 31, 1, tzinfo=timezone.utc)).date().isoformat(),
                         "2027-02-28")
        self.assertEqual(next_reset(Config(billing_day=30), datetime(2027, 2, 10, tzinfo=timezone.utc))
                         .date().isoformat(), "2027-02-28")


class SchedulerState(unittest.TestCase):
    def test_live_pregame_idle(self):
        s = sched_with({"basketball_nba": [("g1", NOW - timedelta(minutes=30))],
                        "icehockey_nhl": [("g2", NOW + timedelta(hours=1))],
                        "baseball_mlb": [("g3", NOW + timedelta(hours=8))],
                        "soccer_epl": [("g4", NOW + timedelta(hours=36))],
                        "tennis_atp": [("g5", NOW + timedelta(hours=60))],
                        "americanfootball_nfl": []})
        self.assertEqual(s.state("basketball_nba", NOW), LIVE)
        self.assertEqual(s.state("icehockey_nhl", NOW), PREGAME)
        self.assertEqual(s.state("baseball_mlb", NOW), EARLY)      # later today: hourly
        self.assertEqual(s.state("soccer_epl", NOW), FAR)          # tomorrow-ish: every 3h
        self.assertIsNone(s.state("tennis_atp", NOW))              # beyond the 48h lookahead
        self.assertIsNone(s.state("americanfootball_nfl", NOW))
        self.assertEqual([s.interval(x) for x in (LIVE, PREGAME, EARLY, FAR)], [60, 900, 3600, 10800])

    def test_game_over_after_duration(self):
        s = sched_with({"basketball_nba": [("g1", NOW - timedelta(minutes=200))]})
        self.assertIsNone(s.state("basketball_nba", NOW))

    def test_no_live_checks_when_nothing_live_is_wanted(self):
        cfg = Config(sports=["basketball_nba"], arb_live=False, ev_live=False, outlier_live=False)
        s = sched_with({"basketball_nba": [("g1", NOW - timedelta(minutes=30)),
                                           ("g2", NOW + timedelta(hours=1))]}, cfg)
        self.assertEqual(s.state("basketball_nba", NOW), PREGAME)   # skips the live game
        s.games["basketball_nba"] = [("g1", NOW - timedelta(minutes=30))]
        self.assertIsNone(s.state("basketball_nba", NOW))

    def test_live_sports_switch(self):
        cfg = Config(sports=["americanfootball_ncaaf", "icehockey_nhl"], live_sports=["icehockey_nhl"])
        s = sched_with({"americanfootball_ncaaf": [("c1", NOW - timedelta(minutes=30))],
                        "icehockey_nhl": [("n1", NOW - timedelta(minutes=30))]}, cfg)
        self.assertIsNone(s.state("americanfootball_ncaaf", NOW))   # no paid live checks
        self.assertEqual(s.state("icehockey_nhl", NOW), LIVE)

    def test_rejected_props_stop(self):
        cfg = Config(sports=["basketball_nba"])
        s = sched_with({"basketball_nba": [("g1", NOW + timedelta(hours=1))]}, cfg)
        class Api:
            remaining = None
            def event_odds(self, sport, gid, markets):
                raise urllib.error.HTTPError("u", 422, "bad market", {}, None)
        s.api = Api()
        s.fetch_props([("basketball_nba", "g1")])
        self.assertIn("basketball_nba", s.bad_prop_sports)
        self.assertEqual(s.props_due(NOW), [])

    def test_pregame_off(self):
        # PREGAME_MINUTES=0 and LOOKAHEAD_HOURS=0: live games only.
        cfg = Config(sports=["icehockey_nhl"], pregame_minutes=0, lookahead_hours=0)
        s = sched_with({"icehockey_nhl": [("g2", NOW + timedelta(hours=1))]}, cfg)
        self.assertIsNone(s.state("icehockey_nhl", NOW))

    def test_ended_after_two_missing_fetches(self):
        s = sched_with({"basketball_nba": [("g1", NOW - timedelta(minutes=30))]})
        s.note_feed("basketball_nba", [], NOW)
        self.assertEqual(s.state("basketball_nba", NOW), LIVE)  # one miss could be a blip
        s.note_feed("basketball_nba", [], NOW)
        self.assertIsNone(s.state("basketball_nba", NOW))

    def test_listed_game_resets_misses(self):
        s = sched_with({"basketball_nba": [("g1", NOW - timedelta(minutes=30))]})
        s.note_feed("basketball_nba", [], NOW)
        s.note_feed("basketball_nba", [{"id": "g1", "bookmakers": [{}]}], NOW)
        s.note_feed("basketball_nba", [], NOW)
        self.assertEqual(s.state("basketball_nba", NOW), LIVE)


class Budget(unittest.TestCase):
    def test_full_speed_when_affordable(self):
        # One 2h40m NBA game, 3 credits/check every 60s = ~480 credits; 100K/month is plenty.
        s = sched_with({"basketball_nba": [("g1", NOW)]})
        s.update_budget(NOW)
        self.assertAlmostEqual(s.forecast, 480, delta=15)
        self.assertEqual(s.scale, 1.0)
        self.assertEqual(s.interval(LIVE), 60)

    def test_slows_down_when_over_budget(self):
        cfg = Config(sports=["basketball_nba"], pregame_minutes=0)
        s = sched_with({"basketball_nba": [(f"g{i}", NOW + timedelta(hours=2 * i)) for i in range(12)]},
                       cfg, remaining=3000)
        s.update_budget(NOW)
        # 24h of back-to-back games = 4320 credits at full speed, far more than
        # (3000 left - 2% cushion) / days until reset. Early checks stretch to the cap first,
        # then live checks slow just enough to fit.
        self.assertGreater(s.scale, 1)
        self.assertAlmostEqual(s.extra_scale, cfg.extra_max_stretch)
        spend = s.core_demand / s.scale + s.extra_demand / s.extra_scale
        self.assertAlmostEqual(spend, s.allowance, delta=1)
        self.assertGreater(s.interval(LIVE), 60)

    def test_early_checks_stretch_before_live_ones(self):
        # One live game plus lots of far-out games: the far-out checks slow down, live stays at 60s.
        cfg = Config(sports=["basketball_nba", "icehockey_nhl"], props_enabled=False, early_minutes=1)
        s = sched_with({"basketball_nba": [("g1", NOW)],
                        "icehockey_nhl": [("h1", NOW + timedelta(hours=20))]}, cfg, remaining=75_000)
        s.update_budget(NOW)
        self.assertLess(s.core_demand, s.allowance)
        self.assertGreater(s.forecast, s.allowance)
        self.assertEqual(s.scale, 1.0)                 # live untouched...
        self.assertGreater(s.extra_scale, 1.0)         # ...early checks absorb the shortfall
        self.assertEqual(s.interval(LIVE), 60)

    def test_live_never_paused_by_early_checks(self):
        # Early checks alone exceed the budget: they stretch beyond the cap; live keeps running.
        cfg = Config(sports=["basketball_nba", "icehockey_nhl"], props_enabled=False, early_minutes=1)
        s = sched_with({"basketball_nba": [("g1", NOW)],
                        "icehockey_nhl": [("h1", NOW + timedelta(hours=20))]}, cfg, remaining=31 * 600)
        s.update_budget(NOW)
        self.assertLess(s.scale, float("inf"))
        self.assertGreater(s.extra_scale, cfg.extra_max_stretch)
        spend = s.core_demand / s.scale + s.extra_demand / s.extra_scale
        self.assertAlmostEqual(spend, s.allowance, delta=1)

    def test_stretch_below_one_is_treated_as_one(self):
        # EXTRA_MAX_STRETCH=0.5 would mean "check early games MORE often"; clamp it so the
        # budget still balances.
        cfg = Config(sports=["basketball_nba", "icehockey_nhl"], props_enabled=False, early_minutes=1,
                     extra_max_stretch=0.5)
        s = sched_with({"basketball_nba": [("g1", NOW)],
                        "icehockey_nhl": [("h1", NOW + timedelta(hours=20))]}, cfg, remaining=31 * 1500)
        s.update_budget(NOW)
        self.assertGreaterEqual(s.extra_scale, 1.0)
        spend = s.core_demand / s.scale + s.extra_demand / s.extra_scale
        self.assertLessEqual(spend, s.allowance + 1)

    def test_weekends_get_more_credits(self):
        s = sched_with({"basketball_nba": []}, remaining=60_000)
        sat = datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)    # Saturday 10am New York
        tue = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)    # Tuesday 10am New York
        s.update_budget(sat)
        weekend = s.allowance
        s.update_budget(tue)
        self.assertGreater(weekend, s.allowance * 2)                # Sat/Sun 2.0-2.2 vs Tue/Wed 0.6

    def test_even_weights_spread_evenly(self):
        cfg = Config(sports=["basketball_nba"], budget_weights={d: 1.0 for d in
                     ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]})
        s = sched_with({"basketball_nba": []}, cfg, remaining=29_000)
        s.update_budget(NOW)
        hours = (next_reset(cfg, NOW) - NOW).total_seconds() / 3600
        self.assertAlmostEqual(s.allowance, 29_000 * 0.98 * 24 / int(hours), delta=5)

    def test_out_of_credits_pauses(self):
        s = sched_with({"basketball_nba": [("g1", NOW)]}, remaining=0)
        s.update_budget(NOW)
        self.assertEqual(s.scale, float("inf"))

    def test_wrong_monthly_setting_doesnt_block(self):
        cfg = Config(sports=["basketball_nba"], monthly_credits=10_000_000)
        s = sched_with({"basketball_nba": [("g1", NOW)]}, cfg, remaining=50_000)
        s.update_budget(NOW)
        self.assertEqual(s.scale, 1.0)

    def test_no_games_costs_nothing(self):
        s = sched_with({"basketball_nba": [], "icehockey_nhl": []})
        s.update_budget(NOW)
        self.assertEqual(s.forecast, 0)
        self.assertEqual(s.due(NOW), [])


class FetchFailures(unittest.TestCase):
    """A request that fails must not count as "looked, and the bet is gone"."""

    def test_fetch_reports_only_sports_that_answered(self):
        class Api:
            remaining = None
            def odds(self, sport, until):
                if sport == "basketball_nba":
                    raise TimeoutError("read timed out")
                return []
        s = sched_with({"basketball_nba": [], "icehockey_nhl": []})
        s.api = Api()
        events, ok = s.fetch(["basketball_nba", "icehockey_nhl"], NOW)
        self.assertEqual((events, ok), ([], {"icehockey_nhl"}))

    def test_fetch_props_counts_empty_answers_not_errors(self):
        import http.client
        class Api:
            remaining = None
            def event_odds(self, sport, gid, markets):
                if gid == "g2":
                    raise http.client.RemoteDisconnected("dropped")
                return {"id": gid, "bookmakers": []}
        s = sched_with({"basketball_nba": []}, Config(sports=["basketball_nba"]))
        s.api = Api()
        events, ok = s.fetch_props([("basketball_nba", "g1"), ("basketball_nba", "g2")])
        self.assertEqual((events, ok), ([], {"g1"}))

    def test_schedule_errors_dont_crash(self):
        import http.client
        errors = [TimeoutError("slow"), http.client.RemoteDisconnected("dropped"), ValueError("bad json")]
        class Api:
            remaining = None
            def events(self, sport, horizon_hours):
                raise errors.pop(0)
        s = sched_with({"basketball_nba": [("g1", NOW)]})
        s.api = Api()
        for _ in range(3):
            s.refresh_events(force=True)
        self.assertEqual(s.games["basketball_nba"], [("g1", NOW)])   # kept the last good schedule
        class Rejected:
            remaining = None
            def events(self, sport, horizon_hours):
                raise urllib.error.HTTPError("u", 401, "bad key", {}, None)
        s.api = Rejected()
        with self.assertRaises(urllib.error.HTTPError):
            s.refresh_events(force=True)

    def test_live_only_pays_for_nothing_pregame(self):
        cfg = Config(sports=["basketball_nba"], live_only=True)
        s = sched_with({"basketball_nba": [("g1", NOW + timedelta(hours=1))]}, cfg)
        self.assertIsNone(s.state("basketball_nba", NOW))
        self.assertEqual(s._prop_tiers(NOW), [])
        s.games["basketball_nba"].append(("g0", NOW - timedelta(minutes=30)))
        self.assertEqual(s.state("basketball_nba", NOW), LIVE)


class AlertLifecycle(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg = Config(webhook_url="https://main", state_dir=str(d), log_file="", min_ev_pct=3,
                          round_stakes=0, ev_log_file=str(d / "ev.csv"), outlier_log_file=str(d / "out.csv"))
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def wire(self, a, fail_first=0, retryable=True):
        def fake(payload, message_id=None, url=""):
            self.sent.append(("PATCH" if message_id else "POST", payload["embeds"][0]["title"]))
            a.send_retryable = False
            if not message_id and fail_first and len([m for m, _ in self.sent if m == "POST"]) <= fail_first:
                a.send_retryable = retryable
                return None
            return message_id or f"m{len(self.sent)}"
        a._discord = fake
        return a

    def test_post_lost_after_sending_is_not_doubled(self):
        # A timeout while reading Discord's reply: the card may be up already, so no second ping.
        a = self.wire(Alerter(self.cfg, dry_run=False), fail_first=1, retryable=False)
        arbs = find_arbs(demo_events(), Config())
        a.handle(arbs, ["basketball_nba"], now=time.time())
        a.handle(arbs, ["basketball_nba"], now=time.time())
        self.assertEqual([m for m, _ in self.sent], ["POST"])

    def test_retry_only_for_errors_before_discord_has_it(self):
        import http.client
        from unittest import mock
        a = Alerter(self.cfg, dry_run=False)
        for err, ok in ((urllib.error.URLError("refused"), True), (urllib.error.HTTPError("u", 500, "x", {}, None), True),
                        (TimeoutError("read timed out"), False), (http.client.RemoteDisconnected("x"), False)):
            with mock.patch("arbbot._webhook", side_effect=err):
                self.assertIsNone(a._discord({"embeds": [{}]}))
            self.assertEqual(a.send_retryable, ok, err)

    def test_failed_first_post_is_retried(self):
        a = self.wire(Alerter(self.cfg, dry_run=False), fail_first=1)
        arbs = find_arbs(demo_events(), Config())
        a.handle(arbs, ["basketball_nba"], now=time.time())
        self.assertIsNone(a.open[arbs[0].key].message_id)
        a.handle(arbs, ["basketball_nba"], now=time.time())          # same prices: retried anyway
        self.assertEqual([m for m, _ in self.sent], ["POST", "POST"])
        self.assertIsNotNone(a.open[arbs[0].key].message_id)
        a.handle(arbs, ["basketball_nba"], now=time.time())          # landed: nothing more to send
        self.assertEqual(len(self.sent), 2)

    def test_prop_alerts_close_at_kickoff(self):
        from arbbot import close_started
        a = EVAlerter(self.cfg, dry_run=True)
        ev = prop_event({"Pinnacle": (1.91, 1.91), "DK": (2.15, 1.70)})
        ev["bookmakers"][0]["key"] = "pinnacle"
        [b] = find_evs([ev], self.cfg.for_props(), NOW)
        a.handle([b], now=1000)
        self.assertEqual(close_started(NOW, a), set())                # not started: stays
        self.assertIn(b.key, a.open)
        tip = datetime(2026, 10, 3, 18, 1, tzinfo=timezone.utc)
        self.assertEqual(close_started(tip, a), {"p1"})
        self.assertEqual(a.open, {})

    def test_ev_to_outlier_and_back_is_one_bet(self):
        from arbbot import hand_over
        ev_alr, out_alr = self.wire(EVAlerter(self.cfg, dry_run=False)), self.wire(OutlierAlerter(self.cfg, dry_run=False))
        logged = []
        ev_alr.on_log = out_alr.on_log = logged.append
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})
        [b] = find_evs([ev], self.cfg, NOW)
        ev_alr.handle([b], ["Test"], now=1000)
        # The same bet becomes an outlier: the +EV card is retired with a pointer, not "GONE".
        hand_over([], [b], ev_alr, out_alr)
        out_alr.handle([b], now=1100)
        ev_alr.handle([], now=1100)
        titles = [t for _, t in self.sent]
        self.assertTrue(titles[1].startswith("⬆️ Now an OUTLIER"))
        self.assertFalse(any("GONE" in t for t in titles))
        self.assertEqual(out_alr.open[b.key].first_seen, 1000)       # same bet, same start time
        self.assertEqual(len(logged), 1)                             # logged once, not twice
        # ...and back to a normal +EV edge.
        hand_over([b], [], ev_alr, out_alr)
        ev_alr.handle([b], now=1200)
        out_alr.handle([], now=1200)
        self.assertTrue(self.sent[-2][1].startswith("↘️ Back to a normal +EV edge"))
        self.assertIn(b.key, ev_alr.open)
        self.assertEqual(out_alr.open, {})
        self.assertEqual(len(logged), 1)
        self.assertFalse(any("GONE" in t for _, t in self.sent))


class ParlayFixes(unittest.TestCase):
    def test_open_parlay_kept_over_a_slightly_better_one(self):
        a, b, c = ev_leg("g1", {"DK": 2.10}), ev_leg("g2", {"DK": 2.12}), ev_leg("g3", {"DK": 2.30})
        cfg = Config(parlay_min_ev_pct=1, parlay_leg_min_ev_pct=1, parlay_max_legs=2, parlay_max_alerts=1)
        [ab] = find_parlays([a, b], cfg)
        [best] = find_parlays([a, b, c], cfg)
        self.assertNotEqual(best.key, ab.key)                        # a new leg makes a better combo...
        [kept] = find_parlays([a, b, c], cfg, keep={ab.key})
        self.assertEqual(kept.key, ab.key)                           # ...but the posted one stays

    def test_started_legs_dropped(self):
        legs = [ev_leg("g1", {"DK": 2.20}), ev_leg("g2", {"DK": 2.20})]
        cfg = Config(parlay_min_ev_pct=1, parlay_leg_min_ev_pct=1)
        self.assertTrue(find_parlays(legs, cfg, now=NOW))
        self.assertEqual(find_parlays(legs, cfg, now=datetime(2026, 10, 3, 18, 1, tzinfo=timezone.utc)), [])

    def test_rejected_prices_not_green_or_parlayed(self):
        from arbbot import _board_lines
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)],
                      {"B": [("Home", 2.10, None)], "C": [("Home", 2.90, None)]})  # C: +45%, a stale line
        [b] = find_evs([ev], Config(min_ev_pct=3, max_ev_pct=25), NOW)
        self.assertEqual(b.book, "B")
        self.assertEqual({r[0]: r[4] for r in b.board}, {"C": False, "B": True})
        self.assertIn("⚪ C", _board_lines(b))
        self.assertNotIn("Any 🟢 book below works", ev_payload(b)["embeds"][0]["description"])
        other = ev_leg("g2", {"C": 2.20})
        self.assertEqual(find_parlays([b, other], Config(parlay_min_ev_pct=1, parlay_leg_min_ev_pct=1)), [])
        [o] = find_outliers([outlier_event(1.85, 1.95)], Config(), NOW)
        self.assertTrue(all(r[4] for r in o.board))


class PropClosingLines(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg = Config(ev_log_file=str(d / "ev.csv"), outlier_log_file=str(d / "out.csv"),
                          closing_file=str(d / "close.csv"), markout_file=str(d / "mk.csv"), pregame_max_age_seconds=10**9)

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, event_id, player=""):
        return {"event_id": event_id, "market": "player_points" if player else "h2h", "outcome": "Over" if player else "Home",
                "point": "25.5" if player else "", "player": player, "home_team": "Lakers",
                "first_seen": "2026-10-03T11:00:00+00:00", "commence_time": "2026-10-03T18:00:00Z"}

    def test_prop_bets_get_their_own_closing_check(self):
        tr = ClosingTracker(self.cfg)
        tr.add(self.row("e1"))
        tr.add(self.row("p1", "LeBron James"))
        self.assertEqual((tr.needs_close(), tr.needs_close_props()), ({"e1"}, {"p1"}))
        cfg = Config(sports=["basketball_nba"], closing_minutes=5, prop_minutes=10**6, prop_early_minutes=10**6)
        s = sched_with({"basketball_nba": [("p1", NOW + timedelta(minutes=4))]}, cfg)
        s.last_props["p1"] = (NOW - timedelta(minutes=10)).timestamp()
        self.assertEqual(s.props_due(NOW), [])                       # no logged prop bet on it
        s.need_close_props = {"p1"}
        self.assertEqual(s.props_due(NOW), [("basketball_nba", "p1")])
        s.last_props["p1"] = s.props_ok["p1"] = (NOW - timedelta(minutes=0.5)).timestamp()
        self.assertEqual(s.props_due(NOW), [])                       # already looked inside the window
        s.props_ok["p1"], s.last_props["p1"] = 0, time.time() - 30   # that look failed...
        self.assertEqual(s.props_due(NOW), [])
        s.last_props["p1"] = time.time() - 61                        # ...so it's tried again a minute later
        self.assertEqual(s.props_due(NOW), [("basketball_nba", "p1")])
        for off in (dict(props_enabled=False), dict(live_only=True)):   # no paid prop checks at all
            s.cfg = replace(cfg, **off)
            self.assertEqual(s.props_due(NOW), [], off)

    def test_close_uses_consensus_when_no_sharp_prices_the_prop(self):
        tr = ClosingTracker(self.cfg)
        tr.add(self.row("p1", "LeBron James"))
        ev = prop_event({"DK": (1.95, 1.87), "FD": (1.90, 1.92), "MGM": (1.93, 1.89), "CZR": (1.92, 1.90)})
        tr.observe([ev], NOW)
        [(p, _)] = tr.latest.values()
        self.assertAlmostEqual(p, 0.5, delta=0.02)


class Settings(unittest.TestCase):
    def env(self, **kv):
        import os
        from unittest import mock
        return mock.patch.dict(os.environ, kv)

    def test_empty_number_means_default_and_maps_take_semicolons(self):
        with self.env(POLL_SECONDS="", BANKROLL=" ", BUDGET_WEIGHTS="sat=2;sun=3", GAME_MINUTES="basketball_ncaab=140",
                      PREGAME_MINUTES="20.0"):
            cfg = Config.from_env()
        self.assertEqual(cfg.poll_seconds, Config().poll_seconds)
        self.assertEqual(cfg.bankroll, Config().bankroll)
        self.assertEqual((cfg.budget_weights["sat"], cfg.budget_weights["sun"]), (2.0, 3.0))
        self.assertEqual(cfg.game_minutes, {"basketball_ncaab": 140})
        self.assertEqual(cfg.pregame_minutes, 20)

    def test_bad_values_name_the_setting(self):
        with self.env(POLL_SECONDS="abc"), self.assertRaisesRegex(ValueError, "POLL_SECONDS"):
            Config.from_env()
        with self.env(BUDGET_WEIGHTS="sat"), self.assertRaisesRegex(ValueError, "BUDGET_WEIGHTS"):
            Config.from_env()
        with self.env(SHARP_WEIGHTS="pinnacle=heavy"), self.assertRaisesRegex(ValueError, "SHARP_WEIGHTS"):
            Config.from_env()

    def test_set_replaces_spaced_lines(self):
        import os, tempfile
        from arbbot import set_env_value, load_dotenv
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / ".env"
            f.write_text("ARBTEST_BOOKS = fanduel\n# ARBTEST_BOOKS=old\nARBTEST_OTHER=1\n")
            set_env_value(f, "ARBTEST_BOOKS", "draftkings")
            self.assertEqual(f.read_text(), "# ARBTEST_BOOKS=old\nARBTEST_OTHER=1\nARBTEST_BOOKS=draftkings\n")
            os.environ.pop("ARBTEST_BOOKS", None)
            os.environ.pop("ARBTEST_OTHER", None)
            load_dotenv(f)
            self.assertEqual(os.environ.pop("ARBTEST_BOOKS"), "draftkings")
            os.environ.pop("ARBTEST_OTHER", None)


class BetResults(unittest.TestCase):
    """Grading every kind of alert, the day view, and the results channel."""
    START = "2026-10-03T18:00:00Z"                              # 2pm in New York
    LATER = datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc)    # every game is over

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg = Config(min_ev_pct=3, round_stakes=0, webhook_url="https://main", state_dir=str(d / "state"),
                          results_webhook_url="https://results", log_file=str(d / "arbs.csv"),
                          ev_log_file=str(d / "ev.csv"), outlier_log_file=str(d / "out.csv"),
                          parlay_log_file=str(d / "par.csv"), ev_results_file=str(d / "res.csv"),
                          closing_file=str(d / "close.csv"), markout_file=str(d / "mk.csv"), pregame_max_age_seconds=10**9,
                          score_check_file=str(d / "sc.csv"))

    def tearDown(self):
        self.tmp.cleanup()

    def scores(self, finals):
        """finals: {event_id: (home, away)} -> a fake API that counts its calls."""
        class Api:
            calls = []
            def scores(self, sport, days_from=3):
                Api.calls.append(sport)
                return [{"id": gid, "completed": True,
                         "scores": [{"name": "Home", "score": str(h)}, {"name": "Away", "score": str(a)}]}
                        for gid, (h, a) in finals.items()]
        return Api()

    def log_ev(self, gid, home_price=2.20, market="h2h"):
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", home_price, None)]},
                      start=self.START)
        ev["id"], ev["sport_key"] = gid, "icehockey_nhl"
        [b] = find_evs([ev], self.cfg, NOW)
        EVAlerter(self.cfg, dry_run=True).handle([b], now=1000)
        return b

    def log_parlay(self, gids):
        legs = [ev_leg(g, {"DK": 2.20}) for g in gids]
        [p] = find_parlays(legs, Config(parlay_min_ev_pct=1, parlay_leg_min_ev_pct=1, parlay_max_legs=len(gids)))
        p.stake = 10
        ParlayAlerter(self.cfg, dry_run=True).handle([p], now=1000)
        return p

    def test_single_bets_graded(self):
        b = self.log_ev("g1")
        api = self.scores({"g1": (4, 2)})
        [row] = settle_pending(self.cfg, api, self.LATER)
        self.assertEqual((row["result"], row["profit"]), ("win", round(b.stake * 1.2, 2)))
        self.assertEqual(api.calls, ["icehockey_nhl"])
        self.assertEqual(settle_pending(self.cfg, api, self.LATER), [])     # graded once, no more calls
        self.assertEqual(api.calls, ["icehockey_nhl"])

    def test_not_graded_before_the_game_can_be_over(self):
        self.log_ev("g1")
        api = self.scores({})
        self.assertEqual(settle_pending(self.cfg, api, datetime(2026, 10, 3, 19, 0, tzinfo=timezone.utc)), [])
        self.assertEqual(api.calls, [])                                     # no credits spent

    def test_parlays_graded_leg_by_leg(self):
        p = self.log_parlay(["g1", "g2"])
        [row] = settle_pending(self.cfg, self.scores({"g1": (3, 1), "g2": (5, 2)}), self.LATER)
        self.assertEqual(row["kind"], "parlay")
        self.assertEqual((row["result"], row["profit"]), ("win", round(10 * (p.price - 1), 2)))

    def test_parlay_push_drops_the_leg_and_a_loss_ends_it_early(self):
        self.log_parlay(["g1", "g2"])
        [row] = settle_pending(self.cfg, self.scores({"g1": (3, 1), "g2": (2, 2)}), self.LATER)
        self.assertEqual((row["result"], row["profit"]), ("win", 12.0))      # only the 2.20 leg counts
        self.tearDown(); self.setUp()
        self.log_parlay(["g1", "g2"])
        [row] = settle_pending(self.cfg, self.scores({"g1": (1, 3)}), self.LATER)   # g2 not final yet
        self.assertEqual((row["result"], row["profit"]), ("loss", -10.0))

    def test_props_listed_but_never_graded(self):
        ev = prop_event({"Pinnacle": (1.91, 1.91), "DK": (2.15, 1.70)}, start=self.START)
        ev["bookmakers"][0]["key"] = "pinnacle"
        [b] = find_evs([ev], self.cfg.for_props(), NOW)
        EVAlerter(self.cfg, dry_run=True).handle([b], now=1000)
        api = self.scores({"p1": (100, 90)})
        self.assertEqual(settle_pending(self.cfg, api, self.LATER), [])
        self.assertEqual(api.calls, [])                                     # nothing to spend credits on
        from arbbot import day_bets, result_line
        from datetime import date
        [r] = day_bets(self.cfg, date(2026, 10, 3))
        self.assertIn("🎯", result_line(r, self.cfg, self.LATER))
        self.assertIn("LeBron James Over 25.5 Points", result_line(r, self.cfg, self.LATER))

    def test_day_view_and_card(self):
        from arbbot import day_bets, results_payload, print_day
        from datetime import date
        self.log_ev("g1")
        self.log_ev("g2", home_price=2.30)
        settle_pending(self.cfg, self.scores({"g1": (4, 2), "g2": (1, 2)}), self.LATER)
        rows = day_bets(self.cfg, date(2026, 10, 3))
        self.assertEqual([r["result"] for r in rows], ["win", "loss"])
        self.assertEqual(day_bets(self.cfg, date(2026, 10, 2)), [])
        card = results_payload(self.cfg, "📅 Results", rows, rows, date(2026, 10, 3), self.LATER)["embeds"][0]
        self.assertTrue(card["title"].startswith("📅 Results · 1-1"))
        self.assertIn("✅ **Home ML", card["description"])
        self.assertIn("❌ **Home ML", card["description"])
        self.assertIn("Final: Away 2, Home 4", card["description"])
        self.assertIn("**All:** 1-1", card["description"])
        import io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            print_day(self.cfg, date(2026, 10, 3), self.LATER)
        self.assertIn("Saturday Oct 3", out.getvalue())
        self.assertNotIn("**", out.getvalue())

    def test_results_channel_posts_each_result_once(self):
        from arbbot import Results
        self.log_ev("g1")
        res = Results(self.cfg, dry_run=False)
        sent = []
        res.send = lambda payload: sent.append(payload["embeds"][0]) or True
        api = self.scores({"g1": (4, 2)})
        self.assertEqual(res.run(api, self.LATER), 1)
        self.assertTrue(sent[0]["title"].startswith("📋 1 result in · 1-0"))
        self.assertEqual(res.run(api, self.LATER), 0)                       # not posted twice
        # A bet graded from the command line still gets posted by the bot.
        self.log_ev("g2")
        settle_pending(self.cfg, self.scores({"g2": (0, 1)}), self.LATER)
        self.assertEqual(res.run(api, self.LATER), 1)
        self.assertIn("❌", sent[-1]["description"])
        # After a restart nothing is re-posted.
        again = Results(self.cfg, dry_run=False)
        again.send = lambda payload: sent.append(payload["embeds"][0]) or True
        self.assertEqual(again.run(api, self.LATER), 0)

    def test_first_run_does_not_flood_old_results(self):
        from arbbot import Results
        self.log_ev("g1")
        settle_pending(self.cfg, self.scores({"g1": (4, 2)}), self.LATER)  # graded before this update
        from unittest import mock
        later = self.LATER
        class Clock(datetime):                     # the first save happens "now": make that LATER
            @classmethod
            def now(cls, tz=None):
                return later
        with mock.patch("arbbot.datetime", Clock):
            res = Results(self.cfg, dry_run=False)
        res.send = lambda payload: self.fail("should not post old results")
        self.assertEqual(res.run(self.scores({}), self.LATER), 0)

    def test_daily_recap(self):
        from arbbot import Results
        from datetime import date
        self.log_ev("g1")
        res = Results(self.cfg, dry_run=False)
        sent = []
        res.send = lambda payload: sent.append(payload["embeds"][0]) or True
        res.daily(self.scores({"g1": (4, 2)}), date(2026, 10, 3), self.LATER)
        self.assertTrue(sent[0]["title"].startswith("📅 Results for Sat Oct 3 · 1-0"))
        self.assertEqual(res.run(self.scores({}), self.LATER), 0)           # not posted again one by one

    def test_record_keeps_parlays_separate(self):
        self.log_ev("g1")
        self.log_parlay(["g2", "g3"])
        settle_pending(self.cfg, self.scores({"g1": (4, 2), "g2": (1, 3), "g3": (1, 3)}), self.LATER)
        self.assertTrue(ev_record(self.cfg).startswith("1 bets, 1-0-0"))
        self.assertTrue(ev_record(self.cfg, kinds=("parlay",)).startswith("1 bets, 0-1-0"))

    def test_pick_labels(self):
        from arbbot import row_pick
        base = {"kind": "ev", "player": "", "point": ""}
        self.assertEqual(row_pick({**base, "market": "h2h", "outcome": "Bills"}), "Bills ML")
        self.assertEqual(row_pick({**base, "market": "spreads", "outcome": "Bills", "point": "-3.5"}), "Bills -3.5")
        self.assertEqual(row_pick({**base, "market": "totals", "outcome": "Over", "point": "47.5"}), "Over 47.5")
        self.assertEqual(row_pick({**base, "market": "player_points", "outcome": "Over", "point": "25.5",
                                   "player": "LeBron James"}), "LeBron James Over 25.5 Points")


class ReviewFixes(unittest.TestCase):
    """Issues found by the independent review of the fix commits."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.d = d
        self.cfg = Config(webhook_url="https://main", state_dir=str(d / "state"), log_file="", min_ev_pct=3,
                          round_stakes=0, ev_log_file=str(d / "ev.csv"), outlier_log_file=str(d / "out.csv"),
                          closing_file=str(d / "close.csv"), markout_file=str(d / "mk.csv"), pregame_max_age_seconds=10**9)
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def wire(self, a):
        def fake(payload, message_id=None, url=""):
            self.sent.append(("PATCH" if message_id else "POST", payload["embeds"][0]["title"]))
            a.send_retryable = False
            return message_id or f"m{len(self.sent)}"
        a._discord = fake
        return a

    # ---- closing lines
    def test_main_line_close_never_from_soft_book_consensus(self):
        tr = ClosingTracker(self.cfg)
        tr.add({"event_id": "e1", "market": "h2h", "outcome": "Home", "point": "", "player": "",
                "home_team": "Home", "first_seen": "2026-10-03T11:00:00+00:00", "commence_time": "2026-10-03T18:00:00Z"})
        no_sharp = event({b: [("h2h", [("Home", h, None), ("Away", a, None)])]
                          for b, h, a in (("DK", 2.20, 1.68), ("FD", 1.95, 1.87), ("MGM", 1.93, 1.89), ("CZR", 1.94, 1.88))})
        tr.observe([no_sharp], NOW)
        self.assertEqual(tr.latest, {})

    def test_prop_close_consensus_needs_prop_min_books(self):
        tr = ClosingTracker(self.cfg)
        tr.add({"event_id": "p1", "market": "player_points", "outcome": "Over", "point": "25.5",
                "player": "LeBron James", "home_team": "Lakers", "first_seen": "2026-10-03T11:00:00+00:00",
                "commence_time": "2026-10-03T18:00:00Z"})
        tr.observe([prop_event({"DK": (1.95, 1.87), "FD": (1.90, 1.92), "MGM": (1.93, 1.89)})], NOW)
        self.assertEqual(tr.latest, {})                                  # 3 books < PROP_MIN_BOOKS (4)

    # ---- schedule
    def test_odd_schedule_data_keeps_the_last_good_one(self):
        replies = [None, {"message": "Unknown sport"}, EOFError("cut off")]
        class Api:
            remaining = None
            def events(self, sport, horizon_hours):
                r = replies.pop(0)
                if isinstance(r, Exception):
                    raise r
                return r
        s = sched_with({"basketball_nba": [("g1", NOW)]})
        s.api = Api()
        for _ in range(3):
            s.refresh_events(force=True)
        self.assertEqual(s.games["basketball_nba"], [("g1", NOW)])

    # ---- restarts
    def restored_alerter(self, cls=Alerter, items=None, checked=None, **kw):
        a = self.wire(cls(self.cfg, dry_run=False, **kw))
        a.handle(items, checked, now=time.time())
        b = self.wire(cls(self.cfg, dry_run=False, **kw))          # restart
        self.sent.clear()
        return b

    def test_restored_alert_closes_on_the_first_check_that_misses_it(self):
        ev = event({"A": [("h2h", [("Home", 2.10, None), ("Away", 1.80, None)])],
                    "B": [("h2h", [("Home", 1.70, None), ("Away", 2.15, None)])]}, start="2030-01-05T18:00:00Z")
        ev["sport_key"] = "basketball_nba"
        arbs = find_arbs([ev], Config(min_profit_pct=0), NOW)
        b = self.restored_alerter(items=arbs, checked=["basketball_nba"])
        b.handle([], ["icehockey_nhl"], now=time.time())                  # another sport: stays
        self.assertEqual(self.sent, [])
        b.handle([], ["basketball_nba"], now=time.time())                 # its sport, not found: gone now
        self.assertEqual(self.sent[0][0], "PATCH")
        self.assertTrue(self.sent[0][1].startswith("❌ GONE"))
        self.assertEqual(b.restored, {})

    def test_restored_prop_waits_for_its_game_not_the_grace_period(self):
        from arbbot import close_started
        ev = prop_event({"Pinnacle": (1.91, 1.91), "DK": (2.15, 1.70)}, start="2030-01-05T23:00:00Z")
        ev["bookmakers"][0]["key"] = "pinnacle"
        [bet] = find_evs([ev], self.cfg.for_props(), NOW)
        b = self.restored_alerter(EVAlerter, [bet], noun="+EV props")
        b.started -= 10_000                                              # long past the grace period
        b.handle([], checked_events={"p2"}, now=time.time())              # another game's props
        self.assertEqual(self.sent, [])
        self.assertIn(bet.key, b.restored)
        close_started(datetime(2030, 1, 5, 23, 1, tzinfo=timezone.utc), b)    # its game starts
        self.assertTrue(self.sent and self.sent[0][1].startswith("❌ GONE"))
        self.assertEqual(b.restored, {})

    def test_old_state_files_still_use_the_grace_period(self):
        import json
        ev = event({"A": [("h2h", [("Home", 2.10, None), ("Away", 1.80, None)])],
                    "B": [("h2h", [("Home", 1.70, None), ("Away", 2.15, None)])]}, start="2030-01-05T18:00:00Z")
        ev["sport_key"] = "basketball_nba"
        arbs = find_arbs([ev], Config(min_profit_pct=0), NOW)
        a = self.wire(Alerter(self.cfg, dry_run=False))
        a.handle(arbs, ["basketball_nba"], now=time.time())
        path = a._state_path()
        data = json.loads(path.read_text())
        for v in data.values():
            for k in ("sport_key", "event_id", "commence_time", "card"):
                v.pop(k, None)
        path.write_text(json.dumps(data))
        b = self.wire(Alerter(self.cfg, dry_run=False))
        self.sent.clear()
        b.handle([], ["basketball_nba"], now=time.time())
        self.assertEqual(self.sent, [])                                  # no scope saved: wait
        b.started -= 1000
        b.handle([], ["basketball_nba"], now=time.time())
        self.assertTrue(self.sent[0][1].startswith("❌ GONE"))

    # ---- card edits
    def test_card_edited_when_anything_on_it_changes(self):
        a = self.wire(EVAlerter(self.cfg, dry_run=False))
        def scan(b_price, sharp=(1.91, 1.91)):
            ev = ev_event([("Home", sharp[0], None), ("Away", sharp[1], None)],
                          {"A": [("Home", 2.15, None)], "B": [("Home", b_price, None)]})
            [bet] = find_evs([ev], self.cfg, NOW)
            a.handle([bet], ["Test"], now=time.time())
            return bet
        scan(2.10)
        scan(2.10)
        self.assertEqual([m for m, _ in self.sent], ["POST"])            # nothing changed: no edit
        scan(1.80)                                                       # only the 'every book' list changed
        self.assertEqual([m for m, _ in self.sent], ["POST", "PATCH"])

    def test_dry_run_still_prints_updates(self):
        import io, contextlib
        a = Alerter(Config(log_file=""), dry_run=True)
        arbs = find_arbs(demo_events(), Config())
        a.handle(arbs, ["basketball_nba"], now=1000)
        moved = find_arbs(demo_events(), Config())
        moved[0].legs[0].price += 0.05
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            a.handle(moved, ["basketball_nba"], now=1060)
        self.assertIn("↻ updated", out.getvalue())

    # ---- money
    def test_thin_arb_still_gets_skip_lines(self):
        ev = event({"A": [("h2h", [("Home", 2.02, None), ("Away", 1.80, None)])],
                    "B": [("h2h", [("Home", 1.80, None), ("Away", 1.995, None)])]})
        [arb] = find_arbs([ev], Config(alert_mode="balanced", min_profit_pct=0.3, bankroll=100), NOW)
        from arbbot import american
        for i, leg in enumerate(arb.legs):
            worst = arb.worst_ok_price(i)
            self.assertIsNotNone(worst)
            self.assertLessEqual(worst, leg.price)
            a = int(american(worst))                                    # the line as the card prints it
            shown = 1 + a / 100 if a > 0 else 1 + 100 / -a
            self.assertGreaterEqual(shown * leg.stake, arb.total_stake - 1e-6)   # never a losing line

    def test_stake_rounding_corner_cases(self):
        from arbbot import round_stake
        small_cap = Config(round_stakes=5, ev_bankroll=100, kelly_fraction=1, ev_max_stake_pct=3)
        self.assertEqual(kelly_stake(0.6, 2.10, small_cap, mult=1.5), 3.0)       # not $0
        self.assertEqual(round_stake(40, 12.5, Config(round_stakes=5)), 10.0)    # parlay cap $12.50
        legs = [ev_leg("g1", {"DK": 2.30}), ev_leg("g2", {"DK": 2.30})]
        cfg = Config(parlay_min_ev_pct=1, parlay_leg_min_ev_pct=1, round_stakes=5, ev_bankroll=1250,
                     parlay_max_stake_pct=1, kelly_fraction=1)
        [p] = find_parlays(legs, cfg)
        self.assertLessEqual(p.stake, 12.5)

    def test_kept_parlay_survives_many_better_legs(self):
        cfg = Config(parlay_min_ev_pct=1, parlay_leg_min_ev_pct=1, parlay_max_legs=2, parlay_max_alerts=3)
        a, b = ev_leg("g1", {"DK": 2.10}), ev_leg("g2", {"DK": 2.12})
        [ab] = find_parlays([a, b], cfg)
        more = [ev_leg(f"g{i}", {"DK": 2.20 + i / 100}) for i in range(3, 11)]
        self.assertNotIn(ab.key, {p.key for p in find_parlays([a, b] + more, cfg)})
        self.assertIn(ab.key, {p.key for p in find_parlays([a, b] + more, cfg, keep={ab.key})})

    def test_parlay_legs_follow_the_ev_price_limits(self):
        cfg = Config(parlay_min_ev_pct=1, parlay_leg_min_ev_pct=1, max_ev_pct=25)
        a, b = ev_leg("g1", {"DK": 2.20}), ev_leg("g2", {"DK": 2.20})
        self.assertTrue(find_parlays([a, b], cfg))
        b.board = [(bk, 2.80, 40.0, ln, True) for bk, _, _, ln, _ in b.board]   # an outlier-sized edge
        self.assertEqual(find_parlays([a, b], cfg), [])

    # ---- settings
    def env(self, **kv):
        import os
        from unittest import mock
        return mock.patch.dict(os.environ, kv)

    def test_numbers_must_be_whole_and_finite(self):
        for kv, key in ((dict(POLL_SECONDS="0.5"), "POLL_SECONDS"), (dict(POLL_SECONDS="inf"), "POLL_SECONDS"),
                        (dict(MONTHLY_CREDITS="1e400"), "MONTHLY_CREDITS"), (dict(POLL_SECONDS="0"), "POLL_SECONDS"),
                        (dict(GAME_MINUTES="basketball_nba=inf"), "GAME_MINUTES"),
                        (dict(TIMEZONE="America/New York"), "TIMEZONE"), (dict(ACTIVE_HOURS="8am-10pm"), "ACTIVE_HOURS"),
                        (dict(CONFIDENCE_STAKES="1,0.75"), "CONFIDENCE_STAKES")):
            with self.env(**kv), self.assertRaisesRegex(ValueError, key):
                Config.from_env()
        with self.env(CONFIDENCE_STAKES="1;0.8;0.6"):
            self.assertEqual(Config.from_env().confidence_stakes, "1,0.8,0.6")

    def test_set_checks_each_new_value_on_its_own(self):
        import os
        from arbbot import check_settings
        with self.env(POLL_SECONDS="6o"):
            problem, others = check_settings({"BANKROLL": "2OO"})
            self.assertTrue(problem.startswith("BANKROLL"), problem)
            problem, others = check_settings({"BANKROLL": "200"})
            self.assertEqual(problem, "")
            self.assertTrue(others and others[0].startswith("POLL_SECONDS"))
            self.assertEqual(os.environ["POLL_SECONDS"], "6o")           # environment left as it was
            self.assertNotIn("BANKROLL", os.environ)
        problem, _ = check_settings({"MY_BOOKS": "fanduel", "BANKROLL": "abc"})
        self.assertTrue(problem.startswith("BANKROLL"))


class ResultsReviewFixes(unittest.TestCase):
    """Issues found by the independent review of the results channel."""
    START, LATER = BetResults.START, BetResults.LATER
    setUp, tearDown, scores = BetResults.setUp, BetResults.tearDown, BetResults.scores
    log_ev, log_parlay = BetResults.log_ev, BetResults.log_parlay

    def test_parlay_realerted_at_a_new_price_counts_once(self):
        legs = [ev_leg("g1", {"DK": 2.20}), ev_leg("g2", {"DK": 2.20})]
        cfg = Config(parlay_min_ev_pct=1, parlay_leg_min_ev_pct=1)
        a = ParlayAlerter(self.cfg, dry_run=True)
        a.handle(find_parlays(legs, cfg), now=1000)
        a.handle([], now=1060)                                           # gone...
        legs[0].board = [(bk, 2.25, ev, ln, ok) for bk, _, ev, ln, ok in legs[0].board]
        a.handle(find_parlays(legs, cfg), now=1120)                      # ...back at a new price
        self.assertEqual(len(Path(self.cfg.parlay_log_file).read_text().splitlines()), 3)
        rows = settle_pending(self.cfg, self.scores({"g1": (1, 3)}), self.LATER)
        self.assertEqual([r["result"] for r in rows], ["loss"])          # one ticket, one result

    def test_parlay_graded_when_its_last_game_ends(self):
        from arbbot import Results
        early = ev_leg("g1", {"DK": 2.20})
        early.sport_key = "soccer_epl"                                   # ends tonight
        late = ev_leg("g2", {"DK": 2.20})
        late.commence_time = "2026-10-04T18:00:00Z"                      # ends tomorrow night
        for b in (early, late):
            EVAlerter(self.cfg, dry_run=True).handle([b], now=1000)
        [p] = find_parlays([early, late], Config(parlay_min_ev_pct=1, parlay_leg_min_ev_pct=1))
        ParlayAlerter(self.cfg, dry_run=True).handle([p], now=1000)
        games = {"g1": ("soccer_epl", datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)),
                 "g2": ("icehockey_nhl", datetime(2026, 10, 4, 21, 0, tzinfo=timezone.utc))}
        clock = [None]
        class Api:   # a game is final only once it's over, and only in its own sport's scores
            calls = []
            def scores(self, sport, days_from=3):
                Api.calls.append(sport)
                return [{"id": gid, "completed": True,
                         "scores": [{"name": "Home", "score": "3"}, {"name": "Away", "score": "1"}]}
                        for gid, (sp, over) in games.items() if sp == sport and over <= clock[0]]
        res = Results(self.cfg, dry_run=False)
        res.send = lambda payload: True
        t = datetime(2026, 10, 3, 22, 0, tzinfo=timezone.utc)
        while t < datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc):      # every 30 minutes
            clock[0] = t
            res.run(Api(), t)
            t += timedelta(minutes=30)
        graded = sorted((r["kind"], r["result"]) for r in _read(self.cfg.ev_results_file))
        self.assertEqual(graded, [("ev", "win"), ("ev", "win"), ("parlay", "win")])
        self.assertLessEqual(len(Api.calls), 4)                          # not every 30 minutes

    def test_parlay_with_a_prop_leg_loses_on_its_main_leg(self):
        from arbbot import day_bets, day_summary
        from datetime import date
        from unittest import mock
        mock.patch("arbbot.PROP_GRADING", False).start()   # a prop leg the bot can't grade
        self.addCleanup(mock.patch.stopall)
        ev = prop_event({"Pinnacle": (1.91, 1.91), "DK": (2.15, 1.70)}, start=self.START)
        ev["bookmakers"][0]["key"] = "pinnacle"
        [prop] = find_evs([ev], self.cfg.for_props(), NOW)
        main = ev_leg("g1", {"DK": 2.20})
        [p] = find_parlays([main, prop], Config(parlay_min_ev_pct=1, parlay_leg_min_ev_pct=1))
        ParlayAlerter(self.cfg, dry_run=True).handle([p], now=1000)
        before = day_summary(day_bets(self.cfg, date(2026, 10, 3)))
        self.assertIn("1 parlay with a prop leg", before)
        self.assertNotIn("prop to check", before)
        [row] = settle_pending(self.cfg, self.scores({"g1": (1, 4)}), self.LATER)
        self.assertEqual(row["result"], "loss")
        self.assertEqual(ev_record(self.cfg, kinds=("parlay",)), "no graded bets yet")  # wins can't be graded
        rows = day_bets(self.cfg, date(2026, 10, 3))
        self.assertEqual(rows[0]["result"], "loss")

    def test_command_line_post_is_not_repeated_by_the_bot(self):
        from arbbot import Results
        from datetime import date
        self.log_ev("g1")
        service = Results(self.cfg, dry_run=False)
        service.send = lambda payload: self.fail("posted twice")
        settle_pending(self.cfg, self.scores({"g1": (4, 2)}), self.LATER)
        cli = Results(self.cfg, dry_run=False)
        cli.send = lambda payload: True
        self.assertEqual(cli.recap(date(2026, 10, 3), self.LATER), "sent")
        self.assertEqual(service.run(self.scores({}), self.LATER), 0)

    def test_failed_post_is_tried_again(self):
        from arbbot import Results
        self.log_ev("g1")
        res = Results(self.cfg, dry_run=False)
        replies = [False, True]
        res.send = lambda payload: replies.pop(0)
        api = self.scores({"g1": (4, 2)})
        self.assertEqual(res.run(api, self.LATER), 0)
        self.assertEqual(res.run(api, self.LATER), 1)
        self.assertEqual(replies, [])

    def test_recap_survives_a_restart(self):
        from arbbot import Results
        from datetime import date
        self.log_ev("g1")
        res = Results(self.cfg, dry_run=False)
        res.send = lambda payload: True
        self.assertTrue(res.recap_due(date(2026, 10, 3)))
        res.daily(self.scores({"g1": (4, 2)}), date(2026, 10, 3), self.LATER)
        again = Results(self.cfg, dry_run=False)
        self.assertFalse(again.recap_due(date(2026, 10, 3)))
        self.assertTrue(again.recap_due(date(2026, 10, 4)))


def _read(name):
    import csv
    with open(name, newline="") as f:
        return list(csv.DictReader(f))


class CardStability(unittest.TestCase):
    """Cards are edited for visible changes only, never for a reshuffle."""

    def test_related_and_board_order_dont_depend_on_rank_or_feed_order(self):
        from arbbot import note_related
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})
        [ml] = find_evs([ev], EVCFG, NOW)
        over = replace(ml, market="totals", line=220.5, point=220.5, outcome="Over")
        spread = replace(ml, market="spreads", line=-3.5, point=-3.5)
        a = EVAlerter(EVCFG, dry_run=True)
        note_related([([ml, over, spread], a, set())])
        first = list(spread.related)
        note_related([([over, ml, spread], a, set())])
        self.assertEqual(spread.related, first)
        books = {"FanDuel": [("Home", 1.95, None)], "BetMGM": [("Home", 1.95, None)], "DK": [("Home", 2.10, None)]}
        one = find_evs([ev_event([("Home", 1.91, None), ("Away", 1.91, None)], books)], EVCFG, NOW)[0]
        two = find_evs([ev_event([("Home", 1.91, None), ("Away", 1.91, None)], dict(reversed(books.items())))],
                       EVCFG, NOW)[0]
        self.assertEqual([r[0] for r in one.board], [r[0] for r in two.board])

    def test_parlay_legs_keep_one_order(self):
        a, b = ev_leg("g1", {"DK": 2.08}), ev_leg("g2", {"DK": 2.20})
        cfg = Config(parlay_min_ev_pct=1, parlay_leg_min_ev_pct=1)
        [p1] = find_parlays([a, b], cfg)
        [p2] = find_parlays([a, b], cfg, keep={p1.key})
        self.assertEqual([x[0].key for x in p1.legs], [x[0].key for x in p2.legs])

    def test_restart_with_the_same_prices_leaves_cards_alone(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = replace(EVCFG, webhook_url="https://main", state_dir=d, ev_log_file="")
            sent = []
            def make():
                a = EVAlerter(cfg, dry_run=False)
                def fake(payload, message_id=None, url=""):
                    sent.append(("PATCH" if message_id else "POST", payload["embeds"][0].get("description", "")))
                    a.send_retryable = False
                    return message_id or "m1"
                a._discord = fake
                return a
            def bet(sharp):
                ev = ev_event([("Home", sharp[0], None), ("Away", sharp[1], None)], {"B": [("Home", 2.20, None)]})
                return find_evs([ev], cfg, NOW)[0]
            t0 = time.time()
            a = make()
            a.handle([bet((1.91, 1.91))], ["Test"], now=t0)
            a.handle([bet((1.89, 1.93))], ["Test"], now=t0 + 60)           # the sharp line moved
            self.assertIn("(was", sent[-1][1])
            n = len(sent)
            b = make()                                                     # restart
            b.handle([bet((1.89, 1.93))], ["Test"], now=t0 + 120)          # same prices as before
            self.assertEqual(len(sent), n)                                 # no edit, no new alert
            b.handle([bet((1.88, 1.94))], ["Test"], now=t0 + 180)          # a real change...
            self.assertEqual(sent[-1][0], "PATCH")
            self.assertIn("(was -110 / -110)", sent[-1][1])                # ...keeps the original line


class SkipLinesNeverLose(unittest.TestCase):
    def test_every_printed_skip_line_still_breaks_even(self):
        from arbbot import american
        cfg = Config(alert_mode="balanced", min_profit_pct=0.01, bankroll=100, round_stakes=5)
        checked = 0
        for plus in range(100, 200, 3):
            for minus in range(-200, -100, 3):
                hp, ap = 1 + plus / 100, 1 + 100 / -minus
                ev = event({"A": [("h2h", [("Home", hp, None), ("Away", 1.30, None)])],
                            "B": [("h2h", [("Home", 1.30, None), ("Away", ap, None)])]})
                for arb in find_arbs([ev], cfg, NOW):
                    for i, leg in enumerate(arb.legs):
                        worst = arb.worst_ok_price(i)
                        if worst is None:
                            continue
                        a = int(american(worst))
                        shown = 1 + a / 100 if a > 0 else 1 + 100 / -a
                        self.assertGreaterEqual(shown * leg.stake, arb.total_stake - 1e-6, (plus, minus, i))
                        checked += 1
        self.assertGreater(checked, 50)


class ParlayBooks(unittest.TestCase):
    def test_outlier_legs_stay_inside_ev_books(self):
        def game(gid):
            books = {"Pinnacle": [("h2h", [("Home", 1.95, None), ("Away", 1.95, None)])],
                     "DraftKings": [("h2h", [("Home", 1.91, None), ("Away", 1.91, None)])],
                     "BetMGM": [("h2h", [("Home", 1.91, None), ("Away", 1.91, None)])],
                     "Caesars": [("h2h", [("Home", 1.90, None), ("Away", 1.92, None)])],
                     "FanDuel": [("h2h", [("Home", 2.30, None), ("Away", 1.62, None)])]}
            ev = event(books)
            for bm in ev["bookmakers"]:
                bm["key"] = {"Pinnacle": "pinnacle", "DraftKings": "draftkings", "BetMGM": "betmgm",
                             "Caesars": "williamhill_us", "FanDuel": "fanduel"}[bm["title"]]
            ev["id"] = gid
            return ev
        cfg = Config(alert_mode="balanced", my_books="draftkings,fanduel", ev_books="draftkings",
                     parlay_min_ev_pct=1, parlay_leg_min_ev_pct=1)
        outs = find_outliers([game("g1"), game("g2")], cfg, NOW)
        self.assertTrue(outs)
        self.assertEqual(find_parlays(outs, cfg), [])                     # FanDuel isn't an EV book
        loose = replace(cfg, ev_books="")
        self.assertTrue(find_parlays(find_outliers([game("g1"), game("g2")], loose, NOW), loose))

    def test_prop_minutes_only_checked_with_props_on(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"PROP_MINUTES": "0", "PROPS_ENABLED": "false"}):
            Config.from_env()
        with mock.patch.dict(os.environ, {"PROP_MINUTES": "0"}), self.assertRaisesRegex(ValueError, "PROP_MINUTES"):
            Config.from_env()


def espn_fake(games):
    """A fake ESPN: games = [{"sport": league path, "id", "date", "home": (id, display, location, name, score),
    "away": (...), "final": bool, "groups": {team id: [(group name, labels, keys, [(player, [stats], dnp)])]}}]"""
    def team(t):
        tid, display, location, name, _ = t
        return {"id": tid, "displayName": display, "location": location, "name": name}
    def get(path):
        league, _, rest = path.rpartition("/")
        if rest.startswith("scoreboard"):
            day = rest.split("dates=")[1][:8]
            return {"events": [{"id": g["id"], "date": g["date"], "status": {"type": {"completed": g["final"]}},
                                "competitions": [{"competitors": [
                {"homeAway": "home", "team": team(g["home"]), "score": str(g["home"][4])},
                {"homeAway": "away", "team": team(g["away"]), "score": str(g["away"][4])}]}]}
                for g in games if g["sport"] == league and g["date"][:10].replace("-", "") == day]}
        eid = rest.split("event=")[1]
        g = next(g for g in games if g["id"] == eid)
        return {"header": {"competitions": [{
                    "status": {"type": {"completed": g["final"], "state": "post" if g["final"] else "in"}},
                    "competitors": [{"id": t[0], "team": team(t), "score": str(t[4])} for t in (g["home"], g["away"])]}]},
                "boxscore": {"players": [{"team": {"id": tid}, "statistics": [
                    {"name": gname, "labels": labels, "keys": keys, "athletes": [
                        {"athlete": {"displayName": who}, "stats": stats, "didNotPlay": dnp}
                        for who, stats, dnp in athletes]}
                    for gname, labels, keys, athletes in groups]} for tid, groups in g["groups"].items()]}}
    return get


NHL_LABELS, NHL_KEYS = ["G", "A", "+/-", "S", "BS"], ["goals", "assists", "plusMinus", "shotsTotal", "blockedShots"]


def nhl_game(cbj_goals=3, final=True, kj=("1", "1", "+1", "4", "0"), kj_dnp=False):
    return {"sport": "hockey/nhl", "id": "401", "date": "2026-10-03T23:00Z", "final": final,
            "home": ("29", "Columbus Blue Jackets", "Columbus", "Blue Jackets", 3),
            "away": ("129", "Utah Mammoth", "Utah", "Mammoth", 2),
            "groups": {"29": [("forwards", NHL_LABELS, NHL_KEYS, [
                ("Kent Johnson", list(kj), kj_dnp),
                ("Zach Werenski", [str(cbj_goals - 1), "2", "+2", "5", "1"], False)])],
                       "129": [("forwards", NHL_LABELS, NHL_KEYS, [
                ("Clayton Keller", ["2", "0", "-1", "6", "0"], False),
                ("Mitchell Marner", ["0", "1", "0", "2", "1"], False)])]}}


class PropGrading(unittest.TestCase):
    def setUp(self):
        import tempfile
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg = Config(min_ev_pct=3, round_stakes=0, ev_log_file=str(d / "ev.csv"),
                          outlier_log_file=str(d / "out.csv"), parlay_log_file=str(d / "par.csv"),
                          ev_results_file=str(d / "res.csv"), closing_file=str(d / "close.csv"),
                          markout_file=str(d / "mk.csv"), log_file="", score_check_file=str(d / "sc.csv"))
        self.games = [nhl_game()]
        mock.patch("arbbot._espn_get", side_effect=lambda path: espn_fake(self.games)(path)).start()
        self.addCleanup(mock.patch.stopall)
        _arbbot._ESPN_CACHE.clear()
        _arbbot._BOX_CACHE.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, player="Kent Johnson", market="player_assists", outcome="Over", point="0.5", price="2.95",
            sport="icehockey_nhl", home="Columbus Blue Jackets", away="Utah Mammoth"):
        return {"event_id": "nhl1", "sport_key": sport, "home_team": home, "away_team": away,
                "commence_time": "2026-10-03T23:00:00Z", "market": market, "outcome": outcome,
                "point": point, "player": player, "stake": "15", "price": price, "kind": "ev"}

    def test_grades_over_under_from_the_box_score(self):
        from arbbot import grade_prop
        self.assertEqual(grade_prop(self.row()), (("win", 29.25), "1"))                    # 1 assist > 0.5
        self.assertEqual(grade_prop(self.row(market="player_shots_on_goal", outcome="Under", point="2.5"))[0][0],
                         "loss")                                                            # 4 shots
        self.assertEqual(grade_prop(self.row(market="player_points", point="2.0"))[0][0], "push")  # 1+1

    def test_box_score_that_doesnt_add_up_is_not_trusted(self):
        from arbbot import grade_prop
        self.games = [nhl_game(cbj_goals=6)]                  # players' goals 6 vs a final score of 3
        res, why = grade_prop(self.row())
        self.assertIsNone(res)
        self.assertIn("didn't check out", why)

    def test_not_final_dnp_and_unknown_players(self):
        from arbbot import grade_prop
        self.games = [nhl_game(final=False)]
        self.assertEqual(grade_prop(self.row()), (None, "not final yet"))
        _arbbot._ESPN_CACHE.clear()
        self.games = [nhl_game(kj=(), kj_dnp=True)]
        self.assertEqual(grade_prop(self.row()), (("push", 0.0), "DNP"))                   # didn't play: void
        self.assertIsNone(grade_prop(self.row(player="Nobody Here"))[0])
        # "Mitch Marner" on the odds feed is "Mitchell Marner" on ESPN.
        self.assertEqual(grade_prop(self.row(player="Mitch Marner"))[1], "1")

    def test_game_matching(self):
        from arbbot import espn_event_id
        self.assertEqual(espn_event_id("icehockey_nhl", "Columbus Blue Jackets", "Utah Mammoth",
                                       "2026-10-03T23:00:00Z"), "401")
        self.assertEqual(espn_event_id("icehockey_nhl", "Utah Mammoth", "Columbus Blue Jackets",   # neutral site
                                       "2026-10-03T23:00:00Z"), "401")
        self.assertIsNone(espn_event_id("icehockey_nhl", "Columbus Blue Jackets", "Utah Mammoth",
                                        "2026-10-06T23:00:00Z"))                           # another day
        clippers = {"sport": "basketball/nba", "id": "9", "date": "2026-10-03T23:00Z", "final": True,
                    "home": ("12", "LA Clippers", "LA", "Clippers", 100), "away": ("1", "Atlanta Hawks", "Atlanta", "Hawks", 90),
                    "groups": {}}
        self.games.append(clippers)
        self.assertEqual(espn_event_id("basketball_nba", "Los Angeles Clippers", "Atlanta Hawks",
                                       "2026-10-03T23:00:00Z"), "9")

    def test_other_sports_read_the_right_columns(self):
        from arbbot import grade_prop
        nba_l = ["MIN", "FG", "3PT", "FT", "REB", "AST", "PTS"]
        nba_k = ["minutes", "fieldGoalsMade-fieldGoalsAttempted", "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
                 "freeThrowsMade-freeThrowsAttempted", "rebounds", "assists", "points"]
        self.games.append({"sport": "basketball/nba", "id": "7", "date": "2026-10-03T23:00Z", "final": True,
                           "home": ("2", "Boston Celtics", "Boston", "Celtics", 30), "away": ("18", "New York Knicks", "New York", "Knicks", 12),
                           "groups": {"2": [("", nba_l, nba_k, [("Jayson Tatum", ["38", "10-21", "3-8", "5-6", "8", "5", "28"], False),
                                                               ("Jaylen Brown", ["30", "1-3", "0-1", "0-0", "2", "1", "2"], False)])],
                                      "18": [("", nba_l, nba_k, [("Jalen Brunson", ["36", "5-9", "2-4", "0-0", "1", "6", "12"], False)])]}})
        row = self.row(player="Jayson Tatum", market="player_threes", point="2.5", sport="basketball_nba",
                       home="Boston Celtics", away="New York Knicks")
        self.assertEqual(grade_prop(row), (("win", 29.25), "3"))                            # 3 of 8 threes
        pra = dict(row, market="player_points_rebounds_assists", point="40.5", outcome="Under")
        self.assertEqual(grade_prop(pra)[1], "41")
        nfl = lambda g, labels, keys, rows: (g, labels, keys, rows)
        self.games.append({"sport": "football/nfl", "id": "5", "date": "2026-10-04T17:00Z", "final": True,
                           "home": ("2", "Buffalo Bills", "Buffalo", "Bills", 27), "away": ("20", "New York Jets", "New York", "Jets", 20),
                           "groups": {"2": [nfl("passing", ["C/ATT", "YDS", "TD"], ["completions/passingAttempts", "passingYards", "passingTouchdowns"],
                                                [("Josh Allen", ["22/30", "260", "2"], False)]),
                                            nfl("rushing", ["CAR", "YDS", "TD"], ["rushingAttempts", "rushingYards", "rushingTouchdowns"],
                                                [("Josh Allen", ["8", "41", "1"], False), ("James Cook", ["15", "70", "0"], False)]),
                                            nfl("receiving", ["REC", "YDS", "TD"], ["receptions", "receivingYards", "receivingTouchdowns"],
                                                [("Khalil Shakir", ["9", "110", "1"], False), ("James Cook", ["13", "150", "1"], False)])],
                                      "20": [nfl("passing", ["C/ATT", "YDS", "TD"], ["completions/passingAttempts", "passingYards", "passingTouchdowns"],
                                                 [("Justin Fields", ["15/25", "180", "1"], False)]),
                                             nfl("receiving", ["REC", "YDS", "TD"], ["receptions", "receivingYards", "receivingTouchdowns"],
                                                 [("Garrett Wilson", ["15", "180", "1"], False)])]}})
        bills = dict(home="Buffalo Bills", away="New York Jets", sport="americanfootball_nfl")
        bills_row = lambda **kw: dict(self.row(**bills), commence_time="2026-10-04T17:00:00Z", **kw)
        self.assertEqual(grade_prop(bills_row(player="Josh Allen", market="player_rush_yds", point="39.5"))[1], "41")
        self.assertEqual(grade_prop(bills_row(player="Josh Allen", market="player_pass_yds", point="39.5"))[1], "260")
        self.assertEqual(grade_prop(bills_row(player="Khalil Shakir", market="player_anytime_td", outcome="Yes",
                                              point=""))[0][0], "win")                     # no rushing line: 0 + 1
        self.assertEqual(grade_prop(bills_row(player="Josh Allen", market="player_pass_completions", point="21.5"))[1], "22")
        self.games.append({"sport": "baseball/mlb", "id": "3", "date": "2026-10-03T23:00Z", "final": True,
                           "home": ("19", "Los Angeles Dodgers", "Los Angeles", "Dodgers", 4), "away": ("25", "San Diego Padres", "San Diego", "Padres", 1),
                           "groups": {"19": [("batting", ["H-AB", "AB", "R", "H", "RBI", "HR"], ["hits-atBats", "atBats", "runs", "hits", "RBIs", "homeRuns"],
                                              [("Shohei Ohtani", ["2-4", "4", "4", "2", "3", "1"], False)]),
                                             ("pitching", ["IP", "H", "R", "ER", "BB", "K"], ["fullInnings.partInnings", "hits", "runs", "earnedRuns", "walks", "strikeouts"],
                                              [("Yoshinobu Yamamoto", ["6.1", "4", "1", "1", "2", "9"], False)])],
                                      "25": [("batting", ["H-AB", "AB", "R", "H", "RBI", "HR"], ["hits-atBats", "atBats", "runs", "hits", "RBIs", "homeRuns"],
                                              [("Manny Machado", ["1-4", "4", "1", "1", "1", "1"], False)])]}})
        dodgers = lambda **kw: dict(self.row(sport="baseball_mlb", home="Los Angeles Dodgers", away="San Diego Padres"), **kw)
        self.assertEqual(grade_prop(dodgers(player="Shohei Ohtani", market="batter_hits", point="1.5"))[1], "2")
        self.assertEqual(grade_prop(dodgers(player="Yoshinobu Yamamoto", market="pitcher_strikeouts", point="7.5"))[1], "9")
        self.assertEqual(grade_prop(dodgers(player="Yoshinobu Yamamoto", market="pitcher_outs", point="18.5"))[1], "19")

    def test_props_graded_and_posted_like_everything_else(self):
        from arbbot import day_bets, result_line
        from datetime import date
        ev = prop_event({"Pinnacle": (1.91, 1.91), "DK": (2.15, 1.70)}, start="2026-10-03T23:00:00Z",
                        market="player_assists", player="Kent Johnson", line=0.5)
        ev.update(id="nhl1", sport_key="icehockey_nhl", home_team="Columbus Blue Jackets", away_team="Utah Mammoth")
        ev["bookmakers"][0]["key"] = "pinnacle"
        [b] = find_evs([ev], self.cfg.for_props(), NOW)
        EVAlerter(self.cfg, dry_run=True).handle([b], now=1000)
        later = datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc)
        [row] = settle_pending(self.cfg, self.scores_unused(), later)
        self.assertEqual((row["result"], row["actual"]), ("win", "1"))
        [r] = day_bets(self.cfg, date(2026, 10, 3))
        line = result_line(r, self.cfg, later)
        self.assertTrue(line.startswith("✅ **Kent Johnson Over 0.5 Assists"))
        self.assertIn("Box score: 1", line)

    def scores_unused(self):
        test = self
        class Api:
            def scores(self, sport, days_from=3):
                test.fail("props don't need Odds API scores")
        return Api()

    def test_check_props_report(self):
        import io, contextlib
        from arbbot import check_props
        cfg = replace(self.cfg, prop_sports=["icehockey_nhl", "tennis_atp"])
        EVAlerter(self.cfg, dry_run=True)   # nothing logged yet
        append_csv(self.cfg.ev_log_file, _arbbot.EV_LOG_FIELDS, {
            **self.row(), "first_seen": "2026-10-03T20:00:00+00:00", "sport": "NHL",
            "matchup": "Utah Mammoth @ Columbus Blue Jackets", "live": False, "n_outcomes": 2, "book": "DK"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            check_props(cfg, datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc))
        text = out.getvalue()
        self.assertIn("✅ box score checks out", text)
        self.assertIn("Assists: Zach Werenski 2", text)
        self.assertIn("Shots on Goal: Clayton Keller 6", text)
        self.assertIn("ATP: no box scores", text)
        self.assertIn("Kent Johnson Over 0.5 Assists", text)
        self.assertIn("✅ win (box score: 1)", text)
        self.assertFalse(Path(self.cfg.ev_results_file).exists())          # a check saves nothing

    def test_unsupported_props_stay_manual(self):
        from arbbot import gradable
        self.assertFalse(gradable(self.row(market="player_double_double", sport="basketball_nba")))
        self.assertTrue(gradable(self.row(market="batter_total_bases", sport="baseball_mlb")))   # MLB's own box
        self.assertTrue(gradable(self.row()))



class MyBooksAreTheLimit(unittest.TestCase):
    def test_ev_books_never_go_outside_my_books(self):
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)],
                      {"Hard Rock Bet": [("Home", 2.20, None)], "FanDuel": [("Home", 2.10, None)]})
        ev["bookmakers"][1]["key"], ev["bookmakers"][2]["key"] = "hardrockbet", "fanduel"
        both = Config(min_ev_pct=3, my_books="fanduel,draftkings", ev_books="hardrockbet,fanduel,ballybet")
        [b] = find_evs([ev], both, NOW)
        self.assertEqual(b.book, "FanDuel")                               # not Hard Rock, even though it's better
        self.assertEqual([r[0] for r in b.board], ["FanDuel"])
        self.assertEqual(find_evs([ev], Config(min_ev_pct=3, ev_books="hardrockbet"), NOW)[0].book, "Hard Rock Bet")
        self.assertEqual(find_evs([ev], Config(min_ev_pct=3, my_books="fanduel", ev_books="hardrockbet"), NOW), [])



class OutlierGuards(unittest.TestCase):
    def stamp(self, ev, title, age):
        ts = (NOW - timedelta(seconds=age)).isoformat().replace("+00:00", "Z")
        for bm in ev["bookmakers"]:
            if bm["title"] == title:
                bm["last_update"] = ts
                for m in bm["markets"]:
                    m["last_update"] = ts

    def test_a_book_that_jumps_away_from_the_market_is_not_an_outlier(self):
        from arbbot import PriceHistory
        h = PriceHistory()
        self.assertEqual(find_outliers([outlier_event(1.25, 4.00)], Config(), NOW, h), [])   # all in line
        jumped = outlier_event(1.85, 1.95)               # Stale moved away (it saw the goal first)
        self.assertEqual(find_outliers([jumped], Config(), NOW + timedelta(seconds=60), h), [])
        self.assertEqual(find_outliers([jumped], Config(), NOW + timedelta(seconds=120), h), [])   # still ahead
        # A book that stays put while the market moves IS stale: that's a real outlier.
        h2 = PriceHistory()
        before = outlier_event(1.85, 1.95)
        for bm in before["bookmakers"]:
            if bm["title"] != "Stale":
                bm["markets"][0]["outcomes"] = [{"name": "Home", "price": 1.80}, {"name": "Away", "price": 2.05}]
        find_outliers([before], Config(), NOW, h2)
        now_out = find_outliers([outlier_event(1.85, 1.95)], Config(), NOW + timedelta(seconds=60), h2)
        self.assertEqual([o.book for o in now_out], ["Stale"])
        # ...and it stays an outlier when it re-quotes the same price (a newer stamp alone means nothing).
        again = outlier_event(1.85, 1.95)
        self.stamp(again, "Stale", 0)
        for t in ("Pinnacle", "DK", "FD", "MGM"):
            self.stamp(again, t, 30)
        self.assertEqual([o.book for o in find_outliers([again], Config(), NOW, h2)], ["Stale"])

    def test_first_look_uses_a_big_stamp_gap_only(self):
        ev = outlier_event(1.85, 1.95)
        for t in ("Pinnacle", "DK", "FD", "MGM"):
            self.stamp(ev, t, 40)
        self.assertEqual(len(find_outliers([ev], Config(), NOW)), 1)              # 40s: could be anything
        for t in ("Pinnacle", "DK", "FD", "MGM"):
            self.stamp(ev, t, 100)
        self.assertEqual(find_outliers([ev], Config(alert_mode="balanced", max_age_seconds=300), NOW), [])

    def test_prop_markets_share_one_stamp_so_it_isnt_used(self):
        books = {"Pinnacle": (1.91, 1.91), "DK": (1.90, 1.92), "FD": (1.91, 1.91), "MGM": (1.92, 1.90),
                 "Stale": (2.60, 1.45)}
        ev = prop_event(books, start="2026-10-03T18:00:00Z")
        ev["bookmakers"][0]["key"] = "pinnacle"
        for bm in ev["bookmakers"]:   # every book's market also holds another player's line
            bm["markets"][0]["outcomes"] += [{"name": "Over", "description": "Anthony Davis", "price": 1.91, "point": 24.5},
                                             {"name": "Under", "description": "Anthony Davis", "price": 1.91, "point": 24.5}]
        self.stamp(ev, "Stale", 0)                       # Stale just moved... someone else's line
        for t in ("Pinnacle", "DK", "FD", "MGM"):
            self.stamp(ev, t, 600)
        self.assertEqual([o.book for o in find_outliers([ev], Config(), NOW)], ["Stale"])

    def test_the_sharp_book_has_to_agree(self):
        books = {"Pinnacle": [("h2h", [("Home", 1.80, None), ("Away", 2.10, None)])],   # sharp: Home ~54%
                 "DK": [("h2h", [("Home", 1.24, None), ("Away", 4.00, None)])],          # the soft books
                 "FD": [("h2h", [("Home", 1.26, None), ("Away", 3.90, None)])],          # are the stale ones
                 "MGM": [("h2h", [("Home", 1.25, None), ("Away", 3.95, None)])],
                 "Stale": [("h2h", [("Home", 1.85, None), ("Away", 1.95, None)])]}
        ev = event(books, start="2026-10-03T11:00:00Z")
        ev["bookmakers"][0]["key"] = "pinnacle"
        self.assertNotIn("Stale", [o.book for o in find_outliers([ev], Config(), NOW)])



class Scoreboard(unittest.TestCase):
    START, LATER = BetResults.START, BetResults.LATER
    setUp, tearDown, scores = BetResults.setUp, BetResults.tearDown, BetResults.scores
    log_ev, log_parlay = BetResults.log_ev, BetResults.log_parlay

    def wire(self, res):
        from unittest import mock
        self.calls = []
        def fake(url, payload, method="POST", message_id=None):
            self.calls.append((method, message_id))
            if method == "PATCH" and message_id in self.deleted:
                raise urllib.error.HTTPError(url, 404, "Unknown Message", {}, None)
            return {"id": f"b{len(self.calls)}"}
        self.deleted = set()
        mock.patch("arbbot._webhook", side_effect=fake).start()
        self.addCleanup(mock.patch.stopall)
        return res

    def test_one_message_kept_up_to_date(self):
        from arbbot import Results
        self.log_ev("g1")
        res = self.wire(Results(self.cfg, dry_run=False))
        res.send = lambda payload: True
        res.tick(self.scores({}), self.LATER)                       # nothing graded yet: the board goes up
        self.assertEqual(self.calls, [("POST", None)])
        res.tick(self.scores({}), self.LATER)                       # nothing changed: no edit
        self.assertEqual(len(self.calls), 1)
        res.tick(self.scores({"g1": (4, 2)}), self.LATER)           # a result: the same message is edited
        self.assertEqual(self.calls[-1], ("PATCH", "b1"))
        again = self.wire(Results(self.cfg, dry_run=False))          # restart: still the same message
        self.log_ev("g2")
        again.send = lambda payload: True
        again.tick(self.scores({"g2": (0, 1)}), self.LATER)
        self.assertEqual(self.calls[-1], ("PATCH", "b1"))
        self.deleted.add("b1")                                       # someone deleted it: a new one goes up
        self.log_ev("g3")
        again.tick(self.scores({"g3": (2, 1)}), self.LATER)
        self.assertEqual(self.calls[-2:], [("PATCH", "b1"), ("POST", None)])

    def test_board_and_command_line_show_the_same_numbers(self):
        import io, contextlib
        from arbbot import scoreboard_text, Results, run
        self.log_ev("g1")
        self.log_parlay(["g2", "g3"])
        settle_pending(self.cfg, self.scores({"g1": (4, 2), "g2": (1, 3), "g3": (1, 3)}), self.LATER)
        text = scoreboard_text(self.cfg, self.LATER)
        self.assertIn("Today · Sat Oct 3", text)
        self.assertIn("📈 +EV: 1-0", text)
        self.assertIn("📦 Parlays: 0-1", text)
        self.assertIn("Last 7 days", text)
        self.assertIn("All time", text)
        self.assertTrue(ev_record(self.cfg).startswith("1 bets, 1-0-0"))   # +EV only, not outliers or parlays

    def test_arbs_counted_once_each(self):
        from arbbot import arbs_on, LOG_FIELDS
        from datetime import date
        for at, pct, secs in (("20:00", 3.5, 40), ("20:05", 3.1, 20), ("21:00", 2.0, 300)):
            append_csv(self.cfg.log_file, LOG_FIELDS, {"first_seen": f"2026-10-03T{at}:00+00:00", "seconds_open": secs,
                                                       "matchup": "A @ B" if at < "21" else "C @ D", "market": "h2h",
                                                       "line": "", "best_profit_pct": pct})
        line = arbs_on(self.cfg, date(2026, 10, 3))
        self.assertIn("2 different (3 alerts)", line)
        self.assertIn("+$5.50", line)                                 # 3.5% + 2.0% of $100, once each
        self.assertIn("half were gone within 40s", line)



class RecapRetries(unittest.TestCase):
    START, LATER = BetResults.START, BetResults.LATER
    setUp, tearDown, scores = BetResults.setUp, BetResults.tearDown, BetResults.scores
    log_ev = BetResults.log_ev

    def test_failed_recap_doesnt_regrade_or_retry_every_pass(self):
        from arbbot import Results
        from datetime import date
        self.log_ev("g1")
        res = Results(self.cfg, dry_run=False)
        res.send = lambda payload: False                             # Discord down
        res.update_board = lambda now=None: None
        api = self.scores({})                                        # the game never shows as final
        day = date(2026, 10, 3)
        self.assertTrue(res.recap_due(day))
        res.daily(api, day, self.LATER)
        self.assertFalse(res.recap_due(day))                         # not again on the next pass...
        res.recap_tried -= 3600                                      # ...but after RESULTS_MINUTES
        self.assertTrue(res.recap_due(day))
        res.daily(api, day, self.LATER)
        self.assertEqual(len(api.calls), 1)                          # the full grading pass ran once

    def test_card_posted_by_hand_midday_doesnt_cancel_the_morning_recap(self):
        from arbbot import Results
        from datetime import date
        self.log_ev("g1")
        res = Results(self.cfg, dry_run=False)
        res.send = lambda payload: True
        self.assertEqual(res.recap(date(2026, 10, 3), self.LATER, finished=False), "sent")
        self.assertTrue(Results(self.cfg, dry_run=False).recap_due(date(2026, 10, 3)))
        self.assertEqual(res.recap(date(2026, 10, 1), self.LATER), "nothing")   # no bets that day

    def test_equal_prices_pick_the_same_book_whatever_the_feed_order(self):
        books = {"Pinnacle": [("h2h", [("Home", 1.91, None), ("Away", 1.91, None)])],
                 "DK": [("h2h", [("Home", 1.91, None), ("Away", 1.91, None)])],
                 "MGM": [("h2h", [("Home", 1.91, None), ("Away", 1.91, None)])],
                 "CZR": [("h2h", [("Home", 1.90, None), ("Away", 1.92, None)])],
                 "FanDuel": [("h2h", [("Home", 2.40, None), ("Away", 1.55, None)])],
                 "Fanatics": [("h2h", [("Home", 2.40, None), ("Away", 1.60, None)])]}
        ev = event(books, start="2026-10-03T11:00:00Z")
        ev["bookmakers"][0]["key"] = "pinnacle"
        first = [o.book for o in find_outliers([ev], Config(), NOW)]
        ev["bookmakers"] = ev["bookmakers"][:4] + ev["bookmakers"][4:][::-1]
        self.assertEqual([o.book for o in find_outliers([ev], Config(), NOW)], first)
        a = event({"FanDuel": [("h2h", [("Home", 2.15, None), ("Away", 1.80, None)])],
                   "BetMGM": [("h2h", [("Home", 2.15, None), ("Away", 1.75, None)])],
                   "DK": [("h2h", [("Home", 1.70, None), ("Away", 2.15, None)])]})
        legs = [l.book for l in find_arbs([a], Config(min_profit_pct=0), NOW)[0].legs]
        a["bookmakers"] = a["bookmakers"][::-1]
        self.assertEqual([l.book for l in find_arbs([a], Config(min_profit_pct=0), NOW)[0].legs], legs)



class PropGradingSafety(unittest.TestCase):
    """Wrong grades are worse than no grade."""

    def box(self, names):
        return {"teams": {"1": {"name": "A", "score": 0, "players": {
            _arbbot._norm(n): {"name": n, "dnp": False, "groups": {}} for n in names}}}}

    def test_never_grades_someone_elses_stats(self):
        from arbbot import find_player
        box = self.box(["Amon-Ra St. Brown", "William Contreras", "Jaylin Williams", "Mitchell Marner", "Cameron Thomas"])
        for scratched in ("A.J. Brown", "Willson Contreras", "Jalen Williams"):
            self.assertIsNone(find_player(box, scratched), scratched)
        self.assertEqual(find_player(box, "Mitch Marner")["name"], "Mitchell Marner")
        self.assertEqual(find_player(box, "Cam Thomas")["name"], "Cameron Thomas")
        self.assertEqual(find_player(self.box(["AJ Brown"]), "A.J. Brown")["name"], "AJ Brown")

    def test_anytime_td_counts_returns_and_only_trusts_a_complete_zero(self):
        from arbbot import grade_prop
        from unittest import mock
        groups = {"rushing": {}, "receiving": {"TD": "0", "REC": "3"}, "puntreturns": {"TD": "1"}}
        turpin = {"name": "KaVontae Turpin", "dnp": False, "groups": groups}
        quiet = {"name": "Jake Ferguson", "dnp": False, "groups": {"receiving": {"TD": "0", "REC": "4"}}}
        box = {"final": True, "touchdowns": 1, "teams": {"1": {"name": "Dallas Cowboys", "score": 7, "players": {
            "kavontaeturpin": turpin, "jakeferguson": quiet}}}}
        row = {"sport_key": "americanfootball_nfl", "home_team": "Dallas Cowboys", "away_team": "X", "commence_time": "",
               "market": "player_anytime_td", "outcome": "Yes", "point": "", "stake": "20", "price": "4.5"}
        with mock.patch("arbbot.game_box", return_value=(box, "")):
            self.assertEqual(grade_prop(dict(row, player="KaVontae Turpin"))[0], ("win", 70.0))   # punt-return TD
            self.assertEqual(grade_prop(dict(row, player="Jake Ferguson"))[0], ("loss", -20.0))   # all TDs credited
            box["touchdowns"] = 2                                       # a TD nobody in the box is credited with
            self.assertIsNone(grade_prop(dict(row, player="Jake Ferguson"))[0])


class StateUpgrades(unittest.TestCase):
    START, LATER = BetResults.START, BetResults.LATER
    setUp, tearDown, scores = BetResults.setUp, BetResults.tearDown, BetResults.scores
    log_ev = BetResults.log_ev

    def test_older_state_files_keep_their_recap_and_posted_marks(self):
        import json
        from arbbot import Results
        from datetime import date
        path = Path(self.cfg.state_dir) / "results_posted.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"posted": {"x": "2026-10-03T18:00:00Z"}, "recap_day": "2026-10-02"}))
        res = Results(self.cfg, dry_run=False)
        self.assertFalse(res.recap_due(date(2026, 10, 2)))             # already recapped before the upgrade
        res._save(self.LATER)
        self.assertIn("2026-10-02", json.loads(path.read_text())["recap_days"])
        path.write_text(json.dumps({"g1|h2h|Home|": "2026-10-03T18:00:00Z"}))   # the first format
        self.assertIn("g1|h2h|Home|", Results(self.cfg, dry_run=False).posted)

    def test_rate_limited_scoreboard_edit_is_retried(self):
        from arbbot import Results
        from unittest import mock
        self.log_ev("g1")
        replies = [{"id": "b1"}, None, {"id": "b1"}]                   # post, then a rate-limited edit
        calls = []
        def fake(url, payload, method="POST", message_id=None):
            calls.append(method)
            return replies.pop(0)
        mock.patch("arbbot._webhook", side_effect=fake).start()
        self.addCleanup(mock.patch.stopall)
        res = Results(self.cfg, dry_run=False)
        res.update_board(self.LATER)
        settle_pending(self.cfg, self.scores({"g1": (4, 2)}), self.LATER)
        res.update_board(self.LATER)                                   # 429 x3: not counted as done
        res.update_board(self.LATER)                                   # so it's tried again
        self.assertEqual(calls, ["POST", "PATCH", "PATCH"])



def league_fake(pages):
    """A fake for the league stats sites: {url suffix: json}."""
    def get(url):
        for suffix, page in pages.items():
            if url.endswith(suffix):
                if isinstance(page, Exception):
                    raise page
                return page
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)
    return get


NHL_PAGES = {
    "/score/2026-10-03": {"games": [{"id": 2025020055, "startTimeUTC": "2026-10-03T23:00:00Z", "gameState": "OFF",
                                     "homeTeam": {"id": 29, "name": {"default": "Blue Jackets"}, "score": 3},
                                     "awayTeam": {"id": 68, "name": {"default": "Mammoth"}, "score": 2}}]},
    "/score/2026-10-04": {"games": []},
    "/gamecenter/2025020055/boxscore": {
        "gameState": "OFF",
        "homeTeam": {"id": 29, "commonName": {"default": "Blue Jackets"}, "score": 3},
        "awayTeam": {"id": 68, "commonName": {"default": "Mammoth"}, "score": 2},
        "playerByGameStats": {
            "homeTeam": {"forwards": [{"playerId": 1, "name": {"default": "K. Johnson"}, "goals": 1, "assists": 1, "sog": 4, "blockedShots": 0}],
                         "defense": [{"playerId": 2, "name": {"default": "Z. Werenski"}, "goals": 2, "assists": 2, "sog": 5, "blockedShots": 1}],
                         "goalies": [{"playerId": 3, "name": {"default": "E. Merzlikins"}, "saveShotsAgainst": "28/30", "goalsAgainst": 2}]},
            "awayTeam": {"forwards": [{"playerId": 4, "name": {"default": "C. Keller"}, "goals": 2, "assists": 0, "sog": 6, "blockedShots": 0}],
                         "defense": [], "goalies": [{"playerId": 5, "name": {"default": "K. Vejmelka"}, "saves": 31, "goalsAgainst": 3}]}}},
    "/gamecenter/2025020055/play-by-play": {"rosterSpots": [
        {"teamId": 29, "playerId": 1, "firstName": {"default": "Kent"}, "lastName": {"default": "Johnson"}},
        {"teamId": 29, "playerId": 2, "firstName": {"default": "Zach"}, "lastName": {"default": "Werenski"}},
        {"teamId": 29, "playerId": 3, "firstName": {"default": "Elvis"}, "lastName": {"default": "Merzlikins"}},
        {"teamId": 68, "playerId": 4, "firstName": {"default": "Clayton"}, "lastName": {"default": "Keller"}},
        {"teamId": 68, "playerId": 5, "firstName": {"default": "Karel"}, "lastName": {"default": "Vejmelka"}}]},
}
MLB_PAGES = {
    "schedule?sportId=1&date=2026-10-03": {"dates": [{"games": [{
        "gamePk": 776, "gameDate": "2026-10-03T23:05:00Z", "status": {"abstractGameState": "Final"},
        "teams": {"home": {"team": {"id": 119, "name": "Los Angeles Dodgers", "teamName": "Dodgers"}, "score": 4},
                  "away": {"team": {"id": 135, "name": "San Diego Padres", "teamName": "Padres"}, "score": 1}}}]}]},
    "schedule?sportId=1&date=2026-10-04": {"dates": []},
    "/game/776/boxscore": {"teams": {
        "home": {"team": {"name": "Los Angeles Dodgers"}, "teamStats": {"batting": {"runs": 4}}, "players": {
            "ID660271": {"person": {"fullName": "Shohei Ohtani"}, "stats": {"batting": {
                "hits": 2, "runs": 4, "homeRuns": 2, "rbi": 3, "baseOnBalls": 0, "strikeOuts": 1, "totalBases": 8}, "pitching": {}}},
            "ID808967": {"person": {"fullName": "Yoshinobu Yamamoto"}, "stats": {"batting": {}, "pitching": {
                "strikeOuts": 9, "hits": 4, "baseOnBalls": 2, "earnedRuns": 1, "inningsPitched": "6.1"}}},
            "ID1": {"person": {"fullName": "Bench Guy"}, "stats": {"batting": {}, "pitching": {}}}}},
        "away": {"team": {"name": "San Diego Padres"}, "teamStats": {"batting": {"runs": 1}}, "players": {
            "ID592450": {"person": {"fullName": "Manny Machado"}, "stats": {"batting": {"hits": 1, "runs": 1, "totalBases": 4}}}}}}},
}


class LeagueStatsSites(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        self.pages = {**NHL_PAGES, **MLB_PAGES}
        mock.patch("arbbot._get_json", side_effect=lambda url: league_fake(self.pages)(url)).start()
        self.espn = mock.patch("arbbot._espn_get", side_effect=OSError("ESPN refused (403)")).start()
        self.addCleanup(mock.patch.stopall)
        _arbbot._ESPN_CACHE.clear()
        _arbbot._BOX_CACHE.clear()

    def row(self, player, market, point, sport="icehockey_nhl", home="Columbus Blue Jackets", away="Utah Mammoth",
            outcome="Over", start="2026-10-03T23:00:00Z"):
        return {"sport_key": sport, "home_team": home, "away_team": away, "commence_time": start, "market": market,
                "outcome": outcome, "point": point, "player": player, "stake": "10", "price": "2.0"}

    def test_nhl_props_from_nhl_com(self):
        from arbbot import grade_prop
        self.assertEqual(grade_prop(self.row("Kent Johnson", "player_assists", "0.5")), (("win", 10.0), "1"))
        self.assertEqual(grade_prop(self.row("Clayton Keller", "player_shots_on_goal", "5.5"))[1], "6")
        self.assertEqual(grade_prop(self.row("Elvis Merzlikins", "player_total_saves", "27.5"))[1], "28")
        self.assertEqual(grade_prop(self.row("Karel Vejmelka", "player_total_saves", "30.5"))[1], "31")
        self.espn.assert_not_called()

    def test_without_full_names_nobody_is_guessed(self):
        from arbbot import grade_prop
        self.pages["/gamecenter/2025020055/play-by-play"] = OSError("down")
        res, why = grade_prop(self.row("Kent Johnson", "player_assists", "0.5"))
        self.assertIsNone(res)                                           # "K. Johnson" isn't enough
        self.assertIn("isn't in the box score", why)

    def test_mlb_props_including_total_bases(self):
        from arbbot import grade_prop
        dodgers = dict(sport="baseball_mlb", home="Los Angeles Dodgers", away="San Diego Padres", start="2026-10-03T23:05:00Z")
        self.assertEqual(grade_prop(self.row("Shohei Ohtani", "batter_total_bases", "2.5", **dodgers)), (("win", 10.0), "8"))
        self.assertEqual(grade_prop(self.row("Yoshinobu Yamamoto", "pitcher_outs", "18.5", **dodgers))[1], "19")
        self.assertEqual(grade_prop(self.row("Yoshinobu Yamamoto", "pitcher_strikeouts", "8.5", **dodgers))[1], "9")
        self.assertEqual(grade_prop(self.row("Bench Guy", "batter_hits", "0.5", **dodgers)), (("push", 0.0), "DNP"))

    def test_falls_back_to_espn_when_the_league_site_is_down(self):
        from arbbot import grade_prop
        self.pages["/score/2026-10-03"] = urllib.error.HTTPError("u", 503, "down", {}, None)
        res, why = grade_prop(self.row("Kent Johnson", "player_assists", "0.5"))
        self.assertIsNone(res)
        self.assertIn("NHL.com", why)
        self.assertIn("ESPN", why)
        self.espn.assert_called()

    def test_check_props_says_where_it_read_from(self):
        import io, contextlib
        from arbbot import check_props
        import tempfile
        d = Path(tempfile.mkdtemp())
        cfg = Config(prop_sports=["icehockey_nhl", "baseball_mlb"], ev_log_file=str(d / "ev.csv"),
                     outlier_log_file=str(d / "out.csv"), parlay_log_file=str(d / "par.csv"),
                     ev_results_file=str(d / "res.csv"), closing_file=str(d / "close.csv"), markout_file=str(d / "mk.csv"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            check_props(cfg, datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc))
        text = out.getvalue()
        self.assertIn("NHL (from NHL.com)", text)
        self.assertIn("MLB (from MLB.com)", text)
        self.assertEqual(text.count("✅ box score checks out"), 2)
        self.assertIn("Shots on Goal: Clayton Keller 6", text)
        self.assertIn("Total Bases: Shohei Ohtani 8", text)



class OutlierHistoryRules(unittest.TestCase):
    def test_two_small_steps_are_still_a_jump(self):
        from arbbot import PriceHistory
        h = PriceHistory()
        cfg = Config(outlier_min_pct=10)
        find_outliers([outlier_event(1.25, 4.00)], cfg, NOW, h)                          # in line
        find_outliers([outlier_event(1.385, 3.40)], cfg, NOW + timedelta(seconds=60), h)  # +8%: under the bar
        self.assertEqual(find_outliers([outlier_event(1.436, 3.20)], cfg, NOW + timedelta(seconds=120), h), [])  # +12%

    def test_out_in_front_expires_if_the_market_never_follows(self):
        from arbbot import PriceHistory
        h = PriceHistory()
        find_outliers([outlier_event(1.25, 4.00)], Config(), NOW, h)
        t = NOW + timedelta(seconds=60)
        self.assertEqual(find_outliers([outlier_event(1.85, 1.95)], Config(), t, h), [])
        ev = lambda: outlier_event(1.85, 1.95)
        later = t + timedelta(minutes=6)                                   # 6 minutes on, nobody followed:
        def fresh(e, when):
            ts = when.isoformat().replace("+00:00", "Z")
            for bm in e["bookmakers"]:
                bm["last_update"] = ts
                for m in bm["markets"]:
                    m["last_update"] = ts
            return e
        self.assertEqual([o.book for o in find_outliers([fresh(ev(), later)], Config(), later, h)], ["Stale"])
        again = later + timedelta(seconds=60)                              # and it isn't flagged again
        self.assertEqual([o.book for o in find_outliers([fresh(ev(), again)], Config(), again, h)], ["Stale"])

    def test_pre_game_history_survives_long_gaps_and_has_no_stamp_shortcut(self):
        from arbbot import PriceHistory
        h = PriceHistory()
        start = "2026-10-04T18:00:00Z"                                    # tomorrow: pre-game
        before = outlier_event(1.85, 1.95, start=start)
        for bm in before["bookmakers"]:
            if bm["title"] != "Stale":
                bm["markets"][0]["outcomes"] = [{"name": "Home", "price": 1.80}, {"name": "Away", "price": 2.05}]
        find_outliers([before], Config(), NOW, h)
        h.prune(NOW + timedelta(hours=4))                                 # checked again 4 hours later
        later = outlier_event(1.85, 1.95, start=start)
        old = (NOW - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        for bm in later["bookmakers"]:                                    # Stale's own stamp is the newest
            if bm["title"] != "Stale":
                bm["last_update"] = old
                for m in bm["markets"]:
                    m["last_update"] = old
        cfg = Config(pregame_max_age_seconds=10**6, far_max_age_seconds=10**6)
        self.assertEqual([o.book for o in find_outliers([later], cfg, NOW + timedelta(hours=4), h)], ["Stale"])


class PropMatchingMore(unittest.TestCase):
    def test_common_nicknames(self):
        from arbbot import find_player
        box = PropGradingSafety.box(None, ["Michael Pittman", "Nicholas Paul", "Gabriel Landeskog", "Mike Evans"])
        self.assertEqual(find_player(box, "Nick Paul")["name"], "Nicholas Paul")
        self.assertEqual(find_player(box, "Gabe Landeskog")["name"], "Gabriel Landeskog")
        self.assertEqual(find_player(box, "Michael Pittman Jr.")["name"], "Michael Pittman")
        self.assertIsNone(find_player(box, "Mike Pittman Evans"))

    def test_pick_six_counted_once(self):
        from arbbot import grade_prop
        from unittest import mock
        db = {"name": "Trevon Diggs", "dnp": False, "groups": {"interceptions": {"TD": "1"}, "defensive": {"TD": "1"}}}
        wr = {"name": "CeeDee Lamb", "dnp": False, "groups": {"receiving": {"TD": "0", "REC": "5"}}}
        box = {"final": True, "touchdowns": 2, "teams": {"1": {"name": "Cowboys", "score": 14,
                                                              "players": {"trevondiggs": db, "ceedeelamb": wr}}}}
        row = {"sport_key": "americanfootball_nfl", "home_team": "Cowboys", "away_team": "X", "commence_time": "",
               "market": "player_anytime_td", "outcome": "Yes", "point": "", "stake": "10", "price": "3.0"}
        with mock.patch("arbbot.game_box", return_value=(box, "")):
            self.assertEqual(grade_prop(dict(row, player="Trevon Diggs"))[1], "1")      # one TD, not two
            self.assertIsNone(grade_prop(dict(row, player="CeeDee Lamb"))[0])         # 2 TDs, only 1 credited



class OnlyYourBooksCount(unittest.TestCase):
    START, LATER = BetResults.START, BetResults.LATER
    setUp, tearDown, scores = BetResults.setUp, BetResults.tearDown, BetResults.scores

    def log(self, gid, book):
        append_csv(self.cfg.ev_log_file, _arbbot.EV_LOG_FIELDS, {
            "first_seen": "2026-10-03T12:00:00+00:00", "event_id": gid, "sport": "NHL", "sport_key": "icehockey_nhl",
            "matchup": "Away @ Home", "home_team": "Home", "away_team": "Away", "commence_time": self.START,
            "live": False, "market": "h2h", "outcome": "Home", "point": "", "n_outcomes": 2, "book": book,
            "price": 2.2, "fair_odds": 2.0, "best_ev_pct": 10, "stake": 10, "player": "", "confidence": "high"})

    def test_alerts_at_other_books_are_left_out(self):
        from arbbot import day_bets, scoreboard_text
        from datetime import date
        self.cfg = replace(self.cfg, my_books="fanduel,draftkings,betmgm,williamhill_us,kalshi")
        self.log("g1", "Hard Rock Bet")
        self.log("g2", "Caesars")                                    # williamhill_us is Caesars
        self.log("g3", "FanDuel")
        settle_pending(self.cfg, self.scores({"g1": (4, 2), "g2": (4, 2), "g3": (1, 2)}), self.LATER)
        self.assertEqual(sorted(r["book"] for r in day_bets(self.cfg, date(2026, 10, 3))), ["Caesars", "FanDuel"])
        self.assertIn("**All:** 1-1", scoreboard_text(self.cfg, self.LATER))
        self.assertTrue(ev_record(self.cfg).startswith("2 bets, 1-1-0"))
        everyone = replace(self.cfg, my_books="")                    # no MY_BOOKS: everything counts
        self.assertEqual(len(day_bets(everyone, date(2026, 10, 3))), 3)



class ConsensusStake(unittest.TestCase):
    def test_props_without_a_pinnacle_price_bet_30_percent_less(self):
        ev = prop_event({"A": (1.87, 1.95), "B": (1.91, 1.91), "C": (1.95, 1.87), "D": (1.89, 1.93),
                         "E": (2.20, 1.68)})                         # no Pinnacle: median of the other books
        cfg = Config(round_stakes=0, ev_bankroll=1000, confidence_stakes="1,1,1").for_props()
        [b] = find_evs([ev], cfg, NOW)
        self.assertTrue(b.sharp_book.startswith("consensus"))
        full = kelly_stake(b.fair_prob, b.price, cfg)
        self.assertEqual(b.stake, kelly_stake(b.fair_prob, b.price, cfg, 0.7))
        self.assertLess(b.stake, full)
        self.assertIn("no Pinnacle price: stake 30% smaller", b.confidence_notes)
        with_pinnacle = prop_event({"Pinnacle": (1.91, 1.91), "DK": (2.15, 1.70)})
        with_pinnacle["bookmakers"][0]["key"] = "pinnacle"
        [p] = find_evs([with_pinnacle], cfg, NOW)
        self.assertEqual(p.stake, kelly_stake(p.fair_prob, p.price, cfg))   # Pinnacle-priced: full stake
        same = find_evs([ev], replace(cfg, consensus_stake=1.0), NOW)[0]
        self.assertEqual(same.stake, full)


    def test_prop_outliers_without_pinnacle_bet_less_too(self):
        books = {"DK": (1.90, 1.92), "FD": (1.91, 1.91), "MGM": (1.89, 1.93), "BetOnline": (1.92, 1.90),
                 "Stale": (2.60, 1.45)}
        cfg = Config(round_stakes=0)
        [o] = find_outliers([prop_event(books)], cfg, NOW)
        self.assertEqual(o.stake, kelly_stake(o.fair_prob, o.price, cfg, 0.7))
        self.assertLess(o.stake, kelly_stake(o.fair_prob, o.price, cfg))
        self.assertIn("📉 No Pinnacle price: stake 30% smaller.", outlier_payload(o)["embeds"][0]["description"])
        # Pinnacle prices it (and agrees): full stake, no note.
        priced = prop_event({"Pinnacle": (1.91, 1.91), **books})
        priced["bookmakers"][0]["key"] = "pinnacle"
        [p] = find_outliers([priced], cfg, NOW)
        self.assertEqual(p.stake, kelly_stake(p.fair_prob, p.price, cfg))
        self.assertNotIn("📉", outlier_payload(p)["embeds"][0]["description"])
        self.assertEqual(find_outliers([prop_event(books)], replace(cfg, consensus_stake=1.0), NOW)[0].stake,
                         kelly_stake(o.fair_prob, o.price, cfg))


class OddsApiCosts(unittest.TestCase):
    """The free events call is checked with the API's own per-call cost, so another process using
    the same key (the running bot, while you run a check) can't trigger a false warning."""

    def call(self, headers):
        import io, contextlib
        from unittest import mock
        from arbbot import OddsAPI
        class Resp:
            def __init__(self, h):
                self.headers = h
            def read(self):
                return b"[]"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        api = OddsAPI(Config(api_key="k", state_dir=""))
        err = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=[Resp(h) for h in headers]), \
                contextlib.redirect_stderr(err):
            api.events("icehockey_nhl")
            api.events("icehockey_nhl")
        return err.getvalue()

    def test_other_processes_spending_isnt_blamed_on_events(self):
        self.assertEqual(self.call([{"x-requests-used": "100", "x-requests-last": "0"},
                                    {"x-requests-used": "112", "x-requests-last": "0"}]), "")
        self.assertIn("events endpoint used credits",
                      self.call([{"x-requests-used": "100", "x-requests-last": "0"},
                                 {"x-requests-used": "101", "x-requests-last": "1"}]))
        # No per-call header: fall back to the running total.
        self.assertIn("events endpoint used credits",
                      self.call([{"x-requests-used": "100"}, {"x-requests-used": "101"}]))


def kalshi_market(event, team_code, label, bid, ask, dollars=True, **extra):
    m = {"ticker": f"{event}-{team_code}", "event_ticker": event, "yes_sub_title": label, "status": "active",
         "market_type": "binary", "yes_bid_size_fp": "5000.00", "yes_ask_size_fp": "5000.00"}
    if dollars:
        m.update(yes_bid_dollars=f"{bid:.4f}", yes_ask_dollars=f"{ask:.4f}")
    else:
        m.update(yes_bid=round(bid * 100), yes_ask=round(ask * 100))
    m.update(extra)
    return m


class KalshiCrossCheck(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        self.markets = {"KXNFLGAME": [kalshi_market("KXNFLGAME-26OCT04BUFNYJ", "BUF", "Buffalo", 0.61, 0.63),
                                      kalshi_market("KXNFLGAME-26OCT04BUFNYJ", "NYJ", "New York J", 0.37, 0.39,
                                                    dollars=False)]}
        self.calls = []
        def fake(url, timeout=None):
            self.calls.append(url)
            series = url.split("series_ticker=")[1].split("&")[0]
            return {"markets": self.markets.get(series, []), "cursor": ""}
        mock.patch("arbbot._get_json", side_effect=fake).start()
        mock.patch.object(_arbbot, "KALSHI_MIN_GAP", 0).start()
        self.sleeps = []
        mock.patch("arbbot.time.sleep", side_effect=self.sleeps.append).start()
        mock.patch.dict(_arbbot._KALSHI_STATE, {"host": 0, "last": 0.0, "pause_until": 0.0, "fails": 0}).start()
        mock.patch.dict(_arbbot._KALSHI_WARNED, clear=True).start()
        self.addCleanup(mock.patch.stopall)
        _arbbot._ESPN_CACHE.clear()

    def game(self, start="2026-10-04T17:00:00Z", home="New York Jets", away="Buffalo Bills", gid="nfl1",
             sport="americanfootball_nfl"):
        return {"id": gid, "sport_key": sport, "commence_time": start, "home_team": home, "away_team": away}

    def fair(self, games, cfg=None, now=NOW, report=None):
        from arbbot import kalshi_fair
        _arbbot._ESPN_CACHE.clear()
        return kalshi_fair(games, cfg or Config(), now, report)

    def test_reading_and_matching(self):
        from arbbot import _kalshi_price, _kalshi_quote, _kalshi_day, _kalshi_label_match
        self.assertEqual(_kalshi_price({"yes_bid_dollars": "0.5600"}, "yes_bid"), 0.56)
        self.assertEqual(_kalshi_price({"yes_bid": 56}, "yes_bid"), 0.56)
        # Only the NO side quoted: the YES side is its mirror image.
        no_side = {"no_bid_dollars": "0.3700", "no_ask_dollars": "0.3800"}
        self.assertEqual((_kalshi_price(no_side, "yes_bid"), _kalshi_price(no_side, "yes_ask")), (0.62, 0.63))
        # A $0 bid or a $1 ask is Kalshi's "nobody there", not a price.
        self.assertIsNone(_kalshi_quote({"yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0400"}))
        self.assertIsNone(_kalshi_quote({"yes_bid_dollars": "0.9700", "yes_ask_dollars": "1.0000"}))
        self.assertEqual(str(_kalshi_day("KXNFLGAME-26SEP20CLETB")), "2026-09-20")
        for label, team, ok in (("Tampa Bay", "Tampa Bay Buccaneers", True), ("New York G", "New York Giants", True),
                                ("New York G", "New York Jets", False), ("Los Angeles R", "Los Angeles Rams", True),
                                ("Los Angeles R", "Los Angeles Chargers", False), ("LA Rams", "Los Angeles Rams", True),
                                ("Miami (OH)", "Miami (OH) RedHawks", True)):
            self.assertEqual(_kalshi_label_match(team, label), ok, (label, team))
        fair = self.fair([self.game()])["nfl1"]
        self.assertAlmostEqual(fair["Buffalo Bills"][0], 0.62)
        self.assertAlmostEqual(fair["New York Jets"][0], 0.38)
        self.assertEqual(fair["New York Jets"][1:], (0.37, 0.39))
        self.assertEqual(self.fair([self.game("2026-10-11T17:00:00Z")]), {})   # another week
        self.assertEqual(self.fair([self.game()], Config(kalshi_check=False)), {})
        self.assertEqual(self.calls[0].split("?")[0], "https://api.elections.kalshi.com/trade-api/v2/markets")

    def test_before_the_game_only(self):
        report = {}
        self.assertEqual(self.fair([self.game()], now=datetime(2026, 10, 4, 17, 1, tzinfo=timezone.utc),
                                   report=report), {})
        self.assertIn("started", report["nfl1"][1])
        self.assertEqual(self.calls, [])           # nothing pre-game: Kalshi isn't even asked
        live = self.fair([self.game()], Config(kalshi_live=True), now=datetime(2026, 10, 4, 18, tzinfo=timezone.utc))
        self.assertIn("nfl1", live)
        # One game under way, another still to come: only the one still to come gets a price.
        self.markets["KXNFLGAME"] += [kalshi_market("KXNFLGAME-26OCT04MIANE", "MIA", "Miami", 0.40, 0.41),
                                      kalshi_market("KXNFLGAME-26OCT04MIANE", "NE", "New England", 0.59, 0.60)]
        later = self.game("2026-10-04T20:25:00Z", home="New England Patriots", away="Miami Dolphins", gid="nfl2")
        f = self.fair([self.game(), later], now=datetime(2026, 10, 4, 18, tzinfo=timezone.utc))
        self.assertEqual(list(f), ["nfl2"])

    def test_wide_quotes_and_outages_are_ignored(self):
        self.markets["KXNFLGAME"][0] = kalshi_market("KXNFLGAME-26OCT04BUFNYJ", "BUF", "Buffalo", 0.55, 0.70)
        report = {}
        self.assertEqual(self.fair([self.game()], report=report), {})       # 15¢ wide: not trusted
        self.assertIn("too wide", report["nfl1"][1])
        from unittest import mock
        with mock.patch("arbbot._get_json", side_effect=OSError("down")):
            self.assertEqual(self.fair([self.game()]), {})

    def nfl(self, buf=(0.61, 0.63), nyj=(0.37, 0.39), **extra):
        self.markets["KXNFLGAME"] = [
            kalshi_market("KXNFLGAME-26OCT04BUFNYJ", "BUF", "Buffalo", *buf, **extra),
            kalshi_market("KXNFLGAME-26OCT04BUFNYJ", "NYJ", "New York J", *nyj)]
        return self.fair([self.game()])

    def test_trust_filters(self):
        self.assertIn("nfl1", self.nfl(buf=(0.60, 0.63)))                   # 3¢: fine for pro games
        self.assertEqual(self.nfl(buf=(0.59, 0.63)), {})                    # 4¢: too wide
        self.assertEqual(self.nfl(status="inactive"), {})                   # paused / not trading
        self.assertEqual(self.nfl(market_type="scalar"), {})
        self.assertEqual(self.nfl(mve_collection_ticker="KXMVE-1"), {})     # a parlay market
        self.assertEqual(self.nfl(yes_bid_size_fp="40.00", yes_ask_size_fp="5000.00"), {})   # thin
        self.assertIn("nfl1", self.nfl(yes_bid_size_fp="400.00", yes_ask_size_fp="5000.00"))
        self.assertEqual(self.nfl(buf=(0.66, 0.68)), {})                    # middles add up to 105%
        self.assertEqual(self.nfl(buf=(0.0, 0.03)), {})                     # no bid at all
        # A third market in the event: not the two-team shape the bot understands.
        self.assertIn("nfl1", self.nfl())
        self.markets["KXNFLGAME"].append(kalshi_market("KXNFLGAME-26OCT04BUFNYJ", "TIE", "Tie", 0.01, 0.02))
        self.assertEqual(self.fair([self.game()]), {})

    def test_college_allows_a_bit_wider(self):
        from arbbot import kalshi_gap_limit
        self.markets["KXNCAAFGAME"] = [kalshi_market("KXNCAAFGAME-26OCT04PURUCLA", "PUR", "Purdue", 0.40, 0.45),
                                       kalshi_market("KXNCAAFGAME-26OCT04PURUCLA", "UCLA", "UCLA", 0.55, 0.60)]
        g = self.game(home="UCLA Bruins", away="Purdue Boilermakers", gid="cf1", sport="americanfootball_ncaaf")
        self.assertAlmostEqual(self.fair([g])["cf1"]["UCLA Bruins"][0], 0.575)   # 5¢ is fine for college
        cfg = Config()
        self.assertEqual(kalshi_gap_limit(cfg, "americanfootball_nfl", 5), 3)
        self.assertEqual(kalshi_gap_limit(cfg, "americanfootball_nfl", 30), 4)
        self.assertEqual(kalshi_gap_limit(cfg, "americanfootball_ncaaf", 5), 5)

    def test_names_kalshi_spells_differently(self):
        self.markets["KXMLBGAME"] = [kalshi_market("KXMLBGAME-26OCT041910CWSATH", "CWS", "Chicago WS", 0.44, 0.45),
                                     kalshi_market("KXMLBGAME-26OCT041910CWSATH", "ATH", "A's", 0.55, 0.56)]
        g = self.game("2026-10-04T23:10:00Z", home="Athletics", away="Chicago White Sox", gid="m1",
                      sport="baseball_mlb")
        f = self.fair([g])["m1"]
        self.assertAlmostEqual(f["Chicago White Sox"][0], 0.445)
        self.markets["KXNCAAFGAME"] = [
            kalshi_market("KXNCAAFGAME-26OCT04UMASSMIA", "UMASS", "UMass", 0.05, 0.06),
            kalshi_market("KXNCAAFGAME-26OCT04UMASSMIA", "MIA", "Miami (FL)", 0.94, 0.95)]
        g = self.game(home="Miami Hurricanes", away="Massachusetts Minutemen", gid="c1", sport="americanfootball_ncaaf")
        self.assertAlmostEqual(self.fair([g])["c1"]["Miami Hurricanes"][0], 0.945)

    def test_team_code_when_the_label_is_unreadable(self):
        self.markets["KXNFLGAME"] = [kalshi_market("KXNFLGAME-26OCT04NESEA", "SEA", "", 0.62, 0.63),
                                     kalshi_market("KXNFLGAME-26OCT04NESEA", "NE", "", 0.37, 0.38)]
        g = self.game(home="Seattle Seahawks", away="New England Patriots")
        self.assertAlmostEqual(self.fair([g])["nfl1"]["Seattle Seahawks"][0], 0.625)

    def test_similar_school_names(self):
        # "Texas" also starts "Texas A&M": the way round where both markets fit wins.
        self.markets["KXNCAAFGAME"] = [kalshi_market("KXNCAAFGAME-26OCT04TEXTXAM", "TEX", "Texas", 0.60, 0.61),
                                       kalshi_market("KXNCAAFGAME-26OCT04TEXTXAM", "TXAM", "Texas A&M", 0.39, 0.40)]
        g = self.game(home="Texas A&M Aggies", away="Texas Longhorns", gid="c1", sport="americanfootball_ncaaf")
        f = self.fair([g])["c1"]
        self.assertAlmostEqual(f["Texas Longhorns"][0], 0.605)
        self.assertAlmostEqual(f["Texas A&M Aggies"][0], 0.395)

    def test_never_borrows_another_games_price(self):
        from unittest import mock
        tk = self.game(home="Kansas Jayhawks", away="Texas Longhorns", gid="tk", sport="americanfootball_ncaaf")
        tt = self.game(home="Kansas State Wildcats", away="Texas Tech Red Raiders", gid="tt",
                       sport="americanfootball_ncaaf")
        texas_kansas = [kalshi_market("KXNCAAFGAME-26OCT04TEXKU", "TEX", "Texas", 0.70, 0.71),
                        kalshi_market("KXNCAAFGAME-26OCT04TEXKU", "KU", "Kansas", 0.29, 0.30)]
        self.markets["KXNCAAFGAME"] = list(texas_kansas)
        # "Texas" isn't "Texas Tech" and "Kansas" isn't "Kansas State": only Texas-Kansas fits.
        report = {}
        f = self.fair([tk, tt], report=report)
        self.assertEqual(list(f), ["tk"])
        self.assertIn("no Kalshi game found", report["tt"][1])
        # Names that do fit two games (here: without the State/Tech rule) give neither game a price.
        with mock.patch.object(_arbbot, "COLLEGE_QUALIFIERS", set()):
            report = {}
            self.assertEqual(self.fair([tk, tt], report=report), {})
            self.assertIn("another game", report["tt"][1])
            # With Texas Tech-Kansas State listed too, each game finds its own (the closer name wins).
            self.markets["KXNCAAFGAME"] = texas_kansas + [
                kalshi_market("KXNCAAFGAME-26OCT04TTUKSU", "TTU", "Texas Tech", 0.45, 0.46),
                kalshi_market("KXNCAAFGAME-26OCT04TTUKSU", "KSU", "Kansas St.", 0.54, 0.55)]
            f = self.fair([tk, tt])
            self.assertAlmostEqual(f["tk"]["Texas Longhorns"][0], 0.705)
            self.assertAlmostEqual(f["tt"]["Texas Tech Red Raiders"][0], 0.455)

    def test_a_started_game_keeps_its_kalshi_game(self):
        from unittest import mock
        self.markets["KXNCAAMBGAME"] = [kalshi_market("KXNCAAMBGAME-27JAN16TEXARK", "TEX", "Texas", 0.30, 0.31),
                                        kalshi_market("KXNCAAMBGAME-27JAN16TEXARK", "ARK", "Arkansas", 0.69, 0.70)]
        big = self.game("2027-01-16T17:00:00Z", home="Arkansas Razorbacks", away="Texas Longhorns", gid="big",
                        sport="basketball_ncaab")
        small = self.game("2027-01-16T23:30:00Z", home="Arkansas-Pine Bluff Golden Lions",
                          away="Texas Southern Tigers", gid="small", sport="basketball_ncaab")
        during = datetime(2027, 1, 16, 18, tzinfo=timezone.utc)                 # big game under way
        with mock.patch.object(_arbbot, "COLLEGE_QUALIFIERS", set()):            # names that collide
            report = {}
            self.assertEqual(self.fair([big, small], now=during, report=report), {})
            self.assertIn("another game", report["small"][1])
        # And "Texas"/"Arkansas" don't name Texas Southern / Arkansas-Pine Bluff at all, even when
        # the big game isn't in the feed.
        self.assertEqual(self.fair([small], now=during), {})
        from arbbot import _kalshi_label_match
        self.assertFalse(_kalshi_label_match("Texas Southern Tigers", "Texas", college=True))
        self.assertTrue(_kalshi_label_match("Texas Southern Tigers", "Texas Southern", college=True))
        self.assertTrue(_kalshi_label_match("Arkansas State Red Wolves", "Arkansas St.", college=True))
        self.assertTrue(_kalshi_label_match("Texas Longhorns", "Texas", college=True))
        self.assertTrue(_kalshi_label_match("Lehigh Mountain Hawks", "Lehigh", college=True))   # a nickname
        from arbbot import _kalshi_strength
        self.assertGreater(_kalshi_strength("Sam Houston State Bearkats", {"yes_sub_title": "Sam Houston"}, False), 0)

    def test_last_nights_game_doesnt_block_tonights_rematch(self):
        # Saturday 8 PM Eastern (Sunday 00:00 UTC) is over and its market gone; Sunday's rematch is listed.
        self.markets["KXNHLGAME"] = [kalshi_market("KXNHLGAME-26OCT04TBFLA", "TB", "Tampa Bay", 0.38, 0.39),
                                     kalshi_market("KXNHLGAME-26OCT04TBFLA", "FLA", "Florida", 0.61, 0.62)]
        sat = self.game("2026-10-04T00:00:00Z", home="Florida Panthers", away="Tampa Bay Lightning", gid="sat",
                        sport="icehockey_nhl")
        sun = dict(sat, id="sun", commence_time="2026-10-04T21:00:00Z")
        f = self.fair([sat, sun], now=datetime(2026, 10, 4, 2, 40, tzinfo=timezone.utc))
        self.assertAlmostEqual(f["sun"]["Tampa Bay Lightning"][0], 0.385)

    def test_doubleheaders_use_the_start_time(self):
        self.markets["KXMLBGAME"] = [
            kalshi_market("KXMLBGAME-26OCT041305NYMPHI", "NYM", "New York M", 0.40, 0.41),
            kalshi_market("KXMLBGAME-26OCT041305NYMPHI", "PHI", "Philadelphia", 0.59, 0.60),
            kalshi_market("KXMLBGAME-26OCT041840NYMPHI", "NYM", "New York M", 0.50, 0.51),
            kalshi_market("KXMLBGAME-26OCT041840NYMPHI", "PHI", "Philadelphia", 0.49, 0.50)]
        g1 = self.game("2026-10-04T17:05:00Z", home="Philadelphia Phillies", away="New York Mets", gid="g1",
                       sport="baseball_mlb")
        g2 = dict(g1, id="g2", commence_time="2026-10-04T22:40:00Z")
        f = self.fair([g1, g2])
        self.assertAlmostEqual(f["g1"]["New York Mets"][0], 0.405)
        self.assertAlmostEqual(f["g2"]["New York Mets"][0], 0.505)
        # Moved to a time Kalshi doesn't have: no price rather than the wrong game's.
        self.assertEqual(self.fair([dict(g1, commence_time="2026-10-04T20:00:00Z")]), {})

    def test_monday_night_is_dated_monday(self):
        self.markets["KXNFLGAME"] = [kalshi_market("KXNFLGAME-26OCT05ATLNO", "ATL", "Atlanta", 0.45, 0.46),
                                     kalshi_market("KXNFLGAME-26OCT05ATLNO", "NO", "New Orleans", 0.54, 0.55)]
        g = self.game("2026-10-06T00:15:00Z", home="New Orleans Saints", away="Atlanta Falcons")
        self.assertIn("nfl1", self.fair([g]))

    def test_cant_tell_which_means_no_price(self):
        # Both teams labelled just "Los Angeles": either way round fits, so no price.
        self.markets["KXNFLGAME"] = [kalshi_market("KXNFLGAME-26OCT04LACLAR", "LAX", "Los Angeles", 0.45, 0.46),
                                     kalshi_market("KXNFLGAME-26OCT04LACLAR", "LAY", "Los Angeles", 0.54, 0.55)]
        g = self.game(home="Los Angeles Rams", away="Los Angeles Chargers")
        self.assertEqual(self.fair([g]), {})
        # Two Kalshi games on the same day that fit equally well: no price either.
        self.nfl()
        self.markets["KXNFLGAME"] += [kalshi_market("KXNFLGAME-26OCT04BUFNYJ2", "BUF", "Buffalo", 0.50, 0.51),
                                      kalshi_market("KXNFLGAME-26OCT04BUFNYJ2", "NYJ", "New York J", 0.49, 0.50)]
        report = {}
        self.assertEqual(self.fair([self.game()], report=report), {})
        self.assertIn("both fit", report["nfl1"][1])

    def test_back_to_back_against_the_same_team(self):
        # Saturday 8 PM Eastern is already Sunday in UTC; the same teams meet again on Sunday.
        self.markets["KXNHLGAME"] = [kalshi_market("KXNHLGAME-26OCT03TBFLA", "TB", "Tampa Bay", 0.45, 0.46),
                                     kalshi_market("KXNHLGAME-26OCT03TBFLA", "FLA", "Florida", 0.54, 0.55),
                                     kalshi_market("KXNHLGAME-26OCT04TBFLA", "TB", "Tampa Bay", 0.38, 0.39),
                                     kalshi_market("KXNHLGAME-26OCT04TBFLA", "FLA", "Florida", 0.61, 0.62)]
        sat = self.game("2026-10-04T00:00:00Z", home="Florida Panthers", away="Tampa Bay Lightning", gid="h1",
                        sport="icehockey_nhl")
        self.assertAlmostEqual(self.fair([sat])["h1"]["Tampa Bay Lightning"][0], 0.455)

    def test_slow_down_and_second_address(self):
        from unittest import mock
        from arbbot import kalshi_markets
        good = {"markets": [], "cursor": ""}
        too_many = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        with mock.patch("arbbot._get_json", side_effect=[too_many, too_many, good]) as g:
            self.assertEqual(kalshi_markets("americanfootball_nfl"), [])
            self.assertEqual(g.call_count, 3)
        self.assertEqual(self.sleeps, [1, 2])
        # Kalshi keeps refusing: give up for this scan and leave it alone for a while.
        _arbbot._ESPN_CACHE.clear()
        with mock.patch("arbbot._get_json", side_effect=too_many) as g:
            with self.assertRaises(urllib.error.HTTPError):
                kalshi_markets("americanfootball_nfl")
            self.assertEqual(g.call_count, 4)                 # never hops to the other address for a 429
            _arbbot._ESPN_CACHE.clear()
            with self.assertRaises(RuntimeError):
                kalshi_markets("americanfootball_nfl")
            self.assertEqual(g.call_count, 4)                 # paused: didn't even ask
        # First address down: the second one answers, and is tried first next time.
        _arbbot._KALSHI_STATE.update(pause_until=0.0)
        _arbbot._ESPN_CACHE.clear()
        urls = []
        def flaky(url, timeout=None):
            urls.append(url)
            if "elections" in url:
                raise urllib.error.URLError("refused")
            return good
        with mock.patch("arbbot._get_json", side_effect=flaky):
            kalshi_markets("americanfootball_nfl")
            _arbbot._ESPN_CACHE.clear()
            kalshi_markets("americanfootball_nfl")
        self.assertEqual(["elections" in u for u in urls], [True, False, False])

    def test_an_outage_never_slows_the_scans(self):
        from unittest import mock
        both = [self.game(), self.game(home="Tampa Bay Lightning", away="Florida Panthers", gid="h1",
                                       sport="icehockey_nhl")]
        with mock.patch("arbbot._get_json", side_effect=TimeoutError("timed out")) as g:
            report = {}
            self.assertEqual(self.fair(both, report=report), {})
            self.assertEqual(g.call_count, 2)          # one try per address, then the second sport waits
            self.assertIn("paused", report["h1"][1])
            self.fair(both)                             # the next scan inside the cool-down: no calls
            self.assertEqual(g.call_count, 2)
            first = _arbbot._KALSHI_STATE["pause_until"] - time.time()
            _arbbot._KALSHI_STATE["pause_until"] = 0
            self.fair(both)                             # cool-down over: tries again, waits longer now
            self.assertEqual(g.call_count, 4)
            self.assertGreater(_arbbot._KALSHI_STATE["pause_until"] - time.time(), first + 60)
        self.assertTrue(55 <= first <= 60)
        _arbbot._KALSHI_STATE["pause_until"] = 0
        self.assertIn("nfl1", self.fair([self.game()]))   # back: works and resets the cool-down
        self.assertEqual(_arbbot._KALSHI_STATE["fails"], 0)

    def test_short_timeout_and_unknown_series(self):
        from unittest import mock
        from arbbot import kalshi_markets
        seen = []
        def fake(url, timeout=None):
            seen.append((url.split("series_ticker=")[1].split("&")[0], timeout))
            if "KXNCAAMBGAME" in url:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return {"markets": [kalshi_market("KXNCAABGAME-27JAN16DUKEUNC", "DUKE", "Duke", 0.6, 0.61)],
                    "cursor": ""}
        with mock.patch("arbbot._get_json", side_effect=fake):
            self.assertEqual(len(kalshi_markets("basketball_ncaab")), 1)   # fell through to the old name
        self.assertEqual([x[0] for x in seen], ["KXNCAAMBGAME", "KXNCAAMBGAME", "KXNCAABGAME"])
        self.assertTrue(all(t == _arbbot.KALSHI_TIMEOUT for _, t in seen))
        self.assertEqual(_arbbot._KALSHI_STATE["pause_until"], 0)          # a missing page isn't an outage

    def test_stops_asking_when_a_scan_has_spent_enough(self):
        from unittest import mock
        both = [self.game(), self.game(home="Tampa Bay Lightning", away="Florida Panthers", gid="h1",
                                       sport="icehockey_nhl")]
        clock = iter([0.0, 0.0, 100.0])
        with mock.patch("arbbot.time.monotonic", side_effect=lambda: next(clock)):
            report = {}
            self.assertIn("nfl1", self.fair(both, report=report))   # NFL comes first...
        self.assertIn("slow", report["h1"][1])                       # ...then the time was up

    def test_bets_at_kalshi_itself(self):
        cfg = Config(min_ev_pct=3, round_stakes=0)
        # Kalshi sells Home at 45¢: 2.1398 after its fee, +7% against Pinnacle's 50%.
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"Kalshi": [("Home", 2.1398, None)]})
        same = {"e1": {"Home": (0.445, 0.44, 0.45)}}
        [b] = find_evs([ev], cfg, NOW, kalshi=same)                 # its own quote isn't a veto...
        self.assertEqual(b.book, "Kalshi")
        self.assertNotIn("Kalshi agrees", b.confidence_notes)       # ...nor a second opinion
        moved = {"e1": {"Home": (0.465, 0.46, 0.47)}}               # 47¢ now: the price is gone
        self.assertEqual(find_evs([ev], cfg, NOW, kalshi=moved), [])
        # Outliers at Kalshi the same way.
        out = outlier_event(1.85, 1.95)
        out["bookmakers"][-1].update(key="kalshi", title="Kalshi")
        [o] = find_outliers([out], Config(), NOW, kalshi={"e1": {"Home": (0.515, 0.51, 0.52)}})
        self.assertEqual(o.book, "Kalshi")
        self.assertEqual(find_outliers([out], Config(), NOW, kalshi={"e1": {"Home": (0.555, 0.55, 0.56)}}), [])

    def test_a_stale_kalshi_price_doesnt_hide_other_books(self):
        cfg = Config(min_ev_pct=3, round_stakes=0)
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)],
                      {"Kalshi": [("Home", 2.1398, None)], "DK": [("Home", 2.10, None)], "FD": [("Home", 2.08, None)]})
        [b] = find_evs([ev], cfg, NOW, kalshi={"e1": {"Home": (0.485, 0.48, 0.49)}})   # Kalshi now 49¢
        self.assertEqual((b.book, b.price), ("DK", 2.10))
        self.assertEqual([bk for bk, _ in b.also], ["FD"])

    def test_blocked_counts_as_an_outage_a_missing_page_doesnt(self):
        from unittest import mock
        def err(code):
            return urllib.error.HTTPError("u", code, "x", {}, None)
        for side_effect, paused in (([err(403), err(403)], True), ([err(401), err(401)], True),
                                    ([TimeoutError(), err(404)], True), ([err(404), TimeoutError()], True),
                                    ([err(404), err(404)], False)):
            _arbbot._KALSHI_STATE.update(pause_until=0.0, fails=0)
            _arbbot._ESPN_CACHE.clear()
            with mock.patch("arbbot._get_json", side_effect=side_effect + [err(404)] * 4):
                report = {}
                self.assertEqual(self.fair([self.game()], report=report), {})
            self.assertEqual(_arbbot._KALSHI_STATE["pause_until"] > 0, paused, side_effect)
            if paused:
                self.assertIn("couldn't reach Kalshi", report["nfl1"][1])

    def test_depth_must_be_shown(self):
        report = {}
        self.markets["KXNFLGAME"][0].pop("yes_ask_size_fp")
        self.assertEqual(self.fair([self.game()], report=report), {})
        self.assertIn("how many contracts", report["nfl1"][1])

    def test_only_asks_kalshi_when_it_can_matter(self):
        from arbbot import kalshi_useful
        self.assertTrue(kalshi_useful(Config()))
        self.assertFalse(kalshi_useful(Config(kalshi_check=False)))
        self.assertFalse(kalshi_useful(Config(ev_enabled=False, outliers_enabled=False)))
        self.assertFalse(kalshi_useful(Config(markets="spreads,totals")))
        self.assertFalse(kalshi_useful(Config(live_only=True)))
        self.assertTrue(kalshi_useful(Config(live_only=True, kalshi_live=True)))
        self.assertEqual(self.fair([self.game()], Config(ev_enabled=False, outliers_enabled=False)), {})
        self.assertEqual(self.calls, [])

    def test_odd_replies_dont_crash(self):
        self.markets["KXNFLGAME"] = [{"event_ticker": "KXNFLGAME-26OCT04BUFNYJ", "yes_sub_title": 7},
                                     {"event_ticker": "KXNFLGAME-26OCT04BUFNYJ", "yes_sub_title": None,
                                      "ticker": None}]
        self.assertEqual(self.fair([self.game()]), {})

    def ev(self):
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})   # 10% edge
        return ev

    def test_ev_bets_need_kalshi_to_agree(self):
        cfg = Config(min_ev_pct=3, round_stakes=0)
        agree = {"e1": {"Home": (0.51, 0.50, 0.52)}}
        [b] = find_evs([self.ev()], cfg, NOW, kalshi=agree)
        self.assertIn("Kalshi agrees", b.confidence_notes)
        self.assertIn("**Kalshi** 51.0% to win (buy 52¢ · sell 50¢)", ev_payload(b)["embeds"][0]["description"])
        self.assertEqual(find_evs([self.ev()], cfg, NOW, kalshi={"e1": {"Home": (0.44, 0.43, 0.45)}}), [])  # 6 pts off
        self.assertEqual(len(find_evs([self.ev()], cfg, NOW, kalshi={"e1": {"Home": (0.47, 0.46, 0.48)}})), 1)
        # 3.5 points off: too far for a game today, allowed for one more than a day out.
        off = {"e1": {"Home": (0.535, 0.53, 0.54)}}
        self.assertEqual(find_evs([self.ev()], cfg, NOW, kalshi=off), [])
        far = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]},
                       start="2026-10-05T18:00:00Z")
        self.assertEqual(len(find_evs([far], cfg, NOW, kalshi=off)), 1)
        # Within the gap but Kalshi says the price isn't good: skipped.
        thin = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.06, None)]})
        self.assertEqual(find_evs([thin], cfg, NOW, kalshi={"e1": {"Home": (0.47, 0.46, 0.48)}}), [])
        self.assertEqual(len(find_evs([thin], cfg, NOW)), 1)                     # no Kalshi price: as before

    def test_outliers_need_kalshi_too(self):
        ev = outlier_event(1.85, 1.95)
        self.assertEqual(len(find_outliers([ev], Config(), NOW)), 1)
        self.assertEqual(find_outliers([ev], Config(), NOW, kalshi={"e1": {"Home": (0.52, 0.51, 0.53)}}), [])
        [o] = find_outliers([ev], Config(), NOW, kalshi={"e1": {"Home": (0.77, 0.76, 0.78)}})
        self.assertIn("**Kalshi** 77.0% to win", outlier_payload(o)["embeds"][0]["description"])

    def test_check_kalshi_report(self):
        import io, contextlib
        from arbbot import check_kalshi
        self.markets["KXNFLGAME"] += [kalshi_market("KXNFLGAME-26OCT04XXXYYY", "XXX", "Nowhere", 0.5, 0.51),
                                      kalshi_market("KXNFLGAME-26OCT04XXXYYY", "YYY", "Elsewhere", 0.49, 0.5),
                                      kalshi_market("KXNFLGAME-26OCT03MIANE", "MIA", "Miami", 0.40, 0.41),
                                      kalshi_market("KXNFLGAME-26OCT03MIANE", "NE", "New England", 0.59, 0.60)]
        class Api:
            def events(self, sport, horizon_hours=48):
                return [{"id": "nfl1", "commence_time": "2026-10-04T17:00:00Z", "home_team": "New York Jets",
                         "away_team": "Buffalo Bills"},
                        {"id": "nfl2", "commence_time": "2026-10-04T20:25:00Z", "home_team": "Denver Broncos",
                         "away_team": "Las Vegas Raiders"},
                        {"id": "nfl3", "commence_time": "2026-10-03T11:00:00Z", "home_team": "New England Patriots",
                         "away_team": "Miami Dolphins"}] if sport == "americanfootball_nfl" else []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            check_kalshi(Config(sports=["americanfootball_nfl", "soccer_epl"]), Api(), now=NOW)
        text = out.getvalue()
        self.assertIn("NFL: 6 open Kalshi markets, 1 of 3 upcoming games usable", text)
        self.assertIn("Miami Dolphins @ New England Patriots: game has started (KALSHI_LIVE=false) "
                      "[KXNFLGAME-26OCT03MIANE]", text)
        self.assertIn("✅ Buffalo Bills @ New York Jets: Buffalo Bills 62% · New York Jets 38%", text)
        self.assertIn("[KXNFLGAME-26OCT04BUFNYJ]", text)
        self.assertIn("Las Vegas Raiders @ Denver Broncos: no Kalshi game found", text)
        self.assertIn("Kalshi games not placed (1): KXNFLGAME-26OCT04XXXYYY (Nowhere / Elsewhere)", text)
        self.assertIn("fields: ", text)
        self.assertIn("EPL: Kalshi has no game markets", text)



class AutoUpdate(unittest.TestCase):
    """deploy/: the self-updater's script parses, and its Discord notice finds the right channel."""

    def test_script_parses(self):
        import shutil, subprocess
        if not shutil.which("bash"):
            self.skipTest("no bash")
        for name in ("auto-update.sh", "enable-auto-update.sh"):
            r = subprocess.run(["bash", "-n", str(Path(__file__).parent / "deploy" / name)], capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_notice_goes_to_the_status_channel(self):
        import importlib.util, tempfile
        spec = importlib.util.spec_from_file_location("notify", Path(__file__).parent / "deploy" / "notify.py")
        notify = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(notify)
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1/main\n"
                           "DISCORD_STATUS_WEBHOOK_URL='https://discord.com/api/webhooks/2/status'\n")
            self.assertTrue(notify.webhook(env).endswith("/2/status"))
            env.write_text("DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1/main\nDISCORD_STATUS_WEBHOOK_URL=\n")
            self.assertTrue(notify.webhook(env).endswith("/1/main"))       # no status channel: the main one
            env.write_text("# DISCORD_WEBHOOK_URL=https://x\nDISCORD_WEBHOOK_URL=paste-here\n")
            self.assertEqual(notify.webhook(env), "")                       # placeholders aren't posted to
            self.assertEqual(notify.webhook(Path(d) / "missing"), "")

# --------------------------------------------------------------------------- markouts: did the edge hold?

def at(secs):
    return NOW + timedelta(seconds=secs)


def stamped(ev, secs):
    """The same prices, quoted secs after NOW (live quotes go stale after MAX_AGE_SECONDS)."""
    ts = at(secs).isoformat().replace("+00:00", "Z")
    for bm in ev["bookmakers"]:
        bm["last_update"] = ts
        for m in bm["markets"]:
            m["last_update"] = ts
    return ev


def moved_event(home_prices, start="2026-10-03T11:00:00Z", stale=(1.85, 1.95)):
    """outlier_event with the four in-line books (Pinnacle too) at home_prices=(home, away)."""
    ev = outlier_event(*stale, start=start)
    for bm in ev["bookmakers"]:
        if bm["title"] != "Stale":
            bm["markets"][0]["outcomes"] = [{"name": "Home", "price": home_prices[0]},
                                            {"name": "Away", "price": home_prices[1]}]
    return ev


PROP_BOOKS = {"Pinnacle": (1.91, 1.91), "DK": (1.90, 1.92), "FD": (1.91, 1.91), "MGM": (1.92, 1.90),
              "Stale": (2.60, 1.45)}


def priced_prop(books=PROP_BOOKS, **kw):
    ev = prop_event(books, **kw)
    for bm in ev["bookmakers"]:
        if bm["title"] == "Pinnacle":
            bm["key"] = "pinnacle"
    return ev


class StopLoop(Exception):
    pass


class Markouts(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = Path(self.tmp.name)
        self.cfg = Config(markout_file=str(d / "mk.csv"), state_dir=str(d / "state"),
                          ev_log_file=str(d / "ev.csv"), outlier_log_file=str(d / "out.csv"),
                          closing_file=str(d / "close.csv"), log_file=str(d / "arbs.csv"),
                          ev_results_file=str(d / "res.csv"), parlay_log_file=str(d / "par.csv"),
                          pregame_max_age_seconds=10**9)
        self.state = d / "state" / "markouts_pending.json"

    def tearDown(self):
        self.tmp.cleanup()

    def wired(self, alerter, tr):
        """An alerter whose new alerts this tracker follows, the way make_markouts wires them."""
        kind = "outlier" if isinstance(alerter, OutlierAlerter) else "ev" if isinstance(alerter, EVAlerter) else "arb"
        alerter.on_open = lambda item, first: tr.add(item, first, kind)
        return alerter

    @staticmethod
    def args(**flags):
        return SimpleNamespace(**{"once": False, "demo": False, "dry_run": False, **flags})

    def live_outlier(self, tr):
        a = self.wired(OutlierAlerter(self.cfg, dry_run=True), tr)
        ev = outlier_event(1.85, 1.95)                       # live (started 11:00), Stale far off on Home
        [o] = find_outliers([ev], self.cfg, NOW)
        a.handle([o], now=NOW.timestamp())
        return a, o

    def prop_outlier(self, tr):
        a = self.wired(OutlierAlerter(self.cfg, dry_run=True, noun="prop outliers"), tr)
        [o] = find_outliers([priced_prop()], self.cfg, NOW)
        a.handle([o], now=NOW.timestamp())
        return o

    # --- following every new alert
    def test_each_new_alert_is_followed_once(self):
        tr = _arbbot.MarkoutTracker(self.cfg)
        a, o = self.live_outlier(tr)
        a.handle([o], now=NOW.timestamp() + 60)                     # same alert, next check: not again
        self.assertEqual(len(tr.pending), 1)
        m = tr.pending[0]
        self.assertEqual((m.kind, m.book, m.price, m.live, m.ref), ("outlier", "Stale", 1.85, True, "median"))
        self.assertAlmostEqual(m.skip, o.worst_ok_price())
        self.assertEqual((m.fair0, m.edge), (o.fair_prob, o.ev_pct))   # the card's own fair price and edge

    def test_handed_over_and_capped_alerts_are_not_followed_again(self):
        tr = _arbbot.MarkoutTracker(self.cfg)
        cfg = replace(self.cfg, min_ev_pct=3)
        ev_alr = self.wired(EVAlerter(cfg, dry_run=True), tr)
        out_alr = self.wired(OutlierAlerter(cfg, dry_run=True), tr)
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})
        [b] = find_evs([ev], cfg, NOW)
        ev_alr.handle([b], now=1000)
        _arbbot.hand_over([], [b], ev_alr, out_alr)
        out_alr.handle([b], now=1100)
        self.assertEqual([m.kind for m in tr.pending], ["ev"])       # one bet, followed as it first went out
        capped = self.wired(EVAlerter(cfg, dry_run=True, noun="capped"), tr)
        capped.max_per_hour, capped.posted_at = 1, [time.time()]
        capped.handle([b], now=time.time())
        self.assertEqual(len(tr.pending), 1)                         # never sent: nothing to follow

    def test_arb_is_followed_leg_by_leg(self):
        tr = _arbbot.MarkoutTracker(self.cfg)
        a = self.wired(Alerter(self.cfg, dry_run=True), tr)
        [arb] = find_arbs(demo_events(), Config())
        a.handle([arb], now=1000)
        self.assertEqual([m.book for m in tr.pending], [l.book for l in arb.legs])
        self.assertEqual({(m.kind, m.ref, m.edge) for m in tr.pending}, {("arb", "arb", arb.profit_pct)})
        self.assertEqual(len({m.arb_id for m in tr.pending}), 1)
        self.assertEqual([m.skip for m in tr.pending], [arb.worst_ok_price(i) for i in range(len(arb.legs))])

    def test_spread_arb_legs_name_their_own_point(self):
        ev = event({"A": [("spreads", [("Home", 2.10, -3.5), ("Away", 1.75, 3.5)])],
                    "B": [("spreads", [("Home", 1.75, -3.5), ("Away", 2.10, 3.5)])]})
        [arb] = find_arbs([ev], Config(min_profit_pct=0), NOW)
        tr = _arbbot.MarkoutTracker(self.cfg)
        tr.add(arb, NOW.timestamp(), "arb")
        self.assertEqual([m.pick for m in tr.pending], ["Away +3.5", "Home -3.5"])   # not both "-3.5"
        rows = [_arbbot.markout_row(m) for m in tr.pending]
        self.assertEqual(rows[0]["bet_id"], "e1|spreads|Away|3.5")                  # the bet id +EV logs use
        for r in rows:
            self.assertEqual(_arbbot._row_line({**r, "home_team": "Home"}), arb.line)
        tr.observe([stamped(ev, 200)], at(200))
        self.assertEqual(tr.finalize(at(4000)), 2)
        self.assertEqual([(r["still_ok"], r["markout_pct"], r["fair_source"]) for r in _read(self.cfg.markout_file)],
                         [("1", "", "arb"), ("1", "", "arb")])          # an arb's yardstick: is it still there?

    # --- one yardstick: the card's own fair price
    def test_later_uses_the_same_fair_odds_as_the_card(self):
        tr = _arbbot.MarkoutTracker(self.cfg)
        _, o = self.live_outlier(tr)                                   # outlier: median of the other books
        ev_cfg = replace(self.cfg, min_ev_pct=3)
        pre = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})
        [b] = find_evs([pre], ev_cfg, NOW)                             # +EV: Pinnacle's no-vig price
        tr.add(b, NOW.timestamp(), "ev")
        cons = prop_event({"A": (1.87, 1.95), "B": (1.91, 1.91), "C": (1.95, 1.87), "D": (1.89, 1.93),
                           "E": (2.20, 1.68)})                         # no Pinnacle: the median of 4+ books
        [p] = find_evs([cons], self.cfg.for_props(), NOW)
        tr.add(p, NOW.timestamp(), "ev")
        self.assertEqual([m.ref for m in tr.pending], ["median", "sharp", "consensus"])
        for m, ev, item, live in zip(tr.pending, (outlier_event(1.85, 1.95), pre, cons), (o, b, p), (True, False, False)):
            self.assertAlmostEqual(_arbbot._markout_fair(m, ev, self.cfg, NOW, live, {}), item.fair_prob, places=9)
        self.assertNotAlmostEqual(devig([1.25, 4.10], "power")[0], o.fair_prob, places=3)   # (not Pinnacle's)
        # ...and as many books as the card needed: 3+ others for an outlier, 4+ for a prop consensus.
        thin = outlier_event(1.85, 1.95)
        thin["bookmakers"] = [bm for bm in thin["bookmakers"] if bm["title"] not in ("DK", "FD")]
        cons["bookmakers"] = cons["bookmakers"][:3]
        self.assertIsNone(_arbbot._markout_fair(tr.pending[0], thin, self.cfg, NOW, True, {}))
        self.assertIsNone(_arbbot._markout_fair(tr.pending[2], cons, self.cfg, NOW, False, {}))

    def test_stale_book_fixes_its_price(self):
        tr = _arbbot.MarkoutTracker(self.cfg)
        _, o = self.live_outlier(tr)
        fixed = outlier_event(1.26, 3.90)                    # Stale caught up; the other books held
        for s in (70, 200, 600):
            tr.observe([stamped(fixed, s)], at(s))
        self.assertEqual(tr.finalize(at(601)), 1)                    # every slot in: written right away
        [row] = _read(self.cfg.markout_file)
        self.assertEqual((row["next_secs"], row["3m_secs"], row["10m_secs"]), ("70", "200", "600"))
        self.assertAlmostEqual(float(row["markout_pct"]), o.ev_pct, places=1)   # the whole edge held
        self.assertAlmostEqual(float(row["edge_pct"]), o.ev_pct, places=1)      # = what the card said
        self.assertEqual(row["markout_secs"], "200")                  # ~3 min reading is the headline
        self.assertEqual((row["moved"], row["fair_source"]), ("book", "median"))
        self.assertEqual(row["still_ok"], "0")                        # 1.26 is under the skip price
        self.assertEqual((row["bet_id"], row["outcome"], row["point"]), ("e1|h2h|Home|", "Home", ""))
        self.assertEqual(tr.pending, [])

    def test_fast_book_market_moves_to_it(self):
        tr = _arbbot.MarkoutTracker(self.cfg)
        self.live_outlier(tr)
        caught = moved_event((1.85, 1.95))                   # everyone moved to Stale's price
        tr.observe([stamped(caught, 65)], at(65))
        tr.observe([stamped(caught, 190)], at(190))
        tr.finalize(at(1300))
        [row] = _read(self.cfg.markout_file)
        self.assertEqual(row["moved"], "market")
        self.assertLess(float(row["markout_pct"]), 0)                 # no edge left (its price has vig)
        self.assertEqual(row["still_ok"], "1")                        # still bettable, just not good
        self.assertEqual(row["10m_secs"], "")                         # no check in the 10-minute window

    def test_pulled_price_counts_as_pulled_and_not_still_there(self):
        tr = _arbbot.MarkoutTracker(self.cfg)
        self.live_outlier(tr)
        gone = stamped(outlier_event(1.85, 1.95), 200)
        stale = next(bm for bm in gone["bookmakers"] if bm["title"] == "Stale")
        stale["last_update"] = stale["markets"][0]["last_update"] = FRESH   # 200s old: suspended, not there
        tr.observe([gone], at(200))
        tr.finalize(at(1300))
        [row] = _read(self.cfg.markout_file)
        self.assertEqual((row["moved"], row["still_ok"], row["3m_price"]), ("pulled", "0", ""))
        self.assertNotEqual(row["markout_pct"], "")                   # the price is still measured
        self.assertIn("pulled 100%", _arbbot.markout_report(self.cfg))

    @staticmethod
    def live_totals(line, a=(1.91, 1.91), b=(1.91, 1.91), lagging=False):
        """A live game's totals at five books; at 220.5, A (Over 2.08) and B (Under 2.05) make an arb.
        lagging: book E, which isn't in the arb, still hangs 220.5."""
        def tot(over, under, pt=None):
            return [("totals", [("Over", over, pt or line), ("Under", under, pt or line)])]
        return event({"A": tot(*a), "B": tot(*b), "C": tot(1.91, 1.91), "D": tot(1.90, 1.92),
                      "E": tot(1.91, 1.91, 220.5 if lagging else None)}, start="2026-10-03T11:30:00Z")

    def test_arb_whose_line_every_book_left_is_not_still_there(self):
        for lagging in (False, True):                                # same answer if a book not in it lags
            Path(self.cfg.markout_file).unlink(missing_ok=True)
            tr = _arbbot.MarkoutTracker(self.cfg)
            [arb] = find_arbs([stamped(self.live_totals(220.5, (2.08, 1.80), (1.80, 2.05)), 0)], self.cfg, NOW)
            tr.add(arb, NOW.timestamp(), "arb")
            for s in (60, 200, 600):                                 # a basket: the total is re-hung at 222.5
                tr.observe([stamped(self.live_totals(222.5, lagging=lagging), s)], at(s))
            tr.finalize(at(4000))
            rows = _read(self.cfg.markout_file)
            self.assertEqual([(r["still_secs"], r["still_price"], r["still_ok"], r["moved"]) for r in rows],
                             [("60", "", "0", "pulled")] * 2, lagging)
            [g] = [g for g in _arbbot.markout_breakdown(self.cfg)["Alert type"] if g["name"] == "Arbs"]
            self.assertEqual((g["n"], g["of"], g["still"]), (1, 1, 0), lagging)

    def spread_ev(self, tr):
        """A pre-game +EV bet on Home -3.5 at B 2.20, priced by Pinnacle at -3.5."""
        cfg = replace(self.cfg, min_ev_pct=3)
        [b] = find_evs([self.spreads(-3.5, -3.5, 2.20)], cfg, NOW)
        tr.add(b, NOW.timestamp(), "ev")
        return b

    @staticmethod
    def spreads(pinnacle, b_line, b_price):
        ev = event({"Pinnacle": [("spreads", [("Home", 1.91, pinnacle), ("Away", 1.91, -pinnacle)])],
                    "B": [("spreads", [("Home", b_price, b_line), ("Away", 1.70, -b_line)])]})
        ev["bookmakers"][0]["key"] = "pinnacle"
        return ev

    def test_bet_whose_line_moved_everywhere_counts_as_pulled(self):
        tr = _arbbot.MarkoutTracker(self.cfg)
        self.spread_ev(tr)
        for s in (900, 1800):                                        # Pinnacle and B both moved to -4.5
            tr.observe([self.spreads(-4.5, -4.5, 1.91)], at(s))
        tr.finalize(at(4000))
        [row] = _read(self.cfg.markout_file)
        self.assertEqual((row["still_secs"], row["still_price"], row["still_ok"], row["moved"]),
                         ("900", "", "0", "pulled"))
        self.assertEqual(row["markout_pct"], "")                     # nothing to price it against then

    def test_book_still_offering_the_price_counts_when_the_yardstick_moved(self):
        tr = _arbbot.MarkoutTracker(self.cfg)
        self.spread_ev(tr)
        for s in (900, 1800):                                        # Pinnacle moved to -2.5; B still has -3.5
            tr.observe([self.spreads(-2.5, -3.5, 2.20)], at(s))
        self.assertEqual((tr.pending[0].still, tr.pending[0].readings), ([900, 2.2], {}))
        tr.finalize(at(4000))
        [row] = _read(self.cfg.markout_file)
        self.assertEqual((row["still_price"], row["still_ok"], row["markout_pct"], row["moved"]), ("2.2", "1", "", ""))
        late = _arbbot.MarkoutTracker(self.cfg)
        self.spread_ev(late)
        late.observe([self.spreads(-2.5, -3.5, 2.20)], at(3700))     # first look over an hour later: too late
        self.assertIsNone(late.pending[0].still)

    def test_still_there_means_at_the_skip_line_the_card_printed(self):
        from unittest import mock
        cfg = replace(self.cfg, min_ev_pct=3)
        pre = ev_event([("Home", 1.80, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})
        [b] = find_evs([pre], cfg, NOW)
        self.assertAlmostEqual(b.worst_ok_price(), 1.96456, places=5)
        self.assertEqual(_arbbot.odds(b.worst_ok_price()), "-104")   # the card: skip if worse than -104
        for fmt, price, still in (("american", 1.96, "1"), ("american", 1.95, "0"),   # -104 / -105
                                  ("decimal", 1.96, "1"), ("decimal", 1.95, "0")):    # 1.96 / 1.95
            Path(self.cfg.markout_file).unlink(missing_ok=True)
            tr = _arbbot.MarkoutTracker(self.cfg)
            tr.add(b, NOW.timestamp(), "ev")
            later = ev_event([("Home", 1.80, None), ("Away", 1.91, None)], {"B": [("Home", price, None)]})
            tr.observe([later], at(900))
            with mock.patch.object(_arbbot, "ODDS_FORMAT", fmt):
                tr.finalize(at(4000))
            self.assertEqual(_read(self.cfg.markout_file)[0]["still_ok"], still, (fmt, price))
        # An arb leg's skip line is already a price the card can print, but quotes come as 2 decimals.
        tr = _arbbot.MarkoutTracker(self.cfg)
        [arb] = find_arbs(demo_events(), Config())
        tr.add(arb, NOW.timestamp(), "arb")
        m = tr.pending[0]
        m.skip = _arbbot.shown_at_or_above(1.9615)
        self.assertEqual(_arbbot.odds(m.skip), "-104")
        for price, still in ((m.skip, 1), (1.96, 1), (1.95, 0)):   # right at the skip line counts
            m.still = [60, price]
            self.assertEqual(_arbbot.markout_row(m)["still_ok"], still, price)

    def test_slots_keep_the_real_time_when_checks_slow_down(self):
        tr = _arbbot.MarkoutTracker(self.cfg)
        self.live_outlier(tr)
        same = outlier_event(1.85, 1.95)
        tr.observe([stamped(same, 400)], at(400))                     # budget slowed live checks
        tr.observe([stamped(same, 900)], at(900))
        tr.finalize(at(1201))
        [row] = _read(self.cfg.markout_file)
        self.assertEqual((row["next_secs"], row["3m_secs"], row["10m_secs"]), ("400", "", "900"))
        self.assertEqual(row["markout_secs"], "900")                  # no ~3 min reading: the ~10 min one
        self.assertEqual(row["moved"], "neither")                     # nothing moved

    def test_pre_game_bet_stops_at_kickoff_and_counts_as_not_measured(self):
        tr = _arbbot.MarkoutTracker(self.cfg)
        a = self.wired(OutlierAlerter(self.cfg, dry_run=True), tr)
        ev = outlier_event(1.85, 1.95, start="2026-10-03T12:05:00Z")    # starts in 5 minutes
        [o] = find_outliers([ev], self.cfg, NOW)
        a.handle([o], now=NOW.timestamp())
        tr.observe([stamped(outlier_event(1.26, 3.90, start="2026-10-03T12:05:00Z"), 360)], at(360))   # live now
        self.assertEqual(tr.pending[0].readings, {})                   # CLV takes over at kickoff
        self.assertEqual(tr.finalize(at(360)), 1)                     # written, with no reading
        self.assertEqual(tr.pending, [])
        [row] = _read(self.cfg.markout_file)
        self.assertEqual((row["markout_pct"], row["still_ok"]), ("", ""))
        self.assertEqual(_arbbot.markout_coverage(self.cfg), (0, 1))  # "measured 0 of 1"

    def test_fair_source_never_swaps(self):
        tr = _arbbot.MarkoutTracker(self.cfg)
        cfg = replace(self.cfg, min_ev_pct=3)
        pre = ev_event([("Home", 1.91, None), ("Away", 1.91, None)],
                       {"B": [("Home", 2.20, None)], "C": [("Home", 1.91, None), ("Away", 1.91, None)],
                        "D": [("Home", 1.90, None), ("Away", 1.92, None)], "F": [("Home", 1.92, None), ("Away", 1.90, None)]})
        [b] = find_evs([pre], cfg, NOW)
        tr.add(b, NOW.timestamp(), "ev")                               # priced by Pinnacle
        pre["bookmakers"] = [bm for bm in pre["bookmakers"] if bm["key"] != "pinnacle"]
        tr.observe([pre], at(200))
        self.assertEqual(tr.pending[0].readings, {})                   # never swaps in the other books
        self.assertEqual(tr.pending[0].still, [200, 2.2])              # (B still had the price, though)
        # An outlier was priced against the other books, so it doesn't need Pinnacle later.
        _, o = self.live_outlier(tr)
        no_pin = outlier_event(1.85, 1.95)
        no_pin["bookmakers"] = [bm for bm in no_pin["bookmakers"] if bm["key"] != "pinnacle"]
        tr.observe([stamped(no_pin, 200)], at(200))
        self.assertIn("3m", tr.pending[1].readings)

    def test_prop_alert_ignores_main_line_checks(self):
        tr = _arbbot.MarkoutTracker(self.cfg)
        self.prop_outlier(tr)
        main = ev_event([("Lakers", 1.91, None), ("Celtics", 1.91, None)], {"DK": [("Lakers", 1.95, None)]})
        main["id"] = "p1"
        tr.observe([main], at(200))                                   # same game, no prop lines in it
        self.assertEqual((tr.pending[0].readings, tr.pending[0].still), ({}, None))   # not "pulled"
        tr.observe([priced_prop()], at(200))
        self.assertIn("3m", tr.pending[0].readings)
        self.assertEqual(tr.pending[0].player, "LeBron James")

    # --- restarts
    def test_restart_keeps_alerts_being_followed_props_too(self):
        import json
        tr = _arbbot.MarkoutTracker(self.cfg)
        o = self.prop_outlier(tr)
        tr.finalize(NOW)                                               # saves the alert being followed
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved[0]["line"], ["LeBron James", 25.5])    # JSON has no tuples
        self.state.write_text(json.dumps(saved + [{"bogus": 1}]))     # one damaged entry: skipped
        again = _arbbot.MarkoutTracker(self.cfg)
        [m] = again.pending
        self.assertEqual((m.line, m.fair0), (("LeBron James", 25.5), o.fair_prob))
        again.observe([priced_prop()], at(200))                       # a prop line after a restart
        self.assertIn("3m", m.readings)
        self.assertEqual(again.finalize(at(4000)), 1)                 # nothing measured is lost
        self.assertEqual(_read(self.cfg.markout_file)[0]["markout_secs"], "200")
        self.assertEqual(json.loads(self.state.read_text()), [])

    def test_damaged_saved_alerts_are_skipped_one_by_one(self):
        import json, contextlib, io
        tr = _arbbot.MarkoutTracker(self.cfg)
        self.live_outlier(tr)
        tr.finalize(NOW)
        [good] = json.loads(self.state.read_text())
        damaged = [{**good, "event_id": ["e1"]}, {**good, "price": "1.85"}, {**good, "line": [["x"]]},
                   {**good, "readings": []}, {**good, "still": "yes"}, {**good, "live": None},
                   {k: v for k, v in good.items() if k != "book"}, ["not", "a", "dict"], None]
        self.state.write_text(json.dumps(damaged + [good]))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            again = _arbbot.MarkoutTracker(self.cfg)
            self.assertEqual(again.update([stamped(outlier_event(1.85, 1.95), 200)], [], at(200)), 0)   # no crash
        self.assertEqual(err.getvalue().count("skipped a saved alert"), len(damaged))
        [m] = again.pending
        self.assertEqual((m.event_id, sorted(m.readings)), ("e1", ["3m", "next"]))

    # --- wiring: what run() calls
    def test_make_markouts_follows_each_alerter_as_its_kind(self):
        a_arb, a_ev, a_out = Alerter(self.cfg, True), EVAlerter(self.cfg, True), OutlierAlerter(self.cfg, True)
        p_arb = Alerter(self.cfg, True, noun="prop arbs")
        p_ev, p_out = EVAlerter(self.cfg, True, noun="+EV props"), OutlierAlerter(self.cfg, True, noun="prop outliers")
        parlays = ParlayAlerter(self.cfg, True)
        tr = _arbbot.make_markouts(self.cfg, self.args(), [a_arb, a_ev, a_out, p_arb, p_ev, p_out, parlays])
        [arb] = find_arbs(demo_events(), Config())
        cfg = replace(self.cfg, min_ev_pct=3)
        [b] = find_evs([ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})], cfg, NOW)
        [o] = find_outliers([outlier_event(1.85, 1.95)], self.cfg, NOW)
        [po] = find_outliers([priced_prop()], self.cfg, NOW)
        for a, item in ((a_arb, arb), (a_ev, b), (a_out, o), (p_arb, arb), (p_ev, b), (p_out, po)):
            a.handle([item], now=NOW.timestamp())
        self.assertEqual([m.kind for m in tr.pending],
                         ["arb", "arb", "ev", "outlier", "arb", "arb", "ev", "outlier"])
        self.assertIsNone(parlays.on_open)                            # parlay legs are followed one by one

    def test_once_demo_and_dry_run_record_nothing(self):
        ev = outlier_event(1.85, 1.95)
        [o] = find_outliers([ev], self.cfg, NOW)
        service = _arbbot.MarkoutTracker(self.cfg)                     # the running service's saved state
        service.add(o, NOW.timestamp(), "outlier")
        service.finalize(NOW)
        saved = self.state.read_text()
        for flag in ("once", "demo", "dry_run"):
            a = OutlierAlerter(self.cfg, dry_run=True)
            tr = _arbbot.make_markouts(self.cfg, self.args(**{flag: True}), [a])
            self.assertEqual(tr.pending, [], flag)                     # doesn't pick up the service's alerts
            tr.update([stamped(ev, 4000)], [], at(4000))
            self.assertEqual(self.state.read_text(), saved, flag)      # ...or rewrite its file
        self.state.unlink()
        for flag in ("once", "demo", "dry_run"):
            a = OutlierAlerter(self.cfg, dry_run=True)
            tr = _arbbot.make_markouts(self.cfg, self.args(**{flag: True}), [a])
            a.handle([o], now=NOW.timestamp())
            self.assertEqual(tr.pending, [], flag)
            self.assertEqual(tr.update([stamped(ev, 200)], [], at(4000)), 0, flag)
            self.assertFalse(self.state.exists(), flag)
            self.assertFalse(Path(self.cfg.markout_file).exists(), flag)
        a = OutlierAlerter(self.cfg, dry_run=True)
        tr = _arbbot.make_markouts(self.cfg, self.args(), [a])         # the service: followed and saved
        a.handle([o], now=NOW.timestamp())
        tr.update([], [], NOW)
        self.assertEqual(len(tr.pending), 1)
        self.assertTrue(self.state.exists())

    def test_update_reads_main_lines_then_props_then_writes(self):
        out_alr, prop_outs = OutlierAlerter(self.cfg, True), OutlierAlerter(self.cfg, True, noun="prop outliers")
        tr = _arbbot.make_markouts(self.cfg, self.args(), [out_alr, prop_outs])
        main, props = outlier_event(1.85, 1.95), priced_prop()
        out_alr.handle(find_outliers([main], self.cfg, NOW), now=NOW.timestamp())     # pass 1: the alerts
        prop_outs.handle(find_outliers([props], self.cfg, NOW), now=NOW.timestamp())
        self.assertEqual(tr.update([main], [props], NOW), 0)
        self.assertEqual([(m.readings, m.still) for m in tr.pending], [({}, None)] * 2)   # its own check isn't "later"
        tr.update([stamped(outlier_event(1.85, 1.95), 200)], [priced_prop()], at(200))   # pass 2
        self.assertEqual([sorted(m.readings) for m in tr.pending], [["3m", "next"], ["3m", "next"]])
        self.assertEqual(tr.update([], [], at(1300)), 2)               # pass 3: both done, both written
        self.assertEqual(len(_read(self.cfg.markout_file)), 2)

    def run_bot(self, cfg_changes=None, **flags):
        """run() for one pass of the main loop against a fake Odds API: a live game with an outlier,
        a game starting in an hour with a +EV bet and an arb, and that game's props."""
        import argparse, contextlib, io
        from unittest import mock
        real = datetime.now(timezone.utc)
        iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
        live_start, pre_start = iso(real - timedelta(minutes=30)), iso(real + timedelta(hours=1))

        def fresh(ev, gid):
            ev["id"], ev["sport_key"] = gid, "basketball_nba"
            ts = iso(datetime.now(timezone.utc))
            for bm in ev["bookmakers"]:
                bm["last_update"] = ts
                for m in bm["markets"]:
                    m["last_update"] = ts
            return ev

        class Api:
            remaining, used = 50000.0, None
            def __init__(self, cfg):
                pass
            def events(self, sport, horizon_hours=26):
                return [{"id": "live1", "commence_time": live_start}, {"id": "p1", "commence_time": pre_start}]
            def odds(self, sport, until):
                pre = event({"Pinnacle": [("h2h", [("Home", 1.91, None), ("Away", 1.91, None)])],
                             "B": [("h2h", [("Home", 2.20, None)])],
                             "X": [("totals", [("Over", 2.10, 220.5), ("Under", 1.75, 220.5)])],
                             "Y": [("totals", [("Over", 1.75, 220.5), ("Under", 2.10, 220.5)])]}, start=pre_start)
                pre["bookmakers"][0]["key"] = "pinnacle"
                return [fresh(outlier_event(1.85, 1.95, start=live_start), "live1"), fresh(pre, "p1")]
            def event_odds(self, sport, gid, markets):
                return fresh(priced_prop(start=pre_start), gid)

        cfg = replace(self.cfg, sports=["basketball_nba"], prop_sports=["basketball_nba"], kalshi_check=False,
                      summary_hour=-1, results_minutes=0, pregame_max_age_seconds=900, **(cfg_changes or {}))
        args = argparse.Namespace(**{"once": False, "demo": False, "dry_run": False, "plan": False,
                                     "check_kalshi": False, "results": None, "post_results": None, **flags})
        seen = []
        real_update = _arbbot.MarkoutTracker.update

        def spy(tracker, events, prop_events, now):
            seen.append((tracker, sorted(e["id"] for e in events), sorted(e["id"] for e in prop_events)))
            return real_update(tracker, events, prop_events, now)

        with mock.patch("arbbot.OddsAPI", Api), mock.patch.object(_arbbot.MarkoutTracker, "update", spy), \
                mock.patch("arbbot.time.sleep", side_effect=StopLoop), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(StopLoop):
                _arbbot.run(cfg, args, _arbbot.Status(cfg, dry_run=True))
        return seen

    def test_run_follows_every_new_alert_and_reads_each_pass(self):
        [(tracker, main_ids, prop_ids)] = self.run_bot()              # one pass, one update after the alerts
        self.assertEqual((main_ids, prop_ids), (["live1", "p1"], ["p1"]))
        self.assertEqual({(m.kind, m.event_id, m.market) for m in tracker.pending},
                         {("outlier", "live1", "h2h"), ("ev", "p1", "h2h"), ("arb", "p1", "totals"),
                          ("outlier", "p1", "player_points"), ("arb", "p1", "player_points")})
        self.assertTrue(self.state.exists())

    def test_run_dry_run_follows_nothing(self):
        [(tracker, main_ids, prop_ids)] = self.run_bot({"props_enabled": False}, dry_run=True)
        self.assertEqual((main_ids, prop_ids), (["live1", "p1"], []))   # (a pass with no prop check)
        self.assertEqual(tracker.pending, [])
        self.assertFalse(self.state.exists())

    def test_results_command_prints_markouts_and_survives_a_broken_file(self):
        import argparse, contextlib, io
        from unittest import mock
        self.rows(3, 2.0)
        args = argparse.Namespace(once=False, demo=False, dry_run=False, plan=False, check_kalshi=False,
                                  results="today", post_results=None)
        for broken in (False, True):
            if broken:
                Path(self.cfg.markout_file).write_bytes(b"first_seen,kind\n\xff\xfe\x00,outlier\n")
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("arbbot.OddsAPI", lambda cfg: SimpleNamespace(remaining=None)), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                _arbbot.run(self.cfg, args, _arbbot.Status(self.cfg, dry_run=True))
            self.assertIn("All time", out.getvalue())
            if broken:
                self.assertNotIn("Markouts (each", out.getvalue())
                self.assertIn("! Markouts (--results)", err.getvalue())
            else:
                self.assertIn("Markouts (each alert's price checked again minutes later)", out.getvalue())
                self.assertIn("Live outliers", out.getvalue())

    # --- never in the way of the alerts
    def test_bad_data_never_stops_the_alerts(self):
        from unittest import mock
        import contextlib, io
        tr = _arbbot.MarkoutTracker(self.cfg)
        a, o = self.live_outlier(tr)
        other = outlier_event(1.85, 1.95)
        other["id"] = "e2"
        tr.add(find_outliers([other], self.cfg, NOW)[0], NOW.timestamp(), "outlier")
        tr.add(o, NOW.timestamp(), "outlier")
        tr.pending[2].line = ["unhashable"]                           # one damaged alert on game e1
        broken = outlier_event(1.85, 1.95)
        del broken["home_team"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            tr.observe([broken, stamped(other, 70)], at(70))          # a bad game doesn't stop the next one
            tr.observe([stamped(outlier_event(1.26, 3.90), 200)], at(200))   # nor a bad alert the next one
        self.assertIn("! Markouts", err.getvalue())
        self.assertEqual([sorted(m.readings) for m in tr.pending], [["3m", "next"], ["next"], []])
        # Following an alert can't break sending it...
        b = OutlierAlerter(self.cfg, dry_run=True, noun="x")
        tr2 = _arbbot.make_markouts(self.cfg, self.args(), [b])
        with mock.patch.object(_arbbot.MarkoutTracker, "_add", side_effect=KeyError("boom")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(b.handle([o], now=NOW.timestamp()), 1)
        self.assertEqual((b.stats["found"], len(b.open), tr2.pending), (1, 1, []))
        # ...and a row that can't be saved is dropped, not retried (and failed) every pass.
        with mock.patch("arbbot.append_csv", side_effect=OSError("disk full")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tr.finalize(at(4000)), 0)
        self.assertEqual(tr.pending, [])

    # --- reporting
    def rows(self, n, pct, kind="outlier", live=True, book="FanDuel", still=1, edge=8.0, sport="NBA", **extra):
        for i in range(n):
            append_csv(self.cfg.markout_file, _arbbot.MARKOUT_FIELDS, {
                "first_seen": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds"),
                "bet_id": f"{kind}{live}{book}{pct}{i}", "kind": kind, "live": live, "sport": sport, "book": book,
                "markout_pct": pct + (i % 2) * 0.2, "markout_secs": 190, "3m_pct": pct + (i % 2) * 0.2,
                "3m_secs": 190, "moved": "book", "still_ok": still, "edge_pct": edge, **extra})

    def arb(self, arb_id, legs, edge=2.0, key=None, **extra):
        """One arb's legs: [(book, still there on the next check)]. Legs with the same key are the same bets."""
        for i, (book, still) in enumerate(legs):
            append_csv(self.cfg.markout_file, _arbbot.MARKOUT_FIELDS, {
                "first_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": "arb", "live": True,
                "book": book, "arb_id": arb_id, "bet_id": f"{key or arb_id}|leg{i}", "still_ok": still,
                "edge_pct": edge, **extra})

    def test_stats_and_verdicts(self):
        n, mean, se, lo, hi = _arbbot.markout_stats([1.0, 3.0])
        self.assertEqual((n, mean), (2, 2.0))
        self.assertAlmostEqual(se, 1.0)
        self.assertAlmostEqual(lo, 2 - 1.96)
        self.assertEqual(_arbbot.markout_verdict(49, 0.5, 2.0), "")         # too few to say
        self.assertEqual(_arbbot.markout_verdict(50, 0.5, 2.0), "✅ real edge")
        self.assertEqual(_arbbot.markout_verdict(50, -2.0, -0.1), "⚠️ review")
        self.assertEqual(_arbbot.markout_verdict(50, -1.0, 1.0), "")

    def test_groups(self):
        g = _arbbot.markout_group
        self.assertEqual(g({"kind": "outlier", "live": "True"}), "Live outliers")
        self.assertEqual(g({"kind": "outlier", "live": "False"}), "Pre-game outliers")
        self.assertEqual(g({"kind": "ev", "live": "False"}), "Pre-game +EV")
        self.assertEqual(g({"kind": "outlier", "live": "False", "player": "LeBron James"}), "Props")
        self.assertEqual(g({"kind": "arb", "live": "True", "player": "LeBron James"}), "Arbs")

    def test_scoreboard_summary_and_command_line(self):
        self.rows(60, 4.0, edge=12.0)                                 # live outliers, clearly above 0
        self.rows(12, -2.0, book="DraftKings", edge=12.0)
        self.rows(60, 3.0, kind="ev", live=False, edge=6.0)           # clearly above 0 too, but pre-game
        self.rows(1, 0, kind="ev", live=False, markout_pct="", still_ok="")   # a pre-game alert never re-read
        self.rows(5, 9.0, book="BetOnline.ag")                        # not your book: left out
        self.arb("a1", [("FanDuel", 1), ("DraftKings", 0)])
        cfg = replace(self.cfg, my_books="fanduel,draftkings")
        board = _arbbot.scoreboard_text(cfg, datetime.now(timezone.utc))
        self.assertIn("📏 Did the edge hold?", board)
        self.assertRegex(board, r"Live outliers\s+72\s+\+12%\s+\+3\.1%.*✅")
        self.assertRegex(board, r"Pre-game \+EV\s+60\s+\+6%\s+\+3\.1%\s+\S+\s+100%\n")   # no verdict for pre-game
        self.assertRegex(board, r"Arbs\s+1\s+\+2%\s+—\s+—\s+0%")
        self.assertIn("Pre-game and props get no ✅/⚠️: their checks can be hours apart and only alerts "
                      "re-checked within an hour are measured, so CLV is the better test there (measured 60 of 61).",
                      board)
        card = _arbbot.summary_payload(cfg, [], 5000)["embeds"][0]["description"]
        self.assertIn("Live outliers: sent +12.0% → later +3.1% (72 bets, 95% range", card)
        self.assertIn("Pre-game +EV: sent +6.0% → later +3.1% (60 of 61 bets measured, 95% range", card)
        self.assertEqual(card.count("✅ real edge"), 1)
        self.assertIn("Arbs: every leg still there on the next check 0% (1 arb)", card)
        self.assertIn("Best book: FanDuel", card)
        self.assertIn("Worst: DraftKings (-1.9% later, 12 bets)", card)
        self.assertNotIn("Worst:", _arbbot.summary_payload(replace(cfg, my_books="fanduel"), [], 5000)
                         ["embeds"][0]["description"])
        report = _arbbot.markout_report(cfg)
        self.assertIn("FanDuel", report)
        self.assertNotIn("BetOnline", report)
        self.assertIn("book fixed it 100%", report)
        self.assertIn("by time:", report)
        self.assertIn("its 📏 part shows whether alert prices held up", _arbbot.guide_payload()["embeds"][0]["description"])

    def test_bets_not_cards(self):
        tr = _arbbot.MarkoutTracker(self.cfg)
        a, o = self.live_outlier(tr)
        tr.observe([stamped(outlier_event(1.85, 1.95), 200)], at(200))
        a.handle([], now=at(250).timestamp())                         # GONE...
        a.handle([o], now=at(300).timestamp())                        # ...and back: a second card, one bet
        tr.observe([stamped(moved_event((1.85, 1.95)), 460)], at(460))   # the second card's ~3 min look
        self.assertEqual(tr.finalize(at(5000)), 2)
        self.assertEqual([r["markout_secs"] for r in _read(self.cfg.markout_file)], ["200", "160"])
        [r] = _arbbot.markout_rows(self.cfg)                          # counted once: the first card
        self.assertEqual(r["markout_secs"], "200")
        rows = _read(self.cfg.markout_file)
        Path(self.cfg.markout_file).unlink()
        for row in rows[::-1]:                                        # a later card can finish first
            append_csv(self.cfg.markout_file, _arbbot.MARKOUT_FIELDS, row)
        self.assertEqual(_arbbot.markout_rows(self.cfg)[0]["markout_secs"], "200")
        self.arb("e1|h2h|200", [("FanDuel", 1), ("DraftKings", 1)], key="e1|h2h")
        self.arb("e1|h2h|900", [("FanDuel", 0), ("DraftKings", 0)], key="e1|h2h")   # the same arb again
        [arbs] = [g for g in _arbbot.markout_breakdown(self.cfg)["Alert type"] if g["name"] == "Arbs"]
        self.assertEqual((arbs["n"], arbs["still"]), (1, 100))

    def test_arb_leg_on_an_alerted_bet_still_counts(self):
        # An outlier on Home ML, then an arb built on that same Home price: one bet_id, both counted.
        self.rows(1, 2.0, still=0, bet_id="g1|h2h|Home|")
        for outcome, still in (("Home", 0), ("Away", 1)):
            append_csv(self.cfg.markout_file, _arbbot.MARKOUT_FIELDS, {
                "first_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": "arb",
                "live": True, "book": "FanDuel", "arb_id": "a1", "bet_id": f"g1|h2h|{outcome}|",
                "still_ok": still, "edge_pct": 2.0})
        groups = {g["name"]: g for g in _arbbot.markout_breakdown(self.cfg)["Alert type"]}
        self.assertEqual((groups["Live outliers"]["n"], groups["Arbs"]["n"], groups["Arbs"]["still"]), (1, 1, 0))

    def test_losing_live_group_is_flagged_on_the_scoreboard(self):
        self.rows(60, -3.0)
        table = _arbbot.markout_table(self.cfg)
        self.assertRegex(table, r"Live outliers\s+60\s+\+8%\s+-2\.9%.* ⚠️\n")
        self.assertIn("✅ = 50+ live bets and above 0 even at the low end · ⚠️ = 50+ live bets and below 0 even "
                      "at the high end: review", table)

    def test_report_numbers_use_the_right_bets(self):
        for pct, secs, moved, still in ((1.0, 120, "pulled", 0), (1.2, 900, "book", 1),
                                        (1.4, 1000, "book", 1), (1.6, 950, "pulled", 1)):
            self.rows(1, pct, kind="ev", live=False, edge=3.0, markout_secs=secs, moved=moved, still=still)
        blank = {"markout_pct": "", "3m_pct": "", "markout_secs": ""}
        for i, (moved, still) in enumerate((("pulled", 0), ("pulled", 0), ("", 1), ("", ""), ("", ""))):
            # looked at again with nothing to price it against (its line was gone, or Pinnacle's
            # had moved), or never looked at again
            self.rows(1, 0, kind="ev", live=False, edge=15.0, moved=moved, still=still, bet_id=f"x{i}", **blank)
        report = _arbbot.markout_report(self.cfg)
        self.assertRegex(report, r"Pre-game \+EV\s+4 of 9 bets measured: sent \+3\.0%")   # the measured, not +9.7%
        self.assertIn("~15m 50s after the alert", report)          # the median, not the quickest
        self.assertIn("still there 57%", report)                    # 4 of the 7 looked at again
        self.assertIn("pulled 57%", report)                         # 4 of those 7, not of the 4 measured or all 9
        self.assertIn("book fixed it 100%", report)

    def test_ev_markout_ties_back_to_the_bet_log(self):
        tr = _arbbot.MarkoutTracker(self.cfg)
        cfg = replace(self.cfg, min_ev_pct=3)
        a = self.wired(EVAlerter(cfg, dry_run=True), tr)
        pre = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})
        [b] = find_evs([pre], cfg, NOW)
        a.handle([b], now=NOW.timestamp())
        tr.finalize(at(4000))
        [row], [logged] = _read(self.cfg.markout_file), _read(self.cfg.ev_log_file)
        self.assertEqual(row["bet_id"], _arbbot._bet_id(logged))      # the same id CLV and results use
        self.assertEqual(row["commence_time"], logged["commence_time"])
        self.assertEqual((row["confidence"], row["fair_source"]), (b.confidence, "sharp"))
        self.assertAlmostEqual(float(row["edge_pct"]), b.ev_pct, places=2)

    def test_table_columns_line_up(self):
        self.rows(3, 1.5)
        self.rows(3, -10.5, live=False)
        self.rows(3, 2.0, kind="ev", live=False)
        self.rows(3, 2.0, kind="ev", live=True)
        self.rows(3, 2.0, player="LeBron James", live=False)
        self.arb("a1", [("FanDuel", 1), ("DraftKings", 1)])
        table = _arbbot.markout_table(self.cfg).split("```")[1].strip("\n").split("\n")
        self.assertEqual([l.split("  ")[0] for l in table[1:]], _arbbot.MARKOUT_GROUP_ORDER)
        self.assertEqual(len({len(l.removesuffix(" ✅").removesuffix(" ⚠️")) for l in table}), 1, "\n".join(table))

    def test_scoreboard_keeps_clv_and_drops_markouts_when_too_long(self):
        from unittest import mock
        append_csv(self.cfg.ev_log_file, _arbbot.EV_LOG_FIELDS, {
            "first_seen": "2026-10-03T12:00:00+00:00", "event_id": "g1", "sport": "NBA", "sport_key": "basketball_nba",
            "matchup": "Away @ Home", "home_team": "Home", "away_team": "Away", "commence_time": "2026-10-03T18:00:00Z",
            "live": False, "market": "h2h", "outcome": "Home", "point": "", "n_outcomes": 2, "book": "FanDuel",
            "price": 2.2, "fair_odds": 2.0, "best_ev_pct": 10, "stake": 10, "player": "", "confidence": "high"})
        append_csv(self.cfg.closing_file, _arbbot.CLOSING_FIELDS, {"bet_id": "g1|h2h|Home|", "closing_fair_prob": 0.5})
        self.rows(3, 2.0)
        now = datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)
        self.assertIn("📏", _arbbot.scoreboard_text(self.cfg, now))
        with mock.patch("arbbot.day_summary", return_value="x" * 3000):
            text = _arbbot.scoreboard_text(self.cfg, now)
            card = _arbbot.scoreboard_payload(self.cfg, now)["embeds"][0]["description"]
        self.assertIn("📐 Bet quality (CLV)", text)
        self.assertNotIn("📏", text)
        self.assertLessEqual(len(text), 4000)
        self.assertIn("bets   CLV  beat", card)                         # the CLV table wasn't cut

    def test_best_and_worst_book_need_ten_bets_each(self):
        self.rows(10, 4.0)
        self.rows(9, -2.0, book="DraftKings")
        self.assertEqual(_arbbot.markout_books_line(self.cfg), "")
        self.rows(1, -2.0, book="DraftKings", bet_id="extra")
        self.assertIn("Worst: DraftKings", _arbbot.markout_books_line(self.cfg))

    def test_book_and_sport_tables_are_single_bets_only(self):
        self.rows(3, 2.0)
        self.arb("a1", [("BetMGM", 1), ("FanDuel", 1)], sport="NBA", markout_pct=1.0)
        mk = _arbbot.markout_breakdown(self.cfg)
        self.assertEqual([g["name"] for g in mk["Book"]], ["FanDuel"])
        self.assertEqual([(g["name"], g["of"]) for g in mk["Sport"]], [("NBA", 3)])

    def test_bad_markouts_file_never_breaks_the_cards(self):
        import contextlib, io
        self.rows(3, 2.0)
        self.rows(1, 0, markout_pct="abc", edge_pct="?", still_ok="x", markout_secs="zz")   # garbage cells
        text = _arbbot.scoreboard_text(self.cfg, NOW)
        self.assertRegex(text, r"Live outliers\s+3\s")                # the damaged row just isn't measured
        Path(self.cfg.markout_file).write_bytes(b"first_seen,kind\n\xff\xfe\x00,outlier\n")   # a damaged file
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            text = _arbbot.scoreboard_text(self.cfg, NOW)
            card = _arbbot.summary_payload(self.cfg, [], 5000)["embeds"][0]["description"]
        self.assertIn("All time", text)
        self.assertNotIn("📏", text + card)
        self.assertIn("💳 **Credits**", card)
        self.assertIn("! Markouts (scoreboard)", err.getvalue())
        self.assertIn("! Markouts (daily summary)", err.getvalue())

    def test_markouts_off_everywhere(self):
        off = replace(self.cfg, markout_file="")
        self.rows(3, 2.0)
        self.assertEqual(_arbbot.markout_rows(off), [])               # never opens the bot's own folder
        self.assertNotIn("📏", _arbbot.scoreboard_text(off, NOW))
        self.assertNotIn("📏", _arbbot.summary_payload(off, [], 5000)["embeds"][0]["description"])
        self.assertEqual(_arbbot.markout_report(off), "")
        tr = _arbbot.MarkoutTracker(off)
        tr.add(find_outliers([outlier_event(1.85, 1.95)], off, NOW)[0], NOW.timestamp(), "outlier")
        self.assertEqual((tr.pending, tr.finalize(at(4000))), ([], 0))
        self.assertFalse(self.state.exists())

    def test_from_env(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"MARKOUT_FILE": ""}, clear=False):
            self.assertEqual(Config.from_env().markout_file, "")
        with mock.patch.dict(os.environ, {"MARKOUT_FILE": "m.csv"}, clear=False):
            self.assertEqual(Config.from_env().markout_file, "m.csv")


# --------------------------------------------------------------------------- alert mix: fewer live alerts,
# more pre-game ones, and live alerts that are still there when you tap them

STARTED = "2026-10-03T11:00:00Z"     # an hour before NOW: a live game


def mix_event(home, away, sharp=(1.91, 1.91), ev_id="e1", start="2026-10-03T18:00:00Z", books=("A", "B"),
              sport="basketball_nba", home_team="Home", away_team="Away"):
    """A moneyline where book 1 has `home` on Home and book 2 `away` on Away (an arb when they add up
    to under 100%), and Pinnacle (key 'pinnacle') the fair line, unless sharp is None."""
    b1, b2 = books
    mk = {b1: [("h2h", [(home_team, home, None), (away_team, 1.50, None)])],
          b2: [("h2h", [(home_team, 1.50, None), (away_team, away, None)])]}
    if sharp:
        mk = {"Pinnacle": [("h2h", [(home_team, sharp[0], None), (away_team, sharp[1], None)])], **mk}
    ev = event(mk, home=home_team, away=away_team, start=start)
    ev.update(id=ev_id, sport_key=sport)
    for bm in ev["bookmakers"]:
        bm["key"] = {"Pinnacle": "pinnacle", "Kalshi": "kalshi", "FanDuel": "fanduel",
                     "DraftKings": "draftkings"}.get(bm["title"], bm["key"])
    return ev


def asked(events, markets=None, since=None):
    """What a fake /odds call answers for these arguments: only the bet types asked for, and with
    `since` only games starting after it (LIVE_MARKETS: the live and the pre-game checks)."""
    import copy
    keep = set(markets.split(",")) if markets else None
    out = []
    for ev in events:
        if since is not None and _parse(ev["commence_time"]) <= since:
            continue
        ev = copy.deepcopy(ev)
        for bm in ev["bookmakers"] if keep is not None else []:
            bm["markets"] = [m for m in bm["markets"] if m["key"] in keep]
        out.append(ev)
    return out


def age_book(ev, title, secs, when=NOW):
    """That book's prices were last updated secs before `when`."""
    bm = next(b for b in ev["bookmakers"] if b["title"] == title)
    bm["last_update"] = bm["markets"][0]["last_update"] = \
        (when - timedelta(seconds=secs)).isoformat().replace("+00:00", "Z")
    return ev


MIX = Config(min_profit_pct=0, min_live_profit_pct=0, round_stakes=0)


class MixFiles(unittest.TestCase):
    """Every file a test's alerters could write goes in a temporary folder."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = Path(self.tmp.name)
        self.files = dict(log_file=str(d / "arbs.csv"), ev_log_file=str(d / "ev.csv"),
                          outlier_log_file=str(d / "out.csv"), closing_file=str(d / "close.csv"),
                          markout_file=str(d / "mk.csv"), ev_results_file=str(d / "res.csv"),
                          parlay_log_file=str(d / "par.csv"), state_dir=str(d / "state"),
                          score_check_file=str(d / "sc.csv"))

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, base=None, **kw):
        return replace(base or Config(), **self.files, **kw)

    @staticmethod
    def args(**flags):
        return SimpleNamespace(**{"once": False, "demo": False, "dry_run": True, **flags})


class FewerLiveAlerts(MixFiles):
    def test_locks_mode_values_and_settings(self):
        import os
        from unittest import mock
        c = Config().with_mode()
        self.assertEqual((c.live_per_hour, c.max_arb_per_hour, c.max_outlier_per_hour), (3, 4, 6))
        self.assertEqual((c.min_profit_pct, c.min_live_profit_pct), (2.0, 5.0))
        self.assertEqual((c.outlier_min_pct, c.outlier_live_min_pct), (15.0, 20.0))
        mine = Config(live_per_hour=2, max_arb_per_hour=1, max_outlier_per_hour=9, outlier_live_min_pct=30).with_mode()
        self.assertEqual((mine.live_per_hour, mine.max_arb_per_hour, mine.max_outlier_per_hour,
                          mine.outlier_live_min_pct), (2, 1, 6, 30))            # yours win only when stricter
        plain = Config(alert_mode="balanced").with_mode()
        self.assertEqual((plain.live_per_hour, plain.max_arb_per_hour, plain.outlier_live_min_pct), (0, 0, 0))
        env = {"ALERT_MODE": "balanced", "LIVE_PER_HOUR": "5", "MAX_ARB_PER_HOUR": "7", "MAX_OUTLIER_PER_HOUR": "8",
               "OUTLIER_LIVE_MIN_PCT": "25", "CONFIDENT_HOURS": "36"}
        with mock.patch.dict(os.environ, env):
            e = Config.from_env()
        self.assertEqual((e.live_per_hour, e.max_arb_per_hour, e.max_outlier_per_hour, e.outlier_live_min_pct,
                          e.confident_hours), (5, 7, 8, 25, 36))
        line = _arbbot.mode_line(c)
        for words in ("arbs 2%+ (live 5%+)", "outliers 15%+ (live 20%+)", "4 arbs/hour", "6 outliers/hour",
                      "3 live alerts/hour in all"):
            self.assertIn(words, line)

    def test_live_arbs_need_five_percent_in_locks_mode(self):
        ev_pre, ev_live = mix_event(2.15, 2.02, ev_id="pre"), mix_event(2.15, 2.02, ev_id="live", start=STARTED)
        locks = Config(round_stakes=0).with_mode()
        [pre] = find_arbs([ev_pre], locks, NOW)                                  # ~4.2%: fine before the game
        self.assertTrue(2 <= pre.profit_pct < 5)
        self.assertEqual(find_arbs([ev_live], locks, NOW), [])                    # live needs 5%+ now
        self.assertEqual(len(find_arbs([ev_live], replace(locks, min_live_profit_pct=3), NOW)), 1)

    def test_live_outliers_need_a_bigger_edge(self):
        live, pre = outlier_event(1.50, 3.0), outlier_event(1.50, 3.0, start="2026-10-03T18:00:00Z")   # ~17%
        locks = Config().with_mode()
        self.assertEqual(find_outliers([live], locks, NOW), [])                   # live: 20%+
        self.assertEqual(len(find_outliers([pre], locks, NOW)), 1)                # before the game: 15%+
        own = Config(outlier_min_pct=10, outlier_live_min_pct=17)
        self.assertEqual(len(find_outliers([live], own, NOW)), 1)
        self.assertEqual(find_outliers([live], replace(own, outlier_live_min_pct=18), NOW), [])
        self.assertEqual(len(find_outliers([live], replace(own, outlier_live_min_pct=0), NOW)), 1)   # 0: OUTLIER_MIN_PCT
        self.assertEqual(find_outliers([live], replace(own, outlier_min_pct=18, outlier_live_min_pct=0), NOW), [])
        self.assertEqual(find_outliers([live], replace(own, outlier_min_pct=18, outlier_live_min_pct=12), NOW), [])

    def test_one_hourly_cap_for_every_live_alert(self):
        cfg = self.cfg(MIX)
        cap = _arbbot.HourlyCap(1)
        arbs, outs = Alerter(cfg, dry_run=True), OutlierAlerter(cfg, dry_run=True)
        arbs.live_cap = outs.live_cap = cap
        [live_arb] = find_arbs([mix_event(2.25, 1.98, ev_id="g1", start=STARTED)], MIX, NOW)
        [pre_arb] = find_arbs([mix_event(2.25, 1.98, ev_id="g2")], MIX, NOW)
        [live_out] = find_outliers([outlier_event(1.85, 1.95)], cfg, NOW)
        self.assertEqual(arbs.handle([live_arb], now=1000), 1)
        self.assertEqual(outs.handle([live_out], now=1010), 0)                  # the hour's one live alert went
        self.assertEqual(outs.held_counts, {"live cap": 1})
        self.assertEqual(outs.open, {})
        self.assertEqual(arbs.handle([live_arb, pre_arb], now=1020), 1)         # pre-game never counts
        self.assertEqual(len(cap.times), 1)
        self.assertEqual(outs.handle([live_out], now=1000 + 3601), 1)           # a new hour

    def test_live_alerts_the_live_cap_holds_dont_use_up_the_arb_cap(self):
        a = Alerter(self.cfg(MIX), dry_run=True)
        a.max_per_hour, a.live_cap = 1, _arbbot.HourlyCap(1)
        a.live_cap.times.append(1000.0)                                          # the hour's live alert went out
        [live] = find_arbs([mix_event(2.25, 1.98, ev_id="live", start=STARTED)], MIX, NOW)
        [pre] = find_arbs([mix_event(2.25, 1.98, ev_id="pre")], MIX, NOW)
        for now in (1060, 1120):
            self.assertEqual(a.handle([live], now=now), 0)
        self.assertEqual((a.held_counts, a.posted_at), ({"live cap": 2}, []))    # no arb slot taken
        self.assertEqual(a.handle([live, pre], now=1180), 1)                     # so a pre-game arb still goes out
        self.assertEqual(list(a.open), [pre.key])

    def test_caps_send_games_that_havent_started_first(self):
        cfg = self.cfg(MIX)
        a = Alerter(cfg, dry_run=True)
        a.max_per_hour = 1
        [big_live] = find_arbs([mix_event(2.60, 1.98, ev_id="live", start=STARTED)], MIX, NOW)
        [small_pre] = find_arbs([mix_event(2.08, 1.98, ev_id="pre")], MIX, NOW)
        self.assertGreater(big_live.profit_pct, small_pre.profit_pct)
        self.assertEqual(a.handle([big_live, small_pre], now=1000), 1)
        self.assertEqual(list(a.open), [small_pre.key])                          # you have time to place it
        self.assertEqual(a.held_counts, {"capped": 1})
        e = EVAlerter(cfg, dry_run=True)
        e.max_per_hour = 2
        bets = [replace(ev_leg(f"g{i}", {"DK": 2.20 + i / 20}), event_id=f"g{i}", is_live=i > 0) for i in range(4)]
        e.handle(bets, now=1000)
        self.assertEqual(sorted(b.event_id for b in (op.arb for op in e.open.values())), ["g0", "g3"])  # then biggest

    def test_main_and_prop_alerts_share_their_caps(self):
        t = _arbbot.Trackers(self.cfg(max_arb_per_hour=1, max_outlier_per_hour=1, live_per_hour=2), self.args())
        self.assertEqual((t.arbs.max_per_hour, t.prop_arbs.max_per_hour, t.outs.max_per_hour,
                          t.prop_outs.max_per_hour), (1, 1, 1, 1))
        self.assertTrue(all(a.live_cap is t.live_cap for a in t.singles()))
        self.assertEqual(t.live_cap.limit, 2)
        self.assertIsNone(t.parlays.live_cap)
        [one] = find_arbs([mix_event(2.25, 1.98, ev_id="g1")], MIX, NOW)
        [two] = find_arbs([mix_event(2.25, 1.98, ev_id="g2")], MIX, NOW)
        self.assertEqual(t.arbs.handle([one], now=1000), 1)
        self.assertEqual(t.prop_arbs.handle([two], now=1100), 0)                 # the hour's one arb already went
        self.assertEqual(t.prop_arbs.handle([two], now=1000 + 3700), 1)
        [o] = find_outliers([outlier_event(1.85, 1.95, start="2026-10-03T18:00:00Z")], t.cfg, NOW)
        po = replace(o, event_id="p1")
        self.assertEqual(t.outs.handle([o], now=1000), 1)
        self.assertEqual(t.prop_outs.handle([po], now=1100), 0)

    def test_related_bets_leave_out_ones_the_live_cap_holds(self):
        from arbbot import note_related
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})
        [ml] = find_evs([ev], EVCFG, NOW)
        live_tot = replace(ml, market="totals", line=220.5, point=220.5, outcome="Over", is_live=True)
        pre_sp = replace(ml, market="spreads", line=-3.5, point=-3.5)
        a = EVAlerter(self.cfg(EVCFG), dry_run=True)
        a.live_cap = _arbbot.HourlyCap(1)
        a.live_cap.times.append(time.time())                                     # the live slot is taken
        note_related([([ml, live_tot, pre_sp], a, set())])
        self.assertEqual(ml.related, ["Home -3.5"])                               # not the live total
        a.live_cap.times.clear()
        live_under = replace(live_tot, outcome="Under", price=2.10)
        note_related([([ml, live_under, live_tot, pre_sp], a, set())])
        self.assertEqual(ml.related, ["Home -3.5", "Over 220.5"])                 # one live slot: the better one

    def test_tonight_and_tomorrow_can_be_high_confidence(self):
        from arbbot import rate_confidence
        self.assertEqual(rate_confidence(2.5, 1.0, 20, 5, False), ("high", ["tight sharp market (2.5% margin)",
                                                                           "other books agree"]))
        self.assertEqual(rate_confidence(2.5, 4.0, 20, 5, False)[0], "high")       # 20h out: the hours point
        self.assertEqual(rate_confidence(2.5, 4.0, 20, 5, False, confident_hours=12)[0], "medium")
        conf, notes = rate_confidence(2.5, 4.0, 30, 5, False)
        self.assertEqual(conf, "medium")
        self.assertIn("early line (less tested)", notes)                          # past CONFIDENT_HOURS
        conf, notes = rate_confidence(2.5, 4.0, 20, 5, True)                      # props: still 12 hours
        self.assertEqual(conf, "medium")
        self.assertIn("early line (less tested)", notes)
        # find_evs passes the hours to kickoff and CONFIDENT_HOURS through.
        tomorrow = ev_event([("Home", 1.93, None), ("Away", 1.93, None)], {"B": [("Home", 2.15, None)],
                            "C": [("Home", 1.93, None), ("Away", 1.93, None)], "D": [("Home", 1.94, None), ("Away", 1.92, None)],
                            "E": [("Home", 1.92, None), ("Away", 1.94, None)]}, start="2026-10-04T08:00:00Z")   # 20h out
        cfg = Config(min_ev_pct=3, round_stakes=0, min_confidence="high")
        [b] = find_evs([tomorrow], cfg, NOW)
        self.assertEqual(b.confidence, "high")
        self.assertEqual(find_evs([tomorrow], replace(cfg, confident_hours=12), NOW), [])


class LiveAlertsHoldUp(MixFiles):
    """"u keep sending notis that by the time u click on it the odds already switch within seconds"."""

    def live_arb(self, ev_id="live", **kw):
        [arb] = find_arbs([mix_event(2.25, 1.98, ev_id=ev_id, start=STARTED, **kw)], MIX, NOW)
        return arb

    def test_locks_mode_values_and_settings(self):
        import os
        from unittest import mock
        c = Config().with_mode()
        self.assertEqual((c.live_confirm_checks, c.live_max_age_alert), (2, 60))
        self.assertEqual((Config().live_confirm_checks, Config().live_max_age_alert), (1, 0))   # off outside locks
        mine = Config(live_confirm_checks=3, live_max_age_alert=30).with_mode()
        self.assertEqual((mine.live_confirm_checks, mine.live_max_age_alert), (3, 30))         # stricter wins
        loose = Config(live_confirm_checks=1, live_max_age_alert=0).with_mode()
        self.assertEqual((loose.live_confirm_checks, loose.live_max_age_alert), (2, 60))
        with mock.patch.dict(os.environ, {"ALERT_MODE": "balanced", "LIVE_CONFIRM_CHECKS": "3",
                                          "LIVE_MAX_AGE_ALERT": "45"}):
            e = Config.from_env()
        self.assertEqual((e.live_confirm_checks, e.live_max_age_alert), (3, 45))
        self.assertIn("live alerts need 2 checks in a row and a price under 60s old", _arbbot.mode_line(c))

    def test_a_live_alert_needs_two_checks_in_a_row(self):
        rules = _arbbot.LiveConfirm(checks=2)
        live, scope = self.live_arb(), {"basketball_nba"}
        [pre] = find_arbs([mix_event(2.25, 1.98, ev_id="pre")], MIX, NOW)
        self.assertEqual(rules.filter([live, pre], scope, 1000), [pre])           # pre-game: right away
        self.assertEqual(rules.held, [(live, "waiting")])
        self.assertEqual(rules.filter([live, pre], scope, 1060), [live, pre])     # found again: it pings
        self.assertEqual(rules.filter([live], scope, 1120), [live])               # (capped? it can still go)
        # Seen, missing on the next check of its sport, back: counting starts over.
        rules = _arbbot.LiveConfirm(checks=2)
        rules.filter([live], scope, 1000)
        self.assertEqual(rules.filter([], scope, 1060), [])
        self.assertEqual(rules.dropped, [(live, 1000, 1)])                        # gone before its 2nd check
        self.assertEqual(rules.filter([live], scope, 1120), [])
        self.assertEqual(rules.filter([live], scope, 1180), [live])
        # A check of other sports isn't "missing"; a check too long after the last one is.
        rules = _arbbot.LiveConfirm(checks=2, window=180)
        rules.filter([live], scope, 1000)
        self.assertEqual(rules.filter([], {"icehockey_nhl"}, 1060), [])
        self.assertEqual(rules.dropped, [])
        self.assertEqual(rules.filter([live], scope, 1170), [live])
        rules = _arbbot.LiveConfirm(checks=2, window=180)
        rules.filter([live], scope, 1000)
        self.assertEqual(rules.filter([live], scope, 1181), [])                    # 181s later: not "in a row"
        # 1 = off; 3 = three in a row.
        self.assertEqual(_arbbot.LiveConfirm(checks=1).filter([live], scope, 1000), [live])
        three = _arbbot.LiveConfirm(checks=3)
        self.assertEqual([len(three.filter([live], scope, t)) for t in (1000, 1060, 1120)], [0, 0, 1])

    def test_open_restored_and_handed_over_alerts_dont_wait(self):
        live, scope = self.live_arb(), {"basketball_nba"}
        cfg = self.cfg(MIX)
        for how in ("open", "restored", "handed"):
            a = Alerter(cfg, dry_run=True)
            if how == "open":
                a.handle([live], now=900)
            elif how == "restored":
                a.restored[live.key] = {"first_seen": 900}
            else:
                a.handed[live.key] = 900
            rules = _arbbot.LiveConfirm(checks=2)
            self.assertEqual(rules.filter([live], scope, 1000, (a,)), [live], how)
            self.assertEqual(rules.filter([live], scope, 1000, (Alerter(cfg, dry_run=True),)), [], how)

    def test_the_price_to_bet_must_be_fresh(self):
        scope = {"basketball_nba"}
        ev = age_book(age_book(mix_event(2.25, 1.98, start=STARTED), "A", 10), "B", 70)
        [old] = find_arbs([ev], MIX, NOW)
        self.assertEqual(old.age, 70)                                            # the oldest price to bet
        rules = _arbbot.LiveConfirm(max_age=60)
        self.assertEqual(rules.filter([old], scope, 1000), [])
        self.assertEqual(rules.held, [(old, "old price")])
        [ok] = find_arbs([age_book(mix_event(2.25, 1.98, start=STARTED), "B", 50)], MIX, NOW)
        self.assertEqual(rules.filter([ok], scope, 1000), [ok])
        [pin_old] = find_arbs([age_book(mix_event(2.25, 1.98, start=STARTED), "Pinnacle", 100)], MIX, NOW)
        self.assertEqual(rules.filter([pin_old], scope, 1000), [pin_old])        # Pinnacle's age doesn't matter
        [pre_old] = find_arbs([age_book(mix_event(2.25, 1.98), "B", 600)], MIX, NOW)
        self.assertEqual(rules.filter([pre_old], scope, 1000), [pre_old])        # before the game: no limit
        self.assertEqual(rules.filter([replace(ok, age=None)], scope, 1000), [])  # no time on it: can't tell
        self.assertEqual(_arbbot.LiveConfirm(max_age=0).filter([old], scope, 1000), [old])   # 0 = off
        # An old price on the 2nd check isn't a sighting: start over, and it isn't "gone" either.
        both = _arbbot.LiveConfirm(checks=2, max_age=60)
        both.filter([ok], scope, 1000)
        self.assertEqual(both.filter([replace(ok, age=70)], scope, 1060), [])
        self.assertEqual((both.held[0][1], both.dropped), ("old price", []))
        self.assertEqual(both.filter([ok], scope, 1120), [])
        self.assertEqual(both.filter([ok], scope, 1180), [ok])
        # Outliers and +EV: the alerted book's price.
        out_ev = age_book(outlier_event(1.85, 1.95), "Stale", 40)
        [o] = find_outliers([out_ev], Config(), NOW)
        self.assertEqual(o.age, 40)
        [b] = find_evs([age_book(ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]}),
                                 "B", 25)], EVCFG, NOW)
        self.assertEqual(b.age, 25)

    def test_two_checks_then_the_ping_and_markouts_follow_only_what_went_out(self):
        cfg = self.cfg(MIX, live_confirm_checks=2, live_max_age_alert=60, ev_enabled=False)
        t = _arbbot.Trackers(cfg, self.args(dry_run=False))
        out_ev = outlier_event(1.85, 1.95)
        out_ev["sport_key"] = "basketball_nba"

        def check(secs):
            evs = [mix_event(2.25, 1.98, ev_id="live", start=STARTED), mix_event(2.25, 1.98, ev_id="pre"), out_ev]
            return _arbbot.scan_main(t, [stamped(e, secs) for e in evs], ["basketball_nba"], at(secs), kalshi=False)
        first = check(0)
        self.assertEqual((first.sent, first.out_sent), (1, 0))                   # the pre-game arb, right away
        self.assertEqual(list(t.arbs.open), ["pre|h2h|None"])
        self.assertEqual(first.held, {"waiting": 2})
        self.assertEqual(_arbbot.held_text(first.held), " | held back: 2 live, waiting for another check")
        self.assertEqual({m.event_id for m in t.markouts.pending}, {"pre"})       # only what went out is followed
        second = check(60)
        self.assertEqual((second.sent, second.out_sent, second.held), (1, 1, {}))   # still there: now they ping
        self.assertEqual(sorted({(m.kind, m.event_id) for m in t.markouts.pending}),
                         [("arb", "live"), ("arb", "pre"), ("outlier", "e1")])
        n = len(t.markouts.pending)
        third = check(120)
        self.assertEqual((third.sent, third.out_sent, len(t.markouts.pending)), (0, 0, n))   # followed once

    def test_the_live_cap_and_live_rules_together(self):
        cfg = self.cfg(MIX, live_confirm_checks=2, live_per_hour=1, ev_enabled=False)
        t = _arbbot.Trackers(cfg, self.args())

        def check(secs, ids):
            evs = [mix_event(2.25 + i / 50, 1.98, ev_id=g, start=STARTED) for i, g in enumerate(ids)]
            return _arbbot.scan_main(t, [stamped(e, secs) for e in evs], ["basketball_nba"], at(secs), kalshi=False)
        check(0, ["g1", "g2"])
        res = check(60, ["g1", "g2"])
        self.assertEqual(res.sent, 1)                                             # one live alert an hour
        self.assertEqual(res.held, {"live cap": 1})
        self.assertEqual(list(t.arbs.open), ["g2|h2h|None"])                      # the bigger one
        self.assertIn("1 over the live-alert cap", _arbbot.held_text(res.held))

    def test_cards_show_how_old_the_price_was_when_sent(self):
        ev = age_book(age_book(mix_event(2.25, 1.98), "A", 10), "B", 35)
        [arb] = find_arbs([ev], MIX, NOW)
        self.assertIn("⏱ prices were up to 35s old when sent", discord_payload(arb)["embeds"][0]["description"])
        [b] = find_evs([age_book(ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]}),
                                 "B", 20)], EVCFG, NOW)
        self.assertIn("⏱ price was 20s old when sent", ev_payload(b)["embeds"][0]["description"])
        [o] = find_outliers([age_book(outlier_event(1.85, 1.95), "Stale", 40)], Config(), NOW)
        self.assertIn("⏱ price was 40s old when sent", outlier_payload(o)["embeds"][0]["description"])
        self.assertEqual([_arbbot._age_note(s) for s in (600, 7200)],
                         ["\n⏱ price was 10 min old when sent", "\n⏱ price was 2h old when sent"])
        self.assertNotIn("⏱", discord_payload(replace(arb, age=None))["embeds"][0]["description"])
        # Kept as first sent: a later sighting with a newer price time doesn't edit the card.
        sent = []
        a = Alerter(self.cfg(MIX, webhook_url="https://x"), dry_run=False)
        a._discord = lambda payload, message_id=None, url="": sent.append(message_id) or message_id or "m1"
        a.handle([arb], now=1000)
        [again] = find_arbs([mix_event(2.25, 1.98)], MIX, NOW)                    # same prices, 0s old now
        a.handle([again], now=1060)
        self.assertEqual((sent, a.open[arb.key].arb.age), ([None], 35))
        [jump] = find_arbs([age_book(mix_event(2.60, 1.98), "A", 5)], MIX, NOW)  # a much better price: new alert
        a.handle([jump], now=1120)
        self.assertEqual(a.open[arb.key].arb.age, 5)                             # ...showing its own price's age
        n = len(sent)
        a.handle(find_arbs([mix_event(2.60, 1.98)], MIX, NOW), now=1180)
        self.assertEqual(len(sent), n)                                           # and no needless edit after it
        b2 = EVAlerter(self.cfg(EVCFG), dry_run=True)
        b2.handle([b], now=1000)
        b2.handle([replace(b, age=0)], now=1060)
        self.assertEqual(b2.open[b.key].arb.age, 20)

    def test_restart_keeps_the_price_age_on_the_card(self):
        cfg = self.cfg(MIX, webhook_url="https://x")
        sent = []

        def make(cls):
            a = cls(cfg, dry_run=False)
            a._discord = lambda payload, message_id=None, url="": sent.append(message_id) or message_id or "m1"
            return a
        [arb] = find_arbs([age_book(mix_event(2.25, 1.98), "B", 35)], MIX, NOW)
        make(Alerter).handle([arb], ["basketball_nba"], now=time.time())
        [b] = find_evs([age_book(ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]}),
                                 "B", 20)], EVCFG, NOW)
        make(EVAlerter).handle([b], now=time.time())
        a2, e2 = make(Alerter), make(EVAlerter)                                   # restart
        [arb2] = find_arbs([mix_event(2.25, 1.98)], MIX, NOW)
        a2.handle([arb2], ["basketball_nba"], now=time.time())
        e2.handle([replace(b, age=0)], now=time.time())
        self.assertEqual(sent, [None, None])                                     # no edits, no new posts
        self.assertEqual((a2.open[arb.key].arb.age, e2.open[b.key].arb.age), (35, 20))

    def test_cards_restored_from_an_older_version_show_no_price_age(self):
        import json
        cfg = self.cfg(MIX, webhook_url="https://x")
        [arb] = find_arbs([age_book(mix_event(2.25, 1.98), "B", 9)], MIX, NOW)   # 9s old at the restart check
        ev = age_book(ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]}), "B", 9)
        ev["sport_key"] = "basketball_nba"
        [b] = find_evs([ev], EVCFG, NOW)
        for cls, item in ((Alerter, arb), (EVAlerter, b)):
            path = cls(cfg, dry_run=False)._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            old = cls(cfg, dry_run=True)
            path.write_text(json.dumps({item.key: {                               # saved before cards had an age
                "message_id": "123", "url": "https://x", "first_seen": time.time() - 2400,
                "alerted_pct": old.value(item), "best_pct": old.value(item), "card": "old", "label": "x",
                "sport_key": item.sport_key, "event_id": item.event_id, "commence_time": item.commence_time}}))
            a, edits = cls(cfg, dry_run=False), []
            a._discord = lambda payload, message_id=None, url="": edits.append((message_id, payload)) or message_id
            a.handle([item], ["basketball_nba"], now=time.time())
            [(mid, card)] = edits                                                  # the old card, edited
            self.assertEqual(mid, "123", cls.__name__)
            self.assertNotIn("⏱", card["embeds"][0]["description"], cls.__name__)   # not "9s old when sent"
            self.assertIsNone(a.open[item.key].arb.age, cls.__name__)

    def test_prop_cards_dont_claim_a_price_age(self):
        """A prop market has one time stamp for every player's line: it says when SOME line last
        moved, not this one. So no ⏱ line, and "?" in arbs.csv's leg_ages."""
        def two_players(ev):
            for bm in ev["bookmakers"]:
                bm["markets"][0]["outcomes"] += [
                    {"name": "Over", "description": "Anthony Davis", "price": 1.90, "point": 27.5},
                    {"name": "Under", "description": "Anthony Davis", "price": 1.90, "point": 27.5}]
            return ev
        out_ev = age_book(priced_prop(), "Stale", 10)
        [o] = find_outliers([out_ev], Config(), NOW)
        self.assertEqual(o.age, 10)                                               # one line: that's its price's age
        [o] = find_outliers([two_players(out_ev)], Config(), NOW)
        self.assertEqual((o.pick, o.book, o.age), ("LeBron James Over 25.5 Points", "Stale", None))
        self.assertNotIn("⏱", outlier_payload(o)["embeds"][0]["description"])
        ev_ev = age_book(priced_prop({"Pinnacle": (1.91, 1.91), "DK": (2.15, 1.70)}), "DK", 20)
        [b] = find_evs([ev_ev], Config().for_props(), NOW)
        self.assertEqual(b.age, 20)
        [b] = find_evs([two_players(ev_ev)], Config().for_props(), NOW)
        self.assertIsNone(b.age)
        self.assertNotIn("⏱", ev_payload(b)["embeds"][0]["description"])
        arb_ev = age_book(priced_prop({"Pinnacle": (2.0, 2.0), "DK": (2.20, 1.80), "FD": (1.80, 2.05)}), "DK", 30)
        [arb] = find_arbs([arb_ev], MIX, NOW)
        self.assertEqual((arb.age, arb.ages), (30, "DK 30s; FD 0s; Pinnacle 0s"))
        [arb] = find_arbs([two_players(arb_ev)], MIX, NOW)
        self.assertEqual((arb.age, arb.ages), (None, "DK ?; FD ?; Pinnacle ?"))  # still logged, as unknown
        self.assertNotIn("⏱", discord_payload(arb)["embeds"][0]["description"])

    def test_demo_shows_a_pregame_arb_and_the_live_outlier_in_locks_mode(self):
        import contextlib, io
        from unittest import mock
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("arbbot.kalshi_fair", side_effect=AssertionError("network")):
            _arbbot.run(self.cfg(Config().with_mode()), SimpleNamespace(demo=True, dry_run=True), None)
        text = out.getvalue()
        self.assertIn("[demo] 1 arb(s), 0 +EV bet(s), 1 outlier(s) in sample data (2 checks", text)
        self.assertEqual(text.count("🚨 OUTLIER"), 1)
        self.assertEqual(len(find_arbs(demo_events(live_arb=False), Config().with_mode())), 1)   # --test-discord too
        self.assertEqual(find_arbs(demo_events(), Config().with_mode()), [])     # (live at 3.5%: not in locks)
        self.assertEqual(list(Path(self.d).iterdir()), [])                       # the demo writes nothing

    def test_slow_live_checks_still_confirm(self):
        t = _arbbot.Trackers(self.cfg(live_confirm_checks=2), self.args())
        self.assertEqual((t.arb_live.window, t.bet_live.window), (180, 180))     # max(3 x POLL_SECONDS, 3 min)
        t.set_live_interval(300)                                                 # a tight budget: every 5 min
        self.assertEqual((t.arb_live.window, t.bet_live.window), (900, 900))
        t.set_live_interval(30)
        self.assertEqual(t.arb_live.window, 180)
        self.assertEqual(_arbbot.Trackers(self.cfg(poll_seconds=90), self.args()).bet_live.window, 270)

    def test_a_waiting_live_bet_isnt_named_on_other_cards(self):
        cfg = self.cfg(live_confirm_checks=2, ev_enabled=False)
        t = _arbbot.Trackers(cfg, self.args())

        def check(secs, both):
            ev = outlier_event(1.85, 1.95)
            ev["sport_key"] = "basketball_nba"
            if both:   # a second book far off on the other side
                ev["bookmakers"].append({"key": "stale2", "title": "Stale2", "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Home", "price": 1.10}, {"name": "Away", "price": 6.5}]}]})
            return _arbbot.scan_main(t, [stamped(ev, secs)], ["basketball_nba"], at(secs), kalshi=False)
        check(0, False)
        check(60, False)
        [home] = t.outs.open.values()
        check(120, True)                                                          # Away waits for its 2nd check
        self.assertEqual(home.arb.related, [])
        check(180, True)
        self.assertEqual(home.arb.related, ["Away ML"])
        self.assertEqual(len(t.outs.open), 2)

    def test_test_discord_sends_an_arb_in_locks_mode(self):
        import os, contextlib, io, sys
        from unittest import mock
        sent = []
        env = {"DISCORD_WEBHOOK_URL": "https://discord.example/hook", "ALERT_MODE": "locks", "STATE_DIR": "",
               "LOG_FILE": ""}
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "argv", ["arbbot.py", "--test-discord"]), \
                mock.patch("arbbot.load_dotenv"), mock.patch("arbbot.time.sleep"), \
                mock.patch("arbbot._webhook", lambda url, payload, *a: sent.append(payload) or {"id": "1"}), \
                contextlib.redirect_stdout(io.StringIO()):
            _arbbot.main()
        self.assertTrue(sent[0]["embeds"][0]["title"].startswith("🧪 SAMPLE · 💰 ARB"))

    def test_once_checks_one_time_and_says_what_waits(self):
        import argparse, contextlib, io
        from unittest import mock
        real = datetime.now(timezone.utc)
        iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
        live_start, pre_start = iso(real - timedelta(minutes=30)), iso(real + timedelta(hours=5))

        class Api:
            remaining, used = 50000.0, None

            def __init__(self, cfg):
                pass

            def events(self, sport, horizon_hours=26):
                return [{"id": "live1", "commence_time": live_start}, {"id": "pre1", "commence_time": pre_start}]

            def odds(self, sport, until, markets=None, since=None, kind="odds"):   # (locks: LIVE_MARKETS=h2h)
                secs = (datetime.now(timezone.utc) - NOW).total_seconds()
                live = outlier_event(1.85, 1.95, start=live_start)
                live.update(id="live1", sport_key="basketball_nba")
                pre = mix_event(2.25, 1.98, ev_id="pre1", start=pre_start)
                return asked([stamped(live, secs), stamped(pre, secs)], markets, since)

            def event_odds(self, sport, gid, markets):   # a prop arb in the same game
                prop = priced_prop({"Pinnacle": (2.0, 2.0), "DK": (2.20, 1.80), "FD": (1.80, 2.05)}, start=pre_start)
                prop["id"] = gid
                return stamped(prop, (datetime.now(timezone.utc) - NOW).total_seconds())

        cfg = self.cfg(Config(round_stakes=0, max_arb_per_hour=1).with_mode(), sports=["basketball_nba"],
                       prop_sports=["basketball_nba"], kalshi_check=False, ev_enabled=False)
        args = argparse.Namespace(once=True, demo=False, dry_run=True, plan=False, check_kalshi=False,
                                  results=None, post_results=None)
        with mock.patch("arbbot.OddsAPI", Api), contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            _arbbot.run(cfg, args, _arbbot.Status(cfg, dry_run=True))
        text = out.getvalue()
        self.assertIn("1 arbs, 1 new", text)                                      # the game later today
        self.assertIn("1 outliers, 0 new | held back: 1 live, waiting for another check", text)
        self.assertIn("0 new arbs, 0 new +EV, 0 new outliers | held back: 1 over the hourly cap", text)   # props
        self.assertIn("Feed freshness:", text)                                   # (and the rest of --once)
        self.assertEqual([r["reason"] for r in _read(self.files["log_file"])], ["capped"])   # (the held prop arb)

    def test_run_confirms_live_alerts_on_its_next_pass(self):
        import argparse, contextlib, io
        from unittest import mock
        real = datetime.now(timezone.utc)
        iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
        live_start = iso(real - timedelta(minutes=30))

        class Api:
            remaining, used = 50000.0, None

            def __init__(self, cfg):
                pass

            def events(self, sport, horizon_hours=26):
                return [{"id": "live1", "commence_time": live_start}]

            def odds(self, sport, until):
                ev = outlier_event(1.85, 1.95, start=live_start)
                ev.update(id="live1", sport_key="basketball_nba")
                return [stamped(ev, (datetime.now(timezone.utc) - NOW).total_seconds())]

        cfg = self.cfg(sports=["basketball_nba"], props_enabled=False, kalshi_check=False, summary_hour=-1,
                       results_minutes=0, live_confirm_checks=2, live_max_age_alert=60, parlays_enabled=False)
        args = argparse.Namespace(once=False, demo=False, dry_run=True, plan=False, check_kalshi=False,
                                  results=None, post_results=None)
        passes, windows = [], []
        real_scan, real_window = _arbbot.scan_main, _arbbot.Trackers.set_live_interval

        def scan(t, *a, **kw):
            passes.append(real_scan(t, *a, **kw))
            return passes[-1]

        def window(t, secs):
            windows.append(secs)
            return real_window(t, secs)
        with mock.patch("arbbot.OddsAPI", Api), mock.patch("arbbot.scan_main", scan), \
                mock.patch.object(_arbbot.Trackers, "set_live_interval", window), \
                mock.patch.object(_arbbot.Scheduler, "due", lambda s, now: list(s.cfg.sports)), \
                mock.patch.object(_arbbot.Scheduler, "interval", lambda s, st: 100.0), \
                mock.patch("arbbot.time.sleep", side_effect=[None, StopLoop]), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(StopLoop):
                _arbbot.run(cfg, args, _arbbot.Status(cfg, dry_run=True))
        self.assertEqual([(p.out_sent, p.held) for p in passes], [(0, {"waiting": 1}), (1, {})])
        self.assertIn("held back: 1 live, waiting for another check", out.getvalue())   # on the console line
        self.assertEqual(windows.count(100), 2)                                   # the live check rate, each pass


class LiveAlertsAlreadyUp(MixFiles):
    """Alerts already up can't send a live ping past the live rules: a much better price on a live
    one, or a pre-game card whose bet turns into a live alert of the other kind at kickoff."""

    def trackers(self, **kw):
        cfg = self.cfg(MIX, **{"live_confirm_checks": 2, "live_max_age_alert": 60, "ev_enabled": False,
                               "webhook_url": "https://x", "discord_mention": "@arb", "ev_mention": "@ev",
                               "outlier_mention": "@out", **kw})
        t = _arbbot.Trackers(cfg, self.args())
        sent = []
        for a in t.singles():
            a.dry_run = False
            a._discord = lambda payload, message_id=None, url="", _a=a: sent.append(
                (_a.noun, message_id or "POST", payload["embeds"][0]["title"], payload.get("content", ""))
            ) or message_id or f"m{len(sent)}"
        return t, sent

    @staticmethod
    def posts(sent, noun):
        return [s for s in sent if s[0] == noun and s[1] == "POST"]

    @staticmethod
    def arb_check(t, secs, home, ages, start=STARTED):
        ev = stamped(mix_event(home, 1.98, ev_id="g", start=start), secs)
        for title, age in ages.items():
            age_book(ev, title, age, at(secs))
        return _arbbot.scan_main(t, [ev], ["basketball_nba"], at(secs), kalshi=False)

    def test_a_live_arbs_better_price_needs_a_fresh_price_seen_twice(self):
        t, sent = self.trackers(outliers_enabled=False)
        self.arb_check(t, 0, 2.25, {"A": 10, "B": 10})                            # waits for a 2nd check
        self.arb_check(t, 60, 2.25, {"A": 10, "B": 10})                           # sent
        [op] = t.arbs.open.values()
        alerted = op.alerted_pct
        res = self.arb_check(t, 120, 2.60, {"A": 100, "B": 50})                   # +12.4%, but A's price is 100s old
        self.assertEqual((res.sent, len(self.posts(sent, "arbs"))), (0, 1))       # no new ping...
        self.assertEqual(sent[-1][1:3], ("m1", "💰 ARB · +$12.40 guaranteed (12.40%)"))   # ...the card is edited
        self.assertEqual(op.alerted_pct, alerted)                                 # so a later check can still send it
        self.assertEqual(self.arb_check(t, 180, 2.60, {"A": 5, "B": 5}).sent, 0)  # fresh, but seen once
        res = self.arb_check(t, 240, 2.60, {"A": 5, "B": 5})                      # fresh, two checks in a row
        self.assertEqual((res.sent, len(self.posts(sent, "arbs"))), (1, 2))
        self.assertEqual(sent[-2][1:3], ("m1", "⬆️ Better price: see the newer alert below"))
        self.assertEqual(sent[-1][1:], ("POST", "💰 ARB · +$12.40 guaranteed (12.40%)", "@arb"))
        self.assertEqual(len(t.live_cap.times), 2)                                # it took a live slot
        self.assertEqual(t.arbs.open["g|h2h|None"].arb.age, 5)                    # showing its own price's age
        # Before the game none of this applies: a better price pings at once, however old.
        pre, sent = self.trackers(outliers_enabled=False)
        self.arb_check(pre, 0, 2.25, {"A": 10, "B": 10}, start="2026-10-03T18:00:00Z")
        self.assertEqual(self.arb_check(pre, 60, 2.60, {"A": 600}, start="2026-10-03T18:00:00Z").sent, 1)
        self.assertEqual(len(self.posts(sent, "arbs")), 2)

    def test_a_better_price_counts_only_checks_in_a_row(self):
        fresh, old = {"A": 5, "B": 5}, {"A": 100, "B": 50}
        for why, steps, sends in (
                ("not better in between", [(2.60, fresh), (2.25, fresh), (2.60, fresh)], [0, 0, 0]),
                ("an old price in between", [(2.60, fresh), (2.60, old), (2.60, fresh)], [0, 0, 0]),
                ("after a re-ping it counts from zero", [(2.40, fresh), (2.40, fresh), (2.55, fresh)], [0, 1, 0])):
            t, sent = self.trackers(outliers_enabled=False)
            for secs in (0, 60):
                self.arb_check(t, secs, 2.25, {"A": 10, "B": 10})                 # alerted at 5.32%
            got = [self.arb_check(t, 120 + 60 * i, home, ages).sent for i, (home, ages) in enumerate(steps)]
            self.assertEqual(got, sends, why)                                     # each last one: seen once in a row

    def test_a_live_alert_whose_post_failed_is_sent_again_only_with_a_fresh_price(self):
        def make(state):
            cfg = self.cfg(MIX, live_max_age_alert=60, webhook_url="https://x", discord_mention="@arb")
            a, sent = Alerter(replace(cfg, state_dir=str(self.d / state)), dry_run=False), []

            def fake(payload, message_id=None, url=""):                           # Discord refuses the first post
                sent.append((message_id or "POST", payload["embeds"][0]["description"], payload.get("content", "")))
                a.send_retryable = len(sent) == 1
                return message_id or (None if len(sent) == 1 else "m1")
            a._discord = fake
            return a, sent

        def check(a, now, age, start=STARTED):
            ev = age_book(age_book(mix_event(2.25, 1.98, start=start), "A", age), "B", age)
            return a.handle(find_arbs([ev], MIX, NOW), ["basketball_nba"], now=now)
        a, sent = make("live")
        check(a, 1000, 15)
        [op] = a.open.values()
        self.assertTrue(op.retry)
        check(a, 1060, 75)                                                        # a check later: 75s old
        self.assertEqual((len(sent), op.retry), (1, True))                        # too old for a live alert: it waits
        check(a, 1120, 20)                                                        # fresh again: sent, with the ping
        self.assertEqual([(m, c) for m, _, c in sent], [("POST", "@arb"), ("POST", "@arb")])
        self.assertIn("⏱ prices were up to 20s old when sent", sent[-1][1])        # its age now, not at the failed post
        self.assertEqual((op.retry, op.arb.age), (False, 20))
        # Before the game the age rule doesn't apply: sent again at once, showing the price's age then.
        a, sent = make("pre")
        check(a, 1000, 15, start="2026-10-03T18:00:00Z")
        check(a, 1060, 600, start="2026-10-03T18:00:00Z")
        self.assertEqual([m for m, *_ in sent], ["POST", "POST"])
        self.assertIn("⏱ prices were up to 10 min old when sent", sent[-1][1])

    def test_a_full_live_cap_holds_the_better_price_ping(self):
        t, sent = self.trackers(outliers_enabled=False, live_per_hour=1)
        for secs in (0, 60):
            self.arb_check(t, secs, 2.25, {"A": 10, "B": 10})                     # the hour's one live alert
        # The better price is fresh on every check from then on (a minute apart).
        sends = [secs for secs in range(120, 3780, 60) if self.arb_check(t, secs, 2.60, {"A": 5, "B": 5}).sent]
        self.assertEqual(sends, [3660])                                           # not before the hour is up
        self.assertEqual(len(self.posts(sent, "arbs")), 2)
        self.assertIn(("arbs", "m1", "💰 ARB · +$12.40 guaranteed (12.40%)", ""), sent)   # until then: edited, no ping
        self.assertEqual(len(t.live_cap.times), 1)                                # (the first slot has expired)

    def test_after_a_restart_a_better_price_is_judged_by_its_own_age(self):
        cfg = self.cfg(MIX, live_max_age_alert=60, webhook_url="https://x")       # (one check is enough here)
        sent = []

        def make():
            a = Alerter(cfg, dry_run=False)
            a._discord = lambda payload, message_id=None, url="": sent.append(message_id or "POST") or message_id or "m1"
            return a
        [arb] = find_arbs([age_book(age_book(mix_event(2.25, 1.98, start=STARTED), "A", 10), "B", 10)], MIX, NOW)
        make().handle([arb], ["basketball_nba"], now=time.time())                 # sent with 10s-old prices
        [better] = find_arbs([age_book(age_book(mix_event(2.60, 1.98, start=STARTED), "A", 100), "B", 50)], MIX, NOW)
        b = make()                                                                # restart
        self.assertEqual(b.handle([better], ["basketball_nba"], now=time.time()), 0)   # A's price is 100s old now
        self.assertEqual(sent, ["POST", "m1"])                                    # edited, no new ping

    def test_live_outliers_and_live_plus_ev_too(self):
        def outlier(secs, home_prices, age):   # the other books move toward Home; Stale's price sits still
            ev = moved_event(home_prices)
            ev["sport_key"] = "basketball_nba"
            return age_book(stamped(ev, secs), "Stale", age, at(secs))

        def plus_ev(secs, price, age):
            ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", price, None)]}, start=STARTED)
            ev["sport_key"] = "basketball_nba"
            return age_book(stamped(ev, secs), "B", age, at(secs))
        for noun, make, small, big, kw in (
                ("outliers", outlier, (1.25, 4.10), (1.18, 5.0), {"arb_live": False}),
                ("+EV bets", plus_ev, 2.20, 2.40, {"ev_enabled": True, "ev_live": True, "outliers_enabled": False})):
            t, sent = self.trackers(**kw)

            def check(secs, prices, age):
                _arbbot.scan_main(t, [make(secs, prices, age)], ["basketball_nba"], at(secs), kalshi=False)
                return len(self.posts(sent, noun))
            self.assertEqual([check(0, small, 5), check(60, small, 5)], [0, 1], noun)
            self.assertEqual(check(120, big, 100), 1, noun)                       # bigger edge, old price
            self.assertEqual(check(180, big, 5), 1, noun)                         # fresh, seen once
            self.assertEqual(check(240, big, 5), 2, noun)                         # seen twice: a new ping
            self.assertEqual(len(t.live_cap.times), 2, noun)

    def test_a_pre_game_card_doesnt_let_its_live_bet_skip_the_live_rules(self):
        kw = dict(ev_enabled=True, min_ev_pct=3, max_ev_pct=100, ev_max_odds=10, min_profit_pct=50)
        t, sent = self.trackers(**kw)

        def check(secs, stale_home, start="2026-10-03T12:00:30Z"):               # (tips off 30s after the 1st check)
            ev = outlier_event(stale_home, 3.3, start=start)
            ev["sport_key"] = "basketball_nba"
            return _arbbot.scan_main(t, [stamped(ev, secs)], ["basketball_nba"], at(secs), kalshi=False)
        first = check(0, 1.36)                                                     # +6.7% before the game
        self.assertEqual((first.ev_sent, first.out_sent), (1, 0))
        [key] = t.evs.open
        second = check(60, 1.85)                                                   # live, and an outlier now
        self.assertEqual((second.out_sent, second.held), (0, {"waiting": 1}))     # a new live alert: it waits
        self.assertEqual((t.evs.open, t.outs.open, t.outs.handed), ({}, {}, {}))
        self.assertTrue(sent[-1][2].startswith("❌ GONE") and sent[-1][:2] == ("+EV bets", "m1"))   # closed as usual
        third = check(120, 1.85)
        self.assertEqual(third.out_sent, 1)
        self.assertEqual(t.outs.open[key].first_seen, at(120).timestamp())        # a new alert, not a hand-over
        self.assertEqual(len(t.live_cap.times), 1)                                # ...that counts as a live one
        # The same for a pre-game card picked back up after a restart.
        t, sent = self.trackers(**kw)
        t.evs.restored[key] = {"message_id": "123", "url": "https://x", "first_seen": at(0).timestamp(),
                               "label": "Home ML", "sport_key": "basketball_nba", "event_id": "e1",
                               "commence_time": "2026-10-03T12:00:30Z", "pick": "Home ML"}
        self.assertEqual(check(60, 1.85).held, {"waiting": 1})
        self.assertEqual((t.evs.restored, t.outs.handed), ({}, {}))
        self.assertIn(("+EV bets", "123", "❌ GONE · Home ML", ""), sent)
        # Live to live (EV_LIVE on) is still handed over at once: the live alert met the rules.
        t, sent = self.trackers(**kw, ev_live=True)
        for secs in (0, 60):
            check(secs, 1.85, start=STARTED)
        self.assertEqual(check(120, 1.36, start=STARTED).ev_sent, 1)             # back to +EV: no wait
        self.assertEqual(t.evs.open[key].first_seen, at(60).timestamp())
        self.assertIn(("outliers", "m1", "↘️ Back to a normal +EV edge: see the new 📈 alert", ""), sent)


class PriceThatWillMoveFirst(MixFiles):
    def test_the_leg_that_beats_pinnacle_goes_first(self):
        # Fair is 50/50. A's Home +125 is 12.5% better than fair; B's Away -102 is 1% worse.
        ev = mix_event(2.25, 1.98)
        ev["bookmakers"][1]["markets"][0]["outcomes"][0]["link"] = "https://a.example/slip"
        [arb] = find_arbs([ev], MIX, NOW)
        self.assertEqual([l.book for l in arb.legs], ["A", "B"])                 # not by name (Away would be 1st)
        self.assertEqual([round(l.edge, 2) for l in arb.legs], [12.5, -1.0])
        self.assertEqual(arb.fair_from, "Pinnacle")
        self.assertEqual(arb.keep_stake, kelly_stake(0.5, 2.25, MIX))
        [asym] = find_arbs([mix_event(2.25, 1.98, sharp=(1.80, 2.10))], MIX, NOW)
        p_home = devig([1.80, 2.10], "power")[0]
        self.assertEqual(asym.keep_stake, kelly_stake(p_home, 2.25, MIX))         # the +EV stake on Home alone
        self.assertNotEqual(asym.keep_stake, kelly_stake(1 - p_home, 2.25, MIX))
        d = discord_payload(arb)["embeds"][0]["description"]
        self.assertTrue(d.startswith("👉 **DO THIS NOW: place BOTH bets, 1️⃣ first. You profit no matter who wins.**"))
        self.assertIn("1️⃣ Open **[A](https://a.example/slip)** → bet", d)
        self.assertIn("\n     ↳ **Bet this one first:** it's the price that will move.", d)
        self.assertIn(f"\n     ↳ If the other price is gone, keep this one: it's a good bet alone. Only betting "
                      f"this one? Bet **{_arbbot.money(arb.keep_stake)}**.", d)
        self.assertIn("Place 1️⃣ first, then 2️⃣ right away. If 1️⃣ has moved past its skip line, skip both.", d)
        self.assertNotIn("back to back", d)
        self.assertEqual(discord_payload(arb)["embeds"][0]["url"], "https://a.example/slip")   # the title opens it
        self.assertIn("Home +125 on A  → stake $46.81  (bet first: +12.5% vs Pinnacle)\n", _arbbot.format_text(arb))
        # Pinnacle's line isn't the same bet (it has a draw): no fair price for these two sides.
        draw = mix_event(2.25, 1.98)
        draw["bookmakers"][0]["markets"][0]["outcomes"].append({"name": "Draw", "price": 12.0})
        [odd] = find_arbs([draw], MIX, NOW)
        self.assertFalse(odd.tagged)
        # No Pinnacle price: today's order and wording, and never "keep".
        [plain] = find_arbs([mix_event(2.25, 1.98, sharp=None)], MIX, NOW)
        self.assertEqual(([l.outcome for l in plain.legs], plain.fair_from, plain.keep_stake), (["Away", "Home"], "", 0))
        p = discord_payload(plain)["embeds"][0]["description"]
        self.assertTrue(p.startswith("👉 **DO THIS NOW: place BOTH bets. You profit no matter who wins.**"))
        self.assertIn("Do them back to back. If one price moved past its skip line, don't place the other.", p)
        for words in ("first", "keep"):
            self.assertNotIn(words, p)
        self.assertNotIn("bet first", _arbbot.format_text(plain))

    def test_keep_it_only_when_its_own_edge_clears_the_ev_minimum(self):
        [small] = find_arbs([mix_event(2.02, 1.99, sharp=(1.95, 1.95))], MIX, NOW)   # Home 1% better than fair
        d = discord_payload(small)["embeds"][0]["description"]
        self.assertIn("Bet this one first", d)
        self.assertNotIn("keep this one", d)
        self.assertEqual(small.keep_stake, 0)
        ev = mix_event(2.09, 2.05, sharp=(2.0, 2.0))                               # Home 4.5% better
        self.assertGreater(find_arbs([ev], replace(MIX, min_ev_pct=4), NOW)[0].keep_stake, 0)
        self.assertEqual(find_arbs([ev], replace(MIX, min_ev_pct=5), NOW)[0].keep_stake, 0)
        ev["sport_key"] = "basketball_ncaab"                                       # college needs 6% (SPORT_MIN_EV)
        self.assertEqual(find_arbs([ev], replace(MIX, min_ev_pct=4), NOW)[0].keep_stake, 0)
        prop = priced_prop({"Pinnacle": (2.0, 2.0), "DK": (2.09, 1.80), "FD": (1.80, 2.05)})
        cfg = replace(MIX, min_ev_pct=4, prop_min_ev_pct=7)                         # props: PROP_MIN_EV_PCT
        [p] = find_arbs([prop], cfg, NOW)
        self.assertTrue(p.tagged)
        self.assertEqual(p.keep_stake, 0)
        self.assertGreater(find_arbs([prop], replace(cfg, prop_min_ev_pct=4), NOW)[0].keep_stake, 0)

    def test_three_way_wording(self):
        ev = event({"Pinnacle": [("h2h", [("Home", 2.9, None), ("Away", 2.9, None), ("Draw", 3.3, None)])],
                    "A": [("h2h", [("Home", 3.4, None), ("Away", 2.5, None), ("Draw", 3.0, None)])],
                    "B": [("h2h", [("Home", 2.6, None), ("Away", 3.1, None), ("Draw", 3.2, None)])],
                    "C": [("h2h", [("Home", 2.6, None), ("Away", 2.5, None), ("Draw", 3.6, None)])]})
        ev["bookmakers"][0]["key"] = "pinnacle"
        [arb] = find_arbs([ev], MIX, NOW)
        self.assertEqual(len(arb.legs), 3)
        self.assertEqual([l.book for l in arb.legs], ["A", "C", "B"])              # most better than fair first
        d = discord_payload(arb)["embeds"][0]["description"]
        self.assertIn("place all 3 bets, 1️⃣ first", d)
        self.assertIn("then the others right away. If 1️⃣ has moved past its skip line, skip them all.", d)
        self.assertIn("If the other prices are gone, keep this one", d)

    def test_pinnacle_is_worked_out_once_per_game_with_an_arb(self):
        from unittest import mock
        ev = mix_event(2.25, 1.98)
        for bm in ev["bookmakers"][1:]:   # a totals arb in the same game
            bm["markets"].append({"key": "totals", "last_update": FRESH, "outcomes": [
                {"name": "Over", "price": 2.2 if bm["title"] == "A" else 1.7, "point": 220.5},
                {"name": "Under", "price": 1.7 if bm["title"] == "A" else 2.2, "point": 220.5}]})
        quiet = mix_event(1.90, 1.90, ev_id="quiet")
        real = _arbbot.sharp_fair
        with mock.patch("arbbot.sharp_fair", side_effect=real) as spy:
            arbs = find_arbs([ev, quiet], MIX, NOW)
        self.assertEqual(len(arbs), 2)
        self.assertEqual([c.args[0]["id"] for c in spy.call_args_list], ["e1"])
        self.assertEqual({a.market: a.tagged for a in arbs}, {"h2h": True, "totals": False})   # no Pinnacle total

    def test_guide_says_when_to_bet_first_and_when_to_keep(self):
        g = _arbbot.GUIDE
        self.assertIn("If a bet says **Bet this one first**, place it first: it's the price that will move.", g)
        self.assertIn('No "first" note? Place the bets back to back.', g)
        self.assertIn('the card says when the first is still worth keeping on its own ("keep this one")', g)
        self.assertNotIn("• Place the bets back to back.", g)
        self.assertIn("⏱ **Price age** on a card: how long the book had shown that price", g)
        self.assertIn("Player props don't show one", g)                          # (their books give no time per line)
        self.assertIn("A live alert only goes out once two checks in a row", g)
        self.assertIn("Games that haven't started come first; live alerts are kept to a few an hour.", g)


class NewYorkCollegeGames(MixFiles):
    """New York's sportsbooks can't take bets on games with a New York college team in them."""

    NY = replace(MIX, ny_rules=True)

    def test_which_games_count(self):
        def ny(team, sport="basketball_ncaab"):
            return _arbbot.ny_college_game({"sport_key": sport, "home_team": team, "away_team": "Duke Blue Devils"})
        for team in ("Syracuse Orange", "St. John's Red Storm", "Saint John's Red Storm", "Army Black Knights",
                     "LIU Sharks", "Albany Great Danes", "Buffalo Bulls", "Stony Brook Seawolves",
                     "St. Bonaventure Bonnies", "Le Moyne Dolphins", "Cornell Big Red"):
            self.assertTrue(ny(team), team)
        self.assertTrue(ny("Hobart Statesmen", "lacrosse_ncaa"))
        self.assertTrue(_arbbot.ny_college_game({"sport_key": "americanfootball_ncaaf", "home_team": "Clemson Tigers",
                                                 "away_team": "Syracuse Orange"}))         # either team
        for team in ("Buffalo State Bengals", "Albany State Golden Rams", "Albany St Golden Rams", "Miami Hurricanes",
                     "Columbia International Rams"):
            self.assertFalse(ny(team), team)
        self.assertFalse(ny("Buffalo Bills", "americanfootball_nfl"))                       # not college

    def test_settings(self):
        import os
        from unittest import mock
        for env, want in (({"US_STATE": "ny", "NY_RULES": ""}, True), ({"US_STATE": "NY", "NY_RULES": ""}, True),
                          ({"US_STATE": "ny", "NY_RULES": "false"}, False), ({"US_STATE": "nj", "NY_RULES": ""}, False),
                          ({"US_STATE": "", "NY_RULES": "true"}, True)):
            with mock.patch.dict(os.environ, env):
                self.assertEqual(Config.from_env().ny_rules, want, env)
        self.assertFalse(Config().ny_rules)

    def game(self, home="Syracuse Orange", sport="americanfootball_ncaaf", books=("FanDuel", "Kalshi")):
        return mix_event(2.25, 1.98, books=books, sport=sport, home_team=home, away_team="Clemson Tigers")

    def test_arbs(self):
        self.assertEqual(len(find_arbs([self.game()], MIX, NOW)), 1)
        self.assertEqual(find_arbs([self.game()], self.NY, NOW), [])                        # FanDuel can't take it
        self.assertEqual(len(find_arbs([self.game(books=("Kalshi", "Other"))], self.NY, NOW)), 1)   # Kalshi can
        for ok in (self.game("Albany State Golden Rams"), self.game("Buffalo Bills", "americanfootball_nfl")):
            self.assertEqual(len(find_arbs([ok], self.NY, NOW)), 1)

    def test_plus_ev_and_its_board(self):
        ev = ev_event([("Syracuse Orange", 1.91, None), ("Clemson Tigers", 1.91, None)],
                      {"FanDuel": [("Syracuse Orange", 2.30, None)], "Kalshi": [("Syracuse Orange", 2.20, None)]})
        ev.update(sport_key="americanfootball_ncaaf", home_team="Syracuse Orange", away_team="Clemson Tigers")
        for bm in ev["bookmakers"]:
            bm["key"] = {"FanDuel": "fanduel", "Kalshi": "kalshi"}.get(bm["title"], bm["key"])
        [b] = find_evs([ev], EVCFG, NOW)
        self.assertEqual((b.book, [r[0] for r in b.board]), ("FanDuel", ["FanDuel", "Kalshi"]))
        [b] = find_evs([ev], replace(EVCFG, ny_rules=True), NOW)
        self.assertEqual((b.book, [r[0] for r in b.board], b.also), ("Kalshi", ["Kalshi"], []))   # no FanDuel at all

    def test_outliers_their_board_hedge_and_parlays(self):
        def syracuse():
            ev = outlier_event(1.85, 1.95)
            ev.update(sport_key="basketball_ncaab", home_team="Syracuse Orange")
            for bm in ev["bookmakers"]:
                bm["key"] = {"DK": "draftkings", "FD": "fanduel", "MGM": "betmgm", "Stale": "kalshi"}.get(
                    bm["title"], bm["key"])
                bm["markets"][0]["outcomes"][0]["name"] = "Syracuse Orange"
            return ev
        [o] = find_outliers([syracuse()], Config(), NOW)
        self.assertEqual(o.book, "Stale")
        self.assertEqual(({r[0] for r in o.board}, o.hedge[0][1]), ({"Stale", "DK", "FD", "MGM"}, "DK"))
        self.assertEqual(o.parlay_books, {"Stale", "DK", "FD", "MGM", "Pinnacle"})
        [o] = find_outliers([syracuse()], Config(ny_rules=True), NOW)                        # still flagged (Kalshi)
        self.assertEqual(([r[0] for r in o.board], o.hedge, o.parlay_books), (["Stale"], [], {"Stale", "Pinnacle"}))
        nyb = syracuse()
        next(bm for bm in nyb["bookmakers"] if bm["title"] == "Stale")["key"] = "fanduel"
        self.assertEqual(len(find_outliers([nyb], Config(), NOW)), 1)
        self.assertEqual(find_outliers([nyb], Config(ny_rules=True), NOW), [])               # a NY book: not flagged


class ArbLogColumns(MixFiles):
    """arbs.csv: which price would move and by how much, every leg's edge and age (Pinnacle's too),
    how many checks it lasted, and the arbs the caps and live rules held back, with the reason."""

    def rows(self):
        return _read(self.files["log_file"])

    def test_alerted_arb_row(self):
        cfg = self.cfg(MIX)
        a = Alerter(cfg, dry_run=True)
        first = age_book(age_book(age_book(mix_event(2.25, 1.98), "A", 10), "B", 30), "Pinnacle", 45)
        first["bookmakers"][0]["markets"].insert(0, {"key": "spreads", "last_update": "2026-10-03T11:55:00Z",
                                                     "outcomes": [{"name": "Home", "price": 1.91, "point": -3.5},
                                                                  {"name": "Away", "price": 1.91, "point": 3.5}]})
        a.handle(find_arbs([first], MIX, NOW), ["basketball_nba"], now=1000)
        later = age_book(mix_event(2.30, 1.98), "A", 2)                          # a check later: new price and ages
        a.handle(find_arbs([later], MIX, NOW), ["basketball_nba"], now=1060)
        a.handle([], ["basketball_nba"], now=1120)
        [row] = self.rows()
        self.assertEqual((row["stale_book"], row["stale_edge_pct"], row["fair_from"]), ("A", "12.5", "Pinnacle"))
        self.assertEqual(row["leg_edges"], "A +12.5%; B -1.0%")                  # the other bet's cost vs fair
        self.assertEqual(row["leg_ages"], "A 10s; B 30s; Pinnacle 45s")          # as first sent, Pinnacle's too
        self.assertEqual((row["checks"], row["reason"], row["seconds_open"]), ("2", "", "120"))
        self.assertEqual(row["legs"], "Home @2.3 A; Away @1.98 B")                # (the latest prices, as before)
        # No Pinnacle price: nothing to measure the legs against.
        a.handle(find_arbs([mix_event(2.25, 1.98, sharp=None)], MIX, NOW), ["basketball_nba"], now=2000)
        a.handle([], ["basketball_nba"], now=2060)
        plain = self.rows()[1]
        self.assertEqual((plain["stale_book"], plain["stale_edge_pct"], plain["leg_edges"], plain["fair_from"],
                          plain["leg_ages"], plain["checks"]), ("", "", "", "", "B 0s; A 0s", "1"))

    def test_restart_keeps_the_first_sighting_and_the_count(self):
        cfg = self.cfg(MIX, webhook_url="https://x")

        def make():
            a = Alerter(cfg, dry_run=False)
            a._discord = lambda payload, message_id=None, url="": message_id or "m1"
            return a
        first = age_book(mix_event(2.25, 1.98), "A", 10)
        a = make()
        a.handle(find_arbs([first], MIX, NOW), ["basketball_nba"], now=time.time())
        a.handle(find_arbs([mix_event(2.25, 1.98)], MIX, NOW), ["basketball_nba"], now=time.time())
        b = make()                                                                # restart
        b.handle(find_arbs([mix_event(2.25, 1.98)], MIX, NOW), ["basketball_nba"], now=time.time())
        b.handle([], ["basketball_nba"], now=time.time())
        [row] = self.rows()
        self.assertEqual((row["leg_ages"], row["checks"]), ("A 10s; B 0s; Pinnacle 0s", "3"))

    def test_held_back_arbs_are_logged_with_the_reason(self):
        cfg = self.cfg(MIX)
        a = Alerter(cfg, dry_run=True)
        a.max_per_hour, a.posted_at = 1, [1000.0]                                 # the hour's one arb went out
        [arb] = find_arbs([mix_event(2.25, 1.98)], MIX, NOW)
        for now in (1060, 1120, 1180):
            self.assertEqual(a.handle([arb], ["basketball_nba"], now=now), 0)
        [row] = self.rows()                                                       # once per stretch, not each check
        self.assertEqual((row["reason"], row["stale_book"], row["leg_ages"], row["gone_at"], row["checks"]),
                         ("capped", "A", "A 0s; B 0s; Pinnacle 0s", "", ""))
        a.handle([arb], ["basketball_nba"], now=1180 + _arbbot.HELD_LOG_GAP + 1)  # held again much later
        self.assertEqual([r["reason"] for r in self.rows()], ["capped", "capped"])
        live = Alerter(cfg, dry_run=True)
        live.live_cap = _arbbot.HourlyCap(1)
        live.live_cap.times.append(1000.0)
        [l_arb] = find_arbs([mix_event(2.25, 1.98, ev_id="live", start=STARTED)], MIX, NOW)
        live.handle([l_arb], ["basketball_nba"], now=1060)
        self.assertEqual(self.rows()[-1]["reason"], "live cap")
        # Held-back rows aren't counted as alerted arbs anywhere.
        a.handle([arb], ["basketball_nba"], now=1000 + 7200)                     # a new hour: alerted
        a.handle([], ["basketball_nba"], now=1000 + 7260)
        self.assertEqual(_arbbot.arbs_on(cfg, _arbbot.local_day(cfg, self.rows()[-1]["first_seen"])).split(" · ")[0],
                         "💰 **Arbs:** 1 different")

    def test_live_rules_log_what_they_held(self):
        cfg = self.cfg(MIX, live_confirm_checks=2, live_max_age_alert=60, ev_enabled=False, outliers_enabled=False)
        t = _arbbot.Trackers(cfg, self.args())

        def check(secs, evs):
            return _arbbot.scan_main(t, [stamped(e, secs) for e in evs], ["basketball_nba"], at(secs), kalshi=False)
        check(0, [mix_event(2.25, 1.98, ev_id="blip", start=STARTED)])
        res = check(60, [])                                                       # gone before its 2nd check
        self.assertEqual(res.held, {"unconfirmed": 1})
        self.assertIn("1 live, gone before it was confirmed", _arbbot.held_text(res.held))
        [row] = self.rows()
        self.assertEqual((row["reason"], row["seconds_open"], row["checks"], row["live"]), ("unconfirmed", "60", "1", "True"))
        self.assertTrue(row["gone_at"])
        old = stamped(mix_event(2.25, 1.98, ev_id="old", start=STARTED), 120)
        age_book(age_book(old, "A", 70, at(120)), "B", 80, at(120))             # both bet prices over a minute old
        res = _arbbot.scan_main(t, [old], ["basketball_nba"], at(120), kalshi=False)
        self.assertEqual(res.held, {"old price": 1})
        self.assertEqual((self.rows()[-1]["reason"], self.rows()[-1]["leg_ages"]), ("old price", "A 70s; B 80s; Pinnacle 0s"))
        self.assertEqual(len(self.rows()), 2)                                     # (waiting isn't logged)

    def test_confirmed_live_arbs_count_every_check_that_found_them(self):
        """`spotted` (first check that found it) and `checks` mean the same on every row, so live arbs
        sent after two checks compare with the ones gone first. first_seen/seconds_open on an alerted
        row stay "from when the alert went out"."""
        cfg = self.cfg(MIX, live_confirm_checks=2, live_max_age_alert=60, live_per_hour=1, ev_enabled=False,
                       outliers_enabled=False)
        t = _arbbot.Trackers(cfg, self.args())

        def check(secs, games):
            evs = [mix_event(home, 1.98, ev_id=g, start=STARTED) for g, home in games]
            _arbbot.scan_main(t, [stamped(e, secs) for e in evs], ["basketball_nba"], at(secs), kalshi=False)
        check(0, [("real", 2.30), ("capped", 2.25), ("blip", 2.25)])
        check(60, [("real", 2.30), ("capped", 2.25)])     # real goes out; capped: confirmed, but the live cap is full
        check(120, [("real", 2.30)])
        check(180, [])                                     # real is gone
        rows = {r["reason"]: r for r in self.rows()}
        cols = ("spotted", "first_seen", "seconds_open", "checks")
        noon, sent = "2026-10-03T12:00:00+00:00", "2026-10-03T12:01:00+00:00"
        self.assertEqual([rows[""][c] for c in cols], [noon, sent, "120", "3"])  # found on 3 checks, open 120s after
        self.assertEqual([rows["unconfirmed"][c] for c in cols], [noon, noon, "60", "1"])
        self.assertEqual([rows["live cap"][c] for c in cols], [noon, sent, "", ""])
        # A restart keeps the first sighting.
        web = self.cfg(MIX, webhook_url="https://x")

        def make():
            a = Alerter(web, dry_run=False)
            a._discord = lambda payload, message_id=None, url="": message_id or "m1"
            return a
        [live] = find_arbs([mix_event(2.25, 1.98, ev_id="r", start=STARTED)], MIX, NOW)
        t0 = time.time()
        make().handle([replace(live, found=(t0 - 60, 2))], ["basketball_nba"], now=t0)
        b = make()
        b.handle([live], ["basketball_nba"], now=t0 + 60)
        b.handle([], ["basketball_nba"], now=t0 + 120)
        row = self.rows()[-1]
        self.assertEqual((row["spotted"], row["checks"]),
                         (datetime.fromtimestamp(t0 - 60, timezone.utc).isoformat(timespec="seconds"), "3"))

    def test_prop_check_counts_against_the_arb_cap(self):
        t = _arbbot.Trackers(self.cfg(MIX, max_arb_per_hour=1, ev_enabled=False, outliers_enabled=False), self.args())
        main = _arbbot.scan_main(t, [mix_event(2.25, 1.98, ev_id="g1")], ["basketball_nba"], NOW, kalshi=False)
        self.assertEqual(main.sent, 1)
        prop = priced_prop({"Pinnacle": (2.0, 2.0), "DK": (2.20, 1.80), "FD": (1.80, 2.05)})
        res = _arbbot.scan_props(t, [prop], {"p1"}, at(60))
        self.assertEqual((res.n_arb, res.held), (0, {"capped": 1}))
        self.assertEqual(_arbbot.held_text(res.held), " | held back: 1 over the hourly cap")
        self.assertEqual([r["reason"] for r in _read(self.files["log_file"])], ["capped"])   # same arbs.csv

    def test_only_arbs_log_held_alerts(self):
        cfg = self.cfg(Config())
        o = OutlierAlerter(cfg, dry_run=True)
        o.max_per_hour, o.posted_at = 1, [1000.0]
        o.handle(find_outliers([outlier_event(1.85, 1.95)], cfg, NOW), now=1060)
        self.assertEqual(o.held_counts, {"capped": 1})
        p = ParlayAlerter(cfg, dry_run=True)
        p.max_per_hour, p.posted_at = 1, [1000.0]
        legs = [ev_leg(f"g{i}", {"DK": 2.30}) for i in range(2)]
        p.handle(find_parlays([replace(b, event_id=b.event_id) for b in legs], replace(cfg, parlay_min_ev_pct=1)),
                 now=1060)
        self.assertEqual(p.held_counts, {"capped": 1})
        self.assertFalse(any(Path(self.files[k]).exists() for k in ("outlier_log_file", "parlay_log_file", "log_file")))


class AccountSafeArbs(MixFiles):
    def test_kalshi_off_price_against_a_normal_sportsbook_bet_ranks_higher(self):
        def arb(books, home=2.25, ev_id="g"):
            [a] = find_arbs([mix_event(home, 1.98, books=books, ev_id=ev_id)], MIX, NOW)
            return a
        safe, book = arb(("Kalshi", "FanDuel"), ev_id="safe"), arb(("DraftKings", "FanDuel"), ev_id="book")
        self.assertEqual((safe.account_safe, book.account_safe), (True, False))
        self.assertFalse(arb(("FanDuel", "Kalshi")).account_safe)                     # the off price at a sportsbook
        self.assertFalse(replace(safe, legs=[replace(l, edge=None) for l in safe.legs]).account_safe)   # no fair
        both_good = find_arbs([mix_event(2.25, 2.10, books=("Kalshi", "FanDuel"))], MIX, NOW)[0]
        self.assertFalse(both_good.account_safe)                                    # FanDuel's bet beats fair too
        a = Alerter(self.cfg(MIX), dry_run=True)
        a.max_per_hour = 1
        a.handle([book, safe], now=1000)
        self.assertEqual(list(a.open), [safe.key])                                  # same %: the safe one
        a = Alerter(self.cfg(MIX), dry_run=True)
        a.max_per_hour = 1
        slightly_smaller = arb(("Kalshi", "FanDuel"), home=2.23, ev_id="safe")
        self.assertLess(book.profit_pct - slightly_smaller.profit_pct, _arbbot.ACCOUNT_SAFE_BONUS)
        a.handle([book, slightly_smaller], now=1000)
        self.assertEqual(list(a.open), [slightly_smaller.key])
        a = Alerter(self.cfg(MIX), dry_run=True)
        a.max_per_hour = 1
        much_smaller = arb(("Kalshi", "FanDuel"), home=2.15, ev_id="safe")
        a.handle([book, much_smaller], now=1000)
        self.assertEqual(list(a.open), [book.key])                                  # a bonus, not a trump card
        d = discord_payload(safe)["embeds"][0]["description"]
        self.assertEqual(d.replace("Kalshi", "DraftKings"), discord_payload(book)["embeds"][0]["description"])   # rank only


class ScanWiring(MixFiles):
    """What run() does on each check, through scan_main / scan_props (the loop's own steps)."""

    PRE = "2026-10-03T18:00:00Z"

    def trackers(self, **kw):
        return _arbbot.Trackers(self.cfg(Config(min_ev_pct=3, max_ev_pct=100, ev_max_odds=10, round_stakes=0),
                                         **kw), self.args())

    def main(self, t, evs, secs, **kw):
        return _arbbot.scan_main(t, [stamped(e, secs) for e in evs], ["basketball_nba"], at(secs), **kw)

    def game(self, stale_home, ev_id="e1"):
        ev = outlier_event(stale_home, 3.3, start=self.PRE)
        ev.update(id=ev_id, sport_key="basketball_nba")
        return ev

    def test_a_bet_that_grows_into_an_outlier_is_handed_over(self):
        t = self.trackers(kalshi_check=False)
        first = self.main(t, [self.game(1.36)], 0)                                 # +6.7%: a +EV bet
        self.assertEqual((first.ev_sent, first.out_sent), (1, 0))
        [key] = t.evs.open
        second = self.main(t, [self.game(1.85)], 60)                               # +44%: an outlier now
        self.assertEqual(second.evs, [])                                          # one card per bet
        self.assertEqual((list(t.evs.open), list(t.outs.open)), ([], [key]))
        self.assertEqual(t.outs.open[key].first_seen, at(0).timestamp())          # the same bet, handed over

    def test_prop_bet_that_grows_into_an_outlier_is_handed_over(self):
        t = self.trackers(kalshi_check=False)

        def prop(over):
            return priced_prop({"Pinnacle": (1.91, 1.91), "DK": (1.90, 1.92), "FD": (1.91, 1.91),
                                "MGM": (1.92, 1.90), "Stale": (over, 1.70)})
        self.assertEqual(_arbbot.scan_props(t, [prop(2.15)], {"p1"}, at(0)).n_ev, 1)
        [key] = t.prop_evs.open
        _arbbot.scan_props(t, [prop(2.60)], {"p1"}, at(60))
        self.assertEqual((list(t.prop_evs.open), list(t.prop_outs.open)), ([], [key]))
        self.assertEqual(t.prop_outs.open[key].first_seen, at(0).timestamp())

    def test_a_book_that_moves_away_from_the_market_isnt_an_outlier(self):
        t = self.trackers(kalshi_check=False, ev_enabled=False)
        self.main(t, [self.game(1.25)], 0)                                         # in line with the others
        self.assertEqual(self.main(t, [self.game(1.85)], 60).outs, [])            # jumped away: it's fast, not stale

    def test_sharp_line_movement_reaches_the_confidence(self):
        t = self.trackers(kalshi_check=False, outliers_enabled=False)

        def pre(home, away):
            ev = ev_event([("Home", home, None), ("Away", away, None)], {"B": [("Home", 2.20, None)]})
            ev["sport_key"] = "basketball_nba"
            return ev
        self.main(t, [pre(2.02, 1.82)], 0)
        self.main(t, [pre(1.91, 1.91)], 600)                                      # Pinnacle moved toward Home
        [op] = t.evs.open.values()
        self.assertTrue(any("moving this way" in n for n in op.arb.confidence_notes))

    def test_kalshi_is_asked_and_used(self):
        from unittest import mock
        t = self.trackers(outliers_enabled=False)
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})
        ev["sport_key"] = "basketball_nba"
        disagrees = {"e1": {"Home": (0.40, 0.39, 0.41), "Away": (0.60, 0.59, 0.61)}}
        with mock.patch("arbbot.kalshi_fair", return_value=disagrees) as asked:
            self.assertEqual(self.main(t, [ev], 0).evs, [])                         # Kalshi says 40%: skipped
        self.assertEqual(asked.call_count, 1)
        with mock.patch("arbbot.kalshi_fair", side_effect=AssertionError("network")):
            self.assertEqual(len(self.main(t, [ev], 60, kalshi=False).evs), 1)

    def test_closing_lines_follow_every_logged_bet(self):
        t = self.trackers(kalshi_check=False, outliers_enabled=False)
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})
        ev["sport_key"] = "basketball_nba"
        self.main(t, [ev], 0)
        self.main(t, [ev], 60)
        self.assertEqual(list(t.closing.tracked), ["e1|h2h|Home|"])                # logged -> followed to kickoff
        self.assertEqual(t.closing.latest["e1|h2h|Home|"][0], 0.5)                 # and read on each check
        p = priced_prop({"Pinnacle": (1.91, 1.91), "DK": (2.15, 1.80)})
        _arbbot.scan_props(t, [p], {"p1"}, at(0))
        _arbbot.scan_props(t, [p], {"p1"}, at(60))
        prop_id = next(k for k in t.closing.tracked if k.startswith("p1"))
        self.assertIn(prop_id, t.closing.latest)

    def test_main_line_closing_lines_are_saved_without_a_prop_check(self):
        t = self.trackers(kalshi_check=False, outliers_enabled=False)
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})
        ev["sport_key"] = "basketball_nba"
        self.main(t, [ev], 0)
        self.main(t, [ev], 60)                                                     # logged and followed
        self.main(t, [ev], 6 * 3600 + 60)                                          # started; no prop check ran
        self.assertEqual([(r["bet_id"], float(r["closing_fair_prob"])) for r in _read(self.files["closing_file"])],
                         [("e1|h2h|Home|", 0.5)])
        self.assertEqual(t.closing.tracked, {})

    def test_prop_check_uses_prop_settings_and_only_closes_its_own_games(self):
        t = self.trackers(kalshi_check=False)

        def prop(over, gid):
            ev = priced_prop({"Pinnacle": (1.91, 1.91), "DK": (over, 1.80)})
            ev["id"] = gid
            return ev
        self.assertEqual(_arbbot.scan_props(t, [prop(2.10, "p1")], {"p1"}, at(0)).n_ev, 0)   # 5%: props need 7%
        self.assertEqual(_arbbot.scan_props(t, [prop(2.15, "p1"), prop(2.15, "p2")], {"p1", "p2"}, at(60)).n_ev, 2)
        _arbbot.scan_props(t, [prop(2.15, "p1")], {"p1"}, at(120))                # only p1 was checked
        self.assertEqual(sorted(op.arb.event_id for op in t.prop_evs.open.values()), ["p1", "p2"])

    def test_every_cap_comes_from_the_settings(self):
        t = _arbbot.Trackers(self.cfg(max_ev_per_hour=5, max_prop_per_hour=4, max_parlay_per_hour=3), self.args())
        self.assertEqual((t.evs.max_per_hour, t.prop_evs.max_per_hour, t.parlays.max_per_hour), (5, 4, 3))


# --------------------------------------------------------------------------- locks for props Pinnacle doesn't price

BROTHERS_BOOKS = "fanduel,draftkings,betmgm,williamhill_us,kalshi"   # MY_BOOKS in New York
PROP_KEYS = {"FanDuel": "fanduel", "DraftKings": "draftkings", "BetMGM": "betmgm", "Caesars": "williamhill_us",
             "ESPN BET": "espnbet", "BetRivers": "betrivers", "Fanatics": "fanatics", "Kalshi": "kalshi",
             "Novig": "novig", "BetOnline.ag": "betonlineag", "LowVig.ag": "lowvig", "Pinnacle": "pinnacle"}
# FanDuel's Over is off (+120); the other five sportsbooks agree it's a coin flip.
LOCK_SIX = {"FanDuel": (2.20, 1.68), "DraftKings": (1.91, 1.91), "BetMGM": (1.93, 1.89), "Caesars": (1.90, 1.92),
            "ESPN BET": (1.92, 1.90), "BetRivers": (1.89, 1.93)}
LEBRON = ("player_points", ("LeBron James", 25.5))
CONSENSUS_SIX = "consensus of DraftKings, BetMGM, Caesars, ESPN BET, BetRivers"
AGES = dict(pregame_max_age_seconds=10**6, far_max_age_seconds=10**6)   # the same quotes, looked at for hours


def keyed_prop(books, **kw):
    """prop_event with the real Odds API book keys (sister books, exchanges and MY_BOOKS go by key)."""
    ev = prop_event(books, **kw)
    for bm in ev["bookmakers"]:
        bm["key"] = PROP_KEYS.get(bm["title"], bm["key"])
    return ev


def pinnacle_lines(ev, lines, player="LeBron James", market="player_points"):
    """Pinnacle prices `player`'s `market` at each (point, over, under) in lines."""
    outs = [o for q, ov, un in lines for o in ({"name": "Over", "description": player, "price": ov, "point": q},
                                               {"name": "Under", "description": player, "price": un, "point": q})]
    ev["bookmakers"].append({"key": "pinnacle", "title": "Pinnacle", "last_update": FRESH,
                             "markets": [{"key": market, "last_update": FRESH, "outcomes": outs}]})
    return ev


def pinnacle_at(ev, point, over, under):
    """Pinnacle prices the same player and stat, at another point."""
    return pinnacle_lines(ev, [(point, over, under)])


class PropsInLocks(MixFiles):
    """A prop Pinnacle doesn't price is judged against the other books' median, and can be a lock."""

    def locks(self, **kw):
        return self.cfg(Config(my_books=BROTHERS_BOOKS, round_stakes=0), **kw).with_mode().for_props()

    def looks(self, books_by_look, cfg=None, h=None, keep=frozenset(), start=0, every=31, events=None, kickoff=None):
        """find_evs on each look, `every` minutes apart (one price history); the last look's bets."""
        cfg, h = cfg or self.locks(**AGES), h if h is not None else _arbbot.PriceHistory()
        game = {"start": kickoff} if kickoff else {}
        out = []
        for i, books in enumerate(books_by_look):
            out = find_evs(events or [keyed_prop(books, **game)], cfg, at(60 * (start + every * i)), prices=h, keep=keep)
        return out

    def wired(self, sent, **kw):
        """Locks Trackers whose prop alerters post to a fake Discord (with @here pings), into sent."""
        cfg = self.cfg(Config(my_books=BROTHERS_BOOKS, round_stakes=0, webhook_url="https://x", ev_mention="@here",
                              outlier_mention="@here", **AGES), **kw).with_mode()
        t = _arbbot.Trackers(cfg, self.args(dry_run=False))
        for a in (t.prop_evs, t.prop_outs):
            def fake(payload, message_id=None, url=""):
                sent.append(("PATCH" if message_id else "POST", payload["embeds"][0]["title"],
                             payload.get("content", "")))
                return message_id or f"m{len(sent)}"
            a._discord = fake
        return t

    def alerter(self, sent, cfg=None):
        a = EVAlerter(replace(cfg or self.locks(), webhook_url="https://x"), dry_run=False, noun="+EV props")

        def fake(payload, message_id=None, url=""):
            sent.append(("PATCH" if message_id else "POST", payload["embeds"][0]["title"], payload.get("content", "")))
            return message_id or f"m{len(sent)}"
        a._discord = fake
        return a

    # ---- what a lock looks like, and that every point is needed
    def test_six_sportsbooks_with_fanduel_off_is_a_lock(self):
        [b] = find_evs([keyed_prop(LOCK_SIX)], self.locks(), NOW)
        self.assertEqual((b.book, b.outcome, b.confidence), ("FanDuel", "Over", "high"))
        self.assertAlmostEqual(b.fair_prob, 0.5, places=6)            # the other five's median, FanDuel left out
        self.assertAlmostEqual(b.ev_pct, 10.0, places=4)
        self.assertEqual(b.sharp_book, CONSENSUS_SIX)                  # names the books it's from
        self.assertEqual(b.confidence_notes, ["other books agree (within 1.1% win chance)",
                                              "6 sportsbooks price it (2 you can't bet at)",
                                              "no Pinnacle price: stake 30% smaller"])
        card = ev_payload(b)["embeds"][0]
        self.assertIn("Confidence: **🟢 High** · other books agree (within 1.1% win chance), 6 sportsbooks price it "
                      "(2 you can't bet at), no Pinnacle price: stake 30% smaller", card["description"])
        self.assertIn(f"({CONSENSUS_SIX})", card["footer"]["text"])
        self.assertEqual(b.stake, kelly_stake(0.5, 2.20, self.locks(), 0.7))   # 30% smaller, high = full
        self.assertIn("When Pinnacle doesn't price a prop, its true odds come from the other books: the card names "
                      "them, and the stake is 30% smaller.", _arbbot.GUIDE)
        # ...and only a lock when every point is there.
        five = {k: v for k, v in LOCK_SIX.items() if k != "BetRivers"}             # 5 sportsbooks
        apart = dict(LOCK_SIX, BetMGM=(1.944, 1.869), **{"ESPN BET": (1.944, 1.869)}, Caesars=(1.869, 1.944),
                     BetRivers=(1.869, 1.944))                                    # 2.1% win chance apart
        big = dict(LOCK_SIX, FanDuel=(2.30, 1.62))                                 # 15%: over 12%, an outlier's job
        small = dict(LOCK_SIX, FanDuel=(2.12, 1.73))                               # 6%: under the 8% prop bar
        for books in (five, apart, big, small):
            self.assertEqual(find_evs([keyed_prop(books)], self.locks(), NOW), [], books)
        early = keyed_prop(LOCK_SIX, start="2026-10-04T06:00:00Z")                 # 18 hours out: props need 12
        self.assertEqual(find_evs([early], self.locks(far_max_age_seconds=10**6, confident_hours=24), NOW), [])

    def test_consensus_confidence_points(self):
        from arbbot import Agreement, rate_confidence
        self.assertEqual(rate_confidence(None, None, 3, 10, True, consensus=Agreement(1.5, 1.5, 6))[0], "high")
        missed = []
        lvl, notes = rate_confidence(None, None, 3, 10, True, consensus=Agreement(3.0, 3.0, 6), missed=missed)
        self.assertEqual((lvl, notes[0], missed), ("medium", "other books roughly agree (within 3.0% win chance)",
                                                  ["books disagree"]))
        missed = []
        lvl, notes = rate_confidence(None, None, 3, 10, True, consensus=Agreement(1.5, 1.5, 5), missed=missed)
        self.assertEqual((lvl, notes[1], missed), ("medium", "only 5 sportsbooks price it", ["under 6 sportsbooks"]))
        missed = []
        self.assertEqual(rate_confidence(None, None, 13, 13, True, consensus=Agreement(1.5, 1.5, 6), missed=missed)[0],
                         "medium")
        self.assertEqual(missed, ["over 12 hours out", "edge over 12%"])
        lvl, notes = rate_confidence(None, None, 3, 10, True, consensus=Agreement(5.0, 6.2, 4, 1))
        self.assertEqual((lvl, notes), ("low", ["other books disagree (6% win chance apart)",
                                                "only 4 sportsbooks price it (1 you can't bet at)"]))
        lvl, notes = rate_confidence(None, None, 3, 10, True, consensus=Agreement(1.4, 2.5, 6))
        self.assertEqual((lvl, notes[0]), ("high", "other books agree (all but one within 1.4% win chance)"))
        self.assertEqual(rate_confidence(None, None, 3, 10, True)[0], "medium")   # no sharp, no consensus: as before

    def test_one_odd_book_cant_swing_the_agreement(self):
        odd = dict(LOCK_SIX, **{"ESPN BET": (1.80, 2.04)})                         # one book 3+ points out
        c = consensus_fair(keyed_prop(odd), Config().for_props(), NOW, False, {"pinnacle"}, leave_out="fanduel")[LEBRON]
        self.assertGreater(c.full_spread["Over"], 3.5)
        self.assertLess(c.spread["Over"], 1.2)                                     # the other four are close
        [b] = find_evs([keyed_prop(odd)], self.locks(), NOW)
        self.assertEqual(b.confidence, "high")
        self.assertIn("other books agree (all but one within 1.1% win chance)", b.confidence_notes)
        few = consensus_fair(keyed_prop({k: odd[k] for k in ("DraftKings", "BetMGM", "ESPN BET")}),
                             replace(Config().for_props(), consensus_min_books=3), NOW, False, set())[LEBRON]
        self.assertEqual(few.spread, few.full_spread)                              # 3 votes: nothing left out

    def test_only_sportsbooks_count_toward_the_six(self):
        swap = {**{k: v for k, v in LOCK_SIX.items() if k != "BetRivers"}, "Kalshi": (1.89, 1.93)}
        self.assertEqual(find_evs([keyed_prop(swap)], self.locks(), NOW), [])   # 5 sportsbooks + an exchange
        [b] = find_evs([keyed_prop(dict(swap, Fanatics=(1.90, 1.92)))], self.locks(), NOW)
        self.assertEqual(b.confidence, "high")                                     # a 6th sportsbook
        self.assertIn("6 sportsbooks price it (2 you can't bet at)", b.confidence_notes)   # ESPN BET, Fanatics
        self.assertEqual(b.sources_used, 6)                                        # the exchange is in the median

    def test_a_prop_alerted_at_an_exchange_doesnt_count_it_toward_the_six(self):
        others = {k: v for k, v in LOCK_SIX.items() if k != "FanDuel"}            # 5 sportsbooks
        [b] = find_evs([keyed_prop({"Kalshi": (2.20, 1.68), **others})], replace(self.locks(), min_confidence="low"), NOW)
        self.assertEqual((b.book, b.confidence), ("Kalshi", "medium"))
        self.assertIn("only 5 sportsbooks price it (2 you can't bet at)", b.confidence_notes)

    def test_you_cant_bet_at_counts_new_yorks_college_rule(self):
        books = {("Kalshi" if t == "FanDuel" else t): v for t, v in LOCK_SIX.items()}
        ev = keyed_prop(books)
        ev.update(sport_key="americanfootball_ncaaf", sport_title="NCAAF", home_team="Syracuse Orange",
                  away_team="Clemson Tigers")                                     # NY books can't take it
        [b] = find_evs([ev], replace(self.locks(), ny_rules=True, min_confidence="low"), NOW)
        self.assertEqual(b.book, "Kalshi")
        self.assertIn("only 5 sportsbooks price it (5 you can't bet at)", b.confidence_notes)

    # ---- a fair fair price
    def test_the_judged_book_is_left_out_of_its_own_median(self):
        cfg, ev = Config().for_props(), keyed_prop(LOCK_SIX)
        c = consensus_fair(ev, cfg, NOW, False, {"pinnacle"}, leave_out="fanduel")[LEBRON]
        self.assertEqual(c.votes, 5)
        self.assertAlmostEqual(c.probs["Over"], 0.5, places=6)
        self.assertAlmostEqual(c.full_spread["Over"], 1.12, places=2)
        everyone = consensus_fair(ev, cfg, NOW, False, {"pinnacle"})[LEBRON]
        self.assertEqual(everyone.votes, 6)
        self.assertLess(everyone.probs["Over"], 0.499)                             # FanDuel would drag it down

    def test_sister_books_are_one_vote(self):
        cfg = Config().for_props()
        books = {"DraftKings": (1.95, 1.87), "BetMGM": (1.93, 1.89), "Caesars": (1.91, 1.91),
                 "BetOnline.ag": (1.80, 2.04), "LowVig.ag": (1.80, 2.04)}
        c = consensus_fair(keyed_prop(books), cfg, NOW, False, {"pinnacle"})[LEBRON]
        self.assertEqual((c.votes, c.books[-1]), (4, ("betonlineag", "lowvig")))
        xs = sorted(devig(list(p), "power")[0] for p in ((1.95, 1.87), (1.93, 1.89), (1.91, 1.91), (1.80, 2.04)))
        self.assertAlmostEqual(c.probs["Over"], (xs[1] + xs[2]) / 2, places=6)
        # Leaving one out leaves its sister out too: 3 votes is under PROP_MIN_BOOKS (4).
        self.assertEqual(consensus_fair(keyed_prop(books), cfg, NOW, False, {"pinnacle"}, leave_out="lowvig"), {})
        few = keyed_prop({"FanDuel": (2.20, 1.68), "DraftKings": (1.91, 1.91), "BetMGM": (1.93, 1.89),
                          "BetOnline.ag": (1.90, 1.92), "LowVig.ag": (1.90, 1.92)})
        self.assertEqual(find_evs([few], Config(min_confidence="low").for_props(), NOW), [])   # 3 other votes
        # The main-line check of Pinnacle against the market still counts them as two books.
        main = ev_event([("Home", 1.91, None), ("Away", 1.91, None)],
                        {"B": [("Home", 2.20, None)], "BetOnline.ag": [("Home", 1.90, None), ("Away", 1.92, None)],
                         "LowVig.ag": [("Home", 1.90, None), ("Away", 1.92, None)],
                         "C": [("Home", 1.92, None), ("Away", 1.90, None)]})
        for bm in main["bookmakers"]:
            bm["key"] = PROP_KEYS.get(bm["title"], bm["key"])
        [b] = find_evs([main], Config(min_ev_pct=3, round_stakes=0), NOW)
        self.assertIn("other books agree", b.confidence_notes)

    def test_sister_books_average_their_prices(self):
        books = {"DraftKings": (2.05, 1.80), "BetMGM": (1.95, 1.87), "Caesars": (1.80, 2.04),
                 "BetOnline.ag": (1.80, 2.04), "LowVig.ag": (2.10, 1.75)}
        c = consensus_fair(keyed_prop(books), Config().for_props(), NOW, False, {"pinnacle"})[LEBRON]
        sisters = (devig([1.80, 2.04], "power")[0] + devig([2.10, 1.75], "power")[0]) / 2
        xs = sorted([devig(list(books[t]), "power")[0] for t in ("DraftKings", "BetMGM", "Caesars")] + [sisters])
        self.assertAlmostEqual(c.probs["Over"], (xs[1] + xs[2]) / 2, places=6)

    # ---- the fast-book guard
    def test_a_book_that_moves_first_is_held_back(self):
        h = _arbbot.PriceHistory()
        cfg = self.locks(**AGES)
        self.assertEqual(find_evs([keyed_prop(dict(LOCK_SIX, FanDuel=(1.91, 1.91)))], cfg, at(-1800), prices=h), [])
        # FanDuel jumps away from five books that haven't moved: it probably saw the news first.
        self.assertEqual(find_evs([keyed_prop(LOCK_SIX)], cfg, NOW, prices=h), [])
        self.assertEqual(h.held, {"moved first": 1})
        # 31 minutes on and nobody followed: it's FanDuel that's off after all.
        self.assertEqual([b.book for b in find_evs([keyed_prop(LOCK_SIX)], cfg, at(1860), prices=h)], ["FanDuel"])

    def test_a_jump_after_a_first_look_already_off_the_market_is_held_too(self):
        # First seen at +5% (over half the 8% bar, so not "in line"), then +10% while the others sit still.
        self.assertEqual(self.looks([dict(LOCK_SIX, FanDuel=(2.10, 1.75)), LOCK_SIX]), [])
        # The same +5% price unchanged on the next look still alerts.
        five_pct = self.locks(alert_mode="balanced", prop_min_ev_pct=4, **AGES)
        self.assertEqual([b.book for b in self.looks([dict(LOCK_SIX, FanDuel=(2.10, 1.75))] * 2, five_pct)],
                         ["FanDuel"])

    def test_a_book_that_moves_again_is_held_again(self):
        # 1.91 (in line), 2.10 (+5%: a move, under the bar), then 2.20 (+10%) while the others sit still.
        steps = [dict(LOCK_SIX, FanDuel=(1.91, 1.91)), dict(LOCK_SIX, FanDuel=(2.10, 1.75)), LOCK_SIX]
        far = "2026-10-04T03:00:00Z"                                               # 15 hours out: checks 4 hours apart
        for every, kickoff in ((31, None), (240, far)):
            h = _arbbot.PriceHistory()
            self.assertEqual(self.looks(steps, h=h, every=every, kickoff=kickoff), [], every)
            self.assertEqual(h.held, {"moved first": 1}, every)                   # the jump since the last look
            # Nobody followed that move either: it's FanDuel that's off after all.
            self.assertEqual([b.book for b in self.looks(steps + [LOCK_SIX], every=every, kickoff=kickoff)], ["FanDuel"])

    def test_a_jump_is_measured_against_the_prop_bar_not_the_outlier_bar(self):
        # +3% -> +9%: a 6-point jump. Under half the outlier bar (7.5), over half the prop bar (4).
        self.assertEqual(self.looks([dict(LOCK_SIX, FanDuel=(2.06, 1.79)), dict(LOCK_SIX, FanDuel=(2.18, 1.70))]), [])

    def test_a_book_that_sits_still_while_the_others_move_alerts(self):
        everyone_high = {t: (2.20, 1.68) for t in LOCK_SIX}
        [b] = self.looks([everyone_high, LOCK_SIX])                                # the others moved; FanDuel didn't
        self.assertEqual(b.book, "FanDuel")

    def test_a_line_never_seen_before_waits_one_check(self):
        h = _arbbot.PriceHistory()
        self.assertEqual(find_evs([keyed_prop(LOCK_SIX)], self.locks(), at(-1800), prices=h), [])
        self.assertEqual(h.held, {"first look": 1})                                # one price that would have alerted
        self.assertEqual([b.book for b in find_evs([keyed_prop(LOCK_SIX)], self.locks(), NOW, prices=h)],
                         ["FanDuel"])

    # ---- an open lock has room to breathe
    def test_an_open_lock_stays_up_while_its_at_least_medium(self):
        sent, h = [], _arbbot.PriceHistory()
        cfg = self.locks(**AGES)
        a = self.alerter(sent, cfg)

        def look(books, i):
            bets = find_evs([keyed_prop(books)], cfg, at(1860 * i), prices=h, keep=set(a.open) | set(a.restored))
            a.handle(bets, checked_events={"p1"}, now=NOW.timestamp() + 1860 * i)
            return bets
        look(LOCK_SIX, 0)                                                          # first look: waits
        [b] = look(LOCK_SIX, 1)
        self.assertEqual([m for m, *_ in sent], ["POST"])
        five = {k: v for k, v in LOCK_SIX.items() if k != "BetRivers"}
        apart = dict(LOCK_SIX, BetMGM=(1.944, 1.869), **{"ESPN BET": (1.944, 1.869)}, Caesars=(1.869, 1.944),
                     BetRivers=(1.869, 1.944))
        better = dict(LOCK_SIX, FanDuel=(2.252, 1.66))                            # +12.6%: past 12%, and 2.6 better
        self.assertEqual(find_evs([keyed_prop(five)], cfg, at(1860 * 2)), [])    # (as a new alert: not a lock)
        for i, books in enumerate((five, LOCK_SIX, apart, LOCK_SIX, better), start=2):
            del sent[:]
            [b] = look(books, i)
            self.assertEqual(b.key, next(iter(a.open)))
            self.assertFalse([t for m, t, _ in sent if m == "POST" or t.startswith("❌")], (i, sent))
            self.assertEqual(b.kept, b.confidence == "medium")
        self.assertEqual((b.confidence, b.kept), ("medium", True))                 # 🟡, no "better price" ping
        self.assertIn("Confidence: **🟡 Medium**", ev_payload(b)["embeds"][0]["description"])
        self.assertEqual(len(_read(cfg.ev_log_file)), 1)                           # one bet, logged once
        # Under medium it does go: 5 sportsbooks that disagree.
        del sent[:]
        split = {"FanDuel": (2.20, 1.68), "DraftKings": (1.91, 1.91), "BetMGM": (2.10, 1.75), "Caesars": (1.75, 2.10),
                 "ESPN BET": (1.91, 1.91)}
        self.assertEqual(look(split, 7), [])
        self.assertEqual([t[:6] for _, t, _ in sent], ["❌ GONE"])

    def test_a_pinnacle_priced_card_gets_no_room_to_breathe(self):
        ev = keyed_prop({"Pinnacle": (1.91, 1.91), "FanDuel": (2.25, 1.66), "DraftKings": (1.91, 1.91),
                         "BetMGM": (1.93, 1.89), "Caesars": (1.90, 1.92)}, start="2026-10-04T08:00:00Z")
        cfg = self.locks(far_max_age_seconds=10**6)                                # 20 hours out, +12.5%: medium
        [b] = find_evs([ev], replace(cfg, min_confidence="low"), NOW)
        self.assertEqual((b.sharp_book, b.confidence), ("Pinnacle", "medium"))
        self.assertEqual(find_evs([ev], cfg, NOW, keep={b.key}), [])              # the room is for the consensus only

    def test_a_restored_lock_is_picked_back_up_after_a_restart(self):
        sent, h = [], _arbbot.PriceHistory()
        cfg = self.locks(**AGES)
        a = self.alerter(sent, cfg)
        for i in range(2):
            a.handle(find_evs([keyed_prop(LOCK_SIX)], cfg, at(1860 * i), prices=h), checked_events={"p1"},
                     now=NOW.timestamp() + 1860 * i)
        self.assertEqual([m for m, *_ in sent], ["POST"])
        del sent[:]
        from unittest import mock
        with mock.patch("arbbot.time.time", return_value=NOW.timestamp() + 1860 * 2):   # (saved state is kept a day)
            again = self.alerter(sent, cfg)                                        # restart: a fresh price history
        self.assertEqual(len(again.restored), 1)
        bets = find_evs([keyed_prop(LOCK_SIX)], cfg, at(1860 * 2), prices=_arbbot.PriceHistory(),
                        keep=set(again.open) | set(again.restored))
        self.assertEqual(again.handle(bets, checked_events={"p1"}, now=NOW.timestamp() + 1860 * 2), 0)
        self.assertEqual(sent, [])                                                 # no GONE, no new post
        self.assertEqual(list(again.open), [b.key for b in bets])

    # ---- Pinnacle has an opinion after all
    def test_pinnacle_pricing_the_player_at_another_point_caps_it_at_medium(self):
        ev = pinnacle_at(keyed_prop(LOCK_SIX), 24.5, 1.91, 1.91)                  # Pinnacle: 24.5, a coin flip
        self.assertEqual(find_evs([ev], self.locks(), NOW), [])                   # not a lock
        h = _arbbot.PriceHistory()
        [b] = self.looks([None, None], self.locks(alert_mode="balanced", **AGES), h, events=[ev])
        self.assertEqual(b.confidence, "medium")
        self.assertIn("Pinnacle has a different line (24.5)", b.confidence_notes)
        self.assertEqual(h.missed, [["Pinnacle has another line"]])               # for the console
        # Pinnacle says Over 24.5 is only 45%, so Over 25.5 is at most that: no edge at +120.
        low = pinnacle_at(keyed_prop(LOCK_SIX), 24.5, 2.10, 1.75)
        self.assertEqual(find_evs([low], self.locks(alert_mode="balanced"), NOW), [])
        h, cfg = _arbbot.PriceHistory(), self.locks(alert_mode="balanced", **AGES)
        for secs in (-1860, 0):                                                    # (the first look waits)
            find_evs([low], cfg, at(secs), prices=h)
        self.assertEqual(h.missed, [["Pinnacle's other line says no edge"]])

    def test_only_the_same_player_and_stat_cast_doubt(self):
        for player, market in (("Anthony Davis", "player_points"), ("LeBron James", "player_rebounds")):
            ev = pinnacle_lines(keyed_prop(LOCK_SIX), [(22.5, 1.80, 2.04)], player, market)
            [b] = find_evs([ev], self.locks(), NOW)
            self.assertEqual(b.confidence, "high", (player, market))               # still a lock

    def test_an_under_is_capped_by_pinnacles_under_at_a_higher_point(self):
        under = {t: (u, o) for t, (o, u) in LOCK_SIX.items()}                      # FanDuel's Under is +120
        low = replace(self.locks(), min_confidence="low")
        # Pinnacle's Under 24.5 caps nothing: Under 25.5 is likelier than that.
        [b] = find_evs([pinnacle_at(keyed_prop(under), 24.5, 1.75, 2.10)], low, NOW)
        self.assertEqual((b.outcome, b.confidence), ("Under", "medium"))
        # Pinnacle's Under 26.5 at 45%: Under 25.5 is at most that, so +120 has no edge.
        self.assertEqual(find_evs([pinnacle_at(keyed_prop(under), 26.5, 1.75, 2.10)], low, NOW), [])

    def test_the_tightest_of_pinnacles_lines_caps_it(self):
        # Over 23.5 at 58% would leave room at +120; Over 24.5 at 45% doesn't.
        ev = pinnacle_lines(keyed_prop(LOCK_SIX), [(23.5, 1.70, 2.20), (24.5, 2.10, 1.75)])
        self.assertEqual(find_evs([ev], replace(self.locks(), min_confidence="low"), NOW), [])

    def test_a_line_pinnacle_took_down_keeps_its_last_word_until_kickoff(self):
        # Prop checks are 4 hours apart from 12 hours out, then 30 minutes: Pinnacle's last price on
        # this exact line is kept until kickoff, however long ago it took the line down.
        cfg = self.locks(**AGES)
        h, far = _arbbot.PriceHistory(), "2026-10-04T03:00:00Z"                     # 15 hours out
        no_edge = dict(LOCK_SIX, Pinnacle=(2.05, 1.80))                            # Over 47%: +120 is only +3%
        got = [find_evs([keyed_prop(books, start=far)], replace(cfg, min_confidence="low"), at(240 * 60 * i), prices=h)
               for i, books in enumerate((LOCK_SIX, no_edge, LOCK_SIX, LOCK_SIX))]   # 15, 11, 7, 3 hours out
        self.assertEqual(got, [[]] * 4)                                            # taken down: its last price stands
        self.assertEqual(h.missed, [["Pinnacle's last price says no edge"]] * 2)
        # 31 minutes apart: first seen with Pinnacle on it (a lock), then taken down, then back.
        h, with_pin = _arbbot.PriceHistory(), dict(LOCK_SIX, Pinnacle=(1.91, 1.91))
        got = [find_evs([keyed_prop(books)], cfg, at(60 * (180 + 31 * i)), prices=h)
               for i, books in enumerate((with_pin, LOCK_SIX, LOCK_SIX, LOCK_SIX, LOCK_SIX, with_pin))]
        self.assertEqual([[b.sharp_book for b in bets] for bets in got], [["Pinnacle"], [], [], [], [], ["Pinnacle"]])
        self.assertEqual(h.held, {"first look": 1})                                # then never a lock while it's down
        self.assertEqual(h.missed, [["Pinnacle took this line down"]] * 3)
        # An alert that's up stays up, as a quiet 🟡 card.
        h = _arbbot.PriceHistory()
        [b] = find_evs([keyed_prop(with_pin)], cfg, at(0), prices=h)
        [b] = find_evs([keyed_prop(LOCK_SIX)], cfg, at(4 * 3600), prices=h, keep={b.key})
        self.assertEqual((b.confidence, b.kept, b.confidence_notes[-2]), ("medium", True, "Pinnacle took this line down"))
        h.prune(at(6 * 3600 - 60))
        self.assertEqual(len(h.sharp), 2)                                          # kept until kickoff (both sides)...
        h.prune(at(6 * 3600))
        self.assertEqual(h.sharp, {})                                              # ...not after

    # ---- the same yardstick everywhere: closing lines and markouts
    def test_prop_closing_lines_leave_the_alerted_book_out(self):
        tr = ClosingTracker(self.cfg())
        row = {"event_id": "p1", "market": "player_points", "outcome": "Over", "point": "25.5",
               "player": "LeBron James", "home_team": "Lakers", "book": "FanDuel",
               "first_seen": "2026-10-03T11:00:00+00:00", "commence_time": "2026-10-03T18:00:00Z"}
        tr.add(row)
        close = {"FanDuel": (2.40, 1.57), "DraftKings": (1.95, 1.87), "BetMGM": (1.93, 1.89),
                 "Caesars": (1.89, 1.93), "ESPN BET": (1.87, 1.95)}                # FanDuel never moved back
        tr.observe([keyed_prop(close)], NOW)
        [(p, _)] = tr.latest.values()
        xs = sorted(devig(list(close[t]), "power")[0] for t in ("DraftKings", "BetMGM", "Caesars", "ESPN BET"))
        self.assertAlmostEqual(p, (xs[1] + xs[2]) / 2, places=6)
        # 3 other books: too few for a +EV prop's close, enough for an outlier's (OUTLIER_MIN_BOOKS=3).
        three = keyed_prop({k: close[k] for k in ("FanDuel", "DraftKings", "BetMGM", "Caesars")})
        for kind, closes in (("ev", False), ("outlier", True)):
            tr = ClosingTracker(self.cfg())
            tr.add(row, kind)
            tr.observe([three], NOW)
            self.assertEqual(bool(tr.latest), closes, kind)
        # The alerted book isn't in the feed any more: its sister book is still left out.
        tr = ClosingTracker(self.cfg())
        tr.add(dict(row, book="BetOnline.ag"))
        sisters = keyed_prop({"LowVig.ag": (2.40, 1.57), **{k: close[k] for k in close if k != "FanDuel"}})
        tr.observe([sisters], NOW)
        self.assertAlmostEqual(tr.latest[_arbbot._bet_id(row)][0], (xs[1] + xs[2]) / 2, places=6)
        t = _arbbot.Trackers(self.cfg(), self.args())                              # outlier logs say so
        t.prop_outs.on_log(row)
        self.assertEqual(t.closing.tracked["p1|player_points|LeBron James|Over|25.5"]["kind"], "outlier")

    def test_ev_and_outlier_closes_count_sister_books_differently(self):
        row = {"event_id": "p1", "market": "player_points", "outcome": "Over", "point": "25.5",
               "player": "LeBron James", "home_team": "Lakers", "book": "FanDuel",
               "first_seen": "2026-10-03T11:00:00+00:00", "commence_time": "2026-10-03T18:00:00Z"}
        close = {"FanDuel": (2.40, 1.57), "DraftKings": (1.95, 1.87), "BetMGM": (1.93, 1.89),
                 "Caesars": (1.89, 1.93), "BetOnline.ag": (1.80, 2.04), "LowVig.ag": (1.80, 2.04)}
        p = {t: devig(list(close[t]), "power")[0] for t in close}
        got = {}
        for kind in ("ev", "outlier"):
            tr = ClosingTracker(self.cfg())
            tr.add(row, kind)
            tr.observe([keyed_prop(close)], NOW)
            [(got[kind], _)] = tr.latest.values()
        ev_xs = sorted([p["DraftKings"], p["BetMGM"], p["Caesars"], p["BetOnline.ag"]])   # +EV: sisters are one vote
        self.assertAlmostEqual(got["ev"], (ev_xs[1] + ev_xs[2]) / 2, places=6)
        out_xs = sorted(p[t] for t in close if t != "FanDuel")                            # outlier: 5 books
        self.assertAlmostEqual(got["outlier"], out_xs[2], places=6)

    def test_markouts_judge_a_consensus_prop_by_the_cards_fair_price(self):
        books = dict(LOCK_SIX, **{"BetOnline.ag": (1.80, 2.04), "LowVig.ag": (1.80, 2.04)})
        ev = keyed_prop(books)
        [b] = find_evs([ev], self.locks(alert_mode="balanced"), NOW)
        tr = _arbbot.MarkoutTracker(self.cfg())
        tr.add(b, NOW.timestamp(), "ev")
        [m] = tr.pending
        self.assertEqual(m.ref, "consensus")
        self.assertAlmostEqual(_arbbot._markout_fair(m, ev, self.cfg(), NOW, False, {}), b.fair_prob, places=12)
        ev["bookmakers"] = [bm for bm in ev["bookmakers"] if bm["title"] in ("FanDuel", "DraftKings", "BetMGM",
                                                                             "BetOnline.ag", "LowVig.ag")]
        self.assertIsNone(_arbbot._markout_fair(m, ev, self.cfg(), NOW, False, {}))   # 3 other votes: can't say

    def test_the_bet_log_says_where_the_fair_price_came_from(self):
        a = EVAlerter(self.cfg(), dry_run=True)
        [cons] = find_evs([keyed_prop(LOCK_SIX)], self.locks(), NOW)
        sharp = find_evs([ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})],
                         Config(min_ev_pct=3), NOW)
        a.handle([cons] + sharp, now=1000)
        self.assertEqual(sorted(r["fair_from"] for r in _read(self.files["ev_log_file"])), ["Pinnacle", CONSENSUS_SIX])
        group = _arbbot.CLV_GROUPS["Fair price from"]
        self.assertEqual([group(r) for r in ({"kind": "ev", "fair_from": "Pinnacle"},
                                             {"kind": "ev", "fair_from": CONSENSUS_SIX},
                                             {"kind": "outlier", "fair_from": "median of 4 other books"},
                                             {"kind": "ev"})],
                         ["Pinnacle", "Other books", "Outlier median", "Unknown"])

    # ---- the prop check (what run() calls)
    def trackers(self, **kw):
        return _arbbot.Trackers(self.cfg(Config(my_books=BROTHERS_BOOKS, round_stakes=0, **AGES), **kw).with_mode(),
                                self.args())

    def test_the_prop_check_holds_first_looks_and_says_so(self):
        t = self.trackers()
        first = _arbbot.scan_props(t, [keyed_prop(LOCK_SIX)], {"p1"}, at(0))
        self.assertEqual((first.n_ev, first.prices_held), (0, {"first look": 1}))
        self.assertEqual(_arbbot.prop_guard_text(first.prices_held, first.missed),
                         " | 1 prop price held (1 first look / 0 moved first)")
        self.assertEqual(_arbbot.scan_props(t, [keyed_prop(LOCK_SIX)], {"p1"}, at(1860)).n_ev, 1)
        self.assertTrue(t.prop_prices.seen)
        _arbbot.scan_props(t, [], set(), at(27 * 3600))
        self.assertEqual(t.prop_prices.seen, {})                                   # kept a day, like outliers'
        t = self.trackers()
        five = {k: v for k, v in LOCK_SIX.items() if k != "BetRivers"}
        p2 = keyed_prop(five)
        p2["id"] = "p2"
        _arbbot.scan_props(t, [p2], {"p2"}, at(1900))
        near = _arbbot.scan_props(t, [p2], {"p2"}, at(3800))
        self.assertEqual((near.n_ev, near.missed, near.prices_held), (0, [["under 6 sportsbooks"]], {}))   # this check
        two = near.missed + [["books disagree", "under 6 sportsbooks"]]
        self.assertEqual(_arbbot.prop_guard_text(near.prices_held, two),
                         " | 2 no-Pinnacle props missed high (2 under 6 sportsbooks, 1 books disagree)")

    def test_the_prop_check_keeps_cards_that_are_up_and_respects_the_cap(self):
        t = self.trackers(max_prop_per_hour=1)
        p2 = keyed_prop(LOCK_SIX, player="Anthony Davis")
        p2["id"] = "p2"
        for i in range(2):
            res = _arbbot.scan_props(t, [keyed_prop(LOCK_SIX), p2], {"p1", "p2"}, at(1860 * i))
        self.assertEqual((res.n_ev, res.held), (1, {"capped": 1}))                 # 6 an hour in locks; 1 here
        [key] = t.prop_evs.open
        five = {k: v for k, v in LOCK_SIX.items() if k != "BetRivers"}
        drop = keyed_prop(five, player="Anthony Davis" if "Anthony" in key else "LeBron James")
        drop["id"] = "p2" if "Anthony" in key else "p1"
        res = _arbbot.scan_props(t, [drop], {drop["id"]}, at(1860 * 2))
        self.assertEqual((list(t.prop_evs.open), res.n_ev, res.held), ([key], 0, {}))   # kept at medium, no slot
        op = t.prop_evs.open.pop(key)                                             # as after a restart:
        t.prop_evs.restored = {key: {"message_id": "m1", "first_seen": op.first_seen, "label": "LeBron",
                                     "sport_key": "basketball_nba", "event_id": drop["id"],
                                     "commence_time": drop["commence_time"]}}    # its card saved,
        t.prop_prices = _arbbot.PriceHistory()                                    # its price history gone
        _arbbot.scan_props(t, [drop], {drop["id"]}, at(1860 * 3))
        self.assertEqual(list(t.prop_evs.open), [key])                             # picked back up, not dropped

    def test_an_open_lock_whose_book_moves_further_stays_up_quietly(self):
        sent = []
        t = self.wired(sent)

        def check(i, books):
            return _arbbot.scan_props(t, [keyed_prop(books)], {"p1"}, at(1860 * i))
        check(0, LOCK_SIX)                                                         # first look
        check(1, LOCK_SIX)
        self.assertEqual([(m, c) for m, _, c in sent], [("POST", "@here")])        # the lock
        further = dict(LOCK_SIX, FanDuel=(2.29, 1.62))                             # +14.5%: FanDuel moved, alone
        res = check(2, further)
        [op] = t.prop_evs.open.values()
        self.assertEqual((op.arb.confidence, op.arb.kept), ("medium", True))       # 🟡 while the others can follow
        self.assertIn("FanDuel just moved its price away from the other books", op.arb.confidence_notes)
        self.assertEqual(res.missed, [["edge over 12%", "book moved first"]])
        for i, books in ((3, further), (4, LOCK_SIX)):                             # nobody followed; then back
            check(i, books)
        self.assertEqual([m for m, *_ in sent], ["POST"] + ["PATCH"] * 3)          # edited: no GONE, no new ping
        self.assertFalse([title for _, title, _ in sent if title.startswith("❌")])
        self.assertEqual((op.arb.confidence, op.arb.kept), ("high", False))
        self.assertEqual(len(_read(self.files["ev_log_file"])), 1)                 # one bet, logged once
        # With your own bars (7%) medium alerts too. A card whose book just moved, +7.5% to +11.5%, is
        # still never a lock then, and no new alert (no "better price" ping).
        steps = [dict(LOCK_SIX, FanDuel=(2.15, 1.75)), dict(LOCK_SIX, FanDuel=(2.23, 1.70))]
        [b] = self.looks(steps, self.locks(alert_mode="balanced", **AGES), keep={op.arb.key})
        self.assertEqual((round(b.ev_pct, 1), b.confidence, b.kept), (11.5, "medium", True))

    def test_a_bet_kept_up_under_high_isnt_a_leg_of_a_new_parlay(self):
        cfg = self.locks()
        [lebron] = find_evs([keyed_prop(LOCK_SIX)], cfg, NOW)
        game2 = keyed_prop(LOCK_SIX, player="Jayson Tatum")
        game2["id"] = "p2"
        [tatum] = find_evs([game2], cfg, NOW)
        [p] = find_parlays([lebron, tatum], cfg)                                    # two locks at FanDuel: a parlay
        five = {k: v for k, v in LOCK_SIX.items() if k != "BetRivers"}
        [kept] = find_evs([keyed_prop(five)], cfg, NOW, keep={lebron.key})        # its card is up: 🟡, kept
        self.assertEqual((kept.confidence, kept.kept), ("medium", True))
        self.assertEqual(find_parlays([kept, tatum], cfg), [])                     # not in a new parlay...
        self.assertEqual([q.key for q in find_parlays([kept, tatum], cfg, keep={p.key})], [p.key])   # ...one up stays
        self.assertEqual(find_parlays([replace(lebron, confidence="medium"), tatum], cfg), [])   # under MIN_CONFIDENCE
        medium_ok = replace(cfg, min_confidence="medium")
        self.assertEqual(len(find_parlays([replace(lebron, confidence="medium"), tatum], medium_ok)), 1)
        self.assertEqual(find_parlays([replace(lebron, confidence="medium", kept=True), tatum], medium_ok), [])
        self.assertEqual(len(find_parlays([replace(lebron, confidence=""), tatum], cfg)), 1)    # (outliers aren't rated)

    def test_run_once_says_a_new_prop_line_waits(self):
        import argparse, contextlib, io
        from unittest import mock
        pre_start = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

        class Api:
            remaining, used = 50000.0, None

            def __init__(self, cfg):
                pass

            def events(self, sport, horizon_hours=26):
                return [{"id": "p1", "commence_time": pre_start}]

            def odds(self, sport, until):
                return []

            def event_odds(self, sport, gid, markets):   # a lock-quality prop Pinnacle doesn't price
                secs = (datetime.now(timezone.utc) - NOW).total_seconds()
                return stamped(keyed_prop(LOCK_SIX, start=pre_start), secs)

        cfg = self.cfg(Config(my_books=BROTHERS_BOOKS, round_stakes=0).with_mode(), sports=["basketball_nba"],
                       prop_sports=["basketball_nba"], kalshi_check=False)
        args = argparse.Namespace(once=True, demo=False, dry_run=True, plan=False, check_kalshi=False,
                                  results=None, post_results=None)
        with mock.patch("arbbot.OddsAPI", Api), contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            _arbbot.run(cfg, args, _arbbot.Status(cfg, dry_run=True))
        self.assertIn("0 new +EV, 0 new outliers | 1 prop price held (1 first look / 0 moved first) | credits left",
                      out.getvalue())                                             # one look can't send it

    def test_an_outlier_that_shrinks_to_a_medium_edge_becomes_a_quiet_ev_card(self):
        sent = []
        t = self.wired(sent)
        outlier = keyed_prop(dict(LOCK_SIX, FanDuel=(2.32, 1.61)))                 # 16%
        self.assertEqual(_arbbot.scan_props(t, [outlier], {"p1"}, at(0)).n_out, 1)
        del sent[:]
        res = _arbbot.scan_props(t, [keyed_prop(dict(LOCK_SIX, FanDuel=(2.26, 1.66)))], {"p1"}, at(1860))   # 13%
        self.assertEqual((res.n_out, list(t.prop_outs.open), len(t.prop_evs.open)), (0, [], 1))
        [(m1, t1, _), (m2, t2, c2)] = sent
        self.assertEqual((m1, m2), ("PATCH", "POST"))
        self.assertTrue(t1.startswith("↘️ Back to a normal +EV edge"))
        self.assertTrue(t2.startswith("📈 +EV 13.0%"))
        self.assertNotIn("@here", c2)                                              # medium in locks: no ping


# --------------------------------------------------------------------------- credits: real costs, live checks
# on moneylines, spare credits to pre-game, free ESPN finals (shadow), the "upcoming" probe

def three_markets(gid, start, sport="basketball_nba", home=(2.0, 1.85, 1.9), away=(1.85, 2.0, 1.9)):
    """A game with moneylines, spreads and totals at books A and B (no arb unless the prices make one)."""
    iso = start if isinstance(start, str) else start.strftime("%Y-%m-%dT%H:%M:%SZ")
    ev = event({"A": [("h2h", [("Home", home[0], None), ("Away", away[0], None)]),
                      ("spreads", [("Home", home[1], -3.5), ("Away", away[1], 3.5)]),
                      ("totals", [("Over", home[2], 220.5), ("Under", away[2], 220.5)])],
                "B": [("h2h", [("Home", away[0], None), ("Away", home[0], None)]),
                      ("spreads", [("Home", away[1], -3.5), ("Away", home[1], 3.5)]),
                      ("totals", [("Over", away[2], 220.5), ("Under", home[2], 220.5)])]}, start=iso)
    ev.update(id=gid, sport_key=sport)
    return ev


class CallCosts(unittest.TestCase):
    """The budget runs on what calls really cost (x-requests-last), not markets x regions."""

    def test_costbook_moves_from_the_formula_to_measured_costs(self):
        b = _arbbot.CostBook()
        k = "props:basketball_nba:12h"
        self.assertEqual(b.estimate(k, 4), 4)                      # no calls yet: the formula
        for _ in range(3):
            b.add(k, 0, 4)                                         # empty answers are free
        self.assertAlmostEqual(b.estimate(k, 4), 2.0)              # 3 calls weigh as much as the formula
        for _ in range(40):
            b.add(k, 1, 4)
        self.assertLess(b.estimate(k, 4), 1.3)                     # the formula fades as calls come in
        for bad in (None, -1, 500, float("nan")):
            b.add("odds:live", bad, 1)                             # no header or nonsense: ignored
        self.assertEqual(b.calls("odds:live", 1), 0)
        self.assertEqual(b.estimate(k, 3), 3)                      # new PROP_MARKETS: starts again
        self.assertEqual(b.take_today(), {k: 40.0})
        self.assertEqual(b.today, {})

    def test_costs_survive_a_restart_and_show_in_the_plan(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state" / "call_costs.json"
            b = _arbbot.CostBook(path)
            for _ in range(3):
                b.add("odds:live", 1, 1)
            b.save()
            again = _arbbot.CostBook(path)
            self.assertEqual((again.calls("odds:live", 1), again.estimate("odds:live", 1)), (3, 1.0))
            path.write_text("not json")
            self.assertEqual(_arbbot.CostBook(path).stats, {})     # unreadable: back to the formula
        cfg = Config(sports=["basketball_nba"], live_markets="h2h", prop_sports=["basketball_nba", "icehockey_nhl"])
        s = sched_with({"basketball_nba": []}, cfg)
        s.api.costs = again
        for _ in range(3):
            again.add("odds:pre", 1, 2)                                # an answer can cost less than the formula
            again.add("props:basketball_nba:12h", 0, 4)
        lines = _arbbot.cost_lines(cfg, s)
        self.assertIn("1.0 (measured over 3 calls; formula 1) for the live check", lines[0])
        self.assertIn("1.5 (measured over 3 calls; formula 2) for the pre-game check (spreads,totals", lines[0])
        self.assertIn("2.0 (measured over 3 calls; formula 4) up to 12h out", lines[2])
        self.assertEqual(lines[3], "NHL props per game: 3 (formula; measured once props are checked)")

    def test_each_call_records_its_own_cost(self):
        import io, json
        from unittest import mock
        from urllib.parse import parse_qs, urlparse

        class Resp:
            def __init__(self, body, cost):
                self.body, self.headers = body, {"x-requests-last": str(cost), "x-requests-remaining": "900"}
            def read(self):
                return json.dumps(self.body).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        api = _arbbot.OddsAPI(Config(api_key="k", state_dir=""))
        now = datetime.now(timezone.utc)
        iso = lambda h: (now + timedelta(hours=h)).isoformat()
        replies = [Resp([], 3), Resp([], 1), Resp([], 2), Resp({"commence_time": iso(1), "bookmakers": [1]}, 4),
                   Resp({"commence_time": iso(5), "bookmakers": [1]}, 1),
                   Resp({"commence_time": iso(20), "bookmakers": []}, 0), Resp([], 2)]
        urls = []

        def urlopen(req, timeout=None):
            urls.append(parse_qs(urlparse(req.full_url).query))
            return replies.pop(0)
        props = "player_points,player_rebounds,player_assists,player_threes"
        with mock.patch("urllib.request.urlopen", urlopen):
            api.odds("basketball_nba", now)
            api.odds("basketball_nba", now, markets="h2h", kind="odds:live")
            api.odds("basketball_nba", now, markets="spreads,totals", since=now, kind="odds:pre")
            for gid in ("g1", "g2", "g3"):
                api.event_odds("basketball_nba", gid, props)
            api.scores("basketball_nba")
        self.assertEqual(api.costs.today, {"odds": 3, "odds:live": 1, "odds:pre": 2, "props:basketball_nba:near": 4,
                                           "props:basketball_nba:6h": 1, "props:basketball_nba:24h": 0, "scores": 2})
        self.assertEqual((api.costs.calls("odds:live", 1), api.costs.calls("odds:pre", 2)), (1, 1))
        self.assertEqual([u["markets"][0] for u in urls[:3]], ["h2h,spreads,totals", "h2h", "spreads,totals"])
        self.assertEqual(["commenceTimeFrom" in u for u in urls[:3]], [False, False, True])

    def test_budget_uses_measured_prop_costs_by_hours_to_start(self):
        cfg = Config(sports=["basketball_nba"], prop_sports=["basketball_nba"], spare_use_pct=0)
        tomorrow = {"basketball_nba": [(f"g{i}", NOW + timedelta(hours=20)) for i in range(5)]}   # 4am ET Sunday
        s = sched_with(tomorrow, cfg, remaining=60_000)
        s.update_budget(NOW)
        formula = s.demand[_arbbot.PROP_EARLY]
        s.api.costs = _arbbot.CostBook()
        for bucket in ("6h", "12h", "24h"):
            for _ in range(20):
                s.api.costs.add(f"props:basketball_nba:{bucket}", 0.2, 4)   # few props posted this early
        s.update_budget(NOW)
        self.assertLess(s.demand[_arbbot.PROP_EARLY], formula * 0.3)
        # Today's games: never below the near-kickoff cost (books post the day's props by morning).
        self.assertLess(s.prop_cost("basketball_nba", 10, today=False), 1)
        self.assertEqual(s.prop_cost("basketball_nba", 10, today=True), 4)
        for _ in range(20):
            s.api.costs.add("props:basketball_nba:near", 2.0, 4)
        self.assertAlmostEqual(s.prop_cost("basketball_nba", 10, today=True), s.prop_cost("basketball_nba", 1), 6)

    def test_the_bots_daily_summary_says_where_credits_went(self):
        import argparse, contextlib, io
        from unittest import mock
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
        clock = {"started": False}

        class Clock(datetime):   # the bot starts at 8am; its loop runs at 10am, past the 9am summary
            @classmethod
            def now(cls, tz=None):
                t = datetime(2026, 10, 4, 10 if clock["started"] else 8, 0, tzinfo=ny)
                if isinstance(tz, ZoneInfo):
                    clock["started"] = True
                return t.astimezone(tz) if tz else t.replace(tzinfo=None)

        class Api:
            remaining, used = 50000.0, None
            def __init__(self, cfg):
                self.costs = _arbbot.CostBook()
                self.costs.add("odds:live", 1, 1)
                self.costs.add("props:basketball_nba:near", 4, 4)
            def events(self, sport, horizon_hours=26):
                return []
        cfg = replace(LiveMoneylinesOnly.default_cfg(), summary_hour=9)
        cards = []
        args = argparse.Namespace(once=False, demo=False, dry_run=True, plan=False, check_kalshi=False,
                                  results=None, post_results=None)
        with mock.patch("arbbot.OddsAPI", Api), mock.patch("arbbot.datetime", Clock), \
                mock.patch.object(_arbbot.Status, "send_card", lambda status, payload: cards.append(payload)), \
                mock.patch("arbbot.time.sleep", side_effect=StopLoop), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(StopLoop):
                _arbbot.run(cfg, args, _arbbot.Status(cfg, dry_run=True))
        [card] = cards
        self.assertIn("Used since the last summary: 1 on live checks, 4 on props", card["embeds"][0]["description"])

    def test_daily_summary_says_where_credits_went(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(closing_file=str(Path(d) / "c.csv"), ev_log_file=str(Path(d) / "e.csv"),
                         outlier_log_file=str(Path(d) / "o.csv"), markout_file=str(Path(d) / "m.csv"))
            card = _arbbot.summary_payload(cfg, [], 50_000, {"odds:live": 410, "odds:pre": 300.4, "odds": 50,
                                                             "props:basketball_nba:near": 120, "scores": 8})
        self.assertIn("Used since the last summary: 410 on live checks, 300 on pre-game checks, 120 on props, "
                      "8 on scores, 50 on main-line checks", card["embeds"][0]["description"])


class LiveMoneylinesOnly(unittest.TestCase):
    """LIVE_MARKETS: live checks ask for moneylines only (1 credit, every game in the sport, every
    minute); the other bet types of upcoming games get their own pre-game check."""

    def env(self, **values):
        import os
        from unittest import mock
        keys = ("ALERT_MODE", "LIVE_MARKETS", "LIVE_MAX_STRETCH", "MARKETS", "EARLY_MINUTES", "EARLY_MIN_MINUTES",
                "PREGAME_MIN_MINUTES", "SPARE_USE_PCT", "FREE_SCORES", "PREGAME_WITH_LIVE_EVERY")
        patch = mock.patch.dict(os.environ, {k: v for k, v in values.items()})
        patch.start()
        self.addCleanup(patch.stop)
        for k in keys:
            if k not in values:
                os.environ.pop(k, None)
        return Config.from_env()

    def test_locks_mode_checks_live_games_for_moneylines_only(self):
        cfg = self.env(ALERT_MODE="locks")
        self.assertEqual((cfg.live_markets, cfg.split_markets()), ("", None))   # off unless asked for
        cfg = self.env(ALERT_MODE="locks", LIVE_MARKETS="h2h")
        self.assertEqual((cfg.live_markets, cfg.live_max_stretch, cfg.split_markets()), ("h2h", 3.0, ("h2h", "spreads,totals")))
        balanced = self.env(ALERT_MODE="balanced")
        self.assertEqual((balanced.live_markets, balanced.split_markets()), ("", None))
        self.assertEqual(balanced.live_max_stretch, Config().live_max_stretch)      # one default, both ways
        self.assertIsNone(self.env(ALERT_MODE="locks", LIVE_MARKETS="totals,h2h,spreads").split_markets())  # opt out
        self.assertEqual(self.env(ALERT_MODE="locks", MARKETS="spreads,totals").live_markets, "")         # no moneylines
        self.assertEqual(self.env(ALERT_MODE="locks", LIVE_MARKETS="h2h", LIVE_MAX_STRETCH="1").live_max_stretch, 1)  # yours wins

    def test_new_settings_are_checked(self):
        for key, value in (("LIVE_MARKETS", "h2h,player_points"), ("EARLY_MIN_MINUTES", "-1"),
                           ("PREGAME_MIN_MINUTES", "20"), ("SPARE_USE_PCT", "96"), ("LIVE_MAX_STRETCH", "0.5"),
                           ("FREE_SCORES", "on"), ("PREGAME_WITH_LIVE_EVERY", "-1"),
                           ("PREGAME_WITH_LIVE_EVERY", "1.5")):
            with self.assertRaises(ValueError) as e:
                self.env(**{key: value})
            self.assertTrue(str(e.exception).startswith(key), str(e.exception))
        self.assertEqual(self.env(EARLY_MINUTES="10").early_min_minutes, 10)    # an unset floor never blocks
        self.assertEqual(self.env(EARLY_MIN_MINUTES="0").early_min_minutes, 0)
        self.assertEqual([self.env(**({"PREGAME_WITH_LIVE_EVERY": v} if v else {})).pregame_with_live_every
                          for v in ("", "3", "0")], [1, 3, 0])

    def test_lanes_and_forecast(self):
        live_game, later = ("g1", NOW - timedelta(minutes=30)), ("g2", NOW + timedelta(hours=6))
        base = Config(sports=["basketball_nba"], props_enabled=False, spare_use_pct=0)
        one = sched_with({"basketball_nba": [live_game, later]}, base, remaining=60_000)
        two = sched_with({"basketball_nba": [live_game, later]}, replace(base, live_markets="h2h"), remaining=60_000)
        self.assertEqual(one.lanes("basketball_nba", NOW), [("all", LIVE)])
        self.assertEqual(two.lanes("basketball_nba", NOW), [("live", LIVE), ("pre", PRE_LIVE)])
        self.assertEqual(two.lanes("basketball_nba", NOW + timedelta(hours=3)), [("all", EARLY)])   # nothing live
        one.update_budget(NOW)
        two.update_budget(NOW)
        # 2h10m left of g1, then 2h40m of g2: 290 live checks x 3 credits vs x 1, plus g2's spreads
        # and totals with every live check while g1 is on (130 checks x 2 credits): the same 3 credits
        # a minute while a later game is waiting, a third of it once none is.
        self.assertAlmostEqual(one.demand[LIVE], 290 * 3, delta=15)
        self.assertAlmostEqual(two.demand[LIVE], 290 * 1, delta=5)
        self.assertAlmostEqual(two.demand[PRE_LIVE], 130 * 2, delta=5)
        self.assertLess(two.forecast, one.forecast * 0.65)
        every2 = sched_with({"basketball_nba": [live_game, later]}, replace(base, live_markets="h2h",
                                                                             pregame_with_live_every=2), remaining=60_000)
        every2.update_budget(NOW)
        self.assertAlmostEqual(every2.demand[PRE_LIVE], 65 * 2, delta=5)           # every 2nd live check

    def test_later_games_spreads_come_with_every_live_check(self):
        g = {"basketball_nba": [("g1", NOW - timedelta(minutes=30)), ("g2", NOW + timedelta(hours=6)),
                                ("g3", NOW + timedelta(minutes=90))]}
        cfg = Config(sports=["basketball_nba"], props_enabled=False, live_markets="h2h")
        s = sched_with(g, cfg)
        self.assertEqual(s.lanes("basketball_nba", NOW), [("live", LIVE), ("pre", PRE_LIVE)])
        self.assertEqual((s.interval(LIVE), s.interval(PRE_LIVE)), (60, 60))       # 1 + 2 credits a minute
        s.last_live["basketball_nba"] = s.last_odds["basketball_nba"] = time.time() - 61
        self.assertEqual(s.due(NOW) and s._due_lanes["basketball_nba"], [("live", LIVE), ("pre", PRE_LIVE)])
        every3 = replace(cfg, poll_seconds=45, pregame_with_live_every=3)
        self.assertEqual(sched_with(g, every3).interval(PRE_LIVE), 135)
        # 0: the normal pre-game pace. A slower setting than that pace: the normal pace (whichever is faster).
        self.assertEqual(sched_with(g, replace(cfg, pregame_with_live_every=0)).lanes("basketball_nba", NOW)[1],
                         ("pre", PREGAME))
        self.assertEqual(sched_with(g, replace(cfg, pregame_with_live_every=20)).lanes("basketball_nba", NOW)[1],
                         ("pre", PREGAME))                                           # 20 min vs 15 near kickoff
        far = {"basketball_nba": g["basketball_nba"][:2]}
        self.assertEqual(sched_with(far, replace(cfg, pregame_with_live_every=20)).lanes("basketball_nba", NOW)[1],
                         ("pre", PRE_LIVE))                                          # 20 min vs hourly 6h out
        s = sched_with(g, replace(cfg, pregame_with_live_every=2))                 # every 2nd live check
        self.assertEqual(s.interval(PRE_LIVE), 120)
        s.last_live["basketball_nba"] = s.last_odds["basketball_nba"] = time.time() - 61
        self.assertEqual((s.due(NOW), s._due_lanes["basketball_nba"]), (["basketball_nba"], [("live", LIVE)]))
        s.last_live["basketball_nba"] = s.last_odds["basketball_nba"] = time.time() - 121
        self.assertEqual(s.due(NOW) and s._due_lanes["basketball_nba"], [("live", LIVE), ("pre", PRE_LIVE)])

    def test_on_a_tight_day_later_games_keep_their_pace_while_live_checks_slow(self):
        # A weeknight with too few credits: the 10pm game's spreads and totals, while its sport has a
        # live game, still come every minute; the live check slows down to pay for it.
        games = {"basketball_nba": [("g1", NOW - timedelta(minutes=30)), ("g2", NOW + timedelta(hours=2, minutes=30))],
                 "icehockey_nhl": [("h1", NOW + timedelta(hours=20))]}
        cfg = Config(sports=["basketball_nba", "icehockey_nhl"], props_enabled=False, live_markets="h2h",
                     live_max_stretch=3)
        s = sched_with(games, cfg, remaining=31 * 300)
        s.update_budget(NOW)
        self.assertEqual(s.lanes("basketball_nba", NOW)[1], ("pre", PRE_LIVE))
        self.assertGreater(s.live_scale, 1.2)
        self.assertGreater(s.interval(LIVE), 70)
        self.assertEqual((s.scale, s.interval(PRE_LIVE)), (1.0, 60))
        spend = (s.demand[LIVE] / s.live_scale + s.demand[PRE_LIVE] + s.demand[PREGAME] + s.demand[EARLY]
                 + s.demand[FAR])
        self.assertAlmostEqual(spend, s.allowance, delta=2)                        # the plan pays for it
        s.last_live["basketball_nba"], s.last_odds["basketball_nba"] = time.time() - 70, time.time() - 121
        s.last_odds["icehockey_nhl"] = time.time()
        self.assertEqual(s.due(NOW) and s._due_lanes["basketball_nba"], [("pre", PRE_LIVE)])   # live not yet
        [line] = _arbbot.pace_lines(cfg, s)
        self.assertIn("so pre-game checks keep their pace, spreads and totals of later games in sports with a "
                      "live game included (every 60s).", line)
        # Tighter still: live checks at their most, then everything slows, this check too.
        tighter = sched_with(games, cfg, remaining=31 * 100)
        tighter.update_budget(NOW)
        self.assertEqual(tighter.live_scale, 3)
        self.assertGreater(tighter.scale, 1.5)
        self.assertAlmostEqual(tighter.interval(PRE_LIVE), 60 * tighter.scale)
        self.assertAlmostEqual(tighter.core_demand, tighter.demand[LIVE] / 3 + tighter.demand[PRE_LIVE]
                               + tighter.demand[PREGAME])
        lines = "\n".join(_arbbot.pace_lines(cfg, tighter))
        self.assertIn("as far as LIVE_MAX_STRETCH allows. That wasn't enough:", lines)
        self.assertIn("spreads and totals of later games in sports with a live game every 3m instead of 60s.", lines)

    def test_fetch_asks_each_check_for_the_right_games_and_bet_types(self):
        calls = []
        games = [three_markets("g1", NOW - timedelta(minutes=30)), three_markets("g2", NOW + timedelta(hours=6))]

        class Api:
            remaining = None
            def odds(self, sport, until, markets=None, since=None, kind="odds"):
                calls.append((sport, kind, markets, since, until))
                if sport == "icehockey_nhl":
                    return []
                return [] if kind == "odds:live" and len(calls) > 4 else asked(games, markets, since)
        cfg = Config(sports=["basketball_nba", "icehockey_nhl"], live_markets="h2h")
        s = sched_with({"basketball_nba": [("g1", NOW - timedelta(minutes=30)), ("g2", NOW + timedelta(hours=6))],
                        "icehockey_nhl": [("h1", NOW + timedelta(hours=3))]}, cfg)
        s.api = Api()
        events, ok = s.fetch(["basketball_nba", "icehockey_nhl"], NOW)
        by = {(c[0], c[1]): c for c in calls}
        live, pre, nhl = by[("basketball_nba", "odds:live")], by[("basketball_nba", "odds:pre")], by[("icehockey_nhl", "odds")]
        self.assertEqual((live[2], live[3]), ("h2h", None))
        self.assertGreater(live[4], NOW + timedelta(hours=47))      # upcoming games' moneylines come along
        self.assertEqual((pre[2], pre[3]), ("spreads,totals", NOW))  # the rest, games not started
        self.assertEqual(nhl[2:4], (None, None))                     # no live game: one check, everything
        self.assertEqual(ok, {"basketball_nba", "icehockey_nhl"})
        self.assertEqual([e["id"] for e in events], ["g1", "g2"])   # one event per game...
        [g2] = [e for e in events if e["id"] == "g2"]
        self.assertEqual({m["key"] for m in g2["bookmakers"][0]["markets"]}, {"h2h", "spreads", "totals"})   # ...all of it
        for _ in range(3):                                           # pre-game checks alone never "lose" g1
            s._due_lanes = {"basketball_nba": [("pre", EARLY)]}
            s.fetch(["basketball_nba"], NOW)
        self.assertNotIn("g1", s.ended)
        games.pop(0)                                                 # g1 is over: no book lists it
        for _ in range(2):                                           # ...the live check notices
            s._due_lanes = {"basketball_nba": [("live", LIVE), ("pre", EARLY)]}
            s.fetch(["basketball_nba"], NOW)
        self.assertIn("g1", s.ended)

    def scope(self, s, lanes, now, answer=()):
        class Api:
            remaining = None
            def odds(self, sport, until, markets=None, since=None, kind="odds"):
                return asked(list(answer), markets, since)
        s.api = Api()
        s._due_lanes = lanes
        events, _ = s.fetch(list(lanes), now)
        return events, s.scope

    def test_each_check_closes_only_what_it_looked_at(self):
        cfg = replace(CFG, sports=["basketball_nba", "icehockey_nhl"], live_markets="h2h", live_sports=["basketball_nba"],
                      log_file="", state_dir="")
        nba = {"basketball_nba": [("g1", NOW - timedelta(minutes=30)), ("g2", NOW + timedelta(hours=6))],
               "icehockey_nhl": [("h1", NOW - timedelta(minutes=1)), ("h2", NOW + timedelta(hours=5))]}
        s = sched_with(nba, cfg)
        a = Alerter(cfg, dry_run=True)
        arb = lambda gid, start, sport="basketball_nba": find_arbs([three_markets(
            gid, start, sport, home=(2.2, 2.2, 2.2), away=(1.7, 1.7, 1.7))], cfg, NOW)
        live_ml = [x for x in arb("g1", NOW - timedelta(minutes=30)) if x.market == "h2h"]
        started_sp = [replace(x, is_live=False) for x in arb("g1", NOW - timedelta(minutes=30)) if x.market == "spreads"]
        later = arb("g2", NOW + timedelta(hours=6))
        nhl = arb("h1", NOW - timedelta(minutes=1), "icehockey_nhl") + arb("h2", NOW + timedelta(hours=5), "icehockey_nhl")
        a.handle(live_ml + started_sp + later + nhl, now=1000)
        self.assertEqual(len(a.open), 1 + 1 + 3 + 6)
        has = lambda gid, m: any(k.startswith(f"{gid}|{m}|") for k in a.open)
        # A live check (moneylines, every game) that finds nothing: the moneylines close, live and
        # upcoming; g2's spreads/totals stay (it didn't ask for those). A started game's spread is gone
        # (only moneylines are checked live). NHL wasn't checked, so all of its alerts stay.
        a.handle([], now=1060, checked_events=self.scope(s, {"basketball_nba": [("live", LIVE)]}, NOW)[1])
        self.assertEqual((has("g1", "h2h"), has("g2", "h2h"), has("g1", "spreads")), (False, False, False))
        self.assertEqual((has("g2", "spreads"), has("g2", "totals")), (True, True))
        self.assertEqual({k.split("|")[0] for k in a.open}, {"g2", "h1", "h2"})
        # The pre-game check (spreads and totals of upcoming games) closes g2's.
        a.handle([], now=1120, checked_events=self.scope(s, {"basketball_nba": [("pre", EARLY)]}, NOW)[1])
        self.assertEqual({k.split("|")[0] for k in a.open}, {"h1", "h2"})
        # NHL has no live check (LIVE_SPORTS): its own check asks for every game, started h1 too.
        self.assertEqual(s.lanes("icehockey_nhl", NOW), [("all", EARLY)])
        a.handle([], now=1180, checked_events=self.scope(s, {"icehockey_nhl": [("all", EARLY)]}, NOW)[1])
        self.assertEqual(a.open, {})

    def test_a_pass_for_another_sport_leaves_a_game_past_its_window_alone(self):
        # Extra innings: MLB game A is past GAME_MINUTES (no live check of MLB now; its next game is
        # tomorrow, so MLB still has an "all" check, which asks for started games too) and its live
        # moneyline arb is still up. A pass that only checks NHL mustn't close it or end its streak.
        cfg = replace(CFG, sports=["baseball_mlb", "icehockey_nhl"], live_markets="h2h", log_file="", state_dir="",
                      live_confirm_checks=2)
        a_start = NOW - timedelta(minutes=cfg.minutes_for("baseball_mlb") + 20)
        s = sched_with({"baseball_mlb": [("A", a_start), ("B", NOW + timedelta(hours=20))],
                        "icehockey_nhl": [("H", NOW - timedelta(minutes=30))]}, cfg)
        self.assertEqual((s.lanes("baseball_mlb", NOW), s.lanes("icehockey_nhl", NOW)[0]),
                         ([("all", EARLY)], ("live", LIVE)))
        game = three_markets("A", a_start, "baseball_mlb", home=(2.2, 2.2, 2.2), away=(1.7, 1.7, 1.7))
        [ml] = [x for x in find_arbs([game], cfg, NOW) if x.market == "h2h"]
        self.assertTrue(ml.is_live)
        alerts, rules = Alerter(cfg, dry_run=True), _arbbot.LiveConfirm(2, 0)
        alerts.handle([ml], now=1000)
        self.assertEqual(rules.filter([ml], {"baseball_mlb"}, 1000), [])            # first sighting: waiting
        _, nhl = self.scope(s, {"icehockey_nhl": [("live", LIVE)]}, NOW)
        alerts.handle([], now=1030, checked_events=nhl)
        self.assertEqual(rules.filter([], nhl, 1030), [])
        self.assertEqual((len(alerts.open), rules.dropped), (1, []))                 # NHL's pass: untouched
        self.assertEqual(len(rules.filter([ml], {"baseball_mlb"}, 1060)), 1)          # its streak carried on
        _, mlb = self.scope(s, {"baseball_mlb": [("all", EARLY)]}, NOW)
        alerts.handle([], now=1090, checked_events=mlb)
        self.assertEqual(alerts.open, {})                                            # MLB's own check: gone

    def test_a_live_game_isnt_over_while_its_spreads_and_totals_are_up(self):
        # Books pull g1's moneyline for a while (the live check only asks for moneylines), but its
        # spreads and totals are still up: the game isn't over, and its live checks go on.
        cfg = Config(sports=["basketball_nba"], live_markets="h2h", props_enabled=False)
        g1_start = NOW - timedelta(minutes=30)
        s = sched_with({"basketball_nba": [("g1", g1_start), ("g2", NOW + timedelta(hours=6))]}, cfg)
        board = {"g1": three_markets("g1", g1_start), "g2": three_markets("g2", NOW + timedelta(hours=6))}
        pulled, calls = {"h2h"}, []

        class Api:
            remaining = None
            def odds(self, sport, until, markets=None, since=None, kind="odds"):
                calls.append((kind, since))
                answer = asked(list(board.values()), markets, since)
                for ev in answer:
                    if ev["id"] == "g1":
                        for bm in ev["bookmakers"]:
                            bm["markets"] = [m for m in bm["markets"] if m["key"] not in pulled]
                        ev["bookmakers"] = [bm for bm in ev["bookmakers"] if bm["markets"]]
                return answer
        s.api = Api()

        def check(lanes=("live", "pre")):
            s._due_lanes = {"basketball_nba": [(lane, LIVE if lane == "live" else PRE_LIVE) for lane in lanes]}
            return s.fetch(["basketball_nba"], NOW)[0]
        for _ in range(5):
            events = check()
            self.assertNotIn("g1", [e["id"] for e in events if e["bookmakers"]])     # no live spreads/totals alerts
        self.assertNotIn("g1", s.ended)
        self.assertEqual(s.live_games("basketball_nba", NOW), ["g1"])
        pre_since = [since for kind, since in calls if kind == "odds:pre"]
        self.assertEqual(pre_since[0], NOW)                                          # nothing missed yet
        self.assertLess(pre_since[1], g1_start)                                      # then it looks at g1 too
        pulled.clear()                                                               # the moneyline is back
        check()
        self.assertEqual((s.pre_since("basketball_nba", NOW), s.misses), (NOW, {}))
        # Every line down for a moment, then all back: that look doesn't count later on.
        pulled.update({"h2h", "spreads", "totals"})
        check(["live"])
        check(["pre"])
        self.assertNotIn("g1", s.ended)
        pulled.clear()
        check()
        self.assertEqual(s.misses, {})
        pulled.add("h2h")
        check(["live"])
        check(["live"])                                                              # (no pre-game check between)
        self.assertNotIn("g1", s.ended)
        # The game ends (every line comes down): the next pre-game check sees it.
        pulled.update({"spreads", "totals"})
        check()
        self.assertIn("g1", s.ended)
        # A game that missed a check, then ran past its window, doesn't move the pre-game check's start.
        s.games["basketball_nba"].append(("g0", NOW - timedelta(hours=5)))
        s.misses["g0"] = 1
        self.assertEqual(s.pre_since("basketball_nba", NOW), NOW)
        # A sport with no pre-game check left: the live check alone decides, as before.
        alone = sched_with({"basketball_nba": [("g1", g1_start)]}, cfg)
        alone.api = Api()
        for _ in range(2):
            alone._due_lanes = {"basketball_nba": [("live", LIVE)]}
            alone.fetch(["basketball_nba"], NOW)
        self.assertIn("g1", alone.ended)

    def test_a_live_alert_still_confirms_when_a_pre_game_check_comes_between(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        cfg = replace(Config(round_stakes=0).with_mode(), sports=["basketball_nba"], kalshi_check=False,
                      log_file=str(d / "arbs.csv"), ev_log_file=str(d / "ev.csv"), outlier_log_file=str(d / "out.csv"),
                      closing_file=str(d / "close.csv"), markout_file="", state_dir="", live_markets="h2h")
        self.assertEqual(cfg.live_confirm_checks, 2)
        t = _arbbot.Trackers(cfg, SimpleNamespace(once=False, demo=False, dry_run=True))
        s = sched_with({"basketball_nba": [("e1", NOW - timedelta(hours=1)), ("p2", NOW + timedelta(hours=1))]}, cfg)
        live = outlier_event(1.85, 1.95, start=STARTED)
        live["sport_key"] = "basketball_nba"
        upcoming = three_markets("p2", NOW + timedelta(hours=1))

        def check(secs, lane):
            now = at(secs)
            evs, scope = self.scope(s, {"basketball_nba": [(lane, LIVE if lane == "live" else PREGAME)]}, now,
                                    [stamped(live, secs), stamped(upcoming, secs)])
            return _arbbot.scan_main(t, evs, ["basketball_nba"], now, kalshi=False, scope=scope)
        self.assertEqual(check(0, "live").held, {"waiting": 1})
        self.assertEqual(check(30, "pre").outs, [])                  # a pre-game check: it didn't look
        self.assertEqual(check(60, "live").out_sent, 1)              # found again on the next live check

    def test_related_alerts_count_what_the_check_didnt_look_at(self):
        from arbbot import note_related
        a = EVAlerter(replace(EVCFG, log_file="", ev_log_file="", state_dir=""), dry_run=True)
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.2, None)]},
                      market="spreads")
        for bm in ev["bookmakers"]:
            for o in bm["markets"][0]["outcomes"]:
                o["point"] = -3.5 if o["name"] == "Home" else 3.5
        ev["sport_key"] = "basketball_nba"
        [spread] = find_evs([ev], EVCFG, NOW)
        a.handle([spread], now=1000)
        ml = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Away", 2.2, None)]})
        ml["sport_key"] = "basketball_nba"
        [b] = find_evs([ml], EVCFG, NOW)
        live_look = _arbbot.Scope()
        live_look.add("basketball_nba", "h2h", None, NOW + timedelta(hours=48))
        note_related([([b], a, live_look)])
        self.assertEqual(b.related, [spread.pick])                   # the spread wasn't looked at: still up
        pre_look = _arbbot.Scope()
        pre_look.add("basketball_nba", "spreads,totals", NOW, NOW + timedelta(hours=48))
        note_related([([b], a, pre_look)])
        self.assertEqual(b.related, [])                              # looked for and not found: closing

    def test_closing_line_check_asks_for_the_bet_types_it_lacks(self):
        cfg = Config(sports=["basketball_nba"], live_markets="h2h", closing_minutes=5, pregame_with_live_every=2)
        s = sched_with({"basketball_nba": [("g1", NOW - timedelta(minutes=30)), ("g2", NOW + timedelta(minutes=4))]}, cfg)
        s.need_close = {"g2"}
        self.assertEqual(s.closing_lanes("basketball_nba", NOW), ["live", "pre"])
        s.market_ok["basketball_nba"]["h2h"] = s.last_live["basketball_nba"] = (NOW - timedelta(seconds=30)).timestamp()
        s.last_live["basketball_nba"] = time.time() - 61
        self.assertEqual(s.closing_lanes("basketball_nba", NOW), ["pre"])   # moneylines were just read live
        s.last_live["basketball_nba"] = s.last_odds["basketball_nba"] = time.time()   # (not due by the clock)
        s.last_odds["basketball_nba"] = time.time() - 61
        self.assertEqual(s.due(NOW), ["basketball_nba"])
        self.assertEqual(s._due_lanes["basketball_nba"], [("pre", PREGAME)])
        for m in ("spreads", "totals"):
            s.market_ok["basketball_nba"][m] = (NOW - timedelta(seconds=10)).timestamp()
        self.assertFalse(s.closing_due("basketball_nba", NOW))

    def test_markouts_read_each_bet_type_only_from_a_check_that_asked_for_it(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        cfg = Config(markout_file=str(d / "mk.csv"), state_dir="", pregame_max_age_seconds=10**9)
        tr = _arbbot.MarkoutTracker(cfg)
        game = three_markets("p2", NOW + timedelta(hours=1), home=(2.2, 2.2, 2.2), away=(1.7, 1.7, 1.7))
        for a in find_arbs([game], cfg, NOW):
            tr.add(a, NOW.timestamp(), "arb")
        self.assertEqual({m.market for m in tr.pending}, {"h2h", "spreads", "totals"})
        tr.observe(asked([stamped(game, 60)], "h2h"), at(60))                         # the live check: moneylines
        still = {(m.market, m.outcome): m.still for m in tr.pending}
        self.assertTrue(all(v is not None for (mk, _), v in still.items() if mk == "h2h"))   # a 1-minute reading
        self.assertTrue(all(v is None for (mk, _), v in still.items() if mk != "h2h"))      # not "pulled"
        tr.observe(asked([stamped(game, 300)], "spreads,totals", at(300)), at(300))  # the pre-game check
        self.assertTrue(all(m.still is not None and m.still[1] for m in tr.pending))

    @staticmethod
    def default_cfg():
        """Config.from_env() with the defaults (locks), its files in a temporary folder, and nothing
        that would reach the network (Kalshi) or wait for a time of day (summary, grading)."""
        import os, tempfile
        from unittest import mock
        d = tempfile.mkdtemp()
        files = {k: str(Path(d) / v) for k, v in (
            ("STATE_DIR", "state"), ("LOG_FILE", "arbs.csv"), ("EV_LOG_FILE", "ev.csv"), ("OUTLIER_LOG_FILE", "out.csv"),
            ("PARLAY_LOG_FILE", "par.csv"), ("EV_RESULTS_FILE", "res.csv"), ("CLOSING_FILE", "close.csv"),
            ("MARKOUT_FILE", "mk.csv"), ("SCORE_CHECK_FILE", "sc.csv"))}
        quiet = {"KALSHI_CHECK": "false", "SUMMARY_HOUR": "-1", "RESULTS_MINUTES": "0", "ODDS_API_KEY": "k"}
        with mock.patch.dict(os.environ, {**files, **quiet}):
            for k in [k for k in os.environ if k in ("ALERT_MODE", "LIVE_MARKETS", "MARKETS", "SPORTS", "POLL_SECONDS",
                                                     "LIVE_CONFIRM_CHECKS", "LIVE_MAX_AGE_ALERT")]:
                os.environ.pop(k)
            return Config.from_env()

    @staticmethod
    def default_api(calls):
        """A fake Odds API for run(): odds() takes keyword arguments and answers like the real one (only
        the bet types asked for; with `since`, only games after it), and records each call's cost."""
        real = datetime.now(timezone.utc)
        iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
        live_start, pre_start = iso(real - timedelta(minutes=30)), iso(real + timedelta(hours=1))

        def fresh(ev, gid):
            ev["id"], ev["sport_key"] = gid, "basketball_nba"
            return stamped(ev, (datetime.now(timezone.utc) - NOW).total_seconds())

        class Api:
            remaining, used = 50000.0, None
            def __init__(self, cfg):
                self.cfg = cfg
                self.costs = _arbbot.CostBook(Path(cfg.state_dir) / "call_costs.json")
            def events(self, sport, horizon_hours=26):
                if sport != "basketball_nba":
                    return []
                return [{"id": "live1", "commence_time": live_start}, {"id": "p1", "commence_time": pre_start}]
            def odds(self, sport, until, markets=None, since=None, kind="odds"):
                calls.append((sport, kind, markets, since is not None, until > real + timedelta(hours=40)))
                self.costs.add(kind, self.cfg.credits_per_call(markets), self.cfg.credits_per_call(markets))
                pre = event({"Pinnacle": [("h2h", [("Home", 1.91, None), ("Away", 1.91, None)])],
                             "B": [("h2h", [("Home", 2.20, None)])],
                             "X": [("totals", [("Over", 2.10, 220.5), ("Under", 1.75, 220.5)])],
                             "Y": [("totals", [("Over", 1.75, 220.5), ("Under", 2.10, 220.5)])]}, start=pre_start)
                pre["bookmakers"][0]["key"] = "pinnacle"
                return asked([fresh(outlier_event(1.85, 1.95, start=live_start), "live1"), fresh(pre, "p1")],
                             markets, since)
            def event_odds(self, sport, gid, markets):
                return {"id": gid, "commence_time": pre_start, "bookmakers": []}
        return Api

    def test_run_keeps_a_live_alert_confirming_through_a_pre_game_pass(self):
        import argparse, contextlib, io
        from unittest import mock
        cfg = replace(self.default_cfg(), props_enabled=False, parlays_enabled=False, live_markets="h2h")
        plan = [{"basketball_nba": [("live", LIVE), ("pre", PREGAME)]}, {"basketball_nba": [("pre", PREGAME)]},
                {"basketball_nba": [("live", LIVE)]}]

        def due(sched, now):
            sched._due_lanes = plan.pop(0)
            return list(sched._due_lanes)
        passes, real_scan = [], _arbbot.scan_main

        def scan(t, *a, **kw):
            passes.append(real_scan(t, *a, **kw))
            return passes[-1]
        args = argparse.Namespace(once=False, demo=False, dry_run=True, plan=False, check_kalshi=False,
                                  results=None, post_results=None)
        with mock.patch("arbbot.OddsAPI", self.default_api([])), mock.patch("arbbot.scan_main", scan), \
                mock.patch.object(_arbbot.Scheduler, "due", due), \
                mock.patch("arbbot.time.sleep", side_effect=[None, None, StopLoop]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(StopLoop):
                _arbbot.run(cfg, args, _arbbot.Status(cfg, dry_run=True))
        self.assertEqual([(len(p.outs), p.out_sent, p.held) for p in passes],
                         [(1, 0, {"waiting": 1}), (0, 0, {}), (1, 1, {})])   # live, pre-game only, live: it pings

    def test_run_with_the_default_settings_makes_one_check_per_sport(self):
        """Config.from_env() defaults (locks): no split, so one check per sport asks for everything,
        live and upcoming, as before; live checks aren't slowed on their own."""
        import argparse, contextlib, io
        from unittest import mock
        cfg = self.default_cfg()
        self.assertIsNone(cfg.split_markets())
        calls = []
        args = argparse.Namespace(once=False, demo=False, dry_run=True, plan=False, check_kalshi=False,
                                  results=None, post_results=None)
        with mock.patch("arbbot.OddsAPI", self.default_api(calls)), mock.patch("arbbot.time.sleep", side_effect=StopLoop), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(StopLoop):
                _arbbot.run(cfg, args, _arbbot.Status(cfg, dry_run=True))
        nba = [c for c in calls if c[0] == "basketball_nba"]
        self.assertEqual([(c[1], c[2]) for c in nba], [("odds", None)])      # one call, MARKETS (all bet types)
        self.assertNotIn("Odds error", err.getvalue())

    def test_run_with_live_moneylines_only_makes_both_checks(self):
        """One pass of run() with LIVE_MARKETS=h2h on top of the from_env() defaults (locks), against an
        API whose odds() takes keyword arguments: the live check (moneylines, every game), the pre-game
        check (spreads and totals of upcoming games), and alerts from both."""
        import argparse, contextlib, io
        from unittest import mock
        cfg = replace(self.default_cfg(), live_markets="h2h")
        real = datetime.now(timezone.utc)
        iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
        live_start, pre_start = iso(real - timedelta(minutes=30)), iso(real + timedelta(hours=1))
        calls = []
        Api = self.default_api(calls)
        args = argparse.Namespace(once=False, demo=False, dry_run=False, plan=False, check_kalshi=False,
                                  results=None, post_results=None)
        followed = []
        real_update = _arbbot.MarkoutTracker.update

        def spy(tracker, events, prop_events, now):
            followed.extend((m.kind, m.event_id, m.market) for m in tracker.pending)
            return real_update(tracker, events, prop_events, now)
        out = io.StringIO()
        with mock.patch("arbbot.OddsAPI", Api), mock.patch.object(_arbbot.MarkoutTracker, "update", spy), \
                mock.patch("arbbot.time.sleep", side_effect=StopLoop), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(StopLoop):
                _arbbot.run(cfg, args, _arbbot.Status(cfg, dry_run=True))
        self.assertEqual(sorted(c for c in calls if c[0] == "basketball_nba"),
                         [("basketball_nba", "odds:live", "h2h", False, True),
                          ("basketball_nba", "odds:pre", "spreads,totals", True, True)])
        self.assertNotIn("Odds error", err.getvalue())
        self.assertIn(("arb", "p1", "totals"), followed)                       # from the pre-game check
        self.assertIn("held back: 1 live, waiting for another check", out.getvalue())   # the live outlier (2 checks)
        self.assertIn("live games: h2h (1 credit/check), the rest before kickoff (2)", out.getvalue())
        self.assertIn("Spare credits", out.getvalue())                          # the Budget: line says what it bought
        saved = _arbbot.CostBook(Path(cfg.state_dir) / "call_costs.json")        # measured costs kept for a restart
        self.assertEqual((saved.calls("odds:live", 1), saved.calls("odds:pre", 2)), (1, 1))


class SpareCredits(unittest.TestCase):
    """Credits a day can spare buy faster pre-game checks, near kickoff first; live checks never go
    faster than POLL_SECONDS, and on a short day they slow down first (LIVE_MAX_STRETCH)."""

    GAMES = {"basketball_nba": [("g1", NOW)],
             "icehockey_nhl": [("h1", NOW + timedelta(hours=1)), ("h2", NOW + timedelta(hours=8)),
                               ("h3", NOW + timedelta(hours=30))]}

    def test_spare_credits_go_to_pregame_checks_never_live(self):
        cfg = Config(sports=list(self.GAMES), props_enabled=False)
        s = sched_with(self.GAMES, cfg, remaining=80_000)
        s.update_budget(NOW)
        self.assertEqual(s.interval(LIVE), 60)                                   # live never faster
        self.assertEqual(s.interval(PREGAME), cfg.pregame_min_minutes * 60)      # near kickoff: as often as allowed
        self.assertEqual(s.interval(EARLY), cfg.early_min_minutes * 60)
        self.assertLessEqual(s.forecast + s.spare, s.allowance * cfg.spare_use_pct / 100 + 1)
        tight = sched_with(self.GAMES, cfg, remaining=31 * 300)
        tight.update_budget(NOW)
        self.assertEqual((tight.speed, tight.spare), ({}, 0.0))                  # nothing spare: normal or slower
        off = sched_with(self.GAMES, replace(cfg, spare_use_pct=0), remaining=80_000)
        off.update_budget(NOW)
        self.assertEqual(off.speed, {})

    def test_spare_goes_near_kickoff_first_and_far_out_last_each_down_to_its_floor(self):
        s = sched_with({"basketball_nba": []}, Config(sports=["basketball_nba"]))
        tiers = (PREGAME, EARLY, _arbbot.PROP_EARLY, _arbbot.PROP_NEAR, FAR)
        for budget, want in ((150, {PREGAME: 2.5}),
                             (650, {PREGAME: 5, EARLY: 3.5}),
                             (850, {PREGAME: 5, EARLY: 4, _arbbot.PROP_EARLY: 2.5}),
                             (1050, {PREGAME: 5, EARLY: 4, _arbbot.PROP_EARLY: 4, _arbbot.PROP_NEAR: 1.5}),
                             (10**6, {PREGAME: 5, EARLY: 4, _arbbot.PROP_EARLY: 4, _arbbot.PROP_NEAR: 2, FAR: 3})):
            s.demand, s.speed, s.spare = dict.fromkeys(tiers, 100.0), {}, 0.0
            s._spend_spare(budget)
            self.assertEqual({k: round(v, 3) for k, v in s.speed.items()}, want)
        self.assertEqual(s.spare, 400 + 300 + 300 + 100 + 200)                     # every tier at its floor
        s.speed = {_arbbot.PROP_EARLY: 4.0, _arbbot.PROP_NEAR: 2.0}
        self.assertEqual((s._prop_every(False), s._prop_every(True)), (3600, 900))   # props use it too

    def test_later_games_in_a_live_sport_get_spare_credits_first_down_to_every_live_check(self):
        s = sched_with({"basketball_nba": []}, Config(sports=["basketball_nba"], live_markets="h2h",
                                                      pregame_with_live_every=2))
        for budget, room, want in ((150, 0, {PRE_LIVE: 2, PREGAME: 1.5}),     # every live check, then near kickoff
                                   (-50, 80, {PRE_LIVE: 1.8}),                 # the rest of the day's share: this only
                                   (10**6, 0, {PRE_LIVE: 2, PREGAME: 5, EARLY: 4})):
            s.demand, s.speed, s.spare = dict.fromkeys((PRE_LIVE, PREGAME, EARLY), 100.0), {}, 0.0
            s._spend_spare(budget, room)
            self.assertEqual({k: round(v, 3) for k, v in s.speed.items()}, want)
        games = {"basketball_nba": [("g1", NOW - timedelta(minutes=30)), ("g2", NOW + timedelta(hours=2, minutes=30))],
                 "icehockey_nhl": [("h1", NOW + timedelta(hours=20))]}
        cfg = Config(sports=list(games), props_enabled=False, live_markets="h2h", live_max_stretch=3,
                     pregame_with_live_every=2)
        roomy = sched_with(games, cfg, remaining=80_000)
        roomy.update_budget(NOW)
        self.assertEqual((roomy.interval(PRE_LIVE), roomy.interval(LIVE)), (60, 60))   # never faster than live
        [line] = _arbbot.pace_lines(cfg, roomy)
        self.assertIn("buy faster pre-game checks: spreads and totals of later games in sports with a live game "
                      "every 60s (from 2m), near kickoff every 3m (from 15m)", line)
        self.assertIn("for the pre-game check (spreads,totals, upcoming games, every 2m at full speed)",
                      _arbbot.cost_lines(cfg, roomy)[0])
        # A day that fits with under 15% to spare (SPARE_USE_PCT): only this check gets faster, using
        # the rest of the day's share (before LIVE_MARKETS it came with every live check).
        near_full = sched_with(games, cfg, remaining=31 * 400)
        near_full.update_budget(NOW)
        self.assertGreater(near_full.forecast, near_full.allowance * cfg.spare_use_pct / 100)
        self.assertEqual(set(near_full.speed), {PRE_LIVE})
        self.assertAlmostEqual(near_full.forecast + near_full.spare, near_full.allowance, delta=1)
        off = sched_with(games, replace(cfg, spare_use_pct=0), remaining=31 * 400)
        off.update_budget(NOW)
        self.assertEqual(off.speed, {})                                            # 0: no speed-ups at all

    def test_live_checks_slow_down_first_when_short(self):
        cfg = Config(sports=["basketball_nba", "icehockey_nhl"], props_enabled=False, early_minutes=5,
                     live_max_stretch=3, live_markets="h2h")
        games = {"basketball_nba": [("g1", NOW)], "icehockey_nhl": [("h1", NOW + timedelta(hours=20))]}
        s = sched_with(games, cfg, remaining=31 * 450)
        s.update_budget(NOW)
        self.assertGreater(s.interval(LIVE), 60)
        self.assertLessEqual(s.interval(EARLY), 5 * 60 + 1)                       # upcoming games keep their pace
        spend = s.core_demand / s.scale + s.extra_demand / s.extra_scale
        self.assertLessEqual(spend, s.allowance + 1)
        old = sched_with(games, replace(cfg, live_max_stretch=0), remaining=31 * 450)
        old.update_budget(NOW)
        self.assertEqual(old.interval(LIVE), 60)                                  # 1 (not locks): early checks pay first
        self.assertGreater(old.interval(EARLY), 5 * 60)

    def test_without_the_split_live_checks_dont_slow_down_alone(self):
        # The live check also carries the sport's upcoming games, so slowing it alone would slow
        # their pre-game checks too: without LIVE_MARKETS, LIVE_MAX_STRETCH does nothing.
        cfg = Config(sports=["basketball_nba", "icehockey_nhl"], props_enabled=False, early_minutes=5,
                     live_max_stretch=3)
        games = {"basketball_nba": [("g1", NOW)], "icehockey_nhl": [("h1", NOW + timedelta(hours=20))]}
        s = sched_with(games, cfg, remaining=31 * 900)
        s.update_budget(NOW)
        self.assertEqual(s.live_scale, 1.0)
        self.assertEqual(s.live_cap_note(NOW), "")

    def test_plan_and_log_say_what_the_budget_did(self):
        import contextlib, io
        from unittest import mock
        cfg = Config(sports=["basketball_nba", "icehockey_nhl"], props_enabled=False, early_minutes=5,
                     live_max_stretch=3, live_markets="h2h")
        games = {"basketball_nba": [("g1", NOW)], "icehockey_nhl": [("h1", NOW + timedelta(hours=20))]}
        short_day = sched_with(games, cfg, remaining=31 * 450)
        short_day.refresh_events = lambda force=False: None
        out = io.StringIO()
        with mock.patch("arbbot.datetime") as dt, contextlib.redirect_stdout(out):
            dt.now.return_value = NOW
            dt.side_effect = lambda *a, **k: datetime(*a, **k)
            _arbbot.print_plan(cfg, short_day)
        self.assertIn("→ Live checks slowed first: every", out.getvalue())
        self.assertNotIn("Fits the budget at full speed", out.getvalue())
        split = sched_with(games, replace(cfg, live_markets="h2h"), remaining=31 * 450)
        split.refresh_events = lambda force=False: None
        out = io.StringIO()
        with mock.patch("arbbot.datetime") as dt, contextlib.redirect_stdout(out):
            dt.now.return_value = NOW
            dt.side_effect = lambda *a, **k: datetime(*a, **k)
            _arbbot.print_plan(split.cfg, split)
        self.assertIn("L = live every 60s (h2h only; spreads,totals of later games every 60s)", out.getvalue())
        self.assertTrue(_arbbot.pace_changed((1.0, 1.0, 1.0, 1.0, {}), short_day))
        roomy = sched_with(SpareCredits.GAMES, Config(sports=list(SpareCredits.GAMES), props_enabled=False),
                           remaining=80_000)
        roomy.update_budget(NOW)
        [line] = _arbbot.pace_lines(roomy.cfg, roomy)
        self.assertTrue(line.startswith("→ Spare credits (~"), line)
        self.assertIn("near kickoff every 3m (from 15m), upcoming games every 15m (from 60m)", line)
        self.assertIn("Live checks never go faster than 60s.", line)
        self.assertTrue(_arbbot.pace_changed((1.0, 1.0, 1.0, 1.0, {}), roomy))
        self.assertFalse(_arbbot.pace_changed(_arbbot.pace_state(roomy), roomy))

    def test_spare_spending_shrinks_when_credits_fall_faster_than_the_calls_cost(self):
        # No x-requests-last header: the bot only has the formula. If the credits left fall faster
        # than that (costs above the formula, or another process on the key), less is spare.
        cfg = Config(sports=list(self.GAMES), props_enabled=False)

        def run(header, spent=450, remaining=45_000, s=None):
            if s is None:
                api = FakeAPI(remaining)
                api.costs = _arbbot.CostBook()
                s = Scheduler(cfg, api)
                s.games.update(self.GAMES)
                s.update_budget(NOW)
            before = s.spare
            for i in range(100):                                     # 300 credits by the calls
                s.api.costs.add("odds", 3 if header is True or (header == "half" and i % 2) else None, 3)
            s.api.remaining -= spent
            s.update_budget(NOW)
            return before, s
        before, s = run(header=False)
        self.assertAlmostEqual(s.overrun, 1.5)
        self.assertGreater(before, 200)
        self.assertEqual((s.spare, s.speed), (0.0, {}))
        self.assertIn("Credits are going 1.5× as fast as the checks should cost", _arbbot.pace_lines(cfg, s)[-1])
        _, measured = run(header=True)                               # the header says: the costs are known
        self.assertEqual((measured.overrun, measured.spare), (1.0, before))
        self.assertEqual(run(header="half", spent=330)[1].overrun, 1.0)   # measured calls count what they cost
        self.assertEqual(run(header=False, spent=320)[1].overrun, 1.0)   # within 10%: noise
        _, reset = run(header=False, spent=-1000)                    # the plan reset: start counting again
        self.assertEqual((reset.overrun, reset.spare), (1.0, before))
        self.assertAlmostEqual(run(header=False, s=reset)[1].overrun, 1.5)   # ...from there

    def test_early_props_and_far_out_give_way_before_the_pre_game_checks_that_come_first(self):
        # Short day: the live check slows first, then early checks; early props and 1-2 days out
        # slow down further before the spreads/totals of later games in a live sport (or near-kickoff
        # checks) give up any of their pace.
        games = {"basketball_nba": [("g1", NOW - timedelta(minutes=30)), ("g2", NOW + timedelta(hours=2, minutes=30))],
                 "americanfootball_nfl": [(f"n{i}", NOW + timedelta(hours=20)) for i in range(12)],
                 "icehockey_nhl": [("h1", NOW + timedelta(hours=30))]}
        cfg = Config(sports=list(games), live_markets="h2h", live_max_stretch=3, prop_sports=["americanfootball_nfl"])
        further = None
        for remaining in range(31 * 300, 31 * 900, 31 * 10):
            s = sched_with(games, cfg, remaining=remaining)
            s.update_budget(NOW)
            self.assertTrue(s.demand[_arbbot.PROP_EARLY] and s.demand[FAR] and s.demand[PRE_LIVE])
            if s.scale > 1:                                   # the pre-game checks that come first slow last
                self.assertEqual((s.live_scale, s.extra_scale, s.side_scale), (3, 4, 4), remaining)
            if s.side_scale > 1:
                self.assertEqual((s.live_scale, s.extra_scale), (3, 4), remaining)
                if s.side_scale < 4:
                    further = s
                    self.assertEqual((s.scale, s.interval(PRE_LIVE)), (1.0, 60))
            if s.forecast > s.allowance:                     # and the plan pays for it
                spend = s.core_demand / s.scale + s.extra_demand / s.extra_scale
                self.assertAlmostEqual(spend, s.allowance, delta=1)
        self.assertIsNotNone(further)
        x = further.extra_scale * further.side_scale
        self.assertAlmostEqual(further._prop_every(False), cfg.prop_early_minutes * 60 * x)
        self.assertAlmostEqual(further.interval(FAR), cfg.far_minutes * 60 * x)
        self.assertAlmostEqual(further.interval(EARLY), cfg.early_minutes * 60 * 4)
        lines = "\n".join(_arbbot.pace_lines(cfg, further))
        self.assertIn("→ Then early props every", lines)
        self.assertIn("h and games 1-2 days out every", lines)
        self.assertIn("slower in all), before near-kickoff checks and spreads and totals of later games in sports "
                      "with a live game slow down.", lines)
        self.assertNotIn("spreads and totals of later games in sports with a live game every", lines)
        self.assertTrue(_arbbot.pace_changed((1.0, 4.0, 3.0, 1.0, {}), further))
        self.assertEqual(_arbbot.pace_state(further)[3], further.side_scale)

    def test_a_live_slow_down_that_fits_exactly_isnt_reported_as_not_enough(self):
        # Worked out so live checks alone fit, the forecast can come out a hair over the allowance
        # (floating point): that mustn't read as "live at its max, that wasn't enough, upcoming slowed 4x".
        games = {"basketball_nba": [("g1", NOW - timedelta(minutes=30)), ("g2", NOW + timedelta(hours=1, minutes=30))]}
        for every in (1, 2):
            cfg = Config(sports=["basketball_nba"], props_enabled=False, live_markets="h2h", live_max_stretch=3,
                         lookahead_hours=0, pregame_with_live_every=every)
            fit = 0
            for remaining in range(2000, 12000, 37):
                s = sched_with(games, cfg, remaining=remaining)
                s.update_budget(NOW)
                if 1.01 < s.live_scale < 3:
                    fit += 1
                    self.assertEqual((s.scale, s.extra_scale), (1.0, 1.0), remaining)
            self.assertGreater(fit, 50)
        s = sched_with(games, replace(cfg, pregame_with_live_every=1), remaining=7439)
        s.update_budget(NOW)
        [line] = _arbbot.pace_lines(s.cfg, s)
        self.assertIn("Live checks slowed first", line)
        self.assertIn("so pre-game checks keep their pace", line)
        # Live checks at their max and everything slower, with nothing early in the next 24h: no
        # "upcoming-game checks slowed" (there are none).
        tight = sched_with(games, cfg, remaining=31 * 40)
        tight.update_budget(NOW)
        self.assertEqual((tight.live_scale, tight.extra_scale, tight.demand[EARLY]), (3, 1.0, 0))
        self.assertGreater(tight.scale, 1.1)
        lines = "\n".join(_arbbot.pace_lines(cfg, tight))
        self.assertIn("as far as LIVE_MAX_STRETCH allows. That wasn't enough:", lines)
        self.assertNotIn("Upcoming-game checks slowed", lines)

    def test_the_log_says_once_a_day_when_live_checks_stay_at_their_slowest(self):
        games = {"basketball_nba": [("g1", NOW - timedelta(minutes=30)), ("g2", NOW + timedelta(hours=1, minutes=30))]}
        cfg = Config(sports=["basketball_nba"], props_enabled=False, live_markets="h2h", live_max_stretch=3,
                     lookahead_hours=0, timezone="America/New_York")
        s = sched_with(games, cfg, remaining=31 * 40)
        s.update_budget(NOW)
        self.assertEqual(s.live_scale, 3)
        self.assertEqual(s.live_cap_note(NOW), "")                                   # just got there
        self.assertEqual(s.live_cap_note(NOW + timedelta(minutes=20)), "")
        note = s.live_cap_note(NOW + timedelta(minutes=31))
        self.assertIn("Live checks have been at their slowest since 8:00 AM: every", note)
        self.assertIn("instead of 60s (LIVE_MAX_STRETCH=3)", note)
        self.assertEqual(s.live_cap_note(NOW + timedelta(minutes=45)), "")           # once a day
        s.live_scale = 2.0                                                           # eased: starts over
        self.assertEqual(s.live_cap_note(NOW + timedelta(minutes=50)), "")
        s.live_scale = 3.0
        tomorrow = NOW + timedelta(days=1)
        s.games["basketball_nba"] = [("g3", tomorrow - timedelta(minutes=30))]
        self.assertEqual(s.live_cap_note(tomorrow), "")
        self.assertIn("since 8:00 AM", s.live_cap_note(tomorrow + timedelta(minutes=30)))   # the next day: again
        self.assertEqual(s.live_cap_note(tomorrow + timedelta(hours=3)), "")         # no game on: nothing to say
        self.assertIsNone(s.capped_since)

    def test_run_prints_the_slowest_live_checks_note(self):
        import argparse, contextlib, io
        from unittest import mock
        cfg = replace(LiveMoneylinesOnly.default_cfg(), props_enabled=False, parlays_enabled=False)
        args = argparse.Namespace(once=False, demo=False, dry_run=True, plan=False, check_kalshi=False,
                                  results=None, post_results=None)
        out = io.StringIO()
        with mock.patch("arbbot.OddsAPI", LiveMoneylinesOnly.default_api([])), \
                mock.patch.object(_arbbot.Scheduler, "live_cap_note", return_value="NOTE: live checks at their slowest"), \
                mock.patch("arbbot.time.sleep", side_effect=StopLoop), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(StopLoop):
                _arbbot.run(cfg, args, _arbbot.Status(cfg, dry_run=True))
        self.assertIn("NOTE: live checks at their slowest", out.getvalue())


def espn_board(games):
    """A fake ESPN scoreboard: games = [dict(id, date, home=(name, score, periods, winner),
    away=(...), status, completed, state)]; winner None leaves the flag out."""
    def get(path):
        day = path.split("dates=")[1][:8]
        evs = []
        for g in games:
            if g["date"][:10].replace("-", "") != day:
                continue

            def side(t, ha):
                nm, score, periods, winner = t
                loc, _, nick = nm.rpartition(" ")
                c = {"homeAway": ha, "score": str(score), "team": {"displayName": nm, "location": loc, "name": nick},
                     "linescores": [{"value": p} for p in periods]}
                if winner is not None:
                    c["winner"] = winner
                return c
            evs.append({"id": g["id"], "date": g["date"], "competitions": [{
                "status": {"type": {"name": g.get("status", "STATUS_FINAL"), "completed": g.get("completed", True),
                                    "state": g.get("state", "post")}},
                "competitors": [side(g["home"], "home"), side(g["away"], "away")]}]})
        return {"events": evs}
    return get


def espn(gid="7", date="2026-10-03T23:30Z", home=("Boston Celtics", 101, [20, 30, 25, 26], True),
         away=("New York Knicks", 99, [30, 20, 24, 25], False), **kw):
    return dict(id=gid, date=date, home=home, away=away, **kw)


class FreeFinals(unittest.TestCase):
    """ESPN's free scoreboard, in shadow mode: logged next to the Odds API's finals, never used to
    grade. Anything unexpected means no ESPN final (slower, never wrong)."""

    def setUp(self):
        import tempfile
        from unittest import mock
        self.games, self.asked = [], []

        def fake(path):
            self.asked.append(path)
            if self.games is None:
                raise OSError("ESPN is down")
            return espn_board(self.games)(path)
        mock.patch("arbbot._espn_get", side_effect=fake).start()
        self.addCleanup(mock.patch.stopall)
        _arbbot._ESPN_CACHE.clear()
        _arbbot._ESPN_FAILED.clear()
        self.addCleanup(_arbbot._ESPN_FAILED.clear)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.cfg = Config(min_ev_pct=3, round_stakes=0, ev_log_file=str(d / "ev.csv"), outlier_log_file=str(d / "out.csv"),
                          parlay_log_file=str(d / "par.csv"), ev_results_file=str(d / "res.csv"),
                          closing_file=str(d / "close.csv"), markout_file=str(d / "mk.csv"),
                          score_check_file=str(d / "sc.csv"), log_file="")

    def final(self, sport="basketball_nba", home="Boston Celtics", away="New York Knicks", start="2026-10-03T23:30:00Z"):
        _arbbot._ESPN_CACHE.clear()
        return _arbbot.espn_final(sport, home, away, start)

    def test_a_final_score_and_the_checks_on_it(self):
        g = espn()
        self.games = [g]
        self.assertEqual(self.final(), ({"Boston Celtics": 101, "New York Knicks": 99}, ""))
        self.games = [espn(home=g["away"], away=g["home"])]                     # listed the other way round
        self.assertEqual(self.final()[0], {"Boston Celtics": 101, "New York Knicks": 99})
        self.games = [espn(status="STATUS_IN_PROGRESS", completed=False, state="in")]
        self.assertEqual(self.final(), (None, "not final: STATUS_IN_PROGRESS"))   # the status name, to add new ones
        self.games = [espn(status="STATUS_END_OF_REGULATION", completed=True, state="post")]
        self.assertEqual(self.final(), (None, "not final: STATUS_END_OF_REGULATION"))
        self.games = [espn(status="STATUS_POSTPONED", completed=False)]
        self.assertEqual(self.final(), (None, "postponed: STATUS_POSTPONED"))
        self.games = [espn(home=("Boston Celtics", 111, [20, 30, 25, 26], True))]  # periods don't add up
        self.assertIsNone(self.final()[0])
        self.games = [espn(home=("Boston Celtics", 101, [20, 30, 25, 26], False))]  # winner flag says otherwise
        self.assertEqual(self.final(), (None, "ESPN's winner flag disagrees with the score"))
        self.games = [g, espn(gid="8")]                                           # two games fit: which one?
        self.assertEqual(self.final(), (None, "not found"))

    def test_no_tied_finals_and_hockey_needs_the_winner_flag(self):
        nhl = lambda home, away: espn(home=("Boston Bruins",) + home, away=("New York Rangers",) + away)
        g = ("icehockey_nhl", "Boston Bruins", "New York Rangers")
        self.games = [nhl((3, [1, 1, 1], None), (3, [1, 1, 1], None))]            # a shootout goal left out?
        self.assertEqual(self.final(*g), (None, "tied score"))
        self.games = [nhl((4, [1, 1, 1, 0], None), (3, [1, 1, 1, 0], None))]
        self.assertEqual(self.final(*g), (None, "no winner flag"))
        self.games = [nhl((4, [1, 1, 1, 0], True), (3, [1, 1, 1, 0], False))]     # shootout: periods 3-3, final 4-3
        self.assertEqual(self.final(*g), ({"Boston Bruins": 4, "New York Rangers": 3}, ""))
        self.games = [espn(home=("Boston Celtics", 99, [99], None), away=("New York Knicks", 99, [99], None))]
        self.assertEqual(self.final(), (None, "tied score"))

    def test_a_rain_shortened_baseball_game(self):
        mlb = ("baseball_mlb", "Los Angeles Dodgers", "San Diego Padres", "2026-10-03T23:00:00Z")
        self.games = [espn(date="2026-10-03T23:00Z", home=("Los Angeles Dodgers", 4, [1, 0, 2, 0, 1, 0], True),
                           away=("San Diego Padres", 1, [0, 0, 1, 0, 0, 0], False))]
        self.assertEqual(self.final(*mlb), (None, "shortened game"))
        self.games = [espn(date="2026-10-03T23:00Z", home=("Los Angeles Dodgers", 4, [1, 0, 2, 0, 1, 0, 0, 0], True),
                           away=("San Diego Padres", 1, [0, 0, 1, 0, 0, 0, 0, 0, 0], False))]   # home didn't bat the 9th
        self.assertEqual(self.final(*mlb)[0], {"Los Angeles Dodgers": 4, "San Diego Padres": 1})
        # ESPN calls the other team home (a neutral site): the team that batted first still has 9.
        self.games = [espn(date="2026-10-03T23:00Z", home=("San Diego Padres", 4, [1, 0, 2, 0, 1, 0, 0, 0], True),
                           away=("Los Angeles Dodgers", 1, [0, 0, 1, 0, 0, 0, 0, 0, 0], False))]
        self.assertEqual(self.final(*mlb), ({"Los Angeles Dodgers": 1, "San Diego Padres": 4}, ""))
        self.games = [espn(date="2026-10-03T23:00Z", home=("San Diego Padres", 4, [1, 0, 2, 0, 1], True),
                           away=("Los Angeles Dodgers", 1, [0, 0, 1, 0, 0, 0], False))]   # swapped, and shortened
        self.assertEqual(self.final(*mlb), (None, "shortened game"))
        self.games = [espn(date="2026-10-03T23:00Z", home=("Los Angeles Dodgers", 4, [], True),
                           away=("San Diego Padres", 1, [], False))]          # no innings listed: not "shortened"
        self.assertEqual(self.final(*mlb), ({"Los Angeles Dodgers": 4, "San Diego Padres": 1}, ""))

    def log(self, gid, market, outcome, point="", sport="basketball_nba", home="Boston Celtics",
            away="New York Knicks", start="2026-10-03T23:30:00Z", first_seen="2026-10-03T20:00:00+00:00"):
        append_csv(self.cfg.ev_log_file, _arbbot.EV_LOG_FIELDS, {
            "first_seen": first_seen, "event_id": gid, "sport": "X", "sport_key": sport, "matchup": f"{away} @ {home}",
            "home_team": home, "away_team": away, "commence_time": start, "live": False, "market": market,
            "outcome": outcome, "point": point, "n_outcomes": 2, "book": "B", "price": 2.0, "fair_odds": 1.9,
            "best_ev_pct": 5, "stake": 10, "player": "", "confidence": "high", "fair_from": "Pinnacle"})

    def grade(self, api_final, sport="basketball_nba", gid="nba7", home="Boston Celtics", away="New York Knicks"):
        calls = []

        class Api:
            def scores(self, s, days_from=3):
                calls.append(s)
                return [{"id": gid, "completed": True,
                         "scores": [{"name": home, "score": str(api_final[0])}, {"name": away, "score": str(api_final[1])}]}]
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rows = settle_pending(self.cfg, Api(), datetime(2026, 10, 4, 4, 0, tzinfo=timezone.utc))
        checks = _read(self.cfg.score_check_file) if Path(self.cfg.score_check_file).exists() else []
        return rows, calls, checks, err.getvalue()

    def test_shadow_logs_agreement_once_per_game_and_grades_from_the_api(self):
        self.games = [espn(home=("Boston Celtics", 101, [101], True), away=("New York Knicks", 99, [99], False))]
        self.log("nba7", "h2h", "Boston Celtics")
        rows, calls, checks, _ = self.grade((101, 99))
        self.assertEqual(([r["result"] for r in rows], calls), (["win"], ["basketball_nba"]))
        self.assertEqual([c["verdict"] for c in checks], ["agree"])
        self.log("nba7", "totals", "Over", 190.5, first_seen="2026-10-03T21:00:00+00:00")   # another bet, same game
        rows, _, checks, _ = self.grade((101, 99))
        self.assertEqual(([r["result"] for r in rows], len(checks)), (["win"], 1))          # still one row

    def test_shadow_logs_a_disagreement_and_the_api_still_decides(self):
        self.games = [espn(home=("Boston Celtics", 101, [101], True), away=("New York Knicks", 99, [99], False))]
        self.log("nba7", "h2h", "Boston Celtics")
        rows, _, checks, err = self.grade((98, 99))
        self.assertEqual([c["verdict"] for c in checks], ["disagree"])
        self.assertEqual(rows[0]["result"], "loss")
        self.assertIn("disagree", err)

    def test_shadow_writes_nothing_when_espn_is_down_and_waits_before_asking_again(self):
        self.games = None
        self.log("nba7", "h2h", "Boston Celtics")
        rows, _, checks, err = self.grade((101, 99))
        self.assertEqual(([r["result"] for r in rows], checks), (["win"], []))
        self.assertEqual(err.count("ESPN scoreboard"), 1)
        asked = len(self.asked)
        with self.assertRaises(OSError):
            _arbbot._espn_quick(self.asked[0])
        self.assertEqual(len(self.asked), asked)                                  # a recent failure isn't asked again

    def test_off_doesnt_ask_espn_about_main_lines(self):
        self.cfg = replace(self.cfg, free_scores="off")
        self.games = [espn(home=("Boston Celtics", 101, [101], True), away=("New York Knicks", 99, [99], False))]
        self.log("nba7", "h2h", "Boston Celtics")
        rows, _, checks, _ = self.grade((101, 99))
        self.assertEqual(([r["result"] for r in rows], checks, self.asked), (["win"], [], []))

    def test_run_lines_and_totals_on_a_rain_shortened_game_stay_manual_in_every_mode(self):
        mlb = dict(sport="baseball_mlb", home="Los Angeles Dodgers", away="San Diego Padres", start="2026-10-03T23:00:00Z")
        for mode in ("off", "shadow"):
            with self.subTest(mode=mode):
                for f in (self.cfg.ev_log_file, self.cfg.ev_results_file, self.cfg.score_check_file):
                    Path(f).unlink(missing_ok=True)
                self.cfg = replace(self.cfg, free_scores=mode)
                self.games = [espn(date="2026-10-03T23:00Z", home=("Los Angeles Dodgers", 4, [1, 0, 2, 0, 1, 0], True),
                                   away=("San Diego Padres", 1, [0, 0, 1, 0, 0, 0], False))]
                _arbbot._ESPN_CACHE.clear()
                self.log("mlb1", "h2h", "Los Angeles Dodgers", **mlb)
                self.log("mlb1", "spreads", "Los Angeles Dodgers", -1.5, **mlb)
                self.log("mlb1", "totals", "Over", 7.5, **mlb)
                rows, _, _, _ = self.grade((4, 1), sport="baseball_mlb", gid="mlb1", home="Los Angeles Dodgers",
                                           away="San Diego Padres")
                self.assertEqual([(r["market"], r["result"]) for r in rows], [("h2h", "win")])
                held = sorted((r["market"], r["result"], r["actual"]) for r in _read(self.cfg.ev_results_file))[1:]
                self.assertEqual(held, [("spreads", "", "rain-shortened"), ("totals", "", "rain-shortened")])
        # Written once: later passes don't buy scores or ask ESPN about it again.
        self.asked.clear()
        rows, calls, _, _ = self.grade((4, 1), sport="baseball_mlb", gid="mlb1", home="Los Angeles Dodgers",
                                       away="San Diego Padres")
        self.assertEqual((rows, calls, self.asked, len(_read(self.cfg.ev_results_file))), ([], [], [], 3))

    def test_a_slow_espn_pass_leaves_rain_checks_for_the_next_pass(self):
        from unittest import mock
        mlb = dict(sport="baseball_mlb", home="Los Angeles Dodgers", away="San Diego Padres", start="2026-10-03T23:00:00Z")
        self.games = [espn(date="2026-10-03T23:00Z", home=("Los Angeles Dodgers", 4, [1, 0, 2, 0, 1, 0, 0, 0], True),
                           away=("San Diego Padres", 1, [0, 0, 1, 0, 0, 0, 0, 0, 0], False))]
        self.log("mlb1", "h2h", "Los Angeles Dodgers", **mlb)
        self.log("mlb1", "spreads", "Los Angeles Dodgers", -1.5, **mlb)
        game = dict(sport="baseball_mlb", gid="mlb1", home="Los Angeles Dodgers", away="San Diego Padres")
        with mock.patch("arbbot.ESPN_PASS_SECONDS", -1):                         # out of time before asking
            rows, _, checks, _ = self.grade((4, 1), **game)
        self.assertEqual(([r["market"] for r in rows], checks, self.asked), (["h2h"], [], []))
        rows, _, _, _ = self.grade((4, 1), **game)                                 # next pass: a full game
        self.assertEqual([r["market"] for r in rows], ["spreads"])

    def test_a_parlay_leg_on_a_rain_shortened_game_isnt_graded(self):
        leg = {"event_id": "mlb1", "home_team": "LA", "away_team": "SD", "market": "spreads", "outcome": "LA",
               "point": -1.5, "price": 2.0, "player": "", "n_outcomes": 2}
        other = {**leg, "event_id": "nhl1", "home_team": "Home", "away_team": "Away", "market": "h2h", "outcome": "Home",
                 "point": ""}
        r = {"_legs": [leg, other], "stake": 10}
        finals = {"mlb1": {"LA": 4, "SD": 1}, "nhl1": {"Home": 3, "Away": 2}}
        self.assertEqual(_arbbot.settle_parlay(r, finals)[0], "win")
        self.assertIsNone(_arbbot.settle_parlay(r, finals, hold={"mlb1"}))
        self.assertEqual(_arbbot.settle_parlay({**r, "_legs": [{**leg, "market": "h2h", "point": ""}, other]},
                                               finals, hold={"mlb1"})[0], "win")       # the moneyline still stands

    MLB = dict(sport="baseball_mlb", home="Los Angeles Dodgers", away="San Diego Padres", start="2026-10-03T23:00:00Z")
    FULL = dict(date="2026-10-03T23:00Z", home=("Los Angeles Dodgers", 4, [1, 0, 2, 0, 1, 0, 0, 0], True),
                away=("San Diego Padres", 1, [0, 0, 1, 0, 0, 0, 0, 0, 0], False))
    RAIN = dict(date="2026-10-03T23:00Z", home=("Los Angeles Dodgers", 4, [1, 0, 2, 0, 1, 0], True),
                away=("San Diego Padres", 1, [0, 0, 1, 0, 0, 0], False))
    NBA = dict(home=("Boston Celtics", 101, [101], True), away=("New York Knicks", 99, [99], False))

    class TwoGames:
        """Odds API finals: Dodgers 4-1 (mlb1) and Celtics 101-99 (nba7)."""
        def __init__(self):
            self.calls = []

        def scores(self, s, days_from=3):
            self.calls.append(s)
            if s == "baseball_mlb":
                return [{"id": "mlb1", "completed": True, "scores": [{"name": "Los Angeles Dodgers", "score": "4"},
                                                                     {"name": "San Diego Padres", "score": "1"}]}]
            return [{"id": "nba7", "completed": True, "scores": [{"name": "Boston Celtics", "score": "101"},
                                                                 {"name": "New York Knicks", "score": "99"}]}]

    def settle(self, api, at=datetime(2026, 10, 4, 4, 0, tzinfo=timezone.utc)):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rows = settle_pending(self.cfg, api, at)
        return rows, out.getvalue()

    def parlay(self, *legs):
        import json
        mk = lambda gid, sport, home, away, market, outcome, point: {
            "event_id": gid, "sport_key": sport, "home_team": home, "away_team": away, "market": market,
            "outcome": outcome, "point": point, "price": 2.0, "player": "", "n_outcomes": 2,
            "commence_time": "2026-10-03T23:00:00Z"}
        append_csv(self.cfg.parlay_log_file, ["first_seen", "book", "price", "fair_prob", "best_ev_pct", "stake",
                                              "games", "legs", "legs_json"],
                   {"first_seen": "2026-10-03T20:00:00+00:00", "book": "B", "price": 4.0, "fair_prob": 0.27,
                    "best_ev_pct": 5, "stake": 10, "games": "x", "legs": "y",
                    "legs_json": json.dumps([mk(*l) for l in legs])})

    MLB_SPREAD = ("mlb1", "baseball_mlb", "Los Angeles Dodgers", "San Diego Padres", "spreads", "Los Angeles Dodgers",
                  -1.5)

    def test_a_rain_held_bet_is_written_once_shown_as_rain_and_left_out_of_the_record(self):
        self.games = [espn(**self.RAIN)]
        self.log("mlb1", "h2h", "Los Angeles Dodgers", **self.MLB)
        self.log("mlb1", "spreads", "Los Angeles Dodgers", -1.5, **self.MLB)
        self.log("mlb1", "totals", "Over", 7.5, **self.MLB)
        api = self.TwoGames()
        rows, out = self.settle(api)
        self.assertEqual([(r["market"], r["result"]) for r in rows], [("h2h", "win")])   # only real grades returned
        self.assertEqual(out.count("ended before the 9th"), 1)                          # said once, for the game
        rows, out = self.settle(api)
        self.assertEqual((rows, out, api.calls, len(self.asked)), ([], "", ["baseball_mlb"], 1))  # never asked again
        later = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)                       # long after: would be "stuck"
        day = _arbbot.local_day(self.cfg, self.MLB["start"])
        bets = _arbbot.day_bets(self.cfg, day)
        lines = {r["market"]: _arbbot.result_line(r, self.cfg, later) for r in bets}
        for m in ("spreads", "totals"):
            self.assertIn("🌧️", lines[m])
            self.assertIn("· game ended early (rain): books usually void run lines and totals, check your book",
                          lines[m])
            self.assertNotIn("check the box score", lines[m])
        summary = _arbbot.day_summary(bets, self.cfg, later)
        self.assertIn("🌧️ 2 on games that ended early (rain): check your book", summary)
        self.assertNotIn("to check yourself", summary)
        self.assertNotIn("still to finish", summary)
        self.assertIn("**All:** 1-0 ·", summary)                                         # the record: the moneyline
        self.assertTrue(_arbbot.ev_record(self.cfg).startswith("1 bets, 1-0-0"))
        self.assertEqual(_arbbot._record(_read(self.cfg.ev_results_file))[:3], (1, 0, 0))

    def test_a_parlay_waiting_only_on_a_rain_held_leg_is_written_once_for_you_to_check(self):
        self.games = [espn(**self.NBA), espn(gid="9", **self.RAIN)]
        self.parlay(self.MLB_SPREAD, ("nba7", "basketball_nba", "Boston Celtics", "New York Knicks", "h2h",
                                      "Boston Celtics", ""))
        api = self.TwoGames()
        rows, out = self.settle(api)
        self.assertEqual((rows, out.count("ended before the 9th")), ([], 1))
        [row] = _read(self.cfg.ev_results_file)
        self.assertEqual((row["kind"], row["result"], row["actual"]), ("parlay", "", "rain-shortened"))
        self.assertEqual(self.settle(api)[0], [])
        self.assertEqual(len(api.calls), 2)                                              # not fetched again
        later = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
        [bet] = _arbbot.day_bets(self.cfg, _arbbot.local_day(self.cfg, self.MLB["start"]))
        self.assertIn("🌧️", _arbbot.result_line(bet, self.cfg, later))
        self.assertIn("has a leg on a game that ended early (rain)", _arbbot.result_line(bet, self.cfg, later))
        summary = _arbbot.day_summary([bet], self.cfg, later)
        self.assertIn("🌧️ 1 on a game that ended early (rain): check your book", summary)
        self.assertNotIn("prop leg", summary)
        self.assertIn("no graded bets yet", summary)

    def test_a_parlay_still_waiting_on_another_game_isnt_written_yet(self):
        self.games = [espn(gid="9", **self.RAIN)]
        self.parlay(self.MLB_SPREAD, ("nba7", "basketball_nba", "Boston Celtics", "New York Knicks", "h2h",
                                      "Boston Celtics", ""))

        class NotFinal(self.TwoGames):
            def scores(self, s, days_from=3):
                return [dict(g, completed=False) if g["id"] == "nba7" else g for g in super().scores(s, days_from)]
        self.assertEqual(self.settle(NotFinal())[0], [])
        self.assertFalse(Path(self.cfg.ev_results_file).exists())                       # the NBA game isn't over
        self.games.append(espn(**self.NBA))
        _arbbot._ESPN_CACHE.clear()
        self.settle(self.TwoGames())
        [row] = _read(self.cfg.ev_results_file)
        self.assertEqual((row["result"], row["actual"]), ("", "rain-shortened"))

    def test_a_parlay_lost_on_another_leg_with_a_rain_held_leg_stays_out_of_the_record(self):
        self.games = [espn(**self.NBA), espn(gid="9", **self.RAIN)]
        self.parlay(self.MLB_SPREAD, ("nba7", "basketball_nba", "Boston Celtics", "New York Knicks", "h2h",
                                      "New York Knicks", ""))
        rows, _ = self.settle(self.TwoGames())
        self.assertEqual([(r["kind"], r["result"], r["manual_legs"]) for r in rows], [("parlay", "loss", 1)])

    def test_a_totals_only_bet_on_a_rain_shortened_game_isnt_graded_with_free_scores_off(self):
        self.cfg = replace(self.cfg, free_scores="off")
        self.games = [espn(**self.RAIN)]
        self.log("mlb1", "totals", "Over", 7.5, **self.MLB)
        rows, _, checks, _ = self.grade((4, 1), sport="baseball_mlb", gid="mlb1", home="Los Angeles Dodgers",
                                        away="San Diego Padres")
        self.assertEqual((rows, checks), ([], []))
        [row] = _read(self.cfg.ev_results_file)
        self.assertEqual((row["market"], row["result"], row["actual"]), ("totals", "", "rain-shortened"))

    def test_rain_checks_go_first_when_espn_is_slow(self):
        from unittest import mock
        # A shadow-only NBA game is logged first, then a full-length MLB game with a run line bet;
        # there's time for one ESPN check. It goes to the rain check, so the run line is graded now.
        self.games = [espn(**self.NBA), espn(gid="9", **self.FULL)]
        self.log("nba7", "h2h", "Boston Celtics")
        self.log("mlb1", "spreads", "Los Angeles Dodgers", -1.5, **self.MLB)
        real, asked = _arbbot.espn_final, []

        def one_then_out(*a, **k):
            asked.append(a[0])
            _arbbot.ESPN_PASS_SECONDS = -1          # out of time after the first game
            return real(*a, **k)
        with mock.patch.object(_arbbot, "ESPN_PASS_SECONDS", 15), mock.patch("arbbot.espn_final", one_then_out):
            rows, _ = self.settle(self.TwoGames())
        self.assertEqual(asked, ["baseball_mlb"])
        self.assertEqual(sorted(r["market"] for r in rows), ["h2h", "spreads"])

    def test_the_agreement_count_and_where_results_print_it(self):
        import argparse, contextlib, io
        from unittest import mock
        self.assertEqual(_arbbot.score_check_line(self.cfg), "")                         # nothing logged yet
        for gid, v in (("a", "agree"), ("b", "disagree"), ("c", "espn: not found")):
            append_csv(self.cfg.score_check_file, _arbbot.SCORE_CHECK_FIELDS, {"event_id": gid, "verdict": v})
        line = _arbbot.score_check_line(self.cfg)
        self.assertEqual(line, "ESPN's free scoreboard agreed on 1 of 3 finals (1 disagreement; the rest it couldn't "
                               f"grade). Details: {self.cfg.score_check_file}")
        d = Path(self.tmp.name)
        cfg = replace(self.cfg, state_dir=str(d / "state"), log_file=str(d / "arbs.csv"))
        args = argparse.Namespace(once=False, demo=False, dry_run=False, plan=False, check_kalshi=False,
                                  results="today", post_results=None)
        out = io.StringIO()
        with mock.patch("arbbot.OddsAPI", lambda cfg: SimpleNamespace(remaining=None)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            _arbbot.run(cfg, args, _arbbot.Status(cfg, dry_run=True))
        self.assertIn(line, out.getvalue())

    def test_a_scoreboard_event_without_two_teams_isnt_a_final(self):
        from unittest import mock
        # (_espn_game already skips these; espn_final doesn't count on it.)
        ev = {"competitions": [{"status": {"type": {"name": "STATUS_FINAL", "completed": True, "state": "post"}},
                                "competitors": [{"team": {"displayName": "Boston Celtics"}, "score": "1"}] * 3}]}
        with mock.patch("arbbot._espn_game", return_value=ev):
            self.assertEqual(self.final(), (None, "not two teams"))


class DataFilesStayOutOfGit(unittest.TestCase):
    def test_every_default_data_file_is_in_gitignore(self):
        # The server's copy is a git checkout: a data file git doesn't ignore shows up as changed by
        # hand, and could be committed with the code.
        from dataclasses import fields
        ignored = (Path(__file__).parent / ".gitignore").read_text().split()
        d = Config()
        files = [getattr(d, f.name) for f in fields(Config) if f.name.endswith("_file")]
        self.assertIn("score_checks.csv", files)
        for name in files:
            self.assertIn(name, ignored)
        self.assertIn(d.state_dir + "/", ignored)


class UpcomingProbe(unittest.TestCase):
    """--check-upcoming: one look at the combined live call, compared with each sport's own."""

    def api(self, combined, per_sport, schedule):
        live = lambda gid, sport, books: {"id": gid, "sport_key": sport, "commence_time": "2026-10-03T11:30:00Z",
                                         "home_team": "H", "away_team": "A", "bookmakers": [{"key": b} for b in books]}

        class Api:
            remaining = used = None
            calls = []
            def events(self, sport, horizon_hours=26):
                return [{"id": g, "commence_time": "2026-10-03T11:30:00Z"} for g in schedule.get(sport, [])]
            def _request(self, path, params):
                Api.calls.append((path, params["markets"]))
                if path == "/sports/upcoming/odds":
                    return [live(g, s, b) for g, s, b in combined], 1.0
                sport = path.split("/")[2]
                return [live(g, sport, b) for g, b in per_sport.get(sport, [])], 1.0
        return Api()

    def probe(self, api, cfg=None):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rep = _arbbot.check_upcoming(cfg or Config(sports=["basketball_nba", "icehockey_nhl"], live_markets="h2h"),
                                         api, NOW)
        return rep, out.getvalue()

    def test_reports_cost_missing_games_and_books(self):
        api = self.api(combined=[("n1", "basketball_nba", "ab"), ("h1", "icehockey_nhl", "ab"), ("s1", "soccer_epl", "ab")],
                       per_sport={"basketball_nba": [("n1", "ab")], "icehockey_nhl": [("h1", "abc"), ("h2", "ab")]},
                       schedule={"basketball_nba": ["n1", "n2"], "icehockey_nhl": ["h1", "h2"]})
        rep, text = self.probe(api)
        self.assertEqual((rep["cost"], rep["formula"]), (1.0, 1))
        self.assertEqual(rep["missing"], {"basketball_nba": [], "icehockey_nhl": ["h2"]})
        self.assertEqual(rep["schedule_only"]["basketball_nba"], ["n2"])     # it ended: not the combined call's fault
        self.assertEqual(rep["fewer_books"]["icehockey_nhl"], ["A @ H: no c"])
        self.assertIn("Don't switch", text)
        self.assertEqual({m for _, m in api.calls}, {"h2h"})                  # LIVE_MARKETS only

    def test_a_game_only_the_schedule_calls_live_doesnt_fail_it(self):
        api = self.api(combined=[("n1", "basketball_nba", "ab"), ("h1", "icehockey_nhl", "ab")],
                       per_sport={"basketball_nba": [("n1", "ab")], "icehockey_nhl": [("h1", "ab")]},
                       schedule={"basketball_nba": ["n1", "n0"], "icehockey_nhl": ["h1"]})
        rep, text = self.probe(api)
        self.assertEqual(rep["missing"], {"basketball_nba": [], "icehockey_nhl": []})
        self.assertIn("Looks good: the combined call had every live game for 1 credits instead of 2", text)

    def test_nothing_live_costs_nothing(self):
        api = self.api(combined=[], per_sport={}, schedule={"basketball_nba": []})
        rep, text = self.probe(api)
        self.assertEqual(api.calls, [])
        self.assertIn("No credits used", text)

    def test_the_command_line_flag(self):
        import os, sys, contextlib, io
        from unittest import mock
        import tempfile
        loop = AssertionError("the main loop started")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        files = {k: str(Path(tmp.name) / k.lower()) for k in ("LOG_FILE", "EV_LOG_FILE", "OUTLIER_LOG_FILE", "PARLAY_LOG_FILE",
                                                              "EV_RESULTS_FILE", "CLOSING_FILE", "MARKOUT_FILE",
                                                              "SCORE_CHECK_FILE")}
        with mock.patch.dict(os.environ, {"ODDS_API_KEY": "k", "STATE_DIR": "", **files}), \
                mock.patch.object(sys, "argv", ["arbbot.py", "--check-upcoming"]), mock.patch("arbbot.load_dotenv"), \
                mock.patch("arbbot.check_upcoming") as probe, contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(_arbbot.Scheduler, "refresh_events", side_effect=loop), \
                mock.patch("arbbot.time.sleep", side_effect=loop):
            _arbbot.main()
        self.assertEqual(probe.call_count, 1)


# --------------------------------------------------------------------------- Kalshi depth for bets at Kalshi

class KalshiDepth(unittest.TestCase):
    """A bet at Kalshi bigger than Kalshi's order book holds at that price: the card says so, and a
    +EV or outlier stake is cut to what's there (an arb's stakes stay: its bets must balance)."""

    setUp, game, fair = KalshiCrossCheck.setUp, KalshiCrossCheck.game, KalshiCrossCheck.fair

    def test_the_quote_carries_the_ask_size_and_still_reads_as_three_numbers(self):
        self.markets["KXNFLGAME"][1]["yes_ask_size_fp"] = "250.00"
        fair = self.fair([self.game()])["nfl1"]
        jets = fair["New York Jets"]
        self.assertEqual(getattr(jets, "ask_size", None), 250)
        self.assertEqual(getattr(fair["Buffalo Bills"], "ask_size", None), 5000)
        p, bid, ask = jets                                                  # old callers unpack three
        self.assertEqual((bid, ask), (0.37, 0.39))
        self.assertEqual(jets, (p, 0.37, 0.39))                            # and compare with plain tuples
        self.assertEqual(_arbbot.kalshi_room(jets), 250 * 0.39)
        self.assertIsNone(_arbbot.kalshi_room((p, 0.37, 0.39)))            # a plain tuple: size not known
        import copy, pickle
        for again in (copy.deepcopy(jets), pickle.loads(pickle.dumps(jets))):
            self.assertEqual((again, getattr(again, "ask_size", None)), (jets, 250))
        self.assertIn("about $0.60 at this price", _arbbot.kalshi_room_line(0.6))

    def test_ev_bet_at_kalshi_is_cut_to_what_kalshi_has(self):
        from arbbot import KalshiQuote
        cfg = Config(min_ev_pct=3, round_stakes=5, confidence_stakes="1,1,1")
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"Kalshi": [("Home", 2.1398, None)]})
        [full] = find_evs([ev], cfg, NOW, kalshi={"e1": {"Home": (0.445, 0.44, 0.45)}})   # no size: as before
        self.assertEqual((full.stake, full.kalshi_room), (15.0, 0.0))
        self.assertNotIn("Kalshi only has", ev_payload(full)["embeds"][0]["description"])
        [b] = find_evs([ev], cfg, NOW, kalshi={"e1": {"Home": KalshiQuote(0.445, 0.44, 0.45, 30)}})   # $13.50 there
        self.assertEqual(b.stake, 10.0)                                    # rounded down to $5s, never over
        desc = ev_payload(b)["embeds"][0]["description"]
        self.assertIn("bet **$10** on **Home ML", desc)
        self.assertIn("↳ Kalshi only has about $13 at this price; the rest would fill at a worse price.", desc)
        [deep] = find_evs([ev], cfg, NOW, kalshi={"e1": {"Home": KalshiQuote(0.445, 0.44, 0.45, 1000)}})
        self.assertEqual((deep.stake, deep.kalshi_room), (15.0, 0.0))     # enough there: unchanged
        # Kalshi's quote on a bet somewhere else is only a second opinion: that stake isn't Kalshi's to cut.
        other = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})
        [b2] = find_evs([other], cfg, NOW, kalshi={"e1": {"Home": KalshiQuote(0.51, 0.50, 0.52, 1)}})
        [same] = find_evs([other], cfg, NOW, kalshi={"e1": {"Home": (0.51, 0.50, 0.52)}})
        self.assertEqual((b2.stake, b2.kalshi_room), (same.stake, 0.0))

    def test_outlier_at_kalshi_is_cut_too(self):
        from arbbot import KalshiQuote
        out = outlier_event(1.85, 1.95)
        out["bookmakers"][-1].update(key="kalshi", title="Kalshi")
        [full] = find_outliers([out], Config(), NOW, kalshi={"e1": {"Home": (0.515, 0.51, 0.52)}})
        [o] = find_outliers([out], Config(), NOW, kalshi={"e1": {"Home": KalshiQuote(0.515, 0.51, 0.52, 10)}})
        self.assertGreater(full.stake, 5.2)
        self.assertEqual((o.stake, o.kalshi_room), (5.0, 10 * 0.52))
        self.assertIn("↳ Kalshi only has about $5 at this price; the rest would fill at a worse price.",
                      outlier_payload(o)["embeds"][0]["description"])

    def kalshi_arb(self):
        return event({"Kalshi": [("h2h", [("Home", 2.20, None), ("Away", 1.60, None)])],
                      "B": [("h2h", [("Home", 1.70, None), ("Away", 2.15, None)])]})

    def test_kalshi_arb_bet_gets_a_note_and_keeps_its_stake(self):
        from arbbot import KalshiQuote, note_kalshi_room
        [arb] = find_arbs([self.kalshi_arb()], CFG, NOW)
        stakes = [(l.book, l.stake) for l in arb.legs]
        note_kalshi_room([arb], {"e1": {"Home": KalshiQuote(0.44, 0.42, 0.43, 100)}}, CFG)   # $43 at 43¢
        self.assertEqual([(l.book, l.stake) for l in arb.legs], stakes)   # an arb's stakes stay balanced
        kalshi = next(l for l in arb.legs if l.book == "Kalshi")
        self.assertGreater(kalshi.stake, 43)
        self.assertAlmostEqual(kalshi.room, 43.0)
        desc = discord_payload(arb)["embeds"][0]["description"]
        self.assertIn("↳ Kalshi only has about $43 at this price; the rest would fill at a worse price.", desc)
        self.assertEqual(desc.count("Kalshi only has"), 1)
        # Kalshi's best ask no longer the card's price (its own order book moved): nothing to count.
        [moved] = find_arbs([self.kalshi_arb()], CFG, NOW)
        note_kalshi_room([moved], {"e1": {"Home": KalshiQuote(0.46, 0.45, 0.46, 100)}}, CFG)
        self.assertEqual([l.room for l in moved.legs], [0.0, 0.0])
        [deep] = find_arbs([self.kalshi_arb()], CFG, NOW)                   # enough there: no note
        note_kalshi_room([deep], {"e1": {"Home": KalshiQuote(0.44, 0.42, 0.43, 5000)}}, CFG)
        self.assertNotIn("Kalshi only has", discord_payload(deep)["embeds"][0]["description"])


class KalshiDepthScan(MixFiles):
    def test_a_kalshi_arb_waits_for_kalshi_so_its_first_card_has_the_note(self):
        from unittest import mock
        from arbbot import KalshiQuote
        t = _arbbot.Trackers(self.cfg(Config(min_profit_pct=0, round_stakes=0)), self.args())
        ev = KalshiDepth.kalshi_arb(self)
        ev["sport_key"] = "basketball_nba"
        quotes = {"e1": {"Home": KalshiQuote(0.44, 0.42, 0.43, 100), "Away": KalshiQuote(0.56, 0.55, 0.57, 100)}}
        with mock.patch("arbbot.kalshi_fair", return_value=quotes) as asked:
            _arbbot.scan_main(t, [ev], ["basketball_nba"], NOW)
        self.assertEqual(asked.call_count, 1)                               # asked once, for everything
        [op] = t.arbs.open.values()
        self.assertIn("Kalshi only has about $43", discord_payload(op.arb)["embeds"][0]["description"])
        self.assertEqual(op.card, t.arbs.card_hash(op.arb, op.first_seen))  # in the card as it went out
        # No Kalshi bet in an arb: arbs still go out before Kalshi is asked.
        t2 = _arbbot.Trackers(self.cfg(Config(min_profit_pct=0, round_stakes=0)), self.args())
        plain = event({"A": [("h2h", [("Home", 2.20, None), ("Away", 1.60, None)])],
                       "B": [("h2h", [("Home", 1.70, None), ("Away", 2.15, None)])]})
        plain["sport_key"] = "basketball_nba"
        order = []
        real_handle = t2.arbs.handle
        t2.arbs.handle = lambda *a, **k: order.append("arbs") or real_handle(*a, **k)
        with mock.patch("arbbot.kalshi_fair", side_effect=lambda *a, **k: order.append("kalshi") or {}):
            _arbbot.scan_main(t2, [plain], ["basketball_nba"], NOW)
        self.assertEqual(order, ["arbs", "kalshi"])
        # Nor for a Kalshi bet Kalshi's quote can't speak for: a live game (KALSHI_LIVE=false), or a spread.
        for live, market in ((True, "h2h"), (False, "spreads")):
            t3 = _arbbot.Trackers(self.cfg(Config(min_profit_pct=0, min_live_profit_pct=0, round_stakes=0)), self.args())
            pt = -1.5 if market == "spreads" else None
            other = event({"Kalshi": [(market, [("Home", 2.20, pt), ("Away", 1.60, None if pt is None else -pt)])],
                           "B": [(market, [("Home", 1.70, pt), ("Away", 2.15, None if pt is None else -pt)])]},
                          start="2026-10-03T11:30:00Z" if live else "2026-10-03T18:00:00Z")
            other["sport_key"] = "basketball_nba"
            order = []
            real3 = t3.arbs.handle
            t3.arbs.handle = lambda *a, real3=real3, **k: order.append("arbs") or real3(*a, **k)
            with mock.patch("arbbot.kalshi_fair", side_effect=lambda *a, **k: order.append("kalshi") or quotes):
                _arbbot.scan_main(t3, [other], ["basketball_nba"], NOW)
            self.assertEqual(order, ["arbs", "kalshi"], (live, market))
            self.assertEqual([l.room for op in t3.arbs.open.values() for l in op.arb.legs], [0.0, 0.0])
        [spread] = find_arbs([other], CFG, NOW)                             # a Kalshi quote is the moneyline's
        _arbbot.note_kalshi_room([spread], quotes, CFG)
        self.assertEqual([l.room for l in spread.legs], [0.0, 0.0])


# --------------------------------------------------------------------------- weekly report card

class WeeklyReportCard(MixFiles):
    """📋 Monday's report card: the last 7 days by alert type, from each one's own file, with
    plain-English suggestions only (the bot never changes a setting)."""

    MONDAY = datetime(2026, 10, 5).date()     # the card covers Mon Sep 28 - Sun Oct 4

    def bet(self, i, kind="ev", live=False, player="", result="win", fair_from="Pinnacle", day="2026-10-01",
            close=0.47, start=None):
        from arbbot import RESULT_FIELDS, EV_LOG_FIELDS, CLOSING_FIELDS
        r = {"first_seen": f"{day}T15:00:00+00:00", "event_id": f"g{i}", "sport": "NHL", "sport_key": "icehockey_nhl",
             "matchup": "A @ H", "home_team": "H", "away_team": "A", "commence_time": start or f"{day}T23:00:00Z",
             "live": live,
             "market": "player_points" if player else "h2h", "outcome": "Over" if player else "H",
             "point": 0.5 if player else "", "n_outcomes": 2, "book": "DraftKings", "price": 2.2, "fair_odds": 2.0,
             "best_ev_pct": 10, "stake": 20, "player": player, "confidence": "high", "fair_from": fair_from}
        append_csv(self.files["ev_log_file" if kind == "ev" else "outlier_log_file"], EV_LOG_FIELDS, r)
        append_csv(self.files["ev_results_file"], RESULT_FIELDS,
                   {**r, "kind": kind, "result": result, "profit": 24 if result == "win" else -20})
        if close and not live:
            append_csv(self.files["closing_file"], CLOSING_FIELDS,
                       {"bet_id": _arbbot._bet_id(r), "closing_fair_prob": close, "closing_fair_odds": 1 / close})

    def mark(self, i, kind="outlier", live=True, pct=-1.0, still=0, day="2026-10-02"):
        append_csv(self.files["markout_file"], _arbbot.MARKOUT_FIELDS,
                   {"first_seen": f"{day}T01:00:00+00:00", "bet_id": f"m{i}", "kind": kind, "live": live,
                    "book": "DraftKings", "edge_pct": 22, "markout_pct": pct, "markout_secs": 170, "still_ok": still,
                    "arb_id": f"a{i}" if kind == "arb" else ""})

    def arb(self, matchup, pct, live=False, day="2026-10-02", reason="", at="01:00"):
        append_csv(self.files["log_file"], _arbbot.LOG_FIELDS,
                   {"first_seen": f"{day}T{at}:00+00:00", "matchup": matchup, "market": "h2h", "line": "",
                    "live": live, "best_profit_pct": pct, "reason": reason})

    def card(self, held=None, cfg=None):
        return _arbbot.weekly_text(cfg or self.cfg(), self.MONDAY, held)

    def test_every_alert_type_from_its_own_files_this_week_only(self):
        for i in range(8):
            self.bet(i, result="win" if i % 3 else "loss")                  # 5-3
        self.bet(90, day="2026-09-20")                                        # last week: not counted
        self.bet(91, day="2026-10-05")                                        # nor this week's
        for i in range(10, 13):
            self.bet(i, kind="outlier", live=True, result="loss")
        for i in range(13, 15):
            self.bet(i, kind="outlier")
        for i in range(20, 26):
            self.bet(i, player="LeBron James", fair_from="consensus of DraftKings, FanDuel, BetMGM, Caesars")
        for i in range(40):
            self.mark(i, pct=-1 + (i % 5) * 0.3, still=int(i % 3 == 0))
        self.mark(99, day="2026-09-21")                                       # last week
        for i in range(5):
            self.arb(f"X{i} @ Y", 2.5)
        self.arb("X0 @ Y", 2.0)                                               # the same arb again: once
        self.arb("Z @ Y", 6, live=True)
        self.arb("Q @ Y", 9, reason="capped")                                 # held back, never sent
        self.arb("W @ Y", 3, day="2026-09-27")                                # last week
        held = {"2026-10-02": {"Live arbs": {"unconfirmed": 3, "live cap": 1}, "Outliers": {"capped": 2}},
                "2026-09-27": {"+EV": {"capped": 9}}}
        title, text, profit = self.card(held)
        self.assertEqual(title, "📋 Weekly report card · Sep 28 – Oct 4")
        self.assertIn("💰 **Pre-game arbs** · 5 different · about +$12.50 locked in at $100 each (ROI +2.5%)", text)
        self.assertIn("💰 **Live arbs** · 1 different · about +$6 locked in at $100 each (ROI +6.0%)", text)
        self.assertIn("✋ Held back: 4 (3 live, gone before it was confirmed, 1 over the live-alert cap)", text)
        self.assertIn("📈 **+EV** · 5-3 · +$60 on $160 staked (ROI +37.5%)\n"
                      "📐 CLV (pre-game): avg +3.4%, beat the close on 100% of 8 bets\n"
                      "📏 Did the edge hold: nothing measured yet\n✋ Held back: none", text)
        self.assertIn("🚨 **Outliers** · 2-3 · −$12 on $100 staked (ROI -12.0%) · 🔴 live 0-3 −$60 · "
                      "⏰ pre-game 2-0 +$48", text)
        self.assertIn("📐 CLV (pre-game): avg +3.4%, beat the close on 100% of 2 bets", text)
        self.assertIn("📏 Live outliers: sent +22.0% → later -0.4% (40 bets", text)
        self.assertIn("still there 35%", text)
        self.assertIn("✋ Held back: 2 (2 over the hourly cap)", text)
        self.assertIn("🎯 **Props** · 6-0 · +$144 on $120 staked (ROI +120.0%)", text)
        self.assertIn("📦 **Parlays** · no graded bets yet\n\n💡 **Suggestions**", text)   # nothing else to say
        self.assertIn("• Live outliers: sent +22.0% → -0.4% ~3 min later (still there 35%) → too early to tell "
                      "(40 bets)", text)
        self.assertIn("• Pre-game +EV: CLV +3.4%, beat the close 100% over 8 bets → keep", text)
        self.assertIn("• Props with no Pinnacle price: CLV +3.4%, beat the close 100% over 6 bets → keep", text)
        self.assertEqual(profit, 60 - 12 + 144)
        payload = _arbbot.weekly_payload(self.cfg(), self.MONDAY, held)["embeds"][0]
        self.assertEqual((payload["title"], payload["color"]), (title, 0x2ECC71))
        self.assertIn("never changes a setting", payload["footer"]["text"])

    def test_a_series_counts_each_game_as_the_daily_recaps_do(self):
        for day, pct in (("2026-09-29", 2.5), ("2026-09-30", 3), ("2026-10-01", 4)):   # 7pm, 3 nights in a row
            self.arb("Detroit Tigers @ Cleveland Guardians", pct, day=day, at="23:00")
        self.arb("Detroit Tigers @ Cleveland Guardians", 2.0, day="2026-10-02", at="01:30")   # 9:30pm the 3rd
        self.assertIn("💰 **Pre-game arbs** · 3 different · about +$9.50 locked in at $100 each (ROI +3.2%)",
                      self.card({})[1])
        nights = [self.MONDAY - timedelta(days=d) for d in (6, 5, 4)]                # Tue-Thu in New York
        self.assertEqual([_arbbot.arbs_on(self.cfg(), d).split(" if you")[0] for d in nights],
                         ["💰 **Arbs:** 1 different · about +$2.50", "💰 **Arbs:** 1 different · about +$3",
                          "💰 **Arbs:** 1 different (2 alerts) · about +$4"])          # the card is their sum

    def test_the_week_is_monday_to_sunday_night_new_york_time(self):
        self.bet(1, day="2026-09-28")                                         # Mon Sep 28, 7pm: the first day
        self.bet(2, start="2026-10-05T01:00:00Z")                             # Sun Oct 4, 9pm: the last night
        self.bet(3, start="2026-09-28T01:00:00Z", result="loss")              # Sun Sep 27, 9pm: last week's
        self.bet(4, start="2026-10-06T01:00:00Z", result="loss")              # Mon Oct 5, 9pm: next week's
        self.mark(1, day="2026-09-29", pct=-1.0)                              # sent Mon Sep 28, 9pm
        self.mark(2, day="2026-09-28", pct=5.0)                               # Sun Sep 27, 9pm: last week's
        self.mark(3, day="2026-10-05", pct=-3.0)                              # Sun Oct 4, 9pm
        self.arb("A1 @ B", 2, day="2026-09-29")                               # the same, for arbs
        self.arb("A2 @ B", 9, day="2026-09-28")
        self.arb("A3 @ B", 3, day="2026-10-05")
        held = {"2026-09-27": {"+EV": {"capped": 9}}, "2026-09-28": {"+EV": {"capped": 1}},
                "2026-10-04": {"+EV": {"capped": 2}}, "2026-10-05": {"+EV": {"capped": 5}}}
        title, text, profit = self.card(held)
        self.assertIn("📈 **+EV** · 2-0 · +$48 on $40 staked", text)
        self.assertIn("✋ Held back: 3 (3 over the hourly cap)", text)
        self.assertIn("• Live outliers: sent +22.0% → -2.0% ~3 min later (still there 0%) → too early to tell "
                      "(2 bets)", text)
        self.assertIn("💰 **Pre-game arbs** · 2 different · about +$5 locked in at $100 each (ROI +2.5%)", text)
        self.assertEqual(profit, 48)

    def test_every_line_works_with_its_file_missing_empty_or_broken(self):
        title, text, profit = self.card()                                     # no files at all
        for line in ("💰 **Pre-game arbs** · no alerts", "💰 **Live arbs** · no alerts", "📈 **+EV** · no graded bets yet",
                     "🚨 **Outliers** · no graded bets yet", "🎯 **Props** · no graded bets yet",
                     "📦 **Parlays** · no graded bets yet", "• Nothing to judge yet this week."):
            self.assertIn(line, text)
        for part in ("📐", "📏", "✋ Held back:"):                           # an empty type is one line
            self.assertNotIn(part, text)
        off = replace(self.cfg(), log_file="", ev_results_file="", closing_file="", markout_file="", state_dir="")
        self.assertEqual(self.card(cfg=off)[1], text)                        # files turned off: the same
        self.assertEqual(profit, 0)
        for name in ("log_file", "ev_log_file", "outlier_log_file", "ev_results_file", "closing_file", "markout_file"):
            Path(self.files[name]).write_text("")                             # empty
        Path(self.files["state_dir"]).mkdir()
        (Path(self.files["state_dir"]) / _arbbot.HELD_FILE).write_text("")
        self.assertEqual(self.card()[1], text)
        for name in ("log_file", "ev_results_file", "markout_file"):         # just a header
            Path(self.files[name]).write_text("first_seen,kind\n")
        self.assertEqual(self.card()[1], text)
        import contextlib, io
        self.bet(1)
        Path(self.files["markout_file"]).write_bytes(b"first_seen,kind\n\xff\xfe\x00,outlier\n")   # broken
        Path(self.files["ev_results_file"]).write_text("result,kind\nwin,ev\n")                     # no columns
        (Path(self.files["state_dir"]) / _arbbot.HELD_FILE).write_text("[not json")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            title, broken, _ = self.card()
        self.assertIn("💰 **Pre-game arbs** · no alerts", broken)               # the other parts still there
        self.assertIn("📐 CLV (pre-game): avg +3.4%", broken)
        self.assertIn(f"⚠️ Couldn't read {self.files['ev_results_file']}, {self.files['markout_file']}", broken)
        self.assertIn("! Weekly report card: couldn't read", err.getvalue())

    def clv(self, n, clv_pct, kind="ev", player="", fair_from="Pinnacle", beat=None):
        return [{"kind": kind, "player": player, "fair_from": fair_from, "clv_pct": clv_pct,
                 "beat_close": clv_pct > 0 if beat is None else beat} for _ in range(n)]

    def marks(self, pcts, kind="outlier"):
        return [{"kind": kind, "live": "True", "player": "", "markout_pct": p, "edge_pct": 22, "markout_secs": 170,
                 "still_ok": "0"} for p in pcts]

    def test_suggestions_only_when_the_existing_bars_say_so(self):
        cfg = Config()
        tips = lambda clv=(), marks=(): _arbbot.weekly_suggestions(cfg, list(clv), list(marks))
        # Markouts: MARKOUT_VERDICT_BETS (50) live bets, judged by the 📏 tables' ✅ / ⚠️.
        self.assertEqual(tips(marks=self.marks([-2.0, -1.0] * 24 + [-1.5])),
                         ["Live outliers: sent +22.0% → -1.5% ~3 min later (still there 0%) → too early to tell (49 bets)"])
        self.assertEqual(tips(marks=self.marks([-2.0, -1.0] * 30)),
                         ["Live outliers: 60 bets, sent +22.0% → -1.5% ~3 min later (still there 0%) → "
                          "consider OUTLIER_LIVE=false"])
        self.assertTrue(tips(marks=self.marks([2.0, 1.0] * 30))[0].endswith("→ keep"))
        self.assertTrue(tips(marks=self.marks([5.0, -5.0] * 30))[0].endswith("→ no clear signal yet: keep watching"))
        self.assertTrue(tips(marks=self.marks([-2.0, -1.0] * 30, kind="ev"))[0].startswith("Live +EV: 60 bets"))
        self.assertTrue(tips(marks=self.marks([-2.0, -1.0] * 30, kind="ev"))[0].endswith("consider EV_LIVE=false"))
        # CLV: "keep" from 5 bets beating the close (the daily summary's rule); a change only from 50.
        self.assertEqual(tips(self.clv(4, 3.0)), ["Pre-game +EV: CLV +3.0%, beat the close 100% → too early to tell (4 bets)"])
        self.assertEqual(tips(self.clv(5, 3.0)), ["Pre-game +EV: CLV +3.0%, beat the close 100% over 5 bets → keep"])
        self.assertEqual(tips(self.clv(5, 3.0, beat=False)),
                         ["Pre-game +EV: CLV +3.0%, beat the close 0% → too early to tell (5 bets)"])
        self.assertEqual(tips(self.clv(20, -2.0)), ["Pre-game +EV: CLV -2.0%, beat the close 0% → too early to tell (20 bets)"])
        self.assertEqual(tips(self.clv(60, -2.0)),
                         ["Pre-game +EV: CLV -2.0%, beat the close 0% over 60 bets → consider raising MIN_EV_PCT (now 4)"])
        self.assertEqual(tips(self.clv(60, -2.0, beat=True)),
                         ["Pre-game +EV: CLV -2.0%, beat the close 100% over 60 bets → no clear signal yet: keep watching"])
        half = lambda pct: self.clv(30, pct, beat=True) + self.clv(30, pct, beat=False)
        self.assertEqual(tips(half(-1.0)),                                  # exactly half: neither way
                         ["Pre-game +EV: CLV -1.0%, beat the close 50% over 60 bets → no clear signal yet: keep watching"])
        self.assertEqual(tips(half(1.0)),
                         ["Pre-game +EV: CLV +1.0%, beat the close 50% over 60 bets → no clear signal yet: keep watching"])
        self.assertEqual(tips(self.clv(60, 0.0)),                           # nor an average of exactly 0
                         ["Pre-game +EV: CLV +0.0%, beat the close 0% over 60 bets → no clear signal yet: keep watching"])
        self.assertEqual(tips(self.clv(60, -1.0, kind="outlier", fair_from="median of 4 other books")),
                         ["Pre-game outliers: CLV -1.0%, beat the close 0% over 60 bets → consider raising OUTLIER_MIN_PCT "
                          "(now 10)"])
        self.assertEqual(tips(self.clv(22, 3.1, player="LeBron James", fair_from="consensus of DraftKings, FanDuel")),
                         ["Props with no Pinnacle price: CLV +3.1%, beat the close 100% over 22 bets → keep"])
        self.assertEqual(tips(self.clv(60, -1.0, player="LeBron James")),
                         ["Props with a Pinnacle price: CLV -1.0%, beat the close 0% over 60 bets → consider raising "
                          "PROP_MIN_EV_PCT (now 7)"])
        self.assertEqual(tips(self.clv(60, -1.0, player="LeBron James", fair_from="consensus of DraftKings, FanDuel")),
                         ["Props with no Pinnacle price: CLV -1.0%, beat the close 0% over 60 bets → consider raising "
                          "PROP_MIN_BOOKS (now 4)"])

    def test_goes_out_once_across_restarts_and_processes(self):
        from arbbot import Results
        cfg = self.cfg(results_webhook_url="https://results.example")
        sent = []
        bot = Results(cfg, dry_run=False)
        bot.send = lambda payload: sent.append(payload["embeds"][0]["title"]) or True
        self.assertTrue(bot.weekly_due(self.MONDAY))
        self.assertEqual(bot.weekly(self.MONDAY), "sent")
        self.assertEqual(sent, ["📋 Weekly report card · Sep 28 – Oct 4"])
        restarted = Results(cfg, dry_run=False)
        self.assertFalse(restarted.weekly_due(self.MONDAY))                 # a restart doesn't send it again
        nxt = self.MONDAY + timedelta(days=7)
        self.assertTrue(restarted.weekly_due(nxt))
        cli = Results(cfg, dry_run=False)                                    # --post-weekly from another process
        cli.send = lambda payload: True
        self.assertEqual(cli.weekly(nxt), "sent")
        bot.weekly_tried = 0
        self.assertFalse(bot.weekly_due(nxt))                               # the bot sees it went out
        bot._save()                                                          # and keeps both weeks when it saves
        import json
        saved = json.loads((Path(cfg.state_dir) / "results_posted.json").read_text())
        self.assertEqual(saved["weekly_days"], [self.MONDAY.isoformat(), nxt.isoformat()])
        # Discord down: not marked, and not retried on every pass (as the daily recap).
        down = Results(cfg, dry_run=False)
        down.send = lambda payload: False
        later = nxt + timedelta(days=7)
        self.assertEqual(down.weekly(later), "failed")
        self.assertFalse(down.weekly_due(later))
        down.weekly_tried -= 3600
        self.assertTrue(down.weekly_due(later))

    def run_loop(self, day, cfg_changes=None, held=None):
        """One pass of run()'s loop at 10am New York time on `day` (summary at 9am)."""
        import argparse, contextlib, io
        from unittest import mock
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                t = datetime(day.year, day.month, day.day, 10, 0, tzinfo=ny)
                return t.astimezone(tz) if tz else t.replace(tzinfo=None)

        class Api:
            remaining, used = 50000.0, None
            def __init__(self, cfg):
                pass
            def events(self, sport, horizon_hours=26):
                return []
        cfg = replace(self.cfg(sports=["basketball_nba"], kalshi_check=False, summary_hour=9, results_minutes=0),
                      **(cfg_changes or {}))
        posted = []
        real_init = _arbbot.HeldTally.__init__

        def seeded(tally, cfg, path):   # what this run of the bot held back so far
            real_init(tally, cfg, path)
            tally.days.update(held or {})
        args = argparse.Namespace(once=False, demo=False, dry_run=True, plan=False, check_kalshi=False,
                                  results=None, post_results=None)
        with mock.patch("arbbot.OddsAPI", Api), mock.patch("arbbot.datetime", Clock), \
                mock.patch.object(_arbbot.Results, "send", lambda res, payload: posted.append(payload) or True), \
                mock.patch.object(_arbbot.Status, "send_card", lambda status, payload: None), \
                mock.patch("arbbot.time.sleep", side_effect=StopLoop), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(_arbbot.HeldTally, "__init__", seeded):
            with self.assertRaises(StopLoop):
                _arbbot.run(cfg, args, _arbbot.Status(cfg, dry_run=True))
        return [p["embeds"][0]["title"] for p in posted] if not held else posted

    def test_the_bot_posts_it_on_monday_at_summary_time(self):
        self.assertIn("📋 Weekly report card · Sep 28 – Oct 4", self.run_loop(self.MONDAY))
        sunday = self.MONDAY - timedelta(days=1)
        self.assertFalse([t for t in self.run_loop(sunday) if t.startswith("📋 Weekly")])
        self.assertFalse([t for t in self.run_loop(self.MONDAY, {"summary_hour": -1}) if t.startswith("📋 Weekly")])
        self.assertFalse([t for t in self.run_loop(self.MONDAY, {"summary_hour": 11}) if t.startswith("📋 Weekly")])
        # Its own count, so it's there even with STATE_DIR empty (nothing saved).
        [card] = [p for p in self.run_loop(self.MONDAY, {"state_dir": ""}, {"2026-10-02": {"Outliers": {"capped": 2}}})
                  if p["embeds"][0]["title"].startswith("📋 Weekly")]
        self.assertIn("✋ Held back: 2 (2 over the hourly cap)", card["embeds"][0]["description"])

    def main(self, *flags, webhook=None):
        import os, sys, contextlib, io
        from unittest import mock
        env = {k.upper(): v for k, v in self.files.items()}
        env.update(DISCORD_RESULTS_WEBHOOK_URL="https://results.example", ODDS_API_KEY="")
        loop = AssertionError("the main loop started")
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "argv", ["arbbot.py", *flags]), \
                mock.patch("arbbot.load_dotenv"), mock.patch("arbbot._webhook", side_effect=webhook or loop), \
                mock.patch.object(_arbbot.Scheduler, "refresh_events", side_effect=loop), \
                mock.patch("arbbot.time.sleep", side_effect=loop), contextlib.redirect_stdout(out):
            _arbbot.main()
        return out.getvalue()

    def test_the_command_line_flags(self):
        import json
        from zoneinfo import ZoneInfo
        self.bet(1, day=(datetime.now(ZoneInfo("America/New_York")) - timedelta(days=2)).date().isoformat())
        text = self.main("--weekly")                                         # free: no key, no grading
        self.assertIn("📋 Weekly report card · ", text)
        self.assertIn("📈 +EV · 1-0 · +$24 on $20 staked", text)            # plain text, no Discord bold
        self.assertFalse((Path(self.files["state_dir"]) / "results_posted.json").exists())
        posts = []
        out = self.main("--post-weekly", webhook=lambda url, payload, *a, **k: posts.append((url, payload)) or {"id": "1"})
        self.assertEqual([u for u, p in posts], ["https://results.example"])
        self.assertTrue(posts[0][1]["embeds"][0]["title"].startswith("📋 Weekly report card · "))
        self.assertIn("Posted the weekly report card", out)
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        saved = json.loads((Path(self.files["state_dir"]) / "results_posted.json").read_text())
        self.assertEqual(saved["weekly_days"], [today])                      # a Monday one won't go out twice


class HeldBackCount(MixFiles):
    """The weekly card's "held back": each alert the caps or live rules stopped, once a day."""

    def test_counts_each_alert_once_a_day_by_type(self):
        from arbbot import HeldTally, Arb, Leg
        path = Path(self.files["state_dir"]) / _arbbot.HELD_FILE
        tally = HeldTally(Config(), path)
        ev = ev_leg("g1", {"DK": 2.30})
        noon = NOW.timestamp()
        for secs in (0, 60, 905, 1810, 2715, 4 * 3600):                     # checks 15+ min apart, all day
            tally.add(ev, "capped", noon + secs)
        tally.add(ev, "live cap", noon)                                     # another reason
        tally.add(ev, "waiting", noon + 5000)                                # ends up sent or "unconfirmed"
        outlier = replace(ev, sharp_book="median of 4 other books")              # the same bet, as an outlier
        prop = replace(ev, line=("LeBron James", 25.5), market="player_points", event_id="g3")
        arb = Arb("g4", "NBA", "A @ H", "2026-10-03T18:00:00Z", True, "h2h", None, [Leg("H", 2.1, "A")], 0.98)
        parlay = _arbbot.Parlay("DK", [(ev, 2.3, "")])
        for item in (outlier, prop, arb, replace(arb, is_live=False, event_id="g5"), parlay):
            tally.add(item, "capped", noon)
        self.assertEqual(tally.days, {"2026-10-03": {"+EV": {"capped": 1, "live cap": 1}, "Outliers": {"capped": 1},
                                                     "Props": {"capped": 1}, "Live arbs": {"capped": 1},
                                                     "Pre-game arbs": {"capped": 1}, "Parlays": {"capped": 1}}})
        self.assertFalse(path.exists())
        tally.save()
        self.assertEqual(HeldTally(Config(), path).days, tally.days)         # survives a restart
        self.assertEqual(HeldTally(Config(), None).days, {})                 # --dry-run & co: memory only
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            tally.add(object(), "capped", noon)                              # a bad item: printed, skipped
        self.assertIn("Couldn't count a held-back alert", err.getvalue())
        for junk in ("{oops", "[1, 2]", '{"2026-10-03": 5}'):
            path.write_text(junk)
            self.assertEqual(HeldTally(Config(), path).days, {})             # unreadable: start again
        many = HeldTally(Config(), path)
        for day in range(20):                                                # 20 days, one alert each
            many.add(replace(ev, event_id=f"d{day}"), "capped", noon + day * 86400)
        many.save()
        self.assertEqual(sorted(_arbbot.read_held(path)), sorted(many.days))
        self.assertEqual(len(many.days), 15)                                 # the last 15 days are kept
        self.assertEqual(min(many.days), "2026-10-08")

    def test_once_a_day_by_the_local_day(self):
        from arbbot import HeldTally
        tally = HeldTally(Config(), None)
        ev = ev_leg("g1", {"DK": 2.30})
        at = lambda day, h, m=0: datetime(2026, 10, day, h, m, tzinfo=timezone.utc).timestamp()
        tally.add(ev, "capped", at(3, 2))                                   # 10pm Fri Oct 2 in New York
        self.assertEqual(tally.days, {"2026-10-02": {"+EV": {"capped": 1}}})
        tally.add(ev, "capped", at(3, 3, 59))                               # 11:59pm: the same day, once
        for m in (0, 16, 32, 48):                                           # from midnight: a new day
            tally.add(ev, "capped", at(3, 4, m))
        tally.add(ev, "capped", at(3, 3, 58))                               # a check from just before midnight
        tally.add(ev, "capped", at(3, 23, 30))
        self.assertEqual(tally.days, {"2026-10-02": {"+EV": {"capped": 1}}, "2026-10-03": {"+EV": {"capped": 1}}})

    def test_the_scan_counts_what_the_caps_hold_back_by_the_bet_not_the_alerter(self):
        cfg = self.cfg(Config(min_ev_pct=3, round_stakes=0, max_ev_per_hour=1, outliers_enabled=False,
                              kalshi_check=False))
        t = _arbbot.Trackers(cfg, self.args(dry_run=False))
        evs = []
        for gid in ("g1", "g2"):
            ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})
            ev.update(id=gid, sport_key="basketball_nba")
            evs.append(ev)
        for mins in (0, 16, 32, 48):                                          # pre-game checks 15+ min apart:
            fresh = (NOW + timedelta(minutes=mins)).isoformat()             # the 2nd bet is capped on each
            for ev in evs:
                for bk in ev["bookmakers"]:
                    bk["last_update"] = bk["markets"][0]["last_update"] = fresh
            _arbbot.scan_main(t, evs, ["basketball_nba"], NOW + timedelta(minutes=mins))
        self.assertEqual(len(t.evs.open), 1)
        self.assertEqual(t.held.days, {"2026-10-03": {"+EV": {"capped": 1}}})   # but counts once
        bet = next(op.arb for op in t.evs.open.values())
        t.outs.note_held(bet, "old price", NOW.timestamp())                  # the live rules note bets on outs
        self.assertEqual(t.held.days, {"2026-10-03": {"+EV": {"capped": 1, "old price": 1}}})
        t.held.save()
        self.assertEqual(_arbbot.read_held(Path(cfg.state_dir) / _arbbot.HELD_FILE), t.held.days)
        self.assertEqual([a.on_held for a in (*t.singles(), t.parlays)], [t.held.add] * 7)   # every alerter
        for flags in ({"dry_run": True}, {"once": True, "dry_run": False}, {"demo": True, "dry_run": False}):
            self.assertIsNone(_arbbot.Trackers(cfg, self.args(**flags)).held.path, flags)   # they write nothing



class HeldBackSaved(unittest.TestCase):
    setUp, tearDown, run_bot = Markouts.setUp, Markouts.tearDown, Markouts.run_bot

    def test_the_bot_saves_it_every_pass(self):
        self.run_bot({"max_arb_per_hour": 1})                                # the prop arb is over the cap
        today = datetime.now(_arbbot.ZoneInfo("America/New_York")).date().isoformat()
        self.assertEqual(_arbbot.read_held(self.d / "state" / _arbbot.HELD_FILE),
                         {today: {"Pre-game arbs": {"capped": 1}}})

    def test_a_dry_run_saves_nothing(self):
        self.run_bot({"max_arb_per_hour": 1}, dry_run=True)
        self.assertFalse((self.d / "state" / _arbbot.HELD_FILE).exists())


# --------------------------------------------------------------------------- after-tax arb view (TAX_RATE, off by default)

class AfterTax(unittest.TestCase):
    def arb(self, home, away, **cfg):
        ev = event({"A": [("h2h", [("Home", home, None), ("Away", away, None)])],
                    "B": [("h2h", [("Home", away, None), ("Away", home, None)])]})
        return find_arbs([ev], replace(CFG, **cfg), NOW)

    def test_off_by_default(self):
        [a] = self.arb(2.10, 1.80)
        self.assertEqual(a.tax, ())
        self.assertNotIn("After tax", discord_payload(a)["embeds"][0]["description"])

    def test_the_card_line(self):
        # $50 + $50 at 2.10: back $105. The winning $50 won $55, the losing $50 is deducted at 90% ($45):
        # $10 taxed at 33% = $3.30 off the $5 profit.
        [a] = self.arb(2.10, 1.80, tax_rate=0.33)
        self.assertAlmostEqual(a.tax[1], 1.70, 6)
        self.assertIn("\n🧾 After tax (~33%): about +$1.70 per $100 (rough guide, not tax advice)",
                      discord_payload(a)["embeds"][0]["description"])
        [thin] = self.arb(2.04, 1.80, tax_rate=0.33)                       # 2%: less than the tax on it
        self.assertIn("After tax (~33%): about −$0.31 per $100", discord_payload(thin)["embeds"][0]["description"])
        # Uneven prices: the worse of the two ways it can land (the long shot winning is taxed more).
        legs = event({"A": [("h2h", [("Home", 3.3, None), ("Away", 1.2, None)])],
                      "B": [("h2h", [("Home", 1.25, None), ("Away", 1.5, None)])]})
        [u] = find_arbs([legs], replace(CFG, tax_rate=0.33), NOW)
        self.assertEqual([l.outcome for l in u.legs], ["Away", "Home"])      # the long shot isn't first
        self.assertAlmostEqual(u.after_tax_pct(0.33), u.exact_pct - 0.33 * 10, 6)    # Home: $71.88 won - $61.88

    def test_the_floor_only_with_a_tax_rate(self):
        self.assertEqual(len(self.arb(2.10, 1.80, tax_rate=0.33, min_after_tax_pct=1)), 1)
        self.assertEqual(self.arb(2.04, 1.80, tax_rate=0.33, min_after_tax_pct=1), [])
        self.assertEqual(len(self.arb(2.04, 1.80, min_after_tax_pct=3)), 1)          # no TAX_RATE: no floor

    def test_settings(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"TAX_RATE": "0.33", "MIN_AFTER_TAX_PCT": "0.5"}):
            c = Config.from_env()
        self.assertEqual((c.tax_rate, c.min_after_tax_pct), (0.33, 0.5))
        with mock.patch.dict(os.environ, {"TAX_RATE": "33"}), self.assertRaises(ValueError) as e:
            Config.from_env()
        self.assertIn("TAX_RATE=33 should be a fraction like 0.33", str(e.exception))


# --------------------------------------------------------------------------- confirmed pre-game bets

AGREE = {"e1": {"Home": (0.505, 0.50, 0.51)}}   # Kalshi: Home 50.5% to win (Pinnacle's no-vig: 50.0%)


def confirm_event(bet=2.08, market="h2h", others=("C", "D", "E", "F"), other=(1.95, 1.95), pin=(1.95, 1.95),
                  start="2026-10-03T18:00:00Z", sport="icehockey_nhl", extra=None, gid="e1"):
    """Pinnacle (key 'pinnacle') at pin: a 50/50 game. Book B has the first side at `bet` (2.08: +4.0%),
    the other books are at `other` (extra: {title: prices} on top). A moneyline, a spread (Home -3.5)
    or a total (Over 5.5), six hours before the game (NOW)."""
    pt = {"h2h": (None, None), "spreads": (-3.5, 3.5), "totals": (5.5, 5.5)}[market]
    names = ("Over", "Under") if market == "totals" else ("Home", "Away")
    outs = lambda h, a: [(names[0], h, pt[0]), (names[1], a, pt[1])]
    books = {"Pinnacle": [(market, outs(*pin))], "B": [(market, outs(bet, 1.80))]}
    books.update({t: [(market, outs(*p))] for t, p in {**{t: other for t in others}, **(extra or {})}.items()})
    ev = event(books, start=start)
    ev["bookmakers"][0]["key"] = "pinnacle"
    ev.update(sport_key=sport, id=gid)
    return ev


class ConfirmedPreGame(MixFiles):
    """Locks: a pre-game moneyline, spread or total under 5% (down to 3.5%, college 4.5%) goes out only when
    a second sharp source agrees with Pinnacle's fair price, at high confidence, with every other check
    as it was. One over 5% is a normal bet, exactly as before."""

    def locks(self, base=None, **kw):
        return self.cfg(base or Config(round_stakes=0), **kw).with_mode()

    def evs(self, ev, cfg=None, kalshi=None, history=None):
        """(find_evs's bets, the near misses it counted)."""
        misses = {}
        bets = find_evs([ev], cfg or self.locks(), NOW, history=history, kalshi=kalshi, confirm_misses=misses)
        return bets, misses

    # ---- settings
    def test_locks_values_and_yours_win_only_when_stricter(self):
        import os
        from unittest import mock
        c = Config().with_mode()
        self.assertEqual((c.pregame_confirmed_ev_pct, c.kalshi_confirm_pts, c.consensus_confirm_pts), (3.5, 1.5, 1.5))
        self.assertEqual((c.confirmed_floor("icehockey_nhl"), c.confirmed_floor("americanfootball_ncaaf"),
                          c.confirmed_floor("basketball_ncaab")), (3.5, 4.5, 4.5))
        for mine, used in ((None, 3.5), (0, 0), (2.5, 3.5), (4.0, 4.0)):              # a higher edge, or 0 (off)
            self.assertEqual(Config(pregame_confirmed_ev_pct=mine).with_mode().pregame_confirmed_ev_pct, used, mine)
        self.assertEqual(Config(pregame_confirmed_ev_pct=0).with_mode().confirmed_floor("americanfootball_ncaaf"), 0)
        tight = Config(kalshi_confirm_pts=1.0, consensus_confirm_pts=1.25).with_mode()      # fewer points: stricter
        self.assertEqual((tight.kalshi_confirm_pts, tight.consensus_confirm_pts), (1.0, 1.25))
        loose = Config(kalshi_confirm_pts=3.0, consensus_confirm_pts=2.5).with_mode()
        self.assertEqual((loose.kalshi_confirm_pts, loose.consensus_confirm_pts), (1.5, 1.5))
        plain = Config(alert_mode="balanced", consensus_confirm_pts=3.0).with_mode()
        self.assertEqual((plain.pregame_confirmed_ev_pct, plain.consensus_confirm_pts), (0, 3.0))   # off outside locks
        self.assertEqual(plain.confirmed_floor("icehockey_nhl"), 0)
        self.assertEqual(Config(alert_mode="balanced", pregame_confirmed_ev_pct=3).with_mode().pregame_confirmed_ev_pct, 3)
        env = {"ALERT_MODE": "locks", "PREGAME_CONFIRMED_EV_PCT": "", "KALSHI_CONFIRM_PTS": "1", "CONSENSUS_CONFIRM_PTS": ""}
        with mock.patch.dict(os.environ, env):
            e = Config.from_env()
        self.assertEqual((e.pregame_confirmed_ev_pct, e.kalshi_confirm_pts, e.consensus_confirm_pts), (3.5, 1.0, 1.5))
        with mock.patch.dict(os.environ, {**env, "CONSENSUS_CONFIRM_PTS": "1.25"}):
            self.assertEqual(Config.from_env().consensus_confirm_pts, 1.25)
        with mock.patch.dict(os.environ, {**env, "PREGAME_CONFIRMED_EV_PCT": "4.25"}):
            self.assertEqual(Config.from_env().pregame_confirmed_ev_pct, 4.25)
        with mock.patch.dict(os.environ, {**env, "ALERT_MODE": "balanced"}):
            self.assertEqual(Config.from_env().pregame_confirmed_ev_pct, 0)
        for key, bad, says in (("PREGAME_CONFIRMED_EV_PCT", "-1", "should be 0 (off) or an edge like 3.5"),
                               ("KALSHI_CONFIRM_PTS", "-0.5", "should be 0 or more"),
                               ("CONSENSUS_CONFIRM_PTS", "-2", "should be 0 or more")):
            with mock.patch.dict(os.environ, {key: bad}), self.assertRaises(ValueError) as ex:
                Config.from_env()
            self.assertIn(f"{key}={bad} {says}", str(ex.exception))
        self.assertIn("+EV 5%+ high-confidence only (pre-game 3.5%+ when Kalshi or 4+ other books agree with "
                      "Pinnacle), props", _arbbot.mode_line(c))
        self.assertIn("+EV 5%+ high-confidence only, props",
                      _arbbot.mode_line(Config(pregame_confirmed_ev_pct=0).with_mode()))

    # ---- moneylines: Kalshi must agree
    def test_a_4_pct_moneyline_with_kalshi_agreeing_alerts_in_locks(self):
        [b], misses = self.evs(confirm_event(), kalshi=AGREE)
        self.assertEqual((b.book, b.pick, round(b.ev_pct, 2), b.confidence), ("B", "Home ML", 4.0, "high"))
        self.assertEqual((b.tier, b.confirm[:3], round(b.confirm[3], 2), misses), ("confirmed", ("Kalshi", 0.505, 5.0), 4.0, {}))
        self.assertEqual(b.stake, kelly_stake(0.5, 2.08, self.locks()))     # normal Kelly: the smaller edge makes it
        self.assertEqual(b.stake, 9.0)                                       # smaller (a 5.5% bet: $12)
        card = ev_payload(b)["embeds"][0]
        self.assertEqual(card["title"], "📈 +EV 4.0% ✅✅ · Home ML +108 at B")
        self.assertIn("Confidence: **🟢 High** · tight sharp market (2.6% margin), other books agree, Kalshi agrees\n\n"
                      "**✅✅ Two sharp books agree: Pinnacle and Kalshi**\n"
                      "A smaller edge than the usual 5% is OK here: both give it almost the same chance to win "
                      "(Pinnacle 50.0%, Kalshi 50.5%). At least +4.0% by both.", card["description"])
        self.assertIn("\n  ✅✅ Two sharp books agree: Pinnacle and Kalshi. A smaller edge than the usual 5%",
                      _arbbot.format_ev_text(b))
        self.assertEqual(self.evs(confirm_event(), self.locks(my_books="c,d"), kalshi=AGREE), ([], {}))  # still your books only
        # Just 1.5 points apart is close enough.
        self.assertEqual(self.evs(confirm_event(), kalshi={"e1": {"Home": (0.515, 0.51, 0.52)}})[0][0].tier, "confirmed")

    def test_kalshi_2_5_points_off_does_not_alert(self):
        off = {"e1": {"Home": (0.525, 0.52, 0.53)}}                    # the usual Kalshi check allows 3 points
        self.assertEqual(self.evs(confirm_event(), kalshi=off), ([], {"Kalshi too far off": 1}))
        self.assertEqual(self.evs(confirm_event(), kalshi={"e1": {"Home": (0.535, 0.53, 0.54)}}),
                         ([], {"Kalshi too far off": 1}))               # (past the usual check: counted the same)
        self.assertEqual(self.evs(confirm_event(), kalshi={"e1": {"Home": (0.475, 0.47, 0.48)}}),
                         ([], {"Kalshi says the price isn't good": 1}))
        self.assertEqual(self.evs(confirm_event(), self.locks(kalshi_confirm_pts=1.0),
                                  kalshi={"e1": {"Home": (0.515, 0.51, 0.52)}}), ([], {"Kalshi too far off": 1}))
        loose = self.cfg(Config(alert_mode="balanced", min_ev_pct=5, pregame_confirmed_ev_pct=3.5, kalshi_confirm_pts=3,
                                min_confidence="high", round_stakes=0)).with_mode()
        self.assertEqual(self.evs(confirm_event(), loose, kalshi=off)[0][0].tier, "confirmed")   # KALSHI_CONFIRM_PTS=3

    def test_no_kalshi_price_does_not_alert(self):
        self.assertEqual(self.evs(confirm_event()), ([], {"no Kalshi price": 1}))
        self.assertEqual(self.evs(confirm_event(), kalshi={"e1": {"Away": (0.495, 0.49, 0.50)}}),
                         ([], {"no Kalshi price": 1}))
        at_kalshi = confirm_event()
        at_kalshi["bookmakers"][1].update(key="kalshi", title="Kalshi")      # Kalshi's own quote can't confirm it
        self.assertEqual(self.evs(at_kalshi, kalshi={"e1": {"Home": (0.455, 0.45, 0.46)}}), ([], {"bet is at Kalshi": 1}))

    # ---- spreads and totals: 4+ other sportsbooks must agree
    def test_a_4_pct_spread_with_a_tight_4_book_consensus_alerts(self):
        [b], misses = self.evs(confirm_event(market="spreads"))
        self.assertEqual((b.pick, b.tier, b.confirm[:3], b.confidence, misses),
                         ("Home -3.5", "confirmed", ("the other books", 0.5, 5.0), "high", {}))
        self.assertIn("**✅✅ Two sharp books agree: Pinnacle and the other books**\nA smaller edge than the usual 5% "
                      "is OK here: both give it almost the same chance to win (Pinnacle 50.0%, the other books 50.0%). "
                      "At least +4.0% by both.", ev_payload(b)["embeds"][0]["description"])
        [t], _ = self.evs(confirm_event(market="totals"))
        self.assertEqual((t.pick, t.tier), ("Over 5.5", "confirmed"))
        both = confirm_event(market="spreads", others=("C", "D", "E", "betonlineag", "lowvig"))
        self.assertEqual(self.evs(both)[0][0].tier, "confirmed")          # BetOnline.ag and LowVig.ag: one of four

    def test_a_3_book_consensus_does_not_alert(self):
        three = confirm_event(market="spreads", others=("C", "D", "E"))
        self.assertEqual(self.evs(three), ([], {"under 4 other sportsbooks": 1}))    # B itself never counts
        for others in (("C", "D", "E", "novig"),                     # an exchange isn't a sportsbook
                       ("C", "D", "betonlineag", "lowvig")):         # sister books are one
            self.assertEqual(self.evs(confirm_event(market="spreads", others=others)),
                             ([], {"under 4 other sportsbooks": 1}), others)
        far = confirm_event(market="spreads", other=(1.88, 2.02))         # four, but 1.9 points from Pinnacle
        self.assertEqual(self.evs(far), ([], {"other books too far off": 1}))
        loose = self.cfg(Config(alert_mode="balanced", min_ev_pct=5, pregame_confirmed_ev_pct=3.5,
                                consensus_confirm_pts=2, min_confidence="high", round_stakes=0)).with_mode()
        self.assertEqual(self.evs(far, loose)[0][0].tier, "confirmed")     # CONSENSUS_CONFIRM_PTS=2

    def test_a_lopsided_spread_or_total_is_judged_on_its_own_side(self):
        for market, pick in (("spreads", "Home -3.5"), ("totals", "Over 5.5")):
            # Pinnacle: 57.3% (1.70/2.25), the other books 56.8% (1.71/2.22); B's 1.83 is +4.9% by Pinnacle.
            [b], misses = self.evs(confirm_event(market=market, pin=(1.70, 2.25), other=(1.71, 2.22), bet=1.83))
            self.assertEqual((b.pick, b.tier, b.confirm[0], round(b.confirm[1], 3), misses),
                             (pick, "confirmed", "the other books", 0.568, {}), market)
            self.assertIn("both give it almost the same chance to win (Pinnacle 57.3%, the other books 56.8%). "
                          "At least +4.0% by both.", ev_payload(b)["embeds"][0]["description"])
        # Pinnacle has Home at 51.3% (1.90/2.00) and the other books at 48.7%: only the Away side's 51.3%
        # would match, and that's the other side.
        mirrored = confirm_event(market="spreads", pin=(1.90, 2.00), other=(2.00, 1.90), bet=2.03)
        self.assertEqual(self.evs(mirrored), ([], {"other books too far off": 1}))

    # ---- the game: before it starts, within a day
    def test_a_live_game_does_not_alert(self):
        cfg = self.locks(ev_live=True)
        self.assertEqual(self.evs(confirm_event(market="spreads", start=STARTED), cfg), ([], {}))
        [five], _ = self.evs(confirm_event(market="spreads", start=STARTED, bet=2.11), cfg)   # 5.5%: as before
        self.assertEqual((five.tier, five.is_live), ("", True))

    def test_a_game_30_hours_out_does_not_alert(self):
        far = "2026-10-04T18:00:00Z"
        self.assertEqual(self.evs(confirm_event(market="spreads", start=far)), ([], {}))
        [five], _ = self.evs(confirm_event(market="spreads", start=far, bet=2.11))           # high even so
        self.assertEqual((five.tier, five.confidence), ("", "high"))
        [near], _ = self.evs(confirm_event(market="spreads", start="2026-10-04T11:00:00Z"))  # 23 hours: fine
        self.assertEqual(near.tier, "confirmed")
        self.assertEqual(self.evs(confirm_event(market="spreads", start="2026-10-04T11:00:00Z"),
                                  self.locks(confident_hours=12)), ([], {}))                 # CONFIDENT_HOURS

    def test_a_sharp_line_moving_against_it_does_not_alert(self):
        h = _arbbot.SharpHistory(60)
        h.record(("e1", "h2h", None, "Home"), NOW - timedelta(minutes=20), 0.52)   # Pinnacle had Home at 52%
        self.assertEqual(self.evs(confirm_event(), kalshi=AGREE, history=h), ([], {"sharp line moving against it": 1}))
        toward = _arbbot.SharpHistory(60)
        toward.record(("e1", "h2h", None, "Home"), NOW - timedelta(minutes=20), 0.48)
        [b], _ = self.evs(confirm_event(), kalshi=AGREE, history=toward)            # toward the bet: fine
        self.assertEqual((b.tier, b.confidence_notes[0]), ("confirmed", "sharp line moving this way (+2.0 pts)"))
        small = _arbbot.SharpHistory(60)
        small.record(("e1", "h2h", None, "Home"), NOW - timedelta(minutes=20), 0.51)   # 1 point: not a move
        self.assertEqual(self.evs(confirm_event(), kalshi=AGREE, history=small)[0][0].tier, "confirmed")

    def look(self, h, mins, pin=(1.95, 1.95), bet=2.08, kalshi=AGREE):
        """One check `mins` minutes from NOW with a shared sharp history, pruned after it as each check
        does: (bets, near misses)."""
        misses = {}
        bets = find_evs([stamped(confirm_event(pin=pin, bet=bet), mins * 60)], self.locks(), at(mins * 60),
                        history=h, kalshi=kalshi, confirm_misses=misses)
        h.prune(at(mins * 60))
        return bets, misses

    def test_checks_an_hour_or_more_apart_still_see_the_sharp_line_move(self):
        moved = {"e1": {"Home": (0.524, 0.52, 0.53)}}
        for gap in (61, 75, 120, 180):           # games 2-24 hours out are checked about once an hour
            h = _arbbot.SharpHistory(60)
            self.look(h, -gap, pin=(1.86, 2.04), bet=1.90, kalshi=moved)       # Pinnacle: Home 52.4%
            self.assertEqual(self.look(h, 0), ([], {"sharp line moving against it": 1}), gap)   # now 50.0%
        h = _arbbot.SharpHistory(60)
        self.look(h, -61)                                                      # it didn't move: fine
        self.assertEqual(self.look(h, 0)[0][0].tier, "confirmed")
        # Checks every 15 minutes, the line still for over an hour, then a move: it waits out the hour.
        h = _arbbot.SharpHistory(60)
        for mins in range(-75, 0, 15):
            self.look(h, mins, pin=(1.86, 2.04), bet=1.90, kalshi=moved)
        for mins in (0, 15, 30, 45):
            self.assertEqual(self.look(h, mins), ([], {"sharp line moving against it": 1}), mins)
        self.assertEqual(self.look(h, 60)[0][0].tier, "confirmed")              # an hour after the move
        # Checked hourly, the line still for 4 hours, then a move: the price seen an hour ago counts.
        h = _arbbot.SharpHistory(60)
        for mins in range(-240, 0, 60):
            self.look(h, mins, pin=(1.86, 2.04), bet=1.90, kalshi=moved)
        self.assertEqual(self.look(h, 0), ([], {"sharp line moving against it": 1}))

    def test_the_first_look_at_a_line_waits_one_check(self):
        h = _arbbot.SharpHistory(60)
        self.assertEqual(self.look(h, 0), ([], {"no sharp history yet": 1}))     # after a restart, or a new line
        [b], misses = self.look(h, 15)                                          # the next look, unchanged
        self.assertEqual((b.tier, misses), ("confirmed", {}))
        h = _arbbot.SharpHistory(60)
        self.look(h, 0)
        self.assertEqual(self.look(h, 61)[0][0].tier, "confirmed")              # an hour apart: still fine
        self.assertEqual(self.look(h, 61 + 181), ([], {"no sharp history yet": 1}))   # over 3 hours: too old
        self.assertEqual(self.evs(confirm_event(), kalshi=AGREE)[0][0].tier, "confirmed")   # (no history kept)

    def test_college_needs_4_5(self):
        ncaa = lambda bet: confirm_event(bet=bet, sport="americanfootball_ncaaf")
        self.assertEqual(self.evs(ncaa(2.08), kalshi=AGREE), ([], {}))                     # 4.0%
        [b], _ = self.evs(ncaa(2.094), kalshi=AGREE)                                      # 4.7%
        self.assertEqual((b.tier, b.confirm[:3], round(b.confirm[3], 2)), ("confirmed", ("Kalshi", 0.505, 6.0), 4.7))
        self.assertIn("A smaller edge than the usual 6% is OK here", ev_payload(b)["embeds"][0]["description"])

    def test_the_edge_must_clear_the_floor_by_both_sharp_sources(self):
        low = {"e1": {"Home": (0.49, 0.48, 0.50)}}           # 1 point from Pinnacle's 50%, but +1.9% by its 49%
        self.assertEqual(self.evs(confirm_event(), kalshi=low), ([], {"under 3.5% by both": 1}))
        ok = {"e1": {"Home": (0.498, 0.49, 0.505)}}           # +3.6% by Kalshi's 49.8%
        [b], misses = self.evs(confirm_event(extra={"G": (2.075, 1.80)}), kalshi=ok)
        self.assertEqual((b.book, b.tier, round(b.confirm[3], 2), misses), ("B", "confirmed", 3.58, {}))
        self.assertIn("(Pinnacle 50.0%, Kalshi 49.8%). At least +3.6% by both.", ev_payload(b)["embeds"][0]["description"])
        self.assertEqual(b.also, [])                          # G: +3.75% by Pinnacle, +3.3% by Kalshi: not listed
        [c], _ = self.evs(confirm_event(extra={"G": (2.075, 1.80)}), kalshi=AGREE)
        self.assertEqual(c.also, [("G", 2.075)])              # (Pinnacle's 50% is the lower one: +3.75%)
        # Spreads: the other books' 48.9% (1.99/1.91) is within 1.5 points, but +1.8% by it.
        self.assertEqual(self.evs(confirm_event(market="spreads", other=(1.99, 1.91))), ([], {"under 3.5% by both": 1}))
        ncaa = confirm_event(bet=2.094, sport="americanfootball_ncaaf")                  # +4.7% by Pinnacle
        self.assertEqual(self.evs(ncaa, kalshi={"e1": {"Home": (0.497, 0.49, 0.505)}}),   # +4.1% by Kalshi
                         ([], {"under 4.5% by both": 1}))

    def test_props_and_outliers_are_unaffected(self):
        def prop(bet):
            ev = prop_event({"Pinnacle": (1.95, 1.95), "B": (bet, 1.80), "C": (1.95, 1.95), "D": (1.95, 1.95),
                             "E": (1.95, 1.95), "F": (1.95, 1.95)})
            ev["bookmakers"][0]["key"] = "pinnacle"
            return ev
        self.assertEqual(self.evs(prop(2.08)), ([], {}))                        # 4%, four books agree: no
        [five], _ = self.evs(prop(2.11))
        self.assertEqual((five.pick, five.tier), ("LeBron James Over 25.5 Points", ""))
        self.assertEqual(self.evs(prop(2.08), self.locks().for_props()), ([], {}))
        alt = confirm_event(market="spreads")
        for bm in alt["bookmakers"]:
            bm["markets"][0]["key"] = "alternate_spreads"                        # main lines only
        self.assertEqual(self.evs(alt), ([], {}))
        for start in ("2026-10-03T11:00:00Z", "2026-10-03T18:00:00Z"):
            ev = outlier_event(1.85, 1.95, start=start)
            self.assertEqual([(o.book, o.price) for o in find_outliers([ev], self.locks(), NOW)],
                             [(o.book, o.price) for o in find_outliers([ev], self.locks(pregame_confirmed_ev_pct=0), NOW)])
        self.assertEqual(find_outliers([confirm_event(market="spreads")], self.locks(), NOW), [])

    def test_a_medium_confidence_4_pct_bet_never_alerts(self):
        medium = confirm_event(pin=(1.87, 1.87), other=(1.75, 2.0))      # a wide Pinnacle market, the books 3.5 pts off
        self.assertEqual(self.evs(medium, kalshi=AGREE), ([], {"not high confidence": 1}))
        loose = self.cfg(Config(alert_mode="balanced", min_ev_pct=5, pregame_confirmed_ev_pct=3.5,
                                round_stakes=0)).with_mode()                # MIN_CONFIDENCE=low
        self.assertEqual(self.evs(medium, loose, kalshi=AGREE), ([], {"not high confidence": 1}))
        [five], _ = self.evs(confirm_event(pin=(1.87, 1.87), other=(1.75, 2.0), bet=2.11), loose, kalshi=AGREE)
        self.assertEqual((five.tier, five.confidence), ("", "medium"))       # a normal bet: MIN_CONFIDENCE decides

    def test_the_5_pct_path_is_unchanged(self):
        ev = confirm_event(bet=2.11, extra={"G": (2.08, 1.80)})              # B: 5.5%, G: 4.0%
        off = {"e1": {"Home": (0.525, 0.52, 0.53)}}                          # Kalshi 2.5 points away: no matter
        [b], misses = self.evs(ev, kalshi=off)
        self.assertEqual((b.book, b.tier, b.confirm, b.also, misses), ("B", "", (), [], {}))   # G's 4% isn't listed
        [before] = find_evs([ev], self.locks(pregame_confirmed_ev_pct=0), NOW, kalshi=off)
        card = lambda x: {k: v for k, v in ev_payload(x)["embeds"][0].items() if k != "timestamp"}
        self.assertEqual(card(b), card(before))
        self.assertEqual(_arbbot.format_ev_text(b), _arbbot.format_ev_text(before))
        self.assertNotIn("✅✅", str(ev_payload(b)))
        [c], misses = self.evs(ev)                                             # no Kalshi price at all
        self.assertEqual((c.tier, misses), ("", {}))
        [d], _ = self.evs(confirm_event(bet=2.11), kalshi=AGREE)              # agreeing too: still a normal bet
        self.assertEqual((d.tier, d.stake), ("", 12.0))

    def test_off_and_outside_locks(self):
        self.assertEqual(self.evs(confirm_event(), self.locks(pregame_confirmed_ev_pct=0), kalshi=AGREE), ([], {}))
        self.assertEqual(self.evs(confirm_event(), self.locks(pregame_confirmed_ev_pct=4.5), kalshi=AGREE), ([], {}))
        over = self.locks(pregame_confirmed_ev_pct=6)                           # over MIN_EV_PCT: nothing changes
        self.assertEqual(self.evs(confirm_event(), over, kalshi=AGREE), ([], {}))
        self.assertEqual(self.evs(confirm_event(bet=2.11), over)[0][0].tier, "")
        plain = self.cfg(Config(alert_mode="balanced", min_ev_pct=5, min_confidence="high", round_stakes=0)).with_mode()
        self.assertEqual(self.evs(confirm_event(), plain, kalshi=AGREE), ([], {}))
        self.assertEqual(self.evs(confirm_event(), replace(plain, pregame_confirmed_ev_pct=3.5), kalshi=AGREE)[0][0].tier,
                         "confirmed")
        self.assertEqual(find_evs([confirm_event()], Config(min_ev_pct=5), NOW, kalshi=AGREE), [])   # no mode applied

    # ---- the caps, the logs and the reports
    def test_it_counts_toward_the_hourly_cap_after_the_5_pct_ones(self):
        a = EVAlerter(self.locks(), dry_run=True)
        a.max_per_hour = 1
        [four], _ = self.evs(confirm_event(), kalshi=AGREE)
        [five], _ = self.evs(confirm_event(bet=2.11, gid="e2"))
        self.assertEqual(a.handle([four, five], now=1000), 1)
        self.assertEqual([op.arb.event_id for op in a.open.values()], ["e2"])
        self.assertEqual(a.held_counts, {"capped": 1})

    def test_logged_with_its_tier_and_followed_by_markouts(self):
        from arbbot import EV_LOG_FIELDS, MarkoutTracker
        cfg = self.locks()
        old = {f: "" for f in EV_LOG_FIELDS[:-1]}
        append_csv(cfg.ev_log_file, EV_LOG_FIELDS[:-1], {**old, "event_id": "old", "market": "h2h"})   # before `tier`
        a = EVAlerter(cfg, dry_run=True)
        [four], _ = self.evs(confirm_event(), kalshi=AGREE)
        [five], _ = self.evs(confirm_event(bet=2.11, gid="e2"))
        a.handle([four, five], now=1000)
        rows = _read(cfg.ev_log_file)
        self.assertEqual(list(rows[0])[-1], "tier")
        self.assertEqual([(r["event_id"], r["tier"]) for r in rows], [("old", ""), ("e2", ""), ("e1", "confirmed")])
        mk = MarkoutTracker(cfg)
        mk.add(four, NOW.timestamp(), "ev")
        mk.add(five, NOW.timestamp(), "ev")
        self.assertEqual(mk.finalize(datetime(2026, 10, 3, 18, 1, tzinfo=timezone.utc)), 2)   # the game started
        self.assertEqual([(r["bet_id"], r["tier"]) for r in _read(cfg.markout_file)],
                         [("e1|h2h|Home|", "confirmed"), ("e2|h2h|Home|", "")])

    def logged(self, i, tier, close=0.49, start="2026-10-03T18:00:00Z"):
        from arbbot import EV_LOG_FIELDS, CLOSING_FIELDS
        r = {"first_seen": "2026-10-03T12:00:00+00:00", "event_id": f"g{i}", "sport": "NHL",
             "sport_key": "icehockey_nhl", "matchup": "A @ H", "home_team": "H", "away_team": "A",
             "commence_time": start, "live": False, "market": "h2h", "outcome": "H", "point": "", "n_outcomes": 2,
             "book": "DraftKings", "price": 2.08, "fair_odds": 2.0, "best_ev_pct": 4, "stake": 10, "player": "",
             "confidence": "high", "fair_from": "Pinnacle", "tier": tier}
        append_csv(self.files["ev_log_file"], EV_LOG_FIELDS, r)
        append_csv(self.files["closing_file"], CLOSING_FIELDS,
                   {"bet_id": _arbbot._bet_id(r), "closing_fair_prob": close, "closing_fair_odds": 1 / close})
        return r

    def test_clv_shows_them_as_their_own_group(self):
        for i, tier in enumerate(["confirmed", "confirmed", ""]):
            self.logged(i, tier)
        cfg = self.locks()
        self.assertEqual([(name, n) for name, n, *_ in _arbbot.clv_breakdown(cfg)["Bet type"]],
                         [("Confirmed pre-game (3.5%+)", 2), ("+EV", 1)])
        self.assertIn("    Confirmed pre-game (3.5%+)    2 bets   CLV  +1.9%   beat close 100%", _arbbot.clv_report(cfg))
        self.assertEqual(_arbbot.clv_breakdown(self.locks(pregame_confirmed_ev_pct=4))["Bet type"][0][0],
                         "Confirmed pre-game (4%+)")
        board = _arbbot.scoreboard_text(cfg, NOW)
        self.assertIn("\n  Confirmed pre-game (3.5%+)   2   +1.9%  100%\n", board)             # not cut short
        self.assertIn(f"```\n{'':30}bets   CLV  beat\nBet type\n", board)
        self.assertNotIn("Confirmed pre-ga\n", board)

    def week_bet(self, i, tier="", result="win", close=0.51):
        from arbbot import RESULT_FIELDS
        r = self.logged(i, tier, close, start="2026-10-01T23:00:00Z")
        append_csv(self.files["ev_results_file"], RESULT_FIELDS,
                   {**r, "kind": "ev", "result": result, "profit": 10.8 if result == "win" else -10})

    def test_the_weekly_card_shows_them_with_their_own_suggestion(self):
        for i, (tier, result) in enumerate([("confirmed", "win"), ("confirmed", "loss"), ("confirmed", "win"),
                                            ("", "win")]):
            self.week_bet(i, tier, result)
        title, text, _ = _arbbot.weekly_text(self.locks(), datetime(2026, 10, 5).date(), {})
        self.assertIn("📈 **+EV** · 3-1 · +$22.40 on $40 staked (ROI +56.0%)\n"
                      "📐 CLV (pre-game): avg +6.1%, beat the close on 100% of 4 bets\n"
                      "✅✅ Confirmed pre-game (3.5%+): 2-1 · +$11.60 on $30 staked (ROI +38.7%) · "
                      "CLV avg +6.1%, beat the close on 100% of 3 bets\n📏", text)
        self.assertIn("• Pre-game +EV: CLV +6.1%, beat the close 100% → too early to tell (1 bet)\n"
                      "• Confirmed pre-game (3.5%+): CLV +6.1%, beat the close 100% → too early to tell (3 bets)", text)
        tips = lambda clv: _arbbot.weekly_suggestions(self.locks(), clv, [])
        row = lambda tier, pct: {"kind": "ev", "player": "", "fair_from": "Pinnacle", "tier": tier, "clv_pct": pct,
                                 "beat_close": pct > 0}
        self.assertEqual(tips([row("confirmed", -2.0)] * 60 + [row("", 3.0)] * 5),
                         ["Pre-game +EV: CLV +3.0%, beat the close 100% over 5 bets → keep",
                          "Confirmed pre-game (3.5%+): CLV -2.0%, beat the close 0% over 60 bets → consider "
                          "PREGAME_CONFIRMED_EV_PCT=0"])
        self.assertEqual(tips([row("confirmed", -2.0)] * 49),
                         ["Confirmed pre-game (3.5%+): CLV -2.0%, beat the close 0% → too early to tell (49 bets)"])
        self.assertEqual(tips([row("confirmed", 1.0)] * 60),
                         ["Confirmed pre-game (3.5%+): CLV +1.0%, beat the close 100% over 60 bets → keep"])

    def test_the_console_says_what_went_out_and_what_missed(self):
        self.assertEqual(_arbbot.confirmed_text(0, {}), "")
        self.assertEqual(_arbbot.confirmed_text(1, {}), " | ✅✅ confirmed pre-game: 1 sent")
        self.assertEqual(_arbbot.confirmed_text(0, {"no Kalshi price": 2, "not high confidence": 3, "Kalshi too far off": 2}),
                         " | ✅✅ confirmed pre-game: 0 sent, 7 near misses (3 not high confidence, 2 Kalshi too far off, "
                         "2 no Kalshi price)")
        t = _arbbot.Trackers(self.locks(), self.args())
        games = [confirm_event(market="spreads"), confirm_event(gid="e2"), confirm_event(bet=2.11, gid="e3")]
        check = lambda mins: _arbbot.scan_main(t, [stamped(g, mins * 60) for g in games], ["icehockey_nhl"],
                                               at(mins * 60), kalshi=False)
        first = check(-15)                                                     # the bot's first look at them
        self.assertEqual((first.ev_sent, first.confirmed_sent, first.confirm_misses),
                         (1, 0, {"no Kalshi price": 1, "no sharp history yet": 1}))
        self.assertEqual(_arbbot.confirmed_text(first.confirmed_sent, first.confirm_misses),
                         " | ✅✅ confirmed pre-game: 0 sent, 2 near misses (1 no Kalshi price, 1 no sharp history yet)")
        res = check(0)
        self.assertEqual((res.ev_sent, res.confirmed_sent, res.confirm_misses), (1, 1, {"no Kalshi price": 1}))
        again = check(15)
        self.assertEqual((again.ev_sent, again.confirmed_sent), (0, 0))       # already up: not sent again

    def wired(self, sent):
        """Locks Trackers whose alerters post to a fake Discord (with @here pings), into sent."""
        t = _arbbot.Trackers(self.locks(webhook_url="https://x", ev_mention="@here"), self.args(dry_run=False))
        for a in (t.arbs, t.outs, t.evs):
            def fake(payload, message_id=None, url=""):
                sent.append(("PATCH" if message_id else "POST", payload["embeds"][0]["title"],
                             payload.get("content", "")))
                return message_id or f"m{len(sent)}"
            a._discord = fake
        return t

    @staticmethod
    def scan(t, games, mins, kalshi):
        """One main-line check `mins` minutes from NOW, Kalshi's quotes as given."""
        from unittest import mock
        with mock.patch("arbbot.kalshi_fair", return_value=kalshi):
            return _arbbot.scan_main(t, [stamped(g, mins * 60) for g in games], ["icehockey_nhl"], at(mins * 60))

    def test_a_card_up_stays_quietly_while_kalshi_is_only_missing(self):
        sent = []
        t = self.wired(sent)
        games = [confirm_event(), confirm_event(gid="e2")]           # e2: Kalshi never has it
        self.scan(t, games, -30, AGREE)                                  # (the first look waits)
        res = self.scan(t, games, -15, AGREE)
        self.assertEqual((res.confirmed_sent, sent), (1, [("POST", "📈 +EV 4.0% ✅✅ · Home ML +108 at B", "@here")]))
        del sent[:]
        blip = self.scan(t, games, 0, {})                              # Kalshi too slow, or too thin, this once
        self.assertEqual((blip.ev_sent, blip.confirmed_sent, blip.confirm_misses), (0, 0, {"no Kalshi price": 1}))
        self.assertEqual([(m, title) for m, title, _ in sent], [("PATCH", "📈 +EV 4.0% ✅✅ · Home ML +108 at B")])
        [op] = t.evs.open.values()
        self.assertTrue(op.arb.kept)                                     # (no new ping for a better price either)
        self.assertIn("**✅✅ Two sharp books agree: Pinnacle and Kalshi**\nA smaller edge than the usual 5% is OK here: "
                      "both gave it almost the same chance to win when it went out. Kalshi has no price right now "
                      "(Pinnacle 50.0%).", ev_payload(op.arb)["embeds"][0]["description"])
        back = self.scan(t, games, 15, AGREE)
        self.assertEqual((back.ev_sent, back.confirmed_sent), (0, 0))
        self.assertEqual([m for m, *_ in sent], ["PATCH", "PATCH"])              # edits only: no GONE, no ping
        self.assertIn("(Pinnacle 50.0%, Kalshi 50.5%)", ev_payload(t.evs.open[op.arb.key].arb)["embeds"][0]["description"])
        self.assertFalse(t.evs.open[op.arb.key].arb.kept)
        self.assertEqual([r["event_id"] for r in _read(self.files["ev_log_file"])], ["e1"])   # logged once
        # Kalshi disagreeing is another matter: the card closes.
        del sent[:]
        self.scan(t, games, 30, {"e1": {"Home": (0.525, 0.52, 0.53)}})
        self.assertEqual([title[:6] for _, title, _ in sent], ["❌ GONE"])
        self.assertEqual(t.evs.open, {})

    def test_a_spread_card_stays_while_a_book_is_only_missing(self):
        sent = []
        t = self.wired(sent)
        full, short = confirm_event(market="spreads"), confirm_event(market="spreads", others=("C", "D", "E"))
        self.scan(t, [full], -30, {})
        self.assertEqual(self.scan(t, [full], -15, {}).confirmed_sent, 1)
        del sent[:]
        res = self.scan(t, [short], 0, {})                             # F's price is missing this check
        self.assertEqual((res.ev_sent, res.confirm_misses), (0, {}))
        self.assertEqual([m for m, *_ in sent], ["PATCH"])
        self.assertIn("Fewer than 4 other books price it right now (Pinnacle 50.0%).",
                      ev_payload(next(iter(t.evs.open.values())).arb)["embeds"][0]["description"])
        self.scan(t, [full], 15, {})
        self.assertEqual([m for m, *_ in sent], ["PATCH", "PATCH"])
        del sent[:]
        self.scan(t, [confirm_event(market="spreads", other=(1.88, 2.02))], 30, {})   # 1.9 points off: closes
        self.assertEqual([title[:6] for _, title, _ in sent if not title.startswith("💰")], ["❌ GONE"])   # (an arb now)
        self.assertEqual(t.evs.open, {})
        # A new bet still needs every source: with too few books it never goes out.
        t2 = self.wired(sent)
        for mins in (0, 15, 30):
            self.assertEqual(self.scan(t2, [short], mins, {}).confirm_misses, {"under 4 other sportsbooks": 1})
        self.assertEqual(t2.evs.open, {})

    def test_a_restored_confirmed_card_is_not_sent_again(self):
        from unittest import mock
        sent = []
        t = self.wired(sent)
        games = [confirm_event(market="spreads")]
        self.scan(t, games, -30, {})
        self.assertEqual(self.scan(t, games, -15, {}).confirmed_sent, 1)
        del sent[:]
        with mock.patch("arbbot.time.time", return_value=NOW.timestamp()):    # (saved state is kept a day)
            again = self.wired(sent)                                         # a restart: no sharp history now
        self.assertEqual(len(again.evs.restored), 1)
        for mins in (0, 15):
            res = self.scan(again, games, mins, {})
            self.assertEqual((res.ev_sent, res.confirmed_sent, res.confirm_misses), (0, 0, {}), mins)
        self.assertEqual([m for m, *_ in sent if m == "POST"], [])                # no new ping
        self.assertNotIn("❌ GONE", str(sent))
        self.assertEqual(len(again.evs.open), 1)
        self.assertEqual(len(_read(self.files["ev_log_file"])), 1)

    def test_run_prints_it_on_the_check_line(self):
        import argparse, contextlib, io
        from unittest import mock
        real = datetime.now(timezone.utc)
        start = (real + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        secs = (real - NOW).total_seconds()

        class Api:
            remaining, used = 50000.0, None

            def __init__(self, cfg):
                pass

            def events(self, sport, horizon_hours=26):
                return [{"id": "e1", "commence_time": start}, {"id": "e2", "commence_time": start}]

            def odds(self, sport, until, markets=None, since=None, kind="odds"):
                games = [confirm_event(market="spreads", start=start), confirm_event(start=start, gid="e2")]
                return asked([stamped(g, secs) for g in games], markets, since)

        cfg = self.locks(sports=["icehockey_nhl"], props_enabled=False, kalshi_check=False)
        args = argparse.Namespace(once=True, demo=False, dry_run=True, plan=False, check_kalshi=False,
                                  results=None, post_results=None)
        with mock.patch("arbbot.OddsAPI", Api), contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            _arbbot.run(cfg, args, _arbbot.Status(cfg, dry_run=True))
        self.assertIn(" | 0 +EV, 0 new | ✅✅ confirmed pre-game: 0 sent, 2 near misses (1 no Kalshi price, 1 no sharp "
                      "history yet) | ", out.getvalue())                     # one look only: nothing to compare with

    def test_the_demo_shows_its_near_miss(self):
        import contextlib, io
        from unittest import mock
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("arbbot.kalshi_fair", side_effect=AssertionError("network")):
            _arbbot.run(self.locks(), SimpleNamespace(demo=True, dry_run=True), None)
        self.assertIn("[demo] 1 arb(s), 0 +EV bet(s), 1 outlier(s) in sample data (2 checks: live alerts go out once 2 "
                      "checks in a row find them) | ✅✅ confirmed pre-game: 0 sent, 1 near miss (1 no Kalshi price)\n",
                      out.getvalue())                                     # (the demo doesn't ask Kalshi)
        self.assertEqual(list(Path(self.d).iterdir()), [])

    def test_the_pinned_guide_explains_the_card(self):
        g = _arbbot.guide_payload()["embeds"][0]["description"]
        self.assertIn("✅✅ **Two sharp books agree** means Pinnacle and Kalshi (or Pinnacle and 4+ other books) give "
                      "almost the same chance to win, so a smaller edge (from 3.5%) is still a lock.", g)
        self.assertLessEqual(len(g), 4096)                                   # a Discord card's limit

    def test_parlays_are_built_as_before(self):
        cfg = self.locks()
        [four], _ = self.evs(confirm_event(), kalshi=AGREE)
        [big], _ = self.evs(confirm_event(bet=2.30, gid="e2"))                # 15% at the same book
        self.assertEqual(find_parlays([four, big], cfg, now=NOW), [])           # a confirmed bet is never a leg
        self.assertEqual(len(find_parlays([replace(four, tier=""), big], cfg, now=NOW)), 1)   # (a normal one would be)
        self.assertEqual(find_parlays([four, big], cfg, keep={find_parlays([replace(four, tier=""), big], cfg,
                                                                           now=NOW)[0].key}, now=NOW), [])


# --------------------------------------------------------------------------- no test writes the bot's own files

def _data_files() -> dict:
    """Every default data file (and the state folder's files, and .env) beside the code and in the folder
    the tests run from, with (size, modified) for each one there."""
    from dataclasses import fields
    d = Config()
    names = [getattr(d, f.name) for f in fields(Config) if f.name.endswith("_file")] + [d.state_dir, ".env"]
    out = {}
    for folder in {Path(_arbbot.HERE).resolve(), Path.cwd().resolve()}:
        for name in names:
            p = folder / name
            for q in ([p] + sorted(p.rglob("*")) if p.is_dir() else [p]):
                if q.exists():
                    st = q.stat()
                    out[str(q)] = (st.st_size, st.st_mtime_ns)
    return out


_FILES_BEFORE: dict = {}


def setUpModule():
    _FILES_BEFORE.update(_data_files())


def tearDownModule():
    """The guard: a test that wrote (or changed, or removed) one of the bot's own data files fails the run."""
    after = _data_files()
    changed = sorted(p for p in set(_FILES_BEFORE) | set(after) if _FILES_BEFORE.get(p) != after.get(p))
    if changed:
        raise AssertionError("tests wrote the bot's own data files (give the test a temporary folder): "
                             + ", ".join(changed))


if __name__ == "__main__":
    unittest.main()
