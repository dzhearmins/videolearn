@echo off
setlocal EnableExtensions
chcp 65001 >nul
title pku-videolearn bootstrap

rem One-click bootstrap for Windows:
rem   - uses the local repo if this file sits next to videolearn\__main__.py
rem   - otherwise downloads the source zip from GitHub
rem   - finds Python 3.10+; if missing, downloads a private Python 3.12 (per-user, no admin)
rem   - creates a venv and installs requirements (Tsinghua PyPI mirror, falls back to pypi.org)
rem   - finds FFmpeg; if missing, tries winget then a portable zip
rem   - starts the local web UI and forwards extra args (%*), e.g. start.bat --port 8123

set "HERE=%~dp0"
set "TOOLS=%LOCALAPPDATA%\pku-videolearn"
set "APP="
set "PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple"
if defined PIP_INDEX_URL set "PIP_INDEX=%PIP_INDEX_URL%"

echo ============================================
echo  pku-videolearn - one-click bootstrap
echo ============================================
echo.

rem ---------------- 1. source code ----------------
if exist "%HERE%videolearn\__main__.py" set "APP=%HERE%"
if defined APP goto :app_ok
if exist "%TOOLS%\app\videolearn\__main__.py" set "APP=%TOOLS%\app"
if defined APP goto :app_ok

echo [1/4] downloading source code ...
if not exist "%TOOLS%" mkdir "%TOOLS%" >nul 2>nul
call :download "https://github.com/dzhearmins/videolearn/archive/refs/heads/main.zip" "%TOOLS%\app.zip" || goto :fail
if exist "%TOOLS%\app" rmdir /s /q "%TOOLS%\app" >nul 2>nul
tar -xf "%TOOLS%\app.zip" -C "%TOOLS%" || goto :fail
if not exist "%TOOLS%\videolearn-main" goto :fail
move /y "%TOOLS%\videolearn-main" "%TOOLS%\app" >nul || goto :fail
set "APP=%TOOLS%\app"

:app_ok
echo     app: %APP%

rem ---------------- 2. python ----------------
set "PYOK="
py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if not errorlevel 1 set "PYOK=py"
if defined PYOK goto :py_ok

python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if not errorlevel 1 set "PYOK=python"
if defined PYOK goto :py_ok

if exist "%TOOLS%\python\python.exe" (
  "%TOOLS%\python\python.exe" -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
  if not errorlevel 1 set "PYOK=%TOOLS%\python\python.exe"
)
if defined PYOK goto :py_ok

echo [2/4] Python 3.10+ not found - downloading a private Python 3.12 ...
set "PYINST=%TOOLS%\python-3.12.10-amd64.exe"
if not exist "%PYINST%" (
  call :download "https://registry.npmmirror.com/-/binary/python/3.12.10/python-3.12.10-amd64.exe" "%PYINST%"
  if errorlevel 1 call :download "https://mirrors.huaweicloud.com/python/3.12.10/python-3.12.10-amd64.exe" "%PYINST%"
  if errorlevel 1 call :download "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" "%PYINST%"
  if errorlevel 1 goto :fail
)
echo     installing (per-user, no admin) ...
start /wait "" "%PYINST%" /quiet InstallAllUsers=0 TargetDir="%TOOLS%\python" Include_pip=1 Include_test=0 Include_launcher=0 PrependPath=0 Shortcuts=0
if not exist "%TOOLS%\python\python.exe" goto :fail
set "PATH=%TOOLS%\python;%TOOLS%\python\Scripts;%PATH%"
set "PYOK=%TOOLS%\python\python.exe"

:py_ok
if "%PYOK%"=="py" set "PATH=%TOOLS%\venv\Scripts;%PATH%"
if "%PYOK%"=="python" set "PATH=%TOOLS%\venv\Scripts;%PATH%"
echo     python: ok

rem ---------------- 3. venv + deps ----------------
if exist "%TOOLS%\venv\Scripts\python.exe" (
  "%TOOLS%\venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
  if errorlevel 1 rmdir /s /q "%TOOLS%\venv" >nul 2>nul
)
if exist "%TOOLS%\venv\Scripts\python.exe" goto :venv_ok
echo [3/4] creating virtualenv ...
if "%PYOK%"=="py" (
  py -3 -m venv "%TOOLS%\venv" || goto :fail
) else if "%PYOK%"=="python" (
  python -m venv "%TOOLS%\venv" || goto :fail
) else (
  "%PYOK%" -m venv "%TOOLS%\venv" || goto :fail
)
:venv_ok
set "VENV_PY=%TOOLS%\venv\Scripts\python.exe"
if not exist "%VENV_PY%" goto :fail

"%VENV_PY%" -c "import faster_whisper, requests, bs4, cryptography, dotenv, numpy" >nul 2>nul
if not errorlevel 1 goto :deps_ok
echo     installing Python packages (first run, several minutes) ...
"%VENV_PY%" -m pip install --disable-pip-version-check --no-input -r "%APP%\requirements.txt" -i "%PIP_INDEX%" --timeout 60
if errorlevel 1 (
  echo     mirror failed, retrying with pypi.org ...
  "%VENV_PY%" -m pip install --disable-pip-version-check --no-input -r "%APP%\requirements.txt" -i "https://pypi.org/simple" --timeout 60
)
if errorlevel 1 goto :fail

:deps_ok
rem ---------------- 4. ffmpeg ----------------
where /q ffmpeg
if errorlevel 1 goto :ff_links
where /q ffprobe
if not errorlevel 1 goto :ff_ok

:ff_links
if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" (
  set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
  goto :ff_ok
)
rem already installed by winget, but not on PATH:
for /d %%D in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*") do (
  for /r "%%D" %%F in (ffmpeg.exe) do if not defined FFDIR set "FFDIR=%%~dpF"
)
if defined FFDIR (
  set "PATH=%FFDIR%;%PATH%"
  goto :ff_ok
)

where /q winget
if errorlevel 1 goto :ff_download
echo [4/4] trying winget for ffmpeg ...
winget install --id Gyan.FFmpeg -e --source winget --accept-source-agreements --accept-package-agreements --silent >nul 2>nul
if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" (
  set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
  goto :ff_ok
)

:ff_download
echo [4/4] downloading portable ffmpeg ...
if not exist "%TOOLS%\tools\ffmpeg" mkdir "%TOOLS%\tools\ffmpeg" >nul 2>nul
call :download "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" "%TOOLS%\ffmpeg.zip"
if errorlevel 1 call :download "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip" "%TOOLS%\ffmpeg.zip"
if errorlevel 1 goto :fail
tar -xf "%TOOLS%\ffmpeg.zip" -C "%TOOLS%\tools\ffmpeg" || goto :fail
set "FFDIR="
for /r "%TOOLS%\tools\ffmpeg" %%F in (ffmpeg.exe) do if not defined FFDIR set "FFDIR=%%~dpF"
if not defined FFDIR goto :fail
set "PATH=%FFDIR%;%PATH%"

:ff_ok
echo     ffmpeg: ok

rem ---------------- 5. start ----------------
echo.
echo Starting web UI ... (close this window or Ctrl+C to stop)
cd /d "%APP%"
"%VENV_PY%" -m videolearn web %*
exit /b %ERRORLEVEL%

rem ---------------- helpers ----------------
:download
set "URL=%~1"
set "DEST=%~2"
if exist "%DEST%" del /q "%DEST%" >nul 2>nul
echo     %URL%
where /q curl
if not errorlevel 1 (
  curl.exe -L --fail --retry 2 --connect-timeout 20 --max-time 1800 -o "%DEST%" "%URL%" >nul 2>nul
  if not errorlevel 1 if exist "%DEST%" exit /b 0
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -UseBasicParsing -Uri '%URL%' -OutFile '%DEST%' } catch { exit 1 }"
if exist "%DEST%" exit /b 0
exit /b 1

:fail
echo.
echo Bootstrap failed. Common fixes:
echo   - check network / proxy (set HTTPS_PROXY if you use one)
echo   - or install Python 3.10+ and FFmpeg manually, then run run.bat
pause
exit /b 1
