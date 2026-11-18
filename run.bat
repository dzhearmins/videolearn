@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

rem Default: start the local web UI at http://127.0.0.1:8765
rem   run.bat                      web UI
rem   run.bat --port 8123          web UI on a custom port
rem   run.bat --no-browser         web UI without opening a browser
rem   run.bat web --port 8123      same, explicit subcommand
rem   run.bat doctor               CLI: environment check (login / courses / run too)
rem   run.bat cli                  legacy interactive CLI
rem   run.bat cli run --course X   legacy CLI with arguments

set "CMD=%~1"
if "%CMD%"=="" goto :web
if /I "%CMD%"=="--cli" goto :strip_cli
if /I "%CMD%"=="cli" goto :strip_cli
if /I "%CMD%"=="web" goto :passthrough
if /I "%CMD%"=="login" goto :passthrough
if /I "%CMD%"=="courses" goto :passthrough
if /I "%CMD%"=="doctor" goto :passthrough
if /I "%CMD%"=="run" goto :passthrough
if "%CMD:~0,1%"=="-" goto :web
goto :passthrough

:web
python -m videolearn web %*
exit /b %ERRORLEVEL%

:passthrough
python -m videolearn %*
exit /b %ERRORLEVEL%

:strip_cli
shift
python -m videolearn %1 %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%
