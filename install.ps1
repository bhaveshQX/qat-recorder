# One command per job, for a tester's Windows machine.
#
#   .\install.ps1 local        install the panel and CLI
#   .\install.ps1 uninstall    remove all of it
#   .\install.ps1 doctor       what is installed, what is missing
#
# Options
#   -Wheel FILE    path to qat_recorder-*.whl (default: found next to this script)
#   -Prefix DIR    install location (default: %LOCALAPPDATA%\qatrec)
#   -Yes           do not prompt
#
# Windows is a tester's machine only: the application under test runs on a Linux
# VM, so nothing here needs Qat or the C++ filter. Use install.sh on the VM.

param(
    [Parameter(Position = 0)]
    [ValidateSet('local', 'uninstall', 'doctor')]
    [string]$Command = '',

    [string]$Wheel = '',
    [string]$Prefix = '',
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Prefix) { $Prefix = Join-Path $env:LOCALAPPDATA 'qatrec' }

function Step($text) { Write-Host "`n==> $text" -ForegroundColor White }
function Ok($text)   { Write-Host "    [ok] $text" -ForegroundColor Green }
function Warn($text) { Write-Host "    [!]  $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host "`nerror: $text" -ForegroundColor Red; exit 1 }

function Find-Python {
    foreach ($candidate in 'python', 'python3', 'py') {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($found) {
            $version = & $found.Source -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$version -ge [version]'3.9') {
                return $found.Source
            }
        }
    }
    Fail "Python 3.9 or newer is required. Install it from python.org or the Microsoft Store."
}

function Find-Wheel {
    if ($Wheel) {
        if (-not (Test-Path $Wheel)) { Fail "no such wheel: $Wheel" }
        return (Resolve-Path $Wheel).Path
    }
    $places = @(
        (Join-Path $ScriptDir 'qat_recorder-*.whl'),
        (Join-Path $ScriptDir 'dist\qat_recorder-*.whl'),
        '.\qat_recorder-*.whl',
        '.\dist\qat_recorder-*.whl',
        (Join-Path $env:USERPROFILE 'Downloads\qat_recorder-*.whl')
    )
    foreach ($place in $places) {
        $hit = Get-ChildItem $place -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    Fail "could not find qat_recorder-*.whl - pass it with -Wheel FILE"
}

function Invoke-Local {
    Step 'Machine'
    $os = (Get-CimInstance Win32_OperatingSystem).Caption
    Ok "$os (tester's machine - no Qat, no filter needed here)"
    $python = Find-Python
    Ok "python: $python"
    $wheelPath = Find-Wheel
    Ok "wheel: $wheelPath"

    Step 'Python environment'
    if (-not (Test-Path (Join-Path $Prefix 'Scripts\python.exe'))) {
        & $python -m venv $Prefix
        if ($LASTEXITCODE -ne 0) { Fail "could not create a virtual environment at $Prefix" }
    }
    $venvPython = Join-Path $Prefix 'Scripts\python.exe'
    & $venvPython -m pip install --quiet --upgrade pip
    Ok "environment at $Prefix"

    & $venvPython -m pip install --quiet --force-reinstall $wheelPath
    if ($LASTEXITCODE -ne 0) { Fail "could not install $wheelPath" }
    Ok 'qat_recorder installed (includes FastAPI web panel)'

    Step 'Done'
    Write-Host @"

  Start the web panel:
    $venvPython -m qat_recorder web-panel

  This opens a browser. Paste the agent URL (VM IP or ngrok URL)
  into the Connect bar to start recording.

  Recording is interactive, so you also need to SEE the VM's screen -
  VNC or X forwarding. The panel drives it; it does not display it.

"@
}

function Invoke-Uninstall {
    Step 'Removing'
    # A fixed list, all under the user's own profile. Never derived from any
    # program's output -- the Linux version of this script once removed a path
    # it had asked Qat for, and destroyed a repository.
    $targets = @(
        $Prefix,
        (Join-Path $env:APPDATA 'qat-recorder'),
        (Join-Path $env:USERPROFILE '.qatrec')
    ) | Where-Object { $_ -and $_.StartsWith($env:USERPROFILE) -or $_.StartsWith($env:LOCALAPPDATA) -or $_.StartsWith($env:APPDATA) }

    if (-not $Yes) {
        Write-Host '  This will delete:'
        foreach ($t in $targets) { if (Test-Path $t) { Write-Host "    $t" } }
        $answer = Read-Host '  Continue? [y/N]'
        if ($answer -notmatch '^(y|Y|yes)$') { Write-Host '  cancelled'; return }
    }

    foreach ($t in $targets) {
        if (Test-Path $t) {
            Remove-Item -Recurse -Force $t
            Ok "removed $t"
        }
    }
    Step 'Done'
}

function Invoke-Doctor {
    Step 'Machine'
    Write-Host "    $((Get-CimInstance Win32_OperatingSystem).Caption)"

    Step 'Installed'
    $venvPython = Join-Path $Prefix 'Scripts\python.exe'
    if (Test-Path $venvPython) {
        Ok "environment  $Prefix"
        $version = & $venvPython -c "import qat_recorder; print(qat_recorder.__version__)" 2>$null
        if ($LASTEXITCODE -eq 0) { Ok "qat_recorder $version" } else { Warn 'qat_recorder is NOT installed' }
        & $venvPython -c "import fastapi" 2>$null
        if ($LASTEXITCODE -eq 0) { Ok 'FastAPI (web panel available)' } else { Warn 'FastAPI missing - run: .\install.ps1 local' }
    } else {
        Warn "no environment at $Prefix - run: .\install.ps1 local"
    }

    Step 'Registered hosts'
    if (Test-Path $venvPython) {
        & $venvPython -m qat_recorder hosts list
    }

    Step 'Note'
    Write-Host '    This machine only drives recording. The application, Qat and the'
    Write-Host '    event filter all live on the Linux VM - set that up with install.sh.'
}

switch ($Command) {
    'local'     { Invoke-Local }
    'uninstall' { Invoke-Uninstall }
    'doctor'    { Invoke-Doctor }
    default {
        Get-Content $MyInvocation.MyCommand.Path | Select-Object -First 13 |
            ForEach-Object { $_ -replace '^# ?', '' }
        exit 1
    }
}
