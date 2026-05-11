@echo off
chcp 65001 >nul
title DART - 패키지 설치
cd /d "%~dp0"

echo [DART] requirements.txt 설치 중...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo 설치 실패. Python 설치 및 PATH 설정을 확인하세요.
    pause
    exit /b 1
)
echo.
echo 완료. 이제 run_dart_app.bat 을 실행하면 됩니다.
pause
