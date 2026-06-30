#!/bin/bash
cd "$(dirname "$0")"

# Chỉ install nếu thiếu package
/opt/homebrew/bin/python3.12 -c "import fastapi, uvicorn, multipart, requests" 2>/dev/null || \
  /opt/homebrew/bin/python3.12 -m pip install -r requirements.txt requests -q --break-system-packages

# Chạy worker nền
/opt/homebrew/bin/python3.12 worker.py &
WORKER_PID=$!

# Mở browser sau 1.5s (đợi server khởi động)
sleep 1.5 && open "http://localhost:7861" &

echo "==============================="
echo "  VidAuto Web đang khởi động..."
echo "  http://localhost:7861"
echo "  Nhấn Ctrl+C để tắt"
echo "==============================="

/opt/homebrew/bin/python3.12 main.py

# Tắt worker khi server dừng
kill $WORKER_PID 2>/dev/null
