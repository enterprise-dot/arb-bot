# Arb Bot

Watches odds from many sportsbooks and pings Discord about two kinds of bets:

- **💰 Arbitrage:** the books disagree enough that betting every side guarantees a profit.
- **📈 +EV:** one book's price beats the "true" odds from Pinnacle, the sharpest book.
  You don't win every bet, but you come out ahead over many of them.

```
💰 3.50% ARB | NBA | New York Knicks @ Boston Celtics (🔴 LIVE)
  Moneyline
  • New York Knicks +145 on DraftKings  → stake $42.50  (bet first: +4.6% vs Pinnacle)
  • Boston Celtics -125 on FanDuel  → stake $57.50
  Total $100 → returns ≥ $103.50 (+$3.50)
```

**Every alert starts with a 👉 DO THIS line**: which book to open, how much to bet on what, and
the price where you should skip it because it has moved too far. Post a how-to guide to your
channel once and pin it, for anyone else using the alerts:

```bash
python arbbot.py --post-guide
```

**What a Discord alert does:**
- **Pings you** the moment a gap appears. Each bet has a **tap-to-bet link** when the book provides one.
- **Updates itself** if the prices shift while the gap is still open.
- **Turns grey with "❌ GONE after 45s"** when the gap closes, so you know not to chase it.
- **Lists the price that will move first.** When Pinnacle prices the line, 1️⃣ is the bet that beats
  Pinnacle's fair odds ("Bet this one first: it's the price that will move"). If the other price is
  gone by the time you get there and 1️⃣ is a good bet on its own, the card says "keep this one" and
  what you'd stake on it alone. Without a Pinnacle price the card says to place them back to back.
- **Shows how old the price was** when the alert went out ("⏱ price was 35s old when sent"). Player
  props don't show it: a book gives one time for all its players' lines, not for each one.
- **Uses round stakes** ($57.50 / $42.50 instead of $57.65 / $42.35), which look like normal bets to the books.
  The headline edge is always for the stakes printed, after rounding.
- **Live arbs are marked 🔴 LIVE** and need a bigger edge (1%; 5% in locks mode). They can go to
  their own channel (`DISCORD_LIVE_WEBHOOK_URL`) or be turned off (`ARB_LIVE=false`).
- **All arbs can be turned off** (`ARBS_ENABLED=false`): main lines and props. Everything else is unchanged.
- **After-tax line (optional, off by default).** Set `TAX_RATE` to your tax rate as a fraction
  (`0.33`) and each arb card adds a rough after-tax profit: "🧾 After tax (~33%): about +$1.70 per
  $100". The simple model: the winning bet's winnings are taxed, and the losing stakes are
  deducted at 90% (that assumes you itemize), whichever way the game goes. Small arbs often come
  out below zero this way. A rough guide, not tax advice. `MIN_AFTER_TAX_PCT` (0 = off) then skips
  arbs under that after-tax %.

**What else you get:**
- **`arbs.csv`:** every gap: when a check first found it (`spotted`), when the alert went out and how
  long it stayed open after that (`first_seen`, `seconds_open`), how many checks found it (`checks`,
  the ones that confirmed a live arb included), which bet was the price that would move and by how
  much (`stale_book`, `stale_edge_pct`), every bet's edge against Pinnacle (`leg_edges`) and how old
  each price was, Pinnacle's too (`leg_ages`; `?` = not known, e.g. a player prop). Arbs the hourly
  caps or the live rules held back are logged too, with a `reason` (`capped`, `live cap`, `old
  price`, `unconfirmed` = gone before a 2nd check found it; `first_seen` is then when it was held
  back, or for `unconfirmed` first found), and so are arbs whose alert never went out (`old data` =
  its odds were too old by the time it was ready, `not sent` = Discord never took it; see "How it
  keeps bets good"); they're never counted as alerts. After a week, this tells you whether you can
  realistically catch them. It keeps the last 60 days (`LOG_KEEP_DAYS`).
- **Bot health in Discord:** 🟢 online, 🔴 crashed, ⚠️ credits running low, and a 📊 daily summary.
  Outages get one message when they start mattering and one when they're over (`HEALTH_ALERTS=true`):
  - The Odds API not answering for a sport for 10 minutes (`ODDS_DOWN_MINUTES`): "Odds API not
    answering for NFL since 3:05 PM (timeouts): no NFL alerts until it's back. Open alerts stay up."
    Then "Odds API back for NFL after 12 min."
  - Pinnacle missing from every game of a sport that other books are pricing, for 15 minutes
    (`SHARP_DOWN_MINUTES`). Missing from the answers, not just an old price (an old price is normal).
    Meanwhile that sport's main-line +EV is paused (there are no fair odds, so its open +EV cards
    close, and post again once Pinnacle is back), Kalshi-only bets wait, props without a Pinnacle price
    are at most 🟡 medium (so none in locks mode), and arbs and outliers carry on. Then a message when
    Pinnacle is back.
  - Kalshi unreachable for 30 minutes while there are games before kickoff (`KALSHI_DOWN_MINUTES`):
    moneyline +EV goes out on Pinnacle alone. Then a message when it's back.
  - After a pause for credits: "Credits available again: scanning resumed."
  0 turns one of them off; `HEALTH_ALERTS=false` turns the messages off (the Pinnacle rules above
  still apply while it's missing). A failure long after the last one, with nothing asked in between
  (one at night, one in the morning), starts the clock again, and a prop request for a game that no
  longer exists (404) counts as the Odds API answering.
- **The Odds API's limits.** The bot spaces its own calls out (up to 20 at once, then 10 a second, so
  never more than the 30 a second the API allows), so a big slate of props doesn't trip the API's speed
  limit. A check of up to 20 calls goes out all at once, as before. If the API still says "slow down"
  (its answer says why: `EXCEEDED_FREQ_LIMIT`), that call is asked again 2 and 4 seconds later (a
  refused call costs nothing); if that doesn't do it, the health channel says "The Odds API asked the
  bot to slow down" and the bot tries again in a minute. When the API says the plan's credits are used
  up (`OUT_OF_USAGE_CREDITS`), the bot doesn't stop: it says so once, checks again quietly every hour
  until the plan resets or is upgraded, then says "Credits available again". Any other "too many
  requests" waits 15 minutes, and a key the API rejects still stops the bot, as before.
- **"Not enough Odds API credits for these settings."** If the budget autopilot has had to keep live
  and near-kickoff checks 3 or more times slower than set for half an hour, the health channel says so,
  once a day: how slow the checks are, when the plan resets and the credits left. Today's settings on
  the 100K plan never get there (2.4x at the very busiest); settings made for a bigger plan running on
  a smaller one do, all month. Right after an upgrade it can also mean the new credits haven't reached
  the bot yet.

## Your books

`MY_BOOKS` lists the books you bet at (e.g. `draftkings,fanduel,betmgm,williamhill_us,kalshi`).
Arbs, +EV, outliers, parlays and lock-in hedges only ever point to those books, and each card's
"Every book" list shows only them. The rest of `BOOKMAKERS` still helps: Pinnacle sets the true
odds and the others confirm the market price for outlier alerts, at no extra cost.

Kalshi (an exchange) charges a fee per trade, so its prices are lowered by that fee before any
comparison (`KALSHI_FEE_RATE`).

**Tap-to-bet links.** Each book name on a card opens that book. When the feed has a link for the bet
itself, it opens the bet in the book's bet slip (DraftKings and FanDuel send these for most bets);
otherwise the market's link, then the game's page. BetMGM's links are per state, filled in from
`US_STATE`. The feed also sends each book's own ids (`INCLUDE_SIDS=true`, no extra credits), so when
DraftKings or FanDuel only sent the game's page (or nothing), the bot builds the bet-slip link
itself: DraftKings `sportsbook.draftkings.com/event/<game id>?outcomes=<bet id>`, FanDuel
`sportsbook.fanduel.com/addToBetslip?marketId=<market id>&selectionId=<bet id>`, and only when both
ids are there. Each book's link logic is its own small function (`LINK_ADAPTERS` in arbbot.py), so
one can change without touching the others; BetMGM and Caesars use the feed's links as they are.
`python arbbot.py --once` ends with how each book's links came out, e.g. "Bet links per price:
DraftKings 210 bet slip, 6 built from ids; FanDuel 220 bet slip".

**New York:** New York's sportsbooks can't take bets on a game with a New York college team in it
(Syracuse, Army, Buffalo, St. John's, Cornell, Columbia, Fordham, Stony Brook, ...). Set
`US_STATE=ny` (or `NY_RULES=true`) and those games are never alerted at FanDuel, DraftKings, BetMGM,
Caesars, BetRivers, Fanatics or Bally Bet: not as arbs, +EV, outliers, hedges, parlays or rows on a
card's book list. Kalshi still counts. "Buffalo State" or "Albany State" aren't New York schools, so
they aren't caught. College games played *in* New York between two out-of-state teams (the Pinstripe
Bowl, a Madison Square Garden tournament) aren't caught yet: skip those yourself.

Change settings without editing files:

```bash
python arbbot.py --set MY_BOOKS=draftkings,fanduel --set EV_BOOKS=
```

## Channels

Arbs post to `DISCORD_WEBHOOK_URL`. Set `DISCORD_EV_WEBHOOK_URL` to send +EV alerts (and props,
outliers and parlays) to another channel; `DISCORD_OUTLIER_WEBHOOK_URL`,
`DISCORD_PARLAY_WEBHOOK_URL` and `DISCORD_LIVE_WEBHOOK_URL` split them further.
`DISCORD_PROPS_WEBHOOK_URL` gives player props (their +EV and outlier cards) a channel of their own, so
lots of prop types don't crowd out the other +EV bets: make a `#props` channel with a webhook, then run
`python arbbot.py --set-webhook props` and restart the bot. Empty, props go where +EV bets and outliers
go, as before. Prop arbs stay with the arbs, and cards already up stay where they were posted.

## 🎯 Player props

*On the $119 plan, remote.env asks for many more: 76 prop types across NFL, NBA, NHL and MLB, alternate
lines included, every 5 minutes in the last 12 hours and every 30 minutes from 2 days out. See "On the $119
plan" below. This section describes the defaults.*

Passing/rushing/receiving yards and receptions (NFL), points/rebounds/assists/threes (NBA), and
points/shots/assists (NHL), plus, in the last 3 hours before kickoff, NHL goals and goalie saves and
NFL alternate lines (`PROP_NEAR_MARKETS`, see below). Props are priced per game, so they're checked
every 30 minutes in the 3 hours before kickoff, and the budget autopilot counts them. If The Odds API
ever rejects an extra prop type, the bot asks again without it, says so once in the health channel,
and keeps checking that sport's other props. If it rejects one of the usual ones (`PROP_MARKETS`), only
that one stops until a restart: the bot finds which (the API's answer names it, or each type is asked
alone once; a refused call costs nothing), the health channel says "The Odds API rejected these NBA
prop types: ... (check PROP_MARKETS). The other NBA props carry on.", and that game's props come in
without it. Only if the API turns down every type does that sport's props stop until a restart (the
health channel then says to check `PROP_MARKETS`). Fair odds come from Pinnacle
when it prices the prop, otherwise from the median of at least 4 other books (the book being judged
doesn't count, and BetOnline.ag and LowVig.ag count as one, since they share an owner), and the
minimum
edge is 7%. Props appear as +EV, arb and outlier alerts. They're tracked for CLV and graded from box
scores (see "Is it working?" below).

**Alternate lines, exact line only.** US books often hang a different main line than Pinnacle (Allen
249.5 passing yards at DraftKings, 245.5 at Pinnacle), but their alternate ladder ("250+", "245+"...)
often has Pinnacle's exact number. Near kickoff the bot also asks for the NFL alternate passing,
rushing and receiving yards and receptions, and when a book's alternate line sits at exactly the point
Pinnacle prices (or the other books' main lines do), that price is judged against that fair price like
any other (a Pinnacle-priced prop can be high confidence at the full stake, instead of a consensus
price or nothing). It's the same bet as the main line at that point (one card, the better of the two
prices, the main line's on a tie), the card says *(alternate line)* so you look for it in the book's
alternate lines (so do a parlay's legs and an outlier's lock-in legs priced that way), and ev_bets.csv
marks it in the `alt` column. Alternate lines never set the fair odds themselves (they're priced off
each book's own main line, with more margin), and the bot never guesses a price between two points.
The "book moved first" hold (outliers, and props judged against the other books) reads the book's main
line at that point: an alternate price that pays more the first time it's seen isn't the book moving,
but its main line moving away from the others still is. The props console line counts them: `alternate
lines: 12 matched, 1 alerted` (matched = an alternate price with a fair price at exactly its line).

**Credits:** NHL goals and saves add 2 credits per NHL game per near-kickoff check (about 7 checks:
every 30 minutes in the last 3 hours, plus the closing check), the NFL alternates 4 per NFL game.
They're paid for only with credits the day has to spare: the bot asks for them only when the next 24
hours fit at full speed with them added, after the near-kickoff speed-up of main-line checks, so they
never slow a live or main-line check. On a short day they wait (`--plan` and the "Budget:" line say
so) and the usual props carry on. In the simulated October below, where the plan's credits are all
used, that left them on about 1 near-kickoff check in 20 (about 200 credits; NHL ~80, NFL ~120), and
live and main-line checks kept their pace (within 1%, the month's spare spending).
`PROP_NEAR_SPARE_ONLY=false` asks for them on every near-kickoff check instead, paid for like the rest
of the prop check: about 3,570 credits that month (NHL ~1,810, NFL ~1,760), so main-line checks came
about 4-5% less often (3-6% by sport) and live checks 2-5 seconds further apart on average. To turn
them off for good, put `PROP_NEAR_MARKETS=icehockey_nhl=;americanfootball_nfl=` in `.env` (or one
sport only; with `--set`, quote it:
`python3 arbbot.py --set "PROP_NEAR_MARKETS=icehockey_nhl=;americanfootball_nfl="`).

**Overs or both sides** (`PROP_SIDES`): in locks mode only prop Overs are alerted, as the blueprint
asks; in balanced and all mode both Overs and Unders are (as before). `PROP_SIDES=over` or
`PROP_SIDES=both` picks for yourself whatever the mode. The Under's price is still used to work out the
Over's fair odds, prop arbs still need both sides, and main-line Unders (totals) aren't affected.

**A slate starting together** (`PROP_MAX_PER_PASS`, 0 = no limit): with props checked every few minutes,
a Sunday's or a busy night's games can all be due at once. `PROP_MAX_PER_PASS=8` asks for at most 8
games' props in one pass: the closing-line checks first, then the games most overdue (the longest past
their next check, counted in their own pace). The rest go in the next pass, a second or so later, so the
Odds API isn't asked for dozens at once and the next live check isn't held up. It doesn't change how
many credits props use. With many prop types two more settings help: `DISCORD_PROPS_WEBHOOK_URL` gives
props a channel of their own (see "Channels"), and `CARD_EDIT_MIN_SECONDS` edits open cards less often
(see "How it keeps bets good"). How the bot handles the Odds API's speed limit, and the message when the
settings need more credits than the plan has, are under "Bot health in Discord" above.

## 📦 Parlays

Built from the +EV bets open right now: 2-3 legs from different games at the same book, where each
leg is +EV there. Parlay EV is the legs' edges multiplied together, so two 5% legs make a ~10%
parlay. Stakes are capped at 1% of `EV_BANKROLL` because parlays swing a lot.

## 🚨 Outliers

```
🚨 OUTLIER +45% | NHL | Bruins @ Rangers (🔴 LIVE)
  Rangers ML -118 on Caesars  → stake $30
  Fair -361 (78.3%, median of 4 other books)
  🔒 Lock in +26.5%: also bet Bruins +300 on DraftKings
```

When one book is far off every other book, it usually just hasn't updated yet. The bot compares
each price with the **median of all the other books** (so it works even if Pinnacle is slow),
alerts at a 10%+ edge (`OUTLIER_MIN_PCT`), has no upper limit, and covers live games. If betting
the other side elsewhere locks in a profit, the alert shows that hedge with stakes per $100.

Books can cancel bets on obvious pricing errors ("palpable errors"), and the bigger the gap,
the more likely that is. Bet fast, and expect the occasional void.

**Bad prop data is left out.** One morning BetMGM's NHL "points" came through priced like goals (Roope
Hintz 1+ point at +190, -125 everywhere else), and 27 prop outliers with 47-80% edges went out at once.
Before every prop check the bot now leaves out, for outliers, +EV, arbs and parlays alike:

- a prop price more than 40% off the median of the other books (`OUTLIER_PROP_MAX_PCT`, 0 = no cap):
  usually a book whose market isn't the same bet. Real long shots that far off are held back too, and so
  are arbs built on such a price (they're the ones books void);
- a book's whole prop type in a game when it's off by the outlier bar on 4 or more players there
  (`OUTLIER_PROP_CLUSTER`, 0 = off) and on at least 60% of the players it can be compared on. News moves
  one team (about half a game's players) or a few teammates, and those still go out. Once left out, that
  market stays out until it's been back in line for 2 checks, so one price near the bar can't make its
  cards go GONE and come back with new pings every other check.

The sharp book is never judged this way: when Pinnacle moves first on news, it's the others that are
behind. A line Pinnacle prices and calls no edge isn't counted as off either. Both show in
candidates.csv ("too big", "book market off"), and the log says when a market is left out and when it's
back. Main-line outliers keep no upper limit.

## Locks mode (default)

The bot only sends alerts worth acting on: arbs that lock in **2%+** ($2 per $100, **5%+ when
live**), +EV bets of **5%+ at high confidence** (before the game **3.5%+** when two sharp sources
agree, see below), props at **8%+** (Overs only, see "Player props"), outliers at **15%+** (**20%+
when live**), and 2-leg parlays at **15%+**. At most 4 arb, 6 +EV, 6 prop, 6 outlier and 2 parlay
alerts go out an hour, and **3 live alerts an hour in all**. Your own stricter settings in `.env`
still win. Set `ALERT_MODE=balanced` to use your own thresholds instead (that's also the way to
loosen any of these).

**Pre-game bets from 3.5% when two sharp books agree.** Lines before the game are efficient, so few
bets get to 5% against Pinnacle. A moneyline, spread or total for a game that starts within 24 hours
(`CONFIDENT_HOURS`) can go out from **3.5%** (college games **4.5%**) when a second sharp source puts
the fair odds where Pinnacle does:

- **Moneylines:** Kalshi's chance to win is within 1.5 points of Pinnacle's (`KALSHI_CONFIRM_PTS`).
  No usable Kalshi price, or a bet at Kalshi itself, means no.
- **Spreads and totals:** the middle price of at least 4 other sportsbooks is within 1.5 points of
  Pinnacle's (`CONSENSUS_CONFIRM_PTS`). The book you'd bet at, exchanges like Kalshi and a second
  sister book (BetOnline.ag and LowVig.ag count as one) don't count toward the 4.

The edge has to clear 3.5% (college 4.5%) by **both** chances, so the less generous one decides; the
card says so, e.g. "At least +3.6% by both". Nothing else gets easier. It still has to be **high
confidence** (never medium, even if you set `MIN_CONFIDENCE` lower), Pinnacle's line can't be moving
against it (1.5+ points since an hour ago, or since the last check when checks are further apart, up
to 3 hours back; right after a restart, or on a brand-new line, it waits one check so there's
something to compare with), and every other check stays: Kalshi's usual check, your books
(`MY_BOOKS`), New York's rules, price age, `MAX_EV_PCT`, `EV_MAX_ODDS` and the +EV hourly cap (5%+
bets go first). Props, outliers and parlays don't get the lower edge (and these bets are never a
parlay leg). The card says **✅✅ Two sharp books agree: Pinnacle and Kalshi** (or "Pinnacle and the
other books") with a line on why the smaller edge is fine. The stake is the usual Kelly stake, so
the smaller edge already makes it smaller. A bet that clears 5% anyway is a normal bet, but it has to
clear 5% by Pinnacle's own price too: one only Kalshi's blended-in price (below) lifts over 5% is still a
confirmed bet, with every check above. Once a card
is up, Kalshi or one of the books having no price for a check (or a restart) doesn't close it: it
stays up quietly, with no new ping. If they disagree with Pinnacle instead, it closes as usual.

`PREGAME_CONFIRMED_EV_PCT` sets that edge: empty means 3.5 in locks mode and off in the other modes,
0 turns it off. In locks mode your own value is used only when it's stricter: a higher edge, or 0.
`KALSHI_CONFIRM_PTS` and `CONSENSUS_CONFIRM_PTS` work the same way (a smaller number is stricter).
`ev_bets.csv` and `markouts.csv` mark these bets `tier=confirmed`, the CLV tables and the weekly
report card show them as their own group, "Confirmed pre-game (3.5%+)", and the card suggests
`PREGAME_CONFIRMED_EV_PCT=0` if 50+ of them lose to the closing line. The console line after each
check says how many went out and why the near misses didn't, e.g. `✅✅ confirmed pre-game: 1 sent,
3 near misses (2 no Kalshi price, 1 not high confidence)`.

**Props Pinnacle doesn't price can be locks too.** Their fair odds are the median of the other
books, so the bot checks how much those books agree instead of Pinnacle's margin. A lock needs all
of these: the other books within 2% win chance of each other (one odd book is ignored), at least 6
sportsbooks on that exact line (exchanges like Kalshi count in the median but not toward the 6),
kickoff within 12 hours, and an 8-12% edge (bigger gaps go out as outliers). It bets 30% less
(`CONSENSUS_STAKE`). The card names the books the fair price comes from, e.g. "consensus of
DraftKings, BetMGM, Caesars, ESPN BET, BetRivers", and says how many of them you can't bet at.

- **Two looks first.** A prop line the bot hasn't seen before waits one prop check (30 minutes near
  kickoff), so `--once` never sends one. If the book with the good price just moved away from the
  others (it may have seen injury or lineup news first), the bot waits a prop check for the others
  to catch up before alerting, and waits again each time that book moves further.
- **Pinnacle still has a say.** If Pinnacle prices the same player at another point (24.5 instead
  of 25.5), or has taken this exact line down, the prop is never a lock (until Pinnacle prices the
  line again). If Pinnacle's other line, or its last price on this line before it came down, shows
  there's no edge at all (Over 25.5 can't be likelier than Pinnacle's Over 24.5), it isn't alerted.
- **An alert that's up stays up** while it's still at least medium confidence (one book pulling its
  line, the books drifting a little apart, the edge growing past 12%, or its book moving further
  away from the others). The card turns 🟡 instead of a false "GONE", and it doesn't ping again.
  Restarts don't change that.

The props console line says how many prices were held back and which lines missed "high" and why,
e.g. `2 prop prices held (1 first look / 1 moved first) | 3 no-Pinnacle props missed high (2 under 6
sportsbooks, 1 books disagree)`.

## Alert mix: fewer live alerts, more for later today and tomorrow

Live prices move in seconds, so live alerts are the ones most likely to be gone when you tap them.
In locks mode:

- **Live alerts are rationed.** Live arbs need 5%+, live outliers 20%+, and every live alert (arbs,
  outliers, live +EV) shares one cap of 3 an hour (`LIVE_PER_HOUR`). Games that haven't started
  never count toward it.
- **Games that haven't started go first.** In each check, alerts for games that haven't started go
  ahead of live ones, then the biggest edge, so a nearly full cap keeps the best. Across the hour
  it's first come, first served: once the slots are used, new alerts wait for the next hour (if
  they're still there). Arbs and prop arbs share the arb cap
  (`MAX_ARB_PER_HOUR=4`), outliers and prop outliers the outlier cap (`MAX_OUTLIER_PER_HOUR=6`). An
  arb whose price that will move is on Kalshi, with your sportsbook bets at normal prices, ranks
  slightly higher: it's easier on your sportsbook accounts.
- **Tonight's and tomorrow's games can be high confidence.** A +EV bet gets its "close to kickoff"
  point for games within 24 hours (`CONFIDENT_HOURS`, was 12), so more +EV alerts for later today
  and tomorrow pass the locks-mode "high confidence" bar. Props still need to be within 12 hours.
- **A live alert has to still be there.** A new live alert only pings when two checks in a row (about
  a minute apart) find it (`LIVE_CONFIRM_CHECKS=2`), and only when the price you're told to bet was
  updated by its book in the last 60 seconds (`LIVE_MAX_AGE_ALERT=60`; Pinnacle's price can be
  older). Ones that vanish within seconds never ping. Updates to alerts already up, and alerts for
  games that haven't started, aren't delayed. After a restart a live alert waits one more check, and
  `--once` (a single look) never sends one.
- **Already-up alerts follow the same rules.** A much better price on a live alert that's already
  up only gets its new ping when two checks in a row find it with a price under 60 seconds old, and
  it counts toward the 3 an hour; until then the card is just updated. A pre-game alert still up
  when the game starts, whose bet turns into a live alert of the other kind (+EV ↔ outlier), is
  closed, and the live one is a new live alert.
- **Each card shows how old the price was** when the alert went out (⏱; not on player props).

The console line after each check says what was held back and why, e.g.
`held back: 2 live, waiting for another check, 1 over the live-alert cap`.

## How it keeps bets good

- **Confidence on every +EV bet** (🟢 High / 🟡 Medium / 🟠 Low), from how tight Pinnacle's own
  market is, whether the other books agree with Pinnacle, whether the game starts within 24 hours
  (`CONFIDENT_HOURS`; props 12), and whether the edge is believable. Stakes scale 100% / 75% / 50%.
  `MIN_CONFIDENCE=medium` drops the low ones. For a prop Pinnacle doesn't price, the first two
  checks become how closely the other books agree and whether 6+ sportsbooks price it.
- **Shaky prices are skipped**: a wide Pinnacle market (over 8% margin, 12% for props), or Pinnacle
  and the rest of the market 10+ points apart (one of them is stale).
- **Old prices are ignored.** A price counts only if its book updated it in the last 2 minutes
  (live, `MAX_AGE_SECONDS`), 15 minutes (within 2 hours of kickoff, `PREGAME_MAX_AGE_SECONDS`) or
  3 hours (further out, `FAR_MAX_AGE_SECONDS`). The Odds API stamps a market each time it sees it,
  so an old stamp usually means the book took the market down. Pinnacle's prices, which set the
  fair odds, can have their own limits: `SHARP_MAX_AGE_SECONDS`, `SHARP_PREGAME_MAX_AGE_SECONDS` and
  `SHARP_FAR_MAX_AGE_SECONDS` (empty = the same as every book, so nothing changes until you set
  them). Markouts read Pinnacle by the same limits.
- **Nothing goes out on odds that went stale while it waited.** Each check notes when its odds
  arrived. A new alert, or a much better price's new ping, that's only ready once those odds are
  older than 10 minutes (`SEND_MAX_DELAY_SECONDS=600`; live games 90 seconds,
  `LIVE_SEND_MAX_DELAY_SECONDS=90`; 0 = no limit) isn't sent: the next check looks again and sends it
  on its fresh odds, only if it still qualifies then. A check normally takes a second or two, so this
  only matters when a pass is very slow or Discord makes the bot wait. When Discord asks the bot to
  wait more than 10 seconds before posting a new alert (`DISCORD_MAX_WAIT_SECONDS=10`; 0 = always
  wait), it doesn't: the console says "Discord didn't take it" and that alert goes out at the next
  check instead, if it's still there. Edits (a price change, the ❌ GONE mark, "see the newer alert")
  and health messages have no next check to go out at, so they always wait as long as Discord asks. An
  alert is logged (`ev_bets.csv`, `outliers.csv`, so results) and followed (markouts) only once its
  post went out, and `post_delay` in those files is the seconds from fetching its odds to that post
  (the bot's real speed, to set these limits from). Parlays aren't checked this way (they're built
  from alerts already up), but a bet waiting for the next check is never a parlay leg.
- **Fewer card edits (optional).** With checks every minute, an open +EV or outlier card would be
  edited on almost every check as other books' prices move, and every edit waits out Discord's limits.
  `CARD_EDIT_MIN_SECONDS=600` edits such a card at most every 10 minutes when only other books' prices
  or its notes changed; the next check after that edits it with the newest prices. A new price, book or
  stake for the bet itself, the ❌ GONE mark, a much better price (a new alert) and live bets are still
  edited right away, and arbs and parlays always are. 0 (the default) edits every change right away.
- **Live arbs need both prices fresh**: priced within 60 seconds of each other, or it's usually
  just one book lagging. In locks mode every live alert also needs two checks in a row and a price
  under 60 seconds old (see "Alert mix" above).
- **CLV tracking** shows whether the bets beat the closing line, broken down by bet type, market,
  book, sport, confidence and where the fair price came from (`--results`, and the daily summary
  card). `ev_bets.csv` and `outliers.csv` say whose fair odds each bet used (`fair_from`), and
  `tier=confirmed` marks a confirmed pre-game bet (see "Locks mode"). A prop's
  closing line follows the same rules as its alert, with the alerted book left out.
- **Restarts don't repeat alerts**: open alerts are remembered, so an update edits the existing
  cards instead of posting them again.

## +EV bets

```
📈 +5.5% EV | NHL | Bruins @ Rangers (starts 7:00 PM)
  Bruins ML +145 on DraftKings  → stake $10 (1u)
  Fair +134 → +132 (43.1%, Pinnacle + Kalshi no-vig)
  Sources 2/2 · Pinnacle 42.5% · Kalshi 45.0%
  Sharp: Pinnacle +125 / -145
```

Each +EV card in Discord shows Pinnacle's prices on both sides, how the fair price has moved
since the first alert, and a table of every book's price and edge on that bet.

Below the line, a **Sources** line says which fair-price references apply to the bet and what each
one gives this side once its margin is taken out, so you can compare them:
"Sources 2/2 · Pinnacle 42.5% · Kalshi 45.0%". The references are the books in `SHARP_BOOKS`, plus
Kalshi on a moneyline before the game in a sport Kalshi lists (`KALSHI_CHECK`; not live, even with
`KALSHI_LIVE`, and not with a Kalshi weight of 0 in `SHARP_WEIGHTS`). One that applies but
had no usable price says so ("Sources 1/2 · Pinnacle 42.5% · Kalshi: no usable price"); a missing
Kalshi price never makes the stake smaller. Spreads and totals have Pinnacle only ("Sources 1/1"). A
prop Pinnacle doesn't price says "Sources: median of 5 books (no Pinnacle price)". `ev_bets.csv`
keeps the same line in its `sources` column.

**How it works:** Pinnacle takes big bettors and keeps a thin margin, so its lines are the
market's best guess at the real odds. The bot removes Pinnacle's margin (the "vig") to get each
side's fair probability. It alerts when a book you can bet at pays more than that, by at least
`MIN_EV_PCT` (4%).

The margin is removed with the **power method**. Books hide most of their margin in the long
shot, and this method takes more of it out there, so long shots don't look better than they are.

**What it costs:** nothing extra. Pinnacle is one of the 10 books in `BOOKMAKERS`, and 10 books
cost the same as one region.

**Stakes** use the Kelly formula: bet more when the edge is bigger. It uses a quarter of full
Kelly to soften the swings, and never more than 3% of `EV_BANKROLL` on one bet.

**Units.** Every bet card (+EV, outlier, parlay) shows the stake in dollars and in units, like
"$15 (1.5u)". One unit is 1% of `EV_BANKROLL` ($10 with the usual $1,000, so 100 units is the whole
bankroll), or `UNIT_SIZE` dollars if you set it. The unit only labels the stake: the dollars always
come from `EV_BANKROLL`. To bet more or less, change the unit with
`python arbbot.py --set UNIT_SIZE=20`: that also sets `EV_BANKROLL=2000`, so every stake grows with
it. If you set `UNIT_SIZE` in `.env` and 100 units isn't `EV_BANKROLL` (say you edited just one of
them), the bot warns you when it starts. The "online" message says what 1u is. Arb cards stay in
dollars (their bets have to add up exactly). `ev_bets.csv` and `outliers.csv` have a `units` column.

**More than one sharp book (optional).** List several in `SHARP_BOOKS` (for example
`pinnacle,betfair_ex_eu`) and the bot blends their fair odds. Each alert's Sources line lists them
("Sources 2/2 · Pinnacle 50.0% · Betfair 49.1%"). If the sharps disagree by more than
`SHARP_DISAGREE_PCT` (props: `SHARP_DISAGREE_PROP_PCT=4`), the line is skipped. If only one of them
priced it, the stake is halved (`SINGLE_SOURCE_STAKE`).

**One odd source is left out (3 or more sources).** When three or more sources price a line (sharp
books, plus Kalshi on a pre-game moneyline), the bot checks each one against the middle of the
others. If the farthest is more than 3 points of win chance away (`OUTLIER_SOURCE_PTS`; props 4,
`OUTLIER_SOURCE_PROP_PTS`), it's left out of that line's fair price, and the rest must still agree
within `SHARP_DISAGREE_PCT`. Only one is left out per line, and never Pinnacle: if Pinnacle is the odd
one out, the line is skipped (whatever order `SHARP_BOOKS` lists them in; it's the source with the
most weight, and a tie in weight goes to the more trusted one). The card's Sources line says so
("Sources 2/3 · Pinnacle 50.0% · BetOnline 50.4% · LowVig left out (5.8 pts off)") and `ev_bets.csv`
has an `excluded` column. Nothing is remembered between checks: a source left out counts again as soon
as it's back within the limit.
With just two sources, a gap over `SHARP_DISAGREE_PCT` (Kalshi: `KALSHI_MAX_GAP`) skips the line, as
before. With today's settings (Pinnacle plus Kalshi) this never comes up.

**How much each source counts.** The blueprint trusts Pinnacle most, then Circa (not in our odds
feed), then Kalshi. So by default Pinnacle counts 0.5, Kalshi 0.15 and any other sharp book 0.15,
shared out over the sources that price a line: Pinnacle + Kalshi is 77% Pinnacle, 23% Kalshi.
`SHARP_WEIGHTS` changes them (for example `pinnacle=0.6,kalshi=0.2`); a source you leave out keeps its
default, and 0 means it isn't used at all (a line only it prices then counts as having no sharp price).
`SHARP_WEIGHTS_PROPS` does the same for player props (empty = `SHARP_WEIGHTS`). The bot warns you at
startup if a weight puts any source above Pinnacle: that source then leads the fair odds, and with 3+
sources Pinnacle can be the one left out.
A prop Pinnacle doesn't price uses the median of at least 4 other books instead (not counting the
book being judged; BetOnline.ag and LowVig.ag count as one), with a 30% smaller stake
(`CONSENSUS_STAKE=0.7`).

**Kalshi second opinion (free).** Before a game starts, the bot also reads Kalshi's own exchange
prices for game winners straight from Kalshi (no key, no Odds API credits). Kalshi's prices are
about as sharp as Pinnacle's. A moneyline +EV bet is skipped when Kalshi's win chance is more than
3 points from Pinnacle's (`KALSHI_MAX_GAP`; 4 for games over a day away, 5 for college), or when
Kalshi says the price isn't good. Outliers need Kalshi to agree as well. When it's within 2 points,
the card says "Kalshi agrees" and the bet's confidence goes up. Each card shows Kalshi's line
("Kalshi 51% to win, buy 52¢ · sell 50¢"). Kalshi prices only count when its market is tight
(`KALSHI_MAX_SPREAD=3` cents, 5 for college) and deep (`KALSHI_MIN_SIZE=100` contracts); if Kalshi
is down or a game isn't listed, alerts go ahead as before. For a bet at Kalshi itself, Kalshi's own
order book is used to confirm the price is still there instead, and to see how much it has at that
price (contracts x price). When the stake is more than that, the card says "Kalshi only has about
$43 at this price; the rest would fill at a worse price", and a +EV or outlier stake is cut to
what's there (rounded down the usual way). An arb with a Kalshi bet gets the same note, but its
stakes stay as they are (its bets must stay balanced). See what it matches with
`python arbbot.py --check-kalshi` (it also prints Kalshi's own rules text for each sport's markets,
when Kalshi's reply has it).

**Kalshi in the fair price (`KALSHI_BLEND=true`).** When a pre-game moneyline passes those checks and
Kalshi has a usable price for both teams, Kalshi's price also goes into the fair odds, at its weight
(77% Pinnacle, 23% Kalshi by default). Pinnacle 50% and Kalshi 52% make a fair price of 50.5%. The
card says "Pinnacle + Kalshi", and the edge and the stake use that price. Everything that checks a bet
still uses Pinnacle's own price: the gap to Kalshi, "Kalshi agrees", confirmed pre-game bets, which
way Pinnacle's line is moving, markouts and CLV. A bet at Kalshi uses Pinnacle's price alone (Kalshi
can't vouch for itself, nor veto itself: when Kalshi's quote disagrees with the sharp books so much
that the line gets no fair price, a bet at Kalshi still has Pinnacle's). Spreads, totals and props are
Pinnacle only (Kalshi doesn't price them). A missing Kalshi price never makes a stake smaller.
`ev_bets.csv` keeps both in `pinnacle_prob` and `kalshi_prob`, so the results can be compared with and
without Kalshi. Note that moneyline edges logged from this change on are worked out slightly
differently from earlier ones. `KALSHI_BLEND=false` goes back to Pinnacle alone.

**Kalshi alone (`KALSHI_ONLY=true`).** Sometimes Pinnacle doesn't list a game's moneyline at all
(often small college games) while it does list the sport's other games. Then a pre-game moneyline
can be priced by Kalshi alone, when both teams have a usable Kalshi price and at least 3 other
sportsbooks (not exchanges: Kalshi can't check itself) are within 10 points of it
(`SHARP_CONSENSUS_MAX_GAP`). These bets are never more than 🟡 Medium (so never in locks mode),
never at Kalshi itself, and bet half the usual stake (`KALSHI_ONLY_STAKE=0.5`). The card says "Kalshi
(no Pinnacle price)" and "Sources 1/2 · Kalshi 52.0% (no Pinnacle price)"; the console line counts
them ("Kalshi-only: 2"). A Pinnacle price that's just old doesn't count as missing (an old stamp
usually means Pinnacle took the market down), and if Pinnacle has no moneylines at all in that
sport, nothing goes out on Kalshi alone. Markouts don't re-check these (they'd need Kalshi's price),
only whether the price was still there.

**NFL ties.** An NFL game can end in a tie. Sportsbooks refund a moneyline bet then; Kalshi settles
its market by its own rules. So a +EV, outlier or arb card with a Kalshi bet on an NFL moneyline adds
one line: "Kalshi settles ties by its own rules; sportsbooks refund a tie." Ties are rare and the edge
is worked out the same way; it's there so a tie doesn't surprise you. `--check-kalshi` shows Kalshi's
wording.

**One source, smaller stake (optional).** `ONE_SOURCE_STAKE` cuts the stake when Pinnacle alone sets
the fair price (spreads, totals, props, moneylines without a usable Kalshi price). It's 1 (the full
stake, as before) unless you change it; `ONE_SOURCE_STAKE=0.75` bets a quarter less on those.

**Bigger edge, new alert.** Discord doesn't ping you when a message is edited. So if a bet's edge
grows by 2.5 points or more while it's open (`REALERT_JUMP_PCT`), you get a fresh alert. For a live
bet in locks mode that fresh alert follows the live rules (two checks in a row, a price under 60
seconds old, one of the 3 live alerts an hour); until it can go, the card is just updated. The same
rule holds when a bet turns from +EV into an outlier or back (its old card points to the new one):
the new card pings you only if its edge is 2.5 points better than the edge you were last pinged
for; otherwise it goes up without the ping.

**One alert per bet (`ONE_ALERT_PER_BET=true`).** A bet goes out once. A better price later, the same bet
at another book, or the bet turning into an outlier never sends a second alert, and the card isn't edited
to show the better price: you bet it once, at the stake shown. The bot remembers every bet it has alerted
(`state/alerted_bets.json`) until a day after its game, so a restart or an update doesn't send any of them
again. The card is still marked GONE when the price goes.

While it's up, the card's only change is one line, edited without a ping: **✅ Still good at <book>** while that book
still has a good price, **⚠️ Price moved** when it doesn't.

**Tuning from the record (`TUNE_ENABLED=true`).** Every hour the bot reads its own logs. A book whose price is
usually gone by the next check (still there under 40% of the time, 30+ bets) needs 2 more points of edge before its
bets go out. A book or sport that clearly beats the closing line (50+ bets) gets 25% bigger stakes; one that clearly
loses to it gets half. The card says so ("📊 Stake ×1.25: ..."). Until a book or sport has enough bets, nothing
changes for it. Settings: `TUNE_*` in `.env.example`.

**Sports to drop when credits get tight (`SHED_SPORTS=soccer`).** If the next 24 hours can't be checked at full
speed, those sports stop being checked before anything else slows down, and the health channel says so. They come
back once everything fits with 10% to spare.

**✅ bet tracking (`DISCORD_BOT_TOKEN`).** With a Discord bot's token set on the server, every new bet card gets a ✅.
Members tap it when they place the bet (and can take it back until the game starts). The nightly results card then
adds "👥 Members took 7 of 12 alerts · 5-2 · +3.1u" and the top three members, in units at each card's stake.
Who took what is kept in `state/taken_bets.json` on the server.

**Bets on one game (`SAME_GAME_FULL`, `SAME_GAME_STAKE`, `SAME_GAME_MAX`).** Bets on one game win and lose
together, so the first two alerts on a game are at full stake, the next ones at half (the card says so), and there
are at most four per game.

**Keeping it safe.** `HEALTHCHECK_URL` (healthchecks.io) tells you when the bot stops checking in.
`DISCORD_BACKUP_WEBHOOK_URL` gets a zipped copy of the betting record every night. The health channel is told when
results can't be graded, when Discord refuses cards, and when updates stop reaching the server.

**Public results page (`RESULTS_SITE_REPO`, `RESULTS_SITE_TOKEN`).** Every night the bot uploads `site/index.html` and
every graded pre-game bet (in units, with CLV, losses included; nothing before its game is over) to a GitHub Pages
repo. The token can write only that repo, never this one.

**Test channel (`DISCORD_TEST_WEBHOOK_URL`).** `arbbot.py --set-webhook test` sets a private channel for
`--test-discord`: each alert type that's on sends a sample there, pinging its book's role like a real alert.

**Sportsbook pings (`BOOK_ROLES`).** Give each sportsbook a Discord role and list them:
`BOOK_ROLES=DraftKings=<role id>,FanDuel=<role id>`. Each new +EV, prop, outlier and parlay card then pings
the role of the book it's at, and nothing else (no @everyone). Edits never ping. A book with no role pings
nobody. Adding a book later is one more `Book=id` entry.

**Is it working? (what hit)** Every +EV, outlier, prop and parlay alert is logged. As games
finish, the bot grades them with the final scores (`ev_results.csv`) and posts each result to a
**results channel**: ✅ won / ❌ lost / ➖ push with the profit at the stake shown, plus the day's
record so far. Every morning it posts the whole previous day. With `RESULTS_DAILY_ONLY=true` there are no
posts as bets settle: one card per New York day (by game start) goes out between 12:30 and 1:00am, once the
day's games are graded, with the record, profit, amount staked, ROI, each alert type and the day's CLV. Parlays are graded leg by leg (a
pushed leg drops out, like at the books). Grading main lines costs 2 credits per sport with a
finished bet, at most every `RESULTS_MINUTES` (30).

**ESPN's free scoreboard is checked next to the paid scores** (`FREE_SCORES=shadow`). It doesn't grade
anything yet: `score_checks.csv` notes, once per game, whether ESPN's final agreed with the Odds API's,
so it can take over (and save those credits) once it has proven itself. It only counts a final when
everything checks out: a final status, the teams the right way round, the periods adding up, no tied
hockey, basketball or baseball final, and in hockey ESPN's winner flag. `--results` prints how often
it agreed. **Rain-shortened MLB games:** when a game ends before the 9th inning, run lines and totals
on it show once on the results card as 🌧️ "game ended early (rain): check your book" (books usually
void them), and they don't count in the record. The moneyline is still graded.

**Player props are graded from box scores** (free, no credits; NHL.com and MLB's own stats site first,
then ESPN). These prop types grade by themselves, alternate lines included:
- NFL (and college): passing yards, TDs, completions, attempts and interceptions; rushing yards and
  attempts; receptions; receiving yards; rushing + receiving yards; anytime TD.
- NBA: points, rebounds, assists, threes, blocks, steals, blocks + steals, turnovers, and points +
  rebounds + assists, points + rebounds, points + assists, rebounds + assists.
- NHL: goals, assists, points, shots on goal, blocked shots, goalie saves, anytime goal scorer.
- MLB: hits, total bases (MLB's box score only: ESPN's has no doubles or triples), home runs, RBIs,
  runs, walks, batter strikeouts, hits + runs + RBIs; pitcher strikeouts, outs, earned runs, hits and
  walks allowed.

Before trusting a box score the bot checks it adds up (players' points = the final score, goals = the
score, runs = the score, receptions = completions); if it doesn't, those props are left as 🎯 for you.
A player who didn't play is a push (books void those). A player the box score has no number for (a
rushing + receiving yards bet on a quarterback who only threw) is left for you too. Any other prop
type stays manual.
Check it against real games any time with `python arbbot.py --check-props`. `PROP_GRADING=off`
turns it off.

**Scoreboard:** one message at the top of the results channel that the bot keeps editing: today,
the last 7 days and all time, by bet type and live vs pre-game, plus bet quality (CLV) and whether
alert prices held up (📏 markouts, below). Pin it.

Make a `#results` channel and connect it: `python arbbot.py --set-webhook results`. Without it,
results go to the bot health channel. On the command line:

```bash
python arbbot.py --results              # today's bets one by one, then 7-day and all-time records
python arbbot.py --results yesterday    # or a date: --results 2026-10-03
python arbbot.py --post-results         # post today's card to the results channel now
python arbbot.py --weekly               # the weekly report card for the last 7 days (free)
python arbbot.py --post-weekly          # post it to the results channel now
```

**📋 Weekly report card (Mondays).** Every Monday at the daily summary time (`SUMMARY_HOUR`; -1 turns
it off too) the results channel (or the health channel, without one) gets the last 7 days for each
alert type: pre-game arbs, live arbs, +EV (with confirmed pre-game bets on a line of their own),
outliers, props and parlays. For each: the record and profit at the stakes shown (arbs: what they
locked in at `BANKROLL`, each arb once a day, as in the daily recaps), ROI, CLV for pre-game bets,
whether the edge held a few minutes later (📏 markouts and how often the price was still there), and
how many alerts the hourly caps and live rules held back (each alert once a day, however many checks
held it back). Then suggestions in plain English, for example:

```
• Live outliers: 64 bets, sent +22.0% → -1.1% ~3 min later (still there 38%) → consider OUTLIER_LIVE=false
• Props with no Pinnacle price: CLV +3.1%, beat the close 68% over 22 bets → keep
• Pre-game outliers: CLV +0.4%, beat the close 50% → too early to tell (12 bets)
• Confirmed pre-game (3.5%+): CLV -0.8%, beat the close 41% over 56 bets → consider PREGAME_CONFIRMED_EV_PCT=0
```

A change is only suggested on enough bets, by the rules the bot already uses: live alert types by
the 📏 markout rule (50+ bets and below 0 even at the high end), pre-game ones by CLV (50+ bets with
CLV below 0 and the close beaten less than half the time). "Keep" needs 5+ bets beating the close
(CLV above 0, the close beaten more often than not), the same test as the daily summary's ✅.
Anything else says "too early to tell (N bets)". **The bot never changes a setting by itself**: the
card only says what the numbers point to. It goes out once a week, even across restarts (and a
`--post-weekly` the same Monday counts). Each line works with its file missing or empty.

**Closing line value (CLV)** is the faster test. For every logged +EV and outlier bet, the bot
keeps tracking Pinnacle's fair price until kickoff and saves it as the closing line
(`closing_lines.csv`), with one last check in the final 5 minutes for games you have a bet on.
CLV is how much better your price was than that closing fair price. Beating the close on most
bets is the clearest sign the edge is real, long before the win/loss record settles down:

```
CLV, all time: avg +2.4%, beat the close on 68% of 54 bets      (example)
```

This assumes you bet every alert at the alerted price. Give it a few hundred bets before judging;
50 bets is mostly luck.

**Markouts (📏 did the edge hold?)** CLV needs a closing line, so it can't judge live bets (most
outliers), and win/loss takes thousands of bets to mean anything. So the bot also looks at every
alert's price again on its next checks of that game: the next check, about 3 minutes later and
about 10 minutes later (it keeps the real time, since checks slow down on a tight budget). It
measures your price against the same fair odds the alert used: Pinnacle for +EV, the other books
for outliers. "Sent +12% → later +3%" means the card said +12% and, a few minutes later, that price
was still worth 3% more than fair. It also notes whether the book still had the price (at or above
the skip line) on the next check (a line every book has moved off counts as gone), and who moved: the book fixing its price (it was slow: a real
edge) or the other books moving to it (it was just fast: no edge). Saved in `markouts.csv`; shown on
the scoreboard, in the daily summary and in `--results`, by alert type, book and sport, with a 95%
range. Each bet counts once, like everywhere else. ✅ = 50+ live bets and above 0 even at the low
end; ⚠️ = 50+ live bets and below 0 even at the high end: review that alert type. Pre-game and
props get no ✅/⚠️: their checks can be hours apart and only alerts re-checked within an hour are
measured, so CLV is the better test there (the cards say how many were measured). No extra
credits, no extra pings. `MARKOUT_FILE=` (empty) turns it off. `markouts.csv` keeps the last 60
days (`LOG_KEEP_DAYS`, see "Candidate log"), so these summaries cover the last 60 days, not all time.

**Candidate log (`candidates.csv`).** Every +EV and outlier bet that came within 1 point of its bar,
and what happened to it, so the bars can be tuned from what nearly went out: `alerted` (with
`post_delay`, the seconds from fetching its odds to its post), held back (`capped`, `live cap`, `old
price`, `unconfirmed`, `deferred` = its odds were too old by sending time), or the check that stopped
it: `sharp hold` (Pinnacle's margin too wide), `market gap` (Pinnacle and the other books too far
apart), `kalshi gap` / `kalshi no` (Kalshi too far off, or says the price isn't good), `sharp no`
(an outlier Pinnacle says isn't good), `sharp other line` / `sharp last price` (a prop Pinnacle prices
at another point, or took down, says no edge), `confirm fail: ...` (a confirmed pre-game bet whose
second source didn't agree), `low confidence`, `moved first` / `first look` (the price guard), or
`under bar` (the best price on that side, within a point under the bar). Each row has the game,
line, book, price, fair odds and where they came from, the Sources line, Kalshi's chance, the edge
and the confidence. The same bet and decision is written at most every 15 minutes.
`CANDIDATE_LOG_FILE=` (empty) turns it off; `--once`, `--demo` and `--dry-run` never write it.
Once a day `candidates.csv`, `arbs.csv` and `markouts.csv` drop rows older than 60 days
(`LOG_KEEP_DAYS`; 0 = keep everything). The bet logs (`ev_bets.csv`, `outliers.csv`, `parlays.csv`,
`ev_results.csv`, `closing_lines.csv`) and `score_checks.csv` (its running "ESPN agreed on N of M" is
how far it can be trusted) are never cut.

**Good to know:**
- **Pre-game only by default.** Live +EV is mostly the feeds updating at different times,
  not a real edge. Pre-game checks run every 15 minutes in the 2 hours before kickoff
  (`PREGAME_MINUTES`, `PREGAME_HOURS`).
- **Long shots are skipped** (`EV_MAX_ODDS=5.0`), because their fair odds are the least reliable.
- **Books limit +EV bettors faster than arbers.** Mixing in some normal bets helps.

## Setup

Needs Python 3.10+. No packages to install.

1. Get an API key at https://the-odds-api.com (this is built around the $59/mo, 100K-credit plan).
2. Create a Discord webhook: channel ⚙️ → Integrations → Webhooks → New Webhook → Copy URL.
3. `cp .env.example .env`, then paste both values in. Set `BILLING_DAY` to the day your plan renews.
4. Check it:
   ```bash
   python arbbot.py --demo --dry-run   # sample data, no API key needed
   python arbbot.py --test-discord     # one sample alert to your channel
   python arbbot.py --plan             # today's schedule + credit forecast (uses NO credits)
   ```
5. Run it: `python arbbot.py`. Leave it running. It sleeps by itself when nothing is on.

## How it saves credits

| What it does | Why it matters |
|---|---|
| **Only checks sports with a game in progress.** It reads the schedule from the free events endpoint. | Most hours of the day most sports have nothing live. Those hours cost **0** credits. |
| **Out-of-season sports cost nothing.** | List every sport you care about. They only cost credits once games are on. |
| **Stops checking a game once books pull it** (the game ended). | It doesn't keep paying for a game that's over. |
| **Budget autopilot.** Every few minutes it compares the next 24h of games with the credits you have left and the days until reset. | Checks every 60s when you can afford it. On a packed day it slows down *just enough* to make the credits last. It never runs dry mid-month. |
| **Optional: live checks ask for moneylines only** (`LIVE_MARKETS=h2h`, off by default). | A live check costs 1 credit instead of 3, and it brings every upcoming game's moneyline in that sport along. Spreads and totals of upcoming games get their own pre-game check (2 credits) with every live check (`PREGAME_WITH_LIVE_EVERY=1`), so at full speed a sport with a live game costs 3 credits a minute, as before. The saving comes on a tight day (next row) and once no later game is waiting. Live spreads and totals aren't checked, so those live alerts stop. |
| **With that on, on a tight day the live check slows down first** (up to every 3 minutes, `LIVE_MAX_STRETCH`). | The spreads and totals of later games keep their every-minute check, and the other pre-game checks keep their pace. Upcoming games' moneylines come with the live check, so they slow down with it. If that isn't enough, upcoming-game checks slow down, then early props and games 1-2 days out slow down further, and only then the rest. The log says once a day when live checks have stayed at their slowest for half an hour. |
| **Spare credits buy faster pre-game checks, never faster live ones.** | Near kickoff first (down to every 3 min), then later today (every 15 min), early props (hourly), props near kickoff (every 15 min), and 1-2 days out last. What's left over carries over to the coming days. |
| **It learns what each check really costs.** The API says after every call what it charged. | Player props cost only what books have posted (an empty answer is free), so early prop checks usually cost less than the formula. `--plan` shows the measured costs, and the daily summary says where the credits went. |
| **Pre-game checks are slower** (every 15 min, only in the 2h before kickoff). | Pre-game gaps last longer, so they don't need minute-by-minute checks. These checks also feed the +EV alerts. |
| **Checks all due sports at once, in parallel.** | An alert goes out within seconds of the check. |
| **Bookmaker trick** (on by default) | Up to 10 named books cost the same as one region, so adding Pinnacle (EU) to your US books costs nothing. |

Each check of a sport costs **(# bet types) × (# regions)** credits: **3** with the defaults. With
`LIVE_MARKETS=h2h`: **1** for a live check (moneylines only) and **2** for the pre-game check of a
sport that also has a live game (with every live check).
**Why it's off by default:** in a full-month simulation it checked tonight's and tomorrow's
spreads/totals more often, but the moneylines of later games in a sport with a live game 30-55% less
often (they ride on the live check, which slows first). Turn it on if you'd rather have fewer live
checks and more spreads/totals: `--set LIVE_MARKETS=h2h`.

**Testing a cheaper live check:** one combined call might cover every sport's live games at once.
Before the bot uses it, check it a few times on a busy night (2+ sports live):
`python arbbot.py --check-upcoming`. It costs about 1 credit plus 1 per sport with a live game, changes
nothing, and says whether the combined call had every live game and book the sports' own calls have.

## Upcoming games, not just live ones

| Game starts | Main lines | Player props |
|---|---|---|
| Live | moneylines every 60s (locks mode; other modes: every bet type) | none |
| Within 2h (props: 3h) | every 15 min, down to every 3 min with spare credits | every 30 min, down to 15 |
| Within 24h | every hour, down to every 15 min | every 4 hours, down to 1 hour |
| Within 48h | every 3 hours, down to every hour | none |

While a game in a sport is live, the live check also brings every upcoming game's moneyline in that
sport every 60 seconds, for the same credit, and their spreads and totals come every 60 seconds too,
in their own check (`PREGAME_WITH_LIVE_EVERY`). One check covers every game in a sport, so watching
tomorrow's games costs almost nothing. Early lines are often the softest, so +EV, outlier and parlay
alerts for later games show up well before kickoff (cards show the day: "Starts Sun 1:00 PM").

When credits are tight, live checks slow down first (locks mode: up to every 3 minutes, and the
moneylines of upcoming games with them), then the upcoming-game checks (early props and games 1-2
days out most); the spreads and totals of later games and near-kickoff checks slow down last, and
keep at least half the budget. On a day with credits to spare, the faster pre-game pace above uses
them (at most 85% of the day's share, `SPARE_USE_PCT`).
`--plan` and the log's "Budget:" line say which checks were slowed down or sped up.

## What the $59 plan gets you

With 4 sports, a typical day has around 12–16 hours of live games added up across sports. At full
speed a sport with a live game costs 3 credits a minute: 1 for the live check (moneylines of every
game) and 2 for the spreads and totals of its later games, both every minute. The plan allows about
3,300 a day, so on busy days the live check slows down first (up to every 3 minutes) and the credits
go to games that haven't started. A simulated October (88K credits left on Oct 4, reset Nov 4; NFL,
college football, MLB playoffs, NHL, NBA, props on), old setup → now, average checks per game:

| Sport | Spreads & totals, last 2h | Spreads & totals, 2–24h out | Moneyline, last 2h | Moneyline, 2–24h out | Live check, typical gap |
|---|---|---|---|---|---|
| NFL | 68 → **74** | 133 → **154** | 68 → 49 | 133 → 99 | 64s → 70s |
| College football | 67 → **83** | 392 → **511** | 67 → 43 | 392 → 203 | 77s → 156s |
| MLB | 24 → **33** | 191 → **241** | 24 → 15 | 191 → 128 | 81s → 178s |
| NHL | 41 → **52** | 243 → **315** | 41 → 27 | 243 → 135 | 79s → 180s |
| NBA | 42 → **51** | 175 → **223** | 42 → 25 | 175 → 92 | 86s → 180s |

Spreads and totals of games that haven't started are checked 10–35% more often in every sport, and
the night's first games get a few more checks near kickoff than before. Moneylines of later games
come with the live check, so on a tight day they slow down with it: they're checked about a third to
half less often than before. Live spreads and totals aren't checked at all. The credits lasted the
month in every run, also with real costs 30% above the formula. Run `--plan` any time to see the
actual numbers for today.

## On the $119 plan

The Odds API's $119 plan has 5,000,000 credits a month (50 times the $59 plan's). remote.env turns on what
that pays for (upgraded Mon Oct 5):

- **Every minute** for the moneylines, spreads and totals of every game in the next 48 hours, and live
  games every 40 seconds (`POLL_SECONDS=40`, as often as the Odds API refreshes live odds).
- **76 prop types** with their alternate lines (`PROP_MARKETS`): NFL passing yards, TDs, completions,
  attempts and interceptions, rushing yards and attempts, receptions, receiving yards, rushing + receiving
  yards and anytime TD; NBA points, rebounds, assists, threes, the four combos, blocks, steals, blocks +
  steals and turnovers; NHL points, shots, assists, goals, goalie saves, blocked shots and anytime goal
  scorer; MLB hits, total bases, home runs, RBIs, runs, walks, hits + runs + RBIs and pitcher strikeouts,
  outs, earned runs, hits allowed and walks. Every 5 minutes in the last 12 hours before a game, every 30
  minutes from 48 hours out, at most 8 games per pass (`PROP_MAX_PER_PASS`).
- **Men's college basketball** (`basketball_ncaab` in `SPORTS`) from November, under the same rules as
  college football (6% for +EV, New York's sportsbooks left out of games with a New York college team).
- Credits spread almost evenly across the week (`BUDGET_WEIGHTS`), a low-credits warning at 250,000
  (`LOW_CREDITS`), and open cards edited at most every 10 minutes for small changes
  (`CARD_EDIT_MIN_SECONDS=600`).

A simulated month at these settings used about 70,000 credits a day in October and 89,000 a day in November
(2.1 to 2.7 million of the 5 million), with no check ever slowed down. Credits reset on the 1st at midnight
UTC (`BILLING_DAY=1`). The bot reads the credits left from every answer and the budget autopilot still slows
things down if they ever run short. `--plan` shows today's numbers.

## Run it 24/7 on a server (~$5/month)

Any small Ubuntu server works (DigitalOcean, Hetzner, Vultr, Linode: the cheapest plan is plenty).

1. In GitHub, create a token that can read this repo: Settings → Developer settings →
   Fine-grained tokens → Generate. Pick only this repo, with Contents set to Read-only.
2. SSH into the server as root and run:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/<you>/arb-bot/main/deploy/install.sh -o install.sh
   bash install.sh https://<TOKEN>@github.com/<you>/arb-bot.git
   ```
   (For a private repo, copy `deploy/install.sh` over with `scp` instead of curl.)
3. Follow the 4 steps it prints: fill in `.env`, check `--plan`, start the bot, watch the logs.

Never set up a server before? Follow [docs/SERVER_SETUP.md](docs/SERVER_SETUP.md). It covers every click on DigitalOcean.

After that it starts when the server boots and restarts itself within a minute if it crashes.
A wrong API key stops it instead of restart-looping, and you'll get a 🔴 message on Discord either way.

| Do this | Command |
|---|---|
| Watch live | `journalctl -u arbbot -f` |
| Stop / start | `systemctl stop arbbot` / `systemctl start arbbot` |
| Update to the latest code | Automatic every 10 minutes after `bash /opt/arb-bot/deploy/enable-auto-update.sh` (tested first, undone if it breaks, posted in Discord). By hand: `cd /opt/arb-bot && sudo -u arbbot git pull && systemctl restart arbbot` |
| Settings pushed for you | `remote.env` in the code folder: settings Claude sends through the automatic updates (it can't log into your server). They win over `.env`; `--set REMOTE_SETTINGS=off` ignores them. The 🟢 online message says when any are in use. |
| Download the arb log | `scp root@SERVER:/opt/arb-bot/arbs.csv .` |

## Before betting real money

- **Check both prices yourself before you bet.** Live odds move in seconds. Place 1️⃣ first
  when the card says "Bet this one first": it's the price that will move. Otherwise place the
  harder leg first (the bigger price, or the book likelier to limit you).
- **Books limit or close accounts** that look like they're betting arbs. Round your stakes,
  and don't only bet arbs.
- **Rules differ between books** (overtime, retired players, voids). One side can be voided
  while the other stands.
- **Fund each sportsbook account with a card or bank account in the account holder's own name.**
  Books check that it matches, and a mismatch can freeze your winnings.
- Make sure sports betting is legal where you are, and only use licensed books.

## The blueprint

Your friend's blueprint gives every requirement an ID (FAIR-09, STAKE-08, ...) and a colour: Green is
fixed, Yellow and Red need your say. [docs/BLUEPRINT.md](docs/BLUEPRINT.md) lists every ID, what the bot
does about it and which setting controls it, the choices made for you on the Yellow and Red ones ("Open
decisions": tell Claude if you want any different), what can't be done on this odds feed, and where the
bot follows your own requests instead. Print the Yellow and Red ones any time, with your current settings
for each open decision (free, nothing is sent):

```bash
python arbbot.py --blueprint
```

## Tests

```bash
python -m unittest -v
```
