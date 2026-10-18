param([string]$NsisPath = '')
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if (-not $NsisPath) { $NsisPath = Join-Path $projectRoot '.local\tools\nsis-3.13\makensis.exe' }
if (-not (Test-Path -LiteralPath $NsisPath -PathType Leaf)) { throw 'NSIS executable is required.' }
$testId = [Guid]::NewGuid().ToString('N')
$fixture = Join-Path $projectRoot ".local\installer-autostart-$testId"
$registryKey = "Software\SparkAssistant.InstallerTests\$testId"
$registryPath = "HKCU:\$registryKey"
if (Test-Path -LiteralPath $fixture) { throw 'Fixture directory must be new.' }
if (Test-Path -LiteralPath $registryPath) { throw 'Registry fixture key must be new.' }
if ($registryKey -notmatch '^Software\\SparkAssistant\.InstallerTests\\[0-9a-f]{32}$') { throw 'Invalid temporary registry path.' }
New-Item -ItemType Directory -Path $fixture | Out-Null
& $NsisPath '/V2' '/INPUTCHARSET' 'UTF8' "/DROOT=$projectRoot" "/DFIXTURE=$fixture" "/DREGKEY=$registryKey" (Join-Path $projectRoot 'tests\installer_autostart_probe.nsi')
if ($LASTEXITCODE -ne 0) { throw 'Autostart fixture compilation failed.' }
$cases = @('owned-quoted', 'owned-unquoted', 'owned-case-insensitive', 'other-installation',
           'source-version', 'unknown-arguments', 'ambiguous-unquoted-space', 'missing-value')
foreach ($testCase in $cases) {
    if (Test-Path -LiteralPath $registryPath) { throw 'Previous fixture did not clean its temporary key.' }
    $process = Start-Process -FilePath (Join-Path $fixture 'probe.exe') -ArgumentList "/CASE=$testCase" -WindowStyle Hidden -PassThru -Wait
    $report = Join-Path $fixture "result-$testCase.txt"
    if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $report)) { throw "$testCase failed with exit $($process.ExitCode)" }
    if ((Get-Content -LiteralPath $report -Raw) -ne 'PASS') { throw "${testCase}: ownership check failed" }
    if (Test-Path -LiteralPath $registryPath) { throw 'Disposable registry key was not removed.' }
    Write-Output "PASS $testCase"
}
Write-Output "$($cases.Count) autostart ownership checks passed; only a unique test registry key was used. Evidence: $fixture"
