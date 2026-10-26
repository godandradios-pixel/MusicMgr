@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Copy USB (E:) to this PC (D:)

rem ==========================================================================
rem  Copy-USB-to-D.bat  -  run on the PC you want to fill from the USB drive
rem
rem  What it does, in order:
rem    1. Checks MusicMgr is not running on this PC.
rem    2. Library: replaces D:\MusicMgr\data with the newest library snapshot
rem       on the USB (E:\MusicMgr\data* with a library.db). The current data
rem       folder is RENAMED to data-before-usb-copy-<time>, never deleted.
rem       The snapshot's own sync\ folder comes with it, so USB sync sees a
rem       library and sync history that belong together (a new library with
rem       the old PC's sync history would look like deletions and be pushed
rem       to the drive). Video thumbnails, settings and other files the
rem       snapshot doesn't have are brought over from the old data folder.
rem    3. Covers and artist pictures from E:\MusicMgr\artwork and \artists.
rem    4. Music, MusicFLAC, Videos, Movies and Radio from E:\ to D:\.
rem       Adds new files and updates changed ones; a file that is newer on
rem       D: is left alone. NOTHING on D: is ever deleted.
rem
rem  Nothing on the USB is changed except the log written next to this script.
rem ==========================================================================

set "USB=E:"
set "DEST=D:"
set "APP=%DEST%\MusicMgr"

for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%T"
set "LOG=%~dp0Copy-USB-to-D-%COMPUTERNAME%-%STAMP%.log"

echo.
echo  ============================================================
echo    Copy USB (%USB%) to this PC (%DEST%)  -  %COMPUTERNAME%
echo  ============================================================
echo.

rem ---- 1. sanity checks ----------------------------------------------------
if not exist "%USB%\MusicMgr\" (
    echo  ERROR: %USB%\MusicMgr was not found. Is the USB drive plugged in as %USB%?
    goto :fail
)
if not exist "%DEST%\" (
    echo  ERROR: This PC has no %DEST% drive.
    goto :fail
)
tasklist /FI "IMAGENAME eq MusicMgr.exe" 2>nul | find /I "MusicMgr.exe" >nul
if not errorlevel 1 (
    echo  ERROR: MusicMgr is running on this PC. Close it, then run this again.
    goto :fail
)

rem common robocopy options: add + update only (/XO = never replace a newer
rem file on D: with an older one), FAT/exFAT-safe timestamps, short retries
set "RC=/E /XO /FFT /R:1 /W:2 /MT:16 /NP /NDL /XJ /LOG+:"%LOG%" /TEE"
set "RCX=/XD $RECYCLE.BIN "System Volume Information" _gsdata_ /XF Thumbs.db desktop.ini"

rem ---- 2. the library ------------------------------------------------------
echo  [1/3] Library (library.db)
set "DATASRC="
for /f "delims=" %%D in ('dir /b /ad /o-d "%USB%\MusicMgr\data*" 2^>nul') do (
    if not defined DATASRC if exist "%USB%\MusicMgr\%%D\library.db" set "DATASRC=%USB%\MusicMgr\%%D"
)
if not defined DATASRC (
    echo    No library snapshot on the USB - library left as it is.
    goto :pictures
)
for %%F in ("%DATASRC%\library.db") do set "SNAPDATE=%%~tF"
echo    USB snapshot : %DATASRC%\library.db
echo                   saved !SNAPDATE!
if exist "%APP%\data\library.db" (
    for %%F in ("%APP%\data\library.db") do set "OLDDATE=%%~tF"
    echo    This PC      : %APP%\data\library.db
    echo                   saved !OLDDATE!
    echo.
    echo    Replacing it gives this PC the library as it was when the snapshot
    echo    was saved. Anything changed on this PC since then is only kept in
    echo    the backup folder. Run USB sync in MusicMgr afterwards to catch up
    echo    with changes the other PCs have put on the drive.
)
echo.
choice /C YN /N /M "   Replace this PC's library with the USB snapshot? [Y/N] "
if errorlevel 2 (
    echo    Library left as it is.
    goto :pictures
)

if exist "%APP%\data\" (
    ren "%APP%\data" "data-before-usb-copy-%STAMP%" || (
        echo  ERROR: Could not rename %APP%\data. Is a file in it open somewhere?
        goto :fail
    )
    echo    Old data folder kept as %APP%\data-before-usb-copy-%STAMP%
)
set "OLDDATA=%APP%\data-before-usb-copy-%STAMP%"

echo    Copying the snapshot ...
rem  library.db and library.db-wal travel together; -shm is rebuilt on open
robocopy "%DATASRC%" "%APP%\data" /E /FFT /R:1 /W:2 /NP /NDL /NFL /LOG+:"%LOG%" /TEE ^
    /XF library.db-shm musicmgr.log /XD backups _gsdata_
if errorlevel 8 (
    echo  ERROR: Copying the library failed - see %LOG%
    echo  Your old library is untouched in %OLDDATA% - rename it back to data.
    goto :fail
)

if exist "%OLDDATA%\" (
    echo    Bringing over thumbnails, settings and pictures the snapshot lacks ...
    rem  /XC /XN /XO: only files the new data folder doesn't have at all.
    rem  Never the old library.db / -wal / -shm or the old sync history.
    robocopy "%OLDDATA%" "%APP%\data" /E /XC /XN /XO /FFT /R:1 /W:2 /NP /NDL /NFL ^
        /LOG+:"%LOG%" /TEE /XF library.db library.db-wal library.db-shm musicmgr.log ^
        /XD sync backups _to_delete _gsdata_
    if errorlevel 8 echo    WARNING: some files could not be copied - see %LOG%
)

rem ---- 3. covers and artist pictures --------------------------------------
:pictures
echo.
echo  [2/3] Covers and artist pictures
for %%P in (artwork artists) do (
    if exist "%USB%\MusicMgr\%%P\" (
        robocopy "%USB%\MusicMgr\%%P" "%APP%\data\%%P" %RC% /NFL
        if errorlevel 8 echo    WARNING: some %%P files could not be copied - see the log.
    )
)

rem ---- 4. music and videos -------------------------------------------------
echo.
echo  [3/3] Music and videos  (adds and updates only - nothing on %DEST% is deleted)
for %%F in (Music MusicFLAC Videos Movies Radio) do (
    if exist "%USB%\%%F\" (
        echo.
        echo    %USB%\%%F  -^>  %DEST%\%%F
        robocopy "%USB%\%%F" "%DEST%\%%F" %RC% %RCX%
        if errorlevel 8 echo    WARNING: some files in %%F could not be copied - see the log.
    ) else (
        echo    %USB%\%%F not found on the USB - skipped.
    )
)

echo.
echo  ============================================================
echo    Done. Log: %LOG%
echo    Start MusicMgr, then run USB sync in Settings to pick up
echo    anything newer on the drive.
echo  ============================================================
echo.
pause
exit /b 0

:fail
echo.
echo  Nothing more was done.
echo.
pause
exit /b 1
