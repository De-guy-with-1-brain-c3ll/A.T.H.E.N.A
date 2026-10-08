@echo off
rem Keeps ATHENA's PC receiver running: file transfers into the inbox, and the
rem browser bridge. Close this window only when you want the receiver stopped.
setlocal
rem %~dp0 ends with a backslash, so the parent is "%~dp0.." and every path
rem built from it needs a separator of its own.
set "ROOT=%~dp0.."
cd /d "%ROOT%"
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
set "ATHENA_PC_TRANSFER_KEY="
for /f "usebackq delims=" %%k in ("%~dp0.pc-transfer-key") do set "ATHENA_PC_TRANSFER_KEY=%%k"
if not defined ATHENA_PC_TRANSFER_KEY (
    echo No transfer key found at "%~dp0.pc-transfer-key".
    echo Run:  .venv\Scripts\python.exe tools\setup_pc_inbox.py --bind auto
    pause
    exit /b 1
)
if not exist "%ROOT%\.venv\Scripts\python.exe" (
    echo Cannot find the virtualenv at "%ROOT%\.venv\Scripts\python.exe".
    pause
    exit /b 1
)
echo Starting ATHENA's PC receiver. Leave this window open.
echo Using --bind auto, so a change of IP address cannot silently break this.
echo.
"%ROOT%\.venv\Scripts\python.exe" -m athena.pc_transfer --bind auto --inbox "%ROOT%\ATHENA Inbox"
set "CODE=%ERRORLEVEL%"
echo.
if not "%CODE%"=="0" (
    echo The receiver stopped with exit code %CODE%.
    echo The reason is in "%ROOT%\logs\pc-inbox.log".
) else (
    echo The receiver stopped normally.
)
pause