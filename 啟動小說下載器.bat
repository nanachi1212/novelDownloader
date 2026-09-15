@echo off
setlocal
set "APP_DIR=%~dp0"
set "PATH=%APP_DIR%;%PATH%"
start "novelDownloader" /d "%APP_DIR%" "%APP_DIR%novelDownloader.exe"
endlocal
