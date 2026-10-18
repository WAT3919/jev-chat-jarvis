@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================================
echo   微博分析 - 本地图形界面
echo   启动后浏览器会自动打开 http://127.0.0.1:8770
echo   关闭本窗口或按 Ctrl+C 即可停止
echo ================================================
where py >nul 2>nul
if %errorlevel%==0 (
  py webui.py
) else (
  python webui.py
)
echo.
echo 服务已停止。
pause