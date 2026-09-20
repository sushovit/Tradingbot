@echo off
REM start_worker.bat — scheduled session start (S2c).
REM Runs ONLY on a trading day: 2026-09-07 was Labor Day and a blind
REM "start it Monday" would have started the desk into a closed market.
cd /d D:\TradingBot
echo ==== %DATE% %TIME% ==== >> logs\start_worker.log
set PYTHONUTF8=1
tradingbot\Scripts\python.exe -c "from dotenv import load_dotenv; load_dotenv(); from broker import Broker; import sys; sys.exit(0 if Broker().is_open_today() else 1)" >> logs\start_worker.log 2>&1
if errorlevel 1 (
    echo not a trading day - worker not started >> logs\start_worker.log
    exit /b 0
)
echo trading day - starting worker >> logs\start_worker.log
tradingbot\Scripts\python.exe run_worker.py >> logs\start_worker.log 2>&1
