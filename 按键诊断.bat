@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title zh2en - key probe
cd /d "%~dp0"

set PYEXE=python
where py >nul 2>nul && set PYEXE=py

%PYEXE% zh2en.py --keys
echo.
echo Press any key to close.
pause >nul
