@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".env" (
    echo [DART] .env 파일이 이미 있습니다. 덮어쓰지 않습니다.
    echo 필요하면 메모장으로 .env 를 열어 DART_API_KEY 만 수정하세요.
    pause
    exit /b 0
)

if not exist ".env.example" (
    echo [DART] .env.example 이 없습니다.
    pause
    exit /b 1
)

copy /Y ".env.example" ".env" >nul
echo [DART] .env 파일을 만들었습니다: %CD%\.env
echo 메모장으로 열어서 DART_API_KEY= 뒤에 OpenDART 발급 키를 넣고 저장하세요.
start "" notepad ".env"
pause
