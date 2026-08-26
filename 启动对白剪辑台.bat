@echo off
cd /d "%~dp0"
if exist "desktop\publish\对白剪辑台.exe" goto desktop
python --version >nul 2>nul
if errorlevel 1 goto no_python
python app.py --open
if errorlevel 1 goto failed
goto end

:desktop
start "" "desktop\publish\对白剪辑台.exe"
goto end

:no_python
echo Python was not found on this computer.
echo Please send a screenshot of this window to Codex.
pause
goto end

:failed
echo.
echo The editor could not start.
echo Please send a screenshot of this window to Codex.
pause

:end
