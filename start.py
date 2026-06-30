import sys, os, subprocess, time, webbrowser
from pathlib import Path

HERE = Path(__file__).parent
PORT = 7861

def find_python():
    if sys.platform == "darwin":
        candidates = ["/opt/homebrew/bin/python3.12", "/usr/local/bin/python3", "python3"]
    else:
        candidates = ["python"]
    for p in candidates:
        if subprocess.run([p, "--version"], capture_output=True).returncode == 0:
            return p
    return sys.executable

py = find_python()

print("=" * 40)
print("  VidAuto Web đang khởi động...")
print(f"  http://localhost:{PORT}")
print("  Nhấn Ctrl+C để tắt")
print("=" * 40)

subprocess.run([py, "-m", "pip", "install", "-r", "requirements.txt", "-q"], cwd=HERE)

worker = subprocess.Popen([py, "worker.py"], cwd=HERE)

time.sleep(1.5)
webbrowser.open(f"http://localhost:{PORT}")

try:
    subprocess.run([py, "main.py"], cwd=HERE)
finally:
    worker.terminate()
