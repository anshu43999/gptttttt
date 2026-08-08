@echo off
REM Load KEY=VALUE from env.db into current process.
REM Non-empty existing vars win. Safe to call multiple times.
REM Usage: call "%~dp0env.db.bat"
REM
REM Implementation: generate a temp .cmd with "if not defined KEY set KEY=VAL"
REM then call it after endlocal so values stick in the caller's environment.

if not exist "%~dp0env.db" exit /b 0

setlocal EnableExtensions DisableDelayedExpansion
set "ENV_FILE=%~dp0env.db"
set "TMP_SET=%TEMP%\gpt_register_env_db_load.cmd"

> "%TMP_SET%" echo @echo off
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
  if not "%%~A"=="" if not "%%~B"=="" (
    >> "%TMP_SET%" echo if not defined %%~A set "%%~A=%%~B"
  )
)
endlocal

call "%TEMP%\gpt_register_env_db_load.cmd"
exit /b 0
