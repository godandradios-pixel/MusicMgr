@echo off
setlocal EnableExtensions EnableDelayedExpansion
title MusicMgr installer for Windows 11

rem ==========================================================================
rem  Install-MusicMgr.bat  -  run from the USB drive's MusicMgr folder
rem
rem  What it does, in order:
rem    1. Checks MusicMgr is not running on this PC.
rem    2. Asks where to install  (default D:\MusicMgr, or C:\MusicMgr if no D:).
rem    3. Copies MusicMgr.exe from the USB.
rem    4. Copies the library snapshot  (the newest  data*  folder on the USB
rem       that holds library.db)  to  <install>\data , plus the covers and
rem       artist pictures in  MusicMgr\artwork / \artists , so MusicMgr starts in
rem       portable mode with your whole library. An existing  data  folder is
rem       renamed to  data-before-install-<timestamp> , never overwritten.
rem    5. Optionally copies Music, MusicFLAC, Videos and Movies from the USB
rem       to D:\  - the paths the library expects. Only missing files are
rem       copied; files already on D: are never overwritten.
rem    6. Makes a desktop shortcut.
rem    7. Asks whether MusicMgr should start automatically when you sign in
rem       (normal window, maximized or full screen). Answering No removes an
rem       existing startup entry. Then offers to start MusicMgr.
rem
rem  Nothing on the USB is changed. Nothing is deleted on this PC.
rem ==========================================================================

set "SRC=%~dp0"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"
set "USB=%~d0"

echo.
echo  ============================================================
echo    MusicMgr installer  -  Windows 11
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

rem newest data* folder on the USB that holds a library.db
set "DATASRC="
for /f "delims=" %%D in ('dir /b /ad /o-d "%SRC%\data*" 2^>nul') do (
    if not defined DATASRC if exist "%SRC%\%%D\library.db" set "DATASRC=%SRC%\%%D"
)
if defined DATASRC (
    echo    Library snapshot: !DATASRC!
) else (
    echo    No library snapshot found on the USB - MusicMgr will start empty.
)
echo.

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
echo  [1/5] Copying MusicMgr.exe ...
if exist "%TARGET%\MusicMgr.exe" (
    rem  identical to the USB copy: nothing to do, and no backup needed
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

rem ---- 4. the library ------------------------------------------------------
echo  [2/5] Library data ...
if not defined DATASRC goto :nodata

if exist "%TARGET%\data\library.db" (
    echo.
    echo    There is already a library in %TARGET%\data
    choice /C KR /N /M "   [K]eep it, or [R]eplace it with the USB snapshot? "
    if errorlevel 2 (
        ren "%TARGET%\data" "data-before-install-%STAMP%" || (
            echo  ERROR: Could not rename the existing data folder. Is it open somewhere?
            goto :fail
        )
        echo    Old library kept as %TARGET%\data-before-install-%STAMP%
    ) else (
        echo    Keeping the existing library.
        goto :music
    )
)

echo    Copying the library, covers and artist pictures - a few minutes ...
rem  library.db + library.db-wal travel together; -shm is rebuilt on open.
rem  backups\ and the log belong to the old PC and are skipped.
robocopy "%DATASRC%" "%TARGET%\data" /E /R:1 /W:1 /NFL /NDL /NP /NJH ^
    /XF library.db-shm musicmgr.log /XD backups _gsdata_
if errorlevel 8 (
    echo  ERROR: Copying the library failed - see the messages above.
    goto :fail
)
rem  library copies saved by USB sync (2026-10-25) leave covers and artist
rem  pictures out - the drive keeps them in MusicMgr\artwork and \artists
for %%P in (artwork artists) do (
    if exist "%SRC%\%%P\" (
        echo    Copying %%P ...
        robocopy "%SRC%\%%P" "%TARGET%\data\%%P" /E /XC /XN /XO /R:1 /W:1 /NFL /NDL /NP /NJH /NJS
        if errorlevel 8 echo    WARNING: some %%P files could not be copied - see above.
    )
)
goto :music

:nodata
echo    Skipped - MusicMgr will create an empty library on first start.

rem ---- 5. music and videos -------------------------------------------------
:music
echo.
echo  [3/5] Music and videos
echo    The library expects them at D:\Music, D:\MusicFLAC, D:\Videos and D:\Movies.
if not exist "D:\" (
    echo    This PC has no D: drive. Tracks will show as missing until you copy the
    echo    music here and change the watched folders in MusicMgr's Settings.
    goto :shortcut
)
echo    Copying them from the USB can take hours for a full library.
echo    Only files missing from D: are copied; existing files are left alone.
choice /C YN /N /M "   Copy them now? [Y/N] "
if errorlevel 2 (
    echo    Skipped. MusicMgr's own USB sync can copy them later.
    goto :shortcut
)
for %%F in (Music MusicFLAC Videos Movies) do (
    if exist "%USB%\%%F\" (
        echo.
        echo    %USB%\%%F  -^>  D:\%%F
        rem  /XC /XN /XO: copy only files missing on D:, never touch existing ones
        robocopy "%USB%\%%F" "D:\%%F" /E /XC /XN /XO /R:1 /W:1 /MT:8 /NFL /NDL /NJH ^
            /XD _gsdata_ "$RECYCLE.BIN" "System Volume Information"
        if errorlevel 8 echo    WARNING: some files in %%F could not be copied - see above.
    ) else (
        echo    %USB%\%%F not found on the USB - skipped.
    )
)

rem ---- 6. desktop shortcut ----------------------------------------------
:shortcut
echo.
echo  [4/5] Desktop shortcut ...
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\MusicMgr.lnk');" ^
  "$s.TargetPath='%TARGET%\MusicMgr.exe'; $s.WorkingDirectory='%TARGET%'; $s.IconLocation='%TARGET%\MusicMgr.exe,0'; $s.Save()" >nul 2>&1
if errorlevel 1 (echo    Could not create the shortcut - start %TARGET%\MusicMgr.exe directly.) else (echo    Done.)

rem ---- 7. start at sign-in -------------------------------------------------
echo.
echo  [5/5] Start at sign-in
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
echo    MusicMgr is installed in %TARGET%
echo  ============================================================
echo.
echo    First start:
echo     - If Windows says "Windows protected your PC", click
echo       More info, then Run anyway. The exe is not code-signed.
echo     - The first start renames the cover pictures to the new
echo       naming. That takes a few seconds.
echo.
echo    Then, in MusicMgr:
echo     - Settings, Updates: tick "Check for updates when MusicMgr starts".
echo     - Settings, USB sync: plug in this USB and press "Check USB now".
echo       It brings plays, ratings, playlists and the Jukebox up to date.
echo       Do NOT press "Set up this drive..." - the USB is already set up.
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
