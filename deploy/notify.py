#!/usr/bin/env python3
"""Post one message to the bot's Discord status channel (or the main channel if there isn't one).
Used by auto-update.sh. Reads the webhook URLs from the bot's .env; standard library only."""
import json
import sys
import urllib.request
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"


def webhook(env: Path = ENV) -> str:
    values = {}
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("DISCORD_STATUS_WEBHOOK_URL", "DISCORD_WEBHOOK_URL"):
        if values.get(key, "").startswith("https://"):
            return values[key]
    return ""


def main() -> int:
    url = webhook()
    if not url or len(sys.argv) < 2:
        return 0
    body = json.dumps({"username": "EV BOT", "content": sys.argv[1][:1900]}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "arbbot/3.0 (update)"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except OSError as e:
        print(f"couldn't post to Discord: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
