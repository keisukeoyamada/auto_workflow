#!/usr/bin/env bash
# Run the Nakama scheduler natively (no Docker) as a per-user background service.
#   Linux / WSL (systemd --user) and macOS (launchd) are supported.
# Usage (from repo root):
#   ./scripts/install-service.sh            # install + start
#   ./scripts/install-service.sh --uninstall
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$REPO/.venv/bin/python"
NAME="nakama"

if [[ ! -x "$PYTHON" ]]; then
  echo "Not found: $PYTHON" >&2
  echo "Create the venv first: python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

uninstall=false
[[ "${1:-}" == "--uninstall" ]] && uninstall=true

case "$(uname -s)" in
  Linux)
    if ! command -v systemctl >/dev/null || ! systemctl --user show-environment >/dev/null 2>&1; then
      echo "systemd --user is not available. On WSL, set [boot] systemd=true in /etc/wsl.conf and restart WSL." >&2
      exit 1
    fi
    unit="$HOME/.config/systemd/user/$NAME.service"
    if $uninstall; then
      systemctl --user disable --now "$NAME" 2>/dev/null || true
      rm -f "$unit" && systemctl --user daemon-reload
      echo "removed $unit"; exit 0
    fi
    mkdir -p "$(dirname "$unit")"
    cat > "$unit" <<EOF
[Unit]
Description=Nakama agent fleet scheduler
After=network-online.target

[Service]
WorkingDirectory=$REPO
ExecStart=$PYTHON -m nakama serve --ui
Restart=on-failure
RestartSec=300

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "$NAME"
    # Keep running after logout (may ask for sudo; harmless if it fails).
    loginctl enable-linger "$USER" 2>/dev/null || echo "hint: run 'sudo loginctl enable-linger $USER' to keep it running after logout"
    echo "installed. logs: journalctl --user -u $NAME -f  (also logs/nakama.log)"
    ;;
  Darwin)
    plist="$HOME/Library/LaunchAgents/com.nakama.scheduler.plist"
    if $uninstall; then
      launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
      rm -f "$plist"
      echo "removed $plist"; exit 0
    fi
    mkdir -p "$(dirname "$plist")" "$REPO/logs"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.nakama.scheduler</string>
  <key>ProgramArguments</key><array>
    <string>$PYTHON</string><string>-m</string><string>nakama</string><string>serve</string><string>--ui</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardErrorPath</key><string>$REPO/logs/launchd.err.log</string>
</dict></plist>
EOF
    launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
    echo "installed. logs: $REPO/logs/nakama.log"
    ;;
  *)
    echo "Unsupported OS: $(uname -s). On Windows use scripts/install-windows-task.ps1" >&2
    exit 1
    ;;
esac
