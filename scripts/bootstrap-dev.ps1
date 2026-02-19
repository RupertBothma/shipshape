param(
    [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path

. "$ScriptDir\ensure-wsl.ps1"

function Write-Step {
    param([string]$Message)
    Write-Host "[bootstrap] $Message"
}

function Fail {
    param([string]$Message)
    Write-Error "[bootstrap] ERROR: $Message"
    exit 1
}

$Distro = Ensure-WslReady -CallerLabel "bootstrap"

$RepoRootFwd = $RepoRoot -replace '\\', '/'
$RepoRootWsl = (& wsl.exe -d $Distro -- wslpath -a "$RepoRootFwd" 2>$null)
if ($RepoRootWsl) { $RepoRootWsl = ($RepoRootWsl -replace "`0", "").Trim() }
if (-not $RepoRootWsl) {
    Fail "Failed to resolve repository path inside WSL. Windows path: $RepoRoot"
}

$VerifyArg = ""
if ($SkipVerify) {
    $VerifyArg = "--skip-verify"
}

$Command = "cd '$RepoRootWsl' && chmod +x ./scripts/bootstrap-dev.sh && ./scripts/bootstrap-dev.sh $VerifyArg"
Write-Step "Running Nix bootstrap inside WSL."
& wsl.exe -d $Distro -- bash -lc "$Command"
