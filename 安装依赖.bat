@echo off
chcp 65001 >nul
title zh2en - install deps
cd /d "%~dp0"

set PYEXE=python
where py >nul 2>nul && set PYEXE=py

echo Using: %PYEXE%
%PYEXE% -c "import sys; print(sys.executable)"
echo.
%PYEXE% -m pip install --upgrade keyboard pyperclip
echo.
echo Done. Press any key to close.
pause >nul
