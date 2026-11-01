@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if errorlevel 1 goto failed
set "UV_CMD="
if exist "%CD%\.tools\uv.exe" set "UV_CMD=%CD%\.tools\uv.exe"
if not defined UV_CMD for /f "delims=" %%i in ('where uv 2^>nul') do if not defined UV_CMD set "UV_CMD=%%i"
if not defined UV_CMD if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV_CMD=%USERPROFILE%\.local\bin\uv.exe"
if defined UV_CMD goto sync
 echo 首次启动：正在从 uv 官方网站下载运行工具，需要联网。
if not exist ".tools" mkdir ".tools"
set "UV_UNMANAGED_INSTALL=%CD%\.tools"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; irm https://astral.sh/uv/0.13.6/install.ps1 | iex"
if errorlevel 1 goto failed
set "UV_CMD=%CD%\.tools\uv.exe"
if not exist "%UV_CMD%" goto failed
:sync
echo 正在准备运行环境，首次可能需要几分钟，请保持此窗口打开。
"%UV_CMD%" sync --frozen --no-dev
if errorlevel 1 goto failed
echo 请在设置窗口中填写自己的 DeepSeek Key，使用期间请保持此窗口打开。
"%UV_CMD%" run --frozen --no-dev --offline --no-env-file python scripts/run_local.py --personal --easy-start --open-browser %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo 启动未完成，请检查网络后重新双击启动。
pause
exit /b 1
