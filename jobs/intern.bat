@echo off
cd /d D:\TradingBot
echo ==== %DATE% %TIME% ==== >> logs\intern.log
set PYTHONUTF8=1
tradingbot\Scripts\python.exe intern_desk.py --trade >> logs\intern.log 2>&1

