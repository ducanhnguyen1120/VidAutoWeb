#!/bin/bash
cd "$(dirname "$0")"

# Chỉ install nếu thiếu package
/opt/homebrew/bin/python3.12 -c "import fastapi, uvicorn, multipart" 2>/dev/null || \
  /opt/homebrew/bin/python3.12 -m pip install -r requirements.txt -q --break-system-packages

/opt/homebrew/bin/python3.12 main.py
