@echo off
cd /d D:\TradingBot
echo ==== %DATE% %TIME% ==== >> logs\snapshot.log
set PYTHONUTF8=1
tradingbot\Scripts\python.exe snapshot.py
tradingbot\Scripts\python.exe drop.py --discord >> logs\snapshot.log 2>&1

