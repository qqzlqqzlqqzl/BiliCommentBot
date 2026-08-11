@echo off
chcp 65001 > nul
echo ==========================================
echo   B站评论人工审核回复工具 - 账号2
echo ==========================================
echo.
set "BILI_PORT=5001"
set "BILI_ACCOUNT_NAME=账号2"
set "BILI_DATA_DIR=%~dp0data-account-2"
if not exist "%BILI_DATA_DIR%" mkdir "%BILI_DATA_DIR%"
echo 正在启动，浏览器将自动打开...
echo 如未自动打开，请访问 http://127.0.0.1:5001
echo.
python main.py
pause
