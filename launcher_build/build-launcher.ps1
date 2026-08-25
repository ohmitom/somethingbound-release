<#
.SYNOPSIS
    Package the launcher as SomethingBoundLauncher.exe.

.DESCRIPTION
    PyInstaller is a build-time tool only, so it is installed into an isolated
    virtual environment under launcher_build/ rather than into the system
    Python. The launcher itself imports nothing outside the standard library.

    The tests are run first, because a launcher that fails its own update and
    rollback tests should never be packaged.

.EXAMPLE
    ./launcher_build/build-launcher.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $PSScriptRoot '.venv'
$venvPython = Join-Path $venv 'Scripts/python.exe'

function Invoke-Native {
    <#
        Windows PowerShell turns any stderr line from a native executable into
        an ErrorRecord, and with ErrorActionPreference set to Stop that aborts
        the script. Both unittest and PyInstaller report normal progress on
        stderr, so the exit code is the only trustworthy signal here.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Executable @Arguments
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }

    if ($code -ne 0) { throw "$Description failed with exit code $code" }
}

Push-Location $repositoryRoot
try {
    if (-not $SkipTests) {
        Write-Host 'Running the launcher test suite'
        Invoke-Native -Description 'The test suite' -Executable $Python `
            -Arguments @('-m', 'unittest', 'discover', '--start-directory', 'tests')
    }

    if (-not (Test-Path $venvPython)) {
        Write-Host "Creating the build environment at $venv"
        Invoke-Native -Description 'Creating the virtual environment' -Executable $Python `
            -Arguments @('-m', 'venv', $venv)
    }

    Invoke-Native -Description 'Installing PyInstaller' -Executable $venvPython `
        -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet',
                     '--upgrade', 'pip', 'pyinstaller')

    $distDir = Join-Path $PSScriptRoot 'dist'
    $workDir = Join-Path $PSScriptRoot 'work'

    Invoke-Native -Description 'PyInstaller' -Executable $venvPython `
        -Arguments @('-m', 'PyInstaller', '--noconfirm', '--clean',
                     '--distpath', $distDir, '--workpath', $workDir,
                     (Join-Path $PSScriptRoot 'somethingbound-launcher.spec'))

    $exe = Join-Path $distDir 'SomethingBoundLauncher.exe'
    if (-not (Test-Path $exe)) { throw "PyInstaller reported success but $exe does not exist." }

    # Stamp the version the executable was actually built with, so publishing
    # cannot claim a different one and leave every player in an update loop.
    $version = & $Python -c "import release_tools; print(release_tools.__version__)"
    Set-Content -Path ($exe + '.version') -Value $version.Trim() -Encoding ascii -NoNewline

    $size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host ''
    Write-Host "Built $exe ($size MB)"
}
finally {
    Pop-Location
}
