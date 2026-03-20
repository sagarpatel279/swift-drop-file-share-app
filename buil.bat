@echo off
:: ================================================================
::  build.bat  —  Build SwiftDrop into a Windows .exe  (Nuitka)
::
::  OUTPUT:
::    SwiftDrop\
::      SwiftDrop.exe      <- double-click to run
::      received\          <- received files appear here
::
::  Copy the entire SwiftDrop\ folder to both machines via USB.
:: ================================================================

echo.
echo  SwiftDrop Build Script
echo  ========================
echo.

:: Check Python
py --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found.
    echo          Download from https://www.python.org/downloads/
    pause
    exit /b 1
)
echo  Found:
py --version
echo.

:: Install Nuitka
echo  [1/4] Installing Nuitka...
pip install --upgrade nuitka zstandard ordered-set --quiet
if errorlevel 1 (
    echo  [ERROR] pip install failed. Check your internet connection.
    pause
    exit /b 1
)
echo  Done.
echo.

:: Compile
echo  [2/4] Compiling peer.py with Nuitka...
echo         First run takes 2-5 minutes (C compilation). Please wait.
echo.

if exist dist rmdir /s /q dist

py -m nuitka ^
    --onefile ^
    --windows-console-mode=disable ^
    --output-filename=SwiftDrop.exe ^
    --output-dir=dist ^
    --assume-yes-for-downloads ^
    --no-progressbar ^
    peer.py

if errorlevel 1 (
    echo.
    echo  [ERROR] Compilation failed. See errors above.
    pause
    exit /b 1
)

echo.
echo  [3/4] Packaging into SwiftDrop\ folder...

:: Clean and create delivery folder
if exist SwiftDrop rmdir /s /q SwiftDrop
mkdir SwiftDrop
mkdir SwiftDrop\received

:: Move exe into delivery folder
move dist\SwiftDrop.exe SwiftDrop\SwiftDrop.exe >nul
if errorlevel 1 (
    echo  [ERROR] Could not move SwiftDrop.exe
    pause
    exit /b 1
)

echo.
echo  ================================================================
echo   BUILD COMPLETE
echo  ================================================================
echo.
echo   Output folder:  %CD%\SwiftDrop\
echo.
echo     SwiftDrop\SwiftDrop.exe    double-click to launch
echo     SwiftDrop\received\        files received will appear here
echo.
echo   IMPORTANT:
echo   Copy the entire SwiftDrop folder (not just the .exe) to both
echo   PCs. Always run SwiftDrop.exe from inside the SwiftDrop folder.
echo   If you move the .exe out of the folder, received files may
echo   appear in the wrong location.
echo.
echo  ================================================================
echo.

:: Verify
echo  [4/4] Verifying output...
if exist SwiftDrop\SwiftDrop.exe (
    for %%I in (SwiftDrop\SwiftDrop.exe) do echo   File size : %%~zI bytes
    echo   Status    : READY TO DISTRIBUTE
) else (
    echo   [ERROR] SwiftDrop.exe not found.
)

echo.
pause