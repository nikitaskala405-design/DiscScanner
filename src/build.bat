@echo off
rem Build DiskScanner-<arch>.exe into ..\app  (requires: pip install pyinstaller pillow)
rem Usage: build.bat [path\to\python.exe]
rem The architecture (x64 / x86 / arm64) is taken from the Python used for the build.
setlocal
cd /d "%~dp0"
set "PY=%~1"
if "%PY%"=="" set "PY=python"

for /f "usebackq" %%a in (`call "%PY%" -c "import sysconfig as s; p=s.get_platform(); print({'win-amd64':'x64','win32':'x86','win-arm64':'arm64'}.get(p,p))"`) do set "ARCH=%%a"
if "%ARCH%"=="" (
  echo Python not found: %PY%
  exit /b 1
)
echo Building for %ARCH% ...

if not exist icon.ico "%PY%" make_icon.py
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name DiskScanner-%ARCH% ^
  --icon "%~dp0icon.ico" --add-data "%~dp0icon.ico;." ^
  --distpath "%~dp0..\app" --workpath "%~dp0build" --specpath "%~dp0build" ^
  "%~dp0disk_scanner.py"
set "RC=%ERRORLEVEL%"
rem Remove temporary build files (they contain local paths of the build machine)
if exist "%~dp0build" rd /s /q "%~dp0build"
if not "%RC%"=="0" exit /b %RC%
echo.
echo Done: app\DiskScanner-%ARCH%.exe
