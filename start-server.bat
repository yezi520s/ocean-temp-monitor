@echo off
title 海洋温度监测平台 · Server
echo.
echo    🌊  海洋温度异常预警监测平台
echo    ────────────────────────────────
echo.
echo    Starting Flask server on http://127.0.0.1:5000/
echo.
echo    按 Ctrl+C 停止服务器
echo    ════════════════════════════════════════
echo.

cd /d "%~dp0"
"C:\Users\35631\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" -u -m flask run --host=0.0.0.0 --port=5000

pause