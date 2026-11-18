@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title zh2en - Chinese to English
cd /d "%~dp0"

set PYEXE=python
where py >nul 2>nul && set PYEXE=py

%PYEXE% zh2en.py
echo.
echo [zh2en stopped] Press any key to close.
pause >nul
