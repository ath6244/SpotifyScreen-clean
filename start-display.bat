@echo off
set "PROJECT_ROOT=%~dp0"
if not exist "%PROJECT_ROOT%var\logs" mkdir "%PROJECT_ROOT%var\logs"
cd /d "%PROJECT_ROOT%"
set "PYTHONPATH=%PROJECT_ROOT%src"
pythonw.exe -m spotify_display.server >> "%PROJECT_ROOT%var\logs\server.log" 2>&1