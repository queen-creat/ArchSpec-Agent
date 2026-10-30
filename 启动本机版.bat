@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 exit /b 1
where uv >nul 2>&1
if errorlevel 1 (
  echo uv is required. See README.md for setup instructions.
  exit /b 127
)
echo Preparing locked dependencies. First run requires internet access.
uv sync --frozen --no-dev
if errorlevel 1 exit /b %errorlevel%
echo Starting local workspace. Press Ctrl+C to stop.
uv run --frozen --no-dev --offline --no-env-file python scripts/run_local.py --personal %*
exit /b %errorlevel%
