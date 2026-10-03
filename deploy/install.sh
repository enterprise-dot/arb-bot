#!/usr/bin/env bash
# Installs the bot as a service on a fresh Ubuntu/Debian server.
# Usage (as root):  bash install.sh https://<TOKEN>@github.com/<you>/arb-bot.git
set -euo pipefail

REPO_URL="${1:?usage: bash install.sh <git-clone-url>}"
APP_DIR=/opt/arb-bot

apt-get update -qq
apt-get install -y -qq python3 git tzdata

id arbbot &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin arbbot

if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi
git -C "$APP_DIR" remote set-url origin "$(echo "$REPO_URL" | sed -E 's#https://[^@]+@#https://#')"

[ -f "$APP_DIR/.env" ] || cp "$APP_DIR/.env.example" "$APP_DIR/.env"
chown -R arbbot:arbbot "$APP_DIR"
chmod 600 "$APP_DIR/.env"

cp "$APP_DIR/deploy/arbbot.service" /etc/systemd/system/arbbot.service
systemctl daemon-reload
systemctl enable arbbot >/dev/null

cat <<MSG

Installed. Next:
  1. nano $APP_DIR/.env              # paste ODDS_API_KEY and DISCORD_WEBHOOK_URL
  2. sudo -u arbbot python3 $APP_DIR/arbbot.py --plan
  3. systemctl start arbbot
  4. journalctl -u arbbot -f         # watch it live (Ctrl+C to stop watching)
MSG
