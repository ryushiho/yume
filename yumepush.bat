@echo off
REM Yume 배포용 배치 파일 - PowerShell 스크립트를 호출

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

REM 커밋 메시지를 인자로 넘길 수도 있고, 없으면 PS 스크립트가 물어본다
REM 예: yumepush.bat "블루전 두음법칙 수정"
powershell -ExecutionPolicy Bypass -File ".\deploy_yume.ps1" %*
