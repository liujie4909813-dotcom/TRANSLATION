@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Push zh2en to GitHub

echo ============================================================
echo   zh2en  -  push to GitHub
echo ============================================================
echo.
echo   Step 1 (browser): create an EMPTY repo on GitHub
echo       https://github.com/new?name=zh2en
echo       Do NOT tick "Add a README / .gitignore / license"
echo       Empty repo means no conflict with what we push.
echo.
echo   Step 2 (here): paste the repo URL below.
echo       It looks like: https://github.com/YOUR_NAME/zh2en.git
echo.
set "CUR="
for /f "tokens=*" %%i in ('git remote get-url origin 2^>nul') do set "CUR=%%i"
if defined CUR echo   Already configured: %CUR%
echo.
set /p REPOURL=Repo URL (Enter = use the one above): 
if "%REPOURL%"=="" if defined CUR set "REPOURL=%CUR%"
if "%REPOURL%"=="" (
  echo.
  echo No URL. Cancelled.
  pause >nul
  exit /b
)

git remote remove origin >nul 2>nul
git remote add origin "%REPOURL%"
git branch -M main

echo.
echo ------------------------------------------------------------
echo Pushing to %REPOURL%
echo A browser window may pop up asking you to sign in to GitHub.
echo Sign in once and it will remember you.
echo ------------------------------------------------------------
echo.
git push -u origin main
if errorlevel 1 (
  echo.
  echo Push FAILED. Common reasons:
  echo   - repo name / user name typo in the URL
  echo   - the repo is not empty and has commits of its own
  echo   - not signed in, or the sign-in window was closed
  echo.
  echo Re-run this file, or send me the error text above.
) else (
  echo.
  echo Done. Your repo now has the code.
)
echo.
pause >nul
