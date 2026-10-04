@echo off
setlocal
rem The Pi's address changes with DHCP. Edit orange_pi\pi-address.txt, not this file.
set PI=192.168.33.153
if exist "%~dp0pi-address.txt" for /f "usebackq delims=" %%a in ("%~dp0pi-address.txt") do set PI=%%a
rem HTTPS, because the microphone and music need a "secure context". If the
rem dashboard has been reverted to plain HTTP with "Use Dashboard HTTPS.cmd
rem --off", change this back to http.
start "" "https://%PI%:8780"
