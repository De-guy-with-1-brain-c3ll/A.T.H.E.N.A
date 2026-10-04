@echo off
rem Deploy the ATHENA release published in orange_pi\update_feed onto the Orange Pi.
rem
rem Two things a signed release cannot carry, done here over SSH:
rem   1. /etc/sudoers.d/athena-web-control, which needs a new rule for the
rem      dashboard's "Restart everything" button.
rem   2. /opt/athena/data/prompts/system_prompt.txt, the Pi's own editable copy,
rem      which always wins over the packaged default in src/athena/system.
rem
rem Run on the PC. Requires root SSH to the Pi.
setlocal EnableDelayedExpansion

set "ROOT=%~dp0.."
pushd "%ROOT%"
set "ROOT=%CD%"
popd
set "FEED=%ROOT%\orange_pi\update_feed"
set "KEYFILE=%ROOT%\orange_pi\.update-key"
set "ADDRFILE=%ROOT%\orange_pi\pi-address.txt"
set "SUDOERS=%ROOT%\orange_pi\config\athena-web-control.sudoers"
set "PORT=8765"

if not exist "%KEYFILE%" (
  echo ERROR: %KEYFILE% is missing.
  pause
  exit /b 1
)
if not exist "%SUDOERS%" (
  echo ERROR: %SUDOERS% is missing.
  pause
  exit /b 1
)
set /p UPDATE_KEY=<"%KEYFILE%"

set "PI="
if exist "%ADDRFILE%" set /p PI=<"%ADDRFILE%"
if not defined PI set /p PI=Pi address: 

for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 ^| Where-Object { $_.PrefixOrigin -ne 'WellKnown' -and $_.IPAddress -notlike '127.*' } ^| Select-Object -First 1 -ExpandProperty IPAddress)"`) do set "PCIP=%%A"
if not defined PCIP (
  echo ERROR: could not work out this PC's LAN address.
  pause
  exit /b 1
)

echo.
echo   Pi          : %PI%
echo   PC feed     : http://%PCIP%:%PORT%/
echo   Release     : %FEED%\manifest.json
echo.
echo Keep this window open: it serves the feed the Pi pulls from.
echo.

start "ATHENA update feed" /min python "%ROOT%\orange_pi\pc\serve_updates.py" --bind 0.0.0.0 --port %PORT% --feed "%FEED%"
timeout /t 3 /nobreak >nul

echo [1/3] Checking the Pi can reach this PC's feed...
ssh -o ConnectTimeout=10 root@%PI% "curl -fsS 'http://%PCIP%:%PORT%/manifest.json' >/dev/null && echo FEED_OK"
if errorlevel 1 (
  echo.
  echo The Pi could not reach http://%PCIP%:%PORT%/ . Check that the Pi is on the
  echo same network and that Windows Firewall allows port %PORT%.
  pause
  exit /b 1
)

echo.
echo [2/3] Pulling and installing the release on the Pi...
ssh -t root@%PI% "ATHENA_UPDATE_URL='http://%PCIP%:%PORT%/' ATHENA_UPDATE_KEY='%UPDATE_KEY%' python3 /opt/athena/update_client.py --url 'http://%PCIP%:%PORT%/' --service athena-voice.service"
if errorlevel 1 (
  echo.
  echo The pull failed. The usual cause is that the Pi's installed update client
  echo predates the feed's manifest format, in which case run install.sh on the
  echo Pi once to refresh /opt/athena/update_client.py, then try again.
  pause
  exit /b 1
)

echo.
echo [3/3] Installing the sudoers rule for the restart-everything button...
type "%SUDOERS%" | ssh root@%PI% "cat > /etc/sudoers.d/athena-web-control && chmod 0440 /etc/sudoers.d/athena-web-control && chown root:root /etc/sudoers.d/athena-web-control && visudo -cf /etc/sudoers.d/athena-web-control"
if errorlevel 1 (
  echo WARNING: the sudoers rule did not install cleanly. The restart button will
  echo report a permission error until it does.
)

echo.
echo Clearing the Pi's prompt override so the new generic prompt takes effect...
ssh root@%PI% "rm -f /opt/athena/data/prompts/system_prompt.txt"
ssh root@%PI% "systemctl restart athena-voice.service"

echo.
echo Done. Open the dashboard and confirm:
echo   - a "Restart everything" button sits under Start / Restart / Stop
echo   - the Behavior tab shows the new prompt, with no mention of JARVIS
echo.
pause
