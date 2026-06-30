#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  VidAuto — cài auto-start cho Mac (chỉ cần chạy 1 lần)
#  Sau đó Mac boot là server + worker tự bật, không cần làm gì
# ─────────────────────────────────────────────────────────────
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/opt/homebrew/bin/python3.12"
LOG="$HOME/Library/Logs/VidAuto"
AGENTS="$HOME/Library/LaunchAgents"

mkdir -p "$LOG"

# Cài packages nếu thiếu
$PYTHON -c "import fastapi, uvicorn, multipart, requests" 2>/dev/null || \
  $PYTHON -m pip install fastapi uvicorn python-multipart requests -q --break-system-packages

# ── Server plist ──────────────────────────────────────────────
cat > "$AGENTS/com.vidauto.server.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.vidauto.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$DIR/main.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$LOG/server.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG/server_error.log</string>
</dict>
</plist>
EOF

# ── Worker plist ──────────────────────────────────────────────
cat > "$AGENTS/com.vidauto.worker.plist" <<EOF
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
        <string>$DIR/worker.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$LOG/worker.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG/worker_error.log</string>
</dict>
</plist>
EOF

# ── Load cả hai ───────────────────────────────────────────────
launchctl unload "$AGENTS/com.vidauto.server.plist" 2>/dev/null || true
launchctl unload "$AGENTS/com.vidauto.worker.plist" 2>/dev/null || true

launchctl load "$AGENTS/com.vidauto.server.plist"
launchctl load "$AGENTS/com.vidauto.worker.plist"

echo ""
echo "✅ VidAuto đã được cài auto-start!"
echo "   Mac khởi động lại bao nhiêu lần cũng tự chạy."
echo ""
echo "   Mở app: http://localhost:7861"
echo "   Log:    $LOG/"
echo ""
echo "   Để gỡ:  launchctl unload $AGENTS/com.vidauto.server.plist"
echo "           launchctl unload $AGENTS/com.vidauto.worker.plist"
echo ""

# Mở browser
sleep 2 && open "http://localhost:7861" &
