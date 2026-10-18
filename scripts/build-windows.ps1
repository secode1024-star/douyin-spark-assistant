param([switch]$SkipInstall, [string]$NsisPath = '')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not $SkipInstall) {
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Unable to create virtual environment.' }
    }
    & $pythonPath -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
$version = (& $pythonPath -c 'from spark_assistant import __version__; print(__version__)').Trim()
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+([.-][A-Za-z0-9.-]+)?$') { throw 'Invalid application version.' }
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $projectRoot '.local\browsers'
$env:PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT = '180000'
& $pythonPath -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Browser installation failed.' }
& $pythonPath scripts/make_icons.py
if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed.' }
& $pythonPath scripts/collect_licenses.py
if ($LASTEXITCODE -ne 0) { throw 'License collection failed.' }
& $pythonPath scripts/write_build_info.py
if ($LASTEXITCODE -ne 0) { throw 'Build fingerprint generation failed.' }
$originalBuildPath = $env:PATH
try {
    # Do not collect same-named DLLs from unrelated programs on the build host.
    $env:PATH = "$env:WINDIR\System32;$env:WINDIR;$(Split-Path -Parent $pythonPath)"
    & $pythonPath -m PyInstaller --clean --noconfirm packaging/SparkAssistant.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
} finally {
    $env:PATH = $originalBuildPath
}
& $pythonPath scripts/make_uninstall_manifest.py
if ($LASTEXITCODE -ne 0) { throw 'Uninstall file list generation failed.' }
if (-not $NsisPath) {
    $compiler = Get-Command makensis.exe -ErrorAction SilentlyContinue
    if ($compiler) { $NsisPath = $compiler.Source }
    else {
        $candidates = @("${env:ProgramFiles(x86)}\NSIS\makensis.exe", "$projectRoot\.local\tools\nsis-3.13\makensis.exe")
        $NsisPath = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
}
if (-not $NsisPath) { throw 'NSIS is required. Install NSIS or pass -NsisPath with its makensis.exe path.' }
New-Item -ItemType Directory -Path release -Force | Out-Null
& $NsisPath /INPUTCHARSET UTF8 "/DROOT=$projectRoot" "/DVERSION=$version" packaging/windows-installer.nsi
if ($LASTEXITCODE -ne 0) { throw 'Installer generation failed.' }
$installer = Join-Path $projectRoot "release\SparkAssistant-Setup-$version-windows-x64.exe"
$hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  $(Split-Path -Leaf $installer)" | Set-Content -LiteralPath "$installer.sha256" -Encoding ascii
Write-Host "Built $installer"
