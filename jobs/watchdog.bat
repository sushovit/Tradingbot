@echo off
cd /d D:\TradingBot
echo ==== %DATE% %TIME% ==== >> logs\watchdog.log
tradingbot\Scripts\python.exe watchdog.py >> logs\watchdog.log 2>&1

