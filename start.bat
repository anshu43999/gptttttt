@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

REM Main-DB flip: load env.db (postgres). Existing env wins. Rollback: set GPT_REGISTER_DB_BACKEND=sqlite
if exist "%~dp0env.db.bat" call "%~dp0env.db.bat"

call :resolve_python
if errorlevel 1 exit /b %ERRORLEVEL%

REM No args -> one-click (Go worker + WebUI, default port 47718)
if "%~1"=="" (
  call :run_start all
  exit /b %ERRORLEVEL%
)

set "CHOICE=%~1"
if /I "%CHOICE%"=="--check" set "CHOICE=check"
if /I "%CHOICE%"=="all" set "CHOICE=all"
if /I "%CHOICE%"=="prod" set "CHOICE=all"
if /I "%CHOICE%"=="one" set "CHOICE=all"
if /I "%CHOICE%"=="start" set "CHOICE=all"
if /I "%CHOICE%"=="go" set "CHOICE=all"
if /I "%CHOICE%"=="dev" set "CHOICE=dev"
if /I "%CHOICE%"=="split" set "CHOICE=dev"
if /I "%CHOICE%"=="check" set "CHOICE=check"
if /I "%CHOICE%"=="menu" set "CHOICE=menu"
if /I "%CHOICE%"=="rebuild" set "CHOICE=rebuild"
if /I "%CHOICE%"=="force" set "CHOICE=rebuild"
if /I "%CHOICE%"=="restart" set "CHOICE=restart"

if /I "%CHOICE%"=="all" (
  call :run_start all
  exit /b %ERRORLEVEL%
)
if /I "%CHOICE%"=="dev" (
  call :run_start dev
  exit /b %ERRORLEVEL%
)
if /I "%CHOICE%"=="check" (
  call :run_start check
  exit /b %ERRORLEVEL%
)
if /I "%CHOICE%"=="menu" (
  call :run_start
  exit /b %ERRORLEVEL%
)
if /I "%CHOICE%"=="rebuild" (
  REM Force frontend rebuild then one-click start on default port.
  set "FORCE_BUILD=1"
  call :run_start all
  exit /b %ERRORLEVEL%
)
if /I "%CHOICE%"=="restart" (
  REM Kill stale WebUI on 47718 (or preferred port), force rebuild, then start.
  set "FORCE_BUILD=1"
  set "GPT_REGISTER_RECLAIM_WEBUI=1"
  call :run_start all
  exit /b %ERRORLEVEL%
)

REM Numeric arg = preferred WebUI port (one-click)
echo %CHOICE%| findstr /R "^[0-9][0-9]*$" >nul
if not errorlevel 1 (
  set "GPT_REGISTER_BACKEND_PORT=%CHOICE%"
  set "GPT_REGISTER_RECLAIM_WEBUI=1"
  call :run_start %CHOICE%
  exit /b %ERRORLEVEL%
)

echo [ERR] unknown option: %CHOICE%
echo Usage:
echo   start.bat              one-click Go worker + WebUI
echo   start.bat all          same as no args
echo   start.bat rebuild      FORCE_BUILD=1 then one-click
echo   start.bat restart      reclaim port 47718 + rebuild + start
echo   start.bat dev          backend --reload + Vite HMR
echo   start.bat check        environment check
echo   start.bat 47718        one-click on preferred port
exit /b 2

:run_start
if "%~1"=="" (
  "%PY313%" "%~dp0start.py"
) else (
  "%PY313%" "%~dp0start.py" %*
)
exit /b %ERRORLEVEL%

:resolve_python
set "PY313="

if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "PY313=%~dp0.venv\Scripts\python.exe"
    goto :python_ready
  )
  echo [ERR] .venv exists but is not Python 3.13
  echo       py -3.13 -m venv .venv
  exit /b 1
)

REM Prefer the py launcher, but always resolve to a real python.exe path so
REM quoting works (project path has spaces: "GPT Register").
py -3.13 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)" >nul 2>nul
if not errorlevel 1 (
  for /f "delims=" %%I in ('py -3.13 -c "import sys; print(sys.executable)" 2^>nul') do set "PY313=%%I"
  if defined PY313 goto :python_ready
)

python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)" >nul 2>nul
if not errorlevel 1 (
  for /f "delims=" %%I in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PY313=%%I"
  if defined PY313 goto :python_ready
)

echo [ERR] Python 3.13 not found
echo       install Python 3.13 or: py -3.13 -m venv .venv
exit /b 1

:python_ready
if not exist "%PY313%" (
  echo [ERR] resolved Python path missing: %PY313%
  exit /b 1
)
echo [Python] %PY313%
"%PY313%" --version
exit /b %ERRORLEVEL%
