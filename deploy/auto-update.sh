#!/usr/bin/env bash
# Keeps the bot up to date by itself. Every 10 minutes (arbbot-update.timer) this checks GitHub
# for new code, runs the tests on it in a scratch folder, and only then switches to it and
# restarts the bot. If the tests fail, or the bot keeps crashing after the restart, it goes back
# to the code it had and leaves that version alone. Every outcome is posted to the Discord status
# channel. Runs as the bot's own user; the only extra thing it may do is restart the bot (see
# enable-auto-update.sh).
set -uo pipefail

APP="${ARBBOT_DIR:-/opt/arb-bot}"
SERVICE="${ARBBOT_SERVICE:-arbbot}"
SETTLE="${ARBBOT_SETTLE_SECONDS:-150}"   # how long the bot must run without crashing after an update
SKIP="$APP/.update-skipped"               # a version that failed: not tried again
TMP=""                                    # scratch copy for the tests (removed on exit)
SYSTEMCTL="${ARBBOT_SYSTEMCTL:-systemctl}"
RESTART="${ARBBOT_RESTART:-sudo -n /usr/bin/systemctl restart $SERVICE}"

main() {
  cd "$APP" || exit 1
  local before after changes
  before=$(git rev-parse HEAD) || exit 1
  git fetch -q origin main || exit 0           # GitHub unreachable: try again next time
  after=$(git rev-parse FETCH_HEAD) || exit 0
  [ "$before" = "$after" ] && exit 0
  if ! git merge-base --is-ancestor "$before" "$after"; then
    tell_once "$after" "⚠️ Couldn't update the bot by itself: the code on the server was changed by hand. Run: cd $APP && sudo -u arbbot git pull && systemctl restart $SERVICE"
    exit 1
  fi
  [ -f "$SKIP" ] && [ "$(cat "$SKIP")" = "$after" ] && exit 0
  changes=$(git log --no-merges --format='• %s' "$before..$after" | head -n 12)

  # 1. Test the new code in a scratch copy (the tests write files beside the code, so never in $APP).
  TMP=$(mktemp -d) || exit 1
  trap 'rm -rf "$TMP"' EXIT
  if ! git archive "$after" | tar -x -C "$TMP"; then
    echo "couldn't unpack $after for testing"
    exit 1
  fi
  if ! (cd "$TMP" && timeout 600 python3 -m unittest -q test_arbbot) >"$TMP/test.log" 2>&1; then
    tail -n 40 "$TMP/test.log"
    echo "$after" >"$SKIP"
    tell "⚠️ New code is on GitHub but its tests failed, so the bot kept running the current version. Nothing to do on your side; Claude will fix it."
    exit 1
  fi

  # 2. Switch to it and restart.
  local crashes
  crashes=$(restarts)
  if ! git merge -q --ff-only "$after"; then
    tell "⚠️ Couldn't switch the bot to the new code (git merge failed). It's still running the current version."
    exit 1
  fi
  if ! $RESTART; then
    rollback "$before" "$after" "the bot couldn't be restarted"
    exit 1
  fi

  # 3. Make sure it stays up; otherwise go back.
  sleep "$SETTLE"
  if $SYSTEMCTL is-active -q "$SERVICE" && [ "$(restarts)" = "$crashes" ]; then
    rm -f "$SKIP"
    tell "🔄 Bot updated to the latest code:
$changes"
  else
    rollback "$before" "$after" "the new code kept crashing"
    exit 1
  fi
}

restarts() {   # how many times systemd has had to restart the bot after a crash
  $SYSTEMCTL show -p NRestarts --value "$SERVICE" 2>/dev/null || echo 0
}

rollback() {
  git reset -q --hard "$1"
  $RESTART
  echo "$2" >"$SKIP"
  tell "⚠️ Update undone ($3), so the bot went back to the previous version. Nothing to do on your side; Claude will fix it."
}

tell() {
  echo "$1"
  python3 "$APP/deploy/notify.py" "$1" || true
}

tell_once() {   # the same warning only once per new version
  [ -f "$SKIP" ] && [ "$(cat "$SKIP")" = "$1" ] && return 0
  echo "$1" >"$SKIP"
  tell "$2"
}

# Everything runs from inside main, which bash has read in full before starting, so the update
# replacing this very file can't confuse the run in progress.
main "$@"
exit
