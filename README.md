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
prices the prop, otherwise from the median of at least 4 books, and the minimum edge is 7%. Props
appear as +EV, arb and outlier alerts. They're tracked for CLV but not graded win/loss (that needs
player stats).

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
live**), +EV bets of **5%+ at high confidence**, props at **8%+**, outliers at **15%+** (**20%+
when live**), and 2-leg parlays at **15%+**. At most 4 arb, 6 +EV, 6 prop, 6 outlier and 2 parlay
alerts go out an hour, and **3 live alerts an hour in all**. Your own stricter settings in `.env`
still win. Set `ALERT_MODE=balanced` to use your own thresholds instead (that's also the way to
loosen any of these).

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
  `MIN_CONFIDENCE=medium` drops the low ones.
- **Shaky prices are skipped**: a wide Pinnacle market (over 8% margin, 12% for props), or Pinnacle
  and the rest of the market 10+ points apart (one of them is stale).
- **Live arbs need both prices fresh**: priced within 60 seconds of each other, or it's usually
  just one book lagging. In locks mode every live alert also needs two checks in a row and a price
  under 60 seconds old (see "Alert mix" above).
- **CLV tracking** shows whether the bets beat the closing line, broken down by bet type, market,
  book, sport and confidence (`--results`, and the daily summary card).
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
A prop Pinnacle doesn't price uses the median of at least 4 other books instead, with a 30%
smaller stake (`CONSENSUS_STAKE=0.7`).

**Kalshi second opinion (free).** Before a game starts, the bot also reads Kalshi's own exchange
prices for game winners straight from Kalshi (no key, no Odds API credits). Kalshi's prices are
about as sharp as Pinnacle's. A moneyline +EV bet is skipped when Kalshi's win chance is more than
3 points from Pinnacle's (`KALSHI_MAX_GAP`; 4 for games over a day away, 5 for college), or when
Kalshi says the price isn't good. Outliers need Kalshi to agree as well. When it's within 2 points,
the card says "Kalshi agrees" and the bet's confidence goes up. Each card shows Kalshi's line
("Kalshi 51% to win, buy 52¢ · sell 50¢"). Kalshi prices only count when its market is tight
(`KALSHI_MAX_SPREAD=3` cents, 5 for college) and deep (`KALSHI_MIN_SIZE=100` contracts); if Kalshi
is down or a game isn't listed, alerts go ahead as before. For a bet at Kalshi itself, Kalshi's own
order book is used to confirm the price is still there instead. See what it matches with
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
```

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
| **Pre-game checks are slower** (every 15 min, only in the 2h before kickoff). | Pre-game gaps last longer, so they don't need minute-by-minute checks. These checks also feed the +EV alerts. |
| **Checks all due sports at once, in parallel.** | An alert goes out within seconds of the check. |
| **Bookmaker trick** (on by default) | Up to 10 named books cost the same as one region, so adding Pinnacle (EU) to your US books costs nothing. |

Each check of a sport costs **(# bet types) × (# regions)** credits, which is **3** with the defaults.

## Upcoming games, not just live ones

| Game starts | Main lines | Player props |
|---|---|---|
| Live | every 60s | none |
| Within 2h (props: 3h) | every 15 min | every 30 min |
| Within 24h | every hour | every 4 hours |
| Within 48h | every 3 hours | none |

One main-line check covers every game in a sport, so watching tomorrow's games costs almost
nothing. Early lines are often the softest, so +EV, outlier and parlay alerts for later games show
up well before kickoff (cards show the day: "Starts Sun 1:00 PM"). When credits are tight, the
upcoming-game checks slow down first; live and near-kickoff checks keep at least half the budget.

## What the $59 plan gets you

With 4 sports and all 3 bet types, a typical day has around 12–16 hours of live games
added up across sports. At one check a minute that's about 2,200–2,900 credits a day,
and the plan allows about 3,300 a day. So most days run at **full speed (every 60s)**. On the
busiest days the autopilot stretches checks to 70–90 seconds. Run `--plan` any time to see
the actual numbers for today.

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
