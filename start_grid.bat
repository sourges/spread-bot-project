@echo off
title GRID - Kraken + Coinbase
cd /d "%~dp0"
timeout /t 30 /nobreak
call venv\Scripts\activate.bat
python main_grid.py
pause
