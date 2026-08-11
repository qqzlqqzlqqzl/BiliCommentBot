@echo off
chcp 65001 > nul
echo ==========================================
echo   B站评论人工审核回复工具 - 账号1
echo ==========================================
echo.
set "BILI_PORT=5000"
set "BILI_ACCOUNT_NAME=账号1"
set "BILI_DATA_DIR="
echo 正在启动，浏览器将自动打开...
echo 如未自动打开，请访问 http://127.0.0.1:5000
echo.
python main.py
pause
