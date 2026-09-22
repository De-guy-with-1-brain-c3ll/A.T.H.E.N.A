@echo off
setlocal
title ATHENA Browser Audio OFF
rem The Pi's address changes with DHCP. Edit orange_pi\pi-address.txt, not this file.
set PI=192.168.33.153
if exist "%~dp0pi-address.txt" for /f "usebackq delims=" %%a in ("%~dp0pi-address.txt") do set PI=%%a

echo Giving the microphone and speaker back to the Pi's own ALSA devices.
echo You will be asked for the Pi's root password.
echo.

ssh -tt root@%PI% "bash -s -- off" < "%~dp0pi\browser_audio.sh"
echo.
pause
