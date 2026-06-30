@echo off
cd /d "%~dp0"
echo Starting VidAuto Web (Windows)...
echo.
echo [Server] http://localhost:7861
echo.
start "VidAuto-Server" cmd /k "python main.py"
timeout /t 2 /nobreak >nul
start "VidAuto-Worker" cmd /k "python worker.py"
timeout /t 2 /nobreak >nul
start http://localhost:7861
