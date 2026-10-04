<#
.SYNOPSIS
  Uploads this project to a fresh Oracle Cloud VM and installs it there.

.DESCRIPTION
  Run this from Windows after you have created the VM in the Oracle console.
  It packs the project, copies it over SSH, runs deploy/oracle-cloud-setup.sh
  on the machine (Docker, firewalls, DuckDNS, TLS, auto-restart), then prints
  the owner password from the container log and the address to open.

  Everything it does is also in README_WEB.md section 12 - this is just the
  one-command version.

.EXAMPLE
  # HTTPS on a free DuckDNS name (recommended if you own no domain):
  .\deploy\deploy-from-windows.ps1 -Server 130.61.12.34 `
      -DuckDnsName mytrader -DuckDnsToken 8f3c-1b2a-... `
      -OwnerEmail me@gmail.com -RegistrationCode let-me-in

.EXAMPLE
  # HTTPS on your own domain, with the AI endpoint configured:
  .\deploy\deploy-from-windows.ps1 -Server 130.61.12.34 -Domain trader.example.com `
      -RegistrationCode let-me-in -LlmBaseUrl https://api.deepseek.com/v1 `
      -LlmApiKey sk-... -LlmModel deepseek-chat

.EXAMPLE
  # Show the exact commands without touching the server:
  .\deploy\deploy-from-windows.ps1 -Server 1.2.3.4 -DuckDnsName mytrader -DryRun
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Server,                      # the VM's public IP

    [string]$Domain = "",                 # your own hostname, if you have one
    [string]$DuckDnsName = "",            # e.g. "mytrader" -> mytrader.duckdns.org
    [string]$DuckDnsToken = "",

    [string]$OwnerEmail = "",
    [string]$RegistrationCode = "",
    [string]$LlmBaseUrl = "",
    [string]$LlmApiKey = "",
    [string]$LlmModel = "",
    [string]$TimeZone = "Europe/Bucharest",

    [string]$User = "ubuntu",
    [string]$KeyPath = "$env:USERPROFILE\.ssh\oracle-paper-trader",
    [string]$AppDir = "/opt/paper-trader",
    [switch]$WithTalib,                   # full 61-pattern TA-Lib build
    [switch]$SkipUpload,                  # only re-run the installer on the VM
    [switch]$DryRun                       # print the commands, change nothing
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$remoteTar = "paper-trader-upload.tar.gz"
$remoteSrc = "paper-trader"

function Say  ($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Warn ($m) { Write-Host "  ! $m" -ForegroundColor Yellow }
function Die  ($m) { Write-Host "  x $m" -ForegroundColor Red; exit 1 }
function Run  ($exe, [string[]]$arguments) {
    if ($DryRun) { Write-Host "  [dry-run] $exe $($arguments -join ' ')" -ForegroundColor DarkGray; return }
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { Die "$exe failed with exit code $LASTEXITCODE" }
}

# --- checks -----------------------------------------------------------------
Say "Checking what we need locally"
foreach ($tool in "ssh", "scp", "tar") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Die "$tool not found" }
}
if (-not (Test-Path $KeyPath)) {
    Die "SSH key not found at $KeyPath. Create one with:`n    ssh-keygen -t ed25519 -f `"$KeyPath`"`n  then paste the .pub file into the Oracle instance dialog."
}
if (-not (Test-Path (Join-Path $projectRoot "app_web.py"))) { Die "app_web.py not found - run this from the project folder" }
if ($DuckDnsName -and -not $DuckDnsToken) { Die "-DuckDnsName needs -DuckDnsToken too (from duckdns.org)" }
if (-not $Domain -and -not $DuckDnsName) {
    Warn "No -Domain and no -DuckDnsName: the site will answer on the VM only."
    Warn "The login form must not travel over plain HTTP, so set one of them."
    if (-not $DryRun) {
        $answer = Read-Host "  Continue anyway? (y/N)"
        if ($answer -notmatch '^[Yy]') { exit 1 }
    }
}

$sshArgs = @("-i", $KeyPath, "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=10", "$User@$Server")

Say "Waiting for SSH on $User@$Server"
$ready = $false
if ($DryRun) { $ready = $true }
for ($i = 1; $i -le 30 -and -not $ready; $i++) {
    & ssh @sshArgs "true" 2>$null
    if ($LASTEXITCODE -eq 0) { $ready = $true } else {
        Write-Host "  not up yet ($i/30)..." -ForegroundColor DarkGray
        Start-Sleep -Seconds 10
    }
}
if (-not $ready) {
    Die "Could not reach the VM. Check the public IP, that the instance is Running, and that port 22 is open."
}
Write-Host "  connected" -ForegroundColor Green

# --- upload -----------------------------------------------------------------
if (-not $SkipUpload) {
    Say "Packing and uploading the project"
    $tar = Join-Path $env:TEMP $remoteTar
    if (Test-Path $tar) { Remove-Item $tar -Force }
    Run "tar" @("-czf", $tar,
                "--exclude=__pycache__", "--exclude=.git", "--exclude=data",
                "--exclude=*.pyc", "--exclude=.env", "--exclude=docs",
                "--exclude=paper_trading_app.db*", "--exclude=*.log",
                # Never ship local secrets or machine-specific state to the
                # server: your invite code / tokens, the local password hash
                # and session secret, the local database and price caches.
                # The server creates its own config and prints its own owner
                # password on first start (this script shows it to you).
                "--exclude=deploy/my-deployment.ps1",
                "--exclude=web_config.json",
                "--exclude=tv_sp500_cache.json",
                "--exclude=tv_symbol_cache.json",
                "--exclude=*.bak",
                "-C", $projectRoot, ".")

    Run "ssh" ($sshArgs + "rm -rf ~/$remoteSrc && mkdir -p ~/$remoteSrc")
    Run "scp" @("-i", $KeyPath, "-o", "StrictHostKeyChecking=accept-new",
                $tar, "${User}@${Server}:~/$remoteTar")
    Run "ssh" ($sshArgs + "tar xzf ~/$remoteTar -C ~/$remoteSrc && rm -f ~/$remoteTar")
    Write-Host "  uploaded to ~/$remoteSrc on the server" -ForegroundColor Green
}

# --- install ----------------------------------------------------------------
Say "Installing on the server (Docker, firewall, DNS, HTTPS, autostart)"
$assignments = @("APP_DIR=$AppDir", "TZ=$TimeZone")
if ($Domain)           { $assignments += "DOMAIN=$Domain" }
if ($DuckDnsName)      { $assignments += "DUCKDNS_SUBDOMAIN=$DuckDnsName" }
if ($DuckDnsToken)     { $assignments += "DUCKDNS_TOKEN=$DuckDnsToken" }
if ($OwnerEmail)       { $assignments += "OWNER_EMAIL=$OwnerEmail" }
if ($RegistrationCode) { $assignments += "REGISTRATION_CODE=$RegistrationCode" }
if ($LlmBaseUrl)       { $assignments += "LLM_BASE_URL=$LlmBaseUrl" }
if ($LlmApiKey)        { $assignments += "LLM_API_KEY=$LlmApiKey" }
if ($LlmModel)         { $assignments += "LLM_MODEL=$LlmModel" }
if ($WithTalib)        { $assignments += "WITH_TALIB=1" }

$quoted = ($assignments | ForEach-Object { "'" + ($_ -replace "'", "'\''") + "'" }) -join " "
$installCmd = "sudo env $quoted bash ~/$remoteSrc/deploy/oracle-cloud-setup.sh"
Run "ssh" ($sshArgs + $installCmd)

if ($DryRun) {
    Write-Host "`n  [dry-run] nothing was changed.`n" -ForegroundColor DarkGray
    return
}

# --- report -----------------------------------------------------------------
Say "The owner password (printed once, on the first start)"
& ssh @sshArgs "cd $AppDir && sudo docker compose logs paper-trader 2>&1 | grep -A6 -i 'OWNER PASSWORD' | head -12" 2>$null

$url = if ($Domain) { "https://$Domain" }
       elseif ($DuckDnsName) { "https://$DuckDnsName.duckdns.org" }
       else { "http://${Server}:8080" }

Say "Done"
Write-Host @"

  Open:          $url
  Settings:      ssh -i "$KeyPath" $User@$Server
                 cd $AppDir && sudo nano .env   # then: sudo docker compose up -d
  Logs:          cd $AppDir && sudo docker compose logs -f
  Your files:    database and accounts live in $AppDir/data (survive rebuilds)
  Registration:  $(if ($RegistrationCode) { "invite code required" } else { "OPEN - set REGISTRATION_CODE in $AppDir/.env" })

  Trading Mode starts IDLE and survives reboots: whoever presses Trading Mode
  in the page is resumed on the next start, and only they are.

  If the page does not load, check in this order:
    1. VCN Security List allows 80 and 443   (Oracle console)
    2. sudo iptables -L INPUT --line-numbers -n | head
    3. cd $AppDir && sudo docker compose ps
"@ -ForegroundColor Green
