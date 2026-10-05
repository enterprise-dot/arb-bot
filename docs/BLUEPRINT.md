# The blueprint, ID by ID

Your friend's blueprint ("+EV Sports Betting Scanner - Personal V1") gives every requirement an ID and a
colour. This page lists all of them: what the bot does about each one, and which setting or part of
`arbbot.py` does it. It was written after the blueprint changes went in, so the statuses describe the
code as it is now. A test (`test_blueprint_ids_preserved`) fails if an ID goes missing or changes colour.

**See the Yellow and Red items any time:** `python3 arbbot.py --blueprint` prints them with their status
and what was decided, then the open decisions below, each with the value your bot is using right now.
It's free: no credits, nothing is sent to Discord.

## How to read it

The colours are the blueprint's own:

- **GREEN**: a fixed requirement.
- **YELLOW**: the blueprint's preferred direction, to be reviewed before it's built.
- **RED**: left open on purpose: the options should be explained and one approved.

A few rows have no blueprint ID of their own, so they get a label: the success criteria in section 21
(SC-9 to SC-21, all GREEN), the out-of-scope list in section 22 (OOS-1 to OOS-14) and the blueprint's
rules for how the work is done (RULE).

Each row's status:

- **DONE**: the bot does it.
- **PARTIAL**: it does part of it; the row says what's missing.
- **SUPERSEDED**: you asked for something different. Your own words are under "Changed by your own requests".
- **NOT_FEASIBLE**: it can't be done on The Odds API's $59 plan without a second paid service. See "Not
  feasible on this feed".
- **PROCESS**: it's about how the work is done, not something the bot does.
- **DEVIATION**: done differently on purpose. The reason is under "Deviations".

## Open decisions (tell Claude if you want different)

The blueprint wants your approval on its Yellow and Red items. You told Claude to "do whatever you gotta
do", so these were decided for you. The bot runs on the default shown. If you want something else, tell
Claude, or change the setting yourself (`python3 arbbot.py --set NAME=value`, then restart the bot).

- **How the margin is taken out** (FAIR-20, RED): the power method, which takes more of the margin out of
  long shots, where books hide most of it. `DEVIG_METHOD=power` (the other choice is `multiplicative`).
- **Kalshi's price** (FAIR-10, RED): the midpoint of each team's best bid and ask, the two scaled to add up
  to 100%. The last trade isn't used. Kalshi's fee (`KALSHI_FEE_RATE=0.07`) only counts when you bet at
  Kalshi. Before the game only (`KALSHI_LIVE=false`): its in-game prices lag.
- **When Kalshi's price counts** (FAIR-03, RED): bid and ask at most 3¢ apart (college 5¢,
  `KALSHI_MAX_SPREAD=3`), at least 100 contracts at each (`KALSHI_MIN_SIZE=100`), both teams' markets open,
  and the two teams' prices adding up (asks at most $1.10 together, midpoints 96-104%; fixed in arbbot.py).
- **How far apart two sources may be** (FAIR-26, FAIR-26A, RED): 3 points of win chance. Two sharp books
  further apart: the line is skipped (`SHARP_DISAGREE_PCT=3`; props 4, `SHARP_DISAGREE_PROP_PCT=4`). Kalshi
  further from Pinnacle: the moneyline bet is skipped (`KALSHI_MAX_GAP=3`; 4 for games more than a day
  away, 5 for college).
- **How much each source counts** (FAIR-09, FAIR-19, FAIR-21, YELLOW): Pinnacle 0.50, Circa 0.35 (not in
  the feed), Kalshi 0.15, any other sharp book 0.15, shared out over the sources that price a line, so
  Pinnacle + Kalshi is 77% / 23%. Kalshi goes into the fair price of pre-game moneylines
  (`KALSHI_BLEND=true`): Pinnacle 50% and Kalshi 52% make 50.5%. Props use the same weights
  (`SHARP_WEIGHTS_PROPS` empty), but only Pinnacle prices them. Change them with `SHARP_WEIGHTS` (for
  example `pinnacle=0.6,kalshi=0.2`). Moneyline edges logged since this change have Kalshi in them when it had
  a usable price; ev_bets.csv's `pinnacle_prob` keeps Pinnacle's own.
- **The odd source left out** (FAIR-17, FAIR-24, FAIR-25, FAIR-27, RED): with 3 or more sources on a line,
  the one farthest from the middle of the others is left out when it's more than 3 points away (props 4):
  `OUTLIER_SOURCE_PTS=3`, `OUTLIER_SOURCE_PROP_PTS=4`. At most one per line, never Pinnacle (the line is
  skipped instead), the less trusted one on a tie, and nothing is remembered: it counts again on the next
  check it's back within the limit. With today's sources (Pinnacle and Kalshi) it never comes up.
- **Stakes when one source sets the price** (STAKE-07, STAKE-08, MATCH-11, MATCH-12, MATCH-14, RED):
  Pinnacle alone gets the full stake (`ONE_SOURCE_STAKE=1`; 0.75 would bet a quarter less, on most bets).
  Kalshi alone bets half (`KALSHI_ONLY_STAKE=0.5`), is never more than medium confidence (`KALSHI_ONLY=true`)
  and waits while Pinnacle is missing from the whole sport. One of several sharp books alone: half
  (`SINGLE_SOURCE_STAKE=0.5`). A prop priced by the other books: 70% (`CONSENSUS_STAKE=0.7`).
- **Units** (STAKE-09, STAKE-10, STAKE-11, YELLOW): 1u is 1% of `EV_BANKROLL` ($10 with $1,000;
  `UNIT_SIZE=0`), so 100 units is the bankroll. Cards show "$15 (1.5u)": the dollars first, the units next
  to them. Arb cards stay in dollars (their bets must add up exactly). `python3 arbbot.py --set
  UNIT_SIZE=20` sets `EV_BANKROLL=2000` too, so every stake scales with the unit.
- **How old Pinnacle's price may be** (FRESH-02, YELLOW): 10 seconds isn't possible on this feed (see below).
  Pinnacle has the same limits as every book: 2 minutes live, 15 minutes within 2 hours of kickoff, 3 hours
  further out (`SHARP_MAX_AGE_SECONDS`, `SHARP_PREGAME_MAX_AGE_SECONDS`, `SHARP_FAR_MAX_AGE_SECONDS`, all
  empty). The feed re-reads Pinnacle every 60 seconds before games, so a tighter pre-game limit (around 180
  seconds) would fit it, at the cost of some bets.
- **Prop Unders** (SCOPE-15, OOS-13): `PROP_SIDES` empty means Overs only in locks mode, as the blueprint
  says, and Overs and Unders in balanced and all mode. Today's flood settings (`ALERT_MODE=balanced` in
  remote.env) send the Unders, about half the prop picks. `PROP_SIDES=over` stops them.
- **Credits for NHL goals and saves and NFL alternate lines** (SCOPE-13, SCOPE-14): asked for in the last 3
  hours before each game (`PROP_NEAR_MARKETS`), but by default only with credits the day has to spare
  (`PROP_NEAR_SPARE_ONLY=true`): when the next 24 hours fit at full speed with them added, after the
  near-kickoff speed-up of main-line checks. They never slow a live or main-line check; on a short day they
  wait and the usual props carry on. In the simulated October (every credit used) that was about 1
  near-kickoff check in 20, about 200 credits. `PROP_NEAR_SPARE_ONLY=false` asks for them every time: about
  3,570 credits that month (NHL ~1,810, NFL ~1,760), paid for by main-line checks coming about 4-5% less often
  and live checks 2-5 seconds further apart. To turn them off: put
  `PROP_NEAR_MARKETS=icehockey_nhl=;americanfootball_nfl=` in `.env` (with `--set`, in quotes:
  `python3 arbbot.py --set "PROP_NEAR_MARKETS=icehockey_nhl=;americanfootball_nfl="`).
- **Odds too old by the time an alert is ready** (FRESH-06, FRESH-07, FRESH-08, PERF-02, YELLOW): it isn't
  posted on odds older than 10 minutes (`SEND_MAX_DELAY_SECONDS=600`), live 90 seconds
  (`LIVE_SEND_MAX_DELAY_SECONDS=90`), and the bot doesn't wait out a Discord rate limit longer than 10
  seconds on a new alert's post (`DISCORD_MAX_WAIT_SECONDS=10`; edits, GONE marks and health messages
  still wait, since they have no next check). These are generous on purpose: they were set before there was
  data. After a week, `post_delay` in ev_bets.csv, outliers.csv and candidates.csv shows how long alerts
  really take.
- **Outage messages** (FAIL-01): after 10 minutes of the Odds API not answering for a sport
  (`ODDS_DOWN_MINUTES=10`), 15 minutes of Pinnacle missing from a sport (`SHARP_DOWN_MINUTES=15`) and 30
  minutes of Kalshi unreachable (`KALSHI_DOWN_MINUTES=30`). `HEALTH_ALERTS=true`.
- **What the candidate log keeps** (LOG-01, YELLOW): bets within 1 point of their bar, and the same bet and
  decision at most once every 15 minutes (both fixed in arbbot.py). `CANDIDATE_LOG_FILE=candidates.csv`.
- **How long logs are kept** (LOG-02, YELLOW): candidates.csv, arbs.csv and markouts.csv keep 60 days
  (`LOG_KEEP_DAYS=60`), so the markout summaries cover the last 60 days, not all time. The bet logs,
  results, closing lines and score_checks.csv are never cut.
- **Bet links built from the books' ids** (LINK-01, LINK-08, LINK-11, YELLOW): `INCLUDE_SIDS=true`. The id
  fields and the DraftKings and FanDuel link shapes come from the docs and haven't been seen in a live
  answer yet. One `python3 arbbot.py --once` on the server shows how many links were "built from ids". If
  none are, nothing changes: the feed's own links stand.

## Not feasible on this feed

Checked on Oct 4, 2026. The sites below were blocked from Claude's sandbox, so these facts come from search
results quoting them, the official docs and the bot's own output.

- **Circa** (FAIR-05, FAIR-12, FAIR-18, MATCH-13, SC-10): The Odds API has no Circa bookmaker
  (https://the-odds-api.com/sports-odds-data/bookmaker-apis.html). Circa is only in other paid feeds:
  SportsGameOdds from $149/month (https://sportsgameodds.com/bookmakers/circa-odds-api), odds-api.io from
  £49/month (https://odds-api.io/sportsbooks/circa), OpticOdds at enterprise prices
  (https://opticodds.com/sportsbooks/circasports-api). A second paid feed goes against DATA-10, and Circa
  doesn't take bets in New York anyway. If The Odds API ever adds it (key `circasports`), it already has
  its 0.35 weight.
- **ParlayAPI** (DATA-05, LINK-04): new (directory listings from September 2026, https://parlay-api.com).
  Its Circa, freshness and alternate-line claims can't be checked without an account. Its comparison page
  says The Odds API has no deep links, which is wrong (they launched on Sep 22, 2024:
  https://the-odds-api.com/releases/deep-links.html), and it lists WynnBET and PointsBet, which left the US
  market (https://www.covers.com/industry/wynnbet-exits-new-york-espn-bet-gears-up-fall-launch-sports-betting-august-1-2024,
  https://sbcamericas.com/2024/04/04/fanatics-closes-pointsbet-acquisition/). You had picked The Odds API
  before the blueprint arrived.
- **Streaming** (DATA-03): The Odds API answers requests only, with no WebSocket or push
  (https://the-odds-api.com/liveapi/guides/v4/). Kalshi's own WebSocket needs a signed API key and covers
  only Kalshi (https://docs.kalshi.com/websockets/websocket-connection). Feeds that stream cost far more
  (odds-api.io's WebSocket doubles its price, SportsGameOdds needs its top plan, OpticOdds is enterprise).
- **A 10-second limit on Pinnacle's price age** (FRESH-02): The Odds API re-reads Pinnacle's main markets
  every 60 seconds before a game and 40 seconds during it, props every 60 seconds
  (https://the-odds-api.com/sports-odds-data/update-intervals.html). Checking one sport every 10 seconds
  would cost about 25,900 credits a day; the plan gives about 3,300.
- **Pikkit** (LINK-12): no public API, SDK or partner program. Pikkit buys its odds from OpticOdds
  (https://opticodds.com/customers/pikkit), and its terms forbid automated access without written
  permission (https://pikkit.com/terms). Scraping it is ruled out by LINK-14 too. You can still use the
  Pikkit app by hand to track the bets you place.

## Deviations

- **FRESH-01, no hard expiry on DraftKings and FanDuel prices.** The cards do show the exact price, how old
  it was and the price to skip at, so you can check it. But a book's price still has to be recent to be
  alerted (2 minutes live, `MAX_AGE_SECONDS`; 15 minutes within 2 hours of kickoff,
  `PREGAME_MAX_AGE_SECONDS`; 3 hours further out, `FAR_MAX_AGE_SECONDS`). The Odds API's time stamp on a
  market is "the last time our system saw odds for that market from the bookmaker", and it stops moving
  when the book suspends the market. So an old stamp means the book took the bet down, not that the price
  sat still, and alerting it would send you to a bet that isn't there.

## Changed by your own requests

These are the SUPERSEDED rows, with what you said.

- **Automatic updates instead of approval between stages** (ARCH-21, PROTOCOL-19): "I want it to
  automaticaly update itself please" and "Are you able to work literlly all night on this? Plan an 8+hour
  work session to continue imrpoving the bot". Every update still has to pass the full test suite and is
  rolled back if it breaks (deploy/auto-update.sh).
- **Results, CLV and a running record** (CORE-02, TRACK-01, OOS-8, OOS-9, OOS-10): "Looks great. Can u track
  CLV for each bet?", "How do I check the results for the past bets today? And can that be filtered to a
  channel so we know what hit?" and "can there be a live summary with record, and stats exactly like u
  have there".
- **More leagues from the start** (SCOPE-07, OOS-12): "if we want to do like four sports and uh, keep it at
  the 30 bucks a month, any bet type" and "what if we wanted to do it for the College football games today
  since theres not nba til the 21st".
- **The Odds API, not ParlayAPI** (DATA-05, LINK-04): "Yeah, I guess the 59 bucks one a month is fine, but
  just build this as efficient as you possibly can."
- **Separate channels** (DELIVERY-02): "Can we do arbs in a different channel ... can we put like the other
  types of bet in like a different channel" and "And can that be filtered to a channel so we know what hit?"
- **Edge bars** (EV-01, EV-02, SC-15): "We just want like locks": locks mode, the default, asks for 5% on
  main lines and 8% on props. Then on Sunday: "It's barely sending any today for the whole NFL we want
  a lot of picks today can we get some sent" and "Literally no picks are coming in can u flood it like
  yesterday", so today's remote.env uses balanced mode at 3% and 6%. The blueprint's 4% and 7% are still
  the `MIN_EV_PCT` and `PROP_MIN_EV_PCT` defaults in balanced mode.
- **Cleaner cards and setup help** (OOS-1): "can you make the uh, notification on Discord like look a little
  cleaner, more spaced out?", "make it clear exactly what needs to be done right away?" and "can you give me
  like detailed instructions?"

Also yours and not in the blueprint: arbitrage alerts, outliers, parlays, locks mode, `MY_BOOKS` and the New
York college rule.

## Every blueprint ID

| ID | Colour | Status | The blueprint says | What the bot does |
|---|---|---|---|---|
| READ-RULES | RULE | DONE | Keep every decision ID so you can ask for all the Yellow and Red items at any time (also section 23: keep the IDs and statuses). | This page, `python3 arbbot.py --blueprint`, and `test_blueprint_ids_preserved`. |
| BLUEPRINT-01 | GREEN | DONE | Track every important decision as Green, Yellow or Red. | This page: every ID with its colour and status. |
| BLUEPRINT-02 | GREEN | DONE | Keep a list of the open decisions that matter for the build. | "Open decisions" above; `--blueprint` prints it with your current values. |
| BLUEPRINT-03 | GREEN | PROCESS | Don't silently make product choices on open Yellow and Red items. | You handed these choices to Claude; each one made is listed under "Open decisions" with its default and setting. |
| BLUEPRINT-04 | GREEN | PROCESS | An open item can wait until the stage where it matters. | Circa and ParlayAPI stay open (not on this feed). Props and Kalshi waited until you asked for them. |
| ARCH-20 | GREEN | PROCESS | Explain big architecture choices and their tradeoffs before building them. | Each change is explained in README.md, this page and the update message, as it ships rather than before (you asked for automatic updates). |
| ARCH-21 | GREEN | SUPERSEDED | Build in stages, with your approval between them. | You asked for automatic updates and overnight work. Every update passes the full test suite first and is rolled back if it breaks. |
| CORE-01 | GREEN | DONE | Build a +EV sports betting scanner. | `find_evs` and `EVAlerter`. |
| CORE-02 | GREEN | SUPERSEDED | A scanner first, not an analytics platform. | You asked for CLV, results and a scoreboard: `ClosingTracker`, `Results`, the scoreboard and the weekly report card. |
| INSP-01 | GREEN | DONE | Vulture Bot is the inspiration, not something to clone. | Ideas borrowed (fair price movement, Pinnacle's odds "(was ...)", every book's price on the card); the layout is the bot's own. |
| CORE-03 | GREEN | DONE | Book lines, sharp comparison, fair probability, EV, stake, alert. | `sharp_fair` (margin out, blend), `find_evs` (edge and checks), `kelly_stake`, `Alerter.handle`. |
| CORE-04 | GREEN | DONE | Alerts are immediately actionable. | Every card starts with a DO THIS line: the book (linked), the stake, and the price where you should skip it. |
| ARCH-01 | GREEN | DONE | V1 is for your own use. | One `.env`, your own channels, `MY_BOOKS`, the New York rules (`US_STATE=ny`). |
| ARCH-02 | GREEN | DONE | Correctness, matching, fair value, speed and dependable alerts before polish. | The full test suite gates every update; every +EV bet passes quality checks (Pinnacle's margin, the market gap, Kalshi, price age); open alerts survive restarts. |
| ARCH-03 | GREEN | PARTIAL | The foundation takes future users, books, sharps, sports and notification channels without rewriting the core. | Books, sharps, sports, markets and channels are settings (`BOOKMAKERS`, `MY_BOOKS`, `SHARP_BOOKS`, `SPORTS`, `PROP_MARKETS`, the Discord webhooks). Not built: other notification services (Discord only, through `_webhook`) and several users (out of V1's scope). The scanners never touch Discord, so another channel can be added later. |
| DATA-02 | GREEN | DONE | Scan DraftKings and FanDuel; adding books later should be easy. | Both are in `BOOKMAKERS`. The books you bet at are `MY_BOOKS` (also BetMGM, Caesars and Kalshi). Up to 10 books cost the same credits. |
| SCOPE-05 | GREEN | DONE | NFL and NHL. | In `SPORTS` and `PROP_SPORTS`; Kalshi's game winners for both. |
| SCOPE-07 | GREEN | SUPERSEDED | Add leagues only after the core is proven. | You asked for four sports and college football from the start: NBA, MLB and college football are in `SPORTS` (college games need 6%, `SPORT_MIN_EV`). |
| SCOPE-08 | YELLOW | DONE | NBA is the likely next league. | NBA is in `SPORTS` and `PROP_SPORTS`. Its checks start by themselves once NBA games are on the (free) schedule. |
| SCOPE-06 | GREEN | DONE | NFL moneyline, spread and total. | `MARKETS=h2h,spreads,totals`, matched at the exact point. |
| SCOPE-09 | GREEN | DONE | NHL moneyline, puck line and total. | The same `MARKETS` (the Odds API's NHL spread is the puck line). |
| SCOPE-11 | GREEN | DONE | NFL props: passing, rushing and receiving yards, receptions. | `DEFAULT_PROP_MARKETS`; graded from box scores. |
| SCOPE-12 | GREEN | DONE | No NFL touchdown or passing attempts and completions props. | None of them is asked for (`DEFAULT_PROP_MARKETS`, `PROP_NEAR_MARKETS`). |
| SCOPE-13 | GREEN | PARTIAL | NHL props: shots on goal, points, goals, assists, goalie saves. | Shots, points and assists on every prop check; goals and goalie saves in the last 3 hours before the game (`PROP_NEAR_MARKETS`), but by default only when the day has spare credits for them (`PROP_NEAR_SPARE_ONLY`). Missing: in a month that uses every credit (the simulated October) that's about 1 near-kickoff check in 20; `PROP_NEAR_SPARE_ONLY=false` asks every time, at some live and main-line pace. An open decision. If the Odds API rejects them, just those stop for NHL until a restart. |
| SCOPE-14 | GREEN | PARTIAL | Alternate player-prop lines are allowed. | NFL alternate passing, rushing and receiving yards and receptions near kickoff (`PROP_NEAR_MARKETS`), bet only at exactly a line that has a fair price (`base_market`, `book_offers`). Cards (and parlay and lock-in legs) say "(alternate line)". Missing: as SCOPE-13, they're asked for only when the day has spare credits (`PROP_NEAR_SPARE_ONLY`), about 1 near-kickoff check in 20 in the simulated October. An open decision. |
| SCOPE-15 | GREEN | PARTIAL | Player props are Overs only. | `PROP_SIDES`: empty means Overs only in locks mode (the default) and both sides in balanced and all. Missing: today's flood settings (balanced, in remote.env) still send Unders, about half the prop picks; `PROP_SIDES=over` makes it DONE. An open decision. |
| DATA-10 | GREEN | DONE | One main odds provider. | The Odds API for all odds (`OddsAPI`). Kalshi's free API is a second opinion on game winners; ESPN, NHL and MLB stats only grade results. |
| DATA-05 | YELLOW | SUPERSEDED | ParlayAPI is the leading candidate; check it against the others first. | You picked The Odds API's $59 plan before the blueprint arrived. ParlayAPI was checked: see "Not feasible on this feed". |
| DATA-03 | YELLOW | NOT_FEASIBLE | Prefer streaming (WebSocket or SSE) if the provider does it reliably. | The Odds API is request-only. The bot checks live sports every 60 seconds at most (`POLL_SECONDS`) and paces every check to the credits. |
| DATA-04 | GREEN | DONE | Turn every source's data into one internal form. | `apply_fees` (Kalshi's fee, BetMGM's state links), `_line_for` and `base_market` (one key per exact line), `EVBet`, `Arb` and `Leg`, Kalshi's quote per team. |
| PERF-01 | GREEN | DONE | Fair value, EV and stakes are worked out locally in milliseconds. | No network calls inside the scanners. A 16-game main-line check takes tens of milliseconds, a 14-game NFL prop slate with alternate ladders about half a second. |
| PERF-02 | YELLOW | PARTIAL | Set speed targets after testing the real provider. | Measured now: `post_delay` (seconds from fetching the odds to the post) in ev_bets.csv, outliers.csv and candidates.csv. The send-time limits (`SEND_MAX_DELAY_SECONDS=600`, `LIVE_SEND_MAX_DELAY_SECONDS=90`) came before that data and are generous on purpose; revisit them after a week of it. Open decision. |
| FAIR-05 | GREEN | PARTIAL | Use Pinnacle, Circa and Kalshi wherever they have the same market. | Pinnacle wherever it prices; Kalshi's own prices on pre-game game winners (`KALSHI_BLEND`). Circa isn't in The Odds API (not feasible), and Kalshi's spread and total markets aren't used. |
| FAIR-06 | GREEN | DONE | Prefer several references over one whenever several are valid. | Pinnacle and Kalshi are blended whenever both have a usable price (`KALSHI_BLEND=true`), and so are any extra sharp books in `SHARP_BOOKS`. Other lines have only Pinnacle on this feed. |
| FAIR-18 | GREEN | DONE | Trust Pinnacle, then Circa, then Kalshi; Pinnacle counts most. | Default weights in that order (`SOURCE_WEIGHTS`: Pinnacle 0.50, Circa 0.35, Kalshi 0.15, other sharp books 0.15). Pinnacle is never the source left out (the line is skipped instead), and the bot warns at startup if `SHARP_WEIGHTS` puts any source above Pinnacle. |
| FAIR-09 | YELLOW | DONE | Candidate weights: Pinnacle 50%, Circa 35%, Kalshi 15%. | Those are the defaults, shared out over the sources present: Pinnacle + Kalshi is 77% / 23% (`SHARP_WEIGHTS`). Open decision. |
| FAIR-19 | YELLOW | DONE | Main lines and props may use different weights. | `SHARP_WEIGHTS_PROPS` (empty = `SHARP_WEIGHTS`). Props are Pinnacle only today: Kalshi doesn't price them. Open decision. |
| FAIR-21 | YELLOW | DONE | Share the weights out again when a source is missing or left out. | `blend_refs` divides by the weights of the sources actually in the price. |
| FAIR-07 | GREEN | DONE | Take Pinnacle's margin out before blending. | `sharp_fair` takes the margin out of each sharp book's full market (`devig`) before `blend_refs`. |
| FAIR-12 | GREEN | NOT_FEASIBLE | Take Circa's margin out before blending. | Circa isn't in The Odds API. If it ever is (key `circasports`), it's handled like Pinnacle, at its 0.35 weight. |
| FAIR-20 | RED | DONE | Pick and justify how the margin is taken out. | The power method (`DEVIG_METHOD=power`; `multiplicative` is the other choice). Decided by Claude: open decision. |
| FAIR-13 | YELLOW | DONE | Kalshi is an independent second reference. | Read straight from Kalshi (free): it stops a moneyline bet it disagrees with, adds confidence when it agrees, goes into the fair price (`KALSHI_BLEND`), must agree for outliers, and is shown on cards. |
| FAIR-10 | RED | DONE | How to get a win chance out of Kalshi. | The midpoint of each team's best bid and ask, scaled so the two add up to 100% (`kalshi_fair`); not the last trade; Kalshi's fee only when betting there; before the game only (`KALSHI_LIVE=false`). Open decision. |
| FAIR-03 | RED | DONE | The least liquidity a Kalshi price needs. | At most 3¢ between bid and ask, 5¢ for college (`KALSHI_MAX_SPREAD`); 100+ contracts at each (`KALSHI_MIN_SIZE`); both markets open; asks at most $1.10 together and midpoints 96-104% (`_kalshi_problem`). Open decision. |
| MATCH-02 | GREEN | DONE | Props need their own matching. | Props match on the player and the exact point (`_line_for`); players are never mixed up. |
| MATCH-04 | GREEN | DONE | Exact lines only. | One key per exact point. No fair price at that key, no bet. |
| MATCH-05 | GREEN | DONE | An alternate line only counts where the same exact line has a reference. | An alternate price is only bet at a point that has a fair price from main lines (`book_offers`); alternate lines never set the fair price. |
| MATCH-09 | GREEN | DONE | No fair value worked out from nearby alternate lines. | Never interpolated. Another point can only stop a bet (`_sharp_doubt`). |
| MATCH-10 | GREEN | DONE | No exact reference line, no EV. | Lines with no fair price at their exact key are skipped. |
| MATCHSET-02 | GREEN | PARTIAL | Check the same sport, game, team or player, market, side, exact line and compatible settlement rules. | Same game id, names, market, side and point; Kalshi's games matched by date, start time and team (`_kalshi_match`). Settlement: the known difference (an NFL tie: books refund it, Kalshi follows its own rules) is a note on Kalshi NFL moneyline cards (`kalshi_tie_note`), and `--check-kalshi` prints Kalshi's rules text. It isn't checked automatically, and results grade a tied NFL game's Kalshi bet as a push, like the books'. |
| MATCHSET-01 | YELLOW | DONE | A matched-market set; its form is up to the AI. | One key per game, market and exact line: `SharpFair`, `Consensus` and the alert key `ev_key`. |
| MATCH-06 | GREEN | DONE | Two references are enough for normal EV; three are better. | Pre-game moneylines use Pinnacle and Kalshi when both are usable ("Sources 2/2"). Three isn't possible on this feed (no Circa) unless you add sharp books. One-source bets are allowed too (MATCH-11, MATCH-12). |
| MATCH-11 | YELLOW | DONE | A one-source bet may be allowed. | Allowed: Pinnacle alone, Kalshi alone (`KALSHI_ONLY`), and props priced by the middle of 4+ other books (`PROP_MIN_BOOKS`, `CONSENSUS_STAKE`). |
| MATCH-12 | YELLOW | DONE | Pinnacle alone may be allowed. | The normal case for spreads, totals and props, at the full stake by default (`ONE_SOURCE_STAKE=1`). |
| MATCH-13 | YELLOW | NOT_FEASIBLE | Circa alone may be allowed. | No Circa on this feed. |
| MATCH-14 | GREEN | DONE | Kalshi alone can alert, with a smaller stake. | `KALSHI_ONLY=true`: a pre-game moneyline Pinnacle doesn't list (while it lists the sport's other games), with 3+ sportsbooks (not exchanges) within `SHARP_CONSENSUS_MAX_GAP`. Half stake (`KALSHI_ONLY_STAKE=0.5`), at most medium confidence, never at Kalshi, and paused while Pinnacle is missing from the whole sport. |
| STAKE-08 | RED | DONE | The exact stake cut for Pinnacle-only, Circa-only and Kalshi-only bets. | Pinnacle alone 1, the full stake (`ONE_SOURCE_STAKE`); Kalshi alone 0.5 (`KALSHI_ONLY_STAKE`); one of several sharp books 0.5 (`SINGLE_SOURCE_STAKE`); Circa can't happen. Only the stake changes, never the edge. Open decision. |
| FAIR-17 | GREEN | DONE | Leave an outlying source out of that calculation. | With 3+ sources, the one farthest from the others is left out (`blend_refs`, `OUTLIER_SOURCE_PTS`). The card says "LowVig left out (4.1 pts off)"; ev_bets.csv has an `excluded` column. Never comes up with today's two sources. |
| FAIR-24 | RED | DONE | The exact test for an outlying source. | More than 3 points of win chance from the middle (median) of the others, props 4; one per line; never Pinnacle (the line is skipped); a tie drops the less trusted. Open decision. |
| FAIR-25 | RED | DONE | Whether props get different outlier limits. | Yes: `OUTLIER_SOURCE_PTS=3` for main lines and `OUTLIER_SOURCE_PROP_PTS=4` for props; the rest must then agree within `SHARP_DISAGREE_PCT=3` or `SHARP_DISAGREE_PROP_PCT=4`. Open decision. |
| FAIR-27 | RED | DONE | When a source that was left out counts again. | Nothing is remembered: it counts again on the next check it's back within the limit. Open decision. |
| FAIR-26 | GREEN | DONE | Two references that strongly disagree: no alert. | Two sharp books more than `SHARP_DISAGREE_PCT` apart: the line is skipped. Kalshi more than `KALSHI_MAX_GAP` from Pinnacle: the moneyline bet is skipped (checked against Pinnacle's own price, before any blend). |
| FAIR-26A | RED | DONE | The disagreement gap X. | 3 points of win chance (`SHARP_DISAGREE_PCT=3`, `KALSHI_MAX_GAP=3`; Kalshi gets 4 for games more than a day away and 5 for college; two sharp books on a prop 4). Open decision. |
| FAIRRESULT-02 | GREEN | DONE | Give the final fair probability and fair odds. | The card's "Fair value +100 · 50.0% to win"; ev_bets.csv `fair_odds`. |
| FAIRRESULT-01 | YELLOW | DONE | The fair-value result's form is up to the AI. | `SharpFair` per line (fair price, the sources in it, each source's own no-margin chances, the one left out) and `EVBet` (see "Data model"). |
| FAIR-16 | GREEN | DONE | Fewer sources lower confidence or stake, never the EV. | Only stake multipliers (`ONE_SOURCE_STAKE`, `KALSHI_ONLY_STAKE`, `SINGLE_SOURCE_STAKE`, `CONSENSUS_STAKE`, `CONFIDENCE_STAKES`); the edge is never changed. |
| EV-01 | GREEN | SUPERSEDED | At least 4.0% on moneylines, spreads and totals. | `MIN_EV_PCT=4` in balanced mode; locks mode (the default, as you asked) raises it to 5%; today's flood settings use 3%. |
| EV-02 | GREEN | SUPERSEDED | At least 7.0% on player props. | `PROP_MIN_EV_PCT=7` in balanced mode; locks 8%; today's flood settings 6%. |
| EV-05 | GREEN | DONE | The EV bars are easy to change. | `MIN_EV_PCT`, `PROP_MIN_EV_PCT`, `SPORT_MIN_EV`, `PREGAME_CONFIRMED_EV_PCT` and `ALERT_MODE`, through `.env`, `--set` or remote.env. |
| FRESH-01 | GREEN | DEVIATION | No hard expiry on DraftKings and FanDuel prices: show the price, you check it. | Cards show the price, its age and the price to skip at, but a book's price still has to be recent (`MAX_AGE_SECONDS`, `PREGAME_MAX_AGE_SECONDS`, `FAR_MAX_AGE_SECONDS`): an old stamp means the book took the market down. See "Deviations". |
| FRESH-02 | YELLOW | NOT_FEASIBLE | Candidate: sharp prices at most 10 seconds old. | 10 seconds isn't possible on this feed. Pinnacle has its own limits (`SHARP_MAX_AGE_SECONDS`, `SHARP_PREGAME_MAX_AGE_SECONDS`, `SHARP_FAR_MAX_AGE_SECONDS`); empty means the same as every book (2 minutes live, 15 near kickoff, 3 hours further out). Open decision. |
| FRESH-03 | GREEN | DONE | Leave a stale sharp out and carry on with the rest. | `sharp_fair` skips a stale sharp book's market and blends the rest; a Kalshi outage just leaves Kalshi out. |
| FRESH-04 | GREEN | DONE | If a sharp moves mid-calculation, start again on the newest data. | Each check prices everything from one fetch; the next check works it all out again and edits or closes the card. |
| FRESH-06 | GREEN | DONE | Check against the freshest data before sending. | Each check notes when its odds arrived. An alert that's only ready once they're older than `SEND_MAX_DELAY_SECONDS` (600; live `LIVE_SEND_MAX_DELAY_SECONDS`, 90) isn't sent; the next check sends it on its fresh odds, only if it still qualifies. The limits are an open decision. |
| FRESH-07 | GREEN | DONE | If the EV worked out again falls under the bar, no alert. | A held alert goes out only if the next check's numbers still clear the bar; otherwise it never posts. |
| FRESH-08 | GREEN | DONE | It must still qualify when it's sent. | As FRESH-06, and when Discord asks the bot to wait more than `DISCORD_MAX_WAIT_SECONDS` (10) before posting it, the alert waits for the next check instead of going out late. Bets are logged and followed only once their post went out. |
| STAKE-02 | GREEN | DONE | Use Kelly. | `kelly_stake`. |
| STAKE-03 | GREEN | DONE | Quarter Kelly. | `KELLY_FRACTION=0.25`, never more than `EV_MAX_STAKE_PCT` (3%) of `EV_BANKROLL`. |
| STAKE-06 | GREEN | DONE | Kelly uses the final fair probability and the book's odds. | `kelly_stake(fair_prob, price)`: the fair price with Kalshi in it when blended; Kalshi's prices after its fee. |
| STAKE-09 | GREEN | PARTIAL | Stakes shown in units, not dollars or a % of the bankroll. | Every bet card shows units ("$15 (1.5u)"), but the dollars come first, and arb cards stay in dollars (their bets have to add up exactly). Open decision. |
| STAKE-10 | GREEN | DONE | You decide what 1 unit is in dollars, and scale your risk by changing it. | `python3 arbbot.py --set UNIT_SIZE=20` also sets `EV_BANKROLL=2000`, so every stake scales. Setting `UNIT_SIZE` in `.env` without the matching `EV_BANKROLL` gets a warning at startup. |
| STAKE-11 | YELLOW | DONE | Candidate: 100 units is the whole bankroll. | 1u is 1% of `EV_BANKROLL` by default ($10 with $1,000; `UNIT_SIZE=0`). Open decision. |
| STAKE-07 | YELLOW | PARTIAL | One-source bets get smaller stakes than multi-source ones. | Kalshi-only bets and one-of-several-sharps bets bet half, props priced by other books 70%. Bets priced by Pinnacle alone keep the full stake unless you set `ONE_SOURCE_STAKE` (1 by default, so stakes didn't change). Open decision. |
| ALERT-01 | GREEN | DONE | Show the exact bet. | "bet $15 (1.5u) on Bruins ML +145" on the DO THIS line (`EVBet.pick`). |
| ALERT-03 | GREEN | DONE | Show the book's current odds. | The price in the title and the DO THIS line, the price to skip at, and every book's price. |
| ALERT-08 | GREEN | DONE | Show the fair probability and fair odds. | "Fair value", with how it has moved since the first alert. |
| ALERT-09 | YELLOW | DONE | Optional: show each source's own value. | The Sources line, "Sources 2/2 · Pinnacle 50.0% · Kalshi 52.0%" (`sources_line`), next to Pinnacle's odds and Kalshi's buy and sell prices. |
| ALERT-10 | GREEN | DONE | Show how many sources were used (3/3, 2/3, 1/3). | The same Sources line, out of the references that apply to that line (`SHARP_BOOKS`, plus Kalshi on pre-game moneylines); ev_bets.csv `sources`. |
| ALERT-11 | GREEN | DONE | Show the quarter-Kelly stake in units. | "$15 (1.5u)" on +EV, outlier and parlay cards (`stake_text`); ev_bets.csv `units`. |
| ALERT-13 | GREEN | DONE | Include a timestamp. | Discord's timestamp on every card, plus the game's start time and when it was spotted. |
| ALERT-06 | GREEN | DONE | One alert when a bet first qualifies. | `Alerter.handle`; open alerts survive restarts. |
| ALERT-07 | GREEN | DONE | Alert the same bet again only if its EV improves by 2.5+ points. | `REALERT_JUMP_PCT=2.5`, also when a bet turns from +EV into an outlier or back (`hand_over`): the new card pings only if its edge is 2.5 points better than the last ping. |
| ALERT-15 | GREEN | DONE | A bet that drops out and qualifies again may alert again. | It closes as GONE and alerts anew when it's back. |
| ALERT-16 | GREEN | DONE | Send a new Discord message instead of constantly editing the old one. | Every ping is a new message (first alert, a 2.5-point better edge, qualifying again). Edits only keep an open card's numbers current, and never ping. |
| ALERT-17 | GREEN | DONE | V1 doesn't need to mark old alerts as dead. | Not needed but allowed: closed cards turn grey with "GONE" so nobody chases them. |
| DELIVERY-01 | GREEN | DONE | Discord. | `_webhook`. |
| DELIVERY-02 | GREEN | SUPERSEDED | Every alert in one channel. | You asked for separate channels (arbs, +EV, outliers, parlays, live, health, results). Leave every webhook but `DISCORD_WEBHOOK_URL` empty and it's one channel again. |
| LINK-01 | GREEN | DONE | A link to the exact DraftKings or FanDuel bet whenever possible. | The feed's bet-slip link first (`INCLUDE_LINKS`), else the market's page; when the feed gives only the game's page or nothing, a bet-slip link built from the books' own ids (`INCLUDE_SIDS`, `book_link`), else the game's page. `--once` counts each kind per book. |
| LINK-04 | YELLOW | SUPERSEDED | Check whether ParlayAPI gives reliable DraftKings and FanDuel bet links. | You chose The Odds API, which sends bet-slip links for DraftKings and FanDuel. ParlayAPI's are unverified. |
| LINK-08 | YELLOW | DONE | Fallback: build links from the game, market and bet ids. | `INCLUDE_SIDS=true` (no credits): DraftKings `event/<game id>?outcomes=<bet id>`, FanDuel `addToBetslip?marketId=<market id>&selectionId=<bet id>`, only when the feed's link is missing or just the game's page, and both ids are there. Not yet seen in a live answer: open decision. |
| LINK-09 | YELLOW | DONE | Fallback: The Odds API for direct links. | The Odds API sends the links on every call, at no extra cost. |
| LINK-12 | YELLOW | NOT_FEASIBLE | Look for an official Pikkit API, SDK, deep link or partnership. | There isn't one: see "Not feasible on this feed". |
| LINK-10 | GREEN | DONE | No AI or browser hunting for each URL. | Links come only from the feed's own fields and ids. |
| LINK-11 | GREEN | DONE | DraftKings and FanDuel link code is separate and replaceable. | One small function per book in `LINK_ADAPTERS` (draftkings, fanduel, betmgm, williamhill_us), used through `book_link` wherever a link is picked. |
| LINK-14 | GREEN | DONE | Don't scrape Pikkit's private endpoints. | No Pikkit code at all. |
| FAIL-01 | GREEN | DONE | Tell you and pause what's affected when a data source fails; no unreliable alerts. | `Health` (`HEALTH_ALERTS`): the Odds API down for a sport (`ODDS_DOWN_MINUTES`; no alerts for it meanwhile), Pinnacle missing from a sport (`SHARP_DOWN_MINUTES`; Kalshi-only bets wait, props without Pinnacle at most medium), Kalshi unreachable (`KALSHI_DOWN_MINUTES`), credits available again: one message when it starts, one when it's over. A rejected API key stops the bot; running out of credits pauses it. |
| TRACK-01 | GREEN | SUPERSEDED | Scanner only: no results, win/loss, CLV or bankroll tracking. | You asked for CLV, results and a live record: `ClosingTracker`, `Results`, the scoreboard, the weekly card. Still no bankroll history. |
| LOG-01 | YELLOW | DONE | Log the candidates looked at and what was decided, not every price tick. | candidates.csv (`CANDIDATE_LOG_FILE`): +EV and outlier bets within 1 point of their bar and what happened to them (alerted, held back, or the check that stopped them); arbs.csv keeps held-back arbs. Open decision. |
| LOG-02 | YELLOW | DONE | A policy for how long logs are kept. | `LOG_KEEP_DAYS=60` for candidates.csv, arbs.csv and markouts.csv (cut once a day); bet logs, results, closing lines and score_checks.csv are kept for good. Open decision. |
| LOG-01/02 | YELLOW | DONE | Review the candidate logging and retention (section 18). | See LOG-01 and LOG-02. |
| CONFIG-01 | GREEN | DONE | The key settings can be changed without touching the code. | Every group the blueprint names is a setting: EV bars, weights (`SHARP_WEIGHTS`, `SHARP_WEIGHTS_PROPS`), freshness, sources on or off (`SHARP_BOOKS`, `KALSHI_CHECK`, `KALSHI_BLEND`, `KALSHI_ONLY`), sports, markets, `REALERT_JUMP_PCT`, Discord, stake multipliers, and outlier limits (`OUTLIER_SOURCE_PTS`). A few finer numbers stay in arbbot.py (Kalshi's price sums, the prop consensus bands, `SHARP_MOVE_PTS`, `CONFIRM_MIN_BOOKS`, the candidate log's 1 point and 15 minutes). |
| ARCH-06 | GREEN | DONE | Data ingestion. | `OddsAPI`, `Scheduler.fetch` and `fetch_props`, Kalshi's `kalshi_markets`. |
| ARCH-07 | GREEN | DONE | Normalization. | As DATA-04. |
| ARCH-08 | GREEN | DONE | Match exact bets across books and references. | `_line_for`, `base_market`, `sharp_fair`, `_kalshi_match`. |
| ARCH-09 | GREEN | DONE | Fair-value engine: take the margin out, check, drop outliers, weight. | `sharp_fair` and `blend_refs`: margin out, freshness and margin checks, the odd source left out, trust-order weights. |
| ARCH-10 | GREEN | DONE | EV engine: fair value against DraftKings and FanDuel, with the bars. | `find_evs`, only at your books (`MY_BOOKS`, `EV_BOOKS`). |
| ARCH-11 | GREEN | DONE | Staking engine: quarter Kelly, in units. | `kelly_stake`, `round_stake`, `Config.unit`, `stake_text`. |
| ARCH-12 | GREEN | DONE | Alert state: first alert, no duplicates, qualifying again, the +2.5 rule. | `Alerter.handle` and `hand_over` (the 2.5-point rule between +EV and outlier cards too); open alerts saved across restarts. |
| ARCH-13 | GREEN | DONE | A deep-link service. | `book_link`, with one adapter per book in `LINK_ADAPTERS`. |
| ARCH-14 | GREEN | DONE | An alert formatter. | `discord_payload`, `ev_payload`, `outlier_payload`, `parlay_payload`, `sources_line`. |
| ARCH-15 | GREEN | DONE | Discord delivery. | `_webhook` (a new alert's wait capped at `DISCORD_MAX_WAIT_SECONDS`, edits and status messages wait), one channel per alert type. |
| ARCH-16 | GREEN | DONE | Settings kept apart from the logic. | `Config`, read from `.env`, remote.env and `--set`; .env.example explains every setting. |
| ARCH-17 | YELLOW | DONE | The storage technology is up to the AI. | CSV logs (`append_csv` adds new columns to old files) and JSON state saved safely in `STATE_DIR`. |
| ARCH-18 | GREEN | PROCESS | The blueprint sets out the layers. | Where each layer lives: see "Data model". |
| ARCH-19 | GREEN | PROCESS | The AI picks the technical details. | One Python file, standard library only, a unittest suite, a systemd service with tested automatic updates. |
| DATA-MODEL-* | YELLOW | PROCESS | Propose the internal data structures. | See "Data model". |
| LINK-* | YELLOW | DONE | Compare the ways to get bet links. | Compared: The Odds API's links (used), links built from ids (`INCLUDE_SIDS`, built), ParlayAPI (unverified), Pikkit (no API). |
| SC-9 | GREEN | DONE | Watch NFL and NHL markets at DraftKings and FanDuel. | `SPORTS`, `BOOKMAKERS`, main-line and prop checks. |
| SC-10 | GREEN | PARTIAL | Watch the approved references. | Pinnacle, and Kalshi on game winners; Circa isn't feasible. |
| SC-11 | GREEN | DONE | Match identical markets and exact lines reliably. | As MATCH-04 and MATCH-05. |
| SC-12 | GREEN | DONE | Work out defensible fair probabilities. | Power method, weighted blend, prop consensus of 4+ books with the book being judged left out. |
| SC-13 | GREEN | DONE | Leave out unusable, stale or outlying references by the approved rules. | Old prices (`is_fresh`), wide margins (`MAX_SHARP_HOLD_PCT`), market gaps, Kalshi's liquidity rules, the odd source left out (`OUTLIER_SOURCE_PTS`). The numbers are open decisions. |
| SC-14 | GREEN | DONE | Work out EV correctly. | `EVBet.ev_pct`; tests check exact values. |
| SC-15 | GREEN | SUPERSEDED | Hold the 4% main-line and 7% prop bars. | As EV-01 and EV-02: locks 5% and 8%, today's flood settings 3% and 6%. |
| SC-16 | GREEN | DONE | Quarter Kelly, turned into units. | As ALERT-11. |
| SC-17 | GREEN | DONE | Check prices again before alerting and drop what no longer qualifies. | As FRESH-06 to FRESH-08. |
| SC-18 | GREEN | DONE | No duplicate spam; the +2.5 rule. | As ALERT-07. |
| SC-19 | GREEN | DONE | A clean Discord card with the source count and a timestamp. | The Sources line and the card's timestamp. |
| SC-20 | GREEN | DONE | Exact DraftKings and FanDuel links whenever possible. | As LINK-01. |
| SC-21 | GREEN | DONE | Pause and tell you when data fails. | As FAIL-01. |
| OOS-1 | OUT OF SCOPE | SUPERSEDED | Polished design or onboarding. | You asked for cleaner cards, clear instructions and a setup guide (docs/SERVER_SETUP.md, `--post-guide`). |
| OOS-2 | OUT OF SCOPE | DONE | Accounts for several users. | Not built. |
| OOS-3 | OUT OF SCOPE | DONE | Payments or subscriptions. | Not built. |
| OOS-4 | OUT OF SCOPE | DONE | Discord settings per user. | Not built: channels are per alert type. |
| OOS-5 | OUT OF SCOPE | DONE | A mobile app. | Not built. |
| OOS-6 | OUT OF SCOPE | DONE | A web dashboard. | Not built: a Discord scoreboard message instead. |
| OOS-7 | OUT OF SCOPE | DONE | Placing bets automatically. | Not built: you place every bet yourself. |
| OOS-8 | OUT OF SCOPE | SUPERSEDED | CLV tracking. | You asked for it: `ClosingTracker`, closing_lines.csv. |
| OOS-9 | OUT OF SCOPE | SUPERSEDED | Tracking bet results. | You asked for it: the results channel, ev_results.csv. |
| OOS-10 | OUT OF SCOPE | SUPERSEDED | Profit analytics. | You asked for a live record: the scoreboard and the weekly card. |
| OOS-11 | OUT OF SCOPE | DONE | Bankroll history. | Not built: `EV_BANKROLL` is a fixed setting. |
| OOS-12 | OUT OF SCOPE | SUPERSEDED | NBA or other leagues. | As SCOPE-07. |
| OOS-13 | OUT OF SCOPE | PARTIAL | Player-prop Unders. | Not alerted in locks mode (`PROP_SIDES`). Missing: today's flood settings (balanced, in remote.env) still send them; `PROP_SIDES=over` makes it DONE. An open decision. |
| OOS-14 | OUT OF SCOPE | DONE | Working out prop prices between lines. | Not built: EV only ever comes from the exact line. |
| HANDOFF-RULE | RULE | PROCESS | Don't code first: audit, list the Yellow and Red blockers, propose, wait for approval. | The bot already existed when the blueprint arrived. The audit and this page stand in for it, with the decisions under "Open decisions". |
| PROTOCOL-19 | RULE | PROCESS | One stage at a time: decide, explain, build, test, show, wait for approval. | You asked for automatic updates. Each change comes with tests that fail without it and must pass the full suite before it ships. |
| STAGES-20 | RULE | PROCESS | Stages 0 to 11, ending with a live trial that measures accuracy, speed, duplicates and links. | Every stage is in the running bot. For the live trial: candidates.csv (`post_delay`, near misses), markouts, CLV and `--once`'s link counts. |
| FIRST-23 | RULE | PROCESS | First: confirm the plan, audit, list the decisions, give a third opinion, propose a stack. | Third opinions given (keep The Odds API; ParlayAPI unverified; weights; units; links). This page is the record. |

## Data model

Where the blueprint's layers (ARCH-06 to ARCH-17) live in `arbbot.py`, and what they hand each other:

- **Settings** (`Config`): every `.env` setting, with remote.env on top and `ALERT_MODE` applied by
  `Config.with_mode`.
- **Odds** (`OddsAPI`, `Scheduler`): The Odds API's answer per game as it came, plus `_fetched_at` (when it
  arrived). `apply_fees` lowers Kalshi's prices by its fee and fills in BetMGM's state.
- **One line** (`_line_for`, `base_market`): the key that makes two prices the same bet: the market and exact
  point for game lines, the player and exact point for props. An alternate market counts as its main one.
- **Fair price** (`SharpFair` from `sharp_fair` and `blend_refs`; `Consensus` from `consensus_fair`; Kalshi's
  quotes from `kalshi_fair`): per line, each side's fair win chance, whose it is, each source's own chance
  with the margin out, and the source left out, if any.
- **A bet** (`EVBet`): game, line, side, book, price, link, the fair chance (`fair_prob`, and the sharp books'
  own `sharp_prob`), the Sources line's references (`refs`), stake, confidence, tier and the alternate-line
  flag. An arb is an `Arb` with a `Leg` per bet; a parlay is a `Parlay`.
- **Alert state** (`OpenArb`, kept by each `Alerter`): the Discord message, the edge last pinged for, whether
  the post went out (`sent`), whether it's held for old odds (`deferred`) and how long it took
  (`post_delay`). Saved in `STATE_DIR`, so a restart doesn't repeat alerts.
- **Logs** (CSV files; `append_csv` adds new columns to old files): arbs.csv (`LOG_FIELDS`), ev_bets.csv and
  outliers.csv (`EV_LOG_FIELDS`, now with `units`, `sources`, `pinnacle_prob`, `kalshi_prob`, `excluded`,
  `alt` and `post_delay`), parlays.csv, ev_results.csv, closing_lines.csv, markouts.csv, candidates.csv
  (`CANDIDATE_FIELDS`) and score_checks.csv.
