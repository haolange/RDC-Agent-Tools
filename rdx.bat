@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "LAUNCHER=%SCRIPT_DIR%\scripts\rdx_bat_launcher.ps1"
if not exist "%LAUNCHER%" (
  echo [RDX][ERR] missing launcher script: %LAUNCHER%
  echo {"ok":false,"error_code":2,"error_message":"missing launcher script","context_id":"default"}
  exit /b 2
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NoLogo -NonInteractive -ExecutionPolicy Bypass -File "%LAUNCHER%" %*
exit /b %ERRORLEVEL%
