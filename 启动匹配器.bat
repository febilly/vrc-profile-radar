@echo off
chcp 65001 >nul
title VRChat Profile 雷达 - 控制台
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto run_venv
python main.py %*
goto end

:run_venv
".venv\Scripts\python.exe" main.py %*

:end
pause
