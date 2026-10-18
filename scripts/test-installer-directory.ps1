param([string]$NsisPath = '')
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$probeDir = Join-Path $projectRoot '.local\installer-directory-probe'
if (-not $NsisPath) {
    $compiler = Get-Command makensis.exe -ErrorAction SilentlyContinue
    if ($compiler) {
        $NsisPath = $compiler.Source
    } else {
        $NsisPath = Join-Path $projectRoot '.local\tools\nsis-3.13\makensis.exe'
    }
}
if (-not (Test-Path -LiteralPath $NsisPath -PathType Leaf)) {
    throw 'NSIS is required. Pass -NsisPath with its makensis.exe path.'
}
if (Test-Path -LiteralPath (Join-Path $probeDir 'never-created')) {
    throw 'Regression fixture must remain nonexistent: .local\installer-directory-probe\never-created'
}
New-Item -ItemType Directory -Path $probeDir -Force | Out-Null
& $NsisPath '/V2' '/INPUTCHARSET' 'UTF8' "/DROOT=$projectRoot" (Join-Path $projectRoot 'tests\installer_directory_probe.nsi')
if ($LASTEXITCODE -ne 0) { throw 'Unable to compile directory validation probe.' }
$probe = Join-Path $probeDir 'installer-directory-probe.exe'
$cases = @(
    'valid-new', 'valid-default', 'valid-trailing', 'valid-multiple-trailing',
    'valid-case', 'valid-spaces-unicode', 'invalid-other', 'invalid-root',
    'invalid-parent', 'invalid-empty'
)
foreach ($testCase in $cases) {
    $report = Join-Path $probeDir "result-$testCase.txt"
    if (Test-Path -LiteralPath $report) { Remove-Item -LiteralPath $report }
    $process = Start-Process -FilePath $probe -ArgumentList "/CASE=$testCase" -WindowStyle Hidden -PassThru -Wait
    if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $report)) {
        throw "Directory validation probe failed: $testCase, exit $($process.ExitCode)"
    }
    $result = Get-Content -LiteralPath $report -Raw
    if ($result -notmatch '(?m)^result=PASS\r?$') { throw $result }
    Write-Output "PASS $testCase"
}
if (Test-Path -LiteralPath (Join-Path $probeDir 'never-created')) {
    throw 'Probe unexpectedly created an installation target.'
}
Write-Output "$($cases.Count) installer directory callback checks passed; no installation target was created."
