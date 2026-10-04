@echo off
setlocal
title ATHENA Dashboard Certificate
rem The Pi's address changes with DHCP. Edit orange_pi\pi-address.txt, not this file.
set PI=192.168.33.153
if exist "%~dp0pi-address.txt" for /f "usebackq delims=" %%a in ("%~dp0pi-address.txt") do set PI=%%a

rem certutil writes to the machine's trust store, which needs elevation.
net session >nul 2>&1
if errorlevel 1 (
  echo This needs administrator rights: close this window, right-click the file
  echo and choose "Run as administrator".
  echo.
  pause
  exit /b 1
)

set CRT=%TEMP%\athena-dashboard.crt
echo Copying the dashboard certificate from %PI% ...
echo You will be asked for the Pi's root password.
echo.
scp root@%PI%:/etc/athena/tls/dashboard.crt "%CRT%"
if errorlevel 1 (
  echo.
  echo Could not copy the certificate. Run "Use Dashboard HTTPS.cmd" first, and
  echo check the address in orange_pi\pi-address.txt.
  echo.
  pause
  exit /b 1
)

certutil -addstore -f Root "%CRT%"
if errorlevel 1 (
  echo.
  echo Windows refused to import the certificate.
  echo.
  pause
  exit /b 1
)

echo.
echo Trusted. Close and reopen your browser (it reads the trust store at start-up),
echo then open https://%PI%:8780 and press Start under "Microphone and speaker".
echo.
pause
