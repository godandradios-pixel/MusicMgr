@echo off
setlocal EnableExtensions EnableDelayedExpansion
title MusicMgr installer (blank library) for Windows 11

rem ==========================================================================
rem  Install-MusicMgr-Blank.bat  -  run from the USB drive's MusicMgr folder
rem
rem  Installs MusicMgr with an EMPTY library - no tracks, playlists, charts
rem  or play history from your other PCs. (Install-MusicMgr.bat next to this
rem  script is the one that copies your whole library.)
rem
rem  What it does, in order:
rem    1. Checks MusicMgr is not running on this PC.
rem    2. Asks where to install  (default D:\MusicMgr, or C:\MusicMgr if no D:).
rem    3. Copies MusicMgr.exe from the USB.
rem    4. Creates an empty  <install>\data  folder, so MusicMgr runs in
rem       portable mode and makes a brand-new library.db there on first start.
rem       An existing  data  folder is renamed to
rem       data-before-install-<timestamp> , never deleted.
rem    5. Makes a desktop shortcut.
rem    6. Asks whether MusicMgr should start automatically when you sign in
rem       (normal window, maximized or full screen). Then offers to start it.
rem
rem  Nothing on the USB is changed. Nothing is deleted on this PC.
rem ==========================================================================

set "SRC=%~dp0"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"
set "USB=%~d0"

echo.
echo  ============================================================
echo    MusicMgr installer  -  blank library  -  Windows 11
echo  ============================================================
echo    Installing from: %SRC%
echo.

rem ---- 1. sanity checks ----------------------------------------------------
if not exist "%SRC%\MusicMgr.exe" (
    echo  ERROR: MusicMgr.exe was not found next to this script.
    goto :fail
)

tasklist /FI "IMAGENAME eq MusicMgr.exe" 2>nul | find /I "MusicMgr.exe" >nul
if not errorlevel 1 (
    echo  ERROR: MusicMgr is running on this PC. Close it, then run this again.
    goto :fail
)

rem ---- 2. install folder ---------------------------------------------------
set "TARGET=C:\MusicMgr"
if exist "D:\" set "TARGET=D:\MusicMgr"
set "ANSWER="
set /p "ANSWER=  Install folder [%TARGET%]: "
if defined ANSWER set "TARGET=%ANSWER%"
set "ANSWER="
if "%TARGET:~-1%"=="\" set "TARGET=%TARGET:~0,-1%"

if /I "%TARGET:~0,2%"=="%USB%" (
    echo  ERROR: Install to this PC, not to the USB drive. MusicMgr never runs from the USB.
    goto :fail
)

if not exist "%TARGET%" mkdir "%TARGET%" || (
    echo  ERROR: Could not create %TARGET%
    goto :fail
)

for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%T"

rem ---- 3. the program ------------------------------------------------------
echo.
echo  [1/4] Copying MusicMgr.exe ...
if exist "%TARGET%\MusicMgr.exe" (
    fc /b "%SRC%\MusicMgr.exe" "%TARGET%\MusicMgr.exe" >nul 2>&1
    if not errorlevel 1 (
        echo    Already up to date - skipped.
        goto :exedone
    )
    copy /Y "%TARGET%\MusicMgr.exe" "%TARGET%\MusicMgr-before-install-%STAMP%.exe" >nul
)
copy /Y "%SRC%\MusicMgr.exe" "%TARGET%\MusicMgr.exe" >nul || (
    echo  ERROR: Could not copy MusicMgr.exe
    goto :fail
)
:exedone
powershell -NoProfile -Command "Unblock-File -LiteralPath '%TARGET%\MusicMgr.exe'" >nul 2>&1

rem ---- 4. empty library ----------------------------------------------------
echo  [2/4] Empty library ...
if exist "%TARGET%\data\" (
    dir /b /a "%TARGET%\data" 2>nul | findstr "^" >nul
    if not errorlevel 1 (
        echo.
        echo    %TARGET%\data already has files in it - possibly an existing library.
        choice /C SK /N /M "   [S]et it aside and start blank, or [K]eep it? "
        if errorlevel 2 (
            echo    Keeping the existing data folder - this is NOT a blank install.
            goto :shortcut
        )
        ren "%TARGET%\data" "data-before-install-%STAMP%" || (
            echo  ERROR: Could not rename the existing data folder. Is it open somewhere?
            goto :fail
        )
        echo    Old data kept as %TARGET%\data-before-install-%STAMP%
    )
)
if not exist "%TARGET%\data\" mkdir "%TARGET%\data" || (
    echo  ERROR: Could not create %TARGET%\data
    goto :fail
)
echo    Done. MusicMgr will create a new, empty library.db there on first start.

rem ---- 5. desktop shortcut -------------------------------------------------
:shortcut
echo.
echo  [3/4] Desktop shortcut ...
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\MusicMgr.lnk');" ^
  "$s.TargetPath='%TARGET%\MusicMgr.exe'; $s.WorkingDirectory='%TARGET%'; $s.IconLocation='%TARGET%\MusicMgr.exe,0'; $s.Save()" >nul 2>&1
if errorlevel 1 (echo    Could not create the shortcut - start %TARGET%\MusicMgr.exe directly.) else (echo    Done.)

rem ---- 6. start at sign-in -------------------------------------------------
echo.
echo  [4/4] Start at sign-in
set "STARTLNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\MusicMgr.lnk"
if exist "%STARTLNK%" echo    MusicMgr currently starts when you sign in.
choice /C YN /N /M "   Start MusicMgr automatically when you sign in to Windows? [Y/N] "
if errorlevel 2 goto :nostartup
echo    How should it open?
echo      1 = normal window    2 = maximized    3 = full screen
choice /C 123 /N /M "   Choose 1, 2 or 3: "
set "STARTARGS="
if errorlevel 3 (set "STARTARGS=--fullscreen") else if errorlevel 2 set "STARTARGS=--maximized"
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:STARTLNK);" ^
  "$s.TargetPath='%TARGET%\MusicMgr.exe'; $s.Arguments='%STARTARGS%'; $s.WorkingDirectory='%TARGET%'; $s.IconLocation='%TARGET%\MusicMgr.exe,0'; $s.Save()" >nul 2>&1
if errorlevel 1 (echo    Could not add the startup entry.) else (echo    Done. Turn it off later in Task Manager, Startup apps.)
goto :finish

:nostartup
if exist "%STARTLNK%" (
    del "%STARTLNK%" >nul 2>&1
    echo    Removed the existing startup entry.
) else (
    echo    MusicMgr will not start automatically.
)

:finish
echo.
echo  ============================================================
echo    MusicMgr is installed in %TARGET%  (blank library)
echo  ============================================================
echo.
echo    First start:
echo     - If Windows says "Windows protected your PC", click
echo       More info, then Run anyway. The exe is not code-signed.
echo.
echo    Then, in MusicMgr:
echo     - Settings: add your music folders and run a scan.
echo     - Settings, Updates: tick "Check for updates when MusicMgr starts".
echo     - Leave USB sync off if this PC should keep its own library -
echo       syncing brings in playlists, charts and plays from your other PCs.
echo.
choice /C YN /N /M "   Start MusicMgr now? [Y/N] "
if not errorlevel 2 start "" /D "%TARGET%" "%TARGET%\MusicMgr.exe"
echo.
pause
exit /b 0

:fail
echo.
echo  Installation stopped. Nothing on the USB was changed.
echo.
pause
exit /b 1
