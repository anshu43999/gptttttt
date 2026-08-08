@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if exist "%~dp0..\env.db.bat" call "%~dp0..\env.db.bat"
if /I "%GPT_REGISTER_DB_BACKEND%"=="" (
  set "GPT_REGISTER_DB_BACKEND=postgres"
)
if "%GPT_REGISTER_DATABASE_URL%"=="" (
  set "GPT_REGISTER_DATABASE_URL=postgresql://gpt:gpt@127.0.0.1:5432/gpt_register"
)
if "%DATABASE_URL%"=="" set "DATABASE_URL=%GPT_REGISTER_DATABASE_URL%"
echo [with-pg] backend=%GPT_REGISTER_DB_BACKEND%
echo [with-pg] url=%GPT_REGISTER_DATABASE_URL%
if "%~1"=="" (
  echo Usage: with-pg.bat go run ./cmd/pure-go-register-batch -n 3 -db ../data/gpt_register.db
  echo        with-pg.bat go test ./internal/store/ -count=1
  exit /b 2
)
%*
exit /b %ERRORLEVEL%
