import math
import time
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from datetime import datetime, timedelta, timezone

from arbbot import (LIVE, PREGAME, EARLY, FAR, Alerter, Config, EVAlerter, OutlierAlerter, Scheduler,
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
        worst = arb.worst_ok_price(0)          # Celtics leg, with the stakes as printed
        # At the skip-line price, the printed Celtics stake still returns total + 0.5%, rounded
        # up to a price the card can show (-133, not -134, which would keep less).
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
        self.assertEqual([l.stake for l in arb.legs], [57.5, 42.5])
        desc = discord_payload(arb)["embeds"][0]["description"]
        self.assertTrue(desc.startswith("👉 **DO THIS NOW: place BOTH bets."))
        self.assertIn("1️⃣ Open **FanDuel** → bet **$57.50** on **Boston Celtics -125**", desc)
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
        self.arbs[0].legs[0].price = 1.95   # 3.5% -> ~7.9%: jump of more than 2.5 points
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
        a = EVAlerter(EVCFG, dry_run=True)
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
        b = EVAlerter(EVCFG, dry_run=True)
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
                          outlier_log_file=str(d / "out.csv"), closing_file=str(d / "close.csv"))

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
        a = OutlierAlerter(Config(), dry_run=True)
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
        self.assertTrue(b.sharp_book.startswith("consensus of 5 books"))
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
        a = EVAlerter(Config(), dry_run=True)
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
        self.assertEqual(c.min_live_profit_pct, 3.0)
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
        a = EVAlerter(Config(), dry_run=True)
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
                          closing_file=str(d / "close.csv"), pregame_max_age_seconds=10**9)

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
                          closing_file=str(d / "close.csv"), pregame_max_age_seconds=10**9)

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
                          closing_file=str(d / "close.csv"), pregame_max_age_seconds=10**9)
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
                          ev_results_file=str(d / "res.csv"), closing_file=str(d / "close.csv"), log_file="")
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
                     ev_results_file=str(d / "res.csv"), closing_file=str(d / "close.csv"))
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
        api = OddsAPI(Config(api_key="k"))
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


if __name__ == "__main__":
    unittest.main()
