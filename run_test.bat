@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === VPN parser — локальный тест (Windows) ===
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [error] Python не найден. Установи Python 3.10+ и добавь в PATH.
    pause
    exit /b 1
)

echo [info] pip install...
python -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo [error] pip install failed
    pause
    exit /b 1
)

REM Быстрый тест: меньше нод и лимитов. Для полного прогона убери set ниже.
set PYTHONUNBUFFERED=1
set NEEDED_WHITELIST=10
set NEEDED_FOREIGN=10
set WHITELIST_SCAN_MAX=800
set FOREIGN_SCAN_MAX=800
set XRAY_GROUP_SIZE=1000
set XRAY_MULTI=0
set REQUEST_CONCURRENCY=150
set MAX_HTTP_MS=800
set PREFERRED_MS=800
set PROXY_TIMEOUT=12
set PROBE_URL=https://www.gstatic.com/generate_204
set WHITELIST_PROBE_URL=http://www.rt.ru
set XRAY_MIN_SPLIT=2
set XRAY_START_DELAY=0.5

echo.
echo [info] NEEDED_WHITELIST=%NEEDED_WHITELIST%  NEEDED_FOREIGN=%NEEDED_FOREIGN%
echo [info] scan max wl=%WHITELIST_SCAN_MAX% global=%FOREIGN_SCAN_MAX%
echo [info] XRAY_GROUP_SIZE=%XRAY_GROUP_SIZE% (1 xray на пачку)
echo.
echo [info] Запуск parser.py...
echo.

python -u parser.py
set EXIT_CODE=%ERRORLEVEL%

echo.
if %EXIT_CODE%==0 (
    echo [ok] Готово. Смотри файлы:
    echo   whitelist.txt  global.txt  foreign.txt
    echo   sub_whitelist.txt  sub_global.txt
) else (
    echo [error] parser завершился с кодом %EXIT_CODE%
)

pause
exit /b %EXIT_CODE%
