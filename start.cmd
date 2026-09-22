@echo off
rem ===========================================================================
rem  Double-click this file to start the Stellaris Agent MCP server.
rem
rem  It is only a shell around start.py: it finds a usable Python 3 and hands
rem  over. start.py does the real work (environment checks, game/mod detection,
rem  indexing, serving, and printing client configuration to paste).
rem
rem  Optional environment overrides:
rem    STELLARIS_MCP_PYTHON  force one interpreter
rem    STELLARIS_MCP_PORT    HTTP port (default 8765)
rem ===========================================================================
setlocal EnableExtensions
title Stellaris Agent MCP
cd /d "%~dp0"

set "ENTRY=%~dp0start.py"
if not exist "%ENTRY%" (
  echo [ERROR] start.py was not found next to this launcher.
  echo         Keep the whole folder together after extracting the archive.
  goto :fail
)

set "PY="
if defined STELLARIS_MCP_PYTHON (
  set "PY=%STELLARIS_MCP_PYTHON%"
  goto :run
)
if exist "%~dp0python\python.exe" (
  set "PY=%~dp0python\python.exe"
  goto :run
)
for /f "delims=" %%P in ('where python.exe python3.exe py.exe 2^>nul') do (
  if not defined PY call :trypython "%%~fP"
)

:run
if not defined PY goto :nopython
"%PY%" "%ENTRY%" %*
set "CODE=%ERRORLEVEL%"
if "%CODE%"=="0" goto :eof
echo.
echo [ERROR] start.py exited with code %CODE%.
echo         See the output above, or start-error.log in this folder.
goto :fail

:trypython
if defined PY goto :eof
"%~1" --version >nul 2>nul
if errorlevel 1 goto :eof
set "PY=%~1"
goto :eof

:nopython
echo [ERROR] No Python 3 was found on this computer.
echo.
echo   Install Python 3.9 or newer from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" during setup, then run this file again.
echo   This tool uses the standard library only: no pip install is needed.
echo.
echo   Already installed but still not found? Point at it explicitly:
echo       set STELLARIS_MCP_PYTHON=C:\Path\to\python.exe
echo.

:fail
echo.
pause
endlocal
exit /b 1
