@echo off
chcp 65001 >nul
title DART 재무조회
cd /d "%~dp0"

echo [DART] 폴더: %CD%
echo [DART] 브라우저가 열리면 앱을 사용하세요. 종료하려면 이 창에서 Ctrl+C 를 누르세요.
echo.

python -m streamlit run app.py
if errorlevel 1 (
    echo.
    echo 실행에 실패했습니다. Python이 PATH에 있는지, 아래를 한 번 실행해 패키지를 설치했는지 확인하세요.
    echo   pip install -r requirements.txt
    echo.
    pause
)
