@echo off
chcp 65001 > nul
title Discord TTS Bot
echo ========================================================
echo               Discord TTS Bot กำลังเริ่มทำงาน...
echo ========================================================
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] ไม่พบ Virtual Environment .venv
    echo กรุณาสร้าง venv และติดตั้ง dependencies ก่อน
    pause
    exit /b 1
)

".venv\Scripts\python.exe" bot.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo บอทหยุดทำงาน
    pause
)
