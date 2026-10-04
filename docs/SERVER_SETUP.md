# Server Setup Guide (DigitalOcean)

Step-by-step instructions for getting the Arb Bot running 24/7 on a DigitalOcean server.
No server experience needed. Every click and every command is spelled out.

**Time:** about 30–40 minutes. **Cost:** $6/month for the server.

---

## Read this first (for the assistant doing the setup)

If you are an AI assistant (such as Claude Cowork) following these steps for the user:

- **Secrets go in exactly one place:** the `.env` file on the server (Part 5), plus the
  GitHub token in the install command (Part 4). Never paste them into a chat, email, document,
  website form or search box other than those.
- **Ask the user for each secret when you reach the step that needs it.** Don't collect them
  all up front, and don't invent or guess values.
- **Stop and ask the user before anything that costs money or can't be undone:** creating the
  Droplet (Part 3, step 11), choosing a paid Odds API plan (Part 1), and destroying anything.
- **If a step doesn't match what you see** (a button is renamed, a page looks different), look
  for the closest equivalent. If you can't find one, stop and tell the user what you see.
  Don't improvise with other commands.
- **Check off each part's "You're done when" line** before moving on.

---

## What you'll need

| Item | Where it comes from | Used in |
|---|---|---|
| DigitalOcean account | You already have one | Part 3 |
| Odds API key | Part 1 | Part 5 |
| Discord webhook URL | Part 2 | Part 5 |
| GitHub token | Part 2b | Part 4 |
| A password manager or a safe note | Somewhere private | Part 3 |

---

## Part 1: Get your Odds API key

1. Go to **https://the-odds-api.com**.
2. Click **Get API Key** (or **Plans**) and choose the **100K credits plan ($59/month)**.
   - *Assistant: confirm the plan and price with the user before paying.*
3. Finish signup. Your **API key** (a long string of letters and numbers) arrives by email.
4. Note the **date your plan renews**. The day of the month (for example `14`) is your
   `BILLING_DAY`.

**You're done when:** you have the API key and the renewal day.

---

## Part 2: Make a Discord webhook

This is the link the bot uses to post alerts into a Discord channel.

1. Open Discord, then the server where you want alerts.
2. Create a channel for them: click **+** next to "Text Channels", name it `arbs`, then click
   **Create Channel**.
3. Hover over the new channel, click the **⚙️ gear** (Edit Channel), then **Integrations** →
   **Webhooks** → **New Webhook**.
4. Click the new webhook, rename it **Arb Bot**, then click **Copy Webhook URL**.
   It looks like `https://discord.com/api/webhooks/123.../abc...`
5. Click **Save Changes**.
6. **Phone alerts:** in the Discord app on your phone, long-press the `#arbs` channel →
   **Notification Settings** → **All Messages**.

**You're done when:** you have the webhook URL copied somewhere safe.

## Part 2b: Make a GitHub token

The code is in a private GitHub repo. This token lets the server download it, read-only.

1. Go to **https://github.com/settings/personal-access-tokens/new** (signed in as
   `enterprise-dot`).
2. Fill in:
   - **Token name:** `arb-bot server`
   - **Expiration:** 1 year (or "No expiration")
   - **Repository access:** **Only select repositories** → choose **enterprise-dot/arb-bot**
   - **Permissions → Repository permissions → Contents:** **Read-only**
     (Metadata: Read-only is added automatically. Leave everything else as "No access".)
3. Click **Generate token**, then **copy it now**. GitHub only shows it once.
   It starts with `github_pat_`.

**You're done when:** you have the token copied somewhere safe.

---

## Part 3: Create the server (a "Droplet")

DigitalOcean calls servers **Droplets**.

1. Log in at **https://cloud.digitalocean.com**.
2. Click the green **Create** button (top right), then **Droplets**.
3. **Choose Region:** **New York**. Any datacenter number is fine.
4. **Choose an image:** **OS** tab → **Ubuntu** → version **24.04 (LTS) x64**.
5. **Choose Size:**
   - Droplet Type: **Basic**
   - CPU options: **Regular** (Disk type: SSD)
   - Pick the **$6/month** option (1 GB RAM / 1 CPU / 25 GB disk).
     The $4 option works too, but $6 has more breathing room.
6. **Additional storage / Backups:** leave off. Your code lives on GitHub.
7. **Choose Authentication Method:** select **Password**.
   - Make a **strong root password** (16+ characters, mixed). DigitalOcean shows the rules.
   - **Save it in your password manager.** You need it if you ever log in another way.
   - *Assistant: ask the user to type this password themselves, or to give you one they've
     saved. Don't make one up without telling them.*
8. **Recommended options:** tick **Monitoring** if it's offered (free graphs). Optional.
9. **Quantity:** 1. **Hostname:** change it to `arb-bot`.
10. Leave Tags and Project as they are.
11. Click **Create Droplet**.
    - *Assistant: confirm with the user first. This starts the $6/month billing.*
12. Wait about a minute until the Droplet shows a green dot and an **IP address**
    (like `164.90.xxx.xxx`).

**You're done when:** the `arb-bot` Droplet shows as running with an IP address.

---

## Part 4: Open the console and install the bot

You'll type commands into the server through your web browser. Nothing to install on your computer.

### Open the console
1. In DigitalOcean, click the **arb-bot** Droplet.
2. Click **Access** in the left menu, then **Launch Droplet Console**.
   (You may also see a **Console** link at the top right of the Droplet page. Same thing.)
3. A black window opens with a prompt ending in `#`, like `root@arb-bot:~#`.
   That means you're in, as the administrator.

**Pasting into the console:** use **Ctrl+Shift+V** (Windows) or **Cmd+V** (Mac), or
right-click → Paste. After pasting a command, press **Enter** to run it.

### Install
4. Take this command and replace **both** `YOUR_TOKEN` spots with the GitHub token from Part 2b:

   ```bash
   git clone https://YOUR_TOKEN@github.com/enterprise-dot/arb-bot.git /tmp/arb && bash /tmp/arb/deploy/install.sh https://YOUR_TOKEN@github.com/enterprise-dot/arb-bot.git
   ```

5. Paste it into the console and press **Enter**. It takes 1–2 minutes and prints a lot of text.
   - If it stops and asks **"Do you want to continue? [Y/n]"**, type `Y` and press **Enter**.
   - If a purple/blue screen asks about restarting services, press **Enter** to accept the default.

**You're done when:** it ends with **"Installed. Next:"** followed by 4 numbered steps.

> **If you see `Authentication failed` or `Repository not found`:** the token is wrong or
> doesn't have access to `arb-bot`. Redo Part 2b, then run step 4 again.

---

## Part 5: Add your keys

1. Open the settings file in a simple text editor called **nano**:

   ```bash
   nano /opt/arb-bot/.env
   ```

2. Use the **arrow keys** to move around (the mouse doesn't work in nano). Fill in these lines.
   Put the value right after the `=`, with no spaces or quotes:

   | Line | What to put |
   |---|---|
   | `ODDS_API_KEY=` | Your Odds API key (Part 1) |
   | `DISCORD_WEBHOOK_URL=` | Your webhook URL (Part 2) |
   | `BILLING_DAY=1` | Change `1` to your renewal day (Part 1) |
   | `EV_BANKROLL=1000` | The money you're setting aside for +EV bets |
   | `BANKROLL=100` | How much to put on each arb in total |
   | `BOOKMAKERS=...` | Keep `pinnacle` first. Replace the others with books **you have accounts at** (up to 9 more) |

   Example of a finished line: `ODDS_API_KEY=a1b2c3d4e5f6...`

3. **Save:** press **Ctrl+O** (the letter O), then **Enter**.
4. **Exit:** press **Ctrl+X**.

**You're done when:** you're back at the `root@arb-bot:~#` prompt.

> To check what you saved without changing it: `cat /opt/arb-bot/.env`

---

## Part 6: Test it, then turn it on

Run these one at a time and read what each prints.

1. **Send a test alert to Discord:**
   ```bash
   sudo -u arbbot python3 /opt/arb-bot/arbbot.py --test-discord
   ```
   A sample 💰 alert should appear in `#arbs`, then change to **❌ GONE** after 5 seconds.
   *If nothing appears, the webhook URL in `.env` is wrong. Fix it in Part 5.*

2. **Check today's schedule and credit forecast** (uses no credits):
   ```bash
   sudo -u arbbot python3 /opt/arb-bot/arbbot.py --plan
   ```
   You should see a small grid of sports and hours, then a line about credits.
   *If it says the key was rejected (401), the Odds API key in `.env` is wrong.*

3. **Check the book names** (one real check, costs a few credits: 3 per sport, or 1 + 2 for a sport
   with a live game):
   ```bash
   sudo -u arbbot python3 /opt/arb-bot/arbbot.py --once --dry-run
   ```
   Look at the line **"Not in the feed right now"**. If a book you use is listed there, its
   name in `BOOKMAKERS` may be misspelled. The "Books in the feed" line shows the correct names.
   (If no games are on or starting soon, it says so. Try again later.)

4. **Turn it on for good:**
   ```bash
   systemctl start arbbot
   ```
   A **🟢 Arb bot online** message appears in Discord.

5. **Watch it work** (optional):
   ```bash
   journalctl -u arbbot -f
   ```
   Press **Ctrl+C** to stop watching. The bot keeps running.

**You're done when:** the 🟢 online message is in Discord.

**Now send Claude the output of steps 2 and 3**, so it can check the numbers and book names.

You can close the console and the browser. The bot keeps running on the server, starts again
by itself if the server reboots, and restarts within a minute if it ever crashes.

6. **Turn on automatic updates** (once):
   ```bash
   bash /opt/arb-bot/deploy/enable-auto-update.sh
   ```
   From then on the server checks GitHub every 10 minutes. New code is tested first, then the
   bot restarts on it, and if it crashes after that it goes back to the previous version by
   itself. Each update (or problem) is posted in your Discord status channel, so you never have
   to run update commands again.

---

## Everyday use

Open the console the same way (Droplet → Access → Launch Droplet Console), then:

| To do this | Run |
|---|---|
| See what it's doing | `journalctl -u arbbot -f` (Ctrl+C to stop watching) |
| Is it running? | `systemctl status arbbot` (look for "active (running)", press Q to exit) |
| Stop it | `systemctl stop arbbot` |
| Start it | `systemctl start arbbot` |
| Change a setting | `sudo -u arbbot python3 /opt/arb-bot/arbbot.py --set NAME=value`, then `systemctl restart arbbot` |
| Connect a Discord channel | `sudo -u arbbot python3 /opt/arb-bot/arbbot.py --set-webhook ev` (paste the URL when asked) |
| See sample alerts | `sudo -u arbbot python3 /opt/arb-bot/arbbot.py --test-discord` |
| Post the how-to guide | `sudo -u arbbot python3 /opt/arb-bot/arbbot.py --post-guide` |
| What hit today (bet by bet) | `sudo -u arbbot python3 /opt/arb-bot/arbbot.py --results` (or `--results yesterday`) |
| Post today's results to Discord | `sudo -u arbbot python3 /opt/arb-bot/arbbot.py --post-results` |
| Check prop grading works | `sudo -u arbbot python3 /opt/arb-bot/arbbot.py --check-props` |
| Check the Kalshi second opinion | `sudo -u arbbot python3 /opt/arb-bot/arbbot.py --check-kalshi` |
| Test the cheaper combined live check (on a night with 2+ sports live; about 1 credit + 1 per live sport) | `sudo -u arbbot python3 /opt/arb-bot/arbbot.py --check-upcoming` |
| See whether ESPN's free scores agreed with the paid ones | `sudo -u arbbot python3 /opt/arb-bot/arbbot.py --results` (last lines), or `cat /opt/arb-bot/score_checks.csv` |
| Where the credits go, and what each check really costs | `sudo -u arbbot python3 /opt/arb-bot/arbbot.py --plan` |
| Get the latest code | Happens by itself every 10 minutes once automatic updates are on (step 6). By hand: `cd /opt/arb-bot && sudo -u arbbot git pull && systemctl restart arbbot` |
| See what the updater did | `journalctl -u arbbot-update -n 30` |
| Settings Claude pushed for you | They're in `/opt/arb-bot/remote.env` and win over `.env`. Use only your own: `sudo -u arbbot python3 /opt/arb-bot/arbbot.py --set REMOTE_SETTINGS=off`, then `systemctl restart arbbot` |
| Turn automatic updates off / on | `systemctl disable --now arbbot-update.timer` / `bash /opt/arb-bot/deploy/enable-auto-update.sh` |
| See the arb log | `cat /opt/arb-bot/arbs.csv` |

---

## Troubleshooting

| What you see | What to do |
|---|---|
| No 🟢 message after `systemctl start arbbot` | Run `journalctl -u arbbot -n 50` and send Claude the output |
| 🔴 "Odds API rejected the key" in Discord | Fix `ODDS_API_KEY` in `.env`, then `systemctl restart arbbot` |
| ⚠️ "No pinnacle odds in the feed" | Make sure `pinnacle` is in `BOOKMAKERS`, then restart |
| ⚠️ "Couldn't update the bot by itself: the code on the server was changed by hand" | Run the command in the message once; automatic updates carry on after that |
| `git pull` asks for a username (or no 🔄 updates arrive) | The GitHub token expired. Make a new one (Part 2b) and rerun the Part 4 install command with it. Your `.env` is kept. |
| Console window is blank or frozen | Close it and launch it again. The bot isn't affected. |
| Forgot the root password | Droplet page → **Access** → **Reset Root Password** (the new one is emailed). |

---

## Stopping for good

To stop all charges: Droplet page → **Destroy** (bottom of the left menu) → **Destroy this
Droplet**. This deletes the server and everything on it, including the `.csv` logs. Download
anything you want first. Cancel the Odds API plan separately on their site.
