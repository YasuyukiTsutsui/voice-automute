# voice-automute uninstaller: removes the Startup shortcut, stops the watcher and
# undoes any mute that a killed watcher might have left behind.
$ErrorActionPreference = 'Continue'
$proj   = $PSScriptRoot
$script = Join-Path $proj 'automute.py'
$venv   = Join-Path $proj '.venv\Scripts'
$py     = Join-Path $venv 'python.exe'

$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'voice-automute.lnk'
if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Output "Removed $lnk" }

# Only processes that (a) run the interpreter from OUR .venv and (b) have our script on the command line.
$ci = [StringComparison]::OrdinalIgnoreCase
$ours = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" | Where-Object {
    $_.ExecutablePath -and $_.CommandLine -and
    $_.ExecutablePath.StartsWith($venv, $ci) -and $_.CommandLine.IndexOf($script, $ci) -ge 0
})
foreach ($p in $ours) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    Wait-Process -Id $p.ProcessId -Timeout 5 -ErrorAction SilentlyContinue
    Write-Output "Stopped pid $($p.ProcessId)"
}
if (-not $ours) { Write-Output "No running watcher found." }

if (Test-Path $py) {
    & $py -I $script --restore
    if ($LASTEXITCODE -eq 0) { Write-Output "Leftover mutes (if any) were undone." }
    else { Write-Output "WARNING: could not confirm that audio was restored (exit $LASTEXITCODE). Check Volume Mixer." }
}
