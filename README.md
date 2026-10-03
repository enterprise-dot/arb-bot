# Arb Bot

Watches odds from many sportsbooks and pings Discord about two kinds of bets:

- **💰 Arbitrage:** the books disagree enough that betting every side guarantees a profit.
- **📈 +EV:** one book's price beats the "true" odds from Pinnacle, the sharpest book.
  You don't win every bet, but you come out ahead over many of them.

```
💰 3.76% ARB | NBA | New York Knicks @ Boston Celtics (🔴 LIVE)
  Moneyline
  • Boston Celtics @ 1.80 on FanDuel  → stake $58
  • New York Knicks @ 2.45 on DraftKings  → stake $42
  Total $100 → returns ≥ $102.90 (+$2.90)
```

**What a Discord alert does:**
- **Pings you** the moment a gap appears. Each bet has a **tap-to-bet link** when the book provides one.
- **Updates itself** if the prices shift while the gap is still open.
- **Turns grey with "❌ GONE after 45s"** when the gap closes, so you know not to chase it.
- **Uses round stakes** ($58 / $42 instead of $57.65 / $42.35), which look like normal bets to the books.

**What else you get:**
- **`arbs.csv`:** every gap and how long it stayed open. After a week, this tells you whether you can realistically catch them.
- **Bot health in Discord:** 🟢 online, 🔴 crashed, ⚠️ credits running low, and a 📊 daily summary.

## +EV bets

```
📈 +5.4% EV | NHL | Bruins @ Rangers (starts 7:00 PM)
  Bruins ML @ 2.45 on DraftKings  → stake $10
  Fair odds 2.32 (Pinnacle no-vig)
```

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

**Bigger edge, new alert.** Discord doesn't ping you when a message is edited. So if a bet's edge
grows by 2.5 points or more while it's open (`REALERT_JUMP_PCT`), you get a fresh alert.

**Is it working?** Every +EV alert is logged to `ev_bets.csv`. Once games finish, the bot grades
them with final scores and keeps a record in `ev_results.csv`. Grading costs 2 credits per sport
per day. The daily Discord summary shows the record, or check any time:

```bash
python arbbot.py --results
# Example output: +EV record, all time: 212 bets, 108-98-6, +$187.40 on $2,450 staked (ROI +7.6%, avg edge 4.3%)
```

This assumes you bet every alert at the alerted price. Give it a few hundred bets before judging;
50 bets is mostly luck.

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
| Update to the latest code | `cd /opt/arb-bot && sudo -u arbbot git pull && systemctl restart arbbot` |
| Download the arb log | `scp root@SERVER:/opt/arb-bot/arbs.csv .` |

## Before betting real money

- **Check both prices yourself before you bet.** Live odds move in seconds. Place the
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
