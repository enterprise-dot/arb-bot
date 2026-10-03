import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone

from arbbot import (LIVE, PREGAME, Alerter, Config, EVAlerter, OutlierAlerter, Scheduler,
                    demo_events, devig, find_outliers, outlier_payload, without_outliers,
                    discord_payload, ev_payload, ev_record, find_arbs, find_evs, kelly_stake,
                    next_reset, seconds_until_active, settle, settle_pending)

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
        ev["commence_time"] = "2026-10-03T18:00:00Z"
        ev["bookmakers"][1]["markets"][0]["last_update"] = (NOW - timedelta(minutes=20)).isoformat()
        self.assertEqual(find_arbs([ev], CFG, NOW), [])           # pre-game: 20 min is too old

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
        self.assertEqual(len(discord_payload(arb)["embeds"][0]["fields"]), 3)


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
        self.assertIn("with exact stakes", discord_payload(arb)["embeds"][0]["fields"][-1]["value"])

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


EVCFG = Config(min_ev_pct=3, round_stakes=0, ev_bankroll=1000, kelly_fraction=0.25)


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
        both = Config(min_ev_pct=3, round_stakes=0, sharp_books="pinnacle,betfair_ex_eu")
        [b] = find_evs([ev], both, NOW)
        self.assertEqual((b.sources_used, b.sources_total), (1, 2))
        self.assertEqual(b.stake, 6.0)   # half of the 11.4 one-sharp stake, rounded
        self.assertAlmostEqual(b.ev_pct, 5.0, places=1)   # the edge itself is unchanged

    def test_units_label(self):
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.10, None)]})
        [b] = find_evs([ev], Config(min_ev_pct=3, round_stakes=0, unit_size=10), NOW)
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
        fields = {f["name"]: f["value"] for f in ev_payload(b)["embeds"][0]["fields"]}
        self.assertIn("-110 / -110", fields["Sharp prices (this side / other side)"])
        self.assertIn("+110", fields["Every book on this bet"])

    def test_fair_movement_carried(self):
        ev1 = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.20, None)]})
        ev2 = ev_event([("Home", 1.87, None), ("Away", 1.95, None)], {"B": [("Home", 2.20, None)]})
        [b1], [b2] = find_evs([ev1], EVCFG, NOW), find_evs([ev2], EVCFG, NOW)
        a = EVAlerter(EVCFG, dry_run=True)
        a.handle([b1], now=1000)
        a.handle([b2], now=1060)
        cur = a.open[b2.key].arb
        self.assertAlmostEqual(cur.first_fair_prob, 0.5)
        self.assertIn("→", ev_payload(cur)["embeds"][0]["fields"][1]["value"])

    def test_feed_freshness(self):
        from arbbot import feed_freshness
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.10, None)]})
        self.assertIn("pre-game: half of prices under 0s", feed_freshness([ev], NOW))

    def test_payload(self):
        ev = ev_event([("Home", 1.91, None), ("Away", 1.91, None)], {"B": [("Home", 2.10, None)]})
        [b] = find_evs([ev], EVCFG, NOW)
        self.assertIn("+5.0% EV: Home ML +110 (B)", ev_payload(b)["embeds"][0]["title"])
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
        self.cfg = Config(min_ev_pct=3, ev_log_file=str(d / "ev.csv"), ev_results_file=str(d / "res.csv"))

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
        self.assertEqual(settle_pending(self.cfg, Scores()), 1)   # ...but graded once
        self.assertEqual(settle_pending(self.cfg, Scores()), 0)   # nothing left; no extra API call
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
        self.assertTrue(any("Lock it in" in f["name"] for f in outlier_payload(outs[0])["embeds"][0]["fields"]))

    def test_needs_enough_books_and_edge(self):
        ev = outlier_event(1.30, 3.60)                     # only slightly off: not an outlier
        self.assertEqual(find_outliers([ev], Config(), NOW), [])
        self.assertEqual(find_outliers([outlier_event(1.85, 1.95)], Config(outlier_min_books=5), NOW), [])
        self.assertEqual(find_outliers([outlier_event(1.85, 1.95)], Config(outlier_live=False), NOW), [])
        self.assertEqual(find_outliers([outlier_event(1.85, 1.95)], Config(outliers_enabled=False), NOW), [])

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


class SchedulerState(unittest.TestCase):
    def test_live_pregame_idle(self):
        s = sched_with({"basketball_nba": [("g1", NOW - timedelta(minutes=30))],
                        "icehockey_nhl": [("g2", NOW + timedelta(hours=1))],
                        "baseball_mlb": [("g3", NOW + timedelta(hours=8))],
                        "americanfootball_nfl": []})
        self.assertEqual(s.state("basketball_nba", NOW), LIVE)
        self.assertEqual(s.state("icehockey_nhl", NOW), PREGAME)
        self.assertIsNone(s.state("baseball_mlb", NOW))
        self.assertIsNone(s.state("americanfootball_nfl", NOW))

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

    def test_pregame_off(self):
        cfg = Config(sports=["icehockey_nhl"], pregame_minutes=0)
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
        # (3000 left - 2% cushion) / days until reset.
        self.assertGreater(s.scale, 1)
        self.assertAlmostEqual(s.forecast / s.scale, s.allowance, delta=1)
        self.assertGreater(s.interval(LIVE), 60)

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


if __name__ == "__main__":
    unittest.main()
