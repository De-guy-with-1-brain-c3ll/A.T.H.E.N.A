@echo off
title ATHENA Live Microphone Monitor
echo Connecting to ATHENA at 192.168.31.159...
echo The display will keep updating until you press Ctrl+C.
echo Stopping the display does not stop ATHENA.
echo.
ssh -tt root@192.168.31.159 "/opt/athena/current/.venv/bin/athena-audio-monitor"
pause
