@echo off
setlocal
title ATHENA Dashboard HTTPS
rem The Pi's address changes with DHCP. Edit orange_pi\pi-address.txt, not this file.
set PI=192.168.33.153
if exist "%~dp0pi-address.txt" for /f "usebackq delims=" %%a in ("%~dp0pi-address.txt") do set PI=%%a

rem Optional first argument: "off" reverts to plain HTTP, anything else (or
rem nothing) enables HTTPS. "--force" rebuilds the certificate.
set MODE=%1
if "%MODE%"=="" set MODE=on

echo Putting ATHENA's dashboard on HTTPS and issuing its certificate.
echo.
echo Why: a browser only hands a page the microphone in a "secure context", and a
echo plain http address on the local network is not one. That is what blocks the
echo microphone -- and music with it, because the speaker attaches over the same
echo socket.
echo.
echo You will be asked for the Pi's root password.
echo Afterwards run "Trust Dashboard Certificate.cmd" as administrator on every
echo device that opens the dashboard.
echo.

ssh -tt root@%PI% "bash -s -- %MODE%" < "%~dp0pi\dashboard_tls.sh"
echo.
if "%MODE%"=="off" (
  echo Open the dashboard at http://%PI%:8780
  echo The microphone will be blocked again until HTTPS is switched back on.
) else (
  echo Open the dashboard at https://%PI%:8780
)
echo.
echo To revert to plain HTTP, run this file again with "off".
echo.
pause
