@echo off
setlocal EnableDelayedExpansion
rem ===========================================================================
rem  make_wheels.bat -- cache every dependency as a wheel into wheels\.
rem
rem  Run this once on a machine WITH a network connection, then ship the
rem  wheels\ folder alongside the tool. setup.bat prefers that folder and
rem  works completely offline.
rem
rem  Why this exists: the setup machine may have no internet, or a very slow
rem  one. Downloading the same packages every time is also wasteful.
rem ===========================================================================
pushd "%~dp0"

set "HERE=%CD%"
set "WHEELS=%HERE%\wheels"

echo.
echo   ==========================================================
echo    Cache dependencies as wheels
echo   ==========================================================
echo.

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
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"

if not defined PY goto :no_python

if not exist "%WHEELS%" mkdir "%WHEELS%"

echo   Python : %PY%
echo   Target : %WHEELS%
echo.
echo   Downloading all wheels, this can take a few minutes ...
echo.

"%PY%" -m pip download --dest "%WHEELS%" --disable-pip-version-check ^
    -r "%HERE%\requirements.txt"
if errorlevel 1 goto :fail

echo.
echo   ==========================================================
echo    Done. wheels\ now contains:
echo   ==========================================================
dir /b "%WHEELS%"
echo.
echo   Ship this folder with the tool. setup.bat will use it
echo   automatically and needs no network.
echo.
goto :end

:no_python
echo.
echo   [x] Python 3.10+ was not found. Install it, or set VECTORFIG_PYTHON.
echo.
pause
exit /b 1

:fail
echo.
echo   [x] Download failed. Check the network and try again.
echo.
pause
exit /b 1

:end
popd
endlocal
