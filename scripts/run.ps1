#!/usr/bin/env pwsh
# scripts/run.ps1 — clean stale processes then start `cargo tauri dev`.
#
# Why: Python sidecar can survive vox.exe crashes and hold ~3GB VRAM via CUDA
# context. New sidecar then OOMs at model load, vox emits sidecar-degraded,
# and the user is forced to reboot. This pre-flight removes the ghost.
#
# Usage:   npm run dev:clean        (preferred)
#          pwsh scripts/run.ps1     (direct)

$ErrorActionPreference = "Continue"
Write-Host "[vox-run] Cleaning stale processes..." -ForegroundColor Cyan

# 1. Kill vox.exe (Tauri shell)
Get-Process -Name "vox" -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "[vox-run]   killing vox.exe PID $($_.Id)"
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}

# 2. Kill python.exe processes whose command line points at the vox sidecar.
#    Match by CommandLine so we never touch unrelated Python work.
$voxRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and (
        $_.CommandLine -match [regex]::Escape($voxRoot) -or
        $_.CommandLine -match '\\sidecar\\main\.py' -or
        $_.CommandLine -match 'vox.*sidecar'
    )
} | ForEach-Object {
    Write-Host "[vox-run]   killing python.exe PID $($_.ProcessId) (vox sidecar)"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

# 3. Kill any python.exe still holding a CUDA context (catches crashes whose
#    CommandLine was already cleared by the OS).
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $cudaPids = & nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>$null
    foreach ($line in $cudaPids) {
        $childPid = $line.Trim()
        if ($childPid -match '^\d+$') {
            $proc = Get-Process -Id $childPid -ErrorAction SilentlyContinue
            if ($proc -and $proc.Name -eq 'python') {
                Write-Host "[vox-run]   killing CUDA-holding python.exe PID $childPid"
                Stop-Process -Id $childPid -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

# 4. Remove stale port lock so the next sidecar starts from a clean slate.
$lockPath = Join-Path $env:USERPROFILE ".vox\data\port.lock"
if (Test-Path $lockPath) {
    Remove-Item $lockPath -Force -ErrorAction SilentlyContinue
    Write-Host "[vox-run]   removed stale port.lock"
}

# 5. Give the NVIDIA driver a moment to release the CUDA context before the
#    new sidecar tries to read free VRAM.
Start-Sleep -Milliseconds 1500

Write-Host "[vox-run] Clean. Starting cargo tauri dev..." -ForegroundColor Green
cargo tauri dev
exit $LASTEXITCODE
