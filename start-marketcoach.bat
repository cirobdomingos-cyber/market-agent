@echo off
REM ============================================================================
REM  start-marketcoach.bat  —  one-click restart for MarketCoach
REM
REM  Launches the backend and frontend in separate windows (so Ctrl+C only
REM  kills one piece) and opens the UI in your default browser.
REM
REM  IB Gateway must be started and logged in manually BEFORE running this —
REM  it's a GUI login and can't be scripted. If you only need research / chat
REM  features (no trading), the backend runs fine without Gateway.
REM
REM  Pin this file to the taskbar, or right-click > Send to > Desktop (create
REM  shortcut) for a one-click launcher after a reboot.
REM ============================================================================

title MarketCoach launcher

echo.
echo ============================================================
echo   MarketCoach launcher
echo ============================================================
echo.
echo   Before continuing, make sure:
echo     1. IB Gateway is running and logged into PAPER
echo        (port 4002 paper / 4001 live). Skip if you're not trading.
echo     2. No other process is using ports 8000 or 5173.
echo.
echo   This window will close itself after launching the two
echo   service windows. Close those to stop MarketCoach.
echo.
pause

REM ── Backend: FastAPI + APScheduler on :8000 ────────────────────────────────
REM  cmd /k keeps the window open so uvicorn logs stay visible. --reload
REM  picks up edits automatically so you don't have to restart this script
REM  every time you change a Python file.
start "MarketCoach Backend" cmd /k "cd /d C:\repo\market-ai-agent\marketcoach && py -3.12 -m uvicorn backend.main:app --reload"

REM ── Frontend: Vite dev server on :5173 ─────────────────────────────────────
start "MarketCoach Frontend" cmd /k "cd /d C:\repo\market-ai-agent\marketcoach\frontend && npm run dev"

REM Give both services ~6s to bind their ports before opening the browser.
REM If the frontend isn't ready yet Vite shows a friendly waiting screen.
timeout /t 6 /nobreak >nul
start "" http://localhost:5173

echo.
echo Launched. You can close this window.
echo.
timeout /t 3 /nobreak >nul
