@echo off
cd /d D:\TradingBot
echo ==== %DATE% %TIME% ==== >> logs\watchdog.log
set PYTHONUTF8=1
tradingbot\Scripts\python.exe watchdog.py >> logs\watchdog.log 2>&1

