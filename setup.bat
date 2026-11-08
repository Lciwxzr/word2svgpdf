@echo off
setlocal EnableDelayedExpansion
rem ===========================================================================
rem  setup.bat -- one-shot environment builder for the vector-figure tool.
rem
rem  Creates env\venv with the pinned Python dependencies and registers an
rem  Inkscape installation into env\inkscape (or reuses one already installed).
rem  start.bat then only has to run the app.
rem
rem  Design notes (learned the hard way; do not "simplify" these away)
rem  ----------------------------------------------------------------
rem  1. "%~dp0" already ends with a backslash. Never append another one, and
rem     never use "cd /d %~dp0\" (the trailing \" escapes the closing quote).
rem     Use pushd "%~dp0" plus relative paths.
rem  2. Skip %LOCALAPPDATA%\Microsoft\WindowsApps\python.exe. That is a 0-byte
rem     App Execution Alias; running it from a batch file crashes with exit
rem     code -1073740791 (0xC0000409). winget.exe in that folder is a stub too.
rem  3. Do NOT put the venv in a OneDrive-synced folder unless it works. The
rem     sync client can lock python.exe and venv creation then fails with
rem     "Permission denied". env\venv is tried first, %LOCALAPPDATA%\vectorfig
rem     is the fallback.
rem  4. requirements.txt must stay ASCII-only. pip reads it with the system
rem     locale codec (cp936 on Chinese Windows) and fails on non-ASCII bytes.
rem  5. Inkscape's download page refuses automated requests (HTTP error on
rem     every attempt, and the page is JS-rendered so it exposes no direct
rem     link). It therefore cannot be downloaded unattended. This script
rem     detects an existing installation and otherwise explains what to do.
rem ===========================================================================
pushd "%~dp0"

echo.
echo   ==========================================================
echo    Environment setup
echo   ==========================================================
echo.

set "HERE=%CD%"
set "ENVDIR=%HERE%\env"
set "VENV=%ENVDIR%\venv"
set "VPY=%VENV%\Scripts\python.exe"

rem ----------------------------------------------------------------------
rem 1) locate a usable base interpreter
rem ----------------------------------------------------------------------
set "PY="
if defined VECTORFIG_PYTHON if exist "%VECTORFIG_PYTHON%" set "PY=%VECTORFIG_PYTHON%"

if not defined PY for %%p in (python.exe) do (
    set "CAND=%%~$PATH:p"
    if defined CAND (
        echo !CAND! | findstr /i /c:"WindowsApps" >nul || set "PY=!CAND!"
    )
)

if not defined PY if exist "%USERPROFILE%\miniconda3\python.exe" set "PY=%USERPROFILE%\miniconda3\python.exe"
if not defined PY if exist "%USERPROFILE%\anaconda3\python.exe" set "PY=%USERPROFILE%\anaconda3\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PY if exist "C:\ProgramData\miniconda3\python.exe" set "PY=C:\ProgramData\miniconda3\python.exe"
if not defined PY if exist "C:\ProgramData\anaconda3\python.exe" set "PY=C:\ProgramData\anaconda3\python.exe"

if not defined PY goto :no_python

"%PY%" -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 goto :old_python

echo   [1/4] Base Python : %PY%
"%PY%" --version

rem ----------------------------------------------------------------------
rem 2) create the virtual environment
rem ----------------------------------------------------------------------
rem A venv is only usable when pyvenv.cfg exists. OneDrive (or an antivirus)
rem can leave a half-created venv behind -- just Scripts\python.exe and no
rem pyvenv.cfg, which fails later with "No pyvenv.cfg file". So the check is
rem for the config file, not for the executable.
if exist "%ENVDIR%\venv\pyvenv.cfg" goto :venv_ready

echo   [2/4] Creating virtual environment at env\venv ...
if not exist "%ENVDIR%" mkdir "%ENVDIR%"
"%PY%" -m venv "%VENV%"
if exist "%VENV%\pyvenv.cfg" goto :venv_ready
goto :venv_retry

:venv_ready
echo         env\venv ready
goto :install

:venv_retry
rem OneDrive (or an antivirus) may hold a lock on the synced folder.
echo         Could not create a usable venv in env\, trying %LOCALAPPDATA% ...
set "VENV=%LOCALAPPDATA%\vectorfig\venv"
set "VPY=%VENV%\Scripts\python.exe"
if exist "%VENV%\pyvenv.cfg" goto :venv_ready2
if not exist "%LOCALAPPDATA%\vectorfig" mkdir "%LOCALAPPDATA%\vectorfig"
"%PY%" -m venv "%VENV%"
if not exist "%VENV%\pyvenv.cfg" goto :venv_fail

:venv_ready2
echo         fallback venv ready: %VENV%

rem ----------------------------------------------------------------------
rem 3) install the pinned dependencies
rem ----------------------------------------------------------------------
:install
echo   [3/4] Installing Python dependencies ...

"%VPY%" -c "import pypdfium2,pikepdf,starlette,uvicorn,PIL,lxml,scipy" >nul 2>&1
if not errorlevel 1 (
    echo         Already installed, nothing to do.
    goto :inkscape
)

"%VPY%" -m pip install --upgrade pip --quiet --disable-pip-version-check 2>nul

rem Prefer a local wheel cache when one is present: it works without a network
rem and is much faster. But an INCOMPLETE cache must not be fatal -- a partial
rem wheels\ folder (say, only the two PDF libraries) would otherwise fail
rem outright because --no-index forbids any download. So the offline attempt
rem is tried first and, if it fails, the online install runs as a fallback.
if exist "%HERE%\wheels" (
    echo         Trying local wheel cache in wheels\ ...
    "%VPY%" -m pip install --no-index --find-links "%HERE%\wheels" ^
        --disable-pip-version-check -r "%HERE%\requirements.txt" 2>nul
    if errorlevel 1 (
        echo         Wheel cache incomplete, falling back to PyPI ...
        "%VPY%" -m pip install --disable-pip-version-check ^
            -r "%HERE%\requirements.txt"
    )
) else (
    echo         Downloading from PyPI, this needs a network connection ...
    "%VPY%" -m pip install --disable-pip-version-check ^
        -r "%HERE%\requirements.txt"
)
if errorlevel 1 goto :pip_fail

"%VPY%" -c "import pypdfium2,pikepdf,starlette,uvicorn,PIL,lxml,scipy" >nul 2>&1
if errorlevel 1 goto :pip_fail
echo         Dependencies installed.

rem ----------------------------------------------------------------------
rem 4) locate Inkscape
rem ----------------------------------------------------------------------
:inkscape
set "INK="
if defined INKSCAPE if exist "%INKSCAPE%" set "INK=%INKSCAPE%"
if not defined INK if exist "%HERE%\env\inkscape\bin\inkscape.exe" set "INK=%HERE%\env\inkscape\bin\inkscape.exe"
if not defined INK if exist "%HERE%\env\inkscape\inkscape.exe" set "INK=%HERE%\env\inkscape\inkscape.exe"
if not defined INK if exist "C:\Program Files\Inkscape\bin\inkscape.exe" set "INK=C:\Program Files\Inkscape\bin\inkscape.exe"
if not defined INK if exist "C:\Program Files\Inkscape\inkscape.exe" set "INK=C:\Program Files\Inkscape\inkscape.exe"
if not defined INK if exist "C:\Program Files (x86)\Inkscape\bin\inkscape.exe" set "INK=C:\Program Files (x86)\Inkscape\bin\inkscape.exe"
if not defined INK if exist "%LOCALAPPDATA%\Programs\Inkscape\bin\inkscape.exe" set "INK=%LOCALAPPDATA%\Programs\Inkscape\bin\inkscape.exe"

if not defined INK goto :no_inkscape

echo   [4/4] Inkscape : %INK%
"%INK%" --version 2>nul
echo.
echo   ==========================================================
echo    Setup complete. Run start.bat to launch the tool.
echo   ==========================================================
echo.
goto :end

rem ----------------------------------------------------------------------
rem error paths
rem ----------------------------------------------------------------------
:no_python
echo.
echo   [x] Python 3.10+ was not found.
echo.
echo       Install from https://www.python.org/downloads/
echo       Tick "Add Python to PATH" during setup, then run this again.
echo.
echo       Or set VECTORFIG_PYTHON to the full path of python.exe.
echo.
pause
exit /b 1

:old_python
echo.
echo   [x] Python found at %PY% is too old; 3.10+ is required.
"%PY%" --version
pause
exit /b 1

:venv_fail
echo.
echo   [x] Could not create a virtual environment.
echo       Tried: %HERE%\env\venv and %LOCALAPPDATA%\vectorfig\venv
echo.
pause
exit /b 1

:pip_fail
echo.
echo   [x] Dependency installation failed.
echo.
echo       If the network is unavailable, put the wheel files into
echo       the "wheels" folder next to this script and run again.
echo.
echo       Manual command:
echo         "%VPY%" -m pip install -r "%HERE%\requirements.txt"
echo.
pause
exit /b 1

:no_inkscape
echo.
echo   [!] Python environment is ready, but Inkscape was not found.
echo.
echo       Inkscape converts SVG to vector PDF and is REQUIRED by this tool.
echo.
echo       Download and install it from:
echo         https://inkscape.org/release/
echo.
echo       The installer can use any location. After installing, run
echo       setup.bat again -- it will find Inkscape automatically.
echo.
echo       To use a portable copy instead, unpack it into
echo         %HERE%\env\inkscape
echo       so that env\inkscape\bin\inkscape.exe exists.
echo.
echo       Or set the INKSCAPE environment variable to the full path of
echo       inkscape.exe.
echo.
pause
exit /b 1

:end
popd
endlocal
