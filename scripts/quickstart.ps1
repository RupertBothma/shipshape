$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path

. "$ScriptDir\ensure-wsl.ps1"

$SkipBootstrap = $false
$SkipBootstrapVerify = $false
$SkipE2E = $false

function Write-Step {
    param([string]$Message)
    Write-Host "[quickstart] $Message"
}

function Fail {
    param([string]$Message)
    Write-Error "[quickstart] ERROR: $Message"
    exit 1
}

function Show-Usage {
    Write-Host "Usage: .\quickstart.cmd [--skip-bootstrap] [--skip-bootstrap-verify] [--skip-e2e]"
    Write-Host ""
    Write-Host "Options:"
    Write-Host "  --skip-bootstrap         Skip running bootstrap."
    Write-Host "  --skip-bootstrap-verify  Pass --skip-verify to bootstrap."
    Write-Host "  --skip-e2e               Skip Kind end-to-end validation."
}

foreach ($Arg in $args) {
    switch ($Arg) {
        "--skip-bootstrap" { $SkipBootstrap = $true; continue }
        "-SkipBootstrap" { $SkipBootstrap = $true; continue }
        "--skip-bootstrap-verify" { $SkipBootstrapVerify = $true; continue }
        "-SkipBootstrapVerify" { $SkipBootstrapVerify = $true; continue }
        "--skip-e2e" { $SkipE2E = $true; continue }
        "-SkipE2E" { $SkipE2E = $true; continue }
        "-h" { Show-Usage; exit 0 }
        "--help" { Show-Usage; exit 0 }
        "-Help" { Show-Usage; exit 0 }
        default { Fail "Unknown option: $Arg" }
    }
}

$Distro = Ensure-WslReady -CallerLabel "quickstart"

if ($SkipE2E -eq $false) {
    Ensure-DockerDesktop -CallerLabel "quickstart" -Distro $Distro
}

# Convert Windows path to WSL path. Use forward slashes to avoid
# PowerShell/wsl.exe argument escaping issues with backslashes.
$RepoRootFwd = $RepoRoot -replace '\\', '/'
$RepoRootWsl = (& wsl.exe -d $Distro -- wslpath -a "$RepoRootFwd" 2>$null)
if ($RepoRootWsl) { $RepoRootWsl = ($RepoRootWsl -replace "`0", "").Trim() }
if (-not $RepoRootWsl) {
    Fail "Failed to resolve repository path inside WSL. Windows path: $RepoRoot"
}

$QuickstartArgs = @()
if ($SkipBootstrap -eq $true) {
    $QuickstartArgs += "--skip-bootstrap"
}
if ($SkipBootstrapVerify -eq $true) {
    $QuickstartArgs += "--skip-bootstrap-verify"
}
if ($SkipE2E -eq $true) {
    $QuickstartArgs += "--skip-e2e"
}

$QuickstartArgString = ""
if ($QuickstartArgs.Count -gt 0) {
    $QuickstartArgString = " " + ($QuickstartArgs -join " ")
}

$Command = "cd '$RepoRootWsl' && chmod +x ./scripts/quickstart.sh && ./scripts/quickstart.sh$QuickstartArgString"
Write-Step "Running full quickstart workflow inside WSL."
& wsl.exe -d $Distro -- bash -lc "$Command"
