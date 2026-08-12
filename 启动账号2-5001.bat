@echo off
echo ==========================================
echo   BiliBot Review - Account 2
echo ==========================================
echo.
if not exist "%~dp0data-account-2" mkdir "%~dp0data-account-2"
echo Starting http://127.0.0.1:5001
echo Keep this window open.
echo.
python launch_instance.py --port 5001 --account-slot 2 --data-dir "data-account-2"
pause
