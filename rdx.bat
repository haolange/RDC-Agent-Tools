@echo off
setlocal EnableExtensions EnableDelayedExpansion
@echo off
set "RDX_NON_INTERACTIVE="
set "RDX_UI_MENU_ACTIVE="
set "RDX_UI_ORIG_PROMPT="
set "RDX_UI_PROMPT_WAS_DEFINED="

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%"
set "RDX_TOOLS_ROOT=%SCRIPT_DIR:~0,-1%"
if not defined RDX_USE_UV set "RDX_USE_UV=0"
set "RDX_ESC="
if not defined NO_COLOR (
  for /f %%a in ('echo prompt $E^| cmd') do set "RDX_ESC=%%a"
)
set "RDX_NON_INTERACTIVE_FLAG="
if /i "%~1"=="--non-interactive" (
  set "RDX_NON_INTERACTIVE=1"
  set "RDX_NON_INTERACTIVE_FLAG=1"
  shift
)

set "PYTHON_EXE=python"
where python >nul 2>&1
if errorlevel 1 (
  where py >nul 2>&1
  if errorlevel 1 (
    echo [RDX] ERROR: Python 3.10+ not found.
    popd >nul
    exit /b 2
  ) else (
    set "PYTHON_EXE=py -3"
  )
)

if "%~1"=="" (
  if defined RDX_NON_INTERACTIVE goto :help
  goto :menu_loop
)

if /i "%~1"=="menu" goto :menu_loop
if /i "%~1"=="mcp" goto :entry_mcp
if /i "%~1"=="cli" goto :unsupported_cli
if /i "%~1"=="cli-shell" goto :entry_cli_shell
if /i "%~1"=="daemon-shell" goto :entry_daemon_shell

if /i "%~1"=="--help" goto :help
if /i "%~1"=="-h" goto :help
goto :unsupported_command

:menu_loop
call :ui_enter_menu
cls
echo ===============================================
echo [RDX] rdx-tools Quick Start Menu
echo ===============================================
echo [RDX] Tip: Use cli-shell for interactive commands and daemon-shell for lifecycle control.
echo [1] env
echo [2] help
echo [3] Start MCP
echo [4] Daemon management

echo [0] Exit
echo.
set "RDX_MENU_CHOICE="
set /p RDX_MENU_CHOICE=[RDX] Choose an option [0-4]
if not defined RDX_MENU_CHOICE goto :menu_loop
if "%RDX_MENU_CHOICE%"=="1" goto :menu_env_check
if "%RDX_MENU_CHOICE%"=="2" goto :menu_help
if "%RDX_MENU_CHOICE%"=="3" goto :menu_start_mcp
if "%RDX_MENU_CHOICE%"=="4" goto :menu_daemon_loop

if "%RDX_MENU_CHOICE%"=="0" goto :menu_exit
echo [RDX] Invalid option: %RDX_MENU_CHOICE%
call :pause_prompt
goto :menu_loop

:menu_env_check
call :ensure_env_ready
set "EC=!ERRORLEVEL!"
if "!EC!"=="0" echo [RDX] Environment check passed.
call :pause_prompt
goto :menu_loop

:menu_help
call :print_help
call :pause_prompt
goto :menu_loop

:menu_start_mcp
call :ui_enter_menu
echo.
echo [RDX] Choose MCP transport:
echo [1] stdio
echo [2] streamable-http
echo [0] Back
set "RDX_MCP_CHOICE="
set /p RDX_MCP_CHOICE=[RDX] Choose an option [0-2]:
if "%RDX_MCP_CHOICE%"=="1" goto :menu_mcp_stdio
if "%RDX_MCP_CHOICE%"=="2" goto :menu_mcp_http
if "%RDX_MCP_CHOICE%"=="0" goto :menu_loop
if not defined RDX_MCP_CHOICE goto :menu_start_mcp
echo [RDX] Invalid option: %RDX_MCP_CHOICE%
call :pause_prompt
goto :menu_loop

:menu_mcp_stdio
call :ensure_env_ready
if errorlevel 1 (
  call :pause_prompt
  goto :menu_loop
)
call :log_success "[RDX] Starting MCP with stdio transport in new window..."
start "RDX MCP (stdio)" "%COMSPEC%" /k "echo [RDX] MCP transport=stdio, 鏃?URL 鍦板潃 & \"%SCRIPT_DIR%rdx.bat\" mcp --transport stdio"
call :log_success "[RDX] MCP started in new window. 鏃?URL 鍦板潃"
call :pause_prompt
goto :menu_loop

:menu_mcp_http
set "RDX_HTTP_HOST="
set /p RDX_HTTP_HOST=[RDX] Host (default 127.0.0.1):
if not defined RDX_HTTP_HOST set "RDX_HTTP_HOST=127.0.0.1"
set "RDX_HTTP_PORT="
set /p RDX_HTTP_PORT=[RDX] Port (default 8765):
if not defined RDX_HTTP_PORT set "RDX_HTTP_PORT=8765"
call :ensure_env_ready
if errorlevel 1 (
  call :pause_prompt
  goto :menu_loop
)
call :log_success "[RDX] Starting MCP with streamable-http on %RDX_HTTP_HOST%:%RDX_HTTP_PORT% ..."
start "RDX MCP (streamable-http)" "%COMSPEC%" /k "echo [RDX] MCP transport=streamable-http, URL=%RDX_HTTP_HOST%:%RDX_HTTP_PORT% & \"%SCRIPT_DIR%rdx.bat\" mcp --transport streamable-http --host \"%RDX_HTTP_HOST%\" --port \"%RDX_HTTP_PORT%\""
call :log_success "[RDX] MCP started in new window. URL: %RDX_HTTP_HOST%:%RDX_HTTP_PORT%"
call :pause_prompt
goto :menu_loop

:menu_daemon_loop
call :ui_enter_menu
cls
echo ===============================================
echo [RDX] Daemon management
echo ===============================================
echo [1] Start Daemon
echo [2] Show command lists
echo [3] Show examples
echo [0] Back
echo.
set "RDX_DAEMON_CHOICE="
set /p RDX_DAEMON_CHOICE=[RDX] Choose an option [0-3]:
if not defined RDX_DAEMON_CHOICE goto :menu_daemon_loop
if "%RDX_DAEMON_CHOICE%"=="1" goto :menu_daemon_start
if "%RDX_DAEMON_CHOICE%"=="2" goto :menu_daemon_status
if "%RDX_DAEMON_CHOICE%"=="3" goto :menu_daemon_examples
if "%RDX_DAEMON_CHOICE%"=="0" goto :menu_loop
echo [RDX] Invalid option: %RDX_DAEMON_CHOICE%
call :pause_prompt
goto :menu_daemon_loop

:menu_daemon_start
call :start_daemon_shell
call :pause_prompt
goto :menu_daemon_loop

:menu_daemon_status
call :log_info "[RDX] Available daemon-shell commands:"
call :log_info "[RDX]   daemon start"
call :log_info "[RDX]   daemon status"
call :log_info "[RDX]   daemon stop"
call :log_info "[RDX]   daemon connect --host [host] --port [port]"
call :log_info "[RDX]   capture open --file [capture.rdc] --frame-index [index]"
call :log_info "[RDX]   capture status"
call :log_info "[RDX]   call [tool] --args-json [json]"
call :pause_prompt
goto :menu_daemon_loop

:menu_daemon_examples
cls
echo ===============================================
echo [RDX] Daemon shell examples
echo ===============================================
echo [RDX] Start daemon shell:
echo [RDX]   rdx.bat daemon-shell cmd
echo.
echo [RDX] Inside shell:
echo [RDX]   1) daemon status
echo [RDX]   2) daemon stop
echo [RDX]   3) rdx [tool-command]
echo [RDX] Example interaction:
echo [RDX]   1 ^(status^)
echo [RDX]   3
echo [RDX]   daemon connect --host 127.0.0.1 --port 8765
echo [RDX]   capture status
echo [RDX]   call rd.event.get_actions --args-json '{"session_id":"[session_id]"}' --json
echo [RDX]   2 ^(stop^)
call :pause_prompt
goto :menu_daemon_loop

:menu_exit
call :ui_leave_menu
popd >nul
exit /b 0

:pause_prompt
echo [RDX] Press any key to return to menu . . .
pause >nul
exit /b 0

:ui_enter_menu
@echo off
if defined RDX_UI_MENU_ACTIVE exit /b 0
set "RDX_UI_MENU_ACTIVE=1"
if defined PROMPT (
  set "RDX_UI_PROMPT_WAS_DEFINED=1"
) else (
  set "RDX_UI_PROMPT_WAS_DEFINED=0"
)
set "RDX_UI_ORIG_PROMPT=%PROMPT%"
prompt $G
exit /b 0

:ui_leave_menu
if not defined RDX_UI_MENU_ACTIVE exit /b 0
if "%RDX_UI_PROMPT_WAS_DEFINED%"=="1" (
  set "PROMPT=%RDX_UI_ORIG_PROMPT%"
) else (
  set "PROMPT="
)
set "RDX_UI_MENU_ACTIVE="
set "RDX_UI_ORIG_PROMPT="
set "RDX_UI_PROMPT_WAS_DEFINED="
exit /b 0

:entry_mcp
call :ensure_env_ready
if errorlevel 1 (
  set "EC=!ERRORLEVEL!"
  popd >nul
  exit /b !EC!
)
goto :dispatch_mcp

:entry_cli_shell
call :ensure_env_ready
if errorlevel 1 (
  set "EC=!ERRORLEVEL!"
  popd >nul
  exit /b !EC!
)
call :run_cli_shell
set "EC=%ERRORLEVEL%"
popd >nul
exit /b %EC%

:entry_daemon_shell
if "%~1"=="" (
  set "RDX_DAEMON_CONTEXT=default"
) else (
  set "RDX_DAEMON_CONTEXT=%~1"
)
if not defined RDX_DAEMON_CONTEXT set "RDX_DAEMON_CONTEXT=default"
call :run_daemon_shell
popd >nul
exit /b %ERRORLEVEL%

:dispatch_mcp
shift
call :run_mcp %1 %2 %3 %4 %5 %6 %7 %8 %9
set "EC=%ERRORLEVEL%"
popd >nul
exit /b %EC%

:run_mcp
call :run_python mcp\\run_mcp.py %*
exit /b %ERRORLEVEL%

:run_cli
call :run_python cli\\run_cli.py %*
exit /b %ERRORLEVEL%

:run_cli_shell
@echo off
cls
doskey rdx=%PYTHON_EXE% "%SCRIPT_DIR%cli\run_cli.py" $*
if errorlevel 1 (
  call :log_error "[RDX] Failed to configure CLI alias."
  exit /b 1
)
call :log_info "[RDX] CLI Window ready."
call :log_info "[RDX] You can now run: rdx command-args"
call :log_info "[RDX] Alias target: python cli/run_cli.py command-args"
echo.
echo [RDX] Examples:
echo [RDX]   rdx capture open --file "C:\path\to\capture.rdc" --frame-index 0
echo [RDX]   rdx capture status
echo [RDX]   rdx call rd.event.get_actions --args-json '{"session_id":"[session_id]"}' --json
echo [RDX]   rdx daemon start
echo [RDX]   rdx daemon status
echo [RDX]   rdx daemon stop
echo.
exit /b 0

:start_daemon_shell
set "RDX_TS=%TIME%"
set "RDX_TS=%RDX_TS: =0%"
set "RDX_TS=%RDX_TS::=%"
set "RDX_TS=%RDX_TS:.=%"
set "RDX_DAEMON_CONTEXT=rdx-daemon-%RDX_TS%-%RANDOM%%RANDOM%"
call :log_info "[RDX] Opening daemon shell with context: %RDX_DAEMON_CONTEXT%"
start "RDX Daemon Shell - %RDX_DAEMON_CONTEXT%" "%COMSPEC%" /k ""%SCRIPT_DIR%rdx.bat" daemon-shell %RDX_DAEMON_CONTEXT%"
exit /b 0

:run_daemon_shell
call :ensure_env_ready
if errorlevel 1 (
  popd >nul
  exit /b 1
)

for /f "delims=" %%P in ('powershell -NoProfile -Command "$ppid=(Get-Process -Id $PID).Parent.Id; if ($ppid) { Write-Output $ppid }"') do set "RDX_DAEMON_OWNER_PID=%%P"
if not defined RDX_DAEMON_OWNER_PID set "RDX_DAEMON_OWNER_PID=0"

call :log_info "[RDX] Daemon shell started for context: %RDX_DAEMON_CONTEXT%"
call :run_cli --daemon-context "%RDX_DAEMON_CONTEXT%" daemon start --owner-pid "%RDX_DAEMON_OWNER_PID%"
if errorlevel 1 (
  call :log_error "[RDX] daemon start failed in shell."
  exit /b 1
)

:menu_daemon_shell_loop
call :ui_enter_menu
cls
echo ===============================================
echo [RDX] Daemon Shell (%RDX_DAEMON_CONTEXT%)
echo ===============================================
echo [1] daemon status
echo [2] daemon stop
echo [3] cli command
echo [0] Back
echo.
set "RDX_DAEMON_SHELL_CHOICE="
set /p RDX_DAEMON_SHELL_CHOICE=[RDX] Choose an option [0-3]:
if not defined RDX_DAEMON_SHELL_CHOICE goto :menu_daemon_shell_loop
if "%RDX_DAEMON_SHELL_CHOICE%"=="1" goto :daemon_shell_status
if "%RDX_DAEMON_SHELL_CHOICE%"=="2" goto :daemon_shell_stop
if "%RDX_DAEMON_SHELL_CHOICE%"=="3" goto :daemon_shell_cli
if "%RDX_DAEMON_SHELL_CHOICE%"=="0" goto :daemon_shell_exit
echo [RDX] Invalid option: %RDX_DAEMON_SHELL_CHOICE%
call :pause_prompt
goto :menu_daemon_shell_loop

:daemon_shell_status
call :run_cli --daemon-context "%RDX_DAEMON_CONTEXT%" daemon status
call :pause_prompt
goto :menu_daemon_shell_loop

:daemon_shell_stop
call :run_cli --daemon-context "%RDX_DAEMON_CONTEXT%" daemon stop
call :pause_prompt
goto :menu_daemon_shell_loop

:daemon_shell_cli
set "RDX_DAEMON_CLI_CMD="
set /p RDX_DAEMON_CLI_CMD=[RDX] CLI arguments (context auto-injected):
if not defined RDX_DAEMON_CLI_CMD goto :menu_daemon_shell_loop
call :run_cli --daemon-context "%RDX_DAEMON_CONTEXT%" %RDX_DAEMON_CLI_CMD%
call :pause_prompt
goto :menu_daemon_shell_loop

:daemon_shell_exit
call :log_warn "[RDX] exiting daemon shell, attempting daemon stop..."
call :run_cli --daemon-context "%RDX_DAEMON_CONTEXT%" daemon stop
call :pause_prompt
call :ui_leave_menu
exit /b 0

:ensure_env_ready
set "RDX_TS=%TIME%"
set "RDX_TS=%RDX_TS: =0%"
set "RDX_TS=%RDX_TS::=%"
set "RDX_TS=%RDX_TS:.=%"
set "RDX_ENV_CHECK_LOG=%TEMP%\\rdx_env_check_%RANDOM%_%RANDOM%_%RDX_TS%.log"
call :run_python mcp\\run_mcp.py --ensure-env >"%RDX_ENV_CHECK_LOG%" 2>&1
set "RDX_ENV_EC=%ERRORLEVEL%"
if "%RDX_ENV_EC%"=="0" (
  del /q "%RDX_ENV_CHECK_LOG%" >nul 2>&1
  exit /b 0
)

call :warn_line "Environment check failed."
type "%RDX_ENV_CHECK_LOG%"

if defined RDX_NON_INTERACTIVE (
  call :warn_line "Non-interactive mode: auto-install disabled."
  call :warn_line "Install manually: %PYTHON_EXE% -m pip install -e ."
  del /q "%RDX_ENV_CHECK_LOG%" >nul 2>&1
  exit /b 1
)

findstr /C:"missing python dependencies:" "%RDX_ENV_CHECK_LOG%" >nul 2>&1
if errorlevel 1 (
  call :warn_line "Auto-install skipped: issue is not Python dependencies."
  del /q "%RDX_ENV_CHECK_LOG%" >nul 2>&1
  exit /b 1
)

set "RDX_AUTO_FIX_CHOICE="
set /p RDX_AUTO_FIX_CHOICE=[RDX] Auto-install missing dependencies now? [Y/n]
if not defined RDX_AUTO_FIX_CHOICE set "RDX_AUTO_FIX_CHOICE=Y"
if /i not "%RDX_AUTO_FIX_CHOICE%"=="Y" if /i not "%RDX_AUTO_FIX_CHOICE%"=="YES" (
  call :warn_line "Skipped auto-install."
  call :warn_line "Install manually: %PYTHON_EXE% -m pip install -e ."
  del /q "%RDX_ENV_CHECK_LOG%" >nul 2>&1
  exit /b 1
)

call :warn_line "Installing dependencies into current Python environment..."
call :run_python -m pip install -e .
if errorlevel 1 (
  call :warn_line "Auto-install failed."
  call :warn_line "Install manually: %PYTHON_EXE% -m pip install -e ."
  del /q "%RDX_ENV_CHECK_LOG%" >nul 2>&1
  exit /b 1
)

call :run_python mcp\\run_mcp.py --ensure-env >"%RDX_ENV_CHECK_LOG%" 2>&1
set "RDX_ENV_EC=%ERRORLEVEL%"
if "%RDX_ENV_EC%"=="0" (
  call :warn_line "Environment is ready."
  del /q "%RDX_ENV_CHECK_LOG%" >nul 2>&1
  exit /b 0
)

call :warn_line "Environment check still failing after auto-install."
type "%RDX_ENV_CHECK_LOG%"
call :warn_line "Install manually: %PYTHON_EXE% -m pip install -e ."
del /q "%RDX_ENV_CHECK_LOG%" >nul 2>&1
exit /b 1

:warn_line
if defined RDX_ESC (
  echo %RDX_ESC%[33m[RDX][WARN] %~1%RDX_ESC%[0m
) else (
  echo [RDX][WARN] %~1
)
exit /b 0

:log_error
if defined RDX_ESC (
  echo %RDX_ESC%[31m%~1%RDX_ESC%[0m
) else (
  echo [RDX][ERR] %~1
)
exit /b 0

:log_success
if defined RDX_ESC (
  echo %RDX_ESC%[32m%~1%RDX_ESC%[0m
) else (
  echo [RDX][OK] %~1
)
exit /b 0

:log_warn
if defined RDX_ESC (
  echo %RDX_ESC%[33m%~1%RDX_ESC%[0m
) else (
  echo [RDX][WARN] %~1
)
exit /b 0

:log_info
if defined RDX_ESC (
  echo %RDX_ESC%[36m%~1%RDX_ESC%[0m
) else (
  echo %~1
)
exit /b 0

:run_python
if /i "%RDX_USE_UV%"=="1" if not defined RDX_UV_FALLBACK_DONE (
  where uv >nul 2>&1
  if not errorlevel 1 (
    uv run --project . python %*
    if not errorlevel 1 exit /b 0
    set "RDX_UV_FALLBACK_DONE=1"
  )
)
if "%PYTHON_EXE%"=="py -3" (
  py -3 %*
) else (
  python %*
)
exit /b %ERRORLEVEL%

:help
call :print_help
popd >nul
exit /b 0
:unsupported_command
echo [RDX] Unknown command: %*
goto :help_err

:help_err
echo [RDX] Unsupported/unknown command.
echo [RDX] Try: rdx.bat menu
echo [RDX] Also available:
echo [RDX]   rdx.bat cli-shell
echo [RDX]   rdx.bat [--non-interactive] daemon-shell [context]
echo [RDX]   rdx.bat --help
popd >nul
exit /b 2

:unsupported_cli
echo [RDX] Error: one-shot CLI entry `rdx.bat cli ...` is no longer supported.
echo [RDX] Use the following replacements:
echo [RDX]   - rdx.bat cli-shell                      (interactive single command mode)
echo [RDX]   - rdx.bat [--non-interactive] daemon-shell [context] (daemon-context workflow)
echo [RDX] You can still run daemon workflows with: rdx.bat daemon-shell and then `rdx daemon ...`.
echo [RDX] For env precheck: rdx.bat --non-interactive mcp --ensure-env
popd >nul
exit /b 2

:print_help
echo ===============================================
echo [RDX] rdx-tools
echo ===============================================
echo.
echo [RDX] Usage:
echo [RDX]   rdx.bat
echo [RDX]   rdx.bat menu
echo [RDX]   rdx.bat --help
echo [RDX]   rdx.bat -h
echo [RDX]   rdx.bat --non-interactive mcp [--ensure-env] [--transport ...]
echo [RDX]   rdx.bat cli-shell
echo [RDX]   rdx.bat [--non-interactive] daemon-shell [context]
echo.
echo [RDX] Menu:
echo [RDX]   1. env
echo [RDX]   2. help
echo [RDX]   3. Start MCP

echo [RDX]   4. Daemon management
echo [RDX]   0. Exit
echo.
echo [RDX] Notes:
echo [RDX] - Use `rdx.bat cli-shell` for interactive commands.
echo [RDX] - Use `rdx.bat daemon-shell` for lifecycle + stateful commands.
exit /b 0




