@echo off
REM Builds dist\OfficeMeetingAgent.exe (one file, no console). Needs Python 3.11 or 3.12 installed.
cd /d %~dp0
if not defined PY set PY=py -3.12
"%PY%" --version >nul 2>&1 || %PY% --version >nul 2>&1 || set PY=py -3.11
"%PY%" --version >nul 2>&1 || %PY% --version >nul 2>&1 || (echo Python 3.11/3.12 not found & exit /b 1)

if exist "%PY%" (set "PY_CMD="%PY%"") else (set "PY_CMD=%PY%")
%PY_CMD% -m pip install --upgrade pip pyinstaller -r requirements.txt pyaudiowpatch || exit /b 1
%PY_CMD% -m PyInstaller --noconfirm --onefile --windowed --name OfficeMeetingAgent ^
  --add-data "config.example.json;." --add-data "assets;assets" ^
  --icon assets\icon.ico --version-file version_info.txt ^
  --hidden-import pystray._win32 --collect-all sounddevice ^
  office_agent.py || exit /b 1

echo.
echo Built dist\OfficeMeetingAgent.exe  (upload it in the dashboard: Install, Other options, Publish a new version)
