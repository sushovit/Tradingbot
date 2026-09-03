@echo off
cd /d D:\TradingBot
echo ==== %DATE% %TIME% ==== >> logs\review.log
set PYTHONUTF8=1
tradingbot\Scripts\python.exe review_bot.py >> logs\review.log 2>&1

