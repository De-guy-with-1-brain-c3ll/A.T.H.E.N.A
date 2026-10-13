@echo off
setlocal
cd /d "%~dp0.."
echo Installing the tested PC agent integration on ATHENA's Pi.
echo Enter the Pi SSH password when asked. It is not saved or displayed.
> "data\openclaw\pi-install-result.txt" echo running
".venv\Scripts\python.exe" tools\run_pi_qa.py --deploy --pc-agent-config
set "INSTALLCODE=%ERRORLEVEL%"
> "data\openclaw\pi-install-result.txt" echo %INSTALLCODE%
if not "%INSTALLCODE%"=="0" (
    echo Installation did not finish. Review the error above.
    pause
    exit /b 1
)
echo ATHENA has been updated. Try: Open a terminal on my desktop.
pause
