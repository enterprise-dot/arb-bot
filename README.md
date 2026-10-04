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
  back, or for `unconfirmed` first found); they're never counted as alerts. After a week, this tells
  you whether you can realistically catch them.
- **Bot health in Discord:** 🟢 online, 🔴 crashed, ⚠️ credits running low, and a 📊 daily summary.

## Your books

`MY_BOOKS` lists the books you bet at (e.g. `draftkings,fanduel,betmgm,williamhill_us,kalshi`).
Arbs, +EV, outliers, parlays and lock-in hedges only ever point to those books, and each card's
"Every book" list shows only them. The rest of `BOOKMAKERS` still helps: Pinnacle sets the true
odds and the others confirm the market price for outlier alerts, at no extra cost.

Kalshi (an exchange) charges a fee per trade, so its prices are lowered by that fee before any
comparison (`KALSHI_FEE_RATE`).

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

## 🎯 Player props

Passing/rushing/receiving yards and receptions (NFL), points/rebounds/assists/threes (NBA), and
points/shots/assists (NHL). Props are priced per game, so they're checked every 30 minutes in the
3 hours before kickoff, and the budget autopilot counts them. Fair odds come from Pinnacle when it
prices the prop, otherwise from the median of at least 4 other books (the book being judged doesn't
count, and BetOnline.ag and LowVig.ag count as one, since they share an owner), and the minimum
edge is 7%. Props appear as +EV, arb and outlier alerts. They're tracked for CLV and graded from box
scores (see "Is it working?" below).

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

## Locks mode (default)

The bot only sends alerts worth acting on: arbs that lock in **2%+** ($2 per $100, **5%+ when
live**), +EV bets of **5%+ at high confidence** (before the game **3.5%+** when two sharp sources
agree, see below), props at **8%+**, outliers at **15%+** (**20%+ when live**), and 2-leg parlays at
**15%+**. At most 4 arb, 6 +EV, 6 prop, 6 outlier and 2 parlay alerts go out an hour, and **3 live
alerts an hour in all**. Your own stricter settings in `.env` still win. Set `ALERT_MODE=balanced`
to use your own thresholds instead (that's also the way to loosen any of these).

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
the smaller edge already makes it smaller. A bet that clears 5% anyway is a normal bet. Once a card
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
📈 +5.4% EV | NHL | Bruins @ Rangers (starts 7:00 PM)
  Bruins ML +145 on DraftKings  → stake $10
  Fair +132 → +130 (43.5%, Pinnacle no-vig, 1/1 sources)
  Sharp: Pinnacle +125 / -145
```

Each +EV card in Discord shows Pinnacle's prices on both sides, how the fair price has moved
since the first alert, and a table of every book's price and edge on that bet.

**How it works:** Pinnacle takes big bettors and keeps a thin margin, so its lines are the
market's best guess at the real odds. The bot removes Pinnacle's margin (the "vig") to get each
side's fair probability. It alerts when a book you can bet at pays more than that, by at least
`MIN_EV_PCT` (4%).

The margin is removed with the **power method**. Books hide most of their margin in the long
shot, and this method takes more of it out there, so long shots don't look better than they are.

**What it costs:** nothing extra. Pinnacle is one of the 10 books in `BOOKMAKERS`, and 10 books
cost the same as one region.

**Stakes** use the Kelly formula: bet more when the edge is bigger. It uses a quarter of full
Kelly to soften the swings, and never more than 3% of `EV_BANKROLL` on one bet. Set `UNIT_SIZE`
to see stakes in units too.

**More than one sharp book (optional).** List several in `SHARP_BOOKS` (for example
`pinnacle,betfair_ex_eu`) and the bot blends their fair odds. Each alert shows how many sources
priced it ("Sources 2/2"). If the sharps disagree by more than `SHARP_DISAGREE_PCT`, the line is
skipped. If only one of them priced it, the stake is halved (`SINGLE_SOURCE_STAKE`).
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
`python arbbot.py --check-kalshi`.

**Bigger edge, new alert.** Discord doesn't ping you when a message is edited. So if a bet's edge
grows by 2.5 points or more while it's open (`REALERT_JUMP_PCT`), you get a fresh alert. For a live
bet in locks mode that fresh alert follows the live rules (two checks in a row, a price under 60
seconds old, one of the 3 live alerts an hour); until it can go, the card is just updated.

**Is it working? (what hit)** Every +EV, outlier, prop and parlay alert is logged. As games
finish, the bot grades them with the final scores (`ev_results.csv`) and posts each result to a
**results channel**: ✅ won / ❌ lost / ➖ push with the profit at the stake shown, plus the day's
record so far. Every morning it posts the whole previous day. Parlays are graded leg by leg (a
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

**Player props are graded from ESPN box scores** (free, no credits): points, rebounds, assists,
threes (NBA), goals, assists, points, shots on goal (NHL), passing/rushing/receiving yards and
receptions (NFL, college), hits and pitcher strikeouts (MLB). Before trusting a box score the bot
checks it adds up (players' points = the final score, goals = the score, runs = the score,
receptions = completions); if it doesn't, those props are left as 🎯 for you. A player who didn't
play is a push (books void those). Total bases isn't in the box score, so it stays manual.
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
credits, no extra pings. `MARKOUT_FILE=` (empty) turns it off.

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

## Tests

```bash
python -m unittest -v
```
