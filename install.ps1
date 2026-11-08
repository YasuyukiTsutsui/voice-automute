# voice-automute installer (current user only, no admin rights needed).
#   1. creates .venv and installs the pinned, hash-verified dependencies
#   2. verifies the install really works (aborts BEFORE touching the Startup folder otherwise)
#   3. puts a shortcut in the user's Startup folder so the watcher starts at login
#   4. starts the watcher now (a second instance exits immediately: single-instance mutex)
$ErrorActionPreference = 'Stop'
$proj    = $PSScriptRoot
$venvPy  = Join-Path $proj '.venv\Scripts\python.exe'
$venvPyw = Join-Path $proj '.venv\Scripts\pythonw.exe'
$script  = Join-Path $proj 'automute.py'

function Assert-Ok($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" } }

if (-not (Test-Path $venvPy)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv (Join-Path $proj '.venv') } else { & python -m venv (Join-Path $proj '.venv') }
    Assert-Ok 'creating the virtual environment'
}
& $venvPy -m pip install --disable-pip-version-check --isolated --only-binary=:all: --require-hashes -r (Join-Path $proj 'requirements.txt')
Assert-Ok 'pip install'
& $venvPy -I -c "import comtypes, psutil, pycaw; print('dependencies OK')"
Assert-Ok 'dependency check'
& $venvPy -I -m py_compile $script
Assert-Ok 'compiling automute.py'

# -I = isolated mode: ignore PYTHONPATH / user site and do not put the script folder on sys.path
$procArgs = '-I "' + $script + '"'
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'voice-automute.lnk'
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnk)
$sc.TargetPath = $venvPyw
$sc.Arguments = $procArgs
$sc.WorkingDirectory = $proj
$sc.WindowStyle = 7   # minimized
$sc.Description = 'Mute other apps while Claude is recording from the microphone'
$sc.Save()

Start-Process -FilePath $venvPyw -ArgumentList $procArgs -WorkingDirectory $proj -WindowStyle Hidden
Write-Output "Installed. Startup shortcut: $lnk"
Write-Output "Log: $env:LOCALAPPDATA\voice-automute\automute.log"
