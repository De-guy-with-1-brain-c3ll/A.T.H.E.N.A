@echo off
setlocal
title ATHENA Live Microphone Monitor
rem The Pi's address changes with DHCP. Edit orange_pi\pi-address.txt, not this file.
set PI=192.168.33.153
if exist "%~dp0pi-address.txt" for /f "usebackq delims=" %%a in ("%~dp0pi-address.txt") do set PI=%%a

echo Connecting to ATHENA at %PI%...
echo The display will keep updating until you press Ctrl+C.
echo Stopping the display does not stop ATHENA.
echo.
ssh -tt root@%PI% "/opt/athena/current/.venv/bin/athena-audio-monitor"
pause
