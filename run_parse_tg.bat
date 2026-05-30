@echo off
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 (
    set PY=python
) else (
    rem Python 3.13 — там уже стоит telethon
    py -3.13 -c "import telethon" 2>nul
    if errorlevel 1 (
        py -3.13 -m pip install -q telethon
    )
    set PY=py -3.13
)

echo [info] %PY% parse_tg.py %*
%PY% parse_tg.py %*
exit /b %ERRORLEVEL%
