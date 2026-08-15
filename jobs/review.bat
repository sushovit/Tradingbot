@echo off
cd /d D:\TradingBot
echo ==== %DATE% %TIME% ==== >> logs\review.log
tradingbot\Scripts\python.exe review_bot.py >> logs\review.log 2>&1

