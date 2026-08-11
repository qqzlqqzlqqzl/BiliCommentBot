@echo off
chcp 65001 > nul
echo ==========================================
echo   B站评论人工审核回复工具 - Web UI
echo ==========================================
echo.
echo 正在启动，浏览器将自动打开...
echo 如未自动打开，请访问 http://127.0.0.1:5000
echo.
python main.py
pause
