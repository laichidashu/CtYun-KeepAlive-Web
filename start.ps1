# CtYun KeepAlive - one-click start (PowerShell version, line-ending agnostic)
# Flow: locate/install python -> ensure .venv -> env check & auto-install deps -> run server
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$log = Join-Path $root 'startup.log'
"START $(Get-Date -Format 'yyyy/MM/dd HH:mm:ss')" | Out-File -FilePath $log -Encoding utf8
function Log($msg) {
    $msg | Out-File -FilePath $log -Append -Encoding utf8
}

Write-Host '================================================'
Write-Host ' CtYun KeepAlive  starting on http://localhost:8080'
Write-Host ' Default password: admin   (change it in Settings)'
Write-Host ' Press Ctrl+C to stop.'
Write-Host '================================================'

# ---------- helpers ----------

function Test-PythonCmd($cmd) {
    $c = Get-Command $cmd -ErrorAction SilentlyContinue
    if (-not $c) { return $null }
    try {
        & $cmd --version 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { return $null }
    } catch { return $null }
    return $cmd
}

function Install-Python {
    $inst = Join-Path $env:TEMP 'python-3.12.8-amd64.exe'
    if (Test-Path $inst) { Remove-Item $inst -Force -ErrorAction SilentlyContinue }
    Write-Host '[SETUP] Python not found. Auto-installing Python 3.12 (about 25 MB, please wait)...'
    $urls = @(
        'https://mirrors.huaweicloud.com/python/3.12.8/python-3.12.8-amd64.exe',
        'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe')
    $ok = $false
    foreach ($u in $urls) {
        Write-Host "[SETUP] Downloading: $u"
        try {
            [Net.ServicePointManager]::SecurityProtocol = 'Tls12'
            if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
                & curl.exe -L --fail --connect-timeout 30 -o $inst $u
                if ($LASTEXITCODE -eq 0 -and (Test-Path $inst) -and ((Get-Item $inst).Length -gt 10MB)) { $ok = $true }
            }
            if (-not $ok) {
                (New-Object Net.WebClient).DownloadFile($u, $inst)
                if ((Test-Path $inst) -and ((Get-Item $inst).Length -gt 10MB)) { $ok = $true }
            }
        } catch { $ok = $false }
        if ($ok) { break }
    }
    if (-not $ok) { Write-Host '[ERROR] Failed to download Python installer.'; return $null }

    Write-Host '[SETUP] Installing silently (per-user, auto PATH). Takes 1-2 minutes, do not close...'
    try {
        Start-Process -FilePath $inst -ArgumentList '/quiet','InstallAllUsers=0','PrependPath=1','Include_test=0' -Wait
    } catch {}
    if (Test-Path $inst) { Remove-Item $inst -Force -ErrorAction SilentlyContinue }

    $target = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    for ($i = 0; $i -lt 80; $i++) {
        if (Test-Path $target) { return $target }
        Start-Sleep -Seconds 3
    }
    Write-Host '[ERROR] Python install did not finish in time.'
    return $null
}

# ---------- 1. locate interpreter ----------

Log '[1/4] locating python'
$py = $null
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (Test-Path $venvPy) { $py = $venvPy }
if (-not $py) { $py = Test-PythonCmd 'python' }
if (-not $py) { $py = Test-PythonCmd 'py' }
if (-not $py) {
    foreach ($v in @('314','313','312','311','310')) {
        $c1 = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe"
        $c2 = Join-Path ${env:ProgramFiles} "Python$v\python.exe"
        if (Test-Path $c1) { $py = $c1; break }
        if (Test-Path $c2) { $py = $c2; break }
    }
}
if (-not $py) { $py = Install-Python }
if (-not $py) {
    Log '[1/4] NO PYTHON FOUND'
    Write-Host '[ERROR] Python 3.10+ is required but could not be found or auto-installed.'
    Write-Host '  1. Download: https://www.python.org/downloads/'
    Write-Host '  2. During install, CHECK "Add python.exe to PATH".'
    Write-Host '  3. Then run this script again.'
    Read-Host 'Press Enter to exit'
    exit 1
}
Write-Host "[SETUP] Using Python: $py"
Log "[1/4] python=$py"

# ---------- 2. ensure venv ----------

if (-not (Test-Path $venvPy)) {
    Write-Host '[SETUP] Creating virtual environment .venv ...'
    & $py -m venv (Join-Path $root '.venv')
    if (-not $?) { Write-Host '[WARN] Failed to create .venv, will use the interpreter above.' }
}
if (Test-Path $venvPy) { $py = $venvPy }

# ---------- 3. environment check ----------

Log '[2/4] checking environment'
Write-Host '[SETUP] Checking environment and dependencies ...'
& $py (Join-Path $root 'backend\bootstrap.py')
if ($LASTEXITCODE -ne 0) {
    Log '[2/4] env check FAILED'
    Write-Host '[ERROR] Environment check failed. Please fix the problems above and retry.'
    Read-Host 'Press Enter to exit'
    exit 1
}

# ---------- 3.5 firewall (best effort) ----------

Log '[3/4] firewall'
$ruleName = 'CtYun-KeepAlive-8080'
$existing = & netsh advfirewall firewall show rule name=$ruleName 2>$null
if ($LASTEXITCODE -ne 0) {
    & netsh advfirewall firewall add rule name=$ruleName dir=in action=allow protocol=TCP localport=8080 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host '[SETUP] Firewall rule added: inbound TCP 8080 allowed.'
        Log '[3/4] firewall rule added'
    } else {
        Write-Host '[HINT] Firewall rule not added - allow TCP 8080 manually to access from other devices.'
        Log '[3/4] firewall rule NOT added'
    }
}

# ---------- 4. start server ----------

Log '[4/4] starting server'
& $py (Join-Path $root 'backend\server.py')
Log '[4/4] server exited'
