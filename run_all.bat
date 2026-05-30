@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === VPN: Telegram + HTTP + Xray checker ===
echo.

where py >nul 2>&1
if errorlevel 1 (set PY=python) else (set PY=py -3.13)

%PY% -m pip install -q -r requirements.txt

set PYTHONUNBUFFERED=1
set XRAY_GROUP_SIZE=1000
set XRAY_MULTI=0
set REQUEST_CONCURRENCY=150
set NEEDED_WHITELIST=100
set NEEDED_FOREIGN=50
set TG_APPEND_SOURCES=1

echo [info] pipeline: sources.txt + tg_sources.txt -^> parser.py -^> whitelist/global
echo.

%PY% -u parser.py
exit /b %ERRORLEVEL%
