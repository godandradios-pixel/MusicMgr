<#
.SYNOPSIS
    Pulls the latest MusicMgr source and rebuilds the single-file Windows .exe.

.DESCRIPTION
    Run this from the repo (or anywhere - it cd's to its own folder first,
    so it works from a desktop shortcut or Task Scheduler too).

    Steps:
      1. git pull            (stops on conflicts/merge errors - resolve
                               those yourself, then re-run)
      2. asks which version this build is (Enter keeps the current
         __version__ in musicmgr\__init__.py; type e.g. 1.6.1 to change it).
         Skipped with -Version, -NoPause, or under GitHub Actions.
      3. pip install -r requirements-dev.txt   (keeps PyInstaller etc. current;
                               skip with -SkipInstall once your env is set up)
      4. pyinstaller MusicMgr.spec   ->   dist\MusicMgr.exe   (single file)
      5. copy dist\MusicMgr.exe to the USB drive (E:\MusicMgr by default),
         so Install-MusicMgr.bat on the drive always installs the newest
         build. Skipped with a warning if the drive isn't plugged in;
         skip on purpose with -SkipUsb.

.PARAMETER SkipPull
    Skip the git pull step and build from whatever is currently checked out.

.PARAMETER SkipInstall
    Skip the pip install step (assumes requirements are already installed).

.PARAMETER Clean
    Delete the build\ and dist\ folders first, forcing a full rebuild
    instead of PyInstaller's incremental cache.

.PARAMETER Version
    Set __version__ to this (X.Y.Z) without asking.

.PARAMETER UsbPath
    Folder on the USB drive that receives the new MusicMgr.exe.
    Default: E:\MusicMgr.

.PARAMETER SkipUsb
    Don't copy the new build to the USB drive.

.PARAMETER NoPause
    Don't wait for a keypress at the end (useful for Task Scheduler /
    automation; interactive double-click runs pause by default so the
    window doesn't vanish before you can read the result).

.EXAMPLE
    .\build.ps1
.EXAMPLE
    .\build.ps1 -SkipInstall
.EXAMPLE
    .\build.ps1 -Clean
.EXAMPLE
    .\build.ps1 -UsbPath F:\MusicMgr
.EXAMPLE
    .\build.ps1 -Version 1.6.1
#>

[CmdletBinding()]
param(
    [switch]$SkipPull,
    [switch]$SkipInstall,
    [switch]$Clean,
    [string]$Version,
    [string]$UsbPath = "E:\MusicMgr",
    [switch]$SkipUsb,
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot
Set-Location $RepoRoot

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Invoke-Checked($exe, $exeArgs, $failMessage) {
    & $exe @exeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "$failMessage (exit code $LASTEXITCODE)"
    }
}

$exitCode = 0

try {
    Write-Step "Repo: $RepoRoot"

    if (-not $SkipPull) {
        $dirty = git status --porcelain
        if ($dirty) {
            Write-Host "Local changes detected (git pull will stop on any conflict):" -ForegroundColor Yellow
            Write-Host $dirty
        }

        Write-Step "git pull"
        Invoke-Checked "git" @("pull") "git pull failed - resolve the conflict/error above, then re-run"
    }
    else {
        Write-Step "Skipping git pull (-SkipPull)"
    }

    # Version for this build. musicmgr\__init__.py's __version__ is the
    # single source of truth (the updater compares release tags to it and
    # release.yml refuses a tag that doesn't match), so this edits that
    # line. Never prompts under CI or with -NoPause - release.yml runs
    # this script with -SkipPull -NoPause and must build the committed
    # version as-is.
    $initPath = Join-Path $RepoRoot "musicmgr\__init__.py"
    $initText = [System.IO.File]::ReadAllText($initPath)
    $versionPattern = '(?m)^__version__\s*=\s*"([^"]+)"'
    $match = [regex]::Match($initText, $versionPattern)
    if (-not $match.Success) {
        throw "Couldn't find __version__ in musicmgr\__init__.py"
    }
    $currentVersion = $match.Groups[1].Value
    $newVersion = $currentVersion
    $interactive = -not ($NoPause -or $env:GITHUB_ACTIONS -or $env:CI)

    if ($Version) {
        $newVersion = $Version.Trim().TrimStart("v")
    }
    elseif ($interactive) {
        $suggest = $currentVersion
        if ($currentVersion -match '^(\d+)\.(\d+)\.(\d+)$') {
            $suggest = "$($Matches[1]).$($Matches[2]).$([int]$Matches[3] + 1)"
        }
        Write-Step "Version"
        Write-Host "Current version: $currentVersion"
        while ($true) {
            $answer = Read-Host "Version for this build (Enter keeps $currentVersion, or type e.g. $suggest)"
            $answer = $answer.Trim().TrimStart("v")
            if (-not $answer) { break }
            if ($answer -match '^\d+\.\d+\.\d+$') { $newVersion = $answer; break }
            Write-Host "Use the form X.Y.Z, e.g. $suggest" -ForegroundColor Yellow
        }
    }

    if ($newVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw "Version '$newVersion' isn't in the form X.Y.Z"
    }
    if ($newVersion -ne $currentVersion) {
        if ([version]$newVersion -lt [version]$currentVersion) {
            Write-Host "Note: $newVersion is lower than the current $currentVersion" -ForegroundColor Yellow
        }
        $updated = [regex]::Replace($initText, $versionPattern, "__version__ = `"$newVersion`"", 1)
        [System.IO.File]::WriteAllText($initPath, $updated, (New-Object System.Text.UTF8Encoding($false)))
        Write-Host "__version__ set to $newVersion in musicmgr\__init__.py" -ForegroundColor Green
    }
    else {
        Write-Host "Building version $currentVersion"
    }

    if (-not $SkipInstall) {
        Write-Step "pip install -r requirements-dev.txt"
        Invoke-Checked "python" @("-m", "pip", "install", "-r", "requirements-dev.txt") "pip install failed"
    }
    else {
        Write-Step "Skipping dependency install (-SkipInstall)"
    }

    if ($Clean) {
        Write-Step "Removing build\ and dist\ (forcing a full rebuild)"
        foreach ($dir in @("build", "dist")) {
            $path = Join-Path $RepoRoot $dir
            if (Test-Path $path) {
                Remove-Item $path -Recurse -Force
            }
        }
    }

    # Bakes the commit this build is packaging into
    # musicmgr\assets\VERSION, which version.py falls back to reading once
    # frozen (a packaged .exe has no .git folder of its own to ask - see
    # that module's docstring). Not committed to git (see .gitignore) -
    # every build.ps1 run regenerates it fresh from whatever's currently
    # checked out, so it can never go stale the way a hand-edited version
    # number would. Written as UTF-8 *without* a BOM, matching
    # version.py's plain `read_text(encoding="utf-8")` - Out-File's
    # default utf8 encoding adds a BOM that would show up as a stray
    # character at the start of the string.
    Write-Step "Writing version file"
    # Plain ASCII "-" separator (previously a middle-dot character) so
    # the version string never depends on codepage/locale decoding lining
    # up between git, PowerShell, and the Qt UI that ends up displaying it.
    $versionText = (git -C $RepoRoot log -1 --format="%h - %cd" --date=short 2>$null)
    if (-not $versionText) {
        Write-Host "Could not read git commit info - VERSION will read 'unknown'" -ForegroundColor Yellow
        $versionText = "unknown"
    }
    $versionPath = Join-Path $RepoRoot "musicmgr\assets\VERSION"
    [System.IO.File]::WriteAllText($versionPath, $versionText, (New-Object System.Text.UTF8Encoding($false)))

    Write-Step "pyinstaller MusicMgr.spec"
    Invoke-Checked "python" @("-m", "PyInstaller", "MusicMgr.spec") "PyInstaller build failed"

    $exePath = Join-Path $RepoRoot "dist\MusicMgr.exe"
    if (-not (Test-Path $exePath)) {
        throw "Build reported success but dist\MusicMgr.exe was not found"
    }

    $size = [math]::Round((Get-Item $exePath).Length / 1MB, 1)
    Write-Step "Build complete: $exePath ($size MB) - version $newVersion"

    # Step 4: refresh the USB copy. The drive only carries the exe for
    # Install-MusicMgr.bat (MusicMgr never runs from the USB), so this is
    # a plain file copy. Copied to a temp name first and then swapped in,
    # so a pulled drive or full disk mid-copy can't leave a half-written
    # MusicMgr.exe behind; the previous one is kept as MusicMgr.exe.old
    # until the next build.
    if ($SkipUsb) {
        Write-Step "Skipping USB copy (-SkipUsb)"
    }
    elseif (-not (Test-Path $UsbPath -PathType Container)) {
        Write-Host ""
        Write-Host "USB folder $UsbPath not found - drive not plugged in? Skipping USB copy." -ForegroundColor Yellow
    }
    else {
        Write-Step "Copying MusicMgr.exe to $UsbPath"
        $usbExe = Join-Path $UsbPath "MusicMgr.exe"
        $usbTmp = "$usbExe.new"
        $usbOld = "$usbExe.old"
        try {
            Copy-Item $exePath $usbTmp -Force
            if ((Get-Item $usbTmp).Length -ne (Get-Item $exePath).Length) {
                throw "size mismatch after copy"
            }
            if (Test-Path $usbExe) {
                if (Test-Path $usbOld) { Remove-Item $usbOld -Force }
                Rename-Item $usbExe (Split-Path $usbOld -Leaf)
            }
            Rename-Item $usbTmp (Split-Path $usbExe -Leaf)
            Write-Host "USB updated: $usbExe (previous kept as MusicMgr.exe.old)" -ForegroundColor Green
        }
        catch {
            if (Test-Path $usbTmp) { Remove-Item $usbTmp -Force -ErrorAction SilentlyContinue }
            # the build itself succeeded - report the USB problem but don't fail
            Write-Host "USB copy failed: $_  (dist\MusicMgr.exe is still good)" -ForegroundColor Yellow
        }
    }

    if ($newVersion -ne $currentVersion) {
        # the version bump is only on disk - it becomes a release once it's
        # committed and tagged (release.yml builds from the tag)
        Write-Host ""
        Write-Host "Version changed $currentVersion -> $newVersion. To release it:" -ForegroundColor Cyan
        Write-Host "  git add musicmgr/__init__.py"
        Write-Host "  git commit -m `"Bump version to $newVersion`""
        Write-Host "  git push"
        Write-Host "  git tag v$newVersion"
        Write-Host "  git push origin v$newVersion"
        Write-Host "  (then, once CI's draft is up: venv\Scripts\python tools\sign_release.py v$newVersion)"
    }
}
catch {
    Write-Host ""
    Write-Host "BUILD FAILED: $_" -ForegroundColor Red
    $exitCode = 1
}

if (-not $NoPause) {
    Write-Host ""
    Read-Host "Press Enter to close"
}

exit $exitCode
