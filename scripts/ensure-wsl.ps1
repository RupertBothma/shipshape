# Shared WSL bootstrap helper. Dot-source from other PS1 scripts:
#   . "$ScriptDir\ensure-wsl.ps1"
# Provides: Ensure-WslReady, which returns the active distro name.
# Provides: Ensure-DockerDesktop, which installs Docker Desktop if missing.

function Ensure-WslReady {
    param([string]$CallerLabel = "setup")

    # --- Step 1: Ensure WSL feature is installed ---
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        Write-Host "[$CallerLabel] WSL not found. Installing WSL..."
        Write-Host "[$CallerLabel] This requires administrator privileges and may prompt for elevation."

        # Use the system wsl.exe path directly since it may not be on PATH yet.
        # On Windows 10, the feature must be enabled first; on Windows 11,
        # 'wsl --install' handles everything.
        $WslInstaller = Join-Path $env:SystemRoot "System32\wsl.exe"
        if (-not (Test-Path $WslInstaller)) {
            # Fallback: enable WSL via DISM (works on all Windows 10+ builds)
            Write-Host "[$CallerLabel] Enabling WSL via DISM..."
            try {
                $DismArgs = "/online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart"
                Start-Process -FilePath "dism.exe" -ArgumentList $DismArgs `
                    -Verb RunAs -Wait -ErrorAction Stop
                $DismArgs2 = "/online /enable-feature /featurename:VirtualMachinePlatform /all /norestart"
                Start-Process -FilePath "dism.exe" -ArgumentList $DismArgs2 `
                    -Verb RunAs -Wait -ErrorAction Stop
            } catch {
                Write-Error "[$CallerLabel] ERROR: Failed to enable WSL features. Run as Administrator, or install WSL manually: https://aka.ms/wsl-install"
                exit 1
            }
            Write-Host ""
            Write-Host "[$CallerLabel] WSL features enabled. You MUST reboot your machine, then run this command again."
            Write-Host "[$CallerLabel] After reboot, open a terminal and re-run: .\quickstart.cmd"
            exit 0
        }

        try {
            Start-Process -FilePath $WslInstaller -ArgumentList "--install" `
                -Verb RunAs -Wait -ErrorAction Stop
        } catch {
            Write-Error "[$CallerLabel] ERROR: WSL installation failed. Install manually: https://aka.ms/wsl-install"
            exit 1
        }

        # Refresh PATH so wsl.exe is discoverable in this session
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + `
                     [System.Environment]::GetEnvironmentVariable("Path", "User")

        if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
            Write-Host ""
            Write-Host "[$CallerLabel] WSL installed but not yet available in this terminal."
            Write-Host "[$CallerLabel] Please REBOOT your machine, then run this command again."
            exit 0
        }
        Write-Host "[$CallerLabel] WSL installed."
    }

    # --- Step 2: Ensure WSL is version 2 ---
    try {
        $WslStatus = & wsl.exe --status 2>$null
        if ($WslStatus -and ($WslStatus -join "`n") -match "Default Version:\s*1") {
            Write-Host "[$CallerLabel] Setting WSL default version to 2..."
            & wsl.exe --set-default-version 2 2>$null | Out-Null
        }
    } catch { }

    # --- Step 3: Ensure at least one distro is installed ---
    # wsl -l -q emits UTF-16LE with null bytes; strip them before filtering.
    # Exclude Docker Desktop's internal distros — they are not usable shells.
    $RawOutput = & wsl.exe -l -q 2>$null
    $Distros = @()
    if ($RawOutput) {
        $Distros = @($RawOutput | ForEach-Object {
            ($_ -replace "`0", "").Trim()
        } | Where-Object { $_ -ne "" -and $_ -notmatch "^docker-desktop" })
    }

    if ($Distros.Count -eq 0) {
        Write-Host "[$CallerLabel] No WSL distro found. Installing Ubuntu (fully automated)..."

        # Snapshot existing distro names before install so we can detect the new one.
        $BeforeInstall = @()
        $BeforeRaw = & wsl.exe -l -q 2>$null
        if ($BeforeRaw) {
            $BeforeInstall = @($BeforeRaw | ForEach-Object {
                ($_ -replace "`0", "").Trim()
            } | Where-Object { $_ -ne "" })
        }

        # Try --no-launch first to avoid the interactive username/password prompt.
        # Let stdout/stderr stream to the console so download progress is visible.
        Write-Host "[$CallerLabel] Downloading and installing Ubuntu distro..."
        & wsl.exe --install -d Ubuntu --no-launch
        if ($LASTEXITCODE -ne 0) {
            # Fallback: run install in the current window so progress is visible.
            Write-Host "[$CallerLabel] --no-launch not supported. Retrying with interactive install..."
            & wsl.exe --install -d Ubuntu
        }

        # Wait for a new (non-docker-desktop) distro to appear
        Write-Host "[$CallerLabel] Waiting for Ubuntu to register..."
        $SetupTimeout = 300
        $Elapsed = 0
        $PollInterval = 3
        $NewDistro = $null
        while ($Elapsed -lt $SetupTimeout) {
            Start-Sleep -Seconds $PollInterval
            $Elapsed += $PollInterval

            $AfterRaw = & wsl.exe -l -q 2>$null
            if ($AfterRaw) {
                $AfterDistros = @($AfterRaw | ForEach-Object {
                    ($_ -replace "`0", "").Trim()
                } | Where-Object { $_ -ne "" -and $_ -notmatch "^docker-desktop" })

                # Find the newly added distro (not in BeforeInstall, not docker-desktop)
                foreach ($d in $AfterDistros) {
                    if ($BeforeInstall -notcontains $d) {
                        $NewDistro = $d
                        break
                    }
                }
                # If no new distro found but an Ubuntu-like one exists, use it
                if (-not $NewDistro -and $AfterDistros.Count -gt 0) {
                    foreach ($d in $AfterDistros) {
                        if ($d -match "^Ubuntu") {
                            $NewDistro = $d
                            break
                        }
                    }
                    if (-not $NewDistro) { $NewDistro = $AfterDistros[0] }
                }
            }

            if ($NewDistro) { break }

            if ($Elapsed % 15 -eq 0) {
                Write-Host "[$CallerLabel] Still waiting for Ubuntu to register... ($Elapsed`s)"
            }
        }

        if (-not $NewDistro) {
            Write-Error "[$CallerLabel] ERROR: No new distro detected after $($SetupTimeout)s. Reboot and retry."
            exit 1
        }

        Write-Host "[$CallerLabel] Distro registered as: $NewDistro"

        # Wait for the distro to actually respond (it may still be initializing)
        $ReadyTimeout = 60
        $ReadyElapsed = 0
        $IsReady = $false
        while ($ReadyElapsed -lt $ReadyTimeout) {
            $TestOut = $null
            try { $TestOut = (& wsl.exe -d $NewDistro -u root -- echo "ok" 2>$null) } catch { }
            if ($TestOut -and ($TestOut -replace "`0", "").Trim() -eq "ok") {
                $IsReady = $true
                break
            }
            Start-Sleep -Seconds 3
            $ReadyElapsed += 3
        }

        if (-not $IsReady) {
            Write-Error "[$CallerLabel] ERROR: Distro '$NewDistro' registered but not responding. Reboot and retry."
            exit 1
        }

        # Create a default user non-interactively (running as root).
        $DefaultUser = "dev"
        $DefaultPass = "dev"
        Write-Host "[$CallerLabel] Creating default user '$DefaultUser'..."

        $TestId = $null
        try { $TestId = (& wsl.exe -d $NewDistro -u root -- id -u $DefaultUser 2>$null) } catch { }

        if (-not $TestId -or ($TestId -replace "`0", "").Trim() -eq "") {
            & wsl.exe -d $NewDistro -u root -- useradd -m -s /bin/bash $DefaultUser 2>$null | Out-Null
            & wsl.exe -d $NewDistro -u root -- bash -c "echo '${DefaultUser}:${DefaultPass}' | chpasswd" 2>$null | Out-Null
            & wsl.exe -d $NewDistro -u root -- usermod -aG sudo $DefaultUser 2>$null | Out-Null
            & wsl.exe -d $NewDistro -u root -- bash -c "echo '${DefaultUser} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/${DefaultUser}" 2>$null | Out-Null
        }

        # Set as default user via /etc/wsl.conf
        & wsl.exe -d $NewDistro -u root -- bash -c "printf '[boot]\nsystemd=true\n\n[user]\ndefault=${DefaultUser}\n' > /etc/wsl.conf" 2>$null | Out-Null

        # Restart distro to apply wsl.conf
        & wsl.exe --terminate $NewDistro 2>$null | Out-Null
        Start-Sleep -Seconds 2

        Write-Host "[$CallerLabel] Ubuntu configured with user '$DefaultUser' (password: '$DefaultPass')."
        Write-Host "[$CallerLabel] Change it later with: wsl -d $NewDistro -- passwd"

        $Distros = @($NewDistro)
    }

    $Distro = $Distros[0]

    # --- Step 4: Ensure the distro is responsive ---
    $MaxRetries = 24
    $RetryDelay = 5
    for ($i = 1; $i -le $MaxRetries; $i++) {
        $TestOutput = $null
        try {
            $TestOutput = (& wsl.exe -d $Distro -- echo "ready" 2>$null)
        } catch { }

        # Strip potential null bytes from UTF-16 output
        if ($TestOutput) {
            $TestOutput = ($TestOutput -replace "`0", "").Trim()
        }

        if ($TestOutput -eq "ready") {
            # Verify WSL2 (not WSL1) for Docker compatibility
            try {
                $VersionInfo = & wsl.exe -l -v 2>$null
                if ($VersionInfo -and ($VersionInfo -join "`n") -match "$([regex]::Escape($Distro))\s+\w+\s+1\b") {
                    Write-Host "[$CallerLabel] Converting '$Distro' from WSL1 to WSL2..."
                    & wsl.exe --set-version $Distro 2 | Out-Null
                }
            } catch { }

            Write-Host "[$CallerLabel] WSL distro ready: $Distro"
            return $Distro
        }

        if ($i -eq $MaxRetries) {
            Write-Error "[$CallerLabel] ERROR: WSL distro '$Distro' did not respond after $($MaxRetries * $RetryDelay)s."
            Write-Host "[$CallerLabel] Try running: wsl -d $Distro"
            Write-Host "[$CallerLabel] If prompted for setup, complete it, then retry."
            exit 1
        }
        Write-Host "[$CallerLabel] Waiting for WSL distro '$Distro' to respond (attempt $i/$MaxRetries)..."
        Start-Sleep -Seconds $RetryDelay
    }
}

function Ensure-DockerDesktop {
    param(
        [string]$CallerLabel = "setup",
        [string]$Distro
    )

    $DockerExe = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $DockerExe) {
        Write-Host "[$CallerLabel] Docker Desktop is already installed."
    } else {
        Write-Host "[$CallerLabel] Docker Desktop not found. Installing via winget..."

        if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
            Write-Error "[$CallerLabel] ERROR: winget is not available. Install Docker Desktop manually: https://www.docker.com/products/docker-desktop/"
            exit 1
        }

        try {
            & winget.exe install -e --id Docker.DockerDesktop --accept-source-agreements --accept-package-agreements
        } catch {
            Write-Error "[$CallerLabel] ERROR: Docker Desktop installation failed. Install manually: https://www.docker.com/products/docker-desktop/"
            exit 1
        }

        if (-not (Test-Path $DockerExe)) {
            Write-Host ""
            Write-Host "[$CallerLabel] Docker Desktop was installed but may require a reboot or log-out/log-in."
            Write-Host "[$CallerLabel] After restarting, re-run this command."
            exit 0
        }
    }

    $DockerRunning = Get-Process "Docker Desktop" -ErrorAction SilentlyContinue
    if (-not $DockerRunning) {
        Write-Host "[$CallerLabel] Starting Docker Desktop..."
        Start-Process $DockerExe
        Write-Host "[$CallerLabel] Waiting for Docker Desktop to start (this may take a minute)..."
        $DockerTimeout = 120
        $DockerElapsed = 0
        while ($DockerElapsed -lt $DockerTimeout) {
            Start-Sleep -Seconds 5
            $DockerElapsed += 5

            $TestDocker = $null
            try {
                $TestDocker = & docker.exe info 2>$null
            } catch { }
            if ($LASTEXITCODE -eq 0 -and $TestDocker) {
                break
            }

            if ($DockerElapsed % 15 -eq 0) {
                Write-Host "[$CallerLabel] Still waiting for Docker Desktop... ($DockerElapsed`s)"
            }
        }

        if ($DockerElapsed -ge $DockerTimeout) {
            Write-Host "[$CallerLabel] WARNING: Docker Desktop may not be fully ready yet."
            Write-Host "[$CallerLabel] If the e2e step fails, wait for Docker Desktop to finish starting and retry."
        } else {
            Write-Host "[$CallerLabel] Docker Desktop is running."
        }
    }

    if ($Distro) {
        Write-Host "[$CallerLabel] Configuring Docker Desktop WSL integration for '$Distro'..."

        # Make this distro the WSL default so Docker Desktop auto-integrates with it.
        & wsl.exe --set-default $Distro 2>$null | Out-Null

        # Patch Docker Desktop's settings.json to add the distro to integratedWslDistros.
        $SettingsFile = Join-Path $env:APPDATA "Docker\settings.json"

        # Docker Desktop creates the file on first launch; wait briefly if needed.
        if (-not (Test-Path $SettingsFile)) {
            $SettingsWait = 0
            while (-not (Test-Path $SettingsFile) -and $SettingsWait -lt 30) {
                Start-Sleep -Seconds 2
                $SettingsWait += 2
            }
        }

        if (Test-Path $SettingsFile) {
            try {
                $Json = Get-Content $SettingsFile -Raw | ConvertFrom-Json
                $Modified = $false

                # Ensure the WSL2 engine is enabled.
                if ($Json.PSObject.Properties.Name -contains "wslEngineEnabled" -and $Json.wslEngineEnabled -ne $true) {
                    $Json.wslEngineEnabled = $true
                    $Modified = $true
                }

                # Add distro to the integrated-distros list.
                if ($Json.PSObject.Properties.Name -contains "integratedWslDistros") {
                    if ($Json.integratedWslDistros -notcontains $Distro) {
                        $Json.integratedWslDistros = @($Json.integratedWslDistros) + @($Distro)
                        $Modified = $true
                    }
                } else {
                    $Json | Add-Member -NotePropertyName "integratedWslDistros" -NotePropertyValue @($Distro) -Force
                    $Modified = $true
                }

                if ($Modified) {
                    # Stop Docker Desktop so we can safely write settings.
                    Stop-Process -Name "Docker Desktop" -Force -ErrorAction SilentlyContinue
                    Start-Sleep -Seconds 5

                    $Json | ConvertTo-Json -Depth 20 | Set-Content $SettingsFile -Encoding UTF8
                    Write-Host "[$CallerLabel] WSL integration enabled for '$Distro' in Docker Desktop settings."

                    # Restart Docker Desktop to pick up the change.
                    Write-Host "[$CallerLabel] Restarting Docker Desktop..."
                    Start-Process $DockerExe

                    $RestartTimeout = 120
                    $RestartElapsed = 0
                    while ($RestartElapsed -lt $RestartTimeout) {
                        Start-Sleep -Seconds 5
                        $RestartElapsed += 5

                        $TestDocker = $null
                        try { $TestDocker = & docker.exe info 2>$null } catch { }
                        if ($LASTEXITCODE -eq 0 -and $TestDocker) {
                            Write-Host "[$CallerLabel] Docker Desktop restarted with WSL integration for '$Distro'."
                            break
                        }

                        if ($RestartElapsed % 15 -eq 0) {
                            Write-Host "[$CallerLabel] Still waiting for Docker Desktop... ($RestartElapsed`s)"
                        }
                    }

                    if ($RestartElapsed -ge $RestartTimeout) {
                        Write-Host "[$CallerLabel] WARNING: Docker Desktop may not be fully ready yet."
                        Write-Host "[$CallerLabel] If the e2e step fails, wait for Docker Desktop to finish starting and retry."
                    }
                } else {
                    Write-Host "[$CallerLabel] Docker Desktop already configured for '$Distro'."
                }
            } catch {
                Write-Host "[$CallerLabel] WARNING: Could not update Docker Desktop settings automatically."
                Write-Host "[$CallerLabel] Enable WSL integration manually:"
                Write-Host "[$CallerLabel]   Docker Desktop > Settings > Resources > WSL Integration > enable '$Distro'"
                Write-Host "[$CallerLabel]   Then restart Docker Desktop."
            }
        } else {
            Write-Host "[$CallerLabel] Docker Desktop settings file not found."
            Write-Host "[$CallerLabel] Enable WSL integration manually after Docker Desktop finishes initializing:"
            Write-Host "[$CallerLabel]   Docker Desktop > Settings > Resources > WSL Integration > enable '$Distro'"
            Write-Host "[$CallerLabel]   Then restart Docker Desktop."
        }
    }
}
