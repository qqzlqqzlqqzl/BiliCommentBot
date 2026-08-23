@echo off
echo ==========================================
echo   BiliBot Review - Account 1
echo ==========================================
echo.
echo Starting http://127.0.0.1:5000
echo Keep this window open.
echo.
python "%~dp0..\launch_instance.py" --port 5000 --account-slot 1
pause
