$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path

. "$ScriptDir\ensure-wsl.ps1"

function Write-Step {
    param([string]$Message)
    Write-Host "[cleanup] $Message"
}

function Fail {
    param([string]$Message)
    Write-Error "[cleanup] ERROR: $Message"
    exit 1
}

$Distro = Ensure-WslReady -CallerLabel "cleanup"

$RepoRootFwd = $RepoRoot -replace '\\', '/'
$RepoRootWsl = (& wsl.exe -d $Distro -- wslpath -a "$RepoRootFwd" 2>$null)
if ($RepoRootWsl) { $RepoRootWsl = ($RepoRootWsl -replace "`0", "").Trim() }
if (-not $RepoRootWsl) {
    Fail "Failed to resolve repository path inside WSL. Windows path: $RepoRoot"
}

$BashEscapedArgs = @()
foreach ($Arg in $args) {
    $Escaped = $Arg -replace "'", "'`"'`"'"
    $BashEscapedArgs += "'$Escaped'"
}

$CleanupArgString = ""
if ($BashEscapedArgs.Count -gt 0) {
    $CleanupArgString = " " + ($BashEscapedArgs -join " ")
}

$Command = "cd '$RepoRootWsl' && chmod +x ./hack/cleanup-local-dev.sh && ./hack/cleanup-local-dev.sh$CleanupArgString"
Write-Step "Running cleanup workflow inside WSL."
& wsl.exe -d $Distro -- bash -lc "$Command"
