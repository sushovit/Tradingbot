@echo off
cd /d D:\TradingBot
echo ==== %DATE% %TIME% ==== >> logs\floor.log
set PYTHONUTF8=1
tradingbot\Scripts\python.exe floor.py --to-file --discord >> logs\floor.log 2>&1

