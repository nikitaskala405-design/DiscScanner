@echo off
rem Build DiskScanner.exe into ..\app  (requires: pip install pyinstaller pillow)
cd /d "%~dp0"
if not exist icon.ico python make_icon.py
python -m PyInstaller --noconfirm --clean --onefile --windowed --name DiskScanner ^
  --icon "%~dp0icon.ico" --add-data "%~dp0icon.ico;." ^
  --distpath "%~dp0..\app" --workpath "%~dp0build" --specpath "%~dp0build" ^
  "%~dp0disk_scanner.py"
rem Remove temporary build files (they contain local paths of the build machine)
if exist "%~dp0build" rd /s /q "%~dp0build"
echo.
echo Done: app\DiskScanner.exe
