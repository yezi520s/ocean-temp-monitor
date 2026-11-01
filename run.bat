@echo off
cd /d "%~dp0"
echo ============================================
echo   海洋温度异常预警监测平台
echo ============================================
echo.
echo [1/2] 正在安装依赖...
..\..\..\..\..\Users\35631\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe -m pip install flask requests -i https://pypi.tuna.tsinghua.edu.cn/simple --quiet --trusted-host pypi.tuna.tsinghua.edu.cn
echo.
echo [2/2] 正在启动服务...
..\..\..\..\..\Users\35631\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe app.py
pause