#!/usr/bin/env bash
# Turns on automatic updates. Run once, as root:  bash /opt/arb-bot/deploy/enable-auto-update.sh
# From then on the server checks GitHub every 10 minutes, tests new code, restarts the bot on it,
# and goes back to the old code if anything breaks.
# Turn it off again:  systemctl disable --now arbbot-update.timer
set -euo pipefail
APP=/opt/arb-bot

# The update job runs as the bot's own user. The one extra thing it's allowed to do is restart the bot.
rule=/etc/sudoers.d/arbbot-update
echo "arbbot ALL=(root) NOPASSWD: /usr/bin/systemctl restart arbbot" >"$rule.tmp"
chmod 440 "$rule.tmp"
visudo -cqf "$rule.tmp"
mv "$rule.tmp" "$rule"

cp "$APP/deploy/arbbot-update.service" "$APP/deploy/arbbot-update.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now arbbot-update.timer >/dev/null

echo "Automatic updates are on. The bot checks GitHub every 10 minutes and posts each update in Discord."
echo "See what it did: journalctl -u arbbot-update -n 30"
