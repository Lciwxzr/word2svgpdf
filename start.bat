@echo off
setlocal EnableDelayedExpansion
rem ===========================================================================
rem  start.bat -- launch the vector-figure tool.
rem
rem  Expects setup.bat to have been run once. If the environment is missing,
rem  this script runs setup.bat automatically.
rem
rem  Design notes (same constraints as setup.bat; do not "simplify"):
rem   1. "%~dp0" already ends with a backslash -- use pushd "%~dp0" and
rem      relative paths, never "cd /d %~dp0\".
rem   2. Skip %LOCALAPPDATA%\Microsoft\WindowsApps\python.exe: a 0-byte
rem      App Execution Alias that crashes from a batch file (0xC0000409).
rem   3. The venv may live in env\venv or in %LOCALAPPDATA%\vectorfig\venv,
rem      because a OneDrive-synced folder can lock it.
rem ===========================================================================
pushd "%~dp0"

set "HERE=%CD%"

rem ---- portable build? use the bundled interpreter first -------------------
rem A portable release carries runtime\python\python.exe and needs no
rem system Python at all. It is preferred when present.
set "VPY=%HERE%\runtime\python\python.exe"
if exist "%VPY%" goto :check_ready

set "VPY=%HERE%\env\venv\Scripts\python.exe"
if not exist "%VPY%" set "VPY=%LOCALAPPDATA%\vectorfig\venv\Scripts\python.exe"

rem ---- environment missing? run setup first --------------------------------
if not exist "%VPY%" goto :need_setup

:check_ready
"%VPY%" -c "import pypdfium2,pikepdf,starlette,uvicorn,PIL,lxml" >nul 2>&1
if errorlevel 1 goto :need_setup

goto :find_inkscape

:need_setup
echo.
echo   Environment not found or incomplete. Running setup.bat ...
echo.
call "%HERE%\setup.bat"
if errorlevel 1 goto :end
set "VPY=%HERE%\env\venv\Scripts\python.exe"
if not exist "%VPY%" set "VPY=%LOCALAPPDATA%\vectorfig\venv\Scripts\python.exe"
if not exist "%VPY%" goto :end

rem ---- Inkscape -----------------------------------------------------------
:find_inkscape
set "INK="
if defined INKSCAPE if exist "%INKSCAPE%" set "INK=%INKSCAPE%"
if not defined INK if exist "%HERE%\env\inkscape\bin\inkscape.exe" set "INK=%HERE%\env\inkscape\bin\inkscape.exe"
if not defined INK if exist "%HERE%\env\inkscape\inkscape.exe" set "INK=%HERE%\env\inkscape\inkscape.exe"
if not defined INK if exist "C:\Program Files\Inkscape\bin\inkscape.exe" set "INK=C:\Program Files\Inkscape\bin\inkscape.exe"
if not defined INK if exist "C:\Program Files\Inkscape\inkscape.exe" set "INK=C:\Program Files\Inkscape\inkscape.exe"
if not defined INK if exist "C:\Program Files (x86)\Inkscape\bin\inkscape.exe" set "INK=C:\Program Files (x86)\Inkscape\bin\inkscape.exe"
if not defined INK if exist "%LOCALAPPDATA%\Programs\Inkscape\bin\inkscape.exe" set "INK=%LOCALAPPDATA%\Programs\Inkscape\bin\inkscape.exe"

if not defined INK (
    echo.
    echo   [!] Inkscape was not found.
    echo.
    echo       It converts SVG to vector PDF and is REQUIRED.
    echo       Download: https://inkscape.org/release/
    echo.
    echo       Or set the INKSCAPE environment variable to inkscape.exe.
    echo.
    pause
    goto :end
)
set "INKSCAPE=%INK%"

echo.
echo   ==========================================================
echo    Starting server ...
echo    Inkscape: %INK%
echo    Close this window to stop.
echo   ==========================================================
echo.

rem Run the launcher as a module so the package's relative imports resolve.
rem It starts the server, waits for the port it chose, and opens the browser on
rem that port. The port is not fixed because Windows reserves whole ranges
rem (8672-8771 on this machine), which makes a hard-coded port unreliable.
"%VPY%" -m w2sp.run

:end
popd
endlocal
