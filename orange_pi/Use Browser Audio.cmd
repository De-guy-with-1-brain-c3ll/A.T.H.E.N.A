@echo off
setlocal
title ATHENA Browser Audio ON
rem The Pi's address changes with DHCP. Edit orange_pi\pi-address.txt, not this file.
set PI=192.168.33.153
if exist "%~dp0pi-address.txt" for /f "usebackq delims=" %%a in ("%~dp0pi-address.txt") do set PI=%%a

echo Letting a device on your local network provide ATHENA's microphone and speaker.
echo You will be asked for the Pi's root password.
echo.
echo Then open http://%PI%:8780 on that device, sign in, and press Start under
echo "Microphone and speaker". Use headphones.
echo.

ssh -tt root@%PI% "bash -s -- on" < "%~dp0pi\browser_audio.sh"
echo.
pause
