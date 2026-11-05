#!/bin/sh
# Install the Claude usage collector on a Mac: pushes this Mac's Claude Code usage totals to the Fetchkin usage
# dashboard every 10 minutes (launchd). One paste, from a Mac on the tailnet:
#
#   curl -fsSL https://laptop-fevkm5fu.tailaecd9a.ts.net:8443/collector/install-mac.sh | sh
#
# Optional env: USAGE_INGEST_TOKEN (else you're asked for it, input hidden), USAGE_DEVICE_LABEL (default "Tam's Mac"),
# USAGE_DEVICE (default this Mac's LocalHostName), USAGE_BASE (default the laptop's dashboard URL).
# Re-running is safe: it updates the script and reloads the job. Uninstall:
#   launchctl bootout gui/$(id -u)/com.fetchkin.claude-usage-collector; rm -rf ~/.claude-usage-collector \
#     ~/Library/LaunchAgents/com.fetchkin.claude-usage-collector.plist ~/.config/fetchkin/usage-collector.env
set -eu

BASE="${USAGE_BASE:-https://laptop-fevkm5fu.tailaecd9a.ts.net:8443}"
LABEL="${USAGE_DEVICE_LABEL:-Tam's Mac}"
DEVICE="${USAGE_DEVICE:-$(scutil --get LocalHostName 2>/dev/null | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9._-')}"
[ -n "$DEVICE" ] || DEVICE="mac"
DIR="$HOME/.claude-usage-collector"
TOKEN_FILE="$HOME/.config/fetchkin/usage-collector.env"
JOB="com.fetchkin.claude-usage-collector"
PLIST="$HOME/Library/LaunchAgents/$JOB.plist"
LOG="$HOME/Library/Logs/claude-usage-collector.log"

echo "Claude usage collector -> $BASE  (device: $DEVICE, label: $LABEL)"

PY=/usr/bin/python3
if ! "$PY" -c 'import sys; assert sys.version_info >= (3, 8)' >/dev/null 2>&1; then
  PY="$(command -v python3 || true)"
  if [ -z "$PY" ] || ! "$PY" -c 'import sys; assert sys.version_info >= (3, 8)' >/dev/null 2>&1; then
    echo "Python 3.8+ is needed. Run: xcode-select --install   then re-run this installer." >&2
    exit 1
  fi
fi

if ! curl -fsS -o /dev/null "$BASE/collector/claude_usage_collector.py"; then
  echo "Can't reach $BASE. Is Tailscale running and signed in on this Mac (tam@fetchkin.com)?" >&2
  exit 1
fi

mkdir -p "$DIR" "$HOME/.config/fetchkin" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
curl -fsSL "$BASE/collector/claude_usage_collector.py" -o "$DIR/claude_usage_collector.py"

TOKEN="${USAGE_INGEST_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -f "$TOKEN_FILE" ]; then
  echo "Keeping the ingest token already saved in $TOKEN_FILE"
else
  if [ -z "$TOKEN" ]; then
    printf "Paste the ingest token (USAGE_INGEST_TOKEN, input hidden): " > /dev/tty
    stty -echo < /dev/tty; read -r TOKEN < /dev/tty; stty echo < /dev/tty; echo > /dev/tty
  fi
  [ -n "$TOKEN" ] || { echo "No token given." >&2; exit 1; }
  umask 077
  printf 'USAGE_INGEST_TOKEN=%s\n' "$TOKEN" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
fi

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$JOB</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DIR/claude_usage_collector.py</string>
    <string>--device</string><string>$DEVICE</string>
    <string>--label</string><string>$LABEL</string>
    <string>--url</string><string>$BASE/api/ingest</string>
  </array>
  <key>StartInterval</key><integer>600</integer>
  <key>RunAtLoad</key><true/>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLISTEOF

echo "Test push..."
"$PY" "$DIR/claude_usage_collector.py" --device "$DEVICE" --label "$LABEL" --url "$BASE/api/ingest"

launchctl bootout "gui/$(id -u)/$JOB" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. Pushes every 10 minutes; log: $LOG"
