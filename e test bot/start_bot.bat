@echo off
title Rasch Test Bot
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   Rasch Test Boti ishga tushirilmoqda...
echo ============================================
echo.

:loop
python bot.py
echo.
echo [DIQQAT] Bot to'xtadi! 3 soniyadan so'ng qayta ishga tushadi...
timeout /t 3 /nobreak >nul
goto loop
