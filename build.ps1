<#
.SYNOPSIS
    Pulls the latest MusicMgr source and rebuilds the single-file Windows .exe.

.DESCRIPTION
    Run this from the repo (or anywhere - it cd's to its own folder first,
    so it works from a desktop shortcut or Task Scheduler too).

    Steps:
      1. git pull            (stops on conflicts/merge errors - resolve
                               those yourself, then re-run)
      2. pip install -r requirements-dev.txt   (keeps PyInstaller etc. current;
                               skip with -SkipInstall once your env is set up)
      3. pyinstaller MusicMgr.spec   ->   dist\MusicMgr.exe   (single file)

.PARAMETER SkipPull
    Skip the git pull step and build from whatever is currently checked out.

.PARAMETER SkipInstall
    Skip the pip install step (assumes requirements are already installed).

.PARAMETER Clean
    Delete the build\ and dist\ folders first, forcing a full rebuild
    instead of PyInstaller's incremental cache.

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
#>

[CmdletBinding()]
param(
    [switch]$SkipPull,
    [switch]$SkipInstall,
    [switch]$Clean,
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

    Write-Step "pyinstaller MusicMgr.spec"
    Invoke-Checked "python" @("-m", "PyInstaller", "MusicMgr.spec") "PyInstaller build failed"

    $exePath = Join-Path $RepoRoot "dist\MusicMgr.exe"
    if (-not (Test-Path $exePath)) {
        throw "Build reported success but dist\MusicMgr.exe was not found"
    }

    $size = [math]::Round((Get-Item $exePath).Length / 1MB, 1)
    Write-Step "Build complete: $exePath ($size MB)"
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
