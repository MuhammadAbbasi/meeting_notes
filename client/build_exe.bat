@echo off
REM Builds dist\OfficeMeetingAgent.exe (one file, no console). Needs Python 3.11 or 3.12 installed.
cd /d %~dp0
set PY=py -3.12
%PY% --version >nul 2>&1 || set PY=py -3.11
%PY% --version >nul 2>&1 || (echo Python 3.11/3.12 not found & exit /b 1)

%PY% -m pip install --upgrade pip pyinstaller -r requirements.txt pyaudiowpatch || exit /b 1
%PY% -m PyInstaller --noconfirm --onefile --windowed --name OfficeMeetingAgent ^
  --add-data "config.example.json;." ^
  --hidden-import pystray._win32 --collect-all sounddevice ^
  office_agent.py || exit /b 1

echo.
echo Built dist\OfficeMeetingAgent.exe  (copy it to colleagues; first start opens the setup form)
