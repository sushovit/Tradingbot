@echo off
REM outcomes.bat - nightly rejection-outcome tracker (S3).
REM Runs AFTER the 16:15 ET shutdown. Read-only against the journal and
REM daily bars; it places no orders and touches no position state.
cd /d D:\TradingBot
echo ==== %DATE% %TIME% ==== >> logs\outcomes.log
set PYTHONUTF8=1
tradingbot\Scripts\python.exe outcomes.py >> logs\outcomes.log 2>&1
