@echo off
setlocal enabledelayedexpansion
title 防火墙放行 · 需管理员权限

:: 检查是否以管理员运行
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo    ⚠️  请右键本文件 → "以管理员身份运行"
    echo    ⚠️  然后重新执行
    pause
    exit /b 1
)

echo    🌊  海洋温度监测平台 · 防火墙放行
echo    ═══════════════════════════════════════
echo.

:: 先删旧规则（避免重复）
netsh advfirewall firewall delete rule name="海洋监测平台" >nul 2>&1

:: 添加新规则
netsh advfirewall firewall add rule name="海洋监测平台" dir=in action=allow protocol=TCP localport=5000

echo.
echo    ✅  端口 5000 已放行
echo    🌐  其他电脑访问地址：
echo.

:: 显示本机 IP
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"192.168." /c:"172." /c:"10."') do (
    set IP=%%a
    set IP=!IP: =!
    echo        http://!IP!:5000/
)
echo.
echo    ─────────────────────────────────
echo    有多个 IP 时逐个试，通常第一个有效
echo    ═══════════════════════════════════════
echo.
pause