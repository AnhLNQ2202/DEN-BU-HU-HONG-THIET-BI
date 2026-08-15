$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "Local Bridge chi chay tren Windows."
}

$InstallRoot = Join-Path $env:LOCALAPPDATA "AssetCompensationHub\OutlookBridge"
$DesktopRoot = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopRoot "Asset Hub - Outlook Bridge.lnk"
$PackageFiles = @("app.py", "core.py", "requirements.txt", "start.cmd", "README.txt")

Write-Host "1/4 - Dang kiem tra Python..." -ForegroundColor Cyan
$PythonCommand = $null
$PythonPrefix = @()
if (Get-Command "py.exe" -ErrorAction SilentlyContinue) {
    $PythonCommand = "py.exe"
    $PythonPrefix = @("-3")
} elseif (Get-Command "python.exe" -ErrorAction SilentlyContinue) {
    $PythonCommand = "python.exe"
}
if (-not $PythonCommand) {
    throw "Chua co Python 3.11 tro len. Hay cai Python tu python.org, danh dau Add Python to PATH, roi chay lai."
}

$VersionText = & $PythonCommand @PythonPrefix -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0) {
    throw "Khong mo duoc Python."
}
$VersionParts = $VersionText.Trim().Split(".")
if ([int]$VersionParts[0] -lt 3 -or ([int]$VersionParts[0] -eq 3 -and [int]$VersionParts[1] -lt 11)) {
    throw "Can Python 3.11 tro len. May nay dang co Python $VersionText."
}

Write-Host "2/4 - Dang chep chuong trinh vao LOCALAPPDATA..." -ForegroundColor Cyan
New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
foreach ($FileName in $PackageFiles) {
    $SourcePath = Join-Path $PSScriptRoot $FileName
    if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) {
        throw "Goi tai thieu file $FileName. Hay tai lai tu Product."
    }
    Copy-Item -LiteralPath $SourcePath -Destination (Join-Path $InstallRoot $FileName) -Force
}

$VenvRoot = Join-Path $InstallRoot ".venv"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    & $PythonCommand @PythonPrefix -m venv $VenvRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Khong tao duoc moi truong Python rieng."
    }
}

Write-Host "3/4 - Dang cai 2 thu vien can thiet..." -ForegroundColor Cyan
& $VenvPython -m pip install --disable-pip-version-check --requirement (Join-Path $InstallRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Khong cai duoc thu vien. Hay kiem tra mang/proxy cong ty roi thu lai."
}

Write-Host "4/4 - Dang tao nut mo tren Desktop..." -ForegroundColor Cyan
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = Join-Path $InstallRoot "start.cmd"
$Shortcut.WorkingDirectory = $InstallRoot
$Shortcut.Description = "Cau noi Classic Outlook voi Asset Compensation Hub"
$Shortcut.Save()

Write-Host ""
Write-Host "DA XONG!" -ForegroundColor Green
Write-Host "Hay mo Classic Outlook, sau do bam 'Asset Hub - Outlook Bridge' tren Desktop."
Write-Host "Chuong trinh khong can quyen Administrator va khong tu gui mail."
Read-Host "Nhan Enter de dong cua so"

