#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  VidAuto Worker — macOS auto-start setup (chỉ cần chạy 1 lần)
# ─────────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/opt/homebrew/bin/python3.12"
PLIST="$HOME/Library/LaunchAgents/com.vidauto.worker.plist"
LOG_DIR="$HOME/Library/Logs/VidAuto"

mkdir -p "$LOG_DIR"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.vidauto.worker</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT_DIR/worker.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/worker.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/worker_error.log</string>
    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
EOF

# Reload
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo ""
echo "✓ VidAuto Worker đã được cài đặt!"
echo "  Sẽ tự động chạy mỗi khi Mac khởi động."
echo ""
echo "  Log:   $LOG_DIR/worker.log"
echo "  Stop:  launchctl unload $PLIST"
echo "  Start: launchctl load $PLIST"
echo ""
