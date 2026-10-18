param([string]$NsisPath = '', [string]$PythonPath = '')
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if (-not $NsisPath) { $NsisPath = Join-Path $projectRoot '.local\tools\nsis-3.13\makensis.exe' }
if (-not $PythonPath) { $PythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $NsisPath -PathType Leaf)) { throw 'NSIS executable is required.' }
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw 'Python executable is required.' }
$fixture = Join-Path $projectRoot ('.local\installer-safety-' + [Guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $fixture) { throw 'Refusing to reuse an existing fixture.' }
New-Item -ItemType Directory -Path $fixture | Out-Null
$payload = Join-Path $fixture 'payload'
New-Item -ItemType Directory -Path (Join-Path $payload '_internal\nested') -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $payload 'SparkAssistant.exe'), 'fake desktop')
[IO.File]::WriteAllText((Join-Path $payload 'SparkWorker.exe'), 'fake worker')
[IO.File]::WriteAllText((Join-Path $payload '_internal\nested\literal$0.txt'), 'owned payload')
& $PythonPath (Join-Path $projectRoot 'scripts\make_uninstall_manifest.py') --dist $payload --output (Join-Path $fixture 'manifest.nsh')
if ($LASTEXITCODE -ne 0) { throw 'Manifest generation failed.' }
& $NsisPath '/V2' '/INPUTCHARSET' 'UTF8' "/DROOT=$projectRoot" "/DFIXTURE=$fixture" (Join-Path $projectRoot 'tests\installer_safety_probe.nsi')
if ($LASTEXITCODE -ne 0) { throw 'Safety fixture compilation failed.' }

function Invoke-Fixture([string]$Case, [string]$Leaf, [bool]$ExpectedSuccess) {
    $target = [IO.Path]::GetFullPath((Join-Path $fixture "cases\$Case\$Leaf"))
    if (-not $target.StartsWith($fixture + '\cases\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe fixture path.' }
    $process = Start-Process -FilePath (Join-Path $fixture 'probe.exe') -ArgumentList @('/S', "/D=$target") -WindowStyle Hidden -PassThru -Wait
    if (($process.ExitCode -eq 0) -ne $ExpectedSuccess) { throw "$Case unexpected exit: $($process.ExitCode)" }
    if ($ExpectedSuccess -and -not (Test-Path -LiteralPath (Join-Path $target 'SparkWorker.exe'))) { throw "$Case did not install fixture files." }
    if (-not $ExpectedSuccess -and (Test-Path -LiteralPath (Join-Path $target 'SparkWorker.exe'))) { throw "$Case wrote payload before rejecting." }
    Write-Output "PASS $Case"
}

Invoke-Fixture 'invalid-silent-path' 'Other' $false
if (Test-Path -LiteralPath (Join-Path $fixture 'cases\invalid-silent-path\Other')) { throw 'Invalid path was created.' }
Invoke-Fixture 'new-directory' 'SparkAssistant' $true
$empty = Join-Path $fixture 'cases\empty-directory\SparkAssistant'
New-Item -ItemType Directory -Path $empty -Force | Out-Null
Invoke-Fixture 'empty-directory' 'SparkAssistant' $true

$unknown = Join-Path $fixture 'cases\unknown-nonempty\SparkAssistant'
New-Item -ItemType Directory -Path $unknown -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $unknown 'external.txt'), 'keep unknown')
Invoke-Fixture 'unknown-nonempty' 'SparkAssistant' $false

$badMarker = Join-Path $fixture 'cases\bad-marker\SparkAssistant'
New-Item -ItemType Directory -Path $badMarker -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $badMarker '.spark-install'), 'SparkAssistant-extra')
Invoke-Fixture 'bad-marker' 'SparkAssistant' $false

$linked = Join-Path $fixture 'cases\linked-upgrade\SparkAssistant'
New-Item -ItemType Directory -Path $linked -Force | Out-Null
$linkTarget = Join-Path $fixture 'link-target'
New-Item -ItemType Directory -Path $linkTarget | Out-Null
[IO.File]::WriteAllText((Join-Path $linked '.spark-install'), 'SparkAssistant')
[IO.File]::WriteAllText((Join-Path $linkTarget 'external.txt'), 'keep link target')
New-Item -ItemType Junction -Path (Join-Path $linked '_internal') -Target $linkTarget | Out-Null
Invoke-Fixture 'linked-upgrade' 'SparkAssistant' $false
if ((Get-Content -LiteralPath (Join-Path $linkTarget 'external.txt') -Raw) -ne 'keep link target') { throw 'Linked target was modified.' }

$upgrade = Join-Path $fixture 'cases\valid-upgrade\SparkAssistant'
New-Item -ItemType Directory -Path (Join-Path $upgrade '_internal\nested') -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $upgrade '.spark-install'), 'SparkAssistant')
[IO.File]::WriteAllText((Join-Path $upgrade '_internal\nested\external-sentinel.txt'), 'retain this external file')
Invoke-Fixture 'valid-upgrade' 'SparkAssistant' $true
if ((Get-Content -LiteralPath (Join-Path $upgrade '_internal\nested\literal$0.txt') -Raw) -ne 'owned payload') { throw 'Literal dollar filename was not installed correctly.' }

# _?= keeps this disposable uninstaller in place so -Wait observes completion.
# Absolute target has just been checked as a newly owned workspace fixture.
$resolvedUpgrade = [IO.Path]::GetFullPath($upgrade)
if (-not $resolvedUpgrade.StartsWith($fixture + '\cases\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe uninstall target.' }
$process = Start-Process -FilePath (Join-Path $resolvedUpgrade 'Uninstall.exe') -ArgumentList @('/S', "_?=$resolvedUpgrade") -WindowStyle Hidden -PassThru -Wait
if ($process.ExitCode -ne 0) { throw "Fixture uninstall failed: $($process.ExitCode)" }
if (Test-Path -LiteralPath (Join-Path $upgrade 'SparkWorker.exe')) { throw 'Packaged file survived fixture uninstall.' }
if (Test-Path -LiteralPath (Join-Path $upgrade '_internal\nested\literal$0.txt')) { throw 'Packaged literal-dollar file survived uninstall.' }
if ((Get-Content -LiteralPath (Join-Path $upgrade '_internal\nested\external-sentinel.txt') -Raw) -ne 'retain this external file') { throw 'External sentinel was changed or deleted.' }
Write-Output 'PASS external-sentinel-preserved'
Write-Output "Fixture evidence retained at $fixture; no registry or shortcut operations were performed."
